from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class ProviderName(str, Enum):
    codex = "codex"
    claude = "claude"
    openai = "openai"
    gemini = "gemini"
    minimax = "minimax"
    kimi = "kimi"
    deepseek = "deepseek"
    qwen = "qwen"
    doubao = "doubao"
    wenxin = "wenxin"
    hunyuan = "hunyuan"
    openai_compatible = "openai_compatible"
    mixed = "mixed"


class AIConfigurationProvider(str, Enum):
    """设置中心可创建的 AI 能力提供方，顺序由官方目录统一维护。"""

    codex = "codex"
    claude = "claude"
    openai = "openai"
    gemini = "gemini"
    minimax = "minimax"
    kimi = "kimi"
    deepseek = "deepseek"
    qwen = "qwen"
    doubao = "doubao"
    wenxin = "wenxin"
    hunyuan = "hunyuan"


class AIProtocol(str, Enum):
    codex = "codex"
    anthropic = "anthropic"
    openai_compatible = "openai_compatible"
    gemini = "gemini"


class AIConfigurationStatus(str, Enum):
    unchecked = "unchecked"
    passed = "passed"
    error = "error"


class CodexCLISource(str, Enum):
    builtin = "builtin"
    custom = "custom"


class ModelSelectionMode(str, Enum):
    ordered = "ordered"
    custom = "custom"


class CodexRuntime(str, Enum):
    auto = "auto"
    sdk = "sdk"
    cli = "cli"


class ReasoningEffort(str, Enum):
    none = "none"
    low = "low"
    medium = "medium"
    high = "high"
    xhigh = "xhigh"
    max = "max"
    ultra = "ultra"


class CodexInferenceSpeed(str, Enum):
    standard = "standard"
    fast = "fast"


class OpenAICompatiblePreset(str, Enum):
    openai = "openai"
    deepseek = "deepseek"
    custom = "custom"


class ResponseFormatMode(str, Enum):
    json_schema = "json_schema"
    json_object = "json_object"


_PROVIDER_PROTOCOLS = {
    AIConfigurationProvider.codex: AIProtocol.codex,
    AIConfigurationProvider.claude: AIProtocol.anthropic,
    AIConfigurationProvider.gemini: AIProtocol.gemini,
    AIConfigurationProvider.openai: AIProtocol.openai_compatible,
    AIConfigurationProvider.minimax: AIProtocol.openai_compatible,
    AIConfigurationProvider.kimi: AIProtocol.openai_compatible,
    AIConfigurationProvider.deepseek: AIProtocol.openai_compatible,
    AIConfigurationProvider.qwen: AIProtocol.openai_compatible,
    AIConfigurationProvider.doubao: AIProtocol.openai_compatible,
    AIConfigurationProvider.wenxin: AIProtocol.openai_compatible,
    AIConfigurationProvider.hunyuan: AIProtocol.openai_compatible,
}


def _https_url(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("必须使用不含用户信息且具有主机名的 https:// URL")
    return value.rstrip("/")


def _https_url_or_empty(value: str) -> str:
    """允许新用户保持未配置；一旦填写仍执行严格 HTTPS 校验。"""

    return _https_url(value) if value.strip() else ""


def _non_empty(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("值不能为空")
    return value


class DocumentSettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    content_template_url: str
    document_template_url: str
    output_folder_url: str
    local_output_dir: str = "./output"

    _content_https = field_validator("content_template_url")(_https_url_or_empty)
    _document_https = field_validator("document_template_url")(_https_url_or_empty)
    _folder_https = field_validator("output_folder_url")(_https_url_or_empty)

    @field_validator("local_output_dir")
    @classmethod
    def validate_local_output_dir(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("本地备份目录不能为空")
        return str(Path(value))


class PromptSettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    image_understanding: str
    component_matching: str
    case_generation_system: str
    case_generation_user: str


class PromptCustomOption(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    content: str

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("提示词选项 ID 不能为空")
        if normalized == "default":
            raise ValueError("自定义提示词不能使用 default ID")
        return normalized

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("提示词名称不能为空")
        return normalized

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("提示词内容不能为空")
        return value


class PromptSlot(BaseModel):
    model_config = ConfigDict(frozen=True)
    selected_option_id: str = "default"
    custom_options: tuple[PromptCustomOption, ...] = ()

    @field_validator("selected_option_id")
    @classmethod
    def validate_selected_option_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("当前提示词选项 ID 不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_option_ids(self):
        option_ids = [option.id for option in self.custom_options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("同类提示词的自定义选项 ID 不能重复")
        if (
            self.selected_option_id != "default"
            and self.selected_option_id not in option_ids
        ):
            raise ValueError("当前提示词选项不存在")
        return self


class PromptLibrarySettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    image_understanding: PromptSlot = Field(default_factory=PromptSlot)
    component_matching: PromptSlot = Field(default_factory=PromptSlot)
    case_generation_system: PromptSlot = Field(default_factory=PromptSlot)
    case_generation_user: PromptSlot = Field(default_factory=PromptSlot)


class CodexSettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    runtime: CodexRuntime = CodexRuntime.auto
    model: str = "gpt-5.4"
    reasoning_effort: ReasoningEffort = ReasoningEffort.high
    inference_speed: CodexInferenceSpeed = CodexInferenceSpeed.standard
    timeout_seconds: int = Field(default=900, ge=1, le=3600)
    max_concurrency: int = Field(default=1, ge=1, le=4)
    cli_path: str | None = None
    use_dedicated_api_key: bool = False

    _model_non_empty = field_validator("model")(_non_empty)

    @field_validator("cli_path")
    @classmethod
    def validate_cli_path(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        path = Path(value).expanduser()
        if not path.is_file():
            raise ValueError("Codex CLI 路径必须指向现有文件")
        return str(path.resolve())


class MiniMaxSettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    base_url: str = "https://api.minimaxi.com"
    model: str = "MiniMax-M2.7"
    timeout_seconds: int = Field(default=300, ge=30, le=900)
    vision_enabled: bool = True

    _base_https = field_validator("base_url")(_https_url)
    _model_non_empty = field_validator("model")(_non_empty)


class OpenAICompatibleSettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    preset: OpenAICompatiblePreset = OpenAICompatiblePreset.openai
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5.4"
    response_format_mode: ResponseFormatMode = ResponseFormatMode.json_schema
    timeout_seconds: int = Field(default=300, ge=1, le=900)
    vision_enabled: bool = True

    _base_https = field_validator("base_url")(_https_url)
    _model_non_empty = field_validator("model")(_non_empty)


class AIConfiguration(BaseModel):
    """一条可排序、可检测且可软删除的 AI 能力配置。"""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    provider: AIConfigurationProvider
    protocol: AIProtocol = AIProtocol.openai_compatible
    model: str
    base_url: str = ""
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    vision_enabled: bool = True
    response_format_mode: ResponseFormatMode = ResponseFormatMode.json_schema
    reasoning_effort: ReasoningEffort = ReasoningEffort.high
    inference_speed: CodexInferenceSpeed = CodexInferenceSpeed.standard
    max_concurrency: int = Field(default=1, ge=1, le=4)
    codex_cli_source: CodexCLISource = CodexCLISource.builtin
    codex_cli_version: str = "bundled"
    codex_cli_path: str | None = None
    status: AIConfigurationStatus = AIConfigurationStatus.unchecked
    status_detail: str = ""
    checked_at: datetime | None = None
    deleted_at: datetime | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_protocol(cls, value):
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        try:
            provider = AIConfigurationProvider(normalized.get("provider"))
        except (TypeError, ValueError):
            return normalized
        # 协议由厂商决定，禁止磁盘手工修改后把密钥发往错误类型的端点。
        normalized["protocol"] = _PROVIDER_PROTOCOLS[provider].value
        return normalized

    @field_validator("name", "model")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        return _non_empty(value)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            raise ValueError("AI 配置 ID 无效")
        if any(
            not (character.isalnum() or character in {"-", "_", "."})
            for character in normalized
        ):
            raise ValueError("AI 配置 ID 只能包含字母、数字、点、横线和下划线")
        return normalized

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return _https_url(value) if value.strip() else ""

    @field_validator("codex_cli_path")
    @classmethod
    def normalize_cli_path(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return str(Path(value).expanduser())

    @model_validator(mode="after")
    def validate_provider_fields(self):
        expected_protocol = _PROVIDER_PROTOCOLS[self.provider]
        if self.protocol != expected_protocol:
            raise ValueError("AI 配置协议与厂商不匹配")
        if self.provider == AIConfigurationProvider.codex:
            if self.base_url:
                raise ValueError("Codex 配置不接受云端 base URL")
            if self.codex_cli_source == CodexCLISource.custom:
                if not self.codex_cli_path:
                    raise ValueError("自定义 Codex CLI 必须填写路径")
            elif not self.codex_cli_version.strip():
                raise ValueError("内置 Codex CLI 必须选择版本")
        elif not self.base_url:
            raise ValueError("云模型配置必须填写 base URL")
        return self

    def requires_api_key(self) -> bool:
        # Codex CLI 复用本机登录态，配置级 API Key 不属于桌面端契约。
        return self.provider != AIConfigurationProvider.codex


class ModelSelectionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: ModelSelectionMode = ModelSelectionMode.ordered
    configuration_ids: tuple[str, ...] = ()

    @field_validator("configuration_ids")
    @classmethod
    def validate_configuration_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value if item.strip())
        if len(normalized) != len(set(normalized)):
            raise ValueError("自定义模型顺序不能重复选择同一配置")
        return normalized

    @model_validator(mode="after")
    def validate_custom_selection(self):
        if self.mode == ModelSelectionMode.ordered and self.configuration_ids:
            raise ValueError("按配置顺序模式不能保存自定义模型列表")
        if self.mode == ModelSelectionMode.custom and not self.configuration_ids:
            raise ValueError("自定义模式至少需要选择一个模型")
        return self


class TestCaseModelPolicies(BaseModel):
    model_config = ConfigDict(frozen=True)

    image_understanding: ModelSelectionPolicy = Field(
        default_factory=ModelSelectionPolicy
    )
    component_matching: ModelSelectionPolicy = Field(
        default_factory=ModelSelectionPolicy
    )
    case_generation: ModelSelectionPolicy = Field(
        default_factory=ModelSelectionPolicy
    )


class AISettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    active_provider: ProviderName = ProviderName.codex
    fallback_enabled: bool = False
    fallback_provider: ProviderName | None = None
    codex: CodexSettings = Field(default_factory=CodexSettings)
    minimax: MiniMaxSettings = Field(default_factory=MiniMaxSettings)
    openai_compatible: OpenAICompatibleSettings = Field(
        default_factory=OpenAICompatibleSettings
    )
    configurations: tuple[AIConfiguration, ...] = ()
    test_case_policies: TestCaseModelPolicies = Field(
        default_factory=TestCaseModelPolicies
    )

    @model_validator(mode="after")
    def validate_fallback(self):
        if self.fallback_enabled:
            if self.fallback_provider is None:
                raise ValueError("启用回退时必须选择回退 Provider")
            if self.fallback_provider == self.active_provider:
                raise ValueError("回退 Provider 不能与活动 Provider 相同")
        configuration_ids = [item.id for item in self.configurations]
        if len(configuration_ids) != len(set(configuration_ids)):
            raise ValueError("AI 配置 ID 不能重复")
        active_names = [
            item.name.casefold()
            for item in self.configurations
            if item.deleted_at is None
        ]
        if len(active_names) != len(set(active_names)):
            raise ValueError("未删除的 AI 配置名称不能重复")
        known_ids = set(configuration_ids)
        for policy in (
            self.test_case_policies.image_understanding,
            self.test_case_policies.component_matching,
            self.test_case_policies.case_generation,
        ):
            if any(item not in known_ids for item in policy.configuration_ids):
                raise ValueError("模型策略引用了不存在的 AI 配置")
        return self


class AppSettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: int = Field(default=5, ge=5, le=5)
    document: DocumentSettings
    prompt_library: PromptLibrarySettings = Field(
        default_factory=PromptLibrarySettings
    )
    prompts: PromptSettings
    ai: AISettings = Field(default_factory=AISettings)


class ResolvedSecrets(BaseModel):
    """One task's resolved credentials; repr/JSON never reveal plaintext."""

    model_config = ConfigDict(frozen=True)
    document_mcp_url: SecretStr | None = Field(default=None, repr=False)
    spreadsheet_mcp_url: SecretStr | None = Field(default=None, repr=False)
    minimax_api_key: SecretStr | None = Field(default=None, repr=False)
    openai_compatible_api_key: SecretStr | None = Field(default=None, repr=False)
    codex_api_key: SecretStr | None = Field(default=None, repr=False)
    ai_configuration_api_keys: dict[str, SecretStr] = Field(
        default_factory=dict,
        repr=False,
    )

    def reveal(self, name: str) -> str | None:
        value = getattr(self, name)
        return value.get_secret_value() if value is not None else None

    def reveal_ai_configuration(self, configuration_id: str) -> str | None:
        value = self.ai_configuration_api_keys.get(configuration_id)
        return value.get_secret_value() if value is not None else None


class SettingsSnapshot(BaseModel):
    """Immutable task-scoped settings and credentials captured together."""

    model_config = ConfigDict(frozen=True)
    settings: AppSettings
    secrets: ResolvedSecrets = Field(repr=False)


class SecretStatus(BaseModel):
    configured: bool
    masked_value: str | None
    source: str
