from string import Formatter

from backend.settings.defaults import (
    DEFAULT_PROMPTS,
    OPENAI_COMPATIBLE_PRESET_MODELS,
    OPENAI_COMPATIBLE_PRESET_RESPONSE_FORMATS,
    OPENAI_COMPATIBLE_PRESET_URLS,
    OPENAI_COMPATIBLE_PRESET_VISION,
    PROMPT_VARIABLES,
)


class PromptValidationError(ValueError):
    """Raised when an editable prompt violates its variable contract."""


class PromptCatalog:
    @staticmethod
    def variables(template: str) -> set[str]:
        formatter = Formatter()
        variables: set[str] = set()

        def collect(fragment: str) -> None:
            try:
                parsed = list(formatter.parse(fragment))
            except ValueError as exc:
                raise PromptValidationError(f"提示词大括号格式错误: {exc}") from exc

            for _, field_name, format_spec, _ in parsed:
                if field_name is None:
                    continue
                if not field_name:
                    raise PromptValidationError("提示词不支持空位置变量")
                variables.add(field_name)
                if format_spec:
                    collect(format_spec)

        collect(template)
        return variables

    @classmethod
    def validate(cls, name: str, template: str) -> None:
        contract = PROMPT_VARIABLES[name]
        actual = cls.variables(template)
        allowed = contract["required"] | contract["optional"]
        unknown = actual - allowed
        missing = contract["required"] - actual
        if unknown or missing:
            raise PromptValidationError(
                f"{name}: 未知变量={sorted(unknown)}, 缺少变量={sorted(missing)}"
            )

    @classmethod
    def render(cls, name: str, template: str, **values: object) -> str:
        cls.validate(name, template)
        expected = cls.variables(template)
        missing_values = expected - values.keys()
        if missing_values:
            raise PromptValidationError(f"{name}: 未提供值={sorted(missing_values)}")
        try:
            return template.format(**{key: values[key] for key in expected})
        except (KeyError, IndexError, ValueError) as exc:
            raise PromptValidationError(f"{name}: 提示词格式化失败: {exc}") from exc
