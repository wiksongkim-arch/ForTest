"""DWS 固定运行时、事件规范化与 ready 生命周期契约测试。"""

from __future__ import annotations

import io
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from backend.eim.connections.dws_runtime import (
    DWS_EXECUTABLE_SHA256,
    DWS_VERSION,
    DWSRuntime,
)
from backend.eim.events.dingtalk_normalizer import normalize_dingtalk_event
from backend.eim.events.subscription import DingTalkSubscription
from backend.eim.models import (
    CanonicalEvent,
    EIMConnection,
    EventType,
    MediaAsset,
    MediaPolicy,
    MessageKind,
)
from backend.eim.runtime.media import MediaManager


def test_normalizer_covers_message_reaction_media_and_self_loop() -> None:
    message = normalize_dingtalk_event(
        {
            "type": "user_im_message_receive_group",
            "event_id": "event-1",
            "message_id": "message-1",
            "conversation_id": "cid-1",
            "sender": "张三",
            "sender_open_dingtalk_id": "open-user-1",
            "create_time": 1_788_192_000_000,
            "message_type": "image",
            "content": {
                "text": "截图",
                "downloadCode": "resource-1",
                "fileName": "screen.png",
                "mimeType": "image/png",
            },
            "quoted_message": {"message_id": "quoted-1", "content": "上文"},
        },
        connection_id="connection-1",
        expected_conversation_id="cid-1",
    )
    assert message.event_type is EventType.MESSAGE
    assert message.message_kind is MessageKind.IMAGE
    assert message.text == "截图"
    assert message.media_assets[0].resource_id == "resource-1"
    assert message.media_assets[0].message_id == "message-1"

    flattened = normalize_dingtalk_event(
        {
            "type": "user_im_message_receive_group",
            "event_id": "event-flat-media",
            "message_id": "message-flat-media",
            "conversation_id": "cid-1",
            "content": "[图片消息](mediaId=@resource-flat)",
        },
        connection_id="connection-1",
        expected_conversation_id="cid-1",
    )
    assert flattened.message_kind is MessageKind.IMAGE
    assert flattened.media_assets == [
        MediaAsset(
            resource_id="@resource-flat",
            message_id="message-flat-media",
            conversation_id="cid-1",
            mime_type="image/jpeg",
        )
    ]
    video = normalize_dingtalk_event(
        {
            "event_id": "event-flat-video",
            "message_id": "message-flat-video",
            "conversation_id": "cid-1",
            "content": "[视频消息](mediaId=@resource-video)",
        },
        connection_id="connection-1",
        expected_conversation_id="cid-1",
    )
    assert video.message_kind is MessageKind.VIDEO
    assert video.media_assets[0].mime_type == "video/mp4"

    with pytest.raises(ValueError, match="别名冲突"):
        normalize_dingtalk_event(
            {
                "event_id": "event-conflict",
                "conversation_id": "cid-1",
                "conversationId": "cid-other",
            },
            connection_id="connection-1",
            expected_conversation_id="cid-1",
        )
    assert message.quoted_message["message_id"] == "quoted-1"

    reaction = normalize_dingtalk_event(
        {
            "type": "user_im_message_reaction_group",
            "event_id": "event-2",
            "message_id": "message-1",
            "conversation_id": "cid-1",
            "operator_open_dingtalk_id": "open-user-2",
            "reaction_name": "LIKE",
            "operation_type": "add",
            "event_time": "2026-09-01T00:00:00Z",
        },
        connection_id="connection-1",
        expected_conversation_id="cid-1",
    )
    assert reaction.event_type is EventType.REACTION
    assert reaction.reaction["name"] == "LIKE"

    own = normalize_dingtalk_event(
        {
            "event_id": "event-own",
            "message_id": "message-own",
            "conversation_id": "cid-1",
            "sender_open_dingtalk_id": "me",
            "content": "ignored",
        },
        connection_id="connection-1",
        expected_conversation_id="cid-1",
        own_open_id="me",
    )
    assert own is None
    own_from_gap = normalize_dingtalk_event(
        {
            "event_id": "event-own-gap",
            "message_id": "message-own-gap",
            "conversation_id": "cid-1",
            "senderUserId": "me",
            "content": "ignored",
        },
        connection_id="connection-1",
        expected_conversation_id="cid-1",
        own_open_id="me",
    )
    assert own_from_gap is None
    with pytest.raises(ValueError, match="群 ID"):
        normalize_dingtalk_event(
            {"event_id": "wrong", "conversation_id": "cid-other"},
            connection_id="connection-1",
            expected_conversation_id="cid-1",
        )


class _Input(io.StringIO):
    def __init__(self, done: threading.Event):
        super().__init__()
        self.done = done

    def close(self) -> None:
        self.done.set()
        super().close()


class _Process:
    def __init__(self):
        self.done = threading.Event()
        self.stdin = _Input(self.done)
        self.stdout = io.StringIO('{"event_id":"event-1"}\nnot-json\n')
        self.stderr = io.StringIO(
            "[event] subscription event_key=message subscribe_id=sub-1\n"
            "[event] ready event_count=1 bus_pid=10\n"
        )
        self.returncode: int | None = None

    def wait(self, timeout: float | None = None) -> int:
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired("fake", timeout)
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.done.set()

    def kill(self) -> None:
        self.done.set()


class _Runtime:
    def __init__(self, root: Path):
        self.root = root
        self.process = _Process()
        self.stop_calls: list[list[str]] = []

    def config_dir(self, _connection_id: str) -> Path:
        return self.root

    def popen(self, _arguments: list[str], *, config_dir: Path) -> _Process:
        assert config_dir == self.root
        return self.process

    def run(self, arguments: list[str], **_kwargs: Any) -> None:
        self.stop_calls.append(arguments)


def test_subscription_waits_for_ready_drains_lines_and_stops_cleanly(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    received: list[dict[str, Any]] = []
    errors: list[str] = []
    delivered = threading.Event()

    def on_event(value: dict[str, Any]) -> None:
        received.append(value)
        delivered.set()

    subscription = DingTalkSubscription(
        runtime,  # type: ignore[arg-type]
        connection_id="connection",
        profile="corp:user",
        conversation_id="cid",
        events=("message",),
        on_event=on_event,
        on_error=errors.append,
    )
    subscription.start(timeout=2)
    assert delivered.wait(1)
    assert received == [{"event_id": "event-1"}]
    assert any("解析失败" in message for message in errors)
    subscription.stop()
    assert runtime.process.returncode == 0
    assert runtime.stop_calls == []


def test_runtime_restricts_config_environment_and_pinned_binary(tmp_path: Path) -> None:
    runtime = DWSRuntime(tmp_path)
    connection_id = "01K42YJ9M2E7H05KTA7VC6ER8R"
    config_dir = runtime.config_dir(connection_id)
    with pytest.raises(ValueError, match="连接 ID"):
        runtime.config_dir("../global")
    environment = runtime.environment(config_dir)
    assert environment["DWS_CONFIG_DIR"] == str(config_dir)
    assert "HTTP_PROXY" not in environment
    assert "OPENAI_API_KEY" not in environment
    with pytest.raises(ValueError, match="参数"):
        runtime._arguments(["auth", "status\nmalicious"])
    assert runtime._arguments(["doc", "--content", "第一行\n第二行"]) == [
        "doc",
        "--content",
        "第一行\n第二行",
    ]
    with pytest.raises(ValueError, match="参数"):
        runtime._arguments(["doc", "--profile", "corp:user\nmalicious"])

    bundled = (
        Path(__file__).resolve().parents[2]
        / "windows_native"
        / ".tools"
        / "dws"
        / f"v{DWS_VERSION}"
        / "runtime"
        / "dws.exe"
    )
    if bundled.is_file():
        assert runtime._sha256(bundled) == DWS_EXECUTABLE_SHA256
        runtime._verify_executable(bundled)


def test_media_download_uses_relative_output_and_hashes_content(tmp_path: Path) -> None:
    class MediaRuntime:
        def config_dir(self, _connection_id: str) -> Path:
            return tmp_path / "config"

        def run_json(self, arguments: list[str], **_kwargs: Any) -> dict[str, Any]:
            output = arguments[arguments.index("--output") + 1]
            assert not Path(output).is_absolute()
            target = tmp_path / output
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"downloaded-media")
            return {"localPath": output, "sizeBytes": len(b"downloaded-media")}

    event = CanonicalEvent(
        connection_id="01K42YJ9M2E7H05KTA7VC6ER8R",
        event_id="event-media",
        event_type=EventType.MESSAGE,
        message_id="message-media",
        conversation_id="cid",
        occurred_at="2026-09-01T00:00:00+00:00",
        message_kind=MessageKind.FILE,
        media_assets=[
            MediaAsset(
                resource_id="resource-media",
                file_name="report.pdf",
                mime_type="application/pdf",
                size=len(b"downloaded-media"),
            )
        ],
    )
    connection = EIMConnection(
        connection_id=event.connection_id,
        config_dir_ref="local",
        profile="corp:user",
    )
    downloaded = MediaManager(tmp_path, MediaRuntime()).download(
        "task-media",
        event,
        connection,
        MediaPolicy(max_bytes=1024),
    )
    asset = downloaded.media_assets[0]
    assert asset.local_path and (tmp_path / asset.local_path).read_bytes() == b"downloaded-media"
    assert asset.sha256 and len(asset.sha256) == 64


def test_media_download_queries_source_message_for_unknown_size(tmp_path: Path) -> None:
    class QueryRuntime:
        def config_dir(self, _connection_id: str) -> Path:
            return tmp_path / "config"

        def run_json(self, arguments: list[str], **_kwargs: Any) -> dict[str, Any]:
            assert "+messages-mget" in arguments
            assert arguments[arguments.index("--msg-ids") + 1] == "source-message"
            output = tmp_path / arguments[arguments.index("--output-dir") + 1]
            output.mkdir(parents=True, exist_ok=True)
            local = output / "download.jpg"
            local.write_bytes(b"downloaded-media")
            return {
                "complete": True,
                "resourceDownloads": {
                    "ok": True,
                    "partial": False,
                    "failedCount": 0,
                    "downloads": [
                        {
                            "resourceId": "resource-1",
                            "localPath": local.relative_to(tmp_path).as_posix(),
                            "sizeBytes": local.stat().st_size,
                        }
                    ],
                },
            }

    connection = EIMConnection(
        connection_id="01K42YJ9M2E7H05KTA7VC6ER8R",
        config_dir_ref="local",
        profile="corp:user",
    )
    event = CanonicalEvent(
        connection_id=connection.connection_id,
        event_id="unknown",
        event_type=EventType.MESSAGE,
        message_id="trigger-message",
        conversation_id="cid",
        occurred_at="2026-09-01T00:00:00+00:00",
        message_kind=MessageKind.TEXT,
        media_assets=[
            MediaAsset(
                resource_id="resource-1",
                message_id="source-message",
                conversation_id="cid",
            )
        ],
    )
    downloaded = MediaManager(tmp_path, QueryRuntime()).download(
        "task", event, connection, MediaPolicy(max_bytes=1024)
    )
    assert downloaded.media_assets[0].message_id == "source-message"
    assert downloaded.media_assets[0].local_path


def test_media_download_fails_closed_for_aggregate_size(tmp_path: Path) -> None:
    class UnexpectedRuntime:
        def run_json(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("大小预检失败时不应开始下载")

    connection = EIMConnection(
        connection_id="01K42YJ9M2E7H05KTA7VC6ER8R",
        config_dir_ref="local",
        profile="corp:user",
    )
    manager = MediaManager(tmp_path, UnexpectedRuntime())
    base = dict(
        connection_id=connection.connection_id,
        event_type=EventType.MESSAGE,
        message_id="message-media",
        conversation_id="cid",
        occurred_at="2026-09-01T00:00:00+00:00",
        message_kind=MessageKind.FILE,
    )
    aggregate = CanonicalEvent(
        event_id="aggregate",
        media_assets=[
            MediaAsset(resource_id="resource-1", size=6),
            MediaAsset(resource_id="resource-2", size=6),
        ],
        **base,
    )
    with pytest.raises(ValueError, match="合计"):
        manager.download("task", aggregate, connection, MediaPolicy(max_bytes=10))
