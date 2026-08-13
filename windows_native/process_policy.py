"""Windows 子进程显示策略。

本模块只在原生桌面进程内安装策略，不修改网页版的任何代码或行为。
"""

from __future__ import annotations

import os
import atexit
import subprocess
import threading
from collections.abc import Callable
from typing import Any

_LOCK = threading.Lock()
_INSTALLED = False
_ORIGINAL_POPEN: Callable[..., subprocess.Popen[Any]] | None = None
_OWNED_PROCESSES: dict[int, subprocess.Popen[Any]] = {}


def _hidden_startupinfo(existing: Any | None = None) -> Any | None:
    """合并 Windows 隐藏窗口参数；非 Windows 平台保持原值。"""

    if os.name != "nt":
        return existing
    startupinfo = existing or subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return startupinfo


def hidden_popen_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """返回适用于后台工具进程的参数副本。"""

    merged = dict(kwargs)
    if os.name != "nt":
        return merged
    merged["creationflags"] = int(merged.get("creationflags", 0)) | int(
        subprocess.CREATE_NO_WINDOW
    )
    merged["startupinfo"] = _hidden_startupinfo(merged.get("startupinfo"))
    return merged


def install_hidden_subprocess_policy() -> None:
    """统一隐藏 Codex SDK、CLI、Git 等子进程的控制台窗口。"""

    global _INSTALLED, _ORIGINAL_POPEN
    with _LOCK:
        if _INSTALLED:
            return
        _ORIGINAL_POPEN = subprocess.Popen

        original_popen = _ORIGINAL_POPEN

        class HiddenPopen(original_popen):  # type: ignore[misc, valid-type]
            """保持 Popen 类语义，兼容 asyncio 在导入时继续继承它。"""

            def __init__(self, *args: Any, **kwargs: Any):
                super().__init__(*args, **hidden_popen_kwargs(kwargs))
                # 只登记由当前 ForTest 实例直接创建的进程，退出时按进程树回收。
                with _LOCK:
                    finished = [
                        pid
                        for pid, process in _OWNED_PROCESSES.items()
                        if process.poll() is not None
                    ]
                    for pid in finished:
                        _OWNED_PROCESSES.pop(pid, None)
                    _OWNED_PROCESSES[self.pid] = self

        HiddenPopen.__name__ = "Popen"
        HiddenPopen.__qualname__ = "Popen"
        HiddenPopen.__module__ = "subprocess"
        subprocess.Popen = HiddenPopen  # type: ignore[assignment]
        _INSTALLED = True
        atexit.register(terminate_owned_subprocesses)


def is_policy_installed() -> bool:
    """供启动自检与自动化测试确认策略已安装。"""

    return _INSTALLED


def terminate_owned_subprocesses(timeout: float = 3.0) -> list[int]:
    """终止本桌面实例创建且仍存活的完整子进程树。

    使用实际 ``Popen`` 对象确认 PID 仍属于原进程，避免按进程名清理造成误杀；
    psutil 只负责向下遍历该进程的子孙进程。
    """

    try:
        import psutil
    except ImportError:
        return []

    with _LOCK:
        roots = [
            process
            for process in _OWNED_PROCESSES.values()
            if process.poll() is None
        ]
    processes: dict[int, psutil.Process] = {}
    for root in roots:
        try:
            parent = psutil.Process(root.pid)
            for child in parent.children(recursive=True):
                processes[child.pid] = child
            processes[parent.pid] = parent
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue

    stopped = sorted(processes)
    # 先结束最深层子进程，再结束直接子进程，减少孤儿进程窗口。
    ordered = list(processes.values())
    ordered.sort(key=lambda process: _process_depth(process), reverse=True)
    for process in ordered:
        try:
            process.terminate()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    _, survivors = psutil.wait_procs(ordered, timeout=max(0.1, float(timeout)))
    for process in survivors:
        try:
            process.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    if survivors:
        psutil.wait_procs(survivors, timeout=1.0)
    with _LOCK:
        for root in roots:
            root.poll()
            _OWNED_PROCESSES.pop(root.pid, None)
    return stopped


def _process_depth(process: Any) -> int:
    """计算子进程深度；读取失败时仍可按根进程处理。"""

    depth = 0
    current = process
    try:
        while current.ppid() and current.ppid() != os.getpid():
            depth += 1
            current = current.parent()
            if current is None:
                break
    except Exception:
        return depth
    return depth
