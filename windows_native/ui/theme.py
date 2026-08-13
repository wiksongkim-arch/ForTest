"""ForTest 的外观模式管理器。"""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from windows_native.desktop_preferences import DesktopPreferences, THEME_MODES
from windows_native.ui.style import ThemeColors, build_app_style, theme_colors


THEME_LABELS = {
    "system": "跟随系统",
    "light": "浅色",
    "dark": "深色",
}


class ThemeManager(QObject):
    """统一应用主题，并持久化用户选择。"""

    mode_changed = Signal(str)
    theme_changed = Signal(str)

    def __init__(
        self,
        app: QApplication,
        preferences: DesktopPreferences | None = None,
    ):
        super().__init__(app)
        self.app = app
        self.preferences = preferences or DesktopPreferences()
        self._mode = self.preferences.get_theme_mode()
        self._fallback_system_mode = (
            "dark" if self._palette_is_dark(app.palette()) else "light"
        )
        self._last_effective_mode: str | None = None
        style_hints = self.app.styleHints()
        color_scheme_changed = getattr(style_hints, "colorSchemeChanged", None)
        if color_scheme_changed is not None:
            color_scheme_changed.connect(self._system_theme_changed)

    @property
    def mode(self) -> str:
        """返回用户选择的模式，而不是解析后的系统明暗值。"""

        return self._mode

    def effective_mode(self) -> str:
        """将“跟随系统”解析为当前实际应用的浅色或深色。"""

        if self._mode in {"light", "dark"}:
            return self._mode
        scheme = self.app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return "dark"
        if scheme == Qt.ColorScheme.Light:
            return "light"
        windows_mode = self._windows_system_mode()
        return windows_mode or self._fallback_system_mode

    def apply(self) -> None:
        """立即把当前主题应用到全部窗口和后续弹窗。"""

        effective = self.effective_mode()
        colors = theme_colors(effective)
        self.app.setPalette(self._build_palette(colors))
        self.app.setStyleSheet(build_app_style(effective))
        self.app.setProperty("themeMode", effective)
        self._last_effective_mode = effective
        self.theme_changed.emit(effective)

    def set_mode(self, mode: str) -> None:
        """保存并应用 system、light 或 dark 三种模式。"""

        normalized = str(mode).strip().lower()
        if normalized not in THEME_MODES:
            raise ValueError(f"不支持的外观模式：{mode}")
        changed = normalized != self._mode
        self.preferences.set_theme_mode(normalized)
        self._mode = normalized
        self.apply()
        if changed:
            self.mode_changed.emit(normalized)

    def _system_theme_changed(self, *_args) -> None:
        """系统主题变化时，仅在跟随系统模式下重新应用。"""

        if self._mode == "system":
            self.apply()

    @staticmethod
    def _palette_is_dark(palette: QPalette) -> bool:
        color = palette.color(QPalette.ColorRole.Window)
        return color.lightness() < 128

    @staticmethod
    def _windows_system_mode() -> str | None:
        """Qt 未提供色彩方案时读取 Windows 应用主题作为后备。"""

        if sys.platform != "win32":
            return None
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if int(value) else "dark"
        except (OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _build_palette(colors: ThemeColors) -> QPalette:
        """为未被 QSS 覆盖的系统控件和弹窗补齐颜色。"""

        palette = QPalette()

        def set_color(role: QPalette.ColorRole, value: str) -> None:
            palette.setColor(role, QColor(value))

        set_color(QPalette.ColorRole.Window, colors.canvas)
        set_color(QPalette.ColorRole.WindowText, colors.text)
        set_color(QPalette.ColorRole.Base, colors.input)
        set_color(QPalette.ColorRole.AlternateBase, colors.surface_hover)
        set_color(QPalette.ColorRole.ToolTipBase, colors.surface)
        set_color(QPalette.ColorRole.ToolTipText, colors.text)
        set_color(QPalette.ColorRole.Text, colors.text)
        set_color(QPalette.ColorRole.Button, colors.surface)
        set_color(QPalette.ColorRole.ButtonText, colors.text)
        set_color(QPalette.ColorRole.BrightText, colors.accent_text)
        set_color(QPalette.ColorRole.Highlight, colors.selection)
        set_color(QPalette.ColorRole.HighlightedText, colors.text)
        set_color(QPalette.ColorRole.PlaceholderText, colors.muted)
        set_color(QPalette.ColorRole.Link, colors.accent)
        set_color(QPalette.ColorRole.LinkVisited, colors.accent_hover)
        disabled = QColor(colors.disabled)
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
            QPalette.ColorRole.PlaceholderText,
        ):
            palette.setColor(QPalette.ColorGroup.Disabled, role, disabled)
        return palette
