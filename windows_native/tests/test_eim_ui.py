"""EIM 原生界面的冻结流程、编辑门禁与多语言测试。"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from windows_native.i18n import set_language, tr, translate_widget_tree
from windows_native.ui.eim_pages import (
    CreateEIMTaskDialog,
    EIMPage,
    EIMStudioPage,
    _capabilities_text,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _service() -> SimpleNamespace:
    return SimpleNamespace(
        eim=SimpleNamespace(),
        get_ai_configurations=lambda: {"configurations": []},
    )


def _snapshot(observed_state: str = "stopped") -> dict:
    return {
        "task": {
            "task_id": "01K42YJ9M2E7H05KTA7VC6ER8R",
            "display_id": "EIM-20260901-TEST",
            "name": "群消息归档",
            "connection_id": "connection-1",
            "source_id": "cid-1",
            "source_name": "测试群",
            "build_state": "ready",
            "observed_state": observed_state,
            "active_version_id": "version-1",
            "editable": observed_state == "stopped",
        },
        "destination": {
            "destination_type": "dingtalk_doc",
            "url": "https://alidocs.dingtalk.com/i/nodes/test",
            "stable_ids": {"document_id": "doc-1"},
            "schema_snapshot": {"fields": ["title", "body"]},
        },
        "dsl": {
            "schema_version": "eim-dsl/v1",
            "triggers": ["message", "reaction"],
            "filters": [],
            "context": {"include_quote": True},
            "extractors": [],
            "ai_steps": [],
            "mappings": {"body": "message.text"},
            "media_policy": {},
            "destination_action": "append",
            "failure_policy": "dead_letter",
        },
        "samples": [],
        "versions": [],
        "logs": [],
        "self_loop_notice": "当前授权账号本人发送的消息不会进入监听；其他成员和机器人消息正常监听。",
    }


def _aitable_snapshot() -> dict:
    value = _snapshot()
    value["destination"] = {
        "destination_type": "dingtalk_aitable",
        "url": "https://alidocs.dingtalk.com/i/nodes/example",
        "stable_ids": {"base_id": "base-1", "table_id": "table-1"},
        "schema_snapshot": {
            "fields": [
                {"fieldId": "field-body", "fieldName": "消息内容", "type": "text"},
                {"fieldId": "field-note", "fieldName": "补充说明", "type": "text"},
                {"fieldId": "field-event", "fieldName": "备用编号", "type": "text"},
                {"fieldId": "field-media", "fieldName": "现场附件", "type": "attachment"},
                {"fieldId": "field-number", "fieldName": "序号", "type": "autoNumber"},
            ],
            "writable_fields": [
                {"fieldId": "field-body", "fieldName": "消息内容", "type": "text"},
                {"fieldId": "field-note", "fieldName": "补充说明", "type": "text"},
                {"fieldId": "field-event", "fieldName": "备用编号", "type": "text"},
                {"fieldId": "field-media", "fieldName": "现场附件", "type": "attachment"},
            ],
        },
    }
    value["dsl"]["destination_action"] = "upsert"
    value["dsl"]["mappings"] = {"field-body": "message.text"}
    return value


def test_create_dialog_contains_only_frozen_four_fields_and_warning(app: QApplication) -> None:
    dialog = CreateEIMTaskDialog([{"id": "cid-1", "name": "测试群"}])
    labels = {label.text() for label in dialog.findChildren(QLabel)}
    assert {"任务名称", "平台", "来源群", "归档目标链接"} <= labels
    assert dialog.platform_combo.isEnabled() is False
    assert "本人发送的消息不会进入监听" in " ".join(labels)
    dialog.close()


def test_workbench_has_five_tabs_and_enforces_running_edit_gate(app: QApplication) -> None:
    page = EIMStudioPage(_service())
    page._apply_snapshot(_snapshot("stopped"))
    assert [page.tabs.tabText(index) for index in range(page.tabs.count())] == [
        "规则与映射",
        "样例与测试",
        "运行日志",
        "高级 DSL",
        "版本",
    ]
    assert page.name_edit.isEnabled()
    assert not page.dsl_edit.isReadOnly()
    assert page.delete_button.isEnabled()
    page.summary_toggle.setChecked(False)
    assert not page.summary_content.isVisible()
    page.summary_toggle.setChecked(True)

    page._apply_snapshot(_snapshot("running"))
    assert not page.name_edit.isEnabled()
    assert page.dsl_edit.isReadOnly()
    assert not page.delete_button.isEnabled()
    assert page.stop_button.isEnabled()
    page.close()


def test_eim_home_and_workbench_translate_to_english(app: QApplication) -> None:
    set_language("zh_CN")
    page = EIMPage(_service())
    page._apply_snapshot(
        {
            "connected": True,
            "self_loop_notice": _snapshot()["self_loop_notice"],
            "tasks": [],
        }
    )
    set_language("en_US")
    translate_widget_tree(page)
    page._apply_snapshot(
        {
            "connected": True,
            "self_loop_notice": _snapshot()["self_loop_notice"],
            "tasks": [
                {
                    **_snapshot()["task"],
                    "source_name": "Quality Group",
                    "archived_today": 1,
                    "failed_today": 0,
                }
            ],
        }
    )
    page.resize(1180, 760)
    page.show()
    app.processEvents()
    texts = {label.text() for label in page.findChildren(QLabel)}
    assert "EIM Monitoring" in texts
    assert any("authorized account itself" in text for text in texts)
    assert tr("连接与运行设置") == "Connection and Runtime Settings"
    actions = page.task_table.cellWidget(0, 6)
    assert actions is not None
    assert page.task_table.columnWidth(6) >= actions.minimumSizeHint().width()
    assert all(
        button.width() >= button.minimumSizeHint().width()
        for button in actions.findChildren(QPushButton)
    )
    page.close()
    set_language("zh_CN")


def test_workbench_is_responsive_and_critical_text_has_three_languages(app: QApplication) -> None:
    page = EIMStudioPage(_service())
    snapshot = _snapshot()
    snapshot["logs"] = [
        {
            "timestamp": "2026-09-02T08:00:00+00:00",
            "stage": "receive",
            "result": "completed",
            "event_id": str(index),
            "preview": "用于验证隐藏日志页不会撑高规则页的长文本" * 4,
        }
        for index in range(68)
    ]
    page._apply_snapshot(snapshot)
    page.resize(720, 720)
    page.show()
    app.processEvents()
    assert page.workspace_splitter.orientation() == Qt.Orientation.Horizontal
    assert page.page_scroll is not None
    assert page.page_scroll.horizontalScrollBar().maximum() == 0
    assert not hasattr(page, "tabs_scroll")
    assert page.page_scroll.verticalScrollBar().maximum() > 0
    assert page.builder_panel.width() == 320
    assert page.filters_edit.height() <= 56
    page.filters_edit.setPlainText("[\n  {\n    \"field\": \"message.text\"\n  }\n]")
    app.processEvents()
    app.processEvents()
    assert page.filters_edit.height() >= page.filters_edit.fontMetrics().lineSpacing() * 5
    assert page.tabs.height() == page.tabs.sizeHint().height()
    assert page.mapping_table.height() <= 120
    rules_height = page.tabs.height()
    assert page.recent_logs_table.height() > rules_height
    trigger_frame = page.tabs.widget(0).layout().itemAt(1).widget()
    assert trigger_frame.height() <= trigger_frame.sizeHint().height()
    page.tabs.setCurrentIndex(2)
    app.processEvents()
    assert page.tabs.height() > rules_height
    page.tabs.setCurrentIndex(0)
    app.processEvents()
    assert page.tabs.height() <= rules_height
    for table in (
        page.mapping_table,
        page.samples_table,
        page.recent_logs_table,
        page.versions_table,
    ):
        assert table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert table.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert page.dsl_edit.lineWrapMode() == page.dsl_edit.LineWrapMode.WidgetWidth
    assert page.dsl_edit.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert page.dsl_edit.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    for index in range(1, 5):
        layout = page.tabs.widget(index).layout()
        assert layout.itemAt(layout.count() - 1).spacerItem() is not None
    assert page.builder_panel.isVisible()
    assert page.builder_panel.geometry().right() <= page.workspace_splitter.contentsRect().right()
    page.resize(1160, 720)
    app.processEvents()
    assert page.workspace_splitter.orientation() == Qt.Orientation.Horizontal
    assert page.page_scroll.horizontalScrollBar().maximum() == 0
    page.close()

    notice = _snapshot()["self_loop_notice"]
    set_language("zh_TW")
    assert "訊息" in tr(notice) and "監聽" in tr(notice)
    set_language("en_US")
    assert "authorized account itself" in tr(notice)
    assert "Credential Valid" in _capabilities_text(
        {"authenticated": True, "token_valid": True}
    )
    set_language("zh_CN")


def test_ai_builder_explains_detection_status_and_instruction_role(app: QApplication) -> None:
    page = EIMStudioPage(_service())
    configurations = {
        "configurations": [
            {
                "id": "ai-1",
                "name": "Luna",
                "model": "gpt-test",
                "complete": True,
            },
            {
                "id": "ai-incomplete",
                "name": "Incomplete",
                "model": "gpt-test",
                "complete": False,
            },
        ]
    }
    page._apply_ai_configurations(
        (
            configurations,
            [
                {
                    "configuration_id": "ai-1",
                    "compatible": False,
                    "detail": "Invalid schema",
                }
            ],
        )
    )
    assert page.model_combo.count() == 1
    assert page.detect_model_combo.count() == 1
    assert "检测失败" in page.detect_model_combo.itemText(0)
    assert "Invalid schema" in page.compatibility_status.text()
    assert "尚无检测通过项" in page.build_configuration_hint.text()
    assert "构建说明会被忽略" in page.instruction_hint.text()

    page._apply_ai_configurations(
        (
            configurations,
            [
                {
                    "configuration_id": "ai-1",
                    "compatible": True,
                    "detail": "EIM 结构化动作兼容",
                }
            ],
        )
    )
    assert page.model_combo.count() == 2
    assert "已兼容" in page.model_combo.itemText(1)
    page.model_combo.setCurrentIndex(1)
    assert "修改目标" in page.instruction_hint.text()
    assert "只归档正文包含“故障”" in page.instruction_edit.placeholderText()
    assert "只归档正文包含“故障”" in page.instruction_edit.property("i18n_placeholder")
    set_language("en_US")
    translate_widget_tree(page)
    assert "archive only messages" in page.instruction_edit.placeholderText()
    page.close()
    set_language("zh_CN")


def test_rules_mapping_explains_text_only_binding_and_uses_semantic_options(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = EIMStudioPage(_service())
    page._apply_snapshot(_aitable_snapshot())
    page.resize(720, 720)
    page.show()
    app.processEvents()
    assert page.destination_refresh_button.y() == page.destination_apply_button.y()

    # 目标绑定只列三个文本字段；字段映射仍列出全部四个可写字段。
    assert page.destination_combo.count() == 4  # 含“请选择”占位项
    assert page.target_combo.count() == 4
    assert "5 个字段" in page.destination_hint_label.text()
    assert "4 个可写" in page.destination_hint_label.text()
    assert "3 个文本字段" in page.destination_hint_label.text()

    source_index = page.source_combo.findData("message.text")
    assert source_index >= 0
    assert "消息正文" in page.source_combo.itemText(source_index)
    assert page.mapping_table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "field-body"
    assert "消息内容" in page.mapping_table.item(0, 0).text()

    page.target_combo.setCurrentIndex(page.target_combo.findData("field-media"))
    page.source_combo.setCurrentIndex(page.source_combo.findData("media"))
    page._add_mapping()
    assert page.mapping_table.item(1, 0).data(Qt.ItemDataRole.UserRole) == "field-media"
    assert page.mapping_table.item(1, 1).data(Qt.ItemDataRole.UserRole) == "media"

    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "windows_native.ui.eim_pages.QMessageBox.information",
        lambda _parent, title, text: dialogs.append((title, text)),
    )
    page._show_destination_help()
    page._show_trigger_help()
    page._show_mapping_help()
    assert [title for title, _text in dialogs] == [
        "目标绑定填写说明",
        "触发与上下文填写说明",
        "字段映射填写说明",
    ]
    assert "EIM 事件 ID" in dialogs[0][1] and "刷新目标结构" in dialogs[0][1]
    assert '"operator": "contains"' in dialogs[1][1]
    assert "消息内容" in dialogs[2][1] and "现场附件" in dialogs[2][1]
    page.close()


def test_source_field_semantics_translate_to_english(app: QApplication) -> None:
    set_language("zh_CN")
    page = EIMStudioPage(_service())
    set_language("en_US")
    translate_widget_tree(page)
    index = page.source_combo.findData("sender.name")
    assert "Sender name" in page.source_combo.itemText(index)
    assert "View Target Binding instructions" == tr(
        "查看{section}填写说明", section=tr("目标绑定")
    )
    page.close()
    set_language("zh_CN")
