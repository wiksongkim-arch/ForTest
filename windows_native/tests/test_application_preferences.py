"""原生服务的应用行为设置集成测试。"""

from __future__ import annotations

import pytest

from windows_native.desktop_preferences import DesktopPreferences
from windows_native.native_service import NativeService


class _MemoryStartupRegistration:
    """以内存状态模拟注册表，覆盖跨层保存与补偿逻辑。"""

    def __init__(self) -> None:
        self.enabled = False
        self.fail_on: bool | None = None
        self.calls: list[bool] = []

    def is_enabled(self) -> bool:
        return self.enabled

    def set_enabled(self, enabled: bool) -> bool:
        value = bool(enabled)
        self.calls.append(value)
        if self.fail_on is value:
            raise PermissionError("registry denied")
        self.enabled = value
        return value


def _service(tmp_path) -> NativeService:
    """绕过重型业务初始化，只装配本用例需要的两个真实契约。"""

    service = object.__new__(NativeService)
    service.desktop_preferences = DesktopPreferences(tmp_path)
    service.startup_registration = _MemoryStartupRegistration()
    return service


def test_application_preferences_save_close_behavior_and_actual_startup_state(
    tmp_path,
) -> None:
    service = _service(tmp_path)

    saved = service.save_application_preferences(
        {"close_behavior": "minimize", "start_with_windows": True}
    )

    assert saved == {
        "close_behavior": "minimize",
        "start_with_windows": True,
    }
    assert DesktopPreferences(tmp_path).get_close_behavior() == "minimize"


def test_application_preferences_do_not_persist_on_registry_failure(tmp_path) -> None:
    service = _service(tmp_path)
    service.startup_registration.fail_on = True

    with pytest.raises(PermissionError, match="registry denied"):
        service.save_application_preferences(
            {"close_behavior": "quit", "start_with_windows": True}
        )

    assert DesktopPreferences(tmp_path).get_close_behavior() == "ask"
    assert service.startup_registration.enabled is False


def test_application_preferences_reject_unknown_close_behavior(tmp_path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="不支持的关闭行为"):
        service.save_application_preferences(
            {"close_behavior": "hide", "start_with_windows": True}
        )

    assert service.startup_registration.calls == []
