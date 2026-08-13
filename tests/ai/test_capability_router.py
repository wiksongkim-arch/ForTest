"""步骤级能力路由、配置顺序与回退测试。"""

from __future__ import annotations

from pathlib import Path

from backend.ai.capability_router import CapabilityRouterProvider
from backend.ai.types import SectionAIRequest, StageEvidence
from backend.settings.defaults import default_settings
from backend.settings.models import (
    AIConfiguration,
    AIConfigurationProvider,
    ModelSelectionMode,
    ModelSelectionPolicy,
    ProviderName,
    ResolvedSecrets,
    SettingsSnapshot,
)


def _configuration(
    configuration_id: str,
    name: str,
    provider: AIConfigurationProvider,
    *,
    vision: bool = True,
) -> AIConfiguration:
    return AIConfiguration(
        id=configuration_id,
        name=name,
        provider=provider,
        model=f"{provider.value}-model",
        base_url=f"https://{provider.value}.example.test/v1",
        vision_enabled=vision,
    )


def _snapshot(
    configurations: tuple[AIConfiguration, ...],
    *,
    component_policy: ModelSelectionPolicy | None = None,
) -> SettingsSnapshot:
    settings = default_settings()
    policies = settings.ai.test_case_policies
    if component_policy is not None:
        policies = policies.model_copy(
            update={"component_matching": component_policy}
        )
    settings = settings.model_copy(
        update={
            "ai": settings.ai.model_copy(
                update={
                    "configurations": configurations,
                    "test_case_policies": policies,
                }
            )
        }
    )
    return SettingsSnapshot(
        settings=settings,
        secrets=ResolvedSecrets(
            ai_configuration_api_keys={item.id: f"key-{item.id}" for item in configurations}
        ),
    )


def _request(*, images: tuple[Path, ...] = ()) -> SectionAIRequest:
    settings = default_settings()
    return SectionAIRequest(
        section_title="登录",
        section_content="点击登录按钮后进入首页。",
        images=images,
        component_names=("按钮",),
        field_specs={
            "module": "所属模块",
            "case_name": "用例名称",
            "prerequisite": "前置条件",
            "test_steps": "测试步骤",
            "expected_result": "预期结果",
            "priority": "优先级",
            "case_type": "用例类型",
            "applicable_phase": "适用阶段",
            "remark": "备注",
            "case_id": "用例编号",
            "execution": "执行状态",
        },
        component_templates={"按钮": []},
        prompts=settings.prompts.model_dump(),
    )


def _case() -> dict[str, str]:
    return {
        "module": "登录",
        "case_name": "登录成功",
        "prerequisite": "账号可用",
        "test_steps": "1. 点击登录",
        "expected_result": "1. 进入首页",
        "priority": "P0",
        "case_type": "功能测试",
        "applicable_phase": "冒烟",
        "remark": "",
        "case_id": "TC-001",
        "execution": "未执行",
    }


class FakeStageClient:
    def __init__(self, configuration: AIConfiguration, failures: set[str], calls: list):
        self.configuration = configuration
        self.failures = failures
        self.calls = calls
        self.closed = False

    def run_structured_stage(
        self,
        stage,
        _system,
        _user,
        _schema,
        *,
        images=(),
    ):
        self.calls.append((self.configuration.id, stage, bool(images)))
        if stage in self.failures:
            raise RuntimeError("secret-bearing provider failure")
        payload = {
            "image_analysis": {"image_findings": ["发现按钮"]},
            "component_matching": {"matched_components": ["按钮"]},
            "case_generation": {"test_cases": [_case()]},
        }[stage]
        return payload, StageEvidence(
            stage=stage,
            provider=ProviderName(self.configuration.provider.value),
            runtime_mode="http",
            model=self.configuration.model,
            duration_ms=1,
            retry_count=0,
            output_valid=True,
        )

    def cancel(self):
        return None

    def close(self):
        self.closed = True


def test_ordered_policy_falls_back_per_stage_and_records_actual_configuration():
    first = _configuration("first", "主模型", AIConfigurationProvider.openai)
    second = _configuration("second", "备用模型", AIConfigurationProvider.deepseek)
    calls = []

    def factory(configuration, _key):
        failures = {"component_matching"} if configuration.id == first.id else set()
        return FakeStageClient(configuration, failures, calls)

    router = CapabilityRouterProvider(
        _snapshot((first, second)),
        client_factory=factory,
    )
    result = router.process_section(_request())
    router.close()

    assert result.provider == ProviderName.mixed
    assert result.matched_components == ["按钮"]
    assert result.test_cases == [_case()]
    assert [item.configuration_id for item in result.evidence] == [
        second.id,
        first.id,
    ]
    assert "回退 1 次" in result.evidence[0].detail
    assert result.evidence[0].provider == ProviderName.deepseek
    assert result.evidence[1].provider == ProviderName.openai


def test_custom_policy_changes_component_priority_without_affecting_case_stage():
    first = _configuration("first", "主模型", AIConfigurationProvider.openai)
    second = _configuration("second", "自定义首选", AIConfigurationProvider.deepseek)
    policy = ModelSelectionPolicy(
        mode=ModelSelectionMode.custom,
        configuration_ids=(second.id, first.id),
    )
    calls = []
    router = CapabilityRouterProvider(
        _snapshot((first, second), component_policy=policy),
        client_factory=lambda configuration, _key: FakeStageClient(
            configuration,
            set(),
            calls,
        ),
    )

    result = router.process_section(_request())

    assert result.evidence[0].configuration_id == second.id
    assert result.evidence[1].configuration_id == first.id


def test_image_stage_skips_text_only_configuration(tmp_path: Path):
    image = tmp_path / "screen.png"
    image.write_bytes(b"png")
    text_only = _configuration(
        "text-only",
        "纯文本",
        AIConfigurationProvider.deepseek,
        vision=False,
    )
    vision = _configuration(
        "vision",
        "视觉",
        AIConfigurationProvider.openai,
        vision=True,
    )
    calls = []
    router = CapabilityRouterProvider(
        _snapshot((text_only, vision)),
        client_factory=lambda configuration, _key: FakeStageClient(
            configuration,
            set(),
            calls,
        ),
    )

    result = router.process_section(_request(images=(image,)))

    assert result.evidence[0].stage == "image_analysis"
    assert result.evidence[0].configuration_id == vision.id
    assert (text_only.id, "image_analysis", True) not in calls
