"""ForTest 原生界面的离屏工作流测试。"""

from __future__ import annotations

import copy
import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, QPoint, QPointF, QThreadPool, Qt, Signal
from PySide6.QtGui import QIcon, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
)

from backend.ai.provider_specs import provider_specs_view
from windows_native.ui.common import (
    BasePage,
    ManualSpinBox,
    SmoothComboBox,
    SmoothTabWidget,
    ThemedCheckBox,
)
from windows_native.ui.main_window import MainWindow
from windows_native.ui.onboarding import ConfigurationGuideDialog
from windows_native.ui.settings_page import (
    AIConfigurationDialog,
    AIConfigurationsPanel,
)
from windows_native.ui.deployment_dialog import (
    NewDeploymentDialog,
    ScheduleRangeDialog,
    SingleDeploymentDialog,
)
from windows_native.ui.task_widgets import TaskDetailsDialog, TaskLogsDialog
from windows_native.ui.deployment_widgets import DeploymentLogsDialog


LIVE_TASK = {
    "id": "20260801010101",
    "task_id": "20260801010101",
    "name": "订单需求",
    "doc_url": "https://alidocs.dingtalk.com/i/nodes/live",
    "status": "running",
    "current_block": 2,
    "total_blocks": 5,
    "logs": ["任务已启动"],
    "trashed": False,
}

TRASHED_TASK = {
    "id": "20260801010202",
    "task_id": "20260801010202",
    "name": "已删除需求",
    "doc_url": "https://alidocs.dingtalk.com/i/nodes/trashed",
    "status": "completed",
    "current_block": 3,
    "total_blocks": 3,
    "logs": ["任务已完成"],
    "trashed": True,
}


class FakeService:
    """只返回内存快照，不访问网络或原业务服务。"""

    def __init__(self):
        self.codex_runtime_state = "bundled"
        self.ai_configurations = []
        self.ai_recycle_bin = []
        self._configuration_serial = 0
        self.model_policies = {
            "image_understanding": {"mode": "ordered", "configuration_ids": []},
            "component_matching": {"mode": "ordered", "configuration_ids": []},
            "case_generation": {"mode": "ordered", "configuration_ids": []},
        }
        self.update_preferences = {
            "enabled": True,
            "channel": "stable",
            "manifest_url": "",
        }
        self.application_preferences = {
            "start_with_windows": False,
            "close_behavior": "ask",
        }

    def get_codex_runtime_status(self) -> dict:
        return self._codex_status()

    def get_codex_runtime_catalog(self, *, refresh: bool = False) -> dict:
        return {
            "status": self._codex_status(),
            "versions": [
                {
                    "version": "0.147.0",
                    "online": True,
                    "installed": False,
                    "bundled": False,
                    "path": "C:/ForTest/runtimes/codex/packages/0.147.0/codex.exe",
                },
                {
                    "version": "0.144.4",
                    "online": True,
                    "installed": True,
                    "bundled": True,
                    "path": "C:/ForTest/bundled/codex.exe",
                },
            ],
            "fetched_at": 1 if refresh else 0,
        }

    def get_local_codex_runtime_catalog(self) -> dict:
        return self.get_codex_runtime_catalog(refresh=False)

    def get_codex_configuration_models(self, _values: dict) -> dict:
        return {
            "models": [
                {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra"},
                {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol"},
            ],
            "default_model": "gpt-5.6-terra",
        }

    def get_ai_configurations(self) -> dict:
        return {
            "providers": [
                {
                    "provider": "codex",
                    "label": "Codex CLI",
                    "protocol": "codex",
                    "base_url": "",
                    "default_model": "gpt-5.6-terra",
                    "vision_enabled": True,
                    "response_format_mode": "json_schema",
                    "requires_api_key": False,
                    "capability_rules": [
                        {
                            "model_prefixes": [""],
                            "reasoning_efforts": [
                                "none",
                                "low",
                                "medium",
                                "high",
                                "xhigh",
                                "max",
                                "ultra",
                            ],
                            "inference_speeds": ["standard", "fast"],
                            "default_reasoning_effort": "high",
                            "default_inference_speed": "standard",
                        }
                    ],
                },
                {
                    "provider": "openai",
                    "label": "OpenAI",
                    "protocol": "openai_compatible",
                    "base_url": "https://api.openai.com/v1",
                    "default_model": "gpt-5.6-terra",
                    "vision_enabled": True,
                    "response_format_mode": "json_schema",
                    "requires_api_key": True,
                    "capability_rules": [],
                },
            ],
            "configurations": copy.deepcopy(self.ai_configurations),
            "recycle_bin": copy.deepcopy(self.ai_recycle_bin),
        }

    def save_ai_configuration(self, payload: dict) -> dict:
        self._configuration_serial += 1
        item = {
            **payload,
            "id": payload.get("id") or f"configuration-{self._configuration_serial}",
            "provider_label": "Codex CLI" if payload["provider"] == "codex" else "OpenAI",
            "display_model": (
                f"{'Codex CLI' if payload['provider'] == 'codex' else 'OpenAI'} · {payload['model']}"
            ),
            "status": "unchecked",
            "status_detail": "",
            "deleted_at": None,
            "complete": bool(payload.get("model")),
        }
        if payload["provider"] != "codex":
            item["secret_status"] = {
                "configured": bool(payload.get("api_key")),
                "masked_value": "••••••" if payload.get("api_key") else None,
                "source": "saved" if payload.get("api_key") else "missing",
            }
        item.pop("api_key", None)
        item.pop("clear_api_key", None)
        self.ai_configurations = [
            item if existing["id"] == item["id"] else existing
            for existing in self.ai_configurations
        ]
        if not any(existing["id"] == item["id"] for existing in self.ai_configurations):
            self.ai_configurations.append(item)
        return self.get_ai_configurations()

    def reorder_ai_configurations(self, configuration_ids: list[str]) -> dict:
        by_id = {item["id"]: item for item in self.ai_configurations}
        self.ai_configurations = [by_id[item] for item in configuration_ids]
        return self.get_ai_configurations()

    def delete_ai_configuration(self, configuration_id: str) -> dict:
        item = next(item for item in self.ai_configurations if item["id"] == configuration_id)
        self.ai_configurations.remove(item)
        item = {**item, "deleted_at": "2026-08-09T00:00:00+00:00"}
        self.ai_recycle_bin.append(item)
        return self.get_ai_configurations()

    def restore_ai_configuration(self, configuration_id: str) -> dict:
        item = next(item for item in self.ai_recycle_bin if item["id"] == configuration_id)
        self.ai_recycle_bin.remove(item)
        self.ai_configurations.append({**item, "deleted_at": None})
        return self.get_ai_configurations()

    def purge_ai_configuration(self, configuration_id: str) -> dict:
        self.ai_recycle_bin = [
            item for item in self.ai_recycle_bin if item["id"] != configuration_id
        ]
        return self.get_ai_configurations()

    def test_ai_configuration(self, configuration_id: str) -> dict:
        configuration = {}
        for item in self.ai_configurations:
            if item["id"] == configuration_id:
                item["status"] = "passed"
                item["status_detail"] = "检测通过"
                configuration = copy.deepcopy(item)
        return {
            "ok": True,
            "detail": "检测通过",
            "models": [],
            "configuration": configuration,
        }

    def test_all_ai_configurations(self) -> dict:
        results = []
        for item in self.ai_configurations:
            item["status"] = "passed"
            results.append({"ok": True, "detail": "检测通过", "models": []})
        return {"results": results, **self.get_ai_configurations()}

    def get_test_case_model_policies(self) -> dict:
        complete = [
            copy.deepcopy(item)
            for item in self.ai_configurations
            if item.get("complete")
        ]
        return {
            "policies": copy.deepcopy(self.model_policies),
            "available": {
                "image_understanding": [
                    item for item in complete if item.get("vision_enabled", True)
                ],
                "component_matching": complete,
                "case_generation": complete,
            },
        }

    def save_test_case_model_policy(
        self,
        stage: str,
        mode: str,
        configuration_ids: list[str],
    ) -> dict:
        self.model_policies[stage] = {
            "mode": mode,
            "configuration_ids": list(configuration_ids) if mode == "custom" else [],
        }
        return self.get_test_case_model_policies()

    def install_codex_runtime(self, version: str) -> dict:
        self.codex_runtime_state = version
        catalog = self.get_codex_runtime_catalog()
        for item in catalog["versions"]:
            if item["version"] == version:
                item["installed"] = True
        return catalog

    def _codex_status(self) -> dict:
        selected = self.codex_runtime_state
        version = "0.144.4" if selected == "bundled" else selected
        path = (
            "C:/ForTest/bundled/codex.exe"
            if selected == "bundled"
            else f"C:/ForTest/runtimes/codex/packages/{version}/codex.exe"
        )
        runtime = {
            "selection": selected,
            "version": version,
            "path": path,
            "available": True,
            "bundled": selected == "bundled",
        }

        return {
            "runtime": runtime,
            "cli": dict(runtime),
            "sdk": dict(runtime),
            "sdk_bindings_version": "0.0.0.dev0",
            "installed_versions": [],
            "local_other_versions": [
                {
                    "version": "0.142.3",
                    "paths": ["C:/Program Files/Codex/codex.exe"],
                }
            ],
        }

    def get_document(self) -> dict:
        return {
            "content_template_url": "https://alidocs.dingtalk.com/sheets/template",
            "document_template_url": "https://alidocs.dingtalk.com/docs/template",
            "output_folder_url": "https://alidocs.dingtalk.com/folder/output",
            "local_output_dir": "./output",
            "document_mcp": {"configured": False},
            "spreadsheet_mcp": {"configured": False},
        }

    def get_prompts(self) -> dict:
        return {"groups": {}}

    def configuration_status(self, *, jenkins_configured: bool = False) -> dict:
        ai_ready = any(
            item.get("complete") and item.get("status") == "passed"
            for item in self.ai_configurations
        )
        sections = [
            {
                "id": "quick_deploy",
                "label": "快捷部署",
                "complete": jenkins_configured,
                "items": [
                    {
                        "id": "jenkins",
                        "label": "Jenkins 配置",
                        "complete": jenkins_configured,
                    }
                ],
            },
            {
                "id": "test_case_generation",
                "label": "测试用例生成",
                "complete": False,
                "items": [
                    {
                        "id": "document",
                        "label": "文档配置",
                        "complete": False,
                        "checks": [
                            {"label": "文档 MCP", "complete": False},
                            {"label": "表格 MCP", "complete": False},
                        ],
                    }
                ],
            },
            {
                "id": "settings",
                "label": "设置",
                "complete": ai_ready,
                "items": [
                    {"id": "ai", "label": "AI 配置", "complete": ai_ready}
                ],
            },
        ]
        return {
            "complete": all(section["complete"] for section in sections),
            "sections": sections,
        }

    def get_update_preferences(self) -> dict:
        return copy.deepcopy(self.update_preferences)

    def save_update_preferences(self, values: dict) -> dict:
        self.update_preferences = copy.deepcopy(values)
        return self.get_update_preferences()

    def get_application_preferences(self) -> dict:
        return copy.deepcopy(self.application_preferences)

    def save_application_preferences(self, values: dict) -> dict:
        self.application_preferences = copy.deepcopy(values)
        return self.get_application_preferences()


class FakeTaskManager:
    """提供主页与回收站所需的最小只读任务接口。"""

    def __init__(self):
        self.tasks = [copy.deepcopy(LIVE_TASK), copy.deepcopy(TRASHED_TASK)]
        self.started = 0
        self.stopped = 0
        self.max_parallel = 1

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def list_tasks(self, *, include_trashed: bool = False) -> list[dict]:
        tasks = self.tasks if include_trashed else [
            task for task in self.tasks if not task.get("trashed")
        ]
        return copy.deepcopy(tasks)

    def active_count(self) -> int:
        return sum(
            task.get("status") in {"queued", "starting", "pending", "running"}
            and not task.get("trashed")
            for task in self.tasks
        )

    def queued_count(self) -> int:
        return sum(
            task.get("status") == "pending" and not task.get("trashed")
            for task in self.tasks
        )

    def get_max_parallel(self) -> int:
        return self.max_parallel

    def set_max_parallel(self, value: int) -> None:
        self.max_parallel = int(value)

    def get_task(self, task_id: str) -> dict | None:
        return next(
            (copy.deepcopy(task) for task in self.tasks if task["task_id"] == task_id),
            None,
        )

    def restore(self, task_id: str) -> None:
        task = next(task for task in self.tasks if task["task_id"] == task_id)
        task["trashed"] = False

    def stop_task(self, task_id: str) -> dict:
        task = next(task for task in self.tasks if task["task_id"] == task_id)
        task["status"] = "stopped"
        return copy.deepcopy(task)


class FakeDeploymentService:
    """快捷部署页面使用的无网络 Jenkins 门面。"""

    def __init__(self):
        self.configured = False
        self.refresh_calls = 0
        self.deployment_tasks = []
        self.saved_show_prod = None
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def configuration_view(self) -> dict:
        return {
            "configured": self.configured,
            "base_url": "",
            "username": "",
            "token_mask": None,
            "security_warning": "",
        }

    def project_snapshot(self) -> dict:
        return {"last_refreshed_at": "", "projects": []}

    def validate_and_save_configuration(
        self,
        address: str,
        username: str,
        token: str,
        **_kwargs,
    ) -> dict:
        self.configured = bool(address and username and token)
        return {
            "configured": self.configured,
            "base_url": address,
            "username": username,
            "token_mask": "••••",
            "security_warning": "",
        }

    def refresh_projects(self, **_kwargs) -> dict:
        self.refresh_calls += 1
        return {"last_refreshed_at": "", "projects": []}

    def task_settings_view(self) -> dict:
        return {
            "show_prod": False,
            "orchestration_mode": "direct_parallel_subtasks",
        }

    def save_task_settings(self, *, show_prod: bool) -> dict:
        self.saved_show_prod = bool(show_prod)
        return {
            "show_prod": bool(show_prod),
            "orchestration_mode": "direct_parallel_subtasks",
        }

    def list_deployment_tasks(self, *, trashed: bool = False) -> list[dict]:
        return [
            copy.deepcopy(item)
            for item in self.deployment_tasks
            if bool(item.get("trashed")) is bool(trashed)
        ]

    def create_deployment_task(
        self,
        name: str,
        selections: list[dict],
        *,
        schedule: dict | None = None,
    ) -> dict:
        schedule = dict(schedule or {"enabled": False})
        task = {
            "task_id": "20260808010101",
            "iteration_name": name,
            "status": "scheduled" if schedule.get("enabled") else "queued",
            "progress_percent": 0,
            "duration_seconds": 0,
            "items": copy.deepcopy(selections),
            "schedule": schedule,
            "trashed": False,
        }
        self.deployment_tasks.append(task)
        return copy.deepcopy(task)

    def create_single_deployment_task(self, selection: dict) -> dict:
        project = str(
            selection.get("project_name")
            or str(selection.get("project_full_name") or "").rsplit("/", 1)[-1]
        )
        task = self.create_deployment_task(
            "单点部署-"
            f"{selection.get('environment')}·{project}·{selection.get('branch')}",
            [selection],
        )
        self.deployment_tasks[-1]["deployment_type"] = "single"
        return copy.deepcopy(self.deployment_tasks[-1])

    def deployment_task(self, task_id: str) -> dict | None:
        return next(
            (
                copy.deepcopy(item)
                for item in self.deployment_tasks
                if item["task_id"] == task_id
            ),
            None,
        )

    def stop_deployment_task(self, task_id: str) -> dict:
        task = next(item for item in self.deployment_tasks if item["task_id"] == task_id)
        task["status"] = "stopped"
        return copy.deepcopy(task)

    def retry_deployment_task(self, task_id: str) -> dict:
        original = self.deployment_task(task_id)
        return self.create_deployment_task(original["iteration_name"], original["items"])

    def trash_deployment_task(self, task_id: str) -> dict:
        task = next(item for item in self.deployment_tasks if item["task_id"] == task_id)
        task["trashed"] = True
        return copy.deepcopy(task)

    def restore_deployment_task(self, task_id: str) -> dict:
        task = next(item for item in self.deployment_tasks if item["task_id"] == task_id)
        task["trashed"] = False
        return copy.deepcopy(task)


class FakeThemeManager(QObject):
    """模拟主题管理器信号，不读取系统设置或写偏好文件。"""

    mode_changed = Signal(str)
    theme_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.mode = "system"

    def effective_mode(self) -> str:
        return "light" if self.mode == "system" else self.mode

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.mode_changed.emit(mode)
        self.theme_changed.emit(self.effective_mode())


class DeferredAsyncHost:
    """暂存页面异步任务，让测试可分别断言执行前、成功和失败状态。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run_async(
        self,
        function,
        *,
        success=None,
        failure=None,
        finished=None,
    ) -> None:
        self.calls.append(
            {
                "function": function,
                "success": success,
                "failure": failure,
                "finished": finished,
            }
        )

    def complete_next(self) -> None:
        """同步完成最早任务，并保持 BasePage 的回调顺序。"""

        call = self.calls.pop(0)
        try:
            result = call["function"]()
        except Exception as exc:
            if call["failure"] is None:
                raise
            call["failure"](str(exc))
        else:
            if call["success"] is not None:
                call["success"](result)
        finally:
            if call["finished"] is not None:
                call["finished"]()


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def workflow(app: QApplication):
    service = FakeService()
    manager = FakeTaskManager()
    theme = FakeThemeManager()
    deployment = FakeDeploymentService()
    window = MainWindow(
        service,
        QIcon(),
        manager,
        theme,
        deployment,
        onboarding_enabled=False,
    )
    window.resize(960, 700)
    window.show()
    window.activateWindow()
    app.processEvents()
    window.start_background_services()
    yield window, service, manager, theme
    window.close()
    _drain_workers(app)
    app.processEvents()


def _drain_workers(app: QApplication) -> None:
    """等待内存 fake 的短任务结束，避免跨测试遗留 Qt 信号。"""

    pool = QThreadPool.globalInstance()
    deadline = time.monotonic() + 2.0
    while pool.activeThreadCount() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    pool.waitForDone(500)
    app.processEvents()


def test_preloaded_window_consumes_snapshot_without_restarting_local_services(
    app: QApplication,
):
    service = FakeService()
    manager = FakeTaskManager()
    theme = FakeThemeManager()
    deployment = FakeDeploymentService()
    window = MainWindow(
        service,
        QIcon(),
        manager,
        theme,
        deployment,
        onboarding_enabled=False,
        background_refresh_enabled=False,
        startup_preloaded=True,
    )
    applied: list[str] = []
    window.home_page.apply_document = lambda _value: applied.append("document")
    window.settings_page.ai_panel.apply_view = lambda _view: applied.append("ai")
    window.quick_deploy_page.apply_startup_snapshot = (
        lambda **_values: applied.append("deployment")
    )
    snapshot = SimpleNamespace(
        document={},
        ai_configurations={},
        configuration_status={"complete": True, "sections": []},
        deployment_configuration={},
        deployment_task_settings={},
        deployment_projects={},
        deployment_tasks=[],
    )

    window.apply_startup_snapshot(snapshot)
    window.start_background_services()

    assert applied == ["document", "ai", "deployment"]
    assert window._startup_snapshot_applied is True
    assert manager.started == 0
    assert deployment.started == 0
    window.close()


def test_sidebar_and_internal_routes(workflow, app: QApplication):
    window, _service, manager, _theme = workflow
    assert [window.nav_group.button(index).text() for index in range(4)] == [
        "快捷部署",
        "测试用例生成",
        "设置",
        "外观",
    ]
    assert len(window.nav_group.buttons()) == 4
    assert window.stack.currentWidget() is window.quick_deploy_page
    assert window.config_page not in window.nav_pages
    assert window.recycle_page not in window.nav_pages
    assert window.deployment_recycle_page not in window.nav_pages
    assert window.deployment_config_page not in window.nav_pages
    assert manager.started == 1

    window.home_page.config_button.click()
    assert window.stack.currentWidget() is window.config_page
    assert [
        window.config_page.tabs.tabText(index)
        for index in range(window.config_page.tabs.count())
    ] == ["文档配置", "提示词配置", "任务配置"]
    window.config_page.back_button.click()
    assert window.stack.currentWidget() is window.home_page

    window.home_page.recycle_button.click()
    assert window.stack.currentWidget() is window.recycle_page
    window.recycle_page.back_button.click()
    assert window.stack.currentWidget() is window.home_page

    window.show_quick_deploy()
    window.quick_deploy_page.recycle_button.click()
    assert window.stack.currentWidget() is window.deployment_recycle_page
    window.deployment_recycle_page.back_button.click()
    assert window.stack.currentWidget() is window.quick_deploy_page

    window.quick_deploy_page.modify_config_button.click()
    assert window.stack.currentWidget() is window.deployment_config_page
    assert [
        window.deployment_config_page.tabs.tabText(index)
        for index in range(window.deployment_config_page.tabs.count())
    ] == ["Jenkins 配置", "任务配置"]
    window.deployment_config_page.back_button.click()
    assert window.stack.currentWidget() is window.quick_deploy_page
    _drain_workers(app)


def test_configuration_guide_follows_menu_groups_and_routes_targets(workflow):
    window, service, _manager, _theme = workflow
    opened: list[str] = []
    status = service.configuration_status(jenkins_configured=False)
    dialog = ConfigurationGuideDialog(
        status,
        on_open=opened.append,
        on_language=lambda _language: None,
        on_dismiss=lambda: None,
        parent=window,
    )

    labels = [label.text() for label in dialog.findChildren(QLabel)]
    for expected in (
        "快捷部署",
        "Jenkins 配置",
        "测试用例生成",
        "文档配置",
        "设置",
        "AI 配置",
    ):
        assert expected in labels
    assert any("缺少：文档 MCP、表格 MCP" in label for label in labels)

    first_open = next(
        button
        for button in dialog.findChildren(QPushButton)
        if button.text() == "前往配置"
    )
    first_open.click()
    assert opened == ["jenkins"]

    window._open_guide_target("jenkins")
    assert window.stack.currentWidget() is window.deployment_config_page
    assert window.deployment_config_page.tabs.currentIndex() == 0
    window._open_guide_target("document")
    assert window.stack.currentWidget() is window.config_page
    assert window.config_page.tabs.currentIndex() == 0
    window._open_guide_target("ai")
    assert window.stack.currentWidget() is window.settings_page
    assert window.settings_page.tabs.currentIndex() == 0


def test_quick_deploy_guide_validates_required_fields_and_enters_feature_page(
    workflow,
    app: QApplication,
):
    window, _service, _manager, _theme = workflow
    page = window.quick_deploy_page
    _drain_workers(app)
    assert page.mode_stack.currentWidget() is page.guide_panel
    assert page.guide_confirm.isEnabled() is False
    assert page.guide_token.echoMode() == QLineEdit.EchoMode.Password
    assert "Security" in page.guide_hint.text()
    assert page.guide_username_hint.text() == "用户登陆账号"
    assert page.guide_username_hint.objectName() == "fieldHint"

    page.guide_address.setText("https://jenkins.example.com/")
    page.guide_username.setText("fortester-bot")
    assert page.guide_confirm.isEnabled() is False
    page.guide_token.setText("local-test-token")
    assert page.guide_confirm.isEnabled() is True
    page.guide_confirm.click()
    deadline = time.monotonic() + 2.0
    while (
        window.deployment_service.refresh_calls < 1
        and time.monotonic() < deadline
    ):
        _drain_workers(app)
        app.processEvents()
        time.sleep(0.005)

    assert page.mode_stack.currentWidget() is page.feature_panel
    assert page.guide_token.text() == ""
    assert page.project_count_label.text() == "可部署项目：0"
    assert window.deployment_service.refresh_calls >= 1


def test_quick_deploy_task_table_and_actions_match_requirement(workflow, app: QApplication):
    window, _service, _manager, _theme = workflow
    page = window.quick_deploy_page
    _drain_workers(app)
    table = page.task_table.table
    assert [table.horizontalHeaderItem(index).text() for index in range(5)] == [
        "任务 ID",
        "迭代名称",
        "进度",
        "状态",
        "操作",
    ]
    assert page.single_deployment_button.text() == "单点部署"
    assert page.new_deployment_button.text() == "新建迭代部署"
    assert page.modify_config_button.text() == "修改配置"
    action_layout = page.single_deployment_button.parentWidget().layout()
    assert [
        widget.text()
        for index in range(action_layout.count())
        if isinstance((widget := action_layout.itemAt(index).widget()), QPushButton)
    ] == ["单点部署", "新建迭代部署", "修改配置"]
    assert page.hide_single_deployments.text() == "隐藏单点部署"
    assert page.recycle_button.text() == "回收站"
    assert page.refresh_projects_button.text() == "刷新项目 ⟳"
    assert table.horizontalHeader().sectionResizeMode(2) == QHeaderView.Fixed
    assert table.columnWidth(2) == 164
    assert page.scheduled_deployments.text() == "定时任务：0"


def test_clicking_deployment_progress_opens_live_logs(
    workflow,
    monkeypatch,
):
    window, _service, _manager, _theme = workflow
    page = window.quick_deploy_page
    window.deployment_service.deployment_tasks = [
        {
            "task_id": "20260808010101",
            "iteration_name": "日志验证",
            "deployment_type": "iteration",
            "status": "running",
            "current_step": 1,
            "total_steps": 2,
            "progress_percent": 50,
            "items": [],
            "logs": ["子任务已提交"],
            "trashed": False,
        }
    ]
    opened = []
    monkeypatch.setattr(
        DeploymentLogsDialog,
        "exec",
        lambda dialog: opened.append(dialog.task_id) or QDialog.Rejected,
    )

    page.refresh_tasks()
    progress = page.task_table.table.cellWidget(0, 2)
    assert not isinstance(progress, QPushButton)
    progress_count = progress.findChild(QPushButton, "progressCountLink")
    assert progress_count is not None
    assert progress_count.text() == "1/2"
    assert progress_count.accessibleName() == "查看部署执行日志"
    assert progress_count.toolTip() == "点击查看部署执行日志"
    progress_count.click()

    assert opened == ["20260808010101"]


def test_clicking_recycled_deployment_progress_opens_saved_logs(
    workflow,
    monkeypatch,
):
    window, _service, _manager, _theme = workflow
    window.deployment_service.deployment_tasks = [
        {
            "task_id": "20260808020202",
            "iteration_name": "回收站日志验证",
            "deployment_type": "iteration",
            "status": "completed",
            "current_step": 2,
            "total_steps": 2,
            "progress_percent": 100,
            "items": [],
            "logs": ["部署任务已完成"],
            "trashed": True,
        }
    ]
    opened = []
    monkeypatch.setattr(
        DeploymentLogsDialog,
        "exec",
        lambda dialog: opened.append(dialog.task_id) or QDialog.Rejected,
    )

    window.show_deployment_recycle()
    progress = window.deployment_recycle_page.table.table.cellWidget(0, 2)
    progress_count = progress.findChild(QPushButton, "progressCountLink")
    assert progress_count is not None
    assert progress_count.text() == "2/2"
    progress_count.click()

    assert opened == ["20260808020202"]


def test_scheduled_task_uses_native_icon_status_and_overview_count(workflow):
    window, _service, _manager, _theme = workflow
    page = window.quick_deploy_page
    window.deployment_service.deployment_tasks = [
        {
            "task_id": "20260808010102",
            "iteration_name": "每日巡检",
            "deployment_type": "iteration",
            "status": "scheduled",
            "current_step": 0,
            "total_steps": 1,
            "progress_percent": 0,
            "items": [],
            "schedule": {"enabled": True, "state": "waiting"},
            "trashed": False,
        }
    ]

    page.refresh_tasks()

    name = page.task_table.table.cellWidget(0, 1)
    assert isinstance(name, QPushButton)
    assert name.icon().isNull() is False
    assert name.toolTip() == "定时部署任务"
    assert name.accessibleName() == "定时部署任务：每日巡检"
    assert page.task_table.table.item(0, 3).text() == "定时等待"
    assert page.scheduled_deployments.text() == "定时任务：1"


def test_deployment_logs_dialog_renders_timestamped_and_legacy_subtask_logs(
    app: QApplication,
):
    class LogService:
        task = {
            "task_id": "20260808010101",
            "status": "running",
            "current_step": 1,
            "total_steps": 2,
            "log_entries": [
                {
                    "timestamp": "2026-08-08T01:02:03+08:00",
                    "message": "子任务 20260808010101-001 已提交 Jenkins 队列 #41",
                }
            ],
            "items": [],
        }

        def deployment_task(self, _task_id: str) -> dict:
            return copy.deepcopy(self.task)

    service = LogService()
    dialog = DeploymentLogsDialog(service, "20260808010101")
    assert "[2026-08-08 01:02:03]" in dialog.logs.toPlainText()
    assert "队列 #41" in dialog.logs.toPlainText()

    service.task = {
        "task_id": "20260808010101",
        "status": "completed",
        "current_step": 1,
        "total_steps": 1,
        "items": [
            {
                "project_full_name": "dtmzp/dtm_pc",
                "environment": "test",
                "branch": "origin/master",
                "status": "completed",
                "queue_id": 41,
                "build_number": 141,
            }
        ],
    }
    dialog.refresh()
    legacy = dialog.logs.toPlainText()
    assert "20260808010101-001" in legacy
    assert "Queue #41" in legacy
    assert "Build #141" in legacy
    dialog.close()


def test_requested_settings_use_visible_clickable_checkboxes(workflow):
    """保留的布尔控件可点击，Codex 不再出现专用密钥选项。"""

    window, service, _manager, _theme = workflow
    task_checkbox = window.deployment_config_page.task_panel.show_prod
    view = service.get_ai_configurations()
    dialog = AIConfigurationDialog(
        view["providers"],
        service.get_local_codex_runtime_catalog(),
    )
    clear_checkbox = dialog.clear_api_key
    assert not hasattr(dialog, "dedicated_key")
    assert dialog.api_key.isHidden()
    assert dialog.clear_api_key.isHidden()

    dialog.provider.setCurrentIndex(dialog.provider.findData("openai"))
    controls = (task_checkbox, clear_checkbox)
    assert all(isinstance(control, ThemedCheckBox) for control in controls)
    assert all(control.sizeHint().width() > control.INDICATOR_SIZE for control in controls)

    for control in controls:
        control.setChecked(False)
        control.click()
        assert control.isChecked() is True

    window.deployment_config_page.task_panel.save()
    assert window.deployment_service.saved_show_prod is True
    payload = dialog.payload()
    assert "use_dedicated_api_key" not in payload
    assert payload["clear_api_key"] is True
    dialog.close()


def test_project_refresh_status_and_restart_confirmation(
    workflow,
    app: QApplication,
    monkeypatch,
):
    window, _service, _manager, _theme = workflow
    page = window.quick_deploy_page
    _drain_workers(app)
    page._apply_project_snapshot(
        {
            "last_refreshed_at": "2026-08-08T01:02:03+08:00",
            "projects": [],
        }
    )
    assert page.last_refresh_label.text() == (
        "项目最后刷新时间：20260808010203"
    )

    calls = []
    monkeypatch.setattr(page, "refresh_projects", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        "windows_native.ui.quick_deploy_page.confirm_action",
        lambda *_args, **_kwargs: False,
    )
    page._set_refreshing(True)
    assert page.last_refresh_label.text() == "项目刷新中"
    assert page.refresh_indicator.timer.isActive() is True
    assert page.refresh_projects_button.isEnabled() is True
    page.refresh_projects_button.click()
    assert calls == []

    monkeypatch.setattr(
        "windows_native.ui.quick_deploy_page.confirm_action",
        lambda *_args, **_kwargs: True,
    )
    page.refresh_projects_button.click()
    assert calls == [{"show_errors": True}]
    page._set_refreshing(False)


def test_first_refresh_blocks_empty_deployment_dialog_with_information(
    workflow,
    app: QApplication,
    monkeypatch,
):
    window, _service, _manager, _theme = workflow
    page = window.quick_deploy_page
    _drain_workers(app)
    page._project_snapshot = {"last_refreshed_at": "", "projects": []}
    page._set_refreshing(True)
    messages = []
    monkeypatch.setattr(page, "show_info", lambda message: messages.append(message))

    page.open_new_deployment()

    assert messages == ["Jenkins项目刷新中，请稍候"]
    assert page._dialogs == set()
    page._set_refreshing(False)


def test_deployment_selector_cascades_environment_project_and_branch(app: QApplication):
    snapshot = {
        "last_refreshed_at": "2026-08-08T01:02:03+08:00",
        "projects": [
            {
                "full_name": "dtmzp/dtmzp_admin",
                "name": "dtmzp_admin",
                "description": "总后台前端",
                "eligible": True,
                "environments": ["test", "staging", "prod"],
                "target_branches": ["origin/master", "origin/feature/a"],
            },
            {
                "full_name": "dtmzp/dtm_pc",
                "name": "dtm_pc",
                "description": "PC 前端",
                "eligible": True,
                "environments": ["test"],
                "target_branches": ["origin/master"],
            },
        ],
    }
    dialog = NewDeploymentDialog(snapshot, on_refresh=lambda: None, show_prod=False)
    assert dialog.refresh_button.text() == "刷新项目 ⟳"
    assert dialog.last_refresh.text() == "项目最后刷新时间：20260808010203"
    dialog.set_refreshing(True)
    assert dialog.refresh_button.isEnabled() is True
    assert dialog.last_refresh.text() == "项目刷新中"
    assert dialog.refresh_status.indicator.timer.isActive() is True
    dialog.set_refreshing(False)
    node = dialog.environment_nodes[0]
    child = node.project_rows[0]
    assert node.environment.findData("prod") == -1
    assert child.project.isEnabled() is False
    assert child.branch.isEnabled() is False

    node.environment.setCurrentIndex(node.environment.findData("test"))
    assert child.project.isEnabled() is True
    assert child.project.count() == 2
    assert child.project.isEditable() is True
    assert child.project.lineEdit().placeholderText() == "选择项目，可输入名称搜索"
    assert child.project.completer().caseSensitivity() == Qt.CaseInsensitive
    assert child.project.completer().filterMode() == Qt.MatchContains
    child.project.completer().setCompletionPrefix("DTM_PC")
    assert child.project.completer().completionCount() == 1
    assert "dtm_pc" in child.project.completer().currentCompletion()
    child.project.setCurrentIndex(
        next(
            index
            for index in range(child.project.count())
            if isinstance(child.project.itemData(index), dict)
            and child.project.itemData(index)["full_name"] == "dtmzp/dtmzp_admin"
        )
    )
    assert child.branch.isEnabled() is True
    assert child.branch.isEditable() is True
    assert child.branch.lineEdit().placeholderText() == "选择分支，可输入名称搜索"
    assert [child.branch.itemData(index) for index in range(child.branch.count())] == [
        "origin/feature/a",
        "origin/master",
    ]

    child.branch.completer().setCompletionPrefix("FEATURE")
    assert child.branch.completer().completionCount() == 1
    assert child.branch.completer().currentCompletion() == "origin/feature/a"
    child.branch.setCurrentIndex(child.branch.findData("origin/master"))
    node.environment.setCurrentIndex(node.environment.findData("staging"))
    assert child.project.currentIndex() == -1
    assert child.branch.currentIndex() == -1
    assert child.project.count() == 1
    dialog.close()


def test_scheduled_deployment_controls_switch_input_and_limit_range(app: QApplication):
    dialog = NewDeploymentDialog(
        {"last_refreshed_at": "", "projects": []},
        on_refresh=lambda: None,
    )
    assert dialog.scheduled_deployment.text() == "定时部署"
    assert dialog.schedule_controls.isHidden() is True
    dialog.scheduled_deployment.setChecked(True)
    assert dialog.schedule_controls.isHidden() is False
    assert dialog.schedule_interval.isHidden() is False
    assert dialog.schedule_time.isHidden() is True
    assert [
        dialog.schedule_mode.itemText(index)
        for index in range(dialog.schedule_mode.count())
    ] == ["间隔", "时刻"]
    assert dialog.schedule_interval.suffix() == " 分钟"
    assert dialog.schedule_mode.width() >= 152
    assert {
        dialog.scheduled_deployment.height(),
        dialog.schedule_range.height(),
        dialog.schedule_mode.height(),
        dialog.schedule_interval.height(),
        dialog.schedule_time.height(),
    } == {40}

    dialog.schedule_mode.setCurrentIndex(
        dialog.schedule_mode.findData("daily_time")
    )
    assert dialog.schedule_interval.isHidden() is True
    assert dialog.schedule_time.isHidden() is False
    assert dialog.schedule_value()["mode"] == "daily_time"
    assert dialog.schedule_range.text() == (
        f"{dialog._schedule_start:%Y-%m-%d %H:%M} — "
        f"{dialog._schedule_end:%Y-%m-%d %H:%M}"
    )

    start = datetime.now().astimezone().replace(second=0, microsecond=0)
    range_dialog = ScheduleRangeDialog(
        start,
        start + timedelta(days=31),
        anchor=dialog.schedule_range,
        parent=dialog,
    )
    selected_start, selected_end = range_dialog.values()
    assert selected_end - selected_start <= timedelta(days=30)
    range_dialog.close()
    dialog.close()


def test_scheduled_deployment_controls_are_localized(app: QApplication):
    from windows_native.i18n import set_language

    expected = {
        "zh_TW": (
            ["間隔", "時刻"],
            " 分鐘",
            "選擇專案，可輸入名稱搜尋",
            "選擇分支，可輸入名稱搜尋",
        ),
        "en_US": (
            ["Interval", "Time"],
            " min",
            "Select or search projects by name",
            "Select or search branches by name",
        ),
    }
    try:
        for language, (
            labels,
            suffix,
            project_hint,
            branch_hint,
        ) in expected.items():
            set_language(language)
            dialog = NewDeploymentDialog(
                {"last_refreshed_at": "", "projects": []},
                on_refresh=lambda: None,
            )
            assert [
                dialog.schedule_mode.itemText(index)
                for index in range(dialog.schedule_mode.count())
            ] == labels
            assert dialog.schedule_interval.suffix() == suffix
            row = dialog.environment_nodes[0].project_rows[0]
            assert row.project.lineEdit().placeholderText() == project_hint
            assert row.branch.lineEdit().placeholderText() == branch_hint
            dialog.close()
    finally:
        set_language("zh_CN")


def test_single_deployment_selector_only_returns_one_complete_selection(app: QApplication):
    snapshot = {
        "last_refreshed_at": "2026-08-08T01:02:03+08:00",
        "projects": [
            {
                "full_name": "dtmzp/dtm_pc",
                "name": "dtm_pc",
                "description": "PC 前端",
                "eligible": True,
                "environments": ["test", "prod"],
                "target_branches": ["origin/master", "origin/feature/a"],
            }
        ],
    }
    dialog = SingleDeploymentDialog(
        snapshot,
        on_refresh=lambda: None,
        show_prod=False,
    )
    new_dialog = NewDeploymentDialog(
        snapshot,
        on_refresh=lambda: None,
        show_prod=False,
    )

    assert dialog.windowTitle() == "单点部署"
    assert dialog.width() == new_dialog.width() == 1040
    assert not hasattr(dialog, "iteration_name")
    assert dialog.environment.findData("prod") == -1
    dialog.environment.setCurrentIndex(dialog.environment.findData("test"))
    assert dialog.project.isEditable() is True
    assert dialog.project.completer().filterMode() == Qt.MatchContains
    dialog.project.setCurrentIndex(0)
    dialog.branch.setCurrentIndex(0)
    editor = dialog.project.lineEdit()
    editor.setFocus()
    editor.selectAll()
    QTest.keyClicks(editor, "DTM_PC")
    app.processEvents()
    assert dialog.project.currentIndex() == -1
    assert dialog.branch.currentIndex() == -1
    dialog.project.completer().setCompletionPrefix("DTM_PC")
    assert dialog.project.completer().completionCount() == 1
    dialog.project.completer().activated[str].emit(
        dialog.project.completer().currentCompletion()
    )
    app.processEvents()
    assert dialog.project.currentData()["name"] == "dtm_pc"
    assert dialog.branch.isEditable() is True
    dialog.branch.completer().setCompletionPrefix("FEATURE")
    assert dialog.branch.completer().completionCount() == 1
    dialog.branch.setCurrentIndex(dialog.branch.findData("origin/feature/a"))

    assert dialog.selections() == [
        {
            "environment": "test",
            "project_full_name": "dtmzp/dtm_pc",
            "project_name": "dtm_pc",
            "description": "PC 前端",
            "branch": "origin/feature/a",
        }
    ]
    dialog._validate_and_accept()
    assert dialog.result() == QDialog.Accepted
    dialog.close()
    new_dialog.close()


def test_hide_single_deployment_filters_only_explicit_single_type(workflow):
    window, _service, _manager, _theme = workflow
    page = window.quick_deploy_page
    service = window.deployment_service
    service.deployment_tasks = [
        {
            "task_id": "20260808010101",
            "iteration_name": "单点部署",
            "deployment_type": "single",
            "status": "completed",
            "items": [],
            "trashed": False,
        },
        {
            "task_id": "20260808010102",
            "iteration_name": "单点部署",
            "deployment_type": "iteration",
            "status": "completed",
            "items": [],
            "trashed": False,
        },
    ]

    page.hide_single_deployments.setChecked(True)
    page.refresh_tasks()

    assert page.task_table.table.rowCount() == 1
    assert page.task_table.table.item(0, 0).text() == "20260808010102"
    assert page.active_deployments.text() == "进行中：0"


def test_home_copy_and_task_table_headers(workflow):
    window, service, _manager, _theme = workflow
    home = window.home_page
    home._apply_document(service.get_document())
    assert home.open_output_button.text() == "打开钉钉输出目录"
    assert home.open_output_button.isEnabled()
    assert home.output_url.text() == "https://alidocs.dingtalk.com/folder/output"
    assert home.active_label.text() == "进行中：1"
    assert home.new_task_button.text() == "新建生成任务"
    assert home.config_button.text() == "修改配置"
    assert home.recycle_button.text() == "回收站"

    table = home.task_table.table
    assert [table.horizontalHeaderItem(column).text() for column in range(5)] == [
        "任务 ID",
        "任务名称",
        "进度",
        "状态",
        "操作",
    ]
    assert table.rowCount() == 1
    assert table.item(0, 0).text() == LIVE_TASK["task_id"]
    assert table.cellWidget(0, 1).text() == LIVE_TASK["name"]
    action_texts = [
        button.text() for button in table.cellWidget(0, 4).findChildren(type(home.new_task_button))
    ]
    assert action_texts == ["停止", "检测恢复", "删除"]

    terminal = copy.deepcopy(LIVE_TASK)
    terminal["status"] = "stopped"
    home.task_table.set_tasks([terminal])
    terminal_actions = [
        button.text()
        for button in home.task_table.table.cellWidget(0, 4).findChildren(QPushButton)
    ]
    assert terminal_actions == ["重新生成", "检测恢复", "删除"]


def test_window_branding_is_compact_and_language_switches_immediately(workflow):
    window, _service, _manager, _theme = workflow
    assert window.windowTitle() == "ForTest"
    assert not window.findChildren(QLabel, "brandIcon")
    assert any(label.text() == "ForTest v0.2.13" for label in window.findChildren(QLabel))

    window.change_language("en_US")
    assert [window.nav_group.button(index).text() for index in range(4)] == [
        "Quick Deploy",
        "Test Case Generation",
        "Settings",
        "Appearance",
    ]
    assert window.guide_button.text().endswith("Complete Setup")
    window.change_language("zh_CN")


def test_language_switch_translates_dynamic_status_and_placeholders(workflow):
    """带运行参数的状态和输入占位提示也必须支持即时双向翻译。"""

    window, _service, _manager, _theme = workflow
    window.settings_page.ai_panel.status.setText("共 7 条 AI 配置")

    window.change_language("en_US")
    assert window.settings_page.ai_panel.status.text() == "7 AI configurations"
    assert window.settings_page.ai_panel.add_button.text() == "Add Configuration"
    quick_texts = [label.text() for label in window.quick_deploy_page.findChildren(QLabel)]
    assert "Configure a Jenkins API Token first" in quick_texts
    assert window.quick_deploy_page.guide_username_hint.text() == "User login account"
    assert window.quick_deploy_page.single_deployment_button.text() == "Single Deployment"
    assert window.quick_deploy_page.hide_single_deployments.text() == (
        "Hide Single Deployments"
    )
    assert window.quick_deploy_page.scheduled_deployments.text() == "Scheduled: 0"
    assert [
        window.quick_deploy_page.task_table.table.horizontalHeaderItem(index).text()
        for index in range(5)
    ] == ["Task ID", "Iteration Name", "Progress", "Status", "Actions"]
    assert [
        window.deployment_config_page.tabs.tabText(index)
        for index in range(window.deployment_config_page.tabs.count())
    ] == ["Jenkins Settings", "Tasks"]

    window.change_language("zh_CN")
    assert window.settings_page.ai_panel.status.text() == "共 7 条 AI 配置"
    assert window.settings_page.ai_panel.add_button.text() == "新增配置"
    assert window.quick_deploy_page.guide_username_hint.text() == "用户登陆账号"
    assert window.quick_deploy_page.single_deployment_button.text() == "单点部署"
    assert window.quick_deploy_page.scheduled_deployments.text() == "定时任务：0"


def test_traditional_language_can_switch_away_without_restart(workflow):
    """繁中回到英文及简中时，配置页标签和选项必须立即重译。"""

    window, _service, _manager, _theme = workflow
    window.change_language("zh_TW")
    assert window.quick_deploy_page.guide_username_hint.text() == "使用者登入帳號"
    assert window.quick_deploy_page.single_deployment_button.text() == "單點部署"
    assert window.quick_deploy_page.scheduled_deployments.text() == "定時任務：0"
    assert window.config_page.tabs.tabText(0) == "文件設定"
    assert window.config_page.prompt_page.tabs.tabText(0) == "圖片理解"
    assert window.settings_page.tabs.tabText(0) == "AI 設定"
    assert window.settings_page.ai_panel.add_button.text() == "新增設定"
    assert window.settings_page.other_panel.start_with_windows.text() == (
        "隨 Windows 開機啟動"
    )
    assert [
        window.settings_page.other_panel.close_behavior.itemText(index)
        for index in range(3)
    ] == ["每次詢問", "最小化到系統匣", "直接結束程式"]
    assert (
        window.config_page.document_page.document_mcp.placeholderText()
        == "留空表示保留已儲存值"
    )
    assert window.config_page.document_page.clear_document.text() == (
        "清除已儲存的文件 MCP"
    )

    window.change_language("en_US")
    assert window.config_page.tabs.tabText(0) == "Document"
    assert window.config_page.prompt_page.tabs.tabText(0) == "Image Understanding"
    assert window.settings_page.tabs.tabText(0) == "AI Settings"
    assert window.settings_page.ai_panel.add_button.text() == "Add Configuration"
    assert window.settings_page.other_panel.start_with_windows.text() == (
        "Start with Windows"
    )
    assert [
        window.settings_page.other_panel.close_behavior.itemText(index)
        for index in range(3)
    ] == ["Ask Every Time", "Minimize to System Tray", "Exit the Application"]

    window.change_language("zh_CN")
    assert window.config_page.tabs.tabText(0) == "文档配置"
    assert window.config_page.prompt_page.tabs.tabText(0) == "图片理解"
    assert window.settings_page.tabs.tabText(0) == "AI 配置"
    assert window.settings_page.ai_panel.add_button.text() == "新增配置"
    assert (
        window.config_page.document_page.document_mcp.placeholderText()
        == "留空表示保留已保存值"
    )


def test_identical_task_snapshot_preserves_cell_widget_and_focus(
    workflow,
    app: QApplication,
):
    window, _service, manager, _theme = workflow
    task_table = window.home_page.task_table
    name_button = task_table.table.cellWidget(0, 1)
    name_button.setFocus(Qt.OtherFocusReason)
    app.processEvents()
    assert app.focusWidget() is name_button

    task_table.set_tasks(manager.list_tasks(include_trashed=False))
    app.processEvents()
    assert task_table.table.cellWidget(0, 1) is name_button
    assert app.focusWidget() is name_button


def test_recycle_page_only_displays_trashed_tasks(workflow):
    window, _service, _manager, _theme = workflow
    window.show_recycle()
    table = window.recycle_page.table.table
    assert table.rowCount() == 1
    assert table.item(0, 0).text() == TRASHED_TASK["task_id"]
    assert table.cellWidget(0, 1).text() == TRASHED_TASK["name"]
    restore_buttons = table.cellWidget(0, 4).findChildren(type(window.home_page.new_task_button))
    assert [button.text() for button in restore_buttons] == ["恢复"]


def test_appearance_has_exactly_three_choices(workflow):
    window, _service, _manager, _theme = workflow
    combo = window.appearance_page.mode_combo
    assert [combo.itemText(index) for index in range(combo.count())] == [
        "跟随系统",
        "浅色",
        "深色",
    ]
    assert [combo.itemData(index) for index in range(combo.count())] == [
        "system",
        "light",
        "dark",
    ]


def test_requested_config_labels_and_ai_capability_controls(workflow):
    window, service, _manager, _theme = workflow
    view = service.get_ai_configurations()
    dialog = AIConfigurationDialog(
        view["providers"],
        service.get_local_codex_runtime_catalog(),
    )
    assert [
        dialog.speed.itemData(index) for index in range(dialog.speed.count())
    ] == [
        "standard",
        "fast",
    ]
    assert not hasattr(window.config_page.task_panel, "node_id")
    labels = [label.text() for label in window.config_page.document_page.findChildren(QLabel)]
    assert "输出文件夹" in labels
    assert "远端输出文件夹" not in labels
    assert [
        window.config_page.prompt_page.tabs.tabText(index)
        for index in range(window.config_page.prompt_page.tabs.count())
    ][-2:] == ["用例生成 · 角色", "用例生成 · 执行"]
    assert dialog.cli_refresh.text() == "刷新"
    assert not hasattr(dialog, "sdk_version")
    dialog.close()


def test_non_codex_reasoning_controls_follow_documented_model_capabilities(
    workflow,
):
    _window, service, _manager, _theme = workflow
    dialog = AIConfigurationDialog(
        provider_specs_view(),
        service.get_local_codex_runtime_catalog(),
    )

    for provider in ("minimax", "kimi"):
        dialog.provider.setCurrentIndex(dialog.provider.findData(provider))
        assert dialog.reasoning.isHidden()
        assert dialog.speed.isHidden()

    dialog.provider.setCurrentIndex(dialog.provider.findData("deepseek"))
    assert not dialog.reasoning.isHidden()
    assert [
        dialog.reasoning.itemData(index)
        for index in range(dialog.reasoning.count())
    ] == ["high", "max"]
    assert dialog.speed.isHidden()

    dialog.provider.setCurrentIndex(dialog.provider.findData("qwen"))
    assert dialog.reasoning.isHidden()
    dialog.model.setEditText("qwen3.8-max-preview")
    assert not dialog.reasoning.isHidden()
    assert [
        dialog.reasoning.itemData(index)
        for index in range(dialog.reasoning.count())
    ] == ["none", "low", "medium", "xhigh"]

    dialog.provider.setCurrentIndex(dialog.provider.findData("doubao"))
    assert not dialog.reasoning.isHidden()
    assert not dialog.speed.isHidden()

    dialog.provider.setCurrentIndex(dialog.provider.findData("wenxin"))
    assert dialog.reasoning.isHidden()
    dialog.model.setEditText("deepseek-v4-pro")
    assert not dialog.reasoning.isHidden()

    dialog.provider.setCurrentIndex(dialog.provider.findData("hunyuan"))
    assert not dialog.reasoning.isHidden()
    assert dialog.speed.isHidden()
    dialog.close()


def test_settings_page_lists_orders_and_recycles_ai_configurations(
    workflow,
    app: QApplication,
):
    window, service, _manager, _theme = workflow
    service.save_ai_configuration(
        {
            "provider": "codex",
            "name": "Codex 主模型",
            "model": "gpt-5.6-terra",
            "codex_cli_source": "builtin",
            "codex_cli_version": "bundled",
        }
    )
    service.save_ai_configuration(
        {
            "provider": "openai",
            "name": "OpenAI 备用",
            "model": "gpt-5.6-terra",
            "base_url": "https://api.openai.com/v1",
            "api_key": "test-key",
        }
    )
    service.ai_configurations[0]["status_detail"] = "CLI 登录态与模型目录均可用"

    window.switch_page(2)
    _drain_workers(app)
    panel = window.settings_page.ai_panel

    assert [
        window.settings_page.tabs.tabText(index)
        for index in range(window.settings_page.tabs.count())
    ] == ["AI 配置", "其他"]
    assert panel.table.rowCount() == 2
    assert panel.table.rowHeight(0) >= 56
    assert panel.table.rowHeight(1) >= 56
    assert panel.table.showGrid() is False
    assert panel.table.focusPolicy() == Qt.NoFocus
    assert panel.table.columnWidth(0) >= 116
    assert panel.table.columnWidth(3) >= 128
    assert panel.table.columnWidth(4) >= 244
    assert panel.table.item(0, 1).text() == "Codex 主模型"
    assert panel.table.item(1, 2).text() == "OpenAI · gpt-5.6-terra"
    # 模型列保持单行时，悬停内容仍须与完整展示值严格一致。
    for row, configuration in enumerate(service.ai_configurations):
        model_item = panel.table.item(row, 2)
        assert model_item.toolTip() == configuration["display_model"]
    # 状态项同时水平、垂直居中，并继续保留已有的详情提示。
    for row in range(panel.table.rowCount()):
        alignment = panel.table.item(row, 3).textAlignment()
        assert alignment & int(Qt.AlignHCenter)
        assert alignment & int(Qt.AlignVCenter)
    assert panel.table.item(0, 3).toolTip() == "CLI 登录态与模型目录均可用"
    assert [button.isEnabled() for button, _available in panel._order_buttons] == [
        False,
        True,
        True,
        False,
    ]

    panel._move(1, -1)
    _drain_workers(app)
    assert panel.table.item(0, 1).text() == "OpenAI 备用"

    deleted_id = service.ai_configurations[0]["id"]
    panel.apply_view(service.delete_ai_configuration(deleted_id))
    panel._open_recycle()
    assert panel._recycle_dialog is not None
    assert panel._recycle_dialog.table.rowCount() == 1
    assert panel._recycle_dialog.table.showGrid() is False
    assert panel._recycle_dialog.table.focusPolicy() == Qt.NoFocus
    assert panel._recycle_dialog.table.columnWidth(2) >= 220
    assert panel._recycle_dialog.table.rowHeight(0) >= 60
    panel._recycle_dialog.close()


def test_ai_dialog_keeps_actions_visible_and_scrolls_form_on_short_screens(
    workflow,
    app: QApplication,
):
    """缩短弹窗后只滚动表单，保存和取消按钮仍固定在可视区域。"""

    _window, service, _manager, _theme = workflow
    view = service.get_ai_configurations()
    dialog = AIConfigurationDialog(
        view["providers"],
        service.get_local_codex_runtime_catalog(),
    )
    dialog.resize(650, 400)
    dialog.show()
    app.processEvents()

    assert dialog.scroll_area.widget() is dialog.form_container
    dialog.form_container.setMinimumHeight(
        dialog.scroll_area.viewport().height() + 180
    )
    app.processEvents()
    assert dialog.scroll_area.verticalScrollBar().maximum() > 0
    assert dialog.buttons.isVisible()
    assert dialog.buttons.geometry().bottom() <= dialog.contentsRect().bottom()
    assert dialog.api_key.isHidden()
    assert dialog.clear_api_key.isHidden()
    dialog.close()


def test_codex_dialog_uses_required_field_order_and_refreshable_model_combo(
    workflow,
):
    _window, service, _manager, _theme = workflow
    dialog = AIConfigurationDialog(
        service.get_ai_configurations()["providers"],
        service.get_local_codex_runtime_catalog(),
    )

    fields = [
        dialog.name,
        dialog.provider,
        dialog.cli_source,
        dialog.path_control,
        dialog.version_control,
        dialog.model_control,
        dialog.reasoning,
        dialog.speed,
        dialog.max_concurrency,
        dialog.timeout,
    ]
    assert [dialog.form.labelForField(field).text() for field in fields] == [
        "配置名称",
        "厂商",
        "CLI 来源",
        "CLI 路径",
        "CLI 版本",
        "模型",
        "推理强度",
        "推理速度",
        "最大并发",
        "超时（秒）",
    ]
    assert dialog.model_refresh.text() == "刷新模型"
    assert dialog.cli_path.text().endswith("codex.exe")
    assert dialog.timeout.value() == 900

    dialog.provider.setCurrentIndex(dialog.provider.findData("openai"))
    assert dialog.timeout.value() == 300
    dialog.provider.setCurrentIndex(dialog.provider.findData("codex"))
    assert dialog.timeout.value() == 900

    dialog.apply_model_catalog(service.get_codex_configuration_models({}))
    assert dialog.model.findData("gpt-5.6-sol") >= 0
    dialog.model.setCurrentIndex(dialog.model.findData("gpt-5.6-sol"))
    assert dialog.payload()["model"] == "gpt-5.6-sol"
    dialog.close()


def test_ai_configuration_dialog_uses_one_codex_cli_source_control(workflow):
    _window, service, _manager, _theme = workflow
    view = service.get_ai_configurations()
    dialog = AIConfigurationDialog(
        view["providers"],
        service.get_local_codex_runtime_catalog(),
    )

    assert dialog.cli_source.count() == 2
    assert [dialog.cli_source.itemData(index) for index in range(2)] == [
        "builtin",
        "custom",
    ]
    assert dialog.cli_version.count() == 2
    assert not hasattr(dialog, "sdk_version")
    _set_index = dialog.provider.findData("openai")
    dialog.provider.setCurrentIndex(_set_index)
    assert dialog.cli_source.isHidden()
    assert dialog.path_control.isHidden()
    assert dialog.version_control.isHidden()
    assert not dialog.api_key.isHidden()
    assert dialog.base_url.text() == "https://api.openai.com/v1"
    dialog.close()


def test_other_settings_loads_and_saves_native_application_preferences(
    workflow,
    app: QApplication,
):
    window, service, _manager, _theme = workflow
    window.switch_page(2)
    window.settings_page.tabs.setCurrentIndex(1)
    _drain_workers(app)
    panel = window.settings_page.other_panel

    assert not panel.start_with_windows.isChecked()
    assert panel.close_behavior.currentData() == "ask"
    panel.start_with_windows.setChecked(True)
    panel.close_behavior.setCurrentIndex(
        panel.close_behavior.findData("minimize")
    )
    panel.save_application_preferences()
    _drain_workers(app)
    assert service.application_preferences == {
        "start_with_windows": True,
        "close_behavior": "minimize",
    }


def test_other_settings_only_loads_and_saves_native_update_preferences(
    workflow,
    app: QApplication,
):
    window, service, _manager, _theme = workflow
    window.switch_page(2)
    window.settings_page.tabs.setCurrentIndex(1)
    _drain_workers(app)
    panel = window.settings_page.other_panel

    assert not hasattr(panel, "api_host")
    assert not hasattr(panel, "api_port")
    assert not hasattr(panel, "frontend_port")
    assert "其他设置" not in [label.text() for label in panel.findChildren(QLabel)]

    panel.update_enabled.setChecked(False)
    panel.update_channel.setCurrentIndex(
        panel.update_channel.findData("beta")
    )
    panel.update_manifest.setText("https://updates.example.test/manifest.json")
    panel.save_update_preferences()
    _drain_workers(app)
    assert service.update_preferences == {
        "enabled": False,
        "channel": "beta",
        "manifest_url": "https://updates.example.test/manifest.json",
    }


def test_prompt_steps_save_ordered_custom_model_policy(workflow, app: QApplication):
    window, service, _manager, _theme = workflow
    first = service.save_ai_configuration(
        {
            "provider": "codex",
            "name": "模型 A",
            "model": "gpt-5.6-terra",
            "codex_cli_source": "builtin",
            "codex_cli_version": "bundled",
        }
    )["configurations"][0]
    second = service.save_ai_configuration(
        {
            "provider": "openai",
            "name": "模型 B",
            "model": "gpt-5.6-terra",
            "base_url": "https://api.openai.com/v1",
            "api_key": "key-b",
        }
    )["configurations"][1]

    window.show_config()
    _drain_workers(app)
    editor = window.config_page.prompt_page.editors["component_matching"].model_policy
    editor.mode.setCurrentIndex(editor.mode.findData("custom"))
    editor._append(first["id"])
    editor._append(second["id"])
    editor._move(1, -1)
    editor.save()
    _drain_workers(app)

    assert service.model_policies["component_matching"] == {
        "mode": "custom",
        "configuration_ids": [second["id"], first["id"]],
    }
    role_policy = window.config_page.prompt_page.editors[
        "case_generation_system"
    ].model_policy
    execution_policy = window.config_page.prompt_page.editors[
        "case_generation_user"
    ].model_policy
    assert role_policy.stage == execution_policy.stage == "case_generation"


def test_codex_configuration_dialog_serializes_one_runtime_selection(workflow):
    _window, service, _manager, _theme = workflow
    view = service.get_ai_configurations()
    dialog = AIConfigurationDialog(
        view["providers"],
        service.get_local_codex_runtime_catalog(),
    )

    assert dialog.cli_version.count() == 2
    assert "0.147.0" in dialog.cli_version.itemText(0)
    assert dialog.cli_version.findData("0.147.0") >= 0
    dialog.cli_version.setCurrentIndex(dialog.cli_version.findData("0.147.0"))
    assert dialog.payload()["codex_cli_version"] == "0.147.0"

    dialog.cli_source.setCurrentIndex(dialog.cli_source.findData("custom"))
    assert not dialog.version_control.isHidden()
    assert not dialog.cli_version.isEnabled()
    assert not dialog.path_control.isHidden()
    assert not dialog.cli_path.isReadOnly()
    dialog.close()


def test_edit_dialog_runtime_refresh_keeps_users_current_cli_version(workflow):
    """编辑旧配置时刷新版本目录，不得覆盖用户刚选中的 CLI 版本。"""

    _window, service, _manager, _theme = workflow
    configuration = {
        "id": "codex-old-runtime",
        "provider": "codex",
        "name": "Codex 旧配置",
        "model": "gpt-5.6-terra",
        "codex_cli_source": "builtin",
        "codex_cli_version": "bundled",
    }
    dialog = AIConfigurationDialog(
        service.get_ai_configurations()["providers"],
        service.get_local_codex_runtime_catalog(),
        configuration,
    )
    requests: list[tuple[str, str]] = []
    dialog.cli_version_change_requested.connect(
        lambda selected, previous: requests.append((selected, previous))
    )
    selected = "0.147.0"
    dialog.cli_version.setCurrentIndex(dialog.cli_version.findData(selected))
    refreshed = service.get_codex_runtime_catalog(refresh=True)
    refreshed_path = "C:/ForTest/refreshed/codex-0.147.0.exe"
    refreshed["versions"][0]["path"] = refreshed_path

    dialog.apply_runtime_catalog(refreshed)

    assert dialog.cli_version.currentData() == selected
    assert dialog.cli_path.text() == refreshed_path
    # 目录程序化回填不能被误判为用户激活，避免重复下载安装。
    assert requests == []
    dialog.close()


def test_user_cli_version_activation_emits_new_and_previous_selection(workflow):
    """只有用户真实激活新版本时，才发出包含新旧版本的切换请求。"""

    _window, service, _manager, _theme = workflow
    configuration = {
        "id": "codex-version-signal",
        "provider": "codex",
        "name": "Codex 版本信号",
        "model": "gpt-5.6-terra",
        "codex_cli_source": "builtin",
        "codex_cli_version": "bundled",
    }
    dialog = AIConfigurationDialog(
        service.get_ai_configurations()["providers"],
        service.get_local_codex_runtime_catalog(),
        configuration,
    )
    requests: list[tuple[str, str]] = []
    dialog.cli_version_change_requested.connect(
        lambda selected, previous: requests.append((selected, previous))
    )
    target_index = dialog.cli_version.findData("0.147.0")

    # currentIndexChanged 也用于初始化和目录回填，本身不得触发安装请求。
    dialog.cli_version.setCurrentIndex(target_index)
    assert requests == []
    dialog.cli_version.activated.emit(target_index)

    assert requests == [("0.147.0", "bundled")]
    dialog.set_cli_operation_busy(True)
    dialog.cli_version.activated.emit(target_index)
    assert requests == [("0.147.0", "bundled")]
    dialog.close()


def test_panel_switches_cli_then_refreshes_models_from_same_version(workflow):
    """切换成功后活动版本、路径、控件和模型刷新使用同一 CLI。"""

    _window, service, _manager, _theme = workflow
    host = DeferredAsyncHost()
    panel = AIConfigurationsPanel(service, host)
    dialog = AIConfigurationDialog(
        service.get_ai_configurations()["providers"],
        service.get_local_codex_runtime_catalog(),
    )
    selected = "0.147.0"
    selected_path = "C:/ForTest/runtimes/codex/packages/0.147.0/codex.exe"
    dialog.cli_version.setCurrentIndex(dialog.cli_version.findData(selected))

    panel._switch_editor_cli_version(dialog, selected, "bundled")

    locked_controls = (
        dialog.cli_source,
        dialog.cli_version,
        dialog.cli_refresh,
        dialog.model_refresh,
        dialog.buttons.button(QDialogButtonBox.Save),
        dialog.buttons.button(QDialogButtonBox.Cancel),
    )
    assert len(host.calls) == 1
    assert service.codex_runtime_state == "bundled"
    assert all(not control.isEnabled() for control in locked_controls)
    assert selected in dialog.form_status.text()
    # 下载过程中取消、Esc 和关闭按钮都不能销毁仍有异步回调的弹窗。
    rejections: list[bool] = []
    dialog.rejected.connect(lambda: rejections.append(True))
    dialog.reject()
    assert rejections == []

    host.complete_next()

    assert service.codex_runtime_state == selected
    assert dialog.cli_version.currentData() == selected
    assert dialog.cli_path.text() == selected_path
    assert dialog._confirmed_cli_version == selected
    assert all(control.isEnabled() for control in locked_controls)

    model_requests: list[dict] = []

    def load_models(values: dict) -> dict:
        # 服务执行模型查询时，应用级活动运行时也必须仍是同一版本。
        assert service.codex_runtime_state == selected
        assert service.get_codex_runtime_status()["runtime"]["path"] == selected_path
        model_requests.append(copy.deepcopy(values))
        return {
            "models": [{"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol"}],
            "default_model": "gpt-5.6-sol",
        }

    service.get_codex_configuration_models = load_models
    panel._refresh_editor_models(dialog)
    assert all(not control.isEnabled() for control in locked_controls)
    host.complete_next()

    assert model_requests[0]["codex_cli_version"] == selected
    # 内置来源按版本解析活动路径，不把只读展示路径误存为自定义路径。
    assert model_requests[0]["codex_cli_path"] is None
    assert service.codex_runtime_state == selected
    assert dialog.model.findData("gpt-5.6-sol") >= 0
    assert all(control.isEnabled() for control in locked_controls)
    dialog.close()
    panel.close()


def test_panel_restores_previous_cli_version_when_switch_fails(workflow):
    """下载或校验失败时恢复已确认版本、路径和可操作状态。"""

    _window, service, _manager, _theme = workflow
    host = DeferredAsyncHost()
    panel = AIConfigurationsPanel(service, host)
    dialog = AIConfigurationDialog(
        service.get_ai_configurations()["providers"],
        service.get_local_codex_runtime_catalog(),
    )
    dialog.cli_version.setCurrentIndex(dialog.cli_version.findData("0.147.0"))

    def fail_install(_selection: str) -> dict:
        raise RuntimeError("下载校验失败")

    service.install_codex_runtime = fail_install
    panel._switch_editor_cli_version(dialog, "0.147.0", "bundled")
    host.complete_next()

    assert service.codex_runtime_state == "bundled"
    assert dialog.cli_version.currentData() == "bundled"
    assert dialog.cli_path.text() == "C:/ForTest/bundled/codex.exe"
    assert dialog._confirmed_cli_version == "bundled"
    assert dialog.cli_source.isEnabled()
    assert dialog.cli_version.isEnabled()
    assert dialog.cli_refresh.isEnabled()
    assert dialog.model_refresh.isEnabled()
    assert dialog.buttons.button(QDialogButtonBox.Save).isEnabled()
    assert dialog.buttons.button(QDialogButtonBox.Cancel).isEnabled()
    assert dialog.form_status.text() == "Codex 版本操作失败：下载校验失败"
    dialog.close()
    panel.close()


def test_cli_switch_feedback_supports_traditional_chinese_and_english():
    """CLI 切换进度、成功和失败反馈必须覆盖应用支持的全部语言。"""

    from windows_native.i18n import set_language, tr

    try:
        set_language("zh_TW")
        assert tr(
            "正在下载、校验并切换 Codex CLI {version}…",
            version="0.147.0",
        ) == "正在下載、驗證並切換 Codex CLI 0.147.0…"
        assert "已切換到 0.147.0" in tr(
            "Codex CLI 已切换到 {version}，后续新任务将统一使用该版本",
            version="0.147.0",
        )

        set_language("en_US")
        assert tr(
            "正在下载、校验并切换 Codex CLI {version}…",
            version="0.147.0",
        ) == "Downloading, verifying, and switching Codex CLI 0.147.0…"
        assert tr(
            "Codex 版本操作失败：{message}",
            message="download failed",
        ) == "Codex version operation failed: download failed"
    finally:
        set_language("zh_CN")


def test_ai_payload_saves_model_ids_instead_of_display_labels(workflow):
    """动态模型目录显示友好名称时，持久化值仍必须是可执行的真实 ID。"""

    _window, service, _manager, _theme = workflow
    view = service.get_ai_configurations()
    dialog = AIConfigurationDialog(
        view["providers"],
        service.get_local_codex_runtime_catalog(),
    )
    dialog.model.setEditText("gpt-5.6-sol")
    assert dialog.payload()["model"] == "gpt-5.6-sol"

    dialog.provider.setCurrentIndex(dialog.provider.findData("openai"))
    dialog.model.setEditText("gpt-5.6-terra")
    payload = dialog.payload()
    assert payload["provider"] == "openai"
    assert payload["model"] == "gpt-5.6-terra"
    dialog.close()


def test_numeric_settings_ignore_wheel_but_allow_manual_edit(workflow):
    """数值框上滚动页面不能误改配置，明确输入仍应立即生效。"""

    window, service, _manager, _theme = workflow
    view = service.get_ai_configurations()
    dialog = AIConfigurationDialog(
        view["providers"],
        service.get_local_codex_runtime_catalog(),
    )
    controls = [
        dialog.timeout,
        dialog.max_concurrency,
        window.config_page.task_panel.max_parallel,
    ]
    assert all(isinstance(control, ManualSpinBox) for control in controls)

    dialog.max_concurrency.setValue(2)
    wheel = QWheelEvent(
        QPointF(4, 4),
        QPointF(4, 4),
        QPoint(),
        QPoint(0, 120),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.ScrollUpdate,
        False,
    )
    dialog.max_concurrency.wheelEvent(wheel)
    assert dialog.max_concurrency.value() == 2
    assert wheel.isAccepted() is False

    dialog.max_concurrency.lineEdit().setText("3")
    dialog.max_concurrency.interpretText()
    assert dialog.max_concurrency.value() == 3
    dialog.close()


def test_ai_fast_speed_survives_save_and_catalog_refresh(workflow):
    """模型目录清空信号和保存回显都不能把快速档误重置为标准档。"""

    _window, service, _manager, _theme = workflow
    configuration = {
        "id": "codex-fast",
        "provider": "codex",
        "name": "Codex Fast",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "inference_speed": "fast",
        "timeout_seconds": 900,
        "max_concurrency": 1,
        "codex_cli_source": "builtin",
        "codex_cli_version": "bundled",
    }
    dialog = AIConfigurationDialog(
        service.get_ai_configurations()["providers"],
        service.get_local_codex_runtime_catalog(),
        configuration,
    )
    assert dialog.speed.currentData() == "fast"

    dialog.apply_runtime_catalog(service.get_codex_runtime_catalog(refresh=True))
    assert dialog.speed.currentData() == "fast"
    assert dialog.payload()["inference_speed"] == "fast"
    dialog.close()


def test_model_values_are_localized_in_details_and_logs(workflow, app: QApplication):
    window, _service, manager, _theme = workflow
    task = copy.deepcopy(LIVE_TASK)
    task["model_info"] = {
        "model_name": "Codex",
        "model_version": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "inference_speed": "fast",
        "runtime": "sdk",
    }
    task["logs"] = ["推理强度：high", "推理速度：fast"]
    task["log_entries"] = []
    manager.tasks[0] = copy.deepcopy(task)

    details = TaskDetailsDialog(task, window)
    details_text = "\n".join(label.text() for label in details.findChildren(QLabel))
    assert "推理强度：高" in details_text
    assert "推理速度：快速" in details_text
    details.close()

    logs = TaskLogsDialog(manager, task["task_id"], window)
    app.processEvents()
    assert "推理强度：高" in logs.logs.toPlainText()
    assert "推理速度：快速" in logs.logs.toPlainText()
    logs.close()


def test_combo_and_tabs_ignore_mouse_wheel(workflow):
    window, _service, _manager, _theme = workflow

    class FakeWheel:
        def __init__(self):
            self.ignored = False

        def ignore(self):
            self.ignored = True

    combo = window.appearance_page.mode_combo
    tabs = window.config_page.tabs
    assert isinstance(combo, SmoothComboBox)
    assert isinstance(tabs, SmoothTabWidget)
    combo_index = combo.currentIndex()
    tab_index = tabs.currentIndex()
    combo_event = FakeWheel()
    tab_event = FakeWheel()
    combo.wheelEvent(combo_event)
    tabs.tabBar().wheelEvent(tab_event)
    assert combo_event.ignored and combo.currentIndex() == combo_index
    assert tab_event.ignored and tabs.currentIndex() == tab_index


def test_base_page_without_description_has_no_empty_label(app: QApplication):
    page = BasePage("只有标题")
    labels = page.findChildren(QLabel)
    assert [label.text() for label in labels] == ["只有标题"]
    assert not [label for label in labels if label.objectName() == "pageDescription"]
    page.close()
