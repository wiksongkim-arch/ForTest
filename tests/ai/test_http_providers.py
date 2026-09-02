import base64
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from backend.ai.base import ProviderResponseError, ProviderUnavailableError
from backend.ai.minimax_provider import MiniMaxProvider
from backend.ai.openai_compatible_provider import (
    OpenAICompatibleProvider,
    _validate_against_schema,
)
from backend.ai.types import (
    CASE_OUTPUT_SCHEMA,
    COMPONENT_OUTPUT_SCHEMA,
    IMAGE_OUTPUT_SCHEMA,
    TEST_CASE_FIELDS,
    SectionAIRequest,
)
from backend.settings.defaults import default_settings
from backend.settings.models import (
    MiniMaxSettings,
    OpenAICompatiblePreset,
    OpenAICompatibleSettings,
    ResponseFormatMode,
)
from backend.settings.prompts import PromptCatalog
import services.minimax as legacy_minimax
from services.minimax import MiniMaxService


def make_section_request(
    *,
    images=(),
    component_names=("输入框", "按钮"),
    field_specs=None,
    component_templates=None,
    prompts=None,
):
    settings = default_settings()
    return SectionAIRequest(
        section_title="登录",
        section_content="用户输入正确账号密码后登录成功。",
        images=images,
        component_names=component_names,
        field_specs=(
            {"用例名称": "描述验证目标"}
            if field_specs is None
            else field_specs
        ),
        component_templates=(
            {} if component_templates is None else component_templates
        ),
        prompts=(settings.prompts.model_dump() if prompts is None else prompts),
    )


def provider_response(payload):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(payload, ensure_ascii=False)
                }
            }
        ]
    }
    return response


def provider_content_response(content):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    return response


def valid_case(**updates):
    case = {field: f"value-{field}" for field in TEST_CASE_FIELDS}
    case.update(updates)
    return case


def failing_response(message):
    response = Mock()
    response.raise_for_status.side_effect = RuntimeError(message)
    return response


def http_error_response(status_code, message="service failure"):
    http_response = requests.Response()
    http_response.status_code = status_code
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError(
        message,
        response=http_response,
    )
    return response


def payload_prompt_text(payload):
    fragments = []
    for message in payload["messages"]:
        content = message["content"]
        if isinstance(content, str):
            fragments.append(content)
            continue
        for part in content:
            if part.get("type") == "text":
                fragments.append(part["text"])
    return "\n".join(fragments)


def example_from_payload(payload):
    marker = "Example JSON output:"
    prompt = payload_prompt_text(payload)
    if marker not in prompt:
        return None
    return json.loads(prompt.rsplit(marker, 1)[1].strip().splitlines()[0])


class HttpProviderTests(unittest.TestCase):
    def test_invalid_provider_base_urls_fail_safely_without_network(self):
        invalid_urls = (
            "https://provider.example.test/gateway?token=query-secret",
            "https://provider.example.test/gateway#fragment-secret",
            "https://provider.example.test:bad-port/gateway",
            "https://provider.example.test:70000/gateway",
        )
        provider_variants = (
            (
                OpenAICompatibleProvider,
                lambda url: OpenAICompatibleSettings(
                    base_url=url,
                    vision_enabled=False,
                ),
            ),
            (
                MiniMaxProvider,
                lambda url: MiniMaxSettings(
                    base_url=url,
                    vision_enabled=False,
                ),
            ),
        )
        for provider_class, settings_factory in provider_variants:
            for invalid_url in invalid_urls:
                with self.subTest(
                    provider=provider_class.__name__,
                    invalid_url=invalid_url,
                ):
                    session = Mock()
                    provider = provider_class(
                        settings_factory(invalid_url),
                        "secret",
                        session=session,
                    )

                    health = provider.health_check()
                    self.assertFalse(health.ok)
                    session.post.assert_not_called()
                    self.assertNotIn(invalid_url, health.detail)

                    with self.assertRaises(ProviderUnavailableError) as raised:
                        provider.process_section(make_section_request())
                    session.post.assert_not_called()
                    self.assertNotIn(invalid_url, str(raised.exception))
                    for forbidden in (
                        "query-secret",
                        "fragment-secret",
                        "bad-port",
                    ):
                        self.assertNotIn(forbidden, str(raised.exception))

    def test_valid_provider_base_paths_build_exact_endpoints(self):
        variants = (
            (
                OpenAICompatibleProvider,
                OpenAICompatibleSettings(
                    base_url="https://[2001:db8::1]:8443/gateway/v1///",
                    vision_enabled=False,
                ),
                "https://[2001:db8::1]:8443/gateway/v1/chat/completions",
            ),
            (
                MiniMaxProvider,
                MiniMaxSettings(
                    base_url="https://minimax.example.test/gateway///",
                    vision_enabled=False,
                ),
                (
                    "https://minimax.example.test/gateway/"
                    "v1/text/chatcompletion_v2"
                ),
            ),
        )
        for provider_class, settings, expected_endpoint in variants:
            with self.subTest(provider=provider_class.__name__):
                session = Mock()
                session.post.side_effect = [
                    provider_response({"matched_components": []}),
                    provider_response({"test_cases": []}),
                ]
                provider = provider_class(settings, "secret", session=session)

                provider.process_section(make_section_request())

                for call in session.post.call_args_list:
                    self.assertEqual(call.args[0], expected_endpoint)

    def test_json_object_prompts_include_schema_valid_deterministic_examples(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "screen.png"
            image.write_bytes(b"image")
            session = Mock()
            session.post.side_effect = [
                provider_response({"matched_components": []}),
                provider_response({"image_findings": []}),
                provider_response({"matched_components": []}),
                provider_response({"test_cases": []}),
            ]
            provider = OpenAICompatibleProvider(
                OpenAICompatibleSettings(
                    preset=OpenAICompatiblePreset.custom,
                    response_format_mode=ResponseFormatMode.json_object,
                    vision_enabled=True,
                ),
                "secret",
                session=session,
            )

            self.assertTrue(provider.health_check().ok)
            provider.process_section(
                make_section_request(images=(image,))
            )

        schemas = (
            COMPONENT_OUTPUT_SCHEMA,
            IMAGE_OUTPUT_SCHEMA,
            COMPONENT_OUTPUT_SCHEMA,
            CASE_OUTPUT_SCHEMA,
        )
        examples = []
        for call, schema in zip(session.post.call_args_list, schemas):
            payload = call.kwargs["json"]
            prompt = payload_prompt_text(payload)
            self.assertIn("JSON", prompt)
            self.assertIn(
                json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                prompt,
            )
            example = example_from_payload(payload)
            self.assertIsNotNone(example)
            _validate_against_schema(example, schema)
            examples.append(example)

        self.assertEqual(examples[0], examples[2])
        self.assertEqual(examples[0], {"matched_components": []})
        self.assertEqual(examples[1], {"image_findings": []})
        self.assertEqual(len(examples[3]["test_cases"]), 1)
        self.assertEqual(
            tuple(examples[3]["test_cases"][0]),
            TEST_CASE_FIELDS,
        )
        self.assertEqual(
            set(examples[3]["test_cases"][0].values()),
            {"example"},
        )

    def test_minimax_prompt_fallback_uses_the_same_schema_valid_examples(self):
        session = Mock()
        session.post.side_effect = [
            provider_response({"matched_components": []}),
            provider_response({"test_cases": []}),
        ]
        provider = MiniMaxProvider(
            MiniMaxSettings(model="MiniMax-M2.7", vision_enabled=False),
            "secret",
            session=session,
        )

        provider.process_section(make_section_request())

        for call, schema in zip(
            session.post.call_args_list,
            (COMPONENT_OUTPUT_SCHEMA, CASE_OUTPUT_SCHEMA),
        ):
            payload = call.kwargs["json"]
            example = example_from_payload(payload)
            self.assertIsNotNone(example)
            _validate_against_schema(example, schema)

    def test_openai_provider_uses_configured_model_and_json_schema(self):
        session = Mock()
        session.post.side_effect = [
            provider_response({"matched_components": ["按钮"]}),
            provider_response({"test_cases": []}),
        ]
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                model="compatible-model",
                vision_enabled=False,
            ),
            "secret",
            session=session,
        )
        provider.process_section(make_section_request())
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(payload["model"], "compatible-model")
        self.assertEqual(payload["response_format"]["type"], "json_schema")

    def test_minimax_transient_failure_retries_once(self):
        session = Mock()
        session.post.side_effect = [
            TimeoutError("slow"),
            provider_response({"matched_components": ["按钮"]}),
            provider_response({"test_cases": []}),
        ]
        provider = MiniMaxProvider(
            MiniMaxSettings(vision_enabled=False),
            "secret",
            session=session,
        )
        result = provider.process_section(make_section_request())
        self.assertEqual(session.post.call_count, 3)
        component_evidence = next(
            item
            for item in result.evidence
            if item.stage == "component_matching"
        )
        self.assertEqual(component_evidence.retry_count, 1)
        case_evidence = next(
            item
            for item in result.evidence
            if item.stage == "case_generation"
        )
        self.assertEqual(case_evidence.retry_count, 0)

    def test_deepseek_preset_uses_supported_json_object_mode(self):
        session = Mock()
        session.post.side_effect = [
            provider_response({"matched_components": []}),
            provider_response({"test_cases": []}),
        ]
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                preset=OpenAICompatiblePreset.deepseek,
                base_url="https://api.deepseek.com",
                model="deepseek-v4-flash",
                response_format_mode=ResponseFormatMode.json_object,
                vision_enabled=False,
            ),
            "secret",
            session=session,
        )
        provider.process_section(make_section_request())
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_openai_requests_use_the_constructor_snapshot_and_exact_schemas(self):
        settings = OpenAICompatibleSettings(
            base_url="https://compatible.example.test/v1/",
            model="snapshot-model",
            timeout_seconds=41,
            vision_enabled=False,
        )
        session = Mock()
        session.post.side_effect = [
            provider_response({"matched_components": []}),
            provider_response({"test_cases": []}),
        ]
        provider = OpenAICompatibleProvider(
            settings,
            "snapshot-key",
            session=session,
        )
        object.__setattr__(settings, "base_url", "https://changed.invalid")
        object.__setattr__(settings, "model", "changed-model")
        object.__setattr__(settings, "timeout_seconds", 899)

        result = provider.process_section(make_section_request())

        self.assertEqual(result.model, "snapshot-model")
        self.assertEqual(session.post.call_count, 2)
        for call in session.post.call_args_list:
            self.assertEqual(
                call.args[0],
                "https://compatible.example.test/v1/chat/completions",
            )
            self.assertEqual(call.kwargs["json"]["model"], "snapshot-model")
            self.assertEqual(call.kwargs["timeout"], 41)
            self.assertIs(call.kwargs["verify"], True)
            self.assertEqual(
                call.kwargs["headers"],
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer snapshot-key",
                },
            )
        component_format = session.post.call_args_list[0].kwargs["json"][
            "response_format"
        ]
        self.assertEqual(component_format["type"], "json_schema")
        self.assertEqual(
            component_format["json_schema"]["schema"],
            COMPONENT_OUTPUT_SCHEMA,
        )
        self.assertIs(component_format["json_schema"]["strict"], True)
        case_format = session.post.call_args_list[1].kwargs["json"][
            "response_format"
        ]
        self.assertEqual(
            case_format,
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "prd_test_cases",
                    "strict": True,
                    "schema": CASE_OUTPUT_SCHEMA,
                },
            },
        )

    def test_minimax_requests_use_fixed_secure_transport_settings(self):
        settings = MiniMaxSettings(
            base_url="https://minimax.example.test/",
            model="minimax-snapshot",
            timeout_seconds=47,
            vision_enabled=False,
        )
        session = Mock()
        session.post.side_effect = [
            provider_response({"matched_components": []}),
            provider_response({"test_cases": []}),
        ]
        provider = MiniMaxProvider(settings, "minimax-key", session=session)
        object.__setattr__(settings, "model", "changed-model")
        object.__setattr__(settings, "timeout_seconds", 899)

        result = provider.process_section(make_section_request())

        self.assertEqual(result.model, "minimax-snapshot")
        self.assertEqual(session.post.call_count, 2)
        for call in session.post.call_args_list:
            self.assertEqual(
                call.args[0],
                "https://minimax.example.test/v1/text/chatcompletion_v2",
            )
            self.assertEqual(call.kwargs["json"]["model"], "minimax-snapshot")
            self.assertEqual(call.kwargs["timeout"], 47)
            self.assertIs(call.kwargs["verify"], True)
            self.assertEqual(
                call.kwargs["headers"]["Authorization"],
                "Bearer minimax-key",
            )

    def test_missing_key_health_is_unhealthy_without_network(self):
        for provider_class, settings in (
            (
                OpenAICompatibleProvider,
                OpenAICompatibleSettings(vision_enabled=False),
            ),
            (MiniMaxProvider, MiniMaxSettings(vision_enabled=False)),
        ):
            with self.subTest(provider=provider_class.__name__):
                session = Mock()
                provider = provider_class(settings, None, session=session)

                health = provider.health_check()

                self.assertFalse(health.ok)
                self.assertNotIn("Authorization", health.detail)
                session.post.assert_not_called()

    def test_health_check_makes_one_schema_constrained_probe(self):
        for provider_class, settings, endpoint in (
            (
                OpenAICompatibleProvider,
                OpenAICompatibleSettings(
                    base_url="https://compatible.example.test/v1",
                    vision_enabled=False,
                ),
                "https://compatible.example.test/v1/chat/completions",
            ),
            (
                MiniMaxProvider,
                MiniMaxSettings(
                    base_url="https://minimax.example.test",
                    vision_enabled=False,
                ),
                "https://minimax.example.test/v1/text/chatcompletion_v2",
            ),
        ):
            with self.subTest(provider=provider_class.__name__):
                session = Mock()
                session.post.return_value = provider_response(
                    {"matched_components": []}
                )
                provider = provider_class(settings, "secret", session=session)

                health = provider.health_check()

                self.assertTrue(health.ok)
                session.post.assert_called_once()
                call = session.post.call_args
                self.assertEqual(call.args[0], endpoint)
                payload = call.kwargs["json"]
                if provider_class is OpenAICompatibleProvider:
                    self.assertEqual(
                        payload["response_format"]["type"],
                        "json_schema",
                    )
                    self.assertEqual(
                        payload["response_format"]["json_schema"][
                            "schema"
                        ],
                        COMPONENT_OUTPUT_SCHEMA,
                    )
                else:
                    self.assertNotIn("response_format", payload)
                    prompts = "\n".join(
                        str(message["content"])
                        for message in payload["messages"]
                    )
                    self.assertIn("JSON", prompts)
                    self.assertIn(
                        json.dumps(
                            COMPONENT_OUTPUT_SCHEMA,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        prompts,
                    )

    def test_health_and_process_failures_are_redacted(self):
        secret = "secret-value"
        raw_error = (
            "Authorization: Bearer secret-value "
            "https://service.invalid/file.png?token=query-secret"
        )
        for provider_class, settings in (
            (
                OpenAICompatibleProvider,
                OpenAICompatibleSettings(vision_enabled=False),
            ),
            (MiniMaxProvider, MiniMaxSettings(vision_enabled=False)),
        ):
            with self.subTest(provider=provider_class.__name__):
                session = Mock()
                session.post.return_value = failing_response(raw_error)
                provider = provider_class(settings, secret, session=session)

                health = provider.health_check()
                self.assertFalse(health.ok)
                for forbidden in (
                    secret,
                    "Authorization",
                    "query-secret",
                    raw_error,
                ):
                    self.assertNotIn(forbidden, health.detail)

                session.reset_mock()
                session.post.return_value = failing_response(raw_error)
                with self.assertRaises(ProviderResponseError) as raised:
                    provider.process_section(make_section_request())
                self.assertEqual(session.post.call_count, 1)
                for forbidden in (
                    secret,
                    "Authorization",
                    "query-secret",
                    raw_error,
                ):
                    self.assertNotIn(forbidden, str(raised.exception))

    def test_json_schema_400_stops_without_mode_downgrade(self):
        session = Mock()
        session.post.return_value = http_error_response(
            400,
            "json_schema unsupported; key=secret",
        )
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(vision_enabled=False),
            "secret",
            session=session,
        )

        with self.assertRaises(ProviderResponseError):
            provider.process_section(make_section_request())

        self.assertEqual(session.post.call_count, 1)
        for call in session.post.call_args_list:
            self.assertEqual(
                call.kwargs["json"]["response_format"]["type"],
                "json_schema",
            )

    def test_permanent_http_errors_do_not_retry(self):
        for status_code in (400, 401, 403, 404, 422):
            with self.subTest(status_code=status_code):
                session = Mock()
                session.post.return_value = http_error_response(
                    status_code,
                    "Authorization: Bearer secret raw-service-error",
                )
                provider = OpenAICompatibleProvider(
                    OpenAICompatibleSettings(vision_enabled=False),
                    "secret",
                    session=session,
                )

                with self.assertRaises(ProviderResponseError) as raised:
                    provider.process_section(make_section_request())

                self.assertEqual(session.post.call_count, 1)
                self.assertNotIn("secret", str(raised.exception))
                self.assertNotIn("raw-service-error", str(raised.exception))

    def test_transient_http_errors_retry_only_the_failed_stage_once(self):
        for status_code in (408, 429, 500, 503):
            with self.subTest(status_code=status_code):
                session = Mock()
                session.post.side_effect = [
                    http_error_response(status_code),
                    provider_response({"matched_components": []}),
                    provider_response({"test_cases": []}),
                ]
                provider = OpenAICompatibleProvider(
                    OpenAICompatibleSettings(vision_enabled=False),
                    "secret",
                    session=session,
                )

                result = provider.process_section(make_section_request())

                self.assertEqual(session.post.call_count, 3)
                component = next(
                    item
                    for item in result.evidence
                    if item.stage == "component_matching"
                )
                self.assertEqual(component.retry_count, 1)

    def test_timeout_and_connection_errors_retry_once(self):
        transient_errors = (
            requests.Timeout("requests timeout"),
            TimeoutError("builtin timeout"),
            requests.ConnectionError("requests connection"),
            ConnectionError("builtin connection"),
        )
        for transient_error in transient_errors:
            with self.subTest(error_type=type(transient_error).__name__):
                session = Mock()
                session.post.side_effect = [
                    transient_error,
                    provider_response({"matched_components": []}),
                    provider_response({"test_cases": []}),
                ]
                provider = MiniMaxProvider(
                    MiniMaxSettings(vision_enabled=False),
                    "secret",
                    session=session,
                )

                result = provider.process_section(make_section_request())

                self.assertEqual(session.post.call_count, 3)
                component = next(
                    item
                    for item in result.evidence
                    if item.stage == "component_matching"
                )
                self.assertEqual(component.retry_count, 1)

    def test_json_object_mode_is_exact_and_prompts_include_json_schema(self):
        session = Mock()
        session.post.side_effect = [
            provider_response({"matched_components": []}),
            provider_response({"test_cases": []}),
        ]
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                preset=OpenAICompatiblePreset.deepseek,
                base_url="https://api.deepseek.com",
                model="deepseek-model",
                response_format_mode=ResponseFormatMode.json_object,
                vision_enabled=False,
            ),
            "secret",
            session=session,
        )

        provider.process_section(make_section_request())

        expected_schemas = (COMPONENT_OUTPUT_SCHEMA, CASE_OUTPUT_SCHEMA)
        for call, schema in zip(session.post.call_args_list, expected_schemas):
            payload = call.kwargs["json"]
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            rendered_prompts = "\n".join(
                str(message["content"]) for message in payload["messages"]
            )
            self.assertIn("JSON", rendered_prompts)
            self.assertIn(
                json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                rendered_prompts,
            )

    def test_close_is_idempotent_and_respects_session_ownership(self):
        injected = Mock()
        injected_provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(vision_enabled=False),
            "secret",
            session=injected,
        )
        injected_provider.close()
        injected_provider.close()
        injected.close.assert_not_called()

        owned = Mock()
        with patch(
            "backend.ai.openai_compatible_provider.httpx.Client",
            return_value=owned,
        ):
            owned_provider = MiniMaxProvider(
                MiniMaxSettings(vision_enabled=False),
                "secret",
            )
        owned_provider.close()
        owned_provider.close()
        owned.close.assert_called_once_with()

    def test_closed_provider_is_unavailable_at_every_network_boundary(self):
        variants = (
            (
                OpenAICompatibleProvider,
                OpenAICompatibleSettings(vision_enabled=False),
            ),
            (MiniMaxProvider, MiniMaxSettings(vision_enabled=False)),
        )
        for provider_class, settings in variants:
            with self.subTest(provider=provider_class.__name__):
                session = Mock()
                provider = provider_class(settings, "secret", session=session)
                provider.close()

                health = provider.health_check()
                self.assertFalse(health.ok)
                self.assertNotIn("secret", health.detail)
                session.post.assert_not_called()

                with self.assertRaises(ProviderUnavailableError) as raised:
                    provider.process_section(make_section_request())
                self.assertNotIn("secret", str(raised.exception))
                session.post.assert_not_called()

                with self.assertRaises(ProviderUnavailableError):
                    provider._post({"model": "must-not-send"})
                session.post.assert_not_called()

    def test_owned_session_close_errors_are_contained_and_idempotent(self):
        owned = Mock()
        owned.close.side_effect = RuntimeError(
            "Authorization: Bearer provider-secret"
        )
        with patch(
            "backend.ai.openai_compatible_provider.httpx.Client",
            return_value=owned,
        ):
            provider = OpenAICompatibleProvider(
                OpenAICompatibleSettings(vision_enabled=False),
                "provider-secret",
            )

        provider.close()
        provider.close()

        owned.close.assert_called_once_with()

    def test_missing_key_process_fails_without_network(self):
        for provider_class, settings in (
            (
                OpenAICompatibleProvider,
                OpenAICompatibleSettings(vision_enabled=False),
            ),
            (MiniMaxProvider, MiniMaxSettings(vision_enabled=False)),
        ):
            with self.subTest(provider=provider_class.__name__):
                session = Mock()
                provider = provider_class(settings, None, session=session)

                with self.assertRaises(ProviderUnavailableError):
                    provider.process_section(make_section_request())

                session.post.assert_not_called()

    def test_blank_key_is_treated_as_missing_without_network(self):
        session = Mock()
        provider = MiniMaxProvider(
            MiniMaxSettings(vision_enabled=False),
            "   ",
            session=session,
        )

        health = provider.health_check()

        self.assertFalse(health.ok)
        session.post.assert_not_called()

    def test_minimax_text_01_uses_native_schema_without_openai_strict(self):
        session = Mock()
        session.post.side_effect = [
            provider_response({"matched_components": []}),
            provider_response({"test_cases": []}),
        ]
        provider = MiniMaxProvider(
            MiniMaxSettings(
                model="MiniMax-Text-01",
                vision_enabled=False,
            ),
            "secret",
            session=session,
        )

        provider.process_section(make_section_request())

        for call, schema in zip(
            session.post.call_args_list,
            (COMPONENT_OUTPUT_SCHEMA, CASE_OUTPUT_SCHEMA),
        ):
            response_format = call.kwargs["json"]["response_format"]
            self.assertEqual(response_format["type"], "json_schema")
            self.assertEqual(
                response_format["json_schema"]["schema"], schema
            )
            self.assertNotIn("strict", response_format["json_schema"])

    def test_minimax_m27_uses_prompt_schema_and_reports_capability(self):
        session = Mock()
        session.post.side_effect = [
            provider_response({"matched_components": []}),
            provider_response({"test_cases": []}),
        ]
        provider = MiniMaxProvider(
            MiniMaxSettings(model="MiniMax-M2.7", vision_enabled=False),
            "secret",
            session=session,
        )

        result = provider.process_section(make_section_request())

        for call, schema in zip(
            session.post.call_args_list,
            (COMPONENT_OUTPUT_SCHEMA, CASE_OUTPUT_SCHEMA),
        ):
            payload = call.kwargs["json"]
            self.assertNotIn("response_format", payload)
            prompts = "\n".join(
                str(message["content"]) for message in payload["messages"]
            )
            self.assertIn("JSON", prompts)
            self.assertIn(
                json.dumps(
                    schema,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                prompts,
            )
        for evidence in result.evidence:
            if evidence.stage != "image_analysis":
                self.assertIn(
                    "native structured output unavailable",
                    evidence.detail,
                )

    def test_structured_retry_adds_strict_bare_json_correction(self):
        case = valid_case(case_name="corrected")
        session = Mock()
        session.post.side_effect = [
            provider_response({"matched_components": []}),
            provider_content_response("not-json"),
            provider_response({"test_cases": [case]}),
        ]
        provider = MiniMaxProvider(
            MiniMaxSettings(model="MiniMax-M2.7", vision_enabled=False),
            "secret",
            session=session,
        )

        result = provider.process_section(make_section_request())

        self.assertEqual(result.test_cases, [case])
        self.assertEqual(session.post.call_count, 3)
        first_case_payload = session.post.call_args_list[1].kwargs["json"]
        retry_case_payload = session.post.call_args_list[2].kwargs["json"]
        first_prompt = payload_prompt_text(first_case_payload)
        retry_prompt = payload_prompt_text(retry_case_payload)
        marker = "single bare JSON object"
        self.assertNotIn(marker, first_prompt)
        self.assertIn(marker, retry_prompt)
        for required_instruction in (
            "entire response",
            "reasoning",
            "explanation",
            "Markdown",
            "code fence",
            "required",
            "string",
        ):
            self.assertIn(required_instruction, retry_prompt)
        schema_text = json.dumps(
            CASE_OUTPUT_SCHEMA,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.assertIn(schema_text, first_prompt)
        self.assertIn(schema_text, retry_prompt)

    def test_http_and_timeout_retries_do_not_add_structured_correction(self):
        transient_errors = (
            http_error_response(503),
            requests.Timeout("timeout"),
        )
        for transient_error in transient_errors:
            with self.subTest(error_type=type(transient_error).__name__):
                session = Mock()
                session.post.side_effect = [
                    transient_error,
                    provider_response({"matched_components": []}),
                    provider_response({"test_cases": []}),
                ]
                provider = MiniMaxProvider(
                    MiniMaxSettings(
                        model="MiniMax-M2.7",
                        vision_enabled=False,
                    ),
                    "secret",
                    session=session,
                )

                provider.process_section(make_section_request())

                first_payload = session.post.call_args_list[0].kwargs["json"]
                retry_payload = session.post.call_args_list[1].kwargs["json"]
                self.assertEqual(first_payload, retry_payload)
                self.assertNotIn(
                    "single bare JSON object",
                    payload_prompt_text(retry_payload),
                )

    def _assert_component_output_rejected(self, content):
        for provider_class, settings in (
            (
                OpenAICompatibleProvider,
                OpenAICompatibleSettings(vision_enabled=False),
            ),
            (MiniMaxProvider, MiniMaxSettings(vision_enabled=False)),
        ):
            with self.subTest(provider=provider_class.__name__):
                session = Mock()
                session.post.side_effect = [
                    provider_content_response(content),
                    provider_content_response(content),
                ]
                provider = provider_class(settings, "secret", session=session)

                with self.assertRaises(ProviderResponseError) as raised:
                    provider.process_section(make_section_request())

                self.assertEqual(session.post.call_count, 2)
                self.assertNotIn("secret", str(raised.exception))

    def test_empty_output_is_rejected_after_one_retry(self):
        self._assert_component_output_rejected("")

    def test_markdown_wrapped_json_is_rejected_after_one_retry(self):
        self._assert_component_output_rejected(
            "```json\n{\"matched_components\": []}\n```"
        )

    def test_malformed_json_is_rejected_after_one_retry(self):
        self._assert_component_output_rejected("{not-json")

    def test_missing_required_field_is_rejected_after_one_retry(self):
        self._assert_component_output_rejected(json.dumps({"other": []}))

    def test_additional_properties_are_rejected_after_one_retry(self):
        self._assert_component_output_rejected(
            json.dumps({"matched_components": [], "extra": "no"})
        )

    def test_wrong_json_value_type_is_rejected_after_one_retry(self):
        self._assert_component_output_rejected(
            json.dumps({"matched_components": [1]})
        )

    def test_case_schema_requires_all_eleven_string_fields_and_no_extras(self):
        invalid_cases = []
        missing = valid_case()
        missing.pop("execution")
        invalid_cases.append(missing)
        invalid_cases.append(valid_case(extra="not-allowed"))
        invalid_cases.append(valid_case(priority=1))

        for invalid_case in invalid_cases:
            with self.subTest(case=invalid_case):
                session = Mock()
                session.post.side_effect = [
                    provider_response({"matched_components": []}),
                    provider_response({"test_cases": [invalid_case]}),
                    provider_response({"test_cases": [invalid_case]}),
                ]
                provider = OpenAICompatibleProvider(
                    OpenAICompatibleSettings(vision_enabled=False),
                    "secret",
                    session=session,
                )

                with self.assertRaises(ProviderResponseError):
                    provider.process_section(make_section_request())

                self.assertEqual(session.post.call_count, 3)
                first_schema = session.post.call_args_list[0].kwargs["json"][
                    "response_format"
                ]["json_schema"]["schema"]
                self.assertEqual(first_schema, COMPONENT_OUTPUT_SCHEMA)

    def test_invalid_case_stage_retries_only_that_stage_and_records_evidence(self):
        case = valid_case(case_name="登录成功")
        session = Mock()
        session.post.side_effect = [
            provider_response({"matched_components": ["按钮"]}),
            provider_response({"test_cases": [{"module": "missing"}]}),
            provider_response({"test_cases": [case]}),
        ]
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(vision_enabled=False),
            "secret",
            session=session,
        )

        result = provider.process_section(make_section_request())

        self.assertEqual(session.post.call_count, 3)
        self.assertEqual(result.test_cases, [case])
        self.assertEqual(
            [item.stage for item in result.evidence],
            ["image_analysis", "component_matching", "case_generation"],
        )
        image, component, cases = result.evidence
        self.assertIn("skipped", image.detail)
        self.assertTrue(image.output_valid)
        self.assertEqual(image.retry_count, 0)
        self.assertEqual(component.retry_count, 0)
        self.assertEqual(cases.retry_count, 1)
        for item in result.evidence:
            self.assertGreaterEqual(item.duration_ms, 0)
            self.assertTrue(item.output_valid)
            self.assertEqual(item.provider, result.provider)
            self.assertEqual(item.runtime_mode, result.runtime_mode)
            self.assertEqual(item.model, result.model)
        self.assertEqual(
            result.retry_count,
            sum(item.retry_count for item in result.evidence),
        )
        self.assertEqual(
            result.output_valid,
            all(item.output_valid for item in result.evidence),
        )

    def test_health_probe_also_uses_strict_local_validation(self):
        session = Mock()
        session.post.return_value = provider_response(
            {"matched_components": [], "extra": "not-allowed"}
        )
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(vision_enabled=False),
            "secret",
            session=session,
        )

        health = provider.health_check()

        self.assertFalse(health.ok)
        session.post.assert_called_once()

    def test_vision_skip_evidence_distinguishes_disabled_and_no_images(self):
        cases = (
            (
                OpenAICompatibleSettings(vision_enabled=False),
                (Path("C:/private/must-not-be-read.png"),),
                "skipped: vision disabled",
            ),
            (
                OpenAICompatibleSettings(vision_enabled=True),
                (),
                "skipped: no images",
            ),
        )
        for settings, images, expected_detail in cases:
            with self.subTest(detail=expected_detail):
                session = Mock()
                session.post.side_effect = [
                    provider_response({"matched_components": []}),
                    provider_response({"test_cases": []}),
                ]
                provider = OpenAICompatibleProvider(
                    settings,
                    "secret",
                    session=session,
                )

                result = provider.process_section(
                    make_section_request(images=images)
                )

                self.assertEqual(session.post.call_count, 2)
                self.assertEqual(result.evidence[0].detail, expected_detail)
                self.assertTrue(result.evidence[0].output_valid)
                self.assertEqual(result.evidence[0].duration_ms, 0)

    def test_vision_encodes_all_supported_images_in_one_user_message(self):
        image_data = {
            "first.png": ("image/png", b"png-bytes"),
            "second.jpg": ("image/jpeg", b"jpg-bytes"),
            "third.jpeg": ("image/jpeg", b"jpeg-bytes"),
            "fourth.webp": ("image/webp", b"webp-bytes"),
        }
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for filename, (_, content) in image_data.items():
                path = Path(directory) / filename
                path.write_bytes(content)
                paths.append(path)
            session = Mock()
            case = valid_case()
            session.post.side_effect = [
                provider_response({"image_findings": ["登录页"]}),
                provider_response(
                    {"matched_components": ["按钮", "输入框"]}
                ),
                provider_response({"test_cases": [case]}),
            ]
            provider = OpenAICompatibleProvider(
                OpenAICompatibleSettings(vision_enabled=True),
                "secret",
                session=session,
            )

            result = provider.process_section(
                make_section_request(images=tuple(paths))
            )

        self.assertEqual(session.post.call_count, 3)
        image_payload = session.post.call_args_list[0].kwargs["json"]
        self.assertEqual(
            image_payload["response_format"]["json_schema"]["schema"],
            IMAGE_OUTPUT_SCHEMA,
        )
        user_messages = [
            message
            for message in image_payload["messages"]
            if message["role"] == "user"
        ]
        self.assertEqual(len(user_messages), 1)
        content = user_messages[0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(len(content), 1 + len(image_data))
        for part, (filename, (mime, raw)) in zip(
            content[1:], image_data.items()
        ):
            self.assertEqual(
                part,
                {
                    "type": "image_url",
                    "image_url": {
                        "url": (
                            f"data:{mime};base64,"
                            f"{base64.b64encode(raw).decode('ascii')}"
                        )
                    },
                },
            )
            self.assertNotIn(filename, json.dumps(image_payload))
        self.assertEqual(result.image_findings, ["登录页"])
        self.assertEqual(result.test_cases, [case])
        self.assertEqual(result.evidence[0].stage, "image_analysis")
        self.assertTrue(result.evidence[0].output_valid)

    def test_invalid_image_response_retries_only_image_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "screen.png"
            path.write_bytes(b"image")
            session = Mock()
            session.post.side_effect = [
                provider_response({"image_findings": [1]}),
                provider_response({"image_findings": ["valid"]}),
                provider_response({"matched_components": []}),
                provider_response({"test_cases": []}),
            ]
            provider = MiniMaxProvider(
                MiniMaxSettings(vision_enabled=True),
                "secret",
                session=session,
            )

            result = provider.process_section(
                make_section_request(images=(path,))
            )

        self.assertEqual(session.post.call_count, 4)
        image, component, cases = result.evidence
        self.assertEqual(image.retry_count, 1)
        self.assertEqual(component.retry_count, 0)
        self.assertEqual(cases.retry_count, 0)

    def test_invalid_or_unreadable_image_path_fails_safely_without_network(self):
        unsafe_images = (
            Path("C:/private/token-query-secret.bmp"),
            Path("C:/private/token-query-secret.png"),
            "https://example.invalid/image.png?token=query-secret",
        )
        for image in unsafe_images:
            with self.subTest(image_type=type(image).__name__):
                session = Mock()
                provider = OpenAICompatibleProvider(
                    OpenAICompatibleSettings(vision_enabled=True),
                    "provider-secret",
                    session=session,
                )

                with self.assertRaises(ProviderResponseError) as raised:
                    provider.process_section(
                        make_section_request(images=(image,))
                    )

                session.post.assert_not_called()
                public_error = str(raised.exception)
                for forbidden in (
                    "token-query-secret",
                    "query-secret",
                    "provider-secret",
                    "C:/private",
                    "https://example.invalid",
                ):
                    self.assertNotIn(forbidden, public_error)


class LegacyMiniMaxServiceTests(unittest.TestCase):
    def test_wrapper_rejects_unsafe_base_urls_without_network(self):
        invalid_urls = (
            "http://minimax.example.test/gateway",
            "https://user-secret:password-secret@minimax.example.test/gateway",
            "https://minimax.example.test/gateway?token=query-secret",
            "https://minimax.example.test/gateway#fragment-secret",
            "https:///missing-host",
            "https://minimax.example.test:bad-port/gateway",
            "https://minimax.example.test:70000/gateway",
        )
        for invalid_url in invalid_urls:
            with self.subTest(invalid_url=invalid_url):
                session = Mock()

                with self.assertRaises(RuntimeError) as raised:
                    MiniMaxService(
                        "wrapper-secret",
                        invalid_url,
                        "model",
                        60,
                        session,
                    )

                session.post.assert_not_called()
                public_error = str(raised.exception)
                self.assertNotIn(invalid_url, public_error)
                for forbidden in (
                    "wrapper-secret",
                    "user-secret",
                    "password-secret",
                    "query-secret",
                    "fragment-secret",
                    "bad-port",
                ):
                    self.assertNotIn(forbidden, public_error)

    def test_wrapper_normalizes_ipv6_path_and_trailing_slashes(self):
        session = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content": "output"}}]
        }
        session.post.return_value = response
        service = MiniMaxService(
            "key",
            "https://[2001:db8::1]:8443/gateway///",
            "model",
            60,
            session,
        )

        service.chat([{"role": "user", "content": "prompt"}])

        self.assertEqual(
            session.post.call_args.args[0],
            (
                "https://[2001:db8::1]:8443/gateway/"
                "v1/text/chatcompletion_v2"
            ),
        )

    def test_dependency_explicit_factory_preserves_legacy_import_only(self):
        factory = legacy_minimax.create_minimax_service
        signature = inspect.signature(factory)
        self.assertEqual(
            list(signature.parameters),
            [
                "api_key",
                "base_url",
                "model",
                "timeout_seconds",
                "session",
            ],
        )
        for parameter in signature.parameters.values():
            self.assertIs(parameter.default, inspect.Parameter.empty)
        session = Mock()

        service = factory(
            "key",
            "https://minimax.example.test",
            "model",
            60,
            session,
        )

        self.assertIsInstance(service, MiniMaxService)
        session.post.assert_not_called()

    def test_constructor_requires_every_dependency(self):
        signature = inspect.signature(MiniMaxService.__init__)
        self.assertEqual(
            list(signature.parameters),
            [
                "self",
                "api_key",
                "base_url",
                "model",
                "timeout_seconds",
                "session",
            ],
        )
        for parameter in list(signature.parameters.values())[1:]:
            self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_chat_posts_only_caller_supplied_messages_with_secure_transport(self):
        session = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content": "structured output"}}]
        }
        session.post.return_value = response
        messages = [
            {"role": "system", "content": "caller system prompt"},
            {"role": "user", "content": "caller user prompt"},
        ]
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": "caller", "schema": {"type": "object"}},
        }
        service = MiniMaxService(
            api_key="snapshot-key",
            base_url="https://minimax.example.test/",
            model="snapshot-model",
            timeout_seconds=53,
            session=session,
        )

        result = service.chat(messages, response_format=response_format)

        self.assertEqual(result, "structured output")
        session.post.assert_called_once_with(
            "https://minimax.example.test/v1/text/chatcompletion_v2",
            json={
                "model": "snapshot-model",
                "messages": messages,
                "response_format": response_format,
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer snapshot-key",
            },
            timeout=53,
            verify=True,
            allow_redirects=False,
        )

    def test_chat_extracts_the_legacy_messages_response_shape(self):
        session = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"messages": [{"content": "legacy output"}]}]
        }
        session.post.return_value = response
        service = MiniMaxService(
            "key",
            "https://minimax.example.test",
            "model",
            60,
            session,
        )

        result = service.chat([{"role": "user", "content": "prompt"}])

        self.assertEqual(result, "legacy output")

    def test_chat_raises_redacted_errors_instead_of_returning_error_text(self):
        raw_error = (
            "Authorization: Bearer wrapper-secret "
            "https://example.invalid/path?token=query-secret"
        )

        def post_failure(session):
            session.post.side_effect = RuntimeError(raw_error)

        def status_failure(session):
            response = Mock()
            response.raise_for_status.side_effect = RuntimeError(raw_error)
            session.post.return_value = response

        def body_error(session):
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"error": {"message": raw_error}}
            session.post.return_value = response

        def empty_choices(session):
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"choices": []}
            session.post.return_value = response

        def empty_content(session):
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "choices": [{"message": {"content": ""}}]
            }
            session.post.return_value = response

        for arrange in (
            post_failure,
            status_failure,
            body_error,
            empty_choices,
            empty_content,
        ):
            with self.subTest(failure=arrange.__name__):
                session = Mock()
                arrange(session)
                service = MiniMaxService(
                    "wrapper-secret",
                    "https://minimax.example.test",
                    "model",
                    60,
                    session,
                )

                with self.assertRaises(RuntimeError) as raised:
                    service.chat([{"role": "user", "content": "prompt"}])

                public_error = str(raised.exception)
                for forbidden in (
                    "wrapper-secret",
                    "Authorization",
                    "query-secret",
                    raw_error,
                ):
                    self.assertNotIn(forbidden, public_error)

    def test_chat_redacts_failures_while_copying_caller_payload(self):
        class ExplodingCopy:
            def __deepcopy__(self, memo):
                raise RuntimeError(
                    "Authorization: Bearer wrapper-secret query-secret"
                )

        session = Mock()
        service = MiniMaxService(
            "wrapper-secret",
            "https://minimax.example.test",
            "model",
            60,
            session,
        )

        with self.assertRaises(RuntimeError) as raised:
            service.chat(
                [{"role": "user", "content": ExplodingCopy()}]
            )

        session.post.assert_not_called()
        public_error = str(raised.exception)
        for forbidden in (
            "wrapper-secret",
            "Authorization",
            "query-secret",
        ):
            self.assertNotIn(forbidden, public_error)

    def test_wrapper_contains_no_prompts_parsers_or_root_config_fallback(self):
        for removed_method in (
            "understand_image",
            "match_components",
            "generate_test_cases",
        ):
            self.assertFalse(hasattr(MiniMaxService, removed_method))
        source = inspect.getsource(legacy_minimax)
        for forbidden_source in (
            "IMAGE_UNDERSTAND_PROMPT",
            "from config import",
            "verify=False",
            "proxies=",
            "```json",
        ):
            self.assertNotIn(forbidden_source, source)


class HttpProviderVisionPromptTests(unittest.TestCase):
    def test_deepseek_json_schema_misconfiguration_fails_without_network(self):
        session = Mock()
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                preset=OpenAICompatiblePreset.deepseek,
                base_url="https://api.deepseek.com",
                model="deepseek-v4",
                response_format_mode=ResponseFormatMode.json_schema,
                vision_enabled=False,
            ),
            "secret",
            session=session,
        )

        health = provider.health_check()
        self.assertFalse(health.ok)
        session.post.assert_not_called()

        with self.assertRaises(ProviderUnavailableError):
            provider.process_section(make_section_request())
        session.post.assert_not_called()

    def test_deepseek_preset_never_sends_images_even_if_flag_is_true(self):
        session = Mock()
        session.post.side_effect = [
            provider_response({"matched_components": []}),
            provider_response({"test_cases": []}),
        ]
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                preset=OpenAICompatiblePreset.deepseek,
                base_url="https://api.deepseek.com",
                model="deepseek-v4",
                response_format_mode=ResponseFormatMode.json_object,
                vision_enabled=True,
            ),
            "secret",
            session=session,
        )

        result = provider.process_section(
            make_section_request(
                images=(Path("C:/private/must-not-be-read.png"),)
            )
        )

        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(
            result.evidence[0].detail,
            "skipped: provider preset is text-only",
        )
        for call in session.post.call_args_list:
            self.assertNotIn("image_url", json.dumps(call.kwargs["json"]))

    def test_deepseek_text_only_skip_precedes_disabled_vision_flag(self):
        session = Mock()
        session.post.side_effect = [
            provider_response({"matched_components": []}),
            provider_response({"test_cases": []}),
        ]
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                preset=OpenAICompatiblePreset.deepseek,
                base_url="https://api.deepseek.com",
                model="deepseek-v4",
                response_format_mode=ResponseFormatMode.json_object,
                vision_enabled=False,
            ),
            "secret",
            session=session,
        )

        result = provider.process_section(make_section_request())

        self.assertEqual(
            result.evidence[0].detail,
            "skipped: provider preset is text-only",
        )

    def test_all_four_prompts_render_and_only_matched_templates_are_sent(self):
        prompts = {
            "image_understanding": "IMAGE {section_title} {image_count}",
            "component_matching": (
                "COMPONENTS {requirement} {component_names}"
            ),
            "case_generation_system": "SYSTEM {field_specs}",
            "case_generation_user": (
                "USER {section_title} {section_content} {image_findings} "
                "{matched_components} {matched_templates}"
            ),
        }
        templates = {
            "按钮": [{"marker": "MATCHED_BUTTON_TEMPLATE"}],
            "输入框": [{"marker": "MATCHED_INPUT_TEMPLATE"}],
            "未知": [{"marker": "UNMATCHED_TEMPLATE_MUST_NOT_APPEAR"}],
        }
        session = Mock()
        session.post.side_effect = [
            provider_response(
                {
                    "matched_components": [
                        "未知",
                        "按钮",
                        "按钮",
                        "输入框",
                        "未知",
                    ]
                }
            ),
            provider_response({"test_cases": []}),
        ]
        provider = OpenAICompatibleProvider(
            OpenAICompatibleSettings(vision_enabled=False),
            "secret",
            session=session,
        )
        rendered_names = []
        original_render = PromptCatalog.render

        def tracking_render(name, template, **values):
            rendered_names.append(name)
            return original_render(name, template, **values)

        with patch.object(PromptCatalog, "render", side_effect=tracking_render):
            result = provider.process_section(
                make_section_request(
                    component_names=("输入框", "按钮"),
                    field_specs={"marker": "FIELD_SPEC_MARKER"},
                    component_templates=templates,
                    prompts=prompts,
                )
            )

        self.assertEqual(
            rendered_names,
            [
                "image_understanding",
                "component_matching",
                "case_generation_system",
                "case_generation_user",
                "image_understanding",
                "component_matching",
                "case_generation_system",
                "case_generation_user",
            ],
        )
        self.assertEqual(result.matched_components, ["按钮", "输入框"])
        case_prompts = "\n".join(
            str(message["content"])
            for message in session.post.call_args_list[-1].kwargs["json"][
                "messages"
            ]
        )
        self.assertIn("FIELD_SPEC_MARKER", case_prompts)
        self.assertIn("MATCHED_BUTTON_TEMPLATE", case_prompts)
        self.assertIn("MATCHED_INPUT_TEMPLATE", case_prompts)
        self.assertNotIn("UNMATCHED_TEMPLATE_MUST_NOT_APPEAR", case_prompts)

    def test_all_prompt_format_specs_are_rendered_before_first_network(self):
        defaults = default_settings().prompts.model_dump()
        invalid_prompts = {
            "image_understanding": (
                defaults["image_understanding"]
                + "\n{section_title:invalid_format_spec}"
            ),
            "component_matching": (
                defaults["component_matching"]
                + "\n{requirement:invalid_format_spec}"
            ),
            "case_generation_system": (
                defaults["case_generation_system"]
                + "\n{field_specs:invalid_format_spec}"
            ),
            "case_generation_user": (
                defaults["case_generation_user"]
                + "\n{section_title:invalid_format_spec}"
            ),
        }
        for prompt_name, invalid_prompt in invalid_prompts.items():
            with self.subTest(prompt=prompt_name):
                prompts = dict(defaults)
                prompts[prompt_name] = invalid_prompt
                session = Mock()
                session.post.return_value = provider_response(
                    {"matched_components": []}
                )
                provider = OpenAICompatibleProvider(
                    OpenAICompatibleSettings(vision_enabled=False),
                    "secret",
                    session=session,
                )

                with self.assertRaises(ProviderResponseError) as raised:
                    provider.process_section(
                        make_section_request(prompts=prompts)
                    )

                session.post.assert_not_called()
                self.assertNotIn("invalid_format_spec", str(raised.exception))

    def test_invalid_prompt_in_any_of_four_slots_fails_before_network(self):
        defaults = default_settings().prompts.model_dump()
        invalid_prompts = {
            "image_understanding": "{unknown}",
            "component_matching": "{requirement}",
            "case_generation_system": "no required field specs",
            "case_generation_user": "{section_title}",
        }
        for prompt_name, invalid_prompt in invalid_prompts.items():
            with self.subTest(prompt=prompt_name):
                prompts = dict(defaults)
                prompts[prompt_name] = invalid_prompt
                session = Mock()
                provider = OpenAICompatibleProvider(
                    OpenAICompatibleSettings(vision_enabled=False),
                    "secret",
                    session=session,
                )

                with self.assertRaises(ProviderResponseError):
                    provider.process_section(
                        make_section_request(prompts=prompts)
                    )

                session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
