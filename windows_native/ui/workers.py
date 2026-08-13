"""Qt 后台任务封装，避免网络与 AI 调用阻塞界面。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from windows_native.errors import friendly_error


class WorkerSignals(QObject):
    """后台任务的统一信号。"""

    success = Signal(object)
    failed = Signal(str)
    finished = Signal()


class FunctionWorker(QRunnable):
    """在线程池执行任意无参数函数。"""

    def __init__(self, function: Callable[[], Any]):
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.function()
        except BaseException as exc:
            self.signals.failed.emit(friendly_error(exc))
        else:
            self.signals.success.emit(result)
        finally:
            self.signals.finished.emit()
