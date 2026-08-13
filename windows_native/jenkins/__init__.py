"""ForTest 快捷部署使用的 Jenkins 原生 API 集成。"""

from __future__ import annotations

from typing import Any

__all__ = ["JenkinsDeploymentService"]


def __getattr__(name: str) -> Any:
    """只在真正需要业务服务时导入 Keyring 与 Jenkins 客户端依赖。"""

    if name != "JenkinsDeploymentService":
        raise AttributeError(name)
    from windows_native.jenkins.service import JenkinsDeploymentService

    return JenkinsDeploymentService
