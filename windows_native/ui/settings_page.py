"""设置中心：AI 能力配置列表与其他系统设置。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from windows_native.i18n import tr
from windows_native.ui.common import (
    BasePage,
    ManualSpinBox,
    SmoothComboBox,
    SmoothTabWidget,
    ThemedCheckBox,
    card,
    status_label,
)


def _set_combo(combo: SmoothComboBox, value: str) -> None:
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)


class AIConfigurationDialog(QDialog):
    """新增和编辑共用的厂商配置弹窗。"""

    model_refresh_requested = Signal()
    cli_version_change_requested = Signal(str, str)

    def __init__(
        self,
        providers: list[dict[str, Any]],
        runtime_catalog: dict[str, Any],
        configuration: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.providers = [dict(item) for item in providers]
        self.configuration = dict(configuration or {})
        self._runtime_paths: dict[str, str] = {}
        self._custom_cli_path = ""
        self._confirmed_cli_version = ""
        self._cli_operation_busy = False
        self.setWindowTitle(
            tr("编辑 AI 配置") if configuration else tr("新增 AI 配置")
        )
        screen = self.screen() or QGuiApplication.primaryScreen()
        available_height = (
            screen.availableGeometry().height() if screen is not None else 800
        )
        self.resize(700, min(760, max(480, available_height - 72)))
        self.setMinimumSize(580, min(440, max(320, available_height - 40)))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)

        # 表单放入滚动区，底部状态与按钮固定可见，低分辨率也能完成保存。
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.form_container = QWidget()
        content = QVBoxLayout(self.form_container)
        content.setContentsMargins(4, 4, 10, 4)
        content.setSpacing(12)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.form = form

        self.provider = SmoothComboBox()
        for item in self.providers:
            self.provider.addItem(str(item["label"]), str(item["provider"]))
        self.name = QLineEdit()
        self.name.setPlaceholderText(tr("例如：日常用例主模型"))

        model_control = QWidget()
        model_layout = QHBoxLayout(model_control)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.setSpacing(8)
        self.model = SmoothComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(SmoothComboBox.NoInsert)
        if self.model.lineEdit() is not None:
            self.model.lineEdit().setPlaceholderText(
                tr("填写厂商可执行的精确模型 ID")
            )
        self.model_refresh = QPushButton(tr("刷新模型"))
        self.model_refresh.clicked.connect(self.model_refresh_requested.emit)
        model_layout.addWidget(self.model, 1)
        model_layout.addWidget(self.model_refresh)
        self.model_control = model_control

        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText(tr("留空保留已保存值"))
        self.clear_api_key = ThemedCheckBox(tr("清除已保存的 API Key"))
        self.secret_status = status_label()
        self.timeout = ManualSpinBox()
        self.timeout.setRange(30, 3600)
        self.timeout.setValue(300)
        self.timeout.setSuffix(tr(" 秒"))
        self.vision = ThemedCheckBox(tr("支持图片理解"))
        self.response_format = SmoothComboBox()
        self.response_format.addItem("JSON Schema", "json_schema")
        self.response_format.addItem("JSON Object", "json_object")

        self.cli_source = SmoothComboBox()
        self.cli_source.addItem(tr("内置 CLI"), "builtin")
        self.cli_source.addItem(tr("自定义路径"), "custom")

        version_control = QWidget()
        version_layout = QHBoxLayout(version_control)
        version_layout.setContentsMargins(0, 0, 0, 0)
        version_layout.setSpacing(8)
        self.cli_version = SmoothComboBox()
        self.cli_refresh = QPushButton(tr("刷新"))
        version_layout.addWidget(self.cli_version, 1)
        version_layout.addWidget(self.cli_refresh)

        path_control = QWidget()
        path_layout = QHBoxLayout(path_control)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(8)
        self.cli_path = QLineEdit()
        self.cli_path.setPlaceholderText(tr("选择 codex.exe"))
        self.cli_browse = QPushButton(tr("浏览"))
        self.cli_browse.clicked.connect(self._browse_cli)
        path_layout.addWidget(self.cli_path, 1)
        path_layout.addWidget(self.cli_browse)

        self.version_control = version_control
        self.path_control = path_control
        self.reasoning = SmoothComboBox()
        for label, value in (
            ("无", "none"),
            ("低", "low"),
            ("中", "medium"),
            ("高", "high"),
            ("极高", "xhigh"),
            ("最大", "max"),
            ("超强", "ultra"),
        ):
            self.reasoning.addItem(tr(label), value)
        self.speed = SmoothComboBox()
        self.speed.addItem(tr("标准"), "standard")
        self.speed.addItem(tr("快速"), "fast")
        self.max_concurrency = ManualSpinBox()
        self.max_concurrency.setRange(1, 4)

        # 行顺序与产品配置语义保持一致；切换厂商只改变可见性。
        form.addRow(tr("配置名称"), self.name)
        form.addRow(tr("厂商"), self.provider)
        form.addRow(tr("CLI 来源"), self.cli_source)
        form.addRow(tr("CLI 路径"), path_control)
        form.addRow(tr("CLI 版本"), version_control)
        form.addRow(tr("模型"), model_control)
        form.addRow(tr("推理强度"), self.reasoning)
        form.addRow(tr("推理速度"), self.speed)
        form.addRow(tr("最大并发"), self.max_concurrency)
        form.addRow(tr("API 地址"), self.base_url)
        form.addRow(tr("API Key"), self.api_key)
        form.addRow("", self.secret_status)
        form.addRow("", self.clear_api_key)
        form.addRow(tr("超时（秒）"), self.timeout)
        form.addRow("", self.vision)
        form.addRow(tr("响应格式"), self.response_format)
        content.addLayout(form)
        content.addStretch(1)
        self.scroll_area.setWidget(self.form_container)
        outer.addWidget(self.scroll_area, 1)

        self.form_status = status_label()
        outer.addWidget(self.form_status)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        self.buttons.button(QDialogButtonBox.Save).setText(tr("保存"))
        self.buttons.button(QDialogButtonBox.Cancel).setText(tr("取消"))
        self.buttons.accepted.connect(self._validate_and_accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.cli_source.currentIndexChanged.connect(self._sync_cli_source)
        self.cli_version.currentIndexChanged.connect(self._sync_builtin_path)
        # 只响应用户真实选中动作，目录回填和语言切换不能误触发版本安装。
        self.cli_version.activated.connect(self._request_cli_version_change)
        self.model.currentTextChanged.connect(self._sync_capabilities)
        self.apply_runtime_catalog(runtime_catalog)
        self._load_configuration()

    def _current_spec(self) -> dict[str, Any]:
        provider = str(self.provider.currentData() or "")
        return next(
            (item for item in self.providers if item.get("provider") == provider),
            {},
        )

    def _provider_changed(self, _index: int) -> None:
        spec = self._current_spec()
        is_codex = spec.get("provider") == "codex"
        for widget in (
            self.cli_source,
            self.path_control,
            self.version_control,
            self.max_concurrency,
        ):
            self.form.setRowVisible(widget, is_codex)
        for widget in (
            self.base_url,
            self.api_key,
            self.secret_status,
            self.clear_api_key,
            self.vision,
            self.response_format,
        ):
            self.form.setRowVisible(widget, not is_codex)
        self.model_refresh.setVisible(is_codex)
        if not self.configuration:
            self.name.setText(str(spec.get("label") or ""))
            self._set_model_text(str(spec.get("default_model") or ""))
            self.base_url.setText(str(spec.get("base_url") or ""))
            # Codex high-reasoning stages commonly exceed five minutes.  Keep
            # the established 900-second Codex default while cloud providers
            # retain their shorter generic default.
            self.timeout.setValue(900 if is_codex else 300)
            self.vision.setChecked(bool(spec.get("vision_enabled")))
            _set_combo(
                self.response_format,
                str(spec.get("response_format_mode") or "json_schema"),
            )
        self._sync_cli_source()
        self._sync_capabilities()

    def _sync_cli_source(self, _index: int = 0) -> None:
        builtin = self.cli_source.currentData() == "builtin"
        if builtin and not self.cli_path.isReadOnly():
            self._custom_cli_path = self.cli_path.text().strip()
        self.cli_version.setEnabled(builtin and not self._cli_operation_busy)
        self.cli_refresh.setEnabled(builtin and not self._cli_operation_busy)
        self.cli_path.setReadOnly(builtin)
        self.cli_browse.setVisible(not builtin)
        if builtin:
            self._sync_builtin_path()
        else:
            self.cli_path.setText(self._custom_cli_path)

    def _sync_builtin_path(self, _index: int = 0) -> None:
        if self.cli_source.currentData() != "builtin":
            return
        selection = str(self.cli_version.currentData() or "bundled")
        self.cli_path.setText(self._runtime_paths.get(selection, ""))

    def _request_cli_version_change(self, index: int) -> None:
        """把用户选择交给页面异步安装，程序化回填不会进入该路径。"""

        if self._cli_operation_busy or index < 0:
            return
        selection = str(self.cli_version.itemData(index) or "")
        previous = self._confirmed_cli_version or "bundled"
        if selection and selection != previous:
            self.cli_version_change_requested.emit(selection, previous)

    def set_cli_operation_busy(self, busy: bool) -> None:
        """CLI 操作期间锁定依赖控件，避免目录刷新、切换和模型查询并发。"""

        self._cli_operation_busy = bool(busy)
        self.provider.setEnabled(
            not self._cli_operation_busy and not self.configuration
        )
        self.cli_source.setEnabled(not self._cli_operation_busy)
        self._sync_cli_source()
        self.model_refresh.setEnabled(not self._cli_operation_busy)
        self.buttons.button(QDialogButtonBox.Save).setEnabled(
            not self._cli_operation_busy
        )
        self.buttons.button(QDialogButtonBox.Cancel).setEnabled(
            not self._cli_operation_busy
        )

    def reject(self) -> None:
        """后台 CLI 操作完成前禁止关闭弹窗，避免回调落到已销毁控件。"""

        if not self._cli_operation_busy:
            super().reject()

    def restore_cli_version(self, selection: str) -> None:
        """版本安装失败时恢复最后一次已经确认可用的选择。"""

        wanted = str(selection or self._confirmed_cli_version or "bundled")
        if self.cli_version.findData(wanted) < 0:
            self.cli_version.addItem(wanted, wanted)
        _set_combo(self.cli_version, wanted)
        self._confirmed_cli_version = wanted
        self._sync_builtin_path()

    def _set_model_text(self, value: str) -> None:
        normalized = str(value or "").strip()
        index = self.model.findData(normalized)
        if index < 0:
            index = self.model.findText(normalized)
        if index < 0 and normalized:
            self.model.addItem(normalized, normalized)
            index = self.model.count() - 1
        if index >= 0:
            self.model.setCurrentIndex(index)
        elif self.model.isEditable():
            self.model.setEditText(normalized)

    def _current_model_id(self) -> str:
        """下拉项显示友好名称，但保存和能力匹配始终使用真实模型 ID。"""

        index = self.model.currentIndex()
        current_text = self.model.currentText().strip()
        if index >= 0 and current_text == self.model.itemText(index).strip():
            return str(self.model.itemData(index) or current_text).strip()
        return current_text

    @staticmethod
    def _capability_rule(spec: dict[str, Any], model: str) -> dict[str, Any]:
        normalized = str(model or "").strip().casefold()
        for rule in spec.get("capability_rules") or []:
            prefixes = [str(item).casefold() for item in rule.get("model_prefixes") or []]
            if any(not prefix or normalized.startswith(prefix) for prefix in prefixes):
                return dict(rule)
        return {}

    @staticmethod
    def _reasoning_label(value: str) -> str:
        return tr(
            {
                "none": "无",
                "low": "低",
                "medium": "中",
                "high": "高",
                "xhigh": "极高",
                "max": "最大",
                "ultra": "超强",
            }.get(value, value)
        )

    @staticmethod
    def _speed_label(value: str) -> str:
        return tr("快速" if value == "fast" else "标准")

    def _replace_options(
        self,
        combo: SmoothComboBox,
        values: list[str],
        *,
        current: str,
        default: str,
        label,
    ) -> None:
        combo.blockSignals(True)
        combo.clear()
        for value in values:
            combo.addItem(label(value), value)
        wanted = current if current in values else default
        if wanted not in values and values:
            wanted = values[0]
        _set_combo(combo, wanted)
        combo.blockSignals(False)

    def _sync_capabilities(self, _text: str = "") -> None:
        rule = self._capability_rule(
            self._current_spec(),
            self._current_model_id(),
        )
        efforts = [str(item) for item in rule.get("reasoning_efforts") or []]
        speeds = [str(item) for item in rule.get("inference_speeds") or []]
        current_effort = str(self.reasoning.currentData() or "")
        current_speed = str(self.speed.currentData() or "")
        self._replace_options(
            self.reasoning,
            efforts,
            current=current_effort,
            default=str(rule.get("default_reasoning_effort") or "high"),
            label=self._reasoning_label,
        )
        self._replace_options(
            self.speed,
            speeds,
            current=current_speed,
            default=str(rule.get("default_inference_speed") or "standard"),
            label=self._speed_label,
        )
        self.form.setRowVisible(self.reasoning, bool(efforts))
        self.form.setRowVisible(self.speed, bool(speeds))

    def apply_runtime_catalog(
        self,
        catalog: dict[str, Any],
        *,
        preferred_selection: str | None = None,
        confirm_selection: bool = False,
    ) -> None:
        """应用目录并优先保留当前选择，避免编辑态回退到旧配置版本。"""

        selected = str(self.cli_version.currentData() or "")
        status = catalog.get("status") or {}
        runtime = status.get("runtime") or status.get("cli") or {}
        active = str(runtime.get("selection") or "")
        self.cli_version.blockSignals(True)
        try:
            self.cli_version.clear()
            self._runtime_paths = {}
            for item in catalog.get("versions") or []:
                version = str(item.get("version") or "")
                value = "bundled" if item.get("bundled") else version
                self._runtime_paths[value] = str(item.get("path") or "")
                suffix = tr("内置") if item.get("bundled") else (
                    tr("已安装") if item.get("installed") else tr("在线")
                )
                self.cli_version.addItem(f"{version} · {suffix}", value)
            wanted = str(
                preferred_selection
                or selected
                or self.configuration.get("codex_cli_version")
                or active
                or "bundled"
            )
            if self.cli_version.findData(wanted) < 0:
                self.cli_version.addItem(wanted, wanted)
            _set_combo(self.cli_version, wanted)
        finally:
            self.cli_version.blockSignals(False)
        if confirm_selection or not self._confirmed_cli_version:
            self._confirmed_cli_version = wanted
        self._sync_builtin_path()

    def apply_model_catalog(self, catalog: dict[str, Any]) -> None:
        """刷新下拉选项时保留用户当前输入，失败回调不会清空选择。"""

        current = self._current_model_id()
        items = list(catalog.get("models") or [])
        self.model.blockSignals(True)
        self.model.clear()
        for item in items:
            if isinstance(item, dict):
                model_id = str(item.get("id") or "").strip()
                label = str(item.get("label") or model_id).strip()
            else:
                model_id = str(item or "").strip()
                label = model_id
            if model_id and self.model.findData(model_id) < 0:
                self.model.addItem(label, model_id)
        if current and self.model.findData(current) < 0:
            self.model.addItem(current, current)
        self.model.blockSignals(False)
        wanted = current or str(catalog.get("default_model") or "")
        if not wanted and self.model.count():
            wanted = str(self.model.itemData(0) or self.model.itemText(0))
        self._set_model_text(wanted)
        self._sync_capabilities()

    def _load_configuration(self) -> None:
        if not self.configuration:
            self._provider_changed(self.provider.currentIndex())
            self.max_concurrency.setValue(1)
            self._sync_cli_source()
            return
        provider = str(self.configuration.get("provider") or "codex")
        _set_combo(self.provider, provider)
        self.provider.setEnabled(False)
        self.name.setText(str(self.configuration.get("name") or ""))
        self._set_model_text(str(self.configuration.get("model") or ""))
        self.base_url.setText(str(self.configuration.get("base_url") or ""))
        self.timeout.setValue(int(self.configuration.get("timeout_seconds") or 300))
        self.vision.setChecked(bool(self.configuration.get("vision_enabled")))
        _set_combo(
            self.response_format,
            str(self.configuration.get("response_format_mode") or "json_schema"),
        )
        _set_combo(
            self.cli_source,
            str(self.configuration.get("codex_cli_source") or "builtin"),
        )
        self.cli_path.setText(str(self.configuration.get("codex_cli_path") or ""))
        self._custom_cli_path = self.cli_path.text().strip()
        _set_combo(
            self.reasoning,
            str(self.configuration.get("reasoning_effort") or "high"),
        )
        _set_combo(
            self.speed,
            str(self.configuration.get("inference_speed") or "standard"),
        )
        self.max_concurrency.setValue(
            int(self.configuration.get("max_concurrency") or 1)
        )
        secret = self.configuration.get("secret_status") or {}
        self.secret_status.setText(
            tr("已保存：{value}", value=secret.get("masked_value"))
            if secret.get("configured")
            else tr("尚未配置")
        )
        self._provider_changed(self.provider.currentIndex())
        _set_combo(
            self.reasoning,
            str(self.configuration.get("reasoning_effort") or "high"),
        )
        _set_combo(
            self.speed,
            str(self.configuration.get("inference_speed") or "standard"),
        )
        self._sync_cli_source()

    def _browse_cli(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self,
            tr("选择 Codex CLI"),
            "",
            "Codex CLI (codex.exe);;Executable (*.exe);;All Files (*)",
        )
        if path:
            self._custom_cli_path = path
            self.cli_path.setText(path)

    def _validate_and_accept(self) -> None:
        if not self.name.text().strip():
            self.form_status.setText(tr("配置名称不能为空"))
            return
        if not self._current_model_id():
            self.form_status.setText(tr("模型不能为空"))
            return
        provider = str(self.provider.currentData() or "")
        if provider != "codex" and not self.base_url.text().strip():
            self.form_status.setText(tr("API 地址不能为空"))
            return
        if (
            provider == "codex"
            and self.cli_source.currentData() == "custom"
            and not self.cli_path.text().strip()
        ):
            self.form_status.setText(tr("请选择 Codex CLI 路径"))
            return
        self.accept()

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.configuration.get("id"),
            "name": self.name.text().strip(),
            "provider": str(self.provider.currentData()),
            "model": self._current_model_id(),
            "base_url": self.base_url.text().strip(),
            "timeout_seconds": self.timeout.value(),
            "vision_enabled": self.vision.isChecked(),
            "response_format_mode": str(self.response_format.currentData()),
            "reasoning_effort": str(
                self.reasoning.currentData()
                or self.configuration.get("reasoning_effort")
                or "high"
            ),
            "inference_speed": str(
                self.speed.currentData()
                or self.configuration.get("inference_speed")
                or "standard"
            ),
            "max_concurrency": self.max_concurrency.value(),
            "codex_cli_source": str(self.cli_source.currentData()),
            "codex_cli_version": str(self.cli_version.currentData() or "bundled"),
            "codex_cli_path": (
                self.cli_path.text().strip() or None
                if self.cli_source.currentData() == "custom"
                else None
            ),
            "api_key": (
                self.api_key.text().strip() or None
                if self.provider.currentData() != "codex"
                else None
            ),
            "clear_api_key": (
                self.clear_api_key.isChecked()
                if self.provider.currentData() != "codex"
                else False
            ),
        }


class AIRecycleDialog(QDialog):
    restore_requested = Signal(str)
    purge_requested = Signal(str)

    def __init__(self, items: list[dict[str, Any]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("AI 配置回收站"))
        self.resize(860, 460)
        outer = QVBoxLayout(self)
        outer.addWidget(
            status_label(
                tr("删除只会移动配置；恢复后密钥与模型策略引用保持不变。")
            )
        )
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            [tr("配置名称"), tr("模型"), tr("操作")]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # 列表不承载键盘选中操作，关闭焦点框以彻底消除单元格边界感。
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setStyleSheet(
            "QTableWidget::item { padding-left: 10px; padding-right: 10px; border: none; }"
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(60)
        self.table.verticalHeader().setMinimumSectionSize(60)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(2, 220)
        outer.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText(tr("关闭"))
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.apply_items(items)

    def apply_items(self, items: list[dict[str, Any]]) -> None:
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            self.table.setItem(row, 0, QTableWidgetItem(str(item.get("name") or "")))
            self.table.setItem(
                row,
                1,
                QTableWidgetItem(str(item.get("display_model") or "")),
            )
            actions = QWidget()
            layout = QHBoxLayout(actions)
            layout.setContentsMargins(10, 6, 10, 6)
            layout.setSpacing(8)
            restore = QPushButton(tr("恢复"))
            purge = QPushButton(tr("彻底删除"))
            restore.clicked.connect(
                lambda _checked=False, value=str(item["id"]): self.restore_requested.emit(value)
            )
            purge.clicked.connect(
                lambda _checked=False, value=str(item["id"]): self.purge_requested.emit(value)
            )
            layout.addWidget(restore)
            layout.addWidget(purge)
            self.table.setCellWidget(row, 2, actions)
            self.table.setRowHeight(row, 60)


class AIConfigurationsPanel(QWidget):
    configuration_changed = Signal()

    def __init__(self, service, page: BasePage) -> None:
        super().__init__()
        self.service = service
        self.page = page
        self.view: dict[str, Any] = {
            "providers": [],
            "configurations": [],
            "recycle_bin": [],
        }
        self._busy = False
        self._recycle_dialog: AIRecycleDialog | None = None
        self._order_buttons: list[tuple[QPushButton, bool]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 14, 8, 8)
        outer.setSpacing(12)
        toolbar = QHBoxLayout()
        self.add_button = QPushButton(tr("新增配置"))
        self.add_button.setObjectName("primary")
        self.add_button.clicked.connect(self._add)
        self.detect_all_button = QPushButton(tr("检测全部"))
        self.detect_all_button.clicked.connect(self._detect_all)
        toolbar.addWidget(self.add_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.detect_all_button)
        outer.addLayout(toolbar)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            [tr("顺序"), tr("配置名称"), tr("模型"), tr("状态"), tr("操作")]
        )
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # 顺序由行内按钮操作，表格本身无需显示单元格焦点框。
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setStyleSheet(
            "QTableWidget::item { padding-left: 10px; padding-right: 10px; border: none; }"
        )
        self.table.verticalHeader().setVisible(False)
        # 单元格包含三枚操作按钮，固定舒适行高避免高 DPI 下被裁切。
        self.table.verticalHeader().setDefaultSectionSize(60)
        self.table.verticalHeader().setMinimumSectionSize(60)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 116)
        self.table.setColumnWidth(3, 128)
        self.table.setColumnWidth(4, 244)
        outer.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.status = status_label(tr("正在读取 AI 配置…"))
        self.recycle_button = QPushButton(tr("回收站"))
        self.recycle_button.clicked.connect(self._open_recycle)
        footer.addWidget(self.status)
        footer.addStretch(1)
        footer.addWidget(self.recycle_button)
        outer.addLayout(footer)

    def refresh(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self.page.run_async(
            self.service.get_ai_configurations,
            success=self.apply_view,
            failure=lambda message: self.status.setText(
                tr("AI 配置读取失败：{message}", message=message)
            ),
            finished=lambda: self._set_busy(False),
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        for control in (
            self.add_button,
            self.detect_all_button,
            self.recycle_button,
        ):
            control.setEnabled(not busy)
        # 行内顺序按钮保留首尾边界规则，异步结束后会被准确恢复。
        for button, position_available in self._order_buttons:
            button.setEnabled(position_available and not busy)

    def apply_view(self, view: dict[str, Any]) -> None:
        self.view = deepcopy(view)
        configurations = [dict(item) for item in view.get("configurations") or []]
        self._order_buttons.clear()
        self.table.setRowCount(len(configurations))
        for row, item in enumerate(configurations):
            order = QWidget()
            order_layout = QHBoxLayout(order)
            order_layout.setContentsMargins(10, 6, 10, 6)
            order_layout.setSpacing(6)
            up = QPushButton("↑")
            down = QPushButton("↓")
            up.setObjectName("compact")
            down.setObjectName("compact")
            up_available = row > 0
            down_available = row < len(configurations) - 1
            self._order_buttons.extend(
                ((up, up_available), (down, down_available))
            )
            up.setEnabled(up_available and not self._busy)
            down.setEnabled(down_available and not self._busy)
            up.clicked.connect(
                lambda _checked=False, index=row: self._move(index, -1)
            )
            down.clicked.connect(
                lambda _checked=False, index=row: self._move(index, 1)
            )
            order_layout.addWidget(up)
            order_layout.addWidget(down)
            self.table.setCellWidget(row, 0, order)
            self.table.setItem(row, 1, QTableWidgetItem(str(item.get("name") or "")))
            # 保持列表单行简洁，鼠标悬停时仍可读取完整厂商与模型 ID。
            model_text = str(item.get("display_model") or "")
            model_item = QTableWidgetItem(model_text)
            model_item.setToolTip(model_text)
            self.table.setItem(row, 2, model_item)
            status = str(item.get("status") or "unchecked")
            if not item.get("complete"):
                status_text = tr("未完成配置")
            elif status == "passed":
                status_text = tr("检测通过")
            elif status == "error":
                status_text = tr("异常")
            else:
                status_text = tr("未检测")
            status_item = QTableWidgetItem(status_text)
            status_item.setToolTip(str(item.get("status_detail") or ""))
            # 状态值在固定宽度列中水平、垂直居中，提升逐行扫读的一致性。
            status_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 3, status_item)

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(10, 6, 10, 6)
            actions_layout.setSpacing(8)
            detect = QPushButton(tr("检测"))
            edit = QPushButton(tr("编辑"))
            delete = QPushButton(tr("删除"))
            detect.clicked.connect(
                lambda _checked=False, value=str(item["id"]): self._detect(value)
            )
            edit.clicked.connect(
                lambda _checked=False, value=dict(item): self._edit(value)
            )
            delete.clicked.connect(
                lambda _checked=False, value=str(item["id"]): self._delete(value)
            )
            actions_layout.addWidget(detect)
            actions_layout.addWidget(edit)
            actions_layout.addWidget(delete)
            self.table.setCellWidget(row, 4, actions)
            self.table.setRowHeight(row, 60)
        self.status.setText(
            tr("共 {count} 条 AI 配置", count=len(configurations))
            if configurations
            else tr("暂无 AI 配置，请点击“新增配置”")
        )
        self.recycle_button.setText(
            tr("回收站（{count}）", count=len(view.get("recycle_bin") or []))
        )
        if self._recycle_dialog is not None:
            self._recycle_dialog.apply_items(view.get("recycle_bin") or [])

    def _runtime_catalog(self) -> dict[str, Any]:
        try:
            return self.service.get_local_codex_runtime_catalog()
        except Exception:
            return {"versions": []}

    def _refresh_editor_versions(self, dialog: AIConfigurationDialog) -> None:
        dialog.set_cli_operation_busy(True)
        self.page.run_async(
            lambda: self.service.get_codex_runtime_catalog(refresh=True),
            success=dialog.apply_runtime_catalog,
            failure=lambda message: dialog.form_status.setText(
                tr("版本目录刷新失败：{message}", message=message)
            ),
            finished=lambda: dialog.set_cli_operation_busy(False),
        )

    def _switch_editor_cli_version(
        self,
        dialog: AIConfigurationDialog,
        selection: str,
        previous: str,
    ) -> None:
        """下载并切换弹窗所选版本，成功前不允许刷新模型或保存。"""

        index = dialog.cli_version.findData(selection)
        display = (
            dialog.cli_version.itemText(index).split(" · ", 1)[0]
            if index >= 0
            else selection
        )
        dialog.set_cli_operation_busy(True)
        dialog.form_status.setText(
            tr("正在下载、校验并切换 Codex CLI {version}…", version=display)
        )

        def success(catalog: dict[str, Any]) -> None:
            dialog.apply_runtime_catalog(
                catalog,
                preferred_selection=selection,
                confirm_selection=True,
            )
            dialog.form_status.setText(
                tr(
                    "Codex CLI 已切换到 {version}，后续新任务将统一使用该版本",
                    version=display,
                )
            )

        def failure(message: str) -> None:
            dialog.restore_cli_version(previous)
            dialog.form_status.setText(
                tr("Codex 版本操作失败：{message}", message=message)
            )

        self.page.run_async(
            lambda: self.service.install_codex_runtime(selection),
            success=success,
            failure=failure,
            finished=lambda: dialog.set_cli_operation_busy(False),
        )

    def _refresh_editor_models(self, dialog: AIConfigurationDialog) -> None:
        dialog.set_cli_operation_busy(True)
        dialog.form_status.setText(tr("正在刷新 Codex 模型…"))
        # Qt 控件只能在 GUI 线程读取；后台线程只接收不可变的表单快照。
        values = dialog.payload()

        def success(catalog: dict[str, Any]) -> None:
            dialog.apply_model_catalog(catalog)
            dialog.form_status.setText(
                tr(
                    "已刷新 {count} 个模型",
                    count=len(catalog.get("models") or []),
                )
            )

        self.page.run_async(
            lambda: self.service.get_codex_configuration_models(values),
            success=success,
            failure=lambda message: dialog.form_status.setText(
                tr("模型刷新失败：{message}", message=message)
            ),
            finished=lambda: dialog.set_cli_operation_busy(False),
        )

    def _open_editor(self, item: dict[str, Any] | None) -> None:
        dialog = AIConfigurationDialog(
            self.view.get("providers") or [],
            self._runtime_catalog(),
            item,
            self,
        )

        dialog.cli_refresh.clicked.connect(
            lambda: self._refresh_editor_versions(dialog)
        )
        dialog.cli_version_change_requested.connect(
            lambda selection, previous: self._switch_editor_cli_version(
                dialog,
                selection,
                previous,
            )
        )
        dialog.model_refresh_requested.connect(
            lambda: self._refresh_editor_models(dialog)
        )
        if dialog.exec() != QDialog.Accepted:
            return
        # 对话框关闭前在 GUI 线程捕获值，后台保存不得直接访问 Qt 控件。
        values = dialog.payload()
        self._set_busy(True)
        self.status.setText(tr("正在保存 AI 配置…"))
        self.page.run_async(
            lambda: self.service.save_ai_configuration(values),
            success=self._saved,
            finished=lambda: self._set_busy(False),
        )

    def _add(self) -> None:
        self._open_editor(None)

    def _edit(self, item: dict[str, Any]) -> None:
        self._open_editor(item)

    def _saved(self, view: dict[str, Any]) -> None:
        self.apply_view(view)
        self.status.setText(tr("AI 配置已保存"))
        self.configuration_changed.emit()

    def _move(self, row: int, offset: int) -> None:
        items = list(self.view.get("configurations") or [])
        target = row + offset
        if target < 0 or target >= len(items):
            return
        items[row], items[target] = items[target], items[row]
        ids = [str(item["id"]) for item in items]
        self._set_busy(True)
        self.page.run_async(
            lambda: self.service.reorder_ai_configurations(ids),
            success=self.apply_view,
            finished=lambda: self._set_busy(False),
        )

    def _detect(self, configuration_id: str) -> None:
        self._set_busy(True)
        self.status.setText(tr("正在检测 AI 配置…"))

        def success(result: dict[str, Any]) -> None:
            updated = deepcopy(self.view)
            configuration = result.get("configuration") or {}
            configuration_id = str(configuration.get("id") or "")
            updated["configurations"] = [
                dict(configuration) if str(item.get("id")) == configuration_id else item
                for item in updated.get("configurations") or []
            ]
            self.apply_view(updated)
            self.status.setText(
                tr("检测通过") if result.get("ok") else tr("检测异常")
            )
            # 配置向导以“检测通过”为完成条件，检测结果变化后立即同步徽标。
            self.configuration_changed.emit()

        self.page.run_async(
            lambda: self.service.test_ai_configuration(configuration_id),
            success=success,
            finished=lambda: self._set_busy(False),
        )

    def _detect_all(self) -> None:
        self._set_busy(True)
        self.status.setText(tr("正在按顺序检测全部配置…"))
        self.page.run_async(
            self.service.test_all_ai_configurations,
            success=self._detect_all_finished,
            finished=lambda: self._set_busy(False),
        )

    def _detect_all_finished(self, view: dict[str, Any]) -> None:
        self.apply_view(view)
        passed = sum(1 for item in view.get("results") or [] if item.get("ok"))
        self.status.setText(
            tr(
                "全部检测完成：{passed}/{total} 通过",
                passed=passed,
                total=len(view.get("results") or []),
            )
        )
        self.configuration_changed.emit()

    def _delete(self, configuration_id: str) -> None:
        answer = QMessageBox.question(
            self,
            tr("删除 AI 配置"),
            tr("确定将这条 AI 配置移入回收站吗？密钥不会被物理删除。"),
        )
        if answer != QMessageBox.Yes:
            return
        self._set_busy(True)
        self.page.run_async(
            lambda: self.service.delete_ai_configuration(configuration_id),
            success=self._saved,
            finished=lambda: self._set_busy(False),
        )

    def _open_recycle(self) -> None:
        dialog = AIRecycleDialog(self.view.get("recycle_bin") or [], self)
        self._recycle_dialog = dialog
        dialog.restore_requested.connect(self._restore)
        dialog.purge_requested.connect(self._purge)
        dialog.finished.connect(
            lambda _result: setattr(self, "_recycle_dialog", None)
        )
        dialog.open()

    def _restore(self, configuration_id: str) -> None:
        self.page.run_async(
            lambda: self.service.restore_ai_configuration(configuration_id),
            success=self._saved,
        )

    def _purge(self, configuration_id: str) -> None:
        answer = QMessageBox.question(
            self,
            tr("彻底删除 AI 配置"),
            tr("彻底删除后配置及已保存密钥无法恢复，是否继续？"),
        )
        if answer != QMessageBox.Yes:
            return
        self.page.run_async(
            lambda: self.service.purge_ai_configuration(configuration_id),
            success=self._saved,
        )


class OtherSettingsPanel(QWidget):
    """展示原生桌面应用行为与更新偏好，不再暴露 Web 服务参数。"""

    def __init__(self, service, page: BasePage) -> None:
        super().__init__()
        self.service = service
        self.page = page
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 18, 8, 8)
        application_card, application_layout = card("应用行为")
        application_form = QFormLayout()
        application_form.setSpacing(12)
        self.start_with_windows = ThemedCheckBox(tr("随 Windows 开机启动"))
        self.close_behavior = SmoothComboBox()
        self.close_behavior.addItem(tr("每次询问"), "ask")
        self.close_behavior.addItem(tr("最小化到系统托盘"), "minimize")
        self.close_behavior.addItem(tr("直接退出程序"), "quit")
        application_form.addRow("", self.start_with_windows)
        application_form.addRow(tr("关闭主窗口时"), self.close_behavior)
        application_layout.addLayout(application_form)
        application_layout.addWidget(
            status_label(
                tr("开机启动仅为当前 Windows 用户启用，无需管理员权限。")
            )
        )
        self.application_status = status_label()
        application_layout.addWidget(self.application_status)
        application_actions = QHBoxLayout()
        application_actions.addStretch(1)
        self.application_save_button = QPushButton(tr("保存应用行为"))
        self.application_save_button.clicked.connect(
            self.save_application_preferences
        )
        application_actions.addWidget(self.application_save_button)
        application_layout.addLayout(application_actions)
        outer.addWidget(application_card)

        update_card, update_layout = card("自动更新")
        update_form = QFormLayout()
        update_form.setSpacing(12)
        self.update_enabled = ThemedCheckBox(tr("启用自动更新检查"))
        self.update_channel = SmoothComboBox()
        self.update_channel.addItem(tr("稳定通道"), "stable")
        self.update_channel.addItem(tr("测试通道"), "beta")
        self.update_manifest = QLineEdit()
        self.update_manifest.setPlaceholderText("https://")
        update_form.addRow("", self.update_enabled)
        update_form.addRow(tr("更新通道"), self.update_channel)
        update_form.addRow(tr("更新清单地址"), self.update_manifest)
        update_layout.addLayout(update_form)
        update_layout.addWidget(
            status_label(tr("更新设置在下次启动应用时生效；地址留空时不会联网检查。"))
        )
        self.update_status = status_label()
        update_layout.addWidget(self.update_status)
        update_actions = QHBoxLayout()
        update_actions.addStretch(1)
        self.update_save_button = QPushButton(tr("保存更新设置"))
        self.update_save_button.clicked.connect(self.save_update_preferences)
        update_actions.addWidget(self.update_save_button)
        update_layout.addLayout(update_actions)
        outer.addWidget(update_card)
        outer.addStretch(1)

    def refresh(self) -> None:
        self.page.run_async(
            self.service.get_application_preferences,
            success=self._apply_application_view,
        )
        self.page.run_async(
            self.service.get_update_preferences,
            success=self._apply_update_view,
        )

    def _apply_application_view(self, view: dict[str, Any]) -> None:
        self.start_with_windows.setChecked(
            bool(view.get("start_with_windows", False))
        )
        _set_combo(
            self.close_behavior,
            str(view.get("close_behavior") or "ask"),
        )

    def save_application_preferences(self) -> None:
        payload = {
            "start_with_windows": self.start_with_windows.isChecked(),
            "close_behavior": str(self.close_behavior.currentData() or "ask"),
        }
        self.application_save_button.setEnabled(False)
        self.application_status.setText(tr("正在保存应用行为…"))

        def success(view: dict[str, Any]) -> None:
            self._apply_application_view(view)
            self.application_status.setText(tr("应用行为已保存"))

        self.page.run_async(
            lambda: self.service.save_application_preferences(payload),
            success=success,
            finished=lambda: self.application_save_button.setEnabled(True),
        )

    def _apply_update_view(self, view: dict[str, Any]) -> None:
        self.update_enabled.setChecked(bool(view.get("enabled", True)))
        _set_combo(self.update_channel, str(view.get("channel") or "stable"))
        self.update_manifest.setText(str(view.get("manifest_url") or ""))

    def save_update_preferences(self) -> None:
        payload = {
            "enabled": self.update_enabled.isChecked(),
            "channel": str(self.update_channel.currentData() or "stable"),
            "manifest_url": self.update_manifest.text().strip(),
        }
        self.update_save_button.setEnabled(False)
        self.update_status.setText(tr("正在保存更新设置…"))

        def success(view: dict[str, Any]) -> None:
            self._apply_update_view(view)
            self.update_status.setText(tr("更新设置已保存"))

        self.page.run_async(
            lambda: self.service.save_update_preferences(payload),
            success=success,
            finished=lambda: self.update_save_button.setEnabled(True),
        )


class SettingsPage(BasePage):
    """侧栏唯一设置入口。"""

    configuration_changed = Signal()

    def __init__(self, service) -> None:
        super().__init__("设置")
        self.tabs = SmoothTabWidget()
        self.ai_panel = AIConfigurationsPanel(service, self)
        self.other_panel = OtherSettingsPanel(service, self)
        self.ai_panel.configuration_changed.connect(self.configuration_changed.emit)
        self.tabs.addTab(self.ai_panel, tr("AI 配置"))
        self.tabs.addTab(self.other_panel, tr("其他"))
        self.tabs.currentChanged.connect(self._refresh_current)
        self.content.addWidget(self.tabs, 1)

    def refresh(self) -> None:
        self._refresh_current(self.tabs.currentIndex())

    def _refresh_current(self, _index: int) -> None:
        if self.tabs.currentWidget() is self.ai_panel:
            self.ai_panel.refresh()
        else:
            self.other_panel.refresh()
