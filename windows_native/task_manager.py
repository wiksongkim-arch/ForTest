"""原生桌面端的持久化生成任务调度器。

该模块只负责任务生命周期、并发上限和本地任务记录；真正的生成逻辑仍由
``backend.api.routes`` 及其业务服务执行，因此不会复制或改变网页版业务逻辑。
"""

from __future__ import annotations

import copy
import inspect
import json
import os
import re
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from time import sleep
from typing import Any, Callable, Protocol
from uuid import uuid4

from filelock import FileLock

from services.requirement_documents import RequirementDocumentSource
from windows_native.errors import friendly_error


_ACTIVE_STATUSES = {"pending", "running"}
_TERMINAL_STATUSES = {
    "completed",
    "partial_failure",
    "failed",
    "stopped",
    "interrupted",
}
_DEFAULT_TASK_NAME = "正在读取需求文档…"
_INTERRUPTED_MESSAGE = "上次运行被意外中断，请重新生成"
_SCHEMA_VERSION = 2
_SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})
_EMBEDDED_LOG_TIME = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s*")
_FILE_ACCESS_RETRIES = 20
_FILE_ACCESS_RETRY_DELAY = 0.025


def _embedded_log_timestamp(message: str, reference: str) -> str | None:
    """用后端日志自带的时分秒恢复真实时间，兼容终态一次性回传日志。"""

    match = _EMBEDDED_LOG_TIME.match(str(message))
    if match is None:
        return None
    try:
        base = datetime.fromisoformat(str(reference))
        candidate = base.replace(
            hour=int(match.group(1)),
            minute=int(match.group(2)),
            second=int(match.group(3)),
            microsecond=0,
        )
    except (TypeError, ValueError):
        return None
    # 任务跨过零点时，终态日期属于次日，而日志时刻可能属于前一日。
    if candidate > base + timedelta(minutes=1):
        candidate -= timedelta(days=1)
    return candidate.isoformat(timespec="seconds")


class _GenerationService(Protocol):
    """调度器所需的最小业务门面，便于纯单元测试。"""

    def start_generation(
        self,
        document_source: str,
        *,
        source_type: str = "link",
    ) -> str: ...

    def task_status(self, task_id: str) -> dict[str, Any]: ...

    def stop_generation(self, task_id: str) -> bool: ...


class _Preferences(Protocol):
    """桌面偏好设置的最小接口。"""

    def get_task_parallelism(self) -> int: ...

    def set_task_parallelism(self, value: int) -> int: ...


class _MemoryPreferences:
    """偏好组件尚未加载时使用的进程内安全后备实现。"""

    def __init__(self) -> None:
        self._value = 1

    def get_task_parallelism(self) -> int:
        return self._value

    def set_task_parallelism(self, value: int) -> int:
        normalized = _validate_parallelism(value)
        self._value = normalized
        return normalized


def _validate_parallelism(value: int) -> int:
    """限制同时执行的重型生成任务数量，避免耗尽本机资源。"""

    if isinstance(value, bool):
        raise ValueError("并行数量必须是整数")
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        raise ValueError("并行数量必须是整数") from None
    if not 1 <= normalized <= 8:
        raise ValueError("并行数量必须在 1 到 8 之间")
    return normalized


class _TaskRepository:
    """使用文件锁与原子替换保存任务，避免崩溃留下半份 JSON。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file_lock = FileLock(str(path) + ".lock")

    @staticmethod
    def _read_text_with_retry(path: Path) -> str:
        """容忍 Windows 杀毒/索引服务造成的短暂共享冲突。"""

        for attempt in range(_FILE_ACCESS_RETRIES):
            try:
                return path.read_text(encoding="utf-8")
            except PermissionError:
                if attempt + 1 == _FILE_ACCESS_RETRIES:
                    raise
                sleep(_FILE_ACCESS_RETRY_DELAY)
        raise AssertionError("unreachable")

    @staticmethod
    def _replace_with_retry(source: Path, destination: Path) -> None:
        """原子替换遇到短暂 Windows 文件占用时重试，避免任务误判失败。"""

        for attempt in range(_FILE_ACCESS_RETRIES):
            try:
                os.replace(source, destination)
                return
            except PermissionError:
                if attempt + 1 == _FILE_ACCESS_RETRIES:
                    raise
                sleep(_FILE_ACCESS_RETRY_DELAY)

    def load(self) -> list[dict[str, Any]]:
        with self._file_lock:
            if not self.path.exists():
                return []
            try:
                payload = json.loads(self._read_text_with_retry(self.path))
                if not isinstance(payload, dict):
                    raise ValueError("任务文件根节点必须是对象")
                version = int(payload.get("schema_version", 0))
                if version not in _SUPPORTED_SCHEMA_VERSIONS:
                    raise ValueError(f"不支持的任务文件版本：{version}")
                tasks = payload.get("tasks", [])
                if not isinstance(tasks, list):
                    raise ValueError("任务列表格式无效")
                return [self._normalize_loaded(item) for item in tasks]
            except PermissionError:
                # 文件仍被外部进程占用时不得把有效数据当作损坏文件隔离或覆盖。
                raise
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                # 损坏文件先改名保留，程序可继续启动且用户仍能人工恢复原数据。
                stamp = datetime.now().strftime("%Y%m%d%H%M%S")
                backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
                suffix = 1
                while backup.exists():
                    backup = self.path.with_name(
                        f"{self.path.name}.corrupt-{stamp}-{suffix}"
                    )
                    suffix += 1
                try:
                    self._replace_with_retry(self.path, backup)
                except OSError:
                    pass
                return []

    def save(self, tasks: list[dict[str, Any]]) -> None:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "tasks": tasks,
        }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        with self._file_lock:
            temporary = self.path.with_name(
                f".{self.path.name}.{uuid4().hex}.tmp"
            )
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._replace_with_retry(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _normalize_loaded(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("任务记录格式无效")
        task_id = str(value.get("task_id") or "")
        if len(task_id) != 14 or not task_id.isdigit():
            raise ValueError("任务编号格式无效")
        status = str(value.get("status") or "failed")
        if status not in _ACTIVE_STATUSES | _TERMINAL_STATUSES:
            status = "failed"
        logs = value.get("logs")
        log_entries = value.get("log_entries")
        result = value.get("result")
        normalized_logs = [str(item) for item in logs] if isinstance(logs, list) else []
        normalized_entries: list[dict[str, str]] = []
        if isinstance(log_entries, list):
            for item in log_entries:
                if not isinstance(item, dict):
                    continue
                message = str(item.get("message") or "").strip()
                if message:
                    normalized_entries.append(
                        {
                            "timestamp": str(item.get("timestamp") or ""),
                            "message": message,
                        }
                    )
        if not normalized_entries and normalized_logs:
            legacy_timestamp = str(
                value.get("updated_at") or value.get("created_at") or ""
            )
            normalized_entries = [
                {"timestamp": legacy_timestamp, "message": message}
                for message in normalized_logs
            ]
        # 旧版本可能在终态把所有接收时间写成完成时刻；启动时按行内时间自动修复。
        reference = str(
            value.get("finished_at")
            or value.get("updated_at")
            or value.get("created_at")
            or ""
        )
        for entry in normalized_entries:
            embedded = _embedded_log_timestamp(entry["message"], reference)
            if embedded:
                entry["timestamp"] = embedded
        source_type = str(value.get("source_type") or "link").strip().casefold()
        if source_type not in {"link", "file"}:
            source_type = "link"
        document_source = str(
            value.get("document_source") or value.get("doc_url") or ""
        ).strip()
        return {
            "task_id": task_id,
            "name": str(value.get("name") or "测试用例"),
            # doc_url 仅供旧扩展读取；新任务统一使用来源类型和地址字段。
            "doc_url": document_source if source_type == "link" else "",
            "source_type": source_type,
            "document_source": document_source,
            "status": status,
            "current_block": max(0, int(value.get("current_block") or 0)),
            "total_blocks": max(0, int(value.get("total_blocks") or 0)),
            "logs": normalized_logs,
            "log_entries": normalized_entries,
            "result": copy.deepcopy(result) if isinstance(result, dict) else None,
            "model_info": (
                copy.deepcopy(value.get("model_info"))
                if isinstance(value.get("model_info"), dict)
                else {}
            ),
            "error": str(value["error"]) if value.get("error") else None,
            "trashed": bool(value.get("trashed", False)),
            "attempt": max(1, int(value.get("attempt") or 1)),
            "created_at": str(value.get("created_at") or ""),
            "started_at": str(value.get("started_at") or "") or None,
            "finished_at": str(value.get("finished_at") or "") or None,
            "updated_at": str(value.get("updated_at") or ""),
            "trashed_at": (
                str(value["trashed_at"]) if value.get("trashed_at") else None
            ),
        }


class TaskManager:
    """持久化、可调整并发上限的先进先出任务调度器。"""

    def __init__(
        self,
        service: _GenerationService,
        data_root: Path,
        preferences: _Preferences | None = None,
        *,
        poll_interval: float = 0.35,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.service = service
        self.data_root = Path(data_root)
        self.preferences = preferences or _default_preferences(self.data_root)
        self._max_parallel = _validate_parallelism(
            self.preferences.get_task_parallelism()
        )
        self._poll_interval = max(0.01, float(poll_interval))
        self._clock = clock or datetime.now
        self._repository = _TaskRepository(
            self.data_root / "data" / "generation_tasks.json"
        )
        self._condition = threading.Condition(threading.RLock())
        self._tasks = {
            item["task_id"]: item for item in self._repository.load()
        }
        self._queue: deque[tuple[str, str]] = deque()
        self._run_tokens: dict[str, str] = {}
        self._backend_task_ids: dict[str, str] = {}
        self._active_workers = 0
        self._dispatcher: threading.Thread | None = None
        self._started = False
        self._stopping = False
        self._repair_interrupted_tasks()

    def start(self) -> None:
        """启动后台调度线程；重复调用不会创建第二个调度器。"""

        with self._condition:
            if self._stopping:
                raise RuntimeError("任务管理器已经停止")
            if self._started:
                return
            self._started = True
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop,
                name="fortester-task-dispatcher",
                daemon=True,
            )
            self._dispatcher.start()

    def stop(self) -> None:
        """停止调度并把未完成任务标记为中断，避免下次启动误判。"""

        dispatcher: threading.Thread | None
        backend_task_ids: list[str]
        with self._condition:
            if self._stopping:
                return
            self._stopping = True
            changed = False
            now = self._iso_now()
            for task in self._tasks.values():
                if task["status"] not in _ACTIVE_STATUSES:
                    continue
                task["status"] = "interrupted"
                task["error"] = _INTERRUPTED_MESSAGE
                self._append_log_locked(task, _INTERRUPTED_MESSAGE, now)
                task["finished_at"] = now
                task["updated_at"] = now
                changed = True
            self._run_tokens.clear()
            self._queue.clear()
            backend_task_ids = list(self._backend_task_ids.values())
            self._backend_task_ids.clear()
            if changed:
                self._persist_locked()
            dispatcher = self._dispatcher
            self._condition.notify_all()
        for backend_task_id in backend_task_ids:
            self._stop_backend_task(backend_task_id)
        if dispatcher is not None and dispatcher is not threading.current_thread():
            dispatcher.join(timeout=1.0)

    def create_task(
        self,
        document_source: str,
        *,
        source_type: str = "link",
    ) -> dict[str, Any]:
        """创建并排队任务；任务编号为十四位本地创建时间。"""

        source = RequirementDocumentSource.create(
            source_type,
            document_source,
        )
        self.start()
        with self._condition:
            task_id = self._next_task_id_locked()
            now = self._iso_now()
            task = {
                "task_id": task_id,
                "name": (
                    Path(source.location).stem
                    if source.source_type == "file"
                    else _DEFAULT_TASK_NAME
                ),
                "doc_url": source.location if source.source_type == "link" else "",
                "source_type": source.source_type,
                "document_source": source.location,
                "status": "pending",
                "current_block": 0,
                "total_blocks": 0,
                "logs": [],
                "log_entries": [],
                "model_info": {},
                "result": None,
                "error": None,
                "trashed": False,
                "attempt": 1,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "updated_at": now,
                "trashed_at": None,
            }
            token = uuid4().hex
            self._tasks[task_id] = task
            self._run_tokens[task_id] = token
            self._queue.append((task_id, token))
            self._persist_locked()
            self._condition.notify_all()
            return copy.deepcopy(task)

    def list_tasks(
        self,
        include_trashed: bool = False,
        only_trashed: bool = False,
    ) -> list[dict[str, Any]]:
        """返回新任务优先的快照；可只读取回收站任务。"""

        with self._condition:
            tasks = [
                copy.deepcopy(item)
                for item in self._tasks.values()
                if (
                    item["trashed"]
                    if only_trashed
                    else include_trashed or not item["trashed"]
                )
            ]
        return sorted(
            tasks,
            key=lambda item: (item["task_id"], item["created_at"]),
            reverse=True,
        )

    def active_count(self) -> int:
        """返回实际占用生成槽位的任务数，回收站中的运行任务也计入。"""

        with self._condition:
            return self._active_workers

    def queued_count(self) -> int:
        """返回尚未取得执行槽位的任务数。"""

        with self._condition:
            return sum(
                1
                for task in self._tasks.values()
                if task["status"] == "pending" and not task["trashed"]
            )

    def retry(self, task_id: str) -> dict[str, Any]:
        """使用执行时的最新配置重新运行同一任务编号。"""

        self.start()
        with self._condition:
            task = self._require_task_locked(task_id)
            if task["trashed"]:
                raise ValueError("请先从回收站恢复任务")
            if task["status"] in _ACTIVE_STATUSES:
                raise ValueError("任务正在执行或排队，不能重复生成")
            token = uuid4().hex
            task.update(
                {
                    "status": "pending",
                    "current_block": 0,
                    "total_blocks": 0,
                    "logs": [],
                    "log_entries": [],
                    "model_info": {},
                    "result": None,
                    "error": None,
                    "attempt": int(task["attempt"]) + 1,
                    "updated_at": self._iso_now(),
                    "started_at": None,
                    "finished_at": None,
                }
            )
            self._run_tokens[task_id] = token
            self._queue.append((task_id, token))
            self._persist_locked()
            self._condition.notify_all()
            return copy.deepcopy(task)

    def stop_task(self, task_id: str) -> dict[str, Any]:
        """停止排队或运行中的单个任务，并立即持久化“已停止”状态。"""

        backend_task_id: str | None = None
        with self._condition:
            task = self._require_task_locked(task_id)
            if task["status"] not in _ACTIVE_STATUSES:
                return copy.deepcopy(task)
            now = self._iso_now()
            task["status"] = "stopped"
            task["error"] = None
            self._append_log_locked(task, "任务已停止", now)
            task["finished_at"] = now
            task["updated_at"] = now
            self._run_tokens.pop(task_id, None)
            backend_task_id = self._backend_task_ids.pop(task_id, None)
            self._persist_locked()
            self._condition.notify_all()
            result = copy.deepcopy(task)
        if backend_task_id is not None:
            # taskkill/SDK 关闭可能需要数秒，后台执行可保证确认框关闭后界面立即响应。
            threading.Thread(
                target=self._stop_and_release_backend_task,
                args=(backend_task_id,),
                name=f"fortester-stop-{task_id}",
                daemon=True,
            ).start()
        return result

    def trash(self, task_id: str) -> dict[str, Any]:
        """软删除任务记录；已开始的远端生成不会被危险地强制终止。"""

        with self._condition:
            task = self._require_task_locked(task_id)
            if not task["trashed"]:
                now = self._iso_now()
                if task["status"] == "pending":
                    # 尚未获得执行槽位的任务不会在回收站中偷偷启动。
                    cancelled = "任务在开始执行前被移入回收站"
                    task["status"] = "interrupted"
                    task["error"] = cancelled
                    self._append_log_locked(task, cancelled, now)
                    task["finished_at"] = now
                    self._run_tokens.pop(task_id, None)
                task["trashed"] = True
                task["trashed_at"] = now
                task["updated_at"] = now
                self._persist_locked()
                self._condition.notify_all()
            return copy.deepcopy(task)

    def restore(self, task_id: str) -> dict[str, Any]:
        """将任务记录移出回收站，不改变任务本身的执行状态。"""

        with self._condition:
            task = self._require_task_locked(task_id)
            if task["trashed"]:
                task["trashed"] = False
                task["trashed_at"] = None
                task["updated_at"] = self._iso_now()
                self._persist_locked()
            return copy.deepcopy(task)

    def get_task(self, task_id: str) -> dict[str, Any]:
        """返回指定任务的防御性副本。"""

        with self._condition:
            return copy.deepcopy(self._require_task_locked(task_id))

    def set_max_parallel(self, value: int) -> int:
        """保存并立即应用新的任务并行上限。"""

        normalized = _validate_parallelism(value)
        saved = _validate_parallelism(
            self.preferences.set_task_parallelism(normalized)
        )
        with self._condition:
            self._max_parallel = saved
            self._condition.notify_all()
        return saved

    def get_max_parallel(self) -> int:
        with self._condition:
            return self._max_parallel

    def _dispatch_loop(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._stopping
                    or (
                        bool(self._queue)
                        and self._active_workers < self._max_parallel
                    )
                )
                if self._stopping:
                    return
                task_id, token = self._queue.popleft()
                task = self._tasks.get(task_id)
                if (
                    task is None
                    or task["status"] != "pending"
                    or self._run_tokens.get(task_id) != token
                ):
                    continue
                task["status"] = "running"
                task["started_at"] = self._iso_now()
                task["finished_at"] = None
                task["updated_at"] = task["started_at"]
                self._active_workers += 1
                self._persist_locked()
                worker = threading.Thread(
                    target=self._run_task,
                    args=(
                        task_id,
                        token,
                        task["source_type"],
                        task["document_source"],
                    ),
                    name=f"fortester-generation-{task_id}",
                    daemon=True,
                )
                try:
                    worker.start()
                except Exception as exc:
                    message = friendly_error(exc)
                    task["status"] = "failed"
                    task["error"] = message
                    self._append_log_locked(task, message)
                    task["finished_at"] = self._iso_now()
                    task["updated_at"] = self._iso_now()
                    self._run_tokens.pop(task_id, None)
                    self._active_workers = max(0, self._active_workers - 1)
                    self._persist_locked()
                    self._condition.notify_all()

    def _run_task(
        self,
        task_id: str,
        token: str,
        source_type: str,
        document_source: str,
    ) -> None:
        backend_task_id: str | None = None
        try:
            self._capture_model_info(task_id, token)
            backend_task_id = self._start_backend_generation(
                document_source,
                source_type,
            )
            with self._condition:
                should_stop = (
                    self._stopping
                    or self._run_tokens.get(task_id) != token
                )
                if not should_stop:
                    self._backend_task_ids[task_id] = backend_task_id
            if should_stop:
                self._stop_backend_task(backend_task_id)
                return
            while not self._stopping:
                with self._condition:
                    if self._run_tokens.get(task_id) != token:
                        return
                snapshot = self.service.task_status(backend_task_id)
                status = str(snapshot.get("status") or "unknown")
                if status == "not_found":
                    self._fail_task(task_id, token, "生成任务已不存在")
                    return
                self._apply_backend_snapshot(task_id, token, snapshot)
                if status in {
                    "completed",
                    "partial_failure",
                    "failed",
                    "stopped",
                }:
                    self._release_backend_task(backend_task_id)
                    return
                sleep(self._poll_interval)
        except BaseException as exc:
            self._fail_task(task_id, token, friendly_error(exc))
        finally:
            with self._condition:
                self._active_workers = max(0, self._active_workers - 1)
                self._backend_task_ids.pop(task_id, None)
                if self._run_tokens.get(task_id) == token:
                    self._run_tokens.pop(task_id, None)
                self._condition.notify_all()

    def _start_backend_generation(
        self,
        document_source: str,
        source_type: str,
    ) -> str:
        """兼容只接受旧 doc_url 参数的链接生成门面。"""

        starter = self.service.start_generation
        try:
            parameters = inspect.signature(starter).parameters.values()
            supports_source_type = any(
                item.name == "source_type"
                or item.kind is inspect.Parameter.VAR_KEYWORD
                for item in parameters
            )
        except (TypeError, ValueError):
            supports_source_type = True
        if supports_source_type:
            return starter(document_source, source_type=source_type)
        if source_type != "link":
            raise RuntimeError("当前生成服务不支持本地需求文档")
        return starter(document_source)

    def _apply_backend_snapshot(
        self,
        task_id: str,
        token: str,
        snapshot: dict[str, Any],
    ) -> None:
        with self._condition:
            task = self._tasks.get(task_id)
            if task is None or self._run_tokens.get(task_id) != token:
                return
            backend_status = str(snapshot.get("status") or "running")
            # 后端 pending 已经占用了桌面调度槽位，桌面统一显示为“执行中”。
            status = "running" if backend_status == "pending" else backend_status
            if status not in {
                "running",
                "completed",
                "partial_failure",
                "failed",
                "stopped",
            }:
                status = "running"
            logs = snapshot.get("logs")
            result = snapshot.get("result")
            name = snapshot.get("task_name")
            error = snapshot.get("error")
            if not error and isinstance(result, dict):
                error = result.get("error")
            if (
                not error
                and status == "failed"
                and isinstance(logs, list)
                and logs
            ):
                # 兼容旧后端仅在日志中发布安全失败说明的状态快照。
                error = logs[-1]
            completed_value = snapshot.get("completed_block")
            if completed_value is None:
                # 兼容尚未提供 completed_block 的旧后端状态。
                completed_value = snapshot.get("current_block")
            if isinstance(result, dict):
                usage = result.get("provider_usage")
                if isinstance(usage, dict):
                    model_info = dict(task.get("model_info") or {})
                    effective_model = str(usage.get("model") or "").strip()
                    if effective_model:
                        model_info["model_version"] = effective_model
                    runtime_mode = str(usage.get("runtime_mode") or "").strip()
                    if runtime_mode:
                        model_info["runtime"] = runtime_mode
                    provider_name = str(usage.get("provider") or "").strip()
                    if provider_name:
                        model_info["provider"] = provider_name
                    task["model_info"] = model_info
            candidate = {
                "status": status,
                "current_block": max(0, int(completed_value or 0)),
                "total_blocks": max(0, int(snapshot.get("total_blocks") or 0)),
                "result": copy.deepcopy(result) if isinstance(result, dict) else None,
                "error": str(error) if error else None,
            }
            backend_logs = [str(item) for item in logs] if isinstance(logs, list) else []
            combined_logs = [*self._model_log_lines(task.get("model_info") or {}), *backend_logs]
            candidate["logs"] = combined_logs
            candidate["log_entries"] = self._merge_log_entries(
                task.get("log_entries") or [],
                combined_logs,
            )
            changed = any(task[key] != value for key, value in candidate.items())
            if isinstance(name, str) and name.strip() and task["name"] != name.strip():
                task["name"] = name.strip()
                changed = True
            if not changed:
                return
            task.update(candidate)
            if status in {
                "completed",
                "partial_failure",
                "failed",
                "stopped",
            }:
                task["finished_at"] = task.get("finished_at") or self._iso_now()
            task["updated_at"] = self._iso_now()
            self._persist_locked()

    def _fail_task(self, task_id: str, token: str, message: str) -> None:
        safe_message = str(message).strip() or "生成失败"
        with self._condition:
            task = self._tasks.get(task_id)
            if task is None or self._run_tokens.get(task_id) != token:
                return
            task["status"] = "failed"
            task["error"] = safe_message
            self._append_log_locked(task, safe_message)
            task["finished_at"] = self._iso_now()
            task["updated_at"] = self._iso_now()
            self._persist_locked()

    def _release_backend_task(self, backend_task_id: str) -> None:
        release = getattr(self.service, "release_generation_task", None)
        if not callable(release):
            return
        try:
            release(backend_task_id)
        except Exception:
            # 清理失败不应覆盖已经持久化的业务结果。
            return

    def _stop_backend_task(self, backend_task_id: str) -> None:
        """调用精确任务中止接口；旧服务未实现时保持向后兼容。"""

        stopper = getattr(self.service, "stop_generation", None)
        if not callable(stopper):
            return
        try:
            stopper(backend_task_id)
        except Exception:
            # 本地状态已经完成收敛，底层关闭异常不能让界面重新回到进行中。
            return

    def _stop_and_release_backend_task(self, backend_task_id: str) -> None:
        """在后台完成中止与快照释放，避免阻塞 Qt 主线程。"""

        self._stop_backend_task(backend_task_id)
        self._release_backend_task(backend_task_id)

    def _repair_interrupted_tasks(self) -> None:
        with self._condition:
            changed = False
            now = self._iso_now()
            for task in self._tasks.values():
                if task["status"] not in _ACTIVE_STATUSES:
                    continue
                task["status"] = "interrupted"
                task["error"] = _INTERRUPTED_MESSAGE
                self._append_log_locked(task, _INTERRUPTED_MESSAGE, now)
                task["finished_at"] = now
                task["updated_at"] = now
                changed = True
            if changed:
                self._persist_locked()

    def _next_task_id_locked(self) -> str:
        candidate_time = self._clock().replace(microsecond=0)
        candidate = candidate_time.strftime("%Y%m%d%H%M%S")
        if candidate not in self._tasks:
            return candidate
        # 同一秒连续创建时使用逻辑上的下一秒，仍保持十四位时间格式与唯一性。
        latest = max(self._tasks)
        try:
            latest_time = datetime.strptime(latest, "%Y%m%d%H%M%S")
        except ValueError:
            latest_time = candidate_time
        if candidate_time.tzinfo is not None and latest_time.tzinfo is None:
            latest_time = latest_time.replace(tzinfo=candidate_time.tzinfo)
        logical_time = max(candidate_time, latest_time) + timedelta(seconds=1)
        while logical_time.strftime("%Y%m%d%H%M%S") in self._tasks:
            logical_time += timedelta(seconds=1)
        return logical_time.strftime("%Y%m%d%H%M%S")

    def _require_task_locked(self, task_id: str) -> dict[str, Any]:
        normalized = str(task_id).strip()
        task = self._tasks.get(normalized)
        if task is None:
            raise KeyError("任务不存在")
        return task

    def _persist_locked(self) -> None:
        self._repository.save(
            [copy.deepcopy(item) for item in self._tasks.values()]
        )

    def _capture_model_info(self, task_id: str, token: str) -> None:
        """在工作线程捕获配置，避免创建任务时阻塞 Qt 界面。"""

        getter = getattr(self.service, "generation_model_info", None)
        if not callable(getter):
            return
        try:
            info = getter()
        except Exception:
            info = {}
        normalized = {
            "provider": str(info.get("provider") or "unknown"),
            "model_name": str(info.get("model_name") or "未知"),
            "model_version": str(info.get("model_version") or "未知"),
            "reasoning_effort": str(info.get("reasoning_effort") or "不适用"),
            "inference_speed": str(info.get("inference_speed") or "standard"),
            "runtime": str(info.get("runtime") or "unknown"),
        }
        with self._condition:
            task = self._tasks.get(task_id)
            if task is None or self._run_tokens.get(task_id) != token:
                return
            task["model_info"] = normalized
            for message in self._model_log_lines(normalized):
                self._append_log_locked(task, message)
            task["updated_at"] = self._iso_now()
            self._persist_locked()

    @staticmethod
    def _model_log_lines(model_info: dict[str, Any]) -> list[str]:
        """生成固定顺序的模型信息日志前缀。"""

        if not model_info:
            return []
        return [
            (
                "模型信息："
                f"{model_info.get('model_name', '未知')} / "
                f"{model_info.get('model_version', '未知')}"
            ),
            f"推理强度：{model_info.get('reasoning_effort', '不适用')}",
            f"推理速度：{model_info.get('inference_speed', 'standard')}",
            f"运行方式：{model_info.get('runtime', 'unknown')}",
        ]

    def _append_log_locked(
        self,
        task: dict[str, Any],
        message: str,
        timestamp: str | None = None,
    ) -> None:
        """同时维护兼容日志列表与带时间戳的结构化日志。"""

        normalized = str(message).strip()
        if not normalized:
            return
        task.setdefault("logs", []).append(normalized)
        task.setdefault("log_entries", []).append(
            {"timestamp": timestamp or self._iso_now(), "message": normalized}
        )

    def _merge_log_entries(
        self,
        existing: list[dict[str, str]],
        messages: list[str],
    ) -> list[dict[str, str]]:
        """保留旧日志时间，只为后端新增行分配接收时间。"""

        merged: list[dict[str, str]] = []
        received_at = self._iso_now()
        for index, message in enumerate(messages):
            embedded = _embedded_log_timestamp(message, received_at)
            if embedded:
                merged.append({"timestamp": embedded, "message": message})
                continue
            old = existing[index] if index < len(existing) else None
            if isinstance(old, dict) and str(old.get("message") or "") == message:
                merged.append(
                    {
                        "timestamp": str(old.get("timestamp") or self._iso_now()),
                        "message": message,
                    }
                )
            else:
                merged.append({"timestamp": received_at, "message": message})
        return merged

    def apply_recovery(self, task_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        """把针对该任务的远端检测恢复结果写回同一条任务记录。"""

        with self._condition:
            task = self._require_task_locked(task_id)
            if task["status"] in _ACTIVE_STATUSES:
                raise ValueError("任务正在执行，不能同时检测恢复")
            result = snapshot.get("result") if isinstance(snapshot, dict) else None
            result = copy.deepcopy(result) if isinstance(result, dict) else {}
            status = str(snapshot.get("status") or "failed")
            if status not in {"completed", "partial_failure", "failed"}:
                status = "failed"
            error = result.get("error") if isinstance(result, dict) else None
            task["status"] = status
            task["result"] = result or None
            task["error"] = str(error) if error else None
            count = int(result.get("test_cases_count") or 0)
            self._append_log_locked(
                task,
                f"检测恢复完成：{count} 条用例"
                if status in {"completed", "partial_failure"}
                else f"检测恢复失败：{error or '未知错误'}",
            )
            recovered_at = self._iso_now()
            # 检测恢复是任务完成后的附加操作，不应拉长原生成任务的耗时。
            task["finished_at"] = task.get("finished_at") or recovered_at
            task["updated_at"] = recovered_at
            self._persist_locked()
            return copy.deepcopy(task)

    def _iso_now(self) -> str:
        return self._clock().astimezone().isoformat(timespec="seconds")


def _default_preferences(data_root: Path) -> _Preferences:
    """优先复用统一桌面偏好组件，缺失时保持默认并行数 1。"""

    try:
        from windows_native.desktop_preferences import DesktopPreferences

        return DesktopPreferences(data_root)
    except (ImportError, OSError, TypeError, ValueError):
        return _MemoryPreferences()
