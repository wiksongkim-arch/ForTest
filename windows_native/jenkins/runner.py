"""迭代任务串行、任务内 Jenkins 子任务并行提交的编排器。"""

from __future__ import annotations

import copy
import threading
from datetime import datetime
from typing import Any, Callable

from windows_native.jenkins.errors import JenkinsError
from windows_native.jenkins.scheduling import (
    next_schedule_occurrence,
    parse_schedule_datetime,
)
from windows_native.jenkins.tasks import DeploymentTaskRepository


_TERMINAL_ITEM_STATUSES = frozenset({"completed", "failed", "stopped"})
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "stopped"})


class DeploymentTaskRunner:
    """任务之间保持串行，同一任务的子任务全部提交后并行跟踪。"""

    def __init__(
        self,
        repository: DeploymentTaskRepository,
        client_provider: Callable[[], Any],
        *,
        poll_interval: float = 1.2,
        clock=None,
    ) -> None:
        self.repository = repository
        self.client_provider = client_provider
        self.poll_interval = max(0.01, float(poll_interval))
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._shutdown = threading.Event()
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._shutdown.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="jenkins-deployment-runner",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """只停止本地跟踪线程，不自动终止 Jenkins 正在运行的构建。"""

        self._shutdown.set()
        with self._condition:
            self._condition.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        self._thread = None

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def _loop(self) -> None:
        while not self._shutdown.is_set():
            task = self._next_task()
            if task is None:
                with self._condition:
                    self._condition.wait(timeout=self.poll_interval)
                continue
            try:
                self._execute(str(task["task_id"]))
            except Exception as exc:
                if not self._shutdown.is_set():
                    self._fail_task(str(task["task_id"]), str(exc))

    def _next_task(self) -> dict | None:
        tasks = sorted(
            self.repository.list(trashed=False),
            key=lambda item: str(item.get("task_id") or ""),
        )
        # 重启后先恢复上一项活动任务，只有它进入终态后才开始下一迭代。
        for status in ("running", "stopping"):
            task = next(
                (item for item in tasks if str(item.get("status")) == status),
                None,
            )
            if task is not None:
                return task

        # 等待中的定时任务不占队列；到期时优先于尚未开始的普通任务，减少触发漂移。
        now = self._now_value()
        for task in tasks:
            if str(task.get("status")) != "scheduled":
                continue
            schedule = task.get("schedule") or {}
            try:
                end = parse_schedule_datetime(schedule.get("end_at"))
                due = parse_schedule_datetime(schedule.get("next_run_at"))
            except ValueError:
                self._fail_task(str(task.get("task_id") or ""), "定时部署配置无效")
                continue
            localized_now = now.astimezone(end.tzinfo)
            if localized_now.replace(second=0, microsecond=0) > end:
                self.repository.update(
                    str(task.get("task_id") or ""),
                    self._expire_schedule,
                )
                continue
            if localized_now >= due:
                return task

        task = next(
            (item for item in tasks if str(item.get("status")) == "queued"),
            None,
        )
        if task is not None:
            return task
        return None

    def _execute(self, task_id: str) -> None:
        task = self.repository.get(task_id)
        if task is None:
            return
        if task.get("stop_requested"):
            self._stop_remote_and_finish(task)
            return
        if str(task.get("status")) in {"queued", "scheduled"}:
            task = self.repository.update(task_id, self._mark_running)
        client = self.client_provider()

        # 先把所有尚未提交的子任务送进 Jenkins，再开始任何 queue/build 轮询。
        self._submit_pending_items(task_id, client)
        while not self._shutdown.is_set():
            task = self.repository.get(task_id)
            if task is None:
                return
            if task.get("stop_requested"):
                self._stop_remote_and_finish(task, client=client)
                return
            task = self.repository.update(task_id, self._refresh_progress)
            if str(task.get("status")) in _TERMINAL_TASK_STATUSES | {"scheduled"}:
                return
            self._monitor_items_once(task_id, client)
            task = self.repository.update(task_id, self._refresh_progress)
            if str(task.get("status")) in _TERMINAL_TASK_STATUSES | {"scheduled"}:
                return
            if self._shutdown.wait(self.poll_interval):
                return

    def _submit_pending_items(self, task_id: str, client: Any) -> None:
        initial = self.repository.get(task_id) or {}
        item_count = len(initial.get("items") or [])
        for index in range(item_count):
            if self._shutdown.is_set():
                return
            task = self.repository.get(task_id)
            if task is None:
                return
            if task.get("stop_requested"):
                self._stop_remote_and_finish(task, client=client)
                return
            item = task["items"][index]
            if (
                str(item.get("status")) in _TERMINAL_ITEM_STATUSES
                or item.get("queue_id") is not None
                or item.get("build_number") is not None
            ):
                continue
            full_name = str(item.get("project_full_name") or "")
            environment = str(item.get("environment") or "")
            branch = str(item.get("branch") or "")
            try:
                project = client.project_details(full_name)
                if environment not in project.environments:
                    raise JenkinsError(
                        f"项目 {full_name} 已不再支持环境 {environment}",
                        code="environment_changed",
                    )
                if branch not in project.target_branches:
                    raise JenkinsError(
                        f"项目 {full_name} 的分支已不存在：{branch}",
                        code="branch_changed",
                    )
                queued = client.trigger_build(
                    full_name,
                    environment=environment,
                    branch=branch,
                )
            except Exception as exc:
                self.repository.update(
                    task_id,
                    lambda value, item_index=index, message=str(exc): self._fail_item(
                        value,
                        item_index,
                        message,
                    ),
                )
                continue
            self.repository.update(
                task_id,
                lambda value, item_index=index, result=queued: self._record_queue(
                    value,
                    item_index,
                    result,
                ),
            )

        self.repository.update(task_id, self._mark_submission_finished)

    def _monitor_items_once(self, task_id: str, client: Any) -> None:
        task = self.repository.get(task_id) or {}
        for index in range(len(task.get("items") or [])):
            if self._shutdown.is_set():
                return
            current = self.repository.get(task_id)
            if current is None:
                return
            if current.get("stop_requested"):
                self._stop_remote_and_finish(current, client=client)
                return
            item = current["items"][index]
            if str(item.get("status")) in _TERMINAL_ITEM_STATUSES:
                continue
            full_name = str(item.get("project_full_name") or "")
            try:
                if item.get("build_number") is None:
                    if item.get("queue_id") is None:
                        raise JenkinsError(
                            f"子任务 {self._subtask_id(current, index)} 缺少 Jenkins 队列关联",
                            code="missing_queue_association",
                        )
                    queue = client.queue_item(int(item["queue_id"]))
                    if queue.get("cancelled"):
                        raise JenkinsError(
                            f"项目 {full_name} 的 Jenkins 排队任务已取消",
                            code="queue_cancelled",
                        )
                    if queue.get("build_number") is not None:
                        self.repository.update(
                            task_id,
                            lambda value, item_index=index, result=queue: self._record_build(
                                value,
                                item_index,
                                result,
                            ),
                        )
                    else:
                        self.repository.update(
                            task_id,
                            lambda value, item_index=index, result=queue: self._record_queue_status(
                                value,
                                item_index,
                                result,
                            ),
                        )
                    continue

                status = client.build_status(
                    full_name,
                    int(item["build_number"]),
                )
                self.repository.update(
                    task_id,
                    lambda value, item_index=index, result=status: self._record_build_status(
                        value,
                        item_index,
                        result,
                    ),
                )
                if status.get("building"):
                    continue
                result = str(status.get("result") or "UNKNOWN").upper()
                if result == "SUCCESS":
                    self.repository.update(
                        task_id,
                        lambda value, item_index=index, build=status: self._complete_item(
                            value,
                            item_index,
                            build,
                        ),
                    )
                    continue
                raise JenkinsError(
                    f"项目 {full_name} 构建结果为 {result}",
                    code="build_failed",
                )
            except Exception as exc:
                self.repository.update(
                    task_id,
                    lambda value, item_index=index, message=str(exc): self._fail_item(
                        value,
                        item_index,
                        message,
                    ),
                )

    def _stop_remote_and_finish(self, task: dict, *, client: Any | None = None) -> None:
        if client is None:
            try:
                client = self.client_provider()
            except Exception:
                client = None
        task_id = str(task["task_id"])
        for index, item in enumerate(task.get("items") or []):
            if str(item.get("status")) in _TERMINAL_ITEM_STATUSES:
                continue
            try:
                if client is not None and item.get("build_number") is not None:
                    client.stop_build(
                        str(item.get("project_full_name") or ""),
                        int(item["build_number"]),
                    )
                elif client is not None and item.get("queue_id") is not None:
                    client.cancel_queue_item(int(item["queue_id"]))
            except Exception as exc:
                self.repository.update(
                    task_id,
                    lambda value, item_index=index, message=str(exc): self._record_stop_error(
                        value,
                        item_index,
                        message,
                    ),
                )
        self.repository.update(task_id, self._mark_stopped)

    def _fail_task(self, task_id: str, message: str) -> None:
        def mutate(task: dict) -> dict:
            normalized = message or "Jenkins 部署失败"
            active_execution = bool(task.get("active_run_id")) or str(
                task.get("status")
            ) == "running"
            task["status"] = "failed"
            task["error"] = normalized
            task["finished_at"] = self._now()
            task["duration_seconds"] = self._elapsed(task)
            for index, item in enumerate(task.get("items") or []):
                if str(item.get("status")) in _TERMINAL_ITEM_STATUSES:
                    continue
                self._set_item_failed(task, index, normalized)
            task["current_step"] = len(task.get("items") or [])
            task["progress_percent"] = 100 if task.get("items") else 0
            self._append_log(task, f"部署任务失败：{normalized}")
            if active_execution:
                return self._finish_run(task, "failed")
            schedule = task.get("schedule") or {}
            if bool(schedule.get("enabled")):
                schedule["state"] = "invalid"
                schedule["next_run_at"] = ""
                task["schedule"] = schedule
            return task

        try:
            self.repository.update(task_id, mutate)
        except KeyError:
            return

    def _mark_running(self, task: dict) -> dict:
        task["schema_version"] = 3
        task["deployment_type"] = str(task.get("deployment_type") or "iteration")
        task["orchestration_mode"] = "direct_parallel_subtasks"
        execution_number = int(task.get("execution_number") or 0) + 1
        template = task.get("template_items") or [
            {
                key: item.get(key, "")
                for key in (
                    "environment",
                    "project_full_name",
                    "project_name",
                    "description",
                    "branch",
                )
            }
            for item in task.get("items") or []
        ]
        task["template_items"] = copy.deepcopy(template)
        task["items"] = self.repository.runtime_items(
            str(task.get("task_id") or ""),
            template,
            execution_number=execution_number,
        )
        task["execution_number"] = execution_number
        task["active_run_id"] = (
            f"{task.get('task_id')}-run-{execution_number:03d}"
        )
        task["active_run_log_start"] = len(task.get("log_entries") or [])
        task["status"] = "running"
        task["started_at"] = self._now()
        task["finished_at"] = ""
        task["duration_seconds"] = 0
        task["error"] = ""
        task["current_step"] = 0
        task["total_steps"] = len(task["items"])
        task["progress_percent"] = 0
        task["submission_finished"] = False
        task["stop_requested"] = False
        schedule = task.get("schedule") or {}
        scheduled_for = ""
        if bool(schedule.get("enabled")):
            scheduled_for = str(schedule.get("next_run_at") or "")
            schedule["state"] = "running"
            schedule["last_run_at"] = scheduled_for
            task["schedule"] = schedule
        for index, item in enumerate(task.get("items") or [], start=1):
            # 运行时补齐旧 schema 字段，保证升级中的活动任务可直接恢复。
            item.setdefault("subtask_id", f"{task.get('task_id')}-{index:03d}")
            item.setdefault("queue_url", "")
            item.setdefault("queue_reason", "")
        self._append_log(
            task,
            f"第 {execution_number} 次部署开始"
            + (f"（计划时间 {scheduled_for}）" if scheduled_for else "")
            + f"，并行提交 {len(task.get('items') or [])} 个部署子任务",
        )
        return task

    def _record_queue(self, task: dict, index: int, queued: dict) -> dict:
        item = task["items"][index]
        item["status"] = "queued"
        item["queue_id"] = int(queued["queue_id"])
        item["queue_url"] = str(queued.get("queue_url") or "")
        self._append_log(
            task,
            f"子任务 {self._subtask_id(task, index)} 已提交 Jenkins 队列 #{item['queue_id']}："
            f"{item.get('project_full_name')} · {item.get('environment')} · {item.get('branch')}",
        )
        return task

    def _record_queue_status(self, task: dict, index: int, queue: dict) -> dict:
        item = task["items"][index]
        item["queue_reason"] = str(queue.get("why") or "")
        return task

    def _mark_submission_finished(self, task: dict) -> dict:
        if task.get("submission_finished"):
            return task
        task["submission_finished"] = True
        queued = sum(
            item.get("queue_id") is not None or item.get("build_number") is not None
            for item in task.get("items") or []
        )
        failed = sum(
            str(item.get("status")) == "failed"
            for item in task.get("items") or []
        )
        self._append_log(
            task,
            f"子任务提交阶段结束：已进入 Jenkins {queued} 个，提交失败 {failed} 个",
        )
        return task

    def _record_build(self, task: dict, index: int, queue: dict) -> dict:
        item = task["items"][index]
        item["status"] = "running"
        item["build_number"] = int(queue["build_number"])
        item["build_url"] = str(queue.get("build_url") or "")
        item["started_at"] = item.get("started_at") or self._now()
        self._append_log(
            task,
            f"子任务 {self._subtask_id(task, index)} 开始 Jenkins 构建 #{item['build_number']}",
        )
        return task

    def _record_build_status(self, task: dict, index: int, status: dict) -> dict:
        item = task["items"][index]
        item["duration_seconds"] = max(
            int(item.get("duration_seconds") or 0),
            int(status.get("duration_ms") or 0) // 1000,
        )
        if status.get("url") and not item.get("build_url"):
            item["build_url"] = str(status["url"])
        task["duration_seconds"] = self._elapsed(task)
        return task

    def _complete_item(self, task: dict, index: int, status: dict) -> dict:
        item = task["items"][index]
        if str(item.get("status")) == "completed":
            return task
        item["status"] = "completed"
        item["finished_at"] = self._now()
        item["duration_seconds"] = max(
            int(item.get("duration_seconds") or 0),
            int(status.get("duration_ms") or 0) // 1000,
        )
        self._append_log(
            task,
            f"子任务 {self._subtask_id(task, index)} 构建成功"
            f"（#{item.get('build_number')}）",
        )
        return task

    def _fail_item(self, task: dict, index: int, message: str) -> dict:
        item = task["items"][index]
        if str(item.get("status")) in _TERMINAL_ITEM_STATUSES:
            return task
        self._set_item_failed(task, index, message or "Jenkins 部署失败")
        self._append_log(
            task,
            f"子任务 {self._subtask_id(task, index)} 失败：{item['error']}",
        )
        return task

    def _set_item_failed(self, task: dict, index: int, message: str) -> None:
        item = task["items"][index]
        item["status"] = "failed"
        item["error"] = str(message or "Jenkins 部署失败")
        item["finished_at"] = self._now()

    def _record_stop_error(self, task: dict, index: int, message: str) -> dict:
        item = task["items"][index]
        item["error"] = str(message or "停止 Jenkins 子任务失败")
        self._append_log(
            task,
            f"子任务 {self._subtask_id(task, index)} 远端停止失败：{item['error']}",
        )
        return task

    def _refresh_progress(self, task: dict) -> dict:
        if str(task.get("status")) in _TERMINAL_TASK_STATUSES:
            return task
        items = task.get("items") or []
        finished = sum(
            str(item.get("status")) in _TERMINAL_ITEM_STATUSES
            for item in items
        )
        total = len(items)
        task["current_step"] = finished
        task["total_steps"] = total
        task["progress_percent"] = round(finished * 100 / total) if total else 0
        task["duration_seconds"] = self._elapsed(task)
        if finished < total:
            return task

        task["finished_at"] = self._now()
        task["duration_seconds"] = self._elapsed(task)
        failures = [
            str(item.get("error") or "Jenkins 部署失败")
            for item in items
            if str(item.get("status")) == "failed"
        ]
        if failures:
            task["error"] = "；".join(dict.fromkeys(failures))
            self._append_log(
                task,
                f"本次部署结束：{total - len(failures)} 个成功，{len(failures)} 个失败",
            )
            return self._finish_run(task, "failed")
        elif any(str(item.get("status")) == "stopped" for item in items):
            self._append_log(task, "本次部署已停止")
            return self._finish_run(task, "stopped")
        else:
            task["progress_percent"] = 100
            self._append_log(task, f"本次部署已完成，共 {total} 个子任务")
            return self._finish_run(task, "completed")

    def _finish_run(self, task: dict, run_status: str) -> dict:
        """归档本次执行；定时任务还有触发点时回到等待态。"""

        task["status"] = run_status
        self._archive_run(task, run_status)
        task["active_run_id"] = ""
        task.pop("active_run_log_start", None)
        schedule = task.get("schedule") or {}
        if not bool(schedule.get("enabled")):
            return task

        schedule["run_count"] = max(
            int(schedule.get("run_count") or 0),
            int(task.get("execution_number") or 0),
        )
        if run_status == "stopped" or task.get("stop_requested"):
            schedule["state"] = "cancelled"
            schedule["next_run_at"] = ""
            task["schedule"] = schedule
            task["status"] = "stopped"
            task["stop_requested"] = False
            return task

        try:
            scheduled_for = parse_schedule_datetime(
                schedule.get("last_run_at") or schedule.get("next_run_at")
            )
            next_run = next_schedule_occurrence(
                schedule,
                not_before=self._now_value(),
                after=scheduled_for,
            )
        except ValueError:
            next_run = None
        if next_run is not None:
            schedule["state"] = "waiting"
            schedule["next_run_at"] = next_run.isoformat(timespec="minutes")
            task["schedule"] = schedule
            task["status"] = "scheduled"
            task["stop_requested"] = False
            self._append_log(
                task,
                f"本次部署日志已保留，下一次部署时间 {schedule['next_run_at']}",
            )
            return task

        schedule["state"] = "completed"
        schedule["next_run_at"] = ""
        task["schedule"] = schedule
        failed_runs = [
            item
            for item in task.get("execution_runs") or []
            if str(item.get("status")) == "failed"
        ]
        task["status"] = "failed" if failed_runs else "completed"
        if failed_runs:
            task["error"] = f"定时部署共 {len(failed_runs)} 次执行失败"
        self._append_log(
            task,
            f"定时部署计划已结束，共执行 {len(task.get('execution_runs') or [])} 次",
        )
        return task

    def _archive_run(self, task: dict, run_status: str) -> None:
        """保存完整子任务快照与本批次日志，后续执行只追加不覆盖。"""

        execution_number = max(1, int(task.get("execution_number") or 1))
        run_id = str(
            task.get("active_run_id")
            or f"{task.get('task_id')}-run-{execution_number:03d}"
        )
        runs = task.setdefault("execution_runs", [])
        if any(str(item.get("run_id") or "") == run_id for item in runs):
            return
        start_index = max(0, int(task.get("active_run_log_start") or 0))
        schedule = task.get("schedule") or {}
        runs.append(
            {
                "run_id": run_id,
                "execution_number": execution_number,
                "scheduled_for": str(schedule.get("last_run_at") or ""),
                "started_at": str(task.get("started_at") or ""),
                "finished_at": str(task.get("finished_at") or ""),
                "duration_seconds": int(task.get("duration_seconds") or 0),
                "status": run_status,
                "error": str(task.get("error") or ""),
                "items": copy.deepcopy(task.get("items") or []),
                "log_entries": copy.deepcopy(
                    (task.get("log_entries") or [])[start_index:]
                ),
            }
        )

    def _expire_schedule(self, task: dict) -> dict:
        """应用恢复时若整个时间范围已过，结束计划而不补跑历史构建。"""

        schedule = task.get("schedule") or {}
        schedule["state"] = "completed"
        schedule["next_run_at"] = ""
        task["schedule"] = schedule
        failed_runs = any(
            str(item.get("status")) == "failed"
            for item in task.get("execution_runs") or []
        )
        task["status"] = "failed" if failed_runs else "completed"
        task["finished_at"] = self._now()
        self._append_log(task, "定时部署时间范围已结束，不补跑错过的历史触发点")
        return task

    def _mark_stopped(self, task: dict) -> dict:
        had_execution = bool(task.get("active_run_id")) or int(
            task.get("execution_number") or 0
        ) > 0
        task["status"] = "stopped"
        task["stop_requested"] = False
        task["finished_at"] = self._now()
        task["duration_seconds"] = self._elapsed(task)
        for item in task.get("items") or []:
            if str(item.get("status")) in _TERMINAL_ITEM_STATUSES:
                continue
            item["status"] = "stopped"
            item["finished_at"] = self._now()
        task["current_step"] = len(task.get("items") or [])
        task["total_steps"] = len(task.get("items") or [])
        task["progress_percent"] = 100 if task.get("items") else 0
        self._append_log(task, "部署任务已停止，活动的 Jenkins 子任务均已处理")
        if had_execution:
            return self._finish_run(task, "stopped")
        schedule = task.get("schedule") or {}
        if bool(schedule.get("enabled")):
            schedule["state"] = "cancelled"
            schedule["next_run_at"] = ""
            task["schedule"] = schedule
        return task

    @staticmethod
    def _subtask_id(task: dict, index: int) -> str:
        item = task["items"][index]
        return str(
            item.get("subtask_id")
            or f"{task.get('task_id')}-{index + 1:03d}"
        )

    def _append_log(self, task: dict, message: str) -> None:
        """同时保留纯文本与时间戳日志，兼容已有任务展示方式。"""

        normalized = str(message or "").strip()
        if not normalized:
            return
        timestamp = self._now()
        task.setdefault("logs", []).append(normalized)
        task.setdefault("log_entries", []).append(
            {"timestamp": timestamp, "message": normalized}
        )

    def _now_value(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo is not None else value.astimezone()

    def _now(self) -> str:
        return self._now_value().isoformat(timespec="seconds")

    def _elapsed(self, task: dict) -> int:
        started = str(task.get("started_at") or "")
        if not started:
            return 0
        try:
            return max(
                0,
                int(
                    (
                        self._now_value()
                        - datetime.fromisoformat(started)
                    ).total_seconds()
                ),
            )
        except ValueError:
            return int(task.get("duration_seconds") or 0)
