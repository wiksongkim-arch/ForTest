"""ForTest 品牌、版本和图标资源测试。"""

from __future__ import annotations

import struct
from pathlib import Path


def test_brand_and_version_resources_are_consistent():
    root = Path(__file__).resolve().parents[1]
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    assert version == "0.2.13"
    assert (root / "ForTest.spec").exists()
    assert not (root / "QAQ.spec").exists()
    assert not (root / "ForTester.spec").exists()
    assert not (root / "PRDtoCASE.spec").exists()
    resource = (root / "version_info.txt").read_text(encoding="utf-8")
    version_tuple = ", ".join(version.split("."))
    assert f"filevers=({version_tuple}, 0)" in resource
    assert f"prodvers=({version_tuple}, 0)" in resource
    assert f"u'FileVersion', u'{version}'" in resource
    assert "ForTest.exe" in resource
    installer = (root / "installer.iss").read_text(encoding="utf-8")
    assert f'#define MyAppVersion "{version}"' in installer
    assert '#define MyAppName "ForTest"' in installer
    assert "ForTest-Windows-x64-Setup-{#MyAppVersion}" in installer
    # 沿用旧 AppId 才能覆盖升级并保留现有用户数据。
    assert "6A87C65B-9717-487B-92A6-B7073540BEB4" in installer
    assert "UsePreviousAppDir=no" in installer
    assert '"{localappdata}\\Programs\\QAQ"' in installer
    assert '"{localappdata}\\Programs\\ForTester"' in installer
    assert '"{localappdata}\\Programs\\PRDtoCASE"' in installer
    assert "Flags: checkedonce" not in installer
    assert "Tasks: desktopicon" not in installer
    assert "InitializeUninstall" in installer
    assert "--delete-user-data" in installer
    assert "是否同时删除 ForTest 的全部用户数据" in installer


def test_windows_icon_contains_all_required_sizes():
    icon = Path(__file__).resolve().parents[1] / "assets" / "ForTester.ico"
    raw = icon.read_bytes()
    reserved, image_type, count = struct.unpack_from("<HHH", raw, 0)
    assert reserved == 0
    assert image_type == 1
    sizes: set[int] = set()
    for index in range(count):
        width, height = struct.unpack_from("<BB", raw, 6 + index * 16)
        normalized_width = width or 256
        normalized_height = height or 256
        assert normalized_width == normalized_height
        sizes.add(normalized_width)
    assert sizes == {16, 20, 24, 32, 40, 48, 64, 128, 256}


def test_build_accepts_multi_digit_patch_versions():
    root = Path(__file__).resolve().parents[1]
    build_script = (root / "build.ps1").read_text(encoding="utf-8")
    assert "(?<patch>\\d+)" in build_script
    assert "($versionMajor, $versionMinor, $versionPatch, 0)" in build_script
    assert "例如 0.2.13" in build_script
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "递增 `0.0.1`" in readme


def test_settings_page_replaces_legacy_global_model_page():
    root = Path(__file__).resolve().parents[1]
    settings_source = (root / "ui" / "settings_page.py").read_text(encoding="utf-8")
    window_source = (root / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert not (root / "ui" / "ai_page.py").exists()
    assert "AIConfigurationsPanel" in settings_source
    assert "新增配置" in settings_source
    assert "AIPage" not in window_source


def test_runtime_product_metadata_is_centralized():
    root = Path(__file__).resolve().parents[1]
    product = (root / "product.py").read_text(encoding="utf-8")
    main = (root / "main.py").read_text(encoding="utf-8")
    window = (root / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert 'PRODUCT_NAME = "ForTest"' in product
    assert 'PRODUCT_VERSION = "0.2.13"' in product
    assert "setApplicationName(PRODUCT_NAME)" in main
    assert "setWindowTitle(PRODUCT_NAME)" in window
