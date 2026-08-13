"""品牌启动页和非阻塞后台等待测试。"""

from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from windows_native.ui.startup_splash import (
    StartupLoader,
    StartupSplash,
    wait_for_readiness,
    wait_for_startup,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    """复用当前进程的 Qt 应用，避免测试间重复创建全局实例。"""

    return QApplication.instance() or QApplication([])


def test_splash_shows_product_version_and_monotonic_progress(app: QApplication):
    splash = StartupSplash(QIcon(), "ForTest", "v0.2.8")
    splash.set_stage("正在准备本地数据…", 28)
    splash.set_stage("正在加载部署服务…", 12)

    assert splash.product.text() == "ForTest"
    assert splash.version.text() == "v0.2.8"
    assert splash.status.text() == "正在加载部署服务…"
    assert splash.progress.value() == 28
    splash.close()


def test_background_wait_keeps_qt_event_loop_responsive(app: QApplication):
    splash = StartupSplash(QIcon(), "ForTest", "v0.2.8")
    splash.show()
    ready = threading.Event()

    def complete_later() -> None:
        time.sleep(0.35)
        ready.set()

    worker = threading.Thread(target=complete_later, daemon=True)
    worker.start()
    assert wait_for_readiness(
        ready.is_set,
        splash,
        text="正在完成启动准备…",
        timeout_seconds=2.0,
    ) is True
    worker.join(timeout=1.0)

    # 等待期间心跳定时器持续执行，证明不是用同步 wait 冻结窗口。
    assert splash.heartbeat_count >= 2
    assert splash.progress.value() >= 84
    splash.close()


def test_background_wait_has_responsive_timeout(app: QApplication):
    splash = StartupSplash(QIcon(), "ForTest", "v0.2.8")
    splash.show()
    started = time.monotonic()
    assert wait_for_readiness(
        lambda: False,
        splash,
        text="正在完成启动准备…",
        timeout_seconds=0.2,
    ) is False
    assert time.monotonic() - started < 1.0
    assert splash.heartbeat_count >= 1
    splash.close()


def test_startup_loader_prepares_every_local_snapshot_before_ready(app: QApplication):
    calls: list[tuple[str, str]] = []

    class Native:
        def _value(self, name: str, value):
            calls.append((name, threading.current_thread().name))
            if name == "document":
                time.sleep(0.16)
            return value

        def get_document(self):
            return self._value("document", {"output_folder_url": "folder"})

        def get_ai_configurations(self):
            return self._value(
                "ai_configurations",
                {"providers": [], "configurations": [], "recycle_bin": []},
            )

        def get_prompts(self):
            return self._value("prompts", {"groups": {}})

        def configuration_status(self, *, jenkins_configured: bool = False):
            assert jenkins_configured is True
            return self._value("configuration", {"complete": True})

    class Deployment:
        def start(self):
            calls.append(("deployment_start", threading.current_thread().name))

        def configuration_view(self):
            return {"configured": True}

        def task_settings_view(self):
            return {"show_prod": False}

        def project_snapshot(self):
            return {"last_refreshed_at": "", "projects": []}

        def list_deployment_tasks(self, *, trashed: bool):
            assert trashed is False
            return [{"task_id": "1", "status": "queued"}]

    class Manager:
        started = False

        def start(self):
            self.started = True
            calls.append(("task_manager", threading.current_thread().name))

    manager = Manager()
    loader = StartupLoader(
        Native(),
        Deployment(),
        lambda: manager,
        cleanup=lambda: calls.append(
            ("cleanup", threading.current_thread().name)
        ),
    )
    splash = StartupSplash(QIcon(), "ForTest", "v0.2.8")
    splash.show()

    assert wait_for_startup(
        loader,
        splash,
        translate=lambda value: value,
        timeout_seconds=2.0,
    ) is True
    snapshot = loader.result()

    assert snapshot.document["output_folder_url"] == "folder"
    assert snapshot.ai_configurations["configurations"] == []
    assert snapshot.deployment_configuration["configured"] is True
    assert snapshot.deployment_tasks[0]["task_id"] == "1"
    assert manager.started is True
    assert splash.heartbeat_count >= 1
    assert all(thread_name == "fortester-startup-loader" for _, thread_name in calls)
    splash.close()
