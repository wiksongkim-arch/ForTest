"""用户卡片、语言入口与首次配置向导。"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from windows_native.i18n import LANGUAGES, current_language, tr, translate_widget_tree


class LanguageButton(QToolButton):
    """使用单色文字图标呈现语言菜单，避免彩色 Emoji 破坏视觉风格。"""

    def __init__(self, on_selected: Callable[[str], None], parent=None):
        super().__init__(parent)
        self.on_selected = on_selected
        self.setObjectName("languageButton")
        self.setText("文/A")
        self.setToolTip(tr("语言"))
        self.setPopupMode(QToolButton.InstantPopup)
        self.rebuild_menu()

    def rebuild_menu(self) -> None:
        menu = QMenu(self)
        for info in LANGUAGES:
            action = menu.addAction(info.label)
            action.setCheckable(True)
            action.setChecked(info.code == current_language())
            action.triggered.connect(
                lambda _checked=False, code=info.code: self.on_selected(code)
            )
        self.setMenu(menu)


class UserCard(QFrame):
    """本地免费用户卡片，为后续登录和会员状态替换预留稳定字段。"""

    def __init__(self, profile: dict[str, str], on_language, parent=None):
        super().__init__(parent)
        self.setObjectName("userCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 9, 9, 9)
        layout.setSpacing(3)
        top = QHBoxLayout()
        self.name_label = QLabel(str(profile.get("display_name") or tr("免费用户")))
        self.name_label.setObjectName("userName")
        self.language_button = LanguageButton(on_language, self)
        top.addWidget(self.name_label, 1)
        top.addWidget(self.language_button)
        layout.addLayout(top)
        self.expiry_label = QLabel(tr("会员到期：永久"))
        self.expiry_label.setObjectName("muted")
        layout.addWidget(self.expiry_label)


class ConfigurationGuideDialog(QDialog):
    """只显示必需配置项的状态与跳转按钮，不复制实际配置表单。"""

    def __init__(
        self,
        status: dict,
        *,
        on_open: Callable[[str], None],
        on_language: Callable[[str], None],
        on_dismiss: Callable[[], None],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.status_snapshot = status
        self.on_open = on_open
        self.on_dismiss = on_dismiss
        self.setWindowTitle(tr("配置向导"))
        self.setModal(True)
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel(tr("配置状态"))
        title.setObjectName("cardTitle")
        header.addWidget(title)
        header.addStretch()
        self.language_button = LanguageButton(on_language, self)
        header.addWidget(self.language_button)
        layout.addLayout(header)
        # 结构与主菜单完全一致：菜单分组下只展示其对应的配置入口。
        for section in status.get("sections") or []:
            section_frame = QFrame()
            section_frame.setObjectName("guideSection")
            section_layout = QVBoxLayout(section_frame)
            section_layout.setContentsMargins(10, 8, 10, 10)
            section_layout.setSpacing(7)

            section_header = QHBoxLayout()
            section_complete = bool(section.get("complete"))
            section_state = QLabel("✓" if section_complete else "!")
            section_state.setObjectName(
                "guideComplete" if section_complete else "guideRequired"
            )
            section_title = QLabel(tr(str(section.get("label") or "")))
            section_title.setObjectName("cardTitle")
            section_header.addWidget(section_state)
            section_header.addWidget(section_title, 1)
            section_layout.addLayout(section_header)

            items = section.get("items") or [section]
            for item in items:
                row = QFrame()
                row.setObjectName("guideRow")
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(12, 8, 8, 8)
                complete = bool(item.get("complete"))
                state = QLabel("✓" if complete else "!")
                state.setObjectName(
                    "guideComplete" if complete else "guideRequired"
                )

                description = QWidget()
                description_layout = QVBoxLayout(description)
                description_layout.setContentsMargins(0, 0, 0, 0)
                description_layout.setSpacing(2)
                label = QLabel(tr(str(item.get("label") or "")))
                description_layout.addWidget(label)
                checks = list(item.get("checks") or [])
                if checks:
                    # 可选在线集成不应让已经可用的本地生成链路显示为缺失。
                    required_checks = [
                        check for check in checks if not check.get("optional")
                    ]
                    completed = sum(
                        1
                        for check in required_checks
                        if bool(check.get("complete"))
                    )
                    missing = [
                        tr(str(check.get("label") or ""))
                        for check in required_checks
                        if not bool(check.get("complete"))
                    ]
                    detail = QLabel(
                        tr(
                            "已完成 {complete}/{total} 项",
                            complete=completed,
                            total=len(required_checks),
                        )
                        if not missing
                        else tr("缺少：{items}", items="、".join(missing))
                    )
                    detail.setObjectName("muted")
                    detail.setWordWrap(True)
                    description_layout.addWidget(detail)

                button = QPushButton(
                    tr("已完成") if complete else tr("前往配置")
                )
                button.setObjectName("compact")
                button.setEnabled(not complete)
                button.clicked.connect(
                    lambda _checked=False, target=str(item.get("id") or ""):
                    self._open(target)
                )
                row_layout.addWidget(state)
                row_layout.addWidget(description, 1)
                row_layout.addWidget(button)
                section_layout.addWidget(row)
            layout.addWidget(section_frame)
        buttons = QDialogButtonBox()
        dismiss = buttons.addButton(tr("不再提示"), QDialogButtonBox.DestructiveRole)
        cancel = buttons.addButton(tr("取消"), QDialogButtonBox.RejectRole)
        dismiss.clicked.connect(self._dismiss)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)
        translate_widget_tree(self)

    def _open(self, target: str) -> None:
        self.on_open(target)
        self.accept()

    def _dismiss(self) -> None:
        self.on_dismiss()
        self.reject()
