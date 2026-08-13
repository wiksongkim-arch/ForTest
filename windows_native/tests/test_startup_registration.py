"""Windows 当前用户开机启动注册管理测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from windows_native.startup_registration import (
    AUTOSTART_ARGUMENT,
    RUN_KEY_PATH,
    VALUE_NAME,
    StartupRegistration,
    StartupRegistrationError,
    build_startup_command,
    is_enabled,
    set_enabled,
)


class _FakeKey:
    """提供与 ``winreg`` 键句柄一致的上下文管理行为。"""

    def __init__(self, registry: "_FakeRegistry", hive: object, path: str) -> None:
        self.registry = registry
        self.hive = hive
        self.path = path

    def __enter__(self) -> "_FakeKey":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeRegistry:
    """只在内存中模拟本测试使用到的 ``winreg`` 接口。"""

    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 0x0001
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1

    def __init__(self) -> None:
        self.keys: set[tuple[object, str]] = set()
        self.values: dict[tuple[object, str, str], tuple[object, int]] = {}
        self.calls: list[tuple[object, ...]] = []
        self.read_error: OSError | None = None
        self.write_error: OSError | None = None
        self.delete_error: OSError | None = None

    def OpenKey(
        self,
        hive: object,
        path: str,
        reserved: int,
        access: int,
    ) -> _FakeKey:
        self.calls.append(("open", hive, path, reserved, access))
        if self.read_error is not None:
            raise self.read_error
        if (hive, path) not in self.keys:
            raise FileNotFoundError(path)
        return _FakeKey(self, hive, path)

    def CreateKeyEx(
        self,
        hive: object,
        path: str,
        reserved: int,
        access: int,
    ) -> _FakeKey:
        self.calls.append(("create", hive, path, reserved, access))
        self.keys.add((hive, path))
        return _FakeKey(self, hive, path)

    def QueryValueEx(self, key: _FakeKey, name: str) -> tuple[object, int]:
        self.calls.append(("query", key.hive, key.path, name))
        stored = self.values.get((key.hive, key.path, name))
        if stored is None:
            raise FileNotFoundError(name)
        return stored

    def SetValueEx(
        self,
        key: _FakeKey,
        name: str,
        reserved: int,
        value_type: int,
        value: object,
    ) -> None:
        self.calls.append(
            ("set", key.hive, key.path, name, reserved, value_type, value)
        )
        if self.write_error is not None:
            raise self.write_error
        self.values[(key.hive, key.path, name)] = (value, value_type)

    def DeleteValue(self, key: _FakeKey, name: str) -> None:
        self.calls.append(("delete", key.hive, key.path, name))
        if self.delete_error is not None:
            raise self.delete_error
        try:
            del self.values[(key.hive, key.path, name)]
        except KeyError as exc:
            raise FileNotFoundError(name) from exc


def test_build_command_for_packaged_application_quotes_executable() -> None:
    executable = r"C:\Program Files\ForTest\ForTest.exe"

    command = build_startup_command(executable_path=executable, frozen=True)

    assert command == subprocess.list2cmdline([executable, AUTOSTART_ARGUMENT])


def test_build_command_for_source_application_includes_main_script() -> None:
    executable = r"C:\Python 3\python.exe"
    main_path = r"D:\Source Folder\ForTester\windows_native\main.py"

    command = build_startup_command(
        executable_path=executable,
        source_main=main_path,
        frozen=False,
    )

    assert command == subprocess.list2cmdline(
        [executable, main_path, AUTOSTART_ARGUMENT]
    )


def test_enable_uses_only_hkcu_run_key_and_writes_expected_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _FakeRegistry()
    executable = r"C:\Program Files\ForTest\ForTest.exe"
    monkeypatch.setattr("windows_native.startup_registration.sys.frozen", True, raising=False)
    registration = StartupRegistration(
        Path(executable),
        registry,
    )

    assert registration.set_enabled(True) is True
    assert registry.values[
        (registry.HKEY_CURRENT_USER, RUN_KEY_PATH, VALUE_NAME)
    ] == (subprocess.list2cmdline([executable, AUTOSTART_ARGUMENT]), registry.REG_SZ)
    assert registration.is_enabled() is True
    assert all(
        call[1] is registry.HKEY_CURRENT_USER
        for call in registry.calls
        if call[0] in {"open", "create"}
    )


def test_existing_stale_or_non_string_registration_is_not_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _FakeRegistry()
    monkeypatch.setattr("windows_native.startup_registration.sys.frozen", True, raising=False)
    registry.keys.add((registry.HKEY_CURRENT_USER, RUN_KEY_PATH))
    key = (registry.HKEY_CURRENT_USER, RUN_KEY_PATH, VALUE_NAME)
    registry.values[key] = (r"C:\Old\ForTest.exe --autostart", registry.REG_SZ)
    registration = StartupRegistration(
        Path(r"C:\New\ForTest.exe"),
        registry,
    )

    assert registration.is_enabled() is False

    registry.values[key] = (123, registry.REG_SZ)
    assert registration.is_enabled() is False


def test_disable_removes_value_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _FakeRegistry()
    monkeypatch.setattr("windows_native.startup_registration.sys.frozen", True, raising=False)
    registration = StartupRegistration(
        Path(r"C:\ForTest\ForTest.exe"),
        registry,
    )
    registration.set_enabled(True)

    assert registration.set_enabled(False) is False
    assert registration.is_enabled() is False
    assert registration.set_enabled(False) is False


def test_module_functions_accept_injected_registry_without_real_registry_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _FakeRegistry()
    monkeypatch.setattr("windows_native.startup_registration.sys.frozen", True, raising=False)
    options = {
        "registry_module": registry,
        "executable_path": Path(r"C:\ForTest\ForTest.exe"),
    }

    assert set_enabled(True, **options) is True
    assert is_enabled(**options) is True
    assert set_enabled(False, **options) is False
    assert is_enabled(**options) is False


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        ("read", "读取 ForTest Windows 开机启动项失败"),
        ("write", "启用 ForTest Windows 开机启动失败"),
        ("delete", "关闭 ForTest Windows 开机启动失败"),
    ],
)
def test_registry_errors_are_wrapped_with_clear_context(
    operation: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _FakeRegistry()
    monkeypatch.setattr("windows_native.startup_registration.sys.frozen", True, raising=False)
    registration = StartupRegistration(
        Path(r"C:\ForTest\ForTest.exe"),
        registry,
    )

    if operation == "read":
        registry.read_error = PermissionError("access denied")
        action = registration.is_enabled
    elif operation == "write":
        registry.write_error = PermissionError("access denied")
        action = lambda: registration.set_enabled(True)
    else:
        registration.set_enabled(True)
        registry.delete_error = PermissionError("access denied")
        action = lambda: registration.set_enabled(False)

    with pytest.raises(StartupRegistrationError, match=message):
        action()


def test_installer_always_removes_current_user_startup_value() -> None:
    """卸载时即使保留业务数据，也不得留下指向已删除程序的启动项。"""

    installer = (
        Path(__file__).resolve().parents[1] / "installer.iss"
    ).read_text(encoding="utf-8")
    assert "RegDeleteValue(" in installer
    assert "Software\\Microsoft\\Windows\\CurrentVersion\\Run" in installer
    assert "'{#MyAppName}'" in installer
    assert installer.index("RegDeleteValue(") < installer.index(
        "if DeleteUserDataOnUninstall then"
    )
