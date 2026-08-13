"""主菜单配置向导完成条件回归。"""

from __future__ import annotations

from utils.default_templates import CONTENT_TEMPLATE, OUTPUT_TEMPLATE
from windows_native.native_service import NativeService


class StatusService:
    """复用真实聚合逻辑，以内存快照隔离 Keyring 和网络。"""

    configuration_status = NativeService.configuration_status

    def __init__(self) -> None:
        self.default_template_paths = {}
        self.document = {
            "document_mcp": {"configured": False},
            "spreadsheet_mcp": {"configured": False},
            "content_template_url": "",
            "document_template_url": "",
            "output_folder_url": "",
            "local_output_dir": "",
        }
        self.ai = {
            "configurations": [
                {"id": "codex", "complete": True, "status": "unchecked"}
            ]
        }

    def get_document(self) -> dict:
        return self.document

    def get_ai_configurations(self) -> dict:
        return self.ai


def test_configuration_status_matches_main_menu_and_all_required_checks():
    service = StatusService()
    status = service.configuration_status(jenkins_configured=False)

    assert status["complete"] is False
    assert [section["label"] for section in status["sections"]] == [
        "快捷部署",
        "测试用例生成",
        "设置",
    ]
    assert [section["items"][0]["label"] for section in status["sections"]] == [
        "Jenkins 配置",
        "文档配置",
        "AI 配置",
    ]
    assert [
        check["label"]
        for check in status["sections"][1]["items"][0]["checks"]
    ] == [
        "文档 MCP",
        "表格 MCP",
        "用例模板表格",
        "输出文档模板",
        "输出文件夹",
        "本地备份目录",
    ]
    # 仅配置完整但从未检测，不能满足 AI 配置完成条件。
    assert status["sections"][2]["complete"] is False


def test_configuration_status_accepts_complete_online_configuration_and_passed_ai():
    service = StatusService()
    service.document = {
        "document_mcp": {"configured": True},
        "spreadsheet_mcp": {"configured": True},
        "content_template_url": "https://alidocs.dingtalk.com/sheets/template",
        "document_template_url": "https://alidocs.dingtalk.com/docs/template",
        "output_folder_url": "https://alidocs.dingtalk.com/folder/output",
        "local_output_dir": "D:/ForTest/output",
    }
    service.ai["configurations"][0]["status"] = "passed"

    status = service.configuration_status(jenkins_configured=True)

    assert status["complete"] is True
    assert all(section["complete"] for section in status["sections"])


def test_configuration_status_accepts_local_templates_without_online_mcp(tmp_path):
    service = StatusService()
    content_template = tmp_path / "content.xlsx"
    output_template = tmp_path / "output.xlsx"
    content_template.touch()
    output_template.touch()
    service.default_template_paths = {
        CONTENT_TEMPLATE: content_template,
        OUTPUT_TEMPLATE: output_template,
    }
    service.document["local_output_dir"] = str(tmp_path / "output")
    service.ai["configurations"][0]["status"] = "passed"

    status = service.configuration_status(jenkins_configured=True)
    document = status["sections"][1]["items"][0]

    assert status["complete"] is True
    assert document["complete"] is True
    assert document["checks"][0]["optional"] is True
    assert document["checks"][1]["optional"] is True
    assert document["checks"][4]["optional"] is True


def test_online_template_configuration_requires_corresponding_mcp(tmp_path):
    service = StatusService()
    content_template = tmp_path / "content.xlsx"
    output_template = tmp_path / "output.xlsx"
    content_template.touch()
    output_template.touch()
    service.default_template_paths = {
        CONTENT_TEMPLATE: content_template,
        OUTPUT_TEMPLATE: output_template,
    }
    service.document.update(
        {
            "content_template_url": "https://alidocs.dingtalk.com/sheets/template",
            "local_output_dir": str(tmp_path / "output"),
        }
    )
    service.ai["configurations"][0]["status"] = "passed"

    status = service.configuration_status(jenkins_configured=True)
    document = status["sections"][1]["items"][0]

    assert document["complete"] is False
    assert document["checks"][1]["optional"] is False
