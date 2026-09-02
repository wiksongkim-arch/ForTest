"""EIM 领域模型、状态机与稳定标识。"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def utc_now() -> str:
    """返回可排序且带时区的 UTC 时间。"""

    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_ulid(timestamp_ms: int | None = None) -> str:
    """使用标准库生成 26 位、按时间排序的 ULID。"""

    timestamp = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    if timestamp < 0 or timestamp >= 1 << 48:
        raise ValueError("ULID 时间戳超出范围")
    value = (timestamp << 80) | int.from_bytes(os.urandom(10), "big")
    chars = ["0"] * 26
    for index in range(25, -1, -1):
        chars[index] = _CROCKFORD[value & 31]
        value >>= 5
    return "".join(chars)


def new_display_id(now: datetime | None = None, *, suffix: str | None = None) -> str:
    """生成便于人工识别的 EIM 时间前缀 ID。"""

    current = (now or datetime.now()).astimezone()
    short = (suffix or new_ulid()[-4:]).upper()
    return f"EIM-{current:%Y%m%d-%H%M%S}-{short}"


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    AUTHORIZING = "authorizing"
    CONNECTED = "connected"
    EXPIRED = "expired"
    PERMISSION_MISSING = "permission_missing"
    ERROR = "error"


class BuildState(StrEnum):
    DRAFT = "draft"
    BUILDING = "building"
    VALIDATING = "validating"
    READY = "ready"
    FAILED = "failed"


class DesiredState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"


class ObservedState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    ERROR = "error"
    STOPPED_APP_EXIT = "stopped_app_exit"


class DestinationType(StrEnum):
    DINGTALK_DOC = "dingtalk_doc"
    DINGTALK_SHEET = "dingtalk_sheet"
    DINGTALK_AITABLE = "dingtalk_aitable"


class EventType(StrEnum):
    MESSAGE = "message"
    REACTION = "reaction"


class MessageKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    AUDIO = "audio"
    VIDEO = "video"
    CARD = "card"
    QUOTE = "quote"
    UNKNOWN = "unknown"


class DeliveryState(StrEnum):
    PENDING = "pending"
    DELIVERING = "delivering"
    RETRY = "retry"
    COMMIT_UNKNOWN = "commit_unknown"
    COMPLETED = "completed"
    DEAD_LETTER = "dead_letter"


class StrictModel(BaseModel):
    """所有持久化模型拒绝未知字段，避免静默吞掉配置错误。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EIMConnection(StrictModel):
    connection_id: str = Field(default_factory=new_ulid)
    platform: Literal["dingtalk"] = "dingtalk"
    account_name: str = ""
    account_id: str = ""
    organization_name: str = ""
    organization_id: str = ""
    profile: str = ""
    config_dir_ref: str
    connection_state: ConnectionState = ConnectionState.DISCONNECTED
    capabilities: dict[str, Any] = Field(default_factory=dict)
    checked_at: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class EIMDestination(StrictModel):
    destination_id: str = Field(default_factory=new_ulid)
    connection_id: str
    destination_type: DestinationType
    url: str
    stable_ids: dict[str, str] = Field(default_factory=dict)
    schema_snapshot: dict[str, Any] = Field(default_factory=dict)
    mapping_revision: int = 0
    capabilities: dict[str, Any] = Field(default_factory=dict)
    checked_at: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) > 2_048:
            raise ValueError("归档目标链接过长")
        try:
            parsed = urlsplit(normalized)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("归档目标链接格式不合法") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != "alidocs.dingtalk.com"
            or port not in {None, 443}
            or parsed.username
            or parsed.password
            or not parsed.path.startswith("/i/")
        ):
            raise ValueError("归档目标必须是钉钉文档的 HTTPS 链接")
        if any(
            key.casefold() in {"token", "access_token", "refresh_token", "password", "secret", "api_key"}
            for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
        ):
            raise ValueError("归档目标链接不能包含凭证参数")
        return normalized


class EIMTask(StrictModel):
    task_id: str = Field(default_factory=new_ulid)
    display_id: str = Field(default_factory=new_display_id)
    name: str
    platform: Literal["dingtalk"] = "dingtalk"
    connection_id: str
    source_id: str
    source_name: str
    destination_id: str
    event_types: list[EventType] = Field(
        default_factory=lambda: [EventType.MESSAGE, EventType.REACTION]
    )
    build_state: BuildState = BuildState.DRAFT
    desired_state: DesiredState = DesiredState.STOPPED
    observed_state: ObservedState = ObservedState.STOPPED
    active_version_id: str | None = None
    draft_revision: int = 1
    deleted_at: str | None = None
    last_activity_at: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @field_validator("name", "source_id", "destination_id", "connection_id")
    @classmethod
    def _required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("任务必填字段不能为空")
        return normalized

    @property
    def accepts_work(self) -> bool:
        """任务只有在未删除且处于活动运行态时才能领取新工作。"""

        return (
            self.deleted_at is None
            and self.desired_state is DesiredState.RUNNING
            and self.observed_state
            in {
                ObservedState.STARTING,
                ObservedState.RUNNING,
                ObservedState.RECONNECTING,
                ObservedState.DEGRADED,
            }
        )


class EIMTaskVersion(StrictModel):
    version_id: str = Field(default_factory=new_ulid)
    task_id: str
    manifest: dict[str, Any]
    dsl: dict[str, Any]
    bundle_path: str
    content_hash: str
    builder_configuration_id: str | None = None
    status: Literal["ready", "superseded", "failed"] = "ready"
    test_evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)


class MediaAsset(StrictModel):
    resource_id: str = ""
    message_id: str = ""
    conversation_id: str = ""
    file_name: str = ""
    mime_type: str = "application/octet-stream"
    size: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    local_path: str | None = None
    stable_url: str | None = None


class CanonicalEvent(StrictModel):
    platform: Literal["dingtalk"] = "dingtalk"
    connection_id: str
    event_id: str
    event_type: EventType
    message_id: str = ""
    conversation_id: str
    sender_id: str = ""
    sender_name: str = ""
    occurred_at: str
    received_at: str = Field(default_factory=utc_now)
    message_kind: MessageKind = MessageKind.UNKNOWN
    text: str = ""
    quoted_message: dict[str, Any] | None = None
    reaction: dict[str, Any] | None = None
    media_assets: list[MediaAsset] = Field(default_factory=list)
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return ":".join(
            (self.platform, self.connection_id, self.conversation_id, self.event_id)
        )


class FilterRule(StrictModel):
    field: str = Field(min_length=1, max_length=120)
    operator: Literal["equals", "contains", "regex", "in", "exists"]
    value: Any = None


class ContextPolicy(StrictModel):
    include_quote: bool = True
    attachment_window_minutes: int = Field(default=0, ge=0, le=60)
    attachment_forward_minutes: int = Field(default=0, ge=0, le=60)
    related_message_limit: int = Field(default=0, ge=0, le=50)


class Extractor(StrictModel):
    kind: Literal["regex", "path", "fixed", "transform"]
    output: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,119}$")
    source: str | None = Field(default=None, max_length=120)
    pattern: str | None = Field(default=None, max_length=500)
    group: int | str = 0
    path: str | None = Field(default=None, max_length=240)
    value: Any = None
    transform: Literal[
        "trim",
        "lower",
        "upper",
        "integer",
        "number",
        "string",
        "dingtalk_site",
        "dingtalk_issue_text",
    ] | None = None

    @model_validator(mode="after")
    def _required_for_kind(self) -> "Extractor":
        if self.kind == "regex" and (not self.source or self.pattern is None):
            raise ValueError("regex 提取器必须提供 source 和 pattern")
        if self.kind == "path" and not self.path:
            raise ValueError("path 提取器必须提供 path")
        if self.kind == "transform" and (not self.source or not self.transform):
            raise ValueError("transform 提取器必须提供 source 和 transform")
        return self


class MediaPolicy(StrictModel):
    download: bool = True
    max_bytes: int = Field(default=50 * 1024 * 1024, ge=1, le=500 * 1024 * 1024)
    retention_hours: int = Field(default=24, ge=1, le=24 * 30)
    archive_as: Literal["link", "attachment", "auto"] = "auto"


class AIStep(StrictModel):
    configuration_id: str = Field(min_length=1, max_length=120)
    input_fields: list[str] = Field(min_length=1, max_length=50)
    redacted_fields: list[str] = Field(default_factory=list)
    output_schema: dict[str, Any]
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    daily_budget: float = Field(default=100_000.0, gt=0)
    budget_unit: Literal["tokens", "calls"] = "tokens"
    include_images: bool = False
    budget_action: Literal["skip", "archive_raw", "retry", "stop"] = "archive_raw"
    unavailable_action: Literal["skip", "archive_raw", "retry", "stop"] = "archive_raw"


class EIMDSL(StrictModel):
    schema_version: Literal["eim-dsl/v1"] = "eim-dsl/v1"
    triggers: list[EventType] = Field(
        default_factory=lambda: [EventType.MESSAGE, EventType.REACTION],
        min_length=1,
        max_length=2,
    )
    filters: list[FilterRule] = Field(default_factory=list, max_length=50)
    context: ContextPolicy = Field(default_factory=ContextPolicy)
    extractors: list[Extractor] = Field(default_factory=list, max_length=50)
    ai_steps: list[AIStep] = Field(default_factory=list, max_length=5)
    mappings: dict[str, str] = Field(default_factory=dict, max_length=200)
    media_policy: MediaPolicy = Field(default_factory=MediaPolicy)
    destination_action: Literal["append", "upsert", "update"] = "append"
    failure_policy: Literal["skip", "retry", "archive_raw", "dead_letter"] = (
        "dead_letter"
    )

    @field_validator("mappings")
    @classmethod
    def _validate_mappings(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for target, source in value.items():
            target_name, source_name = str(target).strip(), str(source).strip()
            if not target_name or not source_name or len(target_name) > 160 or len(source_name) > 160:
                raise ValueError("DSL 映射字段不能为空或过长")
            normalized[target_name] = source_name
        return normalized


_BUILD_TRANSITIONS: dict[BuildState, set[BuildState]] = {
    BuildState.DRAFT: {BuildState.BUILDING},
    BuildState.BUILDING: {BuildState.VALIDATING, BuildState.FAILED},
    BuildState.VALIDATING: {BuildState.READY, BuildState.FAILED},
    BuildState.READY: {BuildState.DRAFT, BuildState.BUILDING},
    BuildState.FAILED: {BuildState.DRAFT, BuildState.BUILDING},
}

_OBSERVED_TRANSITIONS: dict[ObservedState, set[ObservedState]] = {
    ObservedState.STOPPED: {ObservedState.STARTING},
    ObservedState.STARTING: {
        ObservedState.RUNNING,
        ObservedState.DEGRADED,
        ObservedState.STOPPING,
        ObservedState.ERROR,
    },
    ObservedState.RUNNING: {
        ObservedState.RECONNECTING,
        ObservedState.DEGRADED,
        ObservedState.STOPPING,
        ObservedState.ERROR,
    },
    ObservedState.RECONNECTING: {
        ObservedState.RUNNING,
        ObservedState.DEGRADED,
        ObservedState.STOPPING,
        ObservedState.ERROR,
    },
    ObservedState.DEGRADED: {
        ObservedState.RECONNECTING,
        ObservedState.RUNNING,
        ObservedState.STOPPING,
        ObservedState.ERROR,
    },
    ObservedState.STOPPING: {
        ObservedState.STOPPED,
        ObservedState.STOPPED_APP_EXIT,
        ObservedState.ERROR,
    },
    ObservedState.ERROR: {ObservedState.STARTING, ObservedState.STOPPED},
    ObservedState.STOPPED_APP_EXIT: {
        ObservedState.STARTING,
        ObservedState.STOPPED,
    },
}


def validate_build_transition(current: BuildState, target: BuildState) -> None:
    """拒绝越过构建门禁的状态变化。"""

    if target == current:
        return
    if target not in _BUILD_TRANSITIONS[current]:
        raise ValueError(f"非法构建状态转换：{current} -> {target}")


def validate_observed_transition(
    current: ObservedState,
    target: ObservedState,
) -> None:
    """拒绝无法解释的运行状态跳转。"""

    if target == current:
        return
    if target not in _OBSERVED_TRANSITIONS[current]:
        raise ValueError(f"非法运行状态转换：{current} -> {target}")


def ensure_editable(task: EIMTask) -> None:
    """只有完全停止的任务可以修改、复制或删除。"""

    if task.observed_state is not ObservedState.STOPPED:
        raise ValueError("任务运行中，必须先停止后再编辑")
