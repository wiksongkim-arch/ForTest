from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.settings.models import ProviderName


TEST_CASE_FIELDS = (
    "module",
    "case_name",
    "prerequisite",
    "test_steps",
    "expected_result",
    "priority",
    "case_type",
    "applicable_phase",
    "remark",
    "case_id",
    "execution",
)

TEST_CASE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {name: {"type": "string"} for name in TEST_CASE_FIELDS},
    "required": list(TEST_CASE_FIELDS),
    "additionalProperties": False,
}

CASE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "test_cases": {
            "type": "array",
            "items": TEST_CASE_ITEM_SCHEMA,
        },
    },
    "required": ["test_cases"],
    "additionalProperties": False,
}

SECTION_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "image_findings": {"type": "array", "items": {"type": "string"}},
        "matched_components": {
            "type": "array",
            "items": {"type": "string"},
        },
        "test_cases": {
            "type": "array",
            "items": TEST_CASE_ITEM_SCHEMA,
        },
    },
    "required": ["image_findings", "matched_components", "test_cases"],
    "additionalProperties": False,
}

IMAGE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "image_findings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["image_findings"],
    "additionalProperties": False,
}

COMPONENT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "matched_components": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["matched_components"],
    "additionalProperties": False,
}


def _deny_mutation(*args: Any, **kwargs: Any) -> None:
    raise TypeError("Frozen request data cannot be modified.")


class _FrozenDict(dict):
    __setitem__ = _deny_mutation
    __delitem__ = _deny_mutation
    clear = _deny_mutation
    pop = _deny_mutation
    popitem = _deny_mutation
    setdefault = _deny_mutation
    update = _deny_mutation
    __ior__ = _deny_mutation


class _FrozenList(list):
    __setitem__ = _deny_mutation
    __delitem__ = _deny_mutation
    append = _deny_mutation
    clear = _deny_mutation
    extend = _deny_mutation
    insert = _deny_mutation
    pop = _deny_mutation
    remove = _deny_mutation
    reverse = _deny_mutation
    sort = _deny_mutation
    __iadd__ = _deny_mutation
    __imul__ = _deny_mutation


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenDict(
            (key, _deep_freeze(nested)) for key, nested in value.items()
        )
    if isinstance(value, list):
        return _FrozenList(_deep_freeze(nested) for nested in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(nested) for nested in value)
    return value


@dataclass(frozen=True)
class SectionAIRequest:
    section_title: str
    section_content: str
    images: tuple[Path, ...]
    component_names: tuple[str, ...]
    field_specs: dict[str, str]
    component_templates: dict[str, list[dict[str, str]]]
    prompts: dict[str, str]
    output_schema: Mapping[str, Any] = field(
        default_factory=lambda: CASE_OUTPUT_SCHEMA
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "images", tuple(self.images))
        object.__setattr__(
            self,
            "component_names",
            tuple(self.component_names),
        )
        object.__setattr__(self, "field_specs", _deep_freeze(self.field_specs))
        object.__setattr__(
            self,
            "component_templates",
            _deep_freeze(self.component_templates),
        )
        object.__setattr__(self, "prompts", _deep_freeze(self.prompts))
        object.__setattr__(
            self,
            "output_schema",
            _deep_freeze(self.output_schema),
        )


@dataclass(frozen=True)
class StageEvidence:
    stage: str
    provider: ProviderName
    runtime_mode: str
    model: str
    duration_ms: int
    retry_count: int
    output_valid: bool
    turn_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    detail: str = ""
    configuration_id: str | None = None
    configuration_name: str | None = None


@dataclass
class ProviderHealth:
    ok: bool
    provider: ProviderName
    detail: str
    runtime_mode: str = ""


@dataclass
class SectionAIResult:
    provider: ProviderName
    runtime_mode: str
    model: str
    duration_ms: int
    retry_count: int
    output_valid: bool
    image_findings: list[str] = field(default_factory=list)
    matched_components: list[str] = field(default_factory=list)
    test_cases: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[StageEvidence] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderUsage:
    provider: ProviderName
    runtime_mode: str
    model: str
    total_sections: int
    ai_case_count: int
    fallback_count: int
    duration_ms: int
    retry_count: int
