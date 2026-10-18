import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestTranslationChat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_chat_transcript_has_one_plain_large_context_menu(self):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QContextMenuEvent
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QMainWindow, QTabWidget
        from main import _CtrlWheelThumbsFilter
        from ui.chat_panel import ChatPanel

        window = QMainWindow()
        window.tabs = QTabWidget(window)
        window._show_app_context_menu = mock.Mock()
        panel = ChatPanel(lambda: {"chat_font_size": 12.0}, window)
        window.setCentralWidget(panel)
        panel.resize(400, 300)
        panel.transcript.setPlainText("Assistant reply")
        panel.input.setPlainText("User message")
        window.resize(440, 340)
        app_filter = _CtrlWheelThumbsFilter(window)
        self.app.installEventFilter(app_filter)
        window.show()
        self.app.processEvents()
        refresh_requests = []
        reopen_requests = []
        panel.transcript.refreshRequested.connect(
            lambda: refresh_requests.append(True))
        panel.transcript.reopenRequested.connect(
            lambda: reopen_requests.append(True))
        menu = panel._context_menu
        menu_shown = []
        menu.aboutToShow.connect(lambda: menu_shown.append(True))
        try:
            actions = menu.actions()
            self.assertEqual(
                [action.text() for action in actions],
                ["Copy", "Cut", "Paste", "Select All", "",
                 "Refresh", "ReOpen"],
            )
            self.assertTrue(all(
                action.icon().isNull()
                for action in actions if not action.isSeparator()
            ))
            self.assertEqual(
                menu.styleSheet(),
                "QMenu::item { font-size: 17px; padding: 9px 24px; }")
            self.assertTrue(all(
                action.shortcut().isEmpty()
                for action in actions if not action.isSeparator()
            ))
            for widget in (panel.transcript, panel.input):
                self.assertEqual(
                    widget.contextMenuPolicy(),
                    Qt.ContextMenuPolicy.NoContextMenu)
                self.assertEqual(
                    widget.viewport().contextMenuPolicy(),
                    Qt.ContextMenuPolicy.NoContextMenu)
                QTest.mouseClick(
                    widget.viewport(), Qt.MouseButton.RightButton,
                    pos=widget.viewport().rect().center())
                self.app.processEvents()
                self.assertEqual(len(menu_shown), 1)
                self.assertIs(panel._context_menu_target, widget)
                self.assertFalse(window._show_app_context_menu.called)
                menu.hide()
                self.app.processEvents()
                QTest.mouseClick(
                    widget.viewport(), Qt.MouseButton.LeftButton,
                    pos=widget.viewport().rect().center())
                self.app.processEvents()
                self.assertEqual(len(menu_shown), 1)
                self.app.sendEvent(
                    widget.viewport(),
                    QContextMenuEvent(
                        QContextMenuEvent.Reason.Mouse,
                        widget.viewport().rect().center(),
                        widget.viewport().mapToGlobal(
                            widget.viewport().rect().center()),
                    ),
                )
                self.app.processEvents()
                self.assertFalse(window._show_app_context_menu.called)
                menu.hide()
                self.app.processEvents()
                menu_shown.clear()

            for widget, expected_text in (
                (panel.transcript, "Assistant reply"),
                (panel.input, "User message"),
            ):
                panel._show_context_menu(widget, widget.mapToGlobal(
                    widget.rect().center()))
                panel._select_all_context_text()
                self.assertEqual(
                    widget.textCursor().selectedText(), expected_text)
                panel._copy_context_text()
                self.assertEqual(
                    self.app.clipboard().text(), expected_text)

            actions[5].trigger()
            actions[6].trigger()
            self.assertEqual(refresh_requests, [True])
            self.assertEqual(reopen_requests, [True])

            self.app.clipboard().setText("Pasted text")
            panel.transcript.selectAll()
            panel._show_context_menu(
                panel.transcript, panel.transcript.mapToGlobal(
                    panel.transcript.rect().center()))
            self.assertFalse(actions[1].isEnabled())
            self.assertFalse(actions[2].isEnabled())

            panel.input.setPlainText("Cut me")
            panel.input.selectAll()
            panel._show_context_menu(
                panel.input, panel.input.mapToGlobal(
                    panel.input.rect().center()))
            self.assertTrue(actions[1].isEnabled())
            self.assertTrue(actions[2].isEnabled())
            actions[1].trigger()
            self.assertEqual(panel.input.toPlainText(), "")
            self.assertEqual(self.app.clipboard().text(), "Cut me")

            self.app.clipboard().setText("Pasted text")
            panel.input.setPlainText("Replace me")
            panel.input.selectAll()
            panel._show_context_menu(
                panel.input, panel.input.mapToGlobal(
                    panel.input.rect().center()))
            actions[2].trigger()
            self.assertEqual(panel.input.toPlainText(), "Pasted text")

            self.app.clipboard().clear()
            panel._show_context_menu(
                panel.input, panel.input.mapToGlobal(
                    panel.input.rect().center()))
            self.assertFalse(actions[2].isEnabled())
        finally:
            menu.close()
            self.app.removeEventFilter(app_filter)
            window.close()

    def test_standalone_chat_speed_button_and_resizable_composer(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QPushButton
        from ui.chat_panel import ChatPanel

        panel = ChatPanel(lambda: {"speech_rate": 1.0})
        try:
            panel.resize(420, 520)
            panel.show()
            QTest.qWait(50)

            self.assertIsInstance(panel.speed_button, QPushButton)
            self.assertEqual(panel.speed_button.text(), "1.0x")
            panel.apply_theme({
                "selection": "#345678",
                "text": "#ffffff",
                "window": "#202020",
                "border": "#555555",
                "surface": "#303030",
                "accent": "#ff8800",
                "muted": "#aaaaaa",
            })
            self.assertIn("font-size: 15px", panel.speed_button.styleSheet())
            self.assertIn(
                "background-color: transparent",
                panel.chat_actions_scroll.styleSheet(),
            )
            self.assertNotIn(
                "#ffffff",
                panel.chat_actions_scroll.styleSheet().lower(),
            )
            self.assertIn(
                "QWidget#chatActionsContainer",
                panel.chat_actions_scroll.styleSheet(),
            )
            self.assertIn(
                "background-color: #303030",
                panel.chat_actions_scroll.styleSheet(),
            )
            for button in panel._chat_action_buttons + (panel.send_button,):
                self.assertTrue(button.isFlat())
                for corner in (
                    "top-left", "top-right", "bottom-left", "bottom-right",
                ):
                    self.assertIn(
                        f"border-{corner}-radius: 7px",
                        button.styleSheet(),
                    )
                self.assertIn("padding: 6px 4px", button.styleSheet())
            self.assertFalse(panel.stop_speech_button.isEnabled())
            self.assertFalse(panel.explain_button.isEnabled())
            self.assertIn(
                "QPushButton#chatStopSpeech:disabled",
                panel.stop_speech_button.styleSheet(),
            )
            self.assertIn(
                "QPushButton#explainSelectionButton:disabled",
                panel.explain_button.styleSheet(),
            )
            for button in (panel.stop_speech_button, panel.explain_button):
                self.assertIn("background-color: #303030", button.styleSheet())
                self.assertIn("color: #ffffff", button.styleSheet())
            for button in (
                panel.speed_button,
                panel.read_aloud_button,
                panel.stop_speech_button,
                panel.explain_button,
                panel.clear_button,
            ):
                self.assertEqual(button.height(), 44)
            self.assertGreaterEqual(
                panel.speed_button.width(),
                panel.speed_button.sizeHint().width(),
            )

            panel._set_speech_rate(130)
            self.assertEqual(panel.speed_button.text(), "1.3x")
            self.assertGreaterEqual(
                panel.speed_button.width(),
                panel.speed_button.sizeHint().width(),
            )
            panel.setFixedWidth(180)
            panel._compact_chat_actions = None
            panel._update_chat_action_layout()
            self.app.processEvents()
            button_top_positions = {
                button.mapTo(panel.chat_actions_widget, button.rect().topLeft()).y()
                for button in panel._chat_action_buttons
            }
            self.assertEqual(len(button_top_positions), 1)
            for left, right in zip(
                panel._chat_action_buttons,
                panel._chat_action_buttons[1:],
            ):
                self.assertEqual(left.geometry().right() + 3,
                                 right.geometry().left())
            self.assertGreater(
                panel.chat_actions_scroll.horizontalScrollBar().maximum(),
                0,
            )
            panel.setFixedWidth(620)
            panel._compact_chat_actions = None
            panel._update_chat_action_layout()
            self.app.processEvents()
            self.assertEqual(
                panel.chat_actions_widget.width(),
                panel.chat_actions_scroll.viewport().width(),
            )
            self.assertEqual(
                panel._chat_action_buttons[-1].geometry().right() + 1,
                panel.chat_actions_widget.width(),
            )
            for button in panel._chat_action_buttons:
                self.assertEqual(button.height(), 44)
                for corner in (
                    "top-left", "top-right", "bottom-left", "bottom-right",
                ):
                    self.assertIn(
                        f"border-{corner}-radius: 7px",
                        button.styleSheet(),
                    )
            self.assertTrue(any(
                button.width() > button.sizeHint().width()
                for button in panel._chat_action_buttons
            ))

            panel.input_splitter.setSizes([300, 150])
            self.app.processEvents()
            initial_input_height = panel.input.height()
            handle = panel.input_splitter.handle(1)
            drag_start = handle.rect().center()
            drag_end = QPoint(drag_start.x(), drag_start.y() - 55)
            QTest.mousePress(
                handle, Qt.MouseButton.LeftButton, pos=drag_start)
            QTest.mouseMove(handle, drag_end, 100)
            QTest.mouseRelease(
                handle, Qt.MouseButton.LeftButton, pos=drag_end)
            self.app.processEvents()
            self.assertGreater(
                panel.input.height(),
                initial_input_height,
                "dragging the horizontal splitter should enlarge the composer",
            )
        finally:
            panel.close()

    def test_chat_font_size_and_reading_speed_are_restored_after_save(self):
        import tempfile
        from pathlib import Path
        from config import settings as settings_module
        from ui.chat_panel import ChatPanel

        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            with mock.patch.object(
                settings_module, "SETTINGS_PATH", settings_path
            ):
                settings = settings_module.load()
                panel = ChatPanel(lambda: settings)
                try:
                    panel.show()
                    self.app.processEvents()
                    panel._set_chat_font_size(18)
                    panel.hide()
                finally:
                    panel.close()

                restored_settings = settings_module.load()
                restored_panel = ChatPanel(lambda: restored_settings)
                try:
                    self.assertEqual(
                        restored_panel._chat_font_size, 18.0)
                finally:
                    restored_panel.close()

    def test_chat_and_translation_reading_speeds_are_independent(self):
        import tempfile
        from pathlib import Path
        from config import settings as settings_module
        from ui.chat_panel import ChatPanel

        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            with mock.patch.object(
                settings_module, "SETTINGS_PATH", settings_path
            ):
                settings = settings_module.load()
                settings["speech_rate"] = 0.8
                settings_module.save(settings)
                settings = settings_module.load()
                panel = ChatPanel(lambda: settings)
                try:
                    self.assertEqual(panel._speech_rate, 1.0)
                    panel._set_speech_rate(145)
                    panel._speech_rate_save_timer.stop()
                    panel._save_speech_rate()
                finally:
                    panel.close()

                persisted = settings_module.load()
                self.assertEqual(persisted["speech_rate"], 0.8)
                self.assertEqual(persisted["chat_speech_rate"], 1.45)

                persisted["speech_rate"] = 1.2
                settings_module.save(persisted)
                reopened = ChatPanel(lambda: settings_module.load())
                try:
                    self.assertEqual(reopened._speech_rate, 1.45)
                finally:
                    reopened.close()

    def test_explain_button_uses_selection_or_translation_context(self):
        from ui.chat_panel import ChatPanel

        panel = ChatPanel(lambda: {"speech_rate": 1.0})
        try:
            self.assertFalse(panel.explain_button.isEnabled())
            panel.set_translation_context("Full translated passage")
            self.assertTrue(panel.explain_button.isEnabled())

            with mock.patch.object(panel, "_send_question") as send_question:
                panel.explain_button.click()
                question = send_question.call_args.args[0]
                self.assertIn("Full translated passage", question)
                self.assertNotIn(
                    "use_google_search", send_question.call_args.kwargs)

                panel.set_explanation_selection("Selected phrase")
                panel.explain_button.click()
                question = send_question.call_args.args[0]
                self.assertIn("Selected phrase", question)
                self.assertEqual(
                    send_question.call_args.kwargs["display_question"],
                    "Explain: Selected phrase",
                )
                self.assertNotIn("Full translated passage", question)
                self.assertNotIn(
                    "use_google_search", send_question.call_args.kwargs)
        finally:
            panel.close()

    def test_explain_instruction_is_sent_but_hidden_from_transcript(self):
        from ui.chat_panel import ChatPanel

        panel = ChatPanel(lambda: {"speech_rate": 1.0})
        try:
            panel.set_explanation_selection("made a statement")
            with mock.patch("ui.chat_panel.ChatWorker") as chat_worker:
                panel.explain_selection("simple")

            sent_messages = chat_worker.call_args.args[0]
            self.assertEqual(
                sent_messages[-1]["content"],
                "Explain the selected text in very simple, everyday "
                "language. Avoid technical terms and use a brief example "
                "only if it helps.\n\nSelected text:\nmade a statement",
            )
            self.assertEqual(
                panel._entries[-1],
                ("user", "Explain: made a statement"),
            )
            transcript = panel.transcript.toPlainText()
            self.assertIn("Explain: made a statement", transcript)
            self.assertNotIn("very simple, everyday language", transcript)
            self.assertNotIn("Selected text:", panel.transcript.toPlainText())
        finally:
            panel._worker = None
            panel.close()

    def test_all_explain_styles_allow_web_search(self):
        from ui.chat_panel import ChatPanel, explanation_instruction

        panel = ChatPanel(lambda: {"speech_rate": 1.0})
        try:
            for style in (
                "linguistic", "etymological", "simple",
                "translate_and_explain", "context",
            ):
                panel._worker = None
                panel._streaming_active = False
                panel.set_explanation_selection("Selected phrase")
                with mock.patch("ui.chat_panel.ChatWorker") as chat_worker:
                    panel.explain_selection(style)
                    explain_settings = chat_worker.call_args.args[1]
                self.assertNotIn("_chat_google_search", explain_settings)
                if style == "context":
                    self.assertIn(
                        "Google Search", explanation_instruction(style))
            panel._worker = None
            panel._streaming_active = False
            panel.set_explanation_selection("Selected phrase")
            with mock.patch("ui.chat_panel.ChatWorker") as chat_worker:
                panel._send_question("Please answer in Arabic.")
                regular_chat_settings = chat_worker.call_args.args[1]

            self.assertNotIn("_chat_google_search", regular_chat_settings)
        finally:
            panel._worker = None
            panel.close()

    def test_fenced_code_blocks_use_compact_colored_monospace_style(self):
        from PySide6.QtGui import QTextDocument
        from popup.translate_popup import TranslatePopup

        document = QTextDocument()
        document.setMarkdown(
            "Explanation\n\n"
            "```music\n\\begin{array}{c} C & D \\end{array}\n```\n\n"
            "```text\nordinary code\n```"
        )
        TranslatePopup._apply_code_block_style(document, 18.0)

        prose = document.find("Explanation")
        latex = document.find(r"\begin{array}")
        ordinary_code = document.find("ordinary code")
        self.assertEqual(prose.charFormat().fontPointSize(), 0)
        for code in (latex, ordinary_code):
            self.assertAlmostEqual(code.charFormat().fontPointSize(), 15.3)
            self.assertTrue(code.charFormat().fontFixedPitch())
            self.assertEqual(
                code.charFormat().foreground().color().name(), "#36566f")
            self.assertEqual(
                code.block().blockFormat().background().color().name(),
                "#e7edf3",
            )

    def test_adjacent_music_fence_is_rendered_and_silenced(self):
        from popup.translate_popup import TranslatePopup

        markdown = "Before.\n```music\nS | D | T\n```\nAfter."
        rendered = TranslatePopup._markdown_with_inline_notation(markdown)
        self.assertNotIn("Before.\u2028```music", rendered)

        speech = TranslatePopup._markdown_speech_text(markdown)
        self.assertIn("Before.", speech)
        self.assertIn("After.", speech)
        self.assertNotIn("S | D | T", speech)
        self.assertNotIn("```music", speech)

    def test_wrapped_prose_uses_reference_markdown_line_breaks(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QTextDocument
        from popup.translate_popup import TranslatePopup

        markdown = (
            "The door of the cabin had been blown to shreds and the roof had been\n"
            "ripped off, but the occupant still remained inside. The posse left, minus\n"
            "their quarry."
        )
        rendered = TranslatePopup._markdown_with_inline_notation(markdown)
        self.assertEqual(
            rendered,
            "The door of the cabin had been blown to shreds and the roof had been"
            "\u2028ripped off, but the occupant still remained inside. The posse left, "
            "minus\u2028their quarry.",
        )

        document = QTextDocument()
        document.setTextWidth(600)
        document.setMarkdown(rendered)
        document.documentLayout().documentSize()
        block = document.begin()
        self.assertEqual(block.layout().lineCount(), 5)
        self.assertEqual(
            document.defaultTextOption().alignment(),
            Qt.AlignmentFlag.AlignLeft,
        )

        explicit_break = TranslatePopup._markdown_with_inline_notation(
            "One line.  \nNext line."
        )
        self.assertEqual(explicit_break, "One line.  \u2028Next line.")
        document.setMarkdown(explicit_break)
        self.assertEqual(document.toPlainText(), "One line.  \nNext line.")

        escaped_break = TranslatePopup._markdown_with_inline_notation(
            "One line.\\\nNext line."
        )
        self.assertEqual(escaped_break, "One line.\\\u2028Next line.")

    def test_markdown_blocks_keep_reference_line_break_handling(self):
        from popup.translate_popup import TranslatePopup

        markdown = (
            "This English sentence starts on one line\n"
            "and continues on the next.\n"
            "### A heading\n"
            "- First item\n"
            "- Second item\n"
            "This paragraph follows the list\n"
            "and also wraps normally."
        )
        self.assertEqual(
            TranslatePopup._markdown_with_inline_notation(markdown),
            markdown,
        )

        even_backslashes = TranslatePopup._markdown_with_inline_notation(
            "A literal backslash\\\\\ncontinues the same paragraph."
        )
        self.assertEqual(
            even_backslashes,
            "A literal backslash\\\\\u2028continues the same paragraph.",
        )

    def test_english_translation_reflows_wrapped_prose_without_changing_other_languages(self):
        from PySide6.QtGui import QTextDocument
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        markdown = (
            "The source paragraph starts here and this translated line is "
            "relatively short.\n"
            "The next line continues the same sentence with enough text to "
            "show\n"
            "that the right side of the document stays empty even though "
            "the widget has room."
        )
        settings = dict(DEFAULTS, target_language="en")
        popup = TranslatePopup(settings, source_text=None)
        try:
            popup._render_markdown(markdown)
            english_block = popup.result.document().begin()
            self.assertEqual(
                english_block.text(),
                "The source paragraph starts here and this translated line is "
                "relatively short. The next line continues the same sentence "
                "with enough text to show that the right side of the document "
                "stays empty even though the widget has room.",
            )
            self.assertNotIn("\u2028", english_block.text())

            normalized = TranslatePopup._markdown_with_inline_notation(
                markdown, reflow_soft_line_breaks=True)
            document = QTextDocument()
            document.setTextWidth(560)
            document.setMarkdown(normalized)
            document.documentLayout().documentSize()
            widths = [
                document.begin().layout().lineAt(index).naturalTextWidth()
                for index in range(document.begin().layout().lineCount())
            ]
            self.assertGreaterEqual(max(widths), 520)

            structured_markdown = (
                "Intro line continues\n"
                "### Heading\n"
                "- first item\n"
                "- second item\n"
                "After list continues\n"
                "with another line.\n\n"
                "Explicit hard break  \n"
                "next explicit line.\n"
                "Explicit escaped break\\\n"
                "next escaped line.\n"
                "```text\n"
                "literal\n"
                "line breaks\n"
                "```"
            )
            self.assertEqual(
                TranslatePopup._markdown_with_inline_notation(
                    structured_markdown, reflow_soft_line_breaks=True),
                "Intro line continues\n"
                "### Heading\n"
                "- first item\n"
                "- second item\n"
                "After list continues with another line.\n\n"
                "Explicit hard break  \n"
                "next explicit line. "
                "Explicit escaped break\\\n"
                "next escaped line.\n\n"
                "```text\n"
                "literal\n"
                "line breaks\n"
                "```\n\n",
            )

            settings["target_language"] = "ar"
            popup._render_markdown(markdown)
            self.assertIn(
                "\u2028", popup.result.document().begin().text())
        finally:
            popup.close()

    def test_reflow_wrapped_prose_joins_only_soft_wraps(self):
        from popup.translate_popup import reflow_wrapped_prose

        self.assertEqual(
            reflow_wrapped_prose(
                "a woman became tired of listening to the incessant barking\n"
                "of a neighborhood dog near Kingsbury\n"
                "Run. She bundled up and went outside.\n"
                "\n"
                "The woman found two baskets sitting in the snow\n"
                "behind the Hart Manufacturing building."),
            "a woman became tired of listening to the incessant barking of a "
            "neighborhood dog near Kingsbury Run. She bundled up and went "
            "outside.\n\n"
            "The woman found two baskets sitting in the snow behind the Hart "
            "Manufacturing building.",
        )

        # Blank lines, explicit hard breaks, headings, lists, quotes, tables,
        # rules, and fenced code all keep their line break.
        for kept in (
            "Intro line continues\n### Heading\nafter the heading",
            "Lead in\n- first item\n- second item",
            "Lead in\n> quoted line",
            "Lead in\n| a | b |",
            "Lead in\n---",
            "Lead in  \nhard break",
            "Lead in\\\nescaped break",
            "Lead in\n    indented literal",
            "Heading\n===\nbody line",
            "```text\nliteral\nline breaks\n```",
        ):
            with self.subTest(kept=kept):
                self.assertEqual(reflow_wrapped_prose(kept), kept)

    def test_streamed_english_preview_reflows_wrapped_prose(self):
        """A page-faithful wrapped answer must not preview as short lines."""
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        markdown = (
            "to have died as much as three or four weeks earlier. On "
            "September 25,\n"
            "1935, he was buried in a potter's field, taking the secret of "
            "his identity\n"
            "to his grave. The following morning, exactly a week after his "
            "family\n"
            "had last seen him alive, Andrassy was buried in St. Mary's "
            "Cemetery.\n"
            "\n"
            "### MORE VICTIMS\n"
            "\n"
            "In the dead of winter on January 26, 1936, a woman became tired "
            "of\n"
            "listening to the incessant barking of a neighborhood dog near "
            "Kingsbury\n"
            "Run. She bundled up and went outside to investigate."
        )

        settings = dict(DEFAULTS, target_language="en")
        popup = TranslatePopup(settings, source_text=None)
        try:
            step = max(1, len(markdown) // 12)
            for end in range(step, len(markdown) + step, step):
                popup._append_streamed_partial(markdown[:end])
            preview = popup.result.toPlainText()

            self.assertIn(
                "a neighborhood dog near Kingsbury Run. She bundled up",
                preview,
            )
            self.assertIn("MORE VICTIMS", preview)
            self.assertNotIn("\n1935, he was buried", preview)
            # One block per paragraph, plus the heading's own block.
            self.assertLessEqual(
                popup.result.document().blockCount(), 6)
        finally:
            popup.close()

    def test_streamed_preview_matches_the_rendered_result(self):
        from PySide6.QtGui import QTextDocument
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        markdown = (
            "In the dead of winter on January 26, 1936, a woman became tired "
            "of\n"
            "listening to the incessant barking of a neighborhood dog near "
            "Kingsbury\n"
            "Run. She bundled up and went outside to investigate."
        )
        settings = dict(DEFAULTS, target_language="en")
        popup = TranslatePopup(settings, source_text=None)
        try:
            for end in range(4, len(markdown) + 4, 4):
                popup._append_streamed_partial(markdown[:end])

            document = QTextDocument()
            document.setMarkdown(popup._normalize_streamed_markdown(markdown))
            self.assertEqual(
                popup.result.toPlainText().strip(),
                document.toPlainText().strip(),
            )
        finally:
            popup.close()

    def test_streamed_preview_keeps_arabic_line_breaks(self):
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        markdown = "السطر الأول من الفقرة\nيكمل في السطر الثاني."
        settings = dict(DEFAULTS, target_language="ar")
        popup = TranslatePopup(settings, source_text=None)
        try:
            popup._append_streamed_partial(markdown)
            self.assertEqual(
                popup.result.toPlainText().strip(), markdown)
        finally:
            popup.close()

    def test_streamed_preview_normalization_grows_by_prefix(self):
        """Each streamed prefix must extend the previous normalized text."""
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        markdown = (
            "Lead line one that the model wrapped\n"
            "onto a second source line\n"
            "\n"
            "### A heading\n"
            "\n"
            "- first item\n"
            "- second item\n"
            "\n"
            "Trailing prose that the model also wrapped\n"
            "onto a second line.\n"
            "\n"
            "```text\n"
            "literal\n"
            "line breaks\n"
            "```\n"
        )
        settings = dict(DEFAULTS, target_language="en")
        popup = TranslatePopup(settings, source_text=None)
        try:
            previous = ""
            for end in range(1, len(markdown) + 1):
                current = popup._normalize_streamed_markdown(markdown[:end])
                self.assertTrue(
                    current.startswith(previous),
                    f"{current!r} does not extend {previous!r}",
                )
                previous = current
        finally:
            popup.close()

    def test_streamed_preview_shows_exactly_the_normalized_text(self):
        """Character by character streaming must always show the reflowed text."""
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        markdown = (
            "to have died as much as three or four weeks earlier. On "
            "September 25,\n"
            "1935, he was buried in a potter's field, taking the secret of "
            "his identity\n"
            "to his grave. The following morning, exactly a week after his "
            "family\n"
            "had last seen him alive, Andrassy was buried in St. Mary's "
            "Cemetery.\n"
            "\n"
            "### MORE VICTIMS\n"
            "\n"
            "In the dead of winter on January 26, 1936, a woman became tired "
            "of\n"
            "listening to the incessant barking of a neighborhood dog near "
            "Kingsbury\n"
            "Run. She bundled up and went outside to investigate."
        )
        settings = dict(DEFAULTS, target_language="en")
        popup = TranslatePopup(settings, source_text=None)
        try:
            for end in range(1, len(markdown) + 1):
                partial = markdown[:end]
                popup._streamed_partial = markdown[:end - 1]
                popup._streamed_displayed = (
                    popup._normalize_streamed_markdown(markdown[:end - 1])
                )
                popup._append_streamed_partial(partial)
                with self.subTest(end=end):
                    self.assertEqual(
                        popup.result.toPlainText().rstrip("\n"),
                        popup._normalize_streamed_markdown(
                            partial).rstrip("\n"),
                    )
        finally:
            popup.close()

    def test_speech_adds_a_pause_after_standalone_bold_headings(self):
        from popup.translate_popup import TranslatePopup

        markdown = (
            "**الهوية الحقيقية للضحية**\n\n"
            "لم تكن هوية الرجل معروفة."
        )
        speech = TranslatePopup._markdown_speech_text(markdown)

        self.assertIn("الهوية الحقيقية للضحية,لم تكن هوية الرجل", speech)
        self.assertEqual(
            len(speech),
            len("الهوية الحقيقية للضحية\nلم تكن هوية الرجل معروفة."),
        )

    def test_speech_adds_a_comma_pause_between_all_paragraphs(self):
        from popup.translate_popup import TranslatePopup

        cases = (
            ("المحتويان\n\nالمقدمة", "المحتويان,المقدمة"),
            ("كلمة مفردة\n\nالجملة التالية", "كلمة مفردة,الجملة التالية"),
            ("الجملة الأولى.\n\nالجملة الثانية.", "الجملة الأولى.\nالجملة الثانية."),
        )
        for markdown, expected in cases:
            with self.subTest(markdown=markdown):
                self.assertIn(
                    expected, TranslatePopup._markdown_speech_text(markdown))

    def test_math_dollar_delimiters_do_not_consume_currency_amounts(self):
        from popup.translate_popup import TranslatePopup

        markdown = "Prices $5 and $10; inline $x+1$ and display $$y^2$$."
        self.assertEqual(
            TranslatePopup._markdown_with_inline_notation(markdown),
            "Prices $5 and $10; inline x+1 and display y^2.",
        )

    def test_inline_pitch_labels_do_not_truncate_markdown_rendering(self):
        from PySide6.QtGui import QTextDocument
        from popup.translate_popup import TranslatePopup

        markdown = (
            "Before <C> after <F>, <G>, <AB>, <XYZ123>, <Bb>, <F#>, "
            "<B♭>, <alpha>, <x>, and <QZX_42>."
        )
        document = QTextDocument()
        document.setMarkdown(
            TranslatePopup._markdown_with_inline_notation(markdown))

        self.assertEqual(document.toPlainText(), markdown)

    def test_markdown_html_heading_is_not_escaped_as_notation(self):
        from PySide6.QtGui import QTextDocument
        from popup.translate_popup import TranslatePopup

        document = QTextDocument()
        document.setMarkdown(
            TranslatePopup._markdown_with_inline_notation(
                '<h2 align="center">Heading</h2>'))

        heading = document.find("Heading")
        self.assertFalse(heading.isNull())
        self.assertEqual(heading.blockFormat().headingLevel(), 2)

    def test_pipe_markdown_tables_render_as_aligned_tables(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QTextDocument, QTextTable
        from popup.translate_popup import TranslatePopup

        markdown = (
            "Before the table.\n\n"
            "| Harmonic | Unique range | Bandwidth |\n"
            "|:---|:---:|---:|\n"
            "| 7 | 6.48 - 7.36 | 0.88 |\n"
            "| 8 | 7.56 - 8.28 | 0.72 |\n\n"
            "After the table."
        )
        rendered = TranslatePopup._markdown_with_inline_notation(markdown)
        document = QTextDocument()
        document.setMarkdown(rendered)

        tables = [
            frame for frame in document.rootFrame().childFrames()
            if isinstance(frame, QTextTable)
        ]
        self.assertEqual(len(tables), 1)
        table = tables[0]
        self.assertEqual((table.rows(), table.columns()), (3, 3))
        self.assertEqual(
            table.cellAt(0, 0).firstCursorPosition().block().text(),
            "Harmonic",
        )
        self.assertEqual(
            table.cellAt(2, 2).firstCursorPosition().block().text(),
            "0.72",
        )
        self.assertEqual(
            int(table.cellAt(0, 0).firstCursorPosition().blockFormat()
                .alignment()) & 0x7,
            int(Qt.AlignmentFlag.AlignLeft),
        )
        self.assertEqual(
            int(table.cellAt(0, 1).firstCursorPosition().blockFormat()
                .alignment()) & 0x7,
            int(Qt.AlignmentFlag.AlignHCenter),
        )
        self.assertEqual(
            int(table.cellAt(0, 2).firstCursorPosition().blockFormat()
                .alignment()) & 0x7,
            int(Qt.AlignmentFlag.AlignRight),
        )
        self.assertIn("Before the table.", document.toPlainText())
        self.assertIn("After the table.", document.toPlainText())
        self.assertNotIn("| Harmonic |", document.toPlainText())
        speech = TranslatePopup._markdown_speech_text(markdown)
        self.assertIn("Harmonic", speech)
        self.assertIn("0.72", speech)
        self.assertNotIn("<table", speech)
        self.assertNotIn("| Harmonic |", speech)

    def test_translation_rendering_preserves_table_alignment_in_arabic(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QTextTable
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        markdown = (
            "| يسار | وسط | يمين |\n"
            "|:---|:---:|---:|\n"
            "| أ | ب | ج |"
        )
        with mock.patch.object(TranslatePopup, "_start_translation_worker"):
            popup = TranslatePopup(dict(DEFAULTS, target_language="ar"))
        try:
            popup._render_markdown(markdown)
            tables = [
                frame
                for frame in popup.result.document().rootFrame().childFrames()
                if isinstance(frame, QTextTable)
            ]
            self.assertEqual(len(tables), 1)
            table = tables[0]
            self.assertEqual((table.rows(), table.columns()), (2, 3))
            expected = (
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignHCenter,
                Qt.AlignmentFlag.AlignRight,
            )
            for column, alignment in enumerate(expected):
                cell_block = table.cellAt(
                    0, column).firstCursorPosition().block()
                self.assertEqual(
                    int(cell_block.blockFormat().alignment()) & 0x7,
                    int(alignment),
                )
        finally:
            popup.close()

    def test_pipe_text_inside_fenced_code_is_not_converted_to_a_table(self):
        from PySide6.QtGui import QTextDocument, QTextTable
        from popup.translate_popup import TranslatePopup

        markdown = (
            "```text\n"
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n"
            "```"
        )
        document = QTextDocument()
        document.setMarkdown(
            TranslatePopup._markdown_with_inline_notation(markdown))
        self.assertFalse(any(
            isinstance(frame, QTextTable)
            for frame in document.rootFrame().childFrames()
        ))
        self.assertIn("| A | B |", document.toPlainText())

    def test_openai_compatible_chat_sends_bearer_authorization(self):
        import requests
        from translators import opencode_compat

        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": [{
            "message": {"content": "Answer"}, "finish_reason": "stop"}]}
        api_key = "test-api-key"
        with mock.patch.object(requests, "post", return_value=response) as post:
            result = opencode_compat.chat(
                [{"role": "user", "content": "Question"}],
                "https://api.groq.com/openai/v1",
                api_key,
                "openai/gpt-oss-120b",
                "System prompt",
            )

        self.assertEqual(result, "Answer")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            f"Bearer {api_key}",
        )

    def test_reply_uses_selected_bridge_and_translation_context(self):
        from translators import chat

        messages = [{"role": "user", "content": "Why is this chord here?"}]
        settings = {
            "model": "space-bunny-free",
            "source_language": "en",
            "target_language": "ar",
        }
        with mock.patch(
            "translators.chat.opencode_bridge.chat", return_value="شرح"
        ) as bridge_chat:
            answer = chat.reply(
                messages, settings, "Original passage", "Current translation")

        self.assertEqual(answer, "شرح")
        call_messages, model, system_prompt, _ = bridge_chat.call_args.args
        self.assertEqual(call_messages, messages)
        self.assertEqual(model, "space-bunny-free")
        self.assertIn("Original passage", system_prompt)
        self.assertIn("Current translation", system_prompt)
        self.assertIn("Reply in Arabic", system_prompt)

    def test_popup_instructions_are_included_in_chat_system_prompt(self):
        from translators import chat

        settings = {
            "model": "space-bunny-free",
            "source_language": "en",
            "target_language": "ar",
            "explain_instructions": "Explain each cadence step by step.",
        }
        with mock.patch(
            "translators.chat.opencode_bridge.chat", return_value="شرح"
        ) as bridge_chat:
            chat.reply(
                [{"role": "user", "content": "Explain this."}],
                settings,
                "Original passage",
                "Translation",
            )

        system_prompt = bridge_chat.call_args.args[2]
        self.assertIn(
            "Additional instructions from the translation window",
            system_prompt,
        )
        self.assertIn("Explain each cadence step by step.", system_prompt)

    def test_gemini_reply_uses_selected_model_and_key(self):
        from translators import chat

        settings = {
            "model": "gemini-2.5-flash",
            "gemini_key": "configured-key",
            "source_language": "en",
            "target_language": "fr",
        }
        messages = [{"role": "user", "content": "Please answer in French."}]
        with mock.patch(
            "translators.chat.gemini.chat", return_value="Réponse"
        ) as gemini_chat:
            answer = chat.reply(messages, settings, "source", "translation")

        self.assertEqual(answer, "Réponse")
        gemini_chat.assert_called_once()
        self.assertEqual(gemini_chat.call_args.args[:3], (
            messages, "configured-key", "gemini-2.5-flash"))
        self.assertIn("Reply in French", gemini_chat.call_args.args[3])

    def test_streaming_chat_uses_selected_target_language(self):
        from translators import chat

        settings = {
            "model": "gemini-2.5-flash",
            "gemini_key": "configured-key",
            "source_language": "en",
            "target_language": "fr",
        }
        messages = [{"role": "user", "content": "Explain this phrase."}]
        with mock.patch(
            "translators.chat.gemini.stream_chat",
            return_value=iter(("Réponse ", "complète.")),
        ) as gemini_stream:
            answer = "".join(chat.reply_stream(
                messages, settings, "source", "translation",
            ))

        self.assertEqual(answer, "Réponse complète.")
        self.assertIn("Reply in French", gemini_stream.call_args.args[3])
        self.assertIn("Google Search", gemini_stream.call_args.args[3])
        self.assertTrue(gemini_stream.call_args.kwargs["use_google_search"])
        self.assertIn(
            "MUST use the selected target language",
            gemini_stream.call_args.args[3],
        )

    def test_chat_can_force_reply_language_without_changing_translation_target(self):
        from translators import chat

        settings = {
            "model": "gemini-2.5-flash",
            "gemini_key": "configured-key",
            "source_language": "en",
            "target_language": "fr",
            "chat_reply_language": "de",
            "chat_reply_language_only": True,
        }
        with mock.patch(
            "translators.chat.gemini.stream_chat",
            return_value=iter(("Guten Tag.",)),
        ) as gemini_stream:
            answer = "".join(chat.reply_stream(
                [{"role": "user", "content": "Hello"}],
                settings,
                "source",
                "translation",
            ))

        self.assertEqual(answer, "Guten Tag.")
        self.assertIn("Reply in German", gemini_stream.call_args.args[3])
        self.assertIn(
            "selected target language",
            gemini_stream.call_args.args[3],
        )

    def test_streaming_chat_always_enables_google_search(self):
        from translators import chat

        settings = {
            "model": "gemini-2.5-flash",
            "gemini_key": "configured-key",
            "source_language": "en",
            "target_language": "ar",
        }
        messages = [{"role": "user", "content": "Please answer in French."}]
        with mock.patch(
            "translators.chat.gemini.stream_chat",
            return_value=iter(("1872",)),
        ) as gemini_stream:
            answer = "".join(chat.reply_stream(
                messages, settings, "source", "translation"))

        self.assertEqual(answer, "1872")
        self.assertTrue(gemini_stream.call_args.kwargs["use_google_search"])
        self.assertIn("Google Search", gemini_stream.call_args.args[3])

    def test_chat_falls_back_without_search_warning_when_search_is_rejected(self):
        from translators import chat

        settings = {
            "model": "gemini-3.8-flash",
            "gemini_key": "configured-key",
            "source_language": "en",
            "target_language": "ar",
        }
        quota_error = RuntimeError(
            "Google Search request was rejected with HTTP 429. "
            "RESOURCE_EXHAUSTED. You exceeded your current quota.")
        with mock.patch(
            "translators.chat.gemini.stream_chat",
            side_effect=[quota_error, iter(("الجواب من معرفة النموذج.",))],
        ) as gemini_stream:
            answer = "".join(chat.reply_stream(
                [{"role": "user", "content": "في أي سنة ولد؟"}],
                settings,
                "source",
                "translation",
            ))

        self.assertIn("الجواب من معرفة النموذج.", answer)
        self.assertNotIn("تعذّر بحث Google", answer)
        self.assertTrue(
            gemini_stream.call_args_list[0].kwargs["use_google_search"])
        self.assertFalse(
            gemini_stream.call_args_list[1].kwargs["use_google_search"])
        self.assertIn(
            "Google Search is unavailable",
            gemini_stream.call_args_list[1].args[3],
        )

    def test_chat_does_not_fallback_on_non_search_errors(self):
        from translators import chat

        settings = {
            "model": "gemini-3.8-flash",
            "gemini_key": "configured-key",
            "target_language": "ar",
        }
        with mock.patch(
            "translators.chat.gemini.stream_chat",
            side_effect=RuntimeError("model unavailable"),
        ) as gemini_stream:
            with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                list(chat.reply_stream(
                    [{"role": "user", "content": "أهلا"}],
                    settings,
                    "source",
                    "translation",
                ))

        gemini_stream.assert_called_once()

    def test_non_streaming_chat_falls_back_without_search_warning(self):
        from translators import chat

        settings = {
            "model": "gemini-3.8-flash",
            "gemini_key": "configured-key",
            "target_language": "ar",
        }
        quota_error = RuntimeError(
            "Google Search request was rejected with HTTP 429. "
            "RESOURCE_EXHAUSTED.")
        with mock.patch(
            "translators.chat.gemini.chat",
            side_effect=[quota_error, "جواب غير موثق."],
        ) as gemini_chat:
            answer = chat.reply(
                [{"role": "user", "content": "أهلا"}],
                settings,
                "source",
                "translation",
            )

        self.assertIn("جواب غير موثق.", answer)
        self.assertNotIn("تعذّر بحث Google", answer)
        self.assertTrue(gemini_chat.call_args_list[0].kwargs["use_google_search"])
        self.assertFalse(gemini_chat.call_args_list[1].kwargs["use_google_search"])

    def test_regular_greeting_also_enables_google_search(self):
        from translators import chat

        settings = {
            "model": "gemini-2.5-flash",
            "gemini_key": "configured-key",
            "source_language": "en",
            "target_language": "ar",
        }
        with mock.patch(
            "translators.chat.gemini.stream_chat",
            return_value=iter(("وعليكم السلام",)),
        ) as gemini_stream:
            answer = "".join(chat.reply_stream(
                [{"role": "user", "content": "جاوب بالعربية"}],
                settings,
                "source",
                "translation",
            ))

        self.assertEqual(answer, "وعليكم السلام")
        self.assertTrue(gemini_stream.call_args.kwargs["use_google_search"])
        self.assertIn("Google Search", gemini_stream.call_args.args[3])

    def test_openai_compatible_chat_stream_requires_a_complete_reply(self):
        from translators import opencode_compat

        response = mock.Mock()
        response.status_code = 200
        response.iter_lines.return_value = [
            b'data: {"choices":[{"delta":{"content":"English "},"finish_reason":null}]}',
            b'data: {"choices":[{"delta":{"content":"answer."},"finish_reason":"stop"}]}',
            b"data: [DONE]",
        ]
        with mock.patch.object(
            opencode_compat, "_post_stream", return_value=response
        ) as post_stream:
            chunks = list(opencode_compat.stream_chat(
                [{"role": "user", "content": "Question"}],
                "https://example.test/v1",
                "configured-key",
                "test-model",
                "Reply in English.",
            ))

        self.assertEqual("".join(chunks), "English answer.")
        self.assertEqual(
            post_stream.call_args.args[0]["max_tokens"],
            8192,
        )
        response.close.assert_called_once()

    def test_opencode_zen_chat_stream_rejects_eof_without_finish_reason(self):
        from translators import opencode_compat, opencode_zen

        response = mock.Mock()
        response.status_code = 200
        response.iter_lines.return_value = [
            b'data: {"choices":[{"delta":{"content":"Complete "},"finish_reason":null}]}',
            b'data: {"choices":[{"delta":{"content":"reply."},"finish_reason":"stop"}]}',
            b"data: [DONE]",
        ]
        with mock.patch.object(
            opencode_zen.requests, "post", return_value=response
        ):
            chunks = list(opencode_zen.stream_chat(
                [{"role": "user", "content": "Question"}],
                "test-model",
                "Reply in English.",
            ))
        self.assertEqual("".join(chunks), "Complete reply.")

        response.close.reset_mock()
        response.iter_lines.return_value = [
            b'data: {"choices":[{"delta":{"content":"Incomplete"}}]}',
            b"data: [DONE]",
        ]
        with mock.patch.object(
            opencode_zen.requests, "post", return_value=response
        ):
            stream = opencode_zen.stream_chat(
                [{"role": "user", "content": "Question"}],
                "test-model",
                "Reply in English.",
            )
            self.assertEqual(next(stream), "Incomplete")
            with self.assertRaisesRegex(RuntimeError, "ended before"):
                next(stream)
        response.close.assert_called_once()

        response.close.reset_mock()
        response.iter_lines.return_value = [
            b'data: {"choices":[{"delta":{"content":"Incomplete"}}]}',
            b"data: [DONE]",
        ]
        with mock.patch.object(
            opencode_compat, "_post_stream", return_value=response
        ):
            stream = opencode_compat.stream_chat(
                [{"role": "user", "content": "Question"}],
                "https://example.test/v1",
                "configured-key",
                "test-model",
                "Reply in English.",
            )
            self.assertEqual(next(stream), "Incomplete")
            with self.assertRaisesRegex(RuntimeError, "ended before"):
                next(stream)
        response.close.assert_called_once()

    def test_gemini_stream_preserves_all_deltas_and_rejects_truncation(self):
        import requests
        from translators import gemini

        response = mock.Mock()
        response.status_code = 200
        response.iter_lines.return_value = [
            b'data: {"candidates":[{"content":{"parts":[{"text":"An English "}]}}]}',
            b'data: {"candidates":[{"content":{"parts":[{"text":"reply."}]},"finishReason":"STOP"}]}',
        ]
        with mock.patch.object(requests, "post", return_value=response) as post:
            chunks = list(gemini.stream_chat(
                [{"role": "user", "content": "Question"}],
                "configured-key",
                "gemini-2.5-flash",
                "Reply in English.",
            ))
        self.assertEqual("".join(chunks), "An English reply.")
        self.assertEqual(
            post.call_args.kwargs["json"]["generationConfig"]["maxOutputTokens"],
            8192,
        )
        self.assertEqual(
            post.call_args.kwargs["json"]["tools"],
            [{"google_search": {}}],
        )
        response.close.assert_called_once()

        response.close.reset_mock()
        response.iter_lines.return_value = [
            b'data: {"candidates":[{"content":{"parts":[{"text":"Incomplete"}]}}]}',
            b'data: {"candidates":[{"finishReason":"MAX_TOKENS"}]}',
        ]
        with mock.patch.object(requests, "post", return_value=response):
            stream = gemini.stream_chat(
                [{"role": "user", "content": "Question"}],
                "configured-key",
                "gemini-2.5-flash",
                "Reply in English.",
            )
            self.assertEqual(next(stream), "Incomplete")
            with self.assertRaisesRegex(RuntimeError, "cut off"):
                next(stream)
        response.close.assert_called_once()

    def test_gemini_chat_stream_returns_google_search_sources(self):
        import requests
        from translators import gemini

        response = mock.Mock()
        response.status_code = 200
        response.iter_lines.return_value = [
            b'data: {"candidates":[{"content":{"parts":[{"text":"This person wrote the book."}]},"groundingMetadata":{"groundingChunks":[{"web":{"uri":"https://example.com/author","title":"Author biography"}}]},"finishReason":"STOP"}]}',
        ]
        with mock.patch.object(requests, "post", return_value=response) as post:
            answer = "".join(gemini.stream_chat(
                [{"role": "user", "content": "Who is this author?"}],
                "configured-key",
                "gemini-3.5-flash-lite",
                "Use Google Search.",
            ))

        self.assertEqual(
            answer,
            "This person wrote the book.\n\nSources:\n"
            "- [Author biography](https://example.com/author)",
        )
        self.assertEqual(
            post.call_args.kwargs["json"]["tools"],
            [{"google_search": {}}],
        )
        response.close.assert_called_once()

    def test_gemini_chat_stream_can_skip_google_search(self):
        import requests
        from translators import gemini

        response = mock.Mock(status_code=200)
        response.iter_lines.return_value = [
            b'data: {"candidates":[{"content":{"parts":[{"text":"A clear explanation."}]},"finishReason":"STOP"}]}',
        ]
        with mock.patch.object(requests, "post", return_value=response) as post:
            answer = "".join(gemini.stream_chat(
                [{"role": "user", "content": "Explain this word."}],
                "configured-key",
                "gemini-3.5-flash-lite",
                "Explain the selected word from the passage.",
                use_google_search=False,
            ))

        self.assertEqual(answer, "A clear explanation.")
        self.assertNotIn("tools", post.call_args.kwargs["json"])
        response.close.assert_called_once()

    def test_gemini_chat_reports_search_quota_429_without_fallback(self):
        import requests
        from translators import gemini

        rejected = mock.Mock(status_code=429)
        rejected.json.return_value = {
            "error": {
                "status": "RESOURCE_EXHAUSTED",
                "message": "You exceeded your current quota.",
            },
        }
        with mock.patch.object(
            requests, "post", return_value=rejected,
        ) as post:
            with self.assertRaisesRegex(
                RuntimeError,
                "HTTP 429.*RESOURCE_EXHAUSTED.*exceeded your current quota",
            ):
                list(gemini.stream_chat(
                    [{"role": "user", "content": "When was he born?"}],
                    "configured-key",
                    "gemini-3.5-flash-lite",
                    "Answer the question.",
                    use_google_search=True,
                ))

        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.kwargs["json"].get("tools"))
        rejected.close.assert_called_once()

    def test_chat_worker_emits_the_accumulated_reply_without_dropping_tail(self):
        from popup.chat_worker import ChatWorker

        worker = ChatWorker(
            [{"role": "user", "content": "Question"}],
            {"model": "gemini-2.5-flash", "gemini_key": "configured-key"},
            "Original passage",
            "Translation",
        )
        partial = []
        done = []
        failed = []
        worker.partial.connect(partial.append)
        worker.done.connect(done.append)
        worker.failed.connect(failed.append)
        with mock.patch(
            "translators.chat.reply_stream",
            return_value=iter(("The English response ", "arrived in full.")),
        ):
            worker.run()

        self.assertEqual(
            partial,
            ["The English response ", "The English response arrived in full."],
        )
        self.assertEqual(done, ["The English response arrived in full."])
        self.assertEqual(failed, [])

    def test_reply_rejects_history_without_a_final_user_question(self):
        from translators.chat import reply

        with self.assertRaisesRegex(ValueError, "user message"):
            reply(
                [{"role": "assistant", "content": "answer"}],
                {"model": "space-bunny-free"},
                "original",
                "translation",
            )

    def test_popup_tools_and_chat_keep_translation_unchanged(self):
        from PySide6.QtCore import QObject, Qt, Signal
        from PySide6.QtTest import QTest
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
                self.translate_secs = 0.0

            def start(self):
                pass

            def isRunning(self):
                return False

        class StubChatWorker(QObject):
            done = Signal(str)
            failed = Signal(str)
            partial = Signal(str)
            finished = Signal()

            def __init__(self, messages, settings, source, translated):
                super().__init__()
                self.messages = messages
                self.settings = dict(settings)
                self.source = source
                self.translated = translated

            def start(self):
                pass

            def isRunning(self):
                return False

        settings = dict(
            DEFAULTS,
            explain_mode=True,
            explain_instructions="Explain musical terms in context.",
        )
        with (
            mock.patch(
                "popup.translate_popup.TranslateWorker",
                StubTranslateWorker,
            ),
            mock.patch(
                "popup.translate_popup.ChatWorker",
                StubChatWorker,
            ),
        ):
            popup = TranslatePopup(
                settings,
                source_text="original passage",
                floating_language_controls=True,
            )
        try:
            self.assertFalse(hasattr(popup, "chat_btn"))
            self.assertFalse(hasattr(popup, "explain_btn"))
            self.assertFalse(hasattr(popup, "instructions_btn"))
            header_layout = popup.language_action_layout
            self.assertEqual(
                popup.language_action_group.layoutDirection(),
                Qt.LayoutDirection.RightToLeft,
            )
            self.assertLess(
                header_layout.indexOf(popup.target_language_btn),
                header_layout.indexOf(popup.source_language_btn),
            )
            self.assertEqual(popup.chat_send_btn.text(), "Send")
            composer = popup.chat_composer
            self.assertEqual(composer.indexOf(popup.chat_input), 0)
            self.assertEqual(composer.indexOf(popup.chat_speed_btn), 1)
            self.assertEqual(composer.indexOf(popup.chat_send_btn), 2)
            self.assertEqual(popup.chat_send_btn.size().height(), 42)
            self.assertEqual(popup.chat_speed_btn.text(), "»")
            popup._on_done("Current translation", "test")
            self.assertGreaterEqual(
                popup.chat_transcript.font().pointSize(), 16)

            popup.show()
            self.app.processEvents()
            with (
                mock.patch("config.settings.load", return_value={}),
                mock.patch("config.settings.save") as save_settings,
            ):
                popup.chat_speed_btn.click()
                self.assertTrue(popup.speed_menu.isVisible())
                popup._speed_presets[1.25].click()
                self.assertEqual(popup.settings["speech_rate"], 1.25)
                self.assertIn("1.25x", popup.chat_speed_btn.toolTip())
                save_settings.assert_called_once()

            self.assertGreater(
                popup.target_language_btn.geometry().center().x(),
                popup.source_language_btn.geometry().center().x(),
            )
            popup._toggle_chat_panel(True)
            self.app.processEvents()
            popup._speech_available = True
            self.assertFalse(popup.chat_panel.isHidden())
            self.assertEqual(popup.content_splitter.count(), 2)
            self.assertEqual(
                popup.content_splitter.widget(0), popup.translation_area)
            self.assertEqual(
                popup.content_splitter.widget(1), popup.chat_panel)
            original_sizes = popup.content_splitter.sizes()
            popup.content_splitter.setSizes(
                [original_sizes[0] - 12, original_sizes[1] + 12])
            self.app.processEvents()
            resized = popup.content_splitter.sizes()
            self.assertGreater(resized[1], original_sizes[1])

            popup.chat_input.setPlainText("Why?")
            with mock.patch("popup.translate_popup.ChatWorker", StubChatWorker):
                popup.chat_send_btn.click()
                worker = popup._chat_worker
                self.assertFalse(popup.chat_send_btn.isEnabled())
                self.assertEqual(
                    worker.settings["explain_instructions"],
                    "Explain musical terms in context.",
                )
                self.assertEqual(worker.source, "original passage")
                self.assertEqual(worker.translated, "Current translation")
                self.assertEqual(
                    popup.result.toPlainText(), "Current translation")

                live_answer = "This English reply is arriving in full."
                worker.partial.emit(live_answer)
                popup._chat_render_timer.stop()
                popup._render_chat_transcript()
                self.assertIn(
                    live_answer, popup.chat_transcript.toPlainText())
                worker.done.emit("هذا **شرح** للنص.")
                worker.finished.emit()
            self.app.processEvents()
            self.assertEqual(
                [message["role"] for message in popup._chat_messages],
                ["user", "assistant"],
            )
            last_block = popup.chat_transcript.document().lastBlock()
            self.assertEqual(
                last_block.blockFormat().layoutDirection(),
                Qt.LayoutDirection.RightToLeft,
            )
            self.assertEqual(
                last_block.blockFormat().alignment(),
                Qt.AlignmentFlag.AlignLeft,
            )
            markdown_word = popup.chat_transcript.document().find("شرح")
            self.assertEqual(
                markdown_word.block().blockFormat().layoutDirection(),
                Qt.LayoutDirection.RightToLeft,
            )
            self.assertEqual(
                markdown_word.block().blockFormat().alignment(),
                Qt.AlignmentFlag.AlignLeft,
            )
            self.assertIn("\u202b", popup.chat_transcript.toPlainText())
            self.assertEqual(markdown_word.charFormat().fontWeight(), 700)
            self.assertIn("dir='rtl'", popup.chat_transcript.toHtml())
            self.assertIn("\u202b", popup.chat_transcript.toPlainText())
            self.assertIn("\u202c", popup.chat_transcript.toPlainText())
            self.assertEqual(
                popup._current_speech_text(),
                "هذا شرح للنص.",
            )
            self.assertEqual(popup.speak_btn.text(), "Read answer")
            self.assertEqual(popup.result.toPlainText(), "Current translation")
            self.assertEqual(popup.chat_send_btn.text(), "Send")
            self.assertTrue(popup.chat_send_btn.isEnabled())

            popup.chat_input.setPlainText("One more question")
            with mock.patch("popup.translate_popup.ChatWorker", StubChatWorker):
                popup.chat_send_btn.click()
                failed_worker = popup._chat_worker
                failed_worker.partial.emit("An English reply that stopped")
                popup._chat_render_timer.stop()
                popup._render_chat_transcript()
                failed_worker.failed.emit("Provider stopped before completion")
                failed_worker.finished.emit()
            self.app.processEvents()
            self.assertEqual(
                [message["role"] for message in popup._chat_messages],
                ["user", "assistant"],
            )
            transcript = popup.chat_transcript.toPlainText()
            self.assertIn("Partial answer", transcript)
            self.assertIn("An English reply that stopped", transcript)
            self.assertIn("Provider stopped before completion", transcript)

            word_cursor = popup.chat_transcript.document().find("شرح")
            word_rect = popup.chat_transcript.cursorRect(word_cursor)
            first_arabic_word = popup.chat_transcript.document().find("هذا")
            first_word_rect = popup.chat_transcript.cursorRect(first_arabic_word)
            self.assertGreater(
                first_word_rect.center().x(),
                popup.chat_transcript.viewport().width() // 2,
            )
            with mock.patch.object(popup, "_request_speech") as request_speech:
                QTest.mouseClick(
                    popup.chat_transcript.viewport(),
                    Qt.MouseButton.LeftButton,
                    pos=word_rect.center(),
                )
            self.assertEqual(request_speech.call_args.args, (0,))
            self.assertTrue(popup._speech_text_override)
            self.assertIsNotNone(popup._chat_speech_document_start)
            clicked_selection = popup.chat_transcript.extraSelections()
            self.assertEqual(clicked_selection, [])
            popup._speech_active = True
            popup._speech_boundaries = {
                0: [(0, 100_000_000, 0, min(3, len(popup._speech_text_override)))]
            }
            popup._update_speech_highlight()
            selections = popup.chat_transcript.extraSelections()
            self.assertEqual(len(selections), 1)
            self.assertEqual(
                selections[0].format.background().color().name(),
                "#b3e5fc",
            )
            self.assertTrue(selections[0].cursor.selectedText())

            popup._speech_active = False
            popup._clear_speech_highlight()
            popup._chat_entries = [{
                "role": "assistant",
                "content": "😀 first second",
                "speaker": "Tutor",
            }]
            popup._render_chat_transcript()
            second_word = popup.chat_transcript.document().find("second")
            popup._speech_available = True
            with mock.patch.object(popup, "_request_speech"):
                popup._on_chat_transcript_clicked(
                    second_word.selectionStart())
            self.assertEqual(popup._speech_text_override, "second")
            selections = popup.chat_transcript.extraSelections()
            self.assertEqual(
                selections,
                [],
                "chat text is highlighted when measured speech boundaries arrive",
            )

            popup._speech_active = False
            popup._clear_speech_highlight()

            with mock.patch(
                "popup.translate_popup.TranslateWorker", StubTranslateWorker
            ):
                popup.refresh_display(dict(
                    settings,
                    theme="Nordic Gray",
                    explain_mode=False,
                    explain_instructions="Explain musical terms in context.",
                ))
            popup._on_done("Current translation", "Gemini")
            self.assertFalse(hasattr(popup, "chat_btn"))
            self.assertTrue(popup.chat_panel.isHidden())
            self.assertFalse(hasattr(popup, "explain_btn"))
        finally:
            popup.close()

    def test_translation_text_emits_clicked_position(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from popup.translate_popup import TranslationTextEdit

        editor = TranslationTextEdit()
        editor.setPlainText("click target")
        editor.resize(320, 100)
        editor.show()
        try:
            positions = []
            backgrounds = []
            editor.positionClicked.connect(positions.append)
            editor.backgroundClicked.connect(
                lambda: backgrounds.append(True))
            self.app.processEvents()
            cursor = editor.document().find("target")
            cursor.setPosition(cursor.selectionStart() + 2)
            word_point = editor.cursorRect(cursor).center()
            QTest.mouseClick(
                editor.viewport(), Qt.MouseButton.LeftButton, pos=word_point)
            self.app.processEvents()
            self.assertEqual(len(positions), 1)
            self.assertGreaterEqual(positions[0], 0)
            self.assertEqual(backgrounds, [])
            QTest.mouseClick(
                editor.viewport(), Qt.MouseButton.LeftButton,
                pos=QPoint(64, 10))
            self.app.processEvents()
            self.assertEqual(len(positions), 1)
            self.assertEqual(backgrounds, [True])
            QTest.mouseClick(
                editor.viewport(), Qt.MouseButton.LeftButton,
                pos=QPoint(300, 90))
            self.app.processEvents()
            self.assertEqual(backgrounds, [True, True])
        finally:
            editor.close()

    def test_translation_text_selection_does_not_emit_read_click(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from popup.translate_popup import TranslationTextEdit

        editor = TranslationTextEdit()
        editor.setPlainText("first second third")
        editor.resize(320, 100)
        editor.show()
        try:
            positions = []
            backgrounds = []
            editor.positionClicked.connect(positions.append)
            editor.backgroundClicked.connect(
                lambda: backgrounds.append(True))
            self.app.processEvents()
            first = editor.document().find("first")
            last = editor.document().find("third")
            start = editor.cursorRect(first).center()
            end = editor.cursorRect(last).center()
            QTest.mousePress(
                editor.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(editor.viewport(), end, delay=10)
            QTest.mouseRelease(
                editor.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.app.processEvents()
            self.assertTrue(editor.textCursor().hasSelection())
            self.assertEqual(positions, [])
            self.assertEqual(backgrounds, [])
        finally:
            editor.close()

    def test_translation_context_menu_keeps_explain_actions(self):
        from PySide6.QtGui import QTextCursor
        from popup.translate_popup import TranslationTextEdit

        editor = TranslationTextEdit()
        editor.setPlainText("selected translation")
        cursor = QTextCursor(editor.document())
        cursor.setPosition(0)
        cursor.setPosition(len("selected"), QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        try:
            requests = []
            editor.explainRequested.connect(
                lambda style, text: requests.append((style, text)))
            menu = editor.create_context_menu()
            try:
                self.assertEqual(
                    menu.styleSheet(),
                    "QMenu::item { font-size: 17px; padding: 9px 24px; }")
                self.assertTrue(all(
                    action.icon().isNull()
                    and action.shortcut().isEmpty()
                    for action in menu.actions()
                    if not action.isSeparator()))
                actions = [
                    action.text().replace("&", "").split("\t", 1)[0]
                    for action in menu.actions()
                    if not action.isSeparator()
                ]
                self.assertEqual(
                    actions,
                    [
                        "Copy",
                        "Select All",
                        "Explain linguistically",
                        "Explain etymologically",
                        "Explain simply",
                        "Explain in context",
                    ],
                )
                explanation_actions = [
                    action for action in menu.actions()
                    if action.text().startswith("Explain ")
                ]
                self.assertTrue(all(
                    action.isEnabled() for action in explanation_actions))
                for action, style in zip(
                    explanation_actions,
                    ("linguistic", "etymological", "simple", "context"),
                ):
                    action.trigger()
                    self.assertEqual(requests[-1], (style, "selected"))
            finally:
                menu.close()

            editor.moveCursor(QTextCursor.MoveOperation.End)
            menu = editor.create_context_menu()
            try:
                self.assertEqual(
                    [action.text() for action in menu.actions()
                     if not action.isSeparator()],
                    [
                        "Copy",
                        "Select All",
                        "Explain linguistically",
                        "Explain etymologically",
                        "Explain simply",
                        "Explain in context",
                    ],
                )
                self.assertTrue(all(
                    not action.isEnabled()
                    for action in menu.actions()
                    if action.text().startswith("Explain ")
                ))
            finally:
                menu.close()
        finally:
            editor.close()

    def test_translation_popup_explain_actions_leave_embedded_chat_closed(self):
        from PySide6.QtGui import QTextCursor
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
                self.translate_secs = 0.0

            def start(self):
                pass

            def isRunning(self):
                return False

        with mock.patch(
            "popup.translate_popup.TranslateWorker",
            StubTranslateWorker,
        ):
            popup = TranslatePopup(
                dict(DEFAULTS),
                source_text="The original passage",
            )
        try:
            popup.result.setPlainText("selected words")
            cursor = QTextCursor(popup.result.document())
            cursor.select(QTextCursor.SelectionType.Document)
            popup.result.setTextCursor(cursor)
            requests = []
            popup.explanationRequested.connect(
                lambda style, text: requests.append((style, text)))
            menu = popup.result.create_context_menu()
            try:
                explain_action = next(
                    action for action in menu.actions()
                    if action.text() == "Explain linguistically")
                explain_action.trigger()
                self.assertEqual(requests, [
                    ("linguistic", "selected words"),
                ])
            finally:
                menu.close()

            popup.show()
            self.app.processEvents()
            self.assertTrue(popup.chat_panel.isHidden())
            self.assertEqual(popup.content_splitter.count(), 2)
            self.assertFalse(popup.content_splitter.widget(1).isVisible())
            self.assertEqual(popup.layout().indexOf(popup.content_splitter), 1)
        finally:
            popup.close()

    def test_chat_transcript_selection_does_not_emit_read_click(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from popup.translate_popup import ChatTranscriptBrowser

        transcript = ChatTranscriptBrowser()
        transcript.setPlainText("first second third")
        transcript.resize(320, 100)
        transcript.show()
        try:
            clicked = []
            transcript.paragraphClicked.connect(clicked.append)
            self.app.processEvents()
            first = transcript.document().find("first")
            first.setPosition(first.selectionStart() + 2)
            click_point = transcript.cursorRect(first).center()
            QTest.mousePress(
                transcript.viewport(), Qt.MouseButton.LeftButton,
                pos=click_point)
            self.app.processEvents()
            self.assertEqual(clicked, [])
            QTest.mouseRelease(
                transcript.viewport(), Qt.MouseButton.LeftButton,
                pos=click_point)
            self.app.processEvents()
            self.assertEqual(len(clicked), 1)

            start = transcript.cursorRect(first).center()
            last = transcript.document().find("third")
            end = transcript.cursorRect(last).center()
            QTest.mousePress(
                transcript.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(transcript.viewport(), end, delay=10)
            QTest.mouseRelease(
                transcript.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.app.processEvents()
            self.assertTrue(transcript.textCursor().hasSelection())
            self.assertEqual(len(clicked), 1)
        finally:
            transcript.close()

    def test_speech_prefetch_uses_the_configured_debounce(self):
        from popup.translate_popup import (
            SPEECH_PREFETCH_DELAY_MS, TranslatePopup,
        )

        with mock.patch.object(TranslatePopup, "_start_translation_worker"):
            popup = TranslatePopup({})
        try:
            self.assertEqual(
                popup._speech_prefetch_timer.interval(),
                SPEECH_PREFETCH_DELAY_MS,
            )
        finally:
            popup.close()

    def test_close_waits_for_translation_worker_without_terminating_it(self):
        from PySide6.QtGui import QCloseEvent
        from popup.translate_popup import TranslatePopup

        with mock.patch.object(TranslatePopup, "_start_translation_worker"):
            popup = TranslatePopup({})
        worker = mock.Mock()
        worker.isRunning.return_value = True
        popup._translation_workers = [worker]
        event = QCloseEvent()
        with mock.patch.object(popup, "_save_geometry"):
            try:
                popup.closeEvent(event)

                self.assertFalse(event.isAccepted())
                self.assertFalse(popup.isVisible())
                self.assertTrue(popup._close_pending)
                worker.requestInterruption.assert_called_once_with()
                worker.terminate.assert_not_called()
                worker.wait.assert_not_called()

                popup._translation_workers.clear()
                popup._continue_pending_close()
                self.app.processEvents()
                self.assertFalse(popup._close_pending)
            finally:
                popup._translation_workers.clear()
                popup._close_pending = False
                popup.close()

    def test_clicking_during_speech_queues_seek_and_highlights_word(self):
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
                self.translate_secs = 0.0

            def start(self):
                pass

            def isRunning(self):
                return False

        with mock.patch(
            "popup.translate_popup.TranslateWorker", StubTranslateWorker,
        ):
            popup = TranslatePopup(dict(DEFAULTS), source_text="source")
        try:
            popup.result.setPlainText("😀 first second third")
            popup._speech_active = True
            worker = mock.Mock()
            worker.isRunning.return_value = True
            popup._speech_worker = worker

            word = popup.result.document().find("second")
            with (
                mock.patch.object(
                    popup, "_seek_in_cached_read", return_value=False),
                mock.patch.object(popup, "_begin_speech") as begin,
            ):
                popup._on_translation_position_clicked(word.selectionStart())

            begin.assert_not_called()
            self.assertEqual(popup._pending_speech_offset, 8)
            worker.requestInterruption.assert_called_once_with()
            selections = popup.result.extraSelections()
            self.assertEqual(len(selections), 1)
            self.assertEqual(selections[0].cursor.selectedText(), "second")

            restarted_worker = mock.Mock()
            restarted_worker.segment_stream_ready = mock.Mock()
            restarted_worker.word_boundary_ready = mock.Mock()
            restarted_worker.word_boundaries = mock.Mock()
            restarted_worker.segment_ready = mock.Mock()
            restarted_worker.failed = mock.Mock()
            restarted_worker.finished = mock.Mock()
            with mock.patch(
                "popup.translate_popup.AudioStreamServer",
                return_value=mock.Mock(),
            ), mock.patch(
                "popup.translate_popup.SpeechWorker",
                return_value=restarted_worker,
            ) as speech_worker:
                popup._speech_worker = None
                popup._begin_speech(8)
            self.assertFalse(speech_worker.call_args.kwargs["stream_audio"])
            selections = popup.result.extraSelections()
            self.assertEqual(len(selections), 1)
            self.assertEqual(selections[0].cursor.selectedText(), "second")

            worker.requestInterruption.reset_mock()
            popup._speech_active = True
            popup._pending_speech_offset = None
            popup._speech_waiting = False
            popup._speech_worker = worker
            popup._speech_paths = {0: "ready-audio.mp3"}
            popup._speech_boundaries = {
                0: [
                    (0, 1_000_000, 2, 7),
                    (20_005_001, 1_000_000, 8, 14),
                ],
            }
            with mock.patch.object(popup, "_play_speech_segment") as play:
                popup._on_translation_position_clicked(word.selectionStart())
            play.assert_called_once_with("ready-audio.mp3", 2_000)
            worker.requestInterruption.assert_not_called()

            popup._speech_worker = None
            popup._speech_active = True
            popup._pending_speech_offset = None
            popup._speech_waiting = False
            popup._speech_paths.clear()
            popup._speech_boundaries.clear()
            with mock.patch.object(popup, "_begin_speech") as begin:
                popup._on_translation_position_clicked(word.selectionStart())
            begin.assert_called_once_with(8)
            self.assertIsNone(popup._pending_speech_offset)
            self.assertFalse(popup._speech_waiting)

            popup._speech_active = False
            popup._clear_speech_highlight()
            popup._speech_available = True

            popup._speech_active = False
            popup._clear_speech_highlight()
            popup._chat_entries = [{
                "role": "assistant",
                "content": "😀 first second",
                "speaker": "Tutor",
            }]
            popup._toggle_chat_panel(True)
            popup._render_chat_transcript()
            second_word = popup.chat_transcript.document().find("second")
            with mock.patch.object(popup, "_request_speech"):
                popup._on_chat_transcript_clicked(
                    second_word.selectionStart())
            self.assertEqual(popup._speech_text_override, "second")
            selections = popup.chat_transcript.extraSelections()
            self.assertEqual(selections, [])

            class ChatResultEmitter(QObject):
                done = Signal(str)

            emitter = ChatResultEmitter()
            popup._chat_worker = emitter
            popup._speech_active = True
            popup.speak_btn.setText("Stop")
            popup.speak_btn.setEnabled(True)
            emitter.done.connect(popup._on_chat_done)
            emitter.done.emit("Answer completed during speech.")
            self.assertEqual(popup.speak_btn.text(), "Stop")
            self.assertTrue(popup.speak_btn.isEnabled())
        finally:
            popup._speech_worker = None
            popup._speech_active = False
            popup._pending_speech_offset = None
            popup.close()


if __name__ == "__main__":
    unittest.main()
