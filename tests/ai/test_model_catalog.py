from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import requests

from backend.ai.base import ProviderUnavailableError
from backend.ai.codex_provider import CodexProvider
from backend.ai.model_catalog import (
    _minimax_models_endpoint,
    list_minimax_models,
    list_provider_models,
)
from backend.settings.defaults import default_settings
from backend.settings.models import (
    ProviderName,
    ResolvedSecrets,
    SettingsSnapshot,
)


class _Response:
    def __init__(self, payload, *, error=None):
        self.payload = payload
        self.error = error

    def raise_for_status(self):
        if self.error is not None:
            raise self.error

    def json(self):
        return self.payload


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class ModelCatalogTests(unittest.TestCase):
    def test_minimax_endpoint_handles_root_and_v1_bases(self):
        self.assertEqual(
            _minimax_models_endpoint("https://api.minimaxi.com"),
            "https://api.minimaxi.com/v1/models",
        )
        self.assertEqual(
            _minimax_models_endpoint("https://gateway.example.test/v1/"),
            "https://gateway.example.test/v1/models",
        )

    def test_minimax_catalog_uses_saved_key_and_normalizes_items(self):
        session = _Session(
            _Response(
                {
                    "data": [
                        {"id": "MiniMax-M3"},
                        {"id": "MiniMax-M3"},
                        {"id": ""},
                        "invalid",
                        {"id": "MiniMax-M2.7"},
                    ]
                }
            )
        )

        result = list_minimax_models(
            default_settings().ai.minimax,
            "saved-secret",
            session=session,
        )

        self.assertEqual(
            [item["id"] for item in result],
            ["MiniMax-M3", "MiniMax-M2.7"],
        )
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://api.minimaxi.com/v1/models")
        self.assertEqual(
            kwargs["headers"], {"Authorization": "Bearer saved-secret"}
        )
        self.assertLessEqual(kwargs["timeout"], 30)

    def test_minimax_catalog_missing_key_and_bad_response_fail_safely(self):
        session = _Session(_Response({"data": []}))
        with self.assertRaisesRegex(ProviderUnavailableError, "API Key"):
            list_minimax_models(
                default_settings().ai.minimax,
                None,
                session=session,
            )
        self.assertEqual(session.calls, [])

        for response in (
            _Response({"data": []}),
            _Response({"not_data": []}),
            _Response({}, error=requests.HTTPError("private upstream body")),
        ):
            with self.subTest(response=response):
                with self.assertRaises(ProviderUnavailableError) as raised:
                    list_minimax_models(
                        default_settings().ai.minimax,
                        "saved-secret",
                        session=_Session(response),
                    )
                self.assertNotIn("private upstream body", str(raised.exception))

    def test_codex_catalog_uses_model_field_and_always_closes_sdk(self):
        sdk = SimpleNamespace()

        async def models(*, include_hidden):
            self.assertFalse(include_hidden)
            effort = SimpleNamespace(reasoning_effort="xhigh")
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        model="gpt-5.5",
                        id="legacy-id",
                        display_name="GPT-5.5",
                        description="Default model",
                        is_default=True,
                        supported_reasoning_efforts=[effort],
                    )
                ]
            )

        sdk.models = models
        provider = CodexProvider(default_settings().ai.codex)
        close = AsyncMock()
        try:
            with (
                patch.object(provider, "_new_sdk", return_value=sdk),
                patch(
                    "backend.ai.codex_provider.close_sdk_bounded",
                    new=close,
                ),
            ):
                result = provider.list_models()
        finally:
            provider.close()

        self.assertEqual(result[0]["id"], "gpt-5.5")
        self.assertEqual(result[0]["reasoning_efforts"], ["xhigh"])
        self.assertEqual(close.await_count, 1)

    def test_provider_catalog_closes_codex_provider(self):
        instances = []

        class FakeCodex:
            def __init__(self, settings, api_key):
                self.settings = settings
                self.api_key = api_key
                self.closed = False
                instances.append(self)

            def list_models(self):
                return [{"id": "gpt-test", "label": "GPT test"}]

            def close(self):
                self.closed = True

        snapshot = SettingsSnapshot(
            settings=default_settings(),
            secrets=ResolvedSecrets(),
        )
        result = list_provider_models(
            ProviderName.codex,
            snapshot,
            codex_provider_factory=FakeCodex,
        )

        self.assertEqual(result["source"], "codex_app_server")
        self.assertTrue(instances[0].closed)


if __name__ == "__main__":
    unittest.main()
