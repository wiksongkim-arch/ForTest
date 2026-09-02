"""ForTest 桌面进程生命周期诊断。

日志只记录程序自身生成的状态和经过脱敏的异常，不采集业务输入、配置值、
命令行正文或环境变量。活动会话标记在正常退出时删除；进程崩溃、被强制结束
或断电时会保留下来，供下次启动明确标记为非正常结束。
"""

from __future__ import annotations

import atexit
import faulthandler
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable
from uuid import uuid4

from backend.security.redaction import redact_log_text


_MAX_LOG_BYTES = 2 * 1024 * 1024
_MAX_LOG_BACKUPS = 3
_MAX_TEXT_LENGTH = 16_000
_ACTIVE_LOCK = threading.RLock()
_ACTIVE_LIFECYCLE: "LifecycleDiagnostics | None" = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_text(value: object, *, limit: int = _MAX_TEXT_LENGTH) -> str:
    """脱敏并限制诊断文本，避免异常正文把凭据或大对象带入日志。"""

    safe = redact_log_text(str(value or ""))
    if len(safe) <= limit:
        return safe
    return safe[:limit] + "...[truncated]"


def _safe_value(value: object) -> object:
    """把受控诊断字段规范化为有界 JSON 值。"""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe_text(value, limit=2_000)
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value[:32]]
    if isinstance(value, dict):
        return {
            _safe_text(key, limit=100): _safe_value(item)
            for key, item in list(value.items())[:32]
        }
    return _safe_text(type(value).__name__, limit=200)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


class LifecycleDiagnostics:
    """记录一次桌面进程从启动到退出的最小、可审计诊断轨迹。"""

    def __init__(
        self,
        data_root: Path,
        *,
        version: str,
        diagnostic_mode: bool = False,
        enable_exception_hooks: bool = True,
        enable_fatal_handler: bool = True,
    ) -> None:
        self.data_root = Path(data_root)
        self.version = str(version)
        self.diagnostic_mode = bool(diagnostic_mode)
        self.log_path = self.data_root / "logs" / "lifecycle.jsonl"
        self.fatal_log_path = self.data_root / "logs" / "fatal-python.log"
        self.marker_path = self.data_root / "data" / "active-session.json"
        self.run_id = uuid4().hex
        self.pid = os.getpid()
        self._enable_exception_hooks = bool(enable_exception_hooks)
        self._enable_fatal_handler = bool(enable_fatal_handler)
        self._lock = threading.RLock()
        self._started = False
        self._finished = False
        self._started_monotonic = time.monotonic()
        self._exit_reason: str | None = None
        self._marker_payload: dict[str, Any] | None = None
        self._heartbeat_failure_recorded = False
        self._fatal_stream = None
        self._owns_fatal_handler = False
        self._atexit_registered = False
        self._previous_sys_excepthook: Callable[..., Any] | None = None
        self._previous_threading_excepthook: Callable[..., Any] | None = None
        self._previous_unraisablehook: Callable[..., Any] | None = None

    @property
    def exit_reason(self) -> str | None:
        with self._lock:
            return self._exit_reason

    def start(self, *, launch_mode: str) -> dict[str, Any] | None:
        """开始记录，并返回上次遗留的活动会话（若有）。"""

        with self._lock:
            if self._started:
                return None
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.marker_path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_log_if_needed()
            previous = _read_json_object(self.marker_path)
            self._started = True

            if previous is not None:
                self.record(
                    "previous_run_unclean",
                    previous_run_id=str(previous.get("run_id") or ""),
                    previous_pid=_safe_int(previous.get("pid")),
                    previous_started_at=str(previous.get("started_at") or ""),
                    previous_last_seen_at=str(previous.get("last_seen_at") or ""),
                    previous_version=str(previous.get("version") or ""),
                    previous_application_state=str(
                        previous.get("application_state") or ""
                    ),
                    previous_window_visible=previous.get("window_visible"),
                )

            started_at = _utc_now()
            self._marker_payload = {
                "run_id": self.run_id,
                "pid": self.pid,
                "started_at": started_at,
                "last_seen_at": started_at,
                "version": self.version,
                "launch_mode": str(launch_mode),
            }
            _atomic_write_json(self.marker_path, self._marker_payload)
            self.record(
                "run_started",
                started_at=started_at,
                version=self.version,
                launch_mode=str(launch_mode),
                frozen=bool(getattr(sys, "frozen", False)),
                diagnostic_mode=self.diagnostic_mode,
            )
            self._install_hooks()
            self._install_fatal_handler()
            atexit.register(self._on_atexit)
            self._atexit_registered = True
            return previous

    def heartbeat(self, **state: object) -> None:
        """刷新最后存活时间和少量窗口状态，不向主日志写入周期噪声。"""

        with self._lock:
            if not self._started or self._finished or self._marker_payload is None:
                return
            payload = dict(self._marker_payload)
            payload["last_seen_at"] = _utc_now()
            payload.update(
                {
                    _safe_text(key, limit=100): _safe_value(value)
                    for key, value in state.items()
                }
            )
            try:
                _atomic_write_json(self.marker_path, payload)
            except OSError:
                if not self._heartbeat_failure_recorded:
                    self._heartbeat_failure_recorded = True
                    self.record("lifecycle_heartbeat_write_failed")
                return
            self._marker_payload = payload

    def record(self, event: str, **fields: object) -> None:
        """追加一条结构化事件；日志失败不得反向影响主程序。"""

        with self._lock:
            if not self._started or self._finished:
                return
            payload: dict[str, object] = {
                "timestamp": _utc_now(),
                "event": _safe_text(event, limit=120),
                "run_id": self.run_id,
                "pid": self.pid,
                "thread": threading.current_thread().name,
            }
            payload.update(
                {
                    _safe_text(key, limit=100): _safe_value(value)
                    for key, value in fields.items()
                }
            )
            try:
                with self.log_path.open("a", encoding="utf-8", newline="\n") as stream:
                    json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
            except (OSError, TypeError, ValueError):
                return

    def record_exception(
        self,
        event: str,
        exception_type: type[BaseException],
        exception: BaseException,
        exception_traceback,
        *,
        context: str,
    ) -> None:
        """记录已脱敏的 Python 异常类型、上下文和有界堆栈。"""

        try:
            rendered = "".join(
                traceback.format_exception(
                    exception_type,
                    exception,
                    exception_traceback,
                )
            )
        except Exception:
            rendered = f"{exception_type.__name__}: <traceback unavailable>"
        self.record(
            event,
            context=context,
            exception_type=exception_type.__name__,
            message=_safe_text(exception, limit=2_000),
            traceback=_safe_text(rendered),
        )

    def request_exit(self, reason: str, **fields: object) -> str:
        """登记退出请求；首个原因是根因，后续请求仍作为补充事件保留。"""

        normalized = _safe_text(reason, limit=120).strip() or "unknown"
        with self._lock:
            first = self._exit_reason is None
            if first:
                self._exit_reason = normalized
            root_reason = self._exit_reason
        self.record(
            "exit_requested",
            reason=normalized,
            root_reason=root_reason,
            first_request=first,
            **fields,
        )
        return str(root_reason)

    def finish(self, *, exit_code: int, fallback_reason: str) -> None:
        """记录正常收尾并仅删除属于本次运行的活动标记。"""

        with self._lock:
            if not self._started or self._finished:
                return
        if self.exit_reason is None:
            self.request_exit(fallback_reason)
        self.record(
            "run_finished",
            exit_code=int(exit_code),
            reason=self.exit_reason or "unknown",
            uptime_seconds=round(time.monotonic() - self._started_monotonic, 3),
        )
        with self._lock:
            self._remove_own_marker()
            self._finished = True
            self._restore_hooks()
            self._close_fatal_handler()
            if self._atexit_registered:
                try:
                    atexit.unregister(self._on_atexit)
                except Exception:
                    pass
                self._atexit_registered = False
        deactivate_lifecycle(self)

    def _rotate_log_if_needed(self) -> None:
        try:
            if not self.log_path.is_file() or self.log_path.stat().st_size < _MAX_LOG_BYTES:
                return
            oldest = self.log_path.with_name(
                f"{self.log_path.name}.{_MAX_LOG_BACKUPS}"
            )
            oldest.unlink(missing_ok=True)
            for index in range(_MAX_LOG_BACKUPS - 1, 0, -1):
                source = self.log_path.with_name(f"{self.log_path.name}.{index}")
                destination = self.log_path.with_name(
                    f"{self.log_path.name}.{index + 1}"
                )
                if source.exists():
                    os.replace(source, destination)
            os.replace(
                self.log_path,
                self.log_path.with_name(f"{self.log_path.name}.1"),
            )
        except OSError:
            return

    def _install_hooks(self) -> None:
        if not self._enable_exception_hooks:
            return
        self._previous_sys_excepthook = sys.excepthook

        def sys_hook(exception_type, exception, exception_traceback) -> None:
            self.record_exception(
                "uncaught_exception",
                exception_type,
                exception,
                exception_traceback,
                context="main_thread",
            )
            previous = self._previous_sys_excepthook
            if callable(previous):
                previous(exception_type, exception, exception_traceback)

        sys.excepthook = sys_hook

        previous_thread_hook = getattr(threading, "excepthook", None)
        self._previous_threading_excepthook = previous_thread_hook
        if callable(previous_thread_hook):

            def thread_hook(arguments) -> None:
                self.record_exception(
                    "uncaught_thread_exception",
                    arguments.exc_type,
                    arguments.exc_value,
                    arguments.exc_traceback,
                    context=str(getattr(arguments.thread, "name", "worker")),
                )
                previous_thread_hook(arguments)

            threading.excepthook = thread_hook

        previous_unraisable_hook = getattr(sys, "unraisablehook", None)
        self._previous_unraisablehook = previous_unraisable_hook
        if callable(previous_unraisable_hook):

            def unraisable_hook(arguments) -> None:
                exception = arguments.exc_value or RuntimeError("unraisable exception")
                self.record_exception(
                    "unraisable_exception",
                    arguments.exc_type or type(exception),
                    exception,
                    arguments.exc_traceback,
                    context="object_finalizer",
                )
                previous_unraisable_hook(arguments)

            sys.unraisablehook = unraisable_hook

    def _restore_hooks(self) -> None:
        if self._previous_sys_excepthook is not None:
            sys.excepthook = self._previous_sys_excepthook
            self._previous_sys_excepthook = None
        if self._previous_threading_excepthook is not None:
            threading.excepthook = self._previous_threading_excepthook
            self._previous_threading_excepthook = None
        if self._previous_unraisablehook is not None:
            sys.unraisablehook = self._previous_unraisablehook
            self._previous_unraisablehook = None

    def _install_fatal_handler(self) -> None:
        if not self._enable_fatal_handler:
            return
        # 测试器或宿主已安装处理器时不改写其目标文件。
        if faulthandler.is_enabled():
            self.record("fatal_handler_preserved")
            return
        try:
            self._fatal_stream = self.fatal_log_path.open(
                "a",
                encoding="utf-8",
                buffering=1,
            )
            self._fatal_stream.write(
                f"\n[{_utc_now()}] run_id={self.run_id} pid={self.pid}\n"
            )
            self._fatal_stream.flush()
            faulthandler.enable(file=self._fatal_stream, all_threads=True)
            self._owns_fatal_handler = True
            self.record("fatal_handler_enabled")
        except (OSError, RuntimeError, ValueError):
            self._close_fatal_handler()
            self.record("fatal_handler_unavailable")

    def _close_fatal_handler(self) -> None:
        if self._owns_fatal_handler:
            try:
                faulthandler.disable()
            except RuntimeError:
                pass
            self._owns_fatal_handler = False
        if self._fatal_stream is not None:
            try:
                self._fatal_stream.close()
            except OSError:
                pass
            self._fatal_stream = None

    def _remove_own_marker(self) -> None:
        current = _read_json_object(self.marker_path)
        if current is None or str(current.get("run_id") or "") != self.run_id:
            self.record("active_marker_not_owned")
            return
        try:
            self.marker_path.unlink(missing_ok=True)
        except OSError:
            self.record("active_marker_remove_failed")

    def _on_atexit(self) -> None:
        with self._lock:
            if not self._started or self._finished:
                return
        # 不删除标记：解释器绕过主事件循环收尾也应在下次启动显示为异常结束。
        if self.exit_reason is None:
            self.request_exit("python_atexit_without_normal_finish")
        self.record(
            "python_atexit_without_normal_finish",
            reason=self.exit_reason or "unknown",
        )
        self._restore_hooks()
        self._close_fatal_handler()


def activate_lifecycle(diagnostics: LifecycleDiagnostics) -> None:
    global _ACTIVE_LIFECYCLE
    with _ACTIVE_LOCK:
        _ACTIVE_LIFECYCLE = diagnostics


def deactivate_lifecycle(diagnostics: LifecycleDiagnostics) -> None:
    global _ACTIVE_LIFECYCLE
    with _ACTIVE_LOCK:
        if _ACTIVE_LIFECYCLE is diagnostics:
            _ACTIVE_LIFECYCLE = None


def lifecycle_event(event: str, **fields: object) -> None:
    with _ACTIVE_LOCK:
        diagnostics = _ACTIVE_LIFECYCLE
    if diagnostics is not None:
        diagnostics.record(event, **fields)


def request_application_exit(reason: str, **fields: object) -> str:
    with _ACTIVE_LOCK:
        diagnostics = _ACTIVE_LIFECYCLE
    if diagnostics is None:
        return str(reason)
    return diagnostics.request_exit(reason, **fields)


def current_exit_reason() -> str | None:
    with _ACTIVE_LOCK:
        diagnostics = _ACTIVE_LIFECYCLE
    return diagnostics.exit_reason if diagnostics is not None else None
