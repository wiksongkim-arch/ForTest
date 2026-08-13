"""线程安全的业务门面惰性加载器。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any


class LazyNativeService:
    """线程安全地解析完整 backend/Codex 依赖；正式入口会在启动页主动预热。"""

    def __init__(self, data_root: Path):
        self.data_root = data_root
        self._instance: Any | None = None
        self._lock = threading.Lock()

    def _resolve(self):
        if self._instance is not None:
            return self._instance
        with self._lock:
            if self._instance is None:
                from windows_native.native_service import NativeService

                self._instance = NativeService(self.data_root)
        return self._instance

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._resolve(), name)


class LazyJenkinsDeploymentService:
    """线程安全地解析 Jenkins、Keyring 与调度器依赖。"""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self._instance: Any | None = None
        self._resolve_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._background_lock = threading.Lock()
        self._background_thread: threading.Thread | None = None
        self._background_ready = threading.Event()
        self._stop_requested = False

    def _resolve(self):
        if self._instance is not None:
            return self._instance
        with self._resolve_lock:
            if self._instance is None:
                from windows_native.jenkins import JenkinsDeploymentService

                self._instance = JenkinsDeploymentService(self.data_root)
        return self._instance

    def start(self) -> None:
        """解析完成后启动调度；关闭请求先到达时不再创建后台线程。"""

        instance = self._resolve()
        with self._lifecycle_lock:
            if self._stop_requested:
                return
            instance.start()

    def start_in_background(self) -> None:
        """异步启动真实服务，调用方可立即把控制权交还给 Qt 事件循环。"""

        with self._background_lock:
            if self._background_thread is not None:
                return
            self._background_thread = threading.Thread(
                target=self._start_and_signal,
                name="fortester-jenkins-lazy-start",
                daemon=True,
            )
            self._background_thread.start()

    def _start_and_signal(self) -> None:
        """无论启动成功与否都释放首屏预热等待，异常仍由线程诊断信息保留。"""

        try:
            self.start()
        finally:
            self._background_ready.set()

    def wait_until_ready(self, timeout: float = 2.0) -> bool:
        """等待纯本地依赖预热完成；用于避免导入过程与首帧争抢解释器锁。"""

        return self._background_ready.wait(max(0.0, float(timeout)))

    def is_ready(self) -> bool:
        """无阻塞返回后台预热是否结束，供 Qt 事件循环轮询。"""

        return self._background_ready.is_set()

    def stop(self) -> None:
        """未曾使用服务时直接退出，避免关闭应用反而触发重型初始化。"""

        with self._lifecycle_lock:
            self._stop_requested = True
            instance = self._instance
            if instance is not None:
                instance.stop()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._resolve(), name)
