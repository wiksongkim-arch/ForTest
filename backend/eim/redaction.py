"""EIM 持久化和导出边界使用的结构化脱敏。"""

from __future__ import annotations

import re
from typing import Any

from backend.security.redaction import redact_text


_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:api_?key|access_?token|refresh_?token|token|password|secret|app_?secret)(?:$|_)",
    re.IGNORECASE,
)
_MAX_DEPTH = 20
_MAX_ITEMS = 10_000
_MAX_STRING = 1024 * 1024
_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token=",
    "token:",
    "password",
    "secret",
    "authorization",
    "bearer ",
    "sk-",
)
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.IGNORECASE)
_IDENTITY_NUMBER = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")


def _redact_sample_text(value: str) -> str:
    text = redact_text(value)
    text = _PHONE.sub("<redacted:phone>", text)
    text = _EMAIL.sub("<redacted:email>", text)
    return _IDENTITY_NUMBER.sub("<redacted:id>", text)


def redact_structure(value: Any, *, _depth: int = 0) -> Any:
    """递归清理不可信载荷；限制深度和项目数以防异常事件耗尽资源。"""

    if _depth > _MAX_DEPTH:
        return "<redacted: depth limit>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                result["<truncated>"] = True
                break
            name = str(key)[:256]
            result[name] = (
                "<redacted>"
                if _SENSITIVE_KEY.search(name)
                else redact_structure(item, _depth=_depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            redact_structure(item, _depth=_depth + 1)
            for item in value[:_MAX_ITEMS]
        ]
    if isinstance(value, str):
        return _redact_sample_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value)


def sanitize_payload(value: Any, *, _depth: int = 0) -> Any:
    """保留普通消息内容，只清理敏感键及带明显凭证标记的字符串。"""

    if _depth > _MAX_DEPTH:
        return "<redacted: depth limit>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                result["<truncated>"] = True
                break
            name = str(key)[:256]
            result[name] = (
                "<redacted>"
                if _SENSITIVE_KEY.search(name)
                else sanitize_payload(item, _depth=_depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize_payload(item, _depth=_depth + 1) for item in value[:_MAX_ITEMS]]
    if isinstance(value, str):
        lowered = value.casefold()
        return (
            redact_text(value)
            if any(marker in lowered for marker in _SECRET_MARKERS)
            else value[:_MAX_STRING]
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value)
