from copy import deepcopy

from backend.settings.models import (
    AppSettings,
    DocumentSettings,
    PromptLibrarySettings,
    PromptSettings,
)


DEFAULT_PROMPTS = {
    "image_understanding": """你是测试需求图片分析助手。请分析区块“{section_title}”附带的 {image_count} 张图片。
仅基于图片说明：可操作组件、字段级数据显示、布局层级、交互状态和与测试有关的异常提示。
不要猜测图片中不存在的功能；每张图片给出一条不超过 500 字的简洁发现。""",
    "component_matching": """你是测试用例规划助手。根据需求，从给定组件名称中选择确实相关的组件。

需求内容：
{requirement}

可用组件：
{component_names}

只返回列表中存在的组件。可用组件列表非空时，即使没有完全同名的组件，也必须选择语义最接近、范围最宽的一个组件；仅当可用组件列表本身为空时返回空数组。""",
    "case_generation_system": """你是面向测试工程师的测试用例生成助手。
只能依据给定 PRD 区块、图片发现、字段规范和匹配模板生成用例，不评价需求合理性，也不补造产品规则。
覆盖需求暗示的正常、边界、异常、权限、兼容性、UI 与回归场景。
保留原始产品术语、提示文案、角色、状态和编号流程。
用例必须达到可直接执行的颗粒度：前置条件具体，步骤逐条编号，预期结果与步骤逐项对应且可观察。
不要复述需求或生成“进入页面、验证功能正常”一类笼统用例。

字段规范：
{field_specs}

输出必须严格符合给定 JSON Schema。""",
    "case_generation_user": """为以下 PRD 区块生成测试用例。

区块标题：{section_title}
区块正文：
{section_content}

图片关键发现：
{image_findings}

匹配组件：
{matched_components}

可参考模板：
{matched_templates}

每个明确字段、筛选条件、按钮、状态、角色差异、校验分支和匹配模板条目都要单独覆盖，不要把多个验证点合并成一条。
module 使用当前区块内最细粒度的功能名称，不得包含 Markdown、HTML 或图片链接。
priority 仅使用 P0/P1/P2：核心主流程、关键数据正确性和关键权限使用 P0，重要分支使用 P1，低风险界面与兼容性使用 P2。case_type 仅使用功能测试、边界测试、异常测试、界面测试、权限测试、数据测试、安全测试、性能测试、兼容性测试。execution 默认“未执行”；case_id 最终会统一重排。
每个用例必须填写 module、case_name、prerequisite、test_steps、expected_result、priority、case_type、applicable_phase、remark、case_id、execution。""",
}

OPENAI_COMPATIBLE_PRESET_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
}
OPENAI_COMPATIBLE_PRESET_MODELS = {
    "openai": "gpt-5.4",
    "deepseek": "deepseek-v4-flash",
}
OPENAI_COMPATIBLE_PRESET_RESPONSE_FORMATS = {
    "openai": "json_schema",
    "deepseek": "json_object",
}
OPENAI_COMPATIBLE_PRESET_VISION = {
    "openai": True,
    "deepseek": False,
}

PROMPT_VARIABLES = {
    "image_understanding": {
        "optional": {"section_title", "image_count"},
        "required": set(),
    },
    "component_matching": {
        "optional": set(),
        "required": {"requirement", "component_names"},
    },
    "case_generation_system": {
        "optional": set(),
        "required": {"field_specs"},
    },
    "case_generation_user": {
        "optional": set(),
        "required": {
            "section_title",
            "section_content",
            "image_findings",
            "matched_components",
            "matched_templates",
        },
    },
}


def default_settings() -> AppSettings:
    return AppSettings(
        document=DocumentSettings(
            # 业务模板属于用户数据，安装包和源码默认值不得携带实际地址。
            content_template_url="",
            document_template_url="",
            output_folder_url="",
            local_output_dir="./output",
        ),
        prompt_library=PromptLibrarySettings(),
        prompts=PromptSettings(**deepcopy(DEFAULT_PROMPTS)),
    )
