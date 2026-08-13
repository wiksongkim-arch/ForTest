"""桌面专属偏好持久化测试。"""

from __future__ import annotations

import json

import pytest

from windows_native.desktop_preferences import DesktopPreferences


def test_preferences_have_safe_defaults_and_persist(tmp_path):
    preferences = DesktopPreferences(tmp_path)
    assert preferences.get_theme_mode() == "system"
    assert preferences.get_task_parallelism() == 1
    assert preferences.get_close_behavior() == "ask"
    assert preferences.set_theme_mode("light") == "light"
    assert preferences.set_task_parallelism(3) == 3
    assert preferences.set_close_behavior("minimize") == "minimize"

    reloaded = DesktopPreferences(tmp_path)
    assert reloaded.get_theme_mode() == "light"
    assert reloaded.get_task_parallelism() == 3
    assert reloaded.get_close_behavior() == "minimize"


def test_updating_one_section_preserves_unknown_fields(tmp_path):
    preferences = DesktopPreferences(tmp_path)
    preferences.path.parent.mkdir(parents=True, exist_ok=True)
    preferences.path.write_text(
        json.dumps({"future": {"enabled": True}}, ensure_ascii=False),
        encoding="utf-8",
    )
    preferences.set_theme_mode("dark")
    saved = json.loads(preferences.path.read_text(encoding="utf-8"))
    assert saved["future"] == {"enabled": True}
    assert saved["appearance"]["theme"] == "dark"


def test_preferences_reject_invalid_values(tmp_path):
    preferences = DesktopPreferences(tmp_path)
    with pytest.raises(ValueError):
        preferences.set_theme_mode("blue")
    with pytest.raises(ValueError):
        preferences.set_task_parallelism(0)
    with pytest.raises(ValueError):
        preferences.set_close_behavior("hide")
