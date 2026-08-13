"""原生多任务调度与持久化契约测试。"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

import windows_native.task_manager as task_manager_module
from windows_native.task_manager import TaskManager


class FakePreferences:
    def __init__(self, value: int = 1):
        self.value = value

    def get_task_parallelism(self) -> int:
        return self.value

    def set_task_parallelism(self, value: int) -> int:
        self.value = value
        return value


class ControlledGenerationService:
    """只有测试主动放行后，假任务才会完成。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.started: list[tuple[str, str, int]] = []
        self.released: list[str] = []
        self.stopped: list[str] = []
        self.events: dict[str, threading.Event] = {}
        self.config_version = 1

    def start_generation(self, doc_url: str) -> str:
        with self.lock:
            backend_id = f"backend-{len(self.started) + 1}"
            self.started.append((backend_id, doc_url, self.config_version))
            self.events[backend_id] = threading.Event()
            return backend_id

    def task_status(self, task_id: str) -> dict:
        with self.lock:
            event = self.events[task_id]
            source = next(item for item in self.started if item[0] == task_id)
        index = int(task_id.rsplit("-", 1)[-1])
        if task_id in self.stopped:
            return {
                "status": "stopped",
                "task_name": f"需求文档 {index}",
                "current_block": 1,
                "completed_block": 0,
                "total_blocks": 3,
                "logs": ["任务已停止"],
                "result": None,
            }
        if not event.is_set():
            return {
                "status": "running",
                "task_name": f"需求文档 {index}",
                "current_block": 1,
                "completed_block": 0,
                "total_blocks": 3,
                "logs": [f"正在执行 {index}"],
                "result": None,
            }
        return {
            "status": "completed",
            "task_name": f"需求文档 {index}",
            "current_block": 3,
            "completed_block": 3,
            "total_blocks": 3,
            "logs": [f"完成 {index}"],
            "result": {
                "success": True,
                "test_cases_count": index,
                "configuration_version": source[2],
            },
        }

    def release_generation_task(self, task_id: str) -> bool:
        with self.lock:
            self.released.append(task_id)
        return True

    def stop_generation(self, task_id: str) -> bool:
        with self.lock:
            self.stopped.append(task_id)
            self.events[task_id].set()
        return True

    def finish(self, backend_id: str) -> None:
        with self.lock:
            self.events[backend_id].set()


class SourceAwareGenerationService(ControlledGenerationService):
    """记录统一来源参数，验证桌面调度不会把本地文件降级成链接。"""

    def __init__(self):
        super().__init__()
        self.received_sources: list[tuple[str, str]] = []

    def start_generation(
        self,
        document_source: str,
        *,
        source_type: str = "link",
    ) -> str:
        self.received_sources.append((source_type, document_source))
        return super().start_generation(document_source)


def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("等待异步状态超时")


def read_json_when_available(path: Path, timeout: float = 2.0) -> dict:
    """读取后台原子替换的文件时容忍 Windows 的短暂共享冲突。"""

    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, PermissionError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.01)
    pytest.fail(f"等待任务记录可读超时：{type(last_error).__name__}")


def fixed_clock() -> datetime:
    return datetime(2026, 7, 31, 12, 34, 56)


@pytest.fixture
def setup_manager(tmp_path: Path):
    created: list[TaskManager] = []

    def factory(service=None, parallelism: int = 1) -> TaskManager:
        manager = TaskManager(
            service or ControlledGenerationService(),
            tmp_path,
            FakePreferences(parallelism),
            poll_interval=0.01,
            clock=fixed_clock,
        )
        created.append(manager)
        return manager

    yield factory
    for manager in created:
        manager.stop()


def test_default_parallelism_is_one_and_queue_is_fifo(setup_manager):
    service = ControlledGenerationService()
    manager = setup_manager(service)
    first = manager.create_task("https://alidocs.dingtalk.com/first")
    second = manager.create_task("https://alidocs.dingtalk.com/second")

    wait_until(lambda: len(service.started) == 1)
    assert service.started[0][1].endswith("/first")
    assert manager.active_count() == 1
    assert manager.get_task(second["task_id"])["status"] == "pending"

    service.finish("backend-1")
    wait_until(lambda: len(service.started) == 2)
    assert service.started[1][1].endswith("/second")
    service.finish("backend-2")
    wait_until(lambda: manager.get_task(second["task_id"])["status"] == "completed")
    assert first["task_id"] != second["task_id"]


def test_local_document_source_is_absolute_persisted_and_dispatched(
    setup_manager,
    tmp_path,
):
    service = SourceAwareGenerationService()
    manager = setup_manager(service)
    requirement = tmp_path / "本地登录需求.md"
    requirement.write_text("# 登录需求\n用户可以登录。", encoding="utf-8")

    task = manager.create_task(str(requirement), source_type="file")
    wait_until(lambda: len(service.received_sources) == 1)

    expected = str(requirement.resolve())
    assert task["name"] == "本地登录需求"
    assert task["source_type"] == "file"
    assert task["document_source"] == expected
    assert task["doc_url"] == ""
    assert service.received_sources == [("file", expected)]
    payload = read_json_when_available(
        tmp_path / "data" / "generation_tasks.json"
    )
    assert payload["schema_version"] == 2
    assert payload["tasks"][0]["document_source"] == expected

    service.finish("backend-1")
    wait_until(lambda: manager.get_task(task["task_id"])["status"] == "completed")


def test_parallel_limit_and_live_increase_are_enforced(setup_manager):
    service = ControlledGenerationService()
    manager = setup_manager(service)
    tasks = [manager.create_task(f"https://example.test/{index}") for index in range(3)]

    wait_until(lambda: len(service.started) == 1)
    assert manager.set_max_parallel(2) == 2
    wait_until(lambda: len(service.started) == 2)
    assert manager.active_count() == 2
    assert manager.queued_count() == 1
    assert manager.get_task(tasks[2]["task_id"])["status"] == "pending"

    service.finish("backend-1")
    wait_until(lambda: len(service.started) == 3)
    service.finish("backend-2")
    service.finish("backend-3")
    wait_until(lambda: manager.active_count() == 0)


def test_model_metadata_timestamped_logs_and_timing_are_persisted(setup_manager):
    class ModelAwareService(ControlledGenerationService):
        def generation_model_info(self) -> dict:
            return {
                "provider": "codex",
                "model_name": "Codex",
                "model_version": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "inference_speed": "fast",
                "runtime": "sdk",
            }

    service = ModelAwareService()
    manager = setup_manager(service)
    task = manager.create_task("https://example.test/model")
    wait_until(lambda: len(service.started) == 1)
    wait_until(lambda: len(manager.get_task(task["task_id"])["logs"]) >= 5)
    running = manager.get_task(task["task_id"])
    assert running["model_info"]["model_version"] == "gpt-5.6-sol"
    assert running["model_info"]["inference_speed"] == "fast"
    assert running["logs"][:4] == [
        "模型信息：Codex / gpt-5.6-sol",
        "推理强度：high",
        "推理速度：fast",
        "运行方式：sdk",
    ]
    assert len(running["log_entries"]) == len(running["logs"])
    assert all(entry["timestamp"] for entry in running["log_entries"])
    assert running["started_at"]

    service.finish("backend-1")
    wait_until(lambda: manager.get_task(task["task_id"])["status"] == "completed")
    assert manager.get_task(task["task_id"])["finished_at"]


def test_terminal_backend_log_times_are_preserved(setup_manager):
    """终态批量回传日志时，应使用每行自带时间而不是统一完成时间。"""

    fixed_now = datetime.fromisoformat("2026-08-02T18:39:35+08:00")
    manager = setup_manager()
    manager._clock = lambda: fixed_now
    entries = manager._merge_log_entries(
        [],
        [
            "[17:38:33] 开始生成测试用例",
            "[18:37:55] [30/30] 区块: PC端",
        ],
    )

    assert entries[0]["timestamp"] == "2026-08-02T17:38:33+08:00"
    assert entries[1]["timestamp"] == "2026-08-02T18:37:55+08:00"


def test_recovery_updates_exact_task_and_appends_failure_or_success_log(setup_manager):
    manager = setup_manager()
    task = manager.create_task("https://example.test/recovery")
    wait_until(lambda: manager.active_count() == 1)
    manager.service.finish("backend-1")
    wait_until(lambda: manager.get_task(task["task_id"])["status"] == "completed")
    original_finished_at = manager.get_task(task["task_id"])["finished_at"]
    recovered = manager.apply_recovery(
        task["task_id"],
        {
            "status": "completed",
            "result": {
                "test_cases_count": 12,
                "node_id": "node-1",
                "dingtalk_doc_url": "https://example.test/result",
            },
        },
    )
    assert recovered["result"]["test_cases_count"] == 12
    assert recovered["logs"][-1] == "检测恢复完成：12 条用例"
    assert recovered["finished_at"] == original_finished_at
    assert recovered["updated_at"]


def test_decreasing_limit_waits_for_existing_workers_before_dispatch(setup_manager):
    service = ControlledGenerationService()
    manager = setup_manager(service, parallelism=2)
    manager.create_task("https://example.test/one")
    manager.create_task("https://example.test/two")
    manager.create_task("https://example.test/three")
    wait_until(lambda: len(service.started) == 2)

    manager.set_max_parallel(1)
    service.finish("backend-1")
    time.sleep(0.05)
    assert len(service.started) == 2
    service.finish("backend-2")
    wait_until(lambda: len(service.started) == 3)
    service.finish("backend-3")


def test_task_ids_are_unique_fourteen_digit_times(setup_manager):
    manager = setup_manager()
    first = manager.create_task("https://example.test/one")
    second = manager.create_task("https://example.test/two")

    assert first["task_id"] == "20260731123456"
    assert second["task_id"] == "20260731123457"
    assert all(
        len(task["task_id"]) == 14 and task["task_id"].isdigit()
        for task in (first, second)
    )


def test_title_progress_logs_and_result_are_persisted(setup_manager, tmp_path):
    service = ControlledGenerationService()
    manager = setup_manager(service)
    task = manager.create_task("https://example.test/prd")
    wait_until(lambda: len(service.started) == 1)
    wait_until(lambda: manager.get_task(task["task_id"])["total_blocks"] == 3)

    running = manager.get_task(task["task_id"])
    assert running["name"] == "需求文档 1"
    assert running["current_block"] == 0
    assert running["logs"] == ["正在执行 1"]

    service.finish("backend-1")
    wait_until(lambda: manager.get_task(task["task_id"])["status"] == "completed")
    saved = read_json_when_available(
        tmp_path / "data" / "generation_tasks.json"
    )["tasks"][0]
    assert saved["name"] == "需求文档 1"
    assert saved["current_block"] == 3
    assert saved["total_blocks"] == 3
    assert saved["result"]["test_cases_count"] == 1
    assert service.released == ["backend-1"]
    assert not list((tmp_path / "data").glob("*.tmp"))


def test_legacy_backend_progress_falls_back_to_current_block(setup_manager):
    class LegacyProgressService(ControlledGenerationService):
        def task_status(self, task_id: str) -> dict:
            snapshot = super().task_status(task_id)
            snapshot.pop("completed_block", None)
            return snapshot

    service = LegacyProgressService()
    manager = setup_manager(service)
    task = manager.create_task("https://example.test/legacy")

    wait_until(lambda: manager.get_task(task["task_id"])["current_block"] == 1)
    service.finish("backend-1")
    wait_until(lambda: manager.get_task(task["task_id"])["status"] == "completed")


def test_failed_snapshot_uses_last_log_when_outer_error_is_missing(setup_manager):
    class LogOnlyFailureService(ControlledGenerationService):
        def task_status(self, task_id: str) -> dict:
            snapshot = super().task_status(task_id)
            if snapshot["status"] == "completed":
                snapshot.update(
                    {
                        "status": "failed",
                        "logs": ["生成失败（RuntimeError）"],
                        "result": None,
                    }
                )
            return snapshot

    service = LogOnlyFailureService()
    manager = setup_manager(service)
    task = manager.create_task("https://example.test/failure")
    wait_until(lambda: len(service.started) == 1)
    service.finish("backend-1")

    wait_until(lambda: manager.get_task(task["task_id"])["status"] == "failed")
    assert manager.get_task(task["task_id"])["error"] == "生成失败（RuntimeError）"


def test_retry_keeps_id_and_uses_latest_configuration(setup_manager):
    service = ControlledGenerationService()
    manager = setup_manager(service)
    task = manager.create_task("https://example.test/prd")
    wait_until(lambda: len(service.started) == 1)
    service.finish("backend-1")
    wait_until(lambda: manager.get_task(task["task_id"])["status"] == "completed")

    service.config_version = 2
    retried = manager.retry(task["task_id"])
    assert retried["task_id"] == task["task_id"]
    assert retried["attempt"] == 2
    assert retried["result"] is None
    wait_until(lambda: len(service.started) == 2)
    service.finish("backend-2")
    wait_until(
        lambda: manager.get_task(task["task_id"])["result"] is not None
        and manager.get_task(task["task_id"])["result"]["configuration_version"] == 2
    )


def test_stop_pending_task_never_starts_and_can_be_retried(setup_manager):
    service = ControlledGenerationService()
    manager = setup_manager(service)
    first = manager.create_task("https://example.test/first")
    second = manager.create_task("https://example.test/second")
    wait_until(lambda: len(service.started) == 1)

    stopped = manager.stop_task(second["task_id"])
    assert stopped["status"] == "stopped"
    assert stopped["error"] is None
    assert stopped["logs"][-1] == "任务已停止"
    service.finish("backend-1")
    wait_until(lambda: manager.get_task(first["task_id"])["status"] == "completed")
    time.sleep(0.05)
    assert len(service.started) == 1

    manager.retry(second["task_id"])
    wait_until(lambda: len(service.started) == 2)
    service.finish("backend-2")


def test_stop_running_task_targets_only_its_backend_process(setup_manager):
    service = ControlledGenerationService()
    manager = setup_manager(service)
    task = manager.create_task("https://example.test/running")
    wait_until(lambda: manager.get_task(task["task_id"])["total_blocks"] == 3)

    stopped = manager.stop_task(task["task_id"])
    assert stopped["status"] == "stopped"
    wait_until(lambda: service.stopped == ["backend-1"])
    wait_until(lambda: manager.active_count() == 0)
    assert manager.get_task(task["task_id"])["status"] == "stopped"

    manager.retry(task["task_id"])
    wait_until(lambda: len(service.started) == 2)
    service.finish("backend-2")


def test_trashing_pending_task_cancels_queue_and_restore_allows_retry(setup_manager):
    service = ControlledGenerationService()
    manager = setup_manager(service)
    first = manager.create_task("https://example.test/first")
    second = manager.create_task("https://example.test/second")
    wait_until(lambda: len(service.started) == 1)

    trashed = manager.trash(second["task_id"])
    assert trashed["trashed"] is True
    assert trashed["status"] == "interrupted"
    service.finish("backend-1")
    wait_until(lambda: manager.get_task(first["task_id"])["status"] == "completed")
    time.sleep(0.05)
    assert len(service.started) == 1

    restored = manager.restore(second["task_id"])
    assert restored["trashed"] is False
    manager.retry(second["task_id"])
    wait_until(lambda: len(service.started) == 2)
    service.finish("backend-2")


def test_running_task_can_be_recycled_and_finishes_safely(setup_manager):
    service = ControlledGenerationService()
    manager = setup_manager(service)
    task = manager.create_task("https://example.test/running")
    wait_until(lambda: len(service.started) == 1)

    manager.trash(task["task_id"])
    assert manager.list_tasks() == []
    assert manager.list_tasks(include_trashed=True)[0]["trashed"] is True
    assert manager.list_tasks(only_trashed=True)[0]["task_id"] == task["task_id"]
    service.finish("backend-1")
    wait_until(lambda: manager.get_task(task["task_id"])["status"] == "completed")
    assert manager.restore(task["task_id"])["status"] == "completed"


def test_restart_marks_active_records_interrupted_and_keeps_terminal_records(
    tmp_path: Path,
):
    path = tmp_path / "data" / "generation_tasks.json"
    path.parent.mkdir(parents=True)
    base = {
        "name": "需求",
        "doc_url": "https://example.test/prd",
        "current_block": 0,
        "total_blocks": 0,
        "logs": [],
        "result": None,
        "error": None,
        "trashed": False,
        "attempt": 1,
        "created_at": "2026-07-31T12:34:56+08:00",
        "updated_at": "2026-07-31T12:34:56+08:00",
        "trashed_at": None,
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tasks": [
                    {**base, "task_id": "20260731123456", "status": "running"},
                    {**base, "task_id": "20260731123457", "status": "completed"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manager = TaskManager(
        ControlledGenerationService(),
        tmp_path,
        FakePreferences(),
        clock=fixed_clock,
    )
    try:
        assert manager.get_task("20260731123456")["status"] == "interrupted"
        assert manager.get_task("20260731123456")["error"]
        assert manager.get_task("20260731123456")["source_type"] == "link"
        assert manager.get_task("20260731123456")["document_source"] == (
            "https://example.test/prd"
        )
        assert manager.get_task("20260731123457")["status"] == "completed"
    finally:
        manager.stop()


def test_returned_snapshots_cannot_mutate_internal_state(setup_manager):
    manager = setup_manager()
    task = manager.create_task("https://example.test/prd")
    task["name"] = "外部篡改"
    listed = manager.list_tasks()
    listed[0]["logs"].append("外部日志")

    current = manager.get_task(task["task_id"])
    assert current["name"] != "外部篡改"
    assert "外部日志" not in current["logs"]


def test_corrupt_task_file_is_quarantined_instead_of_overwritten(tmp_path: Path):
    path = tmp_path / "data" / "generation_tasks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    manager = TaskManager(
        ControlledGenerationService(),
        tmp_path,
        FakePreferences(),
        clock=fixed_clock,
    )
    try:
        assert manager.list_tasks(include_trashed=True) == []
        backups = list(path.parent.glob("generation_tasks.json.corrupt-*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "{broken"
    finally:
        manager.stop()


def test_task_repository_retries_transient_windows_replace_lock(
    setup_manager,
    monkeypatch,
):
    real_replace = task_manager_module.os.replace
    attempts = 0

    def transient_replace(source, destination):
        nonlocal attempts
        if Path(destination).name == "generation_tasks.json" and attempts < 2:
            attempts += 1
            raise PermissionError("simulated Windows sharing violation")
        attempts += 1
        return real_replace(source, destination)

    monkeypatch.setattr(task_manager_module.os, "replace", transient_replace)
    manager = setup_manager()
    manager.create_task("https://example.test/prd")

    assert attempts >= 3


def test_worker_start_failure_becomes_persisted_failed_task(
    setup_manager,
    monkeypatch,
):
    manager = setup_manager()
    manager.start()

    class BrokenWorker:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def start(self):
            raise RuntimeError("无法创建工作线程")

    monkeypatch.setattr(task_manager_module.threading, "Thread", BrokenWorker)
    task = manager.create_task("https://example.test/prd")
    wait_until(lambda: manager.get_task(task["task_id"])["status"] == "failed")
    assert "无法创建工作线程" in manager.get_task(task["task_id"])["error"]
    assert manager.active_count() == 0


@pytest.mark.parametrize("value", [0, 9, True, "abc"])
def test_parallelism_validation_rejects_unsafe_values(setup_manager, value):
    manager = setup_manager()
    with pytest.raises(ValueError):
        manager.set_max_parallel(value)
