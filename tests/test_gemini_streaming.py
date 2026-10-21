"""Focused tests for the direct Gemini streaming path."""
import unittest
from unittest import mock

import requests

from config.settings import DEFAULTS
from translators import chain, gemini


class TestGeminiStreaming(unittest.TestCase):
    def test_stream_translate_yields_sse_text_and_closes_response(self):
        response = mock.Mock(status_code=200)
        response.iter_lines.return_value = [
            b'data: {"candidates":[{"content":{"parts":[{"text":"first "}]}}]}',
            b'data: {"candidates":[{"content":{"parts":[{"text":"second"}]},'
            b'"finishReason":"STOP"}]}',
        ]
        response.raise_for_status.return_value = None

        with mock.patch.object(requests, "post", return_value=response) as post:
            deltas = list(gemini.stream_translate(
                "source", "test-key", "gemini-3.5-flash-lite"))

        self.assertEqual(deltas, ["first ", "second"])
        self.assertIn(":streamGenerateContent?alt=sse", post.call_args.args[0])
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertEqual(
            post.call_args.kwargs["json"]["generationConfig"]["maxOutputTokens"],
            gemini.TEXT_MIN_OUTPUT_TOKENS,
        )
        response.close.assert_called_once()

    def test_stream_translate_rejects_stream_without_completion_reason(self):
        response = mock.Mock(status_code=200)
        response.iter_lines.return_value = [
            b'data: {"candidates":[{"content":{"parts":[{"text":"partial"}]}}]}',
        ]
        response.raise_for_status.return_value = None

        with mock.patch.object(requests, "post", return_value=response):
            with self.assertRaisesRegex(
                RuntimeError, "without a completion reason",
            ):
                list(gemini.stream_translate(
                    "source", "test-key", "gemini-3.5-flash-lite"))

        response.close.assert_called_once()

    def test_stream_translate_rejects_truncated_generation(self):
        response = mock.Mock(status_code=200)
        response.iter_lines.return_value = [
            b'data: {"candidates":[{"content":{"parts":[{"text":"partial"}]},'
            b'"finishReason":"MAX_TOKENS"}]}',
        ]
        response.raise_for_status.return_value = None

        with mock.patch.object(requests, "post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "reply was cut off"):
                list(gemini.stream_translate(
                    "source", "test-key", "gemini-3.5-flash-lite"))

        response.close.assert_called_once()

    def test_fast_mode_streams_direct_gemini_and_keeps_final_checks(self):
        settings = dict(
            DEFAULTS,
            model="gemini-3.5-flash-lite",
            gemini_key="test-key",
            target_language="ar",
            fast_mode=True,
        )
        arabic = "هذه ترجمة عربية سليمة تحافظ على المعنى والتفاصيل الواضحة."
        with mock.patch(
            "translators.gemini.stream_translate",
            return_value=iter(["هذه ترجمة ", "عربية سليمة تحافظ على المعنى والتفاصيل الواضحة."]),
        ) as stream, mock.patch(
            "translators.gemini.translate",
            side_effect=AssertionError("a valid stream must not issue another request"),
        ):
            events = list(chain.translate_stream("source passage", settings))

        self.assertEqual([event[0] for event in events],
                         ["partial", "partial", "done"])
        self.assertEqual(events[-1][1], arabic)
        self.assertEqual(events[-1][2], "✦ chosen: gemini-3.5-flash-lite")
        stream.assert_called_once()

    def test_compatible_stream_failure_falls_back_without_retrying_model(self):
        settings = dict(
            DEFAULTS,
            model="test-compatible-model",
            openai_base="https://example.invalid/v1",
            openai_key="test-key",
            source_language="en",
            target_language="ar",
        )

        def failed_stream(*_args, **_kwargs):
            def deltas():
                raise RuntimeError("stream disconnected")
                yield ""
            return deltas()

        with (
            mock.patch(
                "config.settings.resolve_model",
                return_value="test-compatible-model",
            ),
            mock.patch.object(chain.registry, "provider_of", return_value="custom"),
            mock.patch(
                "translators.openai_compat.stream_translate",
                side_effect=failed_stream,
            ) as stream,
            mock.patch(
                "translators.fallback.gtx_translate",
                return_value="free translation",
            ) as fallback,
            mock.patch.object(
                chain,
                "_translate_chunk",
                side_effect=AssertionError("must not repeat the selected model"),
            ),
        ):
            events = list(chain._stream_single_compatible("source", settings))

        self.assertEqual(events[-1][1], "free translation")
        self.assertIn("stream disconnected", events[-1][2])
        stream.assert_called_once()
        fallback.assert_called_once_with("source", "en", "ar")


    def test_nonstream_translate_reserves_8192_output_tokens(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "candidates": [{
                "content": {"parts": [{"text": "ترجمة كاملة"}]},
                "finishReason": "STOP",
            }],
        }
        response.raise_for_status.return_value = None

        with mock.patch.object(requests, "post", return_value=response) as post:
            result = gemini.translate(
                "source", "test-key", "gemini-3.5-flash-lite")

        self.assertEqual(result, "ترجمة كاملة")
        self.assertEqual(
            post.call_args.kwargs["json"]["generationConfig"]["maxOutputTokens"],
            gemini.TEXT_MIN_OUTPUT_TOKENS,
        )
