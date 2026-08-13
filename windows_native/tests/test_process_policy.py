"""全局静默子进程策略测试。"""

import os
import subprocess
import sys
import time

from windows_native.process_policy import (
    hidden_popen_kwargs,
    install_hidden_subprocess_policy,
    terminate_owned_subprocesses,
)


def test_existing_creation_flags_are_preserved():
    original = 0x200
    merged = hidden_popen_kwargs({"creationflags": original})
    assert merged["creationflags"] & original
    if os.name == "nt":
        assert merged["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert merged["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert merged["startupinfo"].wShowWindow == subprocess.SW_HIDE


def test_input_dictionary_is_not_mutated():
    source = {"creationflags": 0}
    hidden_popen_kwargs(source)
    assert source == {"creationflags": 0}


def test_installed_popen_remains_a_class_for_asyncio_compatibility():
    install_hidden_subprocess_policy()
    assert isinstance(subprocess.Popen, type)


def test_owned_subprocess_is_reaped_on_desktop_exit():
    """退出清理只处理由当前桌面实例创建且仍存活的子进程。"""

    install_hidden_subprocess_policy()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert process.poll() is None
        stopped = terminate_owned_subprocesses(timeout=1.0)
        deadline = time.monotonic() + 3.0
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process.pid in stopped
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
