"""统一用户数据、偏好与卸载契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from windows_native.desktop_preferences import DesktopPreferences
from windows_native.paths import (
    app_data_root,
    legacy_app_data_root,
    migrate_legacy_user_data,
    older_brand_app_data_root,
    previous_app_data_root,
    remove_user_data,
)
from windows_native.update_service import UpdateService
from windows_native import main as native_main


def test_legacy_data_migrates_without_deleting_source(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    legacy = legacy_app_data_root()
    (legacy / "data").mkdir(parents=True)
    (legacy / "data" / "settings.json").write_text('{"schema_version": 2}', encoding="utf-8")
    (legacy / "prd-to-case-native.lock").write_text("stale", encoding="utf-8")

    target = migrate_legacy_user_data()

    assert target == app_data_root()
    assert (target / "data" / "settings.json").exists()
    assert (target / "data" / ".legacy-migration-complete").exists()
    assert not (target / "prd-to-case-native.lock").exists()
    assert (legacy / "data" / "settings.json").exists()


def test_previous_brand_data_takes_priority_over_older_legacy_data(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    previous = previous_app_data_root()
    older_brand = older_brand_app_data_root()
    legacy = legacy_app_data_root()
    (previous / "data").mkdir(parents=True)
    (older_brand / "data").mkdir(parents=True)
    (legacy / "data").mkdir(parents=True)
    (previous / "data" / "settings.json").write_text("previous", encoding="utf-8")
    (older_brand / "data" / "settings.json").write_text(
        "older-brand",
        encoding="utf-8",
    )
    (legacy / "data" / "settings.json").write_text("legacy", encoding="utf-8")

    target = migrate_legacy_user_data()

    assert (target / "data" / "settings.json").read_text(encoding="utf-8") == "previous"
    marker = (target / "data" / ".legacy-migration-complete").read_text(
        encoding="utf-8"
    )
    assert "QAQ" in marker


def test_preferences_cover_language_guide_profile_and_update_foundation(tmp_path: Path):
    preferences = DesktopPreferences(tmp_path)
    assert preferences.get_language() == "zh_CN"
    assert preferences.set_language("en_US") == "en_US"
    assert preferences.get_language() == "en_US"
    assert preferences.get_guide_dismissed() is False
    preferences.set_guide_dismissed(True)
    assert preferences.get_guide_dismissed() is True
    assert preferences.get_user_profile() == {
        "display_name": "免费用户",
        "membership": "free",
        "membership_expires_at": "permanent",
    }
    update = UpdateService(tmp_path, preferences)
    assert update.can_check() is True
    assert update.state.manifest_url == "https://github.com/wiksongkim-arch/ForTest"
    assert preferences.set_update_preferences(
        enabled=True,
        channel="beta",
        manifest_url="https://github.com/wiksongkim-arch/ForTest/releases/latest/",
    ) == {
        "enabled": True,
        "channel": "beta",
        "manifest_url": "https://github.com/wiksongkim-arch/ForTest",
    }
    assert DesktopPreferences(tmp_path).get_update_preferences()["channel"] == "beta"

    with pytest.raises(ValueError):
        preferences.set_update_preferences(
            enabled=True,
            channel="stable",
            manifest_url="https://updates.example.test/manifest.json",
        )


def test_explicit_user_data_removal_deletes_all_brand_roots(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    new_root = app_data_root()
    previous_root = previous_app_data_root()
    older_brand_root = older_brand_app_data_root()
    old_root = legacy_app_data_root()
    (new_root / "data").mkdir(parents=True)
    (previous_root / "data").mkdir(parents=True)
    (older_brand_root / "data").mkdir(parents=True)
    (old_root / "data").mkdir(parents=True)

    remove_user_data()

    assert not new_root.exists()
    assert not previous_root.exists()
    assert not older_brand_root.exists()
    assert not old_root.exists()


def test_full_cleanup_enumerates_dynamic_ai_secret_names(monkeypatch, tmp_path: Path):
    roots = [tmp_path / name for name in ("current", "previous", "older", "legacy")]
    settings = roots[0] / "data" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        '{"ai":{"configurations":[{"id":"custom-provider"}]}}',
        encoding="utf-8",
    )
    for name, root in zip(
        (
            "app_data_root",
            "previous_app_data_root",
            "older_brand_app_data_root",
            "legacy_app_data_root",
        ),
        roots,
        strict=True,
    ):
        monkeypatch.setattr(native_main, name, lambda root=root: root)

    assert "ai_config:custom-provider:api_key" in native_main._user_secret_names()
