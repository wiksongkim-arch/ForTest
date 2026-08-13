"""Anthropic 与 Gemini 的配置级结构化步骤客户端。"""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import quote

import requests

from backend.ai.base import ProviderResponseError, ProviderUnavailableError
from backend.ai.types import StageEvidence
from backend.settings.models import AIConfiguration, ProviderName


_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _validate_schema(value: Any, schema: dict[str, Any]) -> None:
    kind = schema.get("type")
    if kind == "object":
        if type(value) is not dict:
            raise ValueError("expected object")
        properties = schema.get("properties") or {}
        if any(name not in value for name in schema.get("required") or []):
            raise ValueError("missing field")
        if schema.get("additionalProperties") is False:
            if any(name not in properties for name in value):
                raise ValueError("unexpected field")
        for name, nested in value.items():
            if name in properties:
                _validate_schema(nested, properties[name])
        return
    if kind == "array":
        if type(value) is not list:
            raise ValueError("expected array")
        item_schema = schema.get("items")
        if item_schema:
            for item in value:
                _validate_schema(item, item_schema)
        return
    if kind == "string" and type(value) is str:
        return
    raise ValueError("invalid schema value")


def _image_data(path: Path) -> tuple[str, str]:
    if not isinstance(path, Path):
        raise ValueError("图片必须是本地 Path")
    mime = _IMAGE_MIME_TYPES.get(path.suffix.lower())
    if mime is None:
        raise ValueError("不支持的图片格式")
    content = path.read_bytes()
    if not content or len(content) > _MAX_IMAGE_BYTES:
        raise ValueError("图片大小异常")
    return mime, base64.b64encode(content).decode("ascii")


class _StructuredHTTPClient:
    runtime_mode = "http"

    def __init__(
        self,
        configuration: AIConfiguration,
        api_key: str | None,
        *,
        session: Any | None = None,
    ) -> None:
        self.configuration = configuration
        self.name = ProviderName(configuration.provider.value)
        self.api_key = api_key.strip() if isinstance(api_key, str) else ""
        self.session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self._closed = False
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        if self._owns_session:
            try:
                self.session.close()
            except Exception:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_session:
            try:
                self.session.close()
            except Exception:
                pass

    def run_structured_stage(
        self,
        stage: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        *,
        images: tuple[Path, ...] = (),
    ) -> tuple[dict[str, Any], StageEvidence]:
        if self._closed or self._cancelled:
            raise ProviderUnavailableError("AI 配置客户端已关闭")
        if not self.api_key:
            raise ProviderUnavailableError("API Key 未配置")
        if images and not self.configuration.vision_enabled:
            raise ProviderUnavailableError("当前配置不支持图片理解")
        started = monotonic()
        prepared_user = user_prompt
        for retry_count in range(2):
            payload = self._payload(
                stage,
                system_prompt,
                prepared_user,
                schema,
                images,
            )
            try:
                response = self.session.post(
                    self._endpoint(),
                    json=payload,
                    headers=self._headers(),
                    timeout=self.configuration.timeout_seconds,
                    verify=True,
                    allow_redirects=False,
                )
                # 不跟随携带密钥的重定向，同时兼容不提供状态码的测试替身。
                status = getattr(response, "status_code", None)
                if isinstance(status, int) and 300 <= status <= 399:
                    raise ProviderUnavailableError("凭据请求禁止跨域重定向")
                response.raise_for_status()
                data = self._extract(response.json())
                _validate_schema(data, schema)
                return data, StageEvidence(
                    stage=stage,
                    provider=self.name,
                    runtime_mode=self.runtime_mode,
                    model=self.configuration.model,
                    duration_ms=max(0, int((monotonic() - started) * 1000)),
                    retry_count=retry_count,
                    output_valid=True,
                )
            except ProviderUnavailableError:
                raise
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = (
                    isinstance(exc, (ValueError, requests.Timeout, requests.ConnectionError))
                    or type(status) is int
                    and (status in {408, 429} or 500 <= status <= 599)
                )
                if retry_count == 0 and retryable:
                    prepared_user = (
                        f"{user_prompt}\n\nCorrection: return exactly one JSON object "
                        "that satisfies the supplied schema, with no Markdown or prose."
                    )
                    continue
                raise ProviderResponseError(
                    f"{self.name.value} {stage} 请求失败（{type(exc).__name__}）"
                ) from None
        raise AssertionError("unreachable")

    def _endpoint(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _payload(
        self,
        stage: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        images: tuple[Path, ...],
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _extract(self, payload: Any) -> dict[str, Any]:
        raise NotImplementedError


class AnthropicStructuredClient(_StructuredHTTPClient):
    def _endpoint(self) -> str:
        return f"{self.configuration.base_url.rstrip('/')}/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

    def _payload(
        self,
        stage: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        images: tuple[Path, ...],
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for image in images:
            mime, data = _image_data(image)
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": data,
                    },
                }
            )
        content.append({"type": "text", "text": user_prompt})
        return {
            "model": self.configuration.model,
            "max_tokens": 8192,
            "system": system_prompt,
            "messages": [{"role": "user", "content": content}],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "name": f"fortest_{stage}",
                    "schema": deepcopy(schema),
                }
            },
        }

    def _extract(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("invalid Anthropic response")
        content = payload.get("content")
        if not isinstance(content, list):
            raise ValueError("missing Anthropic content")
        text = "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("Anthropic output is not an object")
        return value


class GeminiStructuredClient(_StructuredHTTPClient):
    def _endpoint(self) -> str:
        model = quote(self.configuration.model, safe="-._")
        return (
            f"{self.configuration.base_url.rstrip('/')}/models/"
            f"{model}:generateContent"
        )

    def _headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "x-goog-api-key": self.api_key,
        }

    def _payload(
        self,
        stage: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        images: tuple[Path, ...],
    ) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [{"text": user_prompt}]
        for image in images:
            mime, data = _image_data(image)
            parts.append(
                {
                    "inline_data": {
                        "mime_type": mime,
                        "data": data,
                    }
                }
            )
        return {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": deepcopy(schema),
            },
        }

    def _extract(self, payload: Any) -> dict[str, Any]:
        try:
            parts = payload["candidates"][0]["content"]["parts"]
            text = "".join(
                str(item.get("text") or "")
                for item in parts
                if isinstance(item, dict)
            ).strip()
            value = json.loads(text)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("invalid Gemini response") from None
        if not isinstance(value, dict):
            raise ValueError("Gemini output is not an object")
        return value
