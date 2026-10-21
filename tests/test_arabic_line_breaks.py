"""Arabic prose must not keep Unicode line separators as hard breaks."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class ArabicUnicodeLineBreakTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_unicode_line_separator_is_a_soft_break_for_arabic_prose(self):
        from popup.translate_popup import reflow_wrapped_prose

        text = (
            "هذه جملة عربية كاملة تنتهي بنقطة.\u2028"
            "وهذه جملة ثانية تكمل الفقرة نفسها ولا تبدأ فقرة جديدة."
        )

        self.assertEqual(
            reflow_wrapped_prose(text),
            "هذه جملة عربية كاملة تنتهي بنقطة. "
            "وهذه جملة ثانية تكمل الفقرة نفسها ولا تبدأ فقرة جديدة.",
        )

    def test_streamed_arabic_preview_does_not_keep_unicode_line_separators(self):
        from popup.translate_popup import TranslatePopup

        class StubPopup:
            settings = {"target_language": "ar"}
            _reflows_wrapped_prose = TranslatePopup._reflows_wrapped_prose

        text = (
            "هذه جملة عربية كاملة تنتهي بنقطة.\u2028"
            "وهذه جملة ثانية تكمل الفقرة نفسها."
        )
        preview = TranslatePopup._normalize_streamed_markdown(
            StubPopup(), text)

        self.assertNotIn("\u2028", preview)
        self.assertEqual(
            preview,
            "هذه جملة عربية كاملة تنتهي بنقطة. "
            "وهذه جملة ثانية تكمل الفقرة نفسها.",
        )

    def test_paragraph_separator_keeps_a_real_paragraph_break(self):
        from popup.translate_popup import reflow_wrapped_prose

        self.assertEqual(
            reflow_wrapped_prose("الفقرة الأولى.\u2029الفقرة الثانية."),
            "الفقرة الأولى.\n\nالفقرة الثانية.",
        )

    def test_rendered_arabic_paragraph_fills_the_available_width(self):
        from PySide6.QtGui import QFont, QTextDocument
        from popup.translate_popup import TranslatePopup

        sentence = (
            "هذه جملة عربية طويلة تحتوي على كلمات كافية لملء عرض نافذة "
            "الترجمة عند التفاف النص بشكل طبيعي."
        )
        text = "\u2028".join([sentence] * 4)
        rendered = TranslatePopup._markdown_with_inline_notation(
            text, reflow_soft_line_breaks=True)

        self.assertNotIn("\u2028", rendered)
        document = QTextDocument()
        document.setDefaultFont(QFont("Segoe UI", 18))
        document.setTextWidth(620)
        document.setMarkdown(rendered)
        document.documentLayout().documentSize()

        block = document.begin()
        widths = [
            block.layout().lineAt(index).naturalTextWidth()
            for index in range(block.layout().lineCount())
        ]
        self.assertGreaterEqual(
            max(widths), 500,
            "the joined Arabic paragraph should wrap near the full width",
        )


if __name__ == "__main__":
    unittest.main()
