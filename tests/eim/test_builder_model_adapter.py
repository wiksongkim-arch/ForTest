"""EIM 构建动作的严格结构化输出适配测试。"""

from __future__ import annotations

from types import SimpleNamespace

from backend.eim.builder.action_schema import ACTION_SCHEMA
from backend.eim.builder.model_adapter import EIMBuilderModelAdapter


def test_action_arguments_use_strict_schema_and_decode_to_object() -> None:
    assert ACTION_SCHEMA["properties"]["arguments"]["type"] == "string"
    adapter = object.__new__(EIMBuilderModelAdapter)
    adapter.router = SimpleNamespace(
        run_configuration_stage=lambda *_args, **_kwargs: (
            {"action": "finish", "arguments": "{}", "message": "已就绪"},
            SimpleNamespace(model="fixture"),
        )
    )

    result, _evidence = adapter.run_action("ai-1", "检测")

    assert result["arguments"] == {}
