"""本地需求文档到默认模板输出的端到端生成契约。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from backend.ai.types import (
    TEST_CASE_FIELDS,
    SectionAIResult,
    StageEvidence,
)
from backend.api.routes import GenerateRequest, build_generator
from backend.settings.defaults import default_settings
from backend.settings.models import (
    AppSettings,
    ProviderName,
    ResolvedSecrets,
    SettingsSnapshot,
)
from services.requirement_documents import RequirementDocumentSource
from utils.default_templates import DefaultTemplateManager


def _local_snapshot(output_dir: Path) -> SettingsSnapshot:
    payload = default_settings().model_dump(mode="python")
    payload["document"].update(
        {
            "content_template_url": "",
            "document_template_url": "",
            "output_folder_url": "",
            "local_output_dir": str(output_dir),
        }
    )
    return SettingsSnapshot(
        settings=AppSettings.model_validate(payload),
        secrets=ResolvedSecrets(),
    )


def _provider() -> Mock:
    case = dict(zip(TEST_CASE_FIELDS, ["本地值"] * len(TEST_CASE_FIELDS)))
    provider = Mock()
    provider.name = ProviderName.codex
    provider.process_section.return_value = SectionAIResult(
        provider=ProviderName.codex,
        runtime_mode="sdk",
        model="gpt-5.6-terra",
        duration_ms=10,
        retry_count=0,
        output_valid=True,
        matched_components=["功能按钮"],
        test_cases=[case],
        evidence=[
            StageEvidence(
                stage="case_generation",
                provider=ProviderName.codex,
                runtime_mode="sdk",
                model="gpt-5.6-terra",
                duration_ms=10,
                retry_count=0,
                output_valid=True,
                turn_id="local-turn",
            ),
        ],
    )
    return provider


def test_generate_request_normalizes_selected_file_to_unified_source(tmp_path):
    requirement = tmp_path / "退款需求.md"
    requirement.write_text("# 退款\n支持用户申请退款。", encoding="utf-8")

    request = GenerateRequest(
        source_type="file",
        document_source=str(requirement),
    )

    assert request.doc_url == ""
    assert request.document_source == str(requirement.resolve())
    assert request.source_reference() == RequirementDocumentSource(
        "file",
        str(requirement.resolve()),
    )


def test_local_document_generates_xlsx_without_online_document_services(
    tmp_path,
):
    requirement = tmp_path / "登录需求.md"
    requirement.write_text(
        "# 登录\n用户输入正确账号密码后进入首页。",
        encoding="utf-8",
    )
    source = RequirementDocumentSource.create("file", requirement)
    template_paths = DefaultTemplateManager(tmp_path / "user-data").ensure_all()
    provider = _provider()
    online_factory = Mock(side_effect=AssertionError("不应连接在线文档服务"))
    generator = build_generator(
        _local_snapshot(tmp_path / "output"),
        provider,
        source=source,
        default_template_paths=template_paths,
        document_factory=online_factory,
        spreadsheet_factory=online_factory,
    )

    try:
        result = generator.generate(source)
    finally:
        generator.close()

    assert result["success"] is True
    assert result["dingtalk_doc_url"] is None
    assert result["test_cases_count"] == 1
    assert Path(result["output_file_path"]).is_file()
    assert Path(result["output_file_path"]).parent == (tmp_path / "output")
    online_factory.assert_not_called()
