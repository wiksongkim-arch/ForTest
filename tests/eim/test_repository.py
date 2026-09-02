"""EIM 数据层的事务、状态机、去重与恢复回归测试。"""

from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.eim.models import (
    BuildState,
    CanonicalEvent,
    ConnectionState,
    DeliveryState,
    DesiredState,
    DestinationType,
    EIMConnection,
    EIMDSL,
    EIMDestination,
    EIMTask,
    EIMTaskVersion,
    EventType,
    MessageKind,
    ObservedState,
)
from backend.eim.repository import EIMRepository
from backend.eim.runtime.media import MediaManager
from backend.eim.runtime.dispatcher import OutboxDispatcher
from backend.eim.runtime.pipeline import EIMPipeline
from backend.eim.service import _csv_cell
from backend.eim.import_export import export_task, import_task


def _repository(tmp_path: Path) -> tuple[EIMRepository, EIMTask]:
    repository = EIMRepository(tmp_path / "eim.db")
    connection = repository.save_connection(
        EIMConnection(
            config_dir_ref="connections/demo",
            profile="default",
            account_name="测试账号",
            connection_state=ConnectionState.CONNECTED,
        )
    )
    destination = repository.save_destination(
        EIMDestination(
            connection_id=connection.connection_id,
            destination_type=DestinationType.DINGTALK_DOC,
            url="https://alidocs.dingtalk.com/i/nodes/demo",
        )
    )
    task = repository.create_task(
        EIMTask(
            name="客户群归档",
            connection_id=connection.connection_id,
            source_id="cid-demo",
            source_name="客户群",
            destination_id=destination.destination_id,
        )
    )
    return repository, task


def _event(connection_id: str, event_id: str = "event-1") -> CanonicalEvent:
    return CanonicalEvent(
        connection_id=connection_id,
        event_id=event_id,
        event_type=EventType.MESSAGE,
        message_id="message-1",
        conversation_id="cid-demo",
        sender_id="user-1",
        occurred_at="2026-09-01T00:00:00+00:00",
        message_kind=MessageKind.TEXT,
        text="hello",
    )


def _publish(repository: EIMRepository, task: EIMTask, content_hash: str) -> EIMTaskVersion:
    repository.transition_build(task.task_id, BuildState.BUILDING)
    repository.transition_build(task.task_id, BuildState.VALIDATING)
    version = EIMTaskVersion(
        task_id=task.task_id,
        manifest={"name": task.name},
        dsl={"schema_version": "eim-dsl/v1"},
        bundle_path=f"bundles/{content_hash}",
        content_hash=content_hash,
    )
    repository.publish_version(version, expected_draft_revision=task.draft_revision)
    return version


def test_repository_state_version_dedupe_and_recovery(tmp_path: Path) -> None:
    repository, task = _repository(tmp_path)

    with sqlite3.connect(repository.database_path) as database:
        assert database.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert database.execute("SELECT version FROM eim_schema_version").fetchone()[0] == 5

    with pytest.raises(ValueError, match="非法构建状态转换"):
        repository.transition_build(task.task_id, BuildState.READY)

    first = _publish(repository, task, "hash-1")
    edited = repository.update_task_draft(
        task.task_id,
        name="客户群归档 v2",
        event_types=[EventType.MESSAGE],
    )
    assert edited.event_types == [EventType.MESSAGE]
    repository.transition_build(task.task_id, BuildState.BUILDING)
    repository.transition_build(task.task_id, BuildState.VALIDATING)
    stale = EIMTaskVersion(
        task_id=task.task_id,
        manifest={},
        dsl={},
        bundle_path="bundles/stale",
        content_hash="hash-stale",
    )
    with pytest.raises(ValueError, match="草稿已被"):
        repository.publish_version(stale, expected_draft_revision=task.draft_revision)
    assert repository.get_task(task.task_id).active_version_id == first.version_id

    second = EIMTaskVersion(
        task_id=task.task_id,
        manifest={},
        dsl={},
        bundle_path="bundles/hash-2",
        content_hash="hash-2",
    )
    repository.publish_version(second, expected_draft_revision=edited.draft_revision)
    versions = repository.list_versions(task.task_id)
    assert [version.status for version in versions] == ["ready", "superseded"]

    event = _event(task.connection_id)
    assert repository.insert_event(task.task_id, event) is True
    assert repository.insert_event(task.task_id, event) is False
    assert repository.claim_events() == []
    repository.transition_observed(
        task.task_id,
        ObservedState.STARTING,
        desired=DesiredState.RUNNING,
    )
    repository.transition_observed(task.task_id, ObservedState.RUNNING)
    claimed_events = repository.claim_events()
    assert [item["event_id"] for item in claimed_events] == [event.event_id]
    assert repository.claim_events() == []
    delivery = repository.enqueue_delivery(
        task_id=task.task_id,
        version_id=second.version_id,
        event_id=event.event_id,
        destination_id=task.destination_id,
        action_name="append",
        payload={"text": event.text},
    )
    assert delivery == repository.enqueue_delivery(
        task_id=task.task_id,
        version_id=second.version_id,
        event_id=event.event_id,
        destination_id=task.destination_id,
        action_name="append",
        payload={"text": "不会覆盖第一次载荷"},
    )
    claimed_deliveries = repository.claim_due_deliveries()
    assert [item["delivery_id"] for item in claimed_deliveries] == [delivery]
    assert claimed_deliveries[0]["previous_state"] == "pending"
    assert repository.claim_due_deliveries() == []

    with pytest.raises(ValueError, match="必须先停止"):
        repository.update_task_draft(task.task_id, name="运行中不可编辑")

    # 重新打开仓储模拟应用崩溃后的启动恢复。
    recovered = EIMRepository(repository.database_path).get_task(task.task_id)
    assert recovered.observed_state is ObservedState.STOPPED_APP_EXIT
    assert recovered.desired_state is DesiredState.RUNNING


def test_copy_delete_retention_and_trust_boundaries(tmp_path: Path) -> None:
    repository, task = _repository(tmp_path)
    copied = repository.copy_task(task.task_id)
    assert copied.task_id != task.task_id
    assert copied.active_version_id is None
    assert copied.observed_state is ObservedState.STOPPED
    assert repository.list_versions(copied.task_id) == []

    deleted = repository.soft_delete_task(copied.task_id)
    assert deleted.deleted_at
    assert all(item.task_id != copied.task_id for item in repository.list_tasks())
    assert repository.restore_task(copied.task_id).deleted_at is None

    repository.append_log(task_id=task.task_id, stage="receive", result="ok")
    event = _event(task.connection_id, "old-event")
    event.received_at = "2026-01-01T00:00:00+00:00"
    repository.insert_event(task.task_id, event)
    repository.set_event_state(task.task_id, event.event_id, "completed")
    result = repository.cleanup_retention(
        logs_before="9999-01-01T00:00:00+00:00",
        completed_payloads_before="9999-01-01T00:00:00+00:00",
        failed_payloads_before="9999-01-01T00:00:00+00:00",
    )
    assert result == {
        "logs": 1,
        "completed_payloads": 1,
        "failed_payloads": 0,
        "expired_event_metadata": 1,
        "completed_delivery_payloads": 0,
        "expired_dead_letters": 0,
    }

    with pytest.raises(ValueError, match="HTTPS"):
        EIMDestination(
            connection_id=task.connection_id,
            destination_type=DestinationType.DINGTALK_DOC,
            url="http://unsafe.example",
        )
    with pytest.raises(ValueError, match="不允许导出"):
        repository.export_rows("sqlite_master")


def test_export_import_is_verified_rebound_and_secret_free(tmp_path: Path) -> None:
    repository, task = _repository(tmp_path)
    repository.add_sample(
        task.task_id,
        {
            "text": "普通链接 https://example.test/path，联系 13800138000",
            "email": "tester@example.test",
            "api_key": "sample-secret",
        },
        {"title": "保留"},
    )
    stored_sample = repository.list_samples(task.task_id)[0]
    assert "13800138000" not in str(stored_sample)
    assert "tester@example.test" not in str(stored_sample)
    archive = export_task(repository, task.task_id, tmp_path / "task.eim.zip", draft_dsl=EIMDSL())
    with zipfile.ZipFile(archive) as package:
        packed = b"\n".join(package.read(name) for name in package.namelist())
        manifest = json.loads(package.read("manifest.json"))
    assert b"sample-secret" not in packed
    assert task.connection_id.encode() not in packed
    assert task.destination_id.encode() not in packed
    assert b"https://example.test/path" not in packed
    assert "普通链接".encode("utf-8") not in packed
    assert "source_name" not in manifest["task"]

    imported, dsl = import_task(
        repository,
        archive,
        connection_id=task.connection_id,
        source_id="cid-rebound",
        source_name="重新绑定的群",
        destination_id=task.destination_id,
    )
    assert imported.task_id != task.task_id
    assert imported.source_id == "cid-rebound"
    assert imported.desired_state is DesiredState.STOPPED
    assert imported.observed_state is ObservedState.STOPPED
    assert imported.active_version_id is None
    assert dsl.schema_version == "eim-dsl/v1"

    unsafe = tmp_path / "unsafe.eim.zip"
    with zipfile.ZipFile(unsafe, "w") as package:
        package.writestr("../manifest.json", "{}")
    with pytest.raises(ValueError, match="文件清单"):
        import_task(
            repository,
            unsafe,
            connection_id=task.connection_id,
            source_id="cid-demo",
            source_name="客户群",
            destination_id=task.destination_id,
        )


def test_csv_export_cells_cannot_start_spreadsheet_formulas() -> None:
    assert _csv_cell("=WEBSERVICE(\"https://example.test\")").startswith("'=")
    assert _csv_cell("@SUM(1,2)").startswith("'@")
    assert _csv_cell("普通预览") == "普通预览"


def test_media_retention_distinguishes_completed_and_incomplete_delivery(tmp_path: Path) -> None:
    repository, task = _repository(tmp_path)
    version = _publish(repository, task, "media-retention")
    event = _event(task.connection_id, "media-event")
    assert repository.insert_event(task.task_id, event)
    assert repository.list_media_retention_events(task.task_id) == [
        {"event_id": "media-event", "has_incomplete_delivery": False}
    ]
    delivery_id = repository.enqueue_delivery(
        task_id=task.task_id,
        version_id=version.version_id,
        event_id=event.event_id,
        destination_id=task.destination_id,
        action_name="append",
        payload={"values": {"body": "message"}},
    )
    assert repository.list_media_retention_events(task.task_id)[0][
        "has_incomplete_delivery"
    ] is True
    repository.update_delivery(delivery_id, DeliveryState.COMPLETED)
    assert repository.list_media_retention_events(task.task_id)[0][
        "has_incomplete_delivery"
    ] is False
    repository.set_event_state(task.task_id, event.event_id, "completed")
    scrubbed = repository.cleanup_retention(
        logs_before="2000-01-01T00:00:00+00:00",
        completed_payloads_before="9999-01-01T00:00:00+00:00",
        failed_payloads_before="2000-01-01T00:00:00+00:00",
    )
    assert scrubbed["completed_payloads"] == 1
    assert scrubbed["expired_event_metadata"] == 0
    assert repository.insert_event(task.task_id, event) is False

    manager = MediaManager(tmp_path, runtime=None)
    event_root = manager._event_directory(task.task_id, event.event_id)
    event_root.mkdir(parents=True)
    old_media = event_root / "old.bin"
    old_media.write_bytes(b"media")
    old_timestamp = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    old_media.touch()
    os.utime(old_media, (old_timestamp, old_timestamp))
    assert manager.cleanup_event(
        task.task_id,
        event.event_id,
        before=datetime.now(UTC) - timedelta(days=1),
    ) == 1

    failed_event = _event(task.connection_id, "failed-media-event")
    assert repository.insert_event(task.task_id, failed_event)
    failed_delivery = repository.enqueue_delivery(
        task_id=task.task_id,
        version_id=version.version_id,
        event_id=failed_event.event_id,
        destination_id=task.destination_id,
        action_name="append",
        payload={"values": {"body": "keep for retry"}},
    )
    repository.update_delivery(failed_delivery, DeliveryState.DEAD_LETTER)
    repository.cleanup_retention(
        logs_before="2000-01-01T00:00:00+00:00",
        completed_payloads_before="9999-01-01T00:00:00+00:00",
        failed_payloads_before="2000-01-01T00:00:00+00:00",
    )
    assert repository.list_dead_letters(task_id=task.task_id)
    result = repository.cleanup_retention(
        logs_before="9999-01-01T00:00:00+00:00",
        completed_payloads_before="9999-01-01T00:00:00+00:00",
        failed_payloads_before="9999-01-01T00:00:00+00:00",
    )
    assert result["failed_payloads"] == 1
    assert result["expired_dead_letters"] == 1
    assert repository.list_dead_letters(task_id=task.task_id) == []


def test_event_retry_is_delayed_bounded_and_manually_recoverable(tmp_path: Path) -> None:
    repository, task = _repository(tmp_path)
    repository.transition_observed(
        task.task_id,
        ObservedState.STARTING,
        desired=DesiredState.RUNNING,
    )
    repository.transition_observed(task.task_id, ObservedState.RUNNING)
    event = _event(task.connection_id, "retry-event")
    assert repository.insert_event(task.task_id, event)
    assert len(repository.claim_events()) == 1
    assert repository.schedule_event_retry(
        task.task_id,
        event.event_id,
        "api_key=must-not-leak",
    ) == "received"
    with sqlite3.connect(repository.database_path) as database:
        stored_error = database.execute(
            "SELECT last_error FROM eim_event_inbox WHERE event_id=?",
            (event.event_id,),
        ).fetchone()[0]
    assert "must-not-leak" not in stored_error
    assert repository.claim_events() == []

    for attempt in (2, 3):
        with sqlite3.connect(repository.database_path) as database:
            database.execute(
                "UPDATE eim_event_inbox SET next_retry_at='2000-01-01T00:00:00+00:00' WHERE event_id=?",
                (event.event_id,),
            )
        assert len(repository.claim_events()) == 1
        state = repository.schedule_event_retry(task.task_id, event.event_id, "暂时失败")
        assert state == ("dead_letter" if attempt == 3 else "received")

    dead = repository.list_dead_events(task_id=task.task_id)
    assert len(dead) == 1
    assert "must-not-leak" not in str(dead)
    assert repository.retry_dead_event(dead[0]["inbox_id"])
    assert len(repository.claim_events()) == 1


def test_same_platform_event_fans_out_once_per_task(tmp_path: Path) -> None:
    repository, first = _repository(tmp_path)
    second = repository.copy_task(first.task_id)
    event = _event(first.connection_id, "shared-event")

    assert repository.insert_event(first.task_id, event)
    assert repository.insert_event(second.task_id, event)
    assert not repository.insert_event(first.task_id, event)


def test_stop_race_returns_claimed_event_and_delivery_to_their_queues(tmp_path: Path) -> None:
    repository, task = _repository(tmp_path)
    version = _publish(repository, task, "stop-fence")
    repository.transition_observed(
        task.task_id,
        ObservedState.STARTING,
        desired=DesiredState.RUNNING,
    )
    repository.transition_observed(task.task_id, ObservedState.RUNNING)
    event = _event(task.connection_id, "stop-race")
    assert repository.insert_event(task.task_id, event)
    claimed_event = repository.claim_events()[0]
    delivery_id = repository.enqueue_delivery(
        task_id=task.task_id,
        version_id=version.version_id,
        event_id=event.event_id,
        destination_id=task.destination_id,
        action_name="append",
        payload={"values": {"body": "待归档"}},
    )
    claimed_delivery = repository.claim_due_deliveries()[0]

    repository.transition_observed(
        task.task_id,
        ObservedState.STOPPING,
        desired=DesiredState.STOPPED,
    )
    repository.transition_observed(task.task_id, ObservedState.STOPPED)
    EIMPipeline(repository, object(), media_manager=object()).process_claimed(claimed_event)
    OutboxDispatcher(repository, object())._deliver(claimed_delivery)

    with repository._connect() as database:
        assert database.execute(
            "SELECT processing_state FROM eim_event_inbox WHERE event_id=?",
            (event.event_id,),
        ).fetchone()[0] == "received"
        assert database.execute(
            "SELECT state FROM eim_delivery_outbox WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()[0] == "pending"
    assert repository.claim_events() == []
    assert repository.claim_due_deliveries() == []
