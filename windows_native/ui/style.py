"""ForTest 的 Codex 风格明暗主题样式。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from string import Template


@dataclass(frozen=True)
class ThemeColors:
    """界面颜色令牌；控件只引用语义，不直接散落十六进制颜色。"""

    canvas: str
    sidebar: str
    surface: str
    surface_hover: str
    input: str
    border: str
    border_strong: str
    text: str
    muted: str
    disabled: str
    accent: str
    accent_hover: str
    accent_soft: str
    accent_text: str
    danger: str
    danger_soft: str
    selection: str
    scrollbar: str


LIGHT_COLORS = ThemeColors(
    canvas="#F7F7F5",
    sidebar="#EFEFED",
    surface="#FFFFFF",
    surface_hover="#F1F1EE",
    input="#FCFCFA",
    border="#E1E1DD",
    border_strong="#CBCBC5",
    text="#252522",
    muted="#74746E",
    disabled="#A7A7A1",
    accent="#586D60",
    accent_hover="#46594D",
    accent_soft="#E2E9E4",
    accent_text="#FFFFFF",
    danger="#B34848",
    danger_soft="#F8E7E5",
    selection="#C9D7CE",
    scrollbar="#C3C3BC",
)

DARK_COLORS = ThemeColors(
    canvas="#20201E",
    sidebar="#191917",
    surface="#292927",
    surface_hover="#32322F",
    input="#242422",
    border="#3D3D39",
    border_strong="#54544E",
    text="#F3F3F0",
    muted="#A7A7A0",
    disabled="#70706A",
    accent="#9CB4A4",
    accent_hover="#B0C5B7",
    accent_soft="#354139",
    accent_text="#182019",
    danger="#E08383",
    danger_soft="#4A2D2D",
    selection="#455A4D",
    scrollbar="#5A5A54",
)


def theme_colors(mode: str) -> ThemeColors:
    """返回有效主题的颜色；系统模式须先由 ThemeManager 解析。"""

    return DARK_COLORS if mode == "dark" else LIGHT_COLORS


_STYLE_TEMPLATE = Template(
    """
QWidget {
    color: $text;
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 14px;
}
QLabel, QCheckBox, QRadioButton {
    background: transparent;
}
QMainWindow, QDialog, QMessageBox, QStackedWidget,
QScrollArea, QScrollArea > QWidget > QWidget {
    background: $canvas;
}
QFrame#sidebar {
    background: $sidebar;
    border-right: 1px solid $border;
}
QLabel#brand {
    font-size: 19px;
    font-weight: 700;
    color: $text;
    padding: 5px 8px 6px 8px;
}
QLabel#pageTitle {
    font-size: 26px;
    font-weight: 700;
    color: $text;
}
QLabel#guideTitle {
    font-size: 22px;
    font-weight: 700;
    color: $text;
}
QLabel#dialogTitle {
    font-size: 20px;
    font-weight: 700;
    color: $text;
}
QLabel#fieldHint {
    color: $muted;
    font-size: 11px;
}
QLabel#securityWarning {
    color: $danger;
    font-size: 12px;
}
QLabel#refreshIndicator {
    color: $accent;
    font-size: 18px;
    font-weight: 700;
}
QLabel#pageDescription, QLabel#muted, QLabel#emptyState {
    color: $muted;
}
QLabel#metric {
    color: $text;
    font-size: 18px;
    font-weight: 650;
}
QLabel#tableElapsed {
    color: $muted;
    font-size: 11px;
}
QFrame#card {
    background: $surface;
    border: 1px solid $border;
    border-radius: 12px;
}
QLabel#cardTitle {
    font-size: 17px;
    font-weight: 650;
    color: $text;
}
QPushButton {
    min-height: 36px;
    padding: 0 15px;
    background: $surface;
    border: 1px solid $border_strong;
    border-radius: 8px;
    color: $text;
}
QPushButton:hover {
    background: $surface_hover;
    border-color: $border_strong;
}
QPushButton:pressed {
    background: $accent_soft;
}
QPushButton:disabled {
    color: $disabled;
    background: $surface_hover;
    border-color: $border;
}
QPushButton#primary {
    background: $accent;
    border-color: $accent;
    color: $accent_text;
    font-weight: 600;
}
QPushButton#primary:hover {
    background: $accent_hover;
    border-color: $accent_hover;
}
QPushButton#danger, QPushButton#dangerCompact {
    color: $danger;
    border-color: $danger;
}
QPushButton#danger:hover, QPushButton#dangerCompact:hover {
    background: $danger_soft;
}
QPushButton#compact, QPushButton#dangerCompact {
    min-height: 28px;
    padding: 0 9px;
    border-radius: 7px;
}
QPushButton#scheduleField {
    min-height: 38px;
    padding: 0 12px;
    border-radius: 8px;
    text-align: left;
}
QPushButton#iconButton, QPushButton#backButton {
    background: transparent;
}
QPushButton#nav {
    min-height: 38px;
    text-align: left;
    padding-left: 16px;
    border: none;
    background: transparent;
    color: $muted;
}
QPushButton#nav:hover {
    background: $surface_hover;
    color: $text;
}
QPushButton#nav:checked {
    background: $accent_soft;
    color: $accent;
    border-left: 3px solid $accent;
}
QPushButton#tableLink {
    min-height: 24px;
    padding: 0 4px;
    border: none;
    background: transparent;
    color: $accent;
    text-align: left;
}
QPushButton#tableLink:hover {
    color: $accent_hover;
    text-decoration: underline;
}
QPushButton#templateLink {
    min-height: 20px;
    padding: 0;
    border: none;
    background: transparent;
    color: $accent;
    font-size: 12px;
    text-align: left;
}
QPushButton#templateLink:hover, QPushButton#templateLink:focus {
    color: $accent_hover;
    text-decoration: underline;
}
QPushButton#progressLink {
    min-height: 0;
    padding: 2px 6px;
    border: 1px solid transparent;
    border-radius: 7px;
    background: transparent;
    text-align: left;
}
QPushButton#progressLink:hover, QPushButton#progressLink:focus {
    background: $surface_hover;
    border-color: $border;
}
QPushButton#progressCountLink {
    min-height: 20px;
    padding: 0 3px;
    border: none;
    border-radius: 4px;
    background: transparent;
    color: $accent;
}
QPushButton#progressCountLink:hover, QPushButton#progressCountLink:focus {
    background: $accent_soft;
    color: $accent_hover;
    text-decoration: underline;
}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox,
QDateTimeEdit, QTimeEdit {
    background: $input;
    color: $text;
    border: 1px solid $border_strong;
    border-radius: 8px;
    padding: 8px 10px;
    selection-background-color: $selection;
    selection-color: $text;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QComboBox:focus, QSpinBox:focus, QDateTimeEdit:focus, QTimeEdit:focus {
    border-color: $accent;
}
QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled,
QComboBox:disabled, QSpinBox:disabled, QDateTimeEdit:disabled, QTimeEdit:disabled {
    color: $disabled;
    background: $surface_hover;
}
QComboBox {
    min-height: 22px;
    border-radius: 8px;
    padding-left: 12px;
    /* drop-down 子控件已经占用 30px；这里只留少量呼吸空间，避免重复挤压文案。 */
    padding-right: 8px;
}
QComboBox:hover {
    background: $surface_hover;
    border-color: $accent;
}
QComboBox:on {
    background: $input;
    border-color: $accent;
}
QComboBox::drop-down {
    border: none;
    border-left: 1px solid $border;
    width: 30px;
    margin: 6px 0;
}
QComboBox::down-arrow {
    image: none;
    border: none;
    width: 0;
    height: 0;
}
QComboBox QAbstractItemView, QMenu {
    background: $surface;
    color: $text;
    border: 1px solid $border;
    selection-background-color: $accent_soft;
    selection-color: $text;
    outline: none;
    padding: 6px;
    border-radius: 10px;
}
QFrame#smoothComboPopupFrame {
    background: transparent;
    border: none;
}
QListView#smoothComboPopup {
    background: $surface;
    color: $text;
    border: 1px solid $border;
    border-radius: 10px;
    outline: none;
    padding: 5px;
}
QComboBox QAbstractItemView::item {
    min-height: 30px;
    padding: 3px 9px;
    border-radius: 7px;
}
QListView#smoothComboPopup::item:hover,
QListView#smoothComboPopup::item:selected {
    background: $accent_soft;
    color: $text;
}
QFrame#userCard, QFrame#guideRow {
    background: $surface;
    border: 1px solid $border;
    border-radius: 10px;
}
QLabel#userName {
    font-weight: 650;
}
QLabel#versionLabel {
    color: $muted;
    font-size: 11px;
}
QLabel#statusRunning {
    color: $accent;
    font-weight: 700;
}
QLabel#statusSuccess {
    color: $accent;
    font-weight: 800;
}
QLabel#statusIdle {
    color: $muted;
    font-weight: 800;
}
QLabel#statusFailed {
    color: $danger;
    font-weight: 800;
}
QToolButton#languageButton {
    min-width: 34px;
    min-height: 26px;
    padding: 0 5px;
    color: $muted;
    background: transparent;
    border: 1px solid $border;
    border-radius: 8px;
}
QPushButton#guideRequiredButton, QPushButton#guideCompleteButton {
    min-height: 24px;
    padding: 0 6px;
    border: none;
    background: transparent;
    font-size: 11px;
}
QPushButton#guideRequiredButton, QLabel#guideRequired {
    color: $danger;
    font-weight: 700;
}
QPushButton#guideCompleteButton, QLabel#guideComplete {
    color: $accent;
}
QCheckBox, QRadioButton {
    spacing: 8px;
    color: $text;
}
QProgressBar {
    min-height: 9px;
    max-height: 9px;
    border: none;
    border-radius: 4px;
    background: $border;
}
QProgressBar::chunk {
    background: $accent;
    border-radius: 4px;
}
QTabWidget::pane {
    border: none;
    background: $canvas;
}
QTabBar::tab {
    padding: 10px 16px;
    color: $muted;
    background: transparent;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:hover {
    color: $text;
}
QTabBar::tab:selected {
    color: $text;
    border-bottom-color: $accent;
}
QTableWidget, QTableView {
    background: $surface;
    alternate-background-color: $surface_hover;
    color: $text;
    border: 1px solid $border;
    border-radius: 8px;
    gridline-color: $border;
    selection-background-color: $accent_soft;
    selection-color: $text;
}
QTableWidget QWidget, QTableView QWidget {
    background: $surface;
}
QHeaderView::section {
    background: $surface_hover;
    color: $muted;
    border: none;
    border-bottom: 1px solid $border;
    padding: 9px 8px;
    font-weight: 600;
}
QTableCornerButton::section {
    background: $surface_hover;
    border: none;
    border-bottom: 1px solid $border;
}
QScrollBar:vertical {
    width: 10px;
    margin: 2px;
    background: transparent;
}
QScrollBar:horizontal {
    height: 10px;
    margin: 2px;
    background: transparent;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: $scrollbar;
    border-radius: 5px;
    min-height: 28px;
    min-width: 28px;
}
QScrollBar::add-line, QScrollBar::sub-line {
    width: 0;
    height: 0;
}
QToolTip {
    background: $surface;
    color: $text;
    border: 1px solid $border_strong;
    padding: 5px 7px;
}
"""
)


def build_app_style(mode: str) -> str:
    """按照有效明暗模式生成完整 Qt 样式表。"""

    return _STYLE_TEMPLATE.substitute(asdict(theme_colors(mode)))


# 为迁移期间仍导入旧常量的入口保留兼容值；正式入口由 ThemeManager 动态应用。
APP_STYLE = build_app_style("dark")
