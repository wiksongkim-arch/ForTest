"""最小权限、只依赖 Jenkins Remote API 的客户端。"""

from __future__ import annotations

import json
import re
import threading
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import requests

from windows_native.jenkins.errors import JenkinsError, JenkinsRefreshCancelled
from windows_native.jenkins.models import JenkinsParameter, JenkinsProject


_CONTAINER_CLASSES = (
    "Folder",
    "OrganizationFolder",
    "WorkflowMultiBranchProject",
)


class JenkinsClient:
    """使用用户名和 API Token 进行预先认证，拒绝跨主机重定向。"""

    def __init__(
        self,
        base_url: str,
        username: str,
        token: str,
        *,
        session: requests.Session | Any | None = None,
        timeout: tuple[float, float] = (5.0, 20.0),
    ) -> None:
        self.base_url = base_url
        self.username = str(username).strip()
        self._token = str(token).strip()
        self.timeout = timeout
        self.session = session or requests.Session()
        # Jenkins 凭据只能发往已配置的目标主机，不继承系统代理以免泄露或被失效代理阻断。
        if hasattr(self.session, "trust_env"):
            self.session.trust_env = False
        self.session.auth = (self.username, self._token)
        headers = getattr(self.session, "headers", None)
        if headers is not None:
            headers.update(
                {
                    "Accept": "application/json",
                    "User-Agent": "ForTest-QuickDeploy/1.0",
                }
            )

    def verify_connection(self) -> dict[str, Any]:
        """只执行 GET，验证身份、API 可见性和 Jenkins 版本。"""

        who_response, who = self._get_json("whoAmI/api/json")
        if not bool(who.get("authenticated")):
            raise JenkinsError(
                "Jenkins 未接受当前用户名和 API Token",
                code="authentication_failed",
                status_code=401,
            )
        root_response, root = self._get_json(
            "api/json",
            params={"tree": "nodeName,nodeDescription,mode,numExecutors"},
        )
        version = str(
            root_response.headers.get("X-Jenkins")
            or who_response.headers.get("X-Jenkins")
            or ""
        )
        return {
            "authenticated": True,
            "username": str(who.get("name") or self.username),
            "authorities": [
                str(item)
                for item in who.get("authorities") or []
                if str(item).strip()
            ],
            "version": version,
            "node_name": str(root.get("nodeName") or ""),
        }

    def discover_projects(
        self,
        *,
        cancel_event: threading.Event | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> list[JenkinsProject]:
        """遍历文件夹并读取参数化任务，不执行任何 Jenkins 写操作。"""

        pending: deque[str] = deque([""])
        visited: set[str] = set()
        projects: list[JenkinsProject] = []
        while pending:
            self._raise_if_cancelled(cancel_event)
            folder_name = pending.popleft()
            if folder_name in visited:
                continue
            visited.add(folder_name)
            _response, payload = self._get_json(
                self._job_api_path(folder_name),
                params={
                    "tree": (
                        "jobs[name,fullName,url,description,_class,buildable,"
                        "actions[_class,parameterDefinitions["
                        "_class,name,type,description,choices,defaultValue,"
                        "defaultParameterValue[value,name]]]]"
                    )
                },
            )
            for raw_job in payload.get("jobs") or []:
                self._raise_if_cancelled(cancel_event)
                if not isinstance(raw_job, dict):
                    continue
                name = str(raw_job.get("name") or "").strip()
                full_name = str(raw_job.get("fullName") or "").strip()
                if not full_name:
                    full_name = f"{folder_name}/{name}".strip("/")
                project_class = str(raw_job.get("_class") or "")
                if any(marker in project_class for marker in _CONTAINER_CLASSES):
                    if full_name:
                        pending.append(full_name)
                    continue
                if not full_name:
                    continue
                project = self._project_from_api(raw_job, full_name)
                project = self._hydrate_dynamic_choices(project)
                projects.append(project)
                if progress is not None:
                    progress(len(projects))
        projects.sort(key=lambda item: item.full_name.casefold())
        return projects

    def project_details(self, full_name: str) -> JenkinsProject:
        """读取单个任务及动态参数，用于触发或重部署前的最终校验。"""

        _response, payload = self._get_json(
            self._job_api_path(full_name),
            params={
                "tree": (
                    "name,fullName,url,description,_class,buildable,"
                    "actions[_class,parameterDefinitions["
                    "_class,name,type,description,choices,defaultValue,"
                    "defaultParameterValue[value,name]]]"
                )
            },
        )
        project = self._project_from_api(payload, full_name)
        return self._hydrate_dynamic_choices(project)

    def trigger_build(
        self,
        full_name: str,
        *,
        environment: str,
        branch: str,
    ) -> dict[str, Any]:
        """触发参数化构建并返回 Jenkins queue item 标识。"""

        response = self._post(
            f"{self._job_base_path(full_name)}/buildWithParameters",
            data={"ENV_NAME": environment, "TARGET_BRANCH": branch},
            accepted={200, 201, 202, 302, 303},
        )
        location = str(response.headers.get("Location") or "")
        match = re.search(r"/queue/item/(\d+)/?", location)
        if match is None:
            raise JenkinsError(
                "Jenkins 已接受构建请求，但没有返回队列编号",
                code="missing_queue_location",
            )
        return {
            "queue_id": int(match.group(1)),
            "queue_url": self._validated_server_url(location),
        }

    def queue_item(self, queue_id: int) -> dict[str, Any]:
        _response, value = self._get_json(
            f"queue/item/{int(queue_id)}/api/json",
            params={
                "tree": "id,cancelled,why,blocked,stuck,executable[number,url]"
            },
        )
        executable = value.get("executable")
        return {
            "id": int(value.get("id") or queue_id),
            "cancelled": bool(value.get("cancelled")),
            "why": str(value.get("why") or ""),
            "blocked": bool(value.get("blocked")),
            "stuck": bool(value.get("stuck")),
            "build_number": int(executable.get("number"))
            if isinstance(executable, dict) and executable.get("number") is not None
            else None,
            "build_url": self._validated_server_url(executable.get("url"))
            if isinstance(executable, dict)
            else "",
        }

    def build_status(self, full_name: str, build_number: int) -> dict[str, Any]:
        _response, value = self._get_json(
            f"{self._job_base_path(full_name)}/{int(build_number)}/api/json",
            params={
                "tree": (
                    "number,url,building,result,duration,timestamp,"
                    "estimatedDuration,displayName,description"
                )
            },
        )
        return {
            "number": int(value.get("number") or build_number),
            "url": self._validated_server_url(value.get("url")),
            "building": bool(value.get("building")),
            "result": str(value.get("result") or ""),
            "duration_ms": int(value.get("duration") or 0),
            "timestamp_ms": int(value.get("timestamp") or 0),
            "estimated_duration_ms": int(value.get("estimatedDuration") or 0),
        }

    def cancel_queue_item(self, queue_id: int) -> None:
        self._post(
            "queue/cancelItem",
            data={"id": str(int(queue_id))},
            accepted={200, 201, 202, 302, 303},
        )

    def stop_build(self, full_name: str, build_number: int) -> None:
        self._post(
            f"{self._job_base_path(full_name)}/{int(build_number)}/stop",
            accepted={200, 201, 202, 302, 303},
        )

    def _project_from_api(self, raw: dict[str, Any], full_name: str) -> JenkinsProject:
        definitions: list[dict[str, Any]] = []
        for action in raw.get("actions") or []:
            if not isinstance(action, dict):
                continue
            current = action.get("parameterDefinitions")
            if isinstance(current, list):
                definitions.extend(item for item in current if isinstance(item, dict))
        parameters = tuple(
            JenkinsClient._parameter_from_api(item)
            for item in definitions
            if str(item.get("name") or "").strip()
        )
        return JenkinsProject(
            full_name=full_name,
            name=str(raw.get("name") or full_name.rsplit("/", 1)[-1]),
            description=str(raw.get("description") or "").strip(),
            url=self._validated_server_url(raw.get("url")),
            project_class=str(raw.get("_class") or ""),
            buildable=bool(raw.get("buildable", True)),
            parameters=parameters,
        )

    def _hydrate_dynamic_choices(self, project: JenkinsProject) -> JenkinsProject:
        """读取 Git Parameter 的动态分支列表，普通参数不增加额外请求。"""

        hydrated: list[JenkinsParameter] = []
        for parameter in project.parameters:
            kind = parameter.kind.lower()
            if "gitparameter" not in kind:
                hydrated.append(parameter)
                continue
            path = (
                f"{self._job_base_path(project.full_name)}/descriptorByName/"
                f"{quote(parameter.kind, safe='.')}/fillValueItems"
            )
            _response, payload = self._post_json_readonly(
                path,
                params={"param": parameter.name},
            )
            raw_values = payload.get("values") or payload.get("items") or []
            choices: list[str] = []
            for item in raw_values:
                if isinstance(item, dict):
                    value = str(item.get("value") or item.get("name") or "").strip()
                else:
                    value = str(item).strip()
                if value and value not in choices:
                    choices.append(value)
            hydrated.append(
                JenkinsParameter(
                    name=parameter.name,
                    kind=parameter.kind,
                    description=parameter.description,
                    choices=tuple(choices) or parameter.choices,
                    default=parameter.default,
                )
            )
        return JenkinsProject(
            full_name=project.full_name,
            name=project.name,
            description=project.description,
            url=project.url,
            project_class=project.project_class,
            buildable=project.buildable,
            parameters=tuple(hydrated),
        )

    @staticmethod
    def _parameter_from_api(raw: dict[str, Any]) -> JenkinsParameter:
        raw_choices = raw.get("choices")
        if isinstance(raw_choices, str):
            choices = [item.strip() for item in raw_choices.splitlines()]
        elif isinstance(raw_choices, list):
            choices = [str(item).strip() for item in raw_choices]
        else:
            choices = []
        default_value = raw.get("defaultValue")
        if default_value is None:
            default = raw.get("defaultParameterValue")
            default_value = default.get("value") if isinstance(default, dict) else ""
        parameter_type = str(raw.get("type") or "").strip()
        parameter_class = str(raw.get("_class") or "").strip()
        # Git Parameter 同时返回 PT_BRANCH 类型与完整描述器类名；动态接口必须使用类名。
        kind = (
            parameter_class
            if "gitparameter" in parameter_class.casefold()
            else parameter_type or parameter_class
        )
        return JenkinsParameter(
            name=str(raw.get("name") or "").strip(),
            kind=kind,
            description=str(raw.get("description") or "").strip(),
            choices=tuple(item for item in choices if item),
            default=str(default_value or "").strip(),
        )

    def _job_api_path(self, full_name: str) -> str:
        if not full_name:
            return "api/json"
        return f"{self._job_base_path(full_name)}/api/json"

    @staticmethod
    def _job_base_path(full_name: str) -> str:
        return "/".join(
            f"job/{quote(part, safe='')}"
            for part in full_name.split("/")
            if part
        )

    def _get_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        url = urljoin(self.base_url, path)
        try:
            response = self.session.get(
                url,
                params=params,
                timeout=self.timeout,
                allow_redirects=False,
            )
            if 300 <= int(response.status_code) < 400:
                location = str(response.headers.get("Location") or "")
                redirected = urljoin(url, location)
                if not self._same_origin(url, redirected):
                    raise JenkinsError(
                        "Jenkins 将请求重定向到其他主机，已拒绝发送凭据",
                        code="unsafe_redirect",
                    )
                response = self.session.get(
                    redirected,
                    params=params,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
        except requests.exceptions.SSLError as exc:
            raise JenkinsError(
                "Jenkins HTTPS 证书校验失败，请检查证书链或地址",
                code="tls_error",
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise JenkinsError(
                "连接 Jenkins 超时，请检查地址、网络或防火墙",
                code="timeout",
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise JenkinsError(
                "无法连接 Jenkins，请检查地址、端口、网络或服务状态",
                code="connection_failed",
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise JenkinsError("Jenkins 请求失败", code="request_failed") from exc

        status = int(response.status_code)
        if status == 401:
            raise JenkinsError(
                "用户名或 API Token 无效",
                code="authentication_failed",
                status_code=status,
            )
        if status == 403:
            raise JenkinsError(
                "账号权限不足：至少需要 Overall/Read 和目标任务的 Job/Read",
                code="permission_denied",
                status_code=status,
            )
        if status == 404:
            raise JenkinsError(
                "Jenkins 地址或目标任务不存在",
                code="not_found",
                status_code=status,
            )
        if status >= 400:
            raise JenkinsError(
                f"Jenkins 返回 HTTP {status}",
                code="http_error",
                status_code=status,
            )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise JenkinsError(
                "Jenkins 返回内容不是有效 JSON，请确认填写的是 Jenkins 根地址",
                code="invalid_response",
            ) from exc
        if not isinstance(payload, dict):
            raise JenkinsError("Jenkins 返回数据格式异常", code="invalid_response")
        return response, payload

    def _post_json_readonly(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """兼容 Jenkins 表单填充接口；该 POST 只读取选项，不修改服务端。"""

        if not path.endswith("/fillValueItems"):
            raise JenkinsError(
                "拒绝通过只读接口访问非项目选项地址",
                code="unsafe_read_endpoint",
            )
        response = self._post(
            path,
            params=params,
            data={},
            accepted={200},
        )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise JenkinsError(
                "Jenkins 项目分支接口返回内容不是有效 JSON",
                code="invalid_response",
            ) from exc
        if not isinstance(payload, dict):
            raise JenkinsError(
                "Jenkins 项目分支接口返回数据格式异常",
                code="invalid_response",
            )
        return response, payload

    def _post(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        accepted: set[int],
    ) -> Any:
        url = urljoin(self.base_url, path)
        try:
            response = self.session.post(
                url,
                params=params,
                data=data,
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.exceptions.SSLError as exc:
            raise JenkinsError(
                "Jenkins HTTPS 证书校验失败，请检查证书链或地址",
                code="tls_error",
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise JenkinsError(
                "连接 Jenkins 超时，请检查地址、网络或防火墙",
                code="timeout",
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise JenkinsError(
                "无法连接 Jenkins，请检查地址、端口、网络或服务状态",
                code="connection_failed",
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise JenkinsError("Jenkins 请求失败", code="request_failed") from exc
        status = int(response.status_code)
        if status not in accepted:
            self._raise_http_error(status)
        location = str(response.headers.get("Location") or "")
        if location and not self._same_origin(url, urljoin(url, location)):
            raise JenkinsError(
                "Jenkins 返回了其他主机的地址，已拒绝继续发送凭据",
                code="unsafe_redirect",
            )
        return response

    @staticmethod
    def _raise_http_error(status: int) -> None:
        if status == 401:
            raise JenkinsError(
                "用户名或 API Token 无效",
                code="authentication_failed",
                status_code=status,
            )
        if status == 403:
            raise JenkinsError(
                "账号权限不足：触发任务需要 Job/Build，停止任务需要 Job/Cancel",
                code="permission_denied",
                status_code=status,
            )
        if status == 404:
            raise JenkinsError(
                "Jenkins 目标任务或构建不存在",
                code="not_found",
                status_code=status,
            )
        raise JenkinsError(
            f"Jenkins 返回 HTTP {status}",
            code="http_error",
            status_code=status,
        )

    def _validated_server_url(self, value: Any) -> str:
        """仅保留与已配置 Jenkins 同源的 HTTP(S) 导航地址。"""

        if not isinstance(value, str):
            return ""
        raw = str(value or "").strip()
        if not raw or any(
            character == "\\"
            or ord(character) <= 0x20
            or ord(character) == 0x7F
            for character in raw
        ):
            return ""
        try:
            normalized = urljoin(self.base_url, raw)
            parsed = urlsplit(normalized)
            _ = parsed.port
        except (TypeError, ValueError):
            return ""
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or not self._same_origin(self.base_url, normalized)
        ):
            return ""
        return normalized

    @staticmethod
    def _same_origin(first: str, second: str) -> bool:
        try:
            a = urlsplit(first)
            b = urlsplit(second)
            return (
                a.scheme.lower(),
                (a.hostname or "").lower(),
                a.port or (443 if a.scheme == "https" else 80),
            ) == (
                b.scheme.lower(),
                (b.hostname or "").lower(),
                b.port or (443 if b.scheme == "https" else 80),
            )
        except ValueError:
            return False

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise JenkinsRefreshCancelled()
