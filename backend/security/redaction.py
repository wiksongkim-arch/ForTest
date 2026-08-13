"""Bounded, idempotent redaction for user-facing errors and logs."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


MAX_REDACTED_TEXT_LENGTH = 65_536
_TRUNCATED = "<truncated>"
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
SK_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}", re.IGNORECASE)
ASSIGNMENT_PATTERN = re.compile(
    r"""
    (?P<prefix>
        [\"']?(?:api[_-]?key|access[_-]?token|token|key|password|secret)[\"']?
        \s*[:=]\s*
    )
    (?:
        (?P<quote>[\"'])
        (?P<quoted>
            (?:\\[^\r\n]|(?!(?P=quote))[^\\\r\n])*
        )
        (?P=quote)
        |
        (?P<unterminated>[\"'][^,;&}\]]*)
        |
        (?P<bare>[^\s,;&}\]\)]+)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
BEARER_PATTERN = re.compile(
    r"(?i)(Authorization\s*(?::|=)?\s*Bearer\s*)[^\s,;&}\]]+"
)


def _bounded_input(value: object) -> str:
    text = str(value)
    if len(text) <= MAX_REDACTED_TEXT_LENGTH:
        return text
    # Fail closed instead of cutting through a credential or URL at the
    # boundary and exposing a meaningful prefix.
    return f"<redacted: oversized input>{_TRUNCATED}"


def _escape_controls(text: str) -> str:
    parts: list[str] = []
    for character in text:
        code = ord(character)
        if code < 32 or 127 <= code <= 159:
            names = {9: "\\t", 10: "\\n", 13: "\\r"}
            parts.append(names.get(code, f"\\x{code:02x}"))
        else:
            parts.append(character)
    return "".join(parts)


def _remove_controls(text: str) -> str:
    """Remove every C0/C1 control before credential pattern matching."""

    return "".join(
        character
        for character in text
        if not (ord(character) < 32 or 127 <= ord(character) <= 159)
    )


def redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"}:
            return "<redacted>"
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError, UnicodeError):
        return "<redacted>"
    if not hostname:
        return "<redacted>"

    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        authority = f"{authority}:{port}"
    path = parsed.path if parsed.path in {"", "/"} else "/<redacted>"
    query = "<redacted>" if parsed.query else ""
    # Fragments are local-only metadata; dropping them is the safest and keeps
    # redaction output stable across callers.
    return urlunsplit((parsed.scheme.lower(), authority, path, query, ""))


def _replace_assignment(match: re.Match[str]) -> str:
    prefix = match.group("prefix")
    quote = match.group("quote") or ""
    return f"{prefix}{quote}<redacted>{quote}"


def redact_text(value: object) -> str:
    text = _remove_controls(_bounded_input(value))
    text = URL_PATTERN.sub(lambda match: redact_url(match.group(0)), text)
    text = BEARER_PATTERN.sub(r"\1<redacted>", text)
    text = ASSIGNMENT_PATTERN.sub(_replace_assignment, text)
    text = SK_PATTERN.sub("<redacted>", text)
    text = _escape_controls(text)
    return text[:MAX_REDACTED_TEXT_LENGTH]


def redact_log_text(value: object) -> str:
    """Return one bounded line suitable for files, terminals and SSE."""

    return redact_text(value)
