"""EIM 页面使用的应用服务门面。"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.eim.ai_runtime import EIMAIRuntime
from backend.eim.builder.orchestrator import EIMBuilder
from backend.eim.builder.simulator import run_samples
from backend.eim.connections.dingtalk import DingTalkConnector, SELF_LOOP_NOTICE
from backend.eim.connections.dws_runtime import DWSRuntime
from backend.eim.destinations.dingtalk import destination_adapter
from backend.eim.import_export import export_task, import_task
from backend.eim.models import (
    ConnectionState,
    DestinationType,
    EIMDSL,
    EIMDestination,
    EIMTask,
    EventType,
    ObservedState,
)
from backend.eim.repository import EIMRepository
from backend.eim.runtime.media import MediaManager
from backend.eim.runtime.supervisor import EIMSupervisor


class EIMApplicationService:
    def __init__(
        self,
        data_root: Path,
        *,
        settings_service: Any = None,
        codex_path_resolver: Any = None,
    ):
        self.data_root = Path(data_root).resolve()
        self.repository = EIMRepository(self.data_root / "data" / "eim.db")
        self.runtime = DWSRuntime(self.data_root)
        self.connector = DingTalkConnector(self.runtime, self.repository)
        self.supervisor = EIMSupervisor(self.repository, self.connector, self.runtime)
        self.ai_runtime = EIMAIRuntime(
            self.repository,
            self.data_root,
            settings_service=settings_service,
            codex_path_resolver=codex_path_resolver,
            stop_callback=lambda task_id: self.supervisor.stop_task(task_id),
        )
        self.supervisor.pipeline.ai_processor = self.ai_runtime.process
        self.builder = EIMBuilder(
            self.repository,
            self.connector,
            self.runtime,
            self.data_root,
            settings_service=settings_service,
            codex_path_resolver=codex_path_resolver,
            start_callback=self.supervisor.start_task,
        )

    def start_background(self, *, restore: bool = True) -> list[str]:
        self.supervisor.start_background()
        return self.supervisor.restore_desired() if restore else []

    def shutdown(self) -> None:
        self.supervisor.stop_all(app_exit=True)

    def overview(self) -> dict[str, Any]:
        connections = self.repository.list_connections()
        connected = [
            item for item in connections
            if item.connection_state is ConnectionState.CONNECTED
        ]
        task_counts = self.repository.task_counts_today()
        tasks = []
        for item in self.repository.list_tasks():
            view = self._task_view(item)
            view.update(task_counts.get(item.task_id, {"archived_today": 0, "failed_today": 0}))
            tasks.append(view)
        return {
            **self.repository.count_overview(),
            "connected": bool(connected),
            "needs_authorization": sum(
                item.connection_state is not ConnectionState.CONNECTED
                for item in connections
            ),
            "connections": [self._connection_view(item) for item in connected],
            "self_loop_notice": SELF_LOOP_NOTICE,
            "tasks": tasks,
        }

    def create_connection(self) -> dict[str, Any]:
        return self._connection_view(self.connector.create_connection())

    def connections(self) -> list[dict[str, Any]]:
        return [self._connection_view(item) for item in self.repository.list_connections()]

    def authorize_connection(
        self,
        connection_id: str,
        *,
        device: bool = False,
        no_browser: bool = False,
    ) -> dict[str, Any]:
        return self._connection_view(
            self.connector.authorize(connection_id, device=device, no_browser=no_browser)
        )

    def refresh_connection(self, connection_id: str) -> dict[str, Any]:
        return self._connection_view(self.connector.refresh(connection_id))

    def groups(self, connection_id: str) -> list[dict[str, str]]:
        return self.connector.list_groups(connection_id)

    def create_task(
        self,
        *,
        name: str,
        connection_id: str,
        source_id: str,
        source_name: str,
        destination_url: str,
    ) -> dict[str, Any]:
        normalized_name = str(name).strip()
        if not normalized_name:
            raise ValueError("任务名称不能为空")
        if any(
            item.name.casefold() == normalized_name.casefold()
            for item in self.repository.list_tasks()
        ):
            raise ValueError("任务名称已存在，请使用可区分的名称")
        connection = self.repository.get_connection(connection_id)
        if connection is None or connection.connection_state is not ConnectionState.CONNECTED:
            raise ValueError("钉钉连接不可用")
        visible = {item["id"]: item for item in self.connector.list_groups(connection_id)}
        if source_id not in visible:
            raise ValueError("来源群不属于当前连接或已不可见")
        if visible[source_id]["name"] != source_name:
            source_name = visible[source_id]["name"]
        destination = self._resolve_destination(connection, destination_url)
        self.repository.save_destination(destination)
        task = self.repository.create_task(
            EIMTask(
                name=normalized_name,
                connection_id=connection_id,
                source_id=source_id,
                source_name=source_name,
                destination_id=destination.destination_id,
            )
        )
        self.builder.save_draft(task.task_id, self._default_dsl(destination))
        return self.task_detail(task.task_id)

    def task_detail(self, task_id: str) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(f"EIM 任务不存在：{task_id}")
        destination = self.repository.get_destination(task.destination_id)
        versions = self.repository.list_versions(task_id)
        return {
            "task": self._task_view(task),
            "destination": destination.model_dump(mode="json") if destination else None,
            "dsl": self.builder.load_draft(task_id).model_dump(mode="json"),
            "samples": self.repository.list_samples(task_id),
            "versions": [item.model_dump(mode="json") for item in versions],
            "logs": self.repository.list_logs(task_id=task_id, limit=200),
            "self_loop_notice": SELF_LOOP_NOTICE,
        }

    def save_task(
        self,
        task_id: str,
        *,
        name: str,
        dsl: dict[str, Any],
    ) -> dict[str, Any]:
        validated = EIMDSL.model_validate(dsl)
        task = self.repository.update_task_draft(
            task_id,
            name=name,
            event_types=list(validated.triggers),
        )
        self.builder.save_draft(task_id, validated)
        return self.task_detail(task.task_id)

    def add_sample(
        self,
        task_id: str,
        event: dict[str, Any],
        expected: dict[str, Any],
    ) -> dict[str, Any]:
        self.repository.add_sample(task_id, event, expected)
        return self.task_detail(task_id)

    def simulate_draft(self, task_id: str) -> dict[str, Any]:
        """用当前草稿运行全部脱敏样例，不写目标。"""

        return run_samples(
            self.builder.load_draft(task_id),
            self.repository.list_samples(task_id),
            target_fields=self.builder._target_fields(task_id),
        )

    def configure_destination(
        self,
        task_id: str,
        stable_ids: dict[str, str],
    ) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if task is None or task.observed_state is not ObservedState.STOPPED:
            raise ValueError("任务必须停止后才能修改目标绑定")
        destination = self.repository.get_destination(task.destination_id)
        connection = self.repository.get_connection(task.connection_id)
        if destination is None or connection is None:
            raise ValueError("任务连接或归档目标不存在")
        requested = {str(key): str(value).strip() for key, value in stable_ids.items()}
        current = dict(destination.stable_ids)
        if destination.destination_type is DestinationType.DINGTALK_SHEET:
            selected = requested.get("sheet_id", "")
            available = {
                _sheet_id(item)
                for item in destination.schema_snapshot.get("sheets") or []
                if isinstance(item, dict)
            }
            if not selected or selected not in available:
                raise ValueError("电子表格只能绑定目标链接真实返回的工作表")
            destination.stable_ids = {"sheet_id": selected}
        elif destination.destination_type is DestinationType.DINGTALK_AITABLE:
            selected = requested.get("event_key_field_id", "")
            text_fields = {
                str(item.get("fieldId") or item.get("field_id") or "")
                for item in destination.schema_snapshot.get("writable_fields") or []
                if isinstance(item, dict)
                and str(item.get("type") or item.get("fieldType") or "").casefold()
                in {"text", "string", "1"}
            }
            if not selected or selected not in text_fields:
                raise ValueError("AI 表格事件 ID 必须绑定真实可写文本字段")
            if selected in self.builder.load_draft(task_id).mappings:
                raise ValueError("EIM 事件 ID 必须使用未参与字段映射的专用文本字段")
            # URL 解析得到的 base/table 不允许由工作台输入覆盖。
            destination.stable_ids = {
                "base_id": str(current.get("base_id") or ""),
                "table_id": str(current.get("table_id") or ""),
                "event_key_field_id": selected,
            }
        else:
            destination.stable_ids = current
        self._refresh_destination_schema(destination, connection)
        self.repository.update_task_draft(task_id)
        return self.task_detail(task_id)

    def refresh_destination(self, task_id: str) -> dict[str, Any]:
        """重新读取目标结构，不修改目标绑定或业务字段。"""

        task = self.repository.get_task(task_id)
        if task is None or task.observed_state is not ObservedState.STOPPED:
            raise ValueError("任务必须停止后才能刷新目标结构")
        destination = self.repository.get_destination(task.destination_id)
        connection = self.repository.get_connection(task.connection_id)
        if destination is None or connection is None:
            raise ValueError("任务连接或归档目标不存在")
        self._refresh_destination_schema(destination, connection)
        self.repository.update_task_draft(task_id)
        return self.task_detail(task_id)

    def build_task(
        self,
        task_id: str,
        *,
        configuration_id: str | None = None,
        instruction: str = "",
        start_after: bool = False,
    ) -> dict[str, Any]:
        return self.builder.build(
            task_id,
            configuration_id=configuration_id,
            instruction=instruction,
            start_after=start_after,
        )

    def cancel_build(self, task_id: str) -> bool:
        return self.builder.cancel(task_id)

    def start_task(self, task_id: str) -> dict[str, Any]:
        self.supervisor.start_task(task_id)
        return self.task_detail(task_id)

    def stop_task(self, task_id: str) -> dict[str, Any]:
        self.supervisor.stop_task(task_id, app_exit=False)
        return self.task_detail(task_id)

    def copy_task(self, task_id: str) -> dict[str, Any]:
        source = self.repository.get_task(task_id)
        if source is None:
            raise KeyError(f"EIM 任务不存在：{task_id}")
        copied = self.repository.copy_task(task_id)
        active = (
            self.repository.get_version(source.active_version_id)
            if source.active_version_id
            else None
        )
        copied_dsl = (
            EIMDSL.model_validate(active.dsl)
            if active is not None
            else self.builder.load_draft(task_id)
        )
        self.builder.save_draft(copied.task_id, copied_dsl)
        for sample in self.repository.list_samples(task_id):
            self.repository.add_sample(
                copied.task_id,
                sample["input"],
                sample["expected"],
                source="copied",
            )
        return self.task_detail(copied.task_id)

    def delete_task(self, task_id: str) -> dict[str, Any]:
        return self._task_view(self.repository.soft_delete_task(task_id))

    def recycle_bin(self) -> list[dict[str, Any]]:
        return [
            self._task_view(item)
            for item in self.repository.list_tasks(include_deleted=True)
            if item.deleted_at
        ]

    def restore_task(self, task_id: str) -> dict[str, Any]:
        return self._task_view(self.repository.restore_task(task_id))

    def purge_task(self, task_id: str) -> None:
        task = self.repository.get_task(task_id)
        if task is None:
            return
        self.repository.purge_task(task_id)
        for root in (
            self.data_root / "eim" / "workspaces",
            self.data_root / "eim" / "bundles",
        ):
            target = (root / task_id).resolve()
            if root.resolve() in target.parents and target.is_dir():
                shutil.rmtree(target)
        media_root = (self.data_root / "eim" / "media").resolve()
        media_target = (
            media_root / hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
        ).resolve()
        if media_root in media_target.parents and media_target.is_dir():
            shutil.rmtree(media_target)

    def export_task(self, task_id: str, output_path: str) -> str:
        return str(
            export_task(
                self.repository,
                task_id,
                Path(output_path),
                draft_dsl=self.builder.load_draft(task_id),
            )
        )

    def import_task(
        self,
        archive_path: str,
        *,
        connection_id: str,
        source_id: str,
        source_name: str,
        destination_id: str | None = None,
        destination_url: str | None = None,
    ) -> dict[str, Any]:
        connection = self.repository.get_connection(connection_id)
        if connection is None or connection.connection_state is not ConnectionState.CONNECTED:
            raise ValueError("导入绑定的钉钉连接无效或需要重新授权")
        destination = self.repository.get_destination(destination_id) if destination_id else None
        if destination is None and destination_url:
            destination = self.repository.save_destination(
                self._resolve_destination(connection, destination_url)
            )
        if destination is None or destination.connection_id != connection_id:
            raise ValueError("导入绑定的归档目标无效")
        visible = {item["id"] for item in self.connector.list_groups(connection_id)}
        if source_id not in visible:
            raise ValueError("导入绑定的来源群不可见")
        adapter = destination_adapter(self.runtime, connection, destination)
        destination.schema_snapshot = adapter.inspect_schema()
        self.repository.save_destination(destination)
        task, dsl = import_task(
            self.repository,
            Path(archive_path),
            connection_id=connection_id,
            source_id=source_id,
            source_name=source_name,
            destination_id=destination.destination_id,
        )
        self.builder.save_draft(task.task_id, dsl)
        return self.task_detail(task.task_id)

    def logs(self, **filters: Any) -> list[dict[str, Any]]:
        return self.repository.list_logs(**filters)

    def export_logs(self, output_path: str, *, format: str = "csv", **filters: Any) -> str:
        rows = self.repository.list_logs(**filters)
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        temporary = Path(temporary_name)
        try:
            if format == "json":
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(rows, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
            elif format == "csv":
                with os.fdopen(handle, "w", encoding="utf-8-sig", newline="") as stream:
                    fieldnames = [
                        "timestamp",
                        "task_id",
                        "event_id",
                        "message_id",
                        "stage",
                        "result",
                        "external_ref",
                        "preview",
                        "details",
                    ]
                    writer = csv.DictWriter(stream, fieldnames=fieldnames)
                    writer.writeheader()
                    for row in rows:
                        export_row = {
                            **{key: row.get(key, "") for key in fieldnames},
                            "details": json.dumps(row.get("details") or {}, ensure_ascii=False),
                        }
                        writer.writerow(
                            {key: _csv_cell(value) for key, value in export_row.items()}
                        )
            else:
                os.close(handle)
                raise ValueError("日志导出格式只能是 csv 或 json")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return str(target)

    def retry_delivery(self, delivery_id: str) -> bool:
        if str(delivery_id).startswith("event:"):
            try:
                inbox_id = int(str(delivery_id).partition(":")[2])
            except ValueError:
                return False
            return self.repository.retry_dead_event(inbox_id)
        return self.repository.retry_dead_letter(delivery_id)

    def dead_letters(
        self,
        *,
        task_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        deliveries = [
            {**item, "kind": "delivery"}
            for item in self.repository.list_dead_letters(task_id=task_id, limit=limit)
        ]
        events = [
            {
                **item,
                "kind": "event",
                "delivery_id": f"event:{item['inbox_id']}",
            }
            for item in self.repository.list_dead_events(task_id=task_id, limit=limit)
        ]
        return [*deliveries, *events][: max(1, min(int(limit), 500))]

    def ai_configuration_health(self) -> list[dict[str, Any]]:
        return self.repository.list_ai_configuration_health()

    def test_ai_configuration(self, configuration_id: str) -> dict[str, Any]:
        return self.builder.detect_configuration(configuration_id)

    def run_retention(self, *, log_days: int = 30) -> dict[str, int]:
        now = datetime.now(UTC)
        media = MediaManager(self.data_root, self.runtime)
        removed = 0
        for task in self.repository.list_tasks(include_deleted=True):
            hours = 24
            if task.active_version_id:
                version = self.repository.get_version(task.active_version_id)
                try:
                    if version is not None:
                        hours = EIMDSL.model_validate(version.dsl).media_policy.retention_hours
                except ValueError:
                    pass
            for event in self.repository.list_media_retention_events(task.task_id):
                retention_hours = 24 * 30 if event["has_incomplete_delivery"] else hours
                removed += media.cleanup_event(
                    task.task_id,
                    str(event["event_id"]),
                    before=now - timedelta(hours=retention_hours),
                )
        # 只用全局上限清理已失去任务引用的孤儿媒体。
        removed += media.cleanup(before=now - timedelta(days=30))
        # 媒体目录依赖 event_id 哈希，必须在删除 inbox 映射前完成对应清理。
        result = self.repository.cleanup_retention(
            logs_before=(now - timedelta(days=max(1, log_days))).isoformat(),
            completed_payloads_before=(now - timedelta(hours=24)).isoformat(),
            failed_payloads_before=(now - timedelta(days=30)).isoformat(),
        )
        result["media"] = removed
        return result

    def _resolve_destination(self, connection, url: str) -> EIMDestination:
        errors: list[Exception] = []
        for destination_type in (
            DestinationType.DINGTALK_AITABLE,
            DestinationType.DINGTALK_SHEET,
            DestinationType.DINGTALK_DOC,
        ):
            destination = EIMDestination(
                connection_id=connection.connection_id,
                destination_type=destination_type,
                url=url,
            )
            adapter = destination_adapter(self.runtime, connection, destination)
            try:
                resolved = adapter.resolve()
                if destination_type is DestinationType.DINGTALK_AITABLE:
                    destination.stable_ids = {
                        "base_id": str(resolved["base_id"]),
                        "table_id": str(resolved["table_id"]),
                    }
                    destination.schema_snapshot = adapter.inspect_schema()
                    event_field = _matching_event_field(destination.schema_snapshot)
                    if event_field:
                        destination.stable_ids["event_key_field_id"] = event_field
                elif destination_type is DestinationType.DINGTALK_SHEET:
                    sheets = resolved.get("sheets") or []
                    if not sheets:
                        raise ValueError("电子表格没有可绑定的工作表")
                    first_id = _sheet_id(sheets[0])
                    if not first_id:
                        raise ValueError("电子表格未返回稳定 sheetId")
                    destination.stable_ids = {"sheet_id": first_id}
                    destination.schema_snapshot = {
                        **adapter.inspect_schema(),
                        "sheets": sheets,
                    }
                else:
                    destination.stable_ids = {
                        "document_id": str(resolved.get("document_id") or "")
                    }
                    destination.schema_snapshot = adapter.inspect_schema()
                destination.capabilities = {"read": True, "write_preflight": False}
                destination.checked_at = datetime.now(UTC).isoformat(timespec="milliseconds")
                return destination
            except Exception as exc:
                errors.append(exc)
        raise ValueError("链接无法识别为可读取的钉钉文档、电子表格或 AI 表格") from errors[-1]

    def _refresh_destination_schema(self, destination, connection) -> None:
        """复用同一条只读结构探测路径并保留电子表格工作表清单。"""

        adapter = destination_adapter(self.runtime, connection, destination)
        schema = adapter.inspect_schema()
        if destination.schema_snapshot.get("sheets") and "sheets" not in schema:
            schema["sheets"] = destination.schema_snapshot["sheets"]
        destination.schema_snapshot = schema
        destination.checked_at = datetime.now(UTC).isoformat(timespec="milliseconds")
        self.repository.save_destination(destination)

    @staticmethod
    def _default_dsl(destination: EIMDestination) -> EIMDSL:
        if destination.destination_type is DestinationType.DINGTALK_DOC:
            mappings = {
                "title": "sender.name",
                "body": "message.text",
                "metadata": "event.id",
                "media": "media",
            }
            action = "append"
        elif destination.destination_type is DestinationType.DINGTALK_SHEET:
            headers = [str(item) for item in destination.schema_snapshot.get("headers") or []]
            data_column = next((item for item in headers if item != "_eim_event_id"), None)
            mappings = {"_eim_event_id": "event.id"}
            if data_column:
                mappings[data_column] = "message.text"
            action = "append"
        else:
            fields = destination.schema_snapshot.get("writable_fields") or []
            event_field = destination.stable_ids.get("event_key_field_id")
            # 默认只把文本写入文本字段；只有附件字段可用时才映射受控媒体。
            first_text = next(
                (
                    str(item.get("fieldId") or item.get("field_id"))
                    for item in fields
                    if isinstance(item, dict)
                    and str(item.get("fieldId") or item.get("field_id")) != event_field
                    and str(item.get("type") or item.get("fieldType") or "").casefold()
                    == "text"
                ),
                None,
            )
            first_attachment = next(
                (
                    str(item.get("fieldId") or item.get("field_id"))
                    for item in fields
                    if isinstance(item, dict)
                    and str(item.get("fieldId") or item.get("field_id")) != event_field
                    and str(item.get("type") or item.get("fieldType") or "").casefold()
                    == "attachment"
                ),
                None,
            )
            mappings = {event_field: "event.id"} if event_field else {}
            if first_text:
                mappings[first_text] = "message.text"
            elif first_attachment:
                mappings[first_attachment] = "media"
            action = "upsert"
        return EIMDSL(mappings=mappings, destination_action=action)

    @staticmethod
    def _connection_view(value) -> dict[str, Any]:
        return {
            **value.model_dump(mode="json"),
            "self_loop_notice": SELF_LOOP_NOTICE,
        }

    @staticmethod
    def _task_view(value: EIMTask) -> dict[str, Any]:
        return {
            **value.model_dump(mode="json"),
            "editable": value.observed_state is ObservedState.STOPPED,
            "self_loop_notice": SELF_LOOP_NOTICE,
        }


def _sheet_id(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(
        value.get("sheetId")
        or value.get("sheet_id")
        or value.get("id")
        or ""
    )


def _matching_event_field(schema: dict[str, Any]) -> str:
    candidates = {"_eim_event_id", "fortest事件id", "eim事件id"}
    for item in schema.get("writable_fields") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("fieldName") or item.get("field_name") or "").casefold()
        if name in candidates and str(item.get("type") or "").casefold() in {"text", "string", "1"}:
            return str(item.get("fieldId") or item.get("field_id") or "")
    return ""


def _csv_cell(value: Any) -> Any:
    """阻止日志内容在 Excel 等表格软件中被解释为公式。"""

    if not isinstance(value, str):
        return value
    return f"'{value}" if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
