from __future__ import annotations

import json
import os
import unittest

from backend.ai.minimax_provider import MiniMaxProvider
from backend.ai.openai_compatible_provider import OpenAICompatibleProvider
from backend.ai.types import TEST_CASE_FIELDS
from backend.api.settings_routes import get_settings_service
from backend.settings.models import ProviderName
from tests.integration.test_codex_live import minimal_live_request


def _assert_http_result(
    case: unittest.TestCase,
    result,
    *,
    provider_name: ProviderName,
    expected_model: str,
) -> None:
    case.assertTrue(result.output_valid)
    case.assertEqual(result.provider, provider_name)
    case.assertEqual(result.runtime_mode, "http")
    case.assertEqual(result.model, expected_model)
    case.assertGreater(len(result.test_cases), 0)
    case.assertGreaterEqual(result.duration_ms, 0)
    case.assertTrue(result.evidence)
    for item in result.test_cases:
        case.assertEqual(set(item), set(TEST_CASE_FIELDS))
        case.assertTrue(all(isinstance(item[field], str) for field in TEST_CASE_FIELDS))
    for evidence in result.evidence:
        case.assertEqual(evidence.provider, provider_name)
        case.assertEqual(evidence.runtime_mode, "http")
        case.assertEqual(evidence.model, expected_model)
        case.assertTrue(evidence.output_valid)


def _emit_http_live_evidence(result) -> None:
    if os.getenv("HTTP_LIVE_EVIDENCE") != "1":
        return
    print(
        "HTTP_LIVE_EVIDENCE="
        + json.dumps(
            {
                "provider": result.provider.value,
                "model": result.model,
                "case_count": len(result.test_cases),
                "stages": [item.stage for item in result.evidence],
                "duration_ms": result.duration_ms,
                "retry_count": result.retry_count,
                "schema_fields": len(TEST_CASE_FIELDS),
                "schema_valid": result.output_valid
                and all(
                    set(item) == set(TEST_CASE_FIELDS)
                    for item in result.test_cases
                ),
            },
            sort_keys=True,
        ),
        flush=True,
    )


@unittest.skipUnless(
    os.getenv("RUN_LIVE_HTTP_PROVIDERS") == "1",
    "paid HTTP provider tests disabled",
)
class LiveHTTPProviderTests(unittest.TestCase):
    def test_saved_minimax_provider_returns_real_schema_valid_cases(self):
        snapshot = get_settings_service().snapshot()
        api_key = snapshot.secrets.reveal("minimax_api_key")
        self.assertTrue(
            api_key,
            "MiniMax live prerequisite failed: saved credential is missing.",
        )
        provider = MiniMaxProvider(snapshot.settings.ai.minimax, api_key)
        try:
            result = provider.process_section(minimal_live_request())
            _assert_http_result(
                self,
                result,
                provider_name=ProviderName.minimax,
                expected_model=snapshot.settings.ai.minimax.model,
            )
        finally:
            provider.close()
        _emit_http_live_evidence(result)

    def test_saved_openai_compatible_provider_returns_real_schema_valid_cases(self):
        service = get_settings_service()
        snapshot = service.snapshot()
        api_key = snapshot.secrets.reveal("openai_compatible_api_key")
        self.assertTrue(
            service.repository.path.exists(),
            "OpenAI-compatible live prerequisite failed: settings are not saved.",
        )
        self.assertTrue(
            api_key,
            "OpenAI-compatible live prerequisite failed: saved credential is missing.",
        )
        provider = OpenAICompatibleProvider(
            snapshot.settings.ai.openai_compatible,
            api_key,
        )
        try:
            result = provider.process_section(minimal_live_request())
            _assert_http_result(
                self,
                result,
                provider_name=ProviderName.openai_compatible,
                expected_model=(snapshot.settings.ai.openai_compatible.model),
            )
        finally:
            provider.close()
        _emit_http_live_evidence(result)


if __name__ == "__main__":
    unittest.main()
