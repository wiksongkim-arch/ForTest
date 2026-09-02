from __future__ import annotations

import base64
from copy import deepcopy
import json
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
import httpx

from backend.ai.base import ProviderResponseError, ProviderUnavailableError
from backend.ai.types import (
    CASE_OUTPUT_SCHEMA,
    COMPONENT_OUTPUT_SCHEMA,
    IMAGE_OUTPUT_SCHEMA,
    ProviderHealth,
    SectionAIRequest,
    SectionAIResult,
    StageEvidence,
)
from backend.settings.models import (
    OpenAICompatiblePreset,
    OpenAICompatibleSettings,
    ProviderName,
    ResponseFormatMode,
)
from backend.settings.prompts import PromptCatalog


_PROMPT_NAMES = (
    "image_understanding",
    "component_matching",
    "case_generation_system",
    "case_generation_user",
)
_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class _StructuredOutputError(ValueError):
    """The service response could not satisfy the local output schema."""


def _validate_against_schema(value: Any, schema: dict[str, Any]) -> None:
    expected_type = schema.get("type")
    if expected_type == "object":
        if type(value) is not dict:
            raise ValueError("expected object")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if any(name not in value for name in required):
            raise ValueError("missing required property")
        if schema.get("additionalProperties") is False:
            if any(name not in properties for name in value):
                raise ValueError("unexpected property")
        for name, nested in value.items():
            property_schema = properties.get(name)
            if property_schema is not None:
                _validate_against_schema(nested, property_schema)
        return
    if expected_type == "array":
        if type(value) is not list:
            raise ValueError("expected array")
        item_schema = schema.get("items")
        if item_schema is not None:
            for item in value:
                _validate_against_schema(item, item_schema)
        return
    if expected_type == "string":
        if type(value) is not str:
            raise ValueError("expected string")
        return
    raise ValueError("unsupported schema")


def _schema_example(schema: dict[str, Any]) -> Any:
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties", {})
        return {
            name: _schema_example(properties[name])
            for name in schema.get("required", [])
        }
    if schema_type == "array":
        item_schema = schema.get("items", {})
        if item_schema.get("type") == "object":
            return [_schema_example(item_schema)]
        return []
    if schema_type == "string":
        return "example"
    raise ValueError("unsupported schema")


def _is_retryable_stage_error(error: Exception) -> bool:
    if isinstance(error, _StructuredOutputError):
        return True
    if isinstance(error, requests.HTTPError):
        status_code = getattr(error.response, "status_code", None)
        return type(status_code) is int and (
            status_code in (408, 429) or 500 <= status_code <= 599
        )
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        return status_code in (408, 429) or 500 <= status_code <= 599
    return isinstance(
        error,
        (
            requests.Timeout,
            requests.ConnectionError,
            httpx.TimeoutException,
            httpx.NetworkError,
            TimeoutError,
            ConnectionError,
        ),
    )


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


class OpenAICompatibleProvider:
    name = ProviderName.openai_compatible
    runtime_mode = "http"

    def __init__(
        self,
        settings: OpenAICompatibleSettings,
        api_key: str | None,
        *,
        session: Any | None = None,
        request_options: dict[str, object] | None = None,
    ):
        normalized_base_url = _normalize_https_base_url(settings.base_url)
        self._base_url = normalized_base_url or ""
        self._base_url_valid = normalized_base_url is not None
        self._model = settings.model
        self._timeout_seconds = settings.timeout_seconds
        self._vision_enabled = settings.vision_enabled
        self._preset = settings.preset
        self._response_format_mode = settings.response_format_mode
        # 仅接收能力目录生成的白名单参数，调用方不能覆盖模型、消息或响应格式。
        self._request_options = {
            key: deepcopy(value)
            for key, value in dict(request_options or {}).items()
            if key
            in {
                "enable_thinking",
                "reasoning_effort",
                "service_tier",
                "thinking",
            }
        }
        self._api_key = (
            api_key
            if isinstance(api_key, str) and api_key.strip()
            else None
        )
        # httpx 的自有连接池可从另一线程关闭，停止任务时能中断在途请求。
        self._session = (
            session
            if session is not None
            else httpx.Client(verify=True, follow_redirects=False)
        )
        self._owns_session = session is None
        self._closed = False
        self._cancelled = False

    def health_check(self) -> ProviderHealth:
        if self._closed:
            return ProviderHealth(
                ok=False,
                provider=self.name,
                detail="Provider is closed.",
                runtime_mode=self.runtime_mode,
            )
        if not self._api_key:
            return ProviderHealth(
                ok=False,
                provider=self.name,
                detail="API credential is not configured.",
                runtime_mode=self.runtime_mode,
            )
        if not self._base_url_valid:
            return ProviderHealth(
                ok=False,
                provider=self.name,
                detail="Provider base URL is invalid.",
                runtime_mode=self.runtime_mode,
            )
        if self._has_unsupported_capability_selection():
            return ProviderHealth(
                ok=False,
                provider=self.name,
                detail="Provider response format is not supported.",
                runtime_mode=self.runtime_mode,
            )
        try:
            response = self._post(
                self._payload(
                    "component_matching",
                    [
                        {
                            "role": "system",
                            "content": (
                                "Return a minimal structured JSON health "
                                "response."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                "Return an empty matched_components array."
                            ),
                        },
                    ],
                    COMPONENT_OUTPUT_SCHEMA,
                )
            )
            self._extract_data(response, COMPONENT_OUTPUT_SCHEMA)
        except Exception:
            return ProviderHealth(
                ok=False,
                provider=self.name,
                detail="Provider health probe failed.",
                runtime_mode=self.runtime_mode,
            )
        return ProviderHealth(
            ok=True,
            provider=self.name,
            detail="Provider health probe succeeded.",
            runtime_mode=self.runtime_mode,
        )

    def process_section(self, request: SectionAIRequest) -> SectionAIResult:
        if self._closed or self._cancelled:
            raise ProviderUnavailableError(
                f"{self.name.value} provider is closed."
            )
        if not self._api_key:
            raise ProviderUnavailableError(
                f"{self.name.value} API credential is not configured."
            )
        if not self._base_url_valid:
            raise ProviderUnavailableError(
                f"{self.name.value} base URL is invalid."
            )
        if self._has_unsupported_capability_selection():
            raise ProviderUnavailableError(
                f"{self.name.value} response format is not supported."
            )
        prompts = self._validated_prompts(request)
        image_prompt = self._render_prompt(
            "image_understanding",
            prompts["image_understanding"],
            section_title=request.section_title,
            image_count=len(request.images),
        )
        image_findings: list[str] = []
        evidence: list[StageEvidence] = []
        skip_detail = self._vision_skip_detail(request)
        if skip_detail is not None:
            evidence.append(self._skipped_image_evidence(skip_detail))
        else:
            try:
                image_messages = self._image_messages(
                    image_prompt,
                    request.images,
                )
            except Exception:
                raise ProviderResponseError(
                    f"{self.name.value} image preparation failed."
                ) from None
            image_data, image_evidence = self._run_messages(
                "image_analysis",
                image_messages,
                IMAGE_OUTPUT_SCHEMA,
            )
            image_findings = image_data["image_findings"]
            evidence.append(image_evidence)

        requirement = (
            f"{request.section_title}\n\n{request.section_content}\n\n"
            f"{json.dumps(image_findings, ensure_ascii=False)}"
        )
        component_prompt = self._render_prompt(
            "component_matching",
            prompts["component_matching"],
            requirement=requirement,
            component_names=json.dumps(
                list(request.component_names), ensure_ascii=False
            ),
        )
        component_data, component_evidence = self._run_stage(
            "component_matching",
            "You must return structured JSON.",
            component_prompt,
            COMPONENT_OUTPUT_SCHEMA,
        )
        evidence.append(component_evidence)
        allowed = set(request.component_names)
        matched_components = []
        for name in component_data.get("matched_components", []):
            if name in allowed and name not in matched_components:
                matched_components.append(name)

        matched_templates = {
            name: request.component_templates[name]
            for name in matched_components
            if name in request.component_templates
        }
        system_prompt = self._render_prompt(
            "case_generation_system",
            prompts["case_generation_system"],
            field_specs=json.dumps(request.field_specs, ensure_ascii=False),
        )
        user_prompt = self._render_prompt(
            "case_generation_user",
            prompts["case_generation_user"],
            section_title=request.section_title,
            section_content=request.section_content,
            image_findings=json.dumps(image_findings, ensure_ascii=False),
            matched_components=json.dumps(
                matched_components, ensure_ascii=False
            ),
            matched_templates=json.dumps(
                matched_templates, ensure_ascii=False
            ),
        )
        case_data, case_evidence = self._run_stage(
            "case_generation",
            system_prompt,
            user_prompt,
            CASE_OUTPUT_SCHEMA,
        )
        evidence.append(case_evidence)
        duration_ms = sum(item.duration_ms for item in evidence)
        return SectionAIResult(
            provider=self.name,
            runtime_mode=self.runtime_mode,
            model=self._model,
            duration_ms=duration_ms,
            retry_count=sum(item.retry_count for item in evidence),
            output_valid=all(item.output_valid for item in evidence),
            image_findings=image_findings,
            matched_components=matched_components,
            test_cases=case_data.get("test_cases", []),
            evidence=evidence,
        )

    def run_structured_stage(
        self,
        stage: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        *,
        images: tuple[Path, ...] = (),
    ) -> tuple[dict[str, Any], StageEvidence]:
        """供能力路由器执行一个真实步骤，避免重复运行整段流水线。"""

        if self._closed or self._cancelled:
            raise ProviderUnavailableError(
                f"{self.name.value} provider is closed."
            )
        if not self._api_key:
            raise ProviderUnavailableError(
                f"{self.name.value} API credential is not configured."
            )
        if not self._base_url_valid:
            raise ProviderUnavailableError(
                f"{self.name.value} base URL is invalid."
            )
        if images:
            if not self._vision_enabled:
                raise ProviderUnavailableError(
                    f"{self.name.value} vision is disabled."
                )
            messages = self._image_messages(user_prompt, images)
            if system_prompt:
                messages.insert(0, {"role": "system", "content": system_prompt})
            return self._run_messages(stage, messages, schema)
        return self._run_stage(stage, system_prompt, user_prompt, schema)

    def cancel(self) -> None:
        """停止后禁止后续请求，并关闭自有会话以促使当前网络调用尽快返回。"""

        self._cancelled = True
        if self._owns_session:
            try:
                self._session.close()
            except Exception:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_session:
            try:
                self._session.close()
            except Exception:
                pass

    def _run_stage(
        self,
        stage: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> tuple[dict[str, Any], StageEvidence]:
        return self._run_messages(
            stage,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            schema,
        )

    def _run_messages(
        self,
        stage: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
    ) -> tuple[dict[str, Any], StageEvidence]:
        started = monotonic()
        payload = self._payload(stage, messages, schema)
        for retry_count in range(2):
            try:
                response = self._post(payload)
                data = self._extract_data(response, schema)
                return data, StageEvidence(
                    stage=stage,
                    provider=self.name,
                    runtime_mode=self.runtime_mode,
                    model=self._model,
                    duration_ms=max(
                        0, int((monotonic() - started) * 1000)
                    ),
                    retry_count=retry_count,
                    output_valid=True,
                    detail=self._stage_detail(),
                )
            except Exception as exc:
                if (
                    retry_count == 1
                    or not _is_retryable_stage_error(exc)
                ):
                    raise ProviderResponseError(
                        f"{self.name.value} {stage} request failed."
                    ) from None
                if isinstance(exc, _StructuredOutputError):
                    payload = self._structured_retry_payload(payload)
        raise AssertionError("unreachable")

    @staticmethod
    def _structured_retry_payload(payload: dict[str, Any]) -> dict[str, Any]:
        retry_payload = deepcopy(payload)
        instruction = (
            "Correction: Return exactly one single bare JSON object as the "
            "entire response. Do not include reasoning, explanations, prose, "
            "Markdown, code fences, or any text before or after the JSON. "
            "The object must strictly satisfy the requested JSON Schema: "
            "include every required field, include no additional fields, "
            "and preserve every declared type. Every field declared as "
            "string must contain a JSON string."
        )
        messages = retry_payload.get("messages")
        if not isinstance(messages, list):
            raise ProviderResponseError(
                "Provider structured retry could not be prepared."
            )
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = f"{content}\n\n{instruction}"
                return retry_payload
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        part["text"] = (
                            f"{part.get('text', '')}\n\n{instruction}"
                        )
                        return retry_payload
            break
        raise ProviderResponseError(
            "Provider structured retry could not be prepared."
        )

    def _endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _post(self, payload: dict[str, Any]):
        if self._closed or self._cancelled:
            raise ProviderUnavailableError(
                f"{self.name.value} provider is closed."
            )
        if not self._base_url_valid:
            raise ProviderUnavailableError(
                f"{self.name.value} base URL is invalid."
            )
        if not self._api_key:
            raise ProviderUnavailableError(
                f"{self.name.value} API credential is not configured."
            )
        arguments = {
            "json": payload,
            "headers": {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            "timeout": self._timeout_seconds,
        }
        response = self._session.post(
            self._endpoint(),
            **arguments,
            **({} if self._owns_session else {"verify": True, "allow_redirects": False}),
        )
        # requests 的真实响应状态码始终为整数；这里保留类型判断，兼容测试替身。
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and 300 <= status_code <= 399:
            raise ProviderUnavailableError(
                "Provider refused a credential-bearing redirect."
            )
        if self._cancelled:
            raise ProviderUnavailableError(
                f"{self.name.value} provider is closed."
            )
        response.raise_for_status()
        return response

    @staticmethod
    def _extract_data(
        response: Any,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            content = response.json()["choices"][0]["message"]["content"]
            if type(content) is not str or not content.strip():
                raise _StructuredOutputError("structured response is empty")
            data = json.loads(content)
            _validate_against_schema(data, schema)
            return data
        except _StructuredOutputError:
            raise
        except (KeyError, IndexError, TypeError, ValueError):
            raise _StructuredOutputError(
                "structured response is invalid"
            ) from None

    def _stage_detail(self) -> str:
        return ""

    def _has_unsupported_capability_selection(self) -> bool:
        return (
            self._preset == OpenAICompatiblePreset.deepseek
            and self._response_format_mode != ResponseFormatMode.json_object
        )

    def _validated_prompts(
        self,
        request: SectionAIRequest,
    ) -> dict[str, str]:
        try:
            prompts = {name: request.prompts[name] for name in _PROMPT_NAMES}
            for name, template in prompts.items():
                PromptCatalog.validate(name, template)
            placeholder_values = {
                "image_understanding": {
                    "section_title": request.section_title,
                    "image_count": len(request.images),
                },
                "component_matching": {
                    "requirement": (
                        f"{request.section_title}\n\n"
                        f"{request.section_content}\n\n[]"
                    ),
                    "component_names": json.dumps(
                        list(request.component_names),
                        ensure_ascii=False,
                    ),
                },
                "case_generation_system": {
                    "field_specs": json.dumps(
                        request.field_specs,
                        ensure_ascii=False,
                    ),
                },
                "case_generation_user": {
                    "section_title": request.section_title,
                    "section_content": request.section_content,
                    "image_findings": "[]",
                    "matched_components": "[]",
                    "matched_templates": "{}",
                },
            }
            for name, template in prompts.items():
                PromptCatalog.render(
                    name,
                    template,
                    **placeholder_values[name],
                )
            return prompts
        except Exception:
            raise ProviderResponseError(
                f"{self.name.value} prompt configuration is invalid."
            ) from None

    def _render_prompt(
        self,
        name: str,
        template: str,
        **values: object,
    ) -> str:
        try:
            return PromptCatalog.render(name, template, **values)
        except Exception:
            raise ProviderResponseError(
                f"{self.name.value} prompt configuration is invalid."
            ) from None

    def _vision_skip_detail(
        self,
        request: SectionAIRequest,
    ) -> str | None:
        if self._preset == OpenAICompatiblePreset.deepseek:
            return "skipped: provider preset is text-only"
        if not self._vision_enabled:
            return "skipped: vision disabled"
        if not request.images:
            return "skipped: no images"
        return None

    def _skipped_image_evidence(self, detail: str) -> StageEvidence:
        return StageEvidence(
            stage="image_analysis",
            provider=self.name,
            runtime_mode=self.runtime_mode,
            model=self._model,
            duration_ms=0,
            retry_count=0,
            output_valid=True,
            detail=detail,
        )

    @staticmethod
    def _image_messages(
        prompt: str,
        images: tuple[Path, ...],
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [
            {"type": "text", "text": prompt}
        ]
        for image in images:
            if not isinstance(image, Path):
                raise TypeError("image input must be a local Path")
            mime_type = _IMAGE_MIME_TYPES.get(image.suffix.lower())
            if mime_type is None:
                raise ValueError("unsupported image type")
            encoded = base64.b64encode(image.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{encoded}"
                    },
                }
            )
        return [{"role": "user", "content": content}]

    @staticmethod
    def _messages_with_schema_instruction(
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
    ) -> list[dict[str, Any]]:
        prepared = deepcopy(messages)
        instruction = (
            "Return JSON matching this schema: "
            + json.dumps(
                schema,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        example = _schema_example(schema)
        _validate_against_schema(example, schema)
        instruction += (
            "\nExample JSON output: "
            + json.dumps(
                example,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        for message in reversed(prepared):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = f"{content}\n\n{instruction}"
                return prepared
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        part["text"] = f"{part.get('text', '')}\n\n{instruction}"
                        return prepared
            break
        raise ValueError("a user text prompt is required")

    def _payload(
        self,
        stage: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        if self._response_format_mode == ResponseFormatMode.json_object:
            response_format: dict[str, Any] = {"type": "json_object"}
            messages = self._messages_with_schema_instruction(
                messages,
                schema,
            )
        else:
            messages = deepcopy(messages)
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": (
                        "prd_test_cases"
                        if stage == "case_generation"
                        else f"prd_{stage}"
                    ),
                    "strict": True,
                    "schema": schema,
                },
            }
        payload = {
            "model": self._model,
            "messages": messages,
            "response_format": response_format,
        }
        payload.update(deepcopy(self._request_options))
        return payload
