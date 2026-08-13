"""原生桌面端使用的用户数据路径约定。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


USER_DATA_ROOT_ENV = "FORTEST_USER_DATA_DIR"


def user_data_root(
    environment: Mapping[str, str] | None = None,
    *,
    platform_name: str | None = None,
) -> Path:
    """返回仅属于当前用户的配置根目录，不回退到源码或安装目录。"""

    source = environment if environment is not None else os.environ
    override = str(source.get(USER_DATA_ROOT_ENV) or "").strip()
    if override:
        return Path(override).expanduser()

    platform_value = platform_name if platform_name is not None else os.name
    if platform_value == "nt":
        local_app_data = str(source.get("LOCALAPPDATA") or "").strip()
        base = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
    else:
        xdg_data_home = str(source.get("XDG_DATA_HOME") or "").strip()
        base = (
            Path(xdg_data_home)
            if xdg_data_home
            else Path.home() / ".local" / "share"
        )
    return base / "ForTest" / "UserData"


def settings_file_path(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """返回统一设置文件路径。"""

    return user_data_root(environment) / "data" / "settings.json"
