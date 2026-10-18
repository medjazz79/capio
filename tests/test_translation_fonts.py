"""Separate Arabic and Latin translation fonts."""
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestTranslationFonts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_dialog_roundtrips_two_fonts_and_migrates_legacy_value(self):
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(dict(DEFAULTS, translation_font="Georgia"))
        try:
            self.assertEqual(dialog.arabic_font.currentData(), "Georgia")
            self.assertEqual(dialog.latin_font.currentData(), "Georgia")

            dialog.arabic_font.setCurrentIndex(
                dialog.arabic_font.findData("Times New Roman"))
            dialog.latin_font.setCurrentIndex(
                dialog.latin_font.findData("Georgia"))
            dialog.arabic_tashkeel.setChecked(True)
            values = dialog.values()
            self.assertEqual(values["translation_font_arabic"], "Times New Roman")
            self.assertEqual(values["translation_font_latin"], "Georgia")
            self.assertEqual(values["translation_font"], "")
            self.assertTrue(values["arabic_tashkeel"])
        finally:
            dialog.close()
            self.app.processEvents()

    def test_tashkeel_instruction_is_added_only_when_enabled_for_arabic(self):
        from translators.chain import _translation_instructions

        disabled = _translation_instructions({
            "target_language": "ar",
            "arabic_tashkeel": False,
        })
        self.assertNotIn("Arabic tashkeel is enabled", disabled)
        self.assertIn("Arabic tashkeel is disabled", disabled)
        self.assertIn("without diacritical marks", disabled)

        enabled = _translation_instructions({
            "target_language": "ar",
            "arabic_tashkeel": True,
            "model_instructions": "Preserve the paragraph layout.",
        })
        self.assertIn("Arabic tashkeel is enabled", enabled)
        self.assertIn("fully vocalize every word written in Arabic script", enabled)
        self.assertIn("internal vowels", enabled)
        self.assertIn("Arabic-script names too", enabled)
        self.assertIn("'كَتَبَ الطَّالِبُ الدَّرْسَ'", enabled)
        self.assertIn("Preserve the paragraph layout.", enabled)
        self.assertIn("shadda, sukun, and tanween", enabled)

        non_arabic = _translation_instructions({
            "target_language": "fr",
            "arabic_tashkeel": True,
        })
        self.assertNotIn("Arabic tashkeel is enabled", non_arabic)
        self.assertNotIn("Arabic tashkeel is disabled", non_arabic)

    def test_custom_provider_system_message_includes_tashkeel_instruction(self):
        from translators.chain import _translation_instructions
        from translators.opencode_compat import _messages_for

        settings = {
            "target_language": "ar",
            "arabic_tashkeel": True,
        }
        instructions = _translation_instructions(
            settings, "custom_provider:agnes:agnes-2.5-flash")
        messages = _messages_for(
            "A sentence.", instructions, "auto", "ar")

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Arabic tashkeel is enabled", messages[0]["content"])
        self.assertIn(
            "shadda, sukun, and tanween", messages[0]["content"])

    def test_refreshing_tashkeel_setting_retranslates_visible_source(self):
        from PySide6.QtCore import QObject, Signal
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        class StubTranslateWorker(QObject):
            done = Signal(str, str)
            stage = Signal(str)
            failed = Signal(str)
            source_ready = Signal(str)
            partial = Signal(str)
            finished = Signal()

            def __init__(self, *args, **kwargs):
                super().__init__()

            def start(self):
                pass

            def isRunning(self):
                return False

        settings = dict(DEFAULTS, arabic_tashkeel=False)
        with mock.patch(
            "popup.translate_popup.TranslateWorker", StubTranslateWorker,
        ):
            popup = TranslatePopup(settings, source_text=None)
        try:
            popup._source_text = "An existing source passage."
            updated = dict(settings, arabic_tashkeel=True)
            with mock.patch.object(
                popup, "_retranslate_for_current_languages",
            ) as retranslate:
                popup.refresh_display(updated)
            retranslate.assert_called_once_with()
        finally:
            popup.close()
            self.app.processEvents()

    def test_translation_window_applies_each_font_to_matching_script(self):
        from PySide6.QtCore import QObject, Qt, Signal
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        class StubTranslateWorker(QObject):
            done = Signal(str, str)
            stage = Signal(str)
            failed = Signal(str)
            source_ready = Signal(str)
            partial = Signal(str)
            finished = Signal()

            def __init__(self, *args, **kwargs):
                super().__init__()

            def start(self):
                pass

            def isRunning(self):
                return False

        settings = dict(
            DEFAULTS,
            translation_font_arabic="Georgia",
            translation_font_latin="Times New Roman",
            line_spacing_px=5,
        )
        with mock.patch(
            "popup.translate_popup.TranslateWorker", StubTranslateWorker,
        ):
            popup = TranslatePopup(settings, source_text=None)
        try:
            popup.result.show()
            streamed_markdown = "# مرحبا café\n\nEnglish"
            popup._append_streamed_partial(streamed_markdown)
            streamed_document = popup.result.document()
            self.assertEqual(
                popup.result.toPlainText(), streamed_markdown)
            self.assertEqual(
                streamed_document.find("مرحبا").charFormat().fontFamilies()[0],
                "Georgia",
            )
            self.assertEqual(
                streamed_document.find("café").charFormat().fontFamilies()[0],
                "Times New Roman",
            )
            self.assertEqual(
                streamed_document.find("English").charFormat().fontFamilies()[0],
                "Times New Roman",
            )
            self.assertEqual(
                streamed_document.defaultTextOption().alignment(),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute,
            )
            blank_block = streamed_document.begin().next()
            self.assertEqual(blank_block.text(), "")
            self.assertEqual(blank_block.blockFormat().topMargin(), 0)
            self.assertEqual(blank_block.blockFormat().bottomMargin(), 0)
            self.assertEqual(
                streamed_document.begin().blockFormat().bottomMargin(), 0)
            self.assertEqual(
                streamed_document.lastBlock().blockFormat().bottomMargin(), 0)
            self.assertAlmostEqual(
                streamed_document.defaultFont().pointSizeF(),
                popup.result.font().pointSizeF(),
            )
            self.assertAlmostEqual(
                streamed_document.find("English").block().blockFormat().lineHeight(),
                5.0,
            )
            self.assertAlmostEqual(
                streamed_document.begin().blockFormat().lineHeight(), 5.0)

            streamed_updated = dict(
                settings,
                translation_font_arabic="Times New Roman",
                translation_font_latin="Georgia",
            )
            popup.refresh_display(streamed_updated)
            streamed_document = popup.result.document()
            self.assertEqual(
                streamed_document.find("مرحبا").charFormat().fontFamilies()[0],
                "Times New Roman",
            )
            self.assertEqual(
                streamed_document.find("English").charFormat().fontFamilies()[0],
                "Georgia",
            )
            self.assertAlmostEqual(
                streamed_document.defaultFont().pointSizeF(),
                popup.result.font().pointSizeF(),
            )
            self.assertEqual(
                streamed_document.begin().blockFormat().lineHeight(), 5.0)

            popup.result.clear()
            popup._streamed_partial = ""
            popup._apply_streamed_typography()
            popup._append_streamed_partial("مرحبا")
            popup._append_streamed_partial("مرحبا\n\nEnglish")
            streamed_document = popup.result.document()
            self.assertEqual(
                streamed_document.find("مرحبا").charFormat().fontFamilies()[0],
                "Times New Roman",
            )
            self.assertEqual(
                streamed_document.find("English").charFormat().fontFamilies()[0],
                "Georgia",
            )
            self.assertAlmostEqual(
                streamed_document.find("English").block().blockFormat().lineHeight(),
                5.0,
            )

            popup.result.clear()
            popup._streamed_partial = ""
            popup._translation_text = "مرحبا café English"
            popup.result.show()
            popup.refresh_display(settings)
            document = popup.result.document()
            self.assertEqual(
                document.find("مرحبا").charFormat().fontFamilies()[0],
                "Georgia",
            )
            self.assertEqual(
                document.find("café").charFormat().fontFamilies()[0],
                "Times New Roman",
            )

            updated = dict(
                settings,
                translation_font_arabic="Times New Roman",
                translation_font_latin="Georgia",
            )
            popup.refresh_display(updated)
            document = popup.result.document()
            self.assertEqual(
                document.find("مرحبا").charFormat().fontFamilies()[0],
                "Times New Roman",
            )
            self.assertEqual(
                document.find("English").charFormat().fontFamilies()[0],
                "Georgia",
            )
        finally:
            popup.close()
            self.app.processEvents()
