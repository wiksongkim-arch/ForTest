from __future__ import annotations

import base64
import io
import importlib.metadata
import os
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openai_codex
from PIL import Image

from backend.ai.codex_provider import (
    CodexProvider,
    installed_codex_cli_bin_path,
    snapshot_sdk_process,
)
from backend.ai.types import CASE_OUTPUT_SCHEMA, SectionAIRequest, TEST_CASE_FIELDS
from backend.settings.defaults import default_settings
from backend.settings.models import CodexRuntime, CodexSettings, ProviderName


LIVE_MODEL = "gpt-5.4"
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42m"
    "Nk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _codex_runtime_path() -> Path:
    provider = CodexProvider(CodexSettings())
    try:
        return provider._bundled_codex_path()
    finally:
        provider.close()


def _enabled(specific: str) -> bool:
    return os.getenv(specific) == "1" or os.getenv("RUN_LIVE_CODEX") == "1"


def minimal_live_request(images: tuple[Path, ...] = ()) -> SectionAIRequest:
    defaults = default_settings()
    return SectionAIRequest(
        section_title="登录",
        section_content=(
            "用户输入正确账号和密码后进入首页；密码错误时提示“账号或密码错误”。"
        ),
        images=images,
        component_names=("输入框", "按钮"),
        field_specs={field: field for field in TEST_CASE_FIELDS},
        component_templates={
            "按钮": [
                {
                    "test_steps": "点击提交按钮",
                    "expected_result": "系统处理当前表单",
                }
            ]
        },
        prompts=defaults.prompts.model_dump(),
        output_schema=CASE_OUTPUT_SCHEMA,
    )


def _assert_live_result(
    case: unittest.TestCase,
    result,
    *,
    runtime: str,
    expect_image_stage: bool,
) -> None:
    case.assertTrue(result.output_valid)
    case.assertEqual(result.provider, ProviderName.codex)
    case.assertEqual(result.runtime_mode, runtime)
    case.assertEqual(result.model, LIVE_MODEL)
    case.assertGreater(len(result.test_cases), 0)
    case.assertTrue(result.evidence)
    for item in result.test_cases:
        case.assertEqual(set(item), set(TEST_CASE_FIELDS))
        case.assertTrue(all(isinstance(item[field], str) for field in TEST_CASE_FIELDS))
    for evidence in result.evidence:
        case.assertEqual(evidence.provider, ProviderName.codex)
        case.assertEqual(evidence.runtime_mode, runtime)
        case.assertEqual(evidence.model, LIVE_MODEL)
        case.assertTrue(evidence.output_valid)
    if expect_image_stage:
        case.assertTrue(
            any(item.stage == "image_analysis" for item in result.evidence)
        )


def _emit_live_evidence(result, processes) -> None:
    if os.getenv("CODEX_LIVE_EVIDENCE") != "1":
        return
    print(
        "LIVE_EVIDENCE="
        + json.dumps(
            {
                "runtime": result.runtime_mode,
                "model": result.model,
                "case_count": len(result.test_cases),
                "stage_count": len(result.evidence),
                "stages": [item.stage for item in result.evidence],
                "duration_ms": result.duration_ms,
                "retry_count": result.retry_count,
                "pid_reaped": bool(processes)
                and all(process.poll() is not None for process in processes),
            },
            sort_keys=True,
        ),
        flush=True,
    )


class CodexRuntimeVersionTests(unittest.TestCase):
    def test_fixed_live_image_is_a_strictly_decodable_one_pixel_png(self):
        with Image.open(io.BytesIO(ONE_PIXEL_PNG)) as image:
            image.load()
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (1, 1))

    def test_pinned_sdk_source_and_bundled_cli_runtime(self):
        self.assertEqual(importlib.metadata.version("openai-codex"), "0.0.0.dev0")
        self.assertEqual(openai_codex.__version__, "0.0.0.dev0")
        provider = CodexProvider(CodexSettings())
        try:
            self.assertEqual(
                provider.diagnostics["sdk_requested_tag"], "rust-v0.144.4"
            )
            cli_path = provider._bundled_codex_path()
            selected_version = provider.diagnostics["selected_cli_version"]
        finally:
            provider.close()

        bundled_path = installed_codex_cli_bin_path()
        if bundled_path is not None:
            self.assertEqual(cli_path.resolve(), bundled_path.resolve())
            self.assertEqual(
                selected_version,
                importlib.metadata.version("openai-codex-cli-bin"),
            )

        completed = subprocess.run(
            [
                str(cli_path),
                "--config",
                'model_reasoning_effort="high"',
                "--version",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            timeout=5,
            shell=False,
        )
        match = re.search(r"\b(\d+\.\d+\.\d+)\b", completed.stdout)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), selected_version)


@unittest.skipUnless(
    _enabled("RUN_CODEX_FREE_AUTH"), "free Codex authentication probe disabled"
)
class LiveCodexFreeAuthenticationTests(unittest.TestCase):
    def test_sdk_desktop_login_account_is_available_without_model_turn(self):
        provider = CodexProvider(
            CodexSettings(
                runtime=CodexRuntime.sdk,
                model=LIVE_MODEL,
                reasoning_effort="low",
                timeout_seconds=300,
                max_concurrency=1,
            )
        )
        try:
            health = provider.health_check()
            self.assertTrue(health.ok, health.detail)
            self.assertEqual(health.runtime_mode, "sdk")
            self.assertIsNone(provider.selected_runtime)
            self.assertEqual(provider.diagnostics["last_auth_type"], "chatgpt")
        finally:
            provider.close()

    def test_cli_login_status_is_available_without_exec_or_model_turn(self):
        provider = CodexProvider(
            CodexSettings(
                runtime=CodexRuntime.cli,
                cli_path=str(_codex_runtime_path()),
                model=LIVE_MODEL,
                reasoning_effort="low",
                timeout_seconds=300,
                max_concurrency=1,
            )
        )
        try:
            health = provider.health_check()
            self.assertTrue(health.ok, health.detail)
            self.assertEqual(health.runtime_mode, "cli")
            self.assertIsNone(provider.selected_runtime)
        finally:
            provider.close()


@unittest.skipUnless(
    _enabled("RUN_LIVE_CODEX_SDK"), "paid Codex SDK test disabled"
)
class LiveCodexSDKTests(unittest.TestCase):
    def test_sdk_returns_schema_valid_cases_and_accepts_local_image(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "pixel.png"
            image_path.write_bytes(ONE_PIXEL_PNG)
            provider = CodexProvider(
                CodexSettings(
                    runtime=CodexRuntime.sdk,
                    model=LIVE_MODEL,
                    reasoning_effort="low",
                    timeout_seconds=300,
                    max_concurrency=1,
                )
            )
            workspace = provider.workspace
            owned_processes = []

            def capture_owned_process(codex):
                process = snapshot_sdk_process(codex)
                if process is not None:
                    owned_processes.append(process)
                return process

            try:
                with patch(
                    "backend.ai.codex_provider.snapshot_sdk_process",
                    side_effect=capture_owned_process,
                ):
                    result = provider.process_section(
                        minimal_live_request(images=(image_path,))
                    )
                _assert_live_result(
                    self,
                    result,
                    runtime="sdk",
                    expect_image_stage=True,
                )
            finally:
                provider.close()
            self.assertFalse(workspace.exists())
            self.assertTrue(owned_processes)
            self.assertTrue(
                all(process.poll() is not None for process in owned_processes)
            )
            _emit_live_evidence(result, owned_processes)


@unittest.skipUnless(
    _enabled("RUN_LIVE_CODEX_CLI"), "paid Codex CLI test disabled"
)
class LiveCodexCLITests(unittest.TestCase):
    def _provider(self) -> CodexProvider:
        self.spawned_processes = []

        def capture_popen(*args, **kwargs):
            process = subprocess.Popen(*args, **kwargs)
            self.spawned_processes.append(process)
            return process

        return CodexProvider(
            CodexSettings(
                runtime=CodexRuntime.cli,
                cli_path=str(_codex_runtime_path()),
                model=LIVE_MODEL,
                reasoning_effort="low",
                timeout_seconds=300,
                max_concurrency=1,
            ),
            popen_factory=capture_popen,
        )

    def test_cli_returns_schema_valid_cases_without_image(self):
        provider = self._provider()
        workspace = provider.workspace
        try:
            result = provider.process_section(minimal_live_request())
            _assert_live_result(
                self,
                result,
                runtime="cli",
                expect_image_stage=False,
            )
        finally:
            provider.close()
        self.assertFalse(workspace.exists())
        self.assertTrue(self.spawned_processes)
        self.assertTrue(
            all(process.poll() is not None for process in self.spawned_processes)
        )
        _emit_live_evidence(result, self.spawned_processes)

    def test_cli_returns_schema_valid_cases_with_local_image(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "pixel.png"
            image_path.write_bytes(ONE_PIXEL_PNG)
            provider = self._provider()
            workspace = provider.workspace
            try:
                result = provider.process_section(
                    minimal_live_request(images=(image_path,))
                )
                _assert_live_result(
                    self,
                    result,
                    runtime="cli",
                    expect_image_stage=True,
                )
            finally:
                provider.close()
            self.assertFalse(workspace.exists())
            self.assertTrue(self.spawned_processes)
            self.assertTrue(
                all(
                    process.poll() is not None
                    for process in self.spawned_processes
                )
            )
            _emit_live_evidence(result, self.spawned_processes)


if __name__ == "__main__":
    unittest.main()
