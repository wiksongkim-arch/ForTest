"""原生桌面端单实例锁的退出、重启和异常恢复测试。"""

from __future__ import annotations

from PySide6.QtCore import QLockFile

from windows_native.single_instance import SingleInstance


def test_cold_relaunch_acquires_immediately_after_normal_release(tmp_path):
    """正常关闭后立即冷启动，不应误报已有实例。"""

    lock_path = tmp_path / "prd-to-case-native.lock"
    first = SingleInstance(lock_path)
    assert first.acquire() is True
    first.release()

    restarted = SingleInstance(lock_path)
    assert restarted.acquire() is True
    restarted.release()
    assert not lock_path.exists()


def test_acquire_recovers_unowned_empty_lock_file(tmp_path):
    """回收 Windows 异常退出后可能遗留的零字节锁文件。"""

    lock_path = tmp_path / "prd-to-case-native.lock"
    lock_path.write_bytes(b"")

    instance = SingleInstance(lock_path)
    assert instance.acquire() is True
    assert lock_path.stat().st_size > 0
    instance.release()
    assert not lock_path.exists()


def test_acquire_never_removes_lock_owned_by_live_instance(tmp_path):
    """存活实例持锁时仍必须拒绝第二个实例，不能误删有效锁。"""

    lock_path = tmp_path / "prd-to-case-native.lock"
    owner = QLockFile(str(lock_path))
    owner.setStaleLockTime(0)
    assert owner.tryLock(0) is True
    try:
        contender = SingleInstance(lock_path)
        assert contender.acquire() is False
        assert lock_path.exists()
        owner_pid, _host, _app = owner.getLockInfo()
        assert owner_pid > 0
    finally:
        owner.unlock()
