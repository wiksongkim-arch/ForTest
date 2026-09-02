"""EIM AI 构建器唯一允许的结构化动作。"""

from __future__ import annotations


ACTIONS = (
    "inspect_task",
    "inspect_source_fields",
    "inspect_destination_schema",
    "read_dsl",
    "propose_patch",
    "apply_patch",
    "add_sample",
    "run_static_validation",
    "run_simulation",
    "run_regression",
    "run_connection_preflight",
    "run_destination_preflight",
    "publish_version",
    "explain_failure",
    "finish",
)

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        # 动作参数种类随 action 变化，编码为 JSON 字符串后再由可信执行器逐项校验；
        # 这样既保留动态参数，也满足 Codex 严格结构化输出禁止开放对象的要求。
        "arguments": {
            "type": "string",
            "description": "JSON 对象字符串；没有参数时返回 {}",
        },
        "message": {"type": "string"},
    },
    "required": ["action", "arguments", "message"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = """你是 ForTest EIM 构建助手。每轮只返回一个符合 schema 的动作，arguments 必须是 JSON 对象字符串。
你只能修改给定任务的 EIM DSL 和脱敏样例；不能请求 shell、文件、网络、环境变量、凭证，
不能更换 task_id、连接、群或归档目标。使用 inspect/read 动作获取事实，apply_patch 使用 JSON
Merge Patch，完成后调用 finish。发布、连接和目标写入门禁最终由 ForTest 自己执行。"""
