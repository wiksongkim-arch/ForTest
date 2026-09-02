"""EIM 监听首页、连接中心、任务工作台、日志与回收站。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QSize, QTimer, Signal, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from windows_native.i18n import tr, translate_widget_tree
from windows_native.ui.common import (
    BasePage,
    ManualSpinBox,
    SmoothComboBox,
    SmoothTabWidget,
    ThemedCheckBox,
    button_row,
    card,
    clear_layout,
    confirm_action,
    status_label,
)


SELF_LOOP_FALLBACK = "当前授权账号本人发送的消息不会进入监听；其他成员和机器人消息正常监听。"
STOPPED_STATES = {"stopped", "stopped_app_exit"}
# 单行映射保持紧凑；行数增加时仍由表格内容自动撑高。
_MAPPING_TABLE_MINIMUM_HEIGHT = 112
STATE_LABELS = {
    "draft": "草稿",
    "building": "构建中",
    "validating": "验证中",
    "ready": "就绪",
    "failed": "失败",
    "superseded": "已替换",
    "disconnected": "未连接",
    "authorizing": "授权中",
    "expired": "授权已过期",
    "permission_missing": "权限不足",
    "auth_required": "需要授权",
    "connecting": "连接中",
    "connected": "已连接",
    "cooldown": "冷却中",
    "stopped": "已停止",
    "stopped_app_exit": "应用退出后停止",
    "starting": "启动中",
    "running": "运行中",
    "reconnecting": "重连中",
    "degraded": "降级",
    "stopping": "停止中",
    "error": "异常",
    "receive": "接收",
    "filter": "过滤",
    "ai": "AI",
    "mapping": "映射",
    "delivery": "写入",
    "retry": "重试",
    "completed": "完成",
    "skipped": "跳过",
    "received": "已接收",
    "duplicate": "重复事件",
    "queued": "已排队",
    "warning": "警告",
    "dead_letter": "死信",
    "archive_raw": "归档原文",
}


class _CurrentPageTabWidget(SmoothTabWidget):
    """只按当前页计算高度，避免长日志撑开其他标签页。"""

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        current = self.currentWidget()
        tab_bar = self.tabBar().sizeHint()
        if current is None:
            return tab_bar
        content = current.layout().sizeHint() if current.layout() else current.sizeHint()
        return QSize(
            max(content.width(), tab_bar.width()),
            content.height() + tab_bar.height() + 6,
        )

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return self.sizeHint()


def _table(headers: list[str]) -> QTableWidget:
    """创建只读、整行选择且可横向滚动的统一表格。"""

    value = QTableWidget(0, len(headers))
    value.setHorizontalHeaderLabels([tr(item) for item in headers])
    value.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    value.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    value.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    value.setAlternatingRowColors(True)
    value.verticalHeader().setVisible(False)
    value.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    value.horizontalHeader().setStretchLastSection(True)
    value.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    return value


def _fit_workbench_table(value: QTableWidget, minimum_height: int) -> None:
    """让工作台表格完整展开，由页面统一负责纵向滚动。"""

    if value.property("eimFittingRows"):
        return
    value.setProperty("eimFittingRows", True)
    try:
        value.resizeRowsToContents()
        rows_height = sum(value.rowHeight(row) for row in range(value.rowCount()))
        height = value.horizontalHeader().height() + rows_height + value.frameWidth() * 2 + 4
        value.setFixedHeight(max(minimum_height, height))
    finally:
        value.setProperty("eimFittingRows", False)


def _workbench_table(headers: list[str], minimum_height: int = 160) -> QTableWidget:
    """创建无内部滚动、按剩余宽度换行的工作台表格。"""

    value = _table(headers)
    value.setWordWrap(True)
    value.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    value.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    value.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    value.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    value.setMinimumHeight(minimum_height)
    value.horizontalHeader().sectionResized.connect(
        lambda _index, _old, _new: _fit_workbench_table(value, minimum_height)
    )
    return value


def _fit_plain_text(value: QPlainTextEdit, minimum_height: int) -> None:
    """让换行后的文本编辑器随内容增高，避免嵌套滚动。"""

    # QPlainTextDocumentLayout 返回的是文本块数量，需按实际换行数换算像素高度。
    line_count = 0
    block = value.document().begin()
    while block.isValid():
        line_count += max(1, block.layout().lineCount())
        block = block.next()
    document_height = (
        line_count * value.fontMetrics().lineSpacing()
        + int(value.document().documentMargin() * 2)
        + value.frameWidth() * 2
        + 6
    )
    value.setFixedHeight(max(minimum_height, document_height))
    parent = value.parentWidget()
    while parent is not None:
        if isinstance(parent, _CurrentPageTabWidget):
            QTimer.singleShot(0, parent.updateGeometry)
            break
        parent = parent.parentWidget()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _state(value: Any) -> str:
    raw = str(value or "")
    return tr(STATE_LABELS.get(raw, raw))


def _capabilities_text(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return tr("尚未完成能力检测")
    labels = (
        ("authenticated", "授权"),
        ("token_valid", "凭证有效"),
        ("group_discovery", "群发现"),
        ("group_message_events", "消息事件"),
        ("reaction_events", "Reaction 事件"),
    )
    parts = [
        f"{tr(label)}：{tr('通过') if value.get(key) else tr('未通过')}"
        for key, label in labels
    ]
    dws = value.get("dws")
    if isinstance(dws, dict) and dws.get("version"):
        parts.append(f"DWS v{dws['version']}")
    return " · ".join(parts)


def _item(value: Any, *, data: Any = None) -> QTableWidgetItem:
    result = QTableWidgetItem(str(value if value is not None else ""))
    if data is not None:
        result.setData(Qt.ItemDataRole.UserRole, data)
    result.setToolTip(result.text())
    return result


def _combo_item_id(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("sheetId") or item.get("sheet_id") or item.get("fieldId") or item.get("field_id") or item.get("id") or "")


class CreateEIMTaskDialog(QDialog):
    """只采集冻结决策规定的名称、平台、群和目标链接四个字段。"""

    def __init__(self, groups: list[dict[str, str]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("创建监听任务"))
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(tr("输入可区分的任务名称"))
        self.platform_combo = SmoothComboBox()
        self.platform_combo.addItem(tr("钉钉"), "dingtalk")
        self.platform_combo.setEnabled(False)
        self.group_combo = SmoothComboBox()
        for group in groups:
            self.group_combo.addItem(str(group.get("name") or group.get("id") or ""), group)
        self.destination_edit = QLineEdit()
        self.destination_edit.setPlaceholderText("https://alidocs.dingtalk.com/i/...")
        form.addRow(tr("任务名称"), self.name_edit)
        form.addRow(tr("平台"), self.platform_combo)
        form.addRow(tr("来源群"), self.group_combo)
        form.addRow(tr("归档目标链接"), self.destination_edit)
        layout.addLayout(form)
        warning = status_label(tr(SELF_LOOP_FALLBACK))
        warning.setObjectName("warningLabel")
        layout.addWidget(warning)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("创建草稿"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        translate_widget_tree(self)

    def accept(self) -> None:  # noqa: D401
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, tr("提示"), tr("任务名称不能为空"))
            return
        if self.group_combo.currentData() is None:
            QMessageBox.warning(self, tr("提示"), tr("请选择来源群"))
            return
        if not self.destination_edit.text().strip():
            QMessageBox.warning(self, tr("提示"), tr("归档目标链接不能为空"))
            return
        super().accept()

    def payload(self) -> dict[str, str]:
        group = self.group_combo.currentData() or {}
        return {
            "name": self.name_edit.text().strip(),
            "connection_id": "",
            "source_id": str(group.get("id") or ""),
            "source_name": str(group.get("name") or group.get("id") or ""),
            "destination_url": self.destination_edit.text().strip(),
        }


class ImportEIMTaskDialog(QDialog):
    """导入包只接受本机连接、来源群和新目标链接重新绑定。"""

    def __init__(self, groups: list[dict[str, str]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("导入 EIM 配置"))
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.group_combo = SmoothComboBox()
        for group in groups:
            self.group_combo.addItem(str(group.get("name") or group.get("id") or ""), group)
        self.destination_edit = QLineEdit()
        self.destination_edit.setPlaceholderText("https://alidocs.dingtalk.com/i/...")
        form.addRow(tr("重新绑定来源群"), self.group_combo)
        form.addRow(tr("重新绑定归档目标"), self.destination_edit)
        layout.addLayout(form)
        layout.addWidget(status_label(tr("导入不会携带凭证、日志、消息正文或运行状态；任务保持已停止。")))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("导入"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        translate_widget_tree(self)

    def accept(self) -> None:
        if self.group_combo.currentData() is None or not self.destination_edit.text().strip():
            QMessageBox.warning(self, tr("提示"), tr("请选择来源群并填写归档目标链接"))
            return
        super().accept()

    def payload(self) -> dict[str, str]:
        group = self.group_combo.currentData() or {}
        return {
            "source_id": str(group.get("id") or ""),
            "source_name": str(group.get("name") or group.get("id") or ""),
            "destination_url": self.destination_edit.text().strip(),
        }


class SampleDialog(QDialog):
    """用 JSON 录入脱敏样例和期望映射，避免隐式执行任何脚本。"""

    def __init__(self, task: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("添加脱敏样例"))
        self.resize(720, 600)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(tr("事件 JSON")))
        self.event_edit = QPlainTextEdit()
        self.event_edit.setPlainText(
            _json(
                {
                    "platform": "dingtalk",
                    "connection_id": task.get("connection_id", ""),
                    "event_id": "sample-event-1",
                    "event_type": "message",
                    "message_id": "sample-message-1",
                    "conversation_id": task.get("source_id", ""),
                    "sender_id": "sample-user",
                    "sender_name": "示例用户",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "message_kind": "text",
                    "text": "请替换为已脱敏的样例内容",
                }
            )
        )
        layout.addWidget(self.event_edit, 1)
        layout.addWidget(QLabel(tr("期望输出 JSON")))
        self.expected_edit = QPlainTextEdit("{}")
        layout.addWidget(self.expected_edit, 1)
        layout.addWidget(status_label(tr("禁止在样例中粘贴 Token、API Key、手机号或未脱敏业务数据。")))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("添加"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        translate_widget_tree(self)

    def values(self) -> tuple[dict[str, Any], dict[str, Any]]:
        event = json.loads(self.event_edit.toPlainText())
        expected = json.loads(self.expected_edit.toPlainText())
        if not isinstance(event, dict) or not isinstance(expected, dict):
            raise ValueError(tr("样例和期望输出必须是 JSON 对象"))
        return event, expected

    def accept(self) -> None:
        try:
            self.values()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, tr("提示"), str(exc))
            return
        super().accept()


class EIMPage(BasePage):
    """EIM 总览与长期监听任务治理入口。"""

    connection_requested = Signal()
    studio_requested = Signal(str)
    logs_requested = Signal(str)
    recycle_requested = Signal()

    def __init__(self, service: Any):
        super().__init__(tr("EIM 监听"), tr("把钉钉群事件可靠归档到文档、电子表格或 AI 表格。"))
        self.service = service
        self.snapshot: dict[str, Any] = {}
        self.connection_banner = status_label(tr("正在读取 EIM 连接状态…"))
        self.connection_banner.setObjectName("statusBanner")
        self.content.addWidget(self.connection_banner)

        stats_frame, stats_layout = card(tr("任务概览"))
        stats_grid = QGridLayout()
        self.stats: dict[str, QLabel] = {}
        for index, (key, label) in enumerate(
            (
                ("running", "运行中"),
                ("stopped", "已停止"),
                ("degraded", "异常 / 降级"),
                ("needs_authorization", "需要授权"),
                ("received_today", "今日接收"),
                ("archived_today", "今日归档"),
                ("failed_today", "今日失败"),
            )
        ):
            tile = QFrame()
            tile.setObjectName("metricTile")
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(12, 8, 12, 8)
            value = QLabel("0")
            value.setObjectName("metricValue")
            caption = QLabel(tr(label))
            caption.setObjectName("muted")
            tile_layout.addWidget(value)
            tile_layout.addWidget(caption)
            stats_grid.addWidget(tile, index // 4, index % 4)
            self.stats[key] = value
        stats_layout.addLayout(stats_grid)
        self.content.addWidget(stats_frame)

        actions_frame, actions_layout = card(tr("快捷操作"))
        action_row = QHBoxLayout()
        self.create_button = QPushButton(tr("创建监听任务"))
        self.connection_button = QPushButton(tr("连接与运行设置"))
        self.import_button = QPushButton(tr("导入配置"))
        self.logs_button = QPushButton(tr("运行日志"))
        self.recycle_button = QPushButton(tr("回收站"))
        self.create_button.clicked.connect(self._prepare_create)
        self.connection_button.clicked.connect(self.connection_requested.emit)
        self.import_button.clicked.connect(self._prepare_import)
        self.logs_button.clicked.connect(lambda: self.logs_requested.emit(""))
        self.recycle_button.clicked.connect(self.recycle_requested.emit)
        for button in (
            self.create_button,
            self.connection_button,
            self.import_button,
            self.logs_button,
            self.recycle_button,
        ):
            action_row.addWidget(button)
        action_row.addStretch()
        actions_layout.addLayout(action_row)
        self.content.addWidget(actions_frame)

        tasks_frame, tasks_layout = card(tr("监听任务"))
        self.task_table = _table(
            ["任务 ID", "任务名称", "来源", "今日归档", "状态", "最近活动", "操作"]
        )
        # 操作列在英文下明显更长，固定下限并允许表格横向滚动，避免按钮文字被裁切。
        self.task_table.horizontalHeader().setSectionResizeMode(
            6, QHeaderView.ResizeMode.Interactive
        )
        self.task_table.setColumnWidth(6, 400)
        self.task_table.setMinimumHeight(260)
        self.task_table.cellDoubleClicked.connect(self._open_row)
        tasks_layout.addWidget(self.task_table)
        self.content.addWidget(tasks_frame)
        self.add_stretch()

    def refresh(self) -> None:
        self.connection_banner.setText(tr("正在读取 EIM 连接状态…"))
        self.run_async(
            lambda: self.service.eim.overview(),
            success=self._apply_snapshot,
            failure=lambda message: self.connection_banner.setText(message),
        )

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = dict(snapshot)
        notice = str(snapshot.get("self_loop_notice") or SELF_LOOP_FALLBACK)
        if snapshot.get("connected"):
            self.connection_banner.setText(f"{tr('钉钉连接可用')} · {tr(notice)}")
        else:
            self.connection_banner.setText(f"{tr('尚未连接钉钉')} · {tr(notice)}")
        for key, label in self.stats.items():
            label.setText(str(snapshot.get(key, 0)))
        tasks = list(snapshot.get("tasks") or [])
        self.task_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            task_id = str(task.get("task_id") or "")
            self.task_table.setItem(row, 0, _item(task.get("display_id"), data=task_id))
            self.task_table.setItem(row, 1, _item(task.get("name"), data=task_id))
            self.task_table.setItem(row, 2, _item(f"{tr('钉钉')} · {task.get('source_name', '')}"))
            archived = int(task.get("archived_today") or 0)
            failed = int(task.get("failed_today") or 0)
            self.task_table.setItem(row, 3, _item(f"{archived} / {tr('失败')} {failed}"))
            state = f"{_state(task.get('build_state'))} · {_state(task.get('observed_state'))}"
            state_item = _item(state)
            state_item.setToolTip(f"{state}\n{tr(SELF_LOOP_FALLBACK)}")
            self.task_table.setItem(row, 4, state_item)
            self.task_table.setItem(row, 5, _item(task.get("last_activity_at") or "—"))
            actions = QWidget()
            row_layout = QHBoxLayout(actions)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            edit = QPushButton(tr("工作台"))
            logs = QPushButton(tr("日志"))
            running = str(task.get("observed_state") or "") not in STOPPED_STATES
            toggle = QPushButton(tr("停止") if running else tr("启动"))
            more = QPushButton(tr("更多"))
            menu = QMenu(more)
            copy = menu.addAction(tr("复制"))
            delete = menu.addAction(tr("删除"))
            more.setMenu(menu)
            edit.clicked.connect(lambda _checked=False, value=task_id: self.studio_requested.emit(value))
            logs.clicked.connect(lambda _checked=False, value=task_id: self.logs_requested.emit(value))
            toggle.clicked.connect(
                lambda _checked=False, value=task_id, should_stop=running: self._toggle_task(value, should_stop)
            )
            copy.triggered.connect(lambda _checked=False, value=task_id: self._copy_task(value))
            delete.triggered.connect(lambda _checked=False, value=task_id: self._delete_task(value))
            delete.setEnabled(bool(task.get("editable")))
            copy.setEnabled(bool(task.get("editable")))
            toggle.setEnabled(bool(task.get("active_version_id")) or running)
            for button in (edit, logs, toggle, more):
                button.setObjectName("compact")
                row_layout.addWidget(button)
            # 英文按钮较长，操作容器至少保留布局建议宽度，避免单元格强制压缩文字。
            actions.setMinimumWidth(actions.minimumSizeHint().width())
            self.task_table.setCellWidget(row, 6, actions)
        self.task_table.resizeRowsToContents()

    def _open_row(self, row: int, _column: int) -> None:
        item = self.task_table.item(row, 0)
        if item is not None and item.data(Qt.ItemDataRole.UserRole):
            self.studio_requested.emit(str(item.data(Qt.ItemDataRole.UserRole)))

    def _prepare_create(self) -> None:
        connections = list(self.snapshot.get("connections") or [])
        if not connections:
            self.connection_requested.emit()
            return
        connection = connections[0]
        self.create_button.setEnabled(False)
        self.run_async(
            lambda: self.service.eim.groups(str(connection["connection_id"])),
            success=lambda groups: self._show_create(connection, groups),
            finished=lambda: self.create_button.setEnabled(True),
        )

    def _show_create(self, connection: dict[str, Any], groups: list[dict[str, str]]) -> None:
        dialog = CreateEIMTaskDialog(groups, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.payload()
        payload["connection_id"] = str(connection["connection_id"])
        self.run_async(
            lambda: self.service.eim.create_task(**payload),
            success=lambda detail: self.studio_requested.emit(str(detail["task"]["task_id"])),
        )

    def _prepare_import(self) -> None:
        archive, _selected = QFileDialog.getOpenFileName(
            self,
            tr("导入 EIM 配置"),
            "",
            tr("EIM 配置包 (*.eim.zip *.zip)"),
        )
        if not archive:
            return
        connections = list(self.snapshot.get("connections") or [])
        if not connections:
            self.connection_requested.emit()
            return
        connection = connections[0]

        def success(groups: list[dict[str, str]]) -> None:
            dialog = ImportEIMTaskDialog(groups, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            payload = dialog.payload()
            self.run_async(
                lambda: self.service.eim.import_task(
                    archive,
                    connection_id=str(connection["connection_id"]),
                    **payload,
                ),
                success=lambda detail: self.studio_requested.emit(str(detail["task"]["task_id"])),
            )

        self.run_async(
            lambda: self.service.eim.groups(str(connection["connection_id"])),
            success=success,
        )

    def _toggle_task(self, task_id: str, stop: bool) -> None:
        action = self.service.eim.stop_task if stop else self.service.eim.start_task
        self.run_async(lambda: action(task_id), success=lambda _value: self.refresh())

    def _copy_task(self, task_id: str) -> None:
        self.run_async(
            lambda: self.service.eim.copy_task(task_id),
            success=lambda detail: self.studio_requested.emit(str(detail["task"]["task_id"])),
        )

    def _delete_task(self, task_id: str) -> None:
        if not confirm_action(self, "删除监听任务", "确定将此监听任务移入回收站吗？"):
            return
        self.run_async(lambda: self.service.eim.delete_task(task_id), success=lambda _value: self.refresh())


class EIMConnectionPage(BasePage):
    """钉钉隔离连接、运行恢复和日志保留设置。"""

    back_requested = Signal()

    def __init__(self, service: Any):
        super().__init__(tr("连接与运行设置"), tr("每个 EIM 连接使用独立 DWS 配置目录，不读取全局登录状态。"))
        self.service = service
        self.connections: list[dict[str, Any]] = []
        back = QPushButton(tr("← 返回"))
        back.clicked.connect(self.back_requested.emit)
        self.content.insertWidget(0, back, 0, Qt.AlignmentFlag.AlignLeft)

        connection_frame, connection_layout = card(tr("平台连接"))
        self.connection_status = status_label(tr("正在读取连接…"))
        connection_layout.addWidget(self.connection_status)
        self.capabilities = status_label("")
        connection_layout.addWidget(self.capabilities)
        row = QHBoxLayout()
        self.authorize_button = QPushButton(tr("连接钉钉"))
        self.refresh_button = QPushButton(tr("刷新连接状态"))
        feishu = QPushButton(tr("飞书 · 规划中"))
        wecom = QPushButton(tr("企业微信 · 规划中"))
        feishu.setEnabled(False)
        wecom.setEnabled(False)
        self.authorize_button.clicked.connect(self._authorize)
        self.refresh_button.clicked.connect(self._refresh_connection)
        for button in (self.authorize_button, self.refresh_button, feishu, wecom):
            row.addWidget(button)
        row.addStretch()
        connection_layout.addLayout(row)
        warning = status_label(tr(SELF_LOOP_FALLBACK))
        warning.setObjectName("warningLabel")
        connection_layout.addWidget(warning)
        self.content.addWidget(connection_frame)

        settings_frame, settings_layout = card(tr("运行设置"))
        self.restore_checkbox = ThemedCheckBox(tr("启动 ForTest 时恢复此前处于运行意图的 EIM 任务"))
        self.retention_spin = ManualSpinBox()
        self.retention_spin.setRange(1, 365)
        self.retention_spin.setSuffix(tr(" 天"))
        form = QFormLayout()
        form.addRow(self.restore_checkbox)
        form.addRow(tr("日志保留天数"), self.retention_spin)
        settings_layout.addLayout(form)
        save = QPushButton(tr("保存运行设置"))
        cleanup = QPushButton(tr("立即执行保留清理"))
        save.clicked.connect(self._save_preferences)
        cleanup.clicked.connect(self._run_cleanup)
        settings_layout.addWidget(button_row(save, cleanup))
        self.content.addWidget(settings_frame)
        self.add_stretch()

    def refresh(self) -> None:
        self.run_async(
            lambda: (self.service.eim.connections(), self.service.get_eim_preferences()),
            success=self._apply,
        )

    def _apply(self, value: tuple[list[dict[str, Any]], dict[str, Any]]) -> None:
        self.connections, preferences = value
        if not self.connections:
            self.connection_status.setText(tr("尚未连接钉钉"))
            self.capabilities.setText(tr("连接后将验证登录、群可见性和目标读写能力。"))
            self.authorize_button.setText(tr("连接钉钉"))
            self.refresh_button.setEnabled(False)
        else:
            connection = self.connections[0]
            identity = " · ".join(
                item for item in (connection.get("account_name"), connection.get("organization_name")) if item
            )
            self.connection_status.setText(
                f"{_state(connection.get('connection_state'))}{' · ' + identity if identity else ''}"
            )
            self.capabilities.setText(_capabilities_text(connection.get("capabilities") or {}))
            self.authorize_button.setText(tr("重新授权"))
            self.refresh_button.setEnabled(True)
        self.restore_checkbox.setChecked(bool(preferences.get("restore_running_tasks", True)))
        self.retention_spin.setValue(int(preferences.get("log_retention_days", 30)))

    def _authorize(self) -> None:
        self.authorize_button.setEnabled(False)

        def work() -> dict[str, Any]:
            connection = self.connections[0] if self.connections else self.service.eim.create_connection()
            return self.service.eim.authorize_connection(str(connection["connection_id"]))

        self.run_async(
            work,
            success=lambda _value: self.refresh(),
            finished=lambda: self.authorize_button.setEnabled(True),
        )

    def _refresh_connection(self) -> None:
        if not self.connections:
            return
        connection_id = str(self.connections[0]["connection_id"])
        self.run_async(
            lambda: self.service.eim.refresh_connection(connection_id),
            success=lambda _value: self.refresh(),
        )

    def _save_preferences(self) -> None:
        values = {
            "restore_running_tasks": self.restore_checkbox.isChecked(),
            "log_retention_days": self.retention_spin.value(),
        }
        self.run_async(
            lambda: self.service.save_eim_preferences(values),
            success=lambda _value: self.show_info(tr("运行设置已保存")),
        )

    def _run_cleanup(self) -> None:
        days = self.retention_spin.value()
        self.run_async(
            lambda: self.service.eim.run_retention(log_days=days),
            success=lambda value: self.show_info(tr("清理完成：{value}", value=_json(value))),
        )


class EIMStudioPage(BasePage):
    """结构化规则、样例、日志、高级 DSL、版本和受控 AI 构建工作台。"""

    back_requested = Signal()
    logs_requested = Signal(str)
    changed = Signal()

    SOURCE_FIELDS = (
        "event.id",
        "event.type",
        "message.id",
        "message.text",
        "message.kind",
        "sender.id",
        "sender.name",
        "conversation.id",
        "occurred_at",
        "quoted_message",
        "reaction.name",
        "reaction.text",
        "reaction.operation",
        "media",
    )
    SOURCE_TYPES = {
        "quoted_message": "object",
        "media": "array",
    }
    SOURCE_OPTIONS = {
        "event.id": "event.id · 事件唯一 ID",
        "event.type": "event.type · 事件类型",
        "message.id": "message.id · 消息唯一 ID",
        "message.text": "message.text · 消息正文",
        "message.kind": "message.kind · 消息类型",
        "sender.id": "sender.id · 发送者 ID",
        "sender.name": "sender.name · 发送者名称",
        "conversation.id": "conversation.id · 会话 ID",
        "occurred_at": "occurred_at · 发生时间",
        "quoted_message": "quoted_message · 引用消息",
        "reaction.name": "reaction.name · Reaction 名称",
        "reaction.text": "reaction.text · Reaction 文本",
        "reaction.operation": "reaction.operation · Reaction 操作",
        "media": "media · 媒体与附件",
    }
    DOCUMENT_FIELD_OPTIONS = {
        "title": "title · 标题",
        "body": "body · 正文",
        "metadata": "metadata · 元数据",
        "media": "media · 媒体与附件",
    }
    TARGET_TYPE_LABELS = {
        "text": "文本",
        "string": "文本",
        "1": "文本",
        "attachment": "附件",
        "user": "人员",
        "date": "日期",
        "singleselect": "单选",
        "multipleselect": "多选",
        "richtext": "富文本",
        "url": "链接",
        "unidirectionallink": "关联记录",
        "autonumber": "自动编号",
    }

    def __init__(self, service: Any):
        super().__init__(tr("EIM 任务工作台"), tr("修改、测试、发布并启动一个可审计的不可变任务版本。"))
        self.service = service
        self.task_id = ""
        self.snapshot: dict[str, Any] = {}
        self._building = False
        self._editable = False
        self._target_field_labels: dict[str, str] = {}

        # 工作台只使用页面级纵向滚动，内部区域不再争抢滚轮或横向空间。
        self.page_scroll = self.findChild(QScrollArea)
        if self.page_scroll is not None:
            self.page_scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )

        back = QPushButton(tr("← 返回"))
        back.clicked.connect(self.back_requested.emit)
        self.content.insertWidget(0, back, 0, Qt.AlignmentFlag.AlignLeft)

        summary_frame, summary_layout = card(tr("任务摘要"))
        self.summary_toggle = QPushButton(tr("收起任务摘要"))
        self.summary_toggle.setCheckable(True)
        self.summary_toggle.setChecked(True)
        self.summary_toggle.setAccessibleName(tr("切换任务摘要显示"))
        self.summary_toggle.toggled.connect(self._toggle_summary)
        summary_layout.addWidget(self.summary_toggle, 0, Qt.AlignmentFlag.AlignRight)
        self.summary_content = QWidget()
        summary_content_layout = QVBoxLayout(self.summary_content)
        summary_content_layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.summary_label = status_label("")
        self.self_loop_label = status_label(tr(SELF_LOOP_FALLBACK))
        self.self_loop_label.setObjectName("warningLabel")
        form.addRow(tr("任务名称"), self.name_edit)
        form.addRow(tr("状态"), self.summary_label)
        summary_content_layout.addLayout(form)
        summary_content_layout.addWidget(self.self_loop_label)
        toolbar = QHBoxLayout()
        self.start_button = QPushButton(tr("启动"))
        self.stop_button = QPushButton(tr("停止"))
        self.logs_button = QPushButton(tr("完整日志"))
        self.copy_button = QPushButton(tr("复制"))
        self.export_button = QPushButton(tr("导出配置"))
        self.delete_button = QPushButton(tr("删除"))
        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(self._stop)
        self.logs_button.clicked.connect(lambda: self.logs_requested.emit(self.task_id))
        self.copy_button.clicked.connect(self._copy)
        self.export_button.clicked.connect(self._export)
        self.delete_button.clicked.connect(self._delete)
        for button in (
            self.start_button,
            self.stop_button,
            self.logs_button,
            self.copy_button,
            self.export_button,
            self.delete_button,
        ):
            toolbar.addWidget(button)
        toolbar.addStretch()
        summary_content_layout.addLayout(toolbar)
        summary_layout.addWidget(self.summary_content)
        self.content.addWidget(summary_frame)

        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.tabs = _CurrentPageTabWidget()
        self._build_rules_tab()
        self._build_samples_tab()
        self._build_logs_tab()
        self._build_advanced_tab()
        self._build_versions_tab()
        self.tabs.setMinimumWidth(0)
        self.tabs.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.tabs.setUsesScrollButtons(False)
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.tabBar().setExpanding(True)
        self.tabs.tabBar().setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        self.workspace_splitter.addWidget(self.tabs)
        self.builder_panel = self._build_builder_panel()
        self.workspace_splitter.addWidget(self.builder_panel)
        self.workspace_splitter.setStretchFactor(0, 1)
        self.workspace_splitter.setStretchFactor(1, 0)
        self.workspace_splitter.setSizes([720, 330])
        self.workspace_splitter.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.content.addWidget(self.workspace_splitter)

    def _add_section_help(
        self,
        layout: QVBoxLayout,
        section: str,
        callback: Callable[[], None],
    ) -> QPushButton:
        """在卡片标题右侧加入可访问的圆形问号按钮。"""

        heading_item = layout.takeAt(0)
        heading = heading_item.widget()
        row = QHBoxLayout()
        if heading is not None:
            row.addWidget(heading)
        button = QPushButton("?")
        button.setObjectName("helpButton")
        button.setFixedSize(24, 24)
        button.setAccessibleName(tr("查看{section}填写说明", section=tr(section)))
        button.setToolTip(tr("查看填写说明"))
        button.clicked.connect(callback)
        row.addWidget(button)
        row.addStretch()
        layout.insertLayout(0, row)
        return button

    def _toggle_summary(self, visible: bool) -> None:
        self.summary_content.setVisible(visible)
        self.summary_toggle.setText(tr("收起任务摘要") if visible else tr("展开任务摘要"))

    def _build_rules_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        destination_frame, destination_layout = card(tr("目标绑定"))
        self.destination_help_button = self._add_section_help(
            destination_layout,
            "目标绑定",
            self._show_destination_help,
        )
        self.destination_type_label = status_label("")
        self.destination_hint_label = status_label("")
        self.destination_combo = SmoothComboBox()
        self.destination_refresh_button = QPushButton(tr("刷新目标结构"))
        self.destination_apply_button = QPushButton(tr("应用目标绑定"))
        self.destination_refresh_button.clicked.connect(self._refresh_destination_schema)
        self.destination_apply_button.clicked.connect(self._apply_destination_binding)
        destination_layout.addWidget(self.destination_type_label)
        destination_layout.addWidget(self.destination_hint_label)
        destination_layout.addWidget(self.destination_combo)
        # 两个目标操作保持同一行，避免纵向堆叠制造空白。
        destination_actions = QHBoxLayout()
        destination_actions.addStretch()
        destination_actions.addWidget(self.destination_refresh_button)
        destination_actions.addWidget(self.destination_apply_button)
        destination_layout.addLayout(destination_actions)
        layout.addWidget(destination_frame)

        trigger_frame, trigger_layout = card(tr("触发与上下文"))
        self.trigger_help_button = self._add_section_help(
            trigger_layout,
            "触发与上下文",
            self._show_trigger_help,
        )
        trigger_row = QGridLayout()
        self.message_trigger = ThemedCheckBox(tr("新消息"))
        self.reaction_trigger = ThemedCheckBox(tr("Reaction 事件"))
        self.include_quote = ThemedCheckBox(tr("包含引用消息"))
        trigger_row.addWidget(self.message_trigger, 0, 0)
        trigger_row.addWidget(self.reaction_trigger, 0, 1)
        trigger_row.addWidget(self.include_quote, 1, 0, 1, 2)
        trigger_row.setColumnStretch(2, 1)
        trigger_layout.addLayout(trigger_row)
        self.filters_edit = QPlainTextEdit()
        self.filters_edit.setPlaceholderText(tr("过滤规则 JSON 数组，例如 []"))
        self.filters_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.filters_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.filters_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 单行空规则保持紧凑，多行 JSON 仍随内容自动撑高。
        self.filters_edit.document().documentLayout().documentSizeChanged.connect(
            lambda _size: _fit_plain_text(self.filters_edit, 52)
        )
        _fit_plain_text(self.filters_edit, 52)
        trigger_layout.addWidget(QLabel(tr("过滤规则")))
        trigger_layout.addWidget(self.filters_edit)
        layout.addWidget(trigger_frame)

        mapping_frame, mapping_layout = card(tr("字段映射"))
        self.mapping_help_button = self._add_section_help(
            mapping_layout,
            "字段映射",
            self._show_mapping_help,
        )
        self.mapping_target_hint = status_label("")
        mapping_layout.addWidget(self.mapping_target_hint)
        source_grid = QGridLayout()
        source_grid.setHorizontalSpacing(8)
        source_grid.setVerticalSpacing(8)
        self.target_combo = SmoothComboBox()
        self.source_combo = SmoothComboBox()
        for field in self.SOURCE_FIELDS:
            source = self.SOURCE_OPTIONS[field]
            self.source_combo.addItem(tr(source), field)
            self.source_combo.setItemData(
                self.source_combo.count() - 1,
                source,
                Qt.ItemDataRole.UserRole + 99,
            )
        add = QPushButton(tr("添加映射"))
        remove = QPushButton(tr("删除选中映射"))
        insert = QPushButton(tr("插入构建说明"))
        insert_rule = QPushButton(tr("插入规则节点"))
        add.clicked.connect(self._add_mapping)
        remove.clicked.connect(self._remove_mapping)
        insert.clicked.connect(self._insert_source_reference)
        insert_rule.clicked.connect(self._insert_rule_reference)
        # 工具按钮单独成行，避免窄窗口被长字段引用和四个动作共同撑宽。
        source_grid.addWidget(QLabel(tr("目标字段")), 0, 0)
        source_grid.addWidget(self.target_combo, 0, 1, 1, 4)
        source_grid.addWidget(QLabel(tr("字段引用")), 1, 0)
        source_grid.addWidget(self.source_combo, 1, 1, 1, 4)
        source_grid.addWidget(add, 2, 0, 1, 2)
        source_grid.addWidget(remove, 2, 2, 1, 2)
        source_grid.addWidget(insert, 3, 0, 1, 2)
        source_grid.addWidget(insert_rule, 3, 2, 1, 2)
        source_grid.setColumnStretch(4, 1)
        mapping_layout.addLayout(source_grid)
        self.mapping_table = _workbench_table(
            ["目标字段", "来源字段"],
            _MAPPING_TABLE_MINIMUM_HEIGHT,
        )
        self.mapping_table.cellDoubleClicked.connect(self._insert_mapping_reference)
        mapping_layout.addWidget(self.mapping_table)
        self.save_rules_button = QPushButton(tr("保存规则与映射"))
        self.save_rules_button.clicked.connect(self._save_structured)
        mapping_layout.addWidget(self.save_rules_button, 0, Qt.AlignmentFlag.AlignRight)
        layout.addWidget(mapping_frame)
        layout.addStretch()
        self.tabs.addTab(tab, tr("规则与映射"))

    def _build_samples_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        controls = QHBoxLayout()
        add = QPushButton(tr("添加脱敏样例"))
        run = QPushButton(tr("运行样例测试"))
        add.clicked.connect(self._add_sample)
        run.clicked.connect(self._run_samples)
        controls.addWidget(add)
        controls.addWidget(run)
        controls.addStretch()
        layout.addLayout(controls)
        self.sample_result = status_label(tr("部署前至少需要一个脱敏样例。"))
        layout.addWidget(self.sample_result)
        self.samples_table = _workbench_table(["来源", "事件 ID", "消息类型", "期望输出"])
        layout.addWidget(self.samples_table)
        # 短页把剩余高度统一留在底部，避免 Qt 将空白分散到控件之间。
        layout.addStretch()
        self.tabs.addTab(tab, tr("样例与测试"))

    def _build_logs_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        controls = QHBoxLayout()
        refresh = QPushButton(tr("刷新"))
        complete = QPushButton(tr("打开完整日志"))
        refresh.clicked.connect(self.refresh)
        complete.clicked.connect(lambda: self.logs_requested.emit(self.task_id))
        controls.addWidget(refresh)
        controls.addWidget(complete)
        controls.addStretch()
        layout.addLayout(controls)
        self.recent_logs_table = _workbench_table(["时间", "阶段", "结果", "事件 ID", "预览"])
        layout.addWidget(self.recent_logs_table)
        layout.addStretch()
        self.tabs.addTab(tab, tr("运行日志"))

    def _build_advanced_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(status_label(tr("高级模式只接受 EIM DSL JSON；不执行 Python、JavaScript、Shell 或任意网络请求。")))
        self.dsl_edit = QPlainTextEdit()
        self.dsl_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.dsl_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.dsl_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.dsl_edit.document().documentLayout().documentSizeChanged.connect(
            lambda _size: _fit_plain_text(self.dsl_edit, 280)
        )
        _fit_plain_text(self.dsl_edit, 280)
        layout.addWidget(self.dsl_edit)
        format_button = QPushButton(tr("格式化"))
        save_button = QPushButton(tr("校验并保存 DSL"))
        format_button.clicked.connect(self._format_dsl)
        save_button.clicked.connect(self._save_advanced)
        layout.addWidget(button_row(format_button, save_button))
        layout.addStretch()
        self.tabs.addTab(tab, tr("高级 DSL"))

    def _build_versions_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(status_label(tr("成功构建后发布不可变版本；失败不会替换当前活动版本。")))
        self.versions_table = _workbench_table(["版本 ID", "状态", "内容哈希", "构建模型", "创建时间"])
        layout.addWidget(self.versions_table)
        layout.addStretch()
        self.tabs.addTab(tab, tr("版本"))

    def _build_builder_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("card")
        panel.setFixedWidth(320)
        layout = QVBoxLayout(panel)
        heading = QLabel(tr("AI 构建助手"))
        heading.setObjectName("cardTitle")
        layout.addWidget(heading)
        layout.addWidget(status_label(tr("AI 只能调用已登记的 EIM DSL 动作；连接器和目标器不可修改。")))
        self.model_combo = SmoothComboBox()
        self.model_combo.addItem(tr("确定性构建（不调用 AI）"), None)
        self.model_combo.currentIndexChanged.connect(self._sync_builder_guidance)
        layout.addWidget(QLabel(tr("构建配置")))
        layout.addWidget(self.model_combo)
        self.build_configuration_hint = status_label(
            tr("构建配置只列出已完成且通过 EIM 兼容性检测的 AI 配置。")
        )
        layout.addWidget(self.build_configuration_hint)
        self.detect_model_combo = SmoothComboBox()
        self.detect_model_combo.currentIndexChanged.connect(
            self._sync_compatibility_status
        )
        layout.addWidget(QLabel(tr("兼容性检测配置")))
        layout.addWidget(self.detect_model_combo)
        self.test_model_button = QPushButton(tr("检测 EIM 构建兼容性"))
        self.test_model_button.clicked.connect(self._test_model)
        layout.addWidget(self.test_model_button)
        self.compatibility_status = status_label(
            tr("先选择完整 AI 配置并检测；失败时会在这里显示可排查原因。")
        )
        layout.addWidget(self.compatibility_status)
        self.instruction_edit = QPlainTextEdit()
        self.instruction_edit.setMinimumHeight(130)
        layout.addWidget(QLabel(tr("构建说明")))
        self.instruction_hint = status_label("")
        layout.addWidget(self.instruction_hint)
        layout.addWidget(self.instruction_edit)
        self.build_button = QPushButton(tr("构建并部署"))
        self.build_start_button = QPushButton(tr("构建、部署并启动"))
        self.cancel_build_button = QPushButton(tr("取消构建"))
        self.cancel_build_button.setEnabled(False)
        self.build_button.clicked.connect(lambda: self._build(False))
        self.build_start_button.clicked.connect(lambda: self._build(True))
        self.cancel_build_button.clicked.connect(self._cancel_build)
        layout.addWidget(self.build_button)
        layout.addWidget(self.build_start_button)
        layout.addWidget(self.cancel_build_button)
        self.build_status = status_label("")
        layout.addWidget(self.build_status)
        layout.addStretch()
        self._configuration_health: dict[str, dict[str, Any]] = {}
        self._sync_builder_guidance()
        return panel

    def _sync_builder_guidance(self, _index: int = -1) -> None:
        """解释当前构建方式和说明文本的实际用途。"""

        if self.model_combo.currentData() is None:
            self.instruction_hint.setText(
                tr("当前为确定性构建：直接校验并发布现有 DSL 与样例，不调用 AI，构建说明会被忽略。")
            )
            placeholder = "确定性构建无需填写；选择已兼容的 AI 配置后可输入自然语言修改要求。"
            self.instruction_edit.setProperty("i18n_placeholder", placeholder)
            self.instruction_edit.setPlaceholderText(tr(placeholder))
            return
        self.instruction_hint.setText(
            tr("选择 AI 构建时，本说明会作为修改目标交给 AI；留空表示只检查并补全当前配置。")
        )
        placeholder = "例如：只归档正文包含“故障”的消息；标题写发送者名称，正文写消息内容，并保留引用消息与附件。"
        self.instruction_edit.setProperty("i18n_placeholder", placeholder)
        self.instruction_edit.setPlaceholderText(tr(placeholder))

    def _sync_compatibility_status(self, _index: int = -1) -> None:
        """在检测按钮旁显示所选配置的最近一次安全检测结果。"""

        configuration_id = str(self.detect_model_combo.currentData() or "")
        if not configuration_id:
            self.compatibility_status.setText(tr("暂无可检测的完整 AI 配置"))
            return
        health = self._configuration_health.get(configuration_id)
        if health is None:
            self.compatibility_status.setText(
                tr("尚未检测；点击上方按钮后，通过的配置会出现在“构建配置”中。")
            )
            return
        detail = str(health.get("detail") or "")
        self.compatibility_status.setText(
            tr("最近检测通过：{detail}", detail=detail)
            if health.get("compatible")
            else tr("最近检测失败：{detail}", detail=detail)
        )

    def open_task(self, task_id: str) -> None:
        self.task_id = str(task_id)
        self.refresh()
        self._load_ai_configurations()

    def refresh(self) -> None:
        if not self.task_id:
            return
        self.run_async(lambda: self.service.eim.task_detail(self.task_id), success=self._apply_snapshot)

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = dict(snapshot)
        task = snapshot["task"]
        editable = bool(task.get("editable")) and not self._building
        self._editable = editable
        self.name_edit.setText(str(task.get("name") or ""))
        self.summary_label.setText(
            f"{task.get('display_id', '')} · {tr('钉钉')} / {task.get('source_name', '')} · "
            f"{_state(task.get('build_state'))} · {_state(task.get('observed_state'))}"
        )
        self.self_loop_label.setText(tr(str(snapshot.get("self_loop_notice") or SELF_LOOP_FALLBACK)))
        self.name_edit.setEnabled(editable)
        self.save_rules_button.setEnabled(editable)
        self.dsl_edit.setReadOnly(not editable)
        self.destination_apply_button.setEnabled(editable)
        self.destination_refresh_button.setEnabled(editable)
        self.delete_button.setEnabled(editable)
        self.copy_button.setEnabled(editable)
        self.build_button.setEnabled(editable and not self._building)
        self.build_start_button.setEnabled(editable and not self._building)
        running = str(task.get("observed_state") or "") not in STOPPED_STATES
        self.start_button.setEnabled(not running and bool(task.get("active_version_id")))
        self.stop_button.setEnabled(running)
        self._apply_destination(snapshot.get("destination") or {})
        self._apply_dsl(snapshot.get("dsl") or {})
        self._apply_samples(list(snapshot.get("samples") or []))
        self._apply_logs(list(snapshot.get("logs") or []))
        self._apply_versions(list(snapshot.get("versions") or []))

    def _apply_dsl(self, dsl: dict[str, Any]) -> None:
        triggers = {str(item) for item in dsl.get("triggers") or []}
        self.message_trigger.setChecked("message" in triggers)
        self.reaction_trigger.setChecked("reaction" in triggers)
        self.include_quote.setChecked(bool((dsl.get("context") or {}).get("include_quote", True)))
        self.filters_edit.setPlainText(_json(dsl.get("filters") or []))
        mappings = dict(dsl.get("mappings") or {})
        self.mapping_table.setRowCount(len(mappings))
        for row, (target, source) in enumerate(mappings.items()):
            self.mapping_table.setItem(
                row,
                0,
                _item(self._target_field_labels.get(target, target), data=target),
            )
            self.mapping_table.setItem(
                row,
                1,
                _item(tr(self.SOURCE_OPTIONS.get(source, source)), data=source),
            )
        _fit_workbench_table(self.mapping_table, _MAPPING_TABLE_MINIMUM_HEIGHT)
        self.dsl_edit.setPlainText(_json(dsl))

    def _apply_destination(self, destination: dict[str, Any]) -> None:
        destination_type = str(destination.get("destination_type") or "")
        destination_name = {
            "dingtalk_doc": "钉钉文档",
            "dingtalk_sheet": "钉钉电子表格",
            "dingtalk_aitable": "钉钉 AI 表格",
        }.get(destination_type, destination_type)
        self.destination_type_label.setText(
            tr("{destination} · 已识别归档目标", destination=tr(destination_name))
        )
        self.destination_type_label.setToolTip(str(destination.get("url") or ""))
        self.destination_combo.clear()
        self.target_combo.clear()
        self._target_field_labels = {}
        stable = destination.get("stable_ids") or {}
        schema = destination.get("schema_snapshot") or {}
        if destination_type == "dingtalk_sheet":
            for sheet in schema.get("sheets") or []:
                sheet_id = _combo_item_id(sheet)
                name = str(sheet.get("name") or sheet.get("sheetName") or sheet_id)
                self.destination_combo.addItem(name, sheet_id)
            current = str(stable.get("sheet_id") or "")
            headers = [str(item) for item in schema.get("headers") or [] if str(item)]
            for header in headers:
                self._target_field_labels[header] = header
                self.target_combo.addItem(header, header)
            self.destination_hint_label.setText(
                tr("已读取 {count} 个首行表头；目标绑定选择实际写入的工作表。", count=len(headers))
            )
        elif destination_type == "dingtalk_aitable":
            fields = [item for item in schema.get("fields") or [] if isinstance(item, dict)]
            writable = [
                item for item in schema.get("writable_fields") or [] if isinstance(item, dict)
            ]
            text_count = 0
            self.destination_combo.addItem(tr("请选择专用 EIM 事件 ID 文本字段"), "")
            current = str(stable.get("event_key_field_id") or "")
            for field in writable:
                field_type = str(field.get("type") or field.get("fieldType") or "").casefold()
                field_id = _combo_item_id(field)
                name = str(field.get("fieldName") or field.get("field_name") or field_id)
                type_label = tr(self.TARGET_TYPE_LABELS.get(field_type, field_type or "未知类型"))
                label = f"{name} · {type_label} · {field_id}"
                self._target_field_labels[field_id] = label
                # 幂等字段由系统写入，不允许同时作为普通业务映射目标。
                if field_id != current:
                    self.target_combo.addItem(label, field_id)
                if field_type in {"text", "string", "1"}:
                    text_count += 1
                    self.destination_combo.addItem(label, field_id)
            if text_count == 0:
                self.destination_combo.clear()
                self.destination_combo.addItem(tr("没有可用的文本事件 ID 字段"), "")
            self.destination_hint_label.setText(
                tr(
                    "检测到 {total} 个字段，其中 {writable} 个可写；目标绑定仅显示 {text} 个文本字段，全部可写字段在下方字段映射中选择。",
                    total=len(fields),
                    writable=len(writable),
                    text=text_count,
                )
            )
        else:
            self.destination_combo.addItem(tr("普通文档无需额外绑定"), None)
            current = ""
            for field, source in self.DOCUMENT_FIELD_OPTIONS.items():
                label = tr(source)
                self._target_field_labels[field] = label
                self.target_combo.addItem(label, field)
            self.destination_hint_label.setText(
                tr("普通文档使用标题、正文、元数据和媒体四个 EIM 模板字段。")
            )
        index = self.destination_combo.findData(current)
        if index >= 0:
            self.destination_combo.setCurrentIndex(index)
        configurable = destination_type in {"dingtalk_sheet", "dingtalk_aitable"}
        binding_available = destination_type != "dingtalk_aitable" or any(
            self.destination_combo.itemData(item_index)
            for item_index in range(self.destination_combo.count())
        )
        self.destination_apply_button.setEnabled(
            self._editable and binding_available
        )
        self.destination_refresh_button.setEnabled(self._editable)
        self.destination_combo.setEnabled(configurable and self._editable)
        self.destination_apply_button.setVisible(configurable)
        self.target_combo.setEnabled(self._editable and self.target_combo.count() > 0)
        self.source_combo.setEnabled(self._editable)
        self.mapping_target_hint.setText(
            tr("可映射目标字段：{count} 个。请选择目标字段和带中文语义的字段引用后添加映射。", count=self.target_combo.count())
        )

    def _apply_samples(self, samples: list[dict[str, Any]]) -> None:
        self.samples_table.setRowCount(len(samples))
        for row, sample in enumerate(samples):
            event = sample.get("input") or {}
            self.samples_table.setItem(row, 0, _item(sample.get("source")))
            self.samples_table.setItem(row, 1, _item(event.get("event_id")))
            self.samples_table.setItem(row, 2, _item(event.get("message_kind") or event.get("event_type")))
            self.samples_table.setItem(row, 3, _item(_json(sample.get("expected") or {})))
        self.sample_result.setText(
            tr("已配置 {count} 个脱敏样例", count=len(samples))
            if samples
            else tr("部署前至少需要一个脱敏样例。")
        )
        _fit_workbench_table(self.samples_table, 160)

    def _apply_logs(self, logs: list[dict[str, Any]]) -> None:
        self.recent_logs_table.setRowCount(len(logs))
        for row, log in enumerate(logs):
            for column, value in enumerate(
                (log.get("timestamp"), _state(log.get("stage")), _state(log.get("result")), log.get("event_id"), log.get("preview"))
            ):
                self.recent_logs_table.setItem(row, column, _item(value))
        _fit_workbench_table(self.recent_logs_table, 160)

    def _apply_versions(self, versions: list[dict[str, Any]]) -> None:
        self.versions_table.setRowCount(len(versions))
        for row, version in enumerate(versions):
            for column, value in enumerate(
                (
                    version.get("version_id"),
                    _state(version.get("status")),
                    version.get("content_hash"),
                    version.get("builder_configuration_id") or tr("确定性"),
                    version.get("created_at"),
                )
            ):
                self.versions_table.setItem(row, column, _item(value))
        _fit_workbench_table(self.versions_table, 160)

    def _draft_from_structured(self) -> dict[str, Any]:
        dsl = json.loads(json.dumps(self.snapshot.get("dsl") or {}))
        triggers = []
        if self.message_trigger.isChecked():
            triggers.append("message")
        if self.reaction_trigger.isChecked():
            triggers.append("reaction")
        if not triggers:
            raise ValueError(tr("至少选择一种触发事件"))
        filters = json.loads(self.filters_edit.toPlainText() or "[]")
        if not isinstance(filters, list):
            raise ValueError(tr("过滤规则必须是 JSON 数组"))
        mappings: dict[str, str] = {}
        for row in range(self.mapping_table.rowCount()):
            target_item = self.mapping_table.item(row, 0)
            source_item = self.mapping_table.item(row, 1)
            target = (
                str(target_item.data(Qt.ItemDataRole.UserRole) or target_item.text()).strip()
                if target_item
                else ""
            )
            source = (
                str(source_item.data(Qt.ItemDataRole.UserRole) or source_item.text()).strip()
                if source_item
                else ""
            )
            if not target or not source:
                raise ValueError(tr("映射的目标字段和来源字段不能为空"))
            mappings[target] = source
        if not mappings:
            raise ValueError(tr("至少配置一个字段映射"))
        dsl["triggers"] = triggers
        dsl["filters"] = filters
        dsl["mappings"] = mappings
        dsl.setdefault("context", {})["include_quote"] = self.include_quote.isChecked()
        return dsl

    def _save_structured(self) -> None:
        try:
            dsl = self._draft_from_structured()
        except (ValueError, json.JSONDecodeError) as exc:
            self.show_error(str(exc))
            return
        self._save_dsl(dsl)

    def _save_advanced(self) -> None:
        try:
            dsl = json.loads(self.dsl_edit.toPlainText())
            if not isinstance(dsl, dict):
                raise ValueError(tr("EIM DSL 必须是 JSON 对象"))
        except (ValueError, json.JSONDecodeError) as exc:
            self.show_error(str(exc))
            return
        self._save_dsl(dsl)

    def _save_dsl(self, dsl: dict[str, Any]) -> None:
        name = self.name_edit.text().strip()
        self.run_async(
            lambda: self.service.eim.save_task(self.task_id, name=name, dsl=dsl),
            success=lambda detail: (self._apply_snapshot(detail), self.changed.emit()),
        )

    def _format_dsl(self) -> None:
        try:
            self.dsl_edit.setPlainText(_json(json.loads(self.dsl_edit.toPlainText())))
        except json.JSONDecodeError as exc:
            self.show_error(str(exc))

    def _add_mapping(self) -> None:
        target = str(self.target_combo.currentData() or "").strip()
        source = str(self.source_combo.currentData() or "").strip()
        if not target or not source:
            self.show_error(tr("请选择目标字段和字段引用"))
            return
        existing = {
            str(
                self.mapping_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                or self.mapping_table.item(row, 0).text()
            ).strip()
            for row in range(self.mapping_table.rowCount())
            if self.mapping_table.item(row, 0) is not None
        }
        if target in existing:
            self.show_error(tr("该目标字段已存在映射"))
            return
        row = self.mapping_table.rowCount()
        self.mapping_table.insertRow(row)
        self.mapping_table.setItem(
            row,
            0,
            _item(self._target_field_labels.get(target, target), data=target),
        )
        self.mapping_table.setItem(
            row,
            1,
            _item(tr(self.SOURCE_OPTIONS.get(source, source)), data=source),
        )
        _fit_workbench_table(self.mapping_table, _MAPPING_TABLE_MINIMUM_HEIGHT)

    def _remove_mapping(self) -> None:
        rows = sorted({index.row() for index in self.mapping_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.mapping_table.removeRow(row)
        _fit_workbench_table(self.mapping_table, _MAPPING_TABLE_MINIMUM_HEIGHT)

    def _append_instruction_reference(self, value: str) -> None:
        """把带类型的可信引用写入说明，不修改 DSL 或自动执行动作。"""

        current = self.instruction_edit.toPlainText()
        separator = "\n" if current and not current.endswith("\n") else ""
        self.instruction_edit.insertPlainText(f"{separator}{value}")
        self.instruction_edit.setFocus()

    def _insert_source_reference(self) -> None:
        field = str(self.source_combo.currentData() or "")
        field_type = self.SOURCE_TYPES.get(field, "string")
        self._append_instruction_reference(f"[source:{field_type}] {field}")

    def _insert_rule_reference(self) -> None:
        self._append_instruction_reference("[rule:object] triggers / filters / context")

    def _insert_mapping_reference(self, row: int, column: int) -> None:
        item = self.mapping_table.item(row, column)
        if item is None:
            return
        value = str(item.data(Qt.ItemDataRole.UserRole) or item.text()).strip()
        if not value:
            return
        kind = "target" if column == 0 else "source"
        field_type = "any" if kind == "target" else self.SOURCE_TYPES.get(value, "string")
        self._append_instruction_reference(f"[{kind}:{field_type}] {value}")

    def _show_destination_help(self) -> None:
        destination = self.snapshot.get("destination") or {}
        destination_type = str(destination.get("destination_type") or "")
        schema = destination.get("schema_snapshot") or {}
        paragraphs = [
            tr("目标绑定用于固定实际写入位置和幂等键，不是选择归档内容字段。"),
        ]
        if destination_type == "dingtalk_aitable":
            fields = [item for item in schema.get("fields") or [] if isinstance(item, dict)]
            writable = [
                item for item in schema.get("writable_fields") or [] if isinstance(item, dict)
            ]
            text_count = sum(
                str(item.get("type") or item.get("fieldType") or "").casefold()
                in {"text", "string", "1"}
                for item in writable
            )
            paragraphs.extend(
                [
                    tr("AI 表格必须绑定一个专用可写文本字段保存 EIM 事件 ID，系统用它防止同一事件重复写入。"),
                    tr(
                        "当前检测到 {total} 个字段、{writable} 个可写字段，其中 {text} 个文本字段可作为 EIM 事件 ID。其他字段会在“字段映射”中完整显示。",
                        total=len(fields),
                        writable=len(writable),
                        text=text_count,
                    ),
                    tr("真实范例：在 AI 表格中新建文本字段“EIM 事件 ID”，点击“刷新目标结构”，再回到这里选择它。不要选择“消息内容”“站点”等业务字段。"),
                ]
            )
        elif destination_type == "dingtalk_sheet":
            paragraphs.append(
                tr("真实范例：选择“问题归档”工作表；首行准备“_eim_event_id、消息内容、发送人”三列表头，_eim_event_id 用于防重复。")
            )
        else:
            paragraphs.append(
                tr("普通文档无需额外绑定。系统固定提供 title（标题）、body（正文）、metadata（元数据）和 media（媒体）四个写入槽位。")
            )
        QMessageBox.information(
            self,
            tr("目标绑定填写说明"),
            "\n\n".join(paragraphs),
        )

    def _show_trigger_help(self) -> None:
        example = [
            {"field": "message.text", "operator": "contains", "value": tr("故障")},
            {"field": "sender.name", "operator": "exists"},
        ]
        QMessageBox.information(
            self,
            tr("触发与上下文填写说明"),
            "\n\n".join(
                [
                    tr("选择任务接收的新消息或 Reaction 事件；“包含引用消息”会把被回复消息一并放入上下文。"),
                    tr("过滤规则填写 JSON 数组，多个条件需要全部满足。支持 equals、contains、regex、in、exists。留空请填写 []。"),
                    tr("真实范例：只归档正文包含“故障”且能识别发送者名称的消息：") + "\n" + _json(example),
                ]
            ),
        )

    def _show_mapping_help(self) -> None:
        destination = self.snapshot.get("destination") or {}
        destination_type = str(destination.get("destination_type") or "")
        schema = destination.get("schema_snapshot") or {}
        paragraphs = [
            tr("目标字段是归档目标中的列或字段；字段引用是钉钉事件中要写入的值。选择两项后点击“添加映射”。"),
            tr("字段引用保留稳定英文路径，并在右侧补充中文语义；保存时只写入英文路径。"),
        ]
        examples: list[str] = []
        if destination_type == "dingtalk_aitable":
            event_field = str((destination.get("stable_ids") or {}).get("event_key_field_id") or "")
            writable = [
                item for item in schema.get("writable_fields") or [] if isinstance(item, dict)
            ]
            for expected_type, source in (("text", "message.text"), ("attachment", "media")):
                field = next(
                    (
                        item
                        for item in writable
                        if _combo_item_id(item) != event_field
                        and str(item.get("type") or item.get("fieldType") or "").casefold()
                        == expected_type
                    ),
                    None,
                )
                if field is not None:
                    field_id = _combo_item_id(field)
                    name = str(field.get("fieldName") or field.get("field_name") or field_id)
                    examples.append(
                        f"{name} · {field_id}  ←  {tr(self.SOURCE_OPTIONS[source])}"
                    )
            paragraphs.append(
                tr("目标绑定中的 EIM 事件 ID 由系统自动写入，不要再添加到字段映射。")
            )
        elif destination_type == "dingtalk_sheet":
            header = next(
                (str(item) for item in schema.get("headers") or [] if str(item) != "_eim_event_id"),
                tr("消息内容"),
            )
            examples.append(f"{header}  ←  {tr(self.SOURCE_OPTIONS['message.text'])}")
        else:
            examples.extend(
                [
                    f"{tr(self.DOCUMENT_FIELD_OPTIONS['title'])}  ←  {tr(self.SOURCE_OPTIONS['sender.name'])}",
                    f"{tr(self.DOCUMENT_FIELD_OPTIONS['body'])}  ←  {tr(self.SOURCE_OPTIONS['message.text'])}",
                    f"{tr(self.DOCUMENT_FIELD_OPTIONS['metadata'])}  ←  {tr(self.SOURCE_OPTIONS['event.id'])}",
                    f"{tr(self.DOCUMENT_FIELD_OPTIONS['media'])}  ←  {tr(self.SOURCE_OPTIONS['media'])}",
                ]
            )
        if examples:
            paragraphs.append(tr("当前目标的真实填写范例：") + "\n" + "\n".join(examples))
        QMessageBox.information(
            self,
            tr("字段映射填写说明"),
            "\n\n".join(paragraphs),
        )

    def _refresh_destination_schema(self) -> None:
        self.destination_refresh_button.setEnabled(False)
        self.run_async(
            lambda: self.service.eim.refresh_destination(self.task_id),
            success=self._apply_snapshot,
            finished=lambda: self.destination_refresh_button.setEnabled(self._editable),
        )

    def _apply_destination_binding(self) -> None:
        destination = self.snapshot.get("destination") or {}
        stable = dict(destination.get("stable_ids") or {})
        destination_type = str(destination.get("destination_type") or "")
        selected = self.destination_combo.currentData()
        if destination_type == "dingtalk_sheet":
            stable["sheet_id"] = str(selected or "")
        elif destination_type == "dingtalk_aitable":
            stable["event_key_field_id"] = str(selected or "")
        self.run_async(
            lambda: self.service.eim.configure_destination(self.task_id, stable),
            success=self._apply_snapshot,
        )

    def _add_sample(self) -> None:
        task = self.snapshot.get("task") or {}
        dialog = SampleDialog(task, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        event, expected = dialog.values()
        self.run_async(
            lambda: self.service.eim.add_sample(self.task_id, event, expected),
            success=self._apply_snapshot,
        )

    def _run_samples(self) -> None:
        self.sample_result.setText(tr("正在运行样例测试…"))
        self.run_async(
            lambda: self.service.eim.simulate_draft(self.task_id),
            success=lambda value: self.sample_result.setText(
                tr(
                    "样例测试通过：{passed}；总数 {total}，失败 {failed}",
                    passed=tr("是") if value.get("passed") else tr("否"),
                    total=value.get("total", 0),
                    failed=value.get("failed", 0),
                )
            ),
        )

    def _load_ai_configurations(self) -> None:
        def work() -> tuple[dict[str, Any], list[dict[str, Any]]]:
            return self.service.get_ai_configurations(), self.service.eim.ai_configuration_health()

        self.run_async(work, success=self._apply_ai_configurations)

    def _apply_ai_configurations(
        self,
        value: tuple[dict[str, Any], list[dict[str, Any]]],
    ) -> None:
        """把完整配置按检测状态分流到检测和构建下拉框。"""

        configurations, health_rows = value
        self._configuration_health = {
            str(item.get("configuration_id")): dict(item)
            for item in health_rows
        }
        compatible = {
            configuration_id
            for configuration_id, health in self._configuration_health.items()
            if health.get("compatible")
        }
        current = self.model_combo.currentData()
        detect_current = self.detect_model_combo.currentData()
        self.model_combo.clear()
        self.detect_model_combo.clear()
        self.model_combo.addItem(tr("确定性构建（不调用 AI）"), None)
        complete_count = 0
        for configuration in configurations.get("configurations") or []:
            if not configuration.get("complete"):
                continue
            complete_count += 1
            name = str(configuration.get("name") or configuration.get("provider_label") or "AI")
            model = str(configuration.get("model") or "")
            configuration_id = str(configuration.get("id"))
            label = f"{name} · {model}"
            health = self._configuration_health.get(configuration_id)
            status = (
                tr("已兼容")
                if health and health.get("compatible")
                else tr("检测失败")
                if health
                else tr("未检测")
            )
            self.detect_model_combo.addItem(f"{label} · {status}", configuration_id)
            if configuration_id in compatible:
                self.model_combo.addItem(f"{label} · {tr('已兼容')}", configuration_id)
        index = self.model_combo.findData(current)
        if index >= 0:
            self.model_combo.setCurrentIndex(index)
        detect_index = self.detect_model_combo.findData(detect_current)
        if detect_index >= 0:
            self.detect_model_combo.setCurrentIndex(detect_index)
        self.test_model_button.setEnabled(complete_count > 0)
        self.build_configuration_hint.setText(
            tr("没有完整 AI 配置；请先到“设置 → AI 配置”补全模型和凭证。")
            if complete_count == 0
            else tr("已有完整 AI 配置，但尚无检测通过项；请在下方检测并根据失败原因排查。")
            if not compatible
            else tr("已有 {count} 条 AI 配置通过检测，可用于受控构建。", count=len(compatible))
        )
        self._sync_builder_guidance()
        self._sync_compatibility_status()

    def _test_model(self) -> None:
        configuration_id = self.detect_model_combo.currentData()
        if not configuration_id:
            self.compatibility_status.setText(tr("暂无可检测的完整 AI 配置"))
            return

        def success(value: dict[str, Any]) -> None:
            self.compatibility_status.setText(
                tr("兼容性：{value}", value=tr("通过") if value.get("compatible") else value.get("detail"))
            )
            self._load_ai_configurations()

        self.test_model_button.setEnabled(False)
        self.compatibility_status.setText(tr("正在检测 EIM 构建兼容性…"))
        self.run_async(
            lambda: self.service.eim.test_ai_configuration(str(configuration_id)),
            success=success,
            failure=lambda message: self.compatibility_status.setText(
                tr("兼容性检测失败：{message}", message=tr(message))
            ),
            finished=lambda: self.test_model_button.setEnabled(
                self.detect_model_combo.count() > 0
            ),
        )

    def _build(self, start_after: bool) -> None:
        try:
            dsl = json.loads(self.dsl_edit.toPlainText())
            if not isinstance(dsl, dict):
                raise ValueError(tr("EIM DSL 必须是 JSON 对象"))
        except (ValueError, json.JSONDecodeError) as exc:
            self.show_error(str(exc))
            return
        self._set_building(True)
        configuration_id = self.model_combo.currentData()
        instruction = self.instruction_edit.toPlainText().strip() if configuration_id else ""
        name = self.name_edit.text().strip()

        def work() -> dict[str, Any]:
            self.service.eim.save_task(self.task_id, name=name, dsl=dsl)
            return self.service.eim.build_task(
                self.task_id,
                configuration_id=str(configuration_id) if configuration_id else None,
                instruction=instruction,
                start_after=start_after,
            )

        self.build_status.setText(tr("正在构建、测试并发布…"))
        self.run_async(
            work,
            success=lambda value: self._build_succeeded(value),
            failure=lambda message: self._build_failed(message),
            finished=lambda: self._set_building(False),
        )

    def _build_succeeded(self, value: dict[str, Any]) -> None:
        self.build_status.setText(tr("构建发布成功 · {value}", value=value.get("version_id", "")))
        self.changed.emit()
        self.refresh()

    def _build_failed(self, message: str) -> None:
        self.build_status.setText(tr("构建失败：{message}", message=tr(message)))
        self.show_error(message)
        self.refresh()

    def _set_building(self, building: bool) -> None:
        self._building = building
        self.build_button.setEnabled(not building)
        self.build_start_button.setEnabled(not building)
        self.cancel_build_button.setEnabled(building)

    def _cancel_build(self) -> None:
        self.service.eim.cancel_build(self.task_id)
        self.build_status.setText(tr("正在取消构建…"))

    def _start(self) -> None:
        self.run_async(
            lambda: self.service.eim.start_task(self.task_id),
            success=lambda detail: (self._apply_snapshot(detail), self.changed.emit()),
        )

    def _stop(self) -> None:
        self.run_async(
            lambda: self.service.eim.stop_task(self.task_id),
            success=lambda detail: (self._apply_snapshot(detail), self.changed.emit()),
        )

    def _copy(self) -> None:
        self.run_async(
            lambda: self.service.eim.copy_task(self.task_id),
            success=lambda detail: self.open_task(str(detail["task"]["task_id"])),
        )

    def _export(self) -> None:
        path, _selected = QFileDialog.getSaveFileName(
            self,
            tr("导出 EIM 配置"),
            f"{self.name_edit.text().strip() or 'eim-task'}.eim.zip",
            tr("EIM 配置包 (*.eim.zip)"),
        )
        if not path:
            return
        self.run_async(
            lambda: self.service.eim.export_task(self.task_id, path),
            success=lambda value: self.show_info(tr("配置已导出：{value}", value=value)),
        )

    def _delete(self) -> None:
        if not confirm_action(self, "删除监听任务", "确定将此监听任务移入回收站吗？"):
            return
        self.run_async(
            lambda: self.service.eim.delete_task(self.task_id),
            success=lambda _value: (self.changed.emit(), self.back_requested.emit()),
        )


class EIMLogsPage(BasePage):
    """可筛选、可导出、可回读关联标识并可人工重试死信的日志页。"""

    back_requested = Signal()

    def __init__(self, service: Any):
        super().__init__(tr("EIM 运行日志"), tr("正文只显示截断且脱敏的预览，导出同样不包含凭证。"))
        self.service = service
        self._requested_task_id = ""
        back = QPushButton(tr("← 返回"))
        back.clicked.connect(self.back_requested.emit)
        self.content.insertWidget(0, back, 0, Qt.AlignmentFlag.AlignLeft)

        filters_frame, filters_layout = card(tr("筛选与导出"))
        row = QGridLayout()
        self.task_combo = SmoothComboBox()
        self.task_combo.addItem(tr("全部任务"), None)
        self.time_combo = SmoothComboBox()
        for label, hours in (("全部时间", None), ("最近 24 小时", 24), ("最近 7 天", 168), ("最近 30 天", 720)):
            self.time_combo.addItem(tr(label), hours)
        self.event_combo = SmoothComboBox()
        self.event_combo.addItem(tr("全部事件"), None)
        self.event_combo.addItem(tr("新消息"), "message")
        self.event_combo.addItem(tr("Reaction 事件"), "reaction")
        self.stage_combo = SmoothComboBox()
        for label, value in (("全部阶段", None), ("接收", "receive"), ("过滤", "filter"), ("AI", "ai"), ("映射", "mapping"), ("写入", "delivery"), ("重试", "retry")):
            self.stage_combo.addItem(tr(label), value)
        self.result_combo = SmoothComboBox()
        for label, value in (
            ("全部结果", None),
            ("完成", "completed"),
            ("跳过", "skipped"),
            ("重试", "retry"),
            ("归档原文", "archive_raw"),
            ("死信", "dead_letter"),
            ("失败", "failed"),
            ("降级", "degraded"),
        ):
            self.result_combo.addItem(tr(label), value)
        refresh = QPushButton(tr("刷新"))
        export_csv = QPushButton(tr("导出 CSV"))
        export_json = QPushButton(tr("导出 JSON"))
        refresh.clicked.connect(self.refresh)
        export_csv.clicked.connect(lambda: self._export("csv"))
        export_json.clicked.connect(lambda: self._export("json"))
        for column, (label, widget) in enumerate(
            (("任务", self.task_combo), ("时间", self.time_combo), ("事件类型", self.event_combo), ("阶段", self.stage_combo), ("结果", self.result_combo))
        ):
            row.addWidget(QLabel(tr(label)), 0, column)
            row.addWidget(widget, 1, column)
        row.addWidget(refresh, 1, 5)
        row.addWidget(export_csv, 1, 6)
        row.addWidget(export_json, 1, 7)
        filters_layout.addLayout(row)
        self.content.addWidget(filters_frame)

        logs_frame, logs_layout = card(tr("日志链路"))
        self.logs_table = _table(["时间", "任务", "阶段", "结果", "事件 ID", "消息 ID", "外部引用", "预览"])
        self.logs_table.setMinimumHeight(280)
        self.logs_table.itemSelectionChanged.connect(self._show_selected_detail)
        logs_layout.addWidget(self.logs_table)
        self.detail_edit = QPlainTextEdit()
        self.detail_edit.setReadOnly(True)
        self.detail_edit.setMaximumHeight(150)
        logs_layout.addWidget(self.detail_edit)
        self.content.addWidget(logs_frame)

        dead_frame, dead_layout = card(tr("死信重试"))
        self.dead_table = _table(["投递 ID", "任务", "尝试次数", "最后错误", "操作"])
        dead_layout.addWidget(self.dead_table)
        self.content.addWidget(dead_frame)
        self.add_stretch()

    def set_task_filter(self, task_id: str = "") -> None:
        self._requested_task_id = str(task_id or "")
        self.refresh()

    def _filters(self) -> dict[str, Any]:
        hours = self.time_combo.currentData()
        since = (datetime.now(UTC) - timedelta(hours=int(hours))).isoformat() if hours else None
        return {
            "task_id": self._requested_task_id or self.task_combo.currentData(),
            "event_type": self.event_combo.currentData(),
            "stage": self.stage_combo.currentData(),
            "result": self.result_combo.currentData(),
            "since": since,
            "limit": 1000,
        }

    def refresh(self) -> None:
        filters = self._filters()

        def work() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
            return (
                self.service.eim.overview(),
                self.service.eim.logs(**filters),
                self.service.eim.dead_letters(task_id=filters.get("task_id")),
            )

        self.run_async(work, success=self._apply)

    def _apply(self, value: tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]) -> None:
        overview, logs, dead_letters = value
        selected = self._requested_task_id or self.task_combo.currentData()
        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        self.task_combo.addItem(tr("全部任务"), None)
        for task in overview.get("tasks") or []:
            self.task_combo.addItem(str(task.get("name") or task.get("display_id")), str(task.get("task_id")))
        index = self.task_combo.findData(selected)
        if index >= 0:
            self.task_combo.setCurrentIndex(index)
        self.task_combo.blockSignals(False)
        self._requested_task_id = ""
        self.logs_table.setRowCount(len(logs))
        for row, log in enumerate(logs):
            values = (
                log.get("timestamp"), log.get("task_id"), _state(log.get("stage")), _state(log.get("result")),
                log.get("event_id"), log.get("message_id"), log.get("external_ref"), log.get("preview"),
            )
            for column, item_value in enumerate(values):
                self.logs_table.setItem(row, column, _item(item_value, data=log if column == 0 else None))
        self.dead_table.setRowCount(len(dead_letters))
        for row, delivery in enumerate(dead_letters):
            self.dead_table.setItem(row, 0, _item(delivery.get("delivery_id")))
            self.dead_table.setItem(row, 1, _item(delivery.get("task_id")))
            self.dead_table.setItem(row, 2, _item(delivery.get("attempts")))
            self.dead_table.setItem(row, 3, _item(delivery.get("last_error")))
            retry = QPushButton(tr("重试"))
            retry.clicked.connect(
                lambda _checked=False, delivery_id=str(delivery.get("delivery_id") or ""): self._retry(delivery_id)
            )
            self.dead_table.setCellWidget(row, 4, retry)

    def _show_selected_detail(self) -> None:
        rows = self.logs_table.selectionModel().selectedRows()
        if not rows:
            self.detail_edit.clear()
            return
        item = self.logs_table.item(rows[0].row(), 0)
        self.detail_edit.setPlainText(_json(item.data(Qt.ItemDataRole.UserRole) if item else {}))

    def _export(self, format_name: str) -> None:
        suffix = ".json" if format_name == "json" else ".csv"
        path, _selected = QFileDialog.getSaveFileName(
            self,
            tr("导出 EIM 日志"),
            f"eim-logs{suffix}",
            tr("JSON 文件 (*.json)") if format_name == "json" else tr("CSV 文件 (*.csv)"),
        )
        if not path:
            return
        filters = self._filters()
        self.run_async(
            lambda: self.service.eim.export_logs(path, format=format_name, **filters),
            success=lambda value: self.show_info(tr("日志已导出：{value}", value=value)),
        )

    def _retry(self, delivery_id: str) -> None:
        self.run_async(
            lambda: self.service.eim.retry_delivery(delivery_id),
            success=lambda value: self.refresh() if value else self.show_error(tr("该死信已被处理或不存在")),
        )


class EIMRecyclePage(BasePage):
    """软删除任务的恢复与二次确认永久删除。"""

    back_requested = Signal()

    def __init__(self, service: Any):
        super().__init__(tr("EIM 回收站"), tr("恢复后任务保持已停止；永久删除会同时清理本地版本和媒体。"))
        self.service = service
        back = QPushButton(tr("← 返回"))
        back.clicked.connect(self.back_requested.emit)
        self.content.insertWidget(0, back, 0, Qt.AlignmentFlag.AlignLeft)
        self.table = _table(["任务 ID", "任务名称", "来源群", "删除时间", "操作"])
        self.content.addWidget(self.table)
        self.add_stretch()

    def refresh(self) -> None:
        self.run_async(lambda: self.service.eim.recycle_bin(), success=self._apply)

    def _apply(self, tasks: list[dict[str, Any]]) -> None:
        self.table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            task_id = str(task.get("task_id") or "")
            for column, value in enumerate(
                (task.get("display_id"), task.get("name"), task.get("source_name"), task.get("deleted_at"))
            ):
                self.table.setItem(row, column, _item(value))
            actions = QWidget()
            layout = QHBoxLayout(actions)
            layout.setContentsMargins(0, 0, 0, 0)
            restore = QPushButton(tr("恢复"))
            purge = QPushButton(tr("永久删除"))
            restore.clicked.connect(lambda _checked=False, value=task_id: self._restore(value))
            purge.clicked.connect(lambda _checked=False, value=task_id: self._purge(value))
            layout.addWidget(restore)
            layout.addWidget(purge)
            self.table.setCellWidget(row, 4, actions)

    def _restore(self, task_id: str) -> None:
        self.run_async(lambda: self.service.eim.restore_task(task_id), success=lambda _value: self.refresh())

    def _purge(self, task_id: str) -> None:
        if not confirm_action(self, "永久删除 EIM 任务", "永久删除后无法恢复，确定继续吗？"):
            return
        self.run_async(lambda: self.service.eim.purge_task(task_id), success=lambda _value: self.refresh())
