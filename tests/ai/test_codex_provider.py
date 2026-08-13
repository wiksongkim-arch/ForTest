import asyncio
from dataclasses import replace
import importlib.metadata
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from openai_codex import (
    AsyncCodex,
    CodexConfig,
    CodexRpcError,
    InvalidParamsError,
    ServerBusyError,
    TransportClosedError,
    is_retryable_error as sdk_is_retryable_error,
)
from openai_codex.client import CodexClient

from backend.ai.base import ProviderResponseError, ProviderUnavailableError
from backend.ai.codex_provider import (
    CodexProvider,
    SDKLaunchError,
    _sdk_requested_tag,
    build_child_env,
    build_cli_args,
    build_sdk_env_overlay,
    close_sdk_bounded,
    run_turn_with_timeout,
    terminate_process_tree,
)
from backend.ai.codex_launcher import (
    main as launcher_main,
    prepare_process_group,
    sanitized_environment,
)
from backend.ai.types import (
    COMPONENT_OUTPUT_SCHEMA,
    SECTION_OUTPUT_SCHEMA,
    SectionAIRequest,
)
from backend.settings.defaults import default_settings
from backend.settings.models import CodexRuntime, CodexSettings


def make_section_request() -> SectionAIRequest:
    settings = default_settings()
    return SectionAIRequest(
        section_title="登录",
        section_content="正确账号密码登录成功，错误密码显示提示。",
        images=(),
        component_names=("输入框", "按钮"),
        field_specs={
            "module": "所属模块",
            "case_name": "用例名称",
            "prerequisite": "前置条件",
            "test_steps": "测试步骤",
            "expected_result": "预期结果",
            "priority": "优先级",
            "case_type": "用例类型",
            "applicable_phase": "适用阶段",
            "remark": "备注",
            "case_id": "用例编号",
            "execution": "执行状态",
        },
        component_templates={},
        prompts=settings.prompts.model_dump(),
    )


def turn_result(
    turn_id: str,
    payload: dict | str,
    *,
    usage=None,
    duration_ms: int = 5,
):
    return SimpleNamespace(
        final_response=(
            payload if isinstance(payload, str) else json.dumps(payload)
        ),
        id=turn_id,
        status=SimpleNamespace(value="completed"),
        error=None,
        duration_ms=duration_ms,
        usage=usage,
    )


def fake_turn(turn_id: str, payload: dict | str):
    handle = MagicMock()
    handle.id = turn_id
    handle.run = AsyncMock(return_value=turn_result(turn_id, payload))
    handle.interrupt = AsyncMock()
    return handle


def fake_codex_sdk(final_response: str | None = None):
    sdk = MagicMock()
    sdk.__aenter__ = AsyncMock(return_value=sdk)
    sdk.__aexit__ = AsyncMock(return_value=False)
    sdk.close = AsyncMock()
    sdk.login_api_key = AsyncMock()
    sdk.account = AsyncMock(
        return_value=SimpleNamespace(account={"type": "chatgpt"})
    )
    sdk.models = AsyncMock(
        return_value=SimpleNamespace(
            data=[SimpleNamespace(model="gpt-5.4", is_default=True)]
        )
    )
    thread = MagicMock()
    thread.turn = AsyncMock(
        side_effect=[
            fake_turn("turn-image", {"image_findings": []}),
            fake_turn("turn-components", {"matched_components": []}),
            fake_turn(
                "turn-cases",
                (
                    final_response
                    if final_response is not None
                    else {"test_cases": []}
                ),
            ),
        ]
    )
    sdk.thread = thread
    sdk.thread_start = AsyncMock(return_value=thread)
    return sdk


def valid_section_payload(**overrides):
    payload = {
        "image_findings": [],
        "matched_components": [],
        "test_cases": [],
    }
    payload.update(overrides)
    return payload


class CodexProviderTests(unittest.TestCase):
    def test_minimum_case_count_scales_with_explicit_requirement_lines(self):
        request = make_section_request()
        request = replace(
            request,
            section_content="\n".join(
                f"- 明确验证规则 {index}" for index in range(24)
            ),
            component_templates={
                "按钮": [
                    {"用例步骤": f"步骤 {index}", "预期结果": "成功"}
                    for index in range(50)
                ]
            },
        )

        self.assertEqual(
            CodexProvider._minimum_case_count(request, ["按钮"]),
            50,
        )
        instruction = CodexProvider._coverage_instruction(50)
        self.assertIn("生成 50 至 60 条", instruction)
        self.assertIn("不得用同义改写凑数", instruction)

    def test_minimum_case_count_has_bounded_document_density(self):
        request = replace(
            make_section_request(),
            section_content="\n".join(
                f"- 明确验证规则 {index}" for index in range(24)
            ),
            component_templates={},
        )

        minimum = CodexProvider._minimum_case_count(request)

        self.assertEqual(minimum, 38)
        self.assertIn("生成 38 至 46 条", CodexProvider._coverage_instruction(minimum))

    def test_auto_mode_prefers_sdk_and_runs_three_turns(self):
        sdk = fake_codex_sdk()
        provider = CodexProvider(
            CodexSettings(runtime=CodexRuntime.auto),
            sdk_factory=lambda: sdk,
        )
        self.addCleanup(provider.close)
        result = provider.process_section(make_section_request())
        self.assertEqual(result.runtime_mode, "sdk")
        self.assertEqual(sdk.thread.turn.call_count, 3)

    def test_sdk_replaces_stale_model_with_account_catalog_default(self):
        sdk = fake_codex_sdk()
        sdk.models = AsyncMock(
            return_value=SimpleNamespace(
                data=[
                    SimpleNamespace(model="gpt-5.5", is_default=True),
                    SimpleNamespace(model="gpt-5.4", is_default=False),
                ]
            )
        )
        provider = CodexProvider(
            CodexSettings(runtime=CodexRuntime.sdk, model="gpt-5.6"),
            sdk_factory=lambda: sdk,
        )
        self.addCleanup(provider.close)

        result = provider.process_section(make_section_request())

        sdk.models.assert_awaited_once_with(include_hidden=False)
        self.assertEqual(sdk.thread_start.await_args.kwargs["model"], "gpt-5.5")
        self.assertEqual(result.model, "gpt-5.5")
        self.assertTrue(all(item.model == "gpt-5.5" for item in result.evidence))
        self.assertEqual(provider.diagnostics["configured_model"], "gpt-5.6")
        self.assertEqual(provider.diagnostics["effective_model"], "gpt-5.5")
        self.assertEqual(provider.diagnostics["model_selection"], "catalog_default")

    def test_sdk_turn_failure_keeps_safe_server_detail_and_redacts_secret(self):
        sdk = fake_codex_sdk()
        failed = MagicMock(id="turn-failed")
        failed.run = AsyncMock(
            side_effect=RuntimeError(
                json.dumps(
                    {
                        "error": {
                            "message": (
                                "configured model is not supported; "
                                "token=sk-private-token-value"
                            )
                        }
                    }
                )
            )
        )
        failed.interrupt = AsyncMock()
        sdk.thread.turn = AsyncMock(return_value=failed)
        provider = CodexProvider(
            CodexSettings(runtime=CodexRuntime.sdk),
            sdk_factory=lambda: sdk,
        )
        self.addCleanup(provider.close)

        with self.assertRaises(ProviderResponseError) as raised:
            provider.process_section(make_section_request())

        message = str(raised.exception)
        self.assertIn("configured model is not supported", message)
        self.assertNotIn("sk-private-token-value", message)
        self.assertIn("<redacted>", message)

    def test_auto_mode_falls_back_only_when_sdk_cannot_start(self):
        runner = Mock(
            return_value={
                "image_findings": [],
                "matched_components": [],
                "test_cases": [],
            }
        )
        provider = CodexProvider(
            CodexSettings(runtime=CodexRuntime.auto),
            sdk_factory=Mock(side_effect=OSError("unavailable")),
            cli_runner=runner,
        )
        self.addCleanup(provider.close)
        result = provider.process_section(make_section_request())
        self.assertEqual(result.runtime_mode, "cli")
        runner.assert_called_once()

    def test_invalid_completed_sdk_output_does_not_call_cli(self):
        sdk = fake_codex_sdk(final_response="not-json")
        runner = Mock()
        provider = CodexProvider(
            CodexSettings(runtime=CodexRuntime.auto),
            sdk_factory=lambda: sdk,
            cli_runner=runner,
        )
        self.addCleanup(provider.close)
        with self.assertRaises(ProviderResponseError):
            provider.process_section(make_section_request())
        runner.assert_not_called()

    def test_pinned_sdk_uses_complete_launcher_override_verbatim(self):
        config = CodexConfig(
            launch_args_override=(
                sys.executable,
                "codex_launcher.py",
                "app-server",
                "--listen",
                "stdio://",
            )
        )
        client = CodexClient(config)
        process = MagicMock()
        with (
            patch(
                "openai_codex.client.subprocess.Popen",
                return_value=process,
            ) as popen,
            patch.object(CodexClient, "_start_stderr_drain_thread"),
            patch.object(CodexClient, "_start_reader_thread"),
        ):
            client.start()
        self.assertEqual(
            popen.call_args.args[0],
            [
                sys.executable,
                "codex_launcher.py",
                "app-server",
                "--listen",
                "stdio://",
            ],
        )
        client.close()
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=2)
        async_codex = AsyncCodex(config)
        self.assertIsNone(async_codex._client._sync._proc)

    def test_cli_arguments_include_images_model_schema_and_safe_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_paths = (root / "one.png", root / "two.png")
            for path in image_paths:
                path.write_bytes(b"png")
            args = build_cli_args(
                cli_path=root / "codex.exe",
                settings=CodexSettings(model="gpt-5.4"),
                schema_path=root / "schema.json",
                output_path=root / "output.json",
                images=image_paths,
            )
        self.assertIn("--ignore-user-config", args)
        self.assertIn("--ignore-rules", args)
        self.assertEqual(args.count("--image"), 2)
        self.assertIn("gpt-5.4", args)
        self.assertEqual(args[-1], "-")

    def test_cli_environment_excludes_unrelated_secrets(self):
        env = build_child_env(
            source={
                "PATH": "bin",
                "SYSTEMROOT": "C:/Windows",
                "MINIMAX_API_KEY": "must-not-leak",
                "DINGTALK_MCP_URL": "must-not-leak",
            },
            codex_api_key="codex-only",
            dedicated_codex_home=Path("D:/isolated-codex"),
        )
        self.assertEqual(env["CODEX_API_KEY"], "codex-only")
        self.assertNotIn("MINIMAX_API_KEY", env)
        self.assertNotIn("DINGTALK_MCP_URL", env)
        sdk_env = sanitized_environment(
            {
                **env,
                "OPENAI_COMPATIBLE_API_KEY": "must-not-leak",
            }
        )
        self.assertNotIn("CODEX_API_KEY", sdk_env)
        self.assertNotIn("OPENAI_COMPATIBLE_API_KEY", sdk_env)

    def test_windows_timeout_kills_and_reaps_process_tree(self):
        process = Mock(pid=1234)
        with patch("backend.ai.codex_provider.subprocess.run") as taskkill:
            terminate_process_tree(process, platform="nt")
        taskkill.assert_called_once_with(
            ["taskkill", "/PID", "1234", "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        process.wait.assert_called_once_with(timeout=5)

    def test_sdk_launcher_creates_a_posix_process_group(self):
        with patch(
            "backend.ai.codex_launcher.os.setsid", create=True
        ) as setsid:
            prepare_process_group(platform="posix")
        setsid.assert_called_once_with()

    def test_posix_timeout_kills_the_isolated_process_group(self):
        process = Mock(pid=1234)
        with patch(
            "backend.ai.codex_provider.os.killpg", create=True
        ) as killpg:
            terminate_process_tree(process, platform="posix")
        killpg.assert_called_once_with(
            1234, getattr(signal, "SIGKILL", signal.SIGTERM)
        )
        process.wait.assert_called_once_with(timeout=5)

    def test_windows_taskkill_timeout_falls_back_to_direct_kill(self):
        process = Mock(pid=1234)
        with patch(
            "backend.ai.codex_provider.subprocess.run",
            side_effect=subprocess.TimeoutExpired("taskkill", 5),
        ):
            terminate_process_tree(process, platform="nt")
        process.kill.assert_called_once_with()

    def test_windows_taskkill_nonzero_falls_back_to_direct_kill(self):
        process = Mock(pid=1234)
        completed = SimpleNamespace(returncode=1)
        with patch(
            "backend.ai.codex_provider.subprocess.run",
            return_value=completed,
        ):
            terminate_process_tree(process, platform="nt")
        process.kill.assert_called_once_with()

    def test_process_wait_failure_is_contained_during_tree_cleanup(self):
        process = Mock(pid=1234)
        process.wait.side_effect = OSError("process already gone")
        with patch(
            "backend.ai.codex_provider.subprocess.run",
            return_value=SimpleNamespace(returncode=0),
        ):
            terminate_process_tree(process, platform="nt")

        process.kill.assert_called_once_with()

    def test_provider_close_timeout_forces_abort_without_context_exit(self):
        sdk = fake_codex_sdk()
        release_close = asyncio.Event()
        owned_process = Mock(pid=5678)
        sdk._client = SimpleNamespace(
            _sync=SimpleNamespace(_proc=owned_process)
        )

        async def close_until_aborted():
            # The pinned SDK clears this before later blocking cleanup.
            sdk._client._sync._proc = None
            await release_close.wait()

        force_abort = Mock(side_effect=lambda _: release_close.set())
        sdk.close = AsyncMock(side_effect=close_until_aborted)
        provider = CodexProvider(
            CodexSettings(runtime=CodexRuntime.sdk),
            sdk_factory=lambda: sdk,
            sdk_force_abort=force_abort,
            cleanup_timeout_seconds=0.01,
        )
        self.addCleanup(provider.close)
        started = time.monotonic()
        result = provider.process_section(make_section_request())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(result.runtime_mode, "sdk")
        self.assertIsNone(sdk._client._sync._proc)
        force_abort.assert_called_once_with(owned_process)
        sdk.__aenter__.assert_not_awaited()
        sdk.__aexit__.assert_not_awaited()


class CodexSDKBoundaryTests(unittest.TestCase):
    def make_provider(self, *args, **kwargs):
        provider = CodexProvider(*args, **kwargs)
        self.addCleanup(provider.close)
        return provider

    def test_dedicated_key_mode_fails_closed_for_missing_or_blank_key(self):
        settings = CodexSettings(
            runtime=CodexRuntime.auto,
            use_dedicated_api_key=True,
        )
        for api_key in (None, "", "   "):
            with self.subTest(api_key=api_key):
                sdk_factory = Mock()
                cli_runner = Mock()
                with (
                    patch(
                        "backend.ai.codex_provider.tempfile.TemporaryDirectory"
                    ) as temporary_directory,
                    self.assertRaises(ProviderUnavailableError),
                ):
                    CodexProvider(
                        settings,
                        api_key=api_key,
                        sdk_factory=sdk_factory,
                        cli_runner=cli_runner,
                    )
                temporary_directory.assert_not_called()
                sdk_factory.assert_not_called()
                cli_runner.assert_not_called()

    def test_first_turn_start_oserror_is_the_last_sdk_fallback_phase(self):
        sdk = fake_codex_sdk()
        sdk.thread.turn = AsyncMock(side_effect=OSError("transport closed"))
        runner = Mock(return_value=valid_section_payload())
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.auto),
            sdk_factory=lambda: sdk,
            cli_runner=runner,
        )

        result = provider.process_section(make_section_request())

        self.assertEqual(result.runtime_mode, "cli")
        self.assertEqual(provider.selected_runtime, "cli")
        runner.assert_called_once()
        sdk.close.assert_awaited_once()

    def test_account_and_thread_launch_failures_are_cli_eligible(self):
        scenarios = []
        missing_account = fake_codex_sdk()
        missing_account.account = AsyncMock(
            return_value=SimpleNamespace(account=None)
        )
        scenarios.append(missing_account)
        rpc_account = fake_codex_sdk()
        rpc_account.account = AsyncMock(
            side_effect=CodexRpcError(500, "service unavailable")
        )
        scenarios.append(rpc_account)
        missing_thread = fake_codex_sdk()
        missing_thread.thread_start = AsyncMock(
            side_effect=OSError("app-server closed")
        )
        scenarios.append(missing_thread)

        for sdk in scenarios:
            with self.subTest(sdk=sdk):
                runner = Mock(return_value=valid_section_payload())
                provider = self.make_provider(
                    CodexSettings(runtime=CodexRuntime.auto),
                    sdk_factory=lambda sdk=sdk: sdk,
                    cli_runner=runner,
                )

                result = provider.process_section(make_section_request())

                self.assertEqual(result.runtime_mode, "cli")
                runner.assert_called_once()

    def test_unclassified_account_and_invalid_params_do_not_call_cli(self):
        account_sdk = fake_codex_sdk()
        account_sdk.account = AsyncMock(side_effect=RuntimeError("bad account"))
        params_sdk = fake_codex_sdk()
        params_sdk.thread.turn = AsyncMock(
            side_effect=InvalidParamsError(-32602, "invalid params")
        )
        for sdk, expected_error in (
            (account_sdk, ProviderUnavailableError),
            (params_sdk, ProviderResponseError),
        ):
            with self.subTest(sdk=sdk):
                runner = Mock()
                provider = self.make_provider(
                    CodexSettings(runtime=CodexRuntime.auto),
                    sdk_factory=lambda sdk=sdk: sdk,
                    cli_runner=runner,
                )

                with self.assertRaises(expected_error):
                    provider.process_section(make_section_request())

                self.assertEqual(provider.selected_runtime, "sdk")
                runner.assert_not_called()

    def test_second_and_third_turn_start_failures_never_call_cli(self):
        for failing_index in (1, 2):
            with self.subTest(failing_index=failing_index):
                sdk = fake_codex_sdk()
                handles = [
                    fake_turn("turn-image", {"image_findings": []}),
                    fake_turn(
                        "turn-components", {"matched_components": []}
                    ),
                    fake_turn("turn-cases", {"test_cases": []}),
                ]
                side_effect = handles[:failing_index]
                side_effect.append(OSError("late launch failure"))
                sdk.thread.turn = AsyncMock(side_effect=side_effect)
                runner = Mock()
                provider = self.make_provider(
                    CodexSettings(runtime=CodexRuntime.auto),
                    sdk_factory=lambda sdk=sdk: sdk,
                    cli_runner=runner,
                )

                with self.assertRaises(ProviderResponseError):
                    provider.process_section(make_section_request())

                self.assertEqual(provider.selected_runtime, "sdk")
                runner.assert_not_called()

    def test_timeout_after_first_handle_never_calls_cli(self):
        sdk = fake_codex_sdk()
        handle = MagicMock(id="turn-timeout")

        async def wait_forever():
            await asyncio.Event().wait()

        handle.run = AsyncMock(side_effect=wait_forever)
        handle.interrupt = AsyncMock(side_effect=wait_forever)
        sdk.thread.turn = AsyncMock(return_value=handle)
        runner = Mock()
        settings = CodexSettings(runtime=CodexRuntime.auto).model_copy(
            update={"timeout_seconds": 0.01}
        )
        provider = self.make_provider(
            settings,
            sdk_factory=lambda: sdk,
            cli_runner=runner,
            cleanup_timeout_seconds=0.01,
        )

        with self.assertRaises(ProviderResponseError):
            provider.process_section(make_section_request())

        self.assertEqual(provider.selected_runtime, "sdk")
        runner.assert_not_called()
        handle.interrupt.assert_awaited_once()

    def test_cli_fallback_selection_is_reused_without_more_sdk_attempts(self):
        sdk_factory = Mock(side_effect=OSError("unavailable"))
        runner = Mock(return_value=valid_section_payload())
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.auto),
            sdk_factory=sdk_factory,
            cli_runner=runner,
        )

        first = provider.process_section(make_section_request())
        second = provider.process_section(make_section_request())

        self.assertEqual((first.runtime_mode, second.runtime_mode), ("cli", "cli"))
        sdk_factory.assert_called_once()
        self.assertEqual(runner.call_count, 2)

    def test_invalid_sdk_response_locks_sdk_for_later_sections(self):
        invalid_sdk = fake_codex_sdk(final_response="not-json")
        sdk_factory = Mock(
            side_effect=[invalid_sdk, OSError("later construction failure")]
        )
        runner = Mock()
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.auto),
            sdk_factory=sdk_factory,
            cli_runner=runner,
        )

        with self.assertRaises(ProviderResponseError):
            provider.process_section(make_section_request())
        with self.assertRaises(ProviderUnavailableError):
            provider.process_section(make_section_request())

        self.assertEqual(provider.selected_runtime, "sdk")
        self.assertEqual(sdk_factory.call_count, 2)
        runner.assert_not_called()

    def test_sdk_retries_only_the_malformed_turn_once(self):
        sdk = fake_codex_sdk()
        sdk.thread.turn = AsyncMock(
            side_effect=[
                fake_turn("image-bad", "not-json"),
                fake_turn("image-good", {"image_findings": []}),
                fake_turn("components", {"matched_components": []}),
                fake_turn("cases", {"test_cases": []}),
            ]
        )
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.sdk),
            sdk_factory=lambda: sdk,
        )

        result = provider.process_section(make_section_request())

        self.assertEqual(sdk.thread.turn.await_count, 4)
        self.assertEqual(result.retry_count, 1)
        self.assertEqual(result.evidence[0].retry_count, 1)

    def test_sdk_uses_official_retry_classifier_for_canonical_server_busy(self):
        sdk = fake_codex_sdk()
        transient = fake_turn("image-transient", {"image_findings": []})
        server_busy = ServerBusyError(
            -32000,
            "please try again",
            {"codex_error_info": "server_overloaded"},
        )
        transient.run = AsyncMock(
            side_effect=server_busy
        )
        sdk.thread.turn = AsyncMock(
            side_effect=[
                transient,
                fake_turn("image-retry", {"image_findings": []}),
                fake_turn("components", {"matched_components": []}),
                fake_turn("cases", {"test_cases": []}),
            ]
        )
        runner = Mock()
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.auto),
            sdk_factory=lambda: sdk,
            cli_runner=runner,
        )

        with patch(
            "backend.ai.codex_provider.is_retryable_error",
            wraps=sdk_is_retryable_error,
        ) as classifier:
            result = provider.process_section(make_section_request())

        self.assertEqual(sdk.thread.turn.await_count, 4)
        self.assertEqual(result.evidence[0].retry_count, 1)
        classifier.assert_any_call(server_busy)
        runner.assert_not_called()

    def test_transport_closed_poisoned_client_is_never_retried_or_cli_fallback(self):
        sdk = fake_codex_sdk()
        poisoned = fake_turn("image-poisoned", {"image_findings": []})
        poisoned.run = AsyncMock(side_effect=TransportClosedError())
        sdk.thread.turn = AsyncMock(return_value=poisoned)
        runner = Mock()
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.auto),
            sdk_factory=lambda: sdk,
            cli_runner=runner,
        )

        with self.assertRaises(ProviderResponseError):
            provider.process_section(make_section_request())

        self.assertEqual(sdk.thread.turn.await_count, 1)
        self.assertEqual(provider.selected_runtime, "sdk")
        runner.assert_not_called()

    def test_sdk_filters_components_templates_and_records_usage(self):
        usage = SimpleNamespace(
            last=SimpleNamespace(input_tokens=3, output_tokens=2),
            total=SimpleNamespace(input_tokens=11, output_tokens=7),
        )
        sdk = fake_codex_sdk()
        image = fake_turn("image-id", {"image_findings": ["visible"]})
        image.run = AsyncMock(
            return_value=turn_result(
                "image-id", {"image_findings": ["visible"]}, usage=usage
            )
        )
        components = fake_turn(
            "component-id",
            {"matched_components": ["输入框", "未知", "输入框"]},
        )
        components.run = AsyncMock(
            return_value=turn_result(
                "component-id",
                {"matched_components": ["输入框", "未知", "输入框"]},
                usage=SimpleNamespace(
                    total=SimpleNamespace(
                        input_tokens=13, output_tokens=9
                    )
                ),
            )
        )
        cases = fake_turn("case-id", {"test_cases": []})
        sdk.thread.turn = AsyncMock(
            side_effect=[image, components, cases]
        )
        request = replace(
            make_section_request(),
            component_templates={
                "输入框": [{"template": "INPUT_ONLY"}],
                "按钮": [{"template": "BUTTON_MUST_NOT_APPEAR"}],
            },
        )
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.sdk),
            sdk_factory=lambda: sdk,
        )

        result = provider.process_section(request)

        self.assertEqual(result.matched_components, ["输入框"])
        case_prompt = sdk.thread.turn.await_args_list[2].args[0]
        self.assertIn("INPUT_ONLY", case_prompt)
        self.assertNotIn("BUTTON_MUST_NOT_APPEAR", case_prompt)
        self.assertEqual(
            [item.turn_id for item in result.evidence],
            ["image-id", "component-id", "case-id"],
        )
        self.assertEqual(result.evidence[0].input_tokens, 3)
        self.assertEqual(result.evidence[0].output_tokens, 2)
        self.assertEqual(result.evidence[1].input_tokens, 13)
        self.assertEqual(result.evidence[1].output_tokens, 9)

    def test_all_prompt_formatting_is_checked_before_sdk_construction(self):
        request = replace(
            make_section_request(),
            prompts={
                **make_section_request().prompts,
                "component_matching": (
                    "{requirement:{unknown}} {component_names}"
                ),
            },
        )
        sdk_factory = Mock()
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.auto),
            sdk_factory=sdk_factory,
        )

        with self.assertRaises(ProviderResponseError):
            provider.process_section(request)

        sdk_factory.assert_not_called()
        self.assertIsNone(provider.selected_runtime)

    def test_default_factory_uses_complete_override_and_scrubbed_launcher_env(self):
        provider = self.make_provider(CodexSettings(runtime=CodexRuntime.sdk))
        sentinel = object()
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "codex.exe"
            executable.write_bytes(b"binary")
            with (
                patch.object(
                    provider,
                    "_bundled_codex_path",
                    return_value=executable,
                ),
                patch(
                    "backend.ai.codex_provider.AsyncCodex",
                    return_value=sentinel,
                ) as constructor,
                patch.dict(
                    os.environ,
                    {
                        "PATH": "bin",
                        "CODEX_HOME": "desktop-home",
                        "MINIMAX_API_KEY": "must-not-leak",
                        "ARBITRARY_SECRET": "arbitrary-must-not-leak",
                    },
                    clear=True,
                ),
            ):
                created = provider._default_sdk_factory()

        self.assertIs(created, sentinel)
        config = constructor.call_args.args[0]
        if os.name == "nt":
            self.assertEqual(
                config.launch_args_override,
                (
                    str(executable),
                    "--config",
                    'model_reasoning_effort="high"',
                    "app-server",
                    "--listen",
                    "stdio://",
                ),
            )
        else:
            launcher = str(
                Path("backend/ai/codex_launcher.py").resolve()
            )
            self.assertEqual(
                config.launch_args_override,
                (
                    sys.executable,
                    launcher,
                    "--config",
                    'model_reasoning_effort="high"',
                    "app-server",
                    "--listen",
                    "stdio://",
                ),
            )
        self.assertEqual(config.env["CODEX_HOME"], "desktop-home")
        self.assertEqual(config.env["MINIMAX_API_KEY"], "")
        self.assertEqual(config.env["ARBITRARY_SECRET"], "")
        self.assertNotIn("must-not-leak", config.env.values())
        if os.name == "nt":
            self.assertNotIn("PRDTOCASE_CODEX_BIN", config.env)
        else:
            self.assertEqual(
                config.env["PRDTOCASE_CODEX_BIN"], str(executable)
            )

        process = MagicMock()
        with (
            patch.dict(
                os.environ,
                {
                    "PATH": "bin",
                    "CODEX_HOME": "desktop-home",
                    "MINIMAX_API_KEY": "must-not-leak",
                    "ARBITRARY_SECRET": "arbitrary-must-not-leak",
                },
                clear=True,
            ),
            patch(
                "openai_codex.client.subprocess.Popen",
                return_value=process,
            ) as popen,
            patch.object(CodexClient, "_start_stderr_drain_thread"),
            patch.object(CodexClient, "_start_reader_thread"),
        ):
            direct_client = CodexClient(config)
            direct_client.start()
            child_environment = popen.call_args.kwargs["env"]
            direct_client.close()
        self.assertEqual(child_environment["MINIMAX_API_KEY"], "")
        self.assertEqual(child_environment["ARBITRARY_SECRET"], "")
        self.assertNotIn("must-not-leak", child_environment.values())
        self.assertNotIn("arbitrary-must-not-leak", child_environment.values())
        process.terminate.assert_called_once_with()

    def test_sdk_environment_overlay_erases_every_non_allowlisted_value(self):
        overlay = build_sdk_env_overlay(
            {
                "PATH": "safe-path",
                "CODEX_HOME": "desktop-home",
                "MINIMAX_API_KEY": "provider-sentinel",
                "ARBITRARY_SECRET": "arbitrary-sentinel",
            }
        )
        merged = {
            "PATH": "safe-path",
            "CODEX_HOME": "desktop-home",
            "MINIMAX_API_KEY": "provider-sentinel",
            "ARBITRARY_SECRET": "arbitrary-sentinel",
        }
        merged.update(overlay)

        self.assertEqual(merged["PATH"], "safe-path")
        self.assertEqual(merged["CODEX_HOME"], "desktop-home")
        self.assertEqual(merged["MINIMAX_API_KEY"], "")
        self.assertEqual(merged["ARBITRARY_SECRET"], "")
        self.assertNotIn("provider-sentinel", merged.values())
        self.assertNotIn("arbitrary-sentinel", merged.values())

    def test_health_probe_only_checks_account_and_closes_without_model_turn(self):
        sdk = fake_codex_sdk()
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.auto),
            sdk_factory=lambda: sdk,
        )

        health = provider.health_check()

        self.assertTrue(health.ok)
        self.assertEqual(health.runtime_mode, "sdk")
        self.assertIsNone(provider.selected_runtime)
        self.assertEqual(provider.diagnostics["last_auth_type"], "chatgpt")
        sdk.account.assert_awaited_once_with(refresh_token=False)
        sdk.login_api_key.assert_not_awaited()
        sdk.thread_start.assert_not_awaited()
        sdk.thread.turn.assert_not_awaited()
        sdk.close.assert_awaited_once()

    def test_dedicated_sdk_health_authenticates_key_without_starting_thread(self):
        sdk = fake_codex_sdk()
        provider = self.make_provider(
            CodexSettings(
                runtime=CodexRuntime.sdk,
                use_dedicated_api_key=True,
            ),
            api_key="dedicated-health-key",
            sdk_factory=lambda: sdk,
        )

        health = provider.health_check()

        self.assertTrue(health.ok)
        sdk.login_api_key.assert_awaited_once_with("dedicated-health-key")
        sdk.account.assert_awaited_once_with(refresh_token=False)
        sdk.thread_start.assert_not_awaited()
        sdk.thread.turn.assert_not_awaited()

    def test_sdk_health_failure_records_only_safe_exception_types(self):
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.sdk),
            sdk_factory=Mock(side_effect=OSError("private failure text")),
        )

        health = provider.health_check()

        self.assertFalse(health.ok)
        self.assertEqual(
            provider.diagnostics["last_health_error_type"],
            "SDKLaunchError",
        )
        self.assertEqual(
            provider.diagnostics["last_health_cause_type"], "OSError"
        )
        self.assertNotIn("private failure text", str(provider.diagnostics))

    def test_sdk_auth_type_reads_only_the_account_root_literal(self):
        sdk = fake_codex_sdk()
        sdk.account = AsyncMock(
            return_value=SimpleNamespace(
                account=SimpleNamespace(
                    root=SimpleNamespace(
                        type="chatgpt",
                        email="private-account@example.test",
                        plan="private-plan",
                    )
                )
            )
        )
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.sdk),
            sdk_factory=lambda: sdk,
        )

        health = provider.health_check()

        self.assertTrue(health.ok)
        self.assertEqual(provider.diagnostics["last_auth_type"], "chatgpt")
        self.assertNotIn("private-account", str(provider.diagnostics))
        self.assertNotIn("private-plan", str(provider.diagnostics))

    def test_close_removes_owned_directories_and_rejects_reuse(self):
        settings = CodexSettings(
            runtime=CodexRuntime.cli,
            use_dedicated_api_key=True,
        )
        provider = CodexProvider(
            settings,
            api_key="codex-secret",
            cli_runner=Mock(return_value=valid_section_payload()),
        )
        workspace = provider.workspace
        codex_home = provider.dedicated_codex_home

        provider.close()

        self.assertFalse(workspace.exists())
        self.assertIsNotNone(codex_home)
        self.assertFalse(codex_home.exists())
        with self.assertRaises(ProviderUnavailableError):
            provider.process_section(make_section_request())

    def test_semaphore_is_shared_by_providers_with_the_same_limit(self):
        active = 0
        maximum = 0
        state_lock = threading.Lock()

        def runner(_request):
            nonlocal active, maximum
            with state_lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with state_lock:
                active -= 1
            return valid_section_payload()

        settings = CodexSettings(
            runtime=CodexRuntime.cli, max_concurrency=1
        )
        first = self.make_provider(settings, cli_runner=runner)
        second = self.make_provider(settings, cli_runner=runner)
        threads = [
            threading.Thread(
                target=provider.process_section,
                args=(make_section_request(),),
            )
            for provider in (first, second)
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)

        self.assertEqual(maximum, 1)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_lifecycle_does_not_serialize_configured_max_concurrency(self):
        active = 0
        maximum = 0
        both_entered = threading.Event()
        release = threading.Event()
        state_lock = threading.Lock()

        def runner(_request):
            nonlocal active, maximum
            with state_lock:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    both_entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("parallel runners did not release")
            with state_lock:
                active -= 1
            return valid_section_payload()

        provider = self.make_provider(
            CodexSettings(
                runtime=CodexRuntime.cli, max_concurrency=2
            ),
            cli_runner=runner,
        )
        self.addCleanup(release.set)
        errors = []
        threads = [
            threading.Thread(
                target=lambda: self._capture_thread_error(
                    errors,
                    lambda: provider.process_section(
                        make_section_request()
                    ),
                )
            )
            for _ in range(2)
        ]

        for thread in threads:
            thread.start()
        self.assertTrue(both_entered.wait(timeout=1))
        release.set()
        for thread in threads:
            thread.join(timeout=1)

        self.assertFalse(errors)
        self.assertEqual(maximum, 2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_health_uses_the_shared_semaphore(self):
        active = 0
        maximum = 0
        state_lock = threading.Lock()

        async def account(*, refresh_token):
            nonlocal active, maximum
            self.assertFalse(refresh_token)
            with state_lock:
                active += 1
                maximum = max(maximum, active)
            await asyncio.sleep(0.03)
            with state_lock:
                active -= 1
            return SimpleNamespace(account={"type": "chatgpt"})

        settings = CodexSettings(
            runtime=CodexRuntime.sdk, max_concurrency=1
        )
        first_sdk = fake_codex_sdk()
        second_sdk = fake_codex_sdk()
        first_sdk.account = AsyncMock(side_effect=account)
        second_sdk.account = AsyncMock(side_effect=account)
        first = self.make_provider(settings, sdk_factory=lambda: first_sdk)
        second = self.make_provider(settings, sdk_factory=lambda: second_sdk)
        results = []
        threads = [
            threading.Thread(target=lambda p=p: results.append(p.health_check()))
            for p in (first, second)
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)

        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(maximum, 1)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_close_waits_for_inflight_process_and_rejects_new_calls(self):
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def runner(_request):
            entered.set()
            if not release.wait(timeout=1):
                raise TimeoutError("test runner was not released")
            return valid_section_payload()

        provider = CodexProvider(
            CodexSettings(
                runtime=CodexRuntime.cli,
                use_dedicated_api_key=True,
            ),
            api_key="dedicated-key",
            cli_runner=runner,
        )
        self.addCleanup(release.set)
        self.addCleanup(provider.close)
        workspace = provider.workspace
        codex_home = provider.dedicated_codex_home
        worker = threading.Thread(
            target=lambda: self._capture_thread_error(
                errors,
                lambda: provider.process_section(make_section_request()),
            )
        )
        worker.start()
        self.assertTrue(entered.wait(timeout=5))
        closer = threading.Thread(target=provider.close)
        closer.start()
        self.assertTrue(self._wait_until(lambda: provider._closing))

        self.assertTrue(closer.is_alive())
        self.assertTrue(workspace.exists())
        self.assertIsNotNone(codex_home)
        self.assertTrue(codex_home.exists())
        with self.assertRaises(ProviderUnavailableError):
            provider.process_section(make_section_request())

        release.set()
        worker.join(timeout=1)
        closer.join(timeout=1)
        self.assertFalse(errors)
        self.assertFalse(worker.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertFalse(workspace.exists())
        self.assertFalse(codex_home.exists())

    def test_close_waits_for_inflight_health_probe(self):
        entered = threading.Event()
        release = threading.Event()

        async def account(*, refresh_token):
            self.assertFalse(refresh_token)
            entered.set()
            if not release.wait(timeout=1):
                raise TimeoutError("test runner was not released")
            return SimpleNamespace(account={"type": "chatgpt"})

        sdk = fake_codex_sdk()
        sdk.account = AsyncMock(side_effect=account)
        provider = CodexProvider(
            CodexSettings(runtime=CodexRuntime.sdk),
            sdk_factory=lambda: sdk,
        )
        self.addCleanup(release.set)
        self.addCleanup(provider.close)
        workspace = provider.workspace
        health_results = []
        probe = threading.Thread(
            target=lambda: health_results.append(provider.health_check())
        )
        probe.start()
        self.assertTrue(entered.wait(timeout=1))
        closer = threading.Thread(target=provider.close)
        closer.start()
        self.assertTrue(self._wait_until(lambda: provider._closing))

        self.assertTrue(closer.is_alive())
        self.assertTrue(workspace.exists())
        self.assertFalse(provider.health_check().ok)

        release.set()
        probe.join(timeout=5)
        closer.join(timeout=5)
        self.assertEqual(len(health_results), 1)
        self.assertTrue(health_results[0].ok)
        self.assertFalse(workspace.exists())

    @staticmethod
    def _capture_thread_error(errors, operation):
        try:
            operation()
        except BaseException as exc:
            errors.append(exc)

    @staticmethod
    def _wait_until(predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def test_diagnostics_keep_sdk_tag_and_bundled_cli_version_distinct(self):
        provider = self.make_provider(CodexSettings())

        diagnostics = provider.diagnostics

        self.assertEqual(diagnostics["sdk_requested_tag"], "rust-v0.144.4")
        self.assertEqual(diagnostics["sdk_package_version"], "0.0.0.dev0")
        # 独立 Windows 构建会显式覆盖 SDK 元数据中的旧 CLI，确保模型目录支持 GPT-5.6。
        self.assertEqual(diagnostics["bundled_cli_package_version"], "0.144.4")

    def test_missing_sdk_distribution_does_not_break_provider_construction(self):
        with patch(
            "backend.ai.codex_provider.importlib.metadata.distribution",
            side_effect=importlib.metadata.PackageNotFoundError("missing"),
        ):
            self.assertEqual(_sdk_requested_tag(), "unavailable")

    def test_missing_bundled_sdk_runtime_falls_back_to_cli(self):
        runner = Mock(return_value=valid_section_payload())
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.auto),
            cli_runner=runner,
        )
        with patch.object(
            provider,
            "_bundled_codex_path",
            side_effect=SDKLaunchError("missing runtime"),
        ):
            result = provider.process_section(make_section_request())

        self.assertEqual(result.runtime_mode, "cli")
        runner.assert_called_once()

    def test_git_missing_or_nonzero_is_cli_eligible(self):
        failures = (
            OSError("git missing"),
            SimpleNamespace(returncode=1),
        )
        for failure in failures:
            with self.subTest(failure=failure):
                git_runner = Mock(
                    side_effect=failure
                    if isinstance(failure, BaseException)
                    else None,
                    return_value=(
                        failure
                        if not isinstance(failure, BaseException)
                        else None
                    ),
                )
                sdk_factory = Mock()
                runner = Mock(return_value=valid_section_payload())
                provider = self.make_provider(
                    CodexSettings(runtime=CodexRuntime.auto),
                    sdk_factory=sdk_factory,
                    cli_runner=runner,
                    git_runner=git_runner,
                )

                result = provider.process_section(make_section_request())

                self.assertEqual(result.runtime_mode, "cli")
                sdk_factory.assert_not_called()
                runner.assert_called_once()


class CodexCLIBoundaryTests(unittest.TestCase):
    def make_executable(self, name: str = "codex.exe") -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        executable = Path(directory.name) / name
        executable.write_bytes(b"binary")
        return executable

    def make_provider(self, settings, **kwargs):
        provider = CodexProvider(settings, **kwargs)
        self.addCleanup(provider.close)
        return provider

    @staticmethod
    def successful_probe(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="codex-cli 0.132.0\n",
            stderr="",
        )

    def test_explicit_cli_path_controls_both_invocation_protocols(self):
        saved = self.make_executable("saved.exe")
        managed_cli = self.make_executable("managed-cli.exe")
        managed_sdk = self.make_executable("managed-sdk.exe")
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.auto, cli_path=str(saved))
        )

        with patch.dict(
            os.environ,
            {
                "FORTEST_CODEX_CLI_BIN": str(managed_cli),
                "FORTEST_CODEX_SDK_BIN": str(managed_sdk),
            },
            clear=True,
        ):
            cli_candidates = provider._candidate_cli_paths()
            sdk_candidates = provider._candidate_sdk_paths()

        self.assertEqual(cli_candidates, [saved])
        self.assertEqual(sdk_candidates, [saved])

    def test_previous_qaq_managed_cli_path_remains_compatible(self):
        managed_cli = self.make_executable("legacy-managed-cli.exe")
        managed_sdk = self.make_executable("legacy-managed-sdk.exe")
        provider = self.make_provider(CodexSettings(runtime=CodexRuntime.auto))

        with patch.dict(
            os.environ,
            {
                "QAQ_CODEX_CLI_BIN": str(managed_cli),
                "QAQ_CODEX_SDK_BIN": str(managed_sdk),
            },
            clear=True,
        ):
            cli_candidates = provider._candidate_cli_paths()
            sdk_candidates = provider._candidate_sdk_paths()

        self.assertEqual(cli_candidates, [managed_cli])
        self.assertEqual(sdk_candidates, [managed_cli])

    def test_cli_exec_uses_exact_safe_process_contract_and_output_file(self):
        executable = self.make_executable()
        image_root = tempfile.TemporaryDirectory()
        self.addCleanup(image_root.cleanup)
        images = (
            Path(image_root.name) / "one.png",
            Path(image_root.name) / "two.png",
        )
        for image in images:
            image.write_bytes(b"png")
        request = replace(make_section_request(), images=images)
        settings = CodexSettings(
            runtime=CodexRuntime.cli,
            cli_path=str(executable),
            model="gpt-5.4",
            reasoning_effort="xhigh",
            use_dedicated_api_key=True,
        )
        captured = {}
        payload = valid_section_payload(
            matched_components=["输入框", "未知", "输入框"]
        )

        def popen(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            captured["schema_path"] = Path(
                args[args.index("--output-schema") + 1]
            )
            captured["output_path"] = Path(args[args.index("-o") + 1])

            class Process:
                pid = 4321
                returncode = 0

                def communicate(self, prompt, timeout):
                    captured["prompt"] = prompt
                    captured["timeout"] = timeout
                    captured["schema"] = json.loads(
                        captured["schema_path"].read_text(encoding="utf-8")
                    )
                    captured["output_path"].write_text(
                        json.dumps(payload, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    return "stdout is deliberately not JSON", ""

            return Process()

        with patch.dict(
            os.environ,
            {
                "PATH": "bin",
                "SYSTEMROOT": "C:/Windows",
                "CODEX_HOME": "desktop-home",
                "MINIMAX_API_KEY": "must-not-leak",
                "DINGTALK_MCP_URL": "must-not-leak",
            },
            clear=True,
        ):
            provider = self.make_provider(
                settings,
                api_key="dedicated-codex-key",
                probe_runner=self.successful_probe,
                popen_factory=popen,
            )
            result = provider.process_section(request)
            dedicated_home = provider.dedicated_codex_home

        args = captured["args"]
        kwargs = captured["kwargs"]
        self.assertEqual(args[0:3], [str(executable), "exec", "--ephemeral"])
        self.assertIn("--ignore-user-config", args)
        self.assertIn("--ignore-rules", args)
        self.assertIn('model_reasoning_effort="xhigh"', args)
        self.assertIn('approval_policy="never"', args)
        self.assertEqual(args.count("--image"), 2)
        self.assertEqual(args[-1], "-")
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["start_new_session"], os.name != "nt")
        self.assertEqual(
            kwargs["creationflags"],
            (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt"
                else 0
            ),
        )
        self.assertEqual(kwargs["env"]["CODEX_API_KEY"], "dedicated-codex-key")
        self.assertEqual(kwargs["env"]["CODEX_HOME"], str(dedicated_home))
        self.assertNotIn("MINIMAX_API_KEY", kwargs["env"])
        self.assertNotIn("DINGTALK_MCP_URL", kwargs["env"])
        self.assertEqual(captured["schema"], SECTION_OUTPUT_SCHEMA)
        self.assertEqual(result.matched_components, ["输入框"])
        self.assertEqual(result.evidence[0].runtime_mode, "cli")
        self.assertTrue(
            any(item.stage == "image_analysis" for item in result.evidence)
        )
        self.assertFalse(captured["schema_path"].exists())
        self.assertFalse(captured["output_path"].exists())

    def test_injected_cli_runner_obeys_component_allowlist(self):
        runner = Mock(
            return_value=valid_section_payload(
                matched_components=["未知", "按钮", "按钮"]
            )
        )
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.cli),
            cli_runner=runner,
        )

        result = provider.process_section(make_section_request())

        self.assertEqual(result.matched_components, ["按钮"])

    def test_explicit_cli_path_is_authoritative_when_probe_fails(self):
        executable = self.make_executable("saved-codex.exe")
        probe = Mock(
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="")
        )
        popen = Mock()
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.cli, cli_path=str(executable)),
            probe_runner=probe,
            popen_factory=popen,
        )

        health = provider.health_check()

        self.assertFalse(health.ok)
        self.assertIsNone(provider.selected_runtime)
        popen.assert_not_called()
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(
            probe.call_args.args[0], [str(executable), "--version"]
        )

    def test_cli_health_uses_free_login_status_without_inference(self):
        executable = self.make_executable()
        commands = []

        def probe(command, **kwargs):
            commands.append((command, kwargs))
            return self.successful_probe()

        popen = Mock()

        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.cli, cli_path=str(executable)),
            probe_runner=probe,
            popen_factory=popen,
        )

        health = provider.health_check()

        self.assertTrue(health.ok)
        self.assertEqual(
            [item[0] for item in commands],
            [
                [str(executable), "--version"],
                [
                    str(executable.resolve()),
                    "--config",
                    'model_reasoning_effort="high"',
                    "login",
                    "status",
                ],
            ],
        )
        login_kwargs = commands[-1][1]
        self.assertIs(login_kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(login_kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(login_kwargs["shell"])
        self.assertEqual(login_kwargs["timeout"], 5)
        self.assertNotIn("MINIMAX_API_KEY", login_kwargs["env"])
        popen.assert_not_called()
        self.assertIsNone(provider.selected_runtime)

    def test_auto_health_falls_back_to_free_cli_auth_only_after_sdk_launch_error(self):
        executable = self.make_executable()
        probe = Mock(side_effect=self.successful_probe)
        popen = Mock()
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.auto, cli_path=str(executable)),
            sdk_factory=Mock(side_effect=OSError("sdk unavailable")),
            probe_runner=probe,
            popen_factory=popen,
        )

        health = provider.health_check()

        self.assertTrue(health.ok)
        self.assertEqual(health.runtime_mode, "cli")
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(probe.call_args.args[0][-2:], ["login", "status"])
        popen.assert_not_called()

    def test_dedicated_cli_health_logs_in_via_stdin_then_checks_status(self):
        executable = self.make_executable()
        calls = []

        def probe(command, **kwargs):
            calls.append((command, kwargs))
            return self.successful_probe()

        provider = self.make_provider(
            CodexSettings(
                runtime=CodexRuntime.cli,
                cli_path=str(executable),
                use_dedicated_api_key=True,
            ),
            api_key="dedicated-health-value",
            probe_runner=probe,
            popen_factory=Mock(),
        )

        health = provider.health_check()

        self.assertTrue(health.ok)
        self.assertEqual(len(calls), 3)
        login_command, login_kwargs = calls[-2]
        status_command, status_kwargs = calls[-1]
        self.assertEqual(login_command[-2:], ["login", "--with-api-key"])
        self.assertEqual(status_command[-2:], ["login", "status"])
        self.assertNotIn("dedicated-health-value", login_command)
        self.assertNotIn("dedicated-health-value", status_command)
        self.assertEqual(login_kwargs["input"], "dedicated-health-value")
        self.assertNotIn("CODEX_API_KEY", login_kwargs["env"])
        self.assertNotIn("CODEX_API_KEY", status_kwargs["env"])
        self.assertIs(login_kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(login_kwargs["stderr"], subprocess.DEVNULL)
        self.assertIs(status_kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(status_kwargs["stderr"], subprocess.DEVNULL)

    def test_bundled_cli_is_first_candidate_and_gets_both_probes(self):
        bundled = self.make_executable("bundled-codex.exe")
        desktop = self.make_executable("desktop-codex.exe")
        probe = Mock(side_effect=self.successful_probe)
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.cli),
            probe_runner=probe,
        )

        with patch(
            "backend.ai.codex_provider.desktop_codex_path",
            return_value=desktop,
        ), patch(
            "backend.ai.codex_provider.installed_codex_cli_bin_path",
            return_value=bundled,
        ):
            resolved = provider._resolve_cli_path()
            again = provider._resolve_cli_path()

        self.assertEqual(resolved, bundled.resolve())
        self.assertEqual(again, bundled.resolve())
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in probe.call_args_list],
            [
                [str(bundled), "--version"],
                [str(bundled), "exec", "--help"],
            ],
        )
        for call in probe.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 5)
            self.assertFalse(call.kwargs["shell"])
            self.assertNotIn("MINIMAX_API_KEY", call.kwargs["env"])
        self.assertEqual(
            provider.diagnostics["selected_cli_version"], "0.132.0"
        )

    def test_cli_resolution_falls_from_bundled_to_desktop_runtime(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        bundled = Path(root.name) / "bundled.exe"
        desktop = Path(root.name) / "desktop.exe"
        for path in (bundled, desktop):
            path.write_bytes(b"binary")

        def probe(command, **_kwargs):
            if Path(command[0]) == bundled:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return self.successful_probe()

        probe_runner = Mock(side_effect=probe)
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.cli),
            probe_runner=probe_runner,
        )
        with (
            patch(
                "backend.ai.codex_provider.desktop_codex_path",
                return_value=desktop,
            ),
            patch(
                "backend.ai.codex_provider.installed_codex_cli_bin_path",
                return_value=bundled,
            ),
            patch("backend.ai.codex_provider.shutil.which", return_value=None),
        ):
            resolved = provider._resolve_cli_path()

        self.assertEqual(resolved, desktop.resolve())
        self.assertEqual(
            [call.args[0] for call in probe_runner.call_args_list],
            [
                [str(bundled), "--version"],
                [str(desktop), "--version"],
                [str(desktop), "exec", "--help"],
            ],
        )

    def test_dedicated_key_is_not_sent_to_untrusted_probe_candidate(self):
        executable = self.make_executable()
        probe = Mock(side_effect=self.successful_probe)
        provider = self.make_provider(
            CodexSettings(
                runtime=CodexRuntime.cli,
                cli_path=str(executable),
                use_dedicated_api_key=True,
            ),
            api_key="probe-must-not-see-this",
            probe_runner=probe,
        )

        provider._resolve_cli_path()

        self.assertEqual(probe.call_count, 2)
        for call in probe.call_args_list:
            self.assertNotIn("CODEX_API_KEY", call.kwargs["env"])
            self.assertEqual(
                call.kwargs["env"]["CODEX_HOME"],
                str(provider.dedicated_codex_home),
            )

    def test_cli_retries_entire_invocation_once_after_malformed_output(self):
        executable = self.make_executable()
        invocations = []

        def popen(args, **kwargs):
            output_path = Path(args[args.index("-o") + 1])
            invocation = len(invocations)
            invocations.append((args, kwargs, output_path.parent))

            class Process:
                pid = 5678
                returncode = 0

                def communicate(self, _prompt, timeout):
                    output_path.write_text(
                        (
                            "not-json"
                            if invocation == 0
                            else json.dumps(valid_section_payload())
                        ),
                        encoding="utf-8",
                    )
                    return "ignored", ""

            return Process()

        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.cli, cli_path=str(executable)),
            probe_runner=self.successful_probe,
            popen_factory=popen,
        )

        result = provider.process_section(make_section_request())

        self.assertEqual(len(invocations), 2)
        self.assertEqual(result.retry_count, 1)
        self.assertTrue(all(not root.exists() for _, _, root in invocations))

    def test_cli_retries_server_overloaded_retry_limit_once(self):
        executable = self.make_executable()
        invocations = 0

        def popen(args, **_kwargs):
            nonlocal invocations
            invocation = invocations
            invocations += 1
            output_path = Path(args[args.index("-o") + 1])

            class Process:
                pid = 7788
                returncode = 1 if invocation == 0 else 0

                def communicate(self, _prompt, timeout):
                    if invocation == 0:
                        return "", "server overloaded; retry limit reached"
                    output_path.write_text(
                        json.dumps(valid_section_payload()),
                        encoding="utf-8",
                    )
                    return "ignored", ""

            return Process()

        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.cli, cli_path=str(executable)),
            probe_runner=self.successful_probe,
            popen_factory=popen,
        )

        result = provider.process_section(make_section_request())

        self.assertEqual(invocations, 2)
        self.assertEqual(result.retry_count, 1)

    def test_cli_nonzero_exit_redacts_secret_without_exposing_stderr(self):
        executable = self.make_executable()

        class Process:
            pid = 9876
            returncode = 2

            def communicate(self, _prompt, timeout):
                return "", "api_key=super-secret-value"

        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.cli, cli_path=str(executable)),
            probe_runner=self.successful_probe,
            popen_factory=Mock(return_value=Process()),
        )

        with self.assertRaises(ProviderResponseError) as raised:
            provider.process_section(make_section_request())

        message = str(raised.exception)
        self.assertIn("<redacted>", message)
        self.assertNotIn("super-secret-value", message)
        self.assertNotIn("api_key", message)

    def test_cli_timeout_terminates_the_exact_process_tree(self):
        executable = self.make_executable()
        process = Mock(pid=2468, returncode=None)
        process.communicate.side_effect = subprocess.TimeoutExpired(
            "codex", 0.01
        )
        settings = CodexSettings(
            runtime=CodexRuntime.cli, cli_path=str(executable)
        ).model_copy(update={"timeout_seconds": 0.01})
        provider = self.make_provider(
            settings,
            probe_runner=self.successful_probe,
            popen_factory=Mock(return_value=process),
        )

        with patch(
            "backend.ai.codex_provider.terminate_process_tree"
        ) as terminate:
            with self.assertRaises(ProviderResponseError):
                provider.process_section(make_section_request())

        terminate.assert_called_once_with(process, platform=os.name)

    def test_cli_communicate_error_terminates_process_and_is_redacted(self):
        executable = self.make_executable()
        process = Mock(pid=8642, returncode=None)
        process.communicate.side_effect = OSError(
            "C:/private/prd.txt?token=super-secret"
        )
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.cli, cli_path=str(executable)),
            probe_runner=self.successful_probe,
            popen_factory=Mock(return_value=process),
        )

        with patch(
            "backend.ai.codex_provider.terminate_process_tree"
        ) as terminate:
            with self.assertRaises(ProviderResponseError) as raised:
                provider.process_section(make_section_request())

        terminate.assert_called_once_with(process, platform=os.name)
        self.assertNotIn("private", str(raised.exception))
        self.assertNotIn("super-secret", str(raised.exception))

    def test_cli_strictly_validates_output_schema_even_when_stdout_is_valid(self):
        executable = self.make_executable()
        calls = 0

        def popen(args, **_kwargs):
            nonlocal calls
            calls += 1
            output_path = Path(args[args.index("-o") + 1])

            class Process:
                pid = 1357
                returncode = 0

                def communicate(self, _prompt, timeout):
                    output_path.write_text(
                        json.dumps(
                            {
                                **valid_section_payload(),
                                "unexpected": "must fail",
                            }
                        ),
                        encoding="utf-8",
                    )
                    return json.dumps(valid_section_payload()), ""

            return Process()

        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.cli, cli_path=str(executable)),
            probe_runner=self.successful_probe,
            popen_factory=popen,
        )

        with self.assertRaises(ProviderResponseError):
            provider.process_section(make_section_request())

        self.assertEqual(calls, 2)

    def test_launcher_execs_fixed_binary_with_only_allowlisted_environment(self):
        with (
            patch.dict(
                os.environ,
                {
                    "PRDTOCASE_CODEX_BIN": "C:/fixed/codex.exe",
                    "PATH": "bin",
                    "CODEX_HOME": "desktop-home",
                    "MINIMAX_API_KEY": "must-not-leak",
                },
                clear=True,
            ),
            patch.object(
                sys,
                "argv",
                [
                    "codex_launcher.py",
                    "app-server",
                    "--listen",
                    "stdio://",
                ],
            ),
            patch("backend.ai.codex_launcher.prepare_process_group") as group,
            patch("backend.ai.codex_launcher.os.execve") as execve,
        ):
            status = launcher_main()

        self.assertEqual(status, 127)
        group.assert_called_once_with()
        executable, argv, environment = execve.call_args.args
        self.assertEqual(executable, "C:/fixed/codex.exe")
        self.assertEqual(
            argv,
            [
                "C:/fixed/codex.exe",
                "app-server",
                "--listen",
                "stdio://",
            ],
        )
        self.assertEqual(environment["CODEX_HOME"], "desktop-home")
        self.assertNotIn("PRDTOCASE_CODEX_BIN", environment)
        self.assertNotIn("MINIMAX_API_KEY", environment)

    def test_git_init_uses_argument_array_and_the_same_allowlist(self):
        sdk = fake_codex_sdk()
        git_runner = Mock(return_value=SimpleNamespace(returncode=0))
        provider = self.make_provider(
            CodexSettings(runtime=CodexRuntime.sdk),
            sdk_factory=lambda: sdk,
            git_runner=git_runner,
        )
        with patch.dict(
            os.environ,
            {
                "PATH": "bin",
                "MINIMAX_API_KEY": "must-not-leak",
            },
            clear=True,
        ):
            provider.process_section(make_section_request())

        call = git_runner.call_args
        self.assertEqual(
            call.args[0],
            ["git", "init", "--quiet", str(provider.workspace)],
        )
        self.assertFalse(call.kwargs["shell"])
        self.assertNotIn("MINIMAX_API_KEY", call.kwargs["env"])


class CodexTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_timeout_interrupts_the_exact_turn(self):
        handle = MagicMock()
        handle.id = "turn-timeout"
        interrupted = asyncio.Event()

        async def wait_forever():
            await interrupted.wait()

        async def interrupt():
            interrupted.set()

        handle.run = AsyncMock(side_effect=wait_forever)
        handle.interrupt = AsyncMock(side_effect=interrupt)
        close_client = AsyncMock()
        with self.assertRaises(TimeoutError):
            await run_turn_with_timeout(
                handle,
                timeout_seconds=0.01,
                close_client=close_client,
                cleanup_timeout_seconds=0.01,
            )
        handle.interrupt.assert_awaited_once()
        close_client.assert_awaited_once()

    async def test_sdk_timeout_is_bounded_when_interrupt_never_returns(self):
        handle = MagicMock(id="turn-stuck-interrupt")

        async def wait_forever():
            await asyncio.Event().wait()

        handle.run = AsyncMock(side_effect=wait_forever)
        handle.interrupt = AsyncMock(side_effect=wait_forever)
        close_client = AsyncMock()
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(
                run_turn_with_timeout(
                    handle,
                    timeout_seconds=0.01,
                    close_client=close_client,
                    cleanup_timeout_seconds=0.01,
                ),
                timeout=0.2,
            )
        close_client.assert_awaited_once()

    async def test_sdk_close_is_bounded_even_when_abort_cannot_release_it(self):
        sdk = MagicMock()
        sdk._client = SimpleNamespace(_sync=SimpleNamespace(_proc=None))

        async def wait_forever():
            await asyncio.Event().wait()

        sdk.close = AsyncMock(side_effect=wait_forever)
        force_abort = Mock()

        await asyncio.wait_for(
            close_sdk_bounded(sdk, 0.01, force_abort),
            timeout=0.2,
        )

        sdk.close.assert_awaited_once()
        force_abort.assert_called_once_with(None)
