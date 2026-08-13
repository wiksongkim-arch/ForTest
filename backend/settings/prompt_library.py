from __future__ import annotations

from uuid import UUID, uuid5

from backend.settings.defaults import DEFAULT_PROMPTS
from backend.settings.models import (
    AppSettings,
    PromptCustomOption,
    PromptLibrarySettings,
    PromptSettings,
    PromptSlot,
)


DEFAULT_PROMPT_OPTION_ID = "default"
PROMPT_NAMES = tuple(DEFAULT_PROMPTS)
_LEGACY_OPTION_NAMESPACE = UUID("173b46e3-2824-48c1-951b-1c03730976d4")
_LEGACY_OPTION_NAME = "旧版自定义"


def require_prompt_name(name: str) -> str:
    if name not in PROMPT_NAMES:
        raise KeyError(name)
    return name


def legacy_option_id(prompt_name: str) -> str:
    """Return the deterministic ID reserved for pre-v2 prompt values."""

    require_prompt_name(prompt_name)
    return f"legacy-{uuid5(_LEGACY_OPTION_NAMESPACE, prompt_name)}"


def legacy_custom_option(
    prompt_name: str,
    content: str,
    *,
    name: str = _LEGACY_OPTION_NAME,
) -> PromptCustomOption:
    return PromptCustomOption(
        id=legacy_option_id(prompt_name),
        name=name,
        content=content,
    )


def resolve_prompt_content(prompt_name: str, slot: PromptSlot) -> str:
    require_prompt_name(prompt_name)
    if slot.selected_option_id == DEFAULT_PROMPT_OPTION_ID:
        return DEFAULT_PROMPTS[prompt_name]
    for option in slot.custom_options:
        if option.id == slot.selected_option_id:
            return option.content
    # PromptSlot normally prevents this state. Keep this guard close to the
    # projection boundary so corrupt data cannot silently use a wrong prompt.
    raise ValueError(
        f"{prompt_name}: 当前提示词选项不存在: {slot.selected_option_id}"
    )


def resolve_prompt_settings(library: PromptLibrarySettings) -> PromptSettings:
    return PromptSettings(
        **{
            prompt_name: resolve_prompt_content(
                prompt_name,
                getattr(library, prompt_name),
            )
            for prompt_name in PROMPT_NAMES
        }
    )


def materialize_settings(settings: AppSettings) -> AppSettings:
    """Refresh the runtime-only effective prompt projection."""

    return settings.model_copy(
        update={"prompts": resolve_prompt_settings(settings.prompt_library)}
    )
