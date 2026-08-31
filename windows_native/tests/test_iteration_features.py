"""本轮本地文档与模板交互的原生界面验收。"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QPlainTextEdit

import windows_native.ui.document_page as document_page_module
from utils.default_templates import CONTENT_TEMPLATE, OUTPUT_TEMPLATE
from windows_native.i18n import set_language, tr
from windows_native.ui.document_page import DocumentSettingsPanel
from windows_native.ui.task_widgets import NewTaskDialog, localized_log_message


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _SynchronousHost:
    """同步执行页面后台动作，让交互测试可确定地断言结果。"""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def show_error(self, message: str) -> None:
        self.errors.append(message)

    def run_async(
        self,
        function,
        *,
        success=None,
        failure=None,
        finished=None,
    ) -> None:
        try:
            result = function()
            if success:
                success(result)
        except Exception as exc:  # pragma: no cover - 仅用于失败回调契约
            if failure:
                failure(str(exc))
            else:
                raise
        finally:
            if finished:
                finished()


class _TemplateService:
    def __init__(self, root: Path) -> None:
        self.paths = {
            CONTENT_TEMPLATE: root / "用例模板表格.xlsx",
            OUTPUT_TEMPLATE: root / "输出文档模板.xlsx",
        }
        for path in self.paths.values():
            path.write_bytes(b"user-template")
        self.restored: list[str] = []

    def default_template_path(self, template_type: str) -> str:
        return str(self.paths[template_type])

    def restore_default_template(self, template_type: str) -> str:
        self.restored.append(template_type)
        return str(self.paths[template_type])


def test_new_task_dialog_switches_between_link_and_absolute_file(
    app,
    monkeypatch,
    tmp_path,
):
    set_language("zh_CN")
    requirement = tmp_path / "本地 需求.md"
    requirement.write_text("# 本地需求", encoding="utf-8")
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(requirement), ""),
    )
    dialog = NewTaskDialog()

    assert dialog.minimumWidth() == 840
    assert dialog.width() >= 980
    labels = [label.text() for label in dialog.findChildren(QLabel)]
    assert (
        "链接模式每行输入一个需求文档地址；任务会按行序创建并开始。"
        in labels
    )
    assert "需求文档" in labels
    assert [dialog.source_type.itemText(index) for index in range(2)] == [
        "链接",
        "文件",
    ]
    assert dialog.file_control.isHidden()
    dialog.url.setPlainText(
        "https://alidocs.dingtalk.com/i/nodes/first\n\n"
        " https://alidocs.dingtalk.com/i/nodes/second "
    )
    assert dialog.url.lineWrapMode() == QPlainTextEdit.NoWrap
    assert dialog.url.minimumHeight() == 132
    assert dialog.document_sources() == [
        "https://alidocs.dingtalk.com/i/nodes/first",
        "https://alidocs.dingtalk.com/i/nodes/second",
    ]
    assert dialog.document_source() == (
        "https://alidocs.dingtalk.com/i/nodes/first"
    )
    assert dialog.create_button.isEnabled()

    dialog.source_type.setCurrentIndex(dialog.source_type.findData("file"))
    assert dialog.url.isHidden()
    assert not dialog.file_control.isHidden()
    assert not dialog.create_button.isEnabled()
    dialog.select_file_button.click()
    app.processEvents()

    assert dialog.document_source_type() == "file"
    assert dialog.file_path.text() == str(requirement.resolve())
    assert dialog.document_source() == str(requirement.resolve())
    assert dialog.document_sources() == [str(requirement.resolve())]
    assert dialog.create_button.isEnabled()
    dialog.close()


def test_document_settings_exposes_user_templates_and_confirms_restore(
    app,
    monkeypatch,
    tmp_path,
):
    del app
    service = _TemplateService(tmp_path)
    host = _SynchronousHost()
    revealed: list[str] = []
    confirmations: list[tuple[str, str]] = []
    monkeypatch.setattr(
        document_page_module,
        "reveal_in_file_manager",
        lambda path: revealed.append(str(path)),
    )
    monkeypatch.setattr(
        document_page_module,
        "confirm_action",
        lambda _parent, title, message: (
            confirmations.append((title, message)) or True
        ),
    )
    panel = DocumentSettingsPanel(service, host)

    assert panel.view_content_template.text() == "查看默认模板文档"
    assert panel.restore_content_template.text() == "恢复默认"
    assert panel.view_document_template.text() == "查看默认模板文档"
    assert panel.restore_document_template.text() == "恢复默认"

    panel.view_content_template.click()
    panel.restore_document_template.click()

    assert revealed == [str(service.paths[CONTENT_TEMPLATE])]
    assert service.restored == [OUTPUT_TEMPLATE]
    assert confirmations == [
        (
            "恢复默认模板",
            "确定使用程序内置模板覆盖当前本地默认模板文档吗？此操作无法撤销。",
        )
    ]
    assert panel.connection_result.text() == "默认模板已恢复"
    assert host.errors == []
    panel.close()


def test_new_iteration_copy_has_traditional_chinese_and_english_catalogs():
    try:
        set_language("zh_TW")
        assert tr("需求文档") == "需求文件"
        assert tr("查看默认模板文档") == "查看預設範本文件"
        assert tr("恢复默认") == "恢復預設"
        assert localized_log_message(
            "区块图片部分下载失败（成功 2/5），继续处理可用图片与文本需求"
        ) == "區塊圖片部分下載失敗（成功 2/5），繼續處理可用圖片與文字需求"

        set_language("en_US")
        assert tr("需求文档") == "Requirements Document"
        assert tr("选择文件") == "Select File"
        assert tr("查看默认模板文档") == "Show Default Template"
        assert tr("恢复默认") == "Restore Default"
        assert localized_log_message(
            "区块图片下载失败，继续处理文本需求"
        ) == (
            "Block images could not be downloaded. "
            "Continuing with the text requirements."
        )
        assert tr(
            "选择环境、项目和分支后，任务名称会自动附加对应组合。"
        ) == (
            "Select an environment, project, and branch. "
            "The task name will include that combination automatically."
        )
    finally:
        set_language("zh_CN")
