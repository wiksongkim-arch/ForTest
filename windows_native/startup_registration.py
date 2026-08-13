"""ForTest Windows 当前用户开机启动注册管理。

该模块只操作当前用户的 ``Run`` 注册表项，不需要管理员权限。注册表接口可以
注入，因此单元测试和非 Windows 环境无需访问真实注册表。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any


RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ForTest"
AUTOSTART_ARGUMENT = "--autostart"


class StartupRegistrationError(RuntimeError):
    """表示读取或修改 Windows 开机启动注册信息失败。"""


def build_startup_command(
    *,
    executable_path: str | Path | None = None,
    source_main: str | Path | None = None,
    frozen: bool | None = None,
) -> str:
    """构造写入注册表的安全 Windows 命令行。

    打包后直接启动当前可执行文件；源码运行时使用当前 Python 解释器启动
    ``windows_native/main.py``。两种模式都会附加 ``--autostart``，并通过
    ``subprocess.list2cmdline`` 正确引用包含空格或特殊字符的路径。
    """

    resolved_executable = str(
        executable_path if executable_path is not None else sys.executable
    )
    if not resolved_executable.strip():
        raise StartupRegistrationError("无法确定开机启动所需的程序路径")

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    arguments = [resolved_executable]
    if not is_frozen:
        main_path = (
            Path(source_main)
            if source_main is not None
            else Path(__file__).resolve().with_name("main.py")
        )
        arguments.append(str(main_path))
    arguments.append(AUTOSTART_ARGUMENT)
    return subprocess.list2cmdline(arguments)


class StartupRegistration:
    """管理 ForTest 在当前用户登录后的自动启动状态。"""

    def __init__(
        self,
        executable_path: Path | None = None,
        registry_module: Any | None = None,
    ) -> None:
        self._registry = registry_module
        self._executable_path = executable_path

    def command(self) -> str:
        """返回当前运行形态应写入注册表的完整启动命令。"""

        return build_startup_command(
            executable_path=self._executable_path,
        )

    def is_enabled(self) -> bool:
        """返回当前用户是否已注册且命令与当前程序一致。"""

        registry = self._registry_api()
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                RUN_KEY_PATH,
                0,
                registry.KEY_QUERY_VALUE,
            ) as key:
                value, value_type = registry.QueryValueEx(key, VALUE_NAME)
        except OSError as exc:
            if _is_missing_registry_value(exc):
                return False
            raise StartupRegistrationError(
                f"读取 ForTest Windows 开机启动项失败：{exc}"
            ) from exc

        # 仅把本模块写入的字符串命令视为有效，避免旧安装路径造成“已开启”假象。
        return value_type == registry.REG_SZ and value == self.command()

    def set_enabled(self, enabled: bool) -> bool:
        """启用或关闭当前用户开机启动，并返回最终开关状态。"""

        if bool(enabled):
            self._enable()
            return True
        self._disable()
        return False

    def _enable(self) -> None:
        """创建或更新 HKCU 下的 ForTest 启动值。"""

        registry = self._registry_api()
        try:
            # HKCU 仅影响当前用户，无需申请管理员权限或写入系统级注册表。
            with registry.CreateKeyEx(
                registry.HKEY_CURRENT_USER,
                RUN_KEY_PATH,
                0,
                registry.KEY_SET_VALUE,
            ) as key:
                registry.SetValueEx(
                    key,
                    VALUE_NAME,
                    0,
                    registry.REG_SZ,
                    self.command(),
                )
        except OSError as exc:
            raise StartupRegistrationError(
                f"启用 ForTest Windows 开机启动失败：{exc}"
            ) from exc

    def _disable(self) -> None:
        """删除 HKCU 下的 ForTest 启动值；值不存在时保持幂等。"""

        registry = self._registry_api()
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                RUN_KEY_PATH,
                0,
                registry.KEY_SET_VALUE,
            ) as key:
                registry.DeleteValue(key, VALUE_NAME)
        except OSError as exc:
            if _is_missing_registry_value(exc):
                return
            raise StartupRegistrationError(
                f"关闭 ForTest Windows 开机启动失败：{exc}"
            ) from exc

    def _registry_api(self) -> Any:
        """返回注入的注册表接口，未注入时延迟加载系统 ``winreg``。"""

        if self._registry is not None:
            return self._registry
        try:
            import winreg
        except ImportError as exc:
            raise StartupRegistrationError(
                "当前系统不支持 Windows 注册表，无法配置开机启动"
            ) from exc
        return winreg


def _is_missing_registry_value(error: OSError) -> bool:
    """判断注册表键或值是否不存在，兼容不同 Python/Windows 错误表示。"""

    return isinstance(error, FileNotFoundError) or getattr(error, "winerror", None) == 2


def is_enabled(
    *,
    registry_module: Any | None = None,
    executable_path: Path | None = None,
) -> bool:
    """便捷读取 ForTest 当前用户开机启动状态。"""

    return StartupRegistration(
        executable_path=executable_path,
        registry_module=registry_module,
    ).is_enabled()


def set_enabled(
    enabled: bool,
    *,
    registry_module: Any | None = None,
    executable_path: Path | None = None,
) -> bool:
    """便捷设置 ForTest 当前用户开机启动状态。"""

    return StartupRegistration(
        executable_path=executable_path,
        registry_module=registry_module,
    ).set_enabled(enabled)
