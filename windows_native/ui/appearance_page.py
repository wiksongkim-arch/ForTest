"""ForTest 外观选择页面。"""

from __future__ import annotations

from PySide6.QtWidgets import QFormLayout, QVBoxLayout, QWidget

from windows_native.ui.common import SmoothComboBox, card, status_label
from windows_native.i18n import tr
from windows_native.ui.theme import THEME_LABELS, ThemeManager


class AppearancePage(QWidget):
    """提供跟随系统、浅色、深色三档外观设置。"""

    def __init__(self, theme_manager: ThemeManager):
        super().__init__()
        self.theme_manager = theme_manager
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 18, 8, 8)
        appearance_card, layout = card("界面主题")
        layout.addWidget(
            status_label(tr("选择舒适的界面明暗模式；更改会立即生效。"))
        )
        form = QFormLayout()
        self.mode_combo = SmoothComboBox()
        for value, label in THEME_LABELS.items():
            self.mode_combo.addItem(tr(label), value)
        self.mode_combo.setAccessibleName("外观模式")
        form.addRow("外观模式", self.mode_combo)
        layout.addLayout(form)
        self.summary = status_label()
        layout.addWidget(self.summary)
        outer.addWidget(appearance_card)
        outer.addStretch(1)

        self.mode_combo.activated.connect(self._mode_selected)
        self.theme_manager.mode_changed.connect(self._sync_mode)
        self.theme_manager.theme_changed.connect(lambda _mode: self._update_summary())
        self.refresh()

    def refresh(self) -> None:
        """页面显示时同步外部可能变更的主题选择。"""

        self._sync_mode(self.theme_manager.mode)

    def _mode_selected(self, index: int) -> None:
        mode = str(self.mode_combo.itemData(index) or "system")
        self.theme_manager.set_mode(mode)
        self._update_summary()

    def _sync_mode(self, mode: str) -> None:
        index = self.mode_combo.findData(mode)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(max(index, 0))
        self.mode_combo.blockSignals(False)
        self._update_summary()

    def _update_summary(self) -> None:
        selected = self.theme_manager.mode
        effective = self.theme_manager.effective_mode()
        if selected == "system":
            self.summary.setText(
                tr(
                    "已跟随系统，当前实际使用{mode}模式。",
                    mode=tr(THEME_LABELS[effective]),
                )
            )
        else:
            self.summary.setText(
                tr("当前使用{mode}模式。", mode=tr(THEME_LABELS[effective]))
            )
