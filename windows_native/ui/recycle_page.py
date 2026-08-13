"""任务回收站页面。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QPushButton

from windows_native.ui.common import BasePage
from windows_native.ui.task_widgets import TaskDetailsDialog, TaskLogsDialog, TaskTable


class RecyclePage(BasePage):
    """展示软删除任务，并允许完整恢复。"""

    back_requested = Signal()

    def __init__(self, task_manager):
        super().__init__("任务回收站", "删除只会移动任务记录；恢复后进度、日志和结果保持不变。")
        self.task_manager = task_manager
        self.back_button = QPushButton("← 返回")
        self.back_button.setObjectName("backButton")
        self.back_button.clicked.connect(self.back_requested.emit)
        self.content.insertWidget(0, self.back_button, 0, Qt.AlignLeft)
        self.table = TaskTable(
            show_recycle_actions=True,
            on_logs=self.open_logs,
            on_details=self.open_details,
            on_restore=self.restore,
        )
        self.table.setMinimumHeight(360)
        self.content.addWidget(self.table)
        self.add_stretch()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self.timer.start()
        self.refresh()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self) -> None:
        tasks = [
            task
            for task in self.task_manager.list_tasks(include_trashed=True)
            if task.get("trashed")
        ]
        self.table.set_tasks(tasks)

    def restore(self, task_id: str) -> None:
        try:
            self.task_manager.restore(task_id)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.refresh()

    def open_logs(self, task_id: str) -> None:
        dialog = TaskLogsDialog(self.task_manager, task_id, self)
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        dialog.show()

    def open_details(self, task: dict) -> None:
        dialog = TaskDetailsDialog(task, self)
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        dialog.show()
