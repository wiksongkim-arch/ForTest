"""从项目 GitHub Release 检查、下载并启动可信 Windows 更新包。"""

from __future__ import annotations

import hashlib
import os
import re
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from windows_native.product import PRODUCT_VERSION


_MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
_ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
_VERSION_PATTERN = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$"
)
_SHA256_PATTERN = re.compile(r"(?:sha256:)?([0-9a-fA-F]{64})")


class UpdateError(RuntimeError):
    """可直接展示给用户且不包含响应正文的更新错误。"""


@dataclass(frozen=True)
class UpdateState:
    enabled: bool
    channel: str
    manifest_url: str
    staging_dir: Path


def _normalized_version(value: object) -> str:
    match = _VERSION_PATTERN.fullmatch(str(value or "").strip())
    if match is None:
        raise UpdateError("GitHub Release 版本号格式无效")
    base = ".".join(match.groups()[:3])
    return f"{base}-{match.group(4)}" if match.group(4) else base


def _version_key(value: object) -> tuple[Any, ...]:
    version = _normalized_version(value)
    match = _VERSION_PATTERN.fullmatch(version)
    assert match is not None
    prerelease = match.group(4)
    prerelease_key = (
        ((2, 0, ""),)
        if prerelease is None
        else tuple(
            (0, int(part), "")
            if part.isdigit()
            else (1, 0, part.casefold())
            for part in prerelease.split(".")
        )
    )
    return (*(int(part) for part in match.groups()[:3]), prerelease_key)


class UpdateService:
    """使用 GitHub API 获取发布信息，并在本地校验安装包后启动。"""

    def __init__(
        self,
        data_root: Path,
        preferences,
        *,
        session: requests.Session | Any | None = None,
        launcher: Callable[[str], Any] | None = None,
        current_version: str = PRODUCT_VERSION,
    ) -> None:
        self.preferences = preferences
        self.session = session or requests.Session()
        self.launcher = launcher or self._default_launcher
        self.current_version = _normalized_version(current_version)
        self._staging_dir = Path(data_root) / "updates" / "staging"
        self._headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ForTest-Update-Service",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.reload()

    def reload(self) -> UpdateState:
        values = self.preferences.get_update_preferences()
        self.state = UpdateState(
            enabled=bool(values["enabled"]),
            channel=str(values["channel"]),
            manifest_url=str(values["manifest_url"]),
            staging_dir=self._staging_dir,
        )
        return self.state

    def can_check(self) -> bool:
        """自动检查同时受启用开关与 GitHub 地址约束。"""

        self.reload()
        return self.state.enabled and bool(self.state.manifest_url)

    def check_for_update(self) -> dict[str, Any]:
        """返回最新版本快照；手动检查不受自动更新开关限制。"""

        self.reload()
        owner, repository = self._repository_parts()
        api = f"https://api.github.com/repos/{owner}/{repository}/releases"
        if self.state.channel == "stable":
            payload = self._request_json(f"{api}/latest")
            releases = [payload]
        else:
            payload = self._request_json(f"{api}?per_page=30")
            releases = payload if isinstance(payload, list) else []

        candidates: list[tuple[tuple[Any, ...], str, dict[str, Any]]] = []
        for release in releases:
            if not isinstance(release, dict) or bool(release.get("draft")):
                continue
            if self.state.channel == "stable" and bool(release.get("prerelease")):
                continue
            try:
                version = _normalized_version(release.get("tag_name"))
                candidates.append((_version_key(version), version, release))
            except UpdateError:
                continue
        if not candidates:
            raise UpdateError("GitHub 仓库中没有可用的 Release")

        _key, version, release = max(candidates, key=lambda item: item[0])
        available = _version_key(version) > _version_key(self.current_version)
        result: dict[str, Any] = {
            "available": available,
            "current_version": self.current_version,
            "version": version,
            "published_at": str(release.get("published_at") or ""),
            "release_url": str(release.get("html_url") or ""),
        }
        if available:
            result["asset"] = self._release_asset(
                release,
                owner=owner,
                repository=repository,
                version=version,
            )
        return result

    def download_and_launch(self, update: dict[str, Any]) -> str:
        """下载、校验并启动安装器；任何一步失败都不会执行文件。"""

        path = self._download(update)
        asset = update.get("asset") or {}
        expected = str(asset.get("sha256") or "").casefold()
        if self._sha256(path) != expected:
            raise UpdateError("更新安装包在启动前发生变化")
        self._validate_x64_executable(path)
        try:
            self.launcher(str(path))
        except OSError:
            raise UpdateError("无法启动更新安装包") from None
        return str(path)

    def _repository_parts(self) -> tuple[str, str]:
        parsed = urlsplit(self.state.manifest_url)
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.hostname != "github.com" or len(parts) != 2:
            raise UpdateError("更新链接必须是 GitHub 仓库地址")
        return parts[0], parts[1]

    def _request_json(self, url: str) -> Any:
        try:
            response = self.session.get(
                url,
                headers=self._headers,
                timeout=(10, 30),
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, TypeError, ValueError):
            raise UpdateError("检查更新失败，请确认网络和 GitHub 链接可用") from None

    def _release_asset(
        self,
        release: dict[str, Any],
        *,
        owner: str,
        repository: str,
        version: str,
    ) -> dict[str, Any]:
        expected_name = f"ForTest-Windows-x64-Setup-{version}.exe"
        assets = [item for item in release.get("assets") or [] if isinstance(item, dict)]
        asset = next(
            (item for item in assets if str(item.get("name") or "") == expected_name),
            None,
        )
        if asset is None:
            raise UpdateError(f"ForTest v{version} 缺少 Windows x64 安装包")
        url = str(asset.get("browser_download_url") or "")
        self._validate_release_url(url, owner, repository)
        size = int(asset.get("size") or 0)
        if not 0 < size <= _MAX_DOWNLOAD_BYTES:
            raise UpdateError("更新安装包大小异常")

        digest = str(asset.get("digest") or "")
        match = _SHA256_PATTERN.fullmatch(digest)
        if match is None:
            checksum_asset = next(
                (
                    item
                    for item in assets
                    if str(item.get("name") or "") == f"{expected_name}.sha256"
                ),
                None,
            )
            if checksum_asset is None:
                raise UpdateError("更新 Release 缺少 SHA-256 校验文件")
            checksum_url = str(checksum_asset.get("browser_download_url") or "")
            self._validate_release_url(checksum_url, owner, repository)
            match = _SHA256_PATTERN.search(self._request_checksum(checksum_url))
        if match is None:
            raise UpdateError("更新 Release 的 SHA-256 校验值无效")
        return {
            "name": expected_name,
            "url": url,
            "size": size,
            "sha256": match.group(1).casefold(),
        }

    def _request_checksum(self, url: str) -> str:
        try:
            response = self.session.get(
                url,
                headers=self._headers,
                timeout=(10, 30),
                allow_redirects=True,
            )
            response.raise_for_status()
            self._validate_download_host(str(getattr(response, "url", url)))
            content = bytes(response.content)
            if len(content) > 4096:
                raise UpdateError("更新校验文件过大")
            return content.decode("ascii")
        except UpdateError:
            raise
        except (requests.RequestException, OSError, UnicodeError):
            raise UpdateError("无法读取更新 Release 的 SHA-256 校验值") from None

    def _download(self, update: dict[str, Any]) -> Path:
        version = _normalized_version(update.get("version"))
        if _version_key(version) <= _version_key(self.current_version):
            raise UpdateError("所选版本不高于当前版本")
        asset = update.get("asset")
        if not isinstance(asset, dict):
            raise UpdateError("更新信息缺少安装包")
        expected_name = f"ForTest-Windows-x64-Setup-{version}.exe"
        if str(asset.get("name") or "") != expected_name:
            raise UpdateError("更新安装包名称异常")
        owner, repository = self._repository_parts()
        url = str(asset.get("url") or "")
        self._validate_release_url(url, owner, repository)
        expected_size = int(asset.get("size") or 0)
        expected_digest = str(asset.get("sha256") or "").casefold()
        if not 0 < expected_size <= _MAX_DOWNLOAD_BYTES:
            raise UpdateError("更新安装包大小异常")
        if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
            raise UpdateError("更新安装包 SHA-256 无效")

        self._staging_dir.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=".fortest-update-",
            suffix=".part",
            dir=str(self._staging_dir),
        )
        os.close(handle)
        temporary = Path(temporary_name)
        destination = self._staging_dir / expected_name
        try:
            try:
                response = self.session.get(
                    url,
                    headers=self._headers,
                    timeout=(15, 180),
                    stream=True,
                    allow_redirects=True,
                )
                response.raise_for_status()
                self._validate_download_host(str(getattr(response, "url", url)))
                digest = hashlib.sha256()
                written = 0
                with temporary.open("wb") as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > _MAX_DOWNLOAD_BYTES:
                            raise UpdateError("更新安装包超过允许的最大体积")
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            except UpdateError:
                raise
            except (requests.RequestException, OSError):
                raise UpdateError("更新安装包下载失败") from None
            if written != expected_size:
                raise UpdateError("更新安装包下载不完整")
            if digest.hexdigest() != expected_digest:
                raise UpdateError("更新安装包 SHA-256 校验失败")
            self._validate_x64_executable(temporary)
            os.replace(temporary, destination)
            return destination.resolve()
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_release_url(url: str, owner: str, repository: str) -> None:
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            raise UpdateError("GitHub Release 下载地址异常") from None
        prefix = f"/{owner}/{repository}/releases/download/".casefold()
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or port not in {None, 443}
            or parsed.username
            or parsed.password
            or not parsed.path.casefold().startswith(prefix)
        ):
            raise UpdateError("GitHub Release 下载地址异常")

    @staticmethod
    def _validate_download_host(url: str) -> None:
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            raise UpdateError("更新安装包被重定向到非 GitHub 官方地址") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS
            or port not in {None, 443}
            or parsed.username
            or parsed.password
        ):
            raise UpdateError("更新安装包被重定向到非 GitHub 官方地址")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _validate_x64_executable(path: Path) -> None:
        try:
            with path.open("rb") as stream:
                if stream.read(2) != b"MZ":
                    raise UpdateError("更新安装包缺少 Windows PE 标记")
                stream.seek(0x3C)
                offset = struct.unpack("<I", stream.read(4))[0]
                stream.seek(offset)
                if stream.read(4) != b"PE\x00\x00":
                    raise UpdateError("更新安装包 PE 签名无效")
                if struct.unpack("<H", stream.read(2))[0] != 0x8664:
                    raise UpdateError("更新安装包不是 Windows x64 架构")
        except UpdateError:
            raise
        except (OSError, struct.error):
            raise UpdateError("无法校验更新安装包") from None

    @staticmethod
    def _default_launcher(path: str) -> None:
        starter = getattr(os, "startfile", None)
        if not callable(starter):
            raise OSError("unsupported platform")
        starter(path)
