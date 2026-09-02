"""EIM DSL 沙箱与三类目标的幂等写入契约测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from backend.eim.builder.compiler import compile_dsl, dsl_from_text, dsl_to_text
from backend.eim.builder.simulator import simulate
from backend.eim.connections.dws_runtime import DWSRuntimeError
from backend.eim.destinations.dingtalk import destination_adapter
from backend.eim.models import (
    CanonicalEvent,
    DestinationType,
    EIMConnection,
    EIMDSL,
    EIMDestination,
    EventType,
    Extractor,
    FilterRule,
    MessageKind,
)


def _event() -> CanonicalEvent:
    return CanonicalEvent(
        connection_id="connection",
        event_id="event-1",
        event_type=EventType.MESSAGE,
        message_id="message-1",
        conversation_id="cid",
        sender_id="sender-1",
        sender_name="张三",
        occurred_at="2026-09-01T00:00:00+00:00",
        message_kind=MessageKind.TEXT,
        text="订单号 A-1024 已完成",
        raw_payload={"business": {"priority": "high"}},
    )


def test_dsl_compiles_filters_extractors_mappings_and_round_trips() -> None:
    dsl = EIMDSL(
        triggers=[EventType.MESSAGE],
        filters=[FilterRule(field="message.text", operator="contains", value="订单号")],
        extractors=[
            Extractor(
                kind="regex",
                output="order_id",
                source="message.text",
                pattern=r"A-(\d+)",
                group=1,
            ),
            Extractor(kind="path", output="priority", path="raw.business.priority"),
        ],
        mappings={"订单": "extracted.order_id", "优先级": "extracted.priority"},
    )
    result = simulate(dsl, _event(), expected={"订单": "1024", "优先级": "high"})
    assert result["passed"] is True
    assert dsl_from_text(dsl_to_text(dsl)) == dsl
    assert compile_dsl(dsl, target_fields={"订单", "优先级"}).execute(_event()) == {
        "订单": "1024",
        "优先级": "high",
    }

    with pytest.raises(ValueError, match="高风险"):
        compile_dsl(
            EIMDSL(filters=[FilterRule(field="message.text", operator="regex", value="(a+)+$")])
        )
    with pytest.raises(ValueError, match="高风险"):
        compile_dsl(
            EIMDSL(filters=[FilterRule(field="message.text", operator="regex", value="a*a*a*a*b")])
        )
    with pytest.raises(ValueError, match="高风险"):
        compile_dsl(
            EIMDSL(filters=[FilterRule(field="message.text", operator="regex", value="a+b")])
        )
    with pytest.raises(ValueError, match="目标字段"):
        compile_dsl(dsl, target_fields={"订单"})
    with pytest.raises(ValidationError):
        EIMDSL.model_validate({"schema_version": "eim-dsl/v1", "shell": "calc.exe"})


def test_dingtalk_issue_transforms_extract_site_and_plain_text() -> None:
    event = _event().model_copy(
        update={"text": "@测试人(user-1) 【华东站】 登录失败 [图片](mediaId=media-1)"}
    )
    dsl = EIMDSL(
        triggers=[EventType.MESSAGE],
        extractors=[
            Extractor(
                kind="transform",
                output="site",
                source="message.text",
                transform="dingtalk_site",
            ),
            Extractor(
                kind="transform",
                output="description",
                source="message.text",
                transform="dingtalk_issue_text",
            ),
        ],
        mappings={"站点": "extracted.site", "问题描述": "extracted.description"},
    )

    assert compile_dsl(dsl).execute(event) == {
        "站点": "华东站",
        "问题描述": "登录失败",
    }


class _Runtime:
    def __init__(self, root: Path):
        self.root = root
        self.data_root = root
        self.calls: list[list[str]] = []
        self.sheet_written = False
        self.doc_written = False
        self.doc_content = ""
        self.doc_media: dict[str, str] = {}
        self.aitable_written = False

    def config_dir(self, _connection_id: str) -> Path:
        return self.root

    def run_json(self, arguments: list[str], **_kwargs: Any) -> dict[str, Any]:
        self.calls.append(arguments)
        joined = " ".join(arguments)
        if "doc +fetch" in joined:
            return {"content": self.doc_content}
        if "doc +media-list" in joined:
            return {
                "media": [
                    {"fileName": name, "blockId": block_id}
                    for name, block_id in self.doc_media.items()
                ]
            }
        if "doc +media-insert" in joined:
            name = Path(arguments[arguments.index("--file") + 1]).name
            block_id = f"block-{len(self.doc_media) + 1}"
            self.doc_media[name.casefold()] = block_id
            return {"data": {"blockId": block_id}}
        if "doc +doc-append" in joined:
            self.doc_written = True
            self.doc_content += "\n" + arguments[arguments.index("--content") + 1]
            return {"success": True}
        if "sheet media-upload" in joined:
            return {"resourceId": "resource-1", "resourceUrl": "/core/api/resources/file/1"}
        if "sheet find" in joined:
            return {"matches": [{"address": "A2"}]} if self.sheet_written else {"matches": []}
        if "sheet append" in joined:
            self.sheet_written = True
            return {"success": True}
        if "aitable record query" in joined:
            return {"records": [{"recordId": "rec-1"}]} if self.aitable_written else {"records": []}
        if "aisearch person" in joined:
            return {
                "result": [
                    {
                        "openDingTalkId": "sender-1",
                        "userId": "resolved-user-1",
                    }
                ]
            }
        if "aitable attachment upload" in joined:
            return {
                "uploadUrl": "https://uploads.example.test/object?signature=hidden",
                "fileToken": "file_token_123",
            }
        if "aitable +record-upsert-by-key" in joined:
            self.aitable_written = True
            return {"recordId": "rec-1"}
        return {"success": True}


class _FailingDocRuntime(_Runtime):
    def __init__(self, root: Path):
        super().__init__(root)
        self.fail_media_marker_once = True

    def run_json(self, arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        if (
            self.fail_media_marker_once
            and "doc +doc-append" in " ".join(arguments)
            and "--content" in arguments
            and arguments[arguments.index("--content") + 1].startswith("[ForTest-EIM-Media:")
        ):
            self.calls.append(arguments)
            self.fail_media_marker_once = False
            raise DWSRuntimeError("模拟媒体标记写入中断")
        return super().run_json(arguments, **kwargs)


def _connection() -> EIMConnection:
    return EIMConnection(
        connection_id="01K42YJ9M2E7H05KTA7VC6ER8R",
        config_dir_ref="local",
        profile="corp:user",
    )


def test_document_sheet_and_aitable_delivery_are_idempotent(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    connection = _connection()

    document = EIMDestination(
        connection_id=connection.connection_id,
        destination_type=DestinationType.DINGTALK_DOC,
        url="https://alidocs.dingtalk.com/i/nodes/doc",
    )
    doc_adapter = destination_adapter(runtime, connection, document)  # type: ignore[arg-type]
    first_doc = doc_adapter.deliver("key-1", {"title": "标题", "body": "正文"})
    second_doc = doc_adapter.deliver("key-1", {"body": "不应重复"})
    assert first_doc.external_ref == "[ForTest-EIM-Complete:key-1]"
    assert second_doc.already_present is True

    sheet = EIMDestination(
        connection_id=connection.connection_id,
        destination_type=DestinationType.DINGTALK_SHEET,
        url="https://alidocs.dingtalk.com/i/nodes/sheet",
        stable_ids={"sheet_id": "sheet-1"},
        schema_snapshot={"headers": ["_eim_event_id", "正文"]},
    )
    sheet_adapter = destination_adapter(runtime, connection, sheet)  # type: ignore[arg-type]
    assert sheet_adapter.deliver("key-2", {"正文": "消息"}).external_ref == "A2"
    assert sheet_adapter.deliver("key-2", {"正文": "消息"}).already_present is True

    aitable = EIMDestination(
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
    aitable_adapter = destination_adapter(runtime, connection, aitable)  # type: ignore[arg-type]
    assert aitable_adapter.deliver("key-3", {"fld-body": "消息"}).external_ref == "rec-1"
    assert aitable_adapter.deliver("key-3", {"fld-body": "消息"}).already_present is False


def test_aitable_user_field_wraps_user_id_with_current_corp(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    connection = _connection().model_copy(update={"organization_id": "corp-1"})
    destination = EIMDestination(
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
                {"fieldId": "fld-user", "type": "user"},
                {"fieldId": "fld-site", "type": "text"},
            ]
        },
    )
    adapter = destination_adapter(runtime, connection, destination)  # type: ignore[arg-type]

    adapter.deliver("user-key", {"fld-user": "user-1"})

    upsert = next(call for call in runtime.calls if "+record-upsert-by-key" in call)
    cells = json.loads(upsert[upsert.index("--cells") + 1])
    assert cells["fld-user"] == [{"userId": "user-1", "corpId": "corp-1"}]


def test_aitable_resolves_sender_open_id_before_writing_user_field(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    connection = _connection().model_copy(update={"organization_id": "corp-1"})
    destination = EIMDestination(
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
                {"fieldId": "fld-user", "type": "user"},
            ]
        },
    )
    adapter = destination_adapter(runtime, connection, destination)  # type: ignore[arg-type]

    values = adapter.prepare_values(
        {"fld-user": "sender-1", "fld-site": ""},
        {"fld-user": "sender.id", "fld-site": "extracted.site"},
        _event(),
    )
    adapter.deliver("sender-key", values)

    lookup = next(call for call in runtime.calls if "aisearch person" in " ".join(call))
    assert lookup[lookup.index("--query") + 1] == "张三"
    upsert = next(call for call in runtime.calls if "+record-upsert-by-key" in call)
    cells = json.loads(upsert[upsert.index("--cells") + 1])
    assert cells["fld-user"] == [{"userId": "resolved-user-1", "corpId": "corp-1"}]
    assert "fld-site" not in cells


def test_sheet_values_neutralize_formula_prefixes(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    connection = _connection()
    destination = EIMDestination(
        connection_id=connection.connection_id,
        destination_type=DestinationType.DINGTALK_SHEET,
        url="https://alidocs.dingtalk.com/i/nodes/sheet",
        stable_ids={"sheet_id": "sheet-1"},
        schema_snapshot={"headers": ["_eim_event_id", "正文"]},
    )
    adapter = destination_adapter(runtime, connection, destination)  # type: ignore[arg-type]

    adapter.deliver("formula-key", {"正文": " \t=HYPERLINK(\"https://example.test\")"})

    append = next(call for call in runtime.calls if "sheet append" in " ".join(call))
    row = json.loads(append[append.index("--values") + 1])[0]
    assert row[1].startswith("'")


def test_media_delivery_uses_trusted_uploads_and_reuses_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_path = tmp_path / "eim" / "media" / "task" / "event" / "asset.pdf"
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"safe-media")
    digest = hashlib.sha256(b"safe-media").hexdigest()
    asset = {
        "file_name": "report.pdf",
        "mime_type": "application/pdf",
        "size": media_path.stat().st_size,
        "sha256": digest,
        "local_path": media_path.relative_to(tmp_path).as_posix(),
    }
    connection = _connection()

    doc_runtime = _Runtime(tmp_path)
    document = EIMDestination(
        connection_id=connection.connection_id,
        destination_type=DestinationType.DINGTALK_DOC,
        url="https://alidocs.dingtalk.com/i/nodes/doc",
    )
    doc_adapter = destination_adapter(doc_runtime, connection, document)  # type: ignore[arg-type]
    assert doc_adapter.deliver("media-key", {"body": "正文", "media": [asset]}).evidence[
        "media_block_ids"
    ] == ["block-1"]
    assert doc_adapter.deliver("media-key", {"body": "正文", "media": [asset]}).already_present
    assert sum("doc +media-insert" in " ".join(call) for call in doc_runtime.calls) == 1

    sheet_runtime = _Runtime(tmp_path)
    sheet = EIMDestination(
        connection_id=connection.connection_id,
        destination_type=DestinationType.DINGTALK_SHEET,
        url="https://alidocs.dingtalk.com/i/nodes/sheet",
        stable_ids={"sheet_id": "sheet-1"},
        schema_snapshot={"headers": ["_eim_event_id", "附件"]},
    )
    cache: dict[str, dict[str, str]] = {}
    saved: list[dict[str, dict[str, str]]] = []
    sheet_adapter = destination_adapter(sheet_runtime, connection, sheet)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="缺少可信"):
        sheet_adapter.deliver(
            "sheet-invalid-media",
            {"附件": [{"file_name": "unsafe.pdf", "stable_url": "http://example.test/a"}]},
        )
    sheet_adapter.deliver(
        "sheet-media-key",
        {"附件": [asset]},
        media_cache=cache,
        persist_media_cache=lambda value: saved.append(dict(value)),
    )
    assert saved and cache[digest]["resource_url"].startswith("/core/api/resources/")

    uploaded: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "backend.eim.destinations.dingtalk._put_https_file",
        lambda _url, _path, _mime, size: uploaded.append((_mime, size)),
    )
    aitable_runtime = _Runtime(tmp_path)
    aitable = EIMDestination(
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
                {"fieldId": "fld-attachment", "type": "attachment"},
            ]
        },
    )
    aitable_adapter = destination_adapter(aitable_runtime, connection, aitable)  # type: ignore[arg-type]
    aitable_adapter.deliver("aitable-media-key", {"fld-attachment": [asset]})
    assert uploaded == [("application/pdf", len(b"safe-media"))]
    upsert = next(call for call in aitable_runtime.calls if "+record-upsert-by-key" in call)
    assert "file_token_123" in upsert[upsert.index("--cells") + 1]


def test_document_media_partial_failure_reuses_inserted_block(tmp_path: Path) -> None:
    media_path = tmp_path / "eim" / "media" / "task" / "event" / "asset.png"
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"png-data")
    asset = {
        "file_name": "image.png",
        "mime_type": "image/png",
        "size": media_path.stat().st_size,
        "sha256": hashlib.sha256(b"png-data").hexdigest(),
        "local_path": media_path.relative_to(tmp_path).as_posix(),
    }
    runtime = _FailingDocRuntime(tmp_path)
    destination = EIMDestination(
        connection_id=_connection().connection_id,
        destination_type=DestinationType.DINGTALK_DOC,
        url="https://alidocs.dingtalk.com/i/nodes/doc",
    )
    adapter = destination_adapter(runtime, _connection(), destination)  # type: ignore[arg-type]
    with pytest.raises(DWSRuntimeError, match="模拟媒体标记"):
        adapter.deliver("partial-key", {"body": "正文", "media": [asset]})
    assert adapter.deliver("partial-key", {"body": "正文", "media": [asset]}).external_ref
    assert sum("doc +media-insert" in " ".join(call) for call in runtime.calls) == 1


def test_destination_url_rejects_non_dingtalk_and_credentials() -> None:
    for url in (
        "https://example.test/i/nodes/doc",
        "https://user:pass@alidocs.dingtalk.com/i/nodes/doc",
        "http://alidocs.dingtalk.com/i/nodes/doc",
    ):
        with pytest.raises(ValidationError):
            EIMDestination(
                connection_id="connection",
                destination_type=DestinationType.DINGTALK_DOC,
                url=url,
            )
