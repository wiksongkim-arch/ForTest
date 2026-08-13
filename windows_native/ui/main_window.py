"""ForTest 主窗口、导航和多分辨率尺寸策略。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QCloseEvent, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from windows_native.paths import app_data_root
from windows_native.product import PRODUCT_NAME, PRODUCT_VERSION
from windows_native.single_instance import SingleInstance, show_duplicate_instance_message
from windows_native.update_service import UpdateService
from windows_native.i18n import set_language, tr, translate_widget_tree
from windows_native.ui.appearance_page import AppearancePage
from windows_native.ui.config_page import ConfigPage
from windows_native.ui.deployment_widgets import DeploymentRecyclePage
from windows_native.ui.deployment_config_page import DeploymentConfigPage
from windows_native.ui.home_page import HomePage
from windows_native.ui.quick_deploy_page import QuickDeployPage
from windows_native.ui.recycle_page import RecyclePage
from windows_native.ui.settings_page import SettingsPage
from windows_native.ui.onboarding import ConfigurationGuideDialog, UserCard

if TYPE_CHECKING:
    from windows_native.native_service import NativeService
    from windows_native.task_manager import TaskManager
    from windows_native.ui.theme import ThemeManager


@dataclass(frozen=True)
class WindowGeometry:
    """便于无界面单元测试的窗口尺寸计算结果。"""

    width: int
    height: int
    x: int
    y: int


class _FallbackPreferences:
    """仅供独立 UI 测试使用的无磁盘偏好实现。"""

    def __init__(self) -> None:
        self.language = "zh_CN"
        self.dismissed = True

    def get_user_profile(self) -> dict[str, str]:
        return {
            "display_name": "免费用户",
            "membership": "free",
            "membership_expires_at": "permanent",
        }

    def set_language(self, language: str) -> str:
        self.language = str(language)
        return self.language

    def get_guide_dismissed(self) -> bool:
        return self.dismissed

    def set_guide_dismissed(self, dismissed: bool) -> bool:
        self.dismissed = bool(dismissed)
        return self.dismissed

    def get_update_preferences(self) -> dict:
        return {"enabled": True, "channel": "stable", "manifest_url": ""}

    def get_close_behavior(self) -> str:
        """独立 UI 测试默认沿用正式程序的每次询问策略。"""

        return "ask"


def adaptive_window_geometry(
    available_width: int,
    available_height: int,
    origin_x: int = 0,
    origin_y: int = 0,
) -> WindowGeometry:
    """将默认窗口稳定控制在可用工作区内，并适配高低分辨率。"""

    margin = 24 if available_width >= 1000 else 10
    width = min(1360, max(720, int(available_width * 0.88)))
    height = min(900, max(560, int(available_height * 0.9)))
    width = min(width, max(320, available_width - margin * 2))
    height = min(height, max(300, available_height - margin * 2))
    x = origin_x + max(0, (available_width - width) // 2)
    y = origin_y + max(0, (available_height - height) // 2)
    return WindowGeometry(width, height, x, y)


class MainWindow(QMainWindow):
    """无 WebView、无本地端口的原生 Qt 主窗口。"""

    def __init__(
        self,
        service: NativeService,
        icon: QIcon,
        task_manager: TaskManager,
        theme_manager: ThemeManager,
        deployment_service,
        *,
        onboarding_enabled: bool = True,
        background_refresh_enabled: bool = True,
        startup_preloaded: bool = False,
        tray_enabled: bool = False,
    ):
        super().__init__()
        self.service = service
        self.task_manager = task_manager
        self.theme_manager = theme_manager
        self.deployment_service = deployment_service
        self.preferences = getattr(theme_manager, "preferences", _FallbackPreferences())
        self.update_service = UpdateService(
            Path(getattr(service, "data_root", app_data_root())),
            self.preferences,
        )
        self.configuration_snapshot: dict = {
            "complete": False,
            "sections": [],
        }
        self._guide_dialog: ConfigurationGuideDialog | None = None
        self._auto_guide_shown = False
        self._background_services_started = False
        self._onboarding_enabled = bool(onboarding_enabled)
        self._background_refresh_enabled = bool(background_refresh_enabled)
        self._startup_snapshot_applied = False
        self._tray_enabled = bool(tray_enabled)
        self._tray_icon: QSystemTrayIcon | None = None
        self._tray_menu: QMenu | None = None
        self._tray_show_action: QAction | None = None
        self._tray_quit_action: QAction | None = None
        self._shutdown_complete = False
        self._quit_requested = False
        self.setWindowTitle(PRODUCT_NAME)
        self.setWindowIcon(icon)
        self.setMinimumSize(720, 520)

        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(204)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(10, 10, 10, 10)
        side_layout.setSpacing(5)

        brand_widget = QWidget()
        brand_layout = QHBoxLayout(brand_widget)
        brand_layout.setContentsMargins(6, 0, 6, 7)
        brand_text = QLabel(PRODUCT_NAME)
        brand_text.setObjectName("brand")
        brand_layout.addWidget(brand_text)
        brand_layout.addStretch()
        side_layout.addWidget(brand_widget)

        self.stack = QStackedWidget()
        self.quick_deploy_page = QuickDeployPage(
            deployment_service,
            auto_refresh=background_refresh_enabled and not startup_preloaded,
        )
        self.home_page = HomePage(service, task_manager)
        # 设置中心直接承接 AI 能力快照，不再创建旧的全局模型选择页面。
        self.settings_page = SettingsPage(service)
        self.settings_page.configuration_changed.connect(
            self.refresh_configuration_status
        )
        self.appearance_page = AppearancePage(theme_manager)
        self.nav_pages = [
            self.quick_deploy_page,
            self.home_page,
            self.settings_page,
            self.appearance_page,
        ]
        labels = [tr("快捷部署"), tr("测试用例生成"), tr("设置"), tr("外观")]
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for index, (label, page) in enumerate(zip(labels, self.nav_pages, strict=True)):
            button = QPushButton(label)
            button.setObjectName("nav")
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, target=index: self.switch_page(target)
            )
            self.nav_group.addButton(button, index)
            side_layout.addWidget(button)
            self.stack.addWidget(page)

        # 修改配置和回收站是生成页的内部路由，不占用侧栏入口。
        self.config_page = ConfigPage(service, task_manager)
        self.recycle_page = RecyclePage(task_manager)
        self.deployment_recycle_page = DeploymentRecyclePage(deployment_service)
        self.deployment_config_page = DeploymentConfigPage(deployment_service)
        self.stack.addWidget(self.config_page)
        self.stack.addWidget(self.recycle_page)
        self.stack.addWidget(self.deployment_recycle_page)
        self.stack.addWidget(self.deployment_config_page)
        self.home_page.modify_config_requested.connect(self.show_config)
        self.home_page.recycle_requested.connect(self.show_recycle)
        self.config_page.back_requested.connect(self._leave_config)
        self.recycle_page.back_requested.connect(self.show_home)
        self.quick_deploy_page.recycle_requested.connect(
            self.show_deployment_recycle
        )
        self.deployment_recycle_page.back_requested.connect(self.show_quick_deploy)
        self.quick_deploy_page.modify_config_requested.connect(
            self.show_deployment_config
        )
        self.deployment_config_page.back_requested.connect(self._leave_deployment_config)
        self.deployment_config_page.configuration_saved.connect(
            self.quick_deploy_page.apply_configuration_view
        )
        self.deployment_config_page.configuration_saved.connect(
            lambda _view: self.refresh_configuration_status()
        )
        self.deployment_config_page.task_settings_saved.connect(
            self.quick_deploy_page.apply_task_settings
        )

        side_layout.addStretch()
        version_row = QHBoxLayout()
        version_row.setContentsMargins(5, 0, 0, 2)
        version = QLabel(f"{PRODUCT_NAME} v{PRODUCT_VERSION}")
        version.setObjectName("versionLabel")
        self.guide_button = QPushButton(tr("完成配置"))
        self.guide_button.setObjectName("guideRequiredButton")
        self.guide_button.clicked.connect(self.show_configuration_guide)
        version_row.addWidget(version)
        version_row.addStretch()
        version_row.addWidget(self.guide_button)
        side_layout.addLayout(version_row)
        self.user_card = UserCard(
            self.preferences.get_user_profile(),
            self.change_language,
            sidebar,
        )
        side_layout.addWidget(self.user_card)
        root_layout.addWidget(sidebar)
        root_layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.nav_group.button(0).setChecked(True)
        # 页面先完成纯控件构建；正式启动由入口注入完整快照后才展示窗口。
        self.switch_page(0, refresh=False)
        translate_widget_tree(self)
        self.config_page.retranslate()
        self._setup_system_tray(icon)

    def _system_tray_available(self) -> bool:
        """集中判断系统托盘能力，便于无桌面环境执行确定性测试。"""

        return bool(QSystemTrayIcon.isSystemTrayAvailable())

    def _setup_system_tray(self, icon: QIcon) -> None:
        """使用应用图标创建托盘入口；不支持托盘的平台保持原关闭语义。"""

        if not self._tray_enabled or not self._system_tray_available():
            return
        tray_menu = QMenu(self)
        show_action = tray_menu.addAction("")
        quit_action = tray_menu.addAction("")
        show_action.triggered.connect(self.show_and_activate)
        quit_action.triggered.connect(self.quit_application)

        tray_icon = QSystemTrayIcon(icon, self)
        tray_icon.setContextMenu(tray_menu)
        tray_icon.activated.connect(self._handle_tray_activation)

        self._tray_menu = tray_menu
        self._tray_show_action = show_action
        self._tray_quit_action = quit_action
        self._tray_icon = tray_icon
        self._retranslate_system_tray()
        tray_icon.show()

    def _retranslate_system_tray(self) -> None:
        """刷新托盘菜单的动态文案，确保切换语言后立即生效。"""

        if self._tray_icon is not None:
            self._tray_icon.setToolTip(PRODUCT_NAME)
        if self._tray_show_action is not None:
            self._tray_show_action.setText(tr("显示主窗口"))
        if self._tray_quit_action is not None:
            self._tray_quit_action.setText(tr("退出程序"))

    def _tray_is_usable(self) -> bool:
        """只允许隐藏到仍然可见且系统确认可用的托盘图标。"""

        return bool(
            self._tray_icon is not None
            and self._tray_icon.isVisible()
            and self._system_tray_available()
        )

    def show_and_activate(self) -> None:
        """从托盘恢复窗口，并尽最大可能将其带到当前桌面前台。"""

        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def _handle_tray_activation(
        self,
        reason: QSystemTrayIcon.ActivationReason,
    ) -> None:
        """单击或双击托盘图标均可恢复主窗口。"""

        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.show_and_activate()

    def _preferred_close_behavior(self) -> str:
        """读取并规范化关闭偏好，损坏配置安全回退为询问。"""

        getter = getattr(self.preferences, "get_close_behavior", None)
        try:
            behavior = str(getter()).strip().lower() if callable(getter) else "ask"
        except (OSError, TypeError, ValueError):
            behavior = "ask"
        return behavior if behavior in {"ask", "minimize", "quit"} else "ask"

    def _ask_close_behavior(self) -> str:
        """用三个明确按钮询问本次关闭行为，关闭对话框等同于取消。"""

        dialog = QMessageBox(self)
        dialog.setWindowTitle(tr("关闭程序"))
        dialog.setText(tr("关闭主窗口后要执行什么操作？"))
        dialog.setIcon(QMessageBox.Icon.Question)
        minimize_button = dialog.addButton(
            tr("最小化到托盘"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        quit_button = dialog.addButton(
            tr("退出程序"),
            QMessageBox.ButtonRole.DestructiveRole,
        )
        cancel_button = dialog.addButton(
            tr("取消"),
            QMessageBox.ButtonRole.RejectRole,
        )
        dialog.setDefaultButton(minimize_button)
        dialog.setEscapeButton(cancel_button)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked is minimize_button:
            return "minimize"
        if clicked is quit_button:
            return "quit"
        return "cancel"

    def _shutdown_once(self) -> None:
        """真正退出时只清理一次调度器，托盘隐藏不会触碰后台任务。"""

        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        self.quick_deploy_page.shutdown()
        deployment_stop = getattr(self.deployment_service, "stop", None)
        if callable(deployment_stop):
            deployment_stop()
        self.task_manager.stop()

    def _finish_quit(self, event: QCloseEvent) -> None:
        """完成窗口退出，并显式结束禁用了末窗退出策略的应用。"""

        self._quit_requested = True
        self._shutdown_once()
        if self._tray_icon is not None:
            self._tray_icon.hide()
        event.accept()
        application = QApplication.instance()
        if application is not None:
            application.quit()

    def quit_application(self) -> None:
        """供托盘菜单调用的幂等退出入口。"""

        self._quit_requested = True
        if not self.close():
            # 理论上强制退出分支不会被拒绝；此处仍保证异常窗口状态可以收尾。
            self._shutdown_once()
            if self._tray_icon is not None:
                self._tray_icon.hide()
            application = QApplication.instance()
            if application is not None:
                application.quit()

    def apply_startup_snapshot(self, snapshot: Any) -> None:
        """将启动页预热结果一次性注入所有首屏消费者。"""

        self.home_page.apply_document(snapshot.document)
        self.settings_page.ai_panel.apply_view(snapshot.ai_configurations)
        self.quick_deploy_page.apply_startup_snapshot(
            configuration=snapshot.deployment_configuration,
            task_settings=snapshot.deployment_task_settings,
            projects=snapshot.deployment_projects,
            tasks=snapshot.deployment_tasks,
        )
        self.apply_configuration_status(
            snapshot.configuration_status,
            allow_guide=False,
        )
        self._startup_snapshot_applied = True

    def start_background_services(self) -> None:
        """主界面出现后只安排轻量交互，不再执行本地首次加载。"""

        if self._background_services_started:
            return
        self._background_services_started = True
        if not self._startup_snapshot_applied:
            # 保留独立 UI 测试与第三方嵌入兼容；正式入口必定走启动快照分支。
            self.task_manager.start()
            background_start = getattr(
                self.deployment_service,
                "start_in_background",
                None,
            )
            if callable(background_start):
                background_start()
            else:
                deployment_start = getattr(self.deployment_service, "start", None)
                if callable(deployment_start):
                    deployment_start()
        if self._onboarding_enabled:
            if self._startup_snapshot_applied:
                QTimer.singleShot(350, self._show_onboarding_from_snapshot)
            else:
                QTimer.singleShot(350, self.refresh_configuration_status)
        if self._startup_snapshot_applied and self._background_refresh_enabled:
            # 外部 Jenkins 刷新不是启动依赖，主窗口稳定后再安全地异步执行。
            QTimer.singleShot(1200, self.quick_deploy_page.start_remote_refresh)

    def apply_adaptive_geometry(self) -> None:
        """按当前屏幕可用区域设置初始尺寸和位置。"""

        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1160, 760)
            return
        area = screen.availableGeometry()
        geometry = adaptive_window_geometry(
            area.width(),
            area.height(),
            area.x(),
            area.y(),
        )
        self.setMinimumSize(
            min(720, max(320, area.width() - 20)),
            min(520, max(300, area.height() - 20)),
        )
        self.setGeometry(geometry.x, geometry.y, geometry.width, geometry.height)

    def switch_page(self, index: int, *, refresh: bool = True) -> None:
        page = self.nav_pages[index]
        self.stack.setCurrentWidget(page)
        button = self.nav_group.button(index)
        if button is not None:
            button.setChecked(True)
        refresh_page = getattr(page, "refresh", None)
        if refresh and callable(refresh_page):
            refresh_page()

    def show_home(self) -> None:
        self.switch_page(1)

    def show_quick_deploy(self) -> None:
        self.switch_page(0)

    def show_config(self) -> None:
        self.stack.setCurrentWidget(self.config_page)
        self.config_page.refresh()

    def _leave_config(self) -> None:
        self.show_home()
        self.refresh_configuration_status()

    def show_recycle(self) -> None:
        self.stack.setCurrentWidget(self.recycle_page)
        self.recycle_page.refresh()

    def show_deployment_recycle(self) -> None:
        self.stack.setCurrentWidget(self.deployment_recycle_page)
        self.deployment_recycle_page.refresh()

    def show_deployment_config(self) -> None:
        self.stack.setCurrentWidget(self.deployment_config_page)
        self.deployment_config_page.refresh()

    def _leave_deployment_config(self) -> None:
        self.show_quick_deploy()
        self.quick_deploy_page.refresh()

    def refresh_configuration_status(self) -> None:
        """异步读取配置完成度，并按首次使用偏好决定是否弹出向导。"""

        def success(snapshot: dict) -> None:
            self.apply_configuration_status(snapshot, allow_guide=True)

        def load_status() -> dict:
            deployment = self.deployment_service.configuration_view()
            return self.service.configuration_status(
                jenkins_configured=bool(deployment.get("configured"))
            )

        self.home_page.run_async(
            load_status,
            success=success,
            # 配置状态读取失败不阻止正常业务页面使用。
            failure=lambda _message: None,
        )

    def apply_configuration_status(
        self,
        snapshot: dict,
        *,
        allow_guide: bool,
    ) -> None:
        """更新配置徽标；启动页阶段只更新控件，不提前弹出对话框。"""

        self.configuration_snapshot = dict(snapshot)
        complete = bool(snapshot.get("complete"))
        self.guide_button.setObjectName(
            "guideCompleteButton" if complete else "guideRequiredButton"
        )
        self.guide_button.setText(
            tr("完成配置") if complete else f"! {tr('完成配置')}"
        )
        self.guide_button.style().unpolish(self.guide_button)
        self.guide_button.style().polish(self.guide_button)
        if allow_guide:
            self._show_onboarding_from_snapshot()

    def _show_onboarding_from_snapshot(self) -> None:
        """仅根据内存快照决定是否显示向导，不触发任何业务读取。"""

        if (
            not bool(self.configuration_snapshot.get("complete"))
            and not self._auto_guide_shown
            and not self.preferences.get_guide_dismissed()
        ):
            self._auto_guide_shown = True
            self.show_configuration_guide()

    def show_configuration_guide(self) -> None:
        """打开可从版本旁随时进入的配置向导。"""

        if self._guide_dialog is not None and self._guide_dialog.isVisible():
            self._guide_dialog.raise_()
            self._guide_dialog.activateWindow()
            return
        dialog = ConfigurationGuideDialog(
            self.configuration_snapshot,
            on_open=self._open_guide_target,
            on_language=self.change_language,
            on_dismiss=lambda: self.preferences.set_guide_dismissed(True),
            parent=self,
        )
        self._guide_dialog = dialog
        dialog.finished.connect(lambda _result: setattr(self, "_guide_dialog", None))
        dialog.show()

    def _open_guide_target(self, target: str) -> None:
        if target == "jenkins":
            self.show_deployment_config()
            self.deployment_config_page.tabs.setCurrentIndex(0)
            return
        if target == "ai":
            self.switch_page(2)
            self.settings_page.tabs.setCurrentIndex(0)
            return
        self.show_config()
        self.config_page.tabs.setCurrentIndex(0)

    def change_language(self, language: str) -> None:
        """立即切换界面语言，配置向导与主程序使用同一持久化设置。"""

        saved = self.preferences.set_language(language)
        set_language(saved)
        translate_widget_tree(self)
        self.user_card.language_button.rebuild_menu()
        if self._guide_dialog is not None:
            self._guide_dialog.language_button.rebuild_menu()
            translate_widget_tree(self._guide_dialog)
            self._guide_dialog.setWindowTitle(tr("配置向导"))
        self.home_page.task_table._last_tasks = None
        self.quick_deploy_page.task_table._last_tasks = None
        self.deployment_recycle_page.table._last_tasks = None
        self.recycle_page.table._last_tasks = None
        self.home_page.refresh_tasks()
        self.recycle_page.refresh()
        self.appearance_page._update_summary()
        self.deployment_config_page.retranslate()
        self._retranslate_system_tray()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        """按用户偏好隐藏到托盘、退出或取消，并防止无托盘时成为幽灵进程。"""

        if self._quit_requested:
            self._finish_quit(event)
            return

        behavior = self._preferred_close_behavior()
        tray_usable = self._tray_is_usable()
        if not tray_usable:
            # 没有可靠的恢复入口时必须正常退出，不能把进程隐藏到后台。
            behavior = "quit"
        elif behavior == "ask":
            behavior = self._ask_close_behavior()

        if behavior == "cancel":
            event.ignore()
            return
        if behavior == "minimize":
            event.ignore()
            self.hide()
            return
        self._finish_quit(event)
