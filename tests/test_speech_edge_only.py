"""Read-aloud engine behavior and mixed-script handling."""
import asyncio
import base64
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from tests.transport_support import requires_loopback
from unittest import mock

from popup import speech_worker


class TestSpeechSynthesis(unittest.TestCase):
    def test_gemini_tts_uses_selected_voice_and_wav_response(self):
        wav = b"RIFF\x00\x00\x00\x00WAVEfake-wav-data"
        encoded = base64.b64encode(wav).decode("ascii")
        response = mock.Mock()
        response.json.return_value = {
            "steps": [{
                "type": "model_output",
                "content": [{"type": "audio", "data": encoded}],
            }]
        }
        worker = speech_worker.SpeechWorker(
            "مرحبا بالعالم",
            engine=speech_worker.GEMINI_TTS_ENGINE,
            gemini_api_key="test-key",
            gemini_voice="Zephyr",
            fast=False,
            stream_audio=False,
        )
        with mock.patch.object(
            speech_worker.requests, "post", return_value=response
        ) as post:
            path, boundaries = asyncio.run(worker._make_segment(0))
        self.addCleanup(worker.cleanup_output)

        self.assertEqual(Path(path).suffix, ".wav")
        self.assertEqual(Path(path).read_bytes(), wav)
        self.assertEqual(boundaries, [])
        request = post.call_args
        self.assertEqual(
            request.args[0], speech_worker.GEMINI_TTS_ENDPOINT)
        self.assertEqual(request.kwargs["headers"]["x-goog-api-key"], "test-key")
        self.assertEqual(
            request.kwargs["json"]["model"], speech_worker.GEMINI_TTS_MODEL)
        self.assertEqual(
            request.kwargs["json"]["generation_config"]["speech_config"],
            [{"voice": "Zephyr"}],
        )

    def test_gemini_tts_requires_an_api_key_and_has_separate_cache_keys(self):
        edge_key = speech_worker._cache_key("hello", "voice", "+0%")
        gemini_key = speech_worker._cache_key(
            "hello", "voice", "+0%",
            engine=speech_worker.GEMINI_TTS_ENGINE,
        )
        self.assertNotEqual(edge_key, gemini_key)
        self.assertFalse(speech_worker.speech_available(
            speech_worker.GEMINI_TTS_ENGINE))
        self.assertTrue(speech_worker.speech_available(
            speech_worker.GEMINI_TTS_ENGINE, "test-key"))
        self.assertEqual(len(speech_worker.GEMINI_TTS_VOICES), 30)

    def test_gemini_tts_uses_regular_segments_not_edge_streaming(self):
        worker = speech_worker.SpeechWorker(
            "مرحبا بالعالم " * 40,
            engine=speech_worker.GEMINI_TTS_ENGINE,
            gemini_api_key="test-key",
            continuous_stream=True,
        )

        self.assertFalse(worker._continuous_stream)
        self.assertLessEqual(
            len(worker._segments[0]),
            speech_worker.FIRST_CHUNK_LENGTH,
        )

    def test_manual_multilingual_voice_is_reused_across_script_runs(self):
        segments, voices, _spans = speech_worker._speech_segments_with_voices(
            "مرحبا، bonjour بك", "fr-FR-VivienneMultilingualNeural"
        )

        self.assertEqual(segments, ["مرحبا, بك"])
        self.assertEqual(
            voices,
            ["fr-FR-VivienneMultilingualNeural"],
        )

    def test_arabic_numbers_keep_western_digits_in_speech(self):
        text = "مرحبا 42 بك ٣٫١٤"
        segments, voices, spans = speech_worker._speech_segments_with_voices(
            text, "fr-FR-VivienneMultilingualNeural", target_language="ar"
        )

        self.assertEqual(segments, ["مرحبا 42 بك ٣, ١٤"])
        self.assertEqual(voices, ["fr-FR-VivienneMultilingualNeural"])
        self.assertEqual(
            [
                text[start:end]
                for segment_spans in spans
                for start, end in segment_spans
            ],
            ["مرحبا", "42", "بك", "٣", "١٤"],
        )

    def test_latin_words_keep_source_spelling_and_source_alignment(self):
        text = "مرحبا USS Frolic"
        segments, _voices, spans = (
            speech_worker._speech_segments_with_voices(
                text,
                "ar-SA-ZariyahNeural",
                target_language="ar",
                read_latin_words=True,
            )
        )
        self.assertEqual(segments, ["مرحبا USS Frolic"])
        self.assertEqual(
            [text[start:end] for start, end in spans[0]],
            ["مرحبا", "USS", "Frolic"],
        )
        self.assertEqual(
            speech_worker._match_synthesis_boundary_span(
                "USS", segments[0], text, spans[0], 1),
            (6, 9, 2),
        )
        self.assertEqual(
            speech_worker._match_synthesis_boundary_span(
                "Frolic", segments[0], text, spans[0], 2),
            (10, 16, 3),
        )

    def test_boundary_matching_resynchronizes_after_a_missed_french_word(self):
        text = "Webb fixa l'heure du décès entre 2 h et 3 h du matin."
        words = speech_worker._speech_source_word_spans(text)
        spans = speech_worker._segment_word_spans(text, [text])[0]
        expected_index = next(
            index for index, (start, end) in enumerate(words)
            if text[start:end] == "décès"
        )

        matched = speech_worker._match_synthesis_boundary_span(
            "entre", text, text, spans, expected_index)

        self.assertEqual(
            matched,
            (spans[expected_index + 1][0],
             spans[expected_index + 1][1],
             expected_index + 2),
        )

    def test_multi_word_boundary_group_does_not_freeze_the_highlight(self):
        """Edge returns one word for every token that has no boundary itself.

        Captured from the real synthesis of this sentence: the boundary text
        "entre 2 h et 3 h" arrives as a *single* word even though it covers
        seven source words -- the digits produce no boundaries of their own.
        A five-word alignment window could not match it, and a failed match
        leaves the source cursor where it was, so every later boundary ("du",
        "matin") was compared against the same stale word and dropped too.
        The highlight then stopped on "décès" while the audio read on.
        """
        text = "Webb fixa l'heure du décès entre 2 h et 3 h du matin."
        spans = speech_worker._segment_word_spans(text, [text])[0]
        boundaries = [
            "Webb", "fixa", "l'heure", "du", "décès",
            "entre 2 h et 3 h", "du", "matin",
        ]

        index = 0
        for boundary_text in boundaries:
            matched = speech_worker._match_synthesis_boundary_span(
                boundary_text, text, text, spans, index)
            expected = text[spans[index][0]:spans[index][1]]
            self.assertIsNotNone(
                matched,
                f"the boundary {boundary_text!r} was dropped at source word "
                f"{index} ({expected!r}); the highlight freezes there for the "
                f"rest of the read",
            )
            start, end, index = matched

        self.assertEqual(index, len(spans))
        self.assertEqual(text[spans[index - 1][0]:spans[index - 1][1]], "matin")

    def test_latin_phrases_normalize_wrappers_without_pause_tokens(self):
        examples = (
            "(Charles (Chauncey) C. Loomis)",
            "(Charles_Chauncey_C. Loomis)",
            '(Charles "Chauncey" C. Loomis)',
        )
        for text in examples:
            with self.subTest(text=text):
                segments, _voices, spans = (
                    speech_worker._speech_segments_with_voices(
                        text,
                        "fr-FR-VivienneMultilingualNeural",
                        target_language="ar",
                        read_latin_words=True,
                    )
                )
                self.assertEqual(
                    segments,
                    ["Charles Chauncey C Loomis"],
                )
                self.assertEqual(
                    [text[start:end] for start, end in spans[0]],
                    ["Charles", "Chauncey", "C", "Loomis"],
                )

    def test_parentheses_do_not_change_the_foreign_speech_run(self):
        texts = (
            "هُول Hall",
            "هُول (Hall)",
        )
        speech_runs = [
            speech_worker._speech_segments_with_voices(
                text,
                "fr-FR-VivienneMultilingualNeural",
                target_language="ar",
                read_latin_words=True,
            )[0]
            for text in texts
        ]
        self.assertEqual(speech_runs[0], speech_runs[1])
        self.assertEqual(speech_runs[1], ["هُول Hall"])

    def test_arabic_parenthetical_word_does_not_add_a_speech_pause(self):
        text = (
            "المِنْطَقَةِ القُطْبِيَّةِ الشِّمَالِيَّةِ "
            "تَشَارْلز (شُونْسِي)"
        )

        segments, voices, _spans = (
            speech_worker._speech_segments_with_voices(
                text,
                "ar-SA-ZariyahNeural",
                target_language="ar",
                read_latin_words=True,
            )
        )

        self.assertEqual(
            segments,
            [
                "المِنْطَقَةِ القُطْبِيَّةِ الشِّمَالِيَّةِ "
                "تَشَارْلز شُونْسِي"
            ],
        )
        self.assertEqual(voices, ["ar-SA-ZariyahNeural"])

    def test_wrappers_are_non_pausing_for_non_latin_speech(self):
        for wrapped in ("(شُونْسِي)", '"شُونْسِي"', "_شُونْسِي_"):
            with self.subTest(wrapped=wrapped):
                segments = speech_worker.split_speech_text(
                    f"تَشَارْلز {wrapped}"
                )
                self.assertEqual(segments, ["تَشَارْلز شُونْسِي"])

    def test_arabic_numbers_stay_in_the_surrounding_speech_run(self):
        text = "بَعْدَ أَقَلَّ مِنْ 100 عَامٍ"
        segments, _voices, spans = (
            speech_worker._speech_segments_with_voices(
                text,
                "fr-FR-VivienneMultilingualNeural",
                target_language="ar",
                read_latin_words=True,
            )
        )

        self.assertEqual(segments, ["بَعْدَ أَقَلَّ مِنْ 100 عَامٍ"])
        self.assertEqual(
            [text[start:end] for start, end in spans[0]],
            ["بَعْدَ", "أَقَلَّ", "مِنْ", "100", "عَامٍ"],
        )

    def test_ignored_latin_words_do_not_remove_adjacent_numbers(self):
        text = "bonjour 42 مرحبا"
        segments, _voices, spans = (
            speech_worker._speech_segments_with_voices(
                text,
                "fr-FR-VivienneMultilingualNeural",
                target_language="ar",
                read_latin_words=False,
            )
        )

        self.assertEqual(segments, ["42 مرحبا"])
        self.assertEqual(
            [text[start:end] for start, end in spans[0]],
            ["42", "مرحبا"],
        )

    def test_automatic_voice_uses_target_language_for_every_script_run(self):
        segments, voices, _spans = speech_worker._speech_segments_with_voices(
            "مرحبا، bonjour بك 42",
            "fr-FR-VivienneMultilingualNeural",
            automatic_voice=True,
            target_language="ar",
        )

        self.assertEqual(segments, ["مرحبا, بك 42"])
        self.assertEqual(voices, ["ar-SA-ZariyahNeural"])

    def test_automatic_voice_falls_back_to_manual_for_unknown_language(self):
        _segments, voices, _spans = speech_worker._speech_segments_with_voices(
            "hello مرحبا",
            "fr-FR-VivienneMultilingualNeural",
            automatic_voice=True,
            target_language="unknown",
        )

        self.assertEqual(voices, ["fr-FR-VivienneMultilingualNeural"] * 2)

    def test_latin_phrases_are_skipped_without_shifting_arabic_highlights(self):
        class FakeCommunicate:
            calls = []

            def __init__(self, text, voice, **_options):
                self.text = text
                self.calls.append((text, voice))

            async def stream(self):
                yield {"type": "audio", "data": b"audio"}
                for word in self.text.split():
                    yield {
                        "type": "WordBoundary",
                        "offset": 0,
                        "duration": 1_000_000,
                        "text": word,
                    }

        class FakeStreamServer:
            def create_stream(self):
                return "token", "http://127.0.0.1/token"

            def write(self, _token, _audio):
                pass

            def finish(self, _token):
                pass

        text = "مرحبا (bonjour) بك 42 و١٣"
        worker = speech_worker.SpeechWorker(
            text,
            source_offset=10,
            voice="fr-FR-VivienneMultilingualNeural",
            target_language="ar",
            fast=False,
            stream_server=FakeStreamServer(),
        )
        FakeCommunicate.calls = []

        async def synthesize_segments():
            results = []
            for index in range(len(worker._segments)):
                _path, boundaries = await worker._make_segment(index)
                results.extend(boundaries)
            return results

        with mock.patch.object(
            speech_worker,
            "edge_tts",
            type("Edge", (), {"Communicate": FakeCommunicate}),
        ):
            boundaries = asyncio.run(synthesize_segments())
        worker.cleanup_output()

        self.assertEqual(
            FakeCommunicate.calls,
            [
                ("مرحبا بك ٤٢ و١٣", "fr-FR-VivienneMultilingualNeural"),
            ],
        )
        expected_spans = [
            span
            for spans in worker._segment_word_spans
            for span in spans
        ]
        self.assertEqual(
            [text[start:end] for start, end in expected_spans],
            ["مرحبا", "بك", "42", "و١٣"],
        )
        self.assertEqual(
            [(start - 10, end - 10) for _, _, start, end in boundaries],
            expected_spans,
        )

    def test_latin_words_can_be_included_for_arabic_translations(self):
        text = "مرحبا، bonjour بك"
        segments, voices, spans = speech_worker._speech_segments_with_voices(
            text,
            "ar-SA-ZariyahNeural",
            target_language="ar",
            read_latin_words=True,
        )

        spoken_words = [
            text[start:end]
            for segment_spans in spans
            for start, end in segment_spans
        ]
        self.assertEqual(spoken_words, ["مرحبا", "bonjour", "بك"])
        self.assertEqual(segments, ["مرحبا, bonjour بك"])
        self.assertEqual(
            voices,
            ["ar-SA-ZariyahNeural"],
        )

    def test_continuous_arabic_playback_uses_one_synthesis_request(self):
        text = (
            "في صباح بارد ومشرق من أيام الربيع الأولى، استيقظت المدينة "
            "على ضجيج غير معتاد تسرب من نوافذ البيوت المغلقة بإحكام. "
            "خرج الناس إلى الشوارع واحدا تلو الآخر، يتبادلون نظرات الدهشة "
            "بينما كانت أصوات الباعة تختلط بصفير الرياح القادمة من جهة البحر. "
            "قال رجل عجوز يقف عند زاوية السوق إن هذا اليوم يذكره بطفولته، "
            "حين كانت الأزقة أضيق والوجوه أكثر ألفة."
        )
        worker = speech_worker.SpeechWorker(
            text,
            voice="fr-FR-RemyMultilingualNeural",
            target_language="ar",
            read_latin_words=False,
            fast=False,
            continuous_stream=True,
            stream_audio=False,
        )

        self.assertEqual(len(worker._segments), 1)
        self.assertIn("البحر. قال", worker._segments[0])

    def test_continuous_speech_preserves_sentence_final_punctuation(self):
        segments, _voices, _spans = (
            speech_worker._speech_segments_with_voices(
                "مرحبا. بخير! كيف حالك؟ تمام",
                "fr-FR-RemyMultilingualNeural",
                target_language="ar",
                continuous=True,
            )
        )

        self.assertEqual(segments, ["مرحبا. بخير! كيف حالك؟ تمام"])

    def test_continuous_skip_latin_never_glues_or_drops_words(self):
        import asyncio
        from unittest import mock
        from popup.speech_worker import (
            SpeechWorker, _speech_source_word_spans,
        )

        class FakeCommunicate:
            def __init__(self, text, *_args, **_kwargs):
                self.text = text

            async def stream(self):
                for start, end in _speech_source_word_spans(self.text):
                    yield {"type": "audio", "data": b"audio"}
                    yield {
                        "type": "WordBoundary",
                        "offset": 0,
                        "duration": 1,
                        "text": self.text[start:end],
                    }

        text = "نَجَحَ بِيْسِلْس (Bessels) فِي السَّيْطَرَةِ."
        worker = SpeechWorker(
            text,
            voice="fr-FR-RemyMultilingualNeural",
            target_language="ar",
            read_latin_words=False,
            fast=False,
            continuous_stream=True,
            stream_audio=False,
        )
        try:
            self.assertEqual(len(worker._segments), 1)
            self.assertNotIn("Bessels", worker._segments[0])
            synthesis_words = _speech_source_word_spans(worker._segments[0])
            self.assertEqual(
                len(worker._segment_word_spans[0]), len(synthesis_words))
            with mock.patch.object(
                speech_worker,
                "edge_tts",
                type("Edge", (), {"Communicate": FakeCommunicate}),
            ):
                _path, boundaries = asyncio.run(worker._make_segment(0))
            # Every spoken word must produce a boundary: a single dropped
            # word desynchronises the matcher and freezes the highlight
            # while the audio keeps going.
            self.assertEqual(len(boundaries), len(synthesis_words))
        finally:
            worker.cleanup_output()

    def test_selected_arabic_voice_keeps_arabic_ssml_locale(self):
        text = "مرحبا hello بك"
        worker = speech_worker.SpeechWorker(
            text,
            voice="ar-SA-ZariyahNeural",
            target_language="ar",
            read_latin_words=True,
            fast=False,
        )

        self.assertEqual(worker._segments, ["مرحبا hello بك"])
        self.assertEqual(worker._segment_voices, ["ar-SA-ZariyahNeural"])
        self.assertEqual(
            [
                worker._ssml_language_for_segment(segment, voice)
                for segment, voice in zip(
                    worker._segments, worker._segment_voices
                )
            ],
                ["ar-SA"],
        )

    def test_mixed_script_uses_one_selected_voice_and_locale(self):
        voice = "en-AU-WilliamMultilingualNeural"
        latin_prefix = "English words " * 30
        worker = speech_worker.SpeechWorker(
            f"{latin_prefix}مرحبا hello بك",
            voice=voice,
            target_language="ar",
            read_latin_words=True,
            fast=False,
        )

        self.assertGreater(len(worker._segments), 1)
        self.assertTrue(any("English" in segment for segment in worker._segments))
        self.assertTrue(any("مرحبا" in segment for segment in worker._segments))
        self.assertEqual(worker._segment_voices, [voice] * len(worker._segments))
        self.assertEqual(
            [
                worker._ssml_language_for_segment(segment, segment_voice)
                for segment, segment_voice in zip(
                    worker._segments, worker._segment_voices)
            ],
            ["ar-SA"] * len(worker._segments),
        )

    def test_mixed_text_keeps_selected_arabic_voice(self):
        voice = "ar-SA-HamedNeural"
        worker = speech_worker.SpeechWorker(
            "مرحبا hello بك",
            voice=voice,
            target_language="ar",
            read_latin_words=True,
            fast=False,
        )

        self.assertEqual(worker._segments, ["مرحبا hello بك"])
        self.assertEqual(worker._segment_voices, [voice])
        self.assertEqual(
            worker._ssml_language_for_segment(
                worker._segments[0], worker._segment_voices[0]),
            "ar-SA",
        )

    def test_mixed_arabic_and_latin_speech_uses_one_request(self):
        from xml.etree import ElementTree
        from edge_tts.communicate import TTSConfig

        class FakeCommunicate:
            calls = []
            ssml = []

            def __init__(self, text, voice, rate=None, **_options):
                self.calls.append((text, voice))
                self.text = text
                self.voice = voice
                self.rate = rate

            async def stream(self):
                type(self).ssml.append(
                    speech_worker.edge_tts_communicate.mkssml(
                        TTSConfig(
                            self.voice, self.rate, "+0%", "+0Hz",
                            "WordBoundary",
                        ),
                        self.text,
                    )
                )
                yield {"type": "audio", "data": b"audio"}
                for word in self.text.split():
                    yield {
                        "type": "WordBoundary",
                        "offset": 0,
                        "duration": 1_000_000,
                        "text": word,
                    }

        text = "مرحبا hello بك"
        worker = speech_worker.SpeechWorker(
            text,
            voice="fr-FR-VivienneMultilingualNeural",
            target_language="ar",
            read_latin_words=True,
            fast=False,
            continuous_stream=True,
        )
        FakeCommunicate.calls = []
        FakeCommunicate.ssml = []
        try:
            with mock.patch.object(
                speech_worker,
                "edge_tts",
                type("Edge", (), {"Communicate": FakeCommunicate}),
            ):
                async def synthesize_runs():
                    for index in range(len(worker._segments)):
                        await worker._make_segment(index)

                asyncio.run(synthesize_runs())
        finally:
            worker.cleanup_output()

        self.assertEqual(
            [text.strip() for text, _voice in FakeCommunicate.calls],
            ["مرحبا hello، بك"],
        )
        self.assertEqual(
            [voice for _text, voice in FakeCommunicate.calls],
            ["fr-FR-VivienneMultilingualNeural"],
        )
        parsed = [
            ElementTree.fromstring(ssml)
            for ssml in FakeCommunicate.ssml
        ]
        namespace = "{http://www.w3.org/2001/10/synthesis}"
        xml_namespace = "{http://www.w3.org/XML/1998/namespace}"
        self.assertEqual(
            [root.attrib[f"{xml_namespace}lang"] for root in parsed],
            ["ar-SA"],
        )

    def test_latin_words_are_skipped_by_default_even_with_non_multilingual_voice(self):
        segments, _voices, _spans = (
            speech_worker._speech_segments_with_voices(
                "مرحبا, hello بك",
                "ar-SA-ZariyahNeural",
                target_language="ar",
            )
        )

        self.assertEqual(segments, ["مرحبا, بك"])
        self.assertNotIn("hello", " ".join(segments))

    def test_latin_only_text_is_skipped_by_default_for_arabic_target(self):
        segments, _voices, _spans = speech_worker._speech_segments_with_voices(
            "bonjour hello",
            "ar-SA-ZariyahNeural",
            target_language="ar",
        )

        self.assertEqual(segments, [])

    def test_latin_target_language_is_not_skipped(self):
        segments, voices, _spans = speech_worker._speech_segments_with_voices(
            "bonjour مرحبا",
            "fr-FR-VivienneMultilingualNeural",
            target_language="fr",
        )

        self.assertEqual(segments, ["bonjour", "مرحبا"])
        self.assertEqual(
            voices,
            ["fr-FR-VivienneMultilingualNeural"] * 2,
        )

    def test_requested_worker_cancellation_is_handled_without_thread_error(self):
        async def cancelled_synthesis():
            raise asyncio.CancelledError()

        worker = speech_worker.SpeechWorker(
            "hello", target_language="en", fast=False)
        failed = []
        worker.failed.connect(failed.append)
        worker.requestInterruption()

        with mock.patch.object(speech_worker, "speech_available",
                               return_value=True), \
                mock.patch.object(worker, "isInterruptionRequested",
                                  return_value=True), \
                mock.patch.object(worker, "_synthesize",
                                  cancelled_synthesis):
            worker.run()

        self.assertEqual(failed, [])

    def test_audio_cache_version_skips_truncated_segment_entries(self):
        new_key = speech_worker._cache_key(
            "مرحبا بالعالم", "fr-FR-VivienneMultilingualNeural", "+15%")

        old_alignment_key = hashlib.sha1(
            "\x00".join((
                "v17", "edge", "fr-FR-VivienneMultilingualNeural", "+15%",
                "مرحبا بالعالم",
            )).encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(new_key, old_alignment_key)

        for old_version in (
            "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10",
            "v11", "v12", "v13", "v14",
        ):
            old_key = hashlib.sha1(
                "\x00".join((
                    old_version, "fr-FR-VivienneMultilingualNeural", "+15%",
                    "مرحبا بالعالم",
                )).encode("utf-8")
            ).hexdigest()
            self.assertNotEqual(new_key, old_key)

    def test_script_language_uses_distinct_cache_entry(self):
        arabic_key = speech_worker._cache_key(
            "مرحبا", "fr-FR-VivienneMultilingualNeural", "+15%",
            ssml_language="ar-SA",
        )
        mixed_key = speech_worker._cache_key(
            "مرحبا",
            "fr-FR-VivienneMultilingualNeural",
            "+15%",
            ssml_language="en-US",
        )

        self.assertNotEqual(arabic_key, mixed_key)

    def test_cached_multilingual_segment_reanchors_highlight_to_current_text(self):
        worker = speech_worker.SpeechWorker(
            "مرحبا (Hello) بك", source_offset=10, fast=True
        )
        segment_index = 0
        segment = worker._segments[segment_index]
        voice = worker._segment_voices[segment_index]
        key = speech_worker._cache_key(
            segment,
            voice,
            worker._rate,
            ssml_language=worker._ssml_language_for_segment(segment, voice),
        )
        cache_dir = tempfile.TemporaryDirectory(prefix="harmony-tts-cache-")
        self.addCleanup(cache_dir.cleanup)

        with mock.patch.dict(os.environ, {"HARMONY_TTS_CACHE": cache_dir.name}):
            worker._write_cache(
                key, b"cached-audio", [(0, 1_000_000, 1, 2)]
            )

            async def load_cached():
                return await worker._make_segment(segment_index)

            with mock.patch.object(
                worker,
                "_store_temp",
                side_effect=lambda _audio, boundaries: ("cached.mp3", boundaries),
            ):
                _path, boundaries = asyncio.run(load_cached())

        self.assertEqual(
            boundaries,
            [(
                0,
                1_000_000,
                10 + worker._segment_word_spans[segment_index][1][0],
                10 + worker._segment_word_spans[segment_index][1][1],
            )],
        )

    @unittest.skipUnless(speech_worker.speech_available(),
                         "edge-tts is an optional dependency")
    def test_uses_current_edge_synthesis_endpoint(self):
        import edge_tts.communicate as communicate
        from edge_tts.constants import SEC_MS_GEC_VERSION

        self.assertTrue(
            communicate.WSS_URL.startswith(
                "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
            )
        )
        self.assertTrue(SEC_MS_GEC_VERSION.startswith("2-"))

    def test_speech_trace_records_first_audio_and_segment_completion(self):
        class Trace:
            def __init__(self):
                self.events = []

            def mark(self, stage, *, once=True, **details):
                self.events.append((stage, details))

        class FakeCommunicate:
            def __init__(self, text, voice, **_options):
                self.text = text

            async def stream(self):
                yield {"type": "audio", "data": b"audio"}
                yield {
                    "type": "WordBoundary",
                    "offset": 0,
                    "duration": 1_000_000,
                    "text": self.text,
                }

        trace = Trace()
        worker = speech_worker.SpeechWorker(
            "hello",
            voice="en-US-AriaNeural",
            target_language="en",
            fast=False,
            stream_audio=False,
            trace=trace,
        )
        try:
            with mock.patch.object(
                speech_worker,
                "edge_tts",
                type("Edge", (), {"Communicate": FakeCommunicate}),
            ):
                asyncio.run(worker._make_segment(0))
        finally:
            worker.cleanup_output()

        stages = [stage for stage, _details in trace.events]
        self.assertIn("segment_started", stages)
        self.assertIn("edge_request_started", stages)
        self.assertIn("edge_first_audio", stages)
        self.assertIn("edge_first_word_boundary", stages)
        completed = next(
            details for stage, details in trace.events
            if stage == "segment_synthesized"
        )
        self.assertEqual(completed["audio_bytes"], len(b"audio"))
        self.assertEqual(completed["word_boundaries"], 1)

    def test_speech_is_unavailable_without_edge_tts(self):
        missing_edge = type("MissingEdge", (), {"Communicate": None})

        with mock.patch.object(speech_worker, "edge_tts", missing_edge):
            self.assertFalse(speech_worker.speech_available())

    def test_edge_synthesis_error_is_not_replaced_by_another_provider(self):
        class FailedCommunicate:
            def __init__(self, text, voice, **options):
                self.text = text
                self.voice = voice
                self.options = options

            def stream(self):
                return self

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("Edge synthesis failed")

        edge = type("Edge", (), {"Communicate": FailedCommunicate})
        worker = speech_worker.SpeechWorker(
            "hello", target_language="en", fast=False)

        with mock.patch.object(speech_worker, "edge_tts", edge):
            with self.assertRaisesRegex(RuntimeError, "Edge synthesis failed"):
                asyncio.run(worker._make_segment(0))

    def test_saved_segment_contains_every_edge_audio_chunk(self):
        class StreamCommunicate:
            def __init__(self, *_args, **_kwargs):
                del _args, _kwargs

            async def _chunks(self):
                yield {"type": "audio", "data": b"first-frame"}
                yield {"type": "audio", "data": b"second-frame"}

            def stream(self):
                return self._chunks()

        class FakeStreamServer:
            def create_stream(self):
                return "token", "http://127.0.0.1/token"

            def write(self, _token, _audio):
                del _token, _audio

            def finish(self, _token):
                del _token

        edge = type("Edge", (), {"Communicate": StreamCommunicate})
        worker = speech_worker.SpeechWorker(
            "Hello",
            target_language="en",
            fast=False,
            stream_server=FakeStreamServer(),
        )

        with mock.patch.object(speech_worker, "edge_tts", edge):
            path, _ = asyncio.run(worker._make_segment(0))
        try:
            self.assertEqual(
                Path(path).read_bytes(), b"first-framesecond-frame")
        finally:
            worker.cleanup_output()

    def test_non_streaming_playback_waits_for_complete_segment_audio(self):
        class StreamCommunicate:
            def __init__(self, *_args, **_kwargs):
                del _args, _kwargs

            async def stream(self):
                yield {"type": "audio", "data": b"first-frame"}
                yield {"type": "audio", "data": b"second-frame"}

        edge = type("Edge", (), {"Communicate": StreamCommunicate})
        worker = speech_worker.SpeechWorker(
            "Hello", target_language="en", fast=False, stream_audio=False)
        stream_urls = []
        worker.segment_stream_ready.connect(
            lambda index, url: stream_urls.append((index, url)))

        with mock.patch.object(speech_worker, "edge_tts", edge), \
                mock.patch.object(
                    speech_worker,
                    "AudioStreamServer",
                    side_effect=AssertionError("stream server must not start"),
                ):
            path, _ = asyncio.run(worker._make_segment(0))

        try:
            self.assertEqual(
                Path(path).read_bytes(), b"first-framesecond-frame")
            self.assertEqual(stream_urls, [])
        finally:
            worker.cleanup_output()

    @requires_loopback
    def test_continuous_playback_streams_segments_in_one_ordered_track(self):
        import threading
        from urllib.request import urlopen
        from PySide6.QtCore import Qt
        from popup.audio_stream import AudioStreamServer

        class FakeCommunicate:
            requests = []

            def __init__(self, text, *_args, **_kwargs):
                self.text = text
                type(self).requests.append(text)

            async def stream(self):
                yield {"type": "audio", "data": b"x" * 48_000}
                yield {
                    "type": "WordBoundary",
                    "offset": 0,
                    "duration": 1_000_000,
                    "text": self.text.split()[0],
                }

        text = "مرحبا بالعالم " * 120
        stream_server = AudioStreamServer()
        worker = speech_worker.SpeechWorker(
            text,
            fast=False,
            stream_server=stream_server,
            continuous_stream=True,
            stream_audio=False,
        )
        stream_urls = []
        boundary_events = []
        received_audio = bytearray()
        stream_errors = []
        consumer_threads = []

        def consume_stream(url):
            try:
                with urlopen(url, timeout=5) as response:
                    received_audio.extend(response.read())
            except Exception as error:
                stream_errors.append(error)

        def start_consumer(index, url):
            stream_urls.append((index, url))
            consumer = threading.Thread(
                target=consume_stream, args=(url,), daemon=True)
            consumer_threads.append(consumer)
            consumer.start()

        worker.segment_stream_ready.connect(
            start_consumer,
            Qt.ConnectionType.DirectConnection,
        )
        worker.word_boundary_ready.connect(
            lambda index, boundary: boundary_events.append((index, boundary)),
            Qt.ConnectionType.DirectConnection,
        )

        with mock.patch(
            "popup.speech_worker.edge_tts",
            type("Edge", (), {"Communicate": FakeCommunicate}),
        ):
            FakeCommunicate.requests = []
            worker.start()
            self.assertTrue(worker.wait(5000))

        try:
            for consumer in consumer_threads:
                consumer.join(5)
            self.assertEqual(len(worker._segments), 1)
            self.assertEqual(FakeCommunicate.requests, worker._segments)
            self.assertEqual(len(stream_urls), 1)
            self.assertEqual(stream_urls[0][0], 0)
            self.assertTrue(
                stream_urls[0][1].startswith("http://127.0.0.1:"))
            self.assertTrue(all(not thread.is_alive()
                                for thread in consumer_threads))
            self.assertEqual(stream_errors, [])
            self.assertEqual(
                received_audio, b"x" * 48_000 * len(worker._segments))
            self.assertEqual(
                [index for index, _boundary in boundary_events],
                [0] * len(worker._segments),
            )
            self.assertEqual(
                [boundary[0] for _index, boundary in boundary_events],
                [
                    80_000_000 * index
                    for index in range(len(worker._segments))
                ],
            )
        finally:
            stream_server.close()
            worker.cleanup_output()

    def test_continuous_playback_prefetches_without_reordering_audio(self):
        import asyncio

        class FakeStreamServer:
            def __init__(self):
                self.audio = bytearray()

            def create_stream(self):
                return "token", "http://127.0.0.1/token"

            def write(self, _token, audio):
                self.audio.extend(audio)

            def finish(self, _token):
                pass

        release_first = asyncio.Event()
        prefetches_started = asyncio.Event()
        started_segments = set()

        class FakeCommunicate:
            def __init__(self, text, *_args, **_kwargs):
                self.text = text

            async def stream(self):
                if self.text == "first":
                    if started_segments != {"second", "third"}:
                        raise AssertionError(
                            "lookahead requests must start before playback audio"
                        )
                    yield {"type": "audio", "data": b"first"}
                    await release_first.wait()
                else:
                    started_segments.add(self.text)
                    if len(started_segments) == 2:
                        prefetches_started.set()
                    yield {"type": "audio", "data": self.text.encode()}
                yield {
                    "type": "WordBoundary",
                    "offset": 0,
                    "duration": 1,
                    "text": self.text,
                }

        server = FakeStreamServer()
        worker = speech_worker.SpeechWorker(
            "first second third",
            target_language="en",
            fast=False,
            stream_server=server,
            continuous_stream=True,
        )
        worker._segments = ["first", "second", "third"]
        worker._segment_voices = ["en-US-AriaNeural"] * 3
        worker._segment_word_spans = [
            [(0, 5)], [(6, 12)], [(13, 18)]
        ]

        async def synthesize():
            task = asyncio.create_task(worker._synthesize_continuous(3))
            await asyncio.wait_for(prefetches_started.wait(), timeout=2)
            self.assertFalse(task.done())
            release_first.set()
            await task

        try:
            with mock.patch.object(
                speech_worker,
                "edge_tts",
                type("Edge", (), {"Communicate": FakeCommunicate}),
            ):
                asyncio.run(synthesize())
            self.assertEqual(bytes(server.audio), b"firstsecondthird")
        finally:
            worker.cleanup_output()

    def test_continuous_playback_does_not_split_first_sentence_at_90_chars(self):
        text = (
            "Before continuing the study of the relationship between "
            "harmonics and musical harmony, we will explain a "
            "psychoacoustic phenomenon of great importance in music."
        )
        worker = speech_worker.SpeechWorker(
            text,
            target_language="en",
            continuous_stream=True,
            fast=False,
        )
        classic_worker = speech_worker.SpeechWorker(
            text, target_language="en", fast=False)

        self.assertEqual(len(worker._segments), 1)
        self.assertGreater(len(worker._segments[0]),
                           speech_worker.FIRST_CHUNK_LENGTH)
        self.assertGreater(len(classic_worker._segments), 1)

    def test_continuous_playback_uses_one_request_for_long_text(self):
        text = "هذه جملة قصيرة. " + "كلمة " * 400
        worker = speech_worker.SpeechWorker(
            text,
            voice="fr-FR-VivienneMultilingualNeural",
            target_language="ar",
            continuous_stream=True,
            fast=False,
        )

        self.assertEqual(len(worker._segments), 1)
        self.assertGreater(
            len(worker._segments[0]),
            speech_worker.MAX_CHUNK_LENGTH,
        )

    def test_continuous_playback_streams_cached_segments_in_order(self):
        class FakeStreamServer:
            def __init__(self):
                self.audio = bytearray()
                self.finished = False

            def create_stream(self):
                return "token", "http://127.0.0.1/token"

            def write(self, _token, audio):
                self.audio.extend(audio)

            def finish(self, _token):
                self.finished = True

        text = "مرحبا بالعالم " * 130
        stream_server = FakeStreamServer()
        worker = speech_worker.SpeechWorker(
            text,
            fast=True,
            stream_server=stream_server,
            continuous_stream=True,
            stream_audio=False,
        )
        cached_audio = b"cached-frame" * 4_000
        boundaries = []
        worker.word_boundary_ready.connect(
            lambda index, boundary: boundaries.append((index, boundary)))

        def cached_segment(_key):
            return cached_audio, [(0, 1_000_000, 0, 1)]

        with mock.patch.object(speech_worker, "_prune_cache"), \
                mock.patch.object(
                    worker, "_read_cache", side_effect=cached_segment):
            asyncio.run(worker._synthesize())

        self.assertEqual(len(worker._segments), 1)
        self.assertEqual(
            bytes(stream_server.audio),
            cached_audio * len(worker._segments),
        )
        self.assertEqual([index for index, _ in boundaries],
                         [0] * len(worker._segments))
        self.assertEqual(
            [boundary[0] for _index, boundary in boundaries],
            [
                (len(cached_audio) * 8 * 10_000_000 // 48_000) * index
                for index in range(len(worker._segments))
            ],
        )
        self.assertTrue(stream_server.finished)

    def test_arabic_synthesis_forces_arabic_language_and_digit_pronunciation(self):
        from xml.etree import ElementTree
        from edge_tts.communicate import TTSConfig

        captured = {}

        class FakeCommunicate:
            def __init__(self, text, voice, rate=None, boundary=None):
                self.text = text
                self.voice = voice
                self.rate = rate

            async def stream(self):
                captured["text"] = self.text
                captured["ssml"] = speech_worker.edge_tts_communicate.mkssml(
                    TTSConfig(
                        self.voice, self.rate, "+0%", "+0Hz", "WordBoundary"
                    ),
                    self.text,
                )
                yield {"type": "audio", "data": b"audio"}
                for index, word in enumerate(self.text.split()):
                    yield {
                        "type": "WordBoundary",
                        "offset": index * 1_000_000,
                        "duration": 1_000_000,
                        "text": word,
                    }

        text = "تغيّر الصوت بعد Arnold في 1910."
        worker = speech_worker.SpeechWorker(
            text,
            voice="fr-FR-VivienneMultilingualNeural",
            target_language="ar",
            fast=False,
            stream_audio=False,
        )
        with mock.patch.object(
            speech_worker,
            "edge_tts",
            type("Edge", (), {"Communicate": FakeCommunicate}),
        ):
            path, boundaries = asyncio.run(worker._make_segment(0))

        try:
            self.assertEqual(
                captured["text"],
                "تغيّر الصوت بعد في ١٩١٠",
            )
            self.assertNotIn("Arnold", captured["text"])
            root = ElementTree.fromstring(captured["ssml"])
            namespace = "{http://www.w3.org/2001/10/synthesis}"
            self.assertEqual(
                root.attrib["{http://www.w3.org/XML/1998/namespace}lang"],
                "ar-SA",
            )
            lang_element = root.find(f".//{namespace}lang")
            self.assertIsNone(lang_element)
            self.assertTrue(any(
                text[start:end] == "1910"
                for _, _, start, end in boundaries
            ))
        finally:
            worker.cleanup_output()

    def test_arabic_only_digit_conversion_preserves_other_languages(self):
        self.assertEqual(
            speech_worker._speech_text_for_synthesis("1910", "ar"),
            "١٩١٠",
        )
        self.assertEqual(
            speech_worker._speech_text_for_synthesis("1910", "fr"),
            "1910",
        )
        self.assertEqual(
            speech_worker._match_boundary_span(
                "١٩١٠", "1910", [(0, 4)], 0),
            (0, 4, 1),
        )
