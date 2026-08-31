"""GitHub 更新检查、下载校验与启动契约。"""

from __future__ import annotations

import hashlib
import struct

import pytest

from windows_native.desktop_preferences import DesktopPreferences
from windows_native.update_service import UpdateError, UpdateService


def _x64_pe() -> bytes:
    """构造只包含校验所需头部的最小 x64 PE 测试文件。"""

    value = bytearray(512)
    value[:2] = b"MZ"
    struct.pack_into("<I", value, 0x3C, 0x80)
    value[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", value, 0x84, 0x8664)
    return bytes(value)


class _Response:
    def __init__(self, *, payload=None, content=b"", url=""):
        self._payload = payload
        self.content = content
        self.url = url

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield self.content


class _Session:
    def __init__(self, installer: bytes):
        self.installer = installer
        self.digest = hashlib.sha256(installer).hexdigest()
        self.download_host = "release-assets.githubusercontent.com"

    def get(self, url: str, **_kwargs):
        if "api.github.com" in url:
            name = "ForTest-Windows-x64-Setup-0.2.15.exe"
            return _Response(
                payload={
                    "tag_name": "v0.2.15",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-09-01T00:00:00Z",
                    "html_url": "https://github.com/wiksongkim-arch/ForTest/releases/tag/v0.2.15",
                    "assets": [
                        {
                            "name": name,
                            "browser_download_url": (
                                "https://github.com/wiksongkim-arch/ForTest/"
                                f"releases/download/v0.2.15/{name}"
                            ),
                            "size": len(self.installer),
                            "digest": f"sha256:{self.digest}",
                        }
                    ],
                },
                url=url,
            )
        return _Response(
            content=self.installer,
            url=f"https://{self.download_host}/release.exe",
        )


def test_update_download_is_verified_before_launcher_runs(tmp_path):
    session = _Session(_x64_pe())
    launched: list[str] = []
    service = UpdateService(
        tmp_path,
        DesktopPreferences(tmp_path),
        session=session,
        launcher=launched.append,
        current_version="0.2.14",
    )

    update = service.check_for_update()
    installed = service.download_and_launch(update)

    assert update["available"] is True
    assert update["version"] == "0.2.15"
    assert launched == [installed]
    assert installed.endswith("ForTest-Windows-x64-Setup-0.2.15.exe")


def test_update_rejects_non_github_redirect_before_launch(tmp_path):
    session = _Session(_x64_pe())
    session.download_host = "downloads.example.test"
    launched: list[str] = []
    service = UpdateService(
        tmp_path,
        DesktopPreferences(tmp_path),
        session=session,
        launcher=launched.append,
        current_version="0.2.14",
    )

    with pytest.raises(UpdateError, match="非 GitHub 官方地址"):
        service.download_and_launch(service.check_for_update())

    assert launched == []
