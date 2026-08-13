"""Jenkins 快捷部署主页、首次配置引导和项目刷新状态。"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QDialog,
    QPushButton,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from windows_native.i18n import tr
from windows_native.jenkins.errors import JenkinsRefreshCancelled
from windows_native.ui.common import (
    BasePage,
    ThemedCheckBox,
    card,
    confirm_action,
    status_label,
)
from windows_native.ui.deployment_dialog import (
    NewDeploymentDialog,
    SingleDeploymentDialog,
)
from windows_native.ui.deployment_widgets import (
    DeploymentDetailsDialog,
    DeploymentLogsDialog,
    DeploymentTaskTable,
)
from windows_native.ui.project_refresh import ProjectRefreshStatus

if TYPE_CHECKING:
    from windows_native.jenkins.service import JenkinsDeploymentService


class QuickDeployPage(BasePage):
    """按 Token 配置状态切换引导页和快捷部署功能页。"""

    modify_config_requested = Signal()
    recycle_requested = Signal()
    new_deployment_requested = Signal()

    def __init__(
        self,
        service: JenkinsDeploymentService,
        *,
        auto_refresh: bool = True,
    ):
        super().__init__("快捷部署")
        self.service = service
        self._configuration_loading = False
        self._configuration_view: dict[str, Any] = {}
        self._project_snapshot: dict[str, Any] = {
            "last_refreshed_at": "",
            "projects": [],
        }
        self._task_settings: dict[str, Any] = {"show_prod": False}
        self._dialogs: set[NewDeploymentDialog | SingleDeploymentDialog] = set()
        self._refresh_serial = 0
        self._refresh_cancel_event: threading.Event | None = None
        self._refreshing = False

        self.mode_stack = QStackedWidget()
        self.loading_panel = self._build_loading_panel()
        self.guide_panel = self._build_guide_panel()
        self.feature_panel = self._build_feature_panel()
        self.mode_stack.addWidget(self.loading_panel)
        self.mode_stack.addWidget(self.guide_panel)
        self.mode_stack.addWidget(self.feature_panel)
        self.content.addWidget(self.mode_stack, 1)

        self.hourly_refresh_timer = QTimer(self)
        self.hourly_refresh_timer.setInterval(60 * 60 * 1000)
        self.hourly_refresh_timer.timeout.connect(
            lambda: self.refresh_projects(show_errors=False)
        )
        self.hourly_refresh_timer.start()

        self.task_poll_timer = QTimer(self)
        self.task_poll_timer.setInterval(750)
        self.task_poll_timer.timeout.connect(self.refresh_tasks)
        self.task_poll_timer.start()

        # 主窗口先完成显示，Keyring 读取再进入后台线程，避免拖慢首屏；隔离自检不读取用户配置。
        if auto_refresh:
            QTimer.singleShot(0, self.refresh)

    def _build_loading_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addStretch()
        label = status_label("正在读取 Jenkins 配置…")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        layout.addStretch()
        return panel

    def _build_guide_panel(self) -> QWidget:
        panel = QWidget()
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 20, 0, 0)
        outer.addStretch(1)

        guide_card = QFrame()
        guide_card.setObjectName("card")
        guide_card.setMaximumWidth(1080)
        guide_layout = QVBoxLayout(guide_card)
        guide_layout.setContentsMargins(28, 28, 28, 26)
        guide_layout.setSpacing(18)

        title = QLabel("请先配置 Jenkins API Token")
        title.setObjectName("guideTitle")
        title.setAlignment(Qt.AlignCenter)
        guide_layout.addWidget(title)

        form_row = QHBoxLayout()
        form_row.setSpacing(10)
        address_column, self.guide_address, _address_hint = self._guide_field(
            "Jenkins 地址",
            "例如：http://jenkins.example.com:8080/",
            minimum_width=250,
        )
        (
            username_column,
            self.guide_username,
            self.guide_username_hint,
        ) = self._guide_field(
            "Jenkins 用户名",
            "用于 API Token 认证的账号",
            minimum_width=170,
            hint="用户登陆账号",
        )
        token_column = QWidget()
        token_layout = QVBoxLayout(token_column)
        token_layout.setContentsMargins(0, 0, 0, 0)
        token_layout.setSpacing(5)
        token_label = QLabel("API Token")
        self.guide_token = QLineEdit()
        self.guide_token.setAccessibleName("API Token")
        self.guide_token.setPlaceholderText("只保存在 Windows 凭据管理器")
        self.guide_token.setEchoMode(QLineEdit.Password)
        self.guide_token.setMinimumWidth(250)
        self.guide_hint = status_label(
            "获取路径：右上角用户名 → Security → API Token → Add new Token → Generate"
        )
        self.guide_hint.setObjectName("fieldHint")
        token_layout.addWidget(token_label)
        token_layout.addWidget(self.guide_token)
        token_layout.addWidget(self.guide_hint)

        self.guide_confirm = QPushButton("确定")
        self.guide_confirm.setObjectName("primary")
        self.guide_confirm.setEnabled(False)
        self.guide_confirm.clicked.connect(self._save_guide_configuration)
        button_column = QWidget()
        button_layout = QVBoxLayout(button_column)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.addWidget(QLabel(" "))
        button_layout.addWidget(self.guide_confirm)
        button_layout.addStretch()

        form_row.addWidget(address_column, 3)
        form_row.addWidget(username_column, 2)
        form_row.addWidget(token_column, 3)
        form_row.addWidget(button_column)
        guide_layout.addLayout(form_row)

        self.guide_status = status_label()
        self.guide_status.setAlignment(Qt.AlignCenter)
        guide_layout.addWidget(self.guide_status)

        centered = QHBoxLayout()
        centered.addStretch(1)
        centered.addWidget(guide_card, 8)
        centered.addStretch(1)
        outer.addLayout(centered)
        outer.addStretch(2)

        for field in (self.guide_address, self.guide_username, self.guide_token):
            field.textChanged.connect(self._update_guide_confirm)
        return panel

    @staticmethod
    def _guide_field(
        label: str,
        placeholder: str,
        *,
        minimum_width: int,
        hint: str = "",
    ) -> tuple[QWidget, QLineEdit, QLabel | None]:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addWidget(QLabel(label))
        field = QLineEdit()
        field.setAccessibleName(label)
        field.setPlaceholderText(placeholder)
        field.setMinimumWidth(minimum_width)
        layout.addWidget(field)
        # 输入框下方保留独立暗文，用户输入内容后仍能看到字段含义。
        hint_label = status_label(hint) if hint else None
        if hint_label is not None:
            hint_label.setObjectName("fieldHint")
            layout.addWidget(hint_label)
        layout.addStretch()
        return container, field, hint_label

    def _build_feature_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        refresh_row = QHBoxLayout()
        refresh_row.addStretch()
        self.refresh_projects_button = QPushButton(tr("刷新项目 ⟳"))
        self.refresh_projects_button.clicked.connect(
            lambda _checked=False: self.request_project_refresh()
        )
        self.project_refresh_status = ProjectRefreshStatus()
        # 保留两个属性别名，便于既有界面测试和辅助功能继续定位对应控件。
        self.refresh_indicator = self.project_refresh_status.indicator
        self.last_refresh_label = self.project_refresh_status.label
        refresh_row.addWidget(self.refresh_projects_button)
        refresh_row.addWidget(self.project_refresh_status)
        layout.addLayout(refresh_row)

        overview_card, overview_layout = card("部署概览")
        metrics = QHBoxLayout()
        self.active_deployments = QLabel("进行中：0")
        self.active_deployments.setObjectName("metric")
        self.queued_deployments = QLabel("排队中：0")
        self.queued_deployments.setObjectName("metric")
        self.scheduled_deployments = QLabel("定时任务：0")
        self.scheduled_deployments.setObjectName("metric")
        self.project_count_label = status_label("可部署项目：0")
        metrics.addWidget(self.active_deployments)
        metrics.addSpacing(24)
        metrics.addWidget(self.queued_deployments)
        metrics.addSpacing(24)
        metrics.addWidget(self.scheduled_deployments)
        metrics.addStretch()
        metrics.addWidget(self.project_count_label)
        overview_layout.addLayout(metrics)

        # 快捷操作与提示合并为概览第二行，第一行统计项的横向关系保持不变。
        action_bar = QWidget()
        action_layout = QHBoxLayout(action_bar)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(8)
        self.security_warning = status_label()
        self.security_warning.setObjectName("securityWarning")
        self.new_deployment_button = QPushButton("新建迭代部署")
        self.new_deployment_button.setObjectName("primary")
        self.single_deployment_button = QPushButton("单点部署")
        self.modify_config_button = QPushButton("修改配置")
        self.new_deployment_button.clicked.connect(self.open_new_deployment)
        self.single_deployment_button.clicked.connect(self.open_single_deployment)
        self.modify_config_button.clicked.connect(self.modify_config_requested.emit)
        action_layout.addWidget(self.security_warning, 1)
        action_layout.addStretch()
        action_layout.addWidget(self.single_deployment_button)
        action_layout.addWidget(self.new_deployment_button)
        action_layout.addWidget(self.modify_config_button)
        overview_layout.addWidget(action_bar)
        layout.addWidget(overview_card)

        tasks_card, tasks_layout = card("迭代部署任务")
        filter_row = QHBoxLayout()
        filter_row.addStretch()
        self.hide_single_deployments = ThemedCheckBox("隐藏单点部署")
        self.hide_single_deployments.toggled.connect(
            lambda _checked: self.refresh_tasks()
        )
        filter_row.addWidget(self.hide_single_deployments)
        tasks_layout.addLayout(filter_row)
        self.task_table = DeploymentTaskTable(
            on_details=self.open_task_details,
            on_logs=self.open_task_logs,
            on_stop=self.stop_task,
            on_retry=self.retry_task,
            on_delete=self.delete_task,
        )
        self.task_table.setMinimumHeight(280)
        tasks_layout.addWidget(self.task_table)
        recycle_row = QHBoxLayout()
        recycle_row.addStretch()
        self.recycle_button = QPushButton("回收站")
        self.recycle_button.setObjectName("iconButton")
        self.recycle_button.setAccessibleName("回收站")
        self.recycle_button.setToolTip("回收站")
        self.recycle_button.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon))
        self.recycle_button.clicked.connect(self.recycle_requested.emit)
        recycle_row.addWidget(self.recycle_button)
        tasks_layout.addLayout(recycle_row)
        layout.addWidget(tasks_card)
        return panel

    def refresh(self) -> None:
        """异步读取配置和缓存，决定显示引导页或功能页。"""

        if self._configuration_loading:
            return
        self._configuration_loading = True

        def success(view: dict[str, Any]) -> None:
            self._configuration_view = dict(view)
            if view.get("configured"):
                self.mode_stack.setCurrentWidget(self.feature_panel)
                self.security_warning.setText(str(view.get("security_warning") or ""))
                self.run_async(
                    self.service.task_settings_view,
                    success=lambda settings: setattr(
                        self,
                        "_task_settings",
                        dict(settings),
                    ),
                    failure=lambda _message: None,
                )
                self._load_project_snapshot(trigger_refresh=True)
                self.refresh_tasks()
            else:
                self.mode_stack.setCurrentWidget(self.guide_panel)

        self.run_async(
            self.service.configuration_view,
            success=success,
            finished=lambda: setattr(self, "_configuration_loading", False),
        )

    def _load_project_snapshot(self, *, trigger_refresh: bool) -> None:
        if trigger_refresh:
            # 首次配置后立即进入明确的刷新中状态，避免空缓存窗口被误认为连接失败。
            self._set_refreshing(True)

        def success(snapshot: dict[str, Any]) -> None:
            self._apply_project_snapshot(snapshot)
            if trigger_refresh and self._refresh_cancel_event is None:
                self.refresh_projects(show_errors=True)

        self.run_async(
            self.service.project_snapshot,
            success=success,
            failure=lambda _message: (
                self.refresh_projects(show_errors=True)
                if trigger_refresh and self._refresh_cancel_event is None
                else None
            ),
        )

    def apply_configuration_view(self, view: dict[str, Any]) -> None:
        """配置页保存成功后立即同步，不等待下一轮 Keyring 读取。"""

        self._configuration_view = dict(view)
        self.security_warning.setText(str(view.get("security_warning") or ""))
        if view.get("configured"):
            self.mode_stack.setCurrentWidget(self.feature_panel)
        else:
            self.mode_stack.setCurrentWidget(self.guide_panel)

    def apply_task_settings(self, settings: dict[str, Any]) -> None:
        self._task_settings = dict(settings)

    def apply_startup_snapshot(
        self,
        *,
        configuration: dict[str, Any],
        task_settings: dict[str, Any],
        projects: dict[str, Any],
        tasks: list[dict[str, Any]],
    ) -> None:
        """一次性应用启动页预热结果，主窗口出现后不再补读本地文件。"""

        self.apply_configuration_view(configuration)
        self.apply_task_settings(task_settings)
        self._apply_project_snapshot(projects)
        self._apply_deployment_tasks(tasks)

    def start_remote_refresh(self) -> None:
        """主界面稳定后仅刷新外部 Jenkins 数据，不再触发本地初始化。"""

        if self._configuration_view.get("configured"):
            self.refresh_projects(show_errors=False)

    def _update_guide_confirm(self) -> None:
        enabled = all(
            field.text().strip()
            for field in (self.guide_address, self.guide_username, self.guide_token)
        )
        self.guide_confirm.setEnabled(enabled)

    def _save_guide_configuration(self) -> None:
        address = self.guide_address.text().strip()
        username = self.guide_username.text().strip()
        token = self.guide_token.text().strip()
        self.guide_confirm.setEnabled(False)
        self.guide_status.setText(tr("正在验证 Jenkins 连接…"))

        def success(view: dict[str, Any]) -> None:
            self._configuration_view = dict(view)
            self.guide_token.clear()
            self.security_warning.setText(str(view.get("security_warning") or ""))
            self.mode_stack.setCurrentWidget(self.feature_panel)
            self.guide_status.setText("")
            self._load_project_snapshot(trigger_refresh=True)

        def failure(message: str) -> None:
            self.guide_status.setText(tr("连接失败：{message}", message=message))

        self.run_async(
            lambda: self.service.validate_and_save_configuration(
                address,
                username,
                token,
            ),
            success=success,
            failure=failure,
            finished=self._update_guide_confirm,
        )

    def refresh_projects(self, *, show_errors: bool = True) -> None:
        """取消旧刷新并启动最新请求，只有最新序号可以更新界面。"""

        if not self._configuration_view.get("configured"):
            return
        if self._refresh_cancel_event is not None:
            self._refresh_cancel_event.set()
        self._refresh_serial += 1
        serial = self._refresh_serial
        cancel_event = threading.Event()
        self._refresh_cancel_event = cancel_event
        self._set_refreshing(True)

        def success(snapshot: dict[str, Any]) -> None:
            if serial == self._refresh_serial and not cancel_event.is_set():
                self._apply_project_snapshot(snapshot)

        def failure(message: str) -> None:
            if serial != self._refresh_serial or cancel_event.is_set():
                return
            if show_errors and message != str(JenkinsRefreshCancelled()):
                self.show_error(message)

        def finished() -> None:
            if serial == self._refresh_serial:
                self._refresh_cancel_event = None
                self._set_refreshing(False)

        self.run_async(
            lambda: self.service.refresh_projects(cancel_event=cancel_event),
            success=success,
            failure=failure,
            finished=finished,
        )

    def request_project_refresh(self) -> None:
        """手动刷新时允许用户确认后替换正在执行的旧请求。"""

        confirmation_parent = QApplication.activeModalWidget() or self
        if self._refreshing and not confirm_action(
            confirmation_parent,
            "重新刷新项目",
            "项目刷新中，是否重新刷新？",
        ):
            return
        self.refresh_projects(show_errors=True)

    def _apply_project_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._project_snapshot = dict(snapshot)
        projects = [
            item
            for item in snapshot.get("projects") or []
            if isinstance(item, dict)
        ]
        eligible_count = sum(bool(item.get("eligible")) for item in projects)
        self.project_count_label.setText(
            tr("可部署项目：{count}", count=eligible_count)
        )
        refreshed_at = str(snapshot.get("last_refreshed_at") or "")
        self.project_refresh_status.set_last_refreshed_at(refreshed_at)
        for dialog in tuple(self._dialogs):
            dialog.update_snapshot(self._project_snapshot)

    def open_new_deployment(self) -> None:
        """打开层级选择器，确认后只创建本地任务记录。"""

        if self._refreshing and not str(
            self._project_snapshot.get("last_refreshed_at") or ""
        ):
            self.show_info(tr("Jenkins项目刷新中，请稍候"))
            return

        dialog = NewDeploymentDialog(
            self._project_snapshot,
            on_refresh=self.request_project_refresh,
            show_prod=bool(self._task_settings.get("show_prod", False)),
            parent=self,
        )
        self._dialogs.add(dialog)
        try:
            if dialog.exec() != QDialog.Accepted:
                return
            self.service.create_deployment_task(
                dialog.iteration_name.text().strip(),
                dialog.selections(),
                schedule=dialog.schedule_value(),
            )
            self.refresh_tasks()
            self.new_deployment_requested.emit()
        except Exception as exc:
            self.show_error(str(exc))
        finally:
            self._dialogs.discard(dialog)
            dialog.deleteLater()

    def open_single_deployment(self) -> None:
        """创建单个环境、项目和分支组合，名称由系统固定。"""

        if self._refreshing and not str(
            self._project_snapshot.get("last_refreshed_at") or ""
        ):
            self.show_info(tr("Jenkins项目刷新中，请稍候"))
            return
        dialog = SingleDeploymentDialog(
            self._project_snapshot,
            on_refresh=self.request_project_refresh,
            show_prod=bool(self._task_settings.get("show_prod", False)),
            parent=self,
        )
        self._dialogs.add(dialog)
        try:
            if dialog.exec() != QDialog.Accepted:
                return
            selection = dialog.selections()[0]
            self.service.create_single_deployment_task(selection)
            self.refresh_tasks()
            self.new_deployment_requested.emit()
        except Exception as exc:
            self.show_error(str(exc))
        finally:
            self._dialogs.discard(dialog)
            dialog.deleteLater()

    def refresh_tasks(self) -> None:
        """刷新本地部署任务列表；不会因轮询访问 Jenkins。"""

        list_tasks = getattr(self.service, "list_deployment_tasks", None)
        if not callable(list_tasks):
            return
        try:
            tasks = list_tasks(trashed=False)
        except Exception:
            return
        self._apply_deployment_tasks(tasks)

    def _apply_deployment_tasks(self, tasks: list[dict[str, Any]]) -> None:
        """把已读取的任务快照映射到统计项与列表，避免重复磁盘访问。"""

        active = sum(
            str(item.get("status")) in {"running", "stopping"}
            for item in tasks
        )
        queued = sum(str(item.get("status")) == "queued" for item in tasks)
        scheduled = sum(
            bool((item.get("schedule") or {}).get("enabled"))
            and str((item.get("schedule") or {}).get("state"))
            in {"waiting", "running"}
            and str(item.get("status"))
            in {"scheduled", "running", "stopping"}
            for item in tasks
        )
        self.active_deployments.setText(tr("进行中：{count}", count=active))
        self.queued_deployments.setText(tr("排队中：{count}", count=queued))
        self.scheduled_deployments.setText(
            tr("定时任务：{count}", count=scheduled)
        )
        visible_tasks = tasks
        if self.hide_single_deployments.isChecked():
            # 只隐藏明确标记的单点任务，避免误伤同名的历史迭代任务。
            visible_tasks = [
                item
                for item in tasks
                if str(item.get("deployment_type") or "iteration") != "single"
            ]
        self.task_table.set_tasks(visible_tasks)

    def open_task_details(self, task: dict) -> None:
        latest = self.service.deployment_task(str(task.get("task_id") or ""))
        dialog = DeploymentDetailsDialog(latest or task, self)
        dialog.exec()

    def open_task_logs(self, task: dict) -> None:
        task_id = str(task.get("task_id") or "")
        dialog = DeploymentLogsDialog(self.service, task_id, self)
        dialog.exec()

    def stop_task(self, task_id: str) -> None:
        if not confirm_action(
            self,
            "停止部署任务",
            "确定停止当前迭代部署吗？正在运行的 Jenkins 构建将被终止。",
        ):
            return
        try:
            self.service.stop_deployment_task(task_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.refresh_tasks()

    def retry_task(self, task_id: str) -> None:
        """后台重新校验所有分支，避免网络检查阻塞主线程。"""

        self.run_async(
            lambda: self.service.retry_deployment_task(task_id),
            success=lambda _task: self.refresh_tasks(),
        )

    def delete_task(self, task_id: str) -> None:
        if not confirm_action(
            self,
            "删除部署任务",
            "确定将这个迭代部署任务移入回收站吗？Jenkins 构建不会被删除。",
        ):
            return
        try:
            self.service.trash_deployment_task(task_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.refresh_tasks()

    def _set_refreshing(self, refreshing: bool) -> None:
        self._refreshing = bool(refreshing)
        self.project_refresh_status.set_refreshing(refreshing)
        for dialog in tuple(self._dialogs):
            dialog.set_refreshing(refreshing)

    def shutdown(self) -> None:
        """关闭窗口时使未完成刷新失效；后台请求会在短超时内自然退出。"""

        if self._refresh_cancel_event is not None:
            self._refresh_cancel_event.set()
        self.hourly_refresh_timer.stop()
        self.project_refresh_status.shutdown()
        self.task_poll_timer.stop()
