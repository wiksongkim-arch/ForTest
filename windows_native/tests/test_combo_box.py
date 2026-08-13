"""统一下拉组件的尺寸、完整文案与交互测试。"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication

from windows_native.ui.common import SmoothComboBox


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_combo_uses_longest_option_for_control_and_popup_width(app: QApplication):
    combo = SmoothComboBox()
    long_text = "非常长的项目选项（描述：确保下拉组件不会把关键文案裁切掉）"
    combo.addItems(["短选项", long_text])
    app.processEvents()

    assert combo.minimumWidth() > SmoothComboBox.MINIMUM_CONTENT_WIDTH
    assert combo.minimumWidth() <= SmoothComboBox.MAXIMUM_CONTENT_WIDTH
    assert combo.popup_width_hint() >= combo.minimumWidth()
    assert combo.popup_width_hint() <= SmoothComboBox.MAXIMUM_POPUP_WIDTH
    assert combo.itemData(1, Qt.ItemDataRole.ToolTipRole) == long_text


def test_combo_keeps_full_current_text_in_tooltip(app: QApplication):
    combo = SmoothComboBox()
    combo.addItems(["第一项", "第二项的完整说明文案"])
    combo.setCurrentIndex(1)
    app.processEvents()

    assert combo.toolTip() == "第二项的完整说明文案"
    assert combo.view().textElideMode() == Qt.TextElideMode.ElideNone


def test_combo_preserves_explicit_larger_minimum_width(app: QApplication):
    combo = SmoothComboBox()
    combo.setMinimumWidth(420)
    combo.addItem("短选项")
    app.processEvents()

    assert combo.minimumWidth() == 420


def test_schedule_sized_combo_has_room_for_english_interval(app: QApplication):
    combo = SmoothComboBox()
    combo.addItems(["Interval", "Time"])
    combo.setFixedWidth(152)
    combo.show()
    app.processEvents()

    # 控件需同时容纳英文文案、下拉子控件和内边距，防止再次退化为 “Interv”。
    text_width = QFontMetrics(combo.font()).horizontalAdvance("Interval")
    assert combo.width() - 48 >= text_width
    combo.close()
