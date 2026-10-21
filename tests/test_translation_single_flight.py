"""One translation request at a time, and no avoidable second request.

Two regressions this file pins down:

* Superseded translations kept running next to the new one. Changing a
  language (or flipping a page) fired a second request while the first was
  still streaming, so providers saw concurrent calls, rate-limited them, and
  the same passage was paid for twice.
* The output-language check re-translated a chunk even when the text was
  already in the target script (an Arabic page full of Latin note names), and
  the streaming check could add a second correction on top of the first.
"""
import threading
import time
import unittest
from unittest import mock

from config.settings import DEFAULTS
from translators import chain


def _settings(**overrides):
    return dict(
        DEFAULTS,
        model="gemini-3.5-flash-lite",
        gemini_key="test-key",
        source_language="en",
        target_language="ar",
        fast_mode=True,
        **overrides,
    )


class SingleFlightTest(unittest.TestCase):
    def test_two_translations_never_reach_the_network_together(self):
        active = 0
        peak = 0
        guard = threading.Lock()
        barrier = threading.Barrier(2)

        def fake_stream(chunk, settings):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.08)
                yield ("done", "نص عربي مترجم للاختبار " * 4, "badge")
            finally:
                with guard:
                    active -= 1

        def run_translation():
            barrier.wait(5)
            list(chain.translate_stream("source passage", _settings()))

        with mock.patch(
            "translators.chain._stream_single_gemini", side_effect=fake_stream,
        ):
            threads = [
                threading.Thread(target=run_translation) for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)

        self.assertEqual(peak, 1)
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_a_cancelled_translation_starts_no_request(self):
        requests = []

        def fake_stream(chunk, settings):
            requests.append(chunk)
            yield ("done", "نص عربي", "badge")

        settings = _settings()
        settings["_cancelled"] = lambda: True
        with mock.patch(
            "translators.chain._stream_single_gemini", side_effect=fake_stream,
        ):
            with self.assertRaises(chain.TranslationCancelled):
                list(chain.translate_stream("source passage", settings))

        self.assertEqual(requests, [])

    def test_cancellation_while_waiting_for_the_slot_sends_nothing(self):
        release = threading.Event()
        entered = threading.Event()
        requests = []

        def blocking_stream(chunk, settings):
            requests.append(("blocking", chunk))
            entered.set()
            try:
                yield ("done", "نص عربي مترجم للاختبار " * 4, "badge")
            finally:
                release.set()

        settings = _settings()
        cancelled = threading.Event()
        settings["_cancelled"] = cancelled.is_set

        first = threading.Thread(
            target=lambda: list(
                chain.translate_stream("first passage", settings)))
        with mock.patch(
            "translators.chain._stream_single_gemini",
            side_effect=blocking_stream,
        ):
            first.start()
            self.assertTrue(entered.wait(5))
            cancelled.set()
            with self.assertRaises(chain.TranslationCancelled):
                list(chain.translate_stream("second passage", settings))
            release.set()
            first.join(5)
        self.assertFalse(first.is_alive())


class OneCorrectionAtMostTest(unittest.TestCase):
    def test_a_wrong_language_costs_exactly_one_extra_request(self):
        calls = []

        def fake_stream(chunk, settings):
            calls.append("stream")
            yield ("done", "This text is still English. " * 3, "badge")

        def fake_chunk(chunk, settings):
            calls.append("retry")
            return "نص عربي مترجم للاختبار " * 4, "badge"

        with (
            mock.patch(
                "translators.chain._stream_single_gemini",
                side_effect=fake_stream,
            ),
            mock.patch(
                "translators.chain._detect_translation_language",
                side_effect=["en", "ar"],
            ),
            mock.patch(
                "translators.chain._translate_chunk", side_effect=fake_chunk,
            ) as retry,
        ):
            events = list(chain.translate_stream("source passage", _settings()))

        self.assertEqual(retry.call_count, 1)
        self.assertEqual(calls, ["stream", "retry"])
        self.assertEqual(events[-1][0], "done")


class NoNeedlessLanguageCheckTest(unittest.TestCase):
    def test_arabic_output_with_latin_note_names_is_not_retranslated(self):
        settings = _settings()
        output = (
            "يشرح المؤلف أن سلم C major يتكون من الأصوات C وD وE وF وG "
            "وA وB، وقد استخدم باخ هذا السلم في أعماله الكبرى."
        )
        with mock.patch(
            "translators.chain._detect_translation_language",
        ) as detect:
            self.assertFalse(chain._needs_language_retry(settings, output))
        detect.assert_not_called()

    def test_short_notation_is_not_retranslated(self):
        settings = _settings()
        with mock.patch(
            "translators.chain._detect_translation_language",
            return_value=None,
        ) as detect:
            self.assertFalse(
                chain._needs_language_retry(settings, "C D E F G A B"))
        detect.assert_called_once()

    def test_genuinely_wrong_language_is_retried(self):
        settings = _settings()
        output = (
            "The author explains that the major scale is built from the "
            "notes C D E F G A B and that Bach used it constantly."
        )
        with mock.patch(
            "translators.chain._detect_translation_language",
            return_value="en",
        ):
            self.assertTrue(chain._needs_language_retry(settings, output))


if __name__ == "__main__":
    unittest.main()
