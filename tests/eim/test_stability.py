"""EIM 千事件对账与重复断线补偿的加速稳定性回归。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from backend.eim.models import (
    BuildState,
    CanonicalEvent,
    ConnectionState,
    DestinationType,
    EIMConnection,
    EIMDSL,
    EIMDestination,
    EIMTask,
    EIMTaskVersion,
    DeliveryState,
    EventType,
    MessageKind,
    DesiredState,
    ObservedState,
)
from backend.eim.repository import EIMRepository
from backend.eim.runtime.dispatcher import OutboxDispatcher
from backend.eim.runtime.pipeline import EIMPipeline
from backend.eim.runtime.supervisor import EIMSupervisor


class _AITableRuntime:
    """用内存记录模拟 AI 表格 upsert 与写后回读。"""

    def __init__(self, root: Path) -> None:
        self.data_root = root
        self.records: dict[str, dict[str, Any]] = {}
        self.upsert_calls = 0

    def config_dir(self, _connection_id: str) -> Path:
        return self.data_root

    def run_json(self, arguments: list[str], **_kwargs: Any) -> dict[str, Any]:
        joined = " ".join(arguments)
        if "aitable record query" in joined:
            filters = json.loads(arguments[arguments.index("--filters") + 1])
            key = str(filters["operands"][0]["operands"][1])
            return {"records": [self.records[key]] if key in self.records else []}
        if "aitable +record-upsert-by-key" in joined:
            self.upsert_calls += 1
            key = arguments[arguments.index("--key-value") + 1]
            cells = json.loads(arguments[arguments.index("--cells") + 1])
            record = {"recordId": f"record-{key}", "cells": cells}
            self.records.setdefault(key, record)
            return record
        return {"success": True}


def _ready_task(root: Path) -> tuple[EIMRepository, EIMTask, _AITableRuntime]:
    repository = EIMRepository(root / "eim.db")
    connection = repository.save_connection(
        EIMConnection(
            config_dir_ref="isolated",
            profile="corp:user",
            connection_state=ConnectionState.CONNECTED,
        )
    )
    destination = repository.save_destination(
        EIMDestination(
            connection_id=connection.connection_id,
            destination_type=DestinationType.DINGTALK_AITABLE,
            url="https://alidocs.dingtalk.com/i/nodes/base?iframeQuery=sheetId%3Dtable",
            stable_ids={
                "base_id": "base-1",
                "table_id": "table-1",
                "event_key_field_id": "fld-event",
            },
            schema_snapshot={
                "writable_fields": [
                    {"fieldId": "fld-event", "type": "text"},
                    {"fieldId": "fld-body", "type": "text"},
                ]
            },
        )
    )
    task = repository.create_task(
        EIMTask(
            name="稳定性验收",
            connection_id=connection.connection_id,
            source_id="cid-stability",
            source_name="稳定性测试群",
            destination_id=destination.destination_id,
        )
    )
    repository.transition_build(task.task_id, BuildState.BUILDING)
    repository.transition_build(task.task_id, BuildState.VALIDATING)
    dsl = EIMDSL(
        mappings={"fld-body": "message.text"},
        destination_action="upsert",
    )
    version = EIMTaskVersion(
        task_id=task.task_id,
        manifest={"name": task.name},
        dsl=dsl.model_dump(mode="json"),
        bundle_path="bundles/stability",
        content_hash="stability-v1",
    )
    repository.publish_version(version, expected_draft_revision=task.draft_revision)
    repository.transition_observed(
        task.task_id,
        ObservedState.STARTING,
        desired=DesiredState.RUNNING,
    )
    repository.transition_observed(task.task_id, ObservedState.RUNNING)
    return repository, repository.get_task(task.task_id), _AITableRuntime(root)


def test_unknown_and_manual_retry_read_back_before_writing(tmp_path: Path) -> None:
    repository, task, runtime = _ready_task(tmp_path)
    version_id = str(task.active_version_id)
    delivery_ids: list[str] = []
    for index, state in enumerate((DeliveryState.COMMIT_UNKNOWN, DeliveryState.RETRY)):
        event_id = f"readback-{index}"
        delivery_id = repository.enqueue_delivery(
            task_id=task.task_id,
            version_id=version_id,
            event_id=event_id,
            destination_id=task.destination_id,
            action_name="upsert",
            payload={"values": {"fld-body": "已在远端写入"}},
        )
        delivery = next(
            item for item in repository.list_due_deliveries() if item["delivery_id"] == delivery_id
        )
        runtime.records[str(delivery["idempotency_key"])] = {
            "recordId": f"record-{index}",
            "cells": {"fld-event": delivery["idempotency_key"]},
        }
        repository.update_delivery(delivery_id, state, last_error="远端结果未知")
        delivery_ids.append(delivery_id)

    assert OutboxDispatcher(repository, runtime).dispatch_once() == 2
    assert runtime.upsert_calls == 0
    with sqlite3.connect(repository.database_path) as database:
        states = database.execute(
            "SELECT state FROM eim_delivery_outbox WHERE delivery_id IN (?, ?)",
            delivery_ids,
        ).fetchall()
    assert states == [("completed",), ("completed",)]


def test_thousand_mixed_events_are_deduplicated_mapped_and_written_once(
    tmp_path: Path,
) -> None:
    repository, task, runtime = _ready_task(tmp_path)
    kinds = (
        MessageKind.TEXT,
        MessageKind.IMAGE,
        MessageKind.FILE,
        MessageKind.AUDIO,
        MessageKind.VIDEO,
        MessageKind.CARD,
    )
    events: list[CanonicalEvent] = []
    for index in range(1_000):
        reaction = index % 10 == 9
        event = CanonicalEvent(
            connection_id=task.connection_id,
            event_id=f"event-{index:04d}",
            event_type=EventType.REACTION if reaction else EventType.MESSAGE,
            message_id=f"message-{index:04d}",
            conversation_id=task.source_id,
            sender_id=f"sender-{index % 7}",
            occurred_at=f"2026-09-01T00:{index // 60 % 60:02d}:{index % 60:02d}+00:00",
            message_kind=MessageKind.UNKNOWN if reaction else kinds[index % len(kinds)],
            text=f"稳定性事件 {index:04d}",
            quoted_message={"message_id": f"message-{index - 1:04d}"}
            if index and index % 23 == 0
            else None,
            reaction={"name": "LIKE", "operation_type": "add"} if reaction else None,
        )
        events.append(event)
        assert repository.insert_event(task.task_id, event)

    # 模拟十个重连窗口重复补偿最近事件，所有重复项均不得再次入箱。
    for offset in range(0, 1_000, 100):
        for event in events[max(0, offset - 10) : offset + 1]:
            assert not repository.insert_event(task.task_id, event)

    pipeline = EIMPipeline(repository, runtime)
    processed = 0
    while count := pipeline.process_once(limit=500):
        processed += count
    dispatcher = OutboxDispatcher(repository, runtime)
    delivered = 0
    while count := dispatcher.dispatch_once(limit=500):
        delivered += count

    assert processed == delivered == len(runtime.records) == 1_000
    with sqlite3.connect(repository.database_path) as database:
        assert database.execute(
            "SELECT COUNT(*) FROM eim_event_inbox WHERE processing_state='completed'"
        ).fetchone()[0] == 1_000
        assert database.execute(
            "SELECT COUNT(*) FROM eim_delivery_outbox WHERE state='completed'"
        ).fetchone()[0] == 1_000


class _GapConnector:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def gap_messages(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self.payloads)


def test_ten_gap_reconciliations_dedupe_overlap_and_report_reaction_degraded(
    tmp_path: Path,
) -> None:
    repository, task, runtime = _ready_task(tmp_path)
    connector = _GapConnector()
    supervisor = EIMSupervisor(repository, connector, runtime)  # type: ignore[arg-type]
    previous: dict[str, Any] | None = None
    for index in range(10):
        current = {
            "type": "user_im_message_receive_group",
            "event_id": f"gap-{index}",
            "message_id": f"gap-message-{index}",
            "conversation_id": task.source_id,
            "sender_open_dingtalk_id": "other-user",
            "create_time": 1_788_192_000_000 + index,
            "message_type": "text",
            "content": {"text": f"断线补偿 {index}"},
        }
        connector.payloads = ([previous] if previous else []) + [current]
        assert supervisor._reconcile_gap(task.task_id, "2026-09-01T00:00:00+00:00")
        previous = current

    with sqlite3.connect(repository.database_path) as database:
        assert database.execute("SELECT COUNT(*) FROM eim_event_inbox").fetchone()[0] == 10
        assert database.execute(
            "SELECT COUNT(*) FROM eim_logs WHERE result='duplicate'"
        ).fetchone()[0] == 9
        assert database.execute(
            "SELECT COUNT(*) FROM eim_logs WHERE stage='reconcile' AND result='degraded'"
        ).fetchone()[0] == 10


def test_gap_reconciliation_does_not_mask_conflicting_conversation_alias(tmp_path: Path) -> None:
    repository, task, runtime = _ready_task(tmp_path)
    connector = _GapConnector()
    connector.payloads = [
        {
            "type": "user_im_message_receive_group",
            "event_id": "gap-conflict",
            "message_id": "gap-message-conflict",
            "conversationId": "cid-other",
            "content": {"text": "不应进入当前任务"},
        }
    ]
    supervisor = EIMSupervisor(repository, connector, runtime)  # type: ignore[arg-type]

    supervisor._reconcile_gap(task.task_id, "2026-09-01T00:00:00+00:00")

    with sqlite3.connect(repository.database_path) as database:
        assert database.execute("SELECT COUNT(*) FROM eim_event_inbox").fetchone()[0] == 0
        assert database.execute(
            "SELECT COUNT(*) FROM eim_logs WHERE stage='normalize' AND result='failed'"
        ).fetchone()[0] == 1
