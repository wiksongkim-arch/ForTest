"""迭代部署任务的本地持久化模型。"""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from windows_native.jenkins.scheduling import normalize_schedule
from windows_native.jenkins.storage import AtomicJsonStore


_ACTIVE_STATUSES = frozenset({"queued", "scheduled", "running", "stopping"})


class DeploymentTaskRepository:
    """保存部署计划与 Jenkins 关联信息，删除操作只设置回收站标记。"""

    def __init__(self, data_root: Path, *, clock=None):
        self._store = AtomicJsonStore(
            Path(data_root) / "data" / "jenkins_deployment_tasks.json"
        )
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._lock = threading.RLock()

    def create(
        self,
        iteration_name: str,
        selections: list[dict[str, Any]],
        *,
        deployment_type: str = "iteration",
        schedule: dict[str, Any] | None = None,
        start_immediately: bool = True,
    ) -> dict:
        name = str(iteration_name or "").strip()
        if not name:
            raise ValueError("请输入迭代名称")
        normalized = self._normalize_selections(selections)
        if not normalized:
            raise ValueError("请至少选择一个环境、项目和分支")
        normalized_type = str(deployment_type or "iteration").strip().casefold()
        if normalized_type not in {"iteration", "single"}:
            raise ValueError("部署任务类型无效")
        if normalized_type == "single" and len(normalized) != 1:
            raise ValueError("单点部署只能选择一个环境、项目和分支")
        with self._lock:
            tasks = self._read_tasks()
            task_id = self._next_task_id(tasks)
            now_value = self._clock()
            now = now_value.isoformat(timespec="seconds")
            normalized_schedule = normalize_schedule(schedule, now=now_value)
            items = self.runtime_items(task_id, normalized, execution_number=1)
            scheduled = bool(normalized_schedule.get("enabled"))
            if not start_immediately:
                creation_message = f"部署任务已保存，共 {len(items)} 个子任务，尚未触发部署"
            elif scheduled:
                creation_message = (
                    f"定时部署任务已创建，共 {len(items)} 个子任务；"
                    f"首次执行时间 {normalized_schedule['next_run_at']}"
                )
            else:
                creation_message = f"部署任务已创建，共 {len(items)} 个子任务"
            task = {
                "schema_version": 3,
                "task_id": task_id,
                "iteration_name": name,
                "deployment_type": normalized_type,
                "status": (
                    "saved"
                    if not start_immediately
                    else "scheduled"
                    if scheduled
                    else "queued"
                ),
                "orchestration_mode": "direct_parallel_subtasks",
                "current_step": 0,
                "total_steps": len(items),
                "progress_percent": 0,
                "items": items,
                "template_items": copy.deepcopy(normalized),
                "created_at": now,
                "updated_at": now,
                "started_at": "",
                "finished_at": "",
                "duration_seconds": 0,
                "error": "",
                "stop_requested": False,
                "trashed": False,
                "schedule": normalized_schedule,
                "execution_number": 0,
                "active_run_id": "",
                "execution_runs": [],
                "logs": [creation_message],
                "log_entries": [
                    {
                        "timestamp": now,
                        "message": creation_message,
                    }
                ],
            }
            tasks.append(task)
            self._write_tasks(tasks)
            return copy.deepcopy(task)

    def redeploy(
        self,
        task_id: str,
        selections: list[dict[str, Any]],
    ) -> dict:
        """在原任务上启动下一批部署，任务 ID 和所有已归档批次保持不变。"""

        normalized = self._normalize_selections(selections)
        if not normalized:
            raise ValueError("请至少选择一个环境、项目和分支")

        def mutate(task: dict) -> dict:
            previous_status = str(task.get("status") or "")
            if previous_status in _ACTIVE_STATUSES:
                raise ValueError("进行中的任务不能重新部署")

            # 兼容升级前没有 execution_runs 的已结束任务，覆盖运行态前先归档快照。
            runs = [
                copy.deepcopy(item)
                for item in task.get("execution_runs") or []
                if isinstance(item, dict)
            ]
            if not runs and task.get("items") and (
                task.get("started_at") or int(task.get("execution_number") or 0)
            ):
                runs = self._history(task)[2]
            execution_number = max(
                [
                    int(task.get("execution_number") or 0),
                    *(int(item.get("execution_number") or 0) for item in runs),
                ]
            )

            schedule = task.get("schedule") or {}
            if previous_status == "saved" and bool(schedule.get("enabled")):
                # 仅保存的定时任务首次点击“部署”时才正式激活计划。
                schedule = normalize_schedule(schedule, now=self._clock())
                next_status = "scheduled"
            else:
                schedule = normalize_schedule(None, now=self._clock())
                next_status = "queued"

            next_execution = execution_number + 1
            now = self._clock().isoformat(timespec="seconds")
            task.update(
                {
                    "schema_version": 3,
                    "status": next_status,
                    "orchestration_mode": "direct_parallel_subtasks",
                    "template_items": copy.deepcopy(normalized),
                    "items": self.runtime_items(
                        str(task.get("task_id") or ""),
                        normalized,
                        execution_number=next_execution,
                    ),
                    "current_step": 0,
                    "total_steps": len(normalized),
                    "progress_percent": 0,
                    "started_at": "",
                    "finished_at": "",
                    "duration_seconds": 0,
                    "error": "",
                    "stop_requested": False,
                    "active_run_id": "",
                    "execution_number": execution_number,
                    "execution_runs": runs,
                    "schedule": schedule,
                    "submission_finished": False,
                }
            )
            task.pop("active_run_log_start", None)
            message = (
                f"已触发首次部署，任务 ID 保持不变"
                if execution_number == 0
                else f"已请求第 {next_execution} 次部署，历史部署记录已保留"
            )
            task.setdefault("logs", []).append(message)
            task.setdefault("log_entries", []).append(
                {"timestamp": now, "message": message}
            )
            return task

        return self.update(task_id, mutate)

    def list(self, *, trashed: bool = False) -> list[dict]:
        with self._lock:
            tasks = [
                copy.deepcopy(item)
                for item in self._read_tasks()
                if bool(item.get("trashed")) is bool(trashed)
            ]
        tasks.sort(key=lambda item: str(item.get("task_id") or ""), reverse=True)
        return tasks

    def get(self, task_id: str) -> dict | None:
        normalized = str(task_id or "").strip()
        with self._lock:
            task = next(
                (
                    item
                    for item in self._read_tasks()
                    if str(item.get("task_id") or "") == normalized
                ),
                None,
            )
            return copy.deepcopy(task) if task is not None else None

    def update(self, task_id: str, mutator) -> dict:
        normalized = str(task_id or "").strip()
        with self._lock:
            tasks = self._read_tasks()
            for index, task in enumerate(tasks):
                if str(task.get("task_id") or "") != normalized:
                    continue
                candidate = mutator(copy.deepcopy(task))
                if not isinstance(candidate, dict):
                    raise TypeError("任务更新函数必须返回字典")
                candidate["updated_at"] = self._clock().isoformat(timespec="seconds")
                tasks[index] = candidate
                self._write_tasks(tasks)
                return copy.deepcopy(candidate)
        raise KeyError("部署任务不存在")

    def trash(self, task_id: str) -> dict:
        def mutate(task: dict) -> dict:
            if str(task.get("status")) in _ACTIVE_STATUSES:
                raise ValueError("进行中的部署任务不能删除，请先停止任务")
            task["trashed"] = True
            return task

        return self.update(task_id, mutate)

    def restore(self, task_id: str) -> dict:
        return self.update(
            task_id,
            lambda task: {**task, "trashed": False},
        )

    def active_count(self) -> int:
        return sum(
            str(item.get("status")) in _ACTIVE_STATUSES
            for item in self.list(trashed=False)
        )

    def queued_count(self) -> int:
        return sum(
            str(item.get("status")) == "queued"
            for item in self.list(trashed=False)
        )

    def scheduled_count(self) -> int:
        """只统计仍在等待或执行的定时计划，历史定时任务不占概览数量。"""

        return sum(
            bool((item.get("schedule") or {}).get("enabled"))
            and str((item.get("schedule") or {}).get("state"))
            in {"waiting", "running"}
            and str(item.get("status"))
            in {"scheduled", "running", "stopping"}
            for item in self.list(trashed=False)
        )

    def _read_tasks(self) -> list[dict]:
        value = self._store.read()
        return [
            item
            for item in value.get("tasks") or []
            if isinstance(item, dict)
        ]

    def _write_tasks(self, tasks: list[dict]) -> None:
        self._store.write({"schema_version": 3, "tasks": tasks})

    def _next_task_id(self, tasks: list[dict]) -> str:
        candidate_time = self._clock().replace(microsecond=0)
        existing = {str(item.get("task_id") or "") for item in tasks}
        while candidate_time.strftime("%Y%m%d%H%M%S") in existing:
            candidate_time += timedelta(seconds=1)
        return candidate_time.strftime("%Y%m%d%H%M%S")

    @staticmethod
    def _normalize_selections(values: list[dict[str, Any]]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for value in values or []:
            if not isinstance(value, dict):
                continue
            environment = str(value.get("environment") or "").strip()
            full_name = str(value.get("project_full_name") or "").strip()
            branch = str(value.get("branch") or "").strip()
            if not environment or not full_name or not branch:
                raise ValueError("每一行都必须选择环境、项目和分支")
            key = (environment.casefold(), full_name.casefold())
            if key in seen:
                raise ValueError(f"同一环境下不能重复选择项目：{full_name}")
            seen.add(key)
            normalized.append(
                {
                    "environment": environment,
                    "project_full_name": full_name,
                    "project_name": str(value.get("project_name") or full_name.rsplit("/", 1)[-1]),
                    "description": str(value.get("description") or "").strip(),
                    "branch": branch,
                }
            )
        return normalized

    @staticmethod
    def runtime_items(
        task_id: str,
        selections: list[dict[str, Any]],
        *,
        execution_number: int,
    ) -> list[dict[str, Any]]:
        """为每次真实部署创建全新运行态，同时保留稳定的项目选择模板。"""

        run_suffix = "" if execution_number <= 1 else f"-r{execution_number:03d}"
        return [
            {
                **copy.deepcopy(item),
                # 子任务标识独立于项目名和执行批次，防止多次部署关联错 Jenkins 构建。
                "subtask_id": f"{task_id}{run_suffix}-{index:03d}",
                "status": "pending",
                "queue_id": None,
                "queue_url": "",
                "queue_reason": "",
                "build_number": None,
                "build_url": "",
                "started_at": "",
                "finished_at": "",
                "duration_seconds": 0,
                "error": "",
            }
            for index, item in enumerate(selections, start=1)
        ]

    @staticmethod
    def _history(
        source: dict[str, Any] | None,
    ) -> tuple[list[dict[str, str]], list[str], list[dict[str, Any]]]:
        """重新部署时复制历史；深拷贝保证来源任务永远不会被后续写入污染。"""

        if not isinstance(source, dict):
            return [], [], []
        entries = [
            copy.deepcopy(item)
            for item in source.get("log_entries") or []
            if isinstance(item, dict)
        ]
        logs = [str(item) for item in source.get("logs") or [] if str(item)]
        runs = [
            copy.deepcopy(item)
            for item in source.get("execution_runs") or []
            if isinstance(item, dict)
        ]
        # schema 2 任务还没有批次归档；重新部署时把其最终快照补成第一批历史。
        if not runs and source.get("items"):
            runs.append(
                {
                    "run_id": f"{source.get('task_id')}-run-001",
                    "execution_number": 1,
                    "scheduled_for": "",
                    "started_at": str(source.get("started_at") or ""),
                    "finished_at": str(source.get("finished_at") or ""),
                    "duration_seconds": int(source.get("duration_seconds") or 0),
                    "status": str(source.get("status") or "unknown"),
                    "items": copy.deepcopy(source.get("items") or []),
                }
            )
        return entries, logs, runs
