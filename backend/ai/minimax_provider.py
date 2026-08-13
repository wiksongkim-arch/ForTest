from __future__ import annotations

from typing import Any

from backend.ai.openai_compatible_provider import OpenAICompatibleProvider
from backend.ai.types import ProviderHealth
from backend.settings.models import (
    MiniMaxSettings,
    OpenAICompatibleSettings,
    ProviderName,
)


class MiniMaxProvider(OpenAICompatibleProvider):
    name = ProviderName.minimax

    def __init__(
        self,
        settings: MiniMaxSettings,
        api_key: str | None,
        *,
        session: Any | None = None,
    ):
        compatible_settings = OpenAICompatibleSettings(
            base_url=settings.base_url,
            model=settings.model,
            timeout_seconds=settings.timeout_seconds,
            vision_enabled=settings.vision_enabled,
        )
        super().__init__(compatible_settings, api_key, session=session)

    def health_check(self) -> ProviderHealth:
        return super().health_check()

    def _endpoint(self) -> str:
        return f"{self._base_url}/v1/text/chatcompletion_v2"

    def _payload(self, stage, messages, schema):
        payload = super()._payload(stage, messages, schema)
        if self._model == "MiniMax-Text-01":
            payload["response_format"]["json_schema"].pop("strict", None)
            return payload
        payload.pop("response_format", None)
        payload["messages"] = self._messages_with_schema_instruction(
            payload["messages"],
            schema,
        )
        return payload

    def _stage_detail(self) -> str:
        if self._model == "MiniMax-Text-01":
            return ""
        return (
            "native structured output unavailable; "
            "local schema validation enforced"
        )
