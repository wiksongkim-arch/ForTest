"""Claude、Gemini 原生协议与配置检测请求契约测试。"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from backend.ai.base import ProviderUnavailableError
from backend.ai.configuration_health import test_cloud_configuration as run_health_test
from backend.ai.structured_clients import (
    AnthropicStructuredClient,
    GeminiStructuredClient,
)
from backend.ai.types import COMPONENT_OUTPUT_SCHEMA
from backend.settings.models import AIConfiguration, AIConfigurationProvider


def _configuration(provider: AIConfigurationProvider) -> AIConfiguration:
    values = {
        AIConfigurationProvider.claude: {
            "base_url": "https://api.anthropic.com",
            "model": "claude-sonnet-5",
        },
        AIConfigurationProvider.gemini: {
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "model": "gemini-3.6-flash",
        },
    }[provider]
    return AIConfiguration(
        id=f"test-{provider.value}",
        name=provider.value,
        provider=provider,
        **values,
    )


def _response(payload: dict, *, status_code: int = 200) -> Mock:
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_anthropic_structured_request_uses_native_headers_and_output_schema():
    session = Mock()
    session.post.return_value = _response(
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"matched_components": ["按钮"]}),
                }
            ]
        }
    )
    configuration = _configuration(AIConfigurationProvider.claude)
    client = AnthropicStructuredClient(configuration, "claude-secret", session=session)

    result, evidence = client.run_structured_stage(
        "component_matching",
        "system",
        "user",
        COMPONENT_OUTPUT_SCHEMA,
    )

    assert result == {"matched_components": ["按钮"]}
    assert evidence.provider.value == "claude"
    call = session.post.call_args
    assert call.args[0] == "https://api.anthropic.com/v1/messages"
    assert call.kwargs["headers"]["x-api-key"] == "claude-secret"
    assert call.kwargs["headers"]["anthropic-version"] == "2023-06-01"
    assert call.kwargs["allow_redirects"] is False
    assert call.kwargs["json"]["output_config"]["format"]["schema"] == (
        COMPONENT_OUTPUT_SCHEMA
    )


def test_gemini_structured_request_uses_native_schema_and_key_header():
    session = Mock()
    session.post.return_value = _response(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {"matched_components": ["按钮"]}
                                )
                            }
                        ]
                    }
                }
            ]
        }
    )
    configuration = _configuration(AIConfigurationProvider.gemini)
    client = GeminiStructuredClient(configuration, "gemini-secret", session=session)

    result, evidence = client.run_structured_stage(
        "component_matching",
        "system",
        "user",
        COMPONENT_OUTPUT_SCHEMA,
    )

    assert result == {"matched_components": ["按钮"]}
    assert evidence.provider.value == "gemini"
    call = session.post.call_args
    assert call.args[0].endswith(
        "/models/gemini-3.6-flash:generateContent"
    )
    assert call.kwargs["headers"]["x-goog-api-key"] == "gemini-secret"
    assert call.kwargs["allow_redirects"] is False
    generation = call.kwargs["json"]["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    assert generation["responseJsonSchema"] == COMPONENT_OUTPUT_SCHEMA


@pytest.mark.parametrize(
    ("provider", "endpoint", "credential_header"),
    [
        (
            AIConfigurationProvider.claude,
            "https://api.anthropic.com/v1/models",
            "x-api-key",
        ),
        (
            AIConfigurationProvider.gemini,
            "https://generativelanguage.googleapis.com/v1beta/models",
            "x-goog-api-key",
        ),
    ],
)
def test_configuration_health_uses_provider_native_model_catalog(
    provider,
    endpoint,
    credential_header,
):
    configuration = _configuration(provider)
    session = Mock()
    session.get.return_value = _response(
        {"data": [{"id": configuration.model}]}
        if provider == AIConfigurationProvider.claude
        else {"models": [{"name": f"models/{configuration.model}"}]}
    )

    health = run_health_test(configuration, "health-secret", session=session)

    assert health.ok is True
    call = session.get.call_args
    assert call.args[0] == endpoint
    assert call.kwargs["headers"][credential_header] == "health-secret"
    assert call.kwargs["allow_redirects"] is False


def test_credential_bearing_redirect_is_rejected_without_echoing_key():
    session = Mock()
    session.post.return_value = _response({}, status_code=302)
    client = GeminiStructuredClient(
        _configuration(AIConfigurationProvider.gemini),
        "redirect-private-secret",
        session=session,
    )

    with pytest.raises(ProviderUnavailableError) as raised:
        client.run_structured_stage(
            "component_matching",
            "system",
            "user",
            COMPONENT_OUTPUT_SCHEMA,
        )

    assert "redirect-private-secret" not in str(raised.value)
