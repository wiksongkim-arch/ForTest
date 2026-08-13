"""
钉钉 MCP 表格服务 - 通过 HTTP API 调用钉钉表格 MCP
用于处理 .axls 格式的 Excel 文档
"""
import json
import time
from typing import Dict, Any, List
import requests

from services.dingtalk_mcp import (
    DingTalkMCPError,
    require_success_result,
    unwrap_tool_result,
)
from backend.security.url_validation import (
    MCPURLValidationError,
    normalize_https_mcp_url,
)


class _SheetList(list[dict[str, Any]]):
    """List result with a narrow compatibility view for legacy callers."""

    def get(self, key: str, default=None):
        if key == "structuredContent":
            return {"sheets": list(self)}
        if key in {"sheets", "items"}:
            return list(self)
        return default


class DingTalkSpreadSheetMCPService:
    """钉钉表格 MCP 服务类 - HTTP API 模式"""

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
        """调用 MCP 方法"""
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
        """调用指定工具"""
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

    def get_all_sheets(self, node_id: str) -> list[dict[str, Any]]:
        """获取所有工作表"""
        payload = self._require_dict("get_all_sheets", {"nodeId": node_id})
        sheets = payload.get("sheets", payload.get("items", []))
        if not isinstance(sheets, list) or any(
            not isinstance(item, dict) for item in sheets
        ):
            raise DingTalkMCPError("get_all_sheets 未返回有效工作表列表")
        return _SheetList(sheets)

    def set_range_from_csv(
        self,
        node_id: str,
        sheet_id: str,
        start_cell: str,
        csv_text: str,
        allow_overwrite: bool,
    ) -> None:
        self._require_success(
            "set_range_from_csv",
            {
                "nodeId": node_id,
                "sheetId": sheet_id,
                "startCell": start_cell,
                "csv": csv_text,
                "allowOverwrite": allow_overwrite,
            },
        )

    def clear_range(
        self,
        node_id: str,
        sheet_id: str,
        range_address: str,
        clear_type: str = "content",
    ) -> None:
        self._require_success(
            "clear_range",
            {
                "nodeId": node_id,
                "sheetId": sheet_id,
                "range": range_address,
                "type": clear_type,
            },
        )

    def get_range_as_csv(
        self,
        node_id: str,
        sheet_id: str,
        range_address: str,
        max_chars: int = 2_000_000,
    ) -> str:
        payload = self._require_dict(
            "get_range_as_csv",
            {
                "nodeId": node_id,
                "sheetId": sheet_id,
                "range": range_address,
                "maxChars": max(1, min(int(max_chars), 2_000_000)),
            },
        )
        if payload.get("hasMore") is True:
            raise DingTalkMCPError("get_range_as_csv 返回被截断的数据")
        csv_text = payload.get("csv", payload.get("text"))
        if not isinstance(csv_text, str):
            raise DingTalkMCPError("get_range_as_csv 未返回 CSV 文本")
        return csv_text

    def get_sheet(self, node_id: str, sheet_id: str) -> Dict[str, Any]:
        """获取工作表信息"""
        return self.call_tool("get_sheet", {"nodeId": node_id, "sheetId": sheet_id})

    def get_range(self, node_id: str, sheet_id: str, start_row: int = 0, end_row: int = None,
                 start_column: int = 0, end_column: int = None) -> Dict[str, Any]:
        """获取指定范围的单元格数据"""
        params = {
            "nodeId": node_id,
            "sheetId": sheet_id,
            "startRow": start_row,
            "startColumn": start_column
        }
        if end_row is not None:
            params["endRow"] = end_row
        if end_column is not None:
            params["endColumn"] = end_column

        return self.call_tool("get_range", params)

    def update_range(self, node_id: str, sheet_id: str, start_row: int, start_column: int, values: List[List]) -> Dict[str, Any]:
        """
        更新指定范围的单元格数据

        Args:
            node_id: 文档节点 ID
            sheet_id: 工作表 ID
            start_row: 起始行（从0开始）
            start_column: 起始列（从0开始）
            values: 二维数组，要写入的数据

        Returns:
            更新结果
        """
        # 计算范围地址 (如 A1:C3)
        def col_to_letter(col):
            result = ""
            while col >= 0:
                result = chr(ord('A') + col % 26) + result
                col = col // 26 - 1
            return result

        if not values:
            return {"success": False, "error": "No values to write"}

        num_rows = len(values)
        num_cols = len(values[0]) if values[0] else 0

        start_col_letter = col_to_letter(start_column)
        end_col_letter = col_to_letter(start_column + num_cols - 1)
        end_row = start_row + num_rows - 1

        range_address = f"{start_col_letter}{start_row + 1}:{end_col_letter}{end_row + 1}"

        params = {
            "nodeId": node_id,
            "sheetId": sheet_id,
            "rangeAddress": range_address,
            "values": values
        }
        return self.call_tool("update_range", params)

    def get_document_content(self, url: str) -> Dict[str, Any]:
        """
        获取钉钉表格文档内容

        Args:
            url: 钉钉文档链接

        Returns:
            表格内容（包含 sheets, texts 等）
        """
        # 从 URL 中提取 nodeId
        node_id = url
        if "/nodes/" in url:
            node_id = url.split("/nodes/")[-1].split("?")[0]

        # 获取所有工作表
        sheets_result = self.get_all_sheets(node_id)
        sheets = []
        raw_sheets = list(sheets_result)
        for s in raw_sheets:
            sheets.append({
                "sheetId": s.get("sheetId", ""),
                "name": s.get("name", "")
            })

        if not sheets:
            return {"sheets": [], "texts": [], "data": [], "isError": False}

        # 默认获取第一个工作表的数据
        first_sheet = sheets[0]
        sheet_id = first_sheet.get("sheetId", "")

        # 获取工作表信息
        sheet_info = self.get_sheet(node_id, sheet_id)

        # 获取前100行数据（避免数据量过大）
        data_result = self.get_range(node_id, sheet_id, start_row=0, end_row=100)

        # 解析数据为文本
        texts = self._parse_range_to_text(data_result, first_sheet.get("name", "Sheet1"))

        return {
            "sheets": sheets,
            "sheet_info": sheet_info,
            "data": data_result,
            "isError": False,
            "texts": texts
        }

    def _parse_range_to_text(self, data_result: Dict[str, Any], sheet_name: str = "") -> List[str]:
        """解析表格数据为文本列表"""
        texts = []
        texts.append(f"=== 工作表: {sheet_name} ===")

        # 从 content 中解析 JSON（displayValues）
        content = data_result.get("content", [])
        for item in content:
            if item.get("type") == "text":
                try:
                    data = json.loads(item.get("text", "{}"))
                    display_values = data.get("displayValues", [])

                    for row in display_values:
                        # 将行数据用 | 分隔组合
                        row_text = " | ".join([str(cell) if cell else "" for cell in row])
                        if row_text.strip():
                            texts.append(row_text)
                    break  # 只处理第一个 text 类型的内容
                except json.JSONDecodeError:
                    continue

        return texts


def create_spreadsheet_service(mcp_url: str = None) -> DingTalkSpreadSheetMCPService:
    """工厂函数：创建钉钉表格 MCP 服务实例"""
    if not mcp_url:
        raise DingTalkMCPError("钉钉表格 MCP URL 未配置")
    return DingTalkSpreadSheetMCPService(mcp_url)
