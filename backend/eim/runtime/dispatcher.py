"""EIM outbox 的幂等投递、回读与有界重试。"""

from __future__ import annotations

from typing import Any

from backend.eim.connections.dws_runtime import DWSRuntimeError
from backend.eim.destinations.dingtalk import destination_adapter
from backend.eim.models import DeliveryState
from backend.eim.repository import EIMRepository
from backend.eim.runtime.retry import delivery_retry_at, retry_exhausted


class OutboxDispatcher:
    def __init__(self, repository: EIMRepository, runtime: Any):
        self.repository = repository
        self.runtime = runtime

    def dispatch_once(self, *, limit: int = 50) -> int:
        deliveries = self.repository.claim_due_deliveries(limit=limit)
        for delivery in deliveries:
            self._deliver(delivery)
        return len(deliveries)

    def _deliver(self, delivery: dict[str, Any]) -> None:
        task = self.repository.get_task(str(delivery["task_id"]))
        destination = self.repository.get_destination(str(delivery["destination_id"]))
        if task is None or destination is None:
            self._dead_letter(delivery, "任务或归档目标已不存在")
            return
        if not task.accepts_work:
            # 停止后不开始新的远端写入；已领取项恢复原状态，重启后继续。
            self.repository.update_delivery(
                str(delivery["delivery_id"]),
                DeliveryState(str(delivery.get("previous_state") or "pending")),
                external_ref=delivery.get("external_ref"),
                last_error=delivery.get("last_error"),
                next_retry_at=delivery.get("next_retry_at"),
                increment_attempt=False,
            )
            return
        connection = self.repository.get_connection(task.connection_id)
        if connection is None:
            self._dead_letter(delivery, "任务连接已不存在")
            return
        adapter = destination_adapter(self.runtime, connection, destination)
        key = str(delivery["idempotency_key"])
        try:
            if str(delivery.get("previous_state") or "") in {
                str(DeliveryState.COMMIT_UNKNOWN),
                str(DeliveryState.RETRY),
            }:
                # 远端结果未知或人工重试时先回读，避免已成功写入又被重复提交并误入死信。
                existing = adapter.find_by_idempotency_key(key)
                if existing:
                    self._complete(delivery, existing, already_present=True)
                    return
            payload = dict(delivery["payload"])
            media_cache = {
                str(cache_key): dict(cache_value)
                for cache_key, cache_value in dict(payload.get("media_uploads") or {}).items()
                if isinstance(cache_value, dict)
            }

            def persist_media_cache(updated: dict[str, dict[str, str]]) -> None:
                # 每完成一个远端媒体上传就落盘，崩溃重试不会制造重复附件。
                payload["media_uploads"] = updated
                self.repository.update_delivery_payload(str(delivery["delivery_id"]), payload)

            result = adapter.deliver(
                key,
                dict(payload.get("values") or {}),
                media_cache=media_cache,
                persist_media_cache=persist_media_cache,
            )
            self._complete(
                delivery,
                result.external_ref,
                already_present=result.already_present,
            )
        except ValueError as exc:
            self._dead_letter(delivery, str(exc))
        except DWSRuntimeError as exc:
            attempts = int(delivery["attempts"]) + 1
            if retry_exhausted(attempts, str(delivery["created_at"])):
                self._dead_letter(delivery, str(exc))
                return
            self.repository.update_delivery(
                str(delivery["delivery_id"]),
                DeliveryState.COMMIT_UNKNOWN,
                last_error=str(exc),
                next_retry_at=delivery_retry_at(attempts),
            )
            self.repository.append_log(
                task_id=task.task_id,
                event_id=str(delivery["event_id"]),
                stage="delivery",
                result="retry",
                preview="目标写入状态未知，将先回读再重试",
                details={"error": str(exc), "attempts": attempts},
            )
        except Exception as exc:
            self._dead_letter(delivery, str(exc))

    def _complete(
        self,
        delivery: dict[str, Any],
        external_ref: str,
        *,
        already_present: bool,
    ) -> None:
        self.repository.update_delivery(
            str(delivery["delivery_id"]),
            DeliveryState.COMPLETED,
            external_ref=external_ref,
        )
        self.repository.append_log(
            task_id=str(delivery["task_id"]),
            event_id=str(delivery["event_id"]),
            stage="delivery",
            result="completed",
            external_ref=external_ref,
            preview="归档完成" if not already_present else "幂等回读确认已归档",
        )

    def _dead_letter(self, delivery: dict[str, Any], error: str) -> None:
        self.repository.update_delivery(
            str(delivery["delivery_id"]),
            DeliveryState.DEAD_LETTER,
            last_error=error,
        )
        self.repository.append_log(
            task_id=str(delivery["task_id"]),
            event_id=str(delivery["event_id"]),
            stage="delivery",
            result="dead_letter",
            preview="归档失败，已进入死信",
            details={"error": error},
        )
