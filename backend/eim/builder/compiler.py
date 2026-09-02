"""无动态执行能力的 EIM DSL 编译器。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from backend.eim.models import CanonicalEvent, EIMDSL, Extractor, FilterRule


_BASE_FIELDS = frozenset(
    {
        "event.id",
        "event.type",
        "message.id",
        "message.kind",
        "message.text",
        "conversation.id",
        "sender.id",
        "sender.name",
        "occurred_at",
        "quoted_message",
        "reaction.name",
        "reaction.text",
        "reaction.operation",
        "media",
    }
)
_FORBIDDEN_REGEX = re.compile(r"\(\?|\\[1-9]|[|{}]")


def source_fields() -> list[str]:
    """返回结构化编辑器和模型可引用的稳定来源字段。"""

    return sorted(_BASE_FIELDS)


@dataclass(frozen=True)
class CompiledDSL:
    dsl: EIMDSL
    regexes: tuple[re.Pattern[str] | None, ...]
    extractor_regexes: tuple[re.Pattern[str] | None, ...]

    def matches(self, event: CanonicalEvent) -> bool:
        """只执行触发器和过滤器，供媒体下载前先做廉价判定。"""

        if event.event_type not in self.dsl.triggers:
            return False
        context = _event_context(event)
        return all(
            _matches(rule, _get(context, rule.field), pattern)
            for rule, pattern in zip(self.dsl.filters, self.regexes, strict=True)
        )

    def execute(self, event: CanonicalEvent) -> dict[str, Any] | None:
        if not self.matches(event):
            return None
        context = _event_context(event)
        extracted: dict[str, Any] = {}
        context["extracted"] = extracted
        for extractor, pattern in zip(
            self.dsl.extractors, self.extractor_regexes, strict=True
        ):
            extracted[extractor.output] = _extract(extractor, context, pattern)
        return {
            target: _get(context, source)
            for target, source in self.dsl.mappings.items()
        }


def compile_dsl(
    value: EIMDSL | dict[str, Any],
    *,
    target_fields: set[str] | None = None,
) -> CompiledDSL:
    """在部署前一次性拒绝未知字段、危险正则和不存在的目标字段。"""

    dsl = value if isinstance(value, EIMDSL) else EIMDSL.model_validate(value)
    if len(set(dsl.triggers)) != len(dsl.triggers):
        raise ValueError("DSL triggers 不能重复")
    regexes: list[re.Pattern[str] | None] = []
    for rule in dsl.filters:
        _validate_source(rule.field, set())
        if rule.operator == "regex":
            if not isinstance(rule.value, str):
                raise ValueError("regex 过滤器的 value 必须是字符串")
            regexes.append(_compile_safe_regex(rule.value))
        else:
            if rule.operator == "in" and (
                not isinstance(rule.value, list) or len(rule.value) > 100
            ):
                raise ValueError("in 过滤器必须提供至多 100 项数组")
            regexes.append(None)

    extractor_names: set[str] = set()
    extractor_regexes: list[re.Pattern[str] | None] = []
    for extractor in dsl.extractors:
        if extractor.output in extractor_names:
            raise ValueError(f"提取字段重复：{extractor.output}")
        if extractor.source:
            _validate_source(extractor.source, extractor_names)
        if extractor.path:
            _validate_path(extractor.path)
        extractor_regexes.append(
            _compile_safe_regex(extractor.pattern or "")
            if extractor.kind == "regex"
            else None
        )
        extractor_names.add(extractor.output)

    for target, source in dsl.mappings.items():
        _validate_source(source, extractor_names)
        if target_fields is not None and target not in target_fields:
            raise ValueError(f"目标字段不存在或不可写：{target}")
    return CompiledDSL(dsl, tuple(regexes), tuple(extractor_regexes))


def dsl_to_text(value: EIMDSL) -> str:
    """JSON 是 YAML 1.2 的安全子集，可直接版本化且无需额外解析依赖。"""

    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False, indent=2)


def dsl_from_text(value: str) -> EIMDSL:
    if len(value.encode("utf-8")) > 1024 * 1024:
        raise ValueError("EIM DSL 超过 1 MiB")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"EIM DSL JSON 格式错误：第 {exc.lineno} 行") from None
    return EIMDSL.model_validate(parsed)


def _compile_safe_regex(value: str) -> re.Pattern[str]:
    if not value or len(value) > 500 or _FORBIDDEN_REGEX.search(value):
        raise ValueError("正则包含未允许的高风险结构")
    # Python re 没有执行超时；只允许一个作用于单个原子的量词，保证匹配线性可控。
    quantifiers = 0
    escaped = False
    in_class = False
    previous = ""
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            previous = "atom"
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "[" and not in_class:
            in_class = True
            continue
        if character == "]" and in_class:
            in_class = False
            previous = "atom"
            continue
        if in_class:
            continue
        if character in "*+?":
            quantifiers += 1
            if quantifiers > 1 or previous != "atom":
                raise ValueError("正则包含未允许的高风险结构")
            if (
                character in "*+"
                and not value.startswith("^")
                and any(item not in ")$" for item in value[index + 1 :])
            ):
                raise ValueError("正则包含未允许的高风险结构")
            previous = "quantifier"
            continue
        if character == ")":
            previous = "group"
        elif character not in "^(" and character != "$":
            previous = "atom"
    try:
        return re.compile(value)
    except re.error as exc:
        raise ValueError(f"正则无效：{exc}") from None


def _validate_source(value: str, extractors: set[str]) -> None:
    if value in _BASE_FIELDS:
        return
    if value.startswith("extracted.") and value.removeprefix("extracted.") in extractors:
        return
    if value.startswith("raw."):
        _validate_path(value)
        return
    raise ValueError(f"未知来源字段：{value}")


def _validate_path(value: str) -> None:
    parts = value.split(".")
    if (
        not parts
        or len(parts) > 20
        or any(not part or len(part) > 120 or part.startswith("_") for part in parts)
    ):
        raise ValueError(f"DSL 路径不合法：{value}")


def _event_context(event: CanonicalEvent) -> dict[str, Any]:
    reaction = event.reaction or {}
    return {
        "event": {"id": event.event_id, "type": str(event.event_type)},
        "message": {
            "id": event.message_id,
            "kind": str(event.message_kind),
            "text": event.text,
        },
        "conversation": {"id": event.conversation_id},
        "sender": {"id": event.sender_id, "name": event.sender_name},
        "occurred_at": event.occurred_at,
        "quoted_message": event.quoted_message,
        "reaction": {
            "name": reaction.get("name"),
            "text": reaction.get("text"),
            "operation": reaction.get("operation"),
        },
        "media": [item.model_dump(mode="json") for item in event.media_assets],
        "raw": event.raw_payload,
    }


def _get(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _matches(rule: FilterRule, actual: Any, pattern: re.Pattern[str] | None) -> bool:
    if rule.operator == "exists":
        return actual not in (None, "", [], {})
    if rule.operator == "equals":
        return actual == rule.value
    if rule.operator == "contains":
        return str(rule.value) in str(actual or "")
    if rule.operator == "in":
        return actual in rule.value
    if rule.operator == "regex":
        assert pattern is not None
        return pattern.search(str(actual or "")[:16_384]) is not None
    raise AssertionError("unreachable")


def _extract(
    extractor: Extractor,
    context: dict[str, Any],
    pattern: re.Pattern[str] | None,
) -> Any:
    if extractor.kind == "fixed":
        return extractor.value
    if extractor.kind == "path":
        return _get(context, extractor.path or "")
    source = _get(context, extractor.source or "")
    if extractor.kind == "regex":
        assert pattern is not None
        match = pattern.search(str(source or "")[:16_384])
        if match is None:
            return None
        try:
            return match.group(extractor.group)
        except (IndexError, KeyError) as exc:
            raise ValueError(f"正则提取组不存在：{extractor.output}") from exc
    if extractor.kind == "transform":
        return _transform(source, extractor.transform or "")
    raise AssertionError("unreachable")


def _transform(value: Any, operation: str) -> Any:
    if operation == "trim":
        return str(value or "").strip()
    if operation == "lower":
        return str(value or "").lower()
    if operation == "upper":
        return str(value or "").upper()
    if operation == "integer":
        return int(value)
    if operation == "number":
        return float(value)
    if operation == "string":
        return "" if value is None else str(value)
    if operation == "dingtalk_site":
        match = re.search(r"【([^【】\n]{1,40})】", str(value or ""))
        return match.group(1).strip() if match else ""
    if operation == "dingtalk_issue_text":
        # 钉钉问题消息只移除展示标记，业务正文保持原顺序和字符不变。
        text = str(value or "")
        text = re.sub(r"\[[^\]\n]{0,40}\]\(mediaId=[^)\s]+\)", " ", text)
        text = re.sub(
            r"\[[^\]\n]{0,40}\]\s*\S{0,200}?\s+fileId:\s*[A-Za-z0-9_-]+",
            " ",
            text,
        )
        text = re.sub(r"注意：如需下载[^\n]*$", " ", text)
        text = re.sub(r"@\S*?\([^()]*\)(?=\s|$|@)", " ", text)
        text = re.sub(r"@[^()\s]*（[^（）]*）(?=\s|$|@)", " ", text)
        text = re.sub(r"@[^\s()（）]+(?=\s|$|@)", " ", text)
        text = re.sub(r"【[^【】]{1,40}】", " ", text)
        return re.sub(r"\s+", " ", text).strip()
    raise ValueError(f"未知转换器：{operation}")
