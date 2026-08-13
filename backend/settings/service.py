import os
import threading
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from backend.settings.models import (
    AIConfiguration,
    AIConfigurationStatus,
    AISettings,
    AppSettings,
    DocumentSettings,
    ModelSelectionMode,
    ModelSelectionPolicy,
    PromptCustomOption,
    PromptSettings,
    PromptSlot,
    ResolvedSecrets,
    SecretStatus,
    SettingsSnapshot,
)
from backend.settings.defaults import DEFAULT_PROMPTS
from backend.settings.prompt_library import (
    DEFAULT_PROMPT_OPTION_ID,
    PROMPT_NAMES,
    legacy_custom_option,
    legacy_option_id,
    require_prompt_name,
    resolve_prompt_settings,
)
from backend.settings.secrets import SecretStore, mask_secret
from backend.settings.store import SettingsRepository
from backend.settings.store import LEGACY_AI_CONFIGURATION_IDS
from backend.security.redaction import redact_text


SECRET_ENV = {
    "document_mcp_url": ("DINGTALK_MCP_URL",),
    "spreadsheet_mcp_url": ("DINGTALK_SPREADSHEET_MCP_URL",),
    "minimax_api_key": ("MINIMAX_API_KEY",),
    "openai_compatible_api_key": (
        "OPENAI_COMPATIBLE_API_KEY",
        "OPENAI_API_KEY",
    ),
    "codex_api_key": ("CODEX_API_KEY",),
}

_LEGACY_CONFIGURATION_SECRETS = {
    LEGACY_AI_CONFIGURATION_IDS["codex"]: "codex_api_key",
    LEGACY_AI_CONFIGURATION_IDS["minimax"]: "minimax_api_key",
    LEGACY_AI_CONFIGURATION_IDS["openai_compatible"]: (
        "openai_compatible_api_key"
    ),
}


def ai_configuration_secret_name(configuration_id: str) -> str:
    """生成与普通旧密钥不冲突的配置级密钥环名称。"""

    normalized = str(configuration_id).strip()
    if not normalized:
        raise SettingsValidationError("AI 配置 ID 不能为空")
    return f"ai_config:{normalized}:api_key"


def scrub_secret_environment(
    environment: MutableMapping[str, str] | None = None,
) -> None:
    """Remove every provider/MCP bootstrap alias from a process environment."""

    target = environment if environment is not None else os.environ
    for env_names in SECRET_ENV.values():
        for env_name in env_names:
            target.pop(env_name, None)


_SERVICE_LOCKS_GUARD = threading.Lock()
_SERVICE_LOCKS: dict[str, threading.RLock] = {}


def _lock_for_repository(repository: SettingsRepository) -> threading.RLock:
    key = str(repository.path.resolve())
    with _SERVICE_LOCKS_GUARD:
        return _SERVICE_LOCKS.setdefault(key, threading.RLock())


class SettingsValidationError(ValueError):
    """Safe user-facing validation failure mapped to HTTP 422."""


class SettingsConsistencyError(RuntimeError):
    """Credential compensation could not fully restore the saved state."""


def _must_detach_failure(
    error: BaseException,
    secret_values: Iterable[str | None],
) -> bool:
    """Detect errors whose traceback text can expose known credential forms."""

    try:
        raw = str(error)
    except Exception:
        return True
    if redact_text(raw) != raw:
        return True
    return any(value and value in raw for value in secret_values)


class SettingsService:
    def __init__(
        self,
        repository: SettingsRepository,
        secrets: SecretStore,
        environment: Mapping[str, str] | None = None,
    ):
        self.repository = repository
        self.secrets = secrets
        self._update_lock = _lock_for_repository(repository)
        source = environment if environment is not None else os.environ
        self._bootstrap_secrets = {
            name: next(
                (
                    source[env_name]
                    for env_name in env_names
                    if source.get(env_name)
                ),
                None,
            )
            for name, env_names in SECRET_ENV.items()
        }

    def load(self) -> AppSettings:
        return self._load_repository()

    def _load_repository(self) -> AppSettings:
        try:
            return self.repository.load()
        except Exception as exc:
            if _must_detach_failure(
                exc,
                self._bootstrap_secrets.values(),
            ):
                raise SettingsConsistencyError(
                    f"设置读取失败（{type(exc).__name__}）"
                ) from None
            raise

    def snapshot(self) -> SettingsSnapshot:
        with self._update_lock:
            settings = AppSettings.model_validate(
                self._load_repository().model_dump()
            )
            configuration_api_keys = {}
            for configuration in settings.ai.configurations:
                value, _source = self._get_ai_configuration_secret_unlocked(
                    configuration.id
                )
                if value:
                    configuration_api_keys[configuration.id] = value
            resolved = ResolvedSecrets(
                **{
                    name: self._get_secret_unlocked(name)[0]
                    for name in SECRET_ENV
                },
                ai_configuration_api_keys=configuration_api_keys,
            )
            return SettingsSnapshot(settings=settings, secrets=resolved)

    def update_document(self, document: DocumentSettings) -> AppSettings:
        return self.update_group("document", document)

    def update_prompts(self, prompts: PromptSettings) -> AppSettings:
        """Compatibility update for the former four-textarea API.

        The effective values still change exactly as requested, but the v2
        repository represents each non-default value as a selected custom
        option instead of persisting the runtime `prompts` projection.
        """

        return self.update_group("prompts", prompts)

    def update_ai(self, ai: AISettings) -> AppSettings:
        return self.update_group("ai", ai)

    @staticmethod
    def _replace_prompt_slot(
        settings: AppSettings,
        prompt_name: str,
        slot: PromptSlot,
    ) -> AppSettings:
        library = settings.prompt_library.model_copy(
            update={prompt_name: slot}
        )
        return settings.model_copy(
            update={
                "prompt_library": library,
                "prompts": resolve_prompt_settings(library),
            }
        )

    @staticmethod
    def _unique_legacy_name(slot: PromptSlot) -> str:
        existing = {option.name.strip() for option in slot.custom_options}
        base = "旧版自定义"
        if base not in existing:
            return base
        index = 2
        while f"{base} {index}" in existing:
            index += 1
        return f"{base} {index}"

    @classmethod
    def _apply_legacy_prompts(
        cls,
        settings: AppSettings,
        prompts: PromptSettings,
    ) -> AppSettings:
        library = settings.prompt_library
        for prompt_name in PROMPT_NAMES:
            content = getattr(prompts, prompt_name)
            if not content.strip():
                raise SettingsValidationError("提示词内容不能为空")
            slot = getattr(library, prompt_name)
            if content == DEFAULT_PROMPTS[prompt_name]:
                updated_slot = slot.model_copy(
                    update={"selected_option_id": DEFAULT_PROMPT_OPTION_ID}
                )
            else:
                matching = next(
                    (
                        option
                        for option in slot.custom_options
                        if option.content == content
                    ),
                    None,
                )
                selected = next(
                    (
                        option
                        for option in slot.custom_options
                        if option.id == slot.selected_option_id
                    ),
                    None,
                )
                reserved_id = legacy_option_id(prompt_name)
                reserved = next(
                    (
                        option
                        for option in slot.custom_options
                        if option.id == reserved_id
                    ),
                    None,
                )
                target = matching or selected or reserved
                if target is None:
                    target = legacy_custom_option(
                        prompt_name,
                        content,
                        name=cls._unique_legacy_name(slot),
                    )
                    options = (*slot.custom_options, target)
                elif target.content != content:
                    target = target.model_copy(update={"content": content})
                    options = tuple(
                        target if option.id == target.id else option
                        for option in slot.custom_options
                    )
                else:
                    options = slot.custom_options
                updated_slot = PromptSlot(
                    selected_option_id=target.id,
                    custom_options=options,
                )
            library = library.model_copy(update={prompt_name: updated_slot})
        return settings.model_copy(
            update={
                "prompt_library": library,
                "prompts": resolve_prompt_settings(library),
            }
        )

    def save_prompt_option(
        self,
        prompt_name: str,
        *,
        option_id: str | None,
        name: str | None = None,
        content: str | None = None,
    ) -> tuple[AppSettings, str]:
        """Atomically select default, or create/update and select a custom."""

        try:
            require_prompt_name(prompt_name)
        except KeyError:
            raise SettingsValidationError("未知提示词类型") from None
        requested_id = option_id.strip() if option_id else None
        if requested_id == DEFAULT_PROMPT_OPTION_ID:
            with self._update_lock:
                saved = self.repository.update(
                    lambda settings: self._replace_prompt_slot(
                        settings,
                        prompt_name,
                        getattr(settings.prompt_library, prompt_name).model_copy(
                            update={
                                "selected_option_id": DEFAULT_PROMPT_OPTION_ID
                            }
                        ),
                    )
                )
            return saved, DEFAULT_PROMPT_OPTION_ID

        normalized_name = name.strip() if name is not None else ""
        if not normalized_name:
            raise SettingsValidationError("提示词名称不能为空")
        if content is None or not content.strip():
            raise SettingsValidationError("提示词内容不能为空")

        saved_id: list[str] = []

        def mutate(settings: AppSettings) -> AppSettings:
            slot = getattr(settings.prompt_library, prompt_name)
            existing = next(
                (
                    option
                    for option in slot.custom_options
                    if option.id == requested_id
                ),
                None,
            )
            if requested_id is not None and existing is None:
                raise SettingsValidationError("提示词选项不存在")
            duplicate = next(
                (
                    option
                    for option in slot.custom_options
                    if option.id != requested_id
                    and option.name.strip() == normalized_name
                ),
                None,
            )
            if duplicate is not None:
                raise SettingsValidationError("同类提示词名称不能重复")
            candidate_id = requested_id or str(uuid4())
            option = PromptCustomOption(
                id=candidate_id,
                name=normalized_name,
                content=content,
            )
            if existing is None:
                options = (*slot.custom_options, option)
            else:
                options = tuple(
                    option if item.id == candidate_id else item
                    for item in slot.custom_options
                )
            updated_slot = PromptSlot(
                selected_option_id=candidate_id,
                custom_options=options,
            )
            saved_id.append(candidate_id)
            return self._replace_prompt_slot(
                settings,
                prompt_name,
                updated_slot,
            )

        with self._update_lock:
            saved = self.repository.update(mutate)
        return saved, saved_id[0]

    def delete_prompt_option(
        self,
        prompt_name: str,
        option_id: str,
    ) -> AppSettings:
        """Delete an existing, unselected custom prompt option."""

        try:
            require_prompt_name(prompt_name)
        except KeyError:
            raise SettingsValidationError("未知提示词类型") from None
        normalized_id = option_id.strip()
        if normalized_id == DEFAULT_PROMPT_OPTION_ID:
            raise SettingsValidationError("默认提示词不能删除")
        if not normalized_id:
            raise SettingsValidationError("提示词选项 ID 不能为空")

        def mutate(settings: AppSettings) -> AppSettings:
            slot = getattr(settings.prompt_library, prompt_name)
            if slot.selected_option_id == normalized_id:
                raise SettingsValidationError("当前选中的提示词不能删除")
            if not any(
                option.id == normalized_id
                for option in slot.custom_options
            ):
                raise SettingsValidationError("提示词选项不存在")
            updated_slot = PromptSlot(
                selected_option_id=slot.selected_option_id,
                custom_options=tuple(
                    option
                    for option in slot.custom_options
                    if option.id != normalized_id
                ),
            )
            return self._replace_prompt_slot(
                settings,
                prompt_name,
                updated_slot,
            )

        with self._update_lock:
            return self.repository.update(mutate)

    def update_group(
        self,
        group: str,
        value: Any,
        *,
        secret_updates: dict[str, str | None] | None = None,
        clear_secrets: Iterable[str] = (),
        final_state_validator: Callable[
            [AppSettings, Mapping[str, str | None]], None
        ]
        | None = None,
    ) -> AppSettings:
        if group not in {"document", "prompts", "ai"}:
            raise KeyError(group)
        if group == "prompts":
            value = PromptSettings.model_validate(value)
        clear_names = set(clear_secrets)
        updates = {
            name: secret.strip()
            for name, secret in (secret_updates or {}).items()
            if (
                name not in clear_names
                and secret is not None
                and secret.strip()
            )
        }
        for name, secret in updates.items():
            if name not in SECRET_ENV:
                raise KeyError(name)
            if "•" in secret:
                raise SettingsValidationError("不能把掩码值保存为密钥")
        for name in clear_names:
            if name not in SECRET_ENV:
                raise KeyError(name)

        with self._update_lock:
            current = self._load_repository()
            apply_group = (
                lambda settings: self._apply_legacy_prompts(settings, value)
                if group == "prompts"
                else settings.model_copy(update={group: value})
            )
            prospective = AppSettings.model_validate(
                apply_group(current).model_dump()
            )
            if final_state_validator is not None:
                resolved_after_update: dict[str, str | None] = {}
                for name in SECRET_ENV:
                    if name in clear_names:
                        saved_value = None
                    elif name in updates:
                        saved_value = updates[name]
                    else:
                        saved_value = self._read_saved_secret(name)
                    resolved_after_update[name] = (
                        saved_value or self._bootstrap_secrets[name]
                    )
                final_state_validator(
                    prospective,
                    MappingProxyType(resolved_after_update),
                )
            touched_names = tuple(sorted(set(updates) | clear_names))
            previous = {
                name: self._read_saved_secret(name)
                for name in touched_names
            }
            sensitive_values = (
                *updates.values(),
                *previous.values(),
                *self._bootstrap_secrets.values(),
            )
            try:
                for name in sorted(updates):
                    self.secrets.set(name, updates[name])
                for name in sorted(clear_names):
                    self.secrets.delete(name)
                return self.repository.update(apply_group)
            except Exception as original_error:
                rollback_error_types = []
                for name in touched_names:
                    try:
                        old_value = previous[name]
                        if old_value is None:
                            self.secrets.delete(name)
                        else:
                            self.secrets.set(name, old_value)
                    except Exception as rollback_error:
                        rollback_error_types.append(
                            type(rollback_error).__name__
                        )
                if rollback_error_types:
                    error_types = ", ".join(
                        sorted(set(rollback_error_types))
                    )
                    safe_error = SettingsConsistencyError(
                        "凭据回滚不完整："
                        f"{len(rollback_error_types)} 项恢复失败"
                        f"（{error_types}）"
                    )
                    if _must_detach_failure(
                        original_error,
                        sensitive_values,
                    ):
                        raise safe_error from None
                    # Preserve legacy error identity only when neither common
                    # credential syntax nor an exact in-scope secret occurs.
                    raise safe_error from original_error
                if _must_detach_failure(original_error, sensitive_values):
                    raise SettingsConsistencyError(
                        "设置保存失败"
                        f"（{type(original_error).__name__}）"
                    ) from None
                raise

    @staticmethod
    def _configuration_by_id(
        settings: AppSettings,
        configuration_id: str,
    ) -> AIConfiguration:
        configuration = next(
            (
                item
                for item in settings.ai.configurations
                if item.id == configuration_id
            ),
            None,
        )
        if configuration is None:
            raise SettingsValidationError("AI 配置不存在")
        return configuration

    def _get_ai_configuration_secret_unlocked(
        self,
        configuration_id: str,
    ) -> tuple[str | None, str]:
        saved = self._read_saved_secret(
            ai_configuration_secret_name(configuration_id)
        )
        if saved:
            return saved, "saved"
        legacy_name = _LEGACY_CONFIGURATION_SECRETS.get(configuration_id)
        if legacy_name:
            return self._get_secret_unlocked(legacy_name)
        return None, "missing"

    def migrate_legacy_ai_secrets(self) -> None:
        """幂等复制旧固定密钥到配置级密钥；成功前绝不删除旧键。"""

        with self._update_lock:
            settings = self._load_repository()
            known_ids = {item.id for item in settings.ai.configurations}
            for configuration_id, legacy_name in _LEGACY_CONFIGURATION_SECRETS.items():
                if configuration_id not in known_ids:
                    continue
                dynamic_name = ai_configuration_secret_name(configuration_id)
                if self._read_saved_secret(dynamic_name):
                    continue
                legacy_value, _source = self._get_secret_unlocked(legacy_name)
                if not legacy_value:
                    continue
                try:
                    self.secrets.set(dynamic_name, legacy_value)
                except Exception as exc:
                    raise SettingsConsistencyError(
                        f"旧 AI 凭据迁移失败（{type(exc).__name__}）"
                    ) from None

    def ai_configuration_secret_status(
        self,
        configuration_id: str,
    ) -> SecretStatus:
        with self._update_lock:
            value, source = self._get_ai_configuration_secret_unlocked(
                configuration_id
            )
        return SecretStatus(
            configured=bool(value),
            masked_value=mask_secret(value),
            source=source,
        )

    def ai_configuration_is_complete(
        self,
        configuration: AIConfiguration,
    ) -> bool:
        """判断配置能否进入业务模型列表，不发起任何网络请求。"""

        if configuration.deleted_at is not None:
            return False
        if configuration.provider.value == "codex":
            if configuration.codex_cli_source.value == "custom":
                path = configuration.codex_cli_path
                if not path or not Path(path).expanduser().is_file():
                    return False
            elif not configuration.codex_cli_version.strip():
                return False
        elif not configuration.base_url:
            return False
        if configuration.requires_api_key():
            return self.ai_configuration_secret_status(
                configuration.id
            ).configured
        return True

    @staticmethod
    def _replace_ai_settings(
        settings: AppSettings,
        configurations: tuple[AIConfiguration, ...],
        *,
        policies=None,
    ) -> AppSettings:
        updates: dict[str, Any] = {"configurations": configurations}
        if policies is not None:
            updates["test_case_policies"] = policies
        return settings.model_copy(
            update={"ai": settings.ai.model_copy(update=updates)}
        )

    def save_ai_configuration(
        self,
        configuration: AIConfiguration,
        *,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> AppSettings:
        """原子新增/编辑配置，并在失败时恢复原密钥。"""

        candidate = AIConfiguration.model_validate(configuration.model_dump())
        normalized_key = api_key.strip() if api_key is not None else ""
        if "•" in normalized_key:
            raise SettingsValidationError("不能把掩码值保存为密钥")
        if clear_api_key and normalized_key:
            raise SettingsValidationError("清除密钥与设置新密钥不能同时执行")

        with self._update_lock:
            current = self._load_repository()
            existing = next(
                (
                    item
                    for item in current.ai.configurations
                    if item.id == candidate.id
                ),
                None,
            )
            if existing is not None and existing.deleted_at is not None:
                raise SettingsValidationError("回收站中的 AI 配置不能直接编辑")
            now = datetime.now(timezone.utc)
            prepared = candidate.model_copy(
                update={
                    "created_at": existing.created_at if existing else now,
                    "updated_at": now,
                    "deleted_at": existing.deleted_at if existing else None,
                    "status": AIConfigurationStatus.unchecked,
                    "status_detail": "",
                    "checked_at": None,
                }
            )
            if existing is None:
                configurations = (*current.ai.configurations, prepared)
            else:
                configurations = tuple(
                    prepared if item.id == prepared.id else item
                    for item in current.ai.configurations
                )
            prospective = AppSettings.model_validate(
                self._replace_ai_settings(
                    current,
                    configurations,
                ).model_dump()
            )

            secret_name = ai_configuration_secret_name(prepared.id)
            previous_secret = self._read_saved_secret(secret_name)
            secret_touched = bool(normalized_key) or clear_api_key
            try:
                if normalized_key:
                    self.secrets.set(secret_name, normalized_key)
                elif clear_api_key:
                    self.secrets.delete(secret_name)
                return self.repository.save(prospective)
            except Exception as original_error:
                if secret_touched:
                    try:
                        if previous_secret is None:
                            self.secrets.delete(secret_name)
                        else:
                            self.secrets.set(secret_name, previous_secret)
                    except Exception as rollback_error:
                        raise SettingsConsistencyError(
                            "AI 凭据回滚不完整"
                            f"（{type(rollback_error).__name__}）"
                        ) from None
                if _must_detach_failure(
                    original_error,
                    (normalized_key, previous_secret),
                ):
                    raise SettingsConsistencyError(
                        f"AI 配置保存失败（{type(original_error).__name__}）"
                    ) from None
                raise

    def reorder_ai_configurations(
        self,
        configuration_ids: Iterable[str],
    ) -> AppSettings:
        """只调整正常列表；回收站保持原有相对顺序。"""

        requested = tuple(str(item).strip() for item in configuration_ids)
        if len(requested) != len(set(requested)):
            raise SettingsValidationError("AI 配置排序不能包含重复项")
        with self._update_lock:
            current = self._load_repository()
            active = tuple(
                item for item in current.ai.configurations if item.deleted_at is None
            )
            if set(requested) != {item.id for item in active}:
                raise SettingsValidationError("AI 配置排序与当前列表不一致")
            by_id = {item.id: item for item in active}
            trashed = tuple(
                item for item in current.ai.configurations if item.deleted_at is not None
            )
            return self.repository.save(
                self._replace_ai_settings(
                    current,
                    tuple(by_id[item] for item in requested) + trashed,
                )
            )

    def trash_ai_configuration(self, configuration_id: str) -> AppSettings:
        with self._update_lock:
            current = self._load_repository()
            target = self._configuration_by_id(current, configuration_id)
            if target.deleted_at is not None:
                return current
            trashed = target.model_copy(
                update={
                    "deleted_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            configurations = tuple(
                trashed if item.id == target.id else item
                for item in current.ai.configurations
            )
            return self.repository.save(
                self._replace_ai_settings(current, configurations)
            )

    def restore_ai_configuration(self, configuration_id: str) -> AppSettings:
        with self._update_lock:
            current = self._load_repository()
            target = self._configuration_by_id(current, configuration_id)
            if target.deleted_at is None:
                return current
            if any(
                item.id != target.id
                and item.deleted_at is None
                and item.name.casefold() == target.name.casefold()
                for item in current.ai.configurations
            ):
                raise SettingsValidationError("存在同名配置，无法恢复")
            restored = target.model_copy(
                update={
                    "deleted_at": None,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            configurations = tuple(
                restored if item.id == target.id else item
                for item in current.ai.configurations
            )
            return self.repository.save(
                self._replace_ai_settings(current, configurations)
            )

    @staticmethod
    def _remove_configuration_from_policy(
        policy: ModelSelectionPolicy,
        configuration_id: str,
    ) -> ModelSelectionPolicy:
        remaining = tuple(
            item for item in policy.configuration_ids if item != configuration_id
        )
        if policy.mode == ModelSelectionMode.custom and not remaining:
            return ModelSelectionPolicy()
        return policy.model_copy(update={"configuration_ids": remaining})

    def purge_ai_configuration(self, configuration_id: str) -> AppSettings:
        """彻底删除回收站配置，同时清理步骤策略和配置级密钥。"""

        with self._update_lock:
            current = self._load_repository()
            target = self._configuration_by_id(current, configuration_id)
            if target.deleted_at is None:
                raise SettingsValidationError("只能彻底删除回收站中的 AI 配置")
            configurations = tuple(
                item for item in current.ai.configurations if item.id != target.id
            )
            policies = current.ai.test_case_policies.model_copy(
                update={
                    name: self._remove_configuration_from_policy(
                        getattr(current.ai.test_case_policies, name),
                        configuration_id,
                    )
                    for name in (
                        "image_understanding",
                        "component_matching",
                        "case_generation",
                    )
                }
            )
            prospective = AppSettings.model_validate(
                self._replace_ai_settings(
                    current,
                    configurations,
                    policies=policies,
                ).model_dump()
            )
            secret_name = ai_configuration_secret_name(configuration_id)
            previous_secret = self._read_saved_secret(secret_name)
            try:
                self.secrets.delete(secret_name)
                return self.repository.save(prospective)
            except Exception as original_error:
                try:
                    if previous_secret:
                        self.secrets.set(secret_name, previous_secret)
                except Exception as rollback_error:
                    raise SettingsConsistencyError(
                        "彻底删除失败且 AI 凭据回滚不完整"
                        f"（{type(rollback_error).__name__}）"
                    ) from None
                if _must_detach_failure(original_error, (previous_secret,)):
                    raise SettingsConsistencyError(
                        f"AI 配置彻底删除失败（{type(original_error).__name__}）"
                    ) from None
                raise

    def record_ai_configuration_status(
        self,
        configuration_id: str,
        *,
        ok: bool,
        detail: str,
    ) -> AppSettings:
        """保存脱敏后的最近检测结果，不触碰密钥。"""

        safe_detail = redact_text(str(detail)).strip()[:500]
        with self._update_lock:
            current = self._load_repository()
            target = self._configuration_by_id(current, configuration_id)
            checked_at = datetime.now(timezone.utc)
            updated = target.model_copy(
                update={
                    "status": (
                        AIConfigurationStatus.passed
                        if ok
                        else AIConfigurationStatus.error
                    ),
                    "status_detail": safe_detail,
                    "checked_at": checked_at,
                    "updated_at": checked_at,
                }
            )
            configurations = tuple(
                updated if item.id == target.id else item
                for item in current.ai.configurations
            )
            return self.repository.save(
                self._replace_ai_settings(current, configurations)
            )

    def save_test_case_model_policy(
        self,
        stage: str,
        policy: ModelSelectionPolicy,
    ) -> AppSettings:
        """保存真实 AI 调用步骤的有序模型策略。"""

        normalized_stage = str(stage).strip()
        valid_stages = {
            "image_understanding",
            "component_matching",
            "case_generation",
        }
        if normalized_stage not in valid_stages:
            raise SettingsValidationError("未知的测试用例生成步骤")
        candidate = ModelSelectionPolicy.model_validate(policy.model_dump())
        with self._update_lock:
            current = self._load_repository()
            by_id = {item.id: item for item in current.ai.configurations}
            for configuration_id in candidate.configuration_ids:
                configuration = by_id.get(configuration_id)
                if configuration is None or configuration.deleted_at is not None:
                    raise SettingsValidationError("模型策略包含已删除或不存在的配置")
                if not self.ai_configuration_is_complete(configuration):
                    raise SettingsValidationError("模型策略只能选择已完成的 AI 配置")
                if (
                    normalized_stage == "image_understanding"
                    and not configuration.vision_enabled
                ):
                    raise SettingsValidationError("图片理解步骤只能选择视觉模型")
            policies = current.ai.test_case_policies.model_copy(
                update={normalized_stage: candidate}
            )
            prospective = current.model_copy(
                update={
                    "ai": current.ai.model_copy(
                        update={"test_case_policies": policies}
                    )
                }
            )
            return self.repository.save(
                AppSettings.model_validate(prospective.model_dump())
            )

    def update_provider_secret(self, provider: str, value: str) -> None:
        key = f"{provider}_api_key"
        if key not in SECRET_ENV:
            raise KeyError(key)
        if "•" in value:
            raise SettingsValidationError("不能把掩码值保存为密钥")
        with self._update_lock:
            try:
                self.secrets.set(key, value)
            except Exception as exc:
                raise SettingsConsistencyError(
                    f"凭据保存失败（{type(exc).__name__}）"
                ) from None

    def clear_secret(self, name: str) -> None:
        if name not in SECRET_ENV:
            raise KeyError(name)
        with self._update_lock:
            try:
                self.secrets.delete(name)
            except Exception as exc:
                raise SettingsConsistencyError(
                    f"凭据删除失败（{type(exc).__name__}）"
                ) from None

    def _read_saved_secret(self, name: str) -> str | None:
        try:
            return self.secrets.get(name)
        except Exception as exc:
            raise SettingsConsistencyError(
                f"凭据读取失败（{type(exc).__name__}）"
            ) from None

    def _get_secret_unlocked(self, name: str) -> tuple[str | None, str]:
        saved = self._read_saved_secret(name)
        if saved:
            return saved, "saved"
        env_value = self._bootstrap_secrets[name]
        if env_value:
            return env_value, "environment"
        return None, "missing"

    def get_secret(self, name: str) -> tuple[str | None, str]:
        with self._update_lock:
            return self._get_secret_unlocked(name)

    def scrub_bootstrap_environment(
        self,
        environment: MutableMapping[str, str] | None = None,
    ) -> None:
        scrub_secret_environment(environment)

    def secret_status(self, name: str) -> SecretStatus:
        value, source = self.get_secret(name)
        return SecretStatus(
            configured=bool(value),
            masked_value=mask_secret(value),
            source=source,
        )
