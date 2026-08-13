"""Low-level compatibility wrapper for the legacy MiniMax chat endpoint."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def _normalize_https_base_url(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (
        not candidate
        or "?" in candidate
        or "#" in candidate
        or "\\" in candidate
        or any(ord(character) < 32 for character in candidate)
    ):
        return None
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme.lower() != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return None
    except (TypeError, ValueError, UnicodeError):
        return None
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        authority = f"{authority}:{port}"
    return urlunsplit(
        ("https", authority, parsed.path.rstrip("/"), "", "")
    )


class MiniMaxServiceError(RuntimeError):
    """A redacted MiniMax transport or response failure."""


class MiniMaxService:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int,
        session: Any,
    ):
        if not isinstance(api_key, str) or not api_key.strip():
            raise MiniMaxServiceError("MiniMax credential is not configured.")
        normalized_base_url = _normalize_https_base_url(base_url)
        if normalized_base_url is None:
            raise MiniMaxServiceError("MiniMax base URL is invalid.")
        if not isinstance(model, str) or not model.strip():
            raise MiniMaxServiceError("MiniMax model is invalid.")
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise MiniMaxServiceError("MiniMax timeout is invalid.")
        if session is None:
            raise MiniMaxServiceError("MiniMax HTTP session is required.")
        self._api_key = api_key
        self._base_url = normalized_base_url
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._session = session

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        if not isinstance(messages, list):
            raise MiniMaxServiceError("MiniMax messages are invalid.")
        try:
            payload: dict[str, Any] = {
                "model": self._model,
                "messages": deepcopy(messages),
            }
            if response_format is not None:
                payload["response_format"] = deepcopy(response_format)
            response = self._session.post(
                f"{self._base_url}/v1/text/chatcompletion_v2",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                timeout=self._timeout_seconds,
                verify=True,
                allow_redirects=False,
            )
            response.raise_for_status()
            return self._extract_content(response.json())
        except MiniMaxServiceError:
            raise
        except Exception:
            raise MiniMaxServiceError("MiniMax request failed.") from None

    @staticmethod
    def _extract_content(body: Any) -> str:
        if not isinstance(body, dict) or "error" in body:
            raise MiniMaxServiceError("MiniMax response is invalid.")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise MiniMaxServiceError("MiniMax response is invalid.")
        first = choices[0]
        if not isinstance(first, dict):
            raise MiniMaxServiceError("MiniMax response is invalid.")
        content: Any = None
        message = first.get("message")
        if isinstance(message, dict):
            content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            legacy_messages = first.get("messages")
            if isinstance(legacy_messages, list) and legacy_messages:
                legacy_message = legacy_messages[0]
                if isinstance(legacy_message, dict):
                    content = legacy_message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise MiniMaxServiceError("MiniMax response is invalid.")
        return content


def create_minimax_service(
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: int,
    session: Any,
) -> MiniMaxService:
    """Preserve the legacy import without restoring implicit configuration."""
    return MiniMaxService(
        api_key,
        base_url,
        model,
        timeout_seconds,
        session,
    )
