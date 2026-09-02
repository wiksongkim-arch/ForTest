"""EIM 确定性构建、受控 AI 动作和 inbox/outbox 端到端测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.eim.builder.orchestrator import EIMBuilder
from backend.eim.ai_runtime import EIMAIRuntime
from backend.eim.models import (
    CanonicalEvent,
    AIStep,
    ConnectionState,
    ContextPolicy,
    DestinationType,
    EIMConnection,
    EIMDSL,
    EIMDestination,
    EIMTask,
    EIMTaskVersion,
    EventType,
    MediaAsset,
    MessageKind,
    BuildState,
    DesiredState,
    FilterRule,
    MediaPolicy,
    ObservedState,
)
from backend.eim.repository import EIMRepository
from backend.eim.runtime.dispatcher import OutboxDispatcher
from backend.eim.runtime.pipeline import EIMPipeline


class _Runtime:
    def __init__(self, root: Path):
        self.data_root = root
        self.root = root / "config"
        self.written = False
        self.content = ""

    def config_dir(self, _connection_id: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def run_json(self, arguments: list[str], **_kwargs: Any) -> dict[str, Any]:
        joined = " ".join(arguments)
        if "doc +fetch" in joined:
            return {"documentId": "doc-1", "content": self.content}
        if "doc +doc-append" in joined and "--dry-run" not in arguments:
            self.written = True
            self.content += "\n" + arguments[arguments.index("--content") + 1]
        return {"success": True}


class _Connector:
    def __init__(self, repository: EIMRepository):
        self.repository = repository

    def refresh(self, connection_id: str) -> EIMConnection:
        connection = self.repository.get_connection(connection_id)
        assert connection
        connection.connection_state = ConnectionState.CONNECTED
        return self.repository.save_connection(connection)


def _setup(tmp_path: Path) -> tuple[EIMRepository, EIMTask, _Runtime, _Connector]:
    repository = EIMRepository(tmp_path / "eim.db")
    connection = repository.save_connection(
        EIMConnection(
            config_dir_ref="local",
            profile="corp:user",
            connection_state=ConnectionState.CONNECTED,
        )
    )
    destination = repository.save_destination(
        EIMDestination(
            connection_id=connection.connection_id,
            destination_type=DestinationType.DINGTALK_DOC,
            url="https://alidocs.dingtalk.com/i/nodes/doc",
        )
    )
    task = repository.create_task(
        EIMTask(
            name="构建测试",
            connection_id=connection.connection_id,
            source_id="cid",
            source_name="测试群",
            destination_id=destination.destination_id,
        )
    )
    event = CanonicalEvent(
        connection_id=connection.connection_id,
        event_id="event-1",
        event_type=EventType.MESSAGE,
        message_id="message-1",
        conversation_id="cid",
        occurred_at="2026-09-01T00:00:00+00:00",
        message_kind=MessageKind.TEXT,
        text="需要归档",
    )
    repository.add_sample(
        task.task_id,
        event.model_dump(mode="json"),
        {"body": "需要归档"},
    )
    runtime = _Runtime(tmp_path)
    return repository, task, runtime, _Connector(repository)


def test_deterministic_build_publishes_immutable_bundle_and_reuses_hash(tmp_path: Path) -> None:
    repository, task, runtime, connector = _setup(tmp_path)
    builder = EIMBuilder(repository, connector, runtime, tmp_path)  # type: ignore[arg-type]
    builder.save_draft(
        task.task_id,
        EIMDSL(mappings={"body": "message.text"}, destination_action="append"),
    )

    first = builder.build(task.task_id)
    second = builder.build(task.task_id)
    assert first["version_id"] == second["version_id"]
    active = repository.get_task(task.task_id)
    assert active.active_version_id == first["version_id"]
    version = repository.get_version(first["version_id"])
    bundle = tmp_path / version.bundle_path
    assert sorted(path.name for path in bundle.iterdir()) == [
        "dsl.json",
        "evidence.json",
        "manifest.json",
        "samples.json",
    ]

    event = CanonicalEvent.model_validate(repository.list_samples(task.task_id)[0]["input"])
    repository.transition_observed(
        task.task_id,
        ObservedState.STARTING,
        desired=DesiredState.RUNNING,
    )
    repository.transition_observed(task.task_id, ObservedState.RUNNING)
    assert repository.insert_event(task.task_id, event)
    assert EIMPipeline(repository, runtime).process_once() == 1
    assert OutboxDispatcher(repository, runtime).dispatch_once() == 1
    logs = repository.list_logs(task_id=task.task_id)
    assert any(item["result"] == "completed" for item in logs)


def test_ai_loop_only_applies_registered_dsl_actions(tmp_path: Path) -> None:
    repository, task, runtime, connector = _setup(tmp_path)
    actions = iter(
        [
            {
                "action": "apply_patch",
                "arguments": {"patch": {"mappings": {"body": "message.text"}}},
                "message": "应用映射",
            },
            {"action": "finish", "arguments": {}, "message": "完成"},
        ]
    )

    def runner(_configuration_id: str, _prompt: str):
        return next(actions), SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            duration_ms=1,
        )

    builder = EIMBuilder(
        repository,
        connector,  # type: ignore[arg-type]
        runtime,
        tmp_path,
        model_runner=runner,
    )
    result = builder.build(task.task_id, configuration_id="fake-model")
    assert result["build_state"] == "ready"

    repository2, task2, runtime2, connector2 = _setup(tmp_path / "invalid")

    def malicious(_configuration_id: str, _prompt: str):
        return (
            {"action": "run_shell", "arguments": {"command": "calc.exe"}, "message": ""},
            SimpleNamespace(),
        )

    rejected = EIMBuilder(
        repository2,
        connector2,  # type: ignore[arg-type]
        runtime2,
        tmp_path / "invalid",
        model_runner=malicious,
    )
    with pytest.raises(ValueError, match="未登记"):
        rejected.build(task2.task_id, configuration_id="fake-model")


def test_ai_runtime_sends_only_explicit_redacted_fields_and_tracks_budget(tmp_path: Path) -> None:
    repository, task, _runtime, _connector = _setup(tmp_path)
    sample = repository.list_samples(task.task_id)[0]["input"]
    event = CanonicalEvent.model_validate(sample)
    prompts: list[str] = []

    def stage_runner(_configuration_id: str, **values: Any):
        prompts.append(values["user_prompt"])
        return {"body": "已摘要"}, SimpleNamespace(
            input_tokens=8,
            output_tokens=2,
            duration_ms=3,
            model="fixture-model",
        )

    runtime = EIMAIRuntime(
        repository,
        tmp_path,
        stage_runner=stage_runner,
    )
    output = runtime.process(
        [
            AIStep(
                configuration_id="ai-1",
                input_fields=["body"],
                redacted_fields=["body"],
                output_schema={
                    "type": "object",
                    "properties": {"body": {"type": "string"}},
                    "required": ["body"],
                    "additionalProperties": False,
                },
                daily_budget=100,
            )
        ],
        {"body": "客户手机号 13800000000"},
        event,
        task.task_id,
    )
    assert output == {"body": "已摘要"}
    assert "13800000000" not in prompts[0]
    assert "<redacted>" in prompts[0]
    assert repository.ai_usage_today("ai-1", "tokens") == {"amount": 10.0, "calls": 1}

    image = tmp_path / "eim" / "media" / "task" / "event" / "image.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"tampered-image")
    event.media_assets = [
        MediaAsset(
            local_path=image.relative_to(tmp_path).as_posix(),
            mime_type="image/png",
            sha256="0" * 64,
        )
    ]
    with pytest.raises(ValueError, match="没有可用"):
        runtime._images(event)


def test_failed_ai_attempts_are_counted_against_call_budget(tmp_path: Path) -> None:
    repository, task, _runtime, _connector = _setup(tmp_path)
    event = CanonicalEvent.model_validate(repository.list_samples(task.task_id)[0]["input"])
    calls = 0

    def failing_runner(*_args: Any, **_kwargs: Any):
        nonlocal calls
        calls += 1
        raise RuntimeError("暂时不可用")

    runtime = EIMAIRuntime(repository, tmp_path, stage_runner=failing_runner)
    output = runtime.process(
        [
            AIStep(
                configuration_id="ai-failure",
                input_fields=["body"],
                output_schema={
                    "type": "object",
                    "properties": {"body": {"type": "string"}},
                    "required": ["body"],
                    "additionalProperties": False,
                },
                budget_unit="calls",
                daily_budget=10,
            )
        ],
        {"body": "原文"},
        event,
        task.task_id,
    )

    assert output == {"body": "原文"}
    assert calls == 2
    assert repository.ai_usage_today("ai-failure", "calls") == {
        "amount": 2.0,
        "calls": 2,
    }


def test_pipeline_filters_before_media_download(tmp_path: Path) -> None:
    repository, task, runtime, _connector = _setup(tmp_path)
    repository.transition_build(task.task_id, BuildState.BUILDING)
    repository.transition_build(task.task_id, BuildState.VALIDATING)
    repository.publish_version(
        EIMTaskVersion(
            task_id=task.task_id,
            manifest={},
            dsl=EIMDSL(
                filters=[
                    FilterRule(
                        field="message.text",
                        operator="equals",
                        value="不会匹配",
                    )
                ],
                mappings={"body": "message.text"},
            ).model_dump(mode="json"),
            bundle_path="eim/bundles/filter-first",
            content_hash="filter-first",
        ),
        expected_draft_revision=task.draft_revision,
    )
    repository.transition_observed(
        task.task_id,
        ObservedState.STARTING,
        desired=DesiredState.RUNNING,
    )
    repository.transition_observed(task.task_id, ObservedState.RUNNING)
    event = CanonicalEvent.model_validate(repository.list_samples(task.task_id)[0]["input"])
    event.media_assets = [MediaAsset(resource_id="resource", size=1)]
    assert repository.insert_event(task.task_id, event)

    class UnexpectedMedia:
        def download(self, *_args: Any, **_kwargs: Any):
            raise AssertionError("未匹配事件不应下载媒体")

    assert EIMPipeline(repository, runtime, media_manager=UnexpectedMedia()).process_once() == 1
    assert repository.list_due_deliveries() == []


def test_pipeline_associates_same_sender_media_on_both_sides(tmp_path: Path) -> None:
    repository, task, runtime, _connector = _setup(tmp_path)
    repository.transition_build(task.task_id, BuildState.BUILDING)
    repository.transition_build(task.task_id, BuildState.VALIDATING)
    repository.publish_version(
        EIMTaskVersion(
            task_id=task.task_id,
            manifest={},
            dsl=EIMDSL(
                filters=[FilterRule(field="message.text", operator="contains", value="@我")],
                context=ContextPolicy(
                    attachment_window_minutes=10,
                    attachment_forward_minutes=4,
                    related_message_limit=10,
                ),
                mappings={"body": "message.text", "media": "media"},
                media_policy=MediaPolicy(download=False),
            ).model_dump(mode="json"),
            bundle_path="eim/bundles/context-media",
            content_hash="context-media",
        ),
        expected_draft_revision=task.draft_revision,
    )
    repository.transition_observed(
        task.task_id,
        ObservedState.STARTING,
        desired=DesiredState.RUNNING,
    )
    repository.transition_observed(task.task_id, ObservedState.RUNNING)

    def event(event_id: str, occurred_at: str, *, text: str = "", resource: str = ""):
        return CanonicalEvent(
            connection_id=task.connection_id,
            event_id=event_id,
            event_type=EventType.MESSAGE,
            message_id=f"message-{event_id}",
            conversation_id=task.source_id,
            sender_id="sender-1",
            occurred_at=occurred_at,
            received_at=occurred_at,
            message_kind=MessageKind.IMAGE if resource else MessageKind.TEXT,
            text=text,
            media_assets=[
                MediaAsset(
                    resource_id=resource,
                    message_id=f"message-{event_id}",
                    conversation_id=task.source_id,
                )
            ]
            if resource
            else [],
        )

    for item in (
        event("before", "2020-01-01T00:00:00+00:00", resource="resource-before"),
        event("matched", "2020-01-01T00:05:00+00:00", text="@我 【示例】 问题"),
        event("after", "2020-01-01T00:08:00+00:00", resource="resource-after"),
    ):
        assert repository.insert_event(task.task_id, item)

    assert EIMPipeline(repository, runtime).process_once() == 3
    delivery = repository.list_due_deliveries()[0]
    assert [item["resource_id"] for item in delivery["payload"]["values"]["media"]] == [
        "resource-before",
        "resource-after",
    ]
    assert all(
        "message_id" not in item and "conversation_id" not in item
        for item in delivery["payload"]["values"]["media"]
    )


def test_pipeline_defers_matched_event_until_forward_window_closes(tmp_path: Path) -> None:
    repository, task, runtime, _connector = _setup(tmp_path)
    repository.transition_build(task.task_id, BuildState.BUILDING)
    repository.transition_build(task.task_id, BuildState.VALIDATING)
    repository.publish_version(
        EIMTaskVersion(
            task_id=task.task_id,
            manifest={},
            dsl=EIMDSL(
                filters=[FilterRule(field="message.text", operator="contains", value="@我")],
                context=ContextPolicy(
                    attachment_forward_minutes=4,
                    related_message_limit=10,
                ),
                mappings={"body": "message.text"},
            ).model_dump(mode="json"),
            bundle_path="eim/bundles/context-wait",
            content_hash="context-wait",
        ),
        expected_draft_revision=task.draft_revision,
    )
    repository.transition_observed(
        task.task_id,
        ObservedState.STARTING,
        desired=DesiredState.RUNNING,
    )
    repository.transition_observed(task.task_id, ObservedState.RUNNING)
    now = datetime.now(UTC).isoformat(timespec="milliseconds")
    event = CanonicalEvent(
        connection_id=task.connection_id,
        event_id="waiting-event",
        event_type=EventType.MESSAGE,
        message_id="waiting-message",
        conversation_id=task.source_id,
        sender_id="sender-1",
        occurred_at=now,
        received_at=now,
        message_kind=MessageKind.TEXT,
        text="@我 【示例】 问题",
    )
    assert repository.insert_event(task.task_id, event)

    assert EIMPipeline(repository, runtime).process_once() == 1
    assert repository.list_due_deliveries() == []
    assert repository.claim_events() == []
    assert any(
        item["stage"] == "context" and item["result"] == "waiting"
        for item in repository.list_logs(task_id=task.task_id)
    )


@pytest.mark.parametrize(
    ("archive_as", "removed"),
    [("link", "local_path"), ("attachment", "stable_url")],
)
def test_pipeline_applies_media_archive_mode(
    tmp_path: Path,
    archive_as: str,
    removed: str,
) -> None:
    repository, task, runtime, _connector = _setup(tmp_path / archive_as)
    repository.transition_build(task.task_id, BuildState.BUILDING)
    repository.transition_build(task.task_id, BuildState.VALIDATING)
    repository.publish_version(
        EIMTaskVersion(
            task_id=task.task_id,
            manifest={},
            dsl=EIMDSL(
                mappings={"media": "media"},
                media_policy=MediaPolicy(download=False, archive_as=archive_as),
            ).model_dump(mode="json"),
            bundle_path=f"eim/bundles/{archive_as}",
            content_hash=f"media-{archive_as}",
        ),
        expected_draft_revision=task.draft_revision,
    )
    repository.transition_observed(
        task.task_id,
        ObservedState.STARTING,
        desired=DesiredState.RUNNING,
    )
    repository.transition_observed(task.task_id, ObservedState.RUNNING)
    event = CanonicalEvent.model_validate(repository.list_samples(task.task_id)[0]["input"])
    event.event_id = f"media-{archive_as}"
    event.media_assets = [
        MediaAsset(
            file_name="evidence.png",
            mime_type="image/png",
            size=4,
            sha256="a" * 64,
            local_path="eim/media/task/event/evidence.png",
            stable_url="https://example.test/evidence.png",
        )
    ]
    assert repository.insert_event(task.task_id, event)

    assert EIMPipeline(repository, runtime).process_once() == 1
    delivery = repository.list_due_deliveries()[0]
    media = delivery["payload"]["values"]["media"][0]
    assert removed not in media


def test_ai_added_samples_commit_only_with_successful_version(tmp_path: Path) -> None:
    repository, task, runtime, connector = _setup(tmp_path / "success")
    sample = repository.list_samples(task.task_id)[0]
    actions = iter(
        [
            {
                "action": "add_sample",
                "arguments": {"input": sample["input"], "expected": sample["expected"]},
                "message": "新增回归样例",
            },
            {"action": "finish", "arguments": {}, "message": "完成"},
        ]
    )
    builder = EIMBuilder(
        repository,
        connector,  # type: ignore[arg-type]
        runtime,
        tmp_path / "success",
        model_runner=lambda _configuration_id, _prompt: (next(actions), SimpleNamespace()),
    )
    builder.save_draft(task.task_id, EIMDSL(mappings={"body": "message.text"}))
    builder.build(task.task_id, configuration_id="fake-model")
    assert len(repository.list_samples(task.task_id)) == 2

    failed_repository, failed_task, failed_runtime, failed_connector = _setup(tmp_path / "failed")
    failed_sample = failed_repository.list_samples(failed_task.task_id)[0]
    failed_actions = iter(
        [
            {
                "action": "add_sample",
                "arguments": {
                    "input": failed_sample["input"],
                    "expected": failed_sample["expected"],
                },
                "message": "暂存",
            },
            {"action": "run_shell", "arguments": {}, "message": "拒绝"},
        ]
    )
    failed_builder = EIMBuilder(
        failed_repository,
        failed_connector,  # type: ignore[arg-type]
        failed_runtime,
        tmp_path / "failed",
        model_runner=lambda _configuration_id, _prompt: (
            next(failed_actions),
            SimpleNamespace(),
        ),
    )
    failed_builder.save_draft(
        failed_task.task_id,
        EIMDSL(mappings={"body": "message.text"}),
    )
    with pytest.raises(ValueError, match="未登记"):
        failed_builder.build(failed_task.task_id, configuration_id="fake-model")
    assert len(failed_repository.list_samples(failed_task.task_id)) == 1
    assert failed_repository.list_versions(failed_task.task_id) == []


def test_pipeline_failure_policies_retry_and_archive_raw(tmp_path: Path) -> None:
    repository, task, runtime, _connector = _setup(tmp_path)

    def publish(policy: str, version_id: str) -> None:
        repository.transition_build(task.task_id, BuildState.BUILDING)
        repository.transition_build(task.task_id, BuildState.VALIDATING)
        repository.publish_version(
            EIMTaskVersion(
                version_id=version_id,
                task_id=task.task_id,
                manifest={},
                dsl=EIMDSL(
                    mappings={"body": "message.text"},
                    ai_steps=[
                        AIStep(
                            configuration_id="ai-1",
                            input_fields=["body"],
                            output_schema={
                                "type": "object",
                                "properties": {"body": {"type": "string"}},
                                "required": ["body"],
                                "additionalProperties": False,
                            },
                        )
                    ],
                    failure_policy=policy,
                ).model_dump(mode="json"),
                bundle_path=f"eim/bundles/{version_id}",
                content_hash=f"hash-{version_id}",
            ),
            expected_draft_revision=repository.get_task(task.task_id).draft_revision,
        )

    publish("retry", "version-retry")
    repository.transition_observed(
        task.task_id,
        ObservedState.STARTING,
        desired=DesiredState.RUNNING,
    )
    repository.transition_observed(task.task_id, ObservedState.RUNNING)
    first = CanonicalEvent.model_validate(repository.list_samples(task.task_id)[0]["input"])
    first.event_id = "event-retry"
    first.message_id = "message-retry"
    assert repository.insert_event(task.task_id, first)
    retry_pipeline = EIMPipeline(
        repository,
        runtime,
        ai_processor=lambda *_args: (_ for _ in ()).throw(RuntimeError("暂时失败")),
    )
    assert retry_pipeline.process_once() == 1
    with repository._connect() as database:
        retry_row = database.execute(
            "SELECT processing_state, attempts FROM eim_event_inbox WHERE event_id=?",
            (first.event_id,),
        ).fetchone()
    assert tuple(retry_row) == ("received", 1)

    repository.transition_observed(
        task.task_id,
        ObservedState.STOPPING,
        desired=DesiredState.STOPPED,
    )
    repository.transition_observed(task.task_id, ObservedState.STOPPED)
    publish("archive_raw", "version-raw")
    repository.transition_observed(
        task.task_id,
        ObservedState.STARTING,
        desired=DesiredState.RUNNING,
    )
    repository.transition_observed(task.task_id, ObservedState.RUNNING)
    second = first.model_copy(update={"event_id": "event-raw", "message_id": "message-raw"})
    assert repository.insert_event(task.task_id, second)
    raw_pipeline = EIMPipeline(
        repository,
        runtime,
        ai_processor=lambda *_args: (_ for _ in ()).throw(RuntimeError("模型不可用")),
    )
    assert raw_pipeline.process_once() == 1
    with repository._connect() as database:
        raw_state = database.execute(
            "SELECT processing_state FROM eim_event_inbox WHERE event_id=?",
            (second.event_id,),
        ).fetchone()[0]
        queued = database.execute(
            "SELECT COUNT(*) FROM eim_delivery_outbox WHERE event_id=?",
            (second.event_id,),
        ).fetchone()[0]
    assert raw_state == "completed"
    assert queued == 1
