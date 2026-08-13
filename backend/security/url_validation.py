"""Credential-safe validation for DingTalk MCP endpoint URLs."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


MAX_MCP_URL_LENGTH = 4096
_ENCODED_CONTROL = re.compile(
    r"%(?:0[0-9a-f]|1[0-9a-f]|7f|8[0-9a-f]|9[0-9a-f])",
    re.IGNORECASE,
)


class MCPURLValidationError(ValueError):
    """An MCP URL failed validation; its message never contains the URL."""


def normalize_https_mcp_url(value: object) -> str:
    """Return a normalized HTTPS MCP URL without exposing it on failure.

    Credential-bearing query strings are supported. Userinfo, fragments,
    controls, backslashes and ambiguous/oversized URLs are rejected before a
    transport receives the value.
    """

    if not isinstance(value, str):
        raise MCPURLValidationError("MCP URL is invalid.")
    candidate = value
    if (
        not candidate
        or len(candidate) > MAX_MCP_URL_LENGTH
        or "\\" in candidate
        or any(
            character.isspace()
            or ord(character) < 32
            or 127 <= ord(character) <= 159
            for character in candidate
        )
        or _ENCODED_CONTROL.search(candidate)
    ):
        raise MCPURLValidationError("MCP URL is invalid.")
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError, UnicodeError):
        raise MCPURLValidationError("MCP URL is invalid.") from None
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port == 0
    ):
        raise MCPURLValidationError("MCP URL must be a safe HTTPS URL.")

    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        authority = f"{authority}:{port}"
    return urlunsplit(("https", authority, parsed.path, parsed.query, ""))
