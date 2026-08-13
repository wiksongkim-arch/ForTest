"""
钉钉 MCP 服务 - 通过 HTTP API 直接调用钉钉 MCP
文档：https://alidocs.dingtalk.com
"""
import json
import time
from typing import Dict, Any, List
import requests

from backend.security.redaction import redact_text
from backend.security.url_validation import (
    MCPURLValidationError,
    normalize_https_mcp_url,
)


class DingTalkMCPError(RuntimeError):
    """A credential-safe DingTalk MCP transport or response failure."""


def extract_node_id(value: str) -> str:
    candidate = value.strip()
    if "/nodes/" in candidate:
        candidate = candidate.split("/nodes/", 1)[1]
    candidate = candidate.split("?", 1)[0].split("#", 1)[0]
    if not candidate or "/" in candidate or "\\" in candidate:
        raise DingTalkMCPError("无效的钉钉节点标识")
    return candidate


def unwrap_tool_result(tool_name: str, result: object) -> dict[str, Any]:
    """Unwrap supported MCP structured/JSON-text result envelopes."""
    if not isinstance(result, dict):
        raise DingTalkMCPError(f"{tool_name} 返回格式无效")
    if result.get("isError") is True:
        raise DingTalkMCPError(f"{tool_name} 执行失败")

    if "structuredContent" in result:
        structured = result["structuredContent"]
        if not isinstance(structured, dict):
            raise DingTalkMCPError(f"{tool_name} 返回结构化内容无效")
        if structured.get("isError") is True:
            raise DingTalkMCPError(f"{tool_name} 执行失败")
        return structured

    if "content" in result:
        content = result["content"]
        if not isinstance(content, list):
            raise DingTalkMCPError(f"{tool_name} 返回内容无效")
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                if any(
                    key in parsed
                    for key in ("structuredContent", "content", "isError")
                ):
                    return unwrap_tool_result(tool_name, parsed)
                return parsed
        raise DingTalkMCPError(f"{tool_name} 未返回有效 JSON 内容")

    # Some MCP implementations return the typed payload directly.
    return result


def require_success_result(tool_name: str, result: object) -> dict[str, Any]:
    payload = unwrap_tool_result(tool_name, result)
    if payload.get("success") is False or payload.get("ok") is False:
        raise DingTalkMCPError(f"{tool_name} 执行失败")
    return payload


class DingTalkMCPService:
    """钉钉 MCP 服务类 - HTTP API 模式"""

    def __init__(self, mcp_url: str):
        try:
            self.mcp_url = normalize_https_mcp_url(mcp_url)
        except MCPURLValidationError:
            raise DingTalkMCPError("MCP URL 无效或不安全") from None
        self._request_deadline: float | None = None
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def set_request_deadline(self, deadline: float | None) -> None:
        self._request_deadline = (
            None if deadline is None else float(deadline)
        )

    def _request_timeout(self) -> requests.adapters.TimeoutSauce:
        if self._request_deadline is None:
            return requests.adapters.TimeoutSauce(
                total=35.0,
                connect=5.0,
                read=30.0,
            )
        remaining = self._request_deadline - time.monotonic()
        if remaining <= 0:
            raise DingTalkMCPError("MCP 请求截止时间已到")
        connect = min(5.0, remaining * 0.25)
        read = min(30.0, remaining)
        return requests.adapters.TimeoutSauce(
            total=remaining,
            connect=connect,
            read=read,
        )

    def _call_mcp(self, method: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        调用 MCP 方法

        Args:
            method: 方法名（如 tools/list, tools/call）
            params: 参数字典

        Returns:
            响应结果
        """
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": 1
        }

        # Keep the default Requests/system proxy policy; TLS stays verified.
        try:
            response = requests.post(
                self.mcp_url,
                json=payload,
                headers=self.headers,
                timeout=self._request_timeout(),
                verify=True,
                allow_redirects=False,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise DingTalkMCPError(
                f"MCP 请求失败（{type(exc).__name__}）"
            ) from None
        if not isinstance(body, dict):
            raise DingTalkMCPError("MCP 响应格式无效")
        if "error" in body:
            raise DingTalkMCPError("MCP 服务返回错误")
        result = body.get("result", {})
        if not isinstance(result, dict):
            raise DingTalkMCPError("MCP result 格式无效")
        return result

    def list_tools(self) -> List[Dict[str, Any]]:
        """列出所有可用工具"""
        result = self._call_mcp("tools/list")
        return result.get("tools", [])

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        调用指定工具

        Args:
            tool_name: 工具名称
            arguments: 参数字典

        Returns:
            工具执行结果
        """
        result = self._call_mcp("tools/call", {
            "name": tool_name,
            "arguments": arguments
        })
        return result

    def _require_dict(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return unwrap_tool_result(
                tool_name,
                self.call_tool(tool_name, arguments),
            )
        except DingTalkMCPError as exc:
            raise DingTalkMCPError(str(exc)) from None
        except Exception as exc:
            raise DingTalkMCPError(
                f"{tool_name} 调用失败（{type(exc).__name__}）"
            ) from None

    def _require_success(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> None:
        try:
            require_success_result(
                tool_name,
                self.call_tool(tool_name, arguments),
            )
        except DingTalkMCPError as exc:
            raise DingTalkMCPError(str(exc)) from None
        except Exception as exc:
            raise DingTalkMCPError(
                f"{tool_name} 调用失败（{type(exc).__name__}）"
            ) from None

    def get_document_info(self, node_id: str) -> dict[str, Any]:
        return self._require_dict("get_document_info", {"nodeId": node_id})

    def list_nodes(self, folder_id: str) -> list[dict[str, Any]]:
        payload = self._require_dict("list_nodes", {"folderId": folder_id})
        nodes = payload.get("nodes", payload.get("items", []))
        if not isinstance(nodes, list) or any(
            not isinstance(item, dict) for item in nodes
        ):
            raise DingTalkMCPError("list_nodes 未返回有效节点列表")
        return list(nodes)

    def copy_document(
        self,
        node_id: str,
        target_folder_id: str,
    ) -> dict[str, Any]:
        return self._require_dict(
            "copy_document",
            {"nodeId": node_id, "targetFolderId": target_folder_id},
        )

    def create_file(
        self,
        name: str,
        file_type: str,
        folder_id: str,
    ) -> dict[str, Any]:
        return self._require_dict(
            "create_file",
            {"name": name, "type": file_type, "folderId": folder_id},
        )

    def rename_document(self, node_id: str, name: str) -> None:
        self._require_success(
            "rename_document",
            {"nodeId": node_id, "newName": name},
        )

    def delete_document(self, node_id: str) -> None:
        self._require_success("delete_document", {"nodeId": node_id})

    def get_document_content(self, url: str) -> dict[str, Any]:
        return self._require_dict(
            "get_document_content",
            {"nodeId": extract_node_id(url)},
        )

    def get_document_name(self, url: str) -> str:
        info = self.get_document_info(extract_node_id(url))
        name = info.get("name")
        return name if isinstance(name, str) and name else "测试用例"

    def list_document_blocks(self, node_id: str, start_index: int = 0, end_index: int = None) -> Dict[str, Any]:
        """
        查询钉钉文档的一级块元素列表

        Args:
            node_id: 文档标识（支持文档链接 URL 或 dentryUuid）
            start_index: 起始位置
            end_index: 终止位置

        Returns:
            文档块列表
        """
        params = {"nodeId": node_id, "startIndex": start_index}
        if end_index is not None:
            params["endIndex"] = end_index

        return self.call_tool("list_document_blocks", params)

    def get_document_text_content(self, node_id: str) -> str:
        """
        获取钉钉文档的纯文本内容

        Args:
            node_id: 文档标识

        Returns:
            文档的文本内容
        """
        blocks_result = self.list_document_blocks(node_id)

        texts = []
        blocks = blocks_result.get("blocks", [])

        for block in blocks:
            block_type = block.get("blockType", "")
            content = block.get("content", {})

            if block_type == "text":
                # 文本块
                texts.append(content.get("text", ""))
            elif block_type == "heading":
                # 标题块
                level = content.get("level", 1)
                text = content.get("text", "")
                texts.append(f"{'#' * level} {text}")
            elif block_type == "list":
                # 列表块
                items = content.get("items", [])
                for i, item in enumerate(items, 1):
                    texts.append(f"{i}. {item}")
            elif block_type == "code":
                # 代码块
                language = content.get("language", "")
                code = content.get("code", "")
                texts.append(f"```{language}\n{code}\n```")
            elif block_type == "quote":
                # 引用块
                text = content.get("text", "")
                texts.append(f"> {text}")
            elif block_type == "image":
                # 图片块
                url = content.get("url", "")
                texts.append(f"![image]({url})")
            elif block_type == "table":
                # 表格块
                rows = content.get("rows", [])
                for row in rows:
                    texts.append(" | ".join(str(cell) for cell in row))

        return "\n\n".join(texts)

    def fetch_document(self, url: str) -> Dict[str, Any]:
        """
        获取钉钉文档内容（通过 URL）

        Args:
            url: 钉钉文档链接

        Returns:
            文档内容（包含 texts, images, files）
        """
        # 从 URL 中提取 nodeId（dentryUuid）
        # 格式: https://alidocs.dingtalk.com/i/nodes/{dentryUuid}?utm_scene=...
        node_id = url
        if "/nodes/" in url:
            node_id = url.split("/nodes/")[-1].split("?")[0]

        try:
            # 先尝试使用 get_document_content 工具（适用于 .adoc 文档）
            result = self.call_tool("get_document_content", {"nodeId": node_id})
            if result.get("isError"):
                # 如果失败，可能是因为文档是 Excel 等其他格式
                # 返回错误信息，供调用方处理
                return result
            return result
        except Exception as e:
            # 返回错误信息
            return {
                "content": [
                    {"type": "text", "text": redact_text(str(e))}
                ],
                "isError": True,
                "error": redact_text(str(e)),
            }

    def download_document_file(self, node_id: str) -> bytes:
        """
        下载钉钉文档（适用于 Excel 等二进制文件）

        Args:
            node_id: 文档节点 ID

        Returns:
            文件内容（字节）
        """
        result = self.call_tool("download_file", {"nodeId": node_id})
        return result


def create_dingtalk_service(mcp_url: str) -> DingTalkMCPService:
    """工厂函数：创建钉钉 MCP 服务实例"""
    return DingTalkMCPService(mcp_url)
