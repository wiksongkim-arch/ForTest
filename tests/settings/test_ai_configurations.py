"""AI 能力配置 schema、密钥、排序和回收站测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.ai.provider_specs import PROVIDER_SPECS
from backend.settings.defaults import default_settings
from backend.settings.models import (
    AIConfiguration,
    AIConfigurationProvider,
    ModelSelectionMode,
    ModelSelectionPolicy,
)
from backend.settings.secrets import MemorySecretStore
from backend.settings.service import (
    SettingsService,
    SettingsValidationError,
    ai_configuration_secret_name,
)
from backend.settings.store import (
    LEGACY_AI_CONFIGURATION_IDS,
    SettingsRepository,
)


def _service(tmp_path: Path) -> tuple[SettingsService, MemorySecretStore]:
    secrets = MemorySecretStore()
    return (
        SettingsService(
            SettingsRepository(tmp_path / "settings.json"),
            secrets,
            environment={},
        ),
        secrets,
    )


def _cloud_configuration(
    configuration_id: str,
    name: str,
) -> AIConfiguration:
    return AIConfiguration(
        id=configuration_id,
        name=name,
        provider=AIConfigurationProvider.openai,
        model="gpt-5.6-terra",
        base_url="https://api.openai.com/v1",
    )


def test_provider_catalog_keeps_required_product_order():
    assert [item.provider.value for item in PROVIDER_SPECS] == [
        "codex",
        "claude",
        "openai",
        "gemini",
        "minimax",
        "kimi",
        "deepseek",
        "qwen",
        "doubao",
        "wenxin",
        "hunyuan",
    ]


def test_v2_migration_creates_stable_legacy_configurations(tmp_path: Path):
    payload = default_settings().model_dump(mode="json")
    payload["schema_version"] = 2
    payload["ai"].pop("configurations")
    payload["ai"].pop("test_case_policies")
    (tmp_path / "settings.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    service, _secrets = _service(tmp_path)

    first = service.load()
    second = service.load()

    assert first.schema_version == 5
    assert [item.id for item in first.ai.configurations] == [
        LEGACY_AI_CONFIGURATION_IDS["codex"],
        LEGACY_AI_CONFIGURATION_IDS["minimax"],
        LEGACY_AI_CONFIGURATION_IDS["openai_compatible"],
    ]
    assert first.ai.configurations == second.ai.configurations


def test_legacy_secret_migration_copies_without_deleting_old_key(tmp_path: Path):
    payload = default_settings().model_dump(mode="json")
    payload["schema_version"] = 2
    payload["ai"].pop("configurations")
    payload["ai"].pop("test_case_policies")
    (tmp_path / "settings.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    service, secrets = _service(tmp_path)
    secrets.set("minimax_api_key", "legacy-secret-value")

    service.migrate_legacy_ai_secrets()

    dynamic_name = ai_configuration_secret_name(
        LEGACY_AI_CONFIGURATION_IDS["minimax"]
    )
    assert secrets.get(dynamic_name) == "legacy-secret-value"
    assert secrets.get("minimax_api_key") == "legacy-secret-value"
    assert "legacy-secret-value" not in (tmp_path / "settings.json").read_text(
        encoding="utf-8"
    )


def test_configuration_crud_order_status_and_recycle_are_atomic(tmp_path: Path):
    service, secrets = _service(tmp_path)
    first = _cloud_configuration("openai-one", "OpenAI 主模型")
    second = AIConfiguration(
        id="codex-local",
        name="Codex 本地",
        provider=AIConfigurationProvider.codex,
        model="gpt-5.6-terra",
    )

    service.save_ai_configuration(first, api_key="sk-test-secret-value")
    service.save_ai_configuration(second)
    assert service.ai_configuration_is_complete(first)
    service.reorder_ai_configurations((second.id, first.id))
    assert [item.id for item in service.load().ai.configurations] == [
        second.id,
        first.id,
    ]

    service.record_ai_configuration_status(
        first.id,
        ok=False,
        detail="request failed token=sk-test-secret-value",
    )
    checked = next(
        item for item in service.load().ai.configurations if item.id == first.id
    )
    assert checked.status.value == "error"
    assert "sk-test-secret-value" not in checked.status_detail

    service.trash_ai_configuration(first.id)
    assert not service.ai_configuration_is_complete(
        next(
            item
            for item in service.load().ai.configurations
            if item.id == first.id
        )
    )
    service.restore_ai_configuration(first.id)
    assert service.ai_configuration_secret_status(first.id).configured
    service.trash_ai_configuration(first.id)
    service.purge_ai_configuration(first.id)
    assert first.id not in {item.id for item in service.load().ai.configurations}
    assert secrets.get(ai_configuration_secret_name(first.id)) is None


def test_purge_removes_only_target_from_custom_stage_policy(tmp_path: Path):
    service, _secrets = _service(tmp_path)
    first = _cloud_configuration("first", "第一个")
    second = _cloud_configuration("second", "第二个")
    service.save_ai_configuration(first, api_key="key-first")
    service.save_ai_configuration(second, api_key="key-second")
    current = service.load()
    policy = ModelSelectionPolicy(
        mode=ModelSelectionMode.custom,
        configuration_ids=(first.id, second.id),
    )
    policies = current.ai.test_case_policies.model_copy(
        update={"component_matching": policy}
    )
    service.repository.save(
        current.model_copy(
            update={
                "ai": current.ai.model_copy(
                    update={"test_case_policies": policies}
                )
            }
        )
    )

    service.trash_ai_configuration(first.id)
    service.purge_ai_configuration(first.id)

    saved = service.load().ai.test_case_policies.component_matching
    assert saved.mode == ModelSelectionMode.custom
    assert saved.configuration_ids == (second.id,)


def test_stage_policy_only_accepts_complete_capability_matching_configs(tmp_path: Path):
    service, _secrets = _service(tmp_path)
    text_only = _cloud_configuration("text-only", "纯文本").model_copy(
        update={"vision_enabled": False}
    )
    service.save_ai_configuration(text_only)
    policy = ModelSelectionPolicy(
        mode=ModelSelectionMode.custom,
        configuration_ids=(text_only.id,),
    )

    try:
        service.save_test_case_model_policy("component_matching", policy)
    except SettingsValidationError as exc:
        assert "已完成" in str(exc)
    else:
        raise AssertionError("缺少密钥的配置不应进入模型策略")

    service.save_ai_configuration(text_only, api_key="text-only-key")
    service.save_test_case_model_policy("component_matching", policy)
    try:
        service.save_test_case_model_policy("image_understanding", policy)
    except SettingsValidationError as exc:
        assert "视觉模型" in str(exc)
    else:
        raise AssertionError("纯文本模型不应进入图片理解策略")


def test_saved_ai_key_cannot_be_rebound_to_a_new_endpoint(tmp_path: Path) -> None:
    service, secrets = _service(tmp_path)
    original = _cloud_configuration("bound-key", "主模型")
    service.save_ai_configuration(original, api_key="original-secret")

    with pytest.raises(SettingsValidationError, match="重新输入 API Key"):
        service.save_ai_configuration(
            original.model_copy(update={"base_url": "https://gateway.example.test/v1"})
        )

    stored = next(item for item in service.load().ai.configurations if item.id == original.id)
    assert stored.base_url == original.base_url
    assert secrets.get(ai_configuration_secret_name(original.id)) == "original-secret"

    renamed = original.model_copy(update={"name": "主模型（重命名）"})
    service.save_ai_configuration(renamed)
    assert next(
        item for item in service.load().ai.configurations if item.id == original.id
    ).name == renamed.name
