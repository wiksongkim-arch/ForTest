"""按业务步骤选择并回退 AI 配置的能力路由 Provider。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from backend.ai.base import ProviderResponseError, ProviderUnavailableError
from backend.ai.codex_provider import CodexProvider
from backend.ai.openai_compatible_provider import OpenAICompatibleProvider
from backend.ai.provider_specs import openai_compatible_request_options
from backend.ai.structured_clients import (
    AnthropicStructuredClient,
    GeminiStructuredClient,
)
from backend.ai.types import (
    CASE_OUTPUT_SCHEMA,
    COMPONENT_OUTPUT_SCHEMA,
    IMAGE_OUTPUT_SCHEMA,
    ProviderHealth,
    SectionAIRequest,
    SectionAIResult,
    StageEvidence,
)
from backend.security.redaction import redact_text
from backend.settings.models import (
    AIConfiguration,
    AIConfigurationProvider,
    AIProtocol,
    CodexCLISource,
    CodexRuntime,
    CodexSettings,
    ModelSelectionMode,
    OpenAICompatiblePreset,
    OpenAICompatibleSettings,
    ProviderName,
    SettingsSnapshot,
)
from backend.settings.prompts import PromptCatalog


CodexPathResolver = Callable[[str], Path | None]
ClientFactory = Callable[[AIConfiguration, str | None], Any]


class CapabilityRouterProvider:
    """一个任务内按三条策略路由，可由不同厂商共同完成一个区块。"""

    name = ProviderName.mixed
    runtime_mode = "capability-router"

    def __init__(
        self,
        snapshot: SettingsSnapshot,
        *,
        codex_path_resolver: CodexPathResolver | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.codex_path_resolver = codex_path_resolver
        self.client_factory = client_factory
        self._clients: dict[str, Any] = {}
        self._lock = RLock()
        self._closed = False
        self._cancelled = False

    def health_check(self) -> ProviderHealth:
        usable = any(
            self._configuration_usable(item, require_vision=False)
            for item in self.snapshot.settings.ai.configurations
            if item.deleted_at is None
        )
        return ProviderHealth(
            ok=usable,
            provider=self.name,
            detail=(
                "AI capability configurations are ready."
                if usable
                else "No complete AI capability configuration is available."
            ),
            runtime_mode=self.runtime_mode,
        )

    def process_section(self, request: SectionAIRequest) -> SectionAIResult:
        if self._closed or self._cancelled:
            raise ProviderUnavailableError("AI 能力路由器已关闭")
        prompts = self._validated_prompts(request)
        evidence: list[StageEvidence] = []
        image_findings: list[str] = []

        if request.images:
            image_prompt = self._render(
                "image_understanding",
                prompts["image_understanding"],
                section_title=request.section_title,
                image_count=len(request.images),
            )
            image_data, image_evidence = self._run_with_fallback(
                policy_stage="image_understanding",
                evidence_stage="image_analysis",
                system_prompt="只返回符合指定结构的 JSON。",
                user_prompt=image_prompt,
                schema=IMAGE_OUTPUT_SCHEMA,
                images=request.images,
            )
            image_findings = list(image_data.get("image_findings") or [])
            evidence.append(image_evidence)

        requirement = (
            f"{request.section_title}\n\n{request.section_content}\n\n"
            f"{json.dumps(image_findings, ensure_ascii=False)}"
        )
        component_prompt = self._render(
            "component_matching",
            prompts["component_matching"],
            requirement=requirement,
            component_names=json.dumps(
                list(request.component_names), ensure_ascii=False
            ),
        )
        component_data, component_evidence = self._run_with_fallback(
            policy_stage="component_matching",
            evidence_stage="component_matching",
            system_prompt="只返回符合指定结构的 JSON。",
            user_prompt=component_prompt,
            schema=COMPONENT_OUTPUT_SCHEMA,
        )
        evidence.append(component_evidence)
        allowed = set(request.component_names)
        matched_components: list[str] = []
        for name in component_data.get("matched_components") or []:
            if name in allowed and name not in matched_components:
                matched_components.append(name)

        matched_templates = {
            name: request.component_templates[name]
            for name in matched_components
            if name in request.component_templates
        }
        system_prompt = self._render(
            "case_generation_system",
            prompts["case_generation_system"],
            field_specs=json.dumps(request.field_specs, ensure_ascii=False),
        )
        user_prompt = self._render(
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
        minimum_cases = CodexProvider._minimum_case_count(
            request,
            matched_components,
        )
        user_prompt = (
            f"{user_prompt}\n\n"
            f"{CodexProvider._coverage_instruction(minimum_cases)}"
        )
        case_data, case_evidence = self._run_with_fallback(
            policy_stage="case_generation",
            evidence_stage="case_generation",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=CASE_OUTPUT_SCHEMA,
        )
        evidence.append(case_evidence)

        models = list(dict.fromkeys(item.model for item in evidence))
        return SectionAIResult(
            provider=self.name,
            runtime_mode=self.runtime_mode,
            model=" → ".join(models) or "configured-order",
            duration_ms=sum(item.duration_ms for item in evidence),
            retry_count=sum(item.retry_count for item in evidence),
            output_valid=all(item.output_valid for item in evidence),
            image_findings=image_findings,
            matched_components=matched_components,
            test_cases=list(case_data.get("test_cases") or []),
            evidence=evidence,
        )

    @staticmethod
    def _validated_prompts(request: SectionAIRequest) -> dict[str, str]:
        names = (
            "image_understanding",
            "component_matching",
            "case_generation_system",
            "case_generation_user",
        )
        try:
            prompts = {name: str(request.prompts[name]) for name in names}
            for name, value in prompts.items():
                PromptCatalog.validate(name, value)
            return prompts
        except Exception:
            raise ProviderResponseError("提示词配置无效") from None

    @staticmethod
    def _render(name: str, template: str, **values: object) -> str:
        try:
            return PromptCatalog.render(name, template, **values)
        except Exception:
            raise ProviderResponseError("提示词配置无效") from None

    def _ordered_configurations(self, stage: str) -> list[AIConfiguration]:
        settings = self.snapshot.settings.ai
        policy = getattr(settings.test_case_policies, stage)
        active = [
            item for item in settings.configurations if item.deleted_at is None
        ]
        if policy.mode == ModelSelectionMode.ordered:
            ordered = active
        else:
            by_id = {item.id: item for item in active}
            ordered = [
                by_id[item]
                for item in policy.configuration_ids
                if item in by_id
            ]
        return [
            item
            for item in ordered
            if self._configuration_usable(
                item,
                require_vision=stage == "image_understanding",
            )
        ]

    def _configuration_usable(
        self,
        configuration: AIConfiguration,
        *,
        require_vision: bool,
    ) -> bool:
        if require_vision and not configuration.vision_enabled:
            return False
        key = self.snapshot.secrets.reveal_ai_configuration(configuration.id)
        if configuration.requires_api_key() and not key:
            return False
        if configuration.provider == AIConfigurationProvider.codex:
            if configuration.codex_cli_source == CodexCLISource.custom:
                return bool(
                    configuration.codex_cli_path
                    and Path(configuration.codex_cli_path).expanduser().is_file()
                )
            if self.codex_path_resolver is not None:
                try:
                    return self.codex_path_resolver(
                        configuration.codex_cli_version
                    ) is not None
                except Exception:
                    return False
            return True
        return bool(configuration.base_url)

    def _run_with_fallback(
        self,
        *,
        policy_stage: str,
        evidence_stage: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        images: tuple[Path, ...] = (),
    ) -> tuple[dict[str, Any], StageEvidence]:
        configurations = self._ordered_configurations(policy_stage)
        if not configurations:
            raise ProviderUnavailableError(
                f"步骤 {policy_stage} 没有可用的 AI 配置"
            )
        failures: list[str] = []
        for index, configuration in enumerate(configurations):
            if self._cancelled:
                raise ProviderUnavailableError("AI 能力路由器已取消")
            client = self._client(configuration)
            try:
                data, evidence = client.run_structured_stage(
                    evidence_stage,
                    system_prompt,
                    user_prompt,
                    schema,
                    images=images,
                )
                detail_parts = [evidence.detail] if evidence.detail else []
                if index:
                    detail_parts.append(f"按配置顺序回退 {index} 次")
                return data, replace(
                    evidence,
                    detail="；".join(detail_parts),
                    configuration_id=configuration.id,
                    configuration_name=configuration.name,
                )
            except Exception as exc:
                failures.append(type(exc).__name__)
                self._discard_client(configuration.id)
        types = "、".join(dict.fromkeys(failures))
        raise ProviderUnavailableError(
            redact_text(
                f"步骤 {policy_stage} 的 {len(configurations)} 条 AI 配置均调用失败"
                + (f"（{types}）" if types else "")
            )
        )

    def _client(self, configuration: AIConfiguration):
        with self._lock:
            existing = self._clients.get(configuration.id)
            if existing is not None:
                return existing
            key = (
                self.snapshot.secrets.reveal_ai_configuration(configuration.id)
                if configuration.requires_api_key()
                else None
            )
            client = (
                self.client_factory(configuration, key)
                if self.client_factory is not None
                else self._create_client(configuration, key)
            )
            self._clients[configuration.id] = client
            return client

    def _create_client(
        self,
        configuration: AIConfiguration,
        api_key: str | None,
    ):
        if configuration.protocol == AIProtocol.codex:
            if configuration.codex_cli_source == CodexCLISource.custom:
                cli_path = configuration.codex_cli_path
            elif self.codex_path_resolver is not None:
                resolved = self.codex_path_resolver(
                    configuration.codex_cli_version
                )
                cli_path = str(resolved) if resolved is not None else None
            else:
                cli_path = None
            return CodexProvider(
                CodexSettings(
                    runtime=CodexRuntime.auto,
                    model=configuration.model,
                    reasoning_effort=configuration.reasoning_effort,
                    inference_speed=configuration.inference_speed,
                    timeout_seconds=configuration.timeout_seconds,
                    max_concurrency=configuration.max_concurrency,
                    cli_path=cli_path,
                    use_dedicated_api_key=False,
                ),
                api_key=None,
            )
        if configuration.protocol == AIProtocol.anthropic:
            return AnthropicStructuredClient(configuration, api_key)
        if configuration.protocol == AIProtocol.gemini:
            return GeminiStructuredClient(configuration, api_key)
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                preset=OpenAICompatiblePreset.custom,
                base_url=configuration.base_url,
                model=configuration.model,
                response_format_mode=configuration.response_format_mode,
                timeout_seconds=min(configuration.timeout_seconds, 900),
                vision_enabled=configuration.vision_enabled,
            ),
            api_key,
            request_options=openai_compatible_request_options(configuration),
        )
        provider.name = ProviderName(configuration.provider.value)
        return provider

    def _discard_client(self, configuration_id: str) -> None:
        with self._lock:
            client = self._clients.pop(configuration_id, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def cancel(self) -> None:
        self._cancelled = True
        with self._lock:
            clients = list(self._clients.values())
        for client in clients:
            try:
                client.cancel()
            except Exception:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                pass
