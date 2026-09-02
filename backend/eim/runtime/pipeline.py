"""EIM inbox → DSL → outbox 的确定性流水线。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.eim.builder.compiler import compile_dsl
from backend.eim.ai_runtime import AIRetryEvent, AISkipEvent
from backend.eim.destinations.dingtalk import destination_adapter
from backend.eim.models import CanonicalEvent, ContextPolicy, MediaAsset
from backend.eim.repository import EIMRepository
from backend.eim.runtime.media import MediaManager


class EIMPipeline:
    def __init__(
        self,
        repository: EIMRepository,
        runtime: Any,
        *,
        ai_processor: Callable[[list[Any], dict[str, Any], CanonicalEvent, str], dict[str, Any]] | None = None,
        media_manager: MediaManager | None = None,
    ):
        self.repository = repository
        self.runtime = runtime
        self.ai_processor = ai_processor
        self.media_manager = media_manager or (
            MediaManager(runtime.data_root, runtime) if hasattr(runtime, "data_root") else None
        )

    def process_claimed(self, claimed: dict[str, Any]) -> None:
        task_id = str(claimed["task_id"])
        event = CanonicalEvent.model_validate(claimed["event"])
        failure_policy = "dead_letter"
        try:
            task = self.repository.get_task(task_id)
            if task is not None and not task.accepts_work:
                # 停止动作发生在领取之后时，把尚未开始的事件退回队列等待重启。
                self.repository.set_event_state(task_id, event.event_id, "received")
                return
            if task is None or not task.active_version_id:
                raise ValueError("任务没有可运行版本")
            version = self.repository.get_version(task.active_version_id)
            connection = self.repository.get_connection(task.connection_id)
            destination = self.repository.get_destination(task.destination_id)
            if version is None or connection is None or destination is None:
                raise ValueError("任务版本、连接或归档目标不存在")
            adapter = destination_adapter(self.runtime, connection, destination)
            compiled = compile_dsl(version.dsl, target_fields=adapter.target_fields())
            failure_policy = compiled.dsl.failure_policy
            expected_action = {
                "dingtalk_doc": "append",
                "dingtalk_sheet": "append",
                "dingtalk_aitable": "upsert",
            }[str(destination.destination_type)]
            if compiled.dsl.destination_action != expected_action:
                raise ValueError(f"当前归档目标只允许 {expected_action} 动作")
            if not compiled.matches(event):
                self.repository.append_log(
                    task_id=task_id,
                    event_id=event.event_id,
                    message_id=event.message_id,
                    stage="filter",
                    result="skipped",
                    preview="事件未匹配规则",
                )
                self.repository.set_event_state(task_id, event.event_id, "completed")
                return
            event = self._with_related_media(task_id, event, compiled.dsl.context)
            if event is None:
                return
            if self.media_manager:
                event = self.media_manager.download(
                    task_id,
                    event,
                    connection,
                    compiled.dsl.media_policy,
                )
            values = compiled.execute(event)
            if values is None:
                self.repository.append_log(
                    task_id=task_id,
                    event_id=event.event_id,
                    message_id=event.message_id,
                    stage="filter",
                    result="skipped",
                    preview="事件未匹配规则",
                )
                self.repository.set_event_state(task_id, event.event_id, "completed")
                return
            values = adapter.prepare_values(values, compiled.dsl.mappings, event)
            for target, source in compiled.dsl.mappings.items():
                if source != "media" or not isinstance(values.get(target), list):
                    continue
                assets = [dict(item) for item in values[target] if isinstance(item, dict)]
                # 消息定位字段只供本地下载，禁止进入归档目标或投递日志。
                for asset in assets:
                    asset.pop("message_id", None)
                    asset.pop("conversation_id", None)
                if compiled.dsl.media_policy.archive_as == "link":
                    for asset in assets:
                        asset.pop("local_path", None)
                        asset.pop("sha256", None)
                elif compiled.dsl.media_policy.archive_as == "attachment":
                    for asset in assets:
                        asset.pop("stable_url", None)
                values[target] = assets
            if compiled.dsl.ai_steps:
                if self.ai_processor is None:
                    raise ValueError("任务包含 AI 增强步骤，但运行模型不可用")
                values = self.ai_processor(compiled.dsl.ai_steps, values, event, task_id)
            delivery_id = self.repository.enqueue_delivery(
                task_id=task_id,
                version_id=version.version_id,
                event_id=event.event_id,
                destination_id=destination.destination_id,
                action_name=compiled.dsl.destination_action,
                payload={"values": values},
            )
            self.repository.set_event_state(task_id, event.event_id, "completed")
            self.repository.touch_task(task_id, event.occurred_at)
            self.repository.append_log(
                task_id=task_id,
                event_id=event.event_id,
                message_id=event.message_id,
                stage="mapping",
                result="queued",
                external_ref=delivery_id,
                preview=event.text,
                details={"targets": sorted(values)},
            )
        except AISkipEvent as exc:
            self.repository.set_event_state(task_id, event.event_id, "completed")
            self.repository.append_log(
                task_id=task_id,
                event_id=event.event_id,
                message_id=event.message_id,
                stage="ai_runtime",
                result="skipped",
                preview=str(exc),
            )
        except AIRetryEvent as exc:
            self._retry_event(task_id, event, str(exc))
        except Exception as exc:
            if failure_policy == "skip":
                self.repository.set_event_state(task_id, event.event_id, "completed")
                result = "skipped"
            elif failure_policy == "retry":
                self._retry_event(task_id, event, str(exc))
                return
            elif failure_policy == "archive_raw":
                try:
                    self._archive_raw(task_id, event)
                    return
                except Exception as archive_exc:
                    exc = archive_exc
                    self.repository.set_event_state(task_id, event.event_id, "dead_letter")
                    result = "dead_letter"
            else:
                self.repository.set_event_state(task_id, event.event_id, "dead_letter")
                result = "dead_letter"
            self.repository.append_log(
                task_id=task_id,
                event_id=event.event_id,
                message_id=event.message_id,
                stage="pipeline",
                result=result,
                preview=event.text,
                details={"error": str(exc), "failure_policy": failure_policy},
            )

    def _with_related_media(
        self,
        task_id: str,
        event: CanonicalEvent,
        policy: ContextPolicy,
    ) -> CanonicalEvent | None:
        """等待后向窗口并合并同一发送人的邻近媒体事件。"""

        if policy.related_message_limit <= 0 or (
            policy.attachment_window_minutes <= 0
            and policy.attachment_forward_minutes <= 0
        ):
            return event
        occurred = datetime.fromisoformat(event.occurred_at).astimezone(UTC)
        now = datetime.now(UTC)
        if policy.attachment_forward_minutes:
            received = datetime.fromisoformat(event.received_at).astimezone(UTC)
            # 补偿批次至少留一秒完成入库；异常的未来事件时间不放大等待上限。
            deadline = max(
                min(occurred, now) + timedelta(minutes=policy.attachment_forward_minutes),
                min(received, now) + timedelta(seconds=1),
            )
            if deadline > now:
                self.repository.defer_event(task_id, event.event_id, deadline.isoformat())
                self.repository.append_log(
                    task_id=task_id,
                    event_id=event.event_id,
                    message_id=event.message_id,
                    stage="context",
                    result="waiting",
                    preview="等待附件关联窗口结束",
                )
                return None
        related = self.repository.list_context_events(
            task_id,
            conversation_id=event.conversation_id,
            sender_id=event.sender_id,
            occurred_after=(
                occurred - timedelta(minutes=policy.attachment_window_minutes)
            ).isoformat(),
            occurred_before=(
                occurred + timedelta(minutes=policy.attachment_forward_minutes)
            ).isoformat(),
            exclude_event_id=event.event_id,
            limit=policy.related_message_limit,
        )
        assets = _dedupe_media(
            [*event.media_assets, *(asset for item in related for asset in item.media_assets)]
        )
        if len(assets) > len(event.media_assets):
            self.repository.append_log(
                task_id=task_id,
                event_id=event.event_id,
                message_id=event.message_id,
                stage="context",
                result="enriched",
                preview="已关联邻近消息附件",
                details={"attachment_count": len(assets)},
            )
        return event.model_copy(update={"media_assets": assets})

    def _retry_event(self, task_id: str, event: CanonicalEvent, error: str) -> None:
        state = self.repository.schedule_event_retry(task_id, event.event_id, error)
        self.repository.append_log(
            task_id=task_id,
            event_id=event.event_id,
            message_id=event.message_id,
            stage="pipeline",
            result="dead_letter" if state == "dead_letter" else "retry",
            preview="事件处理超过重试预算" if state == "dead_letter" else "事件处理将在退避后重试",
            details={"error": error},
        )

    def _archive_raw(self, task_id: str, event: CanonicalEvent) -> None:
        """按目标可信结构生成最小原文投递，DSL 仍不能选择任意写入位置。"""

        task = self.repository.get_task(task_id)
        if task is None or not task.active_version_id:
            raise ValueError("任务没有可归档的活动版本")
        destination = self.repository.get_destination(task.destination_id)
        connection = self.repository.get_connection(task.connection_id)
        if destination is None or connection is None:
            raise ValueError("原文归档目标或连接不存在")
        adapter = destination_adapter(self.runtime, connection, destination)
        fields = adapter.target_fields()
        values: dict[str, Any]
        if str(destination.destination_type) == "dingtalk_doc":
            values = {
                "title": event.sender_name or "EIM 事件",
                "body": event.text,
                "metadata": {
                    "event_id": event.event_id,
                    "message_id": event.message_id,
                    "message_kind": str(event.message_kind),
                },
                "media": [item.model_dump(mode="json") for item in event.media_assets],
            }
        else:
            event_field = (
                "_eim_event_id"
                if "_eim_event_id" in fields
                else str(destination.stable_ids.get("event_key_field_id") or "")
            )
            content_field = next((item for item in fields if item != event_field), "")
            if not event_field or not content_field:
                raise ValueError("归档目标缺少事件 ID 或正文可写字段")
            values = {event_field: event.event_id, content_field: event.text}
        delivery_id = self.repository.enqueue_delivery(
            task_id=task_id,
            version_id=str(task.active_version_id),
            event_id=event.event_id,
            destination_id=destination.destination_id,
            action_name="append" if str(destination.destination_type) != "dingtalk_aitable" else "upsert",
            payload={"values": values},
        )
        self.repository.set_event_state(task_id, event.event_id, "completed")
        self.repository.append_log(
            task_id=task_id,
            event_id=event.event_id,
            message_id=event.message_id,
            stage="pipeline",
            result="archive_raw",
            external_ref=delivery_id,
            preview="已按失败策略排队归档原文",
        )

    def process_once(self, *, limit: int = 50) -> int:
        claimed = self.repository.claim_events(limit=limit)
        for item in claimed:
            self.process_claimed(item)
        return len(claimed)


def _dedupe_media(values: list[MediaAsset]) -> list[MediaAsset]:
    """按稳定资源标识去重，缺少标识时保留文件元数据不同的附件。"""

    result: list[MediaAsset] = []
    seen: set[tuple[Any, ...]] = set()
    for item in values:
        key = (
            item.resource_id,
            item.stable_url,
            item.sha256,
            item.file_name,
            item.size,
            item.mime_type,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
