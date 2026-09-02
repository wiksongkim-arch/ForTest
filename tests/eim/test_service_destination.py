"""EIM 目标结构刷新与幂等字段绑定回归。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.eim.models import (
    ConnectionState,
    DestinationType,
    EIMConnection,
    EIMDSL,
    EIMDestination,
    EIMTask,
)
from backend.eim.repository import EIMRepository
from backend.eim.service import EIMApplicationService


def _service(tmp_path) -> tuple[EIMApplicationService, EIMTask]:
    service = EIMApplicationService.__new__(EIMApplicationService)
    service.repository = EIMRepository(tmp_path / "eim.db")
    service.runtime = SimpleNamespace()
    connection = service.repository.save_connection(
        EIMConnection(
            config_dir_ref="isolated",
            profile="corp:user",
            connection_state=ConnectionState.CONNECTED,
        )
    )
    destination = service.repository.save_destination(
        EIMDestination(
            connection_id=connection.connection_id,
            destination_type=DestinationType.DINGTALK_AITABLE,
            url="https://alidocs.dingtalk.com/i/nodes/example",
            stable_ids={"base_id": "base-1", "table_id": "table-1"},
            schema_snapshot={
                "writable_fields": [
                    {"fieldId": "field-body", "fieldName": "消息内容", "type": "text"}
                ]
            },
        )
    )
    task = service.repository.create_task(
        EIMTask(
            name="目标结构测试",
            connection_id=connection.connection_id,
            source_id="cid-test",
            source_name="测试群",
            destination_id=destination.destination_id,
        )
    )
    service.builder = SimpleNamespace(
        load_draft=lambda _task_id: EIMDSL(
            mappings={"field-body": "message.text"},
            destination_action="upsert",
        )
    )
    return service, task


def test_refresh_destination_updates_schema_without_changing_binding(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, task = _service(tmp_path)
    inspected = {
        "fields": [
            {"fieldId": "field-body", "fieldName": "消息内容", "type": "text"},
            {"fieldId": "field-event", "fieldName": "EIM 事件 ID", "type": "text"},
        ],
        "writable_fields": [
            {"fieldId": "field-body", "fieldName": "消息内容", "type": "text"},
            {"fieldId": "field-event", "fieldName": "EIM 事件 ID", "type": "text"},
        ],
    }
    monkeypatch.setattr(
        "backend.eim.service.destination_adapter",
        lambda *_args: SimpleNamespace(inspect_schema=lambda: inspected),
    )

    detail = service.refresh_destination(task.task_id)

    assert detail["destination"]["schema_snapshot"] == inspected
    assert detail["destination"]["stable_ids"] == {
        "base_id": "base-1",
        "table_id": "table-1",
    }


def test_event_id_binding_rejects_a_business_field_already_in_mapping(tmp_path) -> None:
    service, task = _service(tmp_path)

    with pytest.raises(ValueError, match="专用文本字段"):
        service.configure_destination(
            task.task_id,
            {"event_key_field_id": "field-body"},
        )
