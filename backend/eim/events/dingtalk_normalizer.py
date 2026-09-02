"""把 DWS NDJSON 与断线历史消息转换为稳定 CanonicalEvent。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from backend.eim.models import (
    CanonicalEvent,
    EventType,
    MediaAsset,
    MessageKind,
    utc_now,
)


_FLATTENED_MEDIA = re.compile(
    r"\[(?P<label>[^\]\r\n]{1,64})\]\(\s*mediaId\s*=\s*(?P<resource>[^)\s]{1,512})\s*\)",
    re.IGNORECASE,
)


def normalize_dingtalk_event(
    payload: dict[str, Any],
    *,
    connection_id: str,
    expected_conversation_id: str,
    own_open_id: str = "",
    allow_message_id_as_event_id: bool = False,
) -> CanonicalEvent | None:
    """严格校验路由字段；自己发送的事件按已接受的 self-loop 口径排除。"""

    if not isinstance(payload, dict):
        raise ValueError("DWS 事件必须是对象")
    event_name = str(payload.get("type") or payload.get("event_type") or "").casefold()
    is_reaction = "reaction" in event_name or any(
        key in payload for key in ("reaction_name", "reaction_text", "operation_type")
    )
    event_type = EventType.REACTION if is_reaction else EventType.MESSAGE
    message_id = str(
        payload.get("message_id")
        or payload.get("messageId")
        or payload.get("openMessageId")
        or ""
    ).strip()
    event_id = str(payload.get("event_id") or payload.get("eventId") or "").strip()
    if not event_id and allow_message_id_as_event_id:
        event_id = message_id
    conversation_aliases = [
        str(payload.get(name) or "").strip()
        for name in (
            "conversation_id",
            "conversationId",
            "openConversationId",
        )
        if str(payload.get(name) or "").strip()
    ]
    if len(set(conversation_aliases)) > 1:
        raise ValueError("DWS 事件群 ID 别名冲突")
    conversation_id = conversation_aliases[0] if conversation_aliases else ""
    if not event_id:
        raise ValueError("DWS 事件缺少稳定 event_id")
    if not conversation_id or conversation_id != expected_conversation_id:
        raise ValueError("DWS 事件群 ID 与当前监听任务不一致")
    sender_id = str(
        payload.get("sender_open_dingtalk_id")
        or payload.get("senderOpenDingTalkId")
        or payload.get("sender_user_id")
        or payload.get("senderUserId")
        or payload.get("sender_id")
        or payload.get("senderId")
        or payload.get("operator_open_dingtalk_id")
        or payload.get("operatorOpenDingTalkId")
        or ""
    ).strip()
    if own_open_id and sender_id == own_open_id:
        return None
    content = _content(payload.get("content", payload.get("text", "")))
    quoted = payload.get("quoted_message") or payload.get("quotedMessage")
    media_assets = _media_assets(
        content,
        message_id=message_id,
        conversation_id=conversation_id,
    )
    kind = _message_kind(payload, content, quoted, media_assets)
    return CanonicalEvent(
        connection_id=connection_id,
        event_id=event_id,
        event_type=event_type,
        message_id=message_id,
        conversation_id=conversation_id,
        sender_id=sender_id,
        sender_name=str(payload.get("sender") or payload.get("operator") or ""),
        occurred_at=_timestamp(
            payload.get("event_time")
            or payload.get("eventTime")
            or payload.get("create_time")
            or payload.get("createTime")
            or payload.get("timestamp")
        ),
        received_at=utc_now(),
        message_kind=kind,
        text=_text(content),
        quoted_message=quoted if isinstance(quoted, dict) else None,
        reaction=(
            {
                "name": str(payload.get("reaction_name") or ""),
                "text": str(payload.get("reaction_text") or ""),
                "operation": str(payload.get("operation_type") or ""),
                "operator_id": sender_id,
                "operation_time": str(payload.get("operation_time") or ""),
            }
            if is_reaction
            else None
        ),
        media_assets=media_assets,
        raw_payload=payload,
    )


def _content(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return value


def _message_kind(
    payload: dict[str, Any],
    content: Any,
    quoted: Any,
    media_assets: list[MediaAsset],
) -> MessageKind:
    values = [
        payload.get("message_type"),
        payload.get("messageType"),
        payload.get("msg_type"),
        payload.get("msgtype"),
    ]
    if isinstance(content, dict):
        values.extend((content.get("type"), content.get("msgtype"), content.get("messageType")))
    marker = " ".join(str(item).casefold() for item in values if item)
    for words, kind in (
        (("image", "picture", "photo"), MessageKind.IMAGE),
        (("audio", "voice"), MessageKind.AUDIO),
        (("video",), MessageKind.VIDEO),
        (("file", "attachment"), MessageKind.FILE),
        (("card", "interactive"), MessageKind.CARD),
        (("text", "markdown", "richtext"), MessageKind.TEXT),
    ):
        if any(word in marker for word in words):
            return kind
    for prefix, kind in (
        ("image/", MessageKind.IMAGE),
        ("audio/", MessageKind.AUDIO),
        ("video/", MessageKind.VIDEO),
    ):
        if any(asset.mime_type.casefold().startswith(prefix) for asset in media_assets):
            return kind
    if media_assets:
        return MessageKind.FILE
    if quoted:
        return MessageKind.QUOTE
    if isinstance(content, str) or (isinstance(content, dict) and "text" in content):
        return MessageKind.TEXT
    return MessageKind.UNKNOWN


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content[:65_536]
    if isinstance(content, dict):
        for key in ("text", "title", "content", "fileName", "file_name"):
            if isinstance(content.get(key), str):
                return content[key]
    if content in (None, ""):
        return ""
    return json.dumps(content, ensure_ascii=False, sort_keys=True)[:65_536]


def _media_assets(
    content: Any,
    *,
    message_id: str = "",
    conversation_id: str = "",
) -> list[MediaAsset]:
    candidates: list[dict[str, Any]] = []
    if isinstance(content, dict):
        candidates.append(content)
        for key in ("attachments", "files", "resources"):
            value = content.get(key)
            if isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
    assets: list[MediaAsset] = []
    if isinstance(content, str):
        for match in _FLATTENED_MEDIA.finditer(content):
            resource_id = match.group("resource").strip()
            if not resource_id or any(ord(character) < 32 for character in resource_id):
                continue
            label = match.group("label").casefold()
            mime_type = (
                "image/jpeg"
                if "图片" in label or "image" in label
                else "video/mp4"
                if "视频" in label or "video" in label
                else "audio/mpeg"
                if "语音" in label or "音频" in label or "audio" in label
                else "application/octet-stream"
            )
            assets.append(
                MediaAsset(
                    resource_id=resource_id,
                    message_id=message_id,
                    conversation_id=conversation_id,
                    mime_type=mime_type,
                )
            )
    for item in candidates[:100]:
        resource_id = str(
            item.get("resource_id")
            or item.get("resourceId")
            or item.get("media_id")
            or item.get("mediaId")
            or item.get("downloadCode")
            or ""
        )
        if not resource_id:
            continue
        size = item.get("size") or item.get("fileSize")
        try:
            normalized_size = int(size) if size is not None else None
        except (TypeError, ValueError):
            normalized_size = None
        assets.append(
            MediaAsset(
                resource_id=resource_id,
                message_id=message_id,
                conversation_id=conversation_id,
                file_name=str(item.get("file_name") or item.get("fileName") or ""),
                mime_type=str(item.get("mime_type") or item.get("mimeType") or "application/octet-stream"),
                size=normalized_size if normalized_size is None or normalized_size >= 0 else None,
                stable_url=str(item.get("downloadUrl") or item.get("url") or "") or None,
            )
        )
    return assets


def _timestamp(value: Any) -> str:
    if value is None or value == "":
        return utc_now()
    if isinstance(value, (int, float)) or str(value).isdigit():
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1_000
        try:
            return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="milliseconds")
        except (OSError, OverflowError, ValueError):
            return utc_now()
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).isoformat(
            timespec="milliseconds"
        )
    except ValueError:
        return text[:80]
