"""Jenkins 集成的可展示异常。"""

from __future__ import annotations


class JenkinsError(RuntimeError):
    """携带稳定错误代码，界面可以显示具体原因而不暴露敏感信息。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "jenkins_error",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class JenkinsRefreshCancelled(JenkinsError):
    """新刷新任务替换旧任务时使用的内部控制异常。"""

    def __init__(self) -> None:
        super().__init__("项目刷新已被新的刷新任务替换", code="refresh_cancelled")
