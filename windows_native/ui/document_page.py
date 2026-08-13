"""钉钉文档与输出配置页面。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from windows_native.ui.common import (
    BasePage,
    ThemedCheckBox,
    button_row,
    card,
    confirm_action,
    reveal_in_file_manager,
    status_label,
)
from windows_native.i18n import tr
from utils.default_templates import CONTENT_TEMPLATE, OUTPUT_TEMPLATE

if TYPE_CHECKING:
    from windows_native.native_service import NativeService


class DocumentSettingsPanel(QWidget):
    """可嵌入标签页的文档配置主体，不创建额外滚动区域。"""

    def __init__(self, service: NativeService, host: BasePage):
        super().__init__()
        self.service = service
        self.host = host
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 18, 8, 8)
        outer.setSpacing(18)

        form_card, layout = card("钉钉与输出设置")
        form = QFormLayout()
        form.setSpacing(12)
        self.document_mcp = QLineEdit()
        self.document_mcp.setEchoMode(QLineEdit.Password)
        self.document_mcp.setPlaceholderText("留空表示保留已保存值")
        self.document_status = status_label()
        self.clear_document = ThemedCheckBox("清除已保存的文档 MCP")
        form.addRow("文档 MCP", self.document_mcp)
        form.addRow("", self.document_status)
        form.addRow("", self.clear_document)

        self.spreadsheet_mcp = QLineEdit()
        self.spreadsheet_mcp.setEchoMode(QLineEdit.Password)
        self.spreadsheet_mcp.setPlaceholderText("留空表示保留已保存值")
        self.spreadsheet_status = status_label()
        self.clear_spreadsheet = ThemedCheckBox("清除已保存的表格 MCP")
        form.addRow("表格 MCP", self.spreadsheet_mcp)
        form.addRow("", self.spreadsheet_status)
        form.addRow("", self.clear_spreadsheet)

        self.content_template = QLineEdit()
        self.document_template = QLineEdit()
        self.output_folder = QLineEdit()
        self.local_output = QLineEdit()
        (
            content_template_control,
            self.view_content_template,
            self.restore_content_template,
        ) = self._template_control(self.content_template, CONTENT_TEMPLATE)
        (
            document_template_control,
            self.view_document_template,
            self.restore_document_template,
        ) = self._template_control(self.document_template, OUTPUT_TEMPLATE)
        form.addRow("用例模板表格", content_template_control)
        form.addRow("输出文档模板", document_template_control)
        form.addRow("输出文件夹", self.output_folder)
        form.addRow("本地备份目录", self.local_output)
        layout.addLayout(form)

        self.connection_result = status_label()
        layout.addWidget(self.connection_result)
        self.test_button = QPushButton("测试当前配置")
        self.save_button = QPushButton("保存配置")
        self.save_button.setObjectName("primary")
        self.test_button.clicked.connect(self.test_connection)
        self.save_button.clicked.connect(self.save)
        layout.addWidget(button_row(self.test_button, self.save_button))
        outer.addWidget(form_card)
        outer.addStretch()
        self._loaded = False

    def _template_control(
        self,
        line_edit: QLineEdit,
        template_type: str,
    ) -> tuple[QWidget, QPushButton, QPushButton]:
        """把在线地址与本地模板操作放在同一字段内，保持页面简洁。"""

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(line_edit)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(14)
        view = QPushButton(tr("查看默认模板文档"))
        restore = QPushButton(tr("恢复默认"))
        for button in (view, restore):
            button.setObjectName("templateLink")
            button.setCursor(Qt.PointingHandCursor)
        view.clicked.connect(
            lambda _checked=False, value=template_type: self._view_template(value)
        )
        restore.clicked.connect(
            lambda _checked=False, value=template_type, control=restore:
            self._restore_template(value, control)
        )
        actions.addWidget(view)
        actions.addWidget(restore)
        actions.addStretch(1)
        layout.addLayout(actions)
        return container, view, restore

    def _view_template(self, template_type: str) -> None:
        try:
            path = self.service.default_template_path(template_type)
            reveal_in_file_manager(path)
        except Exception as exc:
            self.host.show_error(str(exc))

    def _restore_template(
        self,
        template_type: str,
        button: QPushButton,
    ) -> None:
        if not confirm_action(
            self,
            "恢复默认模板",
            "确定使用程序内置模板覆盖当前本地默认模板文档吗？此操作无法撤销。",
        ):
            return
        button.setEnabled(False)
        self.connection_result.setText(tr("正在恢复默认模板…"))
        self.host.run_async(
            lambda: self.service.restore_default_template(template_type),
            success=lambda _path: self.connection_result.setText(
                tr("默认模板已恢复")
            ),
            finished=lambda: button.setEnabled(True),
        )

    def refresh(self) -> None:
        self.load()

    def load(self) -> None:
        self.host.run_async(self.service.get_document, success=self._apply_view)

    def _apply_view(self, view: dict) -> None:
        self.content_template.setText(str(view.get("content_template_url") or ""))
        self.document_template.setText(str(view.get("document_template_url") or ""))
        self.output_folder.setText(str(view.get("output_folder_url") or ""))
        self.local_output.setText(str(view.get("local_output_dir") or "./output"))
        doc = view.get("document_mcp") or {}
        sheet = view.get("spreadsheet_mcp") or {}
        self.document_status.setText(
            tr("已保存：{value}", value=doc.get("masked_value"))
            if doc.get("configured")
            else tr("尚未配置")
        )
        self.spreadsheet_status.setText(
            tr("已保存：{value}", value=sheet.get("masked_value"))
            if sheet.get("configured")
            else tr("尚未配置")
        )
        self.document_mcp.clear()
        self.spreadsheet_mcp.clear()
        self.clear_document.setChecked(False)
        self.clear_spreadsheet.setChecked(False)
        self._loaded = True

    def _values(self) -> dict:
        """在 GUI 线程一次性读取控件，后台函数不再访问 Qt 对象。"""

        return {
            "content_template_url": self.content_template.text().strip(),
            "document_template_url": self.document_template.text().strip(),
            "output_folder_url": self.output_folder.text().strip(),
            "local_output_dir": self.local_output.text().strip(),
            "document_mcp_url": self.document_mcp.text().strip() or None,
            "spreadsheet_mcp_url": self.spreadsheet_mcp.text().strip() or None,
            "clear_document_mcp_url": self.clear_document.isChecked(),
            "clear_spreadsheet_mcp_url": self.clear_spreadsheet.isChecked(),
        }

    def save(self) -> None:
        values = self._values()
        self.save_button.setEnabled(False)
        self.connection_result.setText(tr("正在保存…"))
        self.host.run_async(
            lambda: self.service.save_document(values),
            success=lambda view: (
                self._apply_view(view),
                self.connection_result.setText(tr("配置已保存")),
            ),
            finished=lambda: self.save_button.setEnabled(True),
        )

    def test_connection(self) -> None:
        values = self._values()
        self.test_button.setEnabled(False)
        self.connection_result.setText(tr("正在创建并清理连接测试文件，请稍候…"))

        def success(result: dict) -> None:
            checks = result.get("checks") or []
            details = [
                f"{'✓' if item.get('ok') else '✗'} {item.get('name')}"
                + (f"：{item.get('detail')}" if item.get("detail") else "")
                for item in checks
            ]
            self.connection_result.setText(
                (tr("连接测试通过") if result.get("ok") else tr("连接测试未通过"))
                + ("\n" + "\n".join(details) if details else "")
            )

        self.host.run_async(
            lambda: self.service.test_document(values),
            success=success,
            finished=lambda: self.test_button.setEnabled(True),
        )


class DocumentPage(BasePage):
    """保留独立页面包装，供旧导航或外部调用继续使用。"""

    def __init__(self, service: NativeService):
        super().__init__(
            "文档配置",
            "配置钉钉文档与表格 MCP、模板地址和本地备份目录。",
        )
        self.service = service
        self.panel = DocumentSettingsPanel(service, self)
        # 保留旧页面上常用的控件属性，兼容已有自动化和调用代码。
        for name in (
            "document_mcp",
            "document_status",
            "clear_document",
            "spreadsheet_mcp",
            "spreadsheet_status",
            "clear_spreadsheet",
            "content_template",
            "document_template",
            "view_content_template",
            "restore_content_template",
            "view_document_template",
            "restore_document_template",
            "output_folder",
            "local_output",
            "connection_result",
            "test_button",
            "save_button",
        ):
            setattr(self, name, getattr(self.panel, name))
        self.content.addWidget(self.panel)
        self.add_stretch()

    def refresh(self) -> None:
        self.panel.refresh()

    def load(self) -> None:
        self.panel.load()

    def _apply_view(self, view: dict) -> None:
        self.panel._apply_view(view)

    def _values(self) -> dict:
        return self.panel._values()

    def save(self) -> None:
        self.panel.save()

    def test_connection(self) -> None:
        self.panel.test_connection()
