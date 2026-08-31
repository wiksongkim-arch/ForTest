"""ForTest 多任务生成主页。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
)

from windows_native.ui.common import (
    BasePage,
    card,
    confirm_action,
    open_target,
    status_label,
)
from windows_native.ui.task_widgets import (
    NewTaskDialog,
    RecoveryDialog,
    TaskDetailsDialog,
    TaskLogsDialog,
    TaskTable,
)
from windows_native.i18n import tr

if TYPE_CHECKING:
    from windows_native.native_service import NativeService


class HomePage(BasePage):
    """展示持久化任务队列并提供任务入口。"""

    modify_config_requested = Signal()
    recycle_requested = Signal()

    def __init__(self, service: NativeService, task_manager):
        super().__init__(
            tr("测试用例生成"),
        )
        self.service = service
        self.task_manager = task_manager
        self.output_folder_url = ""
        self.local_output_dir = ""
        self._document_loading = False
        self._dialogs: set[QDialog] = set()

        output_card, output_layout = card("钉钉输出目录")
        heading = output_layout.takeAt(0).widget()
        output_heading = QHBoxLayout()
        if heading is not None:
            output_heading.addWidget(heading)
        self.output_url = status_label("正在读取配置…")
        self.output_url.setToolTip("")
        output_heading.addWidget(self.output_url, 1)
        output_layout.insertLayout(0, output_heading)
        output_row = QHBoxLayout()
        self.open_output_button = QPushButton("打开钉钉输出目录")
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self.open_output_folder)
        self.open_local_button = QPushButton("打开本地备份目录")
        self.open_local_button.setEnabled(False)
        self.open_local_button.clicked.connect(self.open_local_folder)
        output_row.addWidget(self.open_output_button)
        output_row.addWidget(self.open_local_button)
        output_row.addStretch()
        output_layout.addLayout(output_row)
        self.content.addWidget(output_card)

        action_card, action_layout = card("任务概览")
        action_row = QHBoxLayout()
        self.active_label = QLabel("进行中：0")
        self.active_label.setObjectName("metric")
        action_row.addWidget(self.active_label)
        self.queued_label = QLabel("排队中：0")
        self.queued_label.setObjectName("metric")
        action_row.addWidget(self.queued_label)
        action_row.addStretch()
        self.new_task_button = QPushButton("新建生成任务")
        self.new_task_button.setObjectName("primary")
        self.config_button = QPushButton("修改配置")
        self.new_task_button.clicked.connect(self.create_task)
        self.config_button.clicked.connect(self.modify_config_requested.emit)
        action_row.addWidget(self.new_task_button)
        action_row.addWidget(self.config_button)
        action_layout.addLayout(action_row)
        self.content.addWidget(action_card)

        tasks_card, tasks_layout = card("生成任务")
        self.task_table = TaskTable(
            on_logs=self.open_logs,
            on_details=self.open_details,
            on_retry=self.retry_task,
            on_stop=self.stop_task,
            on_recover=self.recover_task,
            on_delete=self.delete_task,
        )
        self.task_table.setMinimumHeight(300)
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
        self.content.addWidget(tasks_card)
        self.add_stretch()

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(700)
        self.poll_timer.timeout.connect(self.refresh_tasks)
        self.poll_timer.start()
        self.refresh_tasks()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self.poll_timer.start()
        self.refresh_tasks()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.poll_timer.stop()
        super().hideEvent(event)

    def refresh(self) -> None:
        """切回主页时刷新本地任务和输出配置。"""

        self.refresh_tasks()
        if not self._document_loading:
            self._document_loading = True
            self.run_async(
                self.service.get_document,
                success=self.apply_document,
                finished=lambda: setattr(self, "_document_loading", False),
            )

    def apply_document(self, document: dict) -> None:
        """应用启动页或后台刷新得到的文档配置，不再次访问磁盘。"""

        self.output_folder_url = str(document.get("output_folder_url") or "")
        self.local_output_dir = str(document.get("local_output_dir") or "./output")
        display = self.output_folder_url or tr("尚未配置钉钉输出文件夹")
        self.output_url.setText(display)
        self.output_url.setToolTip(self.output_folder_url)
        self.open_output_button.setEnabled(bool(self.output_folder_url))
        self.open_local_button.setEnabled(bool(self.local_output_dir))

    def _apply_document(self, document: dict) -> None:
        """兼容既有页面测试和扩展调用，统一转入公开快照入口。"""

        self.apply_document(document)

    def refresh_tasks(self) -> None:
        tasks = self.task_manager.list_tasks(include_trashed=False)
        self.task_table.set_tasks(tasks)
        self.active_label.setText(
            tr("进行中：{count}", count=self.task_manager.active_count())
        )
        queued_count = getattr(self.task_manager, "queued_count", lambda: 0)
        self.queued_label.setText(
            tr("排队中：{count}", count=queued_count())
        )

    def create_task(self) -> None:
        dialog = NewTaskDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self.task_manager.create_tasks(
                dialog.document_sources(),
                source_type=dialog.document_source_type(),
            )
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.refresh_tasks()

    def retry_task(self, task_id: str) -> None:
        try:
            self.task_manager.retry(task_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.refresh_tasks()

    def stop_task(self, task_id: str) -> None:
        """二次确认后精确停止当前任务，不影响其它并行任务。"""

        confirmed = confirm_action(
            self,
            "停止任务",
            "确定强制停止当前任务吗？已生成到远端的部分内容可能保留。",
        )
        if not confirmed:
            return
        try:
            self.task_manager.stop_task(task_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.refresh_tasks()

    def recover_task(self, task_id: str) -> None:
        """打开只作用于当前任务的检测恢复对话框。"""

        try:
            task = self.task_manager.get_task(task_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        dialog = RecoveryDialog(
            task,
            self.service,
            self.task_manager,
            self,
            self,
        )
        self._track_dialog(dialog)
        dialog.show()

    def delete_task(self, task_id: str) -> None:
        confirmed = confirm_action(
            self,
            "删除任务",
            "确定将这个任务移入回收站吗？任务记录和生成结果不会被物理删除。",
        )
        if not confirmed:
            return
        try:
            self.task_manager.trash(task_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.refresh_tasks()

    def open_logs(self, task_id: str) -> None:
        dialog = TaskLogsDialog(self.task_manager, task_id, self)
        self._track_dialog(dialog)
        dialog.show()

    def open_details(self, task: dict) -> None:
        # 点击名称时重新读取快照，避免表格刷新间隔造成详情陈旧。
        latest = self.task_manager.get_task(str(task.get("task_id") or task.get("id") or ""))
        dialog = TaskDetailsDialog(latest or task, self)
        self._track_dialog(dialog)
        dialog.show()

    def _track_dialog(self, dialog: QDialog) -> None:
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        self._dialogs.add(dialog)
        dialog.destroyed.connect(lambda: self._dialogs.discard(dialog))

    def open_output_folder(self) -> None:
        if self.output_folder_url:
            open_target(self.output_folder_url)

    def open_local_folder(self) -> None:
        if not self.local_output_dir:
            return
        path = Path(self.local_output_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        open_target(str(path.resolve()))
