"""快捷部署页面使用的线程安全 Jenkins 业务门面。"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.settings.secrets import KeyringSecretStore, SecretStore

from windows_native.jenkins.client import JenkinsClient
from windows_native.jenkins.config import (
    JenkinsConfigurationRepository,
    insecure_http_warning,
    normalize_jenkins_url,
)
from windows_native.jenkins.errors import JenkinsError
from windows_native.jenkins.models import JenkinsConfiguration, JenkinsProject
from windows_native.jenkins.runner import DeploymentTaskRunner
from windows_native.jenkins.storage import AtomicJsonStore
from windows_native.jenkins.tasks import DeploymentTaskRepository


class JenkinsProjectCache:
    """保存最后一次成功刷新，失败或取消不会破坏旧缓存。"""

    def __init__(self, data_root: Path):
        self._store = AtomicJsonStore(
            Path(data_root) / "data" / "jenkins_projects.json"
        )

    def load(self) -> dict[str, Any]:
        value = self._store.read()
        projects = [
            JenkinsProject.from_dict(item).to_dict()
            for item in value.get("projects") or []
            if isinstance(item, dict)
        ]
        return {
            "last_refreshed_at": str(value.get("last_refreshed_at") or ""),
            "projects": projects,
        }

    def save(self, projects: list[JenkinsProject]) -> dict[str, Any]:
        refreshed_at = datetime.now().astimezone().isoformat(timespec="seconds")
        value = {
            "schema_version": 1,
            "last_refreshed_at": refreshed_at,
            "projects": [item.to_dict() for item in projects],
        }
        self._store.write(value)
        return value


class JenkinsDeploymentService:
    """封装配置、连接验证和只读项目发现。"""

    def __init__(
        self,
        data_root: Path,
        *,
        secrets: SecretStore | None = None,
        client_factory=None,
    ) -> None:
        self.data_root = Path(data_root)
        self.configuration = JenkinsConfigurationRepository(
            self.data_root,
            secrets or KeyringSecretStore(),
        )
        self.cache = JenkinsProjectCache(self.data_root)
        self.task_settings = AtomicJsonStore(
            self.data_root / "data" / "jenkins_task_settings.json"
        )
        self.tasks = DeploymentTaskRepository(self.data_root)
        self._client_factory = client_factory or JenkinsClient
        self.runner = DeploymentTaskRunner(self.tasks, self._configured_client)

    def start(self) -> None:
        self.runner.start()

    def stop(self) -> None:
        self.runner.stop()

    def configuration_view(self) -> dict[str, Any]:
        return self.configuration.view()

    def validate_and_save_configuration(
        self,
        address: str,
        username: str,
        token: str,
        *,
        keep_saved_token: bool = False,
    ) -> dict[str, Any]:
        """验证成功后才替换已保存配置，失败时保留原可用配置。"""

        base_url = normalize_jenkins_url(address)
        if self.tasks.active_count():
            raise JenkinsError(
                "存在进行中的部署任务，不能修改 Jenkins 连接配置",
                code="active_tasks",
            )
        normalized_username = str(username or "").strip()
        if not normalized_username:
            raise JenkinsError("请输入 Jenkins 用户名", code="missing_username")
        normalized_token = str(token or "").strip()
        if not normalized_token and keep_saved_token:
            _saved_configuration, saved_token = self.configuration.load()
            normalized_token = str(saved_token or "").strip()
        if not normalized_token:
            raise JenkinsError("请输入 Jenkins API Token", code="missing_token")
        client = self._client_factory(
            base_url,
            normalized_username,
            normalized_token,
        )
        connection = client.verify_connection()
        configuration = JenkinsConfiguration(base_url, normalized_username)
        self.configuration.save(configuration, normalized_token)
        return {
            **self.configuration.view(),
            "connection": connection,
            "security_warning": insecure_http_warning(base_url),
        }

    def project_snapshot(self) -> dict[str, Any]:
        return self.cache.load()

    def task_settings_view(self) -> dict[str, Any]:
        value = self.task_settings.read()
        return {
            "show_prod": bool(value.get("show_prod", False)),
            "orchestration_mode": "direct_parallel_subtasks",
        }

    def save_task_settings(self, *, show_prod: bool) -> dict[str, Any]:
        value = {
            "schema_version": 2,
            "show_prod": bool(show_prod),
            "orchestration_mode": "direct_parallel_subtasks",
        }
        self.task_settings.write(value)
        return self.task_settings_view()

    def create_deployment_task(
        self,
        iteration_name: str,
        selections: list[dict[str, Any]],
        *,
        schedule: dict[str, Any] | None = None,
        start_immediately: bool = True,
    ) -> dict:
        task = self.tasks.create(
            iteration_name,
            selections,
            schedule=schedule,
            start_immediately=start_immediately,
        )
        if start_immediately:
            self.runner.wake()
        return task

    def create_single_deployment_task(
        self,
        selection: dict[str, Any],
    ) -> dict:
        """创建含环境、项目、分支后缀的单点任务，不拼接项目描述。"""

        environment = str(selection.get("environment") or "").strip()
        project = str(
            selection.get("project_name")
            or str(selection.get("project_full_name") or "").rsplit("/", 1)[-1]
        ).strip()
        branch = str(selection.get("branch") or "").strip()
        iteration_name = f"单点部署-{environment}·{project}·{branch}"
        task = self.tasks.create(
            iteration_name,
            [selection],
            deployment_type="single",
        )
        self.runner.wake()
        return task

    def list_deployment_tasks(self, *, trashed: bool = False) -> list[dict]:
        return self.tasks.list(trashed=trashed)

    def deployment_task(self, task_id: str) -> dict | None:
        return self.tasks.get(task_id)

    def deployment_counts(self) -> dict[str, int]:
        return {
            "active": self.tasks.active_count(),
            "queued": self.tasks.queued_count(),
            "scheduled": self.tasks.scheduled_count(),
        }

    def stop_deployment_task(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError("部署任务不存在")
        if str(task.get("status")) not in {
            "queued",
            "scheduled",
            "running",
            "stopping",
        }:
            return task

        def mutate(value: dict) -> dict:
            value["stop_requested"] = True
            value["status"] = "stopping"
            return value

        updated = self.tasks.update(task_id, mutate)
        self.runner.wake()
        return updated

    def retry_deployment_task(self, task_id: str) -> dict:
        """重新部署前重新读取动态分支，避免触发已经被删除的分支。"""

        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError("部署任务不存在")
        if str(task.get("status")) in {
            "queued",
            "scheduled",
            "running",
            "stopping",
        }:
            raise ValueError("进行中的任务不能重新部署")
        client = self._configured_client()
        selections: list[dict[str, Any]] = []
        for item in task.get("template_items") or task.get("items") or []:
            full_name = str(item.get("project_full_name") or "")
            environment = str(item.get("environment") or "")
            branch = str(item.get("branch") or "")
            project = client.project_details(full_name)
            if environment not in project.environments:
                raise JenkinsError(
                    f"项目 {full_name} 已不再支持环境 {environment}",
                    code="environment_changed",
                )
            if branch not in project.target_branches:
                raise JenkinsError(
                    f"项目 {full_name} 的分支已不存在：{branch}",
                    code="branch_changed",
                )
            selections.append(
                {
                    "environment": environment,
                    "project_full_name": full_name,
                    "project_name": str(item.get("project_name") or ""),
                    "description": str(item.get("description") or ""),
                    "branch": branch,
                }
            )
        # 同一任务只追加执行批次，避免列表中出现一份复制任务。
        retried = self.tasks.redeploy(task_id, selections)
        self.runner.wake()
        return retried

    def trash_deployment_task(self, task_id: str) -> dict:
        return self.tasks.trash(task_id)

    def restore_deployment_task(self, task_id: str) -> dict:
        return self.tasks.restore(task_id)

    def refresh_projects(
        self,
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        configuration, token = self.configuration.load()
        if configuration is None or not token:
            raise JenkinsError(
                "请先配置 Jenkins API Token",
                code="not_configured",
            )
        client = self._client_factory(
            configuration.base_url,
            configuration.username,
            token,
        )
        projects = client.discover_projects(cancel_event=cancel_event)
        return self.cache.save(projects)

    def _configured_client(self) -> JenkinsClient:
        configuration, token = self.configuration.load()
        if configuration is None or not token:
            raise JenkinsError(
                "请先配置 Jenkins API Token",
                code="not_configured",
            )
        return self._client_factory(
            configuration.base_url,
            configuration.username,
            token,
        )
