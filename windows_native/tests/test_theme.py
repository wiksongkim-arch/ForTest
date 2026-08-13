"""跟随系统、浅色、深色三档主题测试。"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from windows_native.desktop_preferences import DesktopPreferences
from windows_native.ui.appearance_page import AppearancePage
from windows_native.ui.common import ThemedCheckBox
from windows_native.ui.style import LIGHT_COLORS, build_app_style
from windows_native.ui.theme import ThemeManager


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_theme_manager_applies_and_persists_explicit_modes(tmp_path):
    app = _application()
    preferences = DesktopPreferences(tmp_path)
    manager = ThemeManager(app, preferences)
    manager.set_mode("light")
    assert manager.mode == "light"
    assert manager.effective_mode() == "light"
    assert app.property("themeMode") == "light"
    assert (
        app.palette().color(QPalette.ColorRole.Window).name().upper()
        == LIGHT_COLORS.canvas
    )
    manager.set_mode("dark")
    assert manager.effective_mode() == "dark"
    assert DesktopPreferences(tmp_path).get_theme_mode() == "dark"


def test_appearance_page_exposes_exactly_three_modes(tmp_path):
    app = _application()
    manager = ThemeManager(app, DesktopPreferences(tmp_path))
    page = AppearancePage(manager)
    assert [page.mode_combo.itemData(index) for index in range(page.mode_combo.count())] == [
        "system",
        "light",
        "dark",
    ]
    manager.set_mode("light")
    page.refresh()
    assert page.mode_combo.currentData() == "light"


def test_stylesheet_contains_codex_style_light_palette():
    style = build_app_style("light")
    assert LIGHT_COLORS.canvas in style
    assert LIGHT_COLORS.surface in style
    assert "QTableWidget" in style
    assert "QPushButton#tableLink" in style


def test_themed_checkbox_draws_visible_box_and_whole_row_is_clickable(tmp_path):
    """未选中也必须有方框，点击文字尾部同样可以切换并显示对勾。"""

    app = _application()
    manager = ThemeManager(app, DesktopPreferences(tmp_path))
    manager.set_mode("light")
    checkbox = ThemedCheckBox("在项目选择器中显示 prod 生产环境")
    checkbox.resize(checkbox.sizeHint())
    checkbox.show()
    app.processEvents()

    unchecked = checkbox.grab().toImage()
    sample = QPoint(6, checkbox.height() // 2)
    unchecked_fill = unchecked.pixelColor(sample)
    assert unchecked_fill.name().upper() in {
        LIGHT_COLORS.input,
        LIGHT_COLORS.accent_soft,
    }

    # 点击控件文字最右侧，验证整行命中区域，而不只是左侧小方框。
    QTest.mouseClick(
        checkbox,
        Qt.MouseButton.LeftButton,
        pos=QPoint(checkbox.width() - 2, checkbox.height() // 2),
    )
    app.processEvents()
    assert checkbox.isChecked() is True
    checked = checkbox.grab().toImage()
    checked_colors = {
        checked.pixelColor(x, y).name().upper()
        for y in range(checked.height())
        for x in range(checked.width())
    }
    assert LIGHT_COLORS.accent in checked_colors
    assert checked.pixelColor(sample) != unchecked_fill
    checkbox.close()
