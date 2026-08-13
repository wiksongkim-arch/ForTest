"""从生成主页进入的集中配置页面。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from windows_native.ui.common import (
    BasePage,
    ManualSpinBox,
    SmoothTabWidget,
    button_row,
    card,
    status_label,
)
from windows_native.desktop_preferences import MAX_TASK_PARALLELISM
from windows_native.ui.document_page import DocumentSettingsPanel
from windows_native.ui.prompt_page import PromptSettingsPanel
from windows_native.i18n import tr, translate_widget_tree

if TYPE_CHECKING:
    from windows_native.native_service import NativeService


class TaskConfigPanel(QWidget):
    """桌面端独有的任务并发设置。"""

    def __init__(self, service: NativeService, task_manager, page: BasePage):
        super().__init__()
        self.service = service
        self.task_manager = task_manager
        self.page = page
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 18, 8, 8)
        outer.setSpacing(18)

        parallel_card, parallel_layout = card("任务并行设置")
        description = status_label(
            "并行数量决定最多同时执行多少个生成任务；其余任务会安全排队。默认值为 1。"
        )
        parallel_layout.addWidget(description)
        form = QFormLayout()
        self.max_parallel = ManualSpinBox()
        self.max_parallel.setRange(1, MAX_TASK_PARALLELISM)
        self.max_parallel.setValue(1)
        self.max_parallel.setSuffix(tr(" 个任务"))
        form.addRow("并行数量", self.max_parallel)
        parallel_layout.addLayout(form)
        self.save_status = status_label()
        parallel_layout.addWidget(self.save_status)
        self.save_button = QPushButton("保存任务配置")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        parallel_layout.addWidget(button_row(self.save_button))
        outer.addWidget(parallel_card)
        outer.addStretch()
        self.load()

    def load(self) -> None:
        self.max_parallel.setValue(self.task_manager.get_max_parallel())

    def save(self) -> None:
        try:
            self.task_manager.set_max_parallel(self.max_parallel.value())
        except Exception as exc:
            self.page.show_error(str(exc))
            return
        self.save_status.setText(tr("任务配置已保存"))

    def retranslate(self) -> None:
        """更新 QSpinBox 的非文本子控件文案。"""

        self.max_parallel.setSuffix(tr(" 个任务"))

class ConfigPage(BasePage):
    """将文档、提示词和任务设置聚合为三个独立标签页。"""

    back_requested = Signal()

    def __init__(self, service: NativeService, task_manager):
        super().__init__("修改配置")
        self.back_button = QPushButton("← 返回")
        self.back_button.setObjectName("backButton")
        self.back_button.clicked.connect(self.back_requested.emit)
        self.content.insertWidget(0, self.back_button, 0, Qt.AlignLeft)
        self.tabs = SmoothTabWidget()
        self.document_page = DocumentSettingsPanel(service, self)
        self.prompt_page = PromptSettingsPanel(service, self)
        self.task_panel = TaskConfigPanel(service, task_manager, self)
        self.tabs.addTab(self.document_page, tr("文档配置"))
        self.tabs.addTab(self.prompt_page, tr("提示词配置"))
        self.tabs.addTab(self.task_panel, tr("任务配置"))
        self.content.addWidget(self.tabs, 1)

    def refresh(self) -> None:
        # 分别读取各自的数据源，不把不同标签页的草稿合并提交。
        self.document_page.refresh()
        self.prompt_page.refresh()
        self.task_panel.load()

    def retranslate(self) -> None:
        """切换语言时刷新动态标签，但不重新加载或覆盖正在编辑的配置。"""

        translate_widget_tree(self)
        self.prompt_page.retranslate()
        self.task_panel.retranslate()
