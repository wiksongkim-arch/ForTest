"""EIM 长期任务、DWS 订阅、断线补偿与后台流水线总管。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from backend.eim.builder.compiler import compile_dsl
from backend.eim.connections.dingtalk import DingTalkConnector
from backend.eim.destinations.dingtalk import destination_adapter
from backend.eim.events.dingtalk_normalizer import normalize_dingtalk_event
from backend.eim.events.subscription import DingTalkSubscription, SubscriptionStartError
from backend.eim.models import (
    ConnectionState,
    DesiredState,
    EventType,
    ObservedState,
    utc_now,
)
from backend.eim.repository import EIMRepository
from backend.eim.runtime.dispatcher import OutboxDispatcher
from backend.eim.runtime.pipeline import EIMPipeline
from backend.eim.runtime.retry import subscription_attempt_limit


@dataclass
class _TaskRuntime:
    task_id: str
    consumer_key: tuple[str, str, tuple[str, ...]]
    run_id: str | None = None
    error: str = ""


@dataclass
class _ConsumerRuntime:
    """相同连接、群和事件集合共享一个官方 DWS consumer。"""

    key: tuple[str, str, tuple[str, ...]]
    task_ids: set[str] = field(default_factory=set)
    stop: threading.Event = field(default_factory=threading.Event)
    ready: threading.Event = field(default_factory=threading.Event)
    subscription: DingTalkSubscription | None = None
    thread: threading.Thread | None = None


class EIMSupervisor:
    """EIM 独立生命周期，不复用有限时长的测试用例 TaskManager。"""

    def __init__(
        self,
        repository: EIMRepository,
        connector: DingTalkConnector,
        runtime: Any,
        *,
        engine_interval: float = 0.25,
        ai_processor: Any = None,
    ):
        self.repository = repository
        self.connector = connector
        self.runtime = runtime
        self.pipeline = EIMPipeline(repository, runtime, ai_processor=ai_processor)
        self.dispatcher = OutboxDispatcher(repository, runtime)
        self.engine_interval = max(0.05, float(engine_interval))
        self._tasks: dict[str, _TaskRuntime] = {}
        self._consumers: dict[tuple[str, str, tuple[str, ...]], _ConsumerRuntime] = {}
        self._lock = threading.RLock()
        self._engine_stop = threading.Event()
        self._engine_thread: threading.Thread | None = None

    def start_background(self) -> None:
        with self._lock:
            if self._engine_thread and self._engine_thread.is_alive():
                return
            self._engine_stop.clear()
            self._engine_thread = threading.Thread(
                target=self._engine_loop,
                name="eim-engine",
                daemon=True,
            )
            self._engine_thread.start()

    def start_task(self, task_id: str) -> None:
        self.start_background()
        task = self._preflight(task_id)
        with self._lock:
            existing = self._tasks.get(task_id)
            if existing:
                active_consumer = self._consumers.get(existing.consumer_key)
                if (
                    active_consumer
                    and task_id in active_consumer.task_ids
                    and active_consumer.thread
                    and active_consumer.thread.is_alive()
                ):
                    raise ValueError("EIM 任务已经在运行")
                if existing.run_id:
                    self.repository.stop_run(existing.run_id, "restart_after_error")
                self._tasks.pop(task_id, None)
            task = self.repository.transition_observed(
                task_id,
                ObservedState.STARTING,
                desired=DesiredState.RUNNING,
            )
            connection = self.repository.get_connection(task.connection_id)
            assert connection
            events = tuple(
                sorted(
                    "message" if item is EventType.MESSAGE else "reaction"
                    for item in task.event_types
                )
            )
            key = (task.connection_id, task.source_id, events)
            value = _TaskRuntime(
                task_id=task_id,
                consumer_key=key,
                run_id=self.repository.start_run(task_id, str(task.active_version_id)),
            )
            self._tasks[task_id] = value
            consumer = self._consumers.get(key)
            if consumer and consumer.thread and consumer.thread.is_alive():
                consumer.task_ids.add(task_id)
                if consumer.ready.is_set():
                    self.repository.transition_observed(task_id, ObservedState.RUNNING)
                return
            consumer = _ConsumerRuntime(key=key, task_ids={task_id})
            consumer.thread = threading.Thread(
                target=self._consumer_loop,
                args=(consumer,),
                name=f"eim-consumer-{task.display_id}",
                daemon=True,
            )
            self._consumers[key] = consumer
            consumer.thread.start()

    def _preflight(self, task_id: str):
        task = self.repository.get_task(task_id)
        if task is None or task.deleted_at:
            raise ValueError("EIM 任务不存在或已在回收站")
        if task.observed_state not in {
            ObservedState.STOPPED,
            ObservedState.STOPPED_APP_EXIT,
            ObservedState.ERROR,
        }:
            raise ValueError("EIM 任务当前状态不能启动")
        if not task.active_version_id:
            raise ValueError("EIM 任务尚未部署可运行版本")
        version = self.repository.get_version(task.active_version_id)
        connection = self.connector.refresh(task.connection_id)
        destination = self.repository.get_destination(task.destination_id)
        if version is None or connection is None or destination is None:
            raise ValueError("EIM 任务依赖的版本、连接或目标不存在")
        if connection.connection_state is not ConnectionState.CONNECTED:
            raise ValueError("钉钉连接不可用，请先重新授权")
        adapter = destination_adapter(self.runtime, connection, destination)
        schema = adapter.inspect_schema()
        if destination.schema_snapshot.get("sheets") and "sheets" not in schema:
            schema["sheets"] = destination.schema_snapshot["sheets"]
        destination.schema_snapshot = schema
        destination.checked_at = utc_now()
        self.repository.save_destination(destination)
        compiled = compile_dsl(version.dsl, target_fields=adapter.target_fields())
        expected_action = {
            "dingtalk_doc": "append",
            "dingtalk_sheet": "append",
            "dingtalk_aitable": "upsert",
        }[str(destination.destination_type)]
        if compiled.dsl.destination_action != expected_action:
            raise ValueError(f"当前归档目标只允许 {expected_action} 动作")
        return task

    def _consumer_loop(self, value: _ConsumerRuntime) -> None:
        degraded_tasks: set[str] = set()
        last_ready = utc_now()
        try:
            first = True
            while not value.stop.is_set():
                try:
                    value.ready.clear()
                    subscription = self._create_subscription(value)
                    value.subscription = subscription
                    value.ready.set()
                    for task_id in self._consumer_task_ids(value):
                        current = self.repository.get_task(task_id)
                        if current and current.observed_state in {
                            ObservedState.STARTING,
                            ObservedState.RECONNECTING,
                            ObservedState.DEGRADED,
                        }:
                            target = (
                                ObservedState.DEGRADED
                                if task_id in degraded_tasks
                                else ObservedState.RUNNING
                            )
                            self.repository.transition_observed(task_id, target)
                    last_ready = utc_now()
                    first = False
                    while not value.stop.wait(1.0) and not subscription.exited.is_set():
                        for task_id in self._consumer_task_ids(value):
                            runtime = self._tasks.get(task_id)
                            if runtime and runtime.run_id:
                                self.repository.heartbeat_run(runtime.run_id)
                    if value.stop.is_set():
                        subscription.stop()
                        return
                    value.ready.clear()
                    for task_id in self._consumer_task_ids(value):
                        current = self.repository.get_task(task_id)
                        if current and current.observed_state in {
                            ObservedState.RUNNING,
                            ObservedState.DEGRADED,
                        }:
                            self.repository.transition_observed(task_id, ObservedState.RECONNECTING)
                        if self._reconcile_gap(task_id, last_ready):
                            degraded_tasks.add(task_id)
                except SubscriptionStartError as exc:
                    self._fail_consumer(
                        value,
                        exc,
                        stage="subscription",
                        preview="钉钉事件订阅启动失败",
                        details={"first_start": first},
                    )
                    return
        except Exception as exc:
            self._fail_consumer(
                value,
                exc,
                stage="runtime",
                preview="EIM 任务运行异常",
            )
        finally:
            task_ids = self._consumer_task_ids(value)
            with self._lock:
                if self._consumers.get(value.key) is value:
                    self._consumers.pop(value.key, None)
            for task_id in task_ids:
                runtime = self._tasks.get(task_id)
                if runtime and runtime.run_id:
                    self.repository.stop_run(
                        runtime.run_id,
                        "stopped" if value.stop.is_set() else "error",
                    )
                    runtime.run_id = None

    def _create_subscription(self, value: _ConsumerRuntime) -> DingTalkSubscription:
        attempts = 0
        limit = 2
        while not value.stop.is_set():
            attempts += 1
            connection_id, conversation_id, events = value.key
            connection = self.repository.get_connection(connection_id)
            if connection is None:
                raise SubscriptionStartError("EIM 连接已不存在", retryable=False, retry_after=0)
            subscription = DingTalkSubscription(
                self.runtime,
                connection_id=connection.connection_id,
                profile=connection.profile,
                conversation_id=conversation_id,
                events=events,
                on_event=lambda payload: self._fanout(value, payload),
                on_error=lambda message: self._log_consumer_warning(value, message),
            )
            try:
                subscription.start()
                return subscription
            except SubscriptionStartError as exc:
                limit = subscription_attempt_limit(exc.retryable)
                if attempts >= limit:
                    raise
                delay = exc.retry_after or min(30.0, 2.0**attempts)
                if value.stop.wait(delay):
                    raise SubscriptionStartError(
                        "EIM 订阅启动已取消", retryable=False, retry_after=0
                    ) from None
        raise SubscriptionStartError("EIM 订阅启动已取消", retryable=False, retry_after=0)

    def _consumer_task_ids(self, value: _ConsumerRuntime) -> tuple[str, ...]:
        with self._lock:
            return tuple(value.task_ids)

    def _fanout(self, value: _ConsumerRuntime, payload: dict[str, Any]) -> None:
        """共享 consumer 只读取一次事件，再投递到每个任务的独立 inbox。"""

        for task_id in self._consumer_task_ids(value):
            self._ingest(task_id, payload)

    def _log_consumer_warning(self, value: _ConsumerRuntime, message: str) -> None:
        for task_id in self._consumer_task_ids(value):
            self.repository.append_log(
                task_id=task_id,
                stage="subscription",
                result="warning",
                preview=message,
            )

    def _fail_consumer(
        self,
        value: _ConsumerRuntime,
        exc: Exception,
        *,
        stage: str,
        preview: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        for task_id in self._consumer_task_ids(value):
            runtime = self._tasks.get(task_id)
            if runtime:
                runtime.error = str(exc)
            current = self.repository.get_task(task_id)
            if current and current.observed_state in {
                ObservedState.STARTING,
                ObservedState.RUNNING,
                ObservedState.RECONNECTING,
                ObservedState.DEGRADED,
            }:
                self.repository.transition_observed(task_id, ObservedState.ERROR)
            self.repository.append_log(
                task_id=task_id,
                stage=stage,
                result="failed",
                preview=preview,
                details={"error": str(exc), **(details or {})},
            )

    def _ingest(self, task_id: str, payload: dict[str, Any]) -> None:
        task = self.repository.get_task(task_id)
        if task is None:
            return
        connection = self.repository.get_connection(task.connection_id)
        try:
            event = normalize_dingtalk_event(
                payload,
                connection_id=task.connection_id,
                expected_conversation_id=task.source_id,
                own_open_id=connection.account_id if connection else "",
            )
            if event is None:
                return
            inserted = self.repository.insert_event(task_id, event)
            self.repository.append_log(
                task_id=task_id,
                event_id=event.event_id,
                message_id=event.message_id,
                stage="receive",
                result="received" if inserted else "duplicate",
                preview=event.text,
                details={"event_type": str(event.event_type), "kind": str(event.message_kind)},
            )
        except Exception as exc:
            self.repository.append_log(
                task_id=task_id,
                stage="normalize",
                result="failed",
                preview="无法规范化钉钉事件",
                details={"error": str(exc)},
            )

    def _reconcile_gap(self, task_id: str, since: str) -> bool:
        task = self.repository.get_task(task_id)
        if task is None:
            return True
        degraded = EventType.REACTION in task.event_types
        try:
            for payload in self.connector.gap_messages(
                task.connection_id,
                task.source_id,
                since=since,
            ):
                recovered = dict(payload)
                if not any(
                    recovered.get(name)
                    for name in (
                        "conversation_id",
                        "conversationId",
                        "openConversationId",
                    )
                ):
                    # 仅在所有官方别名都缺失时补群 ID，冲突值交给规范化器拒绝。
                    recovered["conversation_id"] = task.source_id
                recovered.setdefault(
                    "event_id",
                    recovered.get("messageId") or recovered.get("message_id"),
                )
                self._ingest(task_id, recovered)
        except Exception as exc:
            degraded = True
            self.repository.append_log(
                task_id=task_id,
                stage="reconcile",
                result="failed",
                preview="断线窗口无法完整补齐，任务保持降级",
                details={"error": str(exc)},
            )
        if degraded:
            self.repository.append_log(
                task_id=task_id,
                stage="reconcile",
                result="degraded",
                preview="消息已补偿；reaction 断线窗口无法由历史接口完整证明",
            )
        return degraded

    def stop_task(self, task_id: str, *, app_exit: bool = False) -> None:
        with self._lock:
            value = self._tasks.get(task_id)
        task = self.repository.get_task(task_id)
        if task is None:
            return
        desired = None if app_exit else DesiredState.STOPPED
        if task.observed_state in {
            ObservedState.STARTING,
            ObservedState.RUNNING,
            ObservedState.RECONNECTING,
            ObservedState.DEGRADED,
        }:
            self.repository.transition_observed(
                task_id,
                ObservedState.STOPPING,
                desired=desired,
            )
        elif not app_exit and task.observed_state in {
            ObservedState.ERROR,
            ObservedState.STOPPED_APP_EXIT,
        }:
            self.repository.transition_observed(
                task_id,
                ObservedState.STOPPED,
                desired=DesiredState.STOPPED,
            )
        consumer: _ConsumerRuntime | None = None
        stop_consumer = False
        if value:
            with self._lock:
                consumer = self._consumers.get(value.consumer_key)
                if consumer and task_id in consumer.task_ids:
                    consumer.task_ids.discard(task_id)
                    stop_consumer = not consumer.task_ids
                    if stop_consumer:
                        consumer.stop.set()
            if stop_consumer and consumer:
                if consumer.subscription:
                    consumer.subscription.stop()
                if consumer.thread:
                    consumer.thread.join(timeout=10)
            if value.run_id:
                self.repository.stop_run(value.run_id, "stopped")
                value.run_id = None
        current = self.repository.get_task(task_id)
        if current and current.observed_state is ObservedState.STOPPING:
            self.repository.transition_observed(
                task_id,
                ObservedState.STOPPED_APP_EXIT if app_exit else ObservedState.STOPPED,
            )
        with self._lock:
            self._tasks.pop(task_id, None)

    def restore_desired(self) -> list[str]:
        restored: list[str] = []
        for task in self.repository.list_tasks():
            if task.desired_state is not DesiredState.RUNNING:
                continue
            try:
                self.start_task(task.task_id)
                restored.append(task.task_id)
            except Exception as exc:
                self.repository.append_log(
                    task_id=task.task_id,
                    stage="restore",
                    result="failed",
                    preview="EIM 自动恢复失败",
                    details={"error": str(exc)},
                )
        return restored

    def stop_all(self, *, app_exit: bool = True) -> None:
        with self._lock:
            task_ids = list(self._tasks)
        for task_id in task_ids:
            self.stop_task(task_id, app_exit=app_exit)
        self._engine_stop.set()
        if self._engine_thread:
            self._engine_thread.join(timeout=3)

    def status(self) -> dict[str, Any]:
        overview = self.repository.count_overview()
        with self._lock:
            errors = {task_id: value.error for task_id, value in self._tasks.items() if value.error}
        return {**overview, "errors": errors}

    def _engine_loop(self) -> None:
        while not self._engine_stop.wait(self.engine_interval):
            try:
                processed = self.pipeline.process_once()
                delivered = self.dispatcher.dispatch_once()
                if processed or delivered:
                    continue
            except Exception as exc:
                self.repository.append_log(
                    task_id=None,
                    stage="engine",
                    result="failed",
                    preview="EIM 后台调度循环异常",
                    details={"error": str(exc)},
                )
