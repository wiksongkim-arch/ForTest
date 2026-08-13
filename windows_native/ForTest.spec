# -*- mode: python ; coding: utf-8 -*-
"""ForTest 纯原生 Windows x64 单目录构建定义。"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata


native_root = Path(SPECPATH).resolve()
project_root = native_root.parent
entry_path = native_root / "main.py"
icon_path = native_root / "assets" / "ForTester.ico"
version_path = native_root / "version_info.txt"
default_templates_path = native_root / "assets" / "default_templates"

datas = [
    (str(icon_path), "windows_native/assets"),
    # 默认模板作为程序只读母版进入 _internal，运行时只复制到用户目录。
    (str(default_templates_path), "windows_native/assets/default_templates"),
]
binaries = []
hiddenimports = []


def collect_package(module_name):
    """收集动态导入、数据文件和本机二进制依赖。"""

    # Python 代码已进入加密压缩归档；不再额外复制源码和 __pycache__，避免
    # 冗余文件扩大安装包与泄露面。
    package_datas, package_binaries, package_hidden = collect_all(
        module_name,
        include_py_files=False,
        exclude_datas=["**/__pycache__/**", "**/*.pyc"],
    )
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hidden)


for dynamic_package in ("openai_codex", "codex_cli_bin"):
    collect_package(dynamic_package)

hiddenimports.extend(collect_submodules("keyring.backends"))
hiddenimports.extend(
    [
        "backend.ai.codex_provider",
        "backend.ai.minimax_provider",
        "backend.ai.openai_compatible_provider",
        "services.dingtalk_mcp",
        "services.dingtalk_output",
        "services.dingtalk_spreadsheet",
    ]
)

for distribution_name in (
    "openai-codex",
    "openai-codex-cli-bin",
    "keyring",
):
    datas.extend(copy_metadata(distribution_name))

a = Analysis(
    [str(entry_path)],
    pathex=[str(project_root), str(native_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "_pytest",
        "cefpython3",
        "frontend",
        "pytest",
        "streamlit",
        "uvicorn",
        "webview",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ForTest",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path),
    version=str(version_path),
    uac_admin=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ForTest",
)
