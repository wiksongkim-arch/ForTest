from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import operator
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import backend.ai.images as image_module
from backend.ai.base import ProviderUnavailableError
from backend.ai.images import ImageDownloadError, ImageWorkspace
from backend.ai.registry import ProviderRegistry
from backend.ai.types import (
    CASE_OUTPUT_SCHEMA,
    COMPONENT_OUTPUT_SCHEMA,
    IMAGE_OUTPUT_SCHEMA,
    SECTION_OUTPUT_SCHEMA,
    TEST_CASE_FIELDS,
    ProviderHealth,
    SectionAIRequest,
    SectionAIResult,
)
from backend.settings.defaults import default_settings
from backend.settings.models import ProviderName, ResolvedSecrets, SettingsSnapshot


class FakeProvider:
    def __init__(self, name: ProviderName, healthy: bool):
        self.name = name
        self.healthy = healthy
        self.close_count = 0
        self.process_count = 0

    def health_check(self):
        return ProviderHealth(ok=self.healthy, provider=self.name, detail="test")

    def process_section(self, request):
        self.process_count += 1
        return SectionAIResult(
            provider=self.name,
            runtime_mode="fake",
            model="fake-model",
            duration_ms=1,
            retry_count=0,
            output_valid=True,
            test_cases=[],
        )

    def close(self):
        self.close_count += 1


def settings_snapshot(
    *,
    fallback_enabled: bool = False,
    fallback_provider: ProviderName | None = None,
) -> SettingsSnapshot:
    settings = default_settings()
    ai = settings.ai.model_copy(
        update={
            "active_provider": ProviderName.codex,
            "fallback_enabled": fallback_enabled,
            "fallback_provider": fallback_provider,
        }
    )
    return SettingsSnapshot(
        settings=settings.model_copy(update={"ai": ai}),
        secrets=ResolvedSecrets(),
    )


class ProviderCoreTests(unittest.TestCase):
    def test_registry_selects_fallback_before_first_section(self):
        snapshot = settings_snapshot(
            fallback_enabled=True,
            fallback_provider=ProviderName.minimax,
        )
        active = FakeProvider(ProviderName.codex, False)
        fallback = FakeProvider(ProviderName.minimax, True)
        registry = ProviderRegistry(
            {
                ProviderName.codex: lambda _: active,
                ProviderName.minimax: lambda _: fallback,
            }
        )

        provider, decision = registry.create_for_task(snapshot)
        provider.process_section(None)
        provider.process_section(None)

        self.assertIs(provider, fallback)
        self.assertEqual(provider.name, ProviderName.minimax)
        self.assertEqual(decision.selected, ProviderName.minimax)
        self.assertEqual(decision.requested, ProviderName.codex)
        self.assertTrue(decision.used_fallback)
        self.assertEqual(active.close_count, 1)
        self.assertEqual(fallback.close_count, 0)
        self.assertEqual(fallback.process_count, 2)

    def test_registry_never_returns_unhealthy_provider_without_fallback(self):
        snapshot = settings_snapshot()
        active = FakeProvider(ProviderName.codex, False)
        registry = ProviderRegistry({ProviderName.codex: lambda _: active})

        with self.assertRaises(ProviderUnavailableError):
            registry.create_for_task(snapshot)

        self.assertEqual(active.close_count, 1)

    def test_registry_does_not_construct_fallback_when_active_is_healthy(self):
        snapshot = settings_snapshot(
            fallback_enabled=True,
            fallback_provider=ProviderName.minimax,
        )
        active = FakeProvider(ProviderName.codex, True)
        fallback_factory_calls = 0

        def fallback_factory(_):
            nonlocal fallback_factory_calls
            fallback_factory_calls += 1
            return FakeProvider(ProviderName.minimax, True)

        registry = ProviderRegistry(
            {
                ProviderName.codex: lambda _: active,
                ProviderName.minimax: fallback_factory,
            }
        )

        provider, decision = registry.create_for_task(snapshot)

        self.assertIs(provider, active)
        self.assertFalse(decision.used_fallback)
        self.assertEqual(fallback_factory_calls, 0)
        self.assertEqual(active.close_count, 0)

    def test_registry_passes_the_exact_snapshot_to_factory(self):
        snapshot = settings_snapshot()
        received = []

        def factory(candidate):
            received.append(candidate)
            return FakeProvider(ProviderName.codex, True)

        provider, _ = ProviderRegistry(
            {ProviderName.codex: factory}
        ).create_for_task(snapshot)
        try:
            self.assertEqual(received, [snapshot])
            self.assertIs(received[0], snapshot)
        finally:
            provider.close()

    def test_registry_recovers_from_active_health_and_close_exceptions(self):
        snapshot = settings_snapshot(
            fallback_enabled=True,
            fallback_provider=ProviderName.minimax,
        )
        active = FakeProvider(ProviderName.codex, True)
        active.health_check = Mock(
            side_effect=RuntimeError("credential=do-not-leak")
        )
        active.close = Mock(side_effect=RuntimeError("close failed"))
        fallback = FakeProvider(ProviderName.minimax, True)
        registry = ProviderRegistry(
            {
                ProviderName.codex: lambda _: active,
                ProviderName.minimax: lambda _: fallback,
            }
        )

        provider, decision = registry.create_for_task(snapshot)

        self.assertIs(provider, fallback)
        self.assertTrue(decision.used_fallback)
        active.close.assert_called_once_with()

    def test_registry_constructor_failure_is_redacted_and_can_fallback(self):
        snapshot = settings_snapshot(
            fallback_enabled=True,
            fallback_provider=ProviderName.minimax,
        )
        fallback = FakeProvider(ProviderName.minimax, True)

        def broken_factory(_):
            raise RuntimeError("arbitrary-credential-value")

        registry = ProviderRegistry(
            {
                ProviderName.codex: broken_factory,
                ProviderName.minimax: lambda _: fallback,
            }
        )

        provider, decision = registry.create_for_task(snapshot)

        self.assertIs(provider, fallback)
        self.assertNotIn("arbitrary-credential-value", decision.reason)

    def test_registry_failure_never_exposes_constructor_exception(self):
        snapshot = settings_snapshot()

        def broken_factory(_):
            raise RuntimeError("arbitrary-credential-value")

        registry = ProviderRegistry({ProviderName.codex: broken_factory})

        with self.assertRaises(ProviderUnavailableError) as raised:
            registry.create_for_task(snapshot)

        self.assertNotIn("arbitrary-credential-value", str(raised.exception))

    def test_health_for_always_closes_provider(self):
        snapshot = settings_snapshot()
        provider = FakeProvider(ProviderName.codex, True)
        registry = ProviderRegistry({ProviderName.codex: lambda _: provider})

        health = registry.health_for(ProviderName.codex, snapshot)

        self.assertTrue(health.ok)
        self.assertEqual(provider.close_count, 1)

    def test_health_for_contains_health_and_close_exceptions(self):
        snapshot = settings_snapshot()
        provider = FakeProvider(ProviderName.codex, True)
        provider.health_check = Mock(
            side_effect=RuntimeError("arbitrary-credential-value")
        )
        provider.close = Mock(side_effect=RuntimeError("close failed"))
        registry = ProviderRegistry({ProviderName.codex: lambda _: provider})

        health = registry.health_for(ProviderName.codex, snapshot)

        self.assertFalse(health.ok)
        self.assertEqual(health.provider, ProviderName.codex)
        self.assertNotIn("arbitrary-credential-value", health.detail)
        provider.close.assert_called_once_with()

    def test_health_for_contains_constructor_exception(self):
        snapshot = settings_snapshot()

        def broken_factory(_):
            raise RuntimeError("arbitrary-credential-value")

        health = ProviderRegistry(
            {ProviderName.codex: broken_factory}
        ).health_for(ProviderName.codex, snapshot)

        self.assertFalse(health.ok)
        self.assertEqual(health.provider, ProviderName.codex)
        self.assertNotIn("arbitrary-credential-value", health.detail)

    def test_eleven_column_schema_is_exact_and_closed(self):
        expected_fields = (
            "module",
            "case_name",
            "prerequisite",
            "test_steps",
            "expected_result",
            "priority",
            "case_type",
            "applicable_phase",
            "remark",
            "case_id",
            "execution",
        )

        self.assertEqual(TEST_CASE_FIELDS, expected_fields)
        for schema in (CASE_OUTPUT_SCHEMA, SECTION_OUTPUT_SCHEMA):
            item_schema = schema["properties"]["test_cases"]["items"]
            self.assertEqual(tuple(item_schema["properties"]), expected_fields)
            self.assertEqual(item_schema["required"], list(expected_fields))
            self.assertFalse(item_schema["additionalProperties"])
            self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            SECTION_OUTPUT_SCHEMA["required"],
            ["image_findings", "matched_components", "test_cases"],
        )
        self.assertEqual(IMAGE_OUTPUT_SCHEMA["required"], ["image_findings"])
        self.assertEqual(
            COMPONENT_OUTPUT_SCHEMA["required"], ["matched_components"]
        )

    def test_section_request_is_frozen(self):
        request = SectionAIRequest(
            section_title="title",
            section_content="content",
            images=(),
            component_names=(),
            field_specs={},
            component_templates={},
            prompts={},
        )

        with self.assertRaises(FrozenInstanceError):
            request.section_title = "changed"

    def test_section_request_defensively_copies_nested_inputs(self):
        images = [Path("first.png")]
        component_names = ["button"]
        field_specs = {"priority": "P0"}
        component_templates = {"button": [{"case_name": "original"}]}
        prompts = {"system": "original"}
        output_schema = {
            "type": "object",
            "properties": {
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                }
            },
            "required": ["values"],
            "additionalProperties": False,
        }
        request = SectionAIRequest(
            section_title="title",
            section_content="content",
            images=images,
            component_names=component_names,
            field_specs=field_specs,
            component_templates=component_templates,
            prompts=prompts,
            output_schema=output_schema,
        )

        images.append(Path("second.png"))
        component_names.append("input")
        field_specs["priority"] = "changed"
        component_templates["button"][0]["case_name"] = "changed"
        component_templates["button"].append({"case_name": "new"})
        prompts["system"] = "changed"
        output_schema["required"].append("pollution")
        output_schema["properties"]["values"]["items"]["type"] = "number"

        self.assertEqual(request.images, (Path("first.png"),))
        self.assertEqual(request.component_names, ("button",))
        self.assertEqual(request.field_specs["priority"], "P0")
        self.assertEqual(
            request.component_templates["button"],
            [{"case_name": "original"}],
        )
        self.assertEqual(request.prompts["system"], "original")
        self.assertEqual(request.output_schema["required"], ["values"])
        self.assertEqual(
            request.output_schema["properties"]["values"]["items"]["type"],
            "string",
        )

    def test_section_request_rejects_common_nested_mutations(self):
        def make_request():
            return SectionAIRequest(
                section_title="title",
                section_content="content",
                images=(),
                component_names=(),
                field_specs={"priority": "P0"},
                component_templates={
                    "button": [{"case_name": "original"}]
                },
                prompts={"system": "prompt"},
                output_schema=deepcopy(CASE_OUTPUT_SCHEMA),
            )

        dict_targets = (
            ("field_specs", lambda r: (r.field_specs, "priority")),
            ("prompts", lambda r: (r.prompts, "system")),
            (
                "component_templates",
                lambda r: (r.component_templates, "button"),
            ),
            (
                "component_template_item",
                lambda r: (
                    r.component_templates["button"][0],
                    "case_name",
                ),
            ),
            ("output_schema", lambda r: (r.output_schema, "type")),
            (
                "output_schema_properties",
                lambda r: (
                    r.output_schema["properties"],
                    "test_cases",
                ),
            ),
        )
        not_rejected = []
        for label, target in dict_targets:
            for mutation_name in (
                "setitem",
                "delitem",
                "update",
                "clear",
                "pop",
                "popitem",
                "setdefault",
                "ior",
            ):
                mapping, existing_key = target(make_request())
                mutations = {
                    "setitem": lambda: operator.setitem(mapping, "new", "x"),
                    "delitem": lambda: operator.delitem(mapping, existing_key),
                    "update": lambda: mapping.update({"new": "x"}),
                    "clear": mapping.clear,
                    "pop": lambda: mapping.pop(existing_key),
                    "popitem": mapping.popitem,
                    "setdefault": lambda: mapping.setdefault("new", "x"),
                    "ior": lambda: operator.ior(mapping, {"new": "x"}),
                }
                try:
                    mutations[mutation_name]()
                except TypeError:
                    continue
                not_rejected.append(f"{label}.{mutation_name}")

        list_targets = (
            (
                "component_template_list",
                lambda r: r.component_templates["button"],
            ),
            (
                "output_schema_required",
                lambda r: r.output_schema["required"],
            ),
            (
                "output_schema_item_required",
                lambda r: r.output_schema["properties"]["test_cases"][
                    "items"
                ]["required"],
            ),
        )
        for label, target in list_targets:
            for mutation_name in (
                "setitem",
                "delitem",
                "slice_assignment",
                "append",
                "extend",
                "insert",
                "pop",
                "remove",
                "sort",
                "reverse",
                "clear",
                "iadd",
                "imul",
            ):
                values = target(make_request())
                mutations = {
                    "setitem": lambda: operator.setitem(values, 0, "x"),
                    "delitem": lambda: operator.delitem(values, 0),
                    "slice_assignment": lambda: operator.setitem(
                        values, slice(None), []
                    ),
                    "append": lambda: values.append("x"),
                    "extend": lambda: values.extend(["x"]),
                    "insert": lambda: values.insert(0, "x"),
                    "pop": values.pop,
                    "remove": lambda: values.remove(values[0]),
                    "sort": values.sort,
                    "reverse": values.reverse,
                    "clear": values.clear,
                    "iadd": lambda: operator.iadd(values, ["x"]),
                    "imul": lambda: operator.imul(values, 2),
                }
                try:
                    mutations[mutation_name]()
                except TypeError:
                    continue
                not_rejected.append(f"{label}.{mutation_name}")

        self.assertEqual(not_rejected, [])

    def test_default_request_schemas_have_disjoint_nested_containers(self):
        def container_ids(value):
            ids = set()
            if isinstance(value, (dict, list)):
                ids.add(id(value))
                for nested in (
                    value.values() if isinstance(value, dict) else value
                ):
                    ids.update(container_ids(nested))
            return ids

        first = SectionAIRequest("first", "content", (), (), {}, {}, {})
        second = SectionAIRequest("second", "content", (), (), {}, {}, {})

        first_ids = container_ids(first.output_schema)
        second_ids = container_ids(second.output_schema)
        global_ids = container_ids(CASE_OUTPUT_SCHEMA)
        self.assertTrue(first_ids.isdisjoint(second_ids))
        self.assertTrue(first_ids.isdisjoint(global_ids))
        self.assertTrue(second_ids.isdisjoint(global_ids))

    def test_frozen_schema_stays_json_serializable_and_lists_become_tuples(self):
        images = [Path("image.png")]
        component_names = ["button"]
        request = SectionAIRequest(
            section_title="title",
            section_content="content",
            images=images,
            component_names=component_names,
            field_specs={},
            component_templates={},
            prompts={},
        )

        self.assertIsInstance(request.images, tuple)
        self.assertIsInstance(request.component_names, tuple)
        self.assertEqual(json.loads(json.dumps(request.output_schema)), CASE_OUTPUT_SCHEMA)


def image_response(content_type="image/png", body=b"png"):
    response = Mock()
    response.status_code = 200
    response.headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
    }
    response.history = []
    response.url = "https://example.test/image"
    response.iter_content.return_value = [body]
    response.raise_for_status.return_value = None
    return response


def public_image_resolver(_hostname, port, **_kwargs):
    """测试只验证地址策略，不让伪会话触发真实 DNS。"""

    return [(2, 1, 6, "", ("8.8.8.8", port))]


def image_workspace(session, **kwargs):
    return ImageWorkspace(
        session=session,
        resolver=public_image_resolver,
        **kwargs,
    )


class ImageWorkspaceTests(unittest.TestCase):
    def test_rejected_content_type_leaves_no_download(self):
        session = Mock()
        session.get.return_value = image_response("text/html", b"not-an-image")
        workspace = image_workspace(session)
        try:
            with self.assertRaises(ImageDownloadError):
                workspace.download("https://example.test/image")
            self.assertEqual(list(workspace.path.iterdir()), [])
        finally:
            workspace.close()

    def test_more_than_five_images_is_rejected_before_network(self):
        session = Mock()
        workspace = image_workspace(session)
        try:
            with self.assertRaises(ImageDownloadError):
                workspace.download_many(
                    tuple(
                        f"https://example.test/{index}.png"
                        for index in range(6)
                    )
                )
            session.get.assert_not_called()
        finally:
            workspace.close()

    def test_batch_retries_a_transient_failure_without_exposing_the_url(self):
        session = Mock()
        session.get.side_effect = [
            OSError("credential=do-not-leak"),
            image_response("image/png", b"recovered"),
        ]
        delays: list[float] = []
        workspace = image_workspace(
            session,
            retry_delays_seconds=(0.0, 0.0),
            sleep=delays.append,
        )
        try:
            downloaded = workspace.download_many(
                ("https://example.test/signed.png?credential=do-not-leak",)
            )

            self.assertEqual(len(downloaded), 1)
            self.assertEqual(downloaded[0].read_bytes(), b"recovered")
            self.assertEqual(session.get.call_count, 2)
            self.assertEqual(delays, [0.0])
        finally:
            workspace.close()

    def test_batch_preserves_successful_images_when_another_exhausts_retries(self):
        session = Mock()
        session.get.side_effect = [
            image_response("image/png", b"available"),
            OSError("first"),
            OSError("second"),
            OSError("third"),
        ]
        workspace = image_workspace(
            session,
            retry_delays_seconds=(0.0, 0.0),
            sleep=lambda _delay: None,
        )
        try:
            downloaded = workspace.download_many(
                (
                    "https://example.test/available.png",
                    "https://example.test/unavailable.png",
                )
            )

            self.assertEqual(len(downloaded), 1)
            self.assertEqual(downloaded[0].read_bytes(), b"available")
            self.assertEqual(session.get.call_count, 4)
        finally:
            workspace.close()

    def test_batch_raises_when_every_image_exhausts_retries(self):
        session = Mock()
        session.get.side_effect = [OSError("network")] * 3
        workspace = image_workspace(
            session,
            retry_delays_seconds=(0.0, 0.0),
            sleep=lambda _delay: None,
        )
        try:
            with self.assertRaises(ImageDownloadError) as raised:
                workspace.download_many(
                    ("https://example.test/unavailable.png?token=private",)
                )
            self.assertNotIn("private", str(raised.exception))
            self.assertEqual(session.get.call_count, 3)
        finally:
            workspace.close()

    def test_initial_non_https_url_is_rejected_before_network(self):
        session = Mock()
        workspace = image_workspace(session)
        try:
            with self.assertRaises(ImageDownloadError) as raised:
                workspace.download(
                    "http://example.test/image.png?credential=do-not-leak"
                )
            session.get.assert_not_called()
            self.assertNotIn("do-not-leak", str(raised.exception))
        finally:
            workspace.close()

    def test_download_uses_fixed_network_policy_and_local_filename(self):
        session = Mock()
        response = image_response("image/jpeg", b"jpeg")
        response.url = "https://example.test/server-name.exe?token=secret"
        session.get.return_value = response
        workspace = image_workspace(session)
        try:
            downloaded = workspace.download(
                "https://example.test/server-name.exe?token=secret"
            )

            session.get.assert_called_once_with(
                "https://example.test/server-name.exe?token=secret",
                allow_redirects=False,
                timeout=(5, 30),
                verify=True,
                stream=True,
            )
            self.assertEqual(downloaded.parent, workspace.path)
            self.assertEqual(downloaded.suffix, ".jpg")
            self.assertNotIn("server-name", downloaded.name)
            self.assertEqual(downloaded.read_bytes(), b"jpeg")
        finally:
            workspace.close()

    def test_more_than_three_redirects_is_rejected(self):
        session = Mock()
        responses = []
        for index in range(4):
            response = image_response()
            response.status_code = 302
            response.headers["Location"] = (
                f"https://example.test/redirect-{index}"
            )
            responses.append(response)
        session.get.side_effect = responses
        workspace = image_workspace(session)
        try:
            with self.assertRaises(ImageDownloadError):
                workspace.download("https://example.test/image")
            self.assertEqual(session.get.call_count, 4)
            self.assertEqual(list(workspace.path.iterdir()), [])
        finally:
            workspace.close()

    def test_redirect_to_non_https_is_rejected_before_second_request(self):
        session = Mock()
        response = image_response()
        response.status_code = 302
        response.headers["Location"] = "http://example.test/redirect"
        session.get.return_value = response
        workspace = image_workspace(session)
        try:
            with self.assertRaises(ImageDownloadError):
                workspace.download("https://example.test/image")
            self.assertEqual(session.get.call_count, 1)
            self.assertEqual(list(workspace.path.iterdir()), [])
        finally:
            workspace.close()

    def test_loopback_literal_is_rejected_before_network(self):
        session = Mock()
        workspace = image_workspace(session)
        try:
            with self.assertRaises(ImageDownloadError):
                workspace.download("https://127.0.0.1/internal.png")
            session.get.assert_not_called()
        finally:
            workspace.close()

    def test_hostname_resolving_to_private_address_is_rejected_before_network(self):
        session = Mock()
        workspace = ImageWorkspace(
            session=session,
            resolver=lambda _host, port, **_kwargs: [
                (2, 1, 6, "", ("10.0.0.8", port))
            ],
        )
        try:
            with self.assertRaises(ImageDownloadError):
                workspace.download("https://intranet.example/image.png")
            session.get.assert_not_called()
        finally:
            workspace.close()

    def test_redirect_to_link_local_is_rejected_before_second_request(self):
        session = Mock()
        response = image_response()
        response.status_code = 302
        response.headers["Location"] = "https://169.254.169.254/metadata"
        session.get.return_value = response
        workspace = image_workspace(session)
        try:
            with self.assertRaises(ImageDownloadError):
                workspace.download("https://example.test/image")
            self.assertEqual(session.get.call_count, 1)
        finally:
            workspace.close()

    def test_real_requests_session_uses_pinned_transport(self):
        session = image_module.requests.Session()
        session.get = Mock(side_effect=AssertionError("must not resolve twice"))
        response = image_response(body=b"pinned")
        workspace = ImageWorkspace(
            session=session,
            resolver=public_image_resolver,
        )
        try:
            with patch.object(
                image_module._PinnedHTTPSAdapter,
                "send",
                return_value=response,
            ) as send:
                downloaded = workspace.download(
                    "https://example.test/image.png"
                )

            self.assertEqual(downloaded.read_bytes(), b"pinned")
            session.get.assert_not_called()
            self.assertEqual(send.call_count, 1)
        finally:
            workspace.close()

    def test_pinned_connection_uses_vetted_ip_but_keeps_tls_hostname(self):
        connection = image_module._PinnedHTTPSConnection(
            "example.test",
            443,
            pinned_addresses=("8.8.8.8",),
        )
        sentinel_socket = object()
        with patch(
            "urllib3.connection.connection.create_connection",
            return_value=sentinel_socket,
        ) as create_connection:
            created = connection._new_conn()

        self.assertIs(created, sentinel_socket)
        self.assertEqual(create_connection.call_args.args[0], ("8.8.8.8", 443))
        self.assertEqual(connection.host, "example.test")

    def test_declared_size_over_ten_mib_is_rejected_before_streaming(self):
        session = Mock()
        response = image_response()
        response.headers["Content-Length"] = str(10 * 1024 * 1024 + 1)
        session.get.return_value = response
        workspace = image_workspace(session)
        try:
            with self.assertRaises(ImageDownloadError):
                workspace.download("https://example.test/image")
            response.iter_content.assert_not_called()
            self.assertEqual(list(workspace.path.iterdir()), [])
        finally:
            workspace.close()

    def test_streamed_size_over_ten_mib_removes_partial_file(self):
        session = Mock()
        response = image_response(body=b"")
        response.headers.pop("Content-Length")
        response.iter_content.return_value = [
            b"x" * (10 * 1024 * 1024),
            b"x",
        ]
        session.get.return_value = response
        workspace = image_workspace(session)
        try:
            with self.assertRaises(ImageDownloadError):
                workspace.download("https://example.test/image")
            self.assertEqual(list(workspace.path.iterdir()), [])
        finally:
            workspace.close()

    def test_stream_exception_removes_partial_file_and_is_redacted(self):
        session = Mock()
        response = image_response(body=b"")
        response.headers.pop("Content-Length")

        def broken_stream(chunk_size):
            yield b"partial"
            raise OSError("credential=do-not-leak")

        response.iter_content.side_effect = broken_stream
        session.get.return_value = response
        workspace = image_workspace(session)
        try:
            with self.assertRaises(ImageDownloadError) as raised:
                workspace.download("https://example.test/image")
            self.assertEqual(list(workspace.path.iterdir()), [])
            self.assertNotIn("do-not-leak", str(raised.exception))
        finally:
            workspace.close()

    def test_close_is_idempotent_and_prevents_future_network(self):
        session = Mock()
        workspace = image_workspace(session)
        path = workspace.path

        workspace.close()
        workspace.close()

        self.assertFalse(path.exists())
        with self.assertRaises(ImageDownloadError):
            workspace.download("https://example.test/image")
        session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
