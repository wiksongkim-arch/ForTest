"""主窗口托盘与关闭生命周期的离屏回归测试。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QIcon, QPixmap
from PySide6.QtWidgets import QApplication

from windows_native.tests.test_ui_workflow import (
    FakeDeploymentService,
    FakeService,
    FakeTaskManager,
    FakeThemeManager,
)
from windows_native.i18n import set_language
from windows_native.ui.main_window import MainWindow


class _ClosePreferences:
    """只向关闭事件提供当前测试需要的稳定偏好。"""

    def __init__(self, behavior: str):
        self.behavior = behavior

    def get_close_behavior(self) -> str:
        return self.behavior


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _application_icon() -> QIcon:
    """构造无需磁盘资源的有效图标，验证托盘复用注入图标。"""

    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.blue)
    return QIcon(pixmap)


def _create_window(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tray_available: bool,
) -> tuple[MainWindow, FakeTaskManager, FakeDeploymentService, QIcon]:
    monkeypatch.setattr(
        MainWindow,
        "_system_tray_available",
        lambda _self: tray_available,
    )
    manager = FakeTaskManager()
    deployment = FakeDeploymentService()
    icon = _application_icon()
    window = MainWindow(
        FakeService(),
        icon,
        manager,
        FakeThemeManager(),
        deployment,
        onboarding_enabled=False,
        background_refresh_enabled=False,
        tray_enabled=True,
    )
    return window, manager, deployment, icon


def _force_cleanup(window: MainWindow, app: QApplication) -> None:
    """测试结束统一走强制退出分支，避免离屏托盘对象跨用例残留。"""

    window._quit_requested = True
    window.close()
    app.processEvents()


def test_tray_minimize_keeps_tasks_running_and_can_restore_window(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
):
    window, manager, deployment, icon = _create_window(
        monkeypatch,
        tray_available=True,
    )
    try:
        assert window._tray_icon is not None
        assert window._tray_icon.icon().cacheKey() == icon.cacheKey()
        assert window._tray_show_action is not None
        assert window._tray_show_action.text() == "显示主窗口"
        assert window._tray_quit_action is not None
        assert window._tray_quit_action.text() == "退出程序"

        # 菜单“显示主窗口”必须既显示窗口，也恢复其激活入口。
        window.hide()
        window._tray_show_action.trigger()
        app.processEvents()
        assert window.isVisible()

        window.preferences = _ClosePreferences("ask")
        monkeypatch.setattr(window, "_ask_close_behavior", lambda: "minimize")
        closed = window.close()
        app.processEvents()

        assert closed is False
        assert not window.isVisible()
        assert manager.stopped == 0
        assert deployment.stopped == 0
        assert window._shutdown_complete is False
    finally:
        _force_cleanup(window, app)


def test_quit_cleanup_is_idempotent(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
):
    window, manager, deployment, _icon = _create_window(
        monkeypatch,
        tray_available=True,
    )
    shutdown_calls: list[bool] = []
    monkeypatch.setattr(
        window.quick_deploy_page,
        "shutdown",
        lambda: shutdown_calls.append(True),
    )
    window.preferences = _ClosePreferences("quit")

    assert window._tray_quit_action is not None
    window._tray_quit_action.trigger()
    app.processEvents()
    repeated_event = QCloseEvent()
    window.closeEvent(repeated_event)

    assert repeated_event.isAccepted()
    assert shutdown_calls == [True]
    assert manager.stopped == 1
    assert deployment.stopped == 1
    assert window._tray_icon is not None
    assert not window._tray_icon.isVisible()
    _force_cleanup(window, app)


def test_unavailable_tray_falls_back_to_safe_quit(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
):
    window, manager, deployment, _icon = _create_window(
        monkeypatch,
        tray_available=False,
    )
    hidden: list[bool] = []
    monkeypatch.setattr(window, "hide", lambda: hidden.append(True))
    window.preferences = _ClosePreferences("minimize")

    event = QCloseEvent()
    window.closeEvent(event)

    assert window._tray_icon is None
    assert event.isAccepted()
    assert hidden == []
    assert manager.stopped == 1
    assert deployment.stopped == 1
    assert window._shutdown_complete is True
    _force_cleanup(window, app)


def test_tray_menu_supports_all_interface_languages(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
):
    window, _manager, _deployment, _icon = _create_window(
        monkeypatch,
        tray_available=True,
    )
    try:
        set_language("zh_TW")
        window._retranslate_system_tray()
        assert window._tray_show_action.text() == "顯示主視窗"
        assert window._tray_quit_action.text() == "結束程式"

        set_language("en_US")
        window._retranslate_system_tray()
        assert window._tray_show_action.text() == "Show Main Window"
        assert window._tray_quit_action.text() == "Exit"
    finally:
        set_language("zh_CN")
        _force_cleanup(window, app)
