"""The language catalog and its prompt/voice wiring."""
import unittest
from unittest import mock


NEW_LANGUAGES = (
    ("pt", "Portuguese", "Portuguese"),
    ("ja", "Japanese", "Japanese"),
    ("ko", "Korean (South Korea)", "Korean (South Korea)"),
    ("zh", "Chinese Simplified", "Chinese Simplified"),
    ("hi", "Hindi", "Hindi"),
)


class LanguageCatalogTest(unittest.TestCase):
    def test_new_languages_are_appended_in_the_requested_order(self):
        from translators.prompt import LANGUAGE_CHOICES, LANGUAGE_NAMES

        self.assertEqual(
            [code for _label, code in LANGUAGE_CHOICES][-5:],
            [code for code, _label, _name in NEW_LANGUAGES],
        )
        self.assertEqual(
            [label for label, _code in LANGUAGE_CHOICES][-5:],
            [label for _code, label, _name in NEW_LANGUAGES],
        )
        for code, _label, name in NEW_LANGUAGES:
            self.assertEqual(LANGUAGE_NAMES[code], name)

    def test_new_targets_are_accepted_by_the_translation_prompt(self):
        from translators.prompt import system_instructions, user_prompt

        for code, _label, name in NEW_LANGUAGES:
            with self.subTest(target=code):
                system = system_instructions(
                    source_language="en", target_language=code)
                user = user_prompt(
                    "A cadence.", source_language="en", target_language=code)
                self.assertIn(f"into {name}", system)
                self.assertIn(f"into {name}", user)

    def test_explain_mode_uses_a_native_heading_for_each_new_language(self):
        from translators import chain

        headings = {
            "pt": "Explicação",
            "ja": "説明",
            "ko": "설명",
            "zh": "解释",
            "hi": "व्याख्या",
        }
        for code, heading in headings.items():
            with self.subTest(language=code):
                instructions = chain._translation_instructions({
                    "target_language": code,
                    "explain_mode": True,
                })
                self.assertIn(f"`### {heading}`", instructions)

    def test_regional_language_detection_is_normalized_to_the_base_code(self):
        from translators import chain

        with mock.patch("langdetect.detect", return_value="zh-cn"):
            self.assertEqual(
                chain._detect_translation_language("中" * 50), "zh")
        with mock.patch("langdetect.detect", return_value="pt-BR"):
            self.assertEqual(
                chain._detect_translation_language("a" * 50), "pt")

    def test_new_languages_have_automatic_edge_voices(self):
        from popup.speech_worker import (
            AUTOMATIC_SPEECH_VOICES,
            EDGE_TTS_PREFERRED_LANGUAGE_CODES,
        )

        for code, _label, _name in NEW_LANGUAGES:
            with self.subTest(language=code):
                self.assertIn(code, AUTOMATIC_SPEECH_VOICES)
                self.assertIn(code, EDGE_TTS_PREFERRED_LANGUAGE_CODES)


if __name__ == "__main__":
    unittest.main()
