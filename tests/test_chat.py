import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestTranslationChat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_popup_menu_highlight_keeps_readable_text(self):
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        with mock.patch.object(TranslatePopup, "_start_translation_worker"):
            popup = TranslatePopup(
                dict(DEFAULTS), floating_language_controls=True)
        try:
            self.assertIn(
                "background-color: #fff2a8; color: #222831",
                popup.styleSheet(),
            )
        finally:
            popup.close()

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

    def test_translation_rendering_preserves_latex_table_alignment_in_arabic(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QTextTable
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup
        from translators.chain import normalize_latex_output

        markdown = (
            r"\begin{tabular}{|l|c|r|}\hline" "\n"
            r"يسار & وسط & يمين \\" "\n"
            r"\hline" "\n"
            r"أ & ب & ج \\" "\n"
            r"\hline\end{tabular}"
        )
        markdown = normalize_latex_output(markdown)
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
                cell_block = table.cellAt(0, column).firstCursorPosition().block()
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

    def test_openai_compatible_chat_stream_yields_response_deltas(self):
        from translators import opencode_compat

        response = mock.Mock()
        response.status_code = 200
        response.iter_lines.return_value = [
            b'data: {"choices":[{"delta":{"content":"first "}}]}',
            b'data: {"choices":[{"delta":{"content":"second"}}]}',
            b"data: [DONE]",
        ]
        with mock.patch.object(
            opencode_compat, "_post_stream", return_value=response,
        ) as post_stream:
            chunks = list(opencode_compat.stream_chat(
                [{"role": "user", "content": "Question"}],
                "https://api.example/v1",
                "key",
                "test-model",
                "System",
            ))

        self.assertEqual(chunks, ["first ", "second"])
        self.assertTrue(post_stream.call_args.args[0]["stream"])
        response.close.assert_called_once_with()

    def test_openai_compatible_chat_stream_closes_on_http_error(self):
        import requests
        from translators import opencode_compat

        response = mock.Mock()
        response.status_code = 401
        response.raise_for_status.side_effect = requests.HTTPError("unauthorized")
        with mock.patch.object(
            opencode_compat, "_post_stream", return_value=response,
        ):
            with self.assertRaisesRegex(requests.HTTPError, "unauthorized"):
                list(opencode_compat.stream_chat(
                    [{"role": "user", "content": "Question"}],
                    "https://api.example/v1",
                    "key",
                    "test-model",
                    "System",
                ))

        response.close.assert_called_once_with()

    def test_gemini_chat_stream_yields_response_deltas(self):
        from translators import gemini

        response = mock.Mock()
        response.status_code = 200
        response.iter_lines.return_value = [
            b'data: {"candidates":[{"content":{"parts":[{"text":"William "}]}}]}',
            b'data: {"candidates":[{"content":{"parts":[{"text":"Taylor."}]},'
            b'"finishReason":"STOP","groundingMetadata":{"groundingChunks":['
            b'{"web":{"uri":"https://example.com/taylor",'
            b'"title":"Biography"}}]}}]}',
        ]
        with mock.patch(
            "translators.gemini.requests.post", return_value=response,
        ) as post:
            chunks = list(gemini.chat_stream(
                [{"role": "user", "content": "Question"}],
                "key",
                "gemini-test",
                "System",
            ))

        self.assertEqual(
            chunks,
            [
                "William ",
                "Taylor.",
                "\n\nSources:\n- [Biography](https://example.com/taylor)",
            ],
        )
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertEqual(
            post.call_args.kwargs["json"]["tools"],
            [gemini.GOOGLE_SEARCH_TOOL],
        )
        response.close.assert_called_once_with()

    def test_gemini_chat_request_enables_google_search_and_cites_sources(self):
        from translators import gemini

        response = mock.Mock()
        response.json.return_value = {
            "candidates": [{
                "content": {"parts": [{"text": "Born in 1872; died in 1922."}]},
                "finishReason": "STOP",
                "groundingMetadata": {
                    "groundingChunks": [{
                        "web": {
                            "uri": "https://example.com/taylor",
                            "title": "Biography",
                        },
                    }],
                },
            }],
        }
        with mock.patch(
            "translators.gemini.requests.post", return_value=response,
        ) as post:
            answer = gemini.chat(
                [{"role": "user", "content": "When was he born and when did he die?"}],
                "key",
                "gemini-test",
                "Use Google Search for missing facts.",
            )

        self.assertEqual(
            answer,
            "Born in 1872; died in 1922.\n\n"
            "Sources:\n- [Biography](https://example.com/taylor)",
        )
        self.assertEqual(
            post.call_args.kwargs["json"]["tools"],
            [gemini.GOOGLE_SEARCH_TOOL],
        )

    def test_gemini_chat_stream_retries_transient_service_unavailable(self):
        from translators import gemini

        unavailable = mock.Mock()
        unavailable.status_code = 503
        response = mock.Mock()
        response.status_code = 200
        response.iter_lines.return_value = [
            b'data: {"candidates":[{"content":{"parts":[{"text":"Reply"}]},'
            b'"finishReason":"STOP"}]}',
        ]
        with mock.patch(
            "translators.gemini.requests.post",
            side_effect=[unavailable, response],
        ) as post, mock.patch("translators.gemini.time.sleep") as sleep:
            chunks = list(gemini.chat_stream(
                [{"role": "user", "content": "Question"}],
                "key",
                "gemini-test",
                "System",
            ))

        self.assertEqual(chunks, ["Reply"])
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(gemini.RETRY_BACKOFF[0])
        unavailable.close.assert_called_once_with()
        response.close.assert_called_once_with()

    def test_gemini_chat_stream_retries_timeout_before_receiving_text(self):
        import requests
        from translators import gemini

        response = mock.Mock()
        response.status_code = 200
        response.iter_lines.return_value = [
            b'data: {"candidates":[{"content":{"parts":[{"text":"Reply"}]},'
            b'"finishReason":"STOP"}]}',
        ]
        with mock.patch(
            "translators.gemini.requests.post",
            side_effect=[requests.ReadTimeout("read timed out"), response],
        ) as post, mock.patch("translators.gemini.time.sleep") as sleep:
            chunks = list(gemini.chat_stream(
                [{"role": "user", "content": "Question"}],
                "key",
                "gemini-test",
                "System",
            ))

        self.assertEqual(chunks, ["Reply"])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            post.call_args.kwargs["timeout"], gemini.CHAT_TIMEOUT)
        sleep.assert_called_once_with(gemini.RETRY_BACKOFF[0])
        response.close.assert_called_once_with()

    def test_gemini_chat_stream_retries_read_timeout_before_first_chunk(self):
        import requests
        from translators import gemini

        timed_out = mock.Mock()
        timed_out.status_code = 200
        timed_out.iter_lines.side_effect = requests.ReadTimeout(
            "read timed out")
        response = mock.Mock()
        response.status_code = 200
        response.iter_lines.return_value = [
            b'data: {"candidates":[{"content":{"parts":[{"text":"Reply"}]},'
            b'"finishReason":"STOP"}]}',
        ]
        with mock.patch(
            "translators.gemini.requests.post",
            side_effect=[timed_out, response],
        ) as post, mock.patch("translators.gemini.time.sleep"):
            chunks = list(gemini.chat_stream(
                [{"role": "user", "content": "Question"}],
                "key",
                "gemini-test",
                "System",
            ))

        self.assertEqual(chunks, ["Reply"])
        self.assertEqual(post.call_count, 2)
        timed_out.close.assert_called_once_with()
        response.close.assert_called_once_with()

    def test_gemini_chat_stream_does_not_retry_timeout_after_partial_reply(self):
        import requests
        from translators import gemini

        response = mock.Mock()
        response.status_code = 200

        def partial_then_timeout():
            yield (
                b'data: {"candidates":[{"content":{"parts":[{"text":"First"}]}}]}'
            )
            raise requests.ReadTimeout("read timed out")

        response.iter_lines.side_effect = partial_then_timeout
        with mock.patch(
            "translators.gemini.requests.post", return_value=response,
        ) as post:
            with self.assertRaises(requests.ReadTimeout):
                list(gemini.chat_stream(
                    [{"role": "user", "content": "Question"}],
                    "key",
                    "gemini-test",
                    "System",
                ))

        post.assert_called_once()
        response.close.assert_called_once_with()

    def test_gemini_chat_stream_closes_on_http_error(self):
        import requests
        from translators import gemini

        response = mock.Mock()
        response.status_code = 401
        response.raise_for_status.side_effect = requests.HTTPError("unauthorized")
        with mock.patch(
            "translators.gemini.requests.post", return_value=response,
        ):
            with self.assertRaisesRegex(requests.HTTPError, "unauthorized"):
                list(gemini.chat_stream(
                    [{"role": "user", "content": "Question"}],
                    "key",
                    "gemini-test",
                    "System",
                ))

        response.close.assert_called_once_with()

    def test_opencode_cli_chat_stream_yields_json_text_events(self):
        import io
        from translators import opencode_cli

        class FakeProcess:
            def __init__(self):
                self.stdin = mock.Mock()
                self.stdout = io.StringIO(
                    '{"type":"text","part":{"text":"first "}}\n'
                    '{"type":"step-finish","part":{}}\n'
                    '{"type":"text","part":{"text":"second"}}\n'
                )

            def wait(self):
                return 0

            def poll(self):
                return 0

            def kill(self):
                pass

        process = FakeProcess()
        timer = mock.Mock()
        with mock.patch.object(
            opencode_cli, "_executable", return_value="opencode",
        ), mock.patch.object(
            opencode_cli.subprocess, "Popen", return_value=process,
        ) as popen, mock.patch.object(
            opencode_cli.threading, "Timer", return_value=timer,
        ):
            chunks = list(opencode_cli.chat_stream(
                [
                    {"role": "user", "content": "Question"},
                    {"role": "assistant", "content": "Earlier answer"},
                    {"role": "user", "content": "Follow-up"},
                ],
                "openai/test-model",
                "System prompt",
            ))

        self.assertEqual(chunks, ["first ", "second"])
        self.assertEqual(
            process.stdin.write.call_args.args[0],
            "Student:\nQuestion\n\nTutor:\nEarlier answer\n\nStudent:\nFollow-up",
        )
        process.stdin.close.assert_called_once_with()
        self.assertIn("--format", popen.call_args.args[0])
        timer.start.assert_called_once_with()
        timer.cancel.assert_called_once_with()

    def test_reply_stream_uses_opencode_cli_stream(self):
        from translators import chat

        messages = [{"role": "user", "content": "Question"}]
        settings = {
            "model": "opencode-cli:openai/test-model",
            "source_language": "en",
            "target_language": "ar",
        }
        with mock.patch(
            "translators.chat.opencode_cli.chat_stream",
            return_value=iter(["First ", "second."]),
        ) as cli_stream:
            chunks = list(chat.reply_stream(messages, settings, "", ""))

        self.assertEqual(chunks, ["First ", "second."])
        self.assertEqual(cli_stream.call_args.args[0], messages)
        self.assertEqual(cli_stream.call_args.args[1], "openai/test-model")

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

    def test_reply_stream_uses_selected_bridge_and_yields_deltas(self):
        from translators import chat

        messages = [{"role": "user", "content": "Why is this chord here?"}]
        settings = {
            "model": "space-bunny-free",
            "source_language": "en",
            "target_language": "ar",
        }
        with mock.patch(
            "translators.chat.opencode_bridge.chat_stream",
            return_value=iter(["شرح ", "متدرج"]),
        ) as bridge_stream:
            chunks = list(chat.reply_stream(
                messages, settings, "", "النص المترجم"))

        self.assertEqual(chunks, ["شرح ", "متدرج"])
        self.assertEqual(bridge_stream.call_args.args[0], messages)

    def test_chat_worker_emits_partial_reply_before_completion(self):
        from PySide6.QtTest import QSignalSpy
        from popup.chat_worker import ChatWorker

        worker = ChatWorker(
            [{"role": "user", "content": "Question"}], {}, "", "")
        partial_spy = QSignalSpy(worker.partial)
        done_spy = QSignalSpy(worker.done)
        with mock.patch(
            "translators.chat.reply_stream",
            return_value=iter(["First ", "second."]),
        ):
            worker.run()

        self.assertEqual(partial_spy.count(), 2)
        self.assertEqual(partial_spy.at(0), ["First "])
        self.assertEqual(partial_spy.at(1), ["First second."])
        self.assertEqual(done_spy.at(0), ["First second."])

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

    def test_general_chat_does_not_inject_book_context(self):
        from translators.chat import _model_messages

        system, messages = _model_messages(
            [{"role": "user", "content": "What is a cadence?"}],
            "",
            "",
            "auto",
            "ar",
        )

        self.assertEqual(
            messages, [{"role": "user", "content": "What is a cadence?"}])
        self.assertIn("general-purpose conversational AI assistant", system)
        self.assertIn("do not assume a music-related context", system)
        self.assertIn(
            "when the user asks you to look something up, search the web "
            "with Google Search before answering",
            system,
        )
        self.assertNotIn(
            "Do not assume access to a book, document, image", system)
        self.assertIn("Reply in Arabic", system)
        self.assertNotIn("Original passage:", system)
        self.assertNotIn("Translation:", system)

    def test_chat_prompt_uses_selected_reply_language_only(self):
        from translators import chat

        with mock.patch(
            "translators.chat.opencode_bridge.chat_stream",
            return_value=iter(["Réponse"]),
        ) as bridge_stream:
            self.assertEqual(list(chat.reply_stream(
                [{"role": "user", "content": "Explain this phrase."}],
                {
                    "model": "space-bunny-free",
                    "source_language": "en",
                    "target_language": "ar",
                    "chat_reply_language": "fr",
                    "chat_reply_language_only": True,
                },
                "source passage",
                "translated passage",
            )), ["Réponse"])
        system = bridge_stream.call_args.args[2]

        self.assertIn("Strict response-language requirement", system)
        self.assertIn("respond only in French", system)
        self.assertIn("do not assume the passage or question is about music",
                      system)
        self.assertIn(
            "If a requested fact is not in the passage or you are unsure of it",
            system,
        )
        self.assertIn(
            "Always search when the user asks you to look something up",
            system,
        )

    def test_settings_can_enable_a_fixed_chat_response_language(self):
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog
        from PySide6.QtCore import Qt
        from ui.chat_panel import ChatPanel

        dialog = SettingsDialog(dict(DEFAULTS))
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.chat_reply_language_only.isChecked())
        self.assertFalse(dialog.chat_reply_language.isEnabled())

        dialog.chat_reply_language.setCurrentIndex(
            dialog.chat_reply_language.findData("fr"))
        dialog.chat_reply_language_only.setChecked(True)
        values = dialog.values()
        self.assertTrue(dialog.chat_reply_language.isEnabled())
        self.assertEqual(values["chat_reply_language"], "fr")
        self.assertTrue(values["chat_reply_language_only"])

        panel = ChatPanel(lambda: dict(
            DEFAULTS,
            target_language="ar",
            chat_reply_language="fr",
            chat_reply_language_only=True,
        ))
        self.addCleanup(panel.close)
        panel._render()
        self.assertEqual(
            panel.transcript.layoutDirection(),
            Qt.LayoutDirection.LeftToRight,
        )
        self.assertEqual(
            panel.input.layoutDirection(),
            Qt.LayoutDirection.RightToLeft,
        )

    def test_standalone_chat_sends_latest_translation_as_context(self):
        from PySide6.QtCore import QObject, Signal
        from config.settings import DEFAULTS
        from ui.chat_panel import ChatPanel

        class StubChatWorker(QObject):
            partial = Signal(str)
            done = Signal(str)
            failed = Signal(str)
            finished = Signal()

            def __init__(self, messages, settings, source, translated):
                super().__init__()
                self.messages = messages
                self.settings = settings
                self.source = source
                self.translated = translated

            def start(self):
                pass

        panel = ChatPanel(lambda: dict(DEFAULTS, target_language="ar"))
        self.addCleanup(panel.close)
        panel.set_translation_context("الترجمة الحالية")
        with mock.patch("ui.chat_panel.ChatWorker", StubChatWorker):
            panel.input.setPlainText("شنوة معنى cadence؟")
            panel.send_message()

        worker = panel._worker
        self.assertEqual(worker.source, "")
        self.assertEqual(worker.translated, "الترجمة الحالية")
        self.assertEqual(
            worker.messages,
            [{"role": "user", "content": "شنوة معنى cadence؟"}],
        )
        worker.done.emit("هي نهاية جملة موسيقية.")
        worker.finished.emit()
        self.assertEqual(
            [message["role"] for message in panel._messages],
            ["user", "assistant"],
        )

    def test_explain_button_sends_selected_text_with_translation_context(self):
        from PySide6.QtCore import QObject, Signal
        from config.settings import DEFAULTS
        from ui.chat_panel import ChatPanel

        class StubChatWorker(QObject):
            partial = Signal(str)
            done = Signal(str)
            failed = Signal(str)
            finished = Signal()

            def __init__(self, messages, settings, source, translated):
                super().__init__()
                self.messages = messages
                self.settings = settings
                self.source = source
                self.translated = translated

            def start(self):
                pass

        panel = ChatPanel(lambda: dict(DEFAULTS, target_language="ar"))
        self.addCleanup(panel.close)
        self.assertFalse(panel.explain_button.isEnabled())
        panel.set_translation_context("الترجمة الحالية")
        panel.set_explanation_selection("motif")
        self.assertTrue(panel.explain_button.isEnabled())
        panel.input.setPlainText("Keep this draft")

        with mock.patch("ui.chat_panel.ChatWorker", StubChatWorker):
            panel.explain_button.click()

        worker = panel._worker
        self.assertEqual(worker.source, "")
        self.assertEqual(worker.translated, "الترجمة الحالية")
        self.assertEqual(
            worker.messages,
            [{
                "role": "user",
                "content": (
                    "Explain what the selected text means in the context of "
                    "the current translation and original passage. You may "
                    "supplement the passage with relevant general knowledge "
                    "or web research. For factual details not clearly stated "
                    "in the passage, use the available Google Search tool and "
                    "cite its sources; do not limit your answer to the "
                    "passage.\n\n"
                    "Selected text:\nmotif"
                ),
            }],
        )
        self.assertEqual(panel.input.toPlainText(), "Keep this draft")
        self.assertFalse(panel.explain_button.isEnabled())
        worker.finished.emit()
        self.assertTrue(panel.explain_button.isEnabled())

    def test_standalone_chat_renders_partial_model_response(self):
        from PySide6.QtCore import QObject, Signal
        from ui.chat_panel import ChatPanel

        class StubWorker(QObject):
            partial = Signal(str)
            done = Signal(str)
            failed = Signal(str)
            finished = Signal()

            def __init__(self, *args):
                super().__init__()

            def start(self):
                pass

        panel = ChatPanel(lambda: {})
        self.addCleanup(panel.close)
        with mock.patch("ui.chat_panel.ChatWorker", StubWorker):
            panel.input.setPlainText("question")
            panel.send_message()
            worker = panel._worker
            worker.partial.emit("The model is still")
            self.assertIn("The model is still", panel.transcript.toPlainText())
            worker.partial.emit("The model is still responding.")
            self.assertIn(
                "The model is still responding.",
                panel.transcript.toPlainText(),
            )
            worker.done.emit("The model is still responding.")
            self.assertEqual(
                panel.transcript.toPlainText().count(
                    "The model is still responding."),
                1,
            )

    def test_general_chat_context_contains_translation_only(self):
        from translators.chat import _model_messages

        system, _ = _model_messages(
            [{"role": "user", "content": "Explain this."}],
            "",
            "النص المترجم",
            "auto",
            "ar",
        )
        self.assertIn("Translation:\nالنص المترجم", system)
        self.assertIn("Original passage:\n\n", system)

    def test_openai_compatible_non_object_choice_has_clear_error(self):
        from translators.opencode_compat import _reply_text

        response = mock.Mock()
        response.json.return_value = {"choices": [["unexpected"]]}
        with self.assertRaisesRegex(
            RuntimeError, "Unexpected reply from the OpenAI-compatible endpoint"
        ):
            _reply_text(response)

    def test_openai_compatible_translation_rejects_non_object_choice(self):
        from translators import opencode_compat

        response = mock.Mock()
        response.json.return_value = {"choices": [["unexpected"]]}
        with mock.patch.object(
            opencode_compat, "_post_with_rate_limit_retry",
            return_value=response,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "Unexpected reply from the OpenAI-compatible endpoint",
            ):
                opencode_compat.translate(
                    "Translate this", "https://api.example/v1", "key",
                    "example-model",
                )

    def test_openai_compatible_image_translation_rejects_non_object_choice(self):
        from PIL import Image
        from translators import opencode_compat

        response = mock.Mock()
        response.headers = {}
        response.json.return_value = {"choices": [["unexpected"]]}
        with mock.patch.object(
            opencode_compat, "_post_with_rate_limit_retry",
            return_value=response,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "Unexpected reply from the OpenAI-compatible endpoint",
            ):
                opencode_compat.translate_image(
                    Image.new("RGB", (2, 2), "white"),
                    "https://api.example/v1", "key", "example-model",
                )

    def test_openai_compatible_chat_rejects_non_object_choice(self):
        from translators import opencode_compat

        response = mock.Mock()
        response.json.return_value = {"choices": [["unexpected"]]}
        with mock.patch.object(
            opencode_compat.requests, "post", return_value=response,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "Unexpected reply from the OpenAI-compatible endpoint",
            ):
                opencode_compat.chat(
                    [{"role": "user", "content": "Question"}],
                    "https://api.example/v1", "key", "example-model",
                    "System prompt",
                )

    def test_openai_compatible_stream_rejects_non_object_choice(self):
        from translators.opencode_compat import stream_translate

        response = mock.Mock()
        response.status_code = 200
        response.headers = {}
        response.iter_lines.return_value = [
            b'data: {"choices":[["unexpected"]]}',
        ]
        with mock.patch(
            "translators.opencode_compat._post_stream",
            return_value=response,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "choice is not an object"
            ):
                list(stream_translate(
                    "source", "https://api.example/v1", "key", "model"))
        response.close.assert_called_once()

    def test_openai_compatible_image_stream_rejects_non_object_choice(self):
        from PIL import Image
        from translators.opencode_compat import stream_translate_image

        response = mock.Mock()
        response.iter_lines.return_value = [
            b'data: {"choices":[["unexpected"]]}',
        ]
        with mock.patch(
            "translators.opencode_compat._post_stream",
            return_value=response,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "choice is not an object"
            ):
                list(stream_translate_image(
                    Image.new("RGB", (2, 2), "white"),
                    "https://api.example/v1", "key", "model"))
        response.close.assert_called_once()

    def test_translation_ready_updates_standalone_chat_context(self):
        from main import MainWindow

        window = MainWindow()
        self.addCleanup(window.close)
        with mock.patch("popup.worker.TranslateWorker.start"):
            popup = window._show_popup(text="source")
        self.addCleanup(popup.close)
        popup._translation_text = "ترجمة الاختبار"
        popup.translationReady.emit()
        self.assertEqual(
            window.chat_panel._translation_context, "ترجمة الاختبار")

    def test_standalone_chat_uses_every_theme_palette(self):
        from ui.chat_panel import ChatPanel
        from ui.theme import THEME_VARIANTS, theme_colors
        from main import app_stylesheet

        panel = ChatPanel(lambda: {})
        self.addCleanup(panel.close)
        for theme in THEME_VARIANTS:
            for scheme in ("dark", "light"):
                with self.subTest(theme=theme, scheme=scheme):
                    colors = theme_colors(theme, scheme)
                    panel.apply_theme(colors)
                    stylesheet = panel.styleSheet()
                    self.assertIn(
                        f"background-color: {colors['surface']};",
                        stylesheet,
                    )
                    self.assertIn(f"color: {colors['text']};", stylesheet)
                    self.assertIn(
                        f"border: 1px solid {colors['border']};",
                        stylesheet,
                    )
                    app_stylesheet_text = app_stylesheet(theme, scheme)
                    self.assertIn(
                        f"border-left: 1px solid {colors['accent_light']};",
                        app_stylesheet_text,
                    )
                    self.assertIn(
                        f"background-color: {colors['toolbar']};",
                        app_stylesheet_text,
                    )

    def test_chat_enter_sends_and_shift_enter_inserts_newline(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QSignalSpy, QTest
        from ui.chat_panel import ChatPanel

        panel = ChatPanel(lambda: {})
        self.addCleanup(panel.close)
        spy = QSignalSpy(panel.input.sendRequested)

        QTest.keyClick(panel.input, Qt.Key.Key_Return)
        self.assertEqual(spy.count(), 1)

        QTest.keyClick(
            panel.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(panel.input.toPlainText(), "\n")
        self.assertEqual(spy.count(), 1)

        QTest.keyClick(
            panel.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(spy.count(), 2)

    def test_ctrl_wheel_changes_chat_font_and_is_consumed(self):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtWidgets import QApplication
        from ui.chat_panel import ChatPanel

        panel = ChatPanel(lambda: {})
        self.addCleanup(panel.close)
        initial_size = panel.input.font().pointSizeF()
        event = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )

        consumed = QApplication.sendEvent(panel.input.viewport(), event)

        self.assertTrue(consumed)
        self.assertEqual(panel.input.font().pointSizeF(), initial_size + 1)
        self.assertEqual(panel.transcript.font().pointSizeF(), initial_size + 1)

    def test_chat_font_size_is_restored_and_saved(self):
        from ui.chat_panel import ChatPanel

        settings = {"chat_font_size": 18.5}
        panel = ChatPanel(lambda: settings)
        self.addCleanup(panel.close)
        self.assertEqual(panel.input.font().pointSizeF(), 18.5)

        with mock.patch("config.settings.save") as save_settings:
            panel._set_chat_font_size(19.5)
            panel._save_chat_font_size()

        self.assertEqual(settings["chat_font_size"], 19.5)
        save_settings.assert_called_once_with(settings)
        panel._font_size_save_timer.stop()

    def test_clicking_chat_word_starts_speech_from_that_word(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor
        from PySide6.QtTest import QTest
        from ui.chat_panel import ChatPanel

        settings = {
            "speech_engine": "edge",
            "speech_rate": 1.25,
            "speech_voice": "en-US-AriaNeural",
            "target_language": "en",
            "fast_mode": True,
        }
        panel = ChatPanel(lambda: settings)
        self.addCleanup(panel.close)
        from ui.theme import theme_colors
        colors = theme_colors("Midnight Blue", "dark")
        panel.apply_theme(colors)
        panel.resize(520, 640)
        panel._entries = [(
            "assistant",
            "First second third.\n\nA second paragraph must also be read.",
        )]
        fake_worker = mock.Mock()

        with (
            mock.patch("ui.chat_panel.speech_available", return_value=True),
            mock.patch(
                "ui.chat_panel.SpeechWorker", return_value=fake_worker,
            ) as worker_factory,
        ):
            panel._render()
            panel.show()
            self.app.processEvents()
            second_word = panel.transcript.document().find("second")
            second_word.movePosition(
                second_word.MoveOperation.StartOfWord)
            click_position = panel.transcript.cursorRect(second_word).center()
            QTest.mouseClick(
                panel.transcript.viewport(),
                Qt.MouseButton.LeftButton,
                pos=click_position,
            )

        spoken_text = worker_factory.call_args.args[0]
        self.assertTrue(spoken_text.startswith("second third."))
        self.assertIn("A second paragraph must also be read.", spoken_text)
        self.assertNotIn("First", spoken_text)
        self.assertEqual(worker_factory.call_args.args[1], panel)
        self.assertEqual(worker_factory.call_args.kwargs["engine"], "edge")
        self.assertEqual(
            worker_factory.call_args.kwargs["target_language"], "en")
        self.assertEqual(
            worker_factory.call_args.kwargs["voice"],
            settings["speech_voice"],
        )
        fake_worker.start.assert_called_once_with()
        selections = panel.transcript.extraSelections()
        self.assertEqual(
            selections[0].cursor.selectedText(),
            "second",
        )
        self.assertEqual(
            selections[0].format.background().color().name(),
            QColor(colors["selection"]).name(),
        )
        self.assertEqual(
            selections[0].format.foreground().color().name(),
            QColor(colors["text"]).name(),
        )

    def test_chat_read_aloud_and_stop_buttons_control_playback(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from ui.chat_panel import ChatPanel

        panel = ChatPanel(lambda: {"speech_engine": "edge"})
        self.addCleanup(panel.close)
        layout = panel.layout()
        action_row_index = next(
            index for index in range(layout.count())
            if layout.itemAt(index).layout() is not None
            and layout.itemAt(index).layout().indexOf(
                panel.read_aloud_button) >= 0
        )
        self.assertEqual(
            layout.itemAt(action_row_index - 1).widget(),
            panel.transcript,
        )
        self.assertEqual(
            layout.itemAt(action_row_index + 1).layout().indexOf(panel.input),
            0,
        )
        panel._entries = [("assistant", "Read the entire response.")]
        fake_worker = mock.Mock()

        with (
            mock.patch("ui.chat_panel.speech_available", return_value=True),
            mock.patch(
                "ui.chat_panel.SpeechWorker", return_value=fake_worker,
            ) as worker_factory,
        ):
            panel._render()
            self.assertTrue(panel.read_aloud_button.isEnabled())
            self.assertFalse(panel.stop_speech_button.isEnabled())
            QTest.mouseClick(
                panel.read_aloud_button, Qt.MouseButton.LeftButton)

            self.assertEqual(
                worker_factory.call_args.args[0],
                "Read the entire response.",
            )
            self.assertFalse(panel.read_aloud_button.isEnabled())
            self.assertTrue(panel.stop_speech_button.isEnabled())
            QTest.mouseClick(
                panel.stop_speech_button, Qt.MouseButton.LeftButton)

        fake_worker.requestInterruption.assert_called_once_with()
        self.assertFalse(panel.stop_speech_button.isEnabled())

    def test_chat_speech_highlight_does_not_scroll_visible_word_up(self):
        from PySide6.QtGui import QTextCursor
        from ui.chat_panel import ChatPanel

        panel = ChatPanel(lambda: {})
        self.addCleanup(panel.close)
        panel.resize(400, 300)
        panel._entries = [
            ("assistant", "\n\n".join(
                f"Earlier answer paragraph {index} with enough words to "
                "fill the transcript view."
                for index in range(16)
            )),
            ("user", "Third question."),
            ("assistant", "Final answer contains the target word nearby."),
        ]
        panel.show()
        panel._render()
        self.app.processEvents()

        target = panel.transcript.document().find("target")
        self.assertFalse(target.isNull())
        previous_cursor = QTextCursor(panel.transcript.document())
        previous_cursor.setPosition(0)
        panel.transcript.setTextCursor(previous_cursor)
        scroll_bar = panel.transcript.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())
        self.app.processEvents()
        initial_scroll = scroll_bar.value()
        self.assertTrue(
            panel.transcript.viewport().rect().contains(
                panel.transcript.cursorRect(target).center()))

        panel._speech_document_start = target.selectionStart()
        panel._speech_text = "target word"
        panel._speech_active = True

        panel._highlight_speech_range(0, len("target"))
        self.app.processEvents()
        self.assertEqual(scroll_bar.value(), initial_scroll)
        self.assertEqual(len(panel.transcript.extraSelections()), 1)

    def test_chat_transcript_context_menu_can_select_and_copy_all_text(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeySequence, QTextCursor
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QMenu
        from ui.chat_panel import ChatPanel

        panel = ChatPanel(lambda: {})
        self.addCleanup(panel.close)
        panel._entries = [
            ("user", "User message."),
            ("assistant", "Model answer."),
        ]
        panel.resize(360, 320)
        panel._render()
        panel.show()
        self.app.processEvents()
        transcript = panel.transcript
        transcript.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.assertEqual(
            transcript.contextMenuPolicy(),
            Qt.ContextMenuPolicy.NoContextMenu,
        )
        self.assertEqual(
            transcript.viewport().contextMenuPolicy(),
            Qt.ContextMenuPolicy.NoContextMenu,
        )
        for sample in ("User message.", "Model answer."):
            cursor = transcript.document().find(sample)
            self.assertFalse(cursor.isNull())
            position = transcript.cursorRect(cursor).center()
            QTest.mouseClick(
                transcript.viewport(),
                Qt.MouseButton.RightButton,
                pos=position,
            )
            self.app.processEvents()
            menus = [
                widget for widget in self.app.allWidgets()
                if isinstance(widget, QMenu) and widget.isVisible()
            ]
            self.assertEqual(
                len(menus),
                1,
                f"right-click on {sample!r} should show exactly one menu",
            )
            menu = menus[0]
            self.assertIs(menu, transcript._context_menu)
            self.assertEqual(
                menu.layoutDirection(), Qt.LayoutDirection.LeftToRight)
            menu.hide()
            self.app.processEvents()

        QTest.mouseClick(
            transcript.viewport(),
            Qt.MouseButton.RightButton,
            pos=transcript.cursorRect(
                transcript.document().find("Model answer.")).center(),
        )
        self.app.processEvents()
        menu = transcript._context_menu
        self.assertTrue(menu.isVisible())
        actions = menu.actions()
        self.assertEqual(
            [action.text() for action in actions],
            ["Copy", "Select All", "", "⟳ Refresh", "↻ ReOpen"],
        )
        self.assertEqual(
            [actions[index].shortcut().toString() for index in (0, 1)],
            [
                QKeySequence(QKeySequence.StandardKey.Copy).toString(),
                QKeySequence(QKeySequence.StandardKey.SelectAll).toString(),
            ],
        )
        refreshes = []
        reopens = []
        transcript.refreshRequested.connect(lambda: refreshes.append(True))
        transcript.reopenRequested.connect(lambda: reopens.append(True))
        actions[1].trigger()
        self.assertEqual(
            transcript.textCursor().selectedText(),
            "You\u2029User message.\u2029\u2029Chat\u2029Model answer.",
        )
        actions[3].trigger()
        actions[4].trigger()
        self.assertEqual(refreshes, [True])
        self.assertEqual(reopens, [True])
        menu.hide()
        self.app.processEvents()

        cursor = transcript.textCursor()
        cursor.clearSelection()
        transcript.setTextCursor(cursor)
        QTest.mouseClick(
            transcript.viewport(),
            Qt.MouseButton.RightButton,
            pos=transcript.cursorRect(
                transcript.document().find("User message.")).center(),
        )
        self.app.processEvents()
        menu = transcript._context_menu
        self.assertTrue(menu.isVisible())
        menu.actions()[0].trigger()
        self.assertEqual(
            self.app.clipboard().text(),
            "You\nUser message.\n\nChat\nModel answer.",
        )
        self.assertFalse(
            transcript.textCursor().hasSelection(),
            "copying the chat must not select its transcript",
        )
        menu.hide()

        cursor = transcript.textCursor()
        cursor.setPosition(
            transcript.document().find("Model answer.").selectionStart())
        cursor.setPosition(
            cursor.position() + len("Model answer."),
            QTextCursor.MoveMode.KeepAnchor,
        )
        transcript.setTextCursor(cursor)
        QTest.mouseClick(
            transcript.viewport(),
            Qt.MouseButton.RightButton,
            pos=transcript.cursorRect(
                transcript.document().find("Model answer.")).center(),
        )
        self.app.processEvents()
        self.assertTrue(transcript._context_menu.isVisible())
        transcript._context_menu.actions()[0].trigger()
        self.assertEqual(self.app.clipboard().text(), "Model answer.")
        self.assertEqual(
            transcript.textCursor().selectedText(), "Model answer.")
        transcript._context_menu.hide()

    def test_chat_hides_intro_text_and_styles_dock_resize_handle(self):
        from main import app_stylesheet
        from ui.chat_panel import ChatPanel
        from ui.theme import theme_colors

        panel = ChatPanel(lambda: {})
        self.addCleanup(panel.close)
        panel.set_translation_context("A translated passage.")
        self.assertTrue(panel.context_notice.isHidden())
        self.assertEqual(panel.transcript.placeholderText(), "")

        stylesheet = app_stylesheet("Dark Slate", "dark")
        border = theme_colors("Dark Slate", "dark")["border"]
        self.assertIn("QMainWindow::separator", stylesheet)
        self.assertIn(
            f"border-left: 1px solid {border};",
            stylesheet,
        )
        self.assertIn("width: 5px;", stylesheet)

    def test_main_window_chat_dock_is_between_pages_and_settings(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QContextMenuEvent
        from main import MainWindow

        window = MainWindow()
        window.chat_action.setChecked(False)
        window.chat_dock.hide()
        try:
            self.assertTrue(window.source_combo.isHidden())
            self.assertTrue(window.target_combo.isHidden())
            self.assertIsNot(
                window.source_combo.parentWidget(), window._toolbar)
            self.assertIsNot(
                window.target_combo.parentWidget(), window._toolbar)
            actions = [
                action for action in window._toolbar.actions()
                if not action.isSeparator()
            ]
            settings_index = next(
                index for index, action in enumerate(actions)
                if action.text() == "⚙  Settings")
            pages_index = next(
                index for index, action in enumerate(actions)
                if action.text() == "🖼  Pages")
            self.assertLess(pages_index, settings_index)
            self.assertIs(window.chat_button.defaultAction(),
                          window.chat_action)
            self.assertIs(
                window.chat_button.parentWidget(),
                window.toolbar_right_controls,
            )

            window.show()
            self.app.processEvents()
            self.assertFalse(window.chat_dock.isVisible())
            window.chat_action.setChecked(True)
            self.app.processEvents()
            self.assertTrue(window.chat_dock.isVisible())
            viewport = window.chat_panel.transcript.viewport()
            viewport_position = QPoint(8, 8)
            global_position = viewport.mapToGlobal(viewport_position)
            misdirected_event = QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse,
                viewport_position,
                global_position,
            )
            with mock.patch.object(
                window, "_show_app_context_menu",
            ) as app_menu:
                handled = window._thumbs_wheel_filter.eventFilter(
                    window.centralWidget(), misdirected_event)
            self.assertFalse(handled)
            app_menu.assert_not_called()

            position = QPoint(4, 5)
            event = QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse,
                position,
                viewport.mapToGlobal(position),
            )
            with mock.patch.object(window, "_show_app_context_menu") as app_menu:
                handled = window._thumbs_wheel_filter.eventFilter(
                    viewport, event)
            self.assertFalse(handled)
            app_menu.assert_not_called()
            window.chat_action.setChecked(False)
            self.app.processEvents()
            self.assertFalse(window.chat_dock.isVisible())
        finally:
            window.close()

    def test_main_window_saves_user_resized_chat_dock_width(self):
        from PySide6.QtCore import Qt
        from main import MainWindow

        window = MainWindow()
        try:
            window.show()
            window.chat_dock.show()
            self.app.processEvents()
            window.resizeDocks(
                [window.chat_dock], [460], Qt.Orientation.Horizontal)
            self.app.processEvents()
            actual_width = window.chat_dock.width()
            with mock.patch("main.save") as save_settings:
                window._save_session_soon()

            self.assertEqual(
                window.settings["chat_dock_width"], actual_width)
            self.assertGreaterEqual(actual_width, 320)
            save_settings.assert_called_once_with(window.settings)
        finally:
            window.close()

    def test_chat_visibility_and_window_geometry_survive_restart(self):
        from PySide6.QtCore import Qt
        from config.settings import DEFAULTS
        import main

        persisted = dict(DEFAULTS)
        persisted["open_tabs"] = []
        with mock.patch.object(
            main, "load", side_effect=lambda: dict(persisted)
        ), mock.patch.object(
            main, "save", side_effect=lambda settings: persisted.update(settings)
        ):
            first = main.MainWindow()
            try:
                first.resize(1100, 700)
                first.show()
                first.chat_action.setChecked(True)
                first.resizeDocks(
                    [first.chat_dock], [460], Qt.Orientation.Horizontal)
                self.app.processEvents()
                first._save_session_soon()
                geometry_before_minimize = first.settings.get(
                    "window_geometry")
                first.showMinimized()
                self.app.processEvents()
                first._save_session_soon()
                self.assertTrue(first.chat_action.isChecked())
                self.assertTrue(persisted["chat_dock_visible"])
                self.assertEqual(
                    persisted["window_geometry"], geometry_before_minimize)
                first.showNormal()
                self.app.processEvents()
                self.assertTrue(first.chat_dock.isVisible())
                first._save_session_soon()
                expected_size = first.size()
                expected_dock_width = persisted["chat_dock_width"]

                self.assertTrue(persisted["chat_dock_visible"])
                self.assertEqual(persisted["window_geometry"],
                                 first.settings["window_geometry"])
            finally:
                first.close()
                self.app.processEvents()

            reopened = main.MainWindow()
            try:
                reopened.show()
                self.app.processEvents()

                self.assertTrue(reopened.chat_dock.isVisible())
                self.assertEqual(reopened.size(), expected_size)
                self.assertEqual(reopened.chat_dock.width(),
                                 expected_dock_width)
            finally:
                reopened.close()
                self.app.processEvents()

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
            self.assertTrue(popup.chat_btn.isHidden())
            self.assertTrue(popup.explain_btn.isHidden())
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
            self.assertEqual(header_layout.indexOf(popup.chat_btn), -1)
            self.assertEqual(header_layout.indexOf(popup.explain_btn), -1)
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
            self.assertTrue(popup.chat_btn.isHidden())
            self.assertTrue(popup.chat_panel.isHidden())
            self.assertFalse(popup.explain_btn.isChecked())

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

    def test_translation_text_selection_does_not_emit_clicked_position(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from popup.translate_popup import TranslationTextEdit

        editor = TranslationTextEdit()
        editor.setPlainText("Select some words here")
        editor.resize(320, 100)
        editor.show()
        try:
            positions = []
            editor.positionClicked.connect(positions.append)
            QTest.mousePress(
                editor.viewport(), Qt.MouseButton.LeftButton,
                pos=QPoint(8, 10))
            QTest.mouseMove(editor.viewport(), QPoint(90, 10))
            QTest.mouseRelease(
                editor.viewport(), Qt.MouseButton.LeftButton,
                pos=QPoint(90, 10))
            self.app.processEvents()

            self.assertTrue(editor.textCursor().hasSelection())
            self.assertEqual(positions, [])

            QTest.mouseClick(
                editor.viewport(), Qt.MouseButton.LeftButton,
                pos=QPoint(20, 10))
            self.app.processEvents()
            self.assertEqual(len(positions), 1)
        finally:
            editor.close()

    def test_translation_text_clicks_distinguish_text_from_empty_area(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from popup.translate_popup import TranslationTextEdit

        editor = TranslationTextEdit()
        self.addCleanup(editor.close)
        editor.setPlainText("text on the first line")
        editor.resize(320, 180)
        editor.show()
        self.app.processEvents()
        text_clicks = []
        background_clicks = []
        editor.positionClicked.connect(text_clicks.append)
        editor.backgroundClicked.connect(
            lambda: background_clicks.append(True))
        cursor = editor.document().find("first")
        text_point = editor.cursorRect(cursor).center()
        QTest.mouseClick(
            editor.viewport(), Qt.MouseButton.LeftButton, pos=text_point)
        self.app.processEvents()
        self.assertEqual(len(text_clicks), 1)
        self.assertEqual(background_clicks, [])

        QTest.mouseClick(
            editor.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(280, 140))
        self.app.processEvents()
        self.assertEqual(len(text_clicks), 1)
        self.assertEqual(background_clicks, [True])

    def test_translation_background_click_pauses_and_resumes_at_next_word(self):
        from unittest import mock
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        with mock.patch.object(TranslatePopup, "_start_translation_worker"):
            popup = TranslatePopup(dict(DEFAULTS, target_language="en"))
        self.addCleanup(popup.close)
        popup.result.setPlainText("first second third")
        popup.resize(500, 300)
        popup.show()
        self.app.processEvents()
        popup._speech_available = True
        popup._speech_active = True
        popup._speech_index = 0
        popup._speech_start_offset = 0
        popup._speech_boundaries = {
            0: [
                (0, 10_000_000, 0, 5),
                (10_000_000, 10_000_000, 6, 12),
                (20_000_000, 10_000_000, 13, 18),
            ],
        }
        popup._player.position = mock.Mock(return_value=500)
        popup._highlight_translation_range(6, 12)
        self.assertEqual(len(popup.result.extraSelections()), 1)

        with (
            mock.patch.object(popup._player, "stop"),
            mock.patch.object(popup._player, "setSource"),
            mock.patch.object(popup, "_request_speech") as request_speech,
        ):
            empty_position = (
                popup.result.viewport().rect().bottomRight() - QPoint(8, 8))
            QTest.mouseClick(
                popup.result.viewport(),
                Qt.MouseButton.LeftButton,
                pos=empty_position,
            )
            self.assertFalse(popup._speech_active)
            self.assertEqual(popup._speech_start_offset, 6)
            self.assertEqual(len(popup.result.extraSelections()), 1)
            QTest.mouseClick(
                popup.result.viewport(),
                Qt.MouseButton.LeftButton,
                pos=empty_position,
            )

        request_speech.assert_called_once_with(6)

    def test_finished_translation_read_starts_over_on_background_click(self):
        from unittest import mock
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        with mock.patch.object(TranslatePopup, "_start_translation_worker"):
            popup = TranslatePopup(dict(DEFAULTS, target_language="en"))
        self.addCleanup(popup.close)
        popup.result.setPlainText("first second")
        popup._speech_available = True
        popup._speech_start_offset = 6

        with (
            mock.patch.object(popup._player, "stop"),
            mock.patch.object(popup._player, "setSource"),
            mock.patch.object(popup, "_close_speech_stream_server"),
            mock.patch.object(popup, "_clear_speech_files"),
            mock.patch.object(popup, "_clear_speech_highlight"),
            mock.patch.object(popup, "_request_speech") as request_speech,
        ):
            popup._finish_speech()
            self.assertEqual(popup._speech_start_offset, 0)
            popup._on_translation_background_clicked()

        request_speech.assert_called_once_with(0)

    def test_translation_selection_context_menu_offers_explanation_styles(self):
        from PySide6.QtGui import QTextCursor
        from popup.translate_popup import TranslationTextEdit

        editor = TranslationTextEdit()
        self.addCleanup(editor.close)
        editor.setPlainText("Explain this selected phrase")
        cursor = editor.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(
            len("Explain this selected phrase"),
            QTextCursor.MoveMode.KeepAnchor,
        )
        editor.setTextCursor(cursor)

        menu = editor._create_translation_context_menu()
        self.addCleanup(menu.deleteLater)
        actions = {
            action.text(): action for action in menu.actions()
            if action.text() != " "
        }
        expected_styles = {
            "Explain linguistically": "linguistic",
            "Explain simply": "simple",
            "Explain etymologically": "etymological",
            "Translate and explain": "translate_and_explain",
            "Explain in context": "context",
        }
        requests = []
        editor.explainRequested.connect(
            lambda text, style: requests.append((text, style)))
        for label, style in expected_styles.items():
            actions[label].trigger()
            self.assertEqual(
                requests[-1], ("Explain this selected phrase", style))

        cursor = editor.textCursor()
        cursor.clearSelection()
        editor.setTextCursor(cursor)
        empty_menu = editor._create_translation_context_menu()
        self.addCleanup(empty_menu.deleteLater)
        empty_actions = [action.text() for action in empty_menu.actions()]
        self.assertFalse(
            any(label in empty_actions for label in expected_styles))

    def test_popup_explain_selection_routes_to_standalone_chat(self):
        from main import MainWindow

        window = MainWindow()
        self.addCleanup(window.close)
        with mock.patch("popup.worker.TranslateWorker.start"):
            popup = window._show_popup(text="source passage")
        self.addCleanup(popup.close)
        popup._translation_text = "ترجمة السياق"

        popup.explanationSelectionChanged.emit("selected phrase")
        self.assertTrue(window.chat_panel.explain_button.isEnabled())
        with mock.patch.object(
            window.chat_panel, "explain_selection") as explain_selection:
            popup.explainRequested.emit("selected phrase", "etymological")

        self.assertEqual(
            window.chat_panel._translation_context, "ترجمة السياق")
        self.assertEqual(
            window.chat_panel._explanation_selection, "selected phrase")
        self.assertTrue(window._chat_dock_user_visible)
        explain_selection.assert_called_once_with("etymological")

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
