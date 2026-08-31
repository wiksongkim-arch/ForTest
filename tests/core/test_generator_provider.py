import inspect
import threading
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from backend.ai.registry import ProviderDecision
from backend.ai.types import (
    CASE_OUTPUT_SCHEMA,
    TEST_CASE_FIELDS,
    ProviderUsage,
    SectionAIResult,
    StageEvidence,
)
from backend.api import routes
from backend.api.routes import GenerateRequest
from backend.api.settings_routes import RuntimeDependencies
from backend.core.generator import TestCaseGenerator
from backend.settings.defaults import default_settings
from backend.settings.models import (
    AppSettings,
    ProviderName,
    ResolvedSecrets,
    SettingsSnapshot,
)
from services.dingtalk_output import OutputWriteResult


PRD_URL = (
    "https://alidocs.dingtalk.com/i/nodes/"
    "Y1OQX0akWmbeRQlBsvab9OyOVGlDd3mE"
)


def snapshot(
    *,
    provider=ProviderName.codex,
    document_url="https://old.example.test/docs",
    spreadsheet_url="https://old.example.test/sheets",
    provider_key="old-provider-secret",
):
    settings_payload = default_settings().model_dump(mode="python")
    settings_payload["ai"]["active_provider"] = provider
    # 产品默认值刻意不含业务模板；生成链路测试显式模拟用户已完成配置。
    settings_payload["document"].update(
        {
            "content_template_url": "https://example.test/content-template",
            "document_template_url": "https://example.test/document-template",
            "output_folder_url": "https://example.test/output-folder",
        }
    )
    settings = AppSettings.model_validate(settings_payload)
    secrets = {
        "document_mcp_url": document_url,
        "spreadsheet_mcp_url": spreadsheet_url,
    }
    if provider is ProviderName.codex:
        secrets["codex_api_key"] = provider_key
    elif provider is ProviderName.minimax:
        secrets["minimax_api_key"] = provider_key
    else:
        secrets["openai_compatible_api_key"] = provider_key
    return SettingsSnapshot(
        settings=settings,
        secrets=ResolvedSecrets(**secrets),
    )


def complete_case(**overrides):
    case = dict(zip(TEST_CASE_FIELDS, ["value"] * len(TEST_CASE_FIELDS)))
    case.update(overrides)
    return case


def section_result(
    *,
    cases=None,
    components=None,
    valid=True,
    duration=25,
    retries=0,
):
    cases = [complete_case()] if cases is None else cases
    components = ["按钮"] if components is None else components
    return SectionAIResult(
        provider=ProviderName.codex,
        runtime_mode="sdk",
        model="gpt-5.4",
        duration_ms=duration,
        retry_count=retries,
        output_valid=valid,
        matched_components=components,
        test_cases=cases,
        evidence=[
            StageEvidence(
                stage="case_generation",
                provider=ProviderName.codex,
                runtime_mode="sdk",
                model="gpt-5.4",
                duration_ms=duration,
                retry_count=retries,
                output_valid=valid,
                turn_id="turn-cases",
            )
        ],
    )


def fake_provider(result=None):
    provider = Mock()
    provider.name = ProviderName.codex
    provider.process_section.return_value = result or section_result()
    return provider


def fake_template_loader():
    loader = Mock()
    loader.load_template.return_value = {
        "field_specs": {field: field for field in TEST_CASE_FIELDS},
        "components": {
            "按钮": [{"用例步骤": "点击", "预期结果": "成功"}],
            "输入框": [{"用例步骤": "输入", "预期结果": "显示"}],
        },
    }
    return loader


def fake_document_service(text="## 登录\n- 正确账号密码登录成功"):
    service = Mock()
    service.get_document_content.return_value = {
        "texts": text,
        "images": [f"https://img.example.test/{index}.png" for index in range(5)],
        "files": [],
        "format": "markdown",
    }
    service.get_document_name.return_value = "登录需求"
    return service


def fake_image_workspace():
    workspace = Mock()
    workspace.download_many.return_value = tuple(
        Path(f"D:/safe/{index}.png") for index in range(5)
    )
    return workspace


def fake_output_writer(partial_failure=False):
    writer = Mock()
    writer.write.return_value = OutputWriteResult(
        dingtalk_doc_url=(
            "https://alidocs.dingtalk.com/i/nodes/output-node"
        ),
        node_id="output-node",
        output_file_path=(
            None if partial_failure else "D:/tmp/登录需求-用例.xlsx"
        ),
        partial_failure=partial_failure,
        local_error="本地备份写入失败（OSError）"
        if partial_failure
        else None,
    )
    return writer


def make_generator(
    *,
    provider=None,
    template_loader=None,
    document_service=None,
    image_workspace=None,
    output_writer=None,
    task_snapshot=None,
):
    return TestCaseGenerator(
        snapshot=task_snapshot or snapshot(),
        provider=provider or fake_provider(),
        template_loader=template_loader or fake_template_loader(),
        document_service=document_service or fake_document_service(),
        image_workspace=image_workspace or fake_image_workspace(),
        output_writer=output_writer or fake_output_writer(),
    )


class GeneratorProviderTests(unittest.TestCase):
    def test_document_is_read_once_before_template_loading(self):
        events = []
        names = []
        document = fake_document_service()
        template = fake_template_loader()
        content = document.get_document_content.return_value
        document_name = document.get_document_name.return_value
        template_value = template.load_template.return_value
        document.get_document_name.side_effect = lambda _url: (
            events.append("document_name"),
            document_name,
        )[1]
        template.load_template.side_effect = lambda **_kwargs: (
            events.append("template"),
            template_value,
        )[1]
        document.get_document_content.side_effect = lambda _url: (
            events.append("document_content"),
            content,
        )[1]

        result = make_generator(
            document_service=document,
            template_loader=template,
        ).generate(PRD_URL, document_name_callback=names.append)

        self.assertTrue(result["success"])
        self.assertEqual(names, ["登录需求"])
        self.assertEqual(
            events[:3],
            ["document_name", "document_content", "template"],
        )
        document.get_document_name.assert_called_once_with(PRD_URL)
        document.get_document_content.assert_called_once_with(PRD_URL)

    def test_document_name_callback_failure_does_not_fail_generation(self):
        def broken_callback(_name):
            raise RuntimeError("界面回调失败")

        result = make_generator().generate(
            PRD_URL,
            document_name_callback=broken_callback,
        )

        self.assertTrue(result["success"])

    def test_completed_block_is_published_only_after_section_finishes(self):
        section_started = threading.Event()
        section_release = threading.Event()
        completed = []
        result_holder = []
        provider = fake_provider()

        def process_section(_request):
            section_started.set()
            section_release.wait(1)
            return section_result()

        provider.process_section.side_effect = process_section
        generator = make_generator(provider=provider)
        worker = threading.Thread(
            target=lambda: result_holder.append(
                generator.generate(
                    PRD_URL,
                    completed_block_callback=(
                        lambda current, total: completed.append(
                            (current, total)
                        )
                    ),
                )
            ),
            daemon=True,
        )
        self.addCleanup(section_release.set)
        worker.start()

        self.assertTrue(section_started.wait(1))
        self.assertEqual(completed, [])
        section_release.set()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(result_holder[0]["success"])
        self.assertEqual(completed, [(1, 1)])

    def test_completed_block_callback_failure_does_not_fail_generation(self):
        def broken_callback(_current, _total):
            raise RuntimeError("完成进度回调失败")

        result = make_generator().generate(
            PRD_URL,
            completed_block_callback=broken_callback,
        )

        self.assertTrue(result["success"])

    def test_markdown_image_optional_title_is_not_part_of_signed_url(self):
        signed_url = (
            "https://signed.example.test/object.png?Expires=123&"
            "OSSAccessKeyId=test&Signature=abc%2Bdef%3D"
        )

        extracted = TestCaseGenerator._extract_images_from_text(
            f'![architecture]({signed_url} "")'
        )

        self.assertEqual(extracted, [signed_url])
        self.assertNotIn(' ""', extracted[0])

    def test_nested_sections_keep_each_heading_and_parent_path(self):
        text = """# Root A
ROOT_BODY_A_101
## Child A
CHILD_BODY_A_202
### Grandchild A
GRAND_BODY_A_303
# Root B
ROOT_BODY_B_404
## Child B
CHILD_BODY_B_505"""

        parsed = TestCaseGenerator._split_by_headers(text)
        sections = TestCaseGenerator._sections_for_document(text, "Document")

        self.assertEqual(len(parsed), 5)
        self.assertEqual(len(sections), 5)
        self.assertEqual(
            [section["title"] for section in sections],
            [
                "Root A",
                "Root A > Child A",
                "Root A > Child A > Grandchild A",
                "Root B",
                "Root B > Child B",
            ],
        )
        combined = "\n".join(section["content"] for section in sections)
        for original in parsed:
            self.assertEqual(combined.count(original["content"]), 1)

    def test_flat_same_level_sections_remain_independent(self):
        text = """## One
one-body
## Two
two-body
## Three
three-body"""

        parsed = TestCaseGenerator._split_by_headers(text)

        self.assertEqual(
            TestCaseGenerator._sections_for_document(text, "Document"),
            parsed,
        )

    def test_heading_paths_are_clean_and_keep_levels_one_through_six(self):
        text = (
            "# **Root**\nroot\n"
            "### <span>Child</span> ![screen](https://img.example.test/a.png)\n"
            "child\n###### `Leaf`\nleaf"
        )

        sections = TestCaseGenerator._sections_for_document(text, "Document")

        self.assertEqual(
            [section["title"] for section in sections],
            ["Root", "Root > Child", "Root > Child > Leaf"],
        )
        self.assertEqual(sections[1]["_heading_images"], (
            "https://img.example.test/a.png",
        ))

    def test_heading_section_splits_after_24_requirement_lines(self):
        text = "# Dense\n" + "\n".join(
            f"- requirement-{index}" for index in range(25)
        )

        sections = TestCaseGenerator._sections_for_document(text, "Document")

        self.assertEqual(len(sections), 2)
        self.assertEqual(len(sections[0]["content"].splitlines()), 24)
        self.assertTrue(sections[1]["title"].endswith("（续）"))

    def test_43_nested_headings_remain_43_granular_groups(self):
        lines = []
        descendants_per_root = (14, 13, 13)
        body_tokens = []
        for root_index, descendant_count in enumerate(
            descendants_per_root,
            start=1,
        ):
            root_body = f"root-body-{root_index}-unique"
            body_tokens.append(root_body)
            lines.extend((f"# Root {root_index}", root_body))
            for child_index in range(1, descendant_count + 1):
                level = 2 + ((child_index - 1) % 4)
                child_body = (
                    f"child-body-{root_index}-{child_index}-unique"
                )
                body_tokens.append(child_body)
                lines.extend(
                    (
                        f"{'#' * level} Child {root_index}.{child_index}",
                        child_body,
                    )
                )
        text = "\n".join(lines)

        parsed = TestCaseGenerator._split_by_headers(text)
        sections = TestCaseGenerator._sections_for_document(text, "Document")

        self.assertEqual(len(parsed), 43)
        self.assertEqual(len(sections), 43)
        combined = "\n".join(section["content"] for section in sections)
        for token in body_tokens:
            self.assertEqual(combined.count(token), 1)

    def test_nested_groups_split_at_section_boundary_before_12000_chars(self):
        root_body = "R" * 4000
        first_child_body = "A" * 4000
        second_child_body = "B" * 4000
        text = (
            f"# Root\n{root_body}\n"
            f"## First child\n{first_child_body}\n"
            f"## Second child\n{second_child_body}"
        )

        sections = TestCaseGenerator._sections_for_document(text, "Document")

        self.assertEqual(len(sections), 3)
        self.assertTrue(
            all(len(section["content"]) <= 12_000 for section in sections)
        )
        self.assertEqual(
            [section["title"] for section in sections],
            ["Root", "Root > First child", "Root > Second child"],
        )
        combined = "\n".join(section["content"] for section in sections)
        for body in (root_body, first_child_body, second_child_body):
            self.assertEqual(combined.count(body), 1)

    def test_nested_groups_split_before_sixth_unique_image(self):
        image_urls = [
            f"https://img.example.test/nested-{index}.png"
            for index in range(7)
        ]
        root_images = " ".join(
            f"![root-{index}]({image_urls[index]})" for index in range(2)
        )
        first_child_images = " ".join(
            f"![first-{index}]({image_urls[index]})" for index in range(2, 5)
        )
        second_child_images = " ".join(
            f"![second-{index}]({image_urls[index]})" for index in range(5, 7)
        )
        text = (
            f"# Root\nroot-body {root_images}\n"
            f"## First child\nfirst-child-body {first_child_images}\n"
            f"## Second child\nsecond-child-body {second_child_images}"
        )

        sections = TestCaseGenerator._sections_for_document(text, "Document")
        visible_by_group = [
            TestCaseGenerator._section_image_urls(section, ())
            for section in sections
        ]

        self.assertEqual(len(sections), 3)
        self.assertTrue(all(len(urls) <= 5 for urls in visible_by_group))
        self.assertEqual(
            {url for urls in visible_by_group for url in urls},
            set(image_urls),
        )
        combined = "\n".join(section["content"] for section in sections)
        for image_url in image_urls:
            self.assertEqual(combined.count(image_url), 1)

    def test_root_heading_images_are_visible_and_budgeted_in_continuations(self):
        image_urls = [
            f"https://img.example.test/heading-{index}.png"
            for index in range(7)
        ]
        text = (
            f"# Root ![root-0]({image_urls[0]}) "
            f"![root-1]({image_urls[1]})\n"
            f"root-body ![root-body]({image_urls[2]})\n"
            f"## First child ![first-title]({image_urls[3]})\n"
            f"first-body ![first-body]({image_urls[4]})\n"
            f"## Second child ![second-title]({image_urls[5]})\n"
            f"second-body ![second-body]({image_urls[6]})"
        )

        sections = TestCaseGenerator._sections_for_document(text, "Document")
        visible_by_group = [
            TestCaseGenerator._section_image_urls(section, ())
            for section in sections
        ]

        self.assertEqual(len(sections), 3)
        self.assertTrue(all(len(urls) <= 5 for urls in visible_by_group))
        self.assertEqual(
            {url for urls in visible_by_group for url in urls},
            set(TestCaseGenerator._extract_images_from_text(text)),
        )
        self.assertEqual(
            set(TestCaseGenerator._extract_images_from_text(text)),
            set(image_urls),
        )

    def test_lossless_ai_parse_keeps_preamble_and_empty_image_headings(self):
        root_url = "https://img.example.test/empty-root.png"
        child_url = "https://img.example.test/empty-child.png"
        text = (
            "PREAMBLE_TOKEN_101\n\n"
            f"# Root ![root diagram]({root_url} \"\")\n"
            f"## Empty child ![child diagram]({child_url} \"\")\n"
            "### Ordinary empty heading\n"
            "## Child with body\n"
            "BODY_TOKEN_202"
        )

        legacy = TestCaseGenerator._split_by_headers(text)
        sections = TestCaseGenerator._sections_for_document(text, "Document")
        combined = "\n".join(
            f"{section['title']}\n{section['content']}"
            for section in sections
        )
        visible = {
            url
            for section in sections
            for url in TestCaseGenerator._section_image_urls(section, ())
        }

        self.assertEqual(
            legacy,
            [
                {
                    "title": "Child with body",
                    "content": "BODY_TOKEN_202",
                    "level": 2,
                }
            ],
        )
        self.assertEqual(len(sections), 4)
        self.assertEqual(combined.count("PREAMBLE_TOKEN_101"), 1)
        self.assertEqual(combined.count("BODY_TOKEN_202"), 1)
        self.assertIn("Root > Empty child", combined)
        self.assertIn("Root > Empty child > Ordinary empty heading", combined)
        self.assertEqual(visible, {root_url, child_url})

    def test_atomic_seven_image_section_splits_only_between_markdown_lines(self):
        image_urls = [
            f"https://img.example.test/atomic-{index}.png"
            for index in range(7)
        ]
        source_lines = [
            f"LINE_A ![a0]({image_urls[0]}) ![a1]({image_urls[1]})",
            f"LINE_B ![b]({image_urls[2]})",
            f"LINE_C ![c]({image_urls[3]})",
            f"LINE_D ![d]({image_urls[4]})",
            f"LINE_E ![e0]({image_urls[5]}) ![e1]({image_urls[6]})",
        ]
        text = "# Root\n" + "\n".join(source_lines)

        sections = TestCaseGenerator._sections_for_document(text, "Document")
        visible_by_group = [
            TestCaseGenerator._section_image_urls(section, ())
            for section in sections
        ]
        combined = "\n".join(section["content"] for section in sections)

        self.assertEqual(len(sections), 2)
        self.assertTrue(all(len(urls) <= 5 for urls in visible_by_group))
        self.assertEqual(
            {url for urls in visible_by_group for url in urls},
            set(image_urls),
        )
        for line in source_lines:
            self.assertEqual(combined.count(line), 1)

    def test_atomic_multiline_section_splits_without_cutting_a_line(self):
        source_lines = [character * 5_000 for character in ("A", "B", "C")]
        text = "# Root\n" + "\n".join(source_lines)

        sections = TestCaseGenerator._sections_for_document(text, "Document")
        combined_lines = [
            line
            for section in sections
            for line in section["content"].splitlines()
        ]

        self.assertEqual(len(sections), 2)
        self.assertTrue(
            all(
                TestCaseGenerator._group_char_count(
                    section["title"],
                    section["content"],
                    bool(section["content"].splitlines()),
                )
                <= 12_000
                for section in sections
            )
        )
        self.assertEqual(combined_lines, source_lines)

    def test_root_title_with_five_images_does_not_starve_child_image(self):
        image_urls = [
            f"https://img.example.test/title-limit-{index}.png"
            for index in range(6)
        ]
        root_title = "Root " + " ".join(
            f"![diagram-{index}]({image_urls[index]})"
            for index in range(5)
        )
        text = (
            f"# {root_title}\n"
            "## Child\n"
            f"child-body ![child]({image_urls[5]})"
        )

        sections = TestCaseGenerator._sections_for_document(text, "Document")
        visible_by_group = [
            TestCaseGenerator._section_image_urls(section, ())
            for section in sections
        ]

        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0]["title"], "Root")
        self.assertEqual(sections[1]["title"], "Root > Child")
        self.assertNotIn("![", sections[1]["title"])
        self.assertNotIn("https://", sections[1]["title"])
        self.assertIn("Child", sections[1]["title"])
        self.assertTrue(all(len(urls) <= 5 for urls in visible_by_group))
        self.assertEqual(
            {url for urls in visible_by_group for url in urls},
            set(image_urls),
        )

    def test_split_child_body_keeps_child_heading_context_in_every_group(self):
        image_urls = [
            f"https://img.example.test/child-context-{index}.png"
            for index in range(6)
        ]
        child_lines = [
            f"CHILD_LINE_{index} ![child-{index}]({image_urls[index]})"
            for index in range(6)
        ]
        text = (
            "# Root\nROOT_BODY\n"
            "## Child semantic heading\n"
            + "\n".join(child_lines)
        )

        sections = TestCaseGenerator._sections_for_document(text, "Document")

        self.assertEqual(len(sections), 3)
        for child_line in child_lines:
            containing = [
                section
                for section in sections
                if child_line in section["content"]
            ]
            self.assertEqual(len(containing), 1)
            context = (
                f"{containing[0]['title']}\n{containing[0]['content']}"
            )
            self.assertIn("Child semantic heading", context)

    def test_unsplittable_line_or_title_fails_before_any_ai_call(self):
        oversized_line = "X" * 12_001
        oversized_title = "T" * 12_001
        six_title_images = " ".join(
            f"![title-{index}](https://img.example.test/title-{index}.png)"
            for index in range(6)
        )
        six_line_images = " ".join(
            f"![line-{index}](https://img.example.test/line-{index}.png)"
            for index in range(6)
        )
        for text in (
            f"# Root\n{oversized_line}",
            f"# {oversized_title}\nbody",
            f"# Root {six_title_images}\nbody",
            f"# Root\n{six_line_images}",
        ):
            with self.subTest(text_length=len(text)):
                with self.assertRaises(ValueError):
                    TestCaseGenerator._sections_for_document(text, "Document")

        provider = fake_provider()
        document = fake_document_service(f"# Root\n{oversized_line}")

        result = make_generator(
            provider=provider,
            document_service=document,
        ).generate(PRD_URL)

        self.assertFalse(result["success"])
        provider.process_section.assert_not_called()

    def test_document_images_fill_group_capacity_then_use_supplement_groups(self):
        inline_urls = [
            f"https://img.example.test/inline-{index}.png"
            for index in range(6)
        ]
        document_only = [
            f"https://img.example.test/document-{index}.png"
            for index in range(6)
        ]
        text = (
            "# First\n"
            + " ".join(
                f"![first-{index}]({inline_urls[index]})"
                for index in range(2)
            )
            + "\n# Second\n"
            + " ".join(
                f"![second-{index}]({inline_urls[index]})"
                for index in range(2, 6)
            )
        )
        supplied = (
            inline_urls[0],
            *document_only,
            document_only[0],
        )

        sections = TestCaseGenerator._sections_for_document(
            text,
            "Document",
            tuple(supplied),
        )
        visible_by_group = [
            TestCaseGenerator._section_image_urls(
                section,
                tuple(section.get("_document_images", ())),
            )
            for section in sections
        ]

        self.assertEqual(len(sections), 3)
        self.assertTrue(all(len(urls) <= 5 for urls in visible_by_group))
        self.assertEqual(
            {url for urls in visible_by_group for url in urls},
            {*inline_urls, *document_only},
        )
        self.assertEqual(
            [
                tuple(section.get("_document_images", ()))
                for section in sections
            ],
            [
                tuple(document_only[:3]),
                (document_only[3],),
                tuple(document_only[4:]),
            ],
        )

    def test_generate_downloads_every_document_image_in_bounded_groups(self):
        document_urls = tuple(
            f"https://img.example.test/document-live-{index}.png"
            for index in range(12)
        )
        document = fake_document_service("# One\none\n# Two\ntwo")
        document.get_document_content.return_value["images"] = list(document_urls)
        workspace = Mock()
        downloaded: list[tuple[str, ...]] = []

        def download_many(urls):
            downloaded.append(tuple(urls))
            return tuple(
                Path(f"D:/safe/group-{len(downloaded)}-{index}.png")
                for index, _url in enumerate(urls)
            )

        workspace.download_many.side_effect = download_many
        provider = fake_provider()

        result = make_generator(
            provider=provider,
            document_service=document,
            image_workspace=workspace,
        ).generate(PRD_URL)

        self.assertTrue(result["success"])
        self.assertEqual(provider.process_section.call_count, 3)
        self.assertEqual([len(group) for group in downloaded], [5, 5, 2])
        self.assertEqual(
            tuple(url for group in downloaded for url in group),
            document_urls,
        )
        self.assertTrue(
            all(
                len(call.args[0].images) <= 5
                for call in provider.process_section.call_args_list
            )
        )

    def test_generator_uses_one_provider_call_with_full_frozen_request(self):
        provider = fake_provider(
            section_result(
                cases=[
                    complete_case(
                        module="",
                        case_id="UNTRUSTED",
                        priority=1,
                        execution=None,
                    )
                ],
                duration=31,
                retries=1,
            )
        )
        images = fake_image_workspace()
        output = fake_output_writer()
        generator = make_generator(
            provider=provider,
            image_workspace=images,
            output_writer=output,
        )

        result = generator.generate(PRD_URL)

        self.assertTrue(result["success"])
        provider.process_section.assert_called_once()
        request = provider.process_section.call_args.args[0]
        self.assertEqual(
            request.component_templates,
            fake_template_loader().load_template.return_value["components"],
        )
        self.assertEqual(request.output_schema, CASE_OUTPUT_SCHEMA)
        self.assertEqual(len(request.images), 5)
        images.download_many.assert_called_once_with(
            tuple(f"https://img.example.test/{index}.png" for index in range(5))
        )
        written_cases = output.write.call_args.args[1]
        self.assertEqual(written_cases[0]["module"], "登录")
        self.assertEqual(written_cases[0]["case_id"], "TC-001")
        self.assertEqual(written_cases[0]["priority"], "1")
        self.assertEqual(written_cases[0]["execution"], "")
        self.assertTrue(
            all(
                isinstance(written_cases[0][field], str)
                for field in TEST_CASE_FIELDS
            )
        )
        self.assertEqual(
            result["provider_usage"],
            {
                "provider": "codex",
                "runtime_mode": "sdk",
                "model": "gpt-5.4",
                "total_sections": 1,
                "ai_case_count": 1,
                "fallback_count": 0,
                "duration_ms": 31,
                "retry_count": 1,
            },
        )

    def test_generator_keeps_available_images_and_records_partial_download(self):
        provider = fake_provider()
        images = fake_image_workspace()
        images.download_many.return_value = (Path("D:/safe/available.png"),)
        generator = make_generator(provider=provider, image_workspace=images)

        result = generator.generate(PRD_URL)

        self.assertTrue(result["success"])
        request = provider.process_section.call_args.args[0]
        self.assertEqual(request.images, (Path("D:/safe/available.png"),))
        self.assertTrue(
            any(
                "区块图片部分下载失败（成功 1/5）" in line
                for line in result["logs"]
            )
        )

    def test_unknown_component_crossing_provider_boundary_falls_back_once(self):
        provider = fake_provider(section_result(components=["伪造组件"]))
        generator = make_generator(provider=provider)

        result = generator.generate(PRD_URL)

        provider.process_section.assert_called_once()
        self.assertEqual(result["provider_usage"]["total_sections"], 1)
        self.assertEqual(result["provider_usage"]["fallback_count"], 1)
        fallback_logs = [
            line for line in result["logs"] if "兜底" in line
        ]
        self.assertEqual(len(fallback_logs), 1)

    def test_known_component_intersection_survives_unknown_provider_values(self):
        provider = fake_provider(
            section_result(components=["按钮", "伪造组件"])
        )

        result = make_generator(provider=provider).generate(PRD_URL)

        self.assertTrue(result["success"])
        self.assertEqual(result["provider_usage"]["ai_case_count"], 1)
        self.assertEqual(result["provider_usage"]["fallback_count"], 0)

    def test_mixed_cases_keep_valid_items_and_do_not_fallback(self):
        invalid = complete_case()
        invalid.pop("execution")
        valid = complete_case(case_name="保留的合法用例")
        provider = fake_provider(
            section_result(cases=[invalid, valid])
        )
        output = fake_output_writer()

        result = make_generator(
            provider=provider,
            output_writer=output,
        ).generate(PRD_URL)

        self.assertTrue(result["success"])
        self.assertEqual(result["provider_usage"]["ai_case_count"], 1)
        self.assertEqual(result["provider_usage"]["fallback_count"], 0)
        self.assertEqual(
            output.write.call_args.args[1][0]["case_name"],
            "保留的合法用例",
        )

    def test_rejected_result_does_not_pollute_usage_or_evidence(self):
        rejected = section_result(
            valid=False,
            duration=999,
            retries=7,
        )
        rejected.runtime_mode = "cli"
        rejected.model = "untrusted-model"
        generator = make_generator(provider=fake_provider(rejected))

        result = generator.generate(PRD_URL)

        self.assertEqual(
            result["provider_usage"],
            {
                "provider": "codex",
                "runtime_mode": "auto",
                "model": "gpt-5.4",
                "total_sections": 1,
                "ai_case_count": 0,
                "fallback_count": 1,
                "duration_ms": 0,
                "retry_count": 0,
            },
        )
        self.assertEqual(generator.provider_evidence, [])

    def test_malformed_result_metadata_and_evidence_are_rejected_safely(self):
        malformed_metrics = section_result()
        malformed_metrics.duration_ms = "token=metadata-secret"
        malformed_evidence = section_result()
        malformed_evidence.evidence = None

        for malformed in (malformed_metrics, malformed_evidence):
            with self.subTest(value=malformed):
                generator = make_generator(
                    provider=fake_provider(malformed)
                )
                result = generator.generate(PRD_URL)
                self.assertFalse(result["success"])
                self.assertTrue(result["partial_failure"])
                self.assertEqual(
                    result["provider_usage"]["fallback_count"],
                    1,
                )
                self.assertEqual(
                    result["provider_usage"]["duration_ms"],
                    0,
                )
                self.assertEqual(generator.provider_evidence, [])
                self.assertNotIn(
                    "metadata-secret",
                    "\n".join(result["logs"]),
                )

    def test_incomplete_case_crossing_provider_boundary_falls_back(self):
        invalid = complete_case()
        invalid.pop("execution")
        provider = fake_provider(section_result(cases=[invalid]))

        result = make_generator(provider=provider).generate(PRD_URL)

        self.assertEqual(result["provider_usage"]["ai_case_count"], 0)
        self.assertEqual(result["provider_usage"]["fallback_count"], 1)

    def test_provider_exception_is_safe_and_counted_once(self):
        provider = fake_provider()
        provider.process_section.side_effect = RuntimeError(
            "token=provider-ultra-secret"
        )

        result = make_generator(provider=provider).generate(PRD_URL)

        self.assertEqual(result["provider_usage"]["fallback_count"], 1)
        self.assertNotIn("provider-ultra-secret", "\n".join(result["logs"]))

    def test_plain_text_is_one_counted_section(self):
        document = fake_document_service("用户可以使用正确密码登录系统。")

        result = make_generator(document_service=document).generate(PRD_URL)

        self.assertEqual(
            result["provider_usage"],
            {
                "provider": "codex",
                "runtime_mode": "sdk",
                "model": "gpt-5.4",
                "total_sections": 1,
                "ai_case_count": 1,
                "fallback_count": 0,
                "duration_ms": 25,
                "retry_count": 0,
            },
        )

    def test_multiple_sections_aggregate_only_accepted_usage_and_number_once(self):
        accepted = section_result(
            cases=[complete_case(case_name="AI 用例")],
            duration=12,
            retries=1,
        )
        rejected = section_result(
            components=[],
            duration=900,
            retries=8,
        )
        provider = fake_provider()
        provider.process_section.side_effect = (accepted, rejected)
        output = fake_output_writer()
        document = fake_document_service(
            "## 登录\n- 登录成功\n## 退出\n- 退出成功"
        )

        generator = make_generator(
            provider=provider,
            output_writer=output,
            document_service=document,
        )
        result = generator.generate(PRD_URL)

        self.assertEqual(provider.process_section.call_count, 2)
        self.assertEqual(
            result["provider_usage"],
            {
                "provider": "codex",
                "runtime_mode": "sdk",
                "model": "gpt-5.4",
                "total_sections": 2,
                "ai_case_count": 1,
                "fallback_count": 1,
                "duration_ms": 12,
                "retry_count": 1,
            },
        )
        written = output.write.call_args.args[1]
        self.assertEqual(
            [item["case_id"] for item in written],
            [f"TC-{index:03d}" for index in range(1, len(written) + 1)],
        )
        self.assertEqual(
            len([line for line in result["logs"] if "兜底" in line]),
            1,
        )
        self.assertEqual(generator.provider_evidence, accepted.evidence)

    def test_partial_local_output_is_not_reported_as_success(self):
        result = make_generator(
            output_writer=fake_output_writer(partial_failure=True)
        ).generate(PRD_URL)

        self.assertFalse(result["success"])
        self.assertTrue(result["partial_failure"])
        self.assertEqual(
            result["dingtalk_doc_url"],
            "https://alidocs.dingtalk.com/i/nodes/output-node",
        )
        self.assertEqual(result["node_id"], "output-node")

    def test_close_only_closes_image_workspace(self):
        provider = fake_provider()
        images = fake_image_workspace()
        generator = make_generator(provider=provider, image_workspace=images)

        generator.close()
        generator.close()

        images.close.assert_called_once()
        provider.close.assert_not_called()

    def test_generator_source_has_no_root_config_or_minimax_factory(self):
        source = inspect.getsource(routes.TestCaseGenerator)
        module_source = inspect.getsource(
            __import__("backend.core.generator", fromlist=["*"])
        )
        self.assertNotIn("create_minimax_service", module_source)
        self.assertNotIn("from config import", module_source)
        self.assertNotIn("SettingsService", source)


class APIGenerationContractTests(unittest.TestCase):
    def setUp(self):
        with routes._tasks_lock:
            routes._generation_tasks.clear()

    def tearDown(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with routes._tasks_lock:
                running = any(
                    task.status in {"pending", "running"}
                    for task in routes._generation_tasks.values()
                )
            if not running:
                break
            time.sleep(0.01)
        with routes._tasks_lock:
            routes._generation_tasks.clear()

    def test_generate_request_forbids_legacy_output_folder(self):
        with self.assertRaises(ValidationError):
            GenerateRequest.model_validate(
                {"doc_url": PRD_URL, "output_folder": "legacy-secret"}
            )

    def test_build_generator_uses_one_complete_snapshot_tuple(self):
        task_snapshot = snapshot()
        provider = fake_provider()
        captured = {}

        class Document:
            def __init__(self, url):
                captured["document"] = url

        class Sheets:
            def __init__(self, url):
                captured["spreadsheet"] = url

        with (
            patch.object(routes, "DingTalkMCPService", Document),
            patch.object(routes, "DingTalkSpreadSheetMCPService", Sheets),
            patch.object(routes, "TemplateLoader") as template,
            patch.object(routes, "DingTalkOutputWriter") as output,
            patch.object(routes, "ImageWorkspace") as images,
            patch.object(routes, "TestCaseGenerator") as generator,
        ):
            built = routes.build_generator(task_snapshot, provider)

        self.assertIs(built, generator.return_value)
        self.assertEqual(captured["document"], "https://old.example.test/docs")
        self.assertEqual(
            captured["spreadsheet"], "https://old.example.test/sheets"
        )
        template.assert_called_once_with(
            task_snapshot.settings.document.content_template_url,
            unittest.mock.ANY,
            local_template_path=None,
        )
        output.assert_called_once()
        generator.assert_called_once_with(
            snapshot=task_snapshot,
            provider=provider,
            template_loader=template.return_value,
            document_service=unittest.mock.ANY,
            image_workspace=images.return_value,
            output_writer=output.return_value,
            requirement_reader=unittest.mock.ANY,
        )

    def test_build_generator_closes_image_workspace_when_construction_fails(self):
        image_workspace = Mock()
        with (
            patch.object(
                routes,
                "ImageWorkspace",
                return_value=image_workspace,
            ),
            patch.object(
                routes,
                "TestCaseGenerator",
                side_effect=RuntimeError("constructor secret"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                routes.build_generator(snapshot(), fake_provider())

        image_workspace.close.assert_called_once()

    def test_build_failure_closes_returned_provider_and_creates_no_task(self):
        task_snapshot = snapshot()
        service = MutableSnapshotService(task_snapshot)
        registry = CapturingRegistry()
        dependencies = RuntimeDependencies(
            service=service,
            registry=registry,
            document_factory=Mock(),
            spreadsheet_factory=Mock(),
        )
        with patch.object(
            routes,
            "build_generator",
            side_effect=RuntimeError("token=build-secret"),
        ):
            with self.assertRaises(HTTPException) as raised:
                routes.start_generate(
                    GenerateRequest(doc_url=PRD_URL),
                    dependencies,
                )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertNotIn("build-secret", str(raised.exception.detail))
        self.assertEqual(registry.providers[0].closed, 1)
        self.assertEqual(routes._generation_tasks, {})

    def test_provider_unavailable_creates_no_generator_or_task(self):
        dependencies = RuntimeDependencies(
            service=MutableSnapshotService(snapshot()),
            registry=UnavailableRegistry(),
            document_factory=Mock(),
            spreadsheet_factory=Mock(),
        )
        with patch.object(routes, "build_generator") as build:
            with self.assertRaises(HTTPException) as raised:
                routes.start_generate(
                    GenerateRequest(doc_url=PRD_URL),
                    dependencies,
                )

        self.assertEqual(raised.exception.status_code, 502)
        build.assert_not_called()
        self.assertEqual(routes._generation_tasks, {})

    def test_thread_start_failure_closes_both_resources_once(self):
        registry = CapturingRegistry()
        dependencies = RuntimeDependencies(
            service=MutableSnapshotService(snapshot()),
            registry=registry,
            document_factory=Mock(),
            spreadsheet_factory=Mock(),
        )
        generator = BlockingGenerator(snapshot(), None)
        with (
            patch.object(routes, "build_generator", return_value=generator),
            patch.object(
                routes.threading.Thread,
                "start",
                side_effect=RuntimeError("thread secret"),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                routes.start_generate(
                    GenerateRequest(doc_url=PRD_URL),
                    dependencies,
                )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertNotIn("thread secret", str(raised.exception.detail))
        self.assertEqual(generator.closed, 1)
        self.assertEqual(registry.providers[0].closed, 1)
        self.assertEqual(routes._generation_tasks, {})

    def test_worker_terminal_statuses_close_resources_and_clear_snapshot(self):
        cases = (
            (True, False, "completed"),
            (False, True, "partial_failure"),
            (False, False, "failed"),
        )
        for success, partial, expected_status in cases:
            with self.subTest(status=expected_status):
                registry = CapturingRegistry()
                dependencies = RuntimeDependencies(
                    service=MutableSnapshotService(snapshot()),
                    registry=registry,
                    document_factory=Mock(),
                    spreadsheet_factory=Mock(),
                )
                generator = BlockingGenerator(
                    snapshot(),
                    None,
                    result_flags=(success, partial),
                )
                with patch.object(
                    routes,
                    "build_generator",
                    return_value=generator,
                ):
                    task_id = routes.start_generate(
                        GenerateRequest(doc_url=PRD_URL),
                        dependencies,
                    )["task_id"]
                    self.assertEqual(
                        self._wait_for(task_id),
                        expected_status,
                    )

                public = routes.get_task_status(task_id)
                self.assertEqual(
                    set(public),
                    {
                        "status",
                        "task_name",
                        "result",
                        "logs",
                        "error",
                        "current_block",
                        "completed_block",
                        "total_blocks",
                    },
                )
                self.assertEqual(public["task_name"], "登录需求")
                self.assertEqual(public["completed_block"], 1)
                self.assertNotIn("old-provider-secret", repr(public))
                self.assertNotIn("provider_decision", repr(public))
                self.assertEqual(generator.closed, 1)
                self.assertEqual(registry.providers[0].closed, 1)
                with routes._tasks_lock:
                    self.assertIsNone(
                        routes._generation_tasks[task_id].snapshot
                    )
                    routes._generation_tasks.pop(task_id)

    def test_generator_close_failure_cannot_skip_provider_close(self):
        registry = CapturingRegistry()
        dependencies = RuntimeDependencies(
            service=MutableSnapshotService(snapshot()),
            registry=registry,
            document_factory=Mock(),
            spreadsheet_factory=Mock(),
        )
        generator = BlockingGenerator(
            snapshot(),
            None,
            close_error=True,
        )
        with patch.object(
            routes,
            "build_generator",
            return_value=generator,
        ):
            task_id = routes.start_generate(
                GenerateRequest(doc_url=PRD_URL),
                dependencies,
            )["task_id"]
            self.assertEqual(self._wait_for(task_id), "completed")

        self.assertEqual(generator.closed, 1)
        self.assertEqual(registry.providers[0].closed, 1)

    def test_terminal_state_is_published_only_after_close_and_snapshot_clear(self):
        close_started = threading.Event()
        close_release = threading.Event()
        registry = CapturingRegistry()
        dependencies = RuntimeDependencies(
            service=MutableSnapshotService(snapshot()),
            registry=registry,
            document_factory=Mock(),
            spreadsheet_factory=Mock(),
        )
        generator = BlockingGenerator(
            snapshot(),
            None,
            close_started=close_started,
            close_release=close_release,
        )
        with patch.object(
            routes,
            "build_generator",
            return_value=generator,
        ):
            task_id = routes.start_generate(
                GenerateRequest(doc_url=PRD_URL),
                dependencies,
            )["task_id"]
            self.assertTrue(close_started.wait(1))
            self.assertEqual(
                routes.get_task_status(task_id)["status"],
                "running",
            )
            close_release.set()
            self.assertEqual(self._wait_for(task_id), "completed")

        with routes._tasks_lock:
            self.assertIsNone(routes._generation_tasks[task_id].snapshot)
        self.assertEqual(registry.providers[0].closed, 1)

    def test_pending_running_completed_and_not_found_are_observable(self):
        original_thread = threading.Thread
        release = threading.Event()
        started = threading.Event()
        registry = CapturingRegistry()
        dependencies = RuntimeDependencies(
            service=MutableSnapshotService(snapshot()),
            registry=registry,
            document_factory=Mock(),
            spreadsheet_factory=Mock(),
        )
        generator = BlockingGenerator(
            snapshot(),
            None,
            release=release,
            started=started,
        )
        deferred = []

        class DeferredThread:
            def __init__(self, target, **_kwargs):
                self.target = target
                deferred.append(self)

            def start(self):
                return None

        with (
            patch.object(routes, "build_generator", return_value=generator),
            patch.object(routes.threading, "Thread", DeferredThread),
        ):
            task_id = routes.start_generate(
                GenerateRequest(doc_url=PRD_URL),
                dependencies,
            )["task_id"]
        self.assertEqual(
            routes.get_task_status(task_id)["status"],
            "pending",
        )

        worker = original_thread(target=deferred[0].target, daemon=True)
        worker.start()
        self.assertTrue(started.wait(1))
        running = routes.get_task_status(task_id)
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["task_name"], "登录需求")
        self.assertEqual(running["current_block"], 1)
        self.assertEqual(running["completed_block"], 0)
        self.assertEqual(running["total_blocks"], 1)
        release.set()
        worker.join(1)
        completed = routes.get_task_status(task_id)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["completed_block"], 1)
        self.assertEqual(completed["total_blocks"], 1)
        self.assertEqual(
            routes.get_task_status("missing"),
            {"status": "not_found", "result": None},
        )

    def test_only_terminal_tasks_can_be_discarded(self):
        release = threading.Event()
        started = threading.Event()
        registry = CapturingRegistry()
        dependencies = RuntimeDependencies(
            service=MutableSnapshotService(snapshot()),
            registry=registry,
            document_factory=Mock(),
            spreadsheet_factory=Mock(),
        )
        generator = BlockingGenerator(
            snapshot(),
            None,
            release=release,
            started=started,
        )
        with patch.object(routes, "build_generator", return_value=generator):
            task_id = routes.start_generate(
                GenerateRequest(doc_url=PRD_URL),
                dependencies,
            )["task_id"]
            self.assertTrue(started.wait(1))
            self.assertFalse(routes.discard_terminal_task(task_id))
            release.set()
            self.assertEqual(self._wait_for(task_id), "completed")

        self.assertTrue(routes.discard_terminal_task(task_id))
        self.assertFalse(routes.discard_terminal_task(task_id))
        self.assertEqual(
            routes.get_task_status(task_id),
            {"status": "not_found", "result": None},
        )

    def test_running_task_can_be_stopped_and_released(self):
        release = threading.Event()
        started = threading.Event()
        registry = CapturingRegistry()
        dependencies = RuntimeDependencies(
            service=MutableSnapshotService(snapshot()),
            registry=registry,
            document_factory=Mock(),
            spreadsheet_factory=Mock(),
        )
        generator = BlockingGenerator(
            snapshot(),
            None,
            release=release,
            started=started,
        )
        with patch.object(routes, "build_generator", return_value=generator):
            task_id = routes.start_generate(
                GenerateRequest(doc_url=PRD_URL),
                dependencies,
            )["task_id"]
            self.assertTrue(started.wait(1))
            response = routes.stop_generation_task(task_id)
            self.assertTrue(response["stopped"])
            self.assertEqual(response["status"], "stopped")
            self.assertEqual(self._wait_for(task_id), "stopped")

        public = routes.get_task_status(task_id)
        self.assertEqual(public["status"], "stopped")
        self.assertEqual(public["logs"][-1], "任务已停止")
        self.assertIsNone(public["error"])
        deadline = time.monotonic() + 1
        while generator.closed == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(routes.discard_terminal_task(task_id))
        self.assertEqual(generator.closed, 1)
        self.assertEqual(registry.providers[0].closed, 1)

    def test_task_keeps_original_snapshot_after_settings_change(self):
        old = snapshot()
        new = snapshot(
            provider=ProviderName.minimax,
            document_url="https://new.example.test/docs",
            spreadsheet_url="https://new.example.test/sheets",
            provider_key="new-provider-secret",
        )
        service = MutableSnapshotService(old)
        registry = CapturingRegistry()
        dependencies = RuntimeDependencies(
            service=service,
            registry=registry,
            document_factory=Mock(),
            spreadsheet_factory=Mock(),
        )
        release = threading.Event()
        first_started = threading.Event()
        built = []

        def factory(task_snapshot, provider, **_kwargs):
            generator = BlockingGenerator(
                task_snapshot,
                provider,
                release if not built else None,
                first_started if not built else None,
            )
            built.append(generator)
            return generator

        with patch.object(routes, "build_generator", side_effect=factory):
            first_id = routes.start_generate(
                GenerateRequest(doc_url=PRD_URL), dependencies
            )["task_id"]
            self.assertTrue(first_started.wait(1))
            service.current = new
            second_id = routes.start_generate(
                GenerateRequest(doc_url=PRD_URL), dependencies
            )["task_id"]
            release.set()
            self._wait_for(second_id)
            self._wait_for(first_id)

        self.assertEqual(
            registry.captured,
            [snapshot_tuple(old), snapshot_tuple(new)],
        )
        self.assertEqual(
            [snapshot_tuple(item.task_snapshot) for item in built],
            [snapshot_tuple(old), snapshot_tuple(new)],
        )
        for task_id in (first_id, second_id):
            public = routes.get_task_status(task_id)
            text = repr(public)
            self.assertNotIn("provider-secret", text)
            self.assertNotIn("example.test/docs", text)
            with routes._tasks_lock:
                self.assertIsNone(
                    routes._generation_tasks[task_id].snapshot
                )

    def _wait_for(self, task_id):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = routes.get_task_status(task_id)["status"]
            if status not in {"pending", "running"}:
                return status
            time.sleep(0.01)
        self.fail(f"task {task_id} did not finish")


def snapshot_tuple(value):
    provider = value.settings.ai.active_provider
    secret_name = {
        ProviderName.codex: "codex_api_key",
        ProviderName.minimax: "minimax_api_key",
        ProviderName.openai_compatible: "openai_compatible_api_key",
    }[provider]
    return (
        provider,
        value.secrets.reveal(secret_name),
        value.secrets.reveal("document_mcp_url"),
        value.secrets.reveal("spreadsheet_mcp_url"),
    )


class MutableSnapshotService:
    def __init__(self, current):
        self.current = current

    def snapshot(self):
        return self.current


class CapturingProvider:
    def __init__(self, task_snapshot):
        self.name = task_snapshot.settings.ai.active_provider
        self.task_snapshot = task_snapshot
        self.closed = 0

    def close(self):
        self.closed += 1


class CapturingRegistry:
    def __init__(self):
        self.captured = []
        self.providers = []

    def create_for_task(self, task_snapshot):
        self.captured.append(snapshot_tuple(task_snapshot))
        provider = CapturingProvider(task_snapshot)
        self.providers.append(provider)
        return provider, ProviderDecision(
            selected=provider.name,
            requested=provider.name,
            used_fallback=False,
            reason="healthy",
        )


class UnavailableRegistry:
    def create_for_task(self, _task_snapshot):
        from backend.ai.base import ProviderUnavailableError

        raise ProviderUnavailableError("token=provider-secret")


class BlockingGenerator:
    def __init__(
        self,
        task_snapshot,
        provider,
        release=None,
        started=None,
        result_flags=(True, False),
        close_error=False,
        close_started=None,
        close_release=None,
    ):
        self.task_snapshot = task_snapshot
        self.provider = provider
        self.release = release
        self.started = started
        self.closed = 0
        self.result_flags = result_flags
        self.close_error = close_error
        self.close_started = close_started
        self.close_release = close_release

    def add_log(self, _message):
        pass

    def generate(
        self,
        _doc_url,
        progress_callback=None,
        document_name_callback=None,
        completed_block_callback=None,
        cancellation_check=None,
    ):
        if document_name_callback is not None:
            document_name_callback("登录需求")
        if progress_callback is not None:
            progress_callback("[1/1] 区块: 登录")
        if self.started:
            self.started.set()
        if self.release:
            deadline = time.monotonic() + 1
            while not self.release.wait(0.01) and time.monotonic() < deadline:
                if cancellation_check is not None and cancellation_check():
                    raise routes.GenerationCancelledError("任务已停止")
        if completed_block_callback is not None:
            completed_block_callback(1, 1)
        provider_name = (
            self.provider.name
            if self.provider is not None
            else ProviderName.codex
        )
        usage = ProviderUsage(
            provider=provider_name,
            runtime_mode="fake",
            model="fake",
            total_sections=1,
            ai_case_count=1,
            fallback_count=0,
            duration_ms=1,
            retry_count=0,
        )
        return {
            "success": self.result_flags[0],
            "partial_failure": self.result_flags[1],
            "dingtalk_doc_url": "https://alidocs.dingtalk.com/i/nodes/out",
            "node_id": "out",
            "output_file_path": "D:/safe/out.xlsx",
            "test_cases_count": 1,
            "provider_usage": {
                **asdict(usage),
                "provider": usage.provider.value,
            },
            "logs": [],
        }

    def close(self):
        self.closed += 1
        if self.close_started is not None:
            self.close_started.set()
        if self.close_release is not None:
            self.close_release.wait(1)
        if self.close_error:
            raise RuntimeError("generator close secret")


if __name__ == "__main__":
    unittest.main()
