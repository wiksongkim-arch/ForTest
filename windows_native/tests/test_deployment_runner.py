"""桌面端迭代串行、任务内子部署并行编排器测试。"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from backend.settings.secrets import MemorySecretStore
from windows_native.jenkins.models import (
    JenkinsConfiguration,
    JenkinsParameter,
    JenkinsProject,
)
from windows_native.jenkins.service import JenkinsDeploymentService


def _selection(project: str, branch: str = "origin/feature/a") -> dict:
    return {
        "environment": "test",
        "project_full_name": project,
        "project_name": project.rsplit("/", 1)[-1],
        "description": "测试项目",
        "branch": branch,
    }


class FakeJenkinsClient:
    def __init__(
        self,
        *,
        finish_immediately: bool = True,
        failed_projects: set[str] | None = None,
    ):
        self.finish_immediately = finish_immediately
        self.failed_projects = set(failed_projects or set())
        self.triggered: list[tuple[str, str, str]] = []
        self.events: list[tuple[str, str]] = []
        self.stopped: list[tuple[str, int]] = []
        self.cancelled: list[int] = []
        self._next_queue = 40
        self._queue_to_project: dict[int, str] = {}
        self._build_to_project: dict[int, str] = {}
        self.allow_finish = threading.Event()

    def project_details(self, full_name: str) -> JenkinsProject:
        return JenkinsProject(
            full_name=full_name,
            name=full_name.rsplit("/", 1)[-1],
            description="测试项目",
            url="",
            project_class="hudson.model.FreeStyleProject",
            buildable=True,
            parameters=(
                JenkinsParameter("ENV_NAME", choices=("test", "staging")),
                JenkinsParameter(
                    "TARGET_BRANCH",
                    kind="net.uaznia.lukanus.hudson.plugins.gitparameter.GitParameterDefinition",
                    choices=("origin/master", "origin/feature/a"),
                ),
            ),
        )

    def trigger_build(self, full_name: str, *, environment: str, branch: str) -> dict:
        self._next_queue += 1
        queue_id = self._next_queue
        self.triggered.append((full_name, environment, branch))
        self.events.append(("trigger", full_name))
        self._queue_to_project[queue_id] = full_name
        return {"queue_id": queue_id, "queue_url": f"https://jenkins/queue/{queue_id}/"}

    def queue_item(self, queue_id: int) -> dict:
        build_number = queue_id + 100
        project = self._queue_to_project[queue_id]
        self.events.append(("queue", project))
        self._build_to_project[build_number] = project
        return {
            "id": queue_id,
            "cancelled": False,
            "build_number": build_number,
            "build_url": f"https://jenkins/{project}/{build_number}/",
        }

    def build_status(self, _full_name: str, build_number: int) -> dict:
        finished = self.finish_immediately or self.allow_finish.is_set()
        project = self._build_to_project[build_number]
        self.events.append(("build", project))
        return {
            "number": build_number,
            "building": not finished,
            "result": (
                "FAILURE"
                if finished and project in self.failed_projects
                else "SUCCESS"
                if finished
                else ""
            ),
            "duration_ms": 1200,
        }

    def stop_build(self, full_name: str, build_number: int) -> None:
        self.stopped.append((full_name, build_number))

    def cancel_queue_item(self, queue_id: int) -> None:
        self.cancelled.append(queue_id)


def _service(tmp_path, client: FakeJenkinsClient) -> JenkinsDeploymentService:
    secrets = MemorySecretStore()
    service = JenkinsDeploymentService(
        tmp_path,
        secrets=secrets,
        client_factory=lambda *_args: client,
    )
    service.configuration.save(
        JenkinsConfiguration("https://jenkins.example.com/", "bot"),
        "token",
    )
    service.runner.poll_interval = 0.01
    return service


def _wait_for(service: JenkinsDeploymentService, task_id: str, status: str) -> dict:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        task = service.deployment_task(task_id)
        if task and task.get("status") == status:
            return task
        time.sleep(0.01)
    raise AssertionError(f"任务未进入状态 {status}: {service.deployment_task(task_id)}")


def _wait_until(service, task_id: str, predicate) -> dict:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        task = service.deployment_task(task_id)
        if task and predicate(task):
            return task
        time.sleep(0.01)
    raise AssertionError(f"任务未满足等待条件: {service.deployment_task(task_id)}")


class MutableClock:
    """让定时部署测试无需真实等待分钟流逝。"""

    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


def test_runner_submits_all_projects_before_polling_any_queue(tmp_path):
    client = FakeJenkinsClient()
    service = _service(tmp_path, client)
    service.start()
    try:
        task = service.create_deployment_task(
            "并行提交迭代",
            [
                _selection("dtmzp/dtm_pc"),
                _selection("dtmzp/dtmzp_admin"),
            ],
        )
        completed = _wait_for(service, task["task_id"], "completed")

        assert client.triggered == [
            ("dtmzp/dtm_pc", "test", "origin/feature/a"),
            ("dtmzp/dtmzp_admin", "test", "origin/feature/a"),
        ]
        assert client.events[:2] == [
            ("trigger", "dtmzp/dtm_pc"),
            ("trigger", "dtmzp/dtmzp_admin"),
        ]
        assert client.events[2][0] == "queue"
        assert completed["progress_percent"] == 100
        assert completed["current_step"] == 2
        assert [item["status"] for item in completed["items"]] == [
            "completed",
            "completed",
        ]
        assert [item["queue_id"] for item in completed["items"]] == [41, 42]
        assert [item["build_number"] for item in completed["items"]] == [141, 142]
    finally:
        service.stop()


def test_stop_aborts_all_running_builds_and_marks_every_subtask_stopped(tmp_path):
    client = FakeJenkinsClient(finish_immediately=False)
    service = _service(tmp_path, client)
    service.start()
    try:
        task = service.create_deployment_task(
            "停止验证",
            [
                _selection("dtmzp/dtm_pc"),
                _selection("dtmzp/dtmzp_admin"),
            ],
        )
        _wait_for(service, task["task_id"], "running")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            current = service.deployment_task(task["task_id"])
            if all(item.get("build_number") is not None for item in current["items"]):
                break
            time.sleep(0.01)

        service.stop_deployment_task(task["task_id"])
        stopped = _wait_for(service, task["task_id"], "stopped")

        assert {item[0] for item in client.stopped} == {
            "dtmzp/dtm_pc",
            "dtmzp/dtmzp_admin",
        }
        assert [item["status"] for item in stopped["items"]] == [
            "stopped",
            "stopped",
        ]
    finally:
        service.stop()


def test_runner_reports_branch_change_before_triggering_build(tmp_path):
    client = FakeJenkinsClient()

    def missing_branch(full_name: str) -> JenkinsProject:
        project = FakeJenkinsClient.project_details(client, full_name)
        return JenkinsProject(
            full_name=project.full_name,
            name=project.name,
            description=project.description,
            url=project.url,
            project_class=project.project_class,
            buildable=True,
            parameters=(
                JenkinsParameter("ENV_NAME", choices=("test",)),
                JenkinsParameter("TARGET_BRANCH", choices=("origin/master",)),
            ),
        )

    client.project_details = missing_branch
    service = _service(tmp_path, client)
    service.start()
    try:
        task = service.create_deployment_task(
            "分支变更",
            [_selection("dtmzp/dtm_pc")],
        )
        failed = _wait_for(service, task["task_id"], "failed")
        assert "分支已不存在" in failed["error"]
        assert client.triggered == []
    finally:
        service.stop()


def test_one_failed_subtask_does_not_lose_other_build_tracking(tmp_path):
    client = FakeJenkinsClient(failed_projects={"dtmzp/dtm_pc"})
    service = _service(tmp_path, client)
    service.start()
    try:
        task = service.create_deployment_task(
            "部分失败",
            [
                _selection("dtmzp/dtm_pc"),
                _selection("dtmzp/dtmzp_admin"),
            ],
        )
        failed = _wait_for(service, task["task_id"], "failed")

        assert len(client.triggered) == 2
        assert failed["current_step"] == 2
        assert failed["progress_percent"] == 100
        assert [item["status"] for item in failed["items"]] == [
            "failed",
            "completed",
        ]
        assert failed["items"][0]["queue_id"] != failed["items"][1]["queue_id"]
        logs = "\n".join(failed["logs"])
        assert failed["items"][0]["subtask_id"] in logs
        assert failed["items"][1]["subtask_id"] in logs
    finally:
        service.stop()


def test_iteration_tasks_remain_serial_while_subtasks_run_in_parallel(tmp_path):
    client = FakeJenkinsClient(finish_immediately=False)
    service = _service(tmp_path, client)
    service.start()
    try:
        first = service.create_deployment_task(
            "第一迭代",
            [
                _selection("dtmzp/dtm_pc"),
                _selection("dtmzp/dtmzp_admin"),
            ],
        )
        second = service.create_deployment_task(
            "第二迭代",
            [_selection("dtmzp/second")],
        )
        _wait_for(service, first["task_id"], "running")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(client.triggered) < 2:
            time.sleep(0.01)

        assert [item[0] for item in client.triggered] == [
            "dtmzp/dtm_pc",
            "dtmzp/dtmzp_admin",
        ]
        assert service.deployment_task(second["task_id"])["status"] == "queued"

        client.allow_finish.set()
        _wait_for(service, first["task_id"], "completed")
        _wait_for(service, second["task_id"], "completed")
        assert client.triggered[-1][0] == "dtmzp/second"
    finally:
        service.stop()


def test_runner_shutdown_preserves_remote_build_for_restart_recovery(tmp_path):
    client = FakeJenkinsClient(finish_immediately=False)
    service = _service(tmp_path, client)
    service.start()
    task = service.create_deployment_task(
        "重启恢复",
        [_selection("dtmzp/dtm_pc")],
    )
    _wait_for(service, task["task_id"], "running")
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        current = service.deployment_task(task["task_id"])
        if current["items"][0].get("build_number") is not None:
            break
        time.sleep(0.01)
    service.stop()
    assert client.stopped == []
    assert service.deployment_task(task["task_id"])["status"] == "running"

    client.allow_finish.set()
    service.start()
    try:
        completed = _wait_for(service, task["task_id"], "completed")
        assert completed["progress_percent"] == 100
        # 恢复时复用已保存的 build number，不会重复触发。
        assert len(client.triggered) == 1
    finally:
        service.stop()


def test_runner_upgrades_active_legacy_task_without_duplicate_trigger(tmp_path):
    client = FakeJenkinsClient()
    service = _service(tmp_path, client)
    task = service.create_deployment_task(
        "旧任务升级",
        [_selection("dtmzp/dtm_pc")],
    )

    def downgrade(value: dict) -> dict:
        value["schema_version"] = 1
        value.pop("deployment_type", None)
        value["orchestration_mode"] = "direct_serial"
        value["items"][0].pop("subtask_id", None)
        return value

    service.tasks.update(task["task_id"], downgrade)
    service.start()
    try:
        completed = _wait_for(service, task["task_id"], "completed")

        assert completed["schema_version"] == 3
        assert completed["deployment_type"] == "iteration"
        assert completed["orchestration_mode"] == "direct_parallel_subtasks"
        assert completed["items"][0]["subtask_id"].endswith("-001")
        assert len(client.triggered) == 1
    finally:
        service.stop()


def test_single_deployment_and_retry_keep_context_name_and_type(tmp_path):
    client = FakeJenkinsClient()
    service = _service(tmp_path, client)
    service.start()
    try:
        single = service.create_single_deployment_task(
            _selection("dtmzp/dtm_pc")
        )
        completed = _wait_for(service, single["task_id"], "completed")
        retried = service.retry_deployment_task(completed["task_id"])

        assert completed["iteration_name"] == (
            "单点部署-test·dtm_pc·origin/feature/a"
        )
        assert completed["deployment_type"] == "single"
        assert retried["iteration_name"] == completed["iteration_name"]
        assert retried["deployment_type"] == "single"
        assert retried["execution_runs"]
        assert any("历史日志已保留" in line for line in retried["logs"])
    finally:
        service.stop()


def test_waiting_schedule_does_not_block_immediate_task(tmp_path):
    clock = MutableClock(datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc))
    client = FakeJenkinsClient()
    service = _service(tmp_path, client)
    service.tasks._clock = clock
    service.runner._clock = clock
    scheduled = service.create_deployment_task(
        "稍后执行",
        [_selection("dtmzp/later")],
        schedule={
            "enabled": True,
            "mode": "interval_minutes",
            "start_at": (clock.value + timedelta(minutes=10)).isoformat(),
            "end_at": (clock.value + timedelta(minutes=20)).isoformat(),
            "interval_minutes": 10,
        },
    )
    immediate = service.create_deployment_task(
        "立即执行",
        [_selection("dtmzp/now")],
    )
    service.start()
    try:
        _wait_for(service, immediate["task_id"], "completed")
        assert service.deployment_task(scheduled["task_id"])["status"] == "scheduled"
        assert [value[0] for value in client.triggered] == ["dtmzp/now"]
    finally:
        service.stop()


def test_interval_schedule_runs_twice_and_keeps_both_execution_logs(tmp_path):
    clock = MutableClock(datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc))
    client = FakeJenkinsClient()
    service = _service(tmp_path, client)
    service.tasks._clock = clock
    service.runner._clock = clock
    task = service.create_deployment_task(
        "两次定时部署",
        [_selection("dtmzp/scheduled")],
        schedule={
            "enabled": True,
            "mode": "interval_minutes",
            "start_at": clock.value.isoformat(),
            "end_at": (clock.value + timedelta(minutes=1)).isoformat(),
            "interval_minutes": 1,
        },
    )
    service.start()
    try:
        first = _wait_until(
            service,
            task["task_id"],
            lambda value: value.get("status") == "scheduled"
            and len(value.get("execution_runs") or []) == 1,
        )
        assert first["schedule"]["next_run_at"].endswith("01:01+00:00")
        assert service.deployment_counts()["scheduled"] == 1

        clock.advance(minutes=1)
        service.runner.wake()
        completed = _wait_for(service, task["task_id"], "completed")

        assert len(completed["execution_runs"]) == 2
        assert completed["execution_runs"][0]["items"][0]["build_number"] == 141
        assert completed["execution_runs"][1]["items"][0]["build_number"] == 142
        assert completed["items"][0]["subtask_id"].endswith("-r002-001")
        assert sum("次部署开始" in line for line in completed["logs"]) == 2
        assert service.deployment_counts()["scheduled"] == 0
    finally:
        service.stop()


def test_expired_schedule_is_not_replayed_after_restart(tmp_path):
    clock = MutableClock(datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc))
    client = FakeJenkinsClient()
    service = _service(tmp_path, client)
    service.tasks._clock = clock
    service.runner._clock = clock
    task = service.create_deployment_task(
        "过期计划",
        [_selection("dtmzp/expired")],
        schedule={
            "enabled": True,
            "mode": "interval_minutes",
            "start_at": (clock.value + timedelta(minutes=1)).isoformat(),
            "end_at": (clock.value + timedelta(minutes=2)).isoformat(),
            "interval_minutes": 1,
        },
    )
    clock.advance(minutes=5)
    service.start()
    try:
        completed = _wait_for(service, task["task_id"], "completed")
        assert completed["execution_runs"] == []
        assert client.triggered == []
        assert any("不补跑" in line for line in completed["logs"])
    finally:
        service.stop()


def test_waiting_schedule_can_be_stopped_without_triggering_jenkins(tmp_path):
    clock = MutableClock(datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc))
    client = FakeJenkinsClient()
    service = _service(tmp_path, client)
    service.tasks._clock = clock
    service.runner._clock = clock
    task = service.create_deployment_task(
        "取消等待计划",
        [_selection("dtmzp/cancelled")],
        schedule={
            "enabled": True,
            "mode": "daily_time",
            "start_at": clock.value.isoformat(),
            "end_at": (clock.value + timedelta(days=1)).isoformat(),
            "time_of_day": "09:00",
        },
    )
    service.start()
    try:
        service.stop_deployment_task(task["task_id"])
        stopped = _wait_for(service, task["task_id"], "stopped")
        assert stopped["schedule"]["state"] == "cancelled"
        assert stopped["execution_runs"] == []
        assert client.triggered == []
    finally:
        service.stop()


def test_failed_scheduled_run_does_not_cancel_later_occurrence(tmp_path):
    clock = MutableClock(datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc))
    client = FakeJenkinsClient(failed_projects={"dtmzp/flaky"})
    service = _service(tmp_path, client)
    service.tasks._clock = clock
    service.runner._clock = clock
    task = service.create_deployment_task(
        "失败后续跑",
        [_selection("dtmzp/flaky")],
        schedule={
            "enabled": True,
            "mode": "interval_minutes",
            "start_at": clock.value.isoformat(),
            "end_at": (clock.value + timedelta(minutes=1)).isoformat(),
            "interval_minutes": 1,
        },
    )
    service.start()
    try:
        waiting = _wait_until(
            service,
            task["task_id"],
            lambda value: value.get("status") == "scheduled"
            and len(value.get("execution_runs") or []) == 1,
        )
        assert waiting["execution_runs"][0]["status"] == "failed"

        clock.advance(minutes=1)
        service.runner.wake()
        failed = _wait_for(service, task["task_id"], "failed")
        assert len(failed["execution_runs"]) == 2
        assert len(client.triggered) == 2
    finally:
        service.stop()
