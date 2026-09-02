"""EIM 运行期 AI 增强：显式字段、预算、超时和失败策略。"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable

from backend.eim.builder.model_adapter import EIMBuilderModelAdapter
from backend.eim.models import AIStep, CanonicalEvent
from backend.eim.redaction import sanitize_payload
from backend.eim.repository import EIMRepository


class AISkipEvent(RuntimeError):
    """按任务策略跳过当前事件，不标记为系统失败。"""


class AIRetryEvent(RuntimeError):
    """把当前事件交回有界 inbox 重试队列。"""


class EIMAIRuntime:
    def __init__(
        self,
        repository: EIMRepository,
        data_root: Path,
        *,
        settings_service: Any = None,
        codex_path_resolver: Any = None,
        client_factory: Any = None,
        stage_runner: Callable[..., tuple[dict[str, Any], Any]] | None = None,
        stop_callback: Callable[[str], None] | None = None,
    ):
        self.repository = repository
        self.data_root = Path(data_root).resolve()
        self.settings_service = settings_service
        self.codex_path_resolver = codex_path_resolver
        self.client_factory = client_factory
        self.stage_runner = stage_runner
        self.stop_callback = stop_callback

    def process(
        self,
        steps: list[AIStep],
        values: dict[str, Any],
        event: CanonicalEvent,
        task_id: str,
    ) -> dict[str, Any]:
        output = dict(values)
        for index, step in enumerate(steps):
            _validate_output_schema(step.output_schema, allowed_fields=set(output))
            usage = self.repository.ai_usage_today(
                step.configuration_id,
                step.budget_unit,
            )
            consumed = float(usage["amount"])
            if consumed >= step.daily_budget:
                output = self._policy(
                    step.budget_action,
                    output,
                    event,
                    task_id,
                    f"AI 日预算已用尽：{step.configuration_id}",
                )
                continue
            inputs = self._inputs(step, output, event)
            images = self._images(event) if step.include_images else ()
            last_error: Exception | None = None
            for attempt in range(2):
                current = self.repository.ai_usage_today(
                    step.configuration_id,
                    step.budget_unit,
                )
                if float(current["amount"]) >= step.daily_budget:
                    output = self._policy(
                        step.budget_action,
                        output,
                        event,
                        task_id,
                        f"AI 日预算已用尽：{step.configuration_id}",
                    )
                    last_error = None
                    break
                attempt_recorded = False
                if step.budget_unit == "calls":
                    # 调用预算在发出请求前占用，失败和超时同样计费。
                    self.repository.add_ai_usage(
                        step.configuration_id,
                        step.budget_unit,
                        1.0,
                    )
                    attempt_recorded = True
                try:
                    data, evidence = self._run(step, index, inputs, images)
                    tokens = int(getattr(evidence, "input_tokens", 0) or 0) + int(
                        getattr(evidence, "output_tokens", 0) or 0
                    )
                    if step.budget_unit == "tokens":
                        self.repository.add_ai_usage(
                            step.configuration_id,
                            step.budget_unit,
                            float(max(1, tokens)),
                        )
                        attempt_recorded = True
                    _validate_value(data, step.output_schema)
                    unexpected = set(data) - set(output)
                    if unexpected:
                        raise ValueError(f"AI 输出包含未映射目标字段：{', '.join(sorted(unexpected))}")
                    output.update(data)
                    self.repository.append_log(
                        task_id=task_id,
                        event_id=event.event_id,
                        message_id=event.message_id,
                        stage="ai_runtime",
                        result="completed",
                        preview=f"AI 增强步骤 {index + 1} 完成",
                        details={
                            "configuration_id": step.configuration_id,
                            "model": str(getattr(evidence, "model", "")),
                            "tokens": tokens,
                            "duration_ms": int(getattr(evidence, "duration_ms", 0) or 0),
                        },
                    )
                    last_error = None
                    break
                except Exception as exc:
                    if not attempt_recorded:
                        # 失败响应没有可信 token 统计时按最小一单位计入预算。
                        self.repository.add_ai_usage(
                            step.configuration_id,
                            step.budget_unit,
                            1.0,
                        )
                    last_error = exc
                    if attempt == 0:
                        continue
            if last_error is not None:
                output = self._policy(
                    step.unavailable_action,
                    output,
                    event,
                    task_id,
                    f"AI 增强失败：{type(last_error).__name__}",
                )
        return output

    def _run(
        self,
        step: AIStep,
        index: int,
        inputs: dict[str, Any],
        images: tuple[Path, ...],
    ) -> tuple[dict[str, Any], Any]:
        prompt = json.dumps(
            {"inputs": inputs, "instruction": "只按 output_schema 返回结构化结果。"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        adapter: EIMBuilderModelAdapter | None = None
        try:
            if self.stage_runner:
                call = lambda: self.stage_runner(
                    step.configuration_id,
                    stage=f"eim_runtime_{index + 1}",
                    system_prompt="不得返回或请求凭证；只处理显式提供的字段。",
                    user_prompt=prompt,
                    schema=step.output_schema,
                    images=images,
                )
            else:
                if self.settings_service is None:
                    raise ValueError("AI 设置服务不可用")
                adapter = EIMBuilderModelAdapter(
                    self.settings_service,
                    codex_path_resolver=self.codex_path_resolver,
                    client_factory=self.client_factory,
                    timeout_seconds=step.timeout_seconds,
                )
                call = lambda: adapter.run_structured(
                    step.configuration_id,
                    stage=f"eim_runtime_{index + 1}",
                    system_prompt="不得返回或请求凭证；只处理显式提供的字段。",
                    user_prompt=prompt,
                    schema=step.output_schema,
                    images=images,
                )
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eim-ai")
            future = executor.submit(call)
            try:
                return future.result(timeout=step.timeout_seconds)
            except FutureTimeout:
                if adapter:
                    adapter.cancel()
                future.cancel()
                raise TimeoutError("EIM AI 增强步骤超时") from None
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        finally:
            if adapter:
                adapter.close()

    def _inputs(
        self,
        step: AIStep,
        values: dict[str, Any],
        event: CanonicalEvent,
    ) -> dict[str, Any]:
        canonical = {
            "event.id": event.event_id,
            "event.type": str(event.event_type),
            "message.id": event.message_id,
            "message.kind": str(event.message_kind),
            "message.text": event.text,
            "sender.id": event.sender_id,
            "sender.name": event.sender_name,
            "conversation.id": event.conversation_id,
            "occurred_at": event.occurred_at,
        }
        result: dict[str, Any] = {}
        redacted = set(step.redacted_fields)
        for name in step.input_fields:
            if name in redacted:
                result[name] = "<redacted>"
            elif name in values:
                result[name] = sanitize_payload(values[name])
            elif name in canonical:
                result[name] = sanitize_payload(canonical[name])
            else:
                raise ValueError(f"AI 输入字段不存在：{name}")
        return result

    def _images(self, event: CanonicalEvent) -> tuple[Path, ...]:
        images: list[Path] = []
        media_root = (self.data_root / "eim" / "media").resolve()
        for asset in event.media_assets:
            if not asset.local_path:
                continue
            path = (self.data_root / asset.local_path).resolve()
            if (
                media_root not in path.parents
                or path.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}
                or not path.is_file()
                or path.is_symlink()
                or path.stat().st_size > 20 * 1024 * 1024
            ):
                continue
            if asset.sha256:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != asset.sha256.casefold():
                    continue
            images.append(path)
            if len(images) == 4:
                break
        if not images:
            raise ValueError("AI 步骤启用了图片，但事件没有可用的受信本地图片")
        return tuple(images)

    def _policy(
        self,
        action: str,
        values: dict[str, Any],
        event: CanonicalEvent,
        task_id: str,
        reason: str,
    ) -> dict[str, Any]:
        if action == "archive_raw":
            return values
        if action == "skip":
            raise AISkipEvent(reason)
        if action == "stop":
            if self.stop_callback:
                self.stop_callback(task_id)
            raise RuntimeError(reason)
        if action == "retry":
            raise AIRetryEvent(reason)
        raise RuntimeError(reason)


def _validate_output_schema(
    schema: dict[str, Any],
    *,
    allowed_fields: set[str],
    depth: int = 0,
) -> None:
    """只接受模型客户端和本地校验器共同支持的 JSON Schema 子集。"""

    if depth > 10 or not isinstance(schema, dict):
        raise ValueError("AI output_schema 过深或不是对象")
    allowed_keywords = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "description",
    }
    if set(schema) - allowed_keywords:
        raise ValueError("AI output_schema 包含未允许关键字")
    kind = schema.get("type")
    if depth == 0:
        if kind != "object" or schema.get("additionalProperties") is not False:
            raise ValueError("AI output_schema 根节点必须是拒绝额外字段的 object")
        properties = schema.get("properties")
        if not isinstance(properties, dict) or set(properties) - allowed_fields:
            raise ValueError("AI output_schema 只能声明已有目标映射字段")
    if kind not in {"object", "array", "string", "number", "integer", "boolean", "null"}:
        raise ValueError("AI output_schema 类型不受支持")
    properties = schema.get("properties", {})
    if properties:
        if not isinstance(properties, dict) or len(properties) > 100:
            raise ValueError("AI output_schema properties 不合法")
        for child in properties.values():
            _validate_output_schema(child, allowed_fields=allowed_fields, depth=depth + 1)
    if kind == "array":
        _validate_output_schema(
            schema.get("items", {}),
            allowed_fields=allowed_fields,
            depth=depth + 1,
        )


def _validate_value(value: Any, schema: dict[str, Any]) -> None:
    kind = schema.get("type")
    valid = {
        "object": type(value) is dict,
        "array": type(value) is list,
        "string": type(value) is str,
        "number": type(value) in {int, float},
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "null": value is None,
    }.get(kind, False)
    if not valid or ("enum" in schema and value not in schema["enum"]):
        raise ValueError("AI 输出不符合 output_schema")
    if kind == "object":
        properties = schema.get("properties") or {}
        if any(name not in value for name in schema.get("required") or []):
            raise ValueError("AI 输出缺少必填字段")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError("AI 输出包含额外字段")
        for name, child in value.items():
            if name in properties:
                _validate_value(child, properties[name])
    elif kind == "array":
        for item in value:
            _validate_value(item, schema["items"])
