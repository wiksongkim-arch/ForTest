"""提示词配置页面。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from windows_native.ui.common import (
    BasePage,
    SmoothComboBox,
    SmoothTabWidget,
    button_row,
    card,
    clear_layout,
    confirm_action,
    status_label,
)
from windows_native.i18n import tr

if TYPE_CHECKING:
    from windows_native.native_service import NativeService

PROMPT_LABELS = {
    "image_understanding": "图片理解",
    "component_matching": "组件匹配",
    "case_generation_system": "用例生成 · 角色",
    "case_generation_user": "用例生成 · 执行",
}

PROMPT_MODEL_STAGES = {
    "image_understanding": "image_understanding",
    "component_matching": "component_matching",
    # 角色和执行提示词会在同一次用例生成请求中组合，因此共享一条策略。
    "case_generation_system": "case_generation",
    "case_generation_user": "case_generation",
}


class ModelPolicyEditor(QWidget):
    """步骤级模型顺序编辑器；末尾空下拉用于连续追加。"""

    policy_saved = Signal(dict)

    def __init__(self, service, stage: str, host: BasePage) -> None:
        super().__init__()
        self.service = service
        self.stage = stage
        self.host = host
        self.available: list[dict] = []
        self.selected_ids: list[str] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 10)
        selection_card, layout = card("模型选择")
        form = QFormLayout()
        self.mode = SmoothComboBox()
        self.mode.addItem(tr("按配置顺序"), "ordered")
        self.mode.addItem(tr("自定义"), "custom")
        self.mode.currentIndexChanged.connect(self._sync_mode)
        form.addRow(tr("调用方式"), self.mode)
        layout.addLayout(form)
        self.hint = status_label()
        layout.addWidget(self.hint)
        self.custom_widget = QWidget()
        self.custom_layout = QVBoxLayout(self.custom_widget)
        self.custom_layout.setContentsMargins(0, 0, 0, 0)
        self.custom_layout.setSpacing(7)
        layout.addWidget(self.custom_widget)
        self.save_button = QPushButton(tr("保存模型选择"))
        self.save_button.clicked.connect(self.save)
        layout.addWidget(button_row(self.save_button))
        self.status = status_label()
        layout.addWidget(self.status)
        outer.addWidget(selection_card)
        self._sync_mode()

    @staticmethod
    def _label(item: dict) -> str:
        return f"{item.get('name', '')}（{item.get('model', '')}）"

    def load_view(self, view: dict) -> None:
        self.available = [
            dict(item)
            for item in (view.get("available") or {}).get(self.stage, [])
        ]
        policy = (view.get("policies") or {}).get(self.stage) or {}
        mode = str(policy.get("mode") or "ordered")
        _set_combo_value(self.mode, mode)
        self.selected_ids = [
            str(item) for item in policy.get("configuration_ids") or []
        ]
        self._render_rows()
        self._sync_mode()

    def _sync_mode(self, _index: int = 0) -> None:
        custom = self.mode.currentData() == "custom"
        self.custom_widget.setVisible(custom)
        self.hint.setText(
            tr("按 AI 配置列表顺序调用，失败时自动尝试下一项。")
            if not custom
            else tr("按下列顺序调用；可继续添加、上下调整或删除。")
        )

    def _options_for_row(self, row: int) -> list[dict]:
        used_elsewhere = {
            value for index, value in enumerate(self.selected_ids) if index != row
        }
        return [
            item for item in self.available if str(item.get("id")) not in used_elsewhere
        ]

    def _render_rows(self) -> None:
        clear_layout(self.custom_layout)
        for row, configuration_id in enumerate(self.selected_ids):
            control = QWidget()
            row_layout = QHBoxLayout(control)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            combo = SmoothComboBox()
            for item in self._options_for_row(row):
                combo.addItem(self._label(item), str(item.get("id")))
            if combo.findData(configuration_id) < 0:
                combo.addItem(
                    tr("已不可用：{id}", id=configuration_id),
                    configuration_id,
                )
            _set_combo_value(combo, configuration_id)
            combo.currentIndexChanged.connect(
                lambda _value, index=row, target=combo: self._replace(
                    index,
                    str(target.currentData() or ""),
                )
            )
            up = QPushButton("↑")
            down = QPushButton("↓")
            remove = QPushButton(tr("删除"))
            up.setEnabled(row > 0)
            down.setEnabled(row < len(self.selected_ids) - 1)
            up.clicked.connect(
                lambda _checked=False, index=row: self._move(index, -1)
            )
            down.clicked.connect(
                lambda _checked=False, index=row: self._move(index, 1)
            )
            remove.clicked.connect(
                lambda _checked=False, index=row: self._remove(index)
            )
            row_layout.addWidget(QLabel(f"{row + 1}."))
            row_layout.addWidget(combo, 1)
            row_layout.addWidget(up)
            row_layout.addWidget(down)
            row_layout.addWidget(remove)
            self.custom_layout.addWidget(control)

        add_combo = SmoothComboBox()
        add_combo.addItem(tr("选择模型"), "")
        selected = set(self.selected_ids)
        for item in self.available:
            configuration_id = str(item.get("id"))
            if configuration_id not in selected:
                add_combo.addItem(self._label(item), configuration_id)
        add_combo.currentIndexChanged.connect(
            lambda _index: self._append(str(add_combo.currentData() or ""))
        )
        add_combo.setEnabled(add_combo.count() > 1)
        self.custom_layout.addWidget(add_combo)
        if not self.available:
            self.custom_layout.addWidget(
                status_label(tr("暂无已完成且适用于此步骤的 AI 配置"))
            )

    def _append(self, configuration_id: str) -> None:
        if configuration_id and configuration_id not in self.selected_ids:
            self.selected_ids.append(configuration_id)
            self._render_rows()

    def _replace(self, row: int, configuration_id: str) -> None:
        if not configuration_id or row >= len(self.selected_ids):
            return
        self.selected_ids[row] = configuration_id
        self._render_rows()

    def _move(self, row: int, offset: int) -> None:
        target = row + offset
        if target < 0 or target >= len(self.selected_ids):
            return
        self.selected_ids[row], self.selected_ids[target] = (
            self.selected_ids[target],
            self.selected_ids[row],
        )
        self._render_rows()

    def _remove(self, row: int) -> None:
        if 0 <= row < len(self.selected_ids):
            self.selected_ids.pop(row)
            self._render_rows()

    def save(self) -> None:
        mode = str(self.mode.currentData() or "ordered")
        ids = list(self.selected_ids) if mode == "custom" else []
        if mode == "custom" and not ids:
            self.status.setText(tr("自定义模式至少选择一个模型"))
            return
        self.save_button.setEnabled(False)
        self.status.setText(tr("正在保存模型选择…"))

        def success(view: dict) -> None:
            self.policy_saved.emit(view)
            self.status.setText(tr("模型选择已保存"))

        self.host.run_async(
            lambda: self.service.save_test_case_model_policy(
                self.stage,
                mode,
                ids,
            ),
            success=success,
            finished=lambda: self.save_button.setEnabled(True),
        )


def _set_combo_value(combo: SmoothComboBox, value: str) -> None:
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)


class PromptEditor(QWidget):
    """单类提示词的选项、校验和编辑器。"""

    def __init__(
        self,
        service: NativeService,
        prompt_name: str,
        model_stage: str,
        page: BasePage,
    ):
        super().__init__()
        self.service = service
        self.prompt_name = prompt_name
        # 保留 page 属性和参数名，兼容旧调用方；其职责是承载后台任务。
        self.page = page
        self.host = page
        self.saved_selected_id = "default"
        self.options: dict[str, dict] = {}
        self.creating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 18, 4, 4)
        self.model_policy = ModelPolicyEditor(service, model_stage, page)
        layout.addWidget(self.model_policy)
        select_row = QHBoxLayout()
        self.options_combo = SmoothComboBox()
        self.options_combo.currentIndexChanged.connect(self._selection_changed)
        self.new_button = QPushButton("新建")
        self.new_button.clicked.connect(self.new_option)
        select_row.addWidget(QLabel("提示词版本"))
        select_row.addWidget(self.options_combo, 1)
        select_row.addWidget(self.new_button)
        layout.addLayout(select_row)

        form = QFormLayout()
        self.name = QLineEdit()
        self.content = QTextEdit()
        self.content.setMinimumHeight(330)
        form.addRow("名称", self.name)
        form.addRow("内容", self.content)
        layout.addLayout(form)
        self.variables = status_label()
        layout.addWidget(self.variables)
        self.status = status_label()
        layout.addWidget(self.status)
        self.validate_button = QPushButton("校验")
        self.delete_button = QPushButton("删除")
        self.delete_button.setObjectName("danger")
        self.save_button = QPushButton("保存并启用")
        self.save_button.setObjectName("primary")
        self.validate_button.clicked.connect(self.validate)
        self.delete_button.clicked.connect(self.delete)
        self.save_button.clicked.connect(self.save)
        layout.addWidget(
            button_row(self.delete_button, self.validate_button, self.save_button)
        )

    def load_group(self, group: dict) -> None:
        self.saved_selected_id = str(group.get("selected_option_id") or "default")
        self.options = {
            str(item["id"]): item for item in group.get("options") or []
        }
        variables = group.get("variables") or {}
        required = "、".join(variables.get("required") or []) or "无"
        optional = "、".join(variables.get("optional") or []) or "无"
        self.variables.setText(
            tr("必需变量：{value}", value=required)
            + "\n"
            + tr("可选变量：{value}", value=optional)
        )
        self.options_combo.blockSignals(True)
        self.options_combo.clear()
        for option in self.options.values():
            suffix = tr("（当前）") if option["id"] == self.saved_selected_id else ""
            self.options_combo.addItem(f"{option['name']}{suffix}", option["id"])
        index = self.options_combo.findData(self.saved_selected_id)
        self.options_combo.setCurrentIndex(max(index, 0))
        self.options_combo.blockSignals(False)
        self.creating = False
        self._selection_changed()

    def retranslate(self) -> None:
        """只重绘版本下拉文案，不覆盖用户正在编辑的名称和提示词草稿。"""

        selected = self.options_combo.currentData()
        self.options_combo.blockSignals(True)
        for index in range(self.options_combo.count()):
            option_id = str(self.options_combo.itemData(index) or "")
            option = self.options.get(option_id) or {}
            suffix = tr("（当前）") if option_id == self.saved_selected_id else ""
            self.options_combo.setItemText(
                index,
                f"{option.get('name', option_id)}{suffix}",
            )
        selected_index = self.options_combo.findData(selected)
        if selected_index >= 0:
            self.options_combo.setCurrentIndex(selected_index)
        self.options_combo.blockSignals(False)

    def _selection_changed(self) -> None:
        option_id = self.options_combo.currentData()
        option = self.options.get(str(option_id))
        if option is None:
            return
        editable = bool(option.get("editable"))
        self.name.setText(str(option.get("name") or ""))
        self.content.setPlainText(str(option.get("content") or ""))
        self.name.setReadOnly(not editable)
        self.content.setReadOnly(not editable)
        self.save_button.setText(
            tr("启用默认提示词") if option_id == "default" else tr("保存并启用")
        )
        self.save_button.setEnabled(option_id == "default" or editable)
        self.delete_button.setEnabled(editable and option_id != self.saved_selected_id)
        self.status.setText("")
        self.creating = False

    def new_option(self) -> None:
        self.creating = True
        self.options_combo.setCurrentIndex(-1)
        self.name.setReadOnly(False)
        self.content.setReadOnly(False)
        self.name.setText(tr("新的提示词"))
        default = self.options.get(self.saved_selected_id) or self.options.get("default") or {}
        self.content.setPlainText(str(default.get("content") or ""))
        self.save_button.setText(tr("创建并启用"))
        self.save_button.setEnabled(True)
        self.delete_button.setEnabled(False)
        self.status.setText(tr("正在创建新的自定义版本"))

    def validate(self) -> None:
        # 在 GUI 线程读取文本，后台校验只接收普通字符串。
        content = self.content.toPlainText()
        self.status.setText(tr("正在校验…"))
        self.host.run_async(
            lambda: self.service.validate_prompt(self.prompt_name, content),
            success=lambda _value: self.status.setText(tr("校验通过")),
        )

    def save(self) -> None:
        # 先完整捕获控件状态，杜绝工作线程访问 Qt 控件。
        option_id = None if self.creating else self.options_combo.currentData()
        if option_id == "default":
            name = None
            content = None
        else:
            name = self.name.text().strip()
            content = self.content.toPlainText()
        option_id_value = str(option_id) if option_id is not None else None
        self.save_button.setEnabled(False)
        self.status.setText(tr("正在保存…"))

        def success(result: dict) -> None:
            self.load_group(result["group"])
            self.status.setText(tr("已保存并启用"))

        self.host.run_async(
            lambda: self.service.save_prompt(
                self.prompt_name,
                option_id_value,
                name,
                content,
            ),
            success=success,
            finished=lambda: self.save_button.setEnabled(True),
        )

    def delete(self) -> None:
        option_id = str(self.options_combo.currentData() or "")
        option = self.options.get(option_id) or {}
        confirmed = confirm_action(
            self,
            tr("删除提示词"),
            tr("确定删除“{name}”吗？", name=option.get("name", option_id)),
        )
        if not confirmed:
            return
        self.delete_button.setEnabled(False)
        self.host.run_async(
            lambda: self.service.delete_prompt(self.prompt_name, option_id),
            success=lambda group: self.load_group(group),
            finished=lambda: self.delete_button.setEnabled(True),
        )


class PromptSettingsPanel(QWidget):
    """可嵌入集中配置页的提示词主体，不创建 QScrollArea。"""

    def __init__(self, service: NativeService, host: BasePage):
        super().__init__()
        self.service = service
        self.host = host
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 18, 8, 8)
        outer.setSpacing(18)
        prompts_card, layout = card("提示词库")
        self.tabs = SmoothTabWidget()
        self.editors: dict[str, PromptEditor] = {}
        for name, label in PROMPT_LABELS.items():
            editor = PromptEditor(
                service,
                name,
                PROMPT_MODEL_STAGES[name],
                host,
            )
            self.editors[name] = editor
            editor.model_policy.policy_saved.connect(self._apply_policy_view)
            self.tabs.addTab(editor, tr(label))
        layout.addWidget(self.tabs)
        outer.addWidget(prompts_card)
        outer.addStretch()

    def refresh(self) -> None:
        self.host.run_async(self.service.get_prompts, success=self._apply_view)
        self.host.run_async(
            self.service.get_test_case_model_policies,
            success=self._apply_policy_view,
        )

    def _apply_view(self, view: dict) -> None:
        groups = view.get("groups") or {}
        for name, editor in self.editors.items():
            if name in groups:
                editor.load_group(groups[name])

    def _apply_policy_view(self, view: dict) -> None:
        for editor in self.editors.values():
            editor.model_policy.load_view(view)

    def retranslate(self) -> None:
        """更新所有动态提示词版本标签。"""

        for editor in self.editors.values():
            editor.retranslate()


class PromptPage(BasePage):
    """保留提示词独立页面包装，兼容旧导航和外部调用。"""

    def __init__(self, service: NativeService):
        super().__init__(
            "提示词配置",
            "默认提示词只读；可创建多个自定义版本，并在保存时切换当前版本。",
        )
        self.service = service
        self.panel = PromptSettingsPanel(service, self)
        # 保留旧页面公开属性，避免已有调用方感知主体已被抽取。
        self.tabs = self.panel.tabs
        self.editors = self.panel.editors
        self.content.addWidget(self.panel)
        self.add_stretch()

    def refresh(self) -> None:
        self.panel.refresh()

    def _apply_view(self, view: dict) -> None:
        self.panel._apply_view(view)
