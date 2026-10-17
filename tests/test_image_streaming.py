"""Streaming image-translation tests without live provider requests."""
import json
import io
import unittest
from unittest import mock

import requests
from PIL import Image

from config.settings import DEFAULTS
from translators import (
    chain, gemini, opencode_bridge, opencode_cli, opencode_compat, opencode_zen,
)
from translators.vision import image_system_instructions


class TestImageStreamingAdapters(unittest.TestCase):
    def test_qwen3_vl_2b_image_stream_gets_expanded_output_budget(self):
        response = mock.Mock(status_code=200)
        response.iter_lines.return_value = [
            b'data: {"choices":[{"delta":{"content":"translated"},'
            b'"finish_reason":"stop"}]}',
            b"data: [DONE]",
        ]
        response.raise_for_status.return_value = None

        with mock.patch.object(
            requests, "post", return_value=response,
        ) as post:
            deltas = list(opencode_compat.stream_translate_image(
                Image.new("RGB", (32, 24), "white"),
                "http://localhost:11434/v1",
                "ollama",
                "qwen3-vl:2b-instruct",
            ))

        self.assertEqual(deltas, ["translated"])
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 8192)

    def test_qwen3_vl_2b_image_cutoff_explains_ollama_context(self):
        response = mock.Mock(status_code=200)
        response.iter_lines.return_value = [
            b'data: {"choices":[{"delta":{"content":"partial"},'
            b'"finish_reason":"length"}]}',
            b"data: [DONE]",
        ]
        response.raise_for_status.return_value = None

        with mock.patch.object(
            requests, "post", return_value=response,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Ollama's context length to at least 16K",
            ):
                list(opencode_compat.stream_translate_image(
                    Image.new("RGB", (32, 24), "white"),
                    "http://localhost:11434/v1",
                    "ollama",
                    "qwen3-vl:2b-instruct",
                ))

    def test_gemini_image_429_reports_failure_after_retries(self):
        response = mock.Mock(status_code=429)

        with mock.patch.object(
            requests, "post", return_value=response,
        ) as post, mock.patch.object(gemini.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "429 from Gemini"):
                list(gemini.stream_translate_image(
                    Image.new("RGB", (32, 24), "white"),
                    "test-key",
                    "gemini-3.5-flash-lite",
                ))

        self.assertEqual(post.call_count, gemini.MAX_ATTEMPTS)
        self.assertEqual(response.close.call_count, gemini.MAX_ATTEMPTS)

    def test_gemini_streams_image_text_and_closes_response(self):
        response = mock.Mock(status_code=200)
        response.iter_lines.return_value = [
            b'data: {"candidates":[{"content":{"parts":[{"text":"first "}]}}]}',
            b'data: {"candidates":[{"content":{"parts":[{"text":"second"}]},'
            b'"finishReason":"STOP"}]}',
        ]
        response.raise_for_status.return_value = None

        with mock.patch.object(requests, "post", return_value=response) as post:
            deltas = list(gemini.stream_translate_image(
                Image.new("RGB", (32, 24), "white"),
                "test-key",
                "gemini-3.5-flash-lite",
            ))

        self.assertEqual(deltas, ["first ", "second"])
        self.assertIn(
            ":streamGenerateContent?alt=sse", post.call_args.args[0])
        self.assertTrue(post.call_args.kwargs["stream"])
        payload = post.call_args.kwargs["json"]
        parts = payload["contents"][0]["parts"]
        prompt = parts[0]["text"]
        self.assertIn("complete image", prompt)
        self.assertNotIn("Final reminder", prompt)
        self.assertLess(
            len(payload["system_instruction"]["parts"][0]["text"])
            + len(prompt),
            3000,
        )
        system = payload["system_instruction"]["parts"][0]["text"]
        self.assertEqual(
            system,
            image_system_instructions(
                source_language="auto", target_language="ar"),
        )
        self.assertIn("all body text", system)
        self.assertIn("Use Markdown with ### headings", system)
        self.assertIn("fenced `music` blocks", system)
        self.assertIn("Preserve equations as LaTeX", system)
        self.assertIn("Do not summarize or omit unique source text", system)
        self.assertIn("clear, grammatical, natural prose", system)
        self.assertIn("inline_data", parts[1])
        response.close.assert_called_once()

    def test_gemini_nonstream_image_uses_shared_vision_instructions(self):
        response = mock.Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "candidates": [{
                "content": {"parts": [{"text": "translated"}]},
                "finishReason": "STOP",
            }],
        }

        with mock.patch.object(requests, "post", return_value=response) as post:
            result = gemini.translate_image(
                Image.new("RGB", (32, 24), "white"),
                "test-key",
                "gemini-3.5-flash-lite",
                instructions="Preserve names.",
                source_language="en",
                target_language="fr",
            )

        self.assertEqual(result, "translated")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            payload["system_instruction"]["parts"][0]["text"],
            image_system_instructions(
                "Preserve names.", "en", "fr"),
        )

    def test_openai_compatible_streams_image_and_reports_router(self):
        response = mock.Mock(status_code=200)
        response.headers = {"X-Routed-Via": "google/gemini-3.5-flash-lite"}
        response.iter_lines.return_value = [
            ("data: " + json.dumps({"choices": [{
                "delta": {"content": "first "}, "finish_reason": None,
            }]})).encode(),
            ("data: " + json.dumps({"choices": [{
                "delta": {"content": "second"}, "finish_reason": "stop",
            }]})).encode(),
            b"data: [DONE]",
        ]
        report = {}

        with mock.patch.object(requests, "post", return_value=response) as post:
            deltas = list(opencode_compat.stream_translate_image(
                Image.new("RGB", (32, 24), "white"),
                "http://localhost:31415/v1",
                "test-key",
                "auto",
                report=report,
            ))

        self.assertEqual(deltas, ["first ", "second"])
        self.assertTrue(post.call_args.kwargs["json"]["stream"])
        content = post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(report["routed_via"], "google/gemini-3.5-flash-lite")
        response.close.assert_called_once()

    def test_bridge_streams_image_through_its_existing_sse_transport(self):
        image = Image.new("RGB", (32, 24), "white")
        with mock.patch.object(
            opencode_bridge, "_iter_chat", return_value=iter(["partial"]),
        ) as iter_chat:
            self.assertEqual(
                list(opencode_bridge.stream_image(image, "space-bunny-free")),
                ["partial"],
            )

        self.assertTrue(iter_chat.call_args.kwargs["image_url"].startswith(
            "data:image/jpeg;base64,"))

    def test_opencode_zen_streams_an_image_over_sse(self):
        response = mock.Mock(status_code=200)
        response.iter_lines.return_value = [
            ("data: " + json.dumps({"choices": [{
                "delta": {"content": "first"},
                "finish_reason": "stop",
            }]})).encode(),
        ]
        response.raise_for_status.return_value = None
        with mock.patch.object(requests, "post", return_value=response) as post:
            deltas = list(opencode_zen.stream_translate_image(
                Image.new("RGB", (32, 24), "white"), "vendor/vision"))

        self.assertEqual(deltas, ["first"])
        self.assertTrue(post.call_args.kwargs["json"]["stream"])
        response.close.assert_called_once()

    def test_opencode_cli_yields_json_text_events_as_they_arrive(self):
        stdout = "\n".join([
            json.dumps({"type": "text", "part": {"text": "first "}}),
            json.dumps({"type": "text", "part": {"text": "second"}}),
            json.dumps({"type": "step_finish", "part": {}}),
        ]) + "\n"
        process = mock.Mock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO(stdout)
        process.poll.return_value = 0
        process.wait.return_value = 0

        with mock.patch.object(
            opencode_cli, "find_executable", return_value="opencode",
        ), mock.patch.object(
            opencode_cli.subprocess, "Popen", return_value=process,
        ):
            self.assertEqual(
                list(opencode_cli.stream_translate(
                    "source", "opencode/model")),
                ["first ", "second"],
            )


class TestImageStreamingFlow(unittest.TestCase):
    def test_image_stream_failure_does_not_silently_issue_a_second_request(self):
        settings = dict(
            DEFAULTS,
            model="gemini-3.5-flash-lite",
            gemini_key="test-key",
            source_language="en",
            target_language="ar",
            fast_mode=True,
        )
        streamed = "بداية ترجمة غير مكتملة"

        def broken_stream(*_args, **_kwargs):
            yield streamed
            raise RuntimeError("reply was cut off")

        with mock.patch(
            "translators.gemini.stream_translate_image",
            side_effect=broken_stream,
        ), mock.patch(
            "translators.gemini.translate_image",
            side_effect=AssertionError(
                "a stream failure must not trigger a hidden second request"),
        ) as fallback:
            events = chain.translate_image_stream(
                Image.new("RGB", (32, 24), "white"), settings)
            self.assertEqual(next(events), ("partial", streamed, ""))
            with self.assertRaisesRegex(
                RuntimeError, "no second request was made.*reply was cut off",
            ):
                next(events)

        fallback.assert_not_called()

    def test_fragmented_music_latex_rows_render_as_a_compact_wrapping_line(self):
        from popup.translate_popup import TranslatePopup

        fragments = (
            "Fa)", "c)", "ir?>", "4»", "**", "L/s", "—", ">81 L.⁸",
            ".", "8^J", "(»5", "/", "81", "1", r"\₅", "6", "3", "4",
            "6", "*4", "4»", "**", "<1", "4V", "**", "v", "v", ",", "v",
            "y", ", i", "i", "i", "ni", "i",
        )
        source = "```music\n" + "\n".join(fragments) + "\n```"

        result = chain.normalize_latex_output(source)
        body = result.split("\n", 1)[1].rsplit("\n```", 1)[0]

        self.assertEqual(result.splitlines()[0], "```music")
        self.assertNotIn("\n", body)
        self.assertEqual(body, " ".join(fragments))
        self.assertNotIn(body, TranslatePopup._markdown_speech_text(result))

    def test_gemini_image_failure_does_not_start_a_second_image_request(self):
        settings = dict(
            DEFAULTS,
            model="gemini-3.5-flash-lite",
            gemini_key="test-key",
            source_language="ar",
            target_language="ar",
            fast_mode=True,
        )
        error = RuntimeError("Gemini image translation failed after 3 attempts")
        with mock.patch(
            "translators.gemini.stream_translate_image",
            side_effect=error,
        ), mock.patch(
            "translators.gemini.translate_image",
            side_effect=AssertionError(
                "rate limits must not trigger a second image request"),
        ) as fallback:
            with self.assertRaisesRegex(
                RuntimeError, "no second request was made",
            ):
                list(chain.translate_image_stream(
                    Image.new("RGB", (32, 24), "white"), settings))

        fallback.assert_not_called()

    def test_image_translation_normalizes_latex_without_retry(self):
        settings = dict(
            DEFAULTS,
            model="gemini-3.5-flash-lite",
            gemini_key="test-key",
            source_language="en",
            target_language="ar",
        )
        leaked = (
            "يوضح المثال الموسيقي الانقلاب بين النغمات.\n"
            r"\begin{array}{|c|c|}\hline \text{G Clef} & "
            r"\text{F Clef} \\ \hline \end{array}"
        )
        with mock.patch(
            "translators.gemini.translate_image", return_value=leaked,
        ) as translate:
            result, _badge = chain.translate_image(
                Image.new("RGB", (20, 20), "white"), settings)

        self.assertEqual(translate.call_count, 1)
        self.assertEqual(
            translate.call_args.kwargs["source_language"], "en")
        self.assertIn("يوضح المثال الموسيقي", result)
        self.assertIn("G Clef | F Clef", result)
        self.assertIn("```music", result)
        self.assertNotIn("\\", result)
        self.assertEqual(
            translate.call_args.kwargs["source_language"], "en")

    def test_latex_math_stays_speakable_and_music_example_is_silent(self):
        from popup.translate_popup import TranslatePopup

        malformed_music = chain.normalize_latex_output(
            "```music\nbegintabularll\nS | D | T\n"
            "II | D | T\nendtabular\nquad : quad\n```\n"
        )
        self.assertIn("S | D | T", malformed_music)
        self.assertIn("II | D | T", malformed_music)
        self.assertNotIn("begintabularll", malformed_music)
        self.assertNotIn("endtabular", malformed_music)
        self.assertNotIn("quad", malformed_music)
        self.assertNotIn("T```", malformed_music)
        self.assertNotIn(
            "S | D | T",
            TranslatePopup._markdown_speech_text(malformed_music),
        )

        tabular_music = chain.normalize_latex_output(
            "```music\n"
            "\\begin{tabular}{ll}\nS & D\\\\\nII & T\n\\end{tabular}\n"
            "```"
        )
        self.assertIn("S | D", tabular_music)
        self.assertIn("II | T", tabular_music)
        self.assertNotIn("tabular", tabular_music)
        self.assertNotIn("ll", tabular_music)

        normalized = chain.normalize_latex_output(
            "النسبة هي $\\frac{1}{2}$، والجذر هو $\\sqrt{x}$.\n\n"
            "والقيمة $x^2$، والأكورد $D_9$، والنغمة $5f$ و"
            "C\\sharp واضحة.\n\n"
            r"\begin{array}{|c|c|}\hline \text{G Clef} & "
            r"\text{F Clef} \\ \hline \end{array}"
        )
        speech = TranslatePopup._markdown_speech_text(normalized)

        self.assertIn("(1)/(2)", normalized)
        self.assertIn("√(x)", normalized)
        self.assertIn("x²", normalized)
        self.assertIn("D₉", normalized)
        self.assertIn("5f", normalized)
        self.assertIn("(1)/(2)", speech)
        self.assertIn("√(x)", speech)
        self.assertIn("x²", speech)
        self.assertIn("D₉", speech)
        self.assertIn("5f", speech)
        self.assertIn("G Clef", normalized)
        self.assertIn("```music", normalized)
        self.assertEqual(normalized.count("```"), 2)
        self.assertIn("C♯", normalized)
        self.assertNotIn("G Clef", speech)
        self.assertIn("C♯", speech)
        self.assertIn("واضحة", speech)
        self.assertNotIn("\\", normalized)

        tablature = chain.normalize_latex_output(
            "شرح قبل المثال.\n\n```tex\n"
            r"\begin{array}{c} e|--0--| \\ B|--1--| \end{array}"
            "\n```\n\nشرح بعده."
        )
        tablature_speech = TranslatePopup._markdown_speech_text(tablature)
        self.assertEqual(tablature.count("```"), 2)
        self.assertIn("```music", tablature)
        self.assertNotIn("```text", tablature)
        self.assertIn("e|--0--|", tablature)
        self.assertNotIn("e|--0--|", tablature_speech)
        self.assertIn("شرح قبل المثال", tablature_speech)
        self.assertIn("شرح بعده", tablature_speech)

    def test_math_arrays_and_code_are_not_silenced_as_music(self):
        math = chain.normalize_latex_output(
            r"$$\begin{bmatrix}a&b\\c&d\end{bmatrix}$$"
        )
        code = chain.normalize_latex_output(
            "```python\n"
            "scale = [1, 2, 3]\n"
            "matrix = [[1, 0], [0, 1]]\n"
            "fraction = r'\\\\frac{1}{2}'\n"
            "```"
        )

        self.assertNotIn("```music", math)
        self.assertIn("a | b", math)
        self.assertIn("c | d", math)
        self.assertIn("```python", code)
        self.assertIn("scale = [1, 2, 3]", code)
        self.assertIn("matrix = [[1, 0], [0, 1]]", code)
        self.assertIn(r"\\frac{1}{2}", code)
    def test_streamed_latex_is_normalized_before_partial_emission(self):
        settings = dict(
            DEFAULTS,
            model="mimo-v2.6-flash-free",
            source_language="en",
            target_language="ar",
            fast_mode=True,
        )
        latex = (
            "المثال الموسيقي: "
            r"\begin{array}{|c|c|}\hline \text{G Clef} & "
            r"\text{F Clef} \\ \hline \end{array}"
        )
        with (
            mock.patch(
                "translators.opencode_bridge.stream",
                return_value=iter([latex[:45], latex[45:]]),
            ),
        ):
            events = list(chain.translate_stream("source text", settings))

        self.assertGreaterEqual(
            sum(event[0] == "partial" for event in events), 2)
        self.assertEqual(events[-1][0], "done")
        self.assertTrue(all(
            event[0] == "partial" for event in events[:-1]))
        self.assertTrue(all("\\" not in event[1] for event in events))
        self.assertIn("```music", events[-1][1])

    def test_image_stream_emits_partials_then_normalized_final(self):
        settings = dict(
            DEFAULTS,
            model="gemini-3.5-flash-lite",
            gemini_key="test-key",
            source_language="en",
            target_language="ar",
            fast_mode=True,
        )
        translation = "هذه ترجمة عربية واضحة تنقل معنى النص الموجود في الصورة."
        pieces = [translation[:20], translation[20:]]
        with mock.patch(
            "translators.gemini.stream_translate_image",
            return_value=iter(pieces),
        ) as stream, mock.patch(
            "translators.gemini.translate_image",
            side_effect=AssertionError("valid stream should not need another request"),
        ):
            events = list(chain.translate_image_stream(
                Image.new("RGB", (32, 24), "white"), settings))

        self.assertEqual([event[0] for event in events],
                         ["partial", "partial", "done"])
        self.assertEqual(events[-1][1], translation)
        self.assertEqual(
            events[-1][2], "✦ chosen image: gemini-3.5-flash-lite")
        stream.assert_called_once()
        self.assertEqual(
            stream.call_args.kwargs["source_language"], "en")

    def test_router_image_stream_keeps_the_actual_model_badge(self):
        settings = dict(
            DEFAULTS,
            model="flm:auto",
            flm_key="router-key",
            flm_base="http://localhost:31415/v1",
            target_language="ar",
            fast_mode=True,
        )
        translation = "هذه ترجمة عربية سليمة وواضحة لمحتوى الصورة."

        def fake_stream(*_args, **kwargs):
            kwargs["report"]["routed_via"] = "google/gemini-3.5-flash-lite"
            return iter([translation])

        with mock.patch(
            "translators.opencode_compat.stream_translate_image",
            side_effect=fake_stream,
        ):
            events = list(chain.translate_image_stream(
                Image.new("RGB", (32, 24), "white"), settings))

        self.assertEqual(events[-1][1], translation)
        self.assertEqual(events[-1][2], "🔀 google/gemini-3.5-flash-lite")

    def test_ocr_text_streams_with_openai_compatible_models_too(self):
        settings = dict(
            DEFAULTS,
            model="groq:test-model",
            groq_key="test-key",
            target_language="ar",
            fast_mode=True,
        )
        translation = "هذه ترجمة عربية واضحة للنص الذي قرأه OCR."
        with mock.patch(
            "translators.opencode_compat.stream_translate",
            return_value=iter(["هذه ترجمة ", "عربية واضحة للنص الذي قرأه OCR."]),
        ) as stream:
            events = list(chain.translate_stream("OCR source text", settings))

        self.assertEqual([event[0] for event in events],
                         ["partial", "partial", "done"])
        self.assertEqual(events[-1][1], translation)
        stream.assert_called_once()

    def test_ocr_text_streams_with_custom_and_cli_models(self):
        translation = "هذه ترجمة عربية ممتازة للنص المستخرج من الصورة."
        cases = [
            (
                dict(model="vendor/custom-model", openai_base="https://api.example/v1",
                     openai_key="key"),
                "translators.opencode_compat.stream_translate",
                ("https://api.example/v1", "key", "vendor/custom-model"),
            ),
            (
                dict(model="opencode-cli:openai/model"),
                "translators.opencode_cli.stream_translate",
                ("openai/model",),
            ),
        ]
        for options, target, expected_args in cases:
            with self.subTest(model=options["model"]):
                settings = dict(
                    DEFAULTS, target_language="ar", fast_mode=True, **options)
                with mock.patch(target, return_value=iter([translation])) as stream:
                    events = list(chain.translate_stream(
                        "recognized OCR text", settings))

                self.assertEqual(events[-1][1], translation)
                self.assertTrue(stream.called)
                self.assertEqual(stream.call_args.args[1:1 + len(expected_args)],
                                 expected_args)

    def test_ocr_worker_forwards_streamed_translation(self):
        from popup.worker import TranslateWorker

        image = Image.new("RGB", (32, 24), "white")
        worker = TranslateWorker(
            dict(DEFAULTS, selection_mode="ocr", fast_mode=True),
            pil_image=image,
            ocr_langs=("en",),
        )
        partials = []
        completed = []
        worker.partial.connect(partials.append)
        worker.done.connect(lambda text, badge: completed.append((text, badge)))
        with mock.patch(
            "translators.ocr.is_warmed", return_value=True,
        ), mock.patch(
            "translators.ocr.backend_name", return_value="test OCR",
        ), mock.patch(
            "translators.ocr.read_text", return_value="recognized text",
        ), mock.patch(
            "translators.chain.translate_stream",
            return_value=iter([
                ("partial", "early translation", ""),
                ("done", "early translation complete", "test model"),
            ]),
        ):
            worker.run()

        self.assertEqual(partials, ["early translation"])
        self.assertEqual(
            completed, [("early translation complete", "test model")])

    def test_streamed_image_is_accepted_without_language_retry(self):
        settings = dict(
            DEFAULTS,
            model="gemini-3.5-flash-lite",
            gemini_key="test-key",
            source_language="en",
            target_language="ar",
            fast_mode=True,
        )
        streamed = (
            "هذه ترجمة عربية.\n\n"
            "English-looking notation and code output.\n"
            "```python\nrecv = True\n```"
        )
        with mock.patch(
            "translators.gemini.stream_translate_image",
            return_value=iter([streamed]),
        ), mock.patch(
            "translators.gemini.translate_image",
            side_effect=AssertionError(
                "a language mismatch must not trigger a retry"),
        ):
            events = list(chain.translate_image_stream(
                Image.new("RGB", (32, 24), "white"), settings))

        self.assertEqual(events[-1][0], "done")
        self.assertEqual(events[-1][1], streamed)

    def test_streamed_image_does_not_retry_english_inside_silent_code(self):
        settings = dict(
            DEFAULTS,
            model="gemini-3.5-flash-lite",
            gemini_key="test-key",
            source_language="en",
            target_language="ar",
            fast_mode=True,
        )
        translation = (
            "هذه ترجمة عربية سليمة وواضحة لمحتوى الصفحة.\n\n"
            "```python\n"
            "def recv(maxsize, *, block):\n"
            '    "Receives a message"\n'
            "    pass\n"
            "```"
        )
        with mock.patch(
            "translators.gemini.stream_translate_image",
            return_value=iter([translation]),
        ) as stream, mock.patch(
            "translators.gemini.translate_image",
            side_effect=AssertionError(
                "silent code must not trigger a corrective retry"),
        ):
            events = list(chain.translate_image_stream(
                Image.new("RGB", (32, 24), "white"), settings))

        self.assertEqual(events[-1][0], "done")
        self.assertEqual(events[-1][1], translation)
        stream.assert_called_once()

    def test_worker_forwards_image_partials_to_popup(self):
        from popup.worker import TranslateWorker

        settings = dict(DEFAULTS, selection_mode="image", fast_mode=True)
        worker = TranslateWorker(
            settings, pil_image=Image.new("RGB", (32, 24), "white"))
        partials = []
        completed = []
        worker.partial.connect(partials.append)
        worker.done.connect(lambda text, badge: completed.append((text, badge)))

        with mock.patch(
            "translators.chain.translate_image_stream",
            return_value=iter([
                ("partial", "early words", ""),
                ("done", "early words and final text", "image model"),
            ]),
        ):
            worker.run()

        self.assertEqual(partials, ["early words"])
        self.assertEqual(
            completed, [("early words and final text", "image model")])
