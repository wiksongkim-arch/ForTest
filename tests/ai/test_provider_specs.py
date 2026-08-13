"""官方模型能力目录与请求参数映射回归。"""

from __future__ import annotations

from backend.ai.openai_compatible_provider import OpenAICompatibleProvider
from backend.ai.provider_specs import (
    PROVIDER_SPEC_BY_ID,
    openai_compatible_request_options,
    provider_model_capabilities,
)
from backend.settings.models import (
    AIConfiguration,
    AIConfigurationProvider,
    OpenAICompatibleSettings,
)


def _capabilities(provider: str, model: str) -> tuple[list[str], list[str]]:
    view = provider_model_capabilities(AIConfigurationProvider(provider), model)
    return view["reasoning_efforts"], view["inference_speeds"]


def _configuration(
    provider: AIConfigurationProvider,
    model: str,
    *,
    effort: str = "high",
    speed: str = "standard",
) -> AIConfiguration:
    spec = PROVIDER_SPEC_BY_ID[provider]
    return AIConfiguration(
        id=f"{provider.value}-configuration",
        name=spec.label,
        provider=provider,
        model=model,
        base_url=spec.base_url,
        reasoning_effort=effort,
        inference_speed=speed,
    )


def test_codex_is_named_cli_and_never_requires_an_api_key():
    spec = PROVIDER_SPEC_BY_ID[AIConfigurationProvider.codex]

    assert spec.label == "Codex CLI"
    assert spec.requires_api_key is False
    assert _capabilities("codex", "gpt-5.6-terra") == (
        ["none", "low", "medium", "high", "xhigh", "max", "ultra"],
        ["standard", "fast"],
    )


def test_non_codex_controls_only_appear_for_documented_models():
    # MiniMax、KIMI 当前通过模型选择提供不同推理形态，不提供这两个请求参数。
    assert _capabilities("minimax", "MiniMax-M2.7") == ([], [])
    assert _capabilities("kimi", "kimi-k2.6") == ([], [])

    assert _capabilities("deepseek", "deepseek-v4-pro") == (
        ["high", "max"],
        [],
    )
    assert _capabilities("qwen", "qwen3.7-plus") == ([], [])
    assert _capabilities("qwen", "qwen3.8-max-preview") == (
        ["none", "low", "medium", "xhigh"],
        [],
    )
    assert _capabilities("doubao", "doubao-seed-2-0-lite-260215") == (
        ["low", "medium", "high"],
        ["standard", "fast"],
    )
    assert _capabilities("wenxin", "ernie-5.0") == ([], [])
    assert _capabilities("wenxin", "deepseek-v4-flash") == (
        ["high", "max"],
        [],
    )
    assert _capabilities("hunyuan", "hy3") == (
        ["none", "low", "high"],
        [],
    )


def test_documented_capabilities_map_to_each_vendor_chat_payload():
    deepseek = _configuration(
        AIConfigurationProvider.deepseek,
        "deepseek-v4-pro",
        effort="max",
    )
    assert openai_compatible_request_options(deepseek) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }

    qwen = _configuration(
        AIConfigurationProvider.qwen,
        "qwen3.8-max-preview",
        effort="none",
    )
    assert openai_compatible_request_options(qwen) == {
        "enable_thinking": False
    }

    doubao = _configuration(
        AIConfigurationProvider.doubao,
        "doubao-seed-2-0-lite-260215",
        effort="medium",
        speed="fast",
    )
    assert openai_compatible_request_options(doubao) == {
        "reasoning_effort": "medium",
        "service_tier": "auto",
    }

    wenxin = _configuration(
        AIConfigurationProvider.wenxin,
        "deepseek-v4-flash",
        effort="high",
    )
    assert openai_compatible_request_options(wenxin) == {
        "reasoning_effort": "high"
    }

    hunyuan = _configuration(
        AIConfigurationProvider.hunyuan,
        "hy3",
        effort="none",
    )
    assert openai_compatible_request_options(hunyuan) == {}


def test_unknown_or_unsupported_models_emit_no_invented_options():
    for provider, model in (
        (AIConfigurationProvider.minimax, "MiniMax-M2.7"),
        (AIConfigurationProvider.kimi, "kimi-k2.6"),
        (AIConfigurationProvider.qwen, "qwen3.7-plus"),
        (AIConfigurationProvider.wenxin, "ernie-5.0"),
    ):
        assert openai_compatible_request_options(
            _configuration(provider, model)
        ) == {}


def test_openai_compatible_payload_only_accepts_capability_whitelist():
    provider = OpenAICompatibleProvider(
        OpenAICompatibleSettings(
            base_url="https://api.example.test/v1",
            model="safe-model",
        ),
        "secret",
        request_options={
            "reasoning_effort": "high",
            "service_tier": "auto",
            "model": "attacker-model",
            "messages": [{"role": "user", "content": "attacker"}],
        },
    )

    payload = provider._payload(
        "component_matching",
        [{"role": "user", "content": "real"}],
        {"type": "object", "properties": {}},
    )
    provider.close()

    assert payload["model"] == "safe-model"
    assert payload["messages"] == [{"role": "user", "content": "real"}]
    assert payload["reasoning_effort"] == "high"
    assert payload["service_tier"] == "auto"
