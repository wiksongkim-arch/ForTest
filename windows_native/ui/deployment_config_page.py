"""快捷部署的 Jenkins 连接与安全任务配置页面。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from windows_native.i18n import tr, translate_widget_tree
from windows_native.ui.common import (
    BasePage,
    SmoothTabWidget,
    ThemedCheckBox,
    button_row,
    card,
    status_label,
)


class JenkinsConfigPanel(QWidget):
    """修改连接时允许 Token 留空，明确表示保留 Windows 凭据。"""

    saved = Signal(dict)

    def __init__(self, service, page: BasePage):
        super().__init__()
        self.service = service
        self.page = page
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 18, 8, 8)
        layout.setSpacing(14)
        config_card, config_layout = card("Jenkins 配置")
        form = QFormLayout()
        self.address = QLineEdit()
        self.address.setPlaceholderText("例如：http://jenkins.example.com:8080/")
        self.username = QLineEdit()
        self.username.setPlaceholderText("用于 API Token 认证的账号")
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("留空表示保留已保存 Token")
        form.addRow("Jenkins 地址", self.address)
        form.addRow("Jenkins 用户名", self.username)
        form.addRow("API Token", self.token)
        config_layout.addLayout(form)
        hint = status_label(
            "获取路径：右上角用户名 → Security → API Token → Add new Token → Generate"
        )
        hint.setObjectName("fieldHint")
        config_layout.addWidget(hint)
        self.security_warning = status_label()
        self.security_warning.setObjectName("securityWarning")
        config_layout.addWidget(self.security_warning)
        self.status = status_label()
        config_layout.addWidget(self.status)
        self.confirm = QPushButton("确定")
        self.confirm.setObjectName("primary")
        self.confirm.clicked.connect(self.save)
        config_layout.addWidget(button_row(self.confirm))
        layout.addWidget(config_card)
        layout.addStretch()
        for field in (self.address, self.username):
            field.textChanged.connect(self._update_confirm)

    def load(self) -> None:
        self.confirm.setEnabled(False)
        self.status.setText(tr("正在读取 Jenkins 配置…"))

        def success(view: dict) -> None:
            self.address.setText(str(view.get("base_url") or ""))
            self.username.setText(str(view.get("username") or ""))
            self.token.clear()
            self.security_warning.setText(str(view.get("security_warning") or ""))
            self.status.setText(
                tr("已保存：{value}", value=str(view.get("token_mask") or ""))
                if view.get("configured")
                else tr("尚未配置")
            )
            self._update_confirm()

        self.page.run_async(self.service.configuration_view, success=success)

    def save(self) -> None:
        self.confirm.setEnabled(False)
        self.status.setText(tr("正在验证 Jenkins 连接…"))

        def success(view: dict) -> None:
            self.token.clear()
            self.security_warning.setText(str(view.get("security_warning") or ""))
            self.status.setText(tr("Jenkins 配置已保存"))
            self.saved.emit(dict(view))

        def failure(message: str) -> None:
            self.status.setText(tr("连接失败：{message}", message=message))

        self.page.run_async(
            lambda: self.service.validate_and_save_configuration(
                self.address.text().strip(),
                self.username.text().strip(),
                self.token.text().strip(),
                keep_saved_token=True,
            ),
            success=success,
            failure=failure,
            finished=self._update_confirm,
        )

    def _update_confirm(self) -> None:
        self.confirm.setEnabled(
            bool(self.address.text().strip() and self.username.text().strip())
        )


class DeploymentTaskConfigPanel(QWidget):
    """说明固定编排策略，只保留生产环境可见性设置。"""

    saved = Signal(dict)

    def __init__(self, service, page: BasePage):
        super().__init__()
        self.service = service
        self.page = page
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 18, 8, 8)
        layout.setSpacing(14)
        task_card, task_layout = card("任务配置")
        orchestration = status_label(
            "迭代部署任务之间保持串行；同一任务内的部署子任务会并行提交到 Jenkins 队列。"
        )
        task_layout.addWidget(orchestration)
        self.show_prod = ThemedCheckBox("在项目选择器中显示 prod 生产环境")
        task_layout.addWidget(self.show_prod)
        warning = status_label(
            "生产环境默认隐藏。启用后仍需 Jenkins 账号具备对应任务的 Job/Build 权限。"
        )
        warning.setObjectName("securityWarning")
        task_layout.addWidget(warning)
        self.status = status_label()
        task_layout.addWidget(self.status)
        self.save_button = QPushButton("保存任务配置")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        task_layout.addWidget(button_row(self.save_button))
        layout.addWidget(task_card)
        layout.addStretch()

    def load(self) -> None:
        try:
            settings = self.service.task_settings_view()
        except Exception as exc:
            self.page.show_error(str(exc))
            return
        self.show_prod.setChecked(bool(settings.get("show_prod", False)))
        self.status.setText("")

    def save(self) -> None:
        try:
            settings = self.service.save_task_settings(
                show_prod=self.show_prod.isChecked()
            )
        except Exception as exc:
            self.page.show_error(str(exc))
            return
        self.status.setText(tr("任务配置已保存"))
        self.saved.emit(dict(settings))


class DeploymentConfigPage(BasePage):
    """快捷部署内部配置路由。"""

    back_requested = Signal()
    configuration_saved = Signal(dict)
    task_settings_saved = Signal(dict)

    def __init__(self, service):
        super().__init__("修改配置")
        self.service = service
        self.back_button = QPushButton("← 返回")
        self.back_button.setObjectName("backButton")
        self.back_button.clicked.connect(self.back_requested.emit)
        self.content.insertWidget(0, self.back_button, 0, Qt.AlignLeft)
        self.tabs = SmoothTabWidget()
        self.jenkins_panel = JenkinsConfigPanel(service, self)
        self.task_panel = DeploymentTaskConfigPanel(service, self)
        self.tabs.addTab(self.jenkins_panel, tr("Jenkins 配置"))
        self.tabs.addTab(self.task_panel, tr("任务配置"))
        self.content.addWidget(self.tabs, 1)
        self.jenkins_panel.saved.connect(self.configuration_saved.emit)
        self.task_panel.saved.connect(self.task_settings_saved.emit)

    def refresh(self) -> None:
        self.jenkins_panel.load()
        self.task_panel.load()

    def retranslate(self) -> None:
        translate_widget_tree(self)
