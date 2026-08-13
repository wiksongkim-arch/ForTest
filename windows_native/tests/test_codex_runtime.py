"""Codex 单一 CLI 运行时的版本目录、迁移、校验与切换测试。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import os
import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from windows_native.codex_runtime import (
    CODEX_CLI_ENV,
    CODEX_SDK_ENV,
    LEGACY_CODEX_CLI_ENV,
    LEGACY_CODEX_SDK_ENV,
    CodexRuntimeError,
    CodexRuntimeManager,
)


def _x64_executable() -> bytes:
    payload = bytearray(512)
    payload[0:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", payload, 0x84, 0x8664)
    return bytes(payload)


def _release_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # 官方 0.147.0 起会同时发布辅助程序，安装器必须只选择 Codex 主程序。
        archive.writestr("codex-command-runner.exe", b"command-runner")
        archive.writestr("codex-windows-sandbox-setup.exe", b"sandbox-setup")
        archive.writestr("codex-x86_64-pc-windows-msvc.exe", _x64_executable())
    return buffer.getvalue()


class FakeResponse:
    """提供 requests 响应所需的最小只读接口。"""

    def __init__(self, *, payload=None, content: bytes = b"", url: str = ""):
        self.payload = payload
        self.content = content
        self.url = url

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]


class FakeSession:
    """按 URL 返回固定目录、发布元数据和压缩包。"""

    def __init__(self, archive: bytes, *, digest: str | None = None):
        self.archive = archive
        self.digest = digest or hashlib.sha256(archive).hexdigest()
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs):
        self.calls.append(url)
        if "matching-refs" in url:
            return FakeResponse(
                payload=[
                    {"ref": "refs/tags/rust-v0.147.0"},
                    {"ref": "refs/tags/rust-v0.144.4"},
                    {"ref": "refs/tags/rust-v0.145.0-alpha.1"},
                    {"ref": "refs/tags/rust-v0.0.2504291921"},
                ],
                url=url,
            )
        if "/releases/tags/" in url:
            return FakeResponse(
                payload={
                    "assets": [
                        {
                            "name": "codex-x86_64-pc-windows-msvc.exe.zip",
                            "browser_download_url": (
                                "https://github.com/openai/codex/releases/download/"
                                "rust-v0.147.0/codex-x86_64-pc-windows-msvc.exe.zip"
                            ),
                            "digest": f"sha256:{self.digest}",
                            "size": len(self.archive),
                        }
                    ]
                },
                url=url,
            )
        return FakeResponse(
            content=self.archive,
            url="https://release-assets.githubusercontent.com/codex.zip",
        )


def _runner(command, **_kwargs):
    output = "codex-cli 0.147.0" if command[-1] == "--version" else "help"
    return SimpleNamespace(returncode=0, stdout=output, stderr="")


@pytest.fixture(autouse=True)
def clear_runtime_environment(monkeypatch):
    monkeypatch.delenv(CODEX_CLI_ENV, raising=False)
    monkeypatch.delenv(CODEX_SDK_ENV, raising=False)
    monkeypatch.delenv(LEGACY_CODEX_CLI_ENV, raising=False)
    monkeypatch.delenv(LEGACY_CODEX_SDK_ENV, raising=False)
    yield
    os.environ.pop(CODEX_CLI_ENV, None)
    os.environ.pop(CODEX_SDK_ENV, None)
    os.environ.pop(LEGACY_CODEX_CLI_ENV, None)
    os.environ.pop(LEGACY_CODEX_SDK_ENV, None)


def test_catalog_lists_stable_versions_newest_first(tmp_path: Path):
    bundled = tmp_path / "bundled.exe"
    bundled.write_bytes(_x64_executable())
    manager = CodexRuntimeManager(
        tmp_path,
        session=FakeSession(_release_archive()),
        runner=_runner,
        bundled_path_factory=lambda: bundled,
        external_paths_factory=lambda _kind: [],
        clock=lambda: 1000.0,
    )

    catalog = manager.refresh_catalog(force=True)

    assert [item["version"] for item in catalog["versions"]][:2] == [
        "0.147.0",
        "0.144.4",
    ]
    assert all("alpha" not in item["version"] for item in catalog["versions"])
    assert all(item["version"] != "0.0.2504291921" for item in catalog["versions"])


def test_install_verifies_package_and_switches_single_runtime(tmp_path: Path):
    bundled = tmp_path / "bundled.exe"
    bundled.write_bytes(_x64_executable())
    session = FakeSession(_release_archive())
    manager = CodexRuntimeManager(
        tmp_path,
        session=session,
        runner=_runner,
        bundled_path_factory=lambda: bundled,
        external_paths_factory=lambda _kind: [],
    )

    catalog = manager.install_and_switch("0.147.0")

    installed = tmp_path / "runtimes" / "codex" / "packages" / "0.147.0" / "codex.exe"
    state = json.loads(
        (tmp_path / "data" / "codex-runtimes.json").read_text(encoding="utf-8")
    )
    assert installed.is_file()
    assert installed.read_bytes() == _x64_executable()
    assert state == {"schema_version": 2, "selected": "0.147.0"}
    assert Path(catalog["status"]["runtime"]["path"]) == installed
    assert Path(catalog["status"]["cli"]["path"]) == installed
    assert Path(catalog["status"]["sdk"]["path"]) == installed
    assert Path(os.environ[CODEX_CLI_ENV]) == installed
    assert Path(os.environ[CODEX_SDK_ENV]) == installed
    selected_item = next(
        item for item in catalog["versions"] if item["version"] == "0.147.0"
    )
    bundled_item = next(item for item in catalog["versions"] if item["bundled"])
    # 活动版本不等于内置版本；二者必须保留各自的下拉选项与路径。
    assert selected_item["bundled"] is False
    assert Path(selected_item["path"]) == installed
    assert bundled_item["version"] == importlib.metadata.version(
        "openai-codex-cli-bin"
    )
    assert Path(bundled_item["path"]) == bundled


def test_schema_one_state_migrates_to_one_selection_and_repairs_old_split(
    tmp_path: Path,
):
    """旧界面只切换 CLI 后，升级应把该选择同时用于 SDK app-server。"""

    bundled = tmp_path / "bundled.exe"
    bundled.write_bytes(_x64_executable())
    installed = (
        tmp_path / "runtimes" / "codex" / "packages" / "0.147.0" / "codex.exe"
    )
    installed.parent.mkdir(parents=True)
    installed.write_bytes(_x64_executable())
    state_path = tmp_path / "data" / "codex-runtimes.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {"schema_version": 1, "cli": "0.147.0", "sdk": "bundled"}
        ),
        encoding="utf-8",
    )

    manager = CodexRuntimeManager(
        tmp_path,
        session=FakeSession(_release_archive()),
        runner=_runner,
        bundled_path_factory=lambda: bundled,
        external_paths_factory=lambda _kind: [],
    )

    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "schema_version": 2,
        "selected": "0.147.0",
    }
    assert Path(os.environ[CODEX_CLI_ENV]) == installed
    assert Path(os.environ[CODEX_SDK_ENV]) == installed
    assert manager.status()["runtime"]["selection"] == "0.147.0"


def test_bad_official_digest_never_installs_or_switches(tmp_path: Path):
    bundled = tmp_path / "bundled.exe"
    bundled.write_bytes(_x64_executable())
    manager = CodexRuntimeManager(
        tmp_path,
        session=FakeSession(_release_archive(), digest="0" * 64),
        runner=_runner,
        bundled_path_factory=lambda: bundled,
        external_paths_factory=lambda _kind: [],
    )

    with pytest.raises(CodexRuntimeError, match="SHA-256"):
        manager.install_and_switch("0.147.0")

    assert not (tmp_path / "runtimes" / "codex" / "packages" / "0.147.0").exists()
    assert not (tmp_path / "data" / "codex-runtimes.json").exists()


def test_refresh_detects_external_runtime_without_recursive_scan(tmp_path: Path):
    bundled = tmp_path / "bundled.exe"
    external = tmp_path / "desktop-codex.exe"
    for path in (bundled, external):
        path.write_bytes(_x64_executable())

    discovered_kinds: list[str] = []

    def external_paths(kind: str) -> list[Path]:
        discovered_kinds.append(kind)
        return [external]

    def runner(command, **_kwargs):
        if command[-1] == "--version":
            return SimpleNamespace(
                returncode=0,
                stdout="codex-cli 0.142.3",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="help", stderr="")

    manager = CodexRuntimeManager(
        tmp_path,
        session=FakeSession(_release_archive()),
        runner=runner,
        bundled_path_factory=lambda: bundled,
        external_paths_factory=external_paths,
    )

    catalog = manager.refresh_catalog(force=True)

    local = catalog["status"]["local_other_versions"]
    assert discovered_kinds == ["runtime"]
    assert local == [
        {"version": "0.142.3", "paths": [str(external.resolve())]}
    ]
