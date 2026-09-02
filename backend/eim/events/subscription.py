"""DWS 长连接消费生命周期：ready 门禁、持续排空和干净停止。"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from backend.eim.connections.dws_runtime import DWSRuntime, DWSRuntimeError
from backend.security.redaction import redact_text


_READY = re.compile(r"^\[event\] ready\b")
_SUBSCRIPTION = re.compile(r"\bsubscribe_id=([^\s]+)")


class SubscriptionStartError(DWSRuntimeError):
    """携带 DWS 创建预算信号的启动失败。"""

    def __init__(self, message: str, *, retryable: bool | None, retry_after: float):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class DingTalkSubscription:
    """每个群一个 DWS consumer；事件只在正式 ready 后交给业务层。"""

    def __init__(
        self,
        runtime: DWSRuntime,
        *,
        connection_id: str,
        profile: str,
        conversation_id: str,
        events: tuple[str, ...],
        on_event: Callable[[dict[str, Any]], None],
        on_error: Callable[[str], None] | None = None,
    ):
        if not events or any(item not in {"message", "reaction"} for item in events):
            raise ValueError("EIM P0 订阅只支持 message 和 reaction")
        self.runtime = runtime
        self.connection_id = connection_id
        self.profile = profile
        self.conversation_id = conversation_id
        self.events = events
        self.on_event = on_event
        self.on_error = on_error or (lambda _message: None)
        self.process: subprocess.Popen[str] | None = None
        self.ready = threading.Event()
        self.exited = threading.Event()
        self._stopping = threading.Event()
        self._stderr: deque[str] = deque(maxlen=100)
        self._pending: deque[dict[str, Any]] = deque(maxlen=1_000)
        self._subscribe_ids: set[str] = set()
        self._threads: list[threading.Thread] = []

    def start(self, timeout: float = 60) -> None:
        if self.process is not None:
            raise RuntimeError("DWS 订阅已经启动")
        self.process = self.runtime.popen(
            [
                "event",
                "+listen-im",
                "--kind",
                "group",
                "--chat-id",
                self.conversation_id,
                "--events",
                ",".join(self.events),
                "--profile",
                self.profile,
                "-f",
                "ndjson",
            ],
            config_dir=self.runtime.config_dir(self.connection_id),
        )
        if self.process.stdout is None or self.process.stderr is None:
            raise SubscriptionStartError("DWS 未创建输出管道", retryable=False, retry_after=0)
        self._threads = [
            threading.Thread(target=self._read_stdout, name="eim-dws-stdout", daemon=True),
            threading.Thread(target=self._read_stderr, name="eim-dws-stderr", daemon=True),
            threading.Thread(target=self._wait_process, name="eim-dws-wait", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        deadline = time.monotonic() + max(1.0, min(float(timeout), 300.0))
        while not self.ready.is_set() and not self.exited.is_set() and time.monotonic() < deadline:
            self.ready.wait(0.05)
        if not self.ready.is_set():
            detail = "\n".join(self._stderr) or "DWS 未返回 ready 标记"
            retryable, retry_after = _retry_metadata(detail)
            self.stop()
            raise SubscriptionStartError(
                f"DWS 订阅未就绪：{redact_text(detail)}",
                retryable=retryable,
                retry_after=retry_after,
            )

    def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
                if not isinstance(value, dict):
                    raise ValueError("事件根节点不是对象")
            except (json.JSONDecodeError, ValueError) as exc:
                self.on_error(f"DWS 事件行解析失败：{redact_text(exc)}")
                continue
            if not self.ready.is_set():
                if len(self._pending) == self._pending.maxlen:
                    self.on_error("DWS ready 前事件缓冲区已满")
                self._pending.append(value)
                continue
            self._dispatch(value)

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        for line in self.process.stderr:
            text = line.strip()
            if not text:
                continue
            match = _SUBSCRIPTION.search(text)
            if match:
                self._subscribe_ids.add(match.group(1))
            if _READY.search(text):
                self.ready.set()
                while self._pending:
                    self._dispatch(self._pending.popleft())
            else:
                self._stderr.append(text)

    def _dispatch(self, value: dict[str, Any]) -> None:
        try:
            self.on_event(value)
        except Exception as exc:
            self.on_error(f"EIM 事件处理失败：{redact_text(exc)}")

    def _wait_process(self) -> None:
        assert self.process
        self.process.wait()
        self.exited.set()

    def stop(self, timeout: float = 8) -> None:
        if self._stopping.is_set():
            self.exited.wait(max(0.1, timeout))
            return
        self._stopping.set()
        process = self.process
        if process is None:
            self.exited.set()
            return
        if process.stdin is not None:
            try:
                process.stdin.close()  # DWS 把 stdin EOF 作为可清理的停止信号。
            except OSError:
                pass
        try:
            process.wait(timeout=max(0.1, min(timeout, 5.0)))
        except subprocess.TimeoutExpired:
            for subscribe_id in sorted(self._subscribe_ids):
                try:
                    self.runtime.run(
                        ["event", "stop", subscribe_id, "--yes", "--profile", self.profile],
                        config_dir=self.runtime.config_dir(self.connection_id),
                        timeout=10,
                    )
                except DWSRuntimeError as exc:
                    self.on_error(str(exc))
            try:
                process.wait(timeout=max(0.1, timeout - 5.0))
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        self.exited.set()


def _retry_metadata(detail: str) -> tuple[bool | None, float]:
    lowered = detail.casefold()
    retryable: bool | None = None
    if "retryable=false" in lowered or '"retryable":false' in lowered.replace(" ", ""):
        retryable = False
    elif "retryable=true" in lowered or '"retryable":true' in lowered.replace(" ", ""):
        retryable = True
    match = re.search(r"retry after:\s*(\d+)s", detail, re.IGNORECASE)
    return retryable, float(match.group(1)) if match else 0.0
