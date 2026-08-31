"""Jenkins 只读 API 客户端测试。"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from windows_native.jenkins.client import JenkinsClient
from windows_native.jenkins.errors import JenkinsError


class FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, router):
        self.router = router
        self.auth = None
        self.headers = {}
        self.trust_env = True
        self.calls = []
        self.methods = []

    def get(self, url, **kwargs):
        self.methods.append("GET")
        self.calls.append((url, kwargs))
        return self.router(url, kwargs)

    def post(self, url, **kwargs):
        self.methods.append("POST")
        self.calls.append((url, kwargs))
        return self.router(url, kwargs)


def test_verify_connection_uses_preemptive_basic_auth_and_reads_version():
    def router(url, _kwargs):
        path = urlsplit(url).path
        if path.endswith("/whoAmI/api/json"):
            return FakeResponse(
                200,
                {
                    "authenticated": True,
                    "name": "fortester-bot",
                    "authorities": ["authenticated"],
                },
                {"X-Jenkins": "2.568.1"},
            )
        return FakeResponse(200, {"nodeName": "built-in"})

    session = FakeSession(router)
    client = JenkinsClient(
        "https://jenkins.example.com/ci/",
        "fortester-bot",
        "token-value",
        session=session,
    )

    result = client.verify_connection()

    assert session.auth == ("fortester-bot", "token-value")
    assert session.trust_env is False
    assert result["authenticated"] is True
    assert result["version"] == "2.568.1"
    assert all(call[1]["allow_redirects"] is False for call in session.calls)


def test_discover_projects_walks_folders_and_extracts_required_parameters():
    def router(url, _kwargs):
        path = urlsplit(url).path
        if path.endswith("/fillValueItems"):
            return FakeResponse(
                200,
                {
                    "values": [
                        {"name": "origin/master", "value": "origin/master"},
                        {
                            "name": "origin/feature/iteration",
                            "value": "origin/feature/iteration",
                        },
                    ]
                },
            )
        if path.endswith("/job/Backend/api/json"):
            return FakeResponse(
                200,
                {
                    "jobs": [
                        {
                            "name": "order-service",
                            "fullName": "Backend/order-service",
                            "description": "订单服务",
                            "url": "https://jenkins.example.com/job/Backend/job/order-service/",
                            "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
                            "buildable": True,
                            "actions": [
                                {
                                    "parameterDefinitions": [
                                        {
                                            "name": "ENV_NAME",
                                            "type": "ChoiceParameterDefinition",
                                            "choices": ["test", "prod"],
                                        },
                                        {
                                            "name": "TARGET_BRANCH",
                                            "type": "PT_BRANCH",
                                            "_class": "net.uaznia.lukanus.hudson.plugins.gitparameter.GitParameterDefinition",
                                            "defaultValue": "main",
                                        },
                                    ]
                                }
                            ],
                        }
                    ]
                },
            )
        return FakeResponse(
            200,
            {
                "jobs": [
                    {
                        "name": "Backend",
                        "fullName": "Backend",
                        "_class": "com.cloudbees.hudson.plugins.folder.Folder",
                    },
                    {
                        "name": "utility",
                        "fullName": "utility",
                        "description": "无部署参数",
                        "_class": "hudson.model.FreeStyleProject",
                        "buildable": True,
                        "actions": [],
                    },
                ]
            },
        )

    client = JenkinsClient(
        "https://jenkins.example.com/",
        "bot",
        "token",
        session=FakeSession(router),
    )
    projects = client.discover_projects()

    assert [item.full_name for item in projects] == [
        "Backend/order-service",
        "utility",
    ]
    deployable = projects[0]
    assert deployable.description == "订单服务"
    assert deployable.environments == ("test", "prod")
    assert deployable.target_branches == (
        "origin/master",
        "origin/feature/iteration",
        "main",
    )
    assert deployable.eligible is True
    assert projects[1].eligible is False
    fill_index = next(
        index
        for index, (url, _kwargs) in enumerate(client.session.calls)
        if urlsplit(url).path.endswith("/fillValueItems")
    )
    assert client.session.methods[fill_index] == "POST"
    assert client.session.calls[fill_index][1]["params"] == {
        "param": "TARGET_BRANCH"
    }
    assert client.session.calls[fill_index][1]["data"] == {}


def test_readonly_post_refuses_non_fill_endpoint():
    client = JenkinsClient(
        "https://jenkins.example.com/",
        "bot",
        "token",
        session=FakeSession(lambda _url, _kwargs: FakeResponse(200, {})),
    )

    with pytest.raises(JenkinsError) as caught:
        client._post_json_readonly("job/example/build")

    assert caught.value.code == "unsafe_read_endpoint"
    assert client.session.calls == []


def test_client_reports_specific_authentication_and_permission_errors():
    for status, expected_code in ((401, "authentication_failed"), (403, "permission_denied")):
        session = FakeSession(lambda _url, _kwargs: FakeResponse(status, {}))
        client = JenkinsClient(
            "https://jenkins.example.com/",
            "bot",
            "token",
            session=session,
        )
        with pytest.raises(JenkinsError) as caught:
            client.verify_connection()
        assert caught.value.code == expected_code


def test_client_refuses_cross_origin_redirects():
    session = FakeSession(
        lambda _url, _kwargs: FakeResponse(
            302,
            {},
            {"Location": "https://attacker.example/api/json"},
        )
    )
    client = JenkinsClient(
        "https://jenkins.example.com/",
        "bot",
        "token",
        session=session,
    )
    with pytest.raises(JenkinsError) as caught:
        client.verify_connection()
    assert caught.value.code == "unsafe_redirect"


@pytest.mark.parametrize(
    "unsafe_url",
    [
        r"C:\Windows\System32\calc.exe",
        r"\\attacker.invalid\share\payload.exe",
        "file:///C:/Windows/System32/calc.exe",
        "https://attacker.invalid/job/demo/18/",
        "https://user@jenkins.example.com/job/demo/18/",
        "https://jenkins.example.com\\@attacker.invalid/payload",
    ],
)
def test_queue_item_clears_unsafe_server_supplied_build_url(unsafe_url):
    session = FakeSession(
        lambda _url, _kwargs: FakeResponse(
            200,
            {
                "id": 42,
                "executable": {"number": 18, "url": unsafe_url},
            },
        )
    )
    client = JenkinsClient(
        "https://jenkins.example.com/ci/",
        "bot",
        "token",
        session=session,
    )

    assert client.queue_item(42)["build_url"] == ""


@pytest.mark.parametrize(
    ("server_url", "expected"),
    [
        (
            "https://jenkins.example.com/ci/job/demo/18/",
            "https://jenkins.example.com/ci/job/demo/18/",
        ),
        (
            "job/demo/18/",
            "https://jenkins.example.com/ci/job/demo/18/",
        ),
        (
            "//jenkins.example.com/ci/job/demo/18/",
            "https://jenkins.example.com/ci/job/demo/18/",
        ),
    ],
)
def test_queue_item_normalizes_legitimate_same_origin_build_url(
    server_url,
    expected,
):
    session = FakeSession(
        lambda _url, _kwargs: FakeResponse(
            200,
            {
                "id": 42,
                "executable": {"number": 18, "url": server_url},
            },
        )
    )
    client = JenkinsClient(
        "https://jenkins.example.com/ci/",
        "bot",
        "token",
        session=session,
    )

    assert client.queue_item(42)["build_url"] == expected


def test_project_and_build_status_urls_share_the_same_origin_policy():
    def router(url, _kwargs):
        path = urlsplit(url).path
        if path.endswith("/18/api/json"):
            return FakeResponse(
                200,
                {
                    "number": 18,
                    "url": r"\\attacker.invalid\share\payload.exe",
                    "building": False,
                    "result": "SUCCESS",
                },
            )
        return FakeResponse(
            200,
            {
                "name": "demo",
                "fullName": "demo",
                "url": "https://attacker.invalid/job/demo/",
                "buildable": True,
                "actions": [],
            },
        )

    client = JenkinsClient(
        "https://jenkins.example.com/ci/",
        "bot",
        "token",
        session=FakeSession(router),
    )

    assert client.project_details("demo").url == ""
    assert client.build_status("demo", 18)["url"] == ""


def test_build_trigger_queue_poll_build_poll_and_stop_contract():
    def router(url, kwargs):
        path = urlsplit(url).path
        if path.endswith("/buildWithParameters"):
            assert kwargs["data"] == {
                "ENV_NAME": "test",
                "TARGET_BRANCH": "origin/feature/a",
            }
            return FakeResponse(
                201,
                {},
                {"Location": "https://jenkins.example.com/queue/item/42/"},
            )
        if path.endswith("/queue/item/42/api/json"):
            return FakeResponse(
                200,
                {
                    "id": 42,
                    "cancelled": False,
                    "executable": {
                        "number": 18,
                        "url": "https://jenkins.example.com/job/dtmzp/job/dtm_pc/18/",
                    },
                },
            )
        if path.endswith("/18/api/json"):
            return FakeResponse(
                200,
                {
                    "number": 18,
                    "building": False,
                    "result": "SUCCESS",
                    "duration": 42000,
                    "estimatedDuration": 40000,
                },
            )
        return FakeResponse(302, {}, {"Location": "/job/dtmzp/job/dtm_pc/"})

    session = FakeSession(router)
    client = JenkinsClient(
        "https://jenkins.example.com/",
        "bot",
        "token",
        session=session,
    )

    queued = client.trigger_build(
        "dtmzp/dtm_pc",
        environment="test",
        branch="origin/feature/a",
    )
    queue = client.queue_item(queued["queue_id"])
    build = client.build_status("dtmzp/dtm_pc", queue["build_number"])
    client.stop_build("dtmzp/dtm_pc", 18)
    client.cancel_queue_item(42)

    assert queued["queue_id"] == 42
    assert queue["build_number"] == 18
    assert build["result"] == "SUCCESS"
    assert build["duration_ms"] == 42000
