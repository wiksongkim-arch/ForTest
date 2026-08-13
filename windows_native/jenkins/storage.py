"""Jenkins 非敏感配置与项目缓存的原子持久化。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from filelock import FileLock


class AtomicJsonStore:
    """使用文件锁和同目录替换，避免崩溃留下半份 JSON。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = FileLock(str(self.path) + ".lock")
        self._thread_lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, self.lock:
            if not self.path.exists():
                return {}
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return {}
            return value if isinstance(value, dict) else {}

    def write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, self.lock:
            temporary = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def clear(self) -> None:
        with self._thread_lock, self.lock:
            self.path.unlink(missing_ok=True)
