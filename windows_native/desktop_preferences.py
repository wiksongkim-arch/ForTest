"""ForTest 桌面端专属偏好设置。

该文件只保存原生桌面的外观与任务调度偏好，不修改网页版使用的业务配置。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from filelock import FileLock

from windows_native.paths import app_data_root


THEME_MODES = frozenset({"system", "light", "dark"})
LANGUAGE_MODES = frozenset({"zh_CN", "zh_TW", "en_US"})
DEFAULT_THEME_MODE = "system"
DEFAULT_LANGUAGE = "zh_CN"
DEFAULT_TASK_PARALLELISM = 1
MAX_TASK_PARALLELISM = 8
UPDATE_CHANNELS = frozenset({"stable", "beta"})
CLOSE_BEHAVIORS = frozenset({"ask", "minimize", "quit"})
DEFAULT_CLOSE_BEHAVIOR = "ask"


class DesktopPreferences:
    """以线程安全、原子写入方式管理桌面端偏好。"""

    def __init__(self, data_root: Path | None = None):
        root = Path(data_root) if data_root is not None else app_data_root()
        self.path = root / "data" / "desktop_preferences.json"
        self.lock_path = self.path.with_suffix(".lock")
        self._thread_lock = threading.RLock()

    def get_theme_mode(self) -> str:
        """读取外观模式；无效或缺失值自动回退为跟随系统。"""

        value = self._read().get("appearance", {}).get("theme")
        return str(value) if value in THEME_MODES else DEFAULT_THEME_MODE

    def set_theme_mode(self, mode: str) -> str:
        """保存外观模式并返回规范化后的值。"""

        normalized = str(mode).strip().lower()
        if normalized not in THEME_MODES:
            raise ValueError(f"不支持的外观模式：{mode}")
        self._update_section("appearance", "theme", normalized)
        return normalized

    def get_task_parallelism(self) -> int:
        """读取任务并行数量，默认值为 1。"""

        value = self._read().get("tasks", {}).get("max_parallel")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return DEFAULT_TASK_PARALLELISM
        return min(MAX_TASK_PARALLELISM, max(1, parsed))

    def set_task_parallelism(self, value: int) -> int:
        """保存任务并行数量，并限制在桌面端允许的安全范围内。"""

        parsed = int(value)
        if not 1 <= parsed <= MAX_TASK_PARALLELISM:
            raise ValueError(f"并行数量必须在 1 到 {MAX_TASK_PARALLELISM} 之间")
        self._update_section("tasks", "max_parallel", parsed)
        return parsed

    def get_language(self) -> str:
        """读取界面语言；新增语言只需加入语言目录与允许集合。"""

        value = self._read().get("interface", {}).get("language")
        return str(value) if value in LANGUAGE_MODES else DEFAULT_LANGUAGE

    def set_language(self, language: str) -> str:
        """保存界面语言。"""

        normalized = str(language).strip()
        if normalized not in LANGUAGE_MODES:
            raise ValueError(f"不支持的界面语言：{language}")
        self._update_section("interface", "language", normalized)
        return normalized

    def get_guide_dismissed(self) -> bool:
        """返回用户是否选择不再自动提示配置向导。"""

        return bool(self._read().get("onboarding", {}).get("dismissed", False))

    def set_guide_dismissed(self, dismissed: bool) -> bool:
        """保存配置向导自动提示偏好；手动入口始终保留。"""

        value = bool(dismissed)
        self._update_section("onboarding", "dismissed", value)
        return value

    def get_close_behavior(self) -> str:
        """读取主窗口关闭行为；旧配置默认每次询问，避免意外退出。"""

        value = self._read().get("application", {}).get("close_behavior")
        return str(value) if value in CLOSE_BEHAVIORS else DEFAULT_CLOSE_BEHAVIOR

    def set_close_behavior(self, behavior: str) -> str:
        """保存关闭按钮行为，持久化值保持为不随界面语言变化的稳定枚举。"""

        normalized = str(behavior).strip().lower()
        if normalized not in CLOSE_BEHAVIORS:
            raise ValueError(f"不支持的关闭行为：{behavior}")
        self._update_section("application", "close_behavior", normalized)
        return normalized

    def get_user_profile(self) -> dict[str, str]:
        """读取本地用户卡片；字段为未来登录与会员后端预留。"""

        stored = self._read().get("user", {})
        if not isinstance(stored, dict):
            stored = {}
        return {
            "display_name": str(stored.get("display_name") or "免费用户"),
            "membership": str(stored.get("membership") or "free"),
            "membership_expires_at": str(
                stored.get("membership_expires_at") or "permanent"
            ),
        }

    def get_update_preferences(self) -> dict[str, Any]:
        """读取自动更新预留配置；当前无服务端时不会发起下载。"""

        stored = self._read().get("updates", {})
        if not isinstance(stored, dict):
            stored = {}
        return {
            "enabled": bool(stored.get("enabled", True)),
            "channel": (
                str(stored.get("channel"))
                if stored.get("channel") in UPDATE_CHANNELS
                else "stable"
            ),
            "manifest_url": str(stored.get("manifest_url") or ""),
        }

    def set_update_preferences(
        self,
        *,
        enabled: bool,
        channel: str,
        manifest_url: str,
    ) -> dict[str, Any]:
        """保存原生更新设置；远端清单只接受无用户信息的 HTTPS 地址。"""

        normalized_channel = str(channel).strip().lower()
        if normalized_channel not in UPDATE_CHANNELS:
            raise ValueError("更新通道只能是 stable 或 beta")
        normalized_url = str(manifest_url).strip().rstrip("/")
        if normalized_url:
            parsed = urlsplit(normalized_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("更新清单地址必须是无用户信息的 HTTPS URL")
        values = {
            "enabled": bool(enabled),
            "channel": normalized_channel,
            "manifest_url": normalized_url,
        }
        self._update_section_values("updates", values)
        return values

    def _read(self) -> dict[str, Any]:
        """每次从磁盘读取，保证多个组件实例之间不会使用过期缓存。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, FileLock(str(self.lock_path)):
            return self._read_unlocked()

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _update_section(self, section: str, key: str, value: Any) -> None:
        """保留未知字段，只原子更新指定偏好。"""

        self._update_section_values(section, {key: value})

    def _update_section_values(
        self,
        section: str,
        values: dict[str, Any],
    ) -> None:
        """一次原子写入同一分组的多个值。"""

        with self._thread_lock, FileLock(str(self.lock_path)):
            document = self._read_unlocked()
            section_value = document.get(section)
            if not isinstance(section_value, dict):
                section_value = {}
                document[section] = section_value
            section_value.update(values)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, self.path)
            finally:
                if temporary.exists():
                    temporary.unlink()
