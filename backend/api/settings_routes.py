from __future__ import annotations

import os
import hashlib
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Mapping

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.ai.base import ProviderUnavailableError
from backend.ai.model_catalog import list_provider_models
from backend.ai.provider_specs import (
    PROVIDER_SPEC_BY_ID,
    provider_specs_view,
)
from backend.ai.registry import ProviderRegistry, build_provider_registry
from backend.ai.types import ProviderHealth
from backend.security.redaction import redact_text
from backend.security.url_validation import (
    MCPURLValidationError,
    normalize_https_mcp_url,
)
from backend.settings.defaults import (
    DEFAULT_PROMPTS,
    OPENAI_COMPATIBLE_PRESET_MODELS,
    OPENAI_COMPATIBLE_PRESET_RESPONSE_FORMATS,
    OPENAI_COMPATIBLE_PRESET_URLS,
    OPENAI_COMPATIBLE_PRESET_VISION,
    PROMPT_VARIABLES,
)
from backend.settings.models import (
    AIConfiguration,
    AIConfigurationProvider,
    CodexCLISource,
    CodexInferenceSpeed,
    AISettings,
    DocumentSettings,
    ModelSelectionMode,
    ModelSelectionPolicy,
    PromptSettings,
    ProviderName,
    ReasoningEffort,
    ResponseFormatMode,
)
from backend.settings.paths import settings_file_path
from backend.settings.prompts import PromptCatalog, PromptValidationError
from backend.settings.secrets import KeyringSecretStore
from backend.settings.service import SettingsService, SettingsValidationError
from backend.settings.store import SettingsRepository
from services.dingtalk_mcp import (
    DingTalkMCPError,
    DingTalkMCPService,
    extract_node_id,
)
from services.dingtalk_spreadsheet import DingTalkSpreadSheetMCPService
from utils.default_templates import DefaultTemplateManager


class DocumentUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    content_template_url: str | None = None
    document_template_url: str | None = None
    output_folder_url: str | None = None
    local_output_dir: str | None = None
    document_mcp_url: str | None = None
    spreadsheet_mcp_url: str | None = None
    clear_document_mcp_url: bool = False
    clear_spreadsheet_mcp_url: bool = False


class DocumentConnectionTest(DocumentUpdate):
    pass


class AIUpdate(AISettings):
    model_config = ConfigDict(frozen=True, extra="forbid")

    minimax_api_key: str | None = None
    openai_compatible_api_key: str | None = None
    codex_api_key: str | None = None
    clear_minimax_api_key: bool = False
    clear_openai_compatible_api_key: bool = False
    clear_codex_api_key: bool = False


class AIConnectionTest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ProviderName


class AIConfigurationUpsert(BaseModel):
    """设置中心新增/编辑表单；状态、时间和协议均由服务端维护。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str | None = None
    name: str
    provider: AIConfigurationProvider
    model: str
    base_url: str = ""
    timeout_seconds: int = Field(default=300, ge=30, le=3600)
    vision_enabled: bool = True
    response_format_mode: ResponseFormatMode = ResponseFormatMode.json_schema
    reasoning_effort: ReasoningEffort = ReasoningEffort.high
    inference_speed: CodexInferenceSpeed = CodexInferenceSpeed.standard
    max_concurrency: int = Field(default=1, ge=1, le=4)
    codex_cli_source: CodexCLISource = CodexCLISource.builtin
    codex_cli_version: str = "bundled"
    codex_cli_path: str | None = None
    use_dedicated_api_key: bool = False
    api_key: str | None = None
    clear_api_key: bool = False


class AIConfigurationOrder(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    configuration_ids: tuple[str, ...]


class TestCaseModelPolicyUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: ModelSelectionMode
    configuration_ids: tuple[str, ...] = ()


class PromptDraftValidation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str


class PromptOptionSave(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    option_id: str | None = None
    name: str | None = None
    content: str | None = None


@dataclass(frozen=True)
class RuntimeDependencies:
    service: SettingsService
    registry: ProviderRegistry
    document_factory: Callable[[str], DingTalkMCPService]
    spreadsheet_factory: Callable[[str], DingTalkSpreadSheetMCPService]
    default_template_paths: Mapping[str, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbePollPolicy:
    visible_timeout: float = 30.0
    late_cleanup_timeout: float = 120.0
    deletion_timeout: float = 30.0
    interval: float = 1.0


@dataclass
class _HealthFlight:
    completed: threading.Event
    health: ProviderHealth | None = None
    error: BaseException | None = None


class _ProbeAmbiguityError(DingTalkMCPError):
    def __init__(self, node_ids: set[str]):
        super().__init__("连接测试文件出现多个精确匹配节点")
        self.node_ids = frozenset(node_ids)


router = APIRouter(prefix="/api/settings", tags=["settings"])

_LAST_AI_CONNECTION: dict[tuple[int, ProviderName], dict] = {}
_LAST_AI_CONNECTION_LOCK = threading.Lock()
_AI_TEST_FLIGHTS: dict[
    tuple[int, ProviderName, str], _HealthFlight
] = {}
_AI_TEST_FLIGHTS_LOCK = threading.Lock()
_DOCUMENT_PROBE_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def get_settings_service() -> SettingsService:
    service = SettingsService(
        SettingsRepository(settings_file_path(os.environ)),
        KeyringSecretStore(),
        environment=os.environ,
    )
    service.scrub_bootstrap_environment(os.environ)
    return service


@lru_cache(maxsize=1)
def get_provider_registry() -> ProviderRegistry:
    return build_provider_registry()


def get_runtime_dependencies(
    service: SettingsService = Depends(get_settings_service),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> RuntimeDependencies:
    # Web 兼容入口也使用同一份用户模板副本，不让本地回退只存在于原生 UI。
    data_root = settings_file_path(os.environ).parent.parent
    template_paths = DefaultTemplateManager(data_root).ensure_all()
    return RuntimeDependencies(
        service=service,
        registry=registry,
        document_factory=DingTalkMCPService,
        spreadsheet_factory=DingTalkSpreadSheetMCPService,
        default_template_paths=template_paths,
    )


def clear_connection_diagnostic_cache() -> None:
    """Clear cached presentation state; primarily useful for isolated app runs."""
    with _LAST_AI_CONNECTION_LOCK:
        _LAST_AI_CONNECTION.clear()


def document_view(service: SettingsService) -> dict:
    return {
        **service.load().document.model_dump(mode="json"),
        "document_mcp": service.secret_status(
            "document_mcp_url"
        ).model_dump(mode="json"),
        "spreadsheet_mcp": service.secret_status(
            "spreadsheet_mcp_url"
        ).model_dump(mode="json"),
    }


def prompt_variable_view() -> dict:
    return {
        name: {
            kind: sorted(values)
            for kind, values in contract.items()
        }
        for name, contract in PROMPT_VARIABLES.items()
    }


def prompt_view(service: SettingsService) -> dict:
    settings = service.load()
    return {
        "values": settings.prompts.model_dump(mode="json"),
        "defaults": dict(DEFAULT_PROMPTS),
        "variables": prompt_variable_view(),
        "groups": {
            name: prompt_group_view(settings, name)
            for name in DEFAULT_PROMPTS
        },
    }


def prompt_group_view(settings, prompt_name: str) -> dict:
    if prompt_name not in DEFAULT_PROMPTS:
        raise HTTPException(status_code=404, detail="未知提示词类型")
    slot = getattr(settings.prompt_library, prompt_name)
    options = [
        {
            "id": "default",
            "name": "默认",
            "content": DEFAULT_PROMPTS[prompt_name],
            "is_default": True,
            "editable": False,
        }
    ]
    options.extend(
        {
            "id": option.id,
            "name": option.name,
            "content": option.content,
            "is_default": False,
            "editable": True,
        }
        for option in slot.custom_options
    )
    return {
        "selected_option_id": slot.selected_option_id,
        "options": options,
        "variables": prompt_variable_view()[prompt_name],
    }


def ai_view(service: SettingsService) -> dict:
    snapshot = service.snapshot()
    settings = snapshot.settings.ai
    fingerprint = _provider_fingerprint(
        service,
        snapshot,
        settings.active_provider,
    )
    with _LAST_AI_CONNECTION_LOCK:
        record = _LAST_AI_CONNECTION.get(
            (id(service), settings.active_provider)
        )
        last_connection = (
            dict(record["health"])
            if record is not None
            and record.get("fingerprint") == fingerprint
            else None
        )
    return {
        **settings.model_dump(mode="json"),
        "secret_status": {
            "codex": service.secret_status(
                "codex_api_key"
            ).model_dump(mode="json"),
            "minimax": service.secret_status(
                "minimax_api_key"
            ).model_dump(mode="json"),
            "openai_compatible": service.secret_status(
                "openai_compatible_api_key"
            ).model_dump(mode="json"),
        },
        "openai_compatible_presets": {
            name: {
                "base_url": OPENAI_COMPATIBLE_PRESET_URLS[name],
                "model": OPENAI_COMPATIBLE_PRESET_MODELS[name],
                "response_format_mode": (
                    OPENAI_COMPATIBLE_PRESET_RESPONSE_FORMATS[name]
                ),
                "vision_enabled": OPENAI_COMPATIBLE_PRESET_VISION[name],
            }
            for name in OPENAI_COMPATIBLE_PRESET_URLS
        },
        "last_connection": last_connection,
    }


def ai_configuration_view(
    service: SettingsService,
    configuration: AIConfiguration,
) -> dict:
    spec = PROVIDER_SPEC_BY_ID[configuration.provider]
    view = {
        **configuration.model_dump(mode="json"),
        "provider_label": spec.label,
        "display_model": f"{spec.label} · {configuration.model}",
        "complete": service.ai_configuration_is_complete(configuration),
    }
    if configuration.provider != AIConfigurationProvider.codex:
        view["secret_status"] = service.ai_configuration_secret_status(
            configuration.id
        ).model_dump(mode="json")
    return view


def ai_configurations_view(service: SettingsService) -> dict:
    views = [
        ai_configuration_view(service, item)
        for item in service.load().ai.configurations
    ]
    return {
        "providers": provider_specs_view(),
        "configurations": [
            item for item in views if item.get("deleted_at") is None
        ],
        "recycle_bin": [
            item for item in views if item.get("deleted_at") is not None
        ],
    }


def test_case_model_policies_view(service: SettingsService) -> dict:
    settings = service.load()
    complete = [
        ai_configuration_view(service, item)
        for item in settings.ai.configurations
        if item.deleted_at is None
        and service.ai_configuration_is_complete(item)
    ]
    return {
        "policies": settings.ai.test_case_policies.model_dump(mode="json"),
        "available": {
            "image_understanding": [
                item for item in complete if item.get("vision_enabled")
            ],
            "component_matching": complete,
            "case_generation": complete,
        },
    }


def apply_ai_configuration_upsert(
    service: SettingsService,
    payload: AIConfigurationUpsert,
    *,
    forced_id: str | None = None,
) -> None:
    data = payload.model_dump(
        exclude={"api_key", "clear_api_key", "use_dedicated_api_key"},
    )
    requested_id = forced_id or payload.id or str(uuid.uuid4())
    if forced_id is not None and payload.id not in (None, forced_id):
        raise SettingsValidationError("请求路径与 AI 配置 ID 不一致")
    data["id"] = requested_id
    configuration = AIConfiguration.model_validate(data)
    is_codex = configuration.provider == AIConfigurationProvider.codex
    service.save_ai_configuration(
        configuration,
        api_key=None if is_codex else payload.api_key,
        # 兼容旧请求中的 Codex 密钥字段，但保存时主动清除旧配置级密钥。
        clear_api_key=True if is_codex else payload.clear_api_key,
    )


def ai_model_catalog_view(
    service: SettingsService,
    provider: ProviderName,
) -> dict:
    if provider not in {
        ProviderName.codex,
        ProviderName.minimax,
        ProviderName.openai_compatible,
    }:
        raise HTTPException(
            status_code=404,
            detail="该 Provider 不支持动态模型列表。",
        )
    return list_provider_models(provider, service.snapshot())


def validate_prompt_group(payload: PromptSettings) -> dict:
    errors: dict[str, str] = {}
    for name, template in payload.model_dump().items():
        if not template.strip():
            errors[name] = "提示词内容不能为空"
            continue
        try:
            PromptCatalog.validate(name, template)
        except PromptValidationError as exc:
            errors[name] = redact_text(str(exc))
    if errors:
        raise HTTPException(status_code=422, detail={"fields": errors})
    return {"ok": True, "variables": prompt_variable_view()}


def validate_prompt_draft(prompt_name: str, content: str) -> dict:
    if prompt_name not in DEFAULT_PROMPTS:
        raise HTTPException(status_code=404, detail="未知提示词类型")
    if not content.strip():
        raise HTTPException(
            status_code=422,
            detail={"fields": {"content": "提示词内容不能为空"}},
        )
    try:
        PromptCatalog.validate(prompt_name, content)
    except PromptValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"fields": {"content": redact_text(str(exc))}},
        ) from None
    return {
        "ok": True,
        "variables": prompt_variable_view()[prompt_name],
    }


def _normalized_secret(
    value: str | None,
    *,
    clear: bool,
    require_https: bool = False,
) -> str | None:
    # Clear intentionally wins when both controls are submitted.
    if clear or value is None or not value.strip():
        return None
    candidate = value.strip()
    if "•" in candidate:
        raise SettingsValidationError("不能把掩码值保存为密钥")
    if require_https:
        try:
            candidate = normalize_https_mcp_url(value)
        except MCPURLValidationError:
            raise SettingsValidationError(
                "MCP URL 必须是安全的 HTTPS URL"
            ) from None
    return candidate


def _resolved_document_settings(
    saved: DocumentSettings,
    payload: DocumentUpdate,
) -> DocumentSettings:
    values = {}
    for name in DocumentSettings.model_fields:
        candidate = getattr(payload, name)
        values[name] = (
            candidate
            if candidate is not None and candidate.strip()
            else getattr(saved, name)
        )
    try:
        return DocumentSettings.model_validate(values)
    except ValidationError as exc:
        details = []
        for error in exc.errors(include_input=False, include_url=False):
            field = ".".join(str(part) for part in error.get("loc", ()))
            message = str(error.get("msg", "输入验证失败"))
            details.append(f"{field}：{message}" if field else message)
        raise SettingsValidationError(
            "；".join(details) or "文档配置验证失败"
        ) from None


def apply_document_update(
    service: SettingsService,
    payload: DocumentUpdate,
) -> None:
    document = _resolved_document_settings(
        service.load().document,
        payload,
    )
    clear_flags = {
        "document_mcp_url": payload.clear_document_mcp_url,
        "spreadsheet_mcp_url": payload.clear_spreadsheet_mcp_url,
    }
    candidates = {
        "document_mcp_url": payload.document_mcp_url,
        "spreadsheet_mcp_url": payload.spreadsheet_mcp_url,
    }
    updates = {
        name: normalized
        for name, value in candidates.items()
        if (
            normalized := _normalized_secret(
                value,
                clear=clear_flags[name],
                require_https=True,
            )
        )
        is not None
    }
    service.update_group(
        "document",
        document,
        secret_updates=updates,
        clear_secrets={
            name for name, clear in clear_flags.items() if clear
        },
    )


def apply_ai_update(service: SettingsService, payload: AIUpdate) -> None:
    data = payload.model_dump()
    ai = AISettings.model_validate(
        {key: data[key] for key in AISettings.model_fields}
    )
    clear_flags = {
        "codex_api_key": payload.clear_codex_api_key,
        "minimax_api_key": payload.clear_minimax_api_key,
        "openai_compatible_api_key": (
            payload.clear_openai_compatible_api_key
        ),
    }
    candidates = {
        "codex_api_key": payload.codex_api_key,
        "minimax_api_key": payload.minimax_api_key,
        "openai_compatible_api_key": payload.openai_compatible_api_key,
    }
    updates = {
        name: normalized
        for name, value in candidates.items()
        if (
            normalized := _normalized_secret(
                value,
                clear=clear_flags[name],
            )
        )
        is not None
    }

    def validate_final_state(prospective, resolved_secrets) -> None:
        prospective_ai = prospective.ai
        providers_to_validate = [prospective_ai.active_provider]
        if (
            prospective_ai.fallback_enabled
            and prospective_ai.fallback_provider is not None
        ):
            providers_to_validate.append(prospective_ai.fallback_provider)
        for provider in providers_to_validate:
            required_secret = {
                ProviderName.minimax: "minimax_api_key",
                ProviderName.openai_compatible: (
                    "openai_compatible_api_key"
                ),
            }.get(provider)
            if required_secret and not resolved_secrets[required_secret]:
                raise SettingsValidationError(
                    f"{provider.value} 缺少 API key"
                )
            if (
                provider == ProviderName.codex
                and prospective_ai.codex.use_dedicated_api_key
                and not resolved_secrets["codex_api_key"]
            ):
                raise SettingsValidationError("专用 Codex 模式缺少 API key")

    service.update_group(
        "ai",
        ai,
        secret_updates=updates,
        clear_secrets={
            name for name, clear in clear_flags.items() if clear
        },
        final_state_validator=validate_final_state,
    )
    clear_connection_diagnostic_cache()


def _require_https_secret(value: str | None) -> str:
    if not value or "•" in value:
        raise DingTalkMCPError("MCP URL 未配置或是掩码值")
    try:
        return normalize_https_mcp_url(value)
    except MCPURLValidationError:
        raise DingTalkMCPError("MCP URL 无效或不安全") from None


def _node_id(item: dict) -> str:
    value = item.get("nodeId") or item.get("id")
    if not isinstance(value, str) or not value:
        raise DingTalkMCPError("节点缺少 nodeId")
    return value


def _sheet_id(item: dict) -> str:
    value = item.get("sheetId") or item.get("id")
    if not isinstance(value, str) or not value:
        raise DingTalkMCPError("工作表缺少 sheetId")
    return value


def _require_extension(info: dict, expected: str) -> None:
    if info.get("extension") != expected:
        raise DingTalkMCPError(f"节点类型必须为 {expected}")


def _set_request_deadline(target, deadline: float) -> None:
    setter = getattr(target, "set_request_deadline", None)
    if callable(setter):
        setter(deadline)


def _pause(interval: float, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if interval > 0 and remaining > 0:
        time.sleep(min(interval, remaining))


def _find_probe(
    document,
    folder_id: str,
    before: set[str],
    probe_name: str,
    timeout: float,
    interval: float,
) -> str | None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        candidates: dict[str, dict] = {}
        _set_request_deadline(document, deadline)
        for item in document.list_nodes(folder_id):
            node_id = _node_id(item)
            if (
                node_id not in before
                and item.get("extension") == "axls"
                and item.get("name") == probe_name
            ):
                candidates[node_id] = item
        if len(candidates) == 1:
            return next(iter(candidates))
        if len(candidates) > 1:
            raise _ProbeAmbiguityError(set(candidates))
        if time.monotonic() >= deadline:
            return None
        _pause(interval, deadline)


def _strict_probe_ids(
    document,
    folder_id: str,
    before: set[str],
    probe_name: str,
    deadline: float,
) -> set[str]:
    _set_request_deadline(document, deadline)
    return {
        node_id
        for item in document.list_nodes(folder_id)
        if (
            (node_id := _node_id(item)) not in before
            and item.get("extension") == "axls"
            and item.get("name") == probe_name
        )
    }


def _cleanup_correlated_probes(
    document,
    folder_id: str,
    before: set[str],
    probe_name: str,
    initial_probe_ids: set[str],
    timeout: float,
    interval: float,
) -> tuple[set[str], bool]:
    deadline = time.monotonic() + max(0.0, timeout)
    known_ids = set(initial_probe_ids)
    deletion_requested: set[str] = set()
    deletion_failed = False
    stable_absent_observations = 0
    ambiguity_seen = len(known_ids) > 1

    # IDs passed here were already correlated by before/after ID, exact UUID
    # name and axls type. Delete them once even if an eventually-consistent
    # list call temporarily claims they are absent.
    for candidate_id in sorted(known_ids):
        deletion_requested.add(candidate_id)
        try:
            _set_request_deadline(document, deadline)
            document.delete_document(candidate_id)
        except Exception:
            deletion_failed = True

    while True:
        current_ids = _strict_probe_ids(
            document,
            folder_id,
            before,
            probe_name,
            deadline,
        )
        known_ids.update(current_ids)
        ambiguity_seen = ambiguity_seen or len(known_ids) > 1

        for candidate_id in sorted(current_ids - deletion_requested):
            deletion_requested.add(candidate_id)
            try:
                _set_request_deadline(document, deadline)
                document.delete_document(candidate_id)
            except Exception:
                deletion_failed = True

        if current_ids:
            stable_absent_observations = 0
        else:
            stable_absent_observations += 1
            if stable_absent_observations >= 2:
                if deletion_failed:
                    raise DingTalkMCPError(
                        "部分连接测试文件删除失败"
                    )
                return known_ids, ambiguity_seen

        if time.monotonic() >= deadline:
            raise DingTalkMCPError("连接测试文件删除确认超时")
        _pause(interval, deadline)


def _diagnostic_detail(exc: Exception) -> str:
    if isinstance(exc, DingTalkMCPError):
        return redact_text(str(exc))
    return f"诊断调用失败（{type(exc).__name__}）"


_PROVIDER_SECRET_NAMES = {
    ProviderName.codex: "codex_api_key",
    ProviderName.minimax: "minimax_api_key",
    ProviderName.openai_compatible: "openai_compatible_api_key",
}


def _provider_fingerprint(service, snapshot, provider: ProviderName) -> str:
    provider_settings = getattr(snapshot.settings.ai, provider.value)
    secret = snapshot.secrets.reveal(_PROVIDER_SECRET_NAMES[provider])
    canonical = json.dumps(
        {
            "service_identity": id(service),
            "provider": provider.value,
            "settings": provider_settings.model_dump(mode="json"),
            "secret": secret or "",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_provider_health(
    snapshot,
    expected_provider: ProviderName,
    health: ProviderHealth,
) -> ProviderHealth:
    if health.provider != expected_provider:
        raise ProviderUnavailableError(
            "Provider 健康检查返回了错误的 Provider"
        )

    def safe_text(value: object) -> str:
        text = str(value)
        for name in (
            "document_mcp_url",
            "spreadsheet_mcp_url",
            "minimax_api_key",
            "openai_compatible_api_key",
            "codex_api_key",
        ):
            secret = snapshot.secrets.reveal(name)
            if secret:
                text = text.replace(secret, "<redacted>")
        return redact_text(text)

    return ProviderHealth(
        ok=bool(health.ok),
        provider=expected_provider,
        detail=safe_text(health.detail),
        runtime_mode=safe_text(health.runtime_mode),
    )


def _safe_health_failure(exc: BaseException) -> ProviderUnavailableError:
    marker = "".join(
        character
        for character in type(exc).__name__
        if character.isalnum() or character == "_"
    )[:64] or "Exception"
    return ProviderUnavailableError(
        f"Provider 连接测试失败（{marker}）"
    )


def _execute_health_flight(
    key: tuple[int, ProviderName, str],
    flight: _HealthFlight,
    dependencies: RuntimeDependencies,
    snapshot,
    provider: ProviderName,
) -> None:
    try:
        health = dependencies.registry.health_for(provider, snapshot)
        if not isinstance(health, ProviderHealth):
            raise ProviderUnavailableError(
                "Provider 健康检查返回无效结果"
            )
        flight.health = _safe_provider_health(
            snapshot,
            provider,
            health,
        )
    except BaseException as exc:
        flight.error = _safe_health_failure(exc)
    finally:
        flight.completed.set()
        with _AI_TEST_FLIGHTS_LOCK:
            if _AI_TEST_FLIGHTS.get(key) is flight:
                _AI_TEST_FLIGHTS.pop(key, None)


def _publish_health_if_current(
    dependencies: RuntimeDependencies,
    provider: ProviderName,
    fingerprint: str,
    health: ProviderHealth,
) -> None:
    current = dependencies.service.snapshot()
    if (
        _provider_fingerprint(dependencies.service, current, provider)
        != fingerprint
    ):
        return
    with _LAST_AI_CONNECTION_LOCK:
        _LAST_AI_CONNECTION[(id(dependencies.service), provider)] = {
            "fingerprint": fingerprint,
            "health": asdict(health),
        }


def _construct_mcp(factory: Callable[[str], object], url: str):
    try:
        return factory(url)
    except DingTalkMCPError as exc:
        raise DingTalkMCPError(redact_text(exc)) from None
    except Exception as exc:
        raise DingTalkMCPError(
            f"MCP 服务初始化失败（{type(exc).__name__}）"
        ) from None


def _run_document_connection_test_locked(
    dependencies: RuntimeDependencies,
    payload: DocumentConnectionTest,
    poll: ProbePollPolicy,
) -> dict:
    service = dependencies.service
    snapshot = service.snapshot()
    document_settings = _resolved_document_settings(
        snapshot.settings.document,
        payload,
    )
    document_url = (
        (payload.document_mcp_url or "").strip()
        or snapshot.secrets.reveal("document_mcp_url")
    )
    spreadsheet_url = (
        (payload.spreadsheet_mcp_url or "").strip()
        or snapshot.secrets.reveal("spreadsheet_mcp_url")
    )
    document = _construct_mcp(
        dependencies.document_factory,
        _require_https_secret(document_url),
    )
    sheets = _construct_mcp(
        dependencies.spreadsheet_factory,
        _require_https_secret(spreadsheet_url),
    )
    content_id = extract_node_id(document_settings.content_template_url)
    template_id = extract_node_id(document_settings.document_template_url)
    folder_id = extract_node_id(document_settings.output_folder_url)
    checks: list[dict] = []
    before: set[str] = set()
    probe_name = f"PRDtoCASE-connection-test-{uuid.uuid4().hex}"
    probe_id: str | None = None
    ambiguous_probe_ids: set[str] = set()
    create_submitted = False
    try:
        content_info = document.get_document_info(content_id)
        _require_extension(content_info, "axls")
        checks.append({"name": "content_template", "ok": True})

        template_info = document.get_document_info(template_id)
        _require_extension(template_info, "axls")
        checks.append({"name": "output_template", "ok": True})

        if not sheets.get_all_sheets(content_id):
            raise DingTalkMCPError("内容模板没有工作表")
        if not sheets.get_all_sheets(template_id):
            raise DingTalkMCPError("输出模板没有工作表")
        checks.append({"name": "template_sheets", "ok": True})

        folder_info = document.get_document_info(folder_id)
        folder_kind = folder_info.get("nodeType")
        if folder_kind is None:
            folder_kind = folder_info.get("type")
        if folder_kind not in {"folder", "space"}:
            raise DingTalkMCPError("输出目标不是文件夹")
        before = {
            _node_id(item) for item in document.list_nodes(folder_id)
        }

        create_submitted = True
        # Never trust the response ID: only the before/after exact-name
        # association can authorize a subsequent write or delete.
        document.create_file(probe_name, "axls", folder_id)
        probe_id = _find_probe(
            document,
            folder_id,
            before,
            probe_name,
            poll.visible_timeout,
            poll.interval,
        )
        if not probe_id:
            raise DingTalkMCPError("连接测试文件在首次可见期限内不可见")

        write_deadline = time.monotonic() + max(
            0.0,
            poll.visible_timeout,
        )
        _set_request_deadline(sheets, write_deadline)
        probe_sheets = sheets.get_all_sheets(probe_id)
        if not probe_sheets:
            raise DingTalkMCPError("连接测试文件没有工作表")
        _set_request_deadline(sheets, write_deadline)
        sheets.set_range_from_csv(
            probe_id,
            _sheet_id(probe_sheets[0]),
            "A1",
            "connection-test",
            True,
        )
        checks.append({"name": "output_write", "ok": True})
    except _ProbeAmbiguityError as exc:
        ambiguous_probe_ids.update(exc.node_ids)
        checks.append(
            {
                "name": "diagnostic",
                "ok": False,
                "detail": _diagnostic_detail(exc),
            }
        )
    except Exception as exc:
        checks.append(
            {
                "name": "diagnostic",
                "ok": False,
                "detail": _diagnostic_detail(exc),
            }
        )
    finally:
        cleanup_recorded = False
        if create_submitted and not probe_id and not ambiguous_probe_ids:
            try:
                probe_id = _find_probe(
                    document,
                    folder_id,
                    before,
                    probe_name,
                    poll.late_cleanup_timeout,
                    poll.interval,
                )
            except _ProbeAmbiguityError as exc:
                ambiguous_probe_ids.update(exc.node_ids)
            except Exception as exc:
                cleanup_recorded = True
                checks.append(
                    {
                        "name": "cleanup",
                        "ok": False,
                        "detail": _diagnostic_detail(exc),
                        "marker": probe_name,
                    }
                )
        correlated_probe_ids = set(ambiguous_probe_ids)
        if probe_id:
            correlated_probe_ids.add(probe_id)
        if correlated_probe_ids:
            try:
                initially_ambiguous = len(correlated_probe_ids) > 1
                _, ambiguity_seen = _cleanup_correlated_probes(
                    document,
                    folder_id,
                    before,
                    probe_name,
                    correlated_probe_ids,
                    poll.deletion_timeout,
                    poll.interval,
                )
                if ambiguity_seen and not initially_ambiguous:
                    checks.append(
                        {
                            "name": "diagnostic",
                            "ok": False,
                            "detail": (
                                "清理期间发现多个精确匹配连接测试节点"
                            ),
                        }
                    )
                checks.append({"name": "cleanup", "ok": True})
            except Exception as exc:
                checks.append(
                    {
                        "name": "cleanup",
                        "ok": False,
                        "detail": _diagnostic_detail(exc),
                        "marker": probe_name,
                    }
                )
        elif create_submitted and not cleanup_recorded:
            checks.append(
                {
                    "name": "cleanup",
                    "ok": False,
                    "detail": "连接测试文件在迟到清理期限内仍不可关联",
                    "marker": probe_name,
                }
            )
    return {
        "ok": bool(checks) and all(item["ok"] for item in checks),
        "checks": checks,
    }


def run_document_connection_test(
    dependencies: RuntimeDependencies,
    payload: DocumentConnectionTest,
    poll: ProbePollPolicy = ProbePollPolicy(),
) -> dict:
    # Probes create and delete remote state, so serialize them globally.
    with _DOCUMENT_PROBE_LOCK:
        return _run_document_connection_test_locked(
            dependencies,
            payload,
            poll,
        )


def run_ai_connection_test(
    dependencies: RuntimeDependencies,
    payload: AIConnectionTest,
    *,
    wait_timeout_seconds: float = 30.0,
) -> ProviderHealth:
    snapshot = dependencies.service.snapshot()
    fingerprint = _provider_fingerprint(
        dependencies.service,
        snapshot,
        payload.provider,
    )
    key = (id(dependencies.registry), payload.provider, fingerprint)
    with _AI_TEST_FLIGHTS_LOCK:
        flight = _AI_TEST_FLIGHTS.get(key)
        leader = flight is None
        if flight is None:
            flight = _HealthFlight(completed=threading.Event())
            _AI_TEST_FLIGHTS[key] = flight

    if leader:
        try:
            worker = threading.Thread(
                target=_execute_health_flight,
                args=(
                    key,
                    flight,
                    dependencies,
                    snapshot,
                    payload.provider,
                ),
                name="provider-health-check",
                daemon=True,
            )
            worker.start()
        except BaseException as exc:
            flight.error = _safe_health_failure(exc)
            flight.completed.set()
            with _AI_TEST_FLIGHTS_LOCK:
                if _AI_TEST_FLIGHTS.get(key) is flight:
                    _AI_TEST_FLIGHTS.pop(key, None)

    if not flight.completed.wait(
        timeout=max(0.0, wait_timeout_seconds)
    ):
        raise ProviderUnavailableError("Provider 连接测试等待超时")

    if flight.error is not None:
        raise flight.error
    if flight.health is None:
        raise ProviderUnavailableError("Provider 连接测试未返回结果")

    _publish_health_if_current(
        dependencies,
        payload.provider,
        fingerprint,
        flight.health,
    )
    return flight.health


@router.get("/document")
def get_document(service: SettingsService = Depends(get_settings_service)):
    return document_view(service)


@router.put("/document")
def update_document(
    payload: DocumentUpdate,
    service: SettingsService = Depends(get_settings_service),
):
    apply_document_update(service, payload)
    return document_view(service)


@router.post("/document/test")
def test_document(
    payload: DocumentConnectionTest,
    dependencies: RuntimeDependencies = Depends(get_runtime_dependencies),
):
    return run_document_connection_test(dependencies, payload)


@router.get("/prompts")
def get_prompts(service: SettingsService = Depends(get_settings_service)):
    return prompt_view(service)


@router.put("/prompts")
def update_prompts(
    payload: PromptSettings,
    service: SettingsService = Depends(get_settings_service),
):
    validate_prompt_group(payload)
    service.update_group("prompts", payload)
    return prompt_view(service)


@router.post("/prompts/validate")
def validate_prompts(payload: PromptSettings):
    return validate_prompt_group(payload)


@router.post("/prompts/{prompt_name}/validate")
def validate_prompt_option(
    prompt_name: str,
    payload: PromptDraftValidation,
):
    return validate_prompt_draft(prompt_name, payload.content)


@router.put("/prompts/{prompt_name}")
def save_prompt_option(
    prompt_name: str,
    payload: PromptOptionSave,
    service: SettingsService = Depends(get_settings_service),
):
    if prompt_name not in DEFAULT_PROMPTS:
        raise HTTPException(status_code=404, detail="未知提示词类型")
    if payload.option_id == "default":
        if payload.name is not None or payload.content is not None:
            raise HTTPException(
                status_code=422,
                detail="默认提示词名称和内容不能修改",
            )
    else:
        if payload.content is None:
            raise HTTPException(
                status_code=422,
                detail={"fields": {"content": "提示词内容不能为空"}},
            )
        validate_prompt_draft(prompt_name, payload.content)
    settings, saved_option_id = service.save_prompt_option(
        prompt_name,
        option_id=payload.option_id,
        name=payload.name,
        content=payload.content,
    )
    return {
        "saved_option_id": saved_option_id,
        "group": prompt_group_view(settings, prompt_name),
    }


@router.delete("/prompts/{prompt_name}/options/{option_id}")
def delete_prompt_option(
    prompt_name: str,
    option_id: str,
    service: SettingsService = Depends(get_settings_service),
):
    if prompt_name not in DEFAULT_PROMPTS:
        raise HTTPException(status_code=404, detail="未知提示词类型")
    try:
        settings = service.delete_prompt_option(prompt_name, option_id)
    except SettingsValidationError as exc:
        if "当前选中" in str(exc):
            raise HTTPException(status_code=409, detail=str(exc)) from None
        raise
    return {"group": prompt_group_view(settings, prompt_name)}


@router.get("/ai")
def get_ai(service: SettingsService = Depends(get_settings_service)):
    return ai_view(service)


@router.get("/ai/configurations")
def get_ai_configurations(
    service: SettingsService = Depends(get_settings_service),
):
    return ai_configurations_view(service)


@router.post("/ai/configurations")
def create_ai_configuration(
    payload: AIConfigurationUpsert,
    service: SettingsService = Depends(get_settings_service),
):
    apply_ai_configuration_upsert(service, payload)
    return ai_configurations_view(service)


@router.patch("/ai/configurations/order")
def reorder_ai_configurations(
    payload: AIConfigurationOrder,
    service: SettingsService = Depends(get_settings_service),
):
    service.reorder_ai_configurations(payload.configuration_ids)
    return ai_configurations_view(service)


@router.put("/ai/configurations/{configuration_id}")
def update_ai_configuration(
    configuration_id: str,
    payload: AIConfigurationUpsert,
    service: SettingsService = Depends(get_settings_service),
):
    apply_ai_configuration_upsert(
        service,
        payload,
        forced_id=configuration_id,
    )
    return ai_configurations_view(service)


@router.delete("/ai/configurations/{configuration_id}")
def trash_ai_configuration(
    configuration_id: str,
    service: SettingsService = Depends(get_settings_service),
):
    service.trash_ai_configuration(configuration_id)
    return ai_configurations_view(service)


@router.post("/ai/configurations/{configuration_id}/restore")
def restore_ai_configuration(
    configuration_id: str,
    service: SettingsService = Depends(get_settings_service),
):
    service.restore_ai_configuration(configuration_id)
    return ai_configurations_view(service)


@router.delete("/ai/configurations/{configuration_id}/purge")
def purge_ai_configuration(
    configuration_id: str,
    service: SettingsService = Depends(get_settings_service),
):
    service.purge_ai_configuration(configuration_id)
    return ai_configurations_view(service)


@router.get("/ai/model-policies")
def get_test_case_model_policies(
    service: SettingsService = Depends(get_settings_service),
):
    return test_case_model_policies_view(service)


@router.put("/ai/model-policies/{stage}")
def update_test_case_model_policy(
    stage: str,
    payload: TestCaseModelPolicyUpdate,
    service: SettingsService = Depends(get_settings_service),
):
    service.save_test_case_model_policy(
        stage,
        ModelSelectionPolicy(
            mode=payload.mode,
            configuration_ids=(
                payload.configuration_ids
                if payload.mode == ModelSelectionMode.custom
                else ()
            ),
        ),
    )
    return test_case_model_policies_view(service)


@router.put("/ai")
def update_ai(
    payload: AIUpdate,
    service: SettingsService = Depends(get_settings_service),
):
    apply_ai_update(service, payload)
    return ai_view(service)


@router.get("/ai/models/{provider}")
def get_ai_models(
    provider: ProviderName,
    service: SettingsService = Depends(get_settings_service),
):
    return ai_model_catalog_view(service, provider)


@router.post("/ai/models/{provider}/refresh")
def refresh_ai_models(
    provider: ProviderName,
    service: SettingsService = Depends(get_settings_service),
):
    return ai_model_catalog_view(service, provider)


@router.post("/ai/test")
def test_ai(
    payload: AIConnectionTest,
    dependencies: RuntimeDependencies = Depends(get_runtime_dependencies),
):
    return run_ai_connection_test(dependencies, payload)
