"""快捷部署任务表格、详情和回收站复用控件。"""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from windows_native.i18n import tr, translate_widget_tree
from windows_native.ui.common import BasePage, open_target, status_label
from windows_native.ui.task_widgets import StatusIndicator


DEPLOYMENT_STATUS_TEXT = {
    "queued": "排队中",
    "scheduled": "定时等待",
    "running": "进行中",
    "stopping": "停止中",
    "completed": "已完成",
    "failed": "失败",
    "stopped": "已停止",
    "interrupted": "已中断",
}
_ACTIVE_STATUSES = frozenset({"queued", "scheduled", "running", "stopping"})
_ELAPSED_STATUSES = frozenset({"queued", "running", "stopping"})


def deployment_elapsed(task: dict) -> str:
    seconds = int(task.get("duration_seconds") or 0)
    if str(task.get("status")) in _ELAPSED_STATUSES and task.get("started_at"):
        try:
            started = datetime.fromisoformat(str(task["started_at"]))
            seconds = max(
                seconds,
                int((datetime.now().astimezone() - started).total_seconds()),
            )
        except ValueError:
            pass
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return tr(
        "耗时：{hours}小时{minutes}分钟{seconds}秒",
        hours=hours,
        minutes=minutes,
        seconds=seconds,
    )


class DeploymentTaskTable(QWidget):
    """正常列表和回收站共用的部署任务表格。"""

    def __init__(
        self,
        *,
        recycle_mode: bool = False,
        on_details: Callable[[dict], None] | None = None,
        on_logs: Callable[[dict], None] | None = None,
        on_stop: Callable[[str], None] | None = None,
        on_retry: Callable[[str], None] | None = None,
        on_delete: Callable[[str], None] | None = None,
        on_restore: Callable[[str], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.recycle_mode = recycle_mode
        self.on_details = on_details
        self.on_logs = on_logs
        self.on_stop = on_stop
        self.on_retry = on_retry
        self.on_delete = on_delete
        self.on_restore = on_restore
        self._last_tasks: list[dict] | None = None
        self._tasks_by_id: dict[str, dict] = {}
        self._elapsed_labels: dict[str, QLabel] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            [tr("任务 ID"), tr("迭代名称"), tr("进度"), tr("状态"), tr("操作")]
        )
        self.table.setAccessibleName(tr("迭代部署任务列表"))
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(72)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(2, 164)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(self.table)
        self.empty = QLabel(tr("暂无任务"))
        self.empty.setObjectName("emptyState")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setMinimumHeight(220)
        layout.addWidget(self.empty)

        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(1000)
        self.elapsed_timer.timeout.connect(self._refresh_elapsed)
        self.elapsed_timer.start()

    def set_tasks(self, tasks: list[dict]) -> None:
        self._tasks_by_id = {
            str(item.get("task_id") or ""): dict(item) for item in tasks
        }
        if self._last_tasks == tasks:
            self._refresh_elapsed()
            return
        self._last_tasks = copy.deepcopy(tasks)
        self._elapsed_labels.clear()
        self.table.setVisible(bool(tasks))
        self.empty.setVisible(not tasks)
        self.table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            self._fill_row(row, task)

    def _fill_row(self, row: int, task: dict) -> None:
        task_id = str(task.get("task_id") or "")
        item = QTableWidgetItem(task_id)
        item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, 0, item)

        name = QPushButton(str(task.get("iteration_name") or ""))
        name.setObjectName("tableLink")
        if bool((task.get("schedule") or {}).get("enabled")):
            # 优先采用系统主题的日程图标，Windows 无对应主题时使用原生循环箭头。
            fallback = self.style().standardIcon(QStyle.SP_BrowserReload)
            name.setIcon(QIcon.fromTheme("appointment-new", fallback))
            name.setIconSize(QSize(16, 16))
            name.setAccessibleName(
                tr(
                    "定时部署任务：{name}",
                    name=str(task.get("iteration_name") or ""),
                )
            )
            name.setToolTip(tr("定时部署任务"))
        if self.on_details:
            name.clicked.connect(
                lambda _checked=False, value=dict(task): self.on_details(value)
            )
        self.table.setCellWidget(row, 1, name)

        progress_widget = QWidget()
        progress_widget.setObjectName("progressCell")
        progress_layout = QVBoxLayout(progress_widget)
        progress_layout.setContentsMargins(8, 3, 8, 3)
        progress_layout.setSpacing(1)
        top = QHBoxLayout()
        state = str(task.get("status") or "failed")
        indicator_state = (
            "queued"
            if state == "scheduled"
            else "running"
            if state in _ACTIVE_STATUSES
            else state
        )
        top.addWidget(StatusIndicator(indicator_state))
        top.addWidget(QLabel(f"{int(task.get('progress_percent') or 0)}%"))
        current = int(task.get("current_step") or 0)
        total = int(task.get("total_steps") or len(task.get("items") or []))
        progress_count = QPushButton(f"{current}/{total}")
        progress_count.setObjectName("progressCountLink")
        progress_count.setFlat(True)
        progress_count.setCursor(Qt.PointingHandCursor)
        progress_count.setAccessibleName(tr("查看部署执行日志"))
        progress_count.setToolTip(tr("点击查看部署执行日志"))
        if self.on_logs:
            progress_count.clicked.connect(
                lambda _checked=False, value=dict(task): self.on_logs(value)
            )
        top.addWidget(progress_count)
        top.addStretch()
        progress_layout.addLayout(top)
        elapsed = QLabel(deployment_elapsed(task))
        elapsed.setObjectName("tableElapsed")
        progress_layout.addWidget(elapsed)
        self._elapsed_labels[task_id] = elapsed
        self.table.setCellWidget(row, 2, progress_widget)

        status = QTableWidgetItem(tr(DEPLOYMENT_STATUS_TEXT.get(state, "未知")))
        status.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, 3, status)

        actions = QWidget()
        action_layout = QHBoxLayout(actions)
        action_layout.setContentsMargins(6, 0, 6, 0)
        action_layout.setSpacing(6)
        if self.recycle_mode:
            restore = QPushButton(tr("恢复"))
            restore.setObjectName("compact")
            if self.on_restore:
                restore.clicked.connect(
                    lambda _checked=False, value=task_id: self.on_restore(value)
                )
            action_layout.addWidget(restore)
        else:
            active = state in _ACTIVE_STATUSES
            if active:
                stop = QPushButton(tr("停止"))
                stop.setObjectName("dangerCompact")
                if self.on_stop:
                    stop.clicked.connect(
                        lambda _checked=False, value=task_id: self.on_stop(value)
                    )
                action_layout.addWidget(stop)
            else:
                retry = QPushButton(tr("重新部署"))
                retry.setObjectName("compact")
                if self.on_retry:
                    retry.clicked.connect(
                        lambda _checked=False, value=task_id: self.on_retry(value)
                    )
                action_layout.addWidget(retry)
            delete = QPushButton(tr("删除"))
            delete.setObjectName("dangerCompact")
            delete.setEnabled(not active)
            delete.setToolTip(tr("请先停止任务") if active else "")
            if self.on_delete:
                delete.clicked.connect(
                    lambda _checked=False, value=task_id: self.on_delete(value)
                )
            action_layout.addWidget(delete)
        self.table.setCellWidget(row, 4, actions)

    def _refresh_elapsed(self) -> None:
        for task_id, label in list(self._elapsed_labels.items()):
            task = self._tasks_by_id.get(task_id)
            if task is not None:
                label.setText(deployment_elapsed(task))


class DeploymentDetailsDialog(QDialog):
    """按环境、项目、分支层级展示部署计划和 Jenkins 构建结果。"""

    def __init__(self, task: dict, parent: QWidget | None = None):
        super().__init__(parent)
        task_id = str(task.get("task_id") or "")
        self.setWindowTitle(tr("迭代部署详情 · {value}", value=task_id))
        self.setMinimumSize(760, 480)
        layout = QVBoxLayout(self)
        heading = QLabel(str(task.get("iteration_name") or ""))
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)
        status = tr(
            DEPLOYMENT_STATUS_TEXT.get(str(task.get("status") or ""), "未知")
        )
        summary = status_label(
            "\n".join(
                [
                    tr("任务 ID：{value}", value=task_id),
                    tr("状态：{value}", value=status),
                    deployment_elapsed(task),
                    tr(
                        "部署进度：{current}/{total}",
                        current=int(task.get("current_step") or 0),
                        total=int(task.get("total_steps") or len(task.get("items") or [])),
                    ),
                ]
                + (
                    [tr("错误：{value}", value=str(task.get("error") or ""))]
                    if task.get("error")
                    else []
                )
            )
        )
        layout.addWidget(summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            [tr("环境 / 项目"), tr("分支"), tr("状态"), "Jenkins"]
        )
        self.tree.setColumnWidth(0, 310)
        environment_items: dict[str, QTreeWidgetItem] = {}
        for item in task.get("items") or []:
            environment = str(item.get("environment") or "")
            parent_item = environment_items.get(environment)
            if parent_item is None:
                parent_item = QTreeWidgetItem([environment, "", "", ""])
                environment_items[environment] = parent_item
                self.tree.addTopLevelItem(parent_item)
            description = str(item.get("description") or "")
            label = str(item.get("project_full_name") or "")
            if description:
                label = f"{label}（{description}）"
            child = QTreeWidgetItem(
                [
                    label,
                    str(item.get("branch") or ""),
                    tr(DEPLOYMENT_STATUS_TEXT.get(str(item.get("status") or ""), "未知")),
                    f"#{item['build_number']}" if item.get("build_number") is not None else "",
                ]
            )
            child.setData(3, Qt.UserRole, str(item.get("build_url") or ""))
            parent_item.addChild(child)
        self.tree.expandAll()
        layout.addWidget(self.tree, 1)
        self.open_build = QPushButton(tr("打开 Jenkins 构建"))
        self.open_build.setEnabled(False)
        self.tree.currentItemChanged.connect(self._selection_changed)
        self.open_build.clicked.connect(self._open_selected_build)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText(tr("关闭"))
        buttons.rejected.connect(self.reject)
        action_row = QHBoxLayout()
        action_row.addWidget(self.open_build)
        action_row.addStretch()
        action_row.addWidget(buttons)
        layout.addLayout(action_row)
        translate_widget_tree(self)

    def _selection_changed(self, current, _previous) -> None:
        self.open_build.setEnabled(
            current is not None and bool(current.data(3, Qt.UserRole))
        )

    def _open_selected_build(self) -> None:
        current = self.tree.currentItem()
        target = str(current.data(3, Qt.UserRole) or "") if current else ""
        if target:
            open_target(target)


class DeploymentLogsDialog(QDialog):
    """持续显示任务内各 Jenkins 子任务的持久化编排日志。"""

    def __init__(self, service, task_id: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.task_id = str(task_id or "")
        self.setWindowTitle(tr("部署执行日志 · {value}", value=self.task_id))
        self.setMinimumSize(760, 500)
        layout = QVBoxLayout(self)
        heading = QLabel(tr("部署执行日志"))
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)
        self.summary = status_label()
        layout.addWidget(self.summary)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setPlaceholderText(tr("任务开始后，部署日志会显示在这里。"))
        layout.addWidget(self.logs, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText(tr("关闭"))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.timer = QTimer(self)
        self.timer.setInterval(750)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self) -> None:
        task = self.service.deployment_task(self.task_id)
        if not isinstance(task, dict):
            return
        state = tr(
            DEPLOYMENT_STATUS_TEXT.get(str(task.get("status") or ""), "未知")
        )
        run_count = len(task.get("execution_runs") or [])
        summary_key = (
            "状态：{status} · 进度：{current}/{total} · 已执行 {count} 次"
            if run_count
            else "状态：{status} · 进度：{current}/{total}"
        )
        self.summary.setText(
            tr(
                summary_key,
                status=state,
                current=int(task.get("current_step") or 0),
                total=int(task.get("total_steps") or len(task.get("items") or [])),
                count=run_count,
            )
        )
        lines = self._log_lines(task)
        text = "\n".join(lines)
        if self.logs.toPlainText() == text:
            return
        scrollbar = self.logs.verticalScrollBar()
        follow_tail = scrollbar.value() >= scrollbar.maximum() - 4
        self.logs.setPlainText(text)
        if follow_tail:
            scrollbar.setValue(scrollbar.maximum())

    @staticmethod
    def _log_lines(task: dict) -> list[str]:
        lines: list[str] = []
        entries = task.get("log_entries") or []
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                timestamp = str(entry.get("timestamp") or "")
                message = str(entry.get("message") or "").strip()
                if not message:
                    continue
                try:
                    stamp = datetime.fromisoformat(timestamp).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                except ValueError:
                    stamp = timestamp
                lines.append(f"[{stamp}] {message}" if stamp else message)
        if not lines:
            lines = [str(item) for item in task.get("logs") or [] if str(item)]
        if lines:
            return lines

        # 兼容旧任务：即使没有日志字段，也展示已经持久化的 Jenkins 关联摘要。
        for index, item in enumerate(task.get("items") or [], start=1):
            subtask_id = str(
                item.get("subtask_id")
                or f"{task.get('task_id')}-{index:03d}"
            )
            details = [
                f"子任务 {subtask_id}",
                str(item.get("project_full_name") or ""),
                str(item.get("environment") or ""),
                str(item.get("branch") or ""),
                tr(
                    DEPLOYMENT_STATUS_TEXT.get(
                        str(item.get("status") or ""),
                        "未知",
                    )
                ),
            ]
            if item.get("queue_id") is not None:
                details.append(f"Queue #{item['queue_id']}")
            if item.get("build_number") is not None:
                details.append(f"Build #{item['build_number']}")
            lines.append(" · ".join(value for value in details if value))
        return lines

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.timer.stop()
        super().closeEvent(event)


class DeploymentRecyclePage(BasePage):
    """只显示软删除的迭代部署任务。"""

    back_requested = Signal()

    def __init__(self, service):
        super().__init__(
            "任务回收站",
            "删除只会移动部署任务记录；恢复后环境、项目、分支和构建关联保持不变。",
        )
        self.service = service
        self.back_button = QPushButton("← 返回")
        self.back_button.setObjectName("backButton")
        self.back_button.clicked.connect(self.back_requested.emit)
        self.content.insertWidget(0, self.back_button, 0, Qt.AlignLeft)
        self.table = DeploymentTaskTable(
            recycle_mode=True,
            on_details=self.open_details,
            on_logs=self.open_logs,
            on_restore=self.restore,
        )
        self.content.addWidget(self.table, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(900)
        self.timer.timeout.connect(self.refresh)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self.timer.start()
        self.refresh()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self) -> None:
        try:
            self.table.set_tasks(
                self.service.list_deployment_tasks(trashed=True)
            )
        except Exception:
            return

    def restore(self, task_id: str) -> None:
        try:
            self.service.restore_deployment_task(task_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.refresh()

    def open_details(self, task: dict) -> None:
        latest = self.service.deployment_task(str(task.get("task_id") or ""))
        dialog = DeploymentDetailsDialog(latest or task, self)
        dialog.exec()

    def open_logs(self, task: dict) -> None:
        """回收站任务仍保留完整执行日志，进度计数应可直接查看。"""

        dialog = DeploymentLogsDialog(
            self.service,
            str(task.get("task_id") or ""),
            self,
        )
        dialog.exec()
