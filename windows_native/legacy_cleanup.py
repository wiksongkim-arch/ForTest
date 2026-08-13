"""清理 1.x WebView 桌面版可能遗留的本地服务进程。"""

from __future__ import annotations

import os
from pathlib import Path

import psutil


def cleanup_previous_native_application() -> list[int]:
    """仅关闭默认旧安装目录中的无参数历史品牌主程序。

    该函数只在 ForTest 获取单实例锁失败时调用，用于解决覆盖升级后旧主进程
    持有锁的问题；不会按名称清理其他目录中的同名程序。
    """

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return []
    programs = Path(local_app_data) / "Programs"
    expected = {
        (programs / "QAQ" / "QAQ.exe").resolve(),
        (programs / "ForTester" / "ForTester.exe").resolve(),
        (programs / "PRDtoCASE" / "PRDtoCASE.exe").resolve(),
    }
    current_pid = os.getpid()
    candidates: list[psutil.Process] = []
    for process in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            if process.pid == current_pid:
                continue
            executable = Path(str(process.info.get("exe") or "")).resolve()
            if executable not in expected:
                continue
            command = [str(item) for item in (process.info.get("cmdline") or [])]
            if any(item.lower() == "--service" for item in command):
                continue
            candidates.extend(process.children(recursive=True))
            candidates.append(process)
        except (OSError, psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return _stop_processes(candidates)


def _stop_processes(processes: list[psutil.Process]) -> list[int]:
    """先请求正常退出，超时后只强制结束已确认的旧进程树。"""

    unique = {process.pid: process for process in processes}
    ordered = list(unique.values())
    stopped = sorted(unique)
    for process in ordered:
        try:
            process.terminate()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    _, survivors = psutil.wait_procs(ordered, timeout=2.0)
    for process in survivors:
        try:
            process.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    if survivors:
        psutil.wait_procs(survivors, timeout=1.0)
    return stopped


def cleanup_legacy_desktop_processes() -> list[int]:
    """只清理带旧版 --service 标记的 PRDtoCASE 进程及其旧父进程。"""

    current_pid = os.getpid()
    services: list[psutil.Process] = []
    parent_ids: set[int] = set()
    for process in psutil.process_iter(["pid", "name", "exe", "cmdline", "ppid"]):
        try:
            info = process.info
            if info["pid"] == current_pid:
                continue
            executable = str(info.get("exe") or "")
            name = str(info.get("name") or "")
            command = [str(item) for item in (info.get("cmdline") or [])]
            lowered = [item.lower() for item in command]
            if Path(executable or name).name.lower() != "prdtocase.exe":
                continue
            if "--service" not in lowered:
                continue
            index = lowered.index("--service")
            role = lowered[index + 1] if index + 1 < len(lowered) else ""
            if role not in {"backend", "frontend"}:
                continue
            services.append(process)
            parent_ids.add(int(info.get("ppid") or 0))
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue

    stopped: list[int] = []
    for process in services:
        try:
            stopped.append(process.pid)
            process.terminate()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    _, survivors = psutil.wait_procs(services, timeout=2.0)
    for process in survivors:
        try:
            process.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass

    # 旧父进程失去服务后没有继续保留的价值，且会占据旧窗口。
    for parent_id in parent_ids:
        if not parent_id or parent_id == current_pid:
            continue
        try:
            parent = psutil.Process(parent_id)
            if Path(parent.exe()).name.lower() != "prdtocase.exe":
                continue
            stopped.append(parent.pid)
            parent.terminate()
            try:
                parent.wait(timeout=2.0)
            except psutil.TimeoutExpired:
                parent.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return stopped
