"""生成任务列表与对话框控件。"""

from __future__ import annotations

import copy
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from windows_native.ui.common import SmoothComboBox, open_target, status_label
from windows_native.i18n import tr, translate_widget_tree


STATUS_TEXT = {
    "queued": "等待中",
    "starting": "正在启动",
    "pending": "准备中",
    "running": "生成中",
    "completed": "已完成",
    "partial_failure": "部分完成",
    "failed": "失败",
    "stopped": "已停止",
    "interrupted": "已中断",
    "not_found": "任务丢失",
}

_ACTIVE_UI_STATUSES = {"queued", "starting", "pending", "running"}
_REASONING_LABELS = {
    "none": "无",
    "low": "低",
    "medium": "中",
    "high": "高",
    "xhigh": "极高",
    "max": "最大",
    "ultra": "超强",
}
_SPEED_LABELS = {"standard": "标准", "fast": "快速"}
_PARTIAL_IMAGE_LOG = re.compile(
    r"^区块图片部分下载失败（成功 (?P<downloaded>\d+)/(?P<total>\d+)），"
    r"继续处理可用图片与文本需求$"
)


def localized_model_value(kind: str, value: object) -> str:
    """把持久化枚举翻译为与 AI 配置页完全一致的显示文案。"""

    normalized = str(value or "").strip()
    catalog = _REASONING_LABELS if kind == "reasoning" else _SPEED_LABELS
    return tr(catalog.get(normalized, normalized or "不适用"))


def localized_log_message(message: object) -> str:
    """显示时翻译模型日志值，不修改历史任务的原始持久化数据。"""

    text = str(message or "")
    if text.startswith("推理强度："):
        return tr(
            "推理强度：{value}",
            value=localized_model_value("reasoning", text.split("：", 1)[1]),
        )
    if text.startswith("推理速度："):
        return tr(
            "推理速度：{value}",
            value=localized_model_value("speed", text.split("：", 1)[1]),
        )
    if text == "区块图片下载失败，继续处理文本需求":
        return tr(text)
    partial_image = _PARTIAL_IMAGE_LOG.fullmatch(text)
    if partial_image:
        return tr(
            "区块图片部分下载失败（成功 {downloaded}/{total}），"
            "继续处理可用图片与文本需求",
            downloaded=partial_image.group("downloaded"),
            total=partial_image.group("total"),
        )
    return text


def task_identifier(task: dict) -> str:
    """兼容持久化数据的任务标识字段。"""

    return str(task.get("task_id") or task.get("id") or "")


def formatted_elapsed(task: dict) -> str:
    """按任务开始与结束时间计算稳定耗时文案。"""

    started_raw = task.get("started_at")
    finished_raw = task.get("finished_at")
    try:
        started = datetime.fromisoformat(str(started_raw))
        finished = (
            datetime.fromisoformat(str(finished_raw))
            if finished_raw
            else datetime.now().astimezone()
        )
        seconds = max(0, int((finished - started).total_seconds()))
    except (TypeError, ValueError):
        seconds = 0
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return tr(
        "耗时：{hours}小时{minutes}分钟{seconds}秒",
        hours=hours,
        minutes=minutes,
        seconds=seconds,
    )


class StatusIndicator(QLabel):
    """表格中的轻量状态动画；不依赖 GIF，明暗主题下都保持清晰。"""

    _FRAMES = ("◐", "◓", "◑", "◒")

    def __init__(self, state: str, parent=None):
        super().__init__(parent)
        self.state = str(state)
        self.frame = 0
        self.setAlignment(Qt.AlignCenter)
        self.setFixedWidth(16)
        self.timer = QTimer(self)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self._advance)
        self._render()
        if self.state in {"queued", "starting", "pending", "running"}:
            self.timer.start()

    def _advance(self) -> None:
        self.frame = (self.frame + 1) % len(self._FRAMES)
        self._render()

    def _render(self) -> None:
        if self.state == "saved":
            self.setObjectName("statusIdle")
            self.setText("•")
        elif self.state in {"queued", "starting", "pending", "running"}:
            self.setObjectName("statusRunning")
            self.setText(self._FRAMES[self.frame])
        elif self.state in {"completed", "partial_failure"}:
            self.setObjectName("statusSuccess")
            self.setText("✓")
        else:
            self.setObjectName("statusFailed")
            self.setText("×")


class NewTaskDialog(QDialog):
    """收集在线地址或本地需求文件的新建任务对话框。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("新建生成测试用例任务"))
        self.setModal(True)
        # 默认展示更宽的多行链接区，同时保留小屏下缩回 840 像素的能力。
        self.setMinimumWidth(840)
        self.resize(980, 360)
        layout = QVBoxLayout(self)
        note = QLabel(
            tr("链接模式每行输入一个需求文档地址；任务会按行序创建并开始。")
        )
        note.setObjectName("pageDescription")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        source_control = QWidget()
        source_layout = QHBoxLayout(source_control)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(8)
        self.source_type = SmoothComboBox()
        self.source_type.addItem(tr("链接"), "link")
        self.source_type.addItem(tr("文件"), "file")
        self.source_type.setMinimumWidth(96)
        self.url = QPlainTextEdit()
        self.url.setPlaceholderText(
            tr("每行输入一个需求文档链接，例如：https://alidocs.dingtalk.com/...")
        )
        self.url.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.url.setMinimumHeight(132)
        self.url.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.url.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.file_control = QWidget()
        file_layout = QHBoxLayout(self.file_control)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.setSpacing(8)
        self.select_file_button = QPushButton(tr("选择文件"))
        self.file_path = QLineEdit()
        self.file_path.setReadOnly(True)
        self.file_path.setPlaceholderText(tr("尚未选择文件"))
        file_layout.addWidget(self.select_file_button)
        file_layout.addWidget(self.file_path, 1)
        source_layout.addWidget(self.source_type, 0, Qt.AlignTop)
        source_layout.addWidget(self.url, 1)
        source_layout.addWidget(self.file_control, 1)
        form.addRow(tr("需求文档"), source_control)
        layout.addLayout(form)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Cancel | QDialogButtonBox.Ok
        )
        self.create_button = self.buttons.button(QDialogButtonBox.Ok)
        self.create_button.setText("创建")
        self.create_button.setObjectName("primary")
        self.create_button.setEnabled(False)
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.url.textChanged.connect(self._update_ready)
        self.file_path.textChanged.connect(self._update_ready)
        self.source_type.currentIndexChanged.connect(self._source_type_changed)
        self.select_file_button.clicked.connect(self._select_file)
        layout.addWidget(self.buttons)
        self._source_type_changed()
        translate_widget_tree(self)

    def _source_type_changed(self, *_args) -> None:
        file_mode = self.document_source_type() == "file"
        self.url.setVisible(not file_mode)
        self.file_control.setVisible(file_mode)
        self._update_ready()

    def _update_ready(self, *_args) -> None:
        self.create_button.setEnabled(bool(self.document_sources()))

    def _select_file(self) -> None:
        selected, _chosen_filter = QFileDialog.getOpenFileName(
            self,
            tr("选择本地需求文档"),
            "",
            tr(
                "支持的需求文档 (*.md *.txt *.docx *.pdf *.xlsx);;所有文件 (*)"
            ),
        )
        if selected:
            # QFileDialog 在 Windows 通常已返回绝对路径，仍显式解析保证持久化稳定。
            self.file_path.setText(str(Path(selected).expanduser().resolve()))

    def _accept_if_ready(self) -> None:
        if self.create_button.isEnabled():
            self.accept()

    def document_url(self) -> str:
        """保留旧扩展调用，值现在可以是链接或本地绝对路径。"""

        return self.document_source()

    def document_source_type(self) -> str:
        return str(self.source_type.currentData() or "link")

    def document_source(self) -> str:
        values = self.document_sources()
        return values[0] if values else ""

    def document_sources(self) -> list[str]:
        """忽略空行并保留输入顺序；重复链接仍按用户输入创建。"""

        if self.document_source_type() == "file":
            value = self.file_path.text().strip()
            return [value] if value else []
        return [
            line.strip()
            for line in self.url.toPlainText().splitlines()
            if line.strip()
        ]


class TaskDetailsDialog(QDialog):
    """展示单个任务的结果摘要与可打开目标。"""

    def __init__(self, task: dict, parent: QWidget | None = None):
        super().__init__(parent)
        task_id = task_identifier(task)
        self.setWindowTitle(tr("任务详情 · {value}", value=task_id))
        self.setMinimumSize(620, 360)
        layout = QVBoxLayout(self)
        name = QLabel(str(task.get("name") or "正在读取需求文档名称…"))
        name.setObjectName("cardTitle")
        name.setWordWrap(True)
        layout.addWidget(name)
        result = task.get("result") or {}
        summary = status_label()
        status = tr(STATUS_TEXT.get(str(task.get("status")), str(task.get("status") or "未知")))
        count = int(result.get("test_cases_count") or 0)
        error = result.get("error") or task.get("error")
        lines = [
            tr("任务 ID：{value}", value=task_id),
            tr("状态：{value}", value=status),
            tr("测试用例：{value} 条", value=count),
        ]
        model = task.get("model_info") or {}
        if model:
            lines.extend(
                [
                    tr("模型名称：{value}", value=model.get("model_name") or tr("未知")),
                    tr("具体模型版本：{value}", value=model.get("model_version") or tr("未知")),
                    tr(
                        "推理强度：{value}",
                        value=localized_model_value(
                            "reasoning", model.get("reasoning_effort")
                        ),
                    ),
                    tr(
                        "推理速度：{value}",
                        value=localized_model_value(
                            "speed", model.get("inference_speed")
                        ),
                    ),
                    tr("运行方式：{value}", value=model.get("runtime") or tr("未知")),
                ]
            )
        lines.append(formatted_elapsed(task))
        if error:
            lines.append(tr("错误：{value}", value=error))
        summary.setText("\n".join(lines))
        layout.addWidget(summary)
        actions = QHBoxLayout()
        doc_button = QPushButton("打开钉钉结果")
        file_button = QPushButton("打开本地备份")
        doc_url = str(result.get("dingtalk_doc_url") or "")
        file_path = str(result.get("output_file_path") or "")
        doc_button.setEnabled(bool(doc_url))
        file_button.setEnabled(bool(file_path))
        doc_button.clicked.connect(lambda: open_target(doc_url))
        file_button.clicked.connect(
            lambda: open_target(str(Path(file_path).expanduser().resolve()))
        )
        actions.addWidget(doc_button)
        actions.addWidget(file_button)
        actions.addStretch()
        layout.addLayout(actions)
        close_buttons = QDialogButtonBox(QDialogButtonBox.Close)
        close_buttons.button(QDialogButtonBox.Close).setText("关闭")
        close_buttons.rejected.connect(self.reject)
        layout.addWidget(close_buttons)
        translate_widget_tree(self)


class TaskLogsDialog(QDialog):
    """持续读取持久化快照并显示执行日志。"""

    def __init__(self, manager, task_id: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.manager = manager
        self.task_id = task_id
        self.setWindowTitle(tr("执行日志 · {value}", value=task_id))
        self.setMinimumSize(760, 500)
        layout = QVBoxLayout(self)
        self.heading = QLabel(task_id)
        self.heading.setObjectName("cardTitle")
        layout.addWidget(self.heading)
        self.status = status_label()
        layout.addWidget(self.status)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setPlaceholderText("任务开始后，执行日志会显示在这里。")
        layout.addWidget(self.logs, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        translate_widget_tree(self)
        self.timer = QTimer(self)
        self.timer.setInterval(700)
        self.timer.timeout.connect(self.refresh)
        self._ellipsis_tick = 0
        self.timer.start()
        self.refresh()

    def refresh(self) -> None:
        task = self.manager.get_task(self.task_id)
        if not task:
            self.status.setText("任务记录不存在")
            self.timer.stop()
            return
        self.heading.setText(str(task.get("name") or self.task_id))
        current = int(task.get("current_block") or 0)
        total = int(task.get("total_blocks") or 0)
        state = tr(STATUS_TEXT.get(str(task.get("status")), str(task.get("status") or "未知")))
        self.status.setText(f"{state} · {current}/{total}\n{formatted_elapsed(task)}")
        lines: list[str] = []
        entries = task.get("log_entries") or []
        if entries:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                timestamp = str(entry.get("timestamp") or "")
                try:
                    stamp = datetime.fromisoformat(timestamp).strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    stamp = timestamp or "--"
                message = re.sub(
                    r"^\[\d{2}:\d{2}:\d{2}\]\s*",
                    "",
                    str(entry.get("message") or ""),
                )
                lines.append(f"[{stamp}] {localized_log_message(message)}")
        else:
            lines = [localized_log_message(item) for item in task.get("logs") or []]
        if task.get("status") in _ACTIVE_UI_STATUSES:
            self._ellipsis_tick = (self._ellipsis_tick + 1) % 4
            lines.append(tr("正在执行") + "." * (self._ellipsis_tick + 1))
        error = str(task.get("error") or "").strip()
        if task.get("status") in {"failed", "interrupted"} and error:
            if not any(error in line for line in lines):
                lines.append(f"[--] 失败：{error}")
        text = "\n".join(lines)
        if self.logs.toPlainText() != text:
            self.logs.setPlainText(text)
            self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())


class TaskTable(QWidget):
    """主页与回收站共用的任务表格。"""

    def __init__(
        self,
        *,
        show_recycle_actions: bool = False,
        on_logs: Callable[[str], None] | None = None,
        on_details: Callable[[dict], None] | None = None,
        on_retry: Callable[[str], None] | None = None,
        on_stop: Callable[[str], None] | None = None,
        on_delete: Callable[[str], None] | None = None,
        on_recover: Callable[[str], None] | None = None,
        on_restore: Callable[[str], None] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.show_recycle_actions = show_recycle_actions
        self.on_logs = on_logs
        self.on_details = on_details
        self.on_retry = on_retry
        self.on_stop = on_stop
        self.on_delete = on_delete
        self.on_recover = on_recover
        self.on_restore = on_restore
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            [tr("任务 ID"), tr("任务名称"), tr("进度"), tr("状态"), tr("操作")]
        )
        self.table.setAccessibleName("生成任务列表")
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(68)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(self.table)
        self.empty = QLabel(tr("暂无任务"))
        self.empty.setObjectName("emptyState")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setMinimumHeight(180)
        layout.addWidget(self.empty)
        self._last_tasks: list[dict] | None = None
        self._tasks_by_id: dict[str, dict] = {}
        self._elapsed_labels: dict[str, QLabel] = {}
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(1000)
        self.elapsed_timer.timeout.connect(self._update_elapsed_labels)
        self.elapsed_timer.start()

    def set_tasks(self, tasks: list[dict]) -> None:
        self._tasks_by_id = {task_identifier(task): dict(task) for task in tasks}
        # 数据未变化时保留现有 cellWidget，避免定时刷新导致键盘焦点丢失。
        if self._last_tasks == tasks:
            self._update_elapsed_labels()
            return
        self._last_tasks = copy.deepcopy(tasks)
        self._elapsed_labels.clear()
        self.table.setVisible(bool(tasks))
        self.empty.setVisible(not tasks)
        self.table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            self._fill_row(row, task)

    def _fill_row(self, row: int, task: dict) -> None:
        task_id = task_identifier(task)
        id_item = QTableWidgetItem(task_id)
        id_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, 0, id_item)

        name_button = QPushButton(str(task.get("name") or "正在读取需求文档名称…"))
        name_button.setObjectName("tableLink")
        name_button.setToolTip(str(task.get("doc_url") or ""))
        if self.on_details:
            name_button.clicked.connect(
                lambda _checked=False, value=dict(task): self.on_details(value)
            )
        self.table.setCellWidget(row, 1, name_button)

        current = int(task.get("current_block") or 0)
        total = int(task.get("total_blocks") or 0)
        percent = min(100, int(current * 100 / total)) if total else (
            100 if task.get("status") in {"completed", "partial_failure"} else 0
        )
        progress_widget = QWidget()
        progress_layout = QVBoxLayout(progress_widget)
        progress_layout.setContentsMargins(8, 3, 8, 3)
        progress_layout.setSpacing(1)
        progress_top = QHBoxLayout()
        progress_top.setSpacing(6)
        progress_top.addWidget(StatusIndicator(str(task.get("status") or "failed")))
        progress_top.addWidget(QLabel(f"{percent}%"))
        block_button = QPushButton(f"{current}/{total}")
        block_button.setObjectName("tableLink")
        block_button.setToolTip("查看执行日志")
        block_button.setAccessibleName(
            f"查看任务 {task_id} 的执行日志，当前进度 {current}/{total}"
        )
        if self.on_logs:
            block_button.clicked.connect(
                lambda _checked=False, value=task_id: self.on_logs(value)
            )
        progress_top.addWidget(block_button)
        progress_layout.addLayout(progress_top)
        elapsed = QLabel(formatted_elapsed(task))
        elapsed.setObjectName("tableElapsed")
        progress_layout.addWidget(elapsed)
        self._elapsed_labels[task_id] = elapsed
        self.table.setCellWidget(row, 2, progress_widget)

        status = tr(STATUS_TEXT.get(str(task.get("status")), str(task.get("status") or "未知")))
        status_item = QTableWidgetItem(status)
        status_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, 3, status_item)

        actions = QWidget()
        action_layout = QHBoxLayout(actions)
        action_layout.setContentsMargins(6, 0, 6, 0)
        action_layout.setSpacing(6)
        if self.show_recycle_actions:
            restore = QPushButton("恢复")
            restore.setObjectName("compact")
            if self.on_restore:
                restore.clicked.connect(
                    lambda _checked=False, value=task_id: self.on_restore(value)
                )
            action_layout.addWidget(restore)
        else:
            is_active = task.get("status") in _ACTIVE_UI_STATUSES
            if is_active:
                stop = QPushButton(tr("停止"))
                stop.setObjectName("dangerCompact")
                if self.on_stop:
                    stop.clicked.connect(
                        lambda _checked=False, value=task_id: self.on_stop(value)
                    )
                action_layout.addWidget(stop)
            else:
                retry = QPushButton(tr("重新生成"))
                retry.setObjectName("compact")
                if self.on_retry:
                    retry.clicked.connect(
                        lambda _checked=False, value=task_id: self.on_retry(value)
                    )
                action_layout.addWidget(retry)
            recover = QPushButton(tr("检测恢复"))
            recover.setObjectName("compact")
            recover.setEnabled(not is_active)
            delete = QPushButton(tr("删除"))
            delete.setObjectName("dangerCompact")
            if self.on_delete:
                delete.clicked.connect(
                    lambda _checked=False, value=task_id: self.on_delete(value)
                )
            if self.on_recover:
                recover.clicked.connect(
                    lambda _checked=False, value=task_id: self.on_recover(value)
                )
            action_layout.addWidget(recover)
            action_layout.addWidget(delete)
        self.table.setCellWidget(row, 4, actions)

    def _update_elapsed_labels(self) -> None:
        for task_id, label in list(self._elapsed_labels.items()):
            task = self._tasks_by_id.get(task_id)
            if task is not None and label is not None:
                label.setText(formatted_elapsed(task))


class RecoveryDialog(QDialog):
    """针对单条任务检测并恢复已有钉钉输出。"""

    def __init__(self, task: dict, service, manager, host, parent=None):
        super().__init__(parent)
        self.task = task
        self.service = service
        self.manager = manager
        self.host = host
        self.last_result: dict = {}
        task_id = task_identifier(task)
        self.setWindowTitle(tr("检测恢复 · {value}", value=task_id))
        self.setMinimumWidth(600)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        result = task.get("result") or {}
        self.node_id = QLineEdit(
            str(result.get("node_id") or result.get("dingtalk_doc_url") or "")
        )
        self.node_id.setPlaceholderText("钉钉文档地址或 node_id")
        self.expected_count = QLineEdit(
            str(max(1, int(result.get("test_cases_count") or 1)))
        )
        form.addRow("节点地址 / ID", self.node_id)
        form.addRow("预期用例数", self.expected_count)
        layout.addLayout(form)
        self.status = status_label("将对当前任务的远端结果重新读取、验收并恢复本地备份。")
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.open_doc = QPushButton("打开钉钉结果")
        self.open_file = QPushButton("打开本地备份")
        self.open_doc.setEnabled(False)
        self.open_file.setEnabled(False)
        self.open_doc.clicked.connect(self._open_document)
        self.open_file.clicked.connect(self._open_file)
        self.detect_button = QPushButton(tr("检测恢复"))
        self.detect_button.setObjectName("primary")
        self.detect_button.clicked.connect(self.detect)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.reject)
        actions.addWidget(self.open_doc)
        actions.addWidget(self.open_file)
        actions.addStretch()
        actions.addWidget(close_button)
        actions.addWidget(self.detect_button)
        layout.addLayout(actions)
        translate_widget_tree(self)

    def detect(self) -> None:
        node_id = self.node_id.text().strip()
        try:
            expected_count = int(self.expected_count.text().strip())
        except ValueError:
            self.host.show_error("预期用例数必须是正整数")
            return
        if not node_id or expected_count < 1:
            self.host.show_error("请输入节点地址和有效的预期用例数")
            return
        self.detect_button.setEnabled(False)
        self.status.setText("正在检测并恢复远端输出…")

        def success(snapshot: dict) -> None:
            updated = self.manager.apply_recovery(task_identifier(self.task), snapshot)
            self.last_result = updated.get("result") or {}
            status = str(updated.get("status") or "failed")
            count = int(self.last_result.get("test_cases_count") or 0)
            self.status.setText(
                f"检测恢复完成：{count} 条用例"
                if status in {"completed", "partial_failure"}
                else f"检测恢复失败：{updated.get('error') or '未知错误'}"
            )
            self.open_doc.setEnabled(bool(self.last_result.get("dingtalk_doc_url")))
            self.open_file.setEnabled(bool(self.last_result.get("output_file_path")))
            self.host.refresh_tasks()

        self.host.run_async(
            lambda: self.service.recover(node_id, expected_count),
            success=success,
            failure=lambda message: (
                self.status.setText(f"检测恢复失败：{message}"),
                self.host.show_error(message),
            ),
            finished=lambda: self.detect_button.setEnabled(True),
        )

    def _open_document(self) -> None:
        target = str(self.last_result.get("dingtalk_doc_url") or "")
        if target:
            open_target(target)

    def _open_file(self) -> None:
        target = str(self.last_result.get("output_file_path") or "")
        if target:
            open_target(str(Path(target).expanduser().resolve()))
