"""复用现有全部 AI 配置协议的 EIM 结构化动作适配器。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.ai.capability_router import CapabilityRouterProvider
from backend.eim.builder.action_schema import ACTION_SCHEMA, SYSTEM_PROMPT


class EIMBuilderModelAdapter:
    def __init__(
        self,
        settings_service: Any,
        *,
        codex_path_resolver: Any = None,
        client_factory: Any = None,
        timeout_seconds: int | None = None,
    ):
        snapshot = settings_service.snapshot()
        if timeout_seconds is not None:
            cap = max(1, int(timeout_seconds))
            configurations = tuple(
                item.model_copy(
                    update={"timeout_seconds": min(item.timeout_seconds, cap)}
                )
                for item in snapshot.settings.ai.configurations
            )
            # 运行期步骤超时同时约束底层网络调用，避免取消后遗留长请求线程。
            ai = snapshot.settings.ai.model_copy(
                update={"configurations": configurations}
            )
            settings = snapshot.settings.model_copy(update={"ai": ai})
            snapshot = snapshot.model_copy(update={"settings": settings})
        self.router = CapabilityRouterProvider(
            snapshot,
            codex_path_resolver=codex_path_resolver,
            client_factory=client_factory,
        )

    def run_action(
        self,
        configuration_id: str,
        prompt: str,
    ) -> tuple[dict[str, Any], Any]:
        result, evidence = self.router.run_configuration_stage(
            configuration_id,
            stage="eim_builder_action",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
            schema=ACTION_SCHEMA,
        )
        arguments = result.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                raise ValueError("模型动作 arguments 不是合法 JSON") from None
        if not isinstance(arguments, dict):
            raise ValueError("模型动作 arguments 必须是 JSON 对象")
        return {**result, "arguments": arguments}, evidence

    def run_structured(
        self,
        configuration_id: str,
        *,
        stage: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        images: tuple[Path, ...] = (),
    ) -> tuple[dict[str, Any], Any]:
        return self.router.run_configuration_stage(
            configuration_id,
            stage=stage,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
            images=images,
        )

    def cancel(self) -> None:
        self.router.cancel()

    def close(self) -> None:
        self.router.close()
