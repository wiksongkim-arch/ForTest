"""Codex 单一 CLI 二进制的版本管理。

Python SDK 的 app-server 和命令行 exec 都由同一个 ``codex.exe`` 提供，
因此这里只维护一个活动版本；两个环境变量仅用于兼容尚未迁移的调用入口。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests


CODEX_CLI_ENV = "FORTEST_CODEX_CLI_BIN"
CODEX_SDK_ENV = "FORTEST_CODEX_SDK_BIN"
LEGACY_CODEX_CLI_ENV = "QAQ_CODEX_CLI_BIN"
LEGACY_CODEX_SDK_ENV = "QAQ_CODEX_SDK_BIN"
_STATE_SCHEMA = 2
_CATALOG_MAX_AGE_SECONDS = 6 * 60 * 60
_EXTERNAL_CACHE_MAX_AGE_SECONDS = 5 * 60
_MAX_DOWNLOAD_BYTES = 350 * 1024 * 1024
_MAX_EXECUTABLE_BYTES = 350 * 1024 * 1024
_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_TAG_PATTERN = re.compile(r"^refs/tags/rust-v(\d+\.\d+\.\d+)$")
_VERSION_OUTPUT_PATTERN = re.compile(r"\b(\d+\.\d+\.\d+)\b")
_ASSET_NAME = "codex-x86_64-pc-windows-msvc.exe.zip"
_ARCHIVE_EXECUTABLE_NAME = _ASSET_NAME.removesuffix(".zip").casefold()
_GITHUB_API = "https://api.github.com/repos/openai/codex"
_ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class CodexRuntimeError(RuntimeError):
    """可安全展示给用户的 Codex 运行时错误。"""


def _version_key(value: str) -> tuple[int, int, int]:
    match = _VERSION_PATTERN.fullmatch(str(value).strip())
    if match is None:
        raise CodexRuntimeError(f"无效的 Codex 版本号：{value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _normalize_version(value: str) -> str:
    version = str(value).strip()
    _version_key(version)
    return version


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _bundled_runtime_path() -> Path | None:
    try:
        from codex_cli_bin import bundled_codex_path

        path = Path(bundled_codex_path()).resolve()
    except (ImportError, OSError, RuntimeError):
        return None
    return path if path.is_file() else None


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except (importlib.metadata.PackageNotFoundError, OSError):
        return "unavailable"


class CodexRuntimeManager:
    """下载、校验并切换 CLI 与 SDK 共用的 Codex 二进制。"""

    def __init__(
        self,
        data_root: Path,
        *,
        session: requests.Session | Any | None = None,
        runner: Callable[..., Any] = subprocess.run,
        bundled_path_factory: Callable[[], Path | None] = _bundled_runtime_path,
        external_paths_factory: Callable[[str], list[Path]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.data_root = Path(data_root)
        self.runtime_root = self.data_root / "runtimes" / "codex"
        self.package_root = self.runtime_root / "packages"
        self.state_path = self.data_root / "data" / "codex-runtimes.json"
        self.catalog_path = self.data_root / "data" / "codex-runtime-catalog.json"
        self.session = session or requests.Session()
        self.runner = runner
        self.bundled_path_factory = bundled_path_factory
        self.external_paths_factory = external_paths_factory or self._default_external_paths
        self.clock = clock
        self._lock = threading.RLock()
        self._external_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
        self._request_headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ForTest-Codex-Runtime-Manager",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.apply_selected()

    def _default_state(self) -> dict[str, Any]:
        return {
            "schema_version": _STATE_SCHEMA,
            "selected": "bundled",
        }

    def _load_state(self) -> dict[str, Any]:
        state = _read_json(self.state_path)
        if state.get("schema_version") == _STATE_SCHEMA:
            selected = str(state.get("selected") or "bundled")
            if selected == "bundled" or _VERSION_PATTERN.fullmatch(selected):
                return {
                    "schema_version": _STATE_SCHEMA,
                    "selected": selected,
                }
            return self._default_state()

        if state.get("schema_version") == 1:
            # 旧版允许 CLI/SDK 各选一个版本，导致用户切换 CLI 后自动模式仍
            # 使用 SDK 的旧版本。迁移时优先保留非内置 CLI 选择；只有 CLI
            # 仍为内置版时才采用 SDK 的非内置选择。
            cli = str(state.get("cli") or "bundled")
            sdk = str(state.get("sdk") or "bundled")
            valid_cli = cli == "bundled" or bool(_VERSION_PATTERN.fullmatch(cli))
            valid_sdk = sdk == "bundled" or bool(_VERSION_PATTERN.fullmatch(sdk))
            cli = cli if valid_cli else "bundled"
            sdk = sdk if valid_sdk else "bundled"
            selected = cli if cli != "bundled" else sdk
            migrated = {
                "schema_version": _STATE_SCHEMA,
                "selected": selected,
            }
            self._save_state(migrated)
            return migrated

        # 首次启动不主动创建状态文件，避免一次失败下载留下无意义配置。
        return self._default_state()

    def _save_state(self, state: dict[str, Any]) -> None:
        _atomic_write_json(self.state_path, state)

    def _managed_path(self, version: str) -> Path:
        normalized = _normalize_version(version)
        return (self.package_root / normalized / "codex.exe").resolve()

    def _selection_path(self, selected: str) -> Path | None:
        if selected == "bundled":
            return self.bundled_path_factory()
        return self._managed_path(selected)

    def apply_selected(self) -> None:
        """把持久化选择应用到后续创建的 Codex 任务。"""

        with self._lock:
            state = self._load_state()
            path = self._selection_path(str(state["selected"]))
            for environment_name in (CODEX_CLI_ENV, CODEX_SDK_ENV):
                if path is None or not path.is_file():
                    os.environ.pop(environment_name, None)
                else:
                    os.environ[environment_name] = str(path.resolve())

    def _installed_versions(self) -> list[str]:
        if not self.package_root.is_dir():
            return []
        versions = [
            child.name
            for child in self.package_root.iterdir()
            if child.is_dir()
            and _VERSION_PATTERN.fullmatch(child.name)
            and (child / "codex.exe").is_file()
        ]
        return sorted(versions, key=_version_key, reverse=True)

    @staticmethod
    def _default_external_paths(_kind: str) -> list[Path]:
        """只探测固定入口，避免递归扫描磁盘拖慢版本弹窗。"""

        candidates = [
            Path.home() / ".codex" / "plugins" / ".plugin-appserver" / "codex.exe"
        ]
        found = shutil.which("codex")
        if found:
            candidates.append(Path(found))
        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            marker = os.path.normcase(str(candidate.resolve()))
            if marker not in seen:
                seen.add(marker)
                unique.append(candidate)
        return unique

    def _probe_external(self, path: Path) -> str | None:
        # 同一个候选文件必须同时具备 exec 与 app-server，才能安全成为唯一运行时。
        commands = [
            [str(path), "--version"],
            [str(path), "exec", "--help"],
            [str(path), "app-server", "--help"],
        ]
        version = ""
        for index, command in enumerate(commands):
            try:
                completed = self.runner(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    shell=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if int(getattr(completed, "returncode", 1)) != 0:
                return None
            if index == 0:
                output = str(completed.stdout or completed.stderr or "")
                match = _VERSION_OUTPUT_PATTERN.search(output)
                if match is None:
                    return None
                version = match.group(1)
        return version or None

    def _external_versions(self, *, force: bool) -> list[dict[str, str]]:
        cache_key = "runtime"
        cached = self._external_cache.get(cache_key)
        if (
            not force
            and cached is not None
            and self.clock() - cached[0] < _EXTERNAL_CACHE_MAX_AGE_SECONDS
        ):
            return [dict(item) for item in cached[1]]
        excluded: set[str] = set()
        bundled = self.bundled_path_factory()
        if bundled is not None:
            excluded.add(os.path.normcase(str(bundled.resolve())))
        for version in self._installed_versions():
            excluded.add(os.path.normcase(str(self._managed_path(version))))
        discovered: list[dict[str, str]] = []
        seen: set[str] = set()
        for candidate in self.external_paths_factory(cache_key):
            try:
                path = Path(candidate).resolve()
            except OSError:
                continue
            marker = os.path.normcase(str(path))
            if marker in excluded or marker in seen or not path.is_file():
                continue
            seen.add(marker)
            version = self._probe_external(path)
            if version is not None and _VERSION_PATTERN.fullmatch(version):
                discovered.append({"version": version, "path": str(path)})
        discovered.sort(key=lambda item: _version_key(item["version"]), reverse=True)
        self._external_cache[cache_key] = (
            self.clock(),
            [dict(item) for item in discovered],
        )
        return discovered

    def _selection_view(self, selected: str) -> dict[str, Any]:
        path = self._selection_path(selected)
        bundled_version = _package_version("openai-codex-cli-bin")
        version = bundled_version if selected == "bundled" else selected
        return {
            "kind": "runtime",
            "selection": selected,
            "version": version,
            "path": str(path.resolve()) if path is not None else "",
            "available": path is not None and path.is_file(),
            "bundled": selected == "bundled",
        }

    def status(self) -> dict[str, Any]:
        """返回唯一运行时；CLI/SDK 视图仅供旧调用方平滑迁移。"""

        with self._lock:
            state = self._load_state()
            runtime = self._selection_view(str(state["selected"]))
            return {
                "runtime": dict(runtime),
                "cli": {**runtime, "kind": "cli"},
                "sdk": {**runtime, "kind": "sdk"},
                "sdk_bindings_version": _package_version("openai-codex"),
                "installed_versions": self._installed_versions(),
            }

    def path_for_selection(self, selection: str) -> Path | None:
        """解析某条 Codex 配置保存的内置版本，不改变全局活动选择。"""

        normalized = str(selection).strip().lower()
        if normalized != "bundled":
            normalized = _normalize_version(normalized)
        with self._lock:
            path = self._selection_path(normalized)
            return path.resolve() if path is not None and path.is_file() else None

    def _request_json(self, url: str) -> Any:
        try:
            response = self.session.get(
                url,
                headers=self._request_headers,
                timeout=(10, 30),
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, TypeError, ValueError) as exc:
            raise CodexRuntimeError(f"无法读取 OpenAI Codex 官方版本信息：{exc}") from None

    @staticmethod
    def _is_public_version(version: str) -> bool:
        major, minor, patch = _version_key(version)
        # 早期仓库把构建时间写进 0.0.x 标签；它们不是面向用户的稳定版。
        return major > 0 or minor > 0 or patch < 10_000

    def refresh_catalog(self, *, force: bool = False) -> dict[str, Any]:
        """从官方仓库读取稳定标签，并与本地版本合并。"""

        with self._lock:
            cache = _read_json(self.catalog_path)
            fetched_at = float(cache.get("fetched_at") or 0)
            versions = cache.get("versions")
            cache_fresh = (
                isinstance(versions, list)
                and self.clock() - fetched_at < _CATALOG_MAX_AGE_SECONDS
            )
            if force or not cache_fresh:
                payload = self._request_json(f"{_GITHUB_API}/git/matching-refs/tags/rust-v")
                if not isinstance(payload, list):
                    raise CodexRuntimeError("OpenAI Codex 官方版本目录格式异常")
                collected: set[str] = set()
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    match = _TAG_PATTERN.fullmatch(str(item.get("ref") or ""))
                    if match is None:
                        continue
                    version = match.group(1)
                    if self._is_public_version(version):
                        collected.add(version)
                versions = sorted(collected, key=_version_key, reverse=True)
                if not versions:
                    raise CodexRuntimeError("OpenAI Codex 官方版本目录中没有可用的稳定版")
                fetched_at = self.clock()
                _atomic_write_json(
                    self.catalog_path,
                    {
                        "schema_version": _STATE_SCHEMA,
                        "fetched_at": fetched_at,
                        "versions": versions,
                    },
                )
            return self._catalog_view(
                [str(item) for item in versions],
                fetched_at,
                refresh_external=force,
            )

    def local_catalog(self) -> dict[str, Any]:
        """不访问网络，仅返回内置版、本地版和已有缓存。"""

        with self._lock:
            cache = _read_json(self.catalog_path)
            versions = [
                str(item)
                for item in cache.get("versions") or []
                if _VERSION_PATTERN.fullmatch(str(item))
            ]
            return self._catalog_view(
                versions,
                float(cache.get("fetched_at") or 0),
                refresh_external=False,
            )

    def _catalog_view(
        self,
        online_versions: list[str],
        fetched_at: float,
        *,
        refresh_external: bool,
    ) -> dict[str, Any]:
        status = self.status()
        # 内置版本来自安装包本身；当前活动版本可能是已下载版本，不能据此反推“内置”标记。
        bundled_version = _package_version("openai-codex-cli-bin")
        bundled_path = self.bundled_path_factory()
        installed = set(self._installed_versions())
        external = self._external_versions(force=refresh_external)
        versions = set(online_versions) | installed
        versions.update(
            item["version"] for item in external
        )
        if _VERSION_PATTERN.fullmatch(bundled_version):
            versions.add(bundled_version)
        ordered = sorted(versions, key=_version_key, reverse=True)
        online = set(online_versions)
        items: list[dict[str, Any]] = []
        for version in ordered:
            managed_path = self._managed_path(version)
            is_bundled = version == bundled_version
            paths = []
            if is_bundled and bundled_path is not None:
                # 内置条目始终指向安装包路径，不复用当前活动的已下载版本路径。
                paths.append(str(bundled_path.resolve()))
            elif version in installed:
                paths.append(str(managed_path))
            elif version in online:
                paths.append(str(managed_path))
            for entry in external:
                if entry["version"] == version and entry["path"] not in paths:
                    paths.append(entry["path"])
            items.append(
                {
                    "version": version,
                    "online": version in online,
                    "installed": version in installed or is_bundled,
                    "bundled": is_bundled,
                    "path": paths[0] if paths else "",
                    "paths": paths,
                }
            )
        current_version = str(status["runtime"].get("version") or "")
        grouped: dict[str, list[str]] = {}
        for version in installed:
            if version != current_version:
                grouped.setdefault(version, []).append(str(self._managed_path(version)))
        for entry in external:
            if entry["version"] != current_version:
                grouped.setdefault(entry["version"], []).append(entry["path"])
        local_other_versions = [
            {
                "version": version,
                "paths": list(dict.fromkeys(grouped[version])),
            }
            for version in sorted(grouped, key=_version_key, reverse=True)
        ]
        status["local_other_versions"] = local_other_versions
        return {
            "status": status,
            "versions": items,
            "fetched_at": fetched_at,
        }

    def _release_asset(self, version: str) -> dict[str, str | int]:
        payload = self._request_json(f"{_GITHUB_API}/releases/tags/rust-v{version}")
        if not isinstance(payload, dict):
            raise CodexRuntimeError(f"Codex {version} 的官方发布信息格式异常")
        for asset in payload.get("assets") or []:
            if not isinstance(asset, dict) or asset.get("name") != _ASSET_NAME:
                continue
            url = str(asset.get("browser_download_url") or "")
            digest = str(asset.get("digest") or "")
            if not url.startswith("https://github.com/openai/codex/releases/download/"):
                raise CodexRuntimeError("Codex 官方发布包下载地址异常")
            if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
                raise CodexRuntimeError(f"Codex {version} 的官方 SHA-256 摘要缺失")
            return {
                "url": url,
                "sha256": digest.split(":", 1)[1].lower(),
                "size": int(asset.get("size") or 0),
            }
        raise CodexRuntimeError(f"Codex {version} 没有 Windows x64 官方发布包")

    @staticmethod
    def _validate_download_host(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
            raise CodexRuntimeError("Codex 发布包被重定向到非官方地址")

    def _download_asset(self, asset: dict[str, str | int], destination: Path) -> None:
        expected_size = int(asset["size"])
        if expected_size <= 0 or expected_size > _MAX_DOWNLOAD_BYTES:
            raise CodexRuntimeError("Codex 官方发布包大小异常")
        try:
            response = self.session.get(
                str(asset["url"]),
                headers=self._request_headers,
                timeout=(15, 120),
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()
            self._validate_download_host(str(getattr(response, "url", asset["url"])))
            digest = hashlib.sha256()
            written = 0
            with destination.open("wb") as stream:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > _MAX_DOWNLOAD_BYTES:
                        raise CodexRuntimeError("Codex 发布包超过允许的最大体积")
                    digest.update(chunk)
                    stream.write(chunk)
        except CodexRuntimeError:
            raise
        except (requests.RequestException, OSError) as exc:
            raise CodexRuntimeError(f"Codex 发布包下载失败：{exc}") from None
        if written != expected_size:
            raise CodexRuntimeError("Codex 发布包下载不完整")
        if digest.hexdigest() != str(asset["sha256"]):
            raise CodexRuntimeError("Codex 发布包 SHA-256 校验失败")

    @staticmethod
    def _extract_executable(archive_path: Path, destination: Path) -> None:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                # 新版官方包同时携带命令运行器和沙箱安装器；只提取资产名对应的主程序。
                members = [
                    item
                    for item in archive.infolist()
                    if not item.is_dir()
                    and Path(item.filename).name.casefold()
                    == _ARCHIVE_EXECUTABLE_NAME
                ]
                if len(members) != 1:
                    raise CodexRuntimeError("Codex 发布包中的可执行文件数量异常")
                member = members[0]
                if member.file_size <= 0 or member.file_size > _MAX_EXECUTABLE_BYTES:
                    raise CodexRuntimeError("Codex 可执行文件大小异常")
                with archive.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        except CodexRuntimeError:
            raise
        except (OSError, zipfile.BadZipFile) as exc:
            raise CodexRuntimeError(f"Codex 发布包解压失败：{exc}") from None

    @staticmethod
    def _validate_x64_executable(path: Path) -> None:
        try:
            with path.open("rb") as stream:
                if stream.read(2) != b"MZ":
                    raise CodexRuntimeError("Codex 可执行文件缺少 Windows PE 标记")
                stream.seek(0x3C)
                pe_offset_raw = stream.read(4)
                if len(pe_offset_raw) != 4:
                    raise CodexRuntimeError("Codex 可执行文件头不完整")
                pe_offset = struct.unpack("<I", pe_offset_raw)[0]
                stream.seek(pe_offset)
                if stream.read(4) != b"PE\x00\x00":
                    raise CodexRuntimeError("Codex 可执行文件 PE 签名无效")
                machine_raw = stream.read(2)
                if len(machine_raw) != 2 or struct.unpack("<H", machine_raw)[0] != 0x8664:
                    raise CodexRuntimeError("Codex 可执行文件不是 Windows x64 架构")
        except CodexRuntimeError:
            raise
        except OSError as exc:
            raise CodexRuntimeError(f"无法校验 Codex 可执行文件：{exc}") from None

    def _probe(self, path: Path, *, kind: str, expected_version: str) -> None:
        commands = [[str(path), "--version"]]
        commands.append(
            [str(path), "exec", "--help"]
            if kind == "cli"
            else [str(path), "app-server", "--help"]
        )
        version_output = ""
        for index, command in enumerate(commands):
            try:
                completed = self.runner(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    shell=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CodexRuntimeError(f"Codex {kind.upper()} 运行时探测失败：{exc}") from None
            if int(getattr(completed, "returncode", 1)) != 0:
                raise CodexRuntimeError(f"Codex {kind.upper()} 运行时能力探测失败")
            if index == 0:
                version_output = str(completed.stdout or completed.stderr or "")
        match = _VERSION_OUTPUT_PATTERN.search(version_output)
        if match is None or match.group(1) != expected_version:
            raise CodexRuntimeError(
                f"Codex 运行时版本不匹配，期望 {expected_version}，实际输出 {version_output.strip() or '未知'}"
            )

    def _install(self, version: str) -> Path:
        target = self._managed_path(version)
        if target.is_file():
            try:
                self._validate_x64_executable(target)
                self._probe(target, kind="cli", expected_version=version)
                self._probe(target, kind="sdk", expected_version=version)
                return target
            except CodexRuntimeError:
                package = target.parent.resolve()
                expected = (self.package_root.resolve() / version).resolve()
                if package != expected:
                    raise CodexRuntimeError("拒绝清理未通过路径校验的 Codex 运行时")
                shutil.rmtree(package)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.package_root.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(
            tempfile.mkdtemp(prefix=f".{version}-", dir=str(self.runtime_root))
        )
        archive_path = temporary_root / "codex.zip"
        executable_path = temporary_root / "codex.exe"
        try:
            asset = self._release_asset(version)
            self._download_asset(asset, archive_path)
            self._extract_executable(archive_path, executable_path)
            self._validate_x64_executable(executable_path)
            # 安装阶段同时验证两种入口，确保唯一二进制可供两种协议共用。
            self._probe(executable_path, kind="cli", expected_version=version)
            self._probe(executable_path, kind="sdk", expected_version=version)
            package = self.package_root / version
            staged_package = temporary_root / "package"
            staged_package.mkdir()
            os.replace(executable_path, staged_package / "codex.exe")
            _atomic_write_json(
                staged_package / "runtime.json",
                {
                    "schema_version": _STATE_SCHEMA,
                    "version": version,
                    "sha256": str(asset["sha256"]),
                    "source": str(asset["url"]),
                    "installed_at": self.clock(),
                },
            )
            os.replace(staged_package, package)
            return (package / "codex.exe").resolve()
        except FileExistsError:
            if target.is_file():
                return target
            raise CodexRuntimeError(f"Codex {version} 的本地目录已存在但安装不完整") from None
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)

    def install_and_switch(self, version: str) -> dict[str, Any]:
        """需要时下载版本，并让 CLI 与 SDK 后续任务共同使用它。"""

        selected = str(version).strip().lower()
        with self._lock:
            if selected == "bundled":
                path = self.bundled_path_factory()
                bundled_version = _package_version("openai-codex-cli-bin")
                if path is None or not _VERSION_PATTERN.fullmatch(bundled_version):
                    raise CodexRuntimeError("安装包内置 Codex 运行时不可用")
                self._probe(path, kind="cli", expected_version=bundled_version)
                self._probe(path, kind="sdk", expected_version=bundled_version)
            else:
                selected = _normalize_version(selected)
                path = self._install(selected)
                self._probe(path, kind="cli", expected_version=selected)
                self._probe(path, kind="sdk", expected_version=selected)
            state = {
                "schema_version": _STATE_SCHEMA,
                "selected": selected,
            }
            self._save_state(state)
            self.apply_selected()
            return self.local_catalog()

    def ensure_available(self, version: str) -> Path:
        """确保指定版本可运行但不改变活动版本，供配置弹窗预览模型目录。"""

        selected = str(version).strip().lower()
        with self._lock:
            if selected == "bundled":
                path = self.bundled_path_factory()
                bundled_version = _package_version("openai-codex-cli-bin")
                if path is None or not _VERSION_PATTERN.fullmatch(bundled_version):
                    raise CodexRuntimeError("安装包内置 Codex 运行时不可用")
                self._probe(path, kind="cli", expected_version=bundled_version)
                self._probe(path, kind="sdk", expected_version=bundled_version)
                return path.resolve()
            selected = _normalize_version(selected)
            path = self._install(selected)
            self._probe(path, kind="cli", expected_version=selected)
            self._probe(path, kind="sdk", expected_version=selected)
            return path.resolve()
