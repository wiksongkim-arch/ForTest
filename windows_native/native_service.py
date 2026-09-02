"""原生界面调用业务核心的门面。

所有方法都直接调用原项目的 Python 服务，不经过 HTTP、FastAPI 或 Streamlit。
"""

from __future__ import annotations

import os
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from windows_native.process_policy import install_hidden_subprocess_policy
from windows_native.errors import friendly_error
from windows_native.codex_runtime import CodexRuntimeError, CodexRuntimeManager
from windows_native.desktop_preferences import DesktopPreferences
from windows_native.startup_registration import StartupRegistration
from windows_native.update_service import UpdateService

# 必须先安装策略，再加载可能探测 Codex CLI 的业务模块。
install_hidden_subprocess_policy()

from backend.ai.registry import build_provider_registry  # noqa: E402
from backend.ai.codex_provider import CodexProvider  # noqa: E402
from backend.ai.configuration_health import test_cloud_configuration  # noqa: E402
from backend.ai.provider_specs import (  # noqa: E402
    PROVIDER_SPEC_BY_ID,
    provider_specs_view,
)
from backend.api.routes import (  # noqa: E402
    GenerateRequest,
    RecoverOutputRequest,
    discard_terminal_task,
    get_task_status,
    recover_output,
    start_generate,
    stop_generation_task,
)
from backend.api.settings_routes import (  # noqa: E402
    AIConnectionTest,
    AIUpdate,
    DocumentConnectionTest,
    DocumentUpdate,
    RuntimeDependencies,
    ai_model_catalog_view,
    ai_view,
    apply_ai_update,
    apply_document_update,
    document_view,
    prompt_group_view,
    prompt_view,
    run_ai_connection_test,
    run_document_connection_test,
    validate_prompt_draft,
)
from backend.settings.models import (  # noqa: E402
    AIConfiguration,
    AIConfigurationProvider,
    CodexRuntime,
    CodexSettings,
    ModelSelectionMode,
    ModelSelectionPolicy,
    ProviderName,
)
from backend.settings.secrets import KeyringSecretStore  # noqa: E402
from backend.settings.service import SettingsService  # noqa: E402
from backend.settings.store import SettingsRepository  # noqa: E402
from services.dingtalk_mcp import DingTalkMCPService  # noqa: E402
from services.dingtalk_spreadsheet import DingTalkSpreadSheetMCPService  # noqa: E402
from utils.default_templates import (  # noqa: E402
    CONTENT_TEMPLATE,
    OUTPUT_TEMPLATE,
    DefaultTemplateManager,
)


class NativeService:
    """与 Qt 页面共享的线程安全业务入口。"""

    def __init__(self, data_root: Path):
        self.data_root = data_root
        self.desktop_preferences = DesktopPreferences(data_root)
        self.update_service = UpdateService(data_root, self.desktop_preferences)
        self.startup_registration = StartupRegistration()
        self.codex_runtimes = CodexRuntimeManager(data_root)
        self.default_templates = DefaultTemplateManager(data_root)
        self.default_template_paths = self.default_templates.ensure_all()
        self.settings = SettingsService(
            SettingsRepository(data_root / "data" / "settings.json"),
            KeyringSecretStore(),
            environment=os.environ,
        )
        # 旧固定密钥先复制到配置级键，确认成功后仍保留旧键作为版本回退保障。
        self.settings.migrate_legacy_ai_secrets()
        self.settings.scrub_bootstrap_environment(os.environ)
        self.dependencies = RuntimeDependencies(
            service=self.settings,
            registry=build_provider_registry(
                codex_path_resolver=self.codex_runtimes.path_for_selection,
                allow_legacy=False,
            ),
            document_factory=DingTalkMCPService,
            spreadsheet_factory=DingTalkSpreadSheetMCPService,
            default_template_paths=self.default_template_paths,
        )
        self._eim = None
        self._eim_lock = threading.Lock()

    @property
    def eim(self):
        """首次访问时创建独立 EIM 生命周期，普通功能不承担额外启动成本。"""

        if self._eim is not None:
            return self._eim
        with self._eim_lock:
            if self._eim is None:
                from backend.eim.service import EIMApplicationService

                self._eim = EIMApplicationService(
                    self.data_root,
                    settings_service=self.settings,
                    codex_path_resolver=self.codex_runtimes.path_for_selection,
                )
        return self._eim

    def start_eim(self, *, restore: bool = True) -> list[str]:
        """主界面稳定后启动 EIM 调度并恢复运行意图。"""

        return self.eim.start_background(restore=restore)

    def stop_eim(self) -> None:
        """只清理已经初始化的 EIM，退出时不反向触发惰性加载。"""

        if self._eim is not None:
            self._eim.shutdown()

    def eim_status(self) -> dict[str, Any]:
        return self.eim.supervisor.status()

    def get_eim_preferences(self) -> dict[str, Any]:
        return self.desktop_preferences.get_eim_preferences()

    def save_eim_preferences(self, values: dict[str, Any]) -> dict[str, Any]:
        return self.desktop_preferences.set_eim_preferences(
            restore_running_tasks=bool(values.get("restore_running_tasks", True)),
            log_retention_days=int(values.get("log_retention_days", 30)),
        )

    def get_document(self) -> dict[str, Any]:
        view = document_view(self.settings)
        raw = str(view.get("local_output_dir") or "./output")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.data_root / path
        view["local_output_dir"] = str(path.resolve())
        return view

    def save_document(self, values: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(values)
        raw = str(normalized.get("local_output_dir") or "./output").strip()
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.data_root / path
        normalized["local_output_dir"] = str(path.resolve())
        apply_document_update(
            self.settings,
            DocumentUpdate.model_validate(normalized),
        )
        return self.get_document()

    def test_document(self, values: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(values)
        raw = str(normalized.get("local_output_dir") or "./output").strip()
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.data_root / path
        normalized["local_output_dir"] = str(path.resolve())
        payload = DocumentConnectionTest.model_validate(normalized)
        return run_document_connection_test(self.dependencies, payload)

    def default_template_path(self, template_type: str) -> str:
        """只向界面返回用户副本路径，绝不暴露程序母版位置。"""

        return str(self.default_templates.user_path(template_type))

    def restore_default_template(self, template_type: str) -> str:
        """二次确认由 UI 完成；服务层只原子替换用户副本。"""

        return str(self.default_templates.restore(template_type))

    def get_update_preferences(self) -> dict[str, Any]:
        """读取尚未在其它页面展示的原生自动更新参数。"""

        return self.desktop_preferences.get_update_preferences()

    def save_update_preferences(self, values: dict[str, Any]) -> dict[str, Any]:
        saved = self.desktop_preferences.set_update_preferences(
            enabled=bool(values.get("enabled", True)),
            channel=str(values.get("channel") or "stable"),
            manifest_url=str(values.get("manifest_url") or ""),
        )
        self.update_service.reload()
        return saved

    def check_for_update(self) -> dict[str, Any]:
        """手动或自动读取 GitHub Release，不在检查阶段下载文件。"""

        return self.update_service.check_for_update()

    def check_for_update_automatically(self) -> dict[str, Any] | None:
        """仅在用户启用自动检查时联网，启动阶段不下载或安装。"""

        if not self.update_service.can_check():
            return None
        return self.update_service.check_for_update()

    def install_update(self, update: dict[str, Any]) -> str:
        """下载并校验更新安装器，校验通过后才交给 Windows 启动。"""

        return self.update_service.download_and_launch(update)

    def get_application_preferences(self) -> dict[str, Any]:
        """读取窗口关闭策略与 Windows 当前用户的实际开机启动状态。"""

        return {
            "close_behavior": self.desktop_preferences.get_close_behavior(),
            "start_with_windows": self.startup_registration.is_enabled(),
        }

    def save_application_preferences(
        self,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        """保存应用行为；注册表失败时不写入不一致的关闭策略。"""

        close_behavior = str(values.get("close_behavior") or "ask")
        # 先用偏好模型的允许集合校验，但在注册表成功前不落盘。
        if close_behavior not in {"ask", "minimize", "quit"}:
            raise ValueError(f"不支持的关闭行为：{close_behavior}")
        start_with_windows = bool(values.get("start_with_windows", False))
        previous_startup = self.startup_registration.is_enabled()
        try:
            self.startup_registration.set_enabled(start_with_windows)
            self.desktop_preferences.set_close_behavior(close_behavior)
        except Exception:
            # 尽力恢复注册表原状态，避免界面提示失败但系统行为已经改变。
            try:
                self.startup_registration.set_enabled(previous_startup)
            except Exception:
                pass
            raise
        return self.get_application_preferences()

    def configuration_status(
        self,
        *,
        jenkins_configured: bool = False,
    ) -> dict[str, Any]:
        """按主菜单层级返回配置状态；这里只读取已保存的检测结果。"""

        document = self.get_document()
        ai_configurations = self.get_ai_configurations()
        document_mcp = bool((document.get("document_mcp") or {}).get("configured"))
        spreadsheet_mcp = bool(
            (document.get("spreadsheet_mcp") or {}).get("configured")
        )
        content_template_url = str(
            document.get("content_template_url") or ""
        ).strip()
        document_template_url = str(
            document.get("document_template_url") or ""
        ).strip()
        template_paths = getattr(self, "default_template_paths", {}) or {}

        def local_template_ready(key: str) -> bool:
            path = template_paths.get(key)
            return bool(path and Path(path).is_file())

        # 在线能力按实际配置条件校验；未配置在线模板时，本地生成链路即可独立完成。
        needs_document_mcp = bool(document_template_url)
        needs_spreadsheet_mcp = bool(
            content_template_url or document_template_url
        )
        document_checks = [
            {
                "id": "document_mcp",
                "label": "文档 MCP",
                "complete": document_mcp,
                "optional": not needs_document_mcp,
            },
            {
                "id": "spreadsheet_mcp",
                "label": "表格 MCP",
                "complete": spreadsheet_mcp,
                "optional": not needs_spreadsheet_mcp,
            },
            {
                "id": "content_template_url",
                "label": "用例模板表格",
                "complete": bool(
                    content_template_url
                    or local_template_ready(CONTENT_TEMPLATE)
                ),
            },
            {
                "id": "document_template_url",
                "label": "输出文档模板",
                "complete": bool(
                    document_template_url
                    or local_template_ready(OUTPUT_TEMPLATE)
                ),
            },
            {
                "id": "output_folder_url",
                "label": "输出文件夹",
                "complete": bool(str(document.get("output_folder_url") or "").strip()),
                "optional": not bool(document_template_url),
            },
            {
                "id": "local_output_dir",
                "label": "本地备份目录",
                "complete": bool(str(document.get("local_output_dir") or "").strip()),
            },
        ]
        document_ready = all(
            item["complete"]
            for item in document_checks
            if not item.get("optional")
        )
        ai_ready = any(
            bool(item.get("complete")) and item.get("status") == "passed"
            for item in ai_configurations.get("configurations") or []
        )
        sections = [
            {
                "id": "quick_deploy",
                "label": "快捷部署",
                "complete": bool(jenkins_configured),
                "items": [
                    {
                        "id": "jenkins",
                        "label": "Jenkins 配置",
                        "complete": bool(jenkins_configured),
                    }
                ],
            },
            {
                "id": "test_case_generation",
                "label": "测试用例生成",
                "complete": document_ready,
                "items": [
                    {
                        "id": "document",
                        "label": "文档配置",
                        "complete": document_ready,
                        "checks": document_checks,
                    }
                ],
            },
            {
                "id": "settings",
                "label": "设置",
                "complete": ai_ready,
                "items": [
                    {
                        "id": "ai",
                        "label": "AI 配置",
                        "complete": ai_ready,
                        "checks": [
                            {
                                "id": "healthy_ai",
                                "label": "至少一条检测通过的配置",
                                "complete": ai_ready,
                            }
                        ],
                    }
                ],
            },
        ]
        return {
            "complete": all(item["complete"] for item in sections),
            "sections": sections,
        }

    def generation_model_info(self) -> dict[str, str]:
        """捕获任务创建时的模型信息，供任务详情与日志持久化显示。"""

        settings = self.settings.load().ai
        policy = settings.test_case_policies.case_generation
        active = [
            item
            for item in settings.configurations
            if item.deleted_at is None
            and self.settings.ai_configuration_is_complete(item)
        ]
        if policy.mode == ModelSelectionMode.custom:
            by_id = {item.id: item for item in active}
            ordered = [
                by_id[item]
                for item in policy.configuration_ids
                if item in by_id
            ]
            strategy = "自定义模型链"
        else:
            ordered = active
            strategy = "按配置顺序"
        first = ordered[0] if ordered else None
        return {
            "provider": "mixed",
            "model_name": strategy,
            "model_version": " → ".join(item.model for item in ordered),
            "reasoning_effort": (
                first.reasoning_effort.value
                if first is not None
                and first.provider == AIConfigurationProvider.codex
                else "不适用"
            ),
            "inference_speed": (
                first.inference_speed.value
                if first is not None
                and first.provider == AIConfigurationProvider.codex
                else "standard"
            ),
            "runtime": "能力路由",
        }

    def get_prompts(self) -> dict[str, Any]:
        return prompt_view(self.settings)

    def get_prompt_group(self, name: str) -> dict[str, Any]:
        return prompt_group_view(self.settings.load(), name)

    def validate_prompt(self, name: str, content: str) -> dict[str, Any]:
        return validate_prompt_draft(name, content)

    def save_prompt(
        self,
        prompt_name: str,
        option_id: str | None,
        name: str | None,
        content: str | None,
    ) -> dict[str, Any]:
        if option_id != "default":
            if content is None:
                raise ValueError("提示词内容不能为空")
            validate_prompt_draft(prompt_name, content)
        settings, saved_id = self.settings.save_prompt_option(
            prompt_name,
            option_id=option_id,
            name=name,
            content=content,
        )
        return {
            "saved_option_id": saved_id,
            "group": prompt_group_view(settings, prompt_name),
        }

    def delete_prompt(self, prompt_name: str, option_id: str) -> dict[str, Any]:
        settings = self.settings.delete_prompt_option(prompt_name, option_id)
        return prompt_group_view(settings, prompt_name)

    def get_ai(self) -> dict[str, Any]:
        return ai_view(self.settings)

    def _ai_configuration_view(self, configuration: AIConfiguration) -> dict[str, Any]:
        spec = PROVIDER_SPEC_BY_ID[configuration.provider]
        view = {
            **configuration.model_dump(mode="json"),
            "provider_label": spec.label,
            "complete": self.settings.ai_configuration_is_complete(configuration),
            "display_model": f"{spec.label} · {configuration.model}",
        }
        if configuration.provider != AIConfigurationProvider.codex:
            secret = self.settings.ai_configuration_secret_status(configuration.id)
            view["secret_status"] = secret.model_dump(mode="json")
        return view

    def get_ai_configurations(self) -> dict[str, Any]:
        """返回正常列表、回收站和新增弹窗所需的官方厂商目录。"""

        configurations = self.settings.load().ai.configurations
        views = [self._ai_configuration_view(item) for item in configurations]
        return {
            "providers": provider_specs_view(),
            "configurations": [
                item for item in views if item.get("deleted_at") is None
            ],
            "recycle_bin": [
                item for item in views if item.get("deleted_at") is not None
            ],
        }

    def save_ai_configuration(self, values: dict[str, Any]) -> dict[str, Any]:
        """保存一条完整表单；Codex 内置版本先下载校验再提交元数据。"""

        payload = dict(values)
        api_key = payload.pop("api_key", None)
        clear_api_key = bool(payload.pop("clear_api_key", False))
        payload["id"] = str(payload.get("id") or uuid4())
        provider = AIConfigurationProvider(payload["provider"])
        if provider == AIConfigurationProvider.codex:
            # Codex CLI 只使用本机登录态；兼容旧调用方时也不再接收配置级密钥。
            payload.pop("use_dedicated_api_key", None)
            api_key = None
            clear_api_key = True
        spec = PROVIDER_SPEC_BY_ID[provider]
        payload.setdefault("name", spec.label)
        payload.setdefault("model", spec.default_model)
        payload.setdefault("base_url", spec.base_url)
        payload.setdefault("vision_enabled", spec.vision_enabled)
        payload.setdefault("response_format_mode", spec.response_format_mode.value)
        payload.setdefault(
            "timeout_seconds",
            900 if provider == AIConfigurationProvider.codex else 300,
        )
        configuration = AIConfiguration.model_validate(payload)

        if (
            configuration.provider == AIConfigurationProvider.codex
            and configuration.codex_cli_source.value == "builtin"
        ):
            # 下载与切换成功后才写配置，避免保存一个实际上不可用的版本。
            self.codex_runtimes.install_and_switch(
                configuration.codex_cli_version
            )
        self.settings.save_ai_configuration(
            configuration,
            api_key=api_key,
            clear_api_key=clear_api_key,
        )
        return self.get_ai_configurations()

    def reorder_ai_configurations(self, configuration_ids: list[str]) -> dict[str, Any]:
        self.settings.reorder_ai_configurations(configuration_ids)
        return self.get_ai_configurations()

    def delete_ai_configuration(self, configuration_id: str) -> dict[str, Any]:
        self.settings.trash_ai_configuration(configuration_id)
        return self.get_ai_configurations()

    def restore_ai_configuration(self, configuration_id: str) -> dict[str, Any]:
        self.settings.restore_ai_configuration(configuration_id)
        return self.get_ai_configurations()

    def purge_ai_configuration(self, configuration_id: str) -> dict[str, Any]:
        self.settings.purge_ai_configuration(configuration_id)
        return self.get_ai_configurations()

    def test_ai_configuration(self, configuration_id: str) -> dict[str, Any]:
        """检测一条配置并保存脱敏状态；真实密钥从不进入返回值。"""

        snapshot = self.settings.snapshot()
        configuration = next(
            (
                item
                for item in snapshot.settings.ai.configurations
                if item.id == configuration_id
            ),
            None,
        )
        if configuration is None or configuration.deleted_at is not None:
            raise ValueError("AI 配置不存在或已进入回收站")
        api_key = (
            snapshot.secrets.reveal_ai_configuration(configuration.id)
            if configuration.requires_api_key()
            else None
        )
        models: list[str] = []
        if configuration.provider == AIConfigurationProvider.codex:
            if configuration.codex_cli_source.value == "custom":
                cli_path = configuration.codex_cli_path
            else:
                resolved = self.codex_runtimes.path_for_selection(
                    configuration.codex_cli_version
                )
                cli_path = str(resolved) if resolved is not None else None
            if not cli_path:
                ok = False
                detail = "Codex CLI 版本尚未安装或路径不可用"
            else:
                provider = CodexProvider(
                    CodexSettings(
                        runtime=CodexRuntime.auto,
                        model=configuration.model,
                        reasoning_effort=configuration.reasoning_effort,
                        inference_speed=configuration.inference_speed,
                        timeout_seconds=configuration.timeout_seconds,
                        max_concurrency=configuration.max_concurrency,
                        cli_path=cli_path,
                        use_dedicated_api_key=False,
                    ),
                    api_key=None,
                )
                try:
                    health = provider.health_check()
                    ok = bool(health.ok)
                    detail = str(health.detail)
                    if ok:
                        try:
                            models = [
                                str(item.get("id") or "")
                                for item in provider.list_models()
                                if item.get("id")
                            ]
                        except Exception:
                            models = []
                finally:
                    provider.close()
        else:
            health = test_cloud_configuration(configuration, api_key)
            ok = health.ok
            detail = health.detail
            models = list(health.models)
        self.settings.record_ai_configuration_status(
            configuration.id,
            ok=ok,
            detail=detail,
        )
        return {
            "ok": ok,
            "detail": detail,
            "models": models,
            "configuration": self._ai_configuration_view(
                next(
                    item
                    for item in self.settings.load().ai.configurations
                    if item.id == configuration.id
                )
            ),
        }

    def test_all_ai_configurations(self) -> dict[str, Any]:
        """按列表顺序检测全部配置；单项失败不会中断后续检测。"""

        configuration_ids = [
            item.id
            for item in self.settings.load().ai.configurations
            if item.deleted_at is None
        ]
        results = []
        for configuration_id in configuration_ids:
            try:
                results.append(self.test_ai_configuration(configuration_id))
            except Exception as exc:
                self.settings.record_ai_configuration_status(
                    configuration_id,
                    ok=False,
                    detail=f"检测失败（{type(exc).__name__}）",
                )
                results.append(
                    {
                        "ok": False,
                        "detail": f"检测失败（{type(exc).__name__}）",
                        "models": [],
                    }
                )
        return {"results": results, **self.get_ai_configurations()}

    def get_test_case_model_policies(self) -> dict[str, Any]:
        """返回三个真实调用步骤的策略及可选择的完整配置。"""

        settings = self.settings.load()
        views = [
            self._ai_configuration_view(item)
            for item in settings.ai.configurations
            if item.deleted_at is None
        ]
        complete = [item for item in views if item.get("complete")]
        return {
            "policies": settings.ai.test_case_policies.model_dump(mode="json"),
            "available": {
                "image_understanding": [
                    item for item in complete if item.get("vision_enabled")
                ],
                "component_matching": complete,
                "case_generation": complete,
            },
        }

    def save_test_case_model_policy(
        self,
        stage: str,
        mode: str,
        configuration_ids: list[str],
    ) -> dict[str, Any]:
        policy = ModelSelectionPolicy(
            mode=ModelSelectionMode(mode),
            configuration_ids=(
                tuple(configuration_ids)
                if mode == ModelSelectionMode.custom.value
                else ()
            ),
        )
        self.settings.save_test_case_model_policy(stage, policy)
        return self.get_test_case_model_policies()

    def get_codex_runtime_status(self) -> dict[str, Any]:
        return self.codex_runtimes.status()

    def get_codex_runtime_catalog(self, *, refresh: bool = False) -> dict[str, Any]:
        if refresh:
            return self.codex_runtimes.refresh_catalog(force=True)
        try:
            return self.codex_runtimes.refresh_catalog(force=False)
        except CodexRuntimeError as exc:
            catalog = self.codex_runtimes.local_catalog()
            catalog["warning"] = str(exc)
            return catalog

    def get_local_codex_runtime_catalog(self) -> dict[str, Any]:
        return self.codex_runtimes.local_catalog()

    def get_codex_configuration_models(
        self,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        """使用弹窗当前选择的 CLI 刷新账号可见模型，不读取配置级 API Key。"""

        source = str(values.get("codex_cli_source") or "builtin")
        if source == "custom":
            raw_path = str(values.get("codex_cli_path") or "").strip()
            path = Path(raw_path).expanduser() if raw_path else None
            if path is None or not path.is_file():
                raise ValueError("请选择有效的 Codex CLI 路径")
            cli_path = str(path.resolve())
        else:
            version = str(values.get("codex_cli_version") or "bundled")
            # 用户主动刷新即确认该选择：先切换唯一活动运行时，再使用同一路径查询目录。
            runtime_catalog = self.codex_runtimes.install_and_switch(version)
            runtime_status = runtime_catalog.get("status") or {}
            runtime = (
                runtime_status.get("runtime")
                or runtime_status.get("cli")
                or {}
            )
            cli_path = str(runtime.get("path") or "")
            if not cli_path:
                raise CodexRuntimeError("所选 Codex CLI 版本路径不可用")

        provider = CodexProvider(
            CodexSettings(
                runtime=CodexRuntime.auto,
                model=str(values.get("model") or "gpt-5.6-terra"),
                reasoning_effort=str(
                    values.get("reasoning_effort") or "high"
                ),
                inference_speed=str(
                    values.get("inference_speed") or "standard"
                ),
                timeout_seconds=max(
                    30,
                    min(int(values.get("timeout_seconds") or 300), 3600),
                ),
                max_concurrency=max(
                    1,
                    min(int(values.get("max_concurrency") or 1), 4),
                ),
                cli_path=cli_path,
                use_dedicated_api_key=False,
            ),
            api_key=None,
        )
        try:
            models = provider.list_models()
        finally:
            provider.close()
        return {
            "models": models,
            "default_model": str(values.get("model") or ""),
        }

    def install_codex_runtime(self, version: str) -> dict[str, Any]:
        """切换 CLI 与 SDK 共用的唯一 Codex 运行时。"""

        return self.codex_runtimes.install_and_switch(version)

    def save_ai(self, values: dict[str, Any]) -> dict[str, Any]:
        apply_ai_update(self.settings, AIUpdate.model_validate(values))
        return self.get_ai()

    def get_models(self, provider: str) -> dict[str, Any]:
        return ai_model_catalog_view(self.settings, ProviderName(provider))

    def test_ai(self, provider: str) -> dict[str, Any]:
        health = run_ai_connection_test(
            self.dependencies,
            AIConnectionTest(provider=ProviderName(provider)),
        )
        result = asdict(health)
        result["provider"] = health.provider.value
        return result

    def start_generation(
        self,
        document_source: str,
        *,
        source_type: str = "link",
    ) -> str:
        response = start_generate(
            GenerateRequest(
                source_type=source_type,
                document_source=document_source,
            ),
            self.dependencies,
        )
        return response["task_id"]

    def task_status(self, task_id: str) -> dict[str, Any]:
        return get_task_status(task_id)

    def stop_generation(self, task_id: str) -> bool:
        """只中止指定的后端生成任务，不扫描或结束其它同名进程。"""

        response = stop_generation_task(task_id)
        return bool(response.get("stopped"))

    def release_generation_task(self, task_id: str) -> bool:
        """任务结果落盘后释放后端的短期内存快照。"""

        return discard_terminal_task(task_id)

    def recover(self, node_id: str, expected_count: int) -> dict[str, Any]:
        return recover_output(
            RecoverOutputRequest(
                node_id=node_id,
                expected_case_count=expected_count,
            ),
            self.dependencies,
        )
