"""打包启动自检必须独立于正式实例和正式任务数据。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile


def test_local_backup_smoke_isolated_from_user_data(tmp_path):
    """冻结包自检只写一次性目录，并覆盖 openpyxl 的真实导入与保存。"""

    project_root = Path(__file__).resolve().parents[2]
    diagnostics = tmp_path / "backup-diagnostics.json"
    local_app_data = tmp_path / "LocalAppData"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "windows_native.main",
            "--backup-smoke-test",
            "--diagnostics-file",
            str(diagnostics),
        ],
        cwd=project_root,
        env={**os.environ, "LOCALAPPDATA": str(local_app_data)},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(diagnostics.read_text(encoding="utf-8"))
    assert payload == {"success": True, "error_type": None}
    assert not (local_app_data / "ForTest" / "UserData").exists()


def test_smoke_test_skips_user_instance_lock_and_uses_isolated_data(tmp_path):
    """已有实例持锁时，自检仍应快速退出且不进入隐藏消息框。"""

    project_root = Path(__file__).resolve().parents[2]
    local_app_data = tmp_path / "LocalAppData"
    user_data_root = local_app_data / "ForTest" / "UserData"
    user_data_root.mkdir(parents=True)
    lock = QLockFile(str(user_data_root / "prd-to-case-native.lock"))
    lock.setStaleLockTime(0)
    assert lock.tryLock(0)

    diagnostics = tmp_path / "packaged-diagnostics.json"
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(local_app_data)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "windows_native.main",
                "--smoke-test",
                "--diagnostics-file",
                str(diagnostics),
            ],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    finally:
        lock.unlock()

    assert result.returncode == 0, result.stderr
    payload = json.loads(diagnostics.read_text(encoding="utf-8"))
    diagnostics_root = Path(payload["data_root"])
    assert payload["diagnostics_isolated"] is True
    assert payload["product"] == "ForTest"
    assert payload["version"] == "0.2.16"
    assert payload["backend_runtime_loaded"] is False
    assert payload["codex_runtime_loaded"] is False
    assert payload["first_paint_seconds"] < 3.0
    assert payload["splash_first_paint_seconds"] < 1.5
    assert payload["startup_heartbeat_ticks"] > 0
    assert payload["deployment_ready_before_main"] is True
    assert diagnostics_root != user_data_root
    assert not diagnostics_root.exists()


def test_full_startup_smoke_preloads_backend_before_main_and_keeps_heartbeat(tmp_path):
    """完整自检必须覆盖真实本地预热，并证明主窗口出现后事件循环持续响应。"""

    project_root = Path(__file__).resolve().parents[2]
    diagnostics = tmp_path / "full-startup-diagnostics.json"
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(tmp_path / "LocalAppData")
    environment["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "windows_native.main",
            "--full-startup-smoke",
            "--diagnostics-file",
            str(diagnostics),
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        # 外层等待必须大于产品的 45 秒启动门禁，避免在断言前被测试框架误杀。
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(diagnostics.read_text(encoding="utf-8"))
    assert payload["diagnostics_isolated"] is True
    assert payload["full_startup_smoke"] is True
    assert payload["backend_runtime_loaded"] is True
    assert payload["startup_preload_complete"] is True
    assert payload["startup_snapshot_applied"] is True
    assert payload["backend_ready_before_main"] is True
    assert payload["deployment_ready_before_main"] is True
    assert payload["startup_heartbeat_ticks"] > 0
    assert payload["startup_max_heartbeat_gap_seconds"] < 2.5
    assert payload["post_show_heartbeat_ticks"] >= 20
    assert payload["post_show_max_heartbeat_gap_seconds"] < 0.5
    assert payload["post_show_threadpool_peak"] == 0
    assert payload["startup_seconds"] < 45
