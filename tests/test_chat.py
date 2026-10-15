import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestTranslationChat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

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
        messages = [{"role": "user", "content": "Explain this phrase."}]
        with mock.patch(
            "translators.chat.gemini.chat", return_value="Réponse"
        ) as gemini_chat:
            answer = chat.reply(messages, settings, "source", "translation")

        self.assertEqual(answer, "Réponse")
        gemini_chat.assert_called_once()
        self.assertEqual(gemini_chat.call_args.args[:3], (
            messages, "configured-key", "gemini-2.5-flash"))
        self.assertIn("Reply in French", gemini_chat.call_args.args[3])

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
            self.assertFalse(popup.chat_btn.isHidden())
            self.assertFalse(popup.explain_btn.isHidden())
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
            self.assertLess(
                header_layout.indexOf(popup.target_language_btn),
                header_layout.indexOf(popup.chat_btn))
            self.assertLess(
                header_layout.indexOf(popup.chat_btn),
                header_layout.indexOf(popup.explain_btn))
            self.assertEqual(popup.explain_btn.size().width(), 84)
            self.assertEqual(popup.explain_btn.size().height(), 26)
            self.assertIn("color: #3f4a57", popup.explain_btn.styleSheet())
            self.assertIn(
                "border: 1px solid #d8dde4",
                popup.explain_btn.styleSheet(),
            )
            self.assertEqual(popup.chat_send_btn.text(), "Send")
            composer = popup.chat_composer
            self.assertEqual(composer.indexOf(popup.chat_input), 0)
            self.assertEqual(composer.indexOf(popup.chat_send_btn), 1)
            popup._on_done("Current translation", "test")
            self.assertGreaterEqual(
                popup.chat_transcript.font().pointSize(), 16)

            popup.show()
            self.app.processEvents()
            self.assertGreater(
                popup.target_language_btn.geometry().center().x(),
                popup.source_language_btn.geometry().center().x(),
            )
            self.assertGreater(
                popup.target_language_btn.geometry().center().x(),
                popup.chat_btn.geometry().center().x(),
            )
            self.assertGreater(
                popup.chat_btn.geometry().center().x(),
                popup.explain_btn.geometry().center().x(),
            )
            popup.chat_btn.click()
            self.app.processEvents()
            popup._speech_available = True
            self.assertFalse(popup.chat_panel.isHidden())
            self.assertEqual(popup.chat_btn.text(), "اشرح بعمق")
            self.assertEqual(popup.content_splitter.count(), 2)
            self.assertEqual(
                popup.content_splitter.widget(0), popup.translation_area)
            self.assertEqual(
                popup.content_splitter.widget(1), popup.chat_panel)
            self.assertIn(
                "QSplitter#translationChatSplitter::handle:vertical",
                popup.content_splitter.styleSheet(),
            )
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
            self.assertEqual(popup.chat_btn.text(), "اشرح بعمق")
            self.assertEqual(popup.chat_send_btn.text(), "Send")
            self.assertTrue(popup.chat_send_btn.isEnabled())

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
            self.assertFalse(popup.chat_btn.isHidden())
            self.assertTrue(popup.chat_panel.isHidden())
            self.assertFalse(popup.explain_btn.isChecked())
            self.assertIn("#88C0D0", popup.explain_btn.styleSheet())

            with (
                mock.patch(
                    "popup.translate_popup.load",
                    return_value=dict(
                        settings,
                        explain_instructions="Explain musical terms in context.",
                    ),
                ),
                mock.patch("popup.translate_popup.save") as save_settings,
                mock.patch(
                    "popup.translate_popup.TranslateWorker",
                    StubTranslateWorker,
                ),
            ):
                popup.explain_btn.click()
            self.assertTrue(popup.explain_btn.isChecked())
            self.assertIn(
                "QPushButton#explainModeButton:checked",
                popup.explain_btn.styleSheet(),
            )
            self.assertTrue(save_settings.call_args.args[0]["explain_mode"])
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
            editor.positionClicked.connect(positions.append)
            QTest.mouseClick(
                editor.viewport(), Qt.MouseButton.LeftButton,
                pos=QPoint(20, 10))
            self.app.processEvents()
            self.assertEqual(len(positions), 1)
            self.assertGreaterEqual(positions[0], 0)
        finally:
            editor.close()

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
