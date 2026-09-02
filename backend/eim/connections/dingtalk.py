"""基于隔离 DWS profile 的钉钉连接、群发现与断线窗口读取。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from backend.eim.connections.dws_runtime import DWSRuntime, DWSRuntimeError
from backend.eim.models import ConnectionState, EIMConnection, utc_now
from backend.eim.repository import EIMRepository


SELF_LOOP_NOTICE = "当前授权账号本人发送的消息受钉钉 self-loop 过滤，不会进入监听。"


class DingTalkConnector:
    """P0 唯一真实连接器；不搜索 PATH，也不读取全局 DWS 配置。"""

    def __init__(self, runtime: DWSRuntime, repository: EIMRepository):
        self.runtime = runtime
        self.repository = repository

    def create_connection(self) -> EIMConnection:
        value = EIMConnection(config_dir_ref="pending")
        value.config_dir_ref = f"eim/connections/{value.connection_id}/dws"
        self.runtime.config_dir(value.connection_id)
        return self.repository.save_connection(value)

    def authorize(
        self,
        connection_id: str,
        *,
        device: bool = False,
        no_browser: bool = False,
    ) -> EIMConnection:
        connection = self._required(connection_id)
        connection.connection_state = ConnectionState.AUTHORIZING
        self.repository.save_connection(connection)
        arguments = ["auth", "login"]
        if device:
            arguments.append("--device")
        if no_browser:
            arguments.append("--no-browser")
        try:
            self.runtime.run(
                arguments,
                config_dir=self.runtime.config_dir(connection_id),
                timeout=900,
            )
            return self.refresh(connection_id)
        except Exception:
            connection.connection_state = ConnectionState.ERROR
            connection.checked_at = utc_now()
            self.repository.save_connection(connection)
            raise

    def refresh(self, connection_id: str) -> EIMConnection:
        connection = self._required(connection_id)
        config_dir = self.runtime.config_dir(connection_id)
        arguments = ["auth", "status"]
        if connection.profile:
            arguments.extend(("--profile", connection.profile))
        try:
            status = self.runtime.run_json(arguments, config_dir=config_dir)
        except DWSRuntimeError:
            connection.connection_state = ConnectionState.ERROR
            connection.checked_at = utc_now()
            self.repository.save_connection(connection)
            raise
        authenticated = bool(status.get("authenticated"))
        token_valid = bool(status.get("token_valid"))
        connection.account_id = str(status.get("user_id") or "")
        connection.account_name = str(status.get("user_name") or "")
        connection.organization_id = str(status.get("corp_id") or "")
        connection.organization_name = str(status.get("corp_name") or "")
        if connection.organization_id and connection.account_id:
            connection.profile = f"{connection.organization_id}:{connection.account_id}"
        connection.connection_state = (
            ConnectionState.CONNECTED
            if authenticated and token_valid
            else ConnectionState.EXPIRED
            if authenticated
            else ConnectionState.DISCONNECTED
        )
        connection.capabilities = {
            "authenticated": authenticated,
            "token_valid": token_valid,
            "group_discovery": authenticated and token_valid,
            "group_message_events": authenticated and token_valid,
            "reaction_events": authenticated and token_valid,
            "self_loop_excluded": True,
            "self_loop_notice": SELF_LOOP_NOTICE,
            "dws": self.runtime.probe(),
        }
        connection.checked_at = utc_now()
        return self.repository.save_connection(connection)

    def list_groups(self, connection_id: str) -> list[dict[str, str]]:
        connection = self._connected(connection_id)
        groups: list[dict[str, str]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for _ in range(1_000):
            arguments: list[str] = ["chat", "group", "list-all", "--limit", "200"]
            if cursor:
                arguments.extend(("--cursor", cursor))
            arguments.extend(("--profile", connection.profile))
            payload = self.runtime.run_json(
                arguments,
                config_dir=self.runtime.config_dir(connection_id),
                timeout=60,
            )
            page, next_cursor = _group_page(payload)
            for item in page:
                group_id = str(
                    item.get("openConversationId")
                    or item.get("open_conversation_id")
                    or item.get("conversationId")
                    or item.get("conversation_id")
                    or ""
                ).strip()
                if not group_id:
                    continue
                groups.append(
                    {
                        "id": group_id,
                        "name": str(item.get("title") or item.get("name") or group_id),
                        "owner_id": str(item.get("ownerUserId") or item.get("owner_id") or ""),
                    }
                )
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise DWSRuntimeError("钉钉群分页游标重复，已停止读取")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise DWSRuntimeError("钉钉群数量超过安全分页上限")
        # 只按稳定 ID 去重；同名群必须保留给界面消歧。
        return list({group["id"]: group for group in groups}.values())

    def gap_messages(
        self,
        connection_id: str,
        conversation_id: str,
        *,
        since: str,
    ) -> list[dict[str, Any]]:
        """仅在明确断线后读取 newer 窗口，不作为常态轮询。"""

        connection = self._connected(connection_id)
        if not conversation_id.strip():
            raise ValueError("钉钉群稳定 ID 不能为空")
        try:
            boundary = datetime.fromisoformat(since).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise ValueError("断线补偿起始时间不合法") from exc
        messages: list[dict[str, Any]] = []
        seen_boundaries = {boundary}
        for _ in range(100):
            payload = self.runtime.run_json(
                [
                    "chat",
                    "message",
                    "list",
                    "--conversation-id",
                    conversation_id,
                    "--time",
                    boundary,
                    "--direction",
                    "newer",
                    "--limit",
                    "200",
                    "--profile",
                    connection.profile,
                ],
                config_dir=self.runtime.config_dir(connection_id),
                timeout=60,
            )
            page = payload.get("messages") or payload.get("result") or []
            if isinstance(page, dict):
                page = page.get("messages") or page.get("items") or []
            if not isinstance(page, list):
                raise DWSRuntimeError("钉钉消息补偿返回结构异常")
            messages.extend(item for item in page if isinstance(item, dict))
            if not payload.get("hasMore") and not payload.get("has_more"):
                return messages
            next_boundary = _last_create_time(page)
            if not next_boundary or next_boundary in seen_boundaries:
                raise DWSRuntimeError("钉钉消息补偿无法继续分页")
            seen_boundaries.add(next_boundary)
            boundary = next_boundary
        raise DWSRuntimeError("钉钉消息补偿超过安全分页上限")

    def _required(self, connection_id: str) -> EIMConnection:
        value = self.repository.get_connection(connection_id)
        if value is None:
            raise KeyError(f"EIM 连接不存在：{connection_id}")
        return value

    def _connected(self, connection_id: str) -> EIMConnection:
        value = self._required(connection_id)
        if value.connection_state is not ConnectionState.CONNECTED or not value.profile:
            raise ValueError("钉钉连接尚未就绪，请先重新授权")
        return value


def _group_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    result = payload.get("result", payload)
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)], ""
    if not isinstance(result, dict):
        raise DWSRuntimeError("钉钉群列表返回结构异常")
    items = result.get("groups") or result.get("items") or result.get("list") or []
    if not isinstance(items, list):
        raise DWSRuntimeError("钉钉群列表缺少数组")
    cursor = str(
        result.get("nextCursor")
        or result.get("next_cursor")
        or payload.get("nextCursor")
        or payload.get("next_cursor")
        or ""
    )
    return [item for item in items if isinstance(item, dict)], cursor


def _last_create_time(page: list[Any]) -> str:
    for item in reversed(page):
        if isinstance(item, dict):
            value = item.get("createTime") or item.get("create_time")
            if value:
                return str(value)
    return ""
