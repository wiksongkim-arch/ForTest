"""启动阶段使用的轻量品牌占位页。"""

from __future__ import annotations

import time
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QEventLoop, QSize, QTimer, Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from windows_native.ui.style import theme_colors


@dataclass(frozen=True)
class StartupSnapshot:
    """主窗口显示前已经准备完成的本地业务快照。"""

    task_manager: Any
    document: dict[str, Any]
    ai_configurations: dict[str, Any]
    prompts: dict[str, Any]
    configuration_status: dict[str, Any]
    deployment_configuration: dict[str, Any]
    deployment_task_settings: dict[str, Any]
    deployment_projects: dict[str, Any]
    deployment_tasks: list[dict[str, Any]]


class StartupLoader:
    """在单一后台线程完成全部本地初始化，主线程只维护启动页事件循环。"""

    _STAGE_PROGRESS = {
        "native": 38,
        "native_data": 52,
        "deployment": 66,
        "tasks": 78,
        "ui_modules": 86,
        "cleanup": 90,
        "ready": 96,
    }

    def __init__(
        self,
        service: Any,
        deployment_service: Any,
        task_manager_factory: Callable[[], Any],
        *,
        cleanup: Callable[[], None] | None = None,
    ) -> None:
        self.service = service
        self.deployment_service = deployment_service
        self.task_manager_factory = task_manager_factory
        self.cleanup = cleanup
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._stage = "native"
        self._snapshot: StartupSnapshot | None = None
        self._error: BaseException | None = None

    def start(self) -> None:
        """幂等启动预热线程，禁止在主窗口显示后才触发首次解析。"""

        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._load,
                name="fortester-startup-loader",
                daemon=True,
            )
            self._thread.start()

    def _set_stage(self, stage: str) -> None:
        with self._lock:
            self._stage = stage

    def _load(self) -> None:
        try:
            self._set_stage("native")
            # 首次访问会在此线程解析完整 backend/Codex 依赖，主窗口显示后不再补课。
            document = self.service.get_document()
            self._set_stage("native_data")
            ai_configurations = self.service.get_ai_configurations()
            prompts = self.service.get_prompts()

            self._set_stage("deployment")
            # 部署调度器与 Keyring、缓存、历史任务都在启动页期间完成初始化。
            self.deployment_service.start()
            deployment_configuration = self.deployment_service.configuration_view()
            deployment_task_settings = self.deployment_service.task_settings_view()
            deployment_projects = self.deployment_service.project_snapshot()
            deployment_tasks = self.deployment_service.list_deployment_tasks(
                trashed=False
            )
            configuration_status = self.service.configuration_status(
                jenkins_configured=bool(deployment_configuration.get("configured"))
            )

            self._set_stage("tasks")
            task_manager = self.task_manager_factory()
            task_manager.start()

            self._set_stage("ui_modules")
            # 页面模块导入同样可能较重，先在后台完成；QWidget 实例仍只在主线程创建。
            from windows_native.ui import main_window as _main_window  # noqa: F401

            self._set_stage("cleanup")
            if self.cleanup is not None:
                self.cleanup()

            # 深拷贝切断后台服务内部可变对象，主界面只消费稳定的启动快照。
            self._snapshot = StartupSnapshot(
                task_manager=task_manager,
                document=deepcopy(document),
                ai_configurations=deepcopy(ai_configurations),
                prompts=deepcopy(prompts),
                configuration_status=deepcopy(configuration_status),
                deployment_configuration=deepcopy(deployment_configuration),
                deployment_task_settings=deepcopy(deployment_task_settings),
                deployment_projects=deepcopy(deployment_projects),
                deployment_tasks=deepcopy(deployment_tasks),
            )
            self._set_stage("ready")
        except BaseException as exc:
            self._error = exc
        finally:
            self._ready.set()

    def is_ready(self) -> bool:
        """无阻塞查询预热是否已经结束，供 Qt 定时器轮询。"""

        return self._ready.is_set()

    def stage(self) -> str:
        with self._lock:
            return self._stage

    def progress(self) -> int:
        return self._STAGE_PROGRESS.get(self.stage(), 38)

    def result(self) -> StartupSnapshot:
        """读取完整快照；初始化异常在进入主界面前直接上抛。"""

        if not self._ready.is_set():
            raise RuntimeError("启动数据尚未准备完成")
        if self._error is not None:
            raise RuntimeError("本地业务初始化失败") from self._error
        if self._snapshot is None:
            raise RuntimeError("启动数据快照为空")
        return self._snapshot


class StartupSplash(QWidget):
    """可持续处理 Windows 消息的 ForTest 品牌启动页。"""

    def __init__(
        self,
        icon: QIcon,
        product_name: str,
        version: str,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            parent,
            Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint,
        )
        self.setObjectName("startupSplash")
        self.setFixedSize(480, 286)
        self._heartbeat_count = 0
        self._last_heartbeat_at: float | None = None
        self._max_heartbeat_gap_seconds = 0.0
        self._stage_text = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame()
        self.card.setObjectName("startupCard")
        outer.addWidget(self.card)

        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(34, 28, 34, 24)
        layout.setSpacing(0)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(20)
        self.logo = QLabel()
        self.logo.setObjectName("startupLogo")
        self.logo.setFixedSize(78, 78)
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo.setPixmap(
            icon.pixmap(
                QSize(74, 74),
                QIcon.Mode.Normal,
                QIcon.State.Off,
            )
        )
        brand_row.addWidget(self.logo)

        identity = QVBoxLayout()
        identity.setSpacing(3)
        identity.addStretch()
        self.product = QLabel(product_name)
        self.product.setObjectName("startupProduct")
        self.version = QLabel(version)
        self.version.setObjectName("startupVersion")
        identity.addWidget(self.product)
        identity.addWidget(self.version)
        identity.addStretch()
        brand_row.addLayout(identity, 1)
        layout.addLayout(brand_row)
        layout.addStretch(1)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self.status = QLabel()
        self.status.setObjectName("startupStatus")
        self.activity = QLabel("●")
        self.activity.setObjectName("startupActivity")
        self.activity.setFixedWidth(34)
        self.activity.setAlignment(Qt.AlignmentFlag.AlignRight)
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.activity)
        layout.addLayout(status_row)

        self.progress = QProgressBar()
        self.progress.setObjectName("startupProgress")
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        layout.addSpacing(10)
        layout.addWidget(self.progress)

        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(110)
        self._heartbeat_timer.timeout.connect(self._heartbeat)
        self._heartbeat_timer.start()
        self.apply_theme("light")

    @property
    def heartbeat_count(self) -> int:
        """暴露事件循环心跳数，供启动响应性诊断和测试使用。"""

        return self._heartbeat_count

    @property
    def max_heartbeat_gap_seconds(self) -> float:
        """返回启动期间事件循环两次心跳之间的最大间隔。"""

        return self._max_heartbeat_gap_seconds

    def apply_theme(self, mode: str) -> None:
        """使用现有主题令牌绘制启动页，避免另建一套产品颜色。"""

        colors = theme_colors(mode)
        self.setStyleSheet(
            f"""
            QWidget#startupSplash {{
                background: {colors.surface};
                border: 1px solid {colors.border};
            }}
            QFrame#startupCard {{
                background: {colors.surface};
                border: none;
            }}
            QLabel#startupLogo {{
                background: {colors.input};
                border: 1px solid {colors.border};
                border-radius: 16px;
                padding: 2px;
            }}
            QLabel#startupProduct {{
                color: {colors.text};
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 27px;
                font-weight: 700;
            }}
            QLabel#startupVersion, QLabel#startupStatus {{
                color: {colors.muted};
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 12px;
            }}
            QLabel#startupActivity {{
                color: {colors.accent};
                font-family: "Segoe UI";
                font-size: 10px;
                letter-spacing: 2px;
            }}
            QProgressBar#startupProgress {{
                background: {colors.surface_hover};
                border: none;
                border-radius: 3px;
            }}
            QProgressBar#startupProgress::chunk {{
                background: {colors.accent};
                border-radius: 3px;
            }}
            """
        )

    def set_stage(self, text: str, progress: int) -> None:
        """更新可理解的真实加载阶段与单调递增的进度。"""

        self._stage_text = str(text)
        self.status.setText(self._stage_text)
        self.progress.setValue(max(self.progress.value(), min(100, int(progress))))

    def center_on_screen(self) -> None:
        """按主屏可用区域居中，不覆盖系统任务栏。"""

        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(
            area.x() + (area.width() - self.width()) // 2,
            area.y() + (area.height() - self.height()) // 2,
        )

    def finish(self, window: QWidget) -> None:
        """先展示可交互主窗口，再在短暂过渡后关闭启动页。"""

        window.show()
        window.raise_()
        window.activateWindow()
        QTimer.singleShot(120, self.close)

    def _heartbeat(self) -> None:
        """轻量动画同时证明 Qt 事件循环仍能持续处理窗口消息。"""

        now = time.monotonic()
        if self._last_heartbeat_at is not None:
            self._max_heartbeat_gap_seconds = max(
                self._max_heartbeat_gap_seconds,
                now - self._last_heartbeat_at,
            )
        self._last_heartbeat_at = now
        self._heartbeat_count += 1
        self.activity.setText("●" * (1 + self._heartbeat_count % 3))


def wait_for_readiness(
    is_ready: Callable[[], bool],
    splash: StartupSplash,
    *,
    text: str,
    timeout_seconds: float = 20.0,
) -> bool:
    """在保持 Qt 事件循环运行的前提下等待后台本地服务预热。"""

    if is_ready():
        return True
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(40)
    started = time.monotonic()
    result = {"ready": False}

    def poll() -> None:
        elapsed = time.monotonic() - started
        if is_ready():
            result["ready"] = True
            loop.quit()
            return
        if elapsed >= timeout_seconds:
            loop.quit()
            return
        # 等待阶段只缓慢推进到 96%，避免把未知耗时伪装成精确百分比。
        progress = 84 + min(12, int(elapsed / max(0.1, timeout_seconds) * 12))
        splash.set_stage(text, progress)

    timer.timeout.connect(poll)
    timer.start()
    QTimer.singleShot(0, poll)
    loop.exec()
    timer.stop()
    return bool(result["ready"])


def wait_for_startup(
    loader: StartupLoader,
    splash: StartupSplash,
    *,
    translate: Callable[[str], str],
    timeout_seconds: float = 45.0,
) -> bool:
    """按真实阶段更新启动页，并在等待期间持续处理 Windows 窗口消息。"""

    stage_text = {
        "native": "正在加载业务服务…",
        "native_data": "正在读取本地配置…",
        "deployment": "正在加载部署服务…",
        "tasks": "正在恢复本地任务…",
        "ui_modules": "正在加载界面组件…",
        "cleanup": "正在清理旧版运行环境…",
        "ready": "正在完成启动准备…",
    }
    loader.start()
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(40)
    started = time.monotonic()
    result = {"ready": False}

    def poll() -> None:
        stage = loader.stage()
        splash.set_stage(
            translate(stage_text.get(stage, "正在完成启动准备…")),
            loader.progress(),
        )
        if loader.is_ready():
            result["ready"] = True
            loop.quit()
            return
        if time.monotonic() - started >= timeout_seconds:
            loop.quit()

    timer.timeout.connect(poll)
    timer.start()
    QTimer.singleShot(0, poll)
    loop.exec()
    timer.stop()
    return bool(result["ready"])
