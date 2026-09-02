"""普通文档、电子表格和 AI 表格的可信 DWS 目标适配器。"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from backend.eim.connections.dws_runtime import DWSRuntime, DWSRuntimeError
from backend.eim.models import CanonicalEvent, DestinationType, EIMConnection, EIMDestination
from services.dingtalk_output import neutralize_spreadsheet_formula


_EVENT_COLUMN = "_eim_event_id"
_FILE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,256}$")
_MIME_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_ATTACHMENT_FIELD_TYPE = "attachment"
_USER_FIELD_TYPE = "user"
_TEXT_FIELD_TYPES = {"text", "string", "1"}
_READ_ONLY_AITABLE_TYPES = {
    "formula",
    "lookup",
    "filterup",
    "createdtime",
    "lastmodifiedtime",
    "creator",
    "lastmodifier",
    "autonumber",
}


@dataclass(frozen=True)
class DeliveryResult:
    external_ref: str
    evidence: dict[str, Any]
    already_present: bool = False


class _BaseAdapter:
    def __init__(
        self,
        runtime: DWSRuntime,
        connection: EIMConnection,
        destination: EIMDestination,
    ):
        if destination.connection_id != connection.connection_id:
            raise ValueError("归档目标不属于当前 EIM 连接")
        if not connection.profile:
            raise ValueError("钉钉连接缺少 profile")
        self.runtime = runtime
        self.connection = connection
        self.destination = destination
        self.config_dir = runtime.config_dir(connection.connection_id)

    @property
    def profile_args(self) -> list[str]:
        return ["--profile", self.connection.profile]

    def run_json(self, arguments: list[str], *, timeout: float = 60) -> dict[str, Any]:
        return self.runtime.run_json(
            [*arguments, *self.profile_args],
            config_dir=self.config_dir,
            timeout=timeout,
        )

    def target_fields(self) -> set[str]:
        raise NotImplementedError

    def inspect_schema(self) -> dict[str, Any]:
        raise NotImplementedError

    def find_by_idempotency_key(self, key: str) -> str | None:
        raise NotImplementedError

    def deliver(
        self,
        key: str,
        values: dict[str, Any],
        *,
        media_cache: dict[str, dict[str, str]] | None = None,
        persist_media_cache: Callable[[dict[str, dict[str, str]]], None] | None = None,
    ) -> DeliveryResult:
        raise NotImplementedError

    def validate_values(self, values: dict[str, Any]) -> None:
        """构建期目标类型检查；普通目标无需额外约束。"""

    def prepare_values(
        self,
        values: dict[str, Any],
        mappings: dict[str, str],
        event: CanonicalEvent,
    ) -> dict[str, Any]:
        """在入队前完成目标专属、可重试的值解析。"""

        del mappings, event
        return values

    def _local_media(self, value: dict[str, Any], *, max_bytes: int) -> dict[str, Any]:
        """只允许上传 EIM 受控媒体目录内、哈希一致的普通文件。"""

        relative_value = str(value.get("local_path") or "").strip()
        if not relative_value or Path(relative_value).is_absolute():
            raise ValueError("媒体缺少安全的本地相对路径")
        media_root = (self.runtime.data_root / "eim" / "media").resolve()
        path = (self.runtime.data_root / relative_value).resolve()
        if media_root not in path.parents or not path.is_file() or path.is_symlink():
            raise ValueError("媒体文件不在 EIM 受控目录或不是普通文件")
        size = path.stat().st_size
        if size <= 0 or size > max_bytes:
            raise ValueError("媒体文件为空或超过目标上传限制")
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        declared_digest = str(value.get("sha256") or "").casefold()
        if declared_digest and declared_digest != digest:
            raise ValueError("媒体文件哈希与事件记录不一致")
        declared_size = value.get("size")
        if declared_size is not None and int(declared_size) != size:
            raise ValueError("媒体文件大小与事件记录不一致")
        mime_type = str(value.get("mime_type") or "application/octet-stream").strip()
        if not _MIME_TYPE.fullmatch(mime_type):
            mime_type = "application/octet-stream"
        file_name = Path(str(value.get("file_name") or path.name)).name
        file_name = "".join(character for character in file_name if ord(character) >= 32)[:120]
        if not file_name or not Path(file_name).suffix:
            file_name = path.name
        return {
            "path": path,
            "relative_path": path.relative_to(self.runtime.data_root).as_posix(),
            "sha256": digest,
            "size": size,
            "mime_type": mime_type,
            "file_name": file_name,
        }


class DingTalkDocAdapter(_BaseAdapter):
    def resolve(self) -> dict[str, Any]:
        value = self.run_json(
            ["doc", "+fetch", "--node", self.destination.url, "--detail", "with-ids"]
        )
        return {
            "document_id": _find_scalar(value, ("documentId", "document_id", "nodeId", "node_id")),
            "readable": True,
        }

    def inspect_schema(self) -> dict[str, Any]:
        resolved = self.resolve()
        return {"type": "block_template", "fields": ["title", "body", "metadata", "media"], **resolved}

    def target_fields(self) -> set[str]:
        return {"title", "body", "metadata", "media"}

    def validate_write_capability(self) -> None:
        self.run_json(
            [
                "doc",
                "+doc-append",
                "--doc",
                self.destination.url,
                "--content",
                "ForTest EIM 写入权限预检",
                "--dry-run",
                "--yes",
            ]
        )

    @staticmethod
    def _marker(key: str) -> str:
        return f"[ForTest-EIM:{key}]"

    @staticmethod
    def _complete_marker(key: str) -> str:
        return f"[ForTest-EIM-Complete:{key}]"

    @staticmethod
    def _media_marker(key: str, digest: str) -> str:
        value = hashlib.sha256(f"{key}:{digest}".encode("utf-8")).hexdigest()[:24]
        return f"[ForTest-EIM-Media:{value}]"

    def _find_marker(self, marker: str) -> bool:
        value = self.run_json(
            [
                "doc",
                "+fetch",
                "--node",
                self.destination.url,
                "--scope",
                "keyword",
                "--keyword",
                marker,
            ]
        )
        return marker in json.dumps(value, ensure_ascii=False)

    def find_by_idempotency_key(self, key: str) -> str | None:
        marker = self._complete_marker(key)
        return marker if self._find_marker(marker) else None

    def deliver(
        self,
        key: str,
        values: dict[str, Any],
        *,
        media_cache: dict[str, dict[str, str]] | None = None,
        persist_media_cache: Callable[[dict[str, dict[str, str]]], None] | None = None,
    ) -> DeliveryResult:
        del media_cache, persist_media_cache
        existing = self.find_by_idempotency_key(key)
        if existing:
            return DeliveryResult(existing, {"verified": True}, True)

        assets = _media_assets(values.get("media"))
        local_media = [self._local_media(item, max_bytes=500 * 1024 * 1024) for item in assets if item.get("local_path")]
        marker = self._marker(key)
        if not self._find_marker(marker):
            content = self._render(key, values)
            if not local_media:
                content = f"{content}\n{self._complete_marker(key)}"
            self._append(content)
        elif not local_media:
            self._append(self._complete_marker(key))

        media_refs: list[str] = []
        if local_media:
            known_media = self._media_refs()
            for item in local_media:
                media_marker = self._media_marker(key, str(item["sha256"]))
                if self._find_marker(media_marker):
                    continue
                file_key = Path(str(item["relative_path"])).name.casefold()
                block_id = known_media.get(file_key)
                if not block_id:
                    value = self.run_json(
                        [
                            "doc",
                            "+media-insert",
                            "--node",
                            self.destination.url,
                            "--file",
                            str(item["relative_path"]),
                        ],
                        timeout=120,
                    )
                    block_id = str(_find_scalar(value, ("blockId", "block_id")) or "")
                    if not block_id:
                        block_id = self._media_refs().get(file_key, "")
                    if not block_id:
                        raise DWSRuntimeError("普通文档媒体插入后未返回或回读到稳定 blockId")
                    known_media[file_key] = block_id
                self._append(media_marker)
                if not self._find_marker(media_marker):
                    raise DWSRuntimeError("普通文档媒体写入后未回读到媒体幂等标记")
                media_refs.append(block_id)
            self._append(self._complete_marker(key))

        verified = self.find_by_idempotency_key(key)
        if not verified:
            raise DWSRuntimeError("普通文档写入后未找到 EIM 完成标记")
        return DeliveryResult(verified, {"verified": True, "media_block_ids": media_refs})

    def _append(self, content: str) -> None:
        self.run_json(
            [
                "doc",
                "+doc-append",
                "--doc",
                self.destination.url,
                "--content",
                content,
                "--yes",
            ]
        )

    def _media_refs(self) -> dict[str, str]:
        value = self.run_json(["doc", "+media-list", "--node", self.destination.url])
        result: dict[str, str] = {}
        for item in _find_dicts(value):
            name = str(_find_scalar(item, ("fileName", "file_name", "name")) or "").casefold()
            reference = str(_find_scalar(item, ("blockId", "block_id", "resourceId", "resource_id")) or "")
            if name and reference:
                result[Path(name).name] = reference
        return result

    def _render(self, key: str, values: dict[str, Any]) -> str:
        lines = [self._marker(key)]
        for field in ("title", "body", "metadata", "media"):
            value = values.get(field)
            if value not in (None, "", [], {}):
                if field == "media":
                    value = _public_media(value)
                rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                lines.append(f"{field}: {rendered}")
        content = "\n".join(lines)
        if len(content.encode("utf-8")) > 24_000:
            raise ValueError("普通文档单条归档超过 Windows 安全命令长度")
        return content


class DingTalkSheetAdapter(_BaseAdapter):
    def resolve(self) -> dict[str, Any]:
        value = self.run_json(["sheet", "+list-sheets", "--node", self.destination.url])
        sheets = _find_list(value, ("sheets", "items", "list"))
        return {"sheets": sheets, "readable": True}

    def inspect_schema(self) -> dict[str, Any]:
        sheet_id = self._sheet_id()
        value = self.run_json(
            [
                "sheet",
                "+read",
                "--node",
                self.destination.url,
                "--sheet-id",
                sheet_id,
                "--range",
                "A1:ZZ1",
                "--value-render-option",
                "formatted_value",
            ]
        )
        rows = _find_list(value, ("values", "rows"))
        headers = [str(item).strip() for item in rows[0]] if rows and isinstance(rows[0], list) else []
        return {"sheet_id": sheet_id, "headers": headers, "readable": True}

    def target_fields(self) -> set[str]:
        headers = self.destination.schema_snapshot.get("headers") or []
        return {str(item) for item in headers}

    def validate_write_capability(self) -> None:
        headers = [str(item) for item in self.destination.schema_snapshot.get("headers") or []]
        if _EVENT_COLUMN not in headers:
            raise ValueError(f"电子表格必须包含幂等列 {_EVENT_COLUMN}")
        row = ["ForTest EIM 写入权限预检" if item == _EVENT_COLUMN else "" for item in headers]
        self.run_json(
            [
                "sheet",
                "append",
                "--node",
                self.destination.url,
                "--sheet-id",
                self._sheet_id(),
                "--values",
                json.dumps([row], ensure_ascii=False),
                "--dry-run",
            ]
        )

    def find_by_idempotency_key(self, key: str) -> str | None:
        value = self.run_json(
            [
                "sheet",
                "find",
                "--node",
                self.destination.url,
                "--sheet-id",
                self._sheet_id(),
                "--query",
                key,
                "--match-entire-cell",
            ]
        )
        matches = _find_list(value, ("matches", "items", "cells"))
        if not matches:
            return None
        if len(matches) > 1:
            raise DWSRuntimeError("电子表格存在重复 EIM 幂等键")
        return str(
            _find_scalar(matches[0], ("address", "range", "cell", "cellAddress"))
            or key
        )

    def deliver(
        self,
        key: str,
        values: dict[str, Any],
        *,
        media_cache: dict[str, dict[str, str]] | None = None,
        persist_media_cache: Callable[[dict[str, dict[str, str]]], None] | None = None,
    ) -> DeliveryResult:
        existing = self.find_by_idempotency_key(key)
        if existing:
            return DeliveryResult(existing, {"verified": True}, True)
        headers = [str(item) for item in self.destination.schema_snapshot.get("headers") or []]
        if _EVENT_COLUMN not in headers:
            raise ValueError(f"电子表格映射缺少 {_EVENT_COLUMN}")
        row_values = dict(values)
        row_values[_EVENT_COLUMN] = key
        unknown = set(row_values) - set(headers)
        if unknown:
            raise ValueError(f"电子表格目标列不存在：{', '.join(sorted(unknown))}")
        cache = media_cache if media_cache is not None else {}
        for field, value in list(row_values.items()):
            assets = _media_assets(value)
            if assets:
                row_values[field] = self._media_links(assets, cache, persist_media_cache)
        row = [_sheet_value(row_values.get(header, "")) for header in headers]
        self.run_json(
            [
                "sheet",
                "append",
                "--node",
                self.destination.url,
                "--sheet-id",
                self._sheet_id(),
                "--values",
                json.dumps([row], ensure_ascii=False, separators=(",", ":")),
            ]
        )
        verified = self.find_by_idempotency_key(key)
        if not verified:
            raise DWSRuntimeError("电子表格追加后未回读到 EIM 幂等键")
        return DeliveryResult(verified, {"verified": True, "media_uploads": len(cache)})

    def _media_links(
        self,
        assets: list[dict[str, Any]],
        cache: dict[str, dict[str, str]],
        persist: Callable[[dict[str, dict[str, str]]], None] | None,
    ) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        for asset in assets:
            stable_url = _safe_stable_url(asset.get("stable_url"))
            media: dict[str, Any] | None = None
            if asset.get("local_path"):
                local = self._local_media(asset, max_bytes=500 * 1024 * 1024)
                cache_key = str(local["sha256"])
                cached = cache.get(cache_key) or {}
                resource_url = str(cached.get("resource_url") or "")
                resource_id = str(cached.get("resource_id") or "")
                if not resource_url:
                    result = self.run_json(
                        [
                            "sheet",
                            "media-upload",
                            "--node",
                            self.destination.url,
                            "--file",
                            str(local["relative_path"]),
                            "--name",
                            str(local["file_name"]),
                            "--mime-type",
                            str(local["mime_type"]),
                        ],
                        timeout=120,
                    )
                    resource_url = str(_find_scalar(result, ("resourceUrl", "resource_url")) or "")
                    resource_id = str(_find_scalar(result, ("resourceId", "resource_id")) or "")
                    if not resource_url or not resource_id:
                        raise DWSRuntimeError("电子表格媒体上传后缺少 resourceId/resourceUrl")
                    cache[cache_key] = {
                        "resource_url": resource_url,
                        "resource_id": resource_id,
                    }
                    if persist:
                        persist(cache)
                media = {
                    "name": local["file_name"],
                    "mime_type": local["mime_type"],
                    "size": local["size"],
                    "sha256": local["sha256"],
                    "url": resource_url,
                }
            elif stable_url:
                media = {
                    "name": Path(str(asset.get("file_name") or "media")).name,
                    "mime_type": str(asset.get("mime_type") or "application/octet-stream"),
                    "size": asset.get("size"),
                    "url": stable_url,
                }
            if media:
                links.append(media)
        if len(links) != len(assets):
            raise ValueError("电子表格媒体缺少可信本地文件或 HTTPS 稳定链接")
        return links

    def _sheet_id(self) -> str:
        value = str(self.destination.stable_ids.get("sheet_id") or "").strip()
        if not value:
            raise ValueError("电子表格尚未绑定稳定 sheetId")
        return value


class DingTalkAITableAdapter(_BaseAdapter):
    def resolve(self) -> dict[str, Any]:
        value = self.run_json(
            ["aitable", "+url-resolve", "--url", self.destination.url, "--verify"]
        )
        base_id = str(_find_scalar(value, ("baseId", "base_id")) or "")
        table_id = str(_find_scalar(value, ("tableId", "table_id")) or "")
        if not base_id or not table_id:
            raise DWSRuntimeError("AI 表格链接未解析出稳定 baseId/tableId")
        return {"base_id": base_id, "table_id": table_id, "readable": True}

    def inspect_schema(self) -> dict[str, Any]:
        base_id, table_id = self._ids()
        value = self.run_json(
            ["aitable", "table", "get", "--base-id", base_id, "--table-ids", table_id]
        )
        tables = _find_list(value, ("tables", "items"))
        table = next(
            (item for item in tables if str(item.get("tableId") or item.get("table_id")) == table_id),
            None,
        )
        if not isinstance(table, dict):
            raise DWSRuntimeError("AI 表格未返回目标数据表结构")
        fields = table.get("fields") or []
        writable = [
            item
            for item in fields
            if isinstance(item, dict)
            and str(item.get("type") or "").casefold() not in _READ_ONLY_AITABLE_TYPES
        ]
        event_field = str(self.destination.stable_ids.get("event_key_field_id") or "")
        event_definition = next(
            (
                item
                for item in writable
                if str(item.get("fieldId") or item.get("field_id")) == event_field
            ),
            None,
        )
        if event_field and (
            event_definition is None
            or str(event_definition.get("type") or event_definition.get("fieldType") or "").casefold()
            not in _TEXT_FIELD_TYPES
        ):
            raise ValueError("AI 表格 EIM 事件 ID 字段不存在、只读或类型不兼容")
        return {"base_id": base_id, "table_id": table_id, "fields": fields, "writable_fields": writable}

    def target_fields(self) -> set[str]:
        fields = self.destination.schema_snapshot.get("writable_fields") or []
        return {
            str(item.get("fieldId") or item.get("field_id"))
            for item in fields
            if isinstance(item, dict)
        }

    def _attachment_fields(self) -> set[str]:
        fields = self.destination.schema_snapshot.get("writable_fields") or []
        return {
            str(item.get("fieldId") or item.get("field_id"))
            for item in fields
            if isinstance(item, dict)
            and str(item.get("type") or item.get("fieldType") or "").casefold()
            == _ATTACHMENT_FIELD_TYPE
        }

    def _user_fields(self) -> set[str]:
        fields = self.destination.schema_snapshot.get("writable_fields") or []
        return {
            str(item.get("fieldId") or item.get("field_id"))
            for item in fields
            if isinstance(item, dict)
            and str(item.get("type") or item.get("fieldType") or "").casefold()
            == _USER_FIELD_TYPE
        }

    def validate_values(self, values: dict[str, Any]) -> None:
        for field in self._user_fields() & set(values):
            self._user_value(values[field])
        for field in self._attachment_fields() & set(values):
            value = values[field]
            if value not in (None, [], {}) and not _media_assets(value):
                raise ValueError(f"AI 表格附件字段 {field} 必须映射 media_assets")

    def prepare_values(
        self,
        values: dict[str, Any],
        mappings: dict[str, str],
        event: CanonicalEvent,
    ) -> dict[str, Any]:
        """把事件开放 ID 精确解析为 AI 表格人员字段要求的 userId。"""

        result = dict(values)
        user_id = ""
        for target, source in mappings.items():
            if target not in self._user_fields() or source not in {"sender.id", "sender.name"}:
                continue
            if not user_id:
                user_id = self._resolve_sender_user_id(event.sender_id, event.sender_name)
            result[target] = user_id
        return result

    def _resolve_sender_user_id(self, open_dingtalk_id: str, sender_name: str) -> str:
        if not open_dingtalk_id or not sender_name:
            raise ValueError("AI 表格人员字段无法解析缺少标识或姓名的发送者")
        value = self.run_json(
            ["aisearch", "person", "--query", sender_name, "--dimension", "name"]
        )
        matches = {
            str(item.get("userId") or item.get("user_id") or "").strip()
            for item in _find_list(value, ("result",))
            if isinstance(item, dict)
            and str(
                item.get("openDingTalkId")
                or item.get("open_dingtalk_id")
                or ""
            ).strip()
            == open_dingtalk_id
            and str(item.get("userId") or item.get("user_id") or "").strip()
        }
        if len(matches) != 1:
            raise ValueError("无法将发送者开放 ID 唯一解析为 AI 表格人员 userId")
        return matches.pop()

    def validate_write_capability(self) -> None:
        base_id, table_id = self._ids()
        event_field = self._event_field()
        self.run_json(
            [
                "aitable",
                "+record-upsert-by-key",
                "--base-id",
                base_id,
                "--table-id",
                table_id,
                "--key-field-id",
                event_field,
                "--key-value",
                "ForTest-EIM-PREFLIGHT",
                "--cells",
                json.dumps({event_field: "ForTest-EIM-PREFLIGHT"}),
                "--dry-run",
                "--yes",
            ]
        )

    def find_by_idempotency_key(self, key: str) -> str | None:
        base_id, table_id = self._ids()
        event_field = self._event_field()
        filters = {
            "operator": "and",
            "operands": [{"operator": "eq", "operands": [event_field, key]}],
        }
        value = self.run_json(
            [
                "aitable",
                "record",
                "query",
                "--base-id",
                base_id,
                "--table-id",
                table_id,
                "--filters",
                json.dumps(filters, separators=(",", ":")),
                "--limit",
                "2",
            ]
        )
        records = _find_list(value, ("records", "items"))
        if len(records) > 1:
            raise DWSRuntimeError("AI 表格存在重复 EIM 幂等键")
        if not records:
            return None
        return str(_find_scalar(records[0], ("recordId", "record_id", "id")) or key)

    def deliver(
        self,
        key: str,
        values: dict[str, Any],
        *,
        media_cache: dict[str, dict[str, str]] | None = None,
        persist_media_cache: Callable[[dict[str, dict[str, str]]], None] | None = None,
    ) -> DeliveryResult:
        base_id, table_id = self._ids()
        event_field = self._event_field()
        # 钉钉会省略空单元格；不发送空可选值，避免写入成功后回读被误判为不一致。
        cells = {
            field: value
            for field, value in values.items()
            if value not in (None, "", [], {})
        }
        cells[event_field] = key
        unknown = set(cells) - self.target_fields()
        if unknown:
            raise ValueError(f"AI 表格目标字段不存在或只读：{', '.join(sorted(unknown))}")
        self.validate_values(cells)
        for field in self._user_fields() & set(cells):
            cells[field] = self._user_value(cells[field])
        cache = media_cache if media_cache is not None else {}
        for field in self._attachment_fields() & set(cells):
            cells[field] = self._attachment_tokens(
                base_id,
                _media_assets(cells[field]),
                cache,
                persist_media_cache,
            )
        value = self.run_json(
            [
                "aitable",
                "+record-upsert-by-key",
                "--base-id",
                base_id,
                "--table-id",
                table_id,
                "--key-field-id",
                event_field,
                "--key-value",
                key,
                "--cells",
                json.dumps(cells, ensure_ascii=False, separators=(",", ":")),
                "--yes",
            ]
        )
        record_id = str(_find_scalar(value, ("recordId", "record_id", "id")) or "")
        verified = self.find_by_idempotency_key(key)
        if not verified:
            raise DWSRuntimeError("AI 表格 upsert 后未回读到 EIM 幂等键")
        return DeliveryResult(
            verified or record_id,
            {"verified": True, "media_uploads": len(cache)},
        )

    def _user_value(self, value: Any) -> list[dict[str, str]]:
        if value in (None, "", [], {}):
            return []
        if isinstance(value, str):
            users = [{"userId": value}]
        elif isinstance(value, dict):
            users = [value]
        elif isinstance(value, list) and all(isinstance(item, dict) for item in value):
            users = value
        else:
            raise ValueError("AI 表格人员字段必须是用户 ID 或人员对象列表")
        normalized: list[dict[str, str]] = []
        for user in users:
            user_id = str(user.get("userId") or user.get("user_id") or "").strip()
            corp_id = str(
                user.get("corpId")
                or user.get("corp_id")
                or self.connection.organization_id
                or ""
            ).strip()
            if not user_id or not corp_id:
                raise ValueError("AI 表格人员字段缺少 userId 或 corpId")
            normalized.append({"userId": user_id, "corpId": corp_id})
        return normalized

    def _attachment_tokens(
        self,
        base_id: str,
        assets: list[dict[str, Any]],
        cache: dict[str, dict[str, str]],
        persist: Callable[[dict[str, dict[str, str]]], None] | None,
    ) -> list[dict[str, str]]:
        tokens: list[dict[str, str]] = []
        for asset in assets:
            local = self._local_media(asset, max_bytes=100 * 1024 * 1024)
            cache_key = str(local["sha256"])
            cached = cache.get(cache_key) or {}
            file_token = str(cached.get("file_token") or "")
            if not _FILE_TOKEN.fullmatch(file_token):
                value = self.run_json(
                    [
                        "aitable",
                        "attachment",
                        "upload",
                        "--base-id",
                        base_id,
                        "--file-name",
                        str(local["file_name"]),
                        "--size",
                        str(local["size"]),
                        "--mime-type",
                        str(local["mime_type"]),
                    ],
                    timeout=60,
                )
                upload_url = str(_find_scalar(value, ("uploadUrl", "upload_url")) or "")
                file_token = str(_find_scalar(value, ("fileToken", "file_token")) or "")
                if not upload_url or not _FILE_TOKEN.fullmatch(file_token):
                    raise DWSRuntimeError("AI 表格附件上传准备结果缺少合法 uploadUrl/fileToken")
                _put_https_file(
                    upload_url,
                    Path(local["path"]),
                    str(local["mime_type"]),
                    int(local["size"]),
                )
                cache[cache_key] = {"file_token": file_token}
                if persist:
                    persist(cache)
            tokens.append({"fileToken": file_token})
        return tokens

    def _ids(self) -> tuple[str, str]:
        base_id = str(self.destination.stable_ids.get("base_id") or "").strip()
        table_id = str(self.destination.stable_ids.get("table_id") or "").strip()
        if not base_id or not table_id:
            raise ValueError("AI 表格尚未绑定稳定 baseId/tableId")
        return base_id, table_id

    def _event_field(self) -> str:
        value = str(self.destination.stable_ids.get("event_key_field_id") or "").strip()
        if not value:
            raise ValueError("AI 表格必须绑定专用 EIM 事件 ID 文本字段")
        return value


def destination_adapter(
    runtime: DWSRuntime,
    connection: EIMConnection,
    destination: EIMDestination,
) -> _BaseAdapter:
    classes = {
        DestinationType.DINGTALK_DOC: DingTalkDocAdapter,
        DestinationType.DINGTALK_SHEET: DingTalkSheetAdapter,
        DestinationType.DINGTALK_AITABLE: DingTalkAITableAdapter,
    }
    return classes[destination.destination_type](runtime, connection, destination)


def _find_scalar(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if value.get(key) not in (None, ""):
                return value[key]
        for item in value.values():
            found = _find_scalar(item, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_scalar(item, keys)
            if found not in (None, ""):
                return found
    return None


def _find_list(value: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), list):
                return value[key]
        for item in value.values():
            found = _find_list(item, keys)
            if found:
                return found
    return []


def _find_dicts(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        result.append(value)
        for item in value.values():
            result.extend(_find_dicts(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(_find_dicts(item))
    return result


def _media_assets(value: Any) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else [value]
    if not items or not all(isinstance(item, dict) for item in items):
        return []
    media_keys = {"resource_id", "local_path", "stable_url", "sha256", "mime_type", "file_name"}
    return [dict(item) for item in items if media_keys & set(item)]


def _public_media(value: Any) -> Any:
    assets = _media_assets(value)
    if not assets:
        return value
    allowed = ("file_name", "mime_type", "size", "sha256", "stable_url")
    return [{key: item[key] for key in allowed if item.get(key) not in (None, "")} for item in assets]


def _safe_stable_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 4096:
        return ""
    parsed = urlsplit(text)
    return text if parsed.scheme == "https" and parsed.hostname and not parsed.username else ""


def _put_https_file(url: str, path: Path, mime_type: str, size: int) -> None:
    """流式 PUT 官方签名地址；不跟随重定向，也不把签名 URL 写入错误。"""

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        raise DWSRuntimeError("AI 表格返回了不安全的附件上传端口") from None
    if (
        len(url) > 8192
        or parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or host.casefold() == "localhost"
        or host.casefold().endswith(".local")
    ):
        raise DWSRuntimeError("AI 表格返回了不安全的附件上传地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or len(host) > 253:
            raise DWSRuntimeError("AI 表格返回了不安全的附件上传主机") from None
    else:
        if not address.is_global:
            raise DWSRuntimeError("AI 表格附件上传地址指向非公网主机")

    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    connection = http.client.HTTPSConnection(host, port or 443, timeout=120)
    try:
        with path.open("rb") as stream:
            connection.request(
                "PUT",
                target,
                body=stream,
                headers={"Content-Type": mime_type, "Content-Length": str(size)},
            )
            response = connection.getresponse()
            response.read(4096)
            if not 200 <= response.status < 300:
                raise DWSRuntimeError(f"AI 表格附件上传失败（HTTP {response.status}）")
    except (OSError, http.client.HTTPException) as exc:
        raise DWSRuntimeError(f"AI 表格附件上传失败：{type(exc).__name__}") from None
    finally:
        connection.close()


def _sheet_value(value: Any) -> Any:
    if isinstance(value, int) and abs(value) >= 10**15:
        return str(value)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return neutralize_spreadsheet_formula(value) if isinstance(value, str) else value
