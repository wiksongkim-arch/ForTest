"""公共导航入口的协议边界测试。"""

from __future__ import annotations

import pytest

from windows_native.ui import common


@pytest.mark.parametrize(
    "target",
    [
        r"C:\Windows\System32\calc.exe",
        r"\\attacker.invalid\share\payload.exe",
        "file:///C:/Windows/System32/calc.exe",
        "ftp://example.com/payload",
        "https://user:secret@example.com/build/1/",
        "https://example.com\\@attacker.invalid/payload",
    ],
)
def test_remote_open_refuses_local_unc_and_non_http_targets(monkeypatch, target):
    calls = []

    class RecordingDesktopServices:
        @staticmethod
        def openUrl(url):  # noqa: N802 - Qt API
            calls.append(url)
            return True

    monkeypatch.setattr(common, "QDesktopServices", RecordingDesktopServices)

    assert common.is_remote_target(target) is False
    assert common.open_remote_target(target) is False
    assert calls == []


def test_remote_open_keeps_legitimate_http_navigation(monkeypatch):
    calls = []

    class RecordingDesktopServices:
        @staticmethod
        def openUrl(url):  # noqa: N802 - Qt API
            calls.append(url)
            return True

    monkeypatch.setattr(common, "QDesktopServices", RecordingDesktopServices)
    target = "https://jenkins.example.com/job/demo/18/?view=summary#top"

    assert common.is_remote_target(target) is True
    assert common.open_remote_target(target) is True
    assert len(calls) == 1
    assert calls[0].toString() == target
