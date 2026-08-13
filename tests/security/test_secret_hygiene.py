from __future__ import annotations

import asyncio
import ast
import io
import os
import re
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.security.redaction import redact_text, redact_url
from services.dingtalk_mcp import DingTalkMCPError, DingTalkMCPService
from services.dingtalk_spreadsheet import DingTalkSpreadSheetMCPService


class RedactionTests(unittest.TestCase):
    def test_redacts_url_userinfo_path_query_fragment_and_is_idempotent(self):
        raw = (
            "https://user:password@example.test/private/node"
            "?key=query-secret#fragment-secret"
        )
        redacted = redact_url(raw)
        for secret in (
            "user",
            "password",
            "private",
            "node",
            "query-secret",
            "fragment-secret",
        ):
            self.assertNotIn(secret, redacted)
        self.assertEqual(redact_url(redacted), redacted)

    def test_redacts_json_quoted_assignments_bearer_and_secret_keys(self):
        fake_openai_token = "sk-" + "abcdefghijklmnopqrstuvwx"
        raw = " ".join(
            (
                '\"api_key\": \"json-double-secret\"',
                "'token': 'json-single-secret'",
                'access_token = "access-secret"',
                "password: password-secret",
                "secret='generic-secret'",
                "Authorization: Bearer bearer-secret",
                "KEY=uppercase-secret",
                fake_openai_token,
            )
        )
        redacted = redact_text(raw)
        for secret in (
            "json-double-secret",
            "json-single-secret",
            "access-secret",
            "password-secret",
            "generic-secret",
            "bearer-secret",
            "uppercase-secret",
            fake_openai_token,
        ):
            self.assertNotIn(secret, redacted)
        self.assertEqual(redact_text(redacted), redacted)

    def test_redacts_escaped_quotes_without_leaking_assignment_tail(self):
        raw = " ".join(
            (
                r'{"api_key":"prefix\"DOUBLE_TAIL_SECRET"}',
                r"{'password':'prefix\'SINGLE_TAIL_SECRET'}",
                r'token="prefix\\path\"TOKEN_TAIL_SECRET"',
            )
        )
        redacted = redact_text(raw)
        for secret in (
            "DOUBLE_TAIL_SECRET",
            "SINGLE_TAIL_SECRET",
            "TOKEN_TAIL_SECRET",
        ):
            self.assertNotIn(secret, redacted)
        self.assertEqual(redact_text(redacted), redacted)

    def test_unterminated_quoted_assignment_fails_closed_for_full_value(self):
        redacted = redact_text(
            'api_key="prefix UNTERMINATED_TAIL_SECRET still-secret'
        )
        self.assertNotIn("UNTERMINATED_TAIL_SECRET", redacted)
        self.assertNotIn("still-secret", redacted)

    def test_redaction_is_bounded_and_does_not_leak_tail_secret(self):
        raw = "x" * 200_000 + ' \"api_key\": \"tail-never-show\"'
        redacted = redact_text(raw)
        self.assertLessEqual(len(redacted), 65_600)
        self.assertNotIn("tail-never-show", redacted)

    def test_oversized_cross_boundary_secret_fails_closed(self):
        raw = "x" * (65_536 - 4) + "api_key=cross-boundary-secret"
        redacted = redact_text(raw)
        self.assertNotIn("cross-boundary-secret", redacted)
        self.assertNotIn("api_", redacted)
        self.assertLessEqual(len(redacted), 65_536)

    def test_log_redaction_neutralizes_control_characters(self):
        from backend.security.redaction import redact_log_text

        redacted = redact_log_text(
            "ok\r\nevent: hacked\nid: injected\x00\x7f\x85 token=log-secret"
        )
        self.assertNotIn("\r", redacted)
        self.assertNotIn("\n", redacted)
        self.assertNotIn("\x00", redacted)
        self.assertNotIn("\x7f", redacted)
        self.assertNotIn("\x85", redacted)
        self.assertNotIn("log-secret", redacted)

    def test_control_characters_cannot_split_secret_assignment_name(self):
        for code in (0, 1, 7, 27, 127, 133, 159):
            with self.subTest(code=code):
                values = (
                    f"api_key{chr(code)}=CONTROL_BYPASS_SECRET",
                    f"api_key=CONTROL{chr(code)}_BYPASS_SECRET",
                    f"Authorization: Bearer{chr(code)}BEARER_BYPASS_SECRET",
                )
                for value in values:
                    redacted = redact_text(value)
                    self.assertNotIn("BYPASS_SECRET", redacted)


class MCPURLAndTransportTests(unittest.TestCase):
    SERVICE_TYPES = (DingTalkMCPService, DingTalkSpreadSheetMCPService)

    def test_rejects_unsafe_mcp_urls_before_transport(self):
        invalid_urls = (
            "http://example.test/mcp?key=value",
            "https:///missing-host",
            "https://user:pass@example.test/mcp",
            "https://user%40name:pass%3Aword@example.test/mcp",
            "https://example.test\\mcp",
            "https://example.test/mcp\r\nInjected: true",
            "https://example.test:99999/mcp",
            "https://example.test/mcp#fragment",
            "https://example.test/" + "x" * 9000,
            "\thttps://example.test/mcp",
            "https://example.test/mcp\x85",
            *(f"https://example.test/mcp{chr(code)}tail" for code in (0, 1, 7, 27)),
            *(f"https://example.test/mcp%{code:02x}tail" for code in (0, 1, 27, 127, 133, 159)),
        )
        for service_type in self.SERVICE_TYPES:
            for value in invalid_urls:
                with self.subTest(service=service_type.__name__, value=value[:40]):
                    with patch("requests.post") as post:
                        with self.assertRaises(DingTalkMCPError) as raised:
                            service_type(value)
                    post.assert_not_called()
                    self.assertNotIn(value, str(raised.exception))

    def test_transport_keeps_tls_disables_redirects_and_uses_system_proxy(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"result": {"tools": []}}
        for service_type in self.SERVICE_TYPES:
            with self.subTest(service=service_type.__name__):
                with patch("requests.post", return_value=response) as post:
                    service_type(
                        "https://[2001:db8::1]:8443/mcp?key=credential"
                    ).list_tools()
                kwargs = post.call_args.kwargs
                self.assertTrue(kwargs["verify"])
                self.assertFalse(kwargs["allow_redirects"])
                self.assertNotIn("proxies", kwargs)
                timeout = kwargs["timeout"]
                self.assertEqual(timeout.total, 35.0)
                self.assertEqual(timeout.connect_timeout, 5.0)
                self.assertEqual(timeout.read_timeout, 30.0)

    def test_transport_exception_chain_contains_no_url_or_secret(self):
        url = "https://example.test/private?key=transport-chain-secret"
        raw_error = RuntimeError(f"request failed for {url}")
        for service_type in self.SERVICE_TYPES:
            with self.subTest(service=service_type.__name__):
                with patch("requests.post", side_effect=raw_error):
                    try:
                        service_type(url).list_tools()
                    except DingTalkMCPError as exc:
                        rendered = "".join(
                            traceback.format_exception(type(exc), exc, exc.__traceback__)
                        )
                        cause = exc.__cause__
                        suppressed = exc.__suppress_context__
                    else:
                        self.fail("expected DingTalkMCPError")
                self.assertNotIn("transport-chain-secret", rendered)
                self.assertNotIn("/private", rendered)
                self.assertIsNone(cause)
                self.assertTrue(suppressed)


class SSEHardeningTests(unittest.TestCase):
    def test_sse_event_is_single_bounded_logical_event(self):
        from backend.api import routes

        payload = (
            "ok\r\nevent: hacked\nid: injected\n\ndata: evil "
            "token=sse-secret "
            + "x" * 200_000
        )
        event = routes._sse_data(payload)
        self.assertTrue(event.startswith("data: "))
        self.assertTrue(event.endswith("\n\n"))
        self.assertEqual(event.count("\n\n"), 1)
        self.assertNotIn("\nevent:", event)
        self.assertNotIn("\nid:", event)
        self.assertNotIn("sse-secret", event)
        self.assertLessEqual(len(event), 65_620)

    def test_streamed_task_logs_and_errors_use_safe_sse_encoding(self):
        from backend.api import routes

        task_id = "security-sse"
        task = routes.GenerationTask(
            task_id=task_id,
            snapshot=None,
            provider_decision=None,
        )
        task.logs = ["line\r\nevent: hacked token=stream-secret"]
        task.status = "failed"
        task.error = "bad\nid: forged api_key=error-secret"
        with routes._tasks_lock:
            routes._generation_tasks[task_id] = task
        try:
            response = asyncio.run(routes.stream_logs(task_id))

            async def collect():
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                return chunks

            chunks = asyncio.run(collect())
        finally:
            with routes._tasks_lock:
                routes._generation_tasks.pop(task_id, None)
        combined = "".join(
            chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            for chunk in chunks
        )
        self.assertNotIn("stream-secret", combined)
        self.assertNotIn("error-secret", combined)
        self.assertNotIn("\nevent:", combined)
        self.assertNotIn("\nid:", combined)


class MiniMaxExceptionTests(unittest.TestCase):
    def test_minimax_transport_is_tls_safe_and_exception_chain_has_no_auth(self):
        from services.minimax import MiniMaxService, MiniMaxServiceError

        session = Mock()
        session.post.side_effect = RuntimeError(
            "Authorization: Bearer minimax-chain-secret"
        )
        service = MiniMaxService(
            api_key="minimax-chain-secret",
            base_url="https://api.example.test",
            model="model",
            timeout_seconds=5,
            session=session,
        )
        try:
            service.chat([{"role": "user", "content": "hello"}])
        except MiniMaxServiceError as exc:
            rendered = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            cause = exc.__cause__
            suppressed = exc.__suppress_context__
        else:
            self.fail("expected MiniMaxServiceError")
        kwargs = session.post.call_args.kwargs
        self.assertTrue(kwargs["verify"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("minimax-chain-secret", rendered)
        self.assertIsNone(cause)
        self.assertTrue(suppressed)


class SettingsBoundaryTests(unittest.TestCase):
    def test_settings_save_and_diagnostic_share_strict_mcp_validation(self):
        from backend.api.settings_routes import (
            _normalized_secret,
            _require_https_secret,
        )
        from backend.settings.service import SettingsValidationError

        invalid = "https://user:save-secret@example.test/mcp?key=query-secret"
        with self.assertRaises(SettingsValidationError) as saved:
            _normalized_secret(invalid, clear=False, require_https=True)
        with self.assertRaises(DingTalkMCPError) as diagnosed:
            _require_https_secret(invalid)
        for error in (saved.exception, diagnosed.exception):
            rendered = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            self.assertNotIn("save-secret", rendered)
            self.assertNotIn("query-secret", rendered)
            self.assertIsNone(error.__cause__)

        control_prefixed = "\thttps://example.test/mcp?key=value"
        with self.assertRaises(SettingsValidationError):
            _normalized_secret(
                control_prefixed,
                clear=False,
                require_https=True,
            )

    def test_valid_mcp_query_credential_is_preserved_for_transport(self):
        from backend.api.settings_routes import _normalized_secret

        value = "HTTPS://[2001:db8::2]:443/mcp?key=allowed-query-value"
        normalized = _normalized_secret(
            value,
            clear=False,
            require_https=True,
        )
        self.assertEqual(
            normalized,
            "https://[2001:db8::2]:443/mcp?key=allowed-query-value",
        )

    def test_sensitive_settings_persistence_failure_is_safely_detached(self):
        from backend.settings.defaults import default_settings
        from backend.settings.secrets import MemorySecretStore
        from backend.settings.service import SettingsService
        from backend.settings.store import SettingsRepository

        with tempfile.TemporaryDirectory() as directory:
            service = SettingsService(
                SettingsRepository(Path(directory) / "settings.json"),
                MemorySecretStore(),
                environment={},
            )
            secret = "persistence-never-show"
            with patch.object(
                service.repository,
                "update",
                side_effect=RuntimeError(f"api_key={secret}"),
            ):
                try:
                    service.update_group("prompts", default_settings().prompts)
                except Exception as exc:
                    rendered = "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    )
                    cause = exc.__cause__
                    suppressed = exc.__suppress_context__
                else:
                    self.fail("expected a safe persistence failure")
        self.assertNotIn(secret, rendered)
        self.assertIsNone(cause)
        self.assertTrue(suppressed)

    def test_sensitive_keyring_read_failure_is_safely_detached(self):
        from backend.settings.service import SettingsService
        from backend.settings.store import SettingsRepository

        class FailingSecretStore:
            def get(self, _name):
                raise RuntimeError("api_key=keyring-never-show")

            def set(self, _name, _value):
                raise AssertionError("not used")

            def delete(self, _name):
                raise AssertionError("not used")

        with tempfile.TemporaryDirectory() as directory:
            service = SettingsService(
                SettingsRepository(Path(directory) / "settings.json"),
                FailingSecretStore(),
                environment={},
            )
            try:
                service.secret_status("minimax_api_key")
            except Exception as exc:
                rendered = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )
                cause = exc.__cause__
                suppressed = exc.__suppress_context__
            else:
                self.fail("expected safe keyring failure")
        self.assertNotIn("keyring-never-show", rendered)
        self.assertIsNone(cause)
        self.assertTrue(suppressed)

    def test_sensitive_rollback_failure_detaches_original_exception(self):
        from backend.settings.secrets import MemorySecretStore
        from backend.settings.service import SettingsService
        from backend.settings.store import SettingsRepository

        class RollbackFailureStore(MemorySecretStore):
            def __init__(self):
                super().__init__()
                self.values["minimax_api_key"] = "old-value"
                self.rollback = False

            def set(self, name, value):
                if self.rollback and value == "old-value":
                    raise RuntimeError("token=rollback-never-show")
                super().set(name, value)

        with tempfile.TemporaryDirectory() as directory:
            store = RollbackFailureStore()
            service = SettingsService(
                SettingsRepository(Path(directory) / "settings.json"),
                store,
                environment={},
            )

            def fail_update(_mutator):
                store.rollback = True
                raise RuntimeError("api_key=original-never-show")

            with patch.object(service.repository, "update", side_effect=fail_update):
                try:
                    service.update_group(
                        "ai",
                        service.load().ai,
                        secret_updates={"minimax_api_key": "new-value"},
                    )
                except Exception as exc:
                    rendered = "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    )
                    cause = exc.__cause__
                    suppressed = exc.__suppress_context__
                else:
                    self.fail("expected safe rollback failure")
        self.assertNotIn("original-never-show", rendered)
        self.assertNotIn("rollback-never-show", rendered)
        self.assertIsNone(cause)
        self.assertTrue(suppressed)


class MigrationSafetyTests(unittest.TestCase):
    def test_migration_requires_explicit_confirmation_before_any_secret_access(self):
        from scripts import migrate_legacy_secrets as migration

        output = io.StringIO()
        store_factory = Mock(side_effect=AssertionError("store must not be opened"))
        with patch.object(migration, "KeyringSecretStore", store_factory), patch.dict(
            os.environ,
            {"MINIMAX_API_KEY": "migration-never-show"},
            clear=False,
        ), patch("sys.stdout", output):
            status = migration.main([])
        self.assertNotEqual(status, 0)
        store_factory.assert_not_called()
        self.assertNotIn("migration-never-show", output.getvalue())

    def test_confirmed_migration_contains_factory_get_and_set_failures(self):
        from scripts import migrate_legacy_secrets as migration

        sentinel = "migration-trace-never-show"
        for stage in ("factory", "get", "set"):
            with self.subTest(stage=stage):
                output = io.StringIO()
                errors = io.StringIO()
                store = Mock()
                if stage == "get":
                    store.get.side_effect = RuntimeError(
                        f"api_key={sentinel}"
                    )
                else:
                    store.get.return_value = None
                if stage == "set":
                    store.set.side_effect = RuntimeError(
                        f"api_key={sentinel}"
                    )
                factory = Mock(return_value=store)
                if stage == "factory":
                    factory.side_effect = RuntimeError(
                        f"api_key={sentinel}"
                    )
                with patch.object(
                    migration,
                    "KeyringSecretStore",
                    factory,
                ), patch.dict(
                    os.environ,
                    {
                        "DINGTALK_MCP_URL": "https://example.test/mcp?key=value",
                        "DINGTALK_SPREADSHEET_MCP_URL": "https://example.test/sheets?key=value",
                        "MINIMAX_API_KEY": "synthetic-value",
                    },
                    clear=False,
                ), patch("sys.stdout", output), patch("sys.stderr", errors):
                    try:
                        status = migration.main(["--confirm"])
                    except Exception as exc:
                        rendered = "".join(
                            traceback.format_exception(
                                type(exc), exc, exc.__traceback__
                            )
                        )
                        self.fail(f"migration raised an exception: {rendered}")
                self.assertNotEqual(status, 0)
                combined = output.getvalue() + errors.getvalue()
                self.assertNotIn(sentinel, combined)
                self.assertNotIn("Traceback", combined)


class RuntimeSourceHygieneTests(unittest.TestCase):
    SOURCE_ROOTS = (
        Path("backend"),
        Path("frontend"),
        Path("services"),
        Path("utils"),
        Path("scripts"),
        Path("config.py"),
        Path("start.py"),
    )
    SECRET_PATTERNS = (
        ("secret-prefix", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
        (
            "credential-query",
            re.compile(r"[?&](?:key|token|api_key)=[A-Za-z0-9_-]{20,}", re.I),
        ),
        (
            "dingtalk-gateway-credential",
            re.compile(r"mcp-gw\.dingtalk\.com/[^\s\"']+\?key=", re.I),
        ),
    )

    @classmethod
    def _paths(cls):
        for root in cls.SOURCE_ROOTS:
            if root.is_file():
                yield root
            elif root.is_dir():
                yield from sorted(root.rglob("*.py"))

    def test_runtime_source_has_no_plaintext_credential_patterns(self):
        violations = []
        for path in self._paths():
            text = path.read_text(encoding="utf-8", errors="replace")
            if "0.0.0.0" in text:
                violations.append(f"{path}:0:wildcard-bind")
            for kind, pattern in self.SECRET_PATTERNS:
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    violations.append(f"{path}:{line}:{kind}")
        self.assertEqual(violations, [])

    def test_runtime_ast_has_no_disabled_tls_proxy_override_or_shell(self):
        violations = []
        for path in self._paths():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                violations.append(f"{path}:0:invalid-python")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg == "proxies":
                        violations.append(f"{path}:{node.lineno}:proxy-override")
                    if (
                        keyword.arg in {"verify", "shell"}
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is False
                        and keyword.arg == "verify"
                    ):
                        violations.append(f"{path}:{node.lineno}:disabled-tls")
                    if (
                        keyword.arg == "shell"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True
                    ):
                        violations.append(f"{path}:{node.lineno}:shell-true")
        self.assertEqual(violations, [])

    def test_legacy_config_is_not_active_and_optional_archive_is_safe(self):
        self.assertFalse(Path("config.py").exists())
        self.assertIn("预删除/", Path(".gitignore").read_text(encoding="utf-8"))
        archived = Path("预删除/web_legacy/config.py")
        # 公开克隆不会包含受忽略的本机恢复副本；开发工作区中存在时仍校验其安全边界。
        if not archived.is_file():
            return
        tree = ast.parse(archived.read_text(encoding="utf-8"))
        assigned = {
            target.id
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else (node.target,)
            )
            if isinstance(target, ast.Name)
        }
        self.assertEqual(
            assigned,
            {
                "MINIMAX_API_KEY",
                "MINIMAX_API_HOST",
                "DINGTALK_MCP_URL",
                "DINGTALK_SPREADSHEET_MCP_URL",
                "OUTPUT_DIR",
            },
        )
        source = archived.read_text(encoding="utf-8")
        self.assertNotIn("TEST_CASE_CONTENT_TEMPLATE_URL", source)
        self.assertNotIn("TEST_CASE_DOC_TEMPLATE_URL", source)
        self.assertNotIn("OUTPUT_FOLDER_URL", source)


if __name__ == "__main__":
    unittest.main()
