"""项目刷新状态文案、时间格式与动画测试。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from windows_native.i18n import current_language, set_language
from windows_native.ui.project_refresh import (
    ProjectRefreshStatus,
    format_project_refresh_time,
)


def test_refresh_time_is_fourteen_digit_local_time():
    assert format_project_refresh_time("2026-08-08T01:02:03+08:00") == (
        "20260808010203"
    )
    assert format_project_refresh_time("") == ""


def test_refresh_status_uses_exact_copy_and_real_animation():
    app = QApplication.instance() or QApplication([])
    previous_language = current_language()
    set_language("zh_CN")
    try:
        status = ProjectRefreshStatus()
        status.show()
        app.processEvents()
        assert status.label.text() == "项目尚未刷新"
        assert status.indicator.isHidden() is True

        status.set_refreshing(True)
        app.processEvents()
        assert status.label.text() == "项目刷新中"
        assert status.indicator.timer.isActive() is True
        assert status.indicator.isHidden() is False
        initial_frame = status.indicator.frame
        status.indicator._advance()
        assert status.indicator.frame != initial_frame

        # 自绘沙漏应在透明画布中央留下可见像素，且不依赖易偏心的系统图标。
        canvas = QPixmap(status.indicator.size())
        canvas.fill(Qt.transparent)
        status.indicator.render(canvas)
        image = canvas.toImage()
        visible = [
            (x, y)
            for y in range(image.height())
            for x in range(image.width())
            if image.pixelColor(x, y).alpha() > 0
        ]
        assert visible
        xs = [point[0] for point in visible]
        ys = [point[1] for point in visible]
        assert abs((min(xs) + max(xs)) / 2 - status.indicator.width() / 2) <= 1
        assert abs((min(ys) + max(ys)) / 2 - status.indicator.height() / 2) <= 1

        status.set_last_refreshed_at("2026-08-08T01:02:03+08:00")
        assert status.label.text() == "项目刷新中"
        status.set_refreshing(False)
        assert status.indicator.timer.isActive() is False
        assert status.indicator.isHidden() is True
        assert status.label.text() == "项目最后刷新时间：20260808010203"
        status.close()
    finally:
        set_language(previous_language)
