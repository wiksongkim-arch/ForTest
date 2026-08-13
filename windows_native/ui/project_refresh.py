"""项目刷新状态的统一文案与沙漏动画。"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPointF, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from windows_native.i18n import tr


def format_project_refresh_time(value: str) -> str:
    """把缓存中的 ISO 时间统一显示为十四位本地时间。"""

    normalized = str(value or "").strip()
    if not normalized:
        return ""
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return normalized
    # The service timestamp already carries the timezone that defines the
    # displayed wall-clock time.  Converting it to the runner's timezone makes
    # the same cached value render differently on UTC CI hosts.
    return parsed.strftime("%Y%m%d%H%M%S")


class AnimatedRefreshIcon(QLabel):
    """在 Qt 主线程绘制固定居中、沙粒流动的轻量沙漏。"""

    FRAME_COUNT = 30

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("refreshIndicator")
        self.setFixedSize(22, 22)
        self.setAlignment(Qt.AlignCenter)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._frame = 0
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self._advance)
        self.hide()

    @property
    def frame(self) -> int:
        """暴露当前动画帧，供状态测试确认动画确实在推进。"""

        return self._frame

    @property
    def progress(self) -> float:
        """返回当前沙粒流动进度，范围为 0 到 1。"""

        return self._frame / (self.FRAME_COUNT - 1)

    def start(self) -> None:
        self._frame = 0
        self.show()
        self.update()
        self.timer.start()

    def stop(self) -> None:
        self.timer.stop()
        self.hide()

    def _advance(self) -> None:
        self._frame = (self._frame + 1) % self.FRAME_COUNT
        self.update()

    def paintEvent(self, _event) -> None:
        """以 22×22 逻辑坐标绘制，缩放后仍严格围绕控件中心对齐。"""

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        extent = min(self.width(), self.height())
        scale = extent / 22.0
        # 先把逻辑画布移到控件正中心，避免任何图片留白造成视觉偏心。
        painter.translate((self.width() - 22 * scale) / 2, (self.height() - 22 * scale) / 2)
        painter.scale(scale, scale)

        accent = self.palette().color(QPalette.ColorRole.WindowText)
        if not accent.isValid():
            accent = QColor("#586D60")
        frame_color = QColor(accent)
        frame_color.setAlpha(210)
        sand_color = QColor(accent)
        sand_color.setAlpha(245)

        painter.setBrush(Qt.NoBrush)
        painter.setPen(
            QPen(
                frame_color,
                1.45,
                Qt.SolidLine,
                Qt.RoundCap,
                Qt.RoundJoin,
            )
        )

        # 玻璃外框完全固定；动画只改变内部沙粒，不再旋转整张系统位图。
        glass = QPainterPath(QPointF(5.0, 4.2))
        glass.lineTo(17.0, 4.2)
        glass.cubicTo(16.8, 7.2, 14.5, 9.2, 12.0, 11.0)
        glass.cubicTo(14.5, 12.8, 16.8, 14.8, 17.0, 17.8)
        glass.lineTo(5.0, 17.8)
        glass.cubicTo(5.2, 14.8, 7.5, 12.8, 10.0, 11.0)
        glass.cubicTo(7.5, 9.2, 5.2, 7.2, 5.0, 4.2)
        painter.drawPath(glass)
        painter.drawLine(QPointF(4.0, 3.2), QPointF(18.0, 3.2))
        painter.drawLine(QPointF(4.0, 18.8), QPointF(18.0, 18.8))

        progress = self.progress
        painter.setPen(Qt.NoPen)
        painter.setBrush(sand_color)

        # 上半部沙面逐步下降并收窄，形成自然流失效果。
        upper_y = 5.5 + 4.35 * progress
        upper_half_width = max(0.3, 5.0 * (1.0 - progress))
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(11.0 - upper_half_width, upper_y),
                    QPointF(11.0 + upper_half_width, upper_y),
                    QPointF(11.0, 10.35),
                ]
            )
        )

        # 下半部从底部向上堆积，轮廓始终受沙漏玻璃边界约束。
        lower_y = 16.65 - 4.35 * progress
        lower_half_width = max(0.25, 5.0 * (1.0 - progress))
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(6.0, 16.65),
                    QPointF(16.0, 16.65),
                    QPointF(11.0 + lower_half_width, lower_y),
                    QPointF(11.0 - lower_half_width, lower_y),
                ]
            )
        )

        # 流沙线在首尾帧收起，循环重置时不会出现突兀的贯穿线。
        if 0.05 < progress < 0.95:
            stream_color = QColor(sand_color)
            stream_color.setAlpha(175 + (self._frame % 4) * 20)
            painter.setPen(QPen(stream_color, 1.0, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(11.0, 10.35), QPointF(11.0, lower_y))


class ProjectRefreshStatus(QWidget):
    """集中维护刷新中、未刷新和最后刷新时间三种状态。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._refreshing = False
        self._last_refreshed_at = ""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.indicator = AnimatedRefreshIcon(self)
        self.label = QLabel()
        self.label.setObjectName("muted")
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.indicator)
        layout.addWidget(self.label)
        self._render()

    @property
    def refreshing(self) -> bool:
        return self._refreshing

    def set_last_refreshed_at(self, value: str) -> None:
        self._last_refreshed_at = str(value or "")
        self._render()

    def set_refreshing(self, refreshing: bool) -> None:
        self._refreshing = bool(refreshing)
        if self._refreshing:
            self.indicator.start()
        else:
            self.indicator.stop()
        self._render()

    def _render(self) -> None:
        if self._refreshing:
            self.label.setText(tr("项目刷新中"))
            self.setToolTip(tr("项目刷新中"))
            return
        formatted = format_project_refresh_time(self._last_refreshed_at)
        if formatted:
            text = tr("项目最后刷新时间：{time}", time=formatted)
        else:
            text = tr("项目尚未刷新")
        self.label.setText(text)
        self.setToolTip(text)

    def shutdown(self) -> None:
        self.indicator.stop()
