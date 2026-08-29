"""新建迭代部署任务的环境、项目、分支层级选择器。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QDateTime, QTime, Qt
from PySide6.QtWidgets import (
    QCompleter,
    QDialog,
    QDateTimeEdit,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from windows_native.i18n import tr
from windows_native.jenkins.scheduling import normalize_schedule
from windows_native.ui.common import (
    ManualSpinBox,
    SmoothComboBox,
    ThemedCheckBox,
    status_label,
)
from windows_native.ui.project_refresh import ProjectRefreshStatus


SCHEDULE_CONTROL_HEIGHT = 40
DEPLOYMENT_DIALOG_WIDTH = 1040


def _configure_searchable_combo(combo: SmoothComboBox, placeholder: str) -> None:
    """让部署选项支持不改写目录数据的大小写无关包含搜索。"""

    combo.setEditable(True)
    combo.setInsertPolicy(SmoothComboBox.NoInsert)
    editor = combo.lineEdit()
    if editor is not None:
        editor.setPlaceholderText(placeholder)
        editor.setClearButtonEnabled(True)

        def invalidate_typed_selection(text: str) -> None:
            # 用户开始搜索时立即撤销旧选项，避免未选中搜索结果却提交旧数据。
            index = combo.currentIndex()
            if index >= 0 and combo.itemText(index) != text:
                combo.setCurrentIndex(-1)
                editor.setText(text)

        editor.textEdited.connect(invalidate_typed_selection)

    completer = combo.completer()
    if completer is not None:
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)

        def select_completion(text: str) -> None:
            # 只接受目录中的完整选项，禁止把任意输入当成项目或分支提交。
            index = combo.findText(str(text), Qt.MatchFlag.MatchExactly)
            if index >= 0:
                combo.setCurrentIndex(index)

        completer.activated[str].connect(select_completion)


def format_schedule_range(start: datetime, end: datetime) -> str:
    """使用与日期时间选择器一致的格式展示完整定时范围。"""

    return f"{start:%Y-%m-%d %H:%M} — {end:%Y-%m-%d %H:%M}"


def deployment_catalog(
    snapshot: dict[str, Any],
    *,
    show_prod: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """把 Jenkins 项目快照整理成以环境为根节点的稳定目录。"""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for project in snapshot.get("projects") or []:
        if not isinstance(project, dict) or not project.get("eligible"):
            continue
        full_name = str(project.get("full_name") or "").strip()
        if not full_name:
            continue
        description = str(project.get("description") or "").strip()
        branches = sorted(
            _unique(project.get("target_branches") or []),
            key=_name_sort_key,
        )
        if not branches:
            continue
        option = {
            "full_name": full_name,
            "name": str(project.get("name") or full_name.rsplit("/", 1)[-1]),
            "description": description,
            "label": tr(
                "{project}（描述：{description}）",
                project=full_name,
                description=description or tr("无"),
            ),
            "branches": branches,
        }
        for environment in _unique(project.get("environments") or []):
            if not show_prod and environment.casefold() == "prod":
                continue
            grouped.setdefault(environment, []).append(dict(option))
    for projects in grouped.values():
        projects.sort(key=lambda item: _name_sort_key(item["full_name"]))
    return dict(sorted(grouped.items(), key=lambda item: _name_sort_key(item[0])))


def _unique(values) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _name_sort_key(value: Any) -> tuple[str, str]:
    """提供大小写无关且结果稳定的名称排序键。"""

    text = str(value)
    return text.casefold(), text


class ScheduleRangeDialog(QDialog):
    """从触发按钮上方向上展开的开始、结束日期时间选择弹窗。"""

    def __init__(
        self,
        start: datetime,
        end: datetime,
        *,
        anchor: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.anchor = anchor
        self.setWindowTitle(tr("选择定时部署日期范围"))
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        heading = QLabel(tr("选择定时部署日期范围"))
        heading.setObjectName("cardTitle")
        layout.addWidget(heading)

        fields = QHBoxLayout()
        self.start_edit = self._date_time_edit(start)
        self.end_edit = self._date_time_edit(end)
        start_column = QVBoxLayout()
        start_column.addWidget(QLabel(tr("开始时间")))
        start_column.addWidget(self.start_edit)
        end_column = QVBoxLayout()
        end_column.addWidget(QLabel(tr("结束时间")))
        end_column.addWidget(self.end_edit)
        fields.addLayout(start_column)
        fields.addLayout(end_column)
        layout.addLayout(fields)
        self.hint = status_label(tr("开始至结束最长可选择 30 天"))
        layout.addWidget(self.hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Cancel).setText(tr("取消"))
        buttons.button(QDialogButtonBox.Ok).setText(tr("确定"))
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self._validate)
        layout.addWidget(buttons)
        self.start_edit.dateTimeChanged.connect(self._limit_end)
        self._limit_end(self.start_edit.dateTime())

    @staticmethod
    def _date_time_edit(value: datetime) -> QDateTimeEdit:
        edit = QDateTimeEdit(QDateTime(value))
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        edit.setMinimumWidth(186)
        return edit

    def _limit_end(self, start: QDateTime) -> None:
        """联动限制最大 30 天，同时不静默制造非法的反向范围。"""

        self.end_edit.setMinimumDateTime(start.addSecs(60))
        self.end_edit.setMaximumDateTime(start.addDays(30))
        if self.end_edit.dateTime() <= start:
            self.end_edit.setDateTime(start.addSecs(3600))

    def _validate(self) -> None:
        start, end = self.values()
        if end <= start:
            self.hint.setText(tr("结束时间必须晚于开始时间"))
            return
        if end - start > timedelta(days=30):
            self.hint.setText(tr("定时部署日期范围最长为 30 天"))
            return
        self.accept()

    def values(self) -> tuple[datetime, datetime]:
        start = self.start_edit.dateTime().toPython()
        end = self.end_edit.dateTime().toPython()
        if start.tzinfo is None:
            start = start.astimezone()
        if end.tzinfo is None:
            end = end.astimezone()
        return (
            start.replace(second=0, microsecond=0),
            end.replace(second=0, microsecond=0),
        )

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        """优先显示在按钮上方，空间不足时再自动落到下方。"""

        super().showEvent(event)
        self.adjustSize()
        screen = self.anchor.screen().availableGeometry()
        anchor_top = self.anchor.mapToGlobal(self.anchor.rect().topLeft())
        anchor_bottom = self.anchor.mapToGlobal(self.anchor.rect().bottomLeft())
        x = min(max(screen.left(), anchor_top.x()), screen.right() - self.width() + 1)
        y = anchor_top.y() - self.height() - 6
        if y < screen.top():
            y = anchor_bottom.y() + 6
        self.move(x, min(y, screen.bottom() - self.height() + 1))


class ProjectBranchRow(QWidget):
    """环境节点下的单个项目与分支子节点。"""

    def __init__(self, parent_node: "EnvironmentNode"):
        super().__init__(parent_node)
        self.parent_node = parent_node
        layout = QHBoxLayout(self)
        layout.setContentsMargins(34, 4, 0, 4)
        layout.setSpacing(8)
        self.project = SmoothComboBox()
        _configure_searchable_combo(
            self.project,
            tr("选择项目，可输入名称搜索"),
        )
        self.project.setEnabled(False)
        self.branch = SmoothComboBox()
        _configure_searchable_combo(
            self.branch,
            tr("选择分支，可输入名称搜索"),
        )
        self.branch.setEnabled(False)
        self.add_button = QPushButton("+")
        self.remove_button = QPushButton("−")
        for button in (self.add_button, self.remove_button):
            button.setObjectName("compact")
            button.setFixedWidth(38)
        layout.addWidget(self.project, 5)
        layout.addWidget(self.branch, 4)
        layout.addWidget(self.add_button)
        layout.addWidget(self.remove_button)
        self.project.currentIndexChanged.connect(self._project_changed)
        self.add_button.clicked.connect(parent_node.add_project_row)
        self.remove_button.clicked.connect(lambda: parent_node.remove_project_row(self))

    def set_projects(self, projects: list[dict[str, Any]]) -> None:
        self.project.clear()
        for item in projects:
            self.project.addItem(str(item["label"]), item)
        self.project.setCurrentIndex(-1)
        self.project.setEnabled(bool(projects))
        self._project_changed()

    def _project_changed(self) -> None:
        selected = self.project.currentData()
        self.branch.clear()
        if isinstance(selected, dict):
            for branch in selected.get("branches") or []:
                self.branch.addItem(str(branch), str(branch))
        self.branch.setCurrentIndex(-1)
        self.branch.setEnabled(isinstance(selected, dict) and self.branch.count() > 0)

    def selection(self, environment: str) -> dict[str, str]:
        project = self.project.currentData()
        return {
            "environment": environment,
            "project_full_name": str(project.get("full_name") or "")
            if isinstance(project, dict)
            else "",
            "project_name": str(project.get("name") or "")
            if isinstance(project, dict)
            else "",
            "description": str(project.get("description") or "")
            if isinstance(project, dict)
            else "",
            "branch": str(self.branch.currentData() or ""),
        }


class EnvironmentNode(QWidget):
    """环境父节点，管理其下任意数量的项目/分支行。"""

    def __init__(self, dialog: "NewDeploymentDialog"):
        super().__init__(dialog)
        self.dialog = dialog
        self.catalog: dict[str, list[dict[str, Any]]] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 8)
        outer.setSpacing(4)
        parent_row = QHBoxLayout()
        self.environment = SmoothComboBox()
        self.environment.addItem(tr("选择环境"), "")
        self.add_button = QPushButton("+")
        self.remove_button = QPushButton("−")
        for button in (self.add_button, self.remove_button):
            button.setObjectName("compact")
            button.setFixedWidth(38)
        parent_row.addWidget(self.environment, 1)
        parent_row.addWidget(self.add_button)
        parent_row.addWidget(self.remove_button)
        outer.addLayout(parent_row)
        self.children_layout = QVBoxLayout()
        self.children_layout.setContentsMargins(0, 0, 0, 0)
        self.children_layout.setSpacing(2)
        outer.addLayout(self.children_layout)
        self.project_rows: list[ProjectBranchRow] = []
        self.add_project_row()
        self.environment.currentIndexChanged.connect(self._environment_changed)
        self.add_button.clicked.connect(dialog.add_environment_node)
        self.remove_button.clicked.connect(lambda: dialog.remove_environment_node(self))

    def set_catalog(self, catalog: dict[str, list[dict[str, Any]]]) -> None:
        self.catalog = catalog
        self.environment.clear()
        self.environment.addItem(tr("选择环境"), "")
        for environment in catalog:
            self.environment.addItem(environment, environment)
        self.environment.setCurrentIndex(0)
        self._environment_changed()

    def _environment_changed(self) -> None:
        environment = str(self.environment.currentData() or "")
        projects = self.catalog.get(environment, [])
        for row in self.project_rows:
            row.set_projects(projects)

    def add_project_row(self) -> None:
        row = ProjectBranchRow(self)
        self.project_rows.append(row)
        self.children_layout.addWidget(row)
        environment = str(self.environment.currentData() or "")
        row.set_projects(self.catalog.get(environment, []))
        self._update_remove_buttons()

    def remove_project_row(self, row: ProjectBranchRow) -> None:
        if len(self.project_rows) <= 1:
            return
        self.project_rows.remove(row)
        row.deleteLater()
        self._update_remove_buttons()

    def _update_remove_buttons(self) -> None:
        enabled = len(self.project_rows) > 1
        for row in self.project_rows:
            row.remove_button.setEnabled(enabled)

    def selections(self) -> list[dict[str, str]]:
        environment = str(self.environment.currentData() or "")
        return [row.selection(environment) for row in self.project_rows]


class NewDeploymentDialog(QDialog):
    """创建前只组装计划，不在对话框里直接执行 Jenkins 请求。"""

    def __init__(
        self,
        snapshot: dict[str, Any],
        *,
        on_refresh: Callable[[], None],
        show_prod: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("新建迭代部署任务"))
        self.resize(DEPLOYMENT_DIALOG_WIDTH, 620)
        self._show_prod = bool(show_prod)
        self._snapshot = dict(snapshot)
        self._on_refresh = on_refresh
        self.environment_nodes: list[EnvironmentNode] = []
        now = datetime.now().astimezone()
        self._schedule_start = (now + timedelta(minutes=1)).replace(
            second=0,
            microsecond=0,
        )
        self._schedule_end = self._schedule_start + timedelta(days=1)
        self._validated_schedule: dict[str, Any] | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(12)
        title = QLabel(tr("新建迭代部署任务"))
        title.setObjectName("dialogTitle")
        outer.addWidget(title)

        iteration_row = QHBoxLayout()
        iteration_row.addWidget(QLabel(tr("迭代名称")))
        self.iteration_name = QLineEdit()
        self.iteration_name.setPlaceholderText(tr("请输入迭代名称"))
        self.refresh_button = QPushButton(tr("刷新项目 ⟳"))
        self.refresh_status = ProjectRefreshStatus()
        self.last_refresh = self.refresh_status.label
        self.refresh_button.clicked.connect(
            lambda _checked=False: self._on_refresh()
        )
        iteration_row.addWidget(self.iteration_name, 3)
        iteration_row.addWidget(self.refresh_button)
        iteration_row.addWidget(self.refresh_status)
        outer.addLayout(iteration_row)

        selector_label = QLabel(tr("选择环境迭代项目"))
        selector_label.setObjectName("cardTitle")
        outer.addWidget(selector_label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        self.nodes_container = QWidget()
        self.nodes_layout = QVBoxLayout(self.nodes_container)
        self.nodes_layout.setContentsMargins(4, 4, 8, 4)
        self.nodes_layout.setSpacing(8)
        self.nodes_layout.addStretch(1)
        scroll.setWidget(self.nodes_container)
        outer.addWidget(scroll, 1)

        self.validation_status = status_label()
        outer.addWidget(self.validation_status)
        self.create_button = QPushButton(tr("创建"))
        self.create_button.setObjectName("primary")
        self.cancel_button = QPushButton(tr("取消"))
        self.create_button.clicked.connect(self._validate_and_accept)
        self.cancel_button.clicked.connect(self.reject)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.scheduled_deployment = ThemedCheckBox(tr("定时部署"))
        self.scheduled_deployment.setFixedHeight(SCHEDULE_CONTROL_HEIGHT)
        self.schedule_controls = QWidget()
        schedule_layout = QHBoxLayout(self.schedule_controls)
        schedule_layout.setContentsMargins(0, 0, 0, 0)
        schedule_layout.setSpacing(8)
        self.schedule_range = QPushButton()
        self.schedule_range.setObjectName("scheduleField")
        self.schedule_range.setAccessibleName(tr("开始和结束时间"))
        self.schedule_range.setMinimumWidth(292)
        self.schedule_range.setFixedHeight(SCHEDULE_CONTROL_HEIGHT)
        self.schedule_range.clicked.connect(self._open_schedule_range)
        self.schedule_every = QLabel(tr("每"))
        self.schedule_every.setFixedHeight(SCHEDULE_CONTROL_HEIGHT)
        self.schedule_every.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.schedule_mode = SmoothComboBox()
        self.schedule_mode.addItem(tr("间隔"), "interval_minutes")
        self.schedule_mode.addItem(tr("时刻"), "daily_time")
        self.schedule_mode.setFixedSize(152, SCHEDULE_CONTROL_HEIGHT)
        self.schedule_interval = ManualSpinBox()
        self.schedule_interval.setRange(1, 1440)
        self.schedule_interval.setValue(30)
        self.schedule_interval.setSuffix(tr(" 分钟"))
        self.schedule_interval.setFixedSize(118, SCHEDULE_CONTROL_HEIGHT)
        self.schedule_time = QTimeEdit(QTime(9, 0))
        self.schedule_time.setDisplayFormat("HH:mm")
        self.schedule_time.setFixedSize(108, SCHEDULE_CONTROL_HEIGHT)
        self.schedule_deploy_suffix = QLabel(tr("部署"))
        self.schedule_deploy_suffix.setFixedHeight(SCHEDULE_CONTROL_HEIGHT)
        self.schedule_deploy_suffix.setAlignment(Qt.AlignmentFlag.AlignCenter)
        schedule_layout.addWidget(self.schedule_range)
        schedule_layout.addWidget(self.schedule_every)
        schedule_layout.addWidget(self.schedule_mode)
        schedule_layout.addWidget(self.schedule_interval)
        schedule_layout.addWidget(self.schedule_time)
        schedule_layout.addWidget(self.schedule_deploy_suffix)
        self.scheduled_deployment.toggled.connect(self.schedule_controls.setVisible)
        self.schedule_mode.currentIndexChanged.connect(self._schedule_mode_changed)
        self.schedule_controls.setVisible(False)
        self._schedule_mode_changed()
        self._update_schedule_range_text()
        actions.addWidget(self.scheduled_deployment)
        actions.addWidget(self.schedule_controls)
        actions.addStretch()
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.create_button)
        outer.addLayout(actions)

        self.add_environment_node()
        self.update_snapshot(snapshot)

    def add_environment_node(self) -> None:
        node = EnvironmentNode(self)
        self.environment_nodes.append(node)
        self.nodes_layout.insertWidget(self.nodes_layout.count() - 1, node)
        node.set_catalog(deployment_catalog(self._snapshot, show_prod=self._show_prod))
        self._update_environment_remove_buttons()

    def remove_environment_node(self, node: EnvironmentNode) -> None:
        if len(self.environment_nodes) <= 1:
            return
        self.environment_nodes.remove(node)
        node.deleteLater()
        self._update_environment_remove_buttons()

    def _update_environment_remove_buttons(self) -> None:
        enabled = len(self.environment_nodes) > 1
        for node in self.environment_nodes:
            node.remove_button.setEnabled(enabled)

    def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = dict(snapshot)
        catalog = deployment_catalog(snapshot, show_prod=self._show_prod)
        for node in self.environment_nodes:
            # 项目刷新后按需求清空旧环境、项目和分支选择，避免提交过期分支。
            node.set_catalog(catalog)
        value = str(snapshot.get("last_refreshed_at") or "")
        self.refresh_status.set_last_refreshed_at(value)

    def set_refreshing(self, refreshing: bool) -> None:
        # 刷新期间按钮保持可用，用户可通过二次确认主动替换旧刷新请求。
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText(tr("刷新项目 ⟳"))
        self.refresh_status.set_refreshing(refreshing)

    def selections(self) -> list[dict[str, str]]:
        return [
            selection
            for node in self.environment_nodes
            for selection in node.selections()
        ]

    def schedule_value(self) -> dict[str, Any]:
        """返回可直接交给业务层复核的定时配置。"""

        if not self.scheduled_deployment.isChecked():
            return {"enabled": False}
        return {
            "enabled": True,
            "mode": str(self.schedule_mode.currentData() or "interval_minutes"),
            "start_at": self._schedule_start.isoformat(timespec="minutes"),
            "end_at": self._schedule_end.isoformat(timespec="minutes"),
            "interval_minutes": self.schedule_interval.value(),
            "time_of_day": self.schedule_time.time().toString("HH:mm"),
        }

    def _open_schedule_range(self) -> None:
        dialog = ScheduleRangeDialog(
            self._schedule_start,
            self._schedule_end,
            anchor=self.schedule_range,
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        self._schedule_start, self._schedule_end = dialog.values()
        self._update_schedule_range_text()

    def _update_schedule_range_text(self) -> None:
        self.schedule_range.setText(
            format_schedule_range(self._schedule_start, self._schedule_end)
        )

    def _schedule_mode_changed(self, *_args) -> None:
        interval_mode = self.schedule_mode.currentData() == "interval_minutes"
        self.schedule_interval.setVisible(interval_mode)
        self.schedule_time.setVisible(not interval_mode)

    def _validate_and_accept(self) -> None:
        if not self.iteration_name.text().strip():
            self.validation_status.setText(tr("请输入迭代名称"))
            return
        values = self.selections()
        if not values or any(
            not item["environment"]
            or not item["project_full_name"]
            or not item["branch"]
            for item in values
        ):
            self.validation_status.setText(tr("每一行都必须选择环境、项目和分支"))
            return
        keys = [
            (item["environment"].casefold(), item["project_full_name"].casefold())
            for item in values
        ]
        if len(keys) != len(set(keys)):
            self.validation_status.setText(tr("同一环境下不能重复选择项目"))
            return
        try:
            # UI 与业务层执行相同校验，让用户在关闭弹窗前修正时间范围。
            self._validated_schedule = normalize_schedule(self.schedule_value())
        except ValueError as exc:
            self.validation_status.setText(tr(str(exc)))
            return
        self.accept()


class SingleDeploymentDialog(QDialog):
    """只允许一个环境、项目和分支组合的单点部署选择器。"""

    def __init__(
        self,
        snapshot: dict[str, Any],
        *,
        on_refresh: Callable[[], None],
        show_prod: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("单点部署"))
        self.resize(DEPLOYMENT_DIALOG_WIDTH, 320)
        self.setMinimumSize(DEPLOYMENT_DIALOG_WIDTH, 320)
        self._snapshot = dict(snapshot)
        self._show_prod = bool(show_prod)
        self._on_refresh = on_refresh

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(14)
        title = QLabel(tr("单点部署"))
        title.setObjectName("dialogTitle")
        outer.addWidget(title)
        description = status_label(
            tr("选择环境、项目和分支后，任务名称会自动附加对应组合。")
        )
        outer.addWidget(description)

        refresh_row = QHBoxLayout()
        refresh_row.addStretch()
        self.refresh_button = QPushButton(tr("刷新项目 ⟳"))
        self.refresh_status = ProjectRefreshStatus()
        self.last_refresh = self.refresh_status.label
        self.refresh_button.clicked.connect(
            lambda _checked=False: self._on_refresh()
        )
        refresh_row.addWidget(self.refresh_button)
        refresh_row.addWidget(self.refresh_status)
        outer.addLayout(refresh_row)

        selector = QHBoxLayout()
        selector.setSpacing(10)
        self.environment = SmoothComboBox()
        self.environment.setAccessibleName(tr("环境"))
        self.project = SmoothComboBox()
        self.project.setAccessibleName(tr("项目"))
        _configure_searchable_combo(
            self.project,
            tr("选择项目，可输入名称搜索"),
        )
        self.branch = SmoothComboBox()
        self.branch.setAccessibleName(tr("分支"))
        _configure_searchable_combo(
            self.branch,
            tr("选择分支，可输入名称搜索"),
        )
        selector.addWidget(self.environment, 2)
        selector.addWidget(self.project, 5)
        selector.addWidget(self.branch, 3)
        outer.addLayout(selector)
        outer.addStretch(1)

        self.validation_status = status_label()
        outer.addWidget(self.validation_status)
        self.create_button = QPushButton(tr("部署"))
        self.create_button.setObjectName("primary")
        self.cancel_button = QPushButton(tr("取消"))
        self.create_button.clicked.connect(self._validate_and_accept)
        self.cancel_button.clicked.connect(self.reject)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.create_button)
        outer.addLayout(actions)

        self.environment.currentIndexChanged.connect(self._environment_changed)
        self.project.currentIndexChanged.connect(self._project_changed)
        self.update_snapshot(snapshot)

    def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        """项目刷新后清空三级选择，杜绝提交已经失效的分支。"""

        self._snapshot = dict(snapshot)
        catalog = deployment_catalog(snapshot, show_prod=self._show_prod)
        self.environment.clear()
        self.environment.addItem(tr("选择环境"), "")
        for environment in catalog:
            self.environment.addItem(environment, environment)
        self.environment.setCurrentIndex(0)
        self._environment_changed()
        self.refresh_status.set_last_refreshed_at(
            str(snapshot.get("last_refreshed_at") or "")
        )

    def set_refreshing(self, refreshing: bool) -> None:
        # 与迭代弹窗一致，刷新中仍允许用户确认后替换当前刷新请求。
        self.refresh_button.setEnabled(True)
        self.refresh_status.set_refreshing(refreshing)

    def _environment_changed(self) -> None:
        environment = str(self.environment.currentData() or "")
        catalog = deployment_catalog(self._snapshot, show_prod=self._show_prod)
        self.project.clear()
        for item in catalog.get(environment, []):
            self.project.addItem(str(item["label"]), item)
        self.project.setCurrentIndex(-1)
        self.project.setEnabled(self.project.count() > 0)
        self._project_changed()

    def _project_changed(self) -> None:
        project = self.project.currentData()
        self.branch.clear()
        if isinstance(project, dict):
            for branch in project.get("branches") or []:
                self.branch.addItem(str(branch), str(branch))
        self.branch.setCurrentIndex(-1)
        self.branch.setEnabled(isinstance(project, dict) and self.branch.count() > 0)

    def selections(self) -> list[dict[str, str]]:
        project = self.project.currentData()
        return [
            {
                "environment": str(self.environment.currentData() or ""),
                "project_full_name": str(project.get("full_name") or "")
                if isinstance(project, dict)
                else "",
                "project_name": str(project.get("name") or "")
                if isinstance(project, dict)
                else "",
                "description": str(project.get("description") or "")
                if isinstance(project, dict)
                else "",
                "branch": str(self.branch.currentData() or ""),
            }
        ]

    def _validate_and_accept(self) -> None:
        selection = self.selections()[0]
        if any(
            not selection[field]
            for field in ("environment", "project_full_name", "branch")
        ):
            self.validation_status.setText(tr("请选择环境、项目和分支"))
            return
        self.accept()
