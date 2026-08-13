"""配置级云模型目录与连接检测。

检测只请求官方兼容的模型目录，不提交业务提示词。请求禁止跨域重定向，避免
Authorization 头被意外带到其它主机；错误详情只暴露状态码和异常类型。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from backend.settings.models import AIConfiguration, AIProtocol


@dataclass(frozen=True)
class ConfigurationHealthResult:
    ok: bool
    detail: str
    models: tuple[str, ...] = ()


def _models_endpoint(configuration: AIConfiguration) -> str:
    base = configuration.base_url.rstrip("/")
    if configuration.protocol == AIProtocol.anthropic:
        return f"{base}/v1/models"
    return f"{base}/models"


def _headers(configuration: AIConfiguration, api_key: str) -> dict[str, str]:
    if configuration.protocol == AIProtocol.anthropic:
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "accept": "application/json",
        }
    if configuration.protocol == AIProtocol.gemini:
        return {
            "x-goog-api-key": api_key,
            "accept": "application/json",
        }
    return {
        "Authorization": f"Bearer {api_key}",
        "accept": "application/json",
    }


def _extract_models(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    raw = payload.get("data")
    if not isinstance(raw, list):
        raw = payload.get("models")
    if not isinstance(raw, list):
        return ()
    models: list[str] = []
    seen: set[str] = set()
    for item in raw[:500]:
        if not isinstance(item, dict):
            continue
        model = str(item.get("id") or item.get("name") or "").strip()
        if model.startswith("models/"):
            model = model.split("/", 1)[1]
        if not model or len(model) > 256 or model in seen:
            continue
        seen.add(model)
        models.append(model)
    return tuple(models)


def test_cloud_configuration(
    configuration: AIConfiguration,
    api_key: str | None,
    *,
    session: Any = requests,
) -> ConfigurationHealthResult:
    """验证凭据和模型目录；返回值不会包含响应正文或密钥。"""

    if configuration.protocol == AIProtocol.codex:
        return ConfigurationHealthResult(False, "Codex 必须使用本地运行时检测")
    if not isinstance(api_key, str) or not api_key.strip():
        return ConfigurationHealthResult(False, "API Key 未配置")
    try:
        response = session.get(
            _models_endpoint(configuration),
            headers=_headers(configuration, api_key.strip()),
            timeout=min(float(configuration.timeout_seconds), 30.0),
            allow_redirects=False,
        )
        status_code = int(getattr(response, "status_code", 0))
        if 300 <= status_code <= 399:
            return ConfigurationHealthResult(False, "模型目录拒绝跨域重定向")
        response.raise_for_status()
        models = _extract_models(response.json())
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"连接检测失败（HTTP {status}）" if status else "连接检测失败（HTTP）"
        return ConfigurationHealthResult(False, detail)
    except (requests.RequestException, TypeError, ValueError) as exc:
        return ConfigurationHealthResult(
            False,
            f"连接检测失败（{type(exc).__name__}）",
        )
    if not models:
        return ConfigurationHealthResult(False, "连接成功，但模型目录为空或格式异常")
    if configuration.model not in models:
        return ConfigurationHealthResult(
            False,
            f"连接成功，但所选模型 {configuration.model} 不在账号目录中",
            models,
        )
    return ConfigurationHealthResult(True, "检测通过", models)
