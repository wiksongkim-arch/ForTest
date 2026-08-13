import json
import os
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

from filelock import FileLock

from backend.settings.defaults import DEFAULT_PROMPTS, default_settings
from backend.settings.models import (
    AIConfiguration,
    AIConfigurationProvider,
    AppSettings,
    CodexCLISource,
    PromptLibrarySettings,
    PromptSlot,
    TestCaseModelPolicies,
)
from backend.settings.prompt_library import (
    legacy_custom_option,
    materialize_settings,
    resolve_prompt_settings,
)


CURRENT_SCHEMA_VERSION = 5

LEGACY_AI_CONFIGURATION_IDS = {
    "codex": "legacy-codex",
    "minimax": "legacy-minimax",
    "openai_compatible": "legacy-openai-compatible",
}


def _deep_merge(base: dict, override: dict) -> dict:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _migrate_v0_to_v1(payload: dict) -> dict:
    # v0 was an unversioned, potentially partial copy of the v1 shape.
    base = default_settings().model_dump(mode="python")
    base.pop("prompt_library", None)
    # 新版默认对象包含 v3 字段；构造历史 v1 基线时必须移除，随后由正式
    # v2 -> v3 迁移统一生成，避免未版本化旧文件跳过兼容配置创建。
    ai = base.get("ai")
    if isinstance(ai, dict):
        ai.pop("configurations", None)
        ai.pop("test_case_policies", None)
    base.pop("system", None)
    base["schema_version"] = 1
    legacy_payload = deepcopy(payload)
    legacy_payload.pop("prompt_library", None)
    migrated = _deep_merge(base, legacy_payload)
    migrated["schema_version"] = 1
    return migrated


def _migrate_v1_to_v2(payload: dict) -> dict:
    migrated = deepcopy(payload)
    raw_prompts = _deep_merge(
        DEFAULT_PROMPTS,
        migrated.pop("prompts", {}) or {},
    )
    slots: dict[str, dict] = {}
    for prompt_name, default_content in DEFAULT_PROMPTS.items():
        content = raw_prompts[prompt_name]
        # The v1 validator allowed an empty image-understanding prompt because
        # that prompt has no required variables.  V2 deliberately disallows
        # empty custom options, so normalize that legacy state to the safe
        # built-in default instead of making the entire settings file fail to
        # load during migration.
        if isinstance(content, str) and not content.strip():
            slot = PromptSlot()
        elif content == default_content:
            slot = PromptSlot()
        else:
            option = legacy_custom_option(prompt_name, content)
            slot = PromptSlot(
                selected_option_id=option.id,
                custom_options=(option,),
            )
        slots[prompt_name] = slot.model_dump(mode="json")
    migrated["prompt_library"] = slots
    migrated["schema_version"] = 2
    return migrated


def _legacy_compatible_provider(settings: dict) -> AIConfigurationProvider:
    """根据旧预设和官方端点识别旧 OpenAI-compatible 配置。"""

    preset = str(settings.get("preset") or "").strip().lower()
    base_url = str(settings.get("base_url") or "").lower()
    if preset == "deepseek" or "deepseek" in base_url:
        return AIConfigurationProvider.deepseek
    if "moonshot" in base_url or "kimi" in base_url:
        return AIConfigurationProvider.kimi
    if "dashscope" in base_url or "aliyuncs" in base_url:
        return AIConfigurationProvider.qwen
    if "volces" in base_url or "volcengine" in base_url:
        return AIConfigurationProvider.doubao
    if "baidu" in base_url or "qianfan" in base_url:
        return AIConfigurationProvider.wenxin
    if "tencent" in base_url:
        return AIConfigurationProvider.hunyuan
    return AIConfigurationProvider.openai


def _migrate_v2_to_v3(payload: dict) -> dict:
    migrated = deepcopy(payload)
    raw_ai = dict(migrated.get("ai") or {})
    if "configurations" not in raw_ai:
        migrated_at = "1970-01-01T00:00:00+00:00"
        codex = dict(raw_ai.get("codex") or {})
        minimax = dict(raw_ai.get("minimax") or {})
        compatible = dict(raw_ai.get("openai_compatible") or {})
        compatible_provider = _legacy_compatible_provider(compatible)
        configurations = [
            AIConfiguration(
                id=LEGACY_AI_CONFIGURATION_IDS["codex"],
                name="Codex",
                provider=AIConfigurationProvider.codex,
                model=str(codex.get("model") or "gpt-5.4"),
                timeout_seconds=int(codex.get("timeout_seconds") or 900),
                vision_enabled=True,
                reasoning_effort=codex.get("reasoning_effort") or "high",
                inference_speed=codex.get("inference_speed") or "standard",
                max_concurrency=int(codex.get("max_concurrency") or 1),
                codex_cli_source=(
                    CodexCLISource.custom
                    if str(codex.get("cli_path") or "").strip()
                    else CodexCLISource.builtin
                ),
                codex_cli_path=codex.get("cli_path"),
                use_dedicated_api_key=bool(
                    codex.get("use_dedicated_api_key")
                ),
                created_at=migrated_at,
                updated_at=migrated_at,
            ),
            AIConfiguration(
                id=LEGACY_AI_CONFIGURATION_IDS["minimax"],
                name="MiniMax",
                provider=AIConfigurationProvider.minimax,
                model=str(minimax.get("model") or "MiniMax-M2.7"),
                base_url=str(
                    minimax.get("base_url") or "https://api.minimaxi.com"
                ),
                timeout_seconds=int(minimax.get("timeout_seconds") or 300),
                vision_enabled=bool(minimax.get("vision_enabled", True)),
                created_at=migrated_at,
                updated_at=migrated_at,
            ),
            AIConfiguration(
                id=LEGACY_AI_CONFIGURATION_IDS["openai_compatible"],
                name=(
                    "DeepSeek"
                    if compatible_provider == AIConfigurationProvider.deepseek
                    else "OpenAI 兼容"
                ),
                provider=compatible_provider,
                model=str(compatible.get("model") or "gpt-5.4"),
                base_url=str(
                    compatible.get("base_url") or "https://api.openai.com/v1"
                ),
                timeout_seconds=int(compatible.get("timeout_seconds") or 300),
                vision_enabled=bool(compatible.get("vision_enabled", True)),
                response_format_mode=(
                    compatible.get("response_format_mode") or "json_schema"
                ),
                created_at=migrated_at,
                updated_at=migrated_at,
            ),
        ]
        raw_ai["configurations"] = [
            item.model_dump(mode="json") for item in configurations
        ]
    raw_ai.setdefault(
        "test_case_policies",
        TestCaseModelPolicies().model_dump(mode="json"),
    )
    migrated["ai"] = raw_ai
    migrated["schema_version"] = 3
    return migrated


def _migrate_v3_to_v4(payload: dict) -> dict:
    """把启动脚本常量纳入统一用户设置，不复制任何环境或机器数据。"""

    migrated = deepcopy(payload)
    migrated.setdefault(
        "system",
        {"api_host": "127.0.0.1", "api_port": 8000, "frontend_port": 8501},
    )
    migrated["schema_version"] = 4
    return migrated


def _migrate_v4_to_v5(payload: dict) -> dict:
    """原生桌面版不再启动 Web 服务，迁移时清理旧端口设置。"""

    migrated = deepcopy(payload)
    migrated.pop("system", None)
    migrated["schema_version"] = 5
    return migrated


def migrate_payload(payload: dict) -> dict:
    migrated = deepcopy(payload)
    version = int(migrated.get("schema_version", 0))
    if version > CURRENT_SCHEMA_VERSION:
        raise ValueError(f"不支持未来设置版本: {version}")
    if version == 0:
        migrated = _migrate_v0_to_v1(migrated)
        version = 1
    if version == 1:
        migrated = _migrate_v1_to_v2(migrated)
        version = 2
    if version == 2:
        migrated = _migrate_v2_to_v3(migrated)
        version = 3
    if version == 3:
        migrated = _migrate_v3_to_v4(migrated)
        version = 4
    if version == 4:
        migrated = _migrate_v4_to_v5(migrated)
        version = 5
    if version != CURRENT_SCHEMA_VERSION:
        raise ValueError(f"缺少从设置版本 {version} 开始的迁移")
    # `prompts` 是运行时投影，v5 仍只持久化提示词库。
    migrated.pop("prompts", None)
    return migrated


class SettingsRepository:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(path) + ".lock")

    def _load_unlocked(self) -> AppSettings:
        if not self.path.exists():
            return default_settings()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        payload = migrate_payload(raw)
        library = PromptLibrarySettings.model_validate(
            payload.get("prompt_library", {})
        )
        payload["prompt_library"] = library.model_dump(mode="python")
        payload["prompts"] = resolve_prompt_settings(library).model_dump(
            mode="python"
        )
        return materialize_settings(AppSettings.model_validate(payload))

    def _write_unlocked(self, settings: AppSettings) -> AppSettings:
        validated = AppSettings.model_validate(settings.model_dump())
        normalized = materialize_settings(validated)
        payload = normalized.model_dump_json(
            indent=2,
            exclude={"prompts"},
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            temp_path.unlink(missing_ok=True)
        return normalized

    def load(self) -> AppSettings:
        with self.lock:
            return self._load_unlocked()

    def save(self, settings: AppSettings) -> AppSettings:
        if not isinstance(settings, AppSettings):
            raise TypeError("settings 必须是 AppSettings")
        with self.lock:
            return self._write_unlocked(settings)

    def update(
        self,
        mutator: Callable[[AppSettings], AppSettings],
    ) -> AppSettings:
        with self.lock:
            current = self._load_unlocked()
            candidate = mutator(current)
            if not isinstance(candidate, AppSettings):
                raise TypeError("mutator 必须返回 AppSettings")
            validated = AppSettings.model_validate(candidate.model_dump())
            return self._write_unlocked(validated)
