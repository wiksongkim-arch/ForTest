"""Jenkins 地址、密钥分离和配置提交测试。"""

from __future__ import annotations

import json

import pytest

from backend.settings.secrets import MemorySecretStore
from windows_native.jenkins.config import (
    JENKINS_TOKEN_SECRET,
    JenkinsConfigurationRepository,
    insecure_http_warning,
    normalize_jenkins_url,
)
from windows_native.jenkins.errors import JenkinsError
from windows_native.jenkins.models import JenkinsConfiguration
from windows_native.jenkins.service import JenkinsDeploymentService


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("jenkins.example.com:8080", "http://jenkins.example.com:8080/"),
        ("https://jenkins.example.com", "https://jenkins.example.com/"),
        ("https://jenkins.example.com/ci/", "https://jenkins.example.com/ci/"),
    ],
)
def test_jenkins_url_normalization(raw: str, expected: str):
    assert normalize_jenkins_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ftp://jenkins.example.com",
        "http://user:secret@jenkins.example.com",
        "https://jenkins.example.com/?token=secret",
        "http://jenkins.example.com:not-a-port",
    ],
)
def test_jenkins_url_rejects_unsafe_or_invalid_values(raw: str):
    with pytest.raises(JenkinsError):
        normalize_jenkins_url(raw)


def test_http_warning_is_suppressed_only_for_loopback():
    assert insecure_http_warning("http://127.0.0.1:8080/") == ""
    assert insecure_http_warning("http://localhost:8080/") == ""
    # RFC 5737 TEST-NET-1 地址仅用于文档和测试，不指向真实业务主机。
    assert "HTTPS" in insecure_http_warning("http://192.0.2.1:8080/")
    assert insecure_http_warning("https://jenkins.example.com/") == ""


def test_configuration_never_persists_token_to_json(tmp_path):
    secrets = MemorySecretStore()
    repository = JenkinsConfigurationRepository(tmp_path, secrets)
    repository.save(
        JenkinsConfiguration("https://jenkins.example.com/", "fortester-bot"),
        "private-token-value",
    )

    stored_path = tmp_path / "data" / "jenkins.json"
    stored_text = stored_path.read_text(encoding="utf-8")
    stored = json.loads(stored_text)
    assert stored == {
        "schema_version": 1,
        "base_url": "https://jenkins.example.com/",
        "username": "fortester-bot",
    }
    assert "private-token-value" not in stored_text
    assert secrets.get(JENKINS_TOKEN_SECRET) == "private-token-value"
    assert repository.view()["configured"] is True


def test_failed_validation_keeps_previous_configuration(tmp_path):
    secrets = MemorySecretStore()
    service = JenkinsDeploymentService(
        tmp_path,
        secrets=secrets,
        client_factory=lambda *_args: _FailingClient(),
    )
    service.configuration.save(
        JenkinsConfiguration("https://old.example.com/", "old-user"),
        "old-token",
    )

    with pytest.raises(JenkinsError, match="认证失败"):
        service.validate_and_save_configuration(
            "https://new.example.com",
            "new-user",
            "new-token",
        )

    configuration, token = service.configuration.load()
    assert configuration == JenkinsConfiguration(
        "https://old.example.com/",
        "old-user",
    )
    assert token == "old-token"


class _FailingClient:
    def verify_connection(self):
        raise JenkinsError("认证失败", code="authentication_failed")


def test_saved_jenkins_token_is_bound_to_address_and_username(tmp_path):
    calls: list[tuple[str, str, str]] = []

    class _SuccessfulClient:
        def __init__(self, base_url: str, username: str, token: str):
            calls.append((base_url, username, token))

        def verify_connection(self):
            return {"ok": True}

    service = JenkinsDeploymentService(
        tmp_path,
        secrets=MemorySecretStore(),
        client_factory=_SuccessfulClient,
    )
    service.configuration.save(
        JenkinsConfiguration("https://jenkins.example.com/", "bot"),
        "bound-token",
    )

    service.validate_and_save_configuration(
        "https://jenkins.example.com",
        "bot",
        "",
        keep_saved_token=True,
    )
    assert calls[-1] == ("https://jenkins.example.com/", "bot", "bound-token")

    with pytest.raises(JenkinsError, match="重新输入 API Token"):
        service.validate_and_save_configuration(
            "https://other.example.com",
            "bot",
            "",
            keep_saved_token=True,
        )
    assert len(calls) == 1
