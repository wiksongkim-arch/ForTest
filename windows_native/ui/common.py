"""页面公共控件与布局工具。"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import (
    QPoint,
    QPointF,
    QProcess,
    QRectF,
    QSize,
    QTimer,
    Qt,
    QThreadPool,
    QUrl,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QRegion,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from windows_native.ui.workers import FunctionWorker
from windows_native.i18n import tr
from windows_native.ui.style import theme_colors


class ThemedCheckBox(QCheckBox):
    """显式绘制方框与对勾，避免系统主题组合下指示器不可见。"""

    INDICATOR_SIZE = 17
    INDICATOR_GAP = 8

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setMinimumHeight(25)
        self.setAttribute(Qt.WA_Hover, True)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        metrics = QFontMetrics(self.font())
        width = (
            4
            + self.INDICATOR_SIZE
            + self.INDICATOR_GAP
            + metrics.horizontalAdvance(self.text())
            + 2
        )
        return QSize(width, max(25, metrics.height() + 6))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return self.sizeHint()

    def hitButton(self, position: QPoint) -> bool:  # noqa: N802 - Qt API
        """让方框和整段说明文字都能切换状态。"""

        return self.rect().contains(position)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        """按当前明暗主题绘制清晰的未选、悬停、聚焦和选中状态。"""

        palette = self.palette()
        app = QApplication.instance()
        app_mode = str(app.property("themeMode") or "") if app is not None else ""
        # 透明控件自身的 Window 色可能来自旧样式，优先使用主题管理器的应用级结果。
        mode = app_mode if app_mode in {"light", "dark"} else (
            "dark"
            if palette.color(QPalette.ColorRole.Window).lightness() < 128
            else "light"
        )
        colors = theme_colors(mode)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        indicator_y = (self.height() - self.INDICATOR_SIZE) / 2
        if self.layoutDirection() == Qt.RightToLeft:
            indicator_x = self.width() - self.INDICATOR_SIZE - 2
            text_rect = QRectF(
                0,
                0,
                indicator_x - self.INDICATOR_GAP,
                self.height(),
            )
            text_alignment = Qt.AlignVCenter | Qt.AlignRight
        else:
            indicator_x = 2
            text_rect = QRectF(
                indicator_x + self.INDICATOR_SIZE + self.INDICATOR_GAP,
                0,
                self.width() - self.INDICATOR_SIZE - self.INDICATOR_GAP - 2,
                self.height(),
            )
            text_alignment = Qt.AlignVCenter | Qt.AlignLeft
        indicator = QRectF(
            indicator_x,
            indicator_y,
            self.INDICATOR_SIZE,
            self.INDICATOR_SIZE,
        )

        enabled = self.isEnabled()
        checked = self.checkState() != Qt.CheckState.Unchecked
        hovered = self.underMouse()
        focused = self.hasFocus()
        fill = QColor(
            colors.accent
            if checked
            else colors.accent_soft
            if hovered
            else colors.input
        )
        border = QColor(
            colors.accent if hovered or focused or checked else colors.border_strong
        )
        if not enabled:
            fill = QColor(colors.surface_hover)
            border = QColor(colors.disabled)

        painter.setPen(QPen(border, 1.4))
        painter.setBrush(fill)
        painter.drawRoundedRect(indicator, 4.0, 4.0)

        if checked:
            mark = QColor(colors.accent_text if enabled else colors.disabled)
            painter.setPen(
                QPen(mark, 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            )
            if self.checkState() == Qt.CheckState.PartiallyChecked:
                painter.drawLine(
                    QPoint(indicator_x + 4, round(indicator.center().y())),
                    QPoint(indicator_x + 13, round(indicator.center().y())),
                )
            else:
                # 对勾使用三点折线，缩放和高 DPI 下都比位图资源更清晰。
                check = QPainterPath(
                    QPointF(indicator_x + 4.0, indicator_y + 8.8)
                )
                check.lineTo(indicator_x + 7.2, indicator_y + 12.0)
                check.lineTo(indicator_x + 13.2, indicator_y + 5.5)
                painter.drawPath(check)

        painter.setPen(QColor(colors.text if enabled else colors.disabled))
        painter.setFont(self.font())
        painter.drawText(text_rect, text_alignment, self.text())


class SmoothComboBox(QComboBox):
    """统一的圆角下拉框：完整展示文案，并禁止滚轮误切选项。"""

    MINIMUM_CONTENT_WIDTH = 116
    MAXIMUM_CONTENT_WIDTH = 360
    MAXIMUM_POPUP_WIDTH = 560

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._automatic_minimum_width = self.MINIMUM_CONTENT_WIDTH
        self._metrics_update_pending = False
        self.setMinimumWidth(self.MINIMUM_CONTENT_WIDTH)
        self.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.setMinimumContentsLength(10)
        view = QListView(self)
        view.setObjectName("smoothComboPopup")
        view.setUniformItemSizes(True)
        view.setVerticalScrollMode(QListView.ScrollPerPixel)
        view.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setView(view)
        self.setMaxVisibleItems(8)
        model = self.model()
        model.rowsInserted.connect(self._queue_content_metrics_update)
        model.rowsRemoved.connect(self._queue_content_metrics_update)
        model.modelReset.connect(self._queue_content_metrics_update)
        model.dataChanged.connect(self._queue_content_metrics_update)
        self.currentTextChanged.connect(self._sync_current_tooltip)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API
        # 下拉框获得焦点时滚动页面，不应悄悄修改当前选项。
        event.ignore()

    def showPopup(self) -> None:  # noqa: N802 - Qt API
        self._update_content_metrics()
        super().showPopup()
        QTimer.singleShot(0, self._polish_popup)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        """在所有主题和缩放倍率下绘制一致的细线下拉箭头。"""

        super().paintEvent(event)
        app = QApplication.instance()
        mode = str(app.property("themeMode") or "light") if app else "light"
        colors = theme_colors(mode)
        color = QColor(colors.muted if self.isEnabled() else colors.disabled)
        center_x = 17 if self.layoutDirection() == Qt.RightToLeft else self.width() - 17
        center_y = self.height() / 2 + 0.5
        chevron = QPainterPath(QPointF(center_x - 4.5, center_y - 2.0))
        chevron.lineTo(center_x, center_y + 2.5)
        chevron.lineTo(center_x + 4.5, center_y - 2.0)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(color, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(chevron)

    def popup_width_hint(self) -> int:
        """返回能容纳最长选项的弹层宽度，并设置合理的桌面上限。"""

        view_width = max(0, self.view().sizeHintForColumn(0)) + 34
        text_width = self._longest_text_width() + 54
        return min(
            self.MAXIMUM_POPUP_WIDTH,
            max(self.width(), self.MINIMUM_CONTENT_WIDTH, view_width, text_width),
        )

    def _queue_content_metrics_update(self, *_args) -> None:
        if self._metrics_update_pending:
            return
        self._metrics_update_pending = True
        QTimer.singleShot(0, self._update_content_metrics)

    def _update_content_metrics(self) -> None:
        """按当前字体测量选项，布局允许时优先完整展示当前文案。"""

        self._metrics_update_pending = False
        try:
            desired = min(
                self.MAXIMUM_CONTENT_WIDTH,
                max(self.MINIMUM_CONTENT_WIDTH, self._longest_text_width() + 54),
            )
        except RuntimeError:
            # 弹窗刚关闭时排队的 0ms 布局任务可能晚于 C++ 控件销毁执行。
            return
        # 调用方显式设置的更大最小宽度优先；自动宽度仍可随语言和选项变化。
        if self.minimumWidth() <= self._automatic_minimum_width:
            self.setMinimumWidth(desired)
        self._automatic_minimum_width = desired
        for index in range(self.count()):
            if not self.itemData(index, Qt.ItemDataRole.ToolTipRole):
                self.setItemData(
                    index,
                    self.itemText(index),
                    Qt.ItemDataRole.ToolTipRole,
                )
        self._sync_current_tooltip(self.currentText())

    def _longest_text_width(self) -> int:
        metrics = QFontMetrics(self.font())
        return max(
            (metrics.horizontalAdvance(self.itemText(index)) for index in range(self.count())),
            default=0,
        )

    def _sync_current_tooltip(self, text: str) -> None:
        """极窄窗口被迫压缩时，悬停仍能查看完整选中项。"""

        self.setToolTip(str(text or ""))

    def _polish_popup(self) -> None:
        view = self.view()
        popup = view.window()
        if popup is None:
            return
        popup.setObjectName("smoothComboPopupFrame")
        popup.setAttribute(Qt.WA_TranslucentBackground, True)
        popup.setWindowFlag(Qt.FramelessWindowHint, True)
        popup.setWindowFlag(Qt.NoDropShadowWindowHint, True)
        visible_rows = max(1, min(self.count(), self.maxVisibleItems()))
        # 样式表的 item padding 不一定包含在 sizeHintForRow 中，按每行至少 40px
        # 预留并加上列表内边距，避免两项下拉框把末项裁掉。
        row_heights = [
            max(40, view.sizeHintForRow(index))
            for index in range(visible_rows)
        ]
        # 两项短列表仍预留完整边框和上下内边距，避免 Qt 误显示滚动提示箭头。
        popup_height = sum(row_heights) + 24
        view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            if self.count() <= self.maxVisibleItems()
            else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        screen = self.screen().availableGeometry()
        popup_width = min(self.popup_width_hint(), max(120, screen.width() - 8))
        anchor = self.mapToGlobal(QPoint(0, self.height() + 4))
        x = min(max(screen.left(), anchor.x()), screen.right() - popup_width + 1)
        y = anchor.y()
        if y + popup_height > screen.bottom() + 1:
            y = self.mapToGlobal(QPoint(0, -popup_height - 4)).y()
        popup.setGeometry(x, max(screen.top(), y), popup_width, popup_height)
        # Windows 原生弹出容器默认仍是方形，用圆角遮罩彻底移除外层方框。
        path = QPainterPath()
        path.addRoundedRect(0, 0, popup_width, popup_height, 10, 10)
        popup.setMask(QRegion(path.toFillPolygon().toPolygon()))
        popup.show()


class ManualSpinBox(QSpinBox):
    """只接受明确编辑操作，避免滚动页面时误改数值。"""

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API
        # 滚轮应继续交给外层页面处理；数值只能通过键盘编辑或加减按钮修改。
        event.ignore()


class _NoWheelTabBar(QTabBar):
    """禁止鼠标滚轮切换标签页。"""

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API
        event.ignore()


class SmoothTabWidget(QTabWidget):
    """使用无滚轮误触标签栏的统一标签页控件。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setTabBar(_NoWheelTabBar(self))


def clear_layout(layout) -> None:
    """递归清空动态布局。"""

    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child_layout = item.layout()
        if widget is not None:
            widget.deleteLater()
        elif child_layout is not None:
            clear_layout(child_layout)


def card(title: str) -> tuple[QFrame, QVBoxLayout]:
    """创建统一卡片容器。"""

    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 14, 18, 16)
    layout.setSpacing(10)
    heading = QLabel(title)
    heading.setObjectName("cardTitle")
    layout.addWidget(heading)
    return frame, layout


def button_row(*buttons: QPushButton) -> QWidget:
    """创建右对齐按钮栏。"""

    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addStretch()
    for button in buttons:
        layout.addWidget(button)
    return widget


def open_target(target: str) -> None:
    """使用系统默认程序打开 URL 或本地文件。"""

    if target.startswith(("https://", "http://")):
        QDesktopServices.openUrl(QUrl(target))
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))


def is_remote_target(target: str) -> bool:
    """判断远程导航目标是否为结构完整的 HTTP(S) 地址。"""

    return bool(_validated_remote_target(target))


def open_remote_target(target: str) -> bool:
    """只打开远程 HTTP(S) 地址，绝不回退到本地文件处理器。"""

    normalized = _validated_remote_target(target)
    if not normalized:
        return False
    return bool(QDesktopServices.openUrl(QUrl(normalized)))


def _validated_remote_target(target: str) -> str:
    normalized = str(target or "").strip()
    if not normalized or any(
        character == "\\"
        or ord(character) <= 0x20
        or ord(character) == 0x7F
        for character in normalized
    ):
        return ""
    try:
        parsed = urlsplit(normalized)
        _ = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return normalized


def reveal_in_file_manager(target: str | Path) -> None:
    """在系统文件管理器中显示并尽量选中指定文件。"""

    path = Path(target).expanduser().resolve()
    if not path.is_file():
        raise ValueError("要查看的文件不存在")
    if sys.platform == "win32":
        # 参数作为独立列表传给 QProcess，路径中的空格不会被再次解释。
        QProcess.startDetached("explorer.exe", [f"/select,{path}"])
        return
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


def confirm_action(parent: QWidget, title: str, message: str) -> bool:
    """显示使用当前界面语言的确认框，避免 Qt 默认 Yes/No 混入英文。"""

    dialog = QMessageBox(
        QMessageBox.Question,
        tr(title),
        tr(message),
        QMessageBox.Yes | QMessageBox.No,
        parent,
    )
    dialog.setDefaultButton(QMessageBox.No)
    dialog.button(QMessageBox.Yes).setText(tr("确定"))
    dialog.button(QMessageBox.No).setText(tr("取消"))
    return dialog.exec() == QMessageBox.Yes


class BasePage(QWidget):
    """提供滚动容器、后台任务和消息提示的页面基类。"""

    def __init__(self, title: str, description: str = ""):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        self.content = QVBoxLayout(body)
        self.content.setContentsMargins(24, 18, 28, 26)
        self.content.setSpacing(14)
        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        self.content.addWidget(title_label)
        # 没有说明文字时不创建空标签，避免页面标题下方出现无意义留白。
        if description:
            description_label = QLabel(description)
            description_label.setObjectName("pageDescription")
            description_label.setWordWrap(True)
            self.content.addWidget(description_label)
        scroll.setWidget(body)
        self.pool = QThreadPool.globalInstance()
        self._workers: set[FunctionWorker] = set()

    def run_async(
        self,
        function: Callable[[], Any],
        *,
        success: Callable[[Any], None],
        failure: Callable[[str], None] | None = None,
        finished: Callable[[], None] | None = None,
    ) -> FunctionWorker:
        """执行后台任务并持有对象直到完成。"""

        worker = FunctionWorker(function)
        self._workers.add(worker)
        worker.signals.success.connect(success)
        worker.signals.failed.connect(failure or self.show_error)

        def cleanup() -> None:
            self._workers.discard(worker)
            if finished is not None:
                finished()

        worker.signals.finished.connect(cleanup)
        self.pool.start(worker)
        return worker

    def show_error(self, message: str) -> None:
        QMessageBox.critical(self, tr("操作失败"), message)

    def show_info(self, message: str) -> None:
        QMessageBox.information(self, tr("提示"), message)

    def add_stretch(self) -> None:
        self.content.addStretch(1)


def status_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("muted")
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label
