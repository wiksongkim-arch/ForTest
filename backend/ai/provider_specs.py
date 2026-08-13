"""设置中心支持的 AI 厂商目录。

这里只保存公开的官方端点与默认模型，不保存任何账号、业务地址或凭据。
厂商顺序是界面和“按配置顺序”之外的稳定展示约定，不能由字典排序替代。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from backend.settings.models import (
    AIConfiguration,
    AIConfigurationProvider,
    AIProtocol,
    ResponseFormatMode,
)


@dataclass(frozen=True)
class ModelCapabilityRule:
    """按模型前缀声明官方已确认的可调推理参数。"""

    model_prefixes: tuple[str, ...]
    reasoning_efforts: tuple[str, ...] = ()
    inference_speeds: tuple[str, ...] = ()
    default_reasoning_effort: str = "high"
    default_inference_speed: str = "standard"

    def matches(self, model: str) -> bool:
        normalized = str(model or "").strip().casefold()
        return any(
            not prefix or normalized.startswith(prefix.casefold())
            for prefix in self.model_prefixes
        )

    def view(self) -> dict:
        return {
            "model_prefixes": list(self.model_prefixes),
            "reasoning_efforts": list(self.reasoning_efforts),
            "inference_speeds": list(self.inference_speeds),
            "default_reasoning_effort": self.default_reasoning_effort,
            "default_inference_speed": self.default_inference_speed,
        }


@dataclass(frozen=True)
class ProviderSpec:
    provider: AIConfigurationProvider
    label: str
    protocol: AIProtocol
    base_url: str
    default_model: str
    vision_enabled: bool
    response_format_mode: ResponseFormatMode = ResponseFormatMode.json_schema
    requires_api_key: bool = True
    documentation_url: str = ""
    capability_rules: tuple[ModelCapabilityRule, ...] = ()

    def view(self) -> dict:
        value = asdict(self)
        value["provider"] = self.provider.value
        value["protocol"] = self.protocol.value
        value["response_format_mode"] = self.response_format_mode.value
        value["capability_rules"] = [
            rule.view() for rule in self.capability_rules
        ]
        return value

    def capabilities_for(self, model: str) -> dict:
        """返回首条匹配规则，未知模型绝不臆测支持厂商参数。"""

        rule = next(
            (item for item in self.capability_rules if item.matches(model)),
            None,
        )
        return rule.view() if rule is not None else {
            "model_prefixes": [],
            "reasoning_efforts": [],
            "inference_speeds": [],
            "default_reasoning_effort": "high",
            "default_inference_speed": "standard",
        }


# 默认值按 2026-08-10 各厂商官方文档整理；模型 ID 可在配置弹窗中修改，
# 在线目录检测成功时以账号实际可见列表为准。
PROVIDER_SPECS = (
    ProviderSpec(
        AIConfigurationProvider.codex,
        "Codex CLI",
        AIProtocol.codex,
        "",
        "gpt-5.6-terra",
        True,
        requires_api_key=False,
        documentation_url="https://developers.openai.com/codex/",
        capability_rules=(
            ModelCapabilityRule(
                model_prefixes=("",),
                reasoning_efforts=(
                    "none",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                    "ultra",
                ),
                inference_speeds=("standard", "fast"),
                default_reasoning_effort="high",
            ),
        ),
    ),
    ProviderSpec(
        AIConfigurationProvider.claude,
        "Claude",
        AIProtocol.anthropic,
        "https://api.anthropic.com",
        "claude-sonnet-5",
        True,
        documentation_url="https://platform.claude.com/docs/en/about-claude/models/overview",
    ),
    ProviderSpec(
        AIConfigurationProvider.openai,
        "OpenAI",
        AIProtocol.openai_compatible,
        "https://api.openai.com/v1",
        "gpt-5.6-terra",
        True,
        documentation_url="https://developers.openai.com/api/docs/models",
    ),
    ProviderSpec(
        AIConfigurationProvider.gemini,
        "Gemini",
        AIProtocol.gemini,
        "https://generativelanguage.googleapis.com/v1beta",
        "gemini-3.6-flash",
        True,
        documentation_url="https://ai.google.dev/gemini-api/docs/models",
    ),
    ProviderSpec(
        AIConfigurationProvider.minimax,
        "MiniMax",
        AIProtocol.openai_compatible,
        "https://api.minimaxi.com/v1",
        "MiniMax-M2.7",
        True,
        documentation_url="https://platform.minimax.io/docs/guides/models-intro",
    ),
    ProviderSpec(
        AIConfigurationProvider.kimi,
        "KIMI",
        AIProtocol.openai_compatible,
        "https://api.moonshot.ai/v1",
        "kimi-k2.6",
        True,
        documentation_url="https://platform.kimi.ai/docs/api/overview",
    ),
    ProviderSpec(
        AIConfigurationProvider.deepseek,
        "DeepSeek",
        AIProtocol.openai_compatible,
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        False,
        response_format_mode=ResponseFormatMode.json_object,
        documentation_url="https://api-docs.deepseek.com/",
        capability_rules=(
            ModelCapabilityRule(
                model_prefixes=("deepseek-v4-",),
                reasoning_efforts=("high", "max"),
                default_reasoning_effort="high",
            ),
        ),
    ),
    ProviderSpec(
        AIConfigurationProvider.qwen,
        "千问",
        AIProtocol.openai_compatible,
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen3.7-plus",
        True,
        documentation_url="https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
        capability_rules=(
            ModelCapabilityRule(
                model_prefixes=("qwen3.8-max-preview",),
                reasoning_efforts=("none", "low", "medium", "xhigh"),
                default_reasoning_effort="xhigh",
            ),
        ),
    ),
    ProviderSpec(
        AIConfigurationProvider.doubao,
        "豆包",
        AIProtocol.openai_compatible,
        "https://ark.cn-beijing.volces.com/api/v3",
        "doubao-seed-2-0-lite-260215",
        True,
        documentation_url="https://www.volcengine.com/docs/82379/1330626",
        capability_rules=(
            ModelCapabilityRule(
                model_prefixes=("doubao-seed-2-0",),
                reasoning_efforts=("low", "medium", "high"),
                inference_speeds=("standard", "fast"),
                default_reasoning_effort="medium",
            ),
        ),
    ),
    ProviderSpec(
        AIConfigurationProvider.wenxin,
        "文心一言",
        AIProtocol.openai_compatible,
        "https://qianfan.baidubce.com/v2",
        "ernie-5.0",
        True,
        documentation_url="https://cloud.baidu.com/doc/WENXINWORKSHOP/s/Fm2vrveyu",
        capability_rules=(
            ModelCapabilityRule(
                model_prefixes=("deepseek-v4-",),
                reasoning_efforts=("high", "max"),
                default_reasoning_effort="high",
            ),
        ),
    ),
    ProviderSpec(
        AIConfigurationProvider.hunyuan,
        "腾讯混元",
        AIProtocol.openai_compatible,
        "https://tokenhub.tencentmaas.com/v1",
        "hy3",
        True,
        documentation_url="https://cloud.tencent.com/document/product/1729",
        capability_rules=(
            ModelCapabilityRule(
                model_prefixes=("hy3",),
                reasoning_efforts=("none", "low", "high"),
                default_reasoning_effort="high",
            ),
        ),
    ),
)

PROVIDER_SPEC_BY_ID = {item.provider: item for item in PROVIDER_SPECS}


def provider_specs_view() -> list[dict]:
    """返回可直接用于新增配置弹窗的稳定有序目录。"""

    return [item.view() for item in PROVIDER_SPECS]


def provider_model_capabilities(
    provider: AIConfigurationProvider,
    model: str,
) -> dict:
    """供界面和请求层共享，避免两端各自猜测参数支持情况。"""

    return PROVIDER_SPEC_BY_ID[provider].capabilities_for(model)


def openai_compatible_request_options(
    configuration: AIConfiguration,
) -> dict[str, object]:
    """把通用配置映射为厂商官方 Chat API 请求参数白名单。"""

    capabilities = provider_model_capabilities(
        configuration.provider,
        configuration.model,
    )
    supported_efforts = tuple(capabilities["reasoning_efforts"])
    supported_speeds = tuple(capabilities["inference_speeds"])
    effort = configuration.reasoning_effort.value
    speed = configuration.inference_speed.value
    options: dict[str, object] = {}

    if supported_efforts:
        if effort not in supported_efforts:
            effort = str(capabilities["default_reasoning_effort"])
        if configuration.provider == AIConfigurationProvider.qwen:
            if effort == "none":
                options["enable_thinking"] = False
            else:
                options["reasoning_effort"] = effort
        elif configuration.provider == AIConfigurationProvider.deepseek:
            options["thinking"] = {"type": "enabled"}
            options["reasoning_effort"] = effort
        elif configuration.provider == AIConfigurationProvider.hunyuan:
            # Hy3 的 no_think 档位通过不发送 reasoning_effort 表示。
            if effort != "none":
                options["reasoning_effort"] = effort
        else:
            options["reasoning_effort"] = effort

    if supported_speeds:
        if speed not in supported_speeds:
            speed = str(capabilities["default_inference_speed"])
        if configuration.provider == AIConfigurationProvider.doubao:
            options["service_tier"] = (
                "auto" if speed == "fast" else "default"
            )
    return options
