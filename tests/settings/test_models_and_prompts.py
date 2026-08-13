import unittest

from pydantic import ValidationError

from backend.settings.defaults import DEFAULT_PROMPTS, default_settings
from backend.settings.models import (
    DocumentSettings,
    OpenAICompatibleSettings,
    PromptCustomOption,
    PromptLibrarySettings,
    PromptSlot,
    ProviderName,
)
from backend.settings.prompt_library import resolve_prompt_settings
from backend.settings.prompts import PromptCatalog, PromptValidationError


class SettingsModelTests(unittest.TestCase):
    def test_defaults_select_codex_and_keep_four_prompts(self):
        settings = default_settings()
        self.assertEqual(settings.ai.active_provider, ProviderName.codex)
        self.assertEqual(settings.schema_version, 5)
        self.assertEqual(settings.document.content_template_url, "")
        self.assertEqual(settings.document.document_template_url, "")
        self.assertEqual(settings.document.output_folder_url, "")
        self.assertEqual(settings.ai.configurations, ())
        self.assertEqual(len(settings.prompts.model_dump()), 4)
        self.assertEqual(
            settings.prompts.model_dump(),
            DEFAULT_PROMPTS,
        )
        for slot in settings.prompt_library:
            self.assertEqual(slot[1].selected_option_id, "default")
            self.assertEqual(slot[1].custom_options, ())

    def test_prompt_library_resolves_selected_custom_without_copying_defaults(self):
        custom = PromptCustomOption(
            id="custom-1",
            name="我的提示词",
            content="自定义 {requirement} {component_names}",
        )
        library = PromptLibrarySettings(
            component_matching=PromptSlot(
                selected_option_id=custom.id,
                custom_options=(custom,),
            )
        )

        resolved = resolve_prompt_settings(library)

        self.assertEqual(resolved.component_matching, custom.content)
        self.assertEqual(
            resolved.image_understanding,
            DEFAULT_PROMPTS["image_understanding"],
        )
        self.assertNotIn(
            DEFAULT_PROMPTS["image_understanding"],
            str(library.model_dump()),
        )

    def test_prompt_option_models_reject_empty_and_inconsistent_state(self):
        for field, value in (
            ("id", " "),
            ("name", " "),
            ("content", " \n "),
        ):
            payload = {
                "id": "custom-1",
                "name": "名称",
                "content": "内容",
            }
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                PromptCustomOption(**payload)

        with self.assertRaises(ValidationError):
            PromptCustomOption(
                id="default",
                name="伪默认",
                content="内容",
            )
        with self.assertRaises(ValidationError):
            PromptSlot(selected_option_id="missing")

    def test_dingtalk_urls_reject_non_https(self):
        settings = default_settings()
        payload = settings.document.model_dump()
        payload["output_folder_url"] = "http://example.test/folder"
        with self.assertRaises(ValidationError):
            DocumentSettings.model_validate(payload)

    def test_required_prompt_variable_is_enforced(self):
        with self.assertRaises(PromptValidationError) as raised:
            PromptCatalog.validate(
                "component_matching",
                "需求：{requirement}",
            )
        self.assertIn("component_names", str(raised.exception))

    def test_prompt_render_rejects_unknown_variable(self):
        with self.assertRaises(PromptValidationError):
            PromptCatalog.render(
                "component_matching",
                "{requirement} {component_names} {unknown}",
                requirement="R",
                component_names="- A",
            )

    def test_nested_unknown_prompt_variable_is_rejected_by_validation(self):
        template = "{requirement:{unknown}} {component_names}"

        with self.assertRaises(PromptValidationError) as raised:
            PromptCatalog.validate("component_matching", template)

        self.assertIn("unknown", str(raised.exception))

    def test_nested_unknown_prompt_variable_is_rejected_by_render(self):
        template = "{requirement:{unknown}} {component_names}"

        with self.assertRaises(PromptValidationError):
            PromptCatalog.render(
                "component_matching",
                template,
                requirement="R",
                component_names="- A",
            )

    def test_prompt_render_keeps_valid_top_level_fields(self):
        rendered = PromptCatalog.render(
            "component_matching",
            "{requirement} {component_names}",
            requirement="R",
            component_names="- A",
        )

        self.assertEqual(rendered, "R - A")

    def test_empty_positional_prompt_field_is_rejected(self):
        with self.assertRaises(PromptValidationError):
            PromptCatalog.variables("{}")

    def test_prompt_render_normalizes_formatting_errors(self):
        class BrokenFormat:
            def __init__(self, error: Exception):
                self.error = error

            def __format__(self, format_spec: str) -> str:
                raise self.error

        for error in (KeyError("bad"), IndexError("bad"), ValueError("bad")):
            with self.subTest(error_type=type(error).__name__):
                with self.assertRaises(PromptValidationError) as raised:
                    PromptCatalog.render(
                        "component_matching",
                        "{requirement} {component_names}",
                        requirement=BrokenFormat(error),
                        component_names="- A",
                    )
                self.assertIs(raised.exception.__cause__, error)

    def test_provider_model_and_base_url_are_validated(self):
        with self.assertRaises(ValidationError):
            OpenAICompatibleSettings(model=" ")
        with self.assertRaises(ValidationError):
            OpenAICompatibleSettings(base_url="http://api.example.test/v1")
