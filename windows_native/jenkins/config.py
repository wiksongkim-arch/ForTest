"""Jenkins 连接配置仓库与地址校验。"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from backend.settings.secrets import SecretStore, mask_secret

from windows_native.jenkins.errors import JenkinsError
from windows_native.jenkins.models import JenkinsConfiguration
from windows_native.jenkins.storage import AtomicJsonStore


JENKINS_TOKEN_SECRET = "jenkins_api_token"


def normalize_jenkins_url(value: str) -> str:
    """接受 HTTP、HTTPS 和无前缀地址，返回带末尾斜杠的基础地址。"""

    raw = str(value or "").strip()
    if not raw:
        raise JenkinsError("请输入 Jenkins 地址", code="invalid_url")
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        parsed = urlsplit(raw)
        # 访问 port 属性可以提前捕获无效端口格式。
        _ = parsed.port
    except ValueError as exc:
        raise JenkinsError("Jenkins 地址端口无效", code="invalid_url") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise JenkinsError("Jenkins 地址只支持 HTTP 或 HTTPS", code="invalid_url")
    if not parsed.hostname:
        raise JenkinsError("Jenkins 地址缺少主机名", code="invalid_url")
    if parsed.username or parsed.password:
        raise JenkinsError("Jenkins 地址中不能包含账号或密码", code="invalid_url")
    if parsed.query or parsed.fragment:
        raise JenkinsError("Jenkins 地址中不能包含查询参数或锚点", code="invalid_url")
    path = "/" + parsed.path.strip("/") if parsed.path.strip("/") else ""
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, f"{path}/", "", "")
    )


def insecure_http_warning(base_url: str) -> str:
    """HTTP 仅在回环地址上不提示，其他地址都提示凭据传输风险。"""

    parsed = urlsplit(base_url)
    if parsed.scheme != "http":
        return ""
    hostname = str(parsed.hostname or "").lower()
    if hostname in {"localhost", "::1"}:
        return ""
    try:
        if ipaddress.ip_address(hostname).is_loopback:
            return ""
    except ValueError:
        pass
    return "当前使用 HTTP，API Token 在网络中不会被 TLS 加密；生产环境请改用 HTTPS 或 VPN。"


class JenkinsConfigurationRepository:
    """把 Token 与普通配置分开保存。"""

    def __init__(self, data_root: Path, secrets: SecretStore):
        self._store = AtomicJsonStore(Path(data_root) / "data" / "jenkins.json")
        self._secrets = secrets

    def load(self) -> tuple[JenkinsConfiguration | None, str | None]:
        value = self._store.read()
        base_url = str(value.get("base_url") or "").strip()
        username = str(value.get("username") or "").strip()
        token = self._secrets.get(JENKINS_TOKEN_SECRET)
        if not base_url or not username:
            return None, token
        try:
            normalized = normalize_jenkins_url(base_url)
        except JenkinsError:
            return None, token
        return JenkinsConfiguration(normalized, username), token

    def save(self, configuration: JenkinsConfiguration, token: str) -> None:
        normalized_token = str(token or "").strip()
        if not normalized_token:
            raise JenkinsError("请输入 Jenkins API Token", code="missing_token")
        # 先写密钥，再原子更新普通配置；普通配置绝不包含 Token 或掩码。
        self._secrets.set(JENKINS_TOKEN_SECRET, normalized_token)
        self._store.write(
            {
                "schema_version": 1,
                "base_url": configuration.base_url,
                "username": configuration.username,
            }
        )

    def clear(self) -> None:
        self._store.clear()
        self._secrets.delete(JENKINS_TOKEN_SECRET)

    def view(self) -> dict:
        configuration, token = self.load()
        configured = bool(configuration is not None and token)
        base_url = configuration.base_url if configuration else ""
        return {
            "configured": configured,
            "base_url": base_url,
            "username": configuration.username if configuration else "",
            "token_mask": mask_secret(token),
            "security_warning": insecure_http_warning(base_url) if base_url else "",
        }
