"""Live model catalogs used only by the AI settings page."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import requests

from backend.ai.base import ProviderUnavailableError
from backend.ai.codex_provider import CodexProvider
from backend.settings.models import (
    MiniMaxSettings,
    OpenAICompatibleSettings,
    ProviderName,
    SettingsSnapshot,
)


def _minimax_models_endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = f"{path}/models"
    else:
        path = f"{path}/v1/models"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def list_minimax_models(
    settings: MiniMaxSettings,
    api_key: str | None,
    *,
    session: Any = requests,
) -> list[dict[str, Any]]:
    """Read the account-visible OpenAI-compatible MiniMax model list."""

    if not isinstance(api_key, str) or not api_key.strip():
        raise ProviderUnavailableError(
            "MiniMax API Key 未配置，无法刷新模型列表。"
        )
    try:
        response = session.get(
            _minimax_models_endpoint(settings.base_url),
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            timeout=min(float(settings.timeout_seconds), 30.0),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ProviderUnavailableError(
            "MiniMax 模型列表获取失败。"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("data"), list
    ):
        raise ProviderUnavailableError("MiniMax 返回了无效的模型列表。")

    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload["data"][:200]:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id or len(model_id) > 128 or model_id in seen:
            continue
        seen.add(model_id)
        models.append(
            {
                "id": model_id,
                "label": model_id,
                "description": "",
                "is_default": False,
                "reasoning_efforts": [],
            }
        )
    if not models:
        raise ProviderUnavailableError("MiniMax 未返回可用模型。")
    return models


def list_openai_compatible_models(
    settings: OpenAICompatibleSettings,
    api_key: str | None,
    *,
    session: Any = requests,
) -> list[dict[str, Any]]:
    """读取当前 OpenAI 兼容通道公开给该密钥的模型目录。"""

    if not isinstance(api_key, str) or not api_key.strip():
        raise ProviderUnavailableError(
            "OpenAI 兼容 API Key 未配置，无法刷新模型列表。"
        )
    endpoint = f"{settings.base_url.rstrip('/')}/models"
    try:
        response = session.get(
            endpoint,
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            timeout=min(float(settings.timeout_seconds), 30.0),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ProviderUnavailableError(
            "OpenAI 兼容模型列表获取失败。"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ProviderUnavailableError("OpenAI 兼容通道返回了无效的模型列表。")
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload["data"][:500]:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id or len(model_id) > 128 or model_id in seen:
            continue
        seen.add(model_id)
        models.append(
            {
                "id": model_id,
                "label": model_id,
                "description": "",
                "is_default": model_id == settings.model,
                "reasoning_efforts": [],
                "speed_tiers": [],
            }
        )
    if not models:
        raise ProviderUnavailableError("OpenAI 兼容通道未返回可用模型。")
    return models


def list_provider_models(
    provider: ProviderName,
    snapshot: SettingsSnapshot,
    *,
    codex_provider_factory: Callable[..., CodexProvider] = CodexProvider,
    minimax_session: Any = requests,
) -> dict[str, Any]:
    """Return one provider catalog without mutating saved settings."""

    if provider == ProviderName.codex:
        codex = codex_provider_factory(
            snapshot.settings.ai.codex,
            snapshot.secrets.reveal("codex_api_key"),
        )
        try:
            models = codex.list_models()
        finally:
            codex.close()
        source = "codex_app_server"
    elif provider == ProviderName.minimax:
        models = list_minimax_models(
            snapshot.settings.ai.minimax,
            snapshot.secrets.reveal("minimax_api_key"),
            session=minimax_session,
        )
        source = "minimax_api"
    elif provider == ProviderName.openai_compatible:
        models = list_openai_compatible_models(
            snapshot.settings.ai.openai_compatible,
            snapshot.secrets.reveal("openai_compatible_api_key"),
            session=minimax_session,
        )
        source = "openai_compatible_api"
    else:
        raise ProviderUnavailableError(
            "该 Provider 不支持动态模型列表。"
        )
    return {
        "provider": provider.value,
        "source": source,
        "models": models,
    }
