"""ForTest 后台更新基础设施。

当前版本没有更新服务端，因此默认清单地址为空且不会联网。未来接入后端后，可复用
此状态模型完成后台检查、下载到用户数据 staging 目录并在重启时安装。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UpdateState:
    enabled: bool
    channel: str
    manifest_url: str
    staging_dir: Path
    restart_install_supported: bool = True


class UpdateService:
    """只描述更新能力；无清单地址时严格保持离线。"""

    def __init__(self, data_root: Path, preferences):
        values = preferences.get_update_preferences()
        self.state = UpdateState(
            enabled=bool(values["enabled"]),
            channel=str(values["channel"]),
            manifest_url=str(values["manifest_url"]),
            staging_dir=Path(data_root) / "updates" / "staging",
        )

    def can_check(self) -> bool:
        """仅在未来配置 HTTPS 清单后允许后台检查。"""

        return self.state.enabled and self.state.manifest_url.startswith("https://")
