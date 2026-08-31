import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.settings.service as settings_service
from backend.security import redact_text, redact_url
from backend.settings.defaults import DEFAULT_PROMPTS, default_settings
from backend.settings.models import ProviderName
from backend.settings.prompt_library import legacy_option_id
from backend.settings.secrets import MemorySecretStore, mask_secret
from backend.settings.service import SettingsService, SettingsValidationError
from backend.settings.store import SettingsRepository


class SecretAndStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "settings.json"
        self.secrets = MemorySecretStore()
        self.service = SettingsService(
            SettingsRepository(self.path),
            self.secrets,
            environment={},
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_secret_is_masked_and_plaintext_never_enters_json(self):
        self.service.repository.save(default_settings())
        self.service.update_provider_secret("minimax", "secret-value-123456")
        status = self.service.secret_status("minimax_api_key")
        self.assertTrue(status.configured)
        self.assertNotIn("secret-value", status.masked_value)
        self.assertNotIn("secret-value", self.path.read_text(encoding="utf-8"))

    def test_first_load_creates_nested_lock_directory(self):
        nested = Path(self.temp.name) / "new" / "nested" / "settings.json"
        repository = SettingsRepository(nested)
        self.assertEqual(repository.load(), default_settings())
        self.assertTrue(nested.parent.is_dir())

    def test_snapshot_is_immutable_and_keeps_resolved_secrets(self):
        self.secrets.set("document_mcp_url", "old-document-secret")
        before = self.service.snapshot()
        changed = default_settings().document.model_copy(
            update={"local_output_dir": "./changed"}
        )
        self.service.update_group(
            "document",
            changed,
            secret_updates={"document_mcp_url": "new-document-secret"},
        )
        self.assertNotEqual(
            before.settings.document.local_output_dir,
            "./changed",
        )
        self.assertEqual(
            before.secrets.reveal("document_mcp_url"),
            "old-document-secret",
        )
        self.assertNotIn("old-document-secret", repr(before))
        self.assertNotIn("old-document-secret", before.model_dump_json())

    def test_snapshot_resolves_legacy_relative_output_under_user_data_root(self):
        data_root = Path(self.temp.name) / "UserData"
        repository = SettingsRepository(data_root / "data" / "settings.json")
        service = SettingsService(repository, self.secrets, environment={})
        settings = default_settings().model_copy(
            update={
                "document": default_settings().document.model_copy(
                    update={"local_output_dir": "output"}
                )
            }
        )
        repository.save(settings)

        snapshot = service.snapshot()

        self.assertEqual(
            Path(snapshot.settings.document.local_output_dir),
            (data_root / "output").resolve(),
        )
        self.assertEqual(service.load().document.local_output_dir, "output")

    def test_failed_replace_keeps_last_valid_json(self):
        self.service.repository.save(default_settings())
        original = json.loads(self.path.read_text(encoding="utf-8"))
        changed = default_settings().model_copy(
            update={
                "document": default_settings().document.model_copy(
                    update={"local_output_dir": "./changed"}
                )
            }
        )
        with patch("backend.settings.store.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.service.repository.save(changed)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), original)
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_v0_settings_migrate_and_future_settings_are_rejected(self):
        payload = default_settings().model_dump()
        payload.pop("schema_version")
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.service.load().schema_version, 5)
        payload["schema_version"] = 6
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "未来设置版本"):
            self.service.load()

    def test_v5_json_excludes_runtime_prompt_projection_and_default_bodies(self):
        self.service.repository.save(default_settings())

        raw_text = self.path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)

        self.assertEqual(raw["schema_version"], 5)
        self.assertNotIn("prompts", raw)
        self.assertIn("prompt_library", raw)
        for default_content in DEFAULT_PROMPTS.values():
            self.assertNotIn(default_content, raw_text)
        self.assertEqual(
            self.service.load().prompts.model_dump(),
            DEFAULT_PROMPTS,
        )

    def test_v4_web_port_settings_are_removed_during_native_migration(self):
        """旧端口字段只用于 Web 启动，升级原生配置时应安全丢弃。"""

        payload = default_settings().model_dump(mode="json")
        payload["schema_version"] = 4
        payload["system"] = {
            "api_host": "127.0.0.1",
            "api_port": 9100,
            "frontend_port": 9200,
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        migrated = self.service.load()
        self.service.repository.save(migrated)
        saved = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(migrated.schema_version, 5)
        self.assertFalse(hasattr(migrated, "system"))
        self.assertNotIn("system", saved)

    def test_v1_migration_selects_defaults_and_creates_stable_legacy_custom(self):
        payload = default_settings().model_dump(mode="json")
        payload.pop("prompt_library")
        payload["schema_version"] = 1
        payload["prompts"]["component_matching"] = "legacy custom content"
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        first = self.service.load()
        second = self.service.load()
        default_slot = first.prompt_library.image_understanding
        custom_slot = first.prompt_library.component_matching

        self.assertEqual(default_slot.selected_option_id, "default")
        self.assertEqual(default_slot.custom_options, ())
        self.assertEqual(
            custom_slot.selected_option_id,
            legacy_option_id("component_matching"),
        )
        self.assertEqual(len(custom_slot.custom_options), 1)
        self.assertEqual(
            custom_slot.custom_options[0].content,
            "legacy custom content",
        )
        self.assertEqual(
            custom_slot.selected_option_id,
            second.prompt_library.component_matching.selected_option_id,
        )
        self.assertEqual(
            first.prompts.component_matching,
            "legacy custom content",
        )

    def test_v1_blank_image_prompt_migrates_to_safe_default(self):
        payload = default_settings().model_dump(mode="json")
        payload.pop("prompt_library")
        payload["schema_version"] = 1
        payload["prompts"]["image_understanding"] = "  \n "
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        migrated = self.service.load()
        slot = migrated.prompt_library.image_understanding

        self.assertEqual(migrated.schema_version, 5)
        self.assertEqual(slot.selected_option_id, "default")
        self.assertEqual(slot.custom_options, ())
        self.assertEqual(
            migrated.prompts.image_understanding,
            DEFAULT_PROMPTS["image_understanding"],
        )

    def test_two_repository_instances_do_not_lose_group_updates(self):
        self.service.repository.save(default_settings())
        first = SettingsService(
            SettingsRepository(self.path), self.secrets, environment={}
        )
        second = SettingsService(
            SettingsRepository(self.path), self.secrets, environment={}
        )
        barrier = threading.Barrier(2)
        errors = []

        def update_document():
            try:
                barrier.wait()
                first.update_document(
                    first.load().document.model_copy(
                        update={"local_output_dir": "./changed"}
                    )
                )
            except Exception as exc:
                errors.append(exc)

        def update_ai():
            try:
                barrier.wait()
                ai = second.load().ai.model_copy(
                    update={
                        "active_provider": ProviderName.minimax,
                        "fallback_enabled": False,
                    }
                )
                second.update_group("ai", ai)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=update_document),
            threading.Thread(target=update_ai),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(self.service.load().document.local_output_dir, "changed")
        self.assertEqual(self.service.load().ai.active_provider, ProviderName.minimax)

    def test_snapshot_cannot_mix_old_settings_with_new_secrets(self):
        self.secrets.set("minimax_api_key", "old-provider-secret")
        updating_service = SettingsService(
            SettingsRepository(self.path),
            self.secrets,
            environment={},
        )
        load_entered = threading.Event()
        release_load = threading.Event()
        update_started = threading.Event()
        snapshots = []
        errors = []
        original_load = self.service.repository.load

        def blocked_load():
            value = original_load()
            load_entered.set()
            if not release_load.wait(timeout=5):
                raise TimeoutError("test barrier timed out")
            return value

        def take_snapshot():
            try:
                snapshots.append(self.service.snapshot())
            except Exception as exc:
                errors.append(exc)

        def update_provider():
            update_started.set()
            try:
                ai = updating_service.load().ai.model_copy(
                    update={"active_provider": ProviderName.minimax}
                )
                updating_service.update_group(
                    "ai",
                    ai,
                    secret_updates={"minimax_api_key": "new-provider-secret"},
                )
            except Exception as exc:
                errors.append(exc)

        with patch.object(
            self.service.repository,
            "load",
            side_effect=blocked_load,
        ):
            snapshot_thread = threading.Thread(target=take_snapshot)
            snapshot_thread.start()
            self.assertTrue(load_entered.wait(timeout=5))
            update_thread = threading.Thread(target=update_provider)
            update_thread.start()
            self.assertTrue(update_started.wait(timeout=5))
            release_load.set()
            snapshot_thread.join(timeout=5)
            update_thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(
            snapshots[0].settings.ai.active_provider,
            ProviderName.codex,
        )
        self.assertEqual(
            snapshots[0].secrets.reveal("minimax_api_key"),
            "old-provider-secret",
        )
        self.assertEqual(self.service.get_secret("minimax_api_key")[0], "new-provider-secret")

    def test_secret_keep_clear_precedence_mask_rejection_and_rollback(self):
        self.secrets.set("minimax_api_key", "saved-secret")
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "environment-secret"}):
            environment_service = SettingsService(
                self.service.repository,
                self.secrets,
                environment=os.environ,
            )
            self.assertEqual(
                environment_service.get_secret("minimax_api_key"),
                ("saved-secret", "saved"),
            )
            environment_service.update_group(
                "ai",
                environment_service.load().ai,
                secret_updates={"minimax_api_key": " "},
            )
            self.assertEqual(self.secrets.get("minimax_api_key"), "saved-secret")
            environment_service.update_group(
                "ai",
                environment_service.load().ai,
                clear_secrets={"minimax_api_key"},
            )
            self.assertEqual(
                environment_service.get_secret("minimax_api_key"),
                ("environment-secret", "environment"),
            )

        with self.assertRaisesRegex(ValueError, "掩码"):
            self.service.update_provider_secret("minimax", "abc••••••wxyz")

        self.secrets.set("document_mcp_url", "old-secret")
        with patch.object(
            self.service.repository,
            "update",
            side_effect=OSError("disk"),
        ):
            with self.assertRaises(OSError):
                self.service.update_group(
                    "document",
                    self.service.load().document,
                    secret_updates={"document_mcp_url": "new-secret"},
                )
        self.assertEqual(self.secrets.get("document_mcp_url"), "old-secret")

    def test_bootstrap_secret_is_kept_in_memory_then_removed_from_child_env(self):
        environment = {
            "MINIMAX_API_KEY": "bootstrap-secret",
            "PATH": "bin",
        }
        service = SettingsService(
            self.service.repository,
            MemorySecretStore(),
            environment=environment,
        )
        service.scrub_bootstrap_environment(environment)
        self.assertNotIn("MINIMAX_API_KEY", environment)
        self.assertEqual(environment["PATH"], "bin")
        self.assertEqual(
            service.get_secret("minimax_api_key"),
            ("bootstrap-secret", "environment"),
        )

    def test_mask_is_non_reversible(self):
        self.assertEqual(mask_secret("abcdefghijk"), "abc••••••hijk")

    def test_update_prompts_and_ai_delegate_to_group_updates(self):
        prompts = self.service.load().prompts.model_copy(
            update={"image_understanding": "updated prompt"}
        )
        ai = self.service.load().ai.model_copy(
            update={"active_provider": ProviderName.minimax}
        )

        self.service.update_prompts(prompts)
        self.service.update_ai(ai)

        saved = self.service.load()
        self.assertEqual(saved.prompts.image_understanding, "updated prompt")
        self.assertEqual(saved.ai.active_provider, ProviderName.minimax)

    def test_prompt_option_create_update_select_default_and_delete(self):
        created, option_id = self.service.save_prompt_option(
            "component_matching",
            option_id=None,
            name="  自定义一  ",
            content="{requirement} {component_names} custom",
        )
        slot = created.prompt_library.component_matching
        self.assertEqual(slot.selected_option_id, option_id)
        self.assertEqual(slot.custom_options[0].name, "自定义一")
        self.assertEqual(
            created.prompts.component_matching,
            "{requirement} {component_names} custom",
        )

        updated, returned_id = self.service.save_prompt_option(
            "component_matching",
            option_id=option_id,
            name="改名后",
            content="{requirement}\n{component_names}\nupdated",
        )
        self.assertEqual(returned_id, option_id)
        self.assertEqual(
            updated.prompt_library.component_matching.custom_options[0].name,
            "改名后",
        )
        with self.assertRaisesRegex(SettingsValidationError, "当前选中"):
            self.service.delete_prompt_option(
                "component_matching",
                option_id,
            )

        selected_default, selected_id = self.service.save_prompt_option(
            "component_matching",
            option_id="default",
        )
        self.assertEqual(selected_id, "default")
        self.assertEqual(
            selected_default.prompts.component_matching,
            DEFAULT_PROMPTS["component_matching"],
        )
        after_delete = self.service.delete_prompt_option(
            "component_matching",
            option_id,
        )
        self.assertEqual(
            after_delete.prompt_library.component_matching.custom_options,
            (),
        )
        with self.assertRaisesRegex(SettingsValidationError, "默认提示词"):
            self.service.delete_prompt_option(
                "component_matching",
                "default",
            )

    def test_prompt_option_names_are_unique_after_trimming(self):
        _, first_id = self.service.save_prompt_option(
            "image_understanding",
            option_id=None,
            name="名称",
            content="first",
        )
        self.service.save_prompt_option(
            "image_understanding",
            option_id="default",
        )

        with self.assertRaisesRegex(SettingsValidationError, "名称不能重复"):
            self.service.save_prompt_option(
                "image_understanding",
                option_id=None,
                name="  名称  ",
                content="second",
            )

        with self.assertRaisesRegex(SettingsValidationError, "名称不能重复"):
            self.service.save_prompt_option(
                "image_understanding",
                option_id=None,
                name="名称",
                content="third",
            )
        self.assertEqual(
            self.service.load()
            .prompt_library.image_understanding.custom_options[0]
            .id,
            first_id,
        )

    def test_prompt_option_updates_from_two_services_are_atomic(self):
        first = SettingsService(
            SettingsRepository(self.path), self.secrets, environment={}
        )
        second = SettingsService(
            SettingsRepository(self.path), self.secrets, environment={}
        )
        barrier = threading.Barrier(2)
        errors = []

        def create(service, name):
            try:
                barrier.wait()
                service.save_prompt_option(
                    "case_generation_user",
                    option_id=None,
                    name=name,
                    content=f"{name} content",
                )
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=create, args=(first, "并发一")),
            threading.Thread(target=create, args=(second, "并发二")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        names = {
            option.name
            for option in self.service.load()
            .prompt_library.case_generation_user.custom_options
        }
        self.assertEqual(names, {"并发一", "并发二"})

    def test_legacy_prompt_update_reuses_selected_option_and_persists_no_projection(self):
        prompts = self.service.load().prompts.model_copy(
            update={"image_understanding": "legacy first"}
        )
        first = self.service.update_prompts(prompts)
        option_id = first.prompt_library.image_understanding.selected_option_id

        second_prompts = first.prompts.model_copy(
            update={"image_understanding": "legacy second"}
        )
        second = self.service.update_group("prompts", second_prompts)

        self.assertEqual(
            second.prompt_library.image_understanding.selected_option_id,
            option_id,
        )
        self.assertEqual(
            len(second.prompt_library.image_understanding.custom_options),
            1,
        )
        self.assertEqual(second.prompts.image_understanding, "legacy second")
        self.assertNotIn(
            "prompts",
            json.loads(self.path.read_text(encoding="utf-8")),
        )

    def test_clear_secret_reveals_captured_environment_fallback(self):
        secrets = MemorySecretStore()
        secrets.set("minimax_api_key", "saved-secret")
        service = SettingsService(
            self.service.repository,
            secrets,
            environment={"MINIMAX_API_KEY": "environment-secret"},
        )

        service.clear_secret("minimax_api_key")

        self.assertIsNone(secrets.get("minimax_api_key"))
        self.assertEqual(
            service.get_secret("minimax_api_key"),
            ("environment-secret", "environment"),
        )
        with self.assertRaises(KeyError):
            service.clear_secret("unknown")

    def test_redaction_removes_url_query_and_common_secret_forms(self):
        url = "https://example.test/mcp?access_token=url-secret#fragment-secret"
        redacted_url = redact_url(url)
        redacted_text = redact_text(
            f"request={url} api_key=plain-secret sk-Abcdefghijklmnop"
        )

        self.assertEqual(
            redacted_url,
            "https://example.test/<redacted>?<redacted>",
        )
        for plaintext in (
            "url-secret",
            "fragment-secret",
            "plain-secret",
            "sk-Abcdefghijklmnop",
        ):
            self.assertNotIn(plaintext, redacted_text)

    def test_redaction_drops_userinfo_and_masks_non_root_paths(self):
        cases = {
            "https://user:password@example.test/path-token?query-secret#fragment": (
                "https://example.test/<redacted>?<redacted>"
            ),
            "https://user%40mail:pass%2Fword@example.test:8443/path-token": (
                "https://example.test:8443/<redacted>"
            ),
            "https://example.test/": "https://example.test/",
        }

        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(redact_url(value), expected)
                redacted = redact_text(f"request={value}")
                for plaintext in (
                    "user",
                    "password",
                    "user%40mail",
                    "pass%2Fword",
                    "path-token",
                    "query-secret",
                    "fragment",
                ):
                    self.assertNotIn(plaintext, redacted)

    def test_redaction_returns_safe_fallback_for_malformed_urls(self):
        malformed = (
            "https:///path-token",
            "https://example.test:99999/path-token",
            "https://example.test:not-a-port/path-token",
            "https://[broken/path-token",
        )

        for value in malformed:
            with self.subTest(value=value):
                self.assertEqual(redact_url(value), "<redacted>")
                self.assertEqual(
                    redact_text(f"request failed: {value}"),
                    "request failed: <redacted>",
                )

    def test_redact_text_handles_mixed_case_and_malformed_url_schemes(self):
        cases = {
            "HTTPS://user:password@example.test/path-token?query-secret#fragment": (
                "https://example.test/<redacted>?<redacted>"
            ),
            "HtTp://user:password@example.test/path-token?query-secret#fragment": (
                "http://example.test/<redacted>?<redacted>"
            ),
            "HTTPS://[broken/path-token?query-secret#fragment": "<redacted>",
        }

        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(redact_text(value), expected)

    def test_legacy_migration_never_overwrites_and_prints_only_status(self):
        sys.modules.pop("scripts.migrate_legacy_secrets", None)
        migration = importlib.import_module("scripts.migrate_legacy_secrets")

        store = MemorySecretStore()
        store.set("document_mcp_url", "already-configured")
        output = io.StringIO()
        with (
            patch.object(migration, "KeyringSecretStore", return_value=store),
            patch.dict(
                os.environ,
                {
                    "DINGTALK_MCP_URL": "legacy-document",
                    "DINGTALK_SPREADSHEET_MCP_URL": "legacy-spreadsheet",
                    "MINIMAX_API_KEY": "legacy-minimax",
                },
                clear=False,
            ),
            patch("sys.stdout", output),
        ):
            self.assertEqual(migration.main(["--confirm"]), 0)

        self.assertEqual(store.get("document_mcp_url"), "already-configured")
        self.assertEqual(store.get("spreadsheet_mcp_url"), "legacy-spreadsheet")
        self.assertEqual(store.get("minimax_api_key"), "legacy-minimax")
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "document_mcp_url: configured=True",
                "spreadsheet_mcp_url: configured=True",
                "minimax_api_key: configured=True",
            ],
        )
        for plaintext in (
            "legacy-document",
            "legacy-spreadsheet",
            "legacy-minimax",
            "already-configured",
        ):
            self.assertNotIn(plaintext, output.getvalue())

    def test_legacy_migration_direct_script_uses_controlled_dependencies(self):
        project_root = Path(__file__).resolve().parents[2]
        script = project_root / "scripts" / "migrate_legacy_secrets.py"
        fake_values = (
            "controlled-document-value",
            "controlled-spreadsheet-value",
            "controlled-minimax-value",
            "already-configured-value",
        )
        sitecustomize = textwrap.dedent(
            f"""
            import sys
            import keyring


            _values = {{"document_mcp_url": {fake_values[3]!r}}}


            def get_password(service, name):
                return _values.get(name)


            def set_password(service, name, value):
                if name == "document_mcp_url":
                    raise RuntimeError("overwrite attempted")
                _values[name] = value


            def delete_password(service, name):
                _values.pop(name, None)


            keyring.get_password = get_password
            keyring.set_password = set_password
            keyring.delete_password = delete_password

            """
        )

        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            (temp_path / "sitecustomize.py").write_text(
                sitecustomize,
                encoding="utf-8",
            )
            environment = {
                name: os.environ[name]
                for name in (
                    "COMSPEC",
                    "SYSTEMROOT",
                    "TEMP",
                    "TMP",
                    "WINDIR",
                )
                if os.environ.get(name)
            }
            environment.update(
                {
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONPATH": str(temp_path),
                    "DINGTALK_MCP_URL": fake_values[0],
                    "DINGTALK_SPREADSHEET_MCP_URL": fake_values[1],
                    "MINIMAX_API_KEY": fake_values[2],
                }
            )
            completed = subprocess.run(
                [sys.executable, str(script), "--confirm"],
                cwd=temp_path,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                "document_mcp_url: configured=True",
                "spreadsheet_mcp_url: configured=True",
                "minimax_api_key: configured=True",
            ],
        )
        for plaintext in fake_values:
            self.assertNotIn(plaintext, completed.stdout + completed.stderr)

    def test_rollback_attempts_every_secret_and_raises_safe_consistency_error(self):
        original_values = {
            "document_mcp_url": "old-document-value",
            "spreadsheet_mcp_url": "old-spreadsheet-value",
            "minimax_api_key": "old-minimax-value",
        }
        replacement_values = {
            "document_mcp_url": "new-document-value",
            "spreadsheet_mcp_url": "new-spreadsheet-value",
            "minimax_api_key": "new-minimax-value",
        }

        class RollbackFailureStore(MemorySecretStore):
            def __init__(self):
                super().__init__()
                self.values.update(original_values)
                self.rollback_mode = False
                self.rollback_attempts = []
                self.failed_name = None

            def set(self, name, value):
                if self.rollback_mode and value == original_values.get(name):
                    self.rollback_attempts.append(name)
                    if self.failed_name is None:
                        self.failed_name = name
                        raise RuntimeError(f"rollback exposed {value}")
                super().set(name, value)

        store = RollbackFailureStore()
        service = SettingsService(
            self.service.repository,
            store,
            environment={},
        )
        original_error = OSError("settings save failed")

        def fail_repository_update(mutator):
            store.rollback_mode = True
            raise original_error

        with patch.object(
            service.repository,
            "update",
            side_effect=fail_repository_update,
        ):
            with self.assertRaises(
                settings_service.SettingsConsistencyError
            ) as raised:
                service.update_group(
                    "document",
                    service.load().document,
                    secret_updates=replacement_values,
                )

        self.assertIs(raised.exception.__cause__, original_error)
        self.assertEqual(
            store.rollback_attempts,
            sorted(original_values),
        )
        for name, original_value in original_values.items():
            expected = (
                replacement_values[name]
                if name == store.failed_name
                else original_value
            )
            self.assertEqual(store.get(name), expected)

        safe_error = str(raised.exception)
        self.assertIn("1", safe_error)
        self.assertIn("RuntimeError", safe_error)
        self.assertNotIn("rollback exposed", safe_error)
        for plaintext in (*original_values.values(), *replacement_values.values()):
            self.assertNotIn(plaintext, safe_error)

    def test_first_secret_write_failure_rethrows_original_after_rollback(self):
        original_value = "old-document-value"
        replacement_value = "new-document-value"
        original_error = OSError("first write failed")

        class FirstWriteFailureStore(MemorySecretStore):
            def __init__(self):
                super().__init__()
                self.values["document_mcp_url"] = original_value
                self.failed = False

            def set(self, name, value):
                if value == replacement_value and not self.failed:
                    self.failed = True
                    raise original_error
                super().set(name, value)

        store = FirstWriteFailureStore()
        service = SettingsService(
            self.service.repository,
            store,
            environment={},
        )

        with self.assertRaises(OSError) as raised:
            service.update_group(
                "document",
                service.load().document,
                secret_updates={"document_mcp_url": replacement_value},
            )

        self.assertIs(raised.exception, original_error)
        self.assertEqual(store.get("document_mcp_url"), original_value)

    def test_final_state_validator_runs_before_any_json_or_secret_write(self):
        before = self.service.load().model_dump(mode="json")
        observed = {}

        def reject(prospective, resolved_secrets):
            observed["provider"] = prospective.ai.active_provider
            observed["secret"] = resolved_secrets["minimax_api_key"]
            with self.assertRaises(TypeError):
                resolved_secrets["minimax_api_key"] = "mutated"
            raise SettingsValidationError("final state rejected")

        with self.assertRaisesRegex(SettingsValidationError, "final state"):
            self.service.update_group(
                "ai",
                self.service.load().ai.model_copy(
                    update={"active_provider": ProviderName.minimax}
                ),
                secret_updates={"minimax_api_key": "prospective-secret"},
                final_state_validator=reject,
            )

        self.assertEqual(observed["provider"], ProviderName.minimax)
        self.assertEqual(observed["secret"], "prospective-secret")
        self.assertEqual(self.service.load().model_dump(mode="json"), before)
        self.assertIsNone(self.secrets.get("minimax_api_key"))

    def test_final_state_validator_applies_clear_before_bootstrap_fallback(self):
        class RecordingSecretStore(MemorySecretStore):
            def __init__(self):
                super().__init__()
                self.set_values = []

            def set(self, name, value):
                self.set_values.append((name, value))
                super().set(name, value)

        secrets = RecordingSecretStore()
        secrets.set("minimax_api_key", "saved-secret")
        secrets.set_values.clear()
        service = SettingsService(
            self.service.repository,
            secrets,
            environment={"MINIMAX_API_KEY": "bootstrap-secret"},
        )
        observed = []

        service.update_group(
            "ai",
            service.load().ai,
            secret_updates={"minimax_api_key": "ignored-new-secret"},
            clear_secrets={"minimax_api_key"},
            final_state_validator=lambda _settings, resolved: observed.append(
                resolved["minimax_api_key"]
            ),
        )

        self.assertEqual(observed, ["bootstrap-secret"])
        self.assertEqual(secrets.set_values, [])
        self.assertIsNone(secrets.get("minimax_api_key"))
        self.assertEqual(
            service.get_secret("minimax_api_key"),
            ("bootstrap-secret", "environment"),
        )
