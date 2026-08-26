"""原生 AI 配置入口的 Codex CLI 密钥与目录行为。"""

from __future__ import annotations

from backend.settings.secrets import MemorySecretStore
from backend.settings.service import SettingsService, ai_configuration_secret_name
from backend.settings.store import SettingsRepository
from windows_native.native_service import NativeService


class RuntimeStub:
    def __init__(self, path: str = "C:/ForTest/codex.exe") -> None:
        self.installed: list[str] = []
        self.path = path

    def install_and_switch(self, version: str) -> dict:
        """模拟切换后返回真实服务使用的活动运行时目录。"""

        self.installed.append(version)
        return {
            "status": {
                "runtime": {
                    "selection": version,
                    "version": "0.144.4" if version == "bundled" else version,
                    "path": self.path,
                    "available": True,
                    "bundled": version == "bundled",
                }
            }
        }

    def ensure_available(self, version: str):
        self.installed.append(version)
        return self.path


def test_native_codex_save_ignores_api_key_and_clears_legacy_secret(tmp_path):
    secrets = MemorySecretStore()
    settings = SettingsService(
        SettingsRepository(tmp_path / "settings.json"),
        secrets,
        environment={},
    )
    configuration_id = "codex-cli"
    secrets.set(ai_configuration_secret_name(configuration_id), "legacy-api-key")
    native = NativeService.__new__(NativeService)
    native.settings = settings
    native.codex_runtimes = RuntimeStub()

    view = native.save_ai_configuration(
        {
            "id": configuration_id,
            "name": "本机 Codex",
            "provider": "codex",
            "model": "gpt-5.6-terra",
            "codex_cli_source": "builtin",
            "codex_cli_version": "bundled",
            # 兼容旧调用方的输入也必须被忽略并清理。
            "use_dedicated_api_key": True,
            "api_key": "new-api-key-that-must-not-be-saved",
        }
    )

    item = view["configurations"][0]
    assert item["provider_label"] == "Codex CLI"
    assert item["complete"] is True
    assert "secret_status" not in item
    assert "use_dedicated_api_key" not in item
    assert item["timeout_seconds"] == 900
    assert secrets.get(ai_configuration_secret_name(configuration_id)) is None
    assert native.codex_runtimes.installed == ["bundled"]


def test_codex_model_refresh_uses_selected_cli_without_api_key(
    monkeypatch,
    tmp_path,
):
    captured = {}

    class ProviderStub:
        def __init__(self, settings, api_key):
            captured["settings"] = settings
            captured["api_key"] = api_key

        def list_models(self):
            return [{"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol"}]

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(
        "windows_native.native_service.CodexProvider",
        ProviderStub,
    )
    native = NativeService.__new__(NativeService)
    cli_path = tmp_path / "codex.exe"
    cli_path.write_bytes(b"test")
    native.codex_runtimes = RuntimeStub(str(cli_path))

    catalog = native.get_codex_configuration_models(
        {
            "model": "gpt-5.6-terra",
            "codex_cli_source": "builtin",
            "codex_cli_version": "0.147.0",
            "reasoning_effort": "high",
            "inference_speed": "fast",
            "max_concurrency": 2,
            "timeout_seconds": 600,
        }
    )

    assert catalog["models"][0]["id"] == "gpt-5.6-sol"
    assert native.codex_runtimes.installed == ["0.147.0"]
    assert captured["api_key"] is None
    assert captured["settings"].use_dedicated_api_key is False
    assert captured["settings"].cli_path == str(cli_path)
    assert captured["closed"] is True
