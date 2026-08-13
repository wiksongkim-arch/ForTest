"""Task-scoped PRD-to-test-case generation orchestration."""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.ai.base import AIProvider
from backend.ai.images import ImageWorkspace
from backend.ai.types import (
    CASE_OUTPUT_SCHEMA,
    TEST_CASE_FIELDS,
    ProviderUsage,
    SectionAIRequest,
    SectionAIResult,
    StageEvidence,
)
from backend.security.redaction import redact_text
from backend.settings.models import ProviderName, SettingsSnapshot
from services.dingtalk_mcp import DingTalkMCPService
from services.dingtalk_output import (
    DingTalkOutputWriter,
    OutputWriteResult,
    OutputWriteError,
)
from services.requirement_documents import (
    DingTalkRequirementDocumentReader,
    RequirementDocumentGateway,
    RequirementDocumentReader,
    RequirementDocumentSource,
)
from utils.template_loader import TemplateLoader


ProgressCallback = Callable[[str], None]
DocumentNameCallback = Callable[[str], None]
CompletedBlockCallback = Callable[[int, int], None]
CancellationCheck = Callable[[], bool]
_MARKDOWN_IMAGE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*"
    r"(?:<(?P<angle>[^>\r\n]+)>|(?P<plain>[^\s)\r\n]+))"
    r"(?:\s+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\([^\)\r\n]*\)))?"
    r"\s*\)"
)
_HTML_IMAGE = re.compile(
    r'<img[^>]+src=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_HTML_IMAGE_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SECTION_GROUP_MAX_CHARS = 12_000
_SECTION_GROUP_MAX_IMAGES = 5
_SECTION_GROUP_MAX_REQUIREMENT_LINES = 24


class GenerationCancelledError(RuntimeError):
    """任务被用户主动停止时使用的内部控制流异常。"""


class TestCaseGenerator:
    """Generate cases using only immutable task state and injected services."""

    def __init__(
        self,
        snapshot: SettingsSnapshot,
        provider: AIProvider,
        template_loader: TemplateLoader,
        document_service: DingTalkMCPService | None,
        image_workspace: ImageWorkspace,
        output_writer: Any,
        requirement_reader: RequirementDocumentReader | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.provider = provider
        self.template_loader = template_loader
        self.document_service = document_service
        if requirement_reader is None:
            if document_service is None:
                raise ValueError("需求文档读取能力不能为空")
            requirement_reader = RequirementDocumentGateway(
                {"link": DingTalkRequirementDocumentReader(document_service)}
            )
        self.requirement_reader = requirement_reader
        self.image_workspace = image_workspace
        self.output_writer = output_writer
        self.log_messages: list[str] = []
        self.provider_evidence: list[StageEvidence] = []
        self.fallback_count = 0
        self.ai_case_count = 0
        self.total_sections = 0
        self._duration_ms = 0
        self._retry_count = 0
        self._runtime_mode, self._model = self._provider_defaults()
        self._closed = False
        self._cancellation_check: CancellationCheck = lambda: False

    def add_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_messages.append(f"[{timestamp}] {redact_text(message)}")

    def get_logs(self) -> list[str]:
        return list(self.log_messages)

    def clear_logs(self) -> None:
        self.log_messages.clear()

    def close(self) -> None:
        """Release only generator-owned image state; worker owns Provider."""
        if self._closed:
            return
        self._closed = True
        try:
            self.image_workspace.close()
        except Exception:
            pass

    def generate(
        self,
        document_source: RequirementDocumentSource | str,
        progress_callback: ProgressCallback | None = None,
        document_name_callback: DocumentNameCallback | None = None,
        completed_block_callback: CompletedBlockCallback | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> dict[str, Any]:
        self._reset_run_state()
        self._cancellation_check = cancellation_check or (lambda: False)
        test_cases: list[dict[str, str]] = []
        try:
            self._raise_if_cancelled()
            self.add_log("开始生成测试用例")
            # 生成器只调用统一读取能力，不再区分本地文件或具体在线文档平台。
            document = self.requirement_reader.read(document_source)
            document_name = document.name
            for warning in document.warnings:
                self.add_log(warning)
            self._raise_if_cancelled()
            self._notify(document_name_callback, document_name)
            template = self._load_template()
            self._raise_if_cancelled()
            sections = self._sections_for_document(
                document.text,
                document_name,
                document.images,
            )
            if not sections:
                sections = [
                    {
                        "title": document_name or "通用模块",
                        "content": document.text,
                        "level": 0,
                    }
                ]

            prepared_sections = [
                (
                    section,
                    self._section_image_urls(
                        section,
                        tuple(section.get("_document_images", ())),
                    ),
                )
                for section in sections
            ]
            for index, (section, section_images) in enumerate(
                prepared_sections,
                start=1,
            ):
                self._raise_if_cancelled()
                self.total_sections += 1
                progress = f"[{index}/{len(sections)}] 区块: {section['title'][:30]}"
                self.add_log(progress)
                self._notify(progress_callback, progress)
                generated = self._process_section(
                    section,
                    section_images,
                    template,
                )
                self._raise_if_cancelled()
                test_cases.extend(generated)
                # 区块处理完成后再发布完成数；开始日志继续保持原有时机和格式。
                self._notify_completed(
                    completed_block_callback,
                    index,
                    len(sections),
                )

            test_cases = self._normalize_and_number(test_cases)
            self._raise_if_cancelled()
            output = self.output_writer.write(
                document_name,
                test_cases,
                self.snapshot.settings.document.local_output_dir,
            )
            return self._success_result(output, test_cases)
        except GenerationCancelledError:
            self.add_log("任务已停止")
            raise
        except OutputWriteError as exc:
            self.add_log("输出未完成")
            return self._failure_result(
                test_case_count=len(test_cases),
                error=exc.redacted_detail,
                node_id=exc.node_id,
                doc_url=exc.doc_url,
            )
        except Exception as exc:
            self.add_log(f"生成失败（{type(exc).__name__}）")
            return self._failure_result(
                test_case_count=len(test_cases),
                error=f"生成失败（{type(exc).__name__}）",
            )

    def _reset_run_state(self) -> None:
        self.clear_logs()
        self.provider_evidence.clear()
        self.fallback_count = 0
        self.ai_case_count = 0
        self.total_sections = 0
        self._duration_ms = 0
        self._retry_count = 0
        self._runtime_mode, self._model = self._provider_defaults()

    def _load_template(self) -> dict[str, Any]:
        try:
            loaded = self.template_loader.load_template(force_refresh=True)
            if not isinstance(loaded, dict):
                raise TypeError("template")
            field_specs = loaded.get("field_specs")
            components = loaded.get("components")
            if not isinstance(field_specs, dict) or not isinstance(
                components, dict
            ):
                raise TypeError("template")
            return {
                "field_specs": dict(field_specs),
                "components": {
                    str(name): list(items)
                    for name, items in components.items()
                    if isinstance(name, str) and isinstance(items, list)
                },
            }
        except Exception:
            self.add_log("模板加载失败，将按区块使用安全降级")
            return {"field_specs": {}, "components": {}}

    def _process_section(
        self,
        section: dict[str, Any],
        image_urls: tuple[str, ...],
        template: dict[str, Any],
    ) -> list[dict[str, Any]]:
        local_images: tuple[Path, ...] = ()
        if image_urls:
            try:
                local_images = tuple(self.image_workspace.download_many(image_urls))
            except Exception:
                self._raise_if_cancelled()
                self.add_log("区块图片下载失败，继续处理文本需求")

        self._raise_if_cancelled()

        prompts = self.snapshot.settings.prompts.model_dump(mode="python")
        components = template["components"]
        request = SectionAIRequest(
            section_title=str(section.get("title", "")),
            section_content=str(section.get("content", "")),
            images=local_images,
            component_names=tuple(components),
            field_specs=dict(template["field_specs"]),
            component_templates={
                name: [dict(item) for item in items if isinstance(item, dict)]
                for name, items in components.items()
            },
            prompts=prompts,
            output_schema=CASE_OUTPUT_SCHEMA,
        )

        try:
            result = self.provider.process_section(request)
        except Exception as exc:
            self._raise_if_cancelled()
            safe_detail = redact_text(str(exc)).strip()
            reason = f"AI Provider 调用失败（{type(exc).__name__}）"
            if safe_detail:
                reason = f"{reason}：{safe_detail[:200]}"
            return self._fallback_for_section(section, reason)

        self._raise_if_cancelled()

        reason = self._invalid_result_reason(result, set(components))
        if reason:
            return self._fallback_for_section(section, reason)

        valid_cases = [
            case for case in result.test_cases if self._is_valid_case(case)
        ]
        if not valid_cases:
            return self._fallback_for_section(
                section,
                "AI Provider 未生成有效用例",
            )

        self._duration_ms += max(0, int(result.duration_ms))
        self._retry_count += max(0, int(result.retry_count))
        self._runtime_mode = str(result.runtime_mode)
        self._model = str(result.model)
        accepted: list[dict[str, Any]] = []
        for case in valid_cases:
            copied = dict(case)
            if not self._to_text(copied.get("module")):
                copied["module"] = str(section.get("title", ""))[:50]
            accepted.append(copied)
        self.provider_evidence.extend(result.evidence)
        self.ai_case_count += len(accepted)
        return accepted

    def _raise_if_cancelled(self) -> None:
        """在安全边界检查停止请求，避免停止后继续生成或写出结果。"""

        try:
            cancelled = bool(self._cancellation_check())
        except Exception:
            cancelled = False
        if cancelled:
            raise GenerationCancelledError("任务已停止")

    def _invalid_result_reason(
        self,
        result: object,
        known_components: set[str],
    ) -> str | None:
        if not isinstance(result, SectionAIResult):
            return "AI Provider 返回类型无效"
        if result.provider != self.provider.name:
            return "AI Provider 身份不匹配"
        if result.output_valid is not True:
            return "AI Provider 输出校验失败"
        if (
            type(result.duration_ms) is not int
            or result.duration_ms < 0
            or type(result.retry_count) is not int
            or result.retry_count < 0
            or not isinstance(result.runtime_mode, str)
            or not result.runtime_mode
            or not isinstance(result.model, str)
            or not result.model
        ):
            return "AI Provider 元数据无效"
        if not isinstance(result.matched_components, list) or not (
            result.matched_components
        ):
            return "未匹配到已知组件"
        if not any(
            isinstance(name, str) and name in known_components
            for name in result.matched_components
        ):
            return "未匹配到已知组件"
        if not isinstance(result.test_cases, list) or not result.test_cases:
            return "AI Provider 未生成有效用例"
        if not isinstance(result.evidence, list) or not all(
            self._is_valid_evidence(item, result)
            for item in result.evidence
        ):
            return "AI Provider 证据格式无效"
        return None

    @staticmethod
    def _is_valid_evidence(
        item: object,
        result: SectionAIResult,
    ) -> bool:
        mixed = result.provider == ProviderName.mixed
        return (
            isinstance(item, StageEvidence)
            and (
                (mixed and item.provider != ProviderName.mixed)
                or item.provider == result.provider
            )
            and isinstance(item.stage, str)
            and bool(item.stage)
            and isinstance(item.runtime_mode, str)
            and bool(item.runtime_mode)
            and (mixed or item.runtime_mode == result.runtime_mode)
            and isinstance(item.model, str)
            and bool(item.model)
            and (mixed or item.model == result.model)
            and type(item.duration_ms) is int
            and item.duration_ms >= 0
            and type(item.retry_count) is int
            and item.retry_count >= 0
            and type(item.output_valid) is bool
        )

    @staticmethod
    def _is_valid_case(case: object) -> bool:
        expected_fields = set(TEST_CASE_FIELDS)
        return (
            isinstance(case, Mapping)
            and set(case) == expected_fields
            and all(
                value is None
                or isinstance(value, (str, int, float, bool))
                for value in case.values()
            )
        )

    def _fallback_for_section(
        self,
        section: dict[str, Any],
        reason: str,
    ) -> list[dict[str, Any]]:
        self.fallback_count += 1
        self.add_log(f"区块使用兜底逻辑：{reason}")
        cases = self._fallback_section_cases(section)
        if not cases:
            cases = self._fallback_generate_cases(
                str(section.get("content", "")),
                str(section.get("title", "")),
            )
        if not cases:
            cases = [self._create_default_test_case(str(section.get("title", "")))]
        return cases

    def _normalize_and_number(
        self,
        cases: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for index, case in enumerate(cases, start=1):
            row = {
                field: self._to_text(case.get(field))
                for field in TEST_CASE_FIELDS
            }
            row["case_id"] = f"TC-{index:03d}"
            normalized.append(row)
        if not normalized:
            fallback = self._create_default_test_case("通用功能")
            fallback["case_id"] = "TC-001"
            normalized.append(fallback)
        return normalized

    def _success_result(
        self,
        output: OutputWriteResult,
        cases: list[dict[str, str]],
    ) -> dict[str, Any]:
        partial = bool(output.partial_failure or self.fallback_count)
        if output.partial_failure:
            error = output.local_error
        elif self.fallback_count:
            error = f"{self.fallback_count} 个区块使用了兜底逻辑"
        else:
            error = None
        return {
            "success": not partial,
            "partial_failure": partial,
            "dingtalk_doc_url": output.dingtalk_doc_url,
            "node_id": output.node_id,
            "output_file_path": output.output_file_path,
            "test_cases_count": len(cases),
            "provider_usage": self._usage_dict(),
            "logs": self.get_logs(),
            "error": error,
        }

    def _failure_result(
        self,
        test_case_count: int,
        error: str,
        node_id: str | None = None,
        doc_url: str | None = None,
    ) -> dict[str, Any]:
        return {
            "success": False,
            "partial_failure": False,
            "dingtalk_doc_url": doc_url,
            "node_id": node_id,
            "output_file_path": None,
            "test_cases_count": test_case_count,
            "provider_usage": self._usage_dict(),
            "logs": self.get_logs(),
            "error": redact_text(error),
        }

    def _usage_dict(self) -> dict[str, Any]:
        usage = ProviderUsage(
            provider=self.provider.name,
            runtime_mode=self._runtime_mode,
            model=self._model,
            total_sections=self.total_sections,
            ai_case_count=self.ai_case_count,
            fallback_count=self.fallback_count,
            duration_ms=self._duration_ms,
            retry_count=self._retry_count,
        )
        payload = asdict(usage)
        payload["provider"] = usage.provider.value
        return payload

    def _provider_defaults(self) -> tuple[str, str]:
        name = self.provider.name
        ai = self.snapshot.settings.ai
        if name is ProviderName.codex:
            return ai.codex.runtime.value, ai.codex.model
        if name is ProviderName.minimax:
            return "http", ai.minimax.model
        if name is ProviderName.openai_compatible:
            return "http", ai.openai_compatible.model
        if name is ProviderName.mixed:
            return "capability-router", "configured-order"
        return "", ""

    @staticmethod
    def _to_text(value: object) -> str:
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)

    @staticmethod
    def _notify(callback: ProgressCallback | None, message: str) -> None:
        if callback is None:
            return
        try:
            callback(message)
        except Exception:
            pass

    @staticmethod
    def _notify_completed(
        callback: CompletedBlockCallback | None,
        completed: int,
        total: int,
    ) -> None:
        """发布准确完成数；展示层故障不得中断业务生成。"""

        if callback is None:
            return
        try:
            callback(completed, total)
        except Exception:
            pass

    @classmethod
    def _extract_images_from_text(cls, text: str) -> list[str]:
        markdown = [
            match.group("angle") or match.group("plain")
            for match in _MARKDOWN_IMAGE.finditer(text)
        ]
        found = [*markdown, *_HTML_IMAGE.findall(text)]
        return list(dict.fromkeys(found))

    @classmethod
    def _section_image_urls(
        cls,
        section: Mapping[str, Any],
        document_images: tuple[str, ...],
    ) -> tuple[str, ...]:
        inline = cls._extract_images_from_text(
            f"{section.get('title', '')}\n{section.get('content', '')}"
        )
        heading_images = section.get("_heading_images", ())
        if not isinstance(heading_images, (list, tuple)):
            heading_images = ()
        combined = [
            *(item for item in heading_images if isinstance(item, str)),
            *inline,
            *document_images,
        ]
        unique = tuple(dict.fromkeys(combined))
        if len(unique) > _SECTION_GROUP_MAX_IMAGES:
            raise ValueError("AI section image limit exceeded")
        return unique

    @classmethod
    def _sections_for_document(
        cls,
        text: str,
        document_name: str,
        document_images: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        preamble, sections = cls._split_lossless_by_headers(text)
        if sections:
            groups = cls._split_heading_sections(sections, preamble)
        elif preamble:
            groups = cls._coalesce_nested_sections(
                [
                    {
                        "title": document_name or "通用模块",
                        "content": preamble,
                        "level": 0,
                    }
                ]
            )
        else:
            groups = []
        return cls._allocate_document_images(
            groups,
            document_images,
        )

    @staticmethod
    def _split_lossless_by_headers(
        text: str,
        min_level: int = 1,
        max_level: int = 6,
    ) -> tuple[str, list[dict[str, Any]]]:
        preamble: list[str] = []
        sections: list[dict[str, Any]] = []
        current_title: str | None = None
        current_content: list[str] = []
        current_level = 0

        for line in text.splitlines():
            match = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
            if match and min_level <= len(match.group(1)) <= max_level:
                if current_title is not None:
                    sections.append(
                        {
                            "title": current_title,
                            "content": "\n".join(current_content).strip(),
                            "level": current_level,
                        }
                    )
                current_title = match.group(2).strip()
                current_level = len(match.group(1))
                current_content = []
            elif current_title is None:
                preamble.append(line)
            else:
                current_content.append(line)

        if current_title is not None:
            sections.append(
                {
                    "title": current_title,
                    "content": "\n".join(current_content).strip(),
                    "level": current_level,
                }
            )
        return "\n".join(preamble).strip(), sections

    @classmethod
    def _clean_heading(cls, title: str) -> str:
        readable = _MARKDOWN_IMAGE.sub("", title)
        readable = _HTML_IMAGE_TAG.sub("", readable)
        readable = re.sub(r"<[^>]+>", "", readable)
        readable = readable.replace("**", "").replace("__", "")
        readable = readable.replace("`", "").replace("\\+", "+")
        readable = re.sub(r"\\([|#*_{}\[\]()<>])", r"\1", readable)
        readable = re.sub(r"\s+", " ", readable).strip()
        return readable or "未命名区块"

    @classmethod
    def _split_heading_sections(
        cls,
        sections: list[dict[str, Any]],
        preamble: str = "",
    ) -> list[dict[str, Any]]:
        """Keep every Markdown heading as an independently testable unit.

        Heading paths retain parent context, while large or image-heavy bodies
        are split only at Markdown line boundaries.  This avoids collapsing a
        complete PRD under its shallowest headings and losing module detail.
        """

        groups: list[dict[str, Any]] = []
        heading_stack: list[tuple[int, str]] = []
        preamble_attached = False

        for section in sections:
            raw_title = str(section.get("title", ""))
            level = max(1, min(6, int(section.get("level", 1))))
            clean_title = cls._clean_heading(raw_title)
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, clean_title))
            path_title = " > ".join(title for _, title in heading_stack)
            content = str(section.get("content", ""))
            if not preamble_attached and preamble:
                content = f"{preamble}\n\n{content}" if content else preamble
                preamble_attached = True

            heading_images = tuple(cls._extract_images_from_text(raw_title))
            current_title = path_title
            current_lines: list[str] = []
            current_images = set(heading_images)
            current_requirement_lines = 0
            first_chunk = True

            if (
                cls._group_char_count(current_title, "", False)
                > _SECTION_GROUP_MAX_CHARS
                or len(current_images) > _SECTION_GROUP_MAX_IMAGES
            ):
                raise ValueError("AI section heading exceeds safe limits")

            def emit_chunk() -> None:
                nonlocal current_lines, current_images
                nonlocal current_requirement_lines, first_chunk
                payload: dict[str, Any] = {
                    "title": current_title,
                    "content": "\n".join(current_lines).strip(),
                    "level": level,
                }
                if first_chunk and heading_images:
                    payload["_heading_images"] = heading_images
                groups.append(payload)
                first_chunk = False
                current_lines = []
                current_images = set()
                current_requirement_lines = 0

            source_lines = content.splitlines()
            if not source_lines:
                emit_chunk()
                continue

            for line in source_lines:
                line_images = set(cls._extract_images_from_text(line))
                visible_line = _MARKDOWN_IMAGE.sub("", line)
                visible_line = _HTML_IMAGE_TAG.sub("", visible_line)
                is_requirement_line = bool(
                    re.sub(r"[\s|:#*`_-]", "", visible_line)
                )
                candidate_lines = [*current_lines, line]
                candidate_content = "\n".join(candidate_lines).strip()
                candidate_images = current_images | line_images
                candidate_requirement_lines = (
                    current_requirement_lines + int(is_requirement_line)
                )
                fits = (
                    cls._group_char_count(
                        current_title,
                        candidate_content,
                        bool(candidate_content),
                    )
                    <= _SECTION_GROUP_MAX_CHARS
                    and len(candidate_images) <= _SECTION_GROUP_MAX_IMAGES
                    and candidate_requirement_lines
                    <= _SECTION_GROUP_MAX_REQUIREMENT_LINES
                )
                if not fits and current_lines:
                    emit_chunk()
                    current_title = cls._continuation_title(path_title)
                    candidate_lines = [line]
                    candidate_content = line.strip()
                    candidate_images = line_images
                    candidate_requirement_lines = int(is_requirement_line)
                    fits = (
                        cls._group_char_count(
                            current_title,
                            candidate_content,
                            bool(candidate_content),
                        )
                        <= _SECTION_GROUP_MAX_CHARS
                        and len(candidate_images)
                        <= _SECTION_GROUP_MAX_IMAGES
                        and candidate_requirement_lines
                        <= _SECTION_GROUP_MAX_REQUIREMENT_LINES
                    )
                if not fits:
                    raise ValueError("AI section line exceeds safe limits")
                current_lines = candidate_lines
                current_images = candidate_images
                current_requirement_lines = candidate_requirement_lines

            emit_chunk()

        return groups

    @classmethod
    def _continuation_title(cls, title: str) -> str:
        readable = _MARKDOWN_IMAGE.sub(
            lambda match: match.group("alt").strip(),
            title,
        )
        readable = _HTML_IMAGE_TAG.sub("", readable)
        readable = re.sub(r"\s+", " ", readable).strip()
        return f"{readable or 'Continuation'}（续）"

    @staticmethod
    def _group_char_count(title: str, content: str, has_lines: bool) -> int:
        return len(title) + (1 if has_lines else 0) + len(content)

    @classmethod
    def _coalesce_nested_sections(
        cls,
        sections: list[dict[str, Any]],
        preamble: str = "",
    ) -> list[dict[str, Any]]:
        shallowest_level = min(int(section["level"]) for section in sections)
        groups: list[dict[str, Any]] = []
        root_title = ""
        root_level = shallowest_level
        group_title = ""
        group_content = ""
        group_has_lines = False
        group_active = False
        image_urls: set[str] = set()

        def emit_group() -> None:
            nonlocal group_active, group_content, group_has_lines
            if not group_active:
                return
            groups.append(
                {
                    "title": group_title,
                    "content": group_content,
                    "level": root_level,
                }
            )
            group_active = False
            group_content = ""
            group_has_lines = False
            image_urls.clear()

        def start_group(title: str) -> None:
            nonlocal group_active, group_title
            nonlocal group_content, group_has_lines
            group_title = title
            group_content = ""
            group_has_lines = False
            image_urls.clear()
            image_urls.update(cls._extract_images_from_text(title))
            if (
                cls._group_char_count(title, "", False)
                > _SECTION_GROUP_MAX_CHARS
                or len(image_urls) > _SECTION_GROUP_MAX_IMAGES
            ):
                raise ValueError("AI section heading exceeds safe limits")
            group_active = True

        def append_line(
            line: str,
            separator: str,
            context_title: str,
        ) -> None:
            nonlocal group_content, group_has_lines
            actual_separator = separator if group_has_lines else ""
            candidate_content = f"{group_content}{actual_separator}{line}"
            line_images = set(cls._extract_images_from_text(line))
            candidate_images = image_urls | line_images
            fits = (
                cls._group_char_count(group_title, candidate_content, True)
                <= _SECTION_GROUP_MAX_CHARS
                and len(candidate_images) <= _SECTION_GROUP_MAX_IMAGES
            )
            if not fits:
                emit_group()
                start_group(cls._continuation_title(context_title))
                candidate_content = line
                candidate_images = image_urls | line_images
                fits = (
                    cls._group_char_count(
                        group_title,
                        candidate_content,
                        True,
                    )
                    <= _SECTION_GROUP_MAX_CHARS
                    and len(candidate_images) <= _SECTION_GROUP_MAX_IMAGES
                )
                if not fits:
                    raise ValueError("AI section line exceeds safe limits")
            group_content = candidate_content
            group_has_lines = True
            image_urls.update(line_images)

        preamble_attached = False
        for section in sections:
            title = str(section["title"])
            content = str(section["content"])
            level = int(section["level"])
            starts_root = level == shallowest_level or not group_active
            preamble_before_body = False
            if starts_root:
                emit_group()
                root_title = title
                root_level = level
                start_group(root_title)
                if not preamble_attached:
                    for line in preamble.splitlines():
                        append_line(line, "\n", root_title)
                    preamble_attached = True
                    preamble_before_body = bool(preamble)
            else:
                section_lines = [
                    f"{'#' * level} {title}",
                    *content.splitlines(),
                ]
                rendered_section = "\n".join(section_lines)
                section_images = set(
                    cls._extract_images_from_text(rendered_section)
                )
                candidate_separator = "\n\n" if group_has_lines else ""
                candidate_content = (
                    f"{group_content}{candidate_separator}{rendered_section}"
                )
                candidate_fits = (
                    cls._group_char_count(
                        group_title,
                        candidate_content,
                        True,
                    )
                    <= _SECTION_GROUP_MAX_CHARS
                    and len(image_urls | section_images)
                    <= _SECTION_GROUP_MAX_IMAGES
                )
                continuation = cls._continuation_title(title)
                section_fits_fresh = (
                    cls._group_char_count(
                        continuation,
                        rendered_section,
                        True,
                    )
                    <= _SECTION_GROUP_MAX_CHARS
                    and len(
                        set(cls._extract_images_from_text(continuation))
                        | section_images
                    )
                    <= _SECTION_GROUP_MAX_IMAGES
                )
                if not candidate_fits and section_fits_fresh:
                    emit_group()
                    start_group(continuation)
                append_line(
                    f"{'#' * level} {title}",
                    "\n\n",
                    title,
                )

            for index, line in enumerate(content.splitlines()):
                separator = "\n"
                if preamble_before_body and index == 0:
                    separator = "\n\n"
                append_line(line, separator, title)

        emit_group()
        return groups

    @classmethod
    def _allocate_document_images(
        cls,
        sections: list[dict[str, Any]],
        document_images: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        allocated = [dict(section) for section in sections]
        inline_seen: set[str] = set()
        for section in allocated:
            inline_seen.update(
                cls._section_image_urls(section, ())
            )

        ordered_document_images = list(
            dict.fromkeys(
                image
                for image in document_images
                if isinstance(image, str) and image
            )
        )
        pending = [
            image for image in ordered_document_images if image not in inline_seen
        ]
        cursor = 0
        for section in allocated:
            inline = cls._section_image_urls(section, ())
            capacity = _SECTION_GROUP_MAX_IMAGES - len(inline)
            assigned = tuple(pending[cursor : cursor + capacity])
            cursor += len(assigned)
            if assigned:
                section["_document_images"] = assigned

        while cursor < len(pending):
            assigned = tuple(
                pending[cursor : cursor + _SECTION_GROUP_MAX_IMAGES]
            )
            cursor += len(assigned)
            allocated.append(
                {
                    "title": "补充图片",
                    "content": "",
                    "level": 0,
                    "_document_images": assigned,
                }
            )

        for section in allocated:
            cls._section_image_urls(
                section,
                tuple(section.get("_document_images", ())),
            )
        return allocated

    @staticmethod
    def _split_by_headers(
        text: str,
        min_level: int = 1,
        max_level: int = 6,
    ) -> list[dict[str, Any]]:
        sections: list[dict[str, Any]] = []
        current_title = ""
        current_content: list[str] = []
        current_level = 0
        for line in text.splitlines():
            match = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
            if match and min_level <= len(match.group(1)) <= max_level:
                if current_title:
                    content = "\n".join(current_content).strip()
                    if content:
                        sections.append(
                            {
                                "title": current_title,
                                "content": content,
                                "level": current_level,
                            }
                        )
                current_title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
                current_level = len(match.group(1))
                current_content = []
            else:
                current_content.append(line)
        if current_title:
            content = "\n".join(current_content).strip()
            if content:
                sections.append(
                    {
                        "title": current_title,
                        "content": content,
                        "level": current_level,
                    }
                )
        return sections

    @staticmethod
    def _fallback_generate_cases(text: str, module: str) -> list[dict[str, str]]:
        candidates = re.findall(r"[\u4e00-\u9fff]{4,50}", text)
        cases: list[dict[str, str]] = []
        for candidate in candidates[:10]:
            if any(word in candidate for word in ("规则", "限制", "说明", "标注")):
                continue
            cases.append(
                TestCaseGenerator._case(
                    module=module or "通用模块",
                    name=candidate[:50],
                    steps=f"1. 进入对应功能页面\n2. 验证{candidate}功能",
                    expected=f"{candidate}功能正常",
                )
            )
        return cases

    @staticmethod
    def _fallback_section_cases(section: Mapping[str, Any]) -> list[dict[str, str]]:
        title = str(section.get("title", ""))
        content = re.sub(r"<[^>]+>", "", str(section.get("content", "")))
        features: list[str] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if re.match(r"^[-*]\s+", line):
                feature = re.sub(r"^[-*]\s+", "", line).strip()
            elif re.match(r"^\d+[.、]\s*", line):
                feature = re.sub(r"^\d+[.、]\s*", "", line).strip()
            else:
                continue
            if len(feature) > 2:
                features.append(feature)
        return [
            TestCaseGenerator._case(
                module=title,
                name=f"【{title}】- {feature[:30]}",
                steps=(
                    f"1. 进入【{title}】页面\n"
                    f"2. 执行操作：{feature}"
                ),
                expected=f"{feature}执行成功",
            )
            for feature in features
        ]

    @staticmethod
    def _case(
        module: str,
        name: str,
        steps: str,
        expected: str,
    ) -> dict[str, str]:
        return {
            "module": module[:50],
            "case_name": name,
            "prerequisite": "系统正常启动，相关配置已设置",
            "test_steps": steps,
            "expected_result": expected,
            "priority": "中",
            "case_type": "功能测试",
            "applicable_phase": "系统测试",
            "remark": "自动生成",
            "case_id": "",
            "execution": "未执行",
        }

    @staticmethod
    def _create_default_test_case(module: str) -> dict[str, str]:
        return TestCaseGenerator._case(
            module=module or "通用功能",
            name="基础功能验证",
            steps="1. 进入对应功能页面\n2. 验证页面元素\n3. 执行基本操作",
            expected="功能正常运行",
        )
