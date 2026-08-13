"""ForTest 轻量级单实例锁。

该模块刻意不导入主窗口和业务页面，确保重复实例检查不会拖慢启动页首帧。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QMessageBox

from windows_native.i18n import tr
from windows_native.paths import app_data_root


class SingleInstance:
    """使用可自动清理的锁文件阻止重复实例，不创建服务端口。"""

    def __init__(self, lock_path: Path | None = None):
        # 保留旧锁名可阻止升级过程中同时启动两个品牌版本。
        path = lock_path or app_data_root() / "prd-to-case-native.lock"
        self.lock = QLockFile(str(path))
        self._acquired = False
        # 禁止仅因运行时间较长而抢占仍存活的实例；进程已退出或锁文件损坏时，
        # acquire 会通过 QLockFile 的所有者校验安全回收。
        self.lock.setStaleLockTime(0)

    def acquire(self) -> bool:
        if self._acquired:
            return True
        self._acquired = self.lock.tryLock(50)
        if self._acquired:
            return True

        # Windows 在快速退出、强制结束或安全软件短暂介入时，可能只留下一个
        # 无句柄的空锁文件。removeStaleLockFile 会先核验持有进程，不会删除
        # 正由存活实例持有的锁。随后再等待一次也覆盖旧实例恰好退出的竞态。
        self.lock.removeStaleLockFile()
        self._acquired = self.lock.tryLock(250)
        return self._acquired

    def release(self) -> None:
        if not self._acquired:
            return
        self.lock.unlock()
        self._acquired = False


def show_duplicate_instance_message() -> None:
    """使用与原实现一致的提示说明已有实例正在运行。"""

    QMessageBox.information(
        None,
        tr("ForTest 已在运行"),
        tr("程序已经启动，请切换到现有窗口。关闭现有窗口后即可重新启动。"),
    )
