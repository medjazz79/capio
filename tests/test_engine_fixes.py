"""Regression tests for the sixteen translation-engine defects found in audit.

Every test here names the promise it defends, and every one of them was written
*before* the fix and watched fail for the stated reason. They live in their own
file so the existing suites stay untouched.

The two promises `chain.py` exists to keep:

  1. no silent substitution -- the badge always names the engine that answered;
  2. no silent truncation -- a reply the provider cut off is never delivered as
     a finished translation.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from config.settings import DEFAULTS
from translators import (
    chain, gemini, opencode_bridge, opencode_compat, probe, registry, verify,
)
from popup.speech_worker import split_speech_text

ARABIC = "النص العربي المترجم بشكل صحيح ومكتوب كله بالحروف العربية"
ENGLISH = ("This translation was never converted into Arabic at all, which "
           "means the model ignored the request entirely.")


def _settings(**extra):
    s = dict(DEFAULTS)
    s.update(extra)
    return s


class TestSpeechKeepsFrenchElisions(unittest.TestCase):
    def test_first_chunk_split_preserves_word_order(self):
        speech = split_speech_text(
            "aa bb cc d eee", first_chunk_length=7)

        self.assertEqual(speech, ["aa bb", "cc d eee"])

    def test_first_chunk_split_keeps_all_following_words_in_remainder(self):
        from popup.speech_worker import _segment_word_spans, _word_spans

        text = (
            "Premier passage de contrôle : la première phrase avance "
            "normalement, mot après mot. Deuxième passage : nous vérifions "
            "que les mots suivants restent dans le bon ordre. Troisième "
            "passage avec quelques mots répétés, répétés, afin de vérifier "
            "la synchronisation."
        )
        segments = split_speech_text(text)
        expected = [
            text[start:end].casefold()
            for start, end in _word_spans(text)
        ]
        actual = [
            text[start:end].casefold()
            for segment in _segment_word_spans(text, segments)
            for start, end in segment
        ]

        self.assertEqual(actual, expected)

    def test_internal_apostrophes_do_not_become_tts_pauses(self):
        self.assertEqual(
            split_speech_text("l'accord n'apporte rien."),
            ["l'accord n'apporte rien"],
        )
        self.assertEqual(
            split_speech_text("l’accord n’apporte rien."),
            ["l'accord n'apporte rien"],
        )

    def test_apostrophes_outside_words_still_become_punctuation_pauses(self):
        self.assertEqual(
            split_speech_text("mot' autre"),
            ["mot, autre"],
        )

    def test_hyphenated_compounds_do_not_become_tts_pauses(self):
        self.assertEqual(
            split_speech_text("sous-dominante."),
            ["sous-dominante"],
        )
        self.assertEqual(
            split_speech_text("sous‐dominante et contre-dominante."),
            ["sous‐dominante et contre-dominante"],
        )

    def test_dashes_between_words_still_become_tts_pauses(self):
        self.assertEqual(
            split_speech_text("sous - dominante"),
            ["sous, dominante"],
        )


class TestSpeechBoundaryAlignment(unittest.TestCase):
    def test_arabic_repeats_numbers_urls_and_emoji_keep_source_spans(self):
        from popup.speech_worker import _segment_word_spans

        text = "😀 مَرْحَبًا مَرْحَبًا 3.14 https://example.com/a-b"
        segments = split_speech_text(text)
        spans = _segment_word_spans(text, segments)

        self.assertEqual(
            [text[start:end] for start, end in spans[0]],
            [
                "مَرْحَبًا", "مَرْحَبًا", "3", "14",
                "https", "example", "com", "a", "b",
            ],
        )

    def test_boundary_text_resynchronizes_after_merged_or_unmatched_words(self):
        import asyncio
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        class FakeStreamServer:
            def create_stream(self):
                return "stream", "http://localhost/stream"

            def write(self, _token, _audio):
                pass

            def finish(self, _token):
                pass

        class FakeCommunicate:
            def __init__(self, *_args, **_kwargs):
                pass

            async def stream(self):
                words = ("l'accord", "n'apporte", "not-a-source-word",
                         "rien", "aujourd'hui")
                yield {"type": "audio", "data": b"audio"}
                for offset, word in enumerate(words):
                    yield {
                        "type": "WordBoundary",
                        "offset": offset * 10_000_000,
                        "duration": 10_000_000,
                        "text": word,
                    }

        text = "l'accord n'apporte rien aujourd'hui."
        worker = SpeechWorker(
            text,
            target_language="fr",
            fast=False,
            stream_server=FakeStreamServer(),
        )
        result_spans = worker._segment_word_spans[0]
        fake_edge_tts = type("FakeEdgeTTS", (), {
            "Communicate": FakeCommunicate,
        })
        with mock.patch.object(speech_worker, "edge_tts", fake_edge_tts), \
                mock.patch.object(
                    worker, "_store_temp",
                    side_effect=lambda _audio, boundaries: ("audio", boundaries),
                ):
            _, boundaries = asyncio.run(worker._make_segment(0))

        self.assertEqual(
            boundaries,
            [
                (0, 10_000_000, result_spans[0][0], result_spans[1][1]),
                (10_000_000, 10_000_000,
                 result_spans[2][0], result_spans[3][1]),
                (3 * 10_000_000, 10_000_000,
                 result_spans[4][0], result_spans[4][1]),
                (4 * 10_000_000, 10_000_000,
                 result_spans[5][0], result_spans[6][1]),
            ],
        )

    def test_boundary_matching_does_not_jump_to_a_distant_duplicate_word(self):
        from popup.speech_worker import _match_boundary_span, _word_spans

        text = "start one two three four five six seven target end target"
        spans = _word_spans(text)

        self.assertIsNone(
            _match_boundary_span("target", text, spans, next_word_index=0),
            "a mismatched boundary must not skip ahead to a later repeated word",
        )
        self.assertEqual(
            _match_boundary_span("start", text, spans, next_word_index=0),
            (0, 5, 1),
        )

    def test_notation_symbols_do_not_make_speech_highlight_skip_words(self):
        from popup.speech_worker import (
            _match_boundary_span, _segment_word_spans, split_speech_text,
        )

        text = "Ce sont des accords de genre C_a ou C_^a^G."
        segments = split_speech_text(
            text, max_length=1200, first_chunk_length=1200)
        spans = _segment_word_spans(text, segments)[0]
        spoken_words = [
            text[start:end] for start, end in spans
        ]
        self.assertEqual(
            spoken_words,
            ["Ce", "sont", "des", "accords", "de", "genre",
             "C", "a", "ou", "C", "a", "G"],
        )

        next_word_index = 0
        matched_source_words = []
        for boundary_text in spoken_words:
            matched = _match_boundary_span(
                boundary_text, text, spans, next_word_index)
            self.assertIsNotNone(matched, boundary_text)
            start, end, next_word_index = matched
            matched_source_words.append(text[start:end])
        self.assertEqual(
            matched_source_words,
            spoken_words,
            "punctuation in notation must not shift later boundaries",
        )
        first_notation_start = text.index("C_a")
        merged_notation = _match_boundary_span(
            "C_a", text, spans, next_word_index=6)
        self.assertEqual(
            merged_notation,
            (first_notation_start, first_notation_start + 3, 8),
        )


class TestSpeechStreamBackpressure(unittest.TestCase):
    def test_blocked_stream_write_does_not_block_other_synthesis_tasks(self):
        import asyncio
        import threading
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        write_started = threading.Event()
        release_write = threading.Event()
        heartbeat_ran = threading.Event()
        heartbeat_at_release = []

        class BlockingStreamServer:
            def create_stream(self):
                return "token", "http://localhost/stream"

            def write(self, _token, _audio):
                write_started.set()
                if not release_write.wait(2):
                    raise TimeoutError("test did not release the blocked write")

            def finish(self, _token):
                pass

        class FakeCommunicate:
            def __init__(self, *_args, **_kwargs):
                pass

            async def stream(self):
                yield {"type": "audio", "data": b"audio"}

        worker = SpeechWorker(
            "sample",
            target_language="fr",
            fast=False,
            stream_server=BlockingStreamServer(),
        )

        def release_after_timeout():
            heartbeat_at_release.append(heartbeat_ran.is_set())
            release_write.set()

        async def run_with_heartbeat():
            timer = threading.Timer(0.1, release_after_timeout)
            timer.start()
            try:
                segment = asyncio.create_task(worker._make_segment(0))
                await asyncio.to_thread(write_started.wait, 1)
                await asyncio.sleep(0.01)
                heartbeat_ran.set()
                await segment
            finally:
                release_write.set()
                timer.cancel()

        with (
            mock.patch.object(
                speech_worker, "edge_tts",
                type("FakeEdgeTTS", (), {"Communicate": FakeCommunicate}),
            ),
            mock.patch.object(
                worker, "_store_temp",
                side_effect=lambda _audio, boundaries: ("audio", boundaries),
            ),
        ):
            asyncio.run(run_with_heartbeat())

        self.assertEqual(heartbeat_at_release, [True])


class TestSpeechCache(unittest.TestCase):
    def test_cache_round_trip_checks_audio_pair_and_refreshes_recency(self):
        import tempfile
        from pathlib import Path
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(
                    speech_worker, "_cache_dir",
                    return_value=Path(directory),
                ):
            key = speech_worker._cache_key("مرحبا", "voice", "+0%")
            expected_boundaries = [(0, 10, 0, 5)]
            SpeechWorker._write_cache(key, b"mp3 bytes", expected_boundaries)

            mp3_path = Path(directory) / f"{key}.mp3"
            meta_path = Path(directory) / f"{key}.json"
            old_time = 1_000_000_000
            os.utime(mp3_path, ns=(old_time, old_time))
            os.utime(meta_path, ns=(old_time, old_time))

            self.assertEqual(
                SpeechWorker._read_cache(key),
                (b"mp3 bytes", expected_boundaries),
            )
            self.assertGreater(mp3_path.stat().st_mtime_ns, old_time)
            self.assertGreater(meta_path.stat().st_mtime_ns, old_time)

            mp3_path.write_bytes(b"different audio")
            self.assertIsNone(SpeechWorker._read_cache(key))

    def test_cache_pruning_expires_entries_after_seven_days(self):
        import tempfile
        from pathlib import Path
        from popup import speech_worker

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(
                    speech_worker, "_cache_dir",
                    return_value=Path(directory),
                ), mock.patch.object(
                    speech_worker.time, "time", return_value=1_000_000,
                ):
            cache = Path(directory)
            expired = cache / "expired.mp3"
            expired.write_bytes(b"old audio")
            expired.with_suffix(".json").write_text("[]", encoding="utf-8")
            recent = cache / "recent.wav"
            recent.write_bytes(b"recent audio")
            recent.with_suffix(".json").write_text("[]", encoding="utf-8")
            old_time = 1_000_000 - speech_worker.TTS_CACHE_MAX_AGE_SECONDS - 1
            os.utime(expired, (old_time, old_time))
            os.utime(expired.with_suffix(".json"), (old_time, old_time))

            speech_worker._prune_cache()

            self.assertFalse(expired.exists())
            self.assertFalse(expired.with_suffix(".json").exists())
            self.assertTrue(recent.exists())
            self.assertTrue(recent.with_suffix(".json").exists())


class TestSpeechTrace(unittest.TestCase):
    def test_trace_is_disabled_unless_explicitly_enabled(self):
        from popup import speech_trace

        with mock.patch.dict(os.environ, {"HARMONY_TTS_TRACE": "0"}, clear=True):
            with mock.patch.object(speech_trace, "get_logger") as get_logger:
                trace = speech_trace.SpeechTrace("test")
                trace.mark("sample")

        get_logger.assert_not_called()

    def test_capio_trace_name_is_supported(self):
        from popup import speech_trace

        logger = mock.Mock()
        with mock.patch.dict(
                os.environ,
                {"CAPIO_TTS_TRACE": "1", "HARMONY_TTS_TRACE": "0"},
                clear=True):
            with mock.patch.object(
                speech_trace, "get_logger", return_value=logger
            ):
                trace = speech_trace.SpeechTrace("test")

        logger.info.assert_called_once()

    def test_trace_logs_each_stage_once_when_enabled(self):
        from popup import speech_trace

        logger = mock.Mock()
        with mock.patch.dict(os.environ, {"HARMONY_TTS_TRACE": "1"}, clear=True):
            with mock.patch.object(
                speech_trace, "get_logger", return_value=logger
            ):
                trace = speech_trace.SpeechTrace("test")
                trace.mark("sample", value=1)
                trace.mark("sample", value=2)

        self.assertEqual(logger.info.call_count, 2)
        self.assertEqual(logger.info.call_args_list[0].args[3], "trace_started")
        self.assertEqual(logger.info.call_args_list[1].args[3], "sample")

    def test_trace_can_log_repeated_events_and_quote_string_values(self):
        from popup import speech_trace

        logger = mock.Mock()
        with mock.patch.dict(os.environ, {"HARMONY_TTS_TRACE": "1"}, clear=True):
            with mock.patch.object(
                speech_trace, "get_logger", return_value=logger
            ):
                trace = speech_trace.SpeechTrace("test")
                trace.mark("highlight", once=False, label="word one")
                trace.mark("highlight", once=False, label="word two")

        self.assertEqual(logger.info.call_count, 3)
        self.assertIn("label='word one'", logger.info.call_args_list[1].args[4])
        self.assertIn("label='word two'", logger.info.call_args_list[2].args[4])


class TestSpeechPrefetch(unittest.TestCase):
    def test_prefetch_caches_only_first_segment_without_opening_audio_stream(self):
        import asyncio
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        class FakeCommunicate:
            requested = []

            def __init__(self, text, *_args, **_kwargs):
                self.text = text
                type(self).requested.append(text)

            async def stream(self):
                yield {
                    "type": "WordBoundary",
                    "offset": 0,
                    "duration": 10_000,
                    "text": "mot",
                }
                yield {"type": "audio", "data": b"mp3"}

        fake_edge_tts = type("FakeEdgeTTS", (), {
            "Communicate": FakeCommunicate,
        })
        worker = SpeechWorker(
            "mot " * 100,
            target_language="fr",
            fast=True,
            prefetch_only=True,
        )
        cached = []
        with (
            mock.patch.object(speech_worker, "edge_tts", fake_edge_tts),
            mock.patch.object(speech_worker, "_prune_cache"),
            mock.patch.object(worker, "_read_cache", return_value=None),
            mock.patch.object(
                worker, "_write_cache",
                side_effect=lambda _key, audio, _bounds: cached.append(audio),
            ),
            mock.patch.object(
                speech_worker, "AudioStreamServer") as stream_server,
            mock.patch.object(worker, "_store_temp") as store_temp,
        ):
            asyncio.run(worker._synthesize())

        self.assertEqual(FakeCommunicate.requested, [worker._segments[0]])
        self.assertEqual(cached, [b"mp3"])
        stream_server.assert_not_called()
        store_temp.assert_not_called()
        self.assertEqual(worker._output_paths, [])

    def test_prefetch_cache_hit_does_not_recreate_temp_audio(self):
        import asyncio
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        class FakeCommunicate:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("cache hit must not call Edge TTS")

        fake_edge_tts = type("FakeEdgeTTS", (), {
            "Communicate": FakeCommunicate,
        })
        worker = SpeechWorker(
            "mot " * 100, target_language="fr", prefetch_only=True)
        with (
            mock.patch.object(speech_worker, "edge_tts", fake_edge_tts),
            mock.patch.object(speech_worker, "_prune_cache"),
            mock.patch.object(
                worker, "_read_cache", return_value=(b"mp3", [])),
            mock.patch.object(worker, "_store_temp") as store_temp,
        ):
            asyncio.run(worker._synthesize())

        store_temp.assert_not_called()
        self.assertEqual(worker._output_paths, [])


class TestSpeechSegmentScheduling(unittest.TestCase):
    def test_fast_cache_pruning_runs_after_synthesis(self):
        import asyncio
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        events = []
        worker = SpeechWorker("sample", fast=True)

        async def synthesize():
            events.append("synthesis")

        fake_edge_tts = type("FakeEdgeTTS", (), {"Communicate": object})
        with mock.patch.object(
            worker, "_synthesize", new=synthesize
        ), mock.patch.object(
            speech_worker, "edge_tts", fake_edge_tts
        ), mock.patch.object(
            speech_worker, "_prune_cache",
            side_effect=lambda: events.append("prune"),
        ):
            worker.run()

        self.assertEqual(events, ["synthesis", "prune"])

    def test_fast_cache_pruning_runs_when_synthesis_fails(self):
        import asyncio
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        events = []
        worker = SpeechWorker("sample", fast=True)

        async def synthesize():
            events.append("synthesis")
            raise RuntimeError("synthetic failure")

        fake_edge_tts = type("FakeEdgeTTS", (), {"Communicate": object})
        with mock.patch.object(
            worker, "_synthesize", new=synthesize
        ), mock.patch.object(
            speech_worker, "edge_tts", fake_edge_tts
        ), mock.patch.object(
            speech_worker, "_prune_cache",
            side_effect=lambda: events.append("prune"),
        ):
            worker.run()

        self.assertEqual(events, ["synthesis", "prune"])

    def test_fast_cache_pruning_runs_when_synthesis_is_cancelled(self):
        import asyncio
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        events = []
        worker = SpeechWorker("sample", fast=True)

        async def synthesize():
            events.append("synthesis")
            raise asyncio.CancelledError()

        fake_edge_tts = type("FakeEdgeTTS", (), {"Communicate": object})
        with mock.patch.object(
            worker, "_synthesize", new=synthesize
        ), mock.patch.object(
            speech_worker, "edge_tts", fake_edge_tts
        ), mock.patch.object(
            speech_worker, "_prune_cache",
            side_effect=lambda: events.append("prune"),
        ):
            worker.run()

        self.assertEqual(events, ["synthesis", "prune"])

    def test_first_request_is_short_and_following_requests_are_large(self):
        from popup.speech_worker import (
            FIRST_CHUNK_LENGTH, MAX_CHUNK_LENGTH, SpeechWorker,
        )

        worker = SpeechWorker("mot " * 1000, target_language="fr")

        self.assertLessEqual(len(worker._segments[0]), FIRST_CHUNK_LENGTH)
        self.assertGreater(len(worker._segments[1]), FIRST_CHUNK_LENGTH)
        self.assertTrue(all(
            len(segment) <= MAX_CHUNK_LENGTH
            for segment in worker._segments
        ))

    def test_long_passage_is_split_into_few_large_requests(self):
        from popup.speech_worker import MAX_CHUNK_LENGTH, SpeechWorker

        worker = SpeechWorker("كلمة " * 5000)

        self.assertLessEqual(len(worker._segments), 25)
        self.assertTrue(all(
            len(segment) <= MAX_CHUNK_LENGTH
            for segment in worker._segments
        ))

    def test_fast_mode_bounds_parallel_requests_and_emits_segments_in_order(self):
        import asyncio
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        class FakeStreamServer:
            def create_stream(self):
                return "stream", "http://localhost/stream"

            def write(self, _token, _audio):
                pass

            def finish(self, _token):
                pass

        class FakeCommunicate:
            active = 0
            maximum_active = 0

            def __init__(self, text, *_args, **_kwargs):
                self.text = text

            async def stream(self):
                type(self).active += 1
                type(self).maximum_active = max(
                    type(self).maximum_active, type(self).active)
                try:
                    delay = {
                        "zero": 0,
                        "one": 0.01,
                        "two": 0.08,
                        "three": 0.08,
                        "four": 0.01,
                    }[self.text]
                    await asyncio.sleep(delay)
                    yield {"type": "audio", "data": self.text.encode()}
                    yield {
                        "type": "WordBoundary",
                        "offset": 0,
                        "duration": 1,
                        "text": self.text,
                    }
                finally:
                    type(self).active -= 1

        worker = SpeechWorker(
            "ignored",
            target_language="en",
            fast=True,
            stream_server=FakeStreamServer(),
        )
        worker._segments = ["zero", "one", "two", "three", "four"]
        worker._segment_voices = ["en-US-AriaNeural"] * 5
        worker._segment_word_spans = [
            [(0, len(segment))] for segment in worker._segments
        ]
        emitted = []
        worker.segment_ready.connect(
            lambda index, _path: emitted.append((index, FakeCommunicate.active)))

        fake_edge_tts = type("FakeEdgeTTS", (), {
            "Communicate": FakeCommunicate,
        })
        with mock.patch.object(speech_worker, "edge_tts", fake_edge_tts), \
                mock.patch.object(speech_worker, "_prune_cache"), \
                mock.patch.object(worker, "_read_cache", return_value=None), \
                mock.patch.object(worker, "_write_cache"), \
                mock.patch.object(
                    worker, "_store_temp",
                    side_effect=lambda audio, boundaries:
                        (audio.decode(), boundaries),
                ):
            asyncio.run(worker._synthesize())

        self.assertEqual([index for index, _ in emitted], [0, 1, 2, 3, 4])
        self.assertLessEqual(
            FakeCommunicate.maximum_active, speech_worker.MAX_PARALLEL_SEGMENTS)
        self.assertGreater(emitted[1][1], 0)

    def test_each_segment_is_streamed_and_emitted_in_order(self):
        import asyncio
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        class FakeStreamServer:
            def __init__(self):
                self.created = []
                self.finished = []

            def create_stream(self):
                index = len(self.created)
                token = str(index)
                self.created.append(index)
                return token, f"http://localhost/{token}"

            def write(self, _token, _audio):
                pass

            def finish(self, token):
                self.finished.append(int(token))

        class FakeCommunicate:
            def __init__(self, text, *_args, **_kwargs):
                self.text = text

            async def stream(self):
                yield {"type": "audio", "data": self.text.encode()}
                yield {
                    "type": "WordBoundary",
                    "offset": 0,
                    "duration": 1,
                    "text": self.text,
                }

        server = FakeStreamServer()
        worker = SpeechWorker(
            "ignored",
            target_language="en",
            fast=False,
            stream_server=server,
        )
        worker._segments = ["first", "second"]
        worker._segment_voices = ["en-US-AriaNeural"] * 2
        worker._segment_word_spans = [[(0, 5)], [(0, 6)]]
        events = []
        worker.segment_stream_ready.connect(
            lambda index, _url: events.append(("stream", index)))
        worker.segment_ready.connect(
            lambda index, _path: events.append(("ready", index)))
        fake_edge_tts = type("FakeEdgeTTS", (), {
            "Communicate": FakeCommunicate,
        })
        with mock.patch.object(speech_worker, "edge_tts", fake_edge_tts), \
                mock.patch.object(worker, "_read_cache", return_value=None), \
                mock.patch.object(worker, "_write_cache"):
            try:
                asyncio.run(worker._synthesize())

                self.assertEqual(server.created, [0, 1])
                self.assertEqual(server.finished, [0, 1])
                self.assertEqual(
                    events,
                    [("stream", 0), ("ready", 0), ("stream", 1), ("ready", 1)],
                )
            finally:
                worker.cleanup_output()

    def test_stream_queue_is_bounded_and_close_unblocks_a_full_queue(self):
        import time
        import threading
        from popup.audio_stream import (
            STREAM_QUEUE_MAX_BYTES, AudioStreamServer,
        )

        server = AudioStreamServer()
        token, _url = server.create_stream()
        stream = server._streams[token]
        server.write(token, b"x" * STREAM_QUEUE_MAX_BYTES)

        writer_started = threading.Event()
        writer_finished = threading.Event()

        def write_while_full():
            writer_started.set()
            server.write(token, b"blocked")
            writer_finished.set()

        writer = threading.Thread(target=write_while_full)
        writer.start()
        self.assertTrue(writer_started.wait(1))
        time.sleep(0.06)
        self.assertFalse(writer_finished.is_set())
        self.assertEqual(stream._queued_bytes, STREAM_QUEUE_MAX_BYTES)

        started = time.monotonic()
        server.close()
        writer.join(1)

        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(writer_finished.is_set())
        self.assertFalse(writer.is_alive())
        self.assertLessEqual(stream._queued_bytes, STREAM_QUEUE_MAX_BYTES)

    def test_stream_buffers_multiple_synthesis_segments_before_backpressure(self):
        import threading
        from urllib.request import urlopen
        from popup.audio_stream import STREAM_QUEUE_MAX_BYTES, AudioStreamServer

        server = AudioStreamServer()
        token, url = server.create_stream()
        segment_bytes = 496_944
        chunk_size = 720
        segment_count = 4
        segment = b"x" * segment_bytes
        writes_finished = threading.Event()
        write_errors = []

        def synthesize_into_stream():
            try:
                for _ in range(segment_count):
                    for offset in range(0, len(segment), chunk_size):
                        server.write(
                            token, segment[offset:offset + chunk_size])
            except Exception as error:
                write_errors.append(error)
            finally:
                writes_finished.set()

        writer = threading.Thread(target=synthesize_into_stream)
        try:
            writer.start()
            self.assertTrue(writes_finished.wait(2))
            writer.join(1)
            self.assertFalse(writer.is_alive())
            self.assertEqual(write_errors, [])
            self.assertEqual(
                server._streams[token]._queued_bytes,
                segment_bytes * segment_count,
            )
            self.assertLessEqual(
                server._streams[token]._queued_bytes, STREAM_QUEUE_MAX_BYTES)
            server.finish(token)

            with urlopen(url, timeout=5) as response:
                self.assertEqual(
                    response.read(), segment * segment_count)
        finally:
            server.close()

    def test_finished_stream_remains_available_until_first_get(self):
        from urllib.request import urlopen
        from popup.audio_stream import AudioStreamServer

        server = AudioStreamServer()
        token, url = server.create_stream()
        try:
            server.write(token, b"completed before GET")
            server.finish(token)

            with urlopen(url + "?player=probe", timeout=5) as response:
                self.assertEqual(response.read(), b"completed before GET")

            self.assertNotIn(token, server._streams)
        finally:
            server.close()

    def test_close_unblocks_handler_waiting_for_first_audio_chunk(self):
        import threading
        from urllib.request import urlopen
        from popup.audio_stream import AudioStreamServer

        server = AudioStreamServer()
        _token, url = server.create_stream()
        response_ready = threading.Event()
        reader_finished = threading.Event()

        def read_until_server_closes():
            try:
                with urlopen(url, timeout=5) as response:
                    response_ready.set()
                    response.read()
            finally:
                reader_finished.set()

        reader = threading.Thread(target=read_until_server_closes)
        try:
            reader.start()
            self.assertTrue(response_ready.wait(5))
            server.close()
            reader.join(2)

            self.assertTrue(reader_finished.is_set())
            self.assertFalse(reader.is_alive())
        finally:
            server.close()
            reader.join(2)

    def test_interruption_cancels_a_stalled_stream_promptly(self):
        import asyncio
        import threading
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        stream_started = threading.Event()
        stream_closed = threading.Event()

        class FakeStreamServer:
            def create_stream(self):
                return "stream", "http://localhost/stream"

            def write(self, _token, _audio):
                pass

            def finish(self, _token):
                pass

        class FakeCommunicate:
            def __init__(self, *_args, **_kwargs):
                pass

            async def stream(self):
                stream_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stream_closed.set()
                if False:
                    yield {}

        fake_edge_tts = type("FakeEdgeTTS", (), {
            "Communicate": FakeCommunicate,
        })
        worker = SpeechWorker(
            "first word",
            target_language="en",
            fast=False,
            stream_server=FakeStreamServer(),
        )
        with mock.patch.object(speech_worker, "edge_tts", fake_edge_tts):
            worker.start()
            self.assertTrue(stream_started.wait(2))
            worker.requestInterruption()
            self.assertTrue(worker.wait(2_000))

        self.assertTrue(stream_closed.wait(1))


class FakeResponse:
    """A `requests.Response` stand-in that records whether it was closed."""

    def __init__(self, status_code=200, headers=None, body=None, lines=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self._lines = list(lines)
        self.closed = 0
        self.raise_for_status_error = None

    def json(self):
        return self._body

    def iter_lines(self):
        return iter(self._lines)

    def close(self):
        self.closed += 1

    def raise_for_status(self):
        if self.raise_for_status_error is not None:
            raise self.raise_for_status_error


class TestGemma2OmitsUnsupportedReasoningOptions(unittest.TestCase):
    def test_disabling_reasoning_sends_an_explicit_off_switch(self):
        response = FakeResponse(
            body={"choices": [{"message": {"content": "OK"}}]})
        with mock.patch.object(
            opencode_compat.requests, "post", return_value=response,
        ) as post:
            result = opencode_compat.translate(
                "Say OK", "https://api.example/v1", "key",
                "reasoning-model", reasoning_enabled=False,
            )

        self.assertEqual(result, "OK")
        payload = post.call_args.kwargs["json"]
        # Omitting the field is not enough: gateways then apply the model's
        # own default, which for the open-weight vision models is thinking
        # ON. The switch must be explicit so nothing turns reasoning on.
        self.assertEqual(payload.get("reasoning_effort"), "none")
        self.assertNotIn("extra_body", payload)

    def test_nonstreaming_gemma2_request_omits_reasoning_parameters(self):
        response = FakeResponse(
            body={"choices": [{"message": {"content": "OK"}}]})
        with mock.patch.object(
            opencode_compat.requests, "post", return_value=response,
        ) as post:
            result = opencode_compat.translate(
                "Say OK", "http://localhost:11434/v1", "ollama",
                "gemma2:2b", reasoning_enabled=True,
            )

        self.assertEqual(result, "OK")
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("extra_body", payload)

    def test_streaming_gemma2_omits_reasoning_but_other_models_keep_it(self):
        response = FakeResponse(lines=[
            b'data: {"choices":[{"delta":{"content":"OK"},'
            b'"finish_reason":"stop"}]}',
            b"data: [DONE]",
        ])
        with mock.patch.object(
            opencode_compat.requests, "post", return_value=response,
        ) as post:
            result = "".join(opencode_compat.stream_translate(
                "Say OK", "http://localhost:11434/v1", "ollama",
                "gemma2:2b", reasoning_enabled=True,
            ))

        self.assertEqual(result, "OK")
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("extra_body", payload)

        with mock.patch.object(
            opencode_compat.requests, "post", return_value=response,
        ) as post:
            list(opencode_compat.stream_translate(
                "Say OK", "https://api.example/v1", "key",
                "reasoning-model", reasoning_enabled=True,
            ))

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["reasoning_effort"], "medium")
        self.assertEqual(payload["extra_body"], {"reasoning": {"enabled": True}})

    def test_ollama_reasoning_toggle_sends_explicit_think_value(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                response = FakeResponse(lines=[
                    b'data: {"choices":[{"delta":{"content":"OK"},'
                    b'"finish_reason":"stop"}]}',
                    b"data: [DONE]",
                ])
                with mock.patch.object(
                    opencode_compat.requests, "post", return_value=response,
                ) as post:
                    result = "".join(opencode_compat.stream_translate(
                        "Say OK", "http://localhost:11434/v1", "ollama",
                        "qwen3:8b", reasoning_enabled=enabled,
                    ))

                self.assertEqual(result, "OK")
                payload = post.call_args.kwargs["json"]
                self.assertIs(payload["think"], enabled)
                self.assertNotIn("reasoning_effort", payload)
                self.assertNotIn("extra_body", payload)


class TestCustomProviderCapabilitiesAreEnforced(unittest.TestCase):
    def _settings(self, vision_enabled):
        return dict(
            DEFAULTS,
            model="custom:local",
            custom_providers=[{
                "id": "local",
                "name": "Local",
                "base_url": "http://localhost:11434/v1",
                "api_key": "ollama",
                "model": "vision-model",
                "enabled": True,
                "reasoning_enabled": False,
                "vision_enabled": vision_enabled,
            }],
        )

    def test_unchecking_vision_marks_model_unsupported_and_blocks_images(self):
        from PIL import Image

        settings = self._settings(False)
        self.assertEqual(
            registry.custom_image_capability("custom:local", settings),
            "unsupported",
        )
        with mock.patch(
            "translators.opencode_compat.translate_image",
        ) as translate_image:
            with self.assertRaisesRegex(
                RuntimeError, "Enable Vision / Images",
            ):
                chain.translate_image(Image.new("RGB", (8, 8)), settings)
        translate_image.assert_not_called()

    def test_checking_vision_sends_the_image_to_the_custom_provider(self):
        from PIL import Image

        settings = self._settings(True)
        with mock.patch(
            "translators.opencode_compat.translate_image",
            return_value="تمت ترجمة الصورة",
        ) as translate_image:
            translated, _badge = chain.translate_image(
                Image.new("RGB", (8, 8)), settings)

        self.assertEqual(translated, "تمت ترجمة الصورة")
        self.assertEqual(translate_image.call_args.args[3], "vision-model")


# --- 1. the badge must not call the user's own model a substitute -----------

class TestBadgeNamesTheEngineThatAnswered(unittest.TestCase):
    """No silent substitution. `_merge_badges` used to decide "was this your
    model?" by searching each badge for the literal `"chosen:"`. The bridge
    badge (`◈ chosen bridge: ...`) and the router badge (`🔀 platform/model`)
    never contain that substring, so a page answered entirely by the user's own
    engine was reported `⚠ … (not your model)` -- a false statement."""

    def test_a_bridge_page_is_not_reported_as_not_your_model(self):
        badges = ["◈ chosen bridge: space-bunny"] * 3 + [
            "Free service  ⚠ chosen model space-bunny-free failed: 403"]
        merged = chain._merge_badges(badges, chosen="space-bunny-free")
        self.assertNotIn("not your model", merged)
        self.assertIn("3/4 chunks by your model", merged)

    def test_a_router_page_is_not_reported_as_not_your_model(self):
        """The router badge names the engine it routed to, so the pseudo-model
        the user typed never appears in it. That is the whole point of the
        integration, and the merge must not read it as a substitute."""
        badges = ["🔀 groq/qwen3.8-27b"] * 2 + [
            "⚡ groq: qwen/qwen3.8-27b"]
        merged = chain._merge_badges(badges, chosen="flm:auto")
        self.assertNotIn("not your model", merged)
        self.assertIn("2/3 chunks by your model", merged)

    def test_a_gemini_page_is_still_credited_to_the_user(self):
        badges = ["✦ chosen: gemini-3.5-flash-lite"] * 6 + [
            "Free service  ⚠ chosen model gemini-3.5-flash-lite failed: 429"]
        merged = chain._merge_badges(badges, chosen="gemini-3.5-flash-lite")
        self.assertNotIn("not your model", merged)
        self.assertIn("6/7 chunks by your model", merged)

    def test_a_genuine_substitute_page_is_still_called_one(self):
        # Mixed, and none of it the chosen engine: the honest warning must
        # survive the fix, not be blunted into silence.
        badges = ["⚡ groq: qwen/x"] * 2 + [
            "☁ cf: @cf/meta/llama-3.3-70b-instruct-fp8-fast"]
        merged = chain._merge_badges(badges, chosen="gemini-3.5-flash-lite")
        self.assertIn("not your model", merged)

    def test_streaming_403_falls_back_without_retrying_the_blocked_model(self):
        s = _settings(model="opencode-cli:opencode/space-bunny-free")

        def denied_stream(*_args, **_kwargs):
            if False:
                yield ""
            raise RuntimeError(
                "403 — free tier blocked for this account")

        with mock.patch(
            "translators.opencode_cli.stream_translate",
            side_effect=denied_stream,
        ) as stream, mock.patch(
            "translators.opencode_cli.translate",
        ) as retry, mock.patch(
            "translators.fallback.gtx_translate",
            return_value="ترجمة مجانية",
        ) as free_translation:
            events = list(chain._stream_single_compatible("source", s))

        self.assertEqual(stream.call_count, 1)
        retry.assert_not_called()
        free_translation.assert_called_once()
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(events[-1][1], "ترجمة مجانية")
        self.assertIn("403", events[-1][2])

    def test_end_to_end_bridge_page_reports_the_chosen_model_share(self):
        """3 chunks answered by the bridge, 1 by a substitute: the badge must
        count the bridge chunks instead of claiming none used your model."""
        s = _settings(model="space-bunny-free")

        def stream(text, model, **_kwargs):
            if text == "chunk-4":
                raise RuntimeError("403 free tier")
            return iter(["تمت الترجمة"])

        def translate(text, model, **_kwargs):
            if text == "chunk-4":
                raise RuntimeError("403 free tier")
            return "تمت الترجمة"

        with mock.patch("translators.opencode_bridge.stream",
                        side_effect=stream), \
                mock.patch("translators.opencode_bridge.translate",
                           side_effect=translate), \
                mock.patch("translators.fallback.gtx_translate",
                           return_value="ترجمة مجانية"), \
                mock.patch("translators.chain.chunk_text",
                           return_value=["chunk-1", "chunk-2", "chunk-3",
                                         "chunk-4"]):
            _text, badge = chain.translate("long passage", s)
        self.assertNotIn("not your model", badge)
        self.assertIn("3/4 chunks by your model", badge)


# --- 2 & 3. no silent truncation on the streaming paths ---------------------

class TestStreamingTruncationIsNeverSilent(unittest.TestCase):

    def test_bridge_stream_cut_off_after_text_is_rejected(self):
        """`opencode_bridge` raised only when a truncated stream had produced
        no text at all. A stream cut off by the token cap *after* emitting a
        paragraph was returned as a finished translation with a normal badge."""
        import json

        class FakeStream:
            def iter_lines(self):
                yield ("data: " + json.dumps({
                    "choices": [{"delta": {"content": "already translated"},
                                 "finish_reason": None}]})).encode()
                yield ("data: " + json.dumps({
                    "choices": [{"delta": {"content": " and then cut"},
                                 "finish_reason": "length"}]})).encode()
                yield b"data: [DONE]"

            def close(self):
                pass

        with mock.patch.object(opencode_bridge, "_open_stream",
                               return_value=FakeStream()):
            with self.assertRaisesRegex(RuntimeError, "cut off"):
                opencode_bridge.translate("source", "mimo-v2.6-flash-free")

    def test_bridge_stream_cut_off_after_text_is_rejected_while_streaming(self):
        """The popup consumes the generator directly, so the raise has to
        happen at the end of the stream there too."""
        import json

        class FakeStream:
            def iter_lines(self):
                yield ("data: " + json.dumps({
                    "choices": [{"delta": {"content": "partial"},
                                 "finish_reason": "length"}]})).encode()
                yield b"data: [DONE]"

            def close(self):
                pass

        with mock.patch.object(opencode_bridge, "_open_stream",
                               return_value=FakeStream()):
            with self.assertRaisesRegex(RuntimeError, "cut off"):
                list(opencode_bridge.stream("source", "mimo-v2.6-flash-free"))

    def test_sse_reply_cut_off_mid_passage_is_not_delivered_as_complete(self):
        """`stream_translate` accumulated `delta.content` and never looked at
        `finish_reason`, while the non-streaming `_reply_text` rejects the same
        reply. Fast mode is the default, so this was the path users saw."""
        import json

        def sse():
            yield ("data: " + json.dumps({
                "choices": [{"delta": {"content": "half a page"},
                             "finish_reason": None}]})).encode()
            yield ("data: " + json.dumps({
                "choices": [{"delta": {"content": " then the cap"},
                             "finish_reason": "length"}]})).encode()
            yield b"data: [DONE]"

        response = FakeResponse(
            200, headers={"X-Routed-Via": "groq/qwen3.8-27b"}, lines=sse())
        with mock.patch.object(opencode_compat.requests, "post",
                               return_value=response):
            report = {}
            with self.assertRaisesRegex(RuntimeError, "cut off"):
                list(opencode_compat.stream_translate(
                    "source", "http://localhost:31415/v1", "K", "auto",
                    report=report))
        # the routing badge must still reach the popup
        self.assertEqual(report["routed_via"], "groq/qwen3.8-27b")
        self.assertEqual(response.closed, 1)

    def test_a_complete_sse_reply_still_works(self):
        import json

        def sse():
            yield ("data: " + json.dumps({
                "choices": [{"delta": {"content": "النص"},
                             "finish_reason": None}]})).encode()
            yield ("data: " + json.dumps({
                "choices": [{"delta": {}, "finish_reason": "stop"}]})).encode()
            yield b"data: [DONE]"

        response = FakeResponse(200, lines=sse())
        with mock.patch.object(opencode_compat.requests, "post",
                               return_value=response):
            out = list(opencode_compat.stream_translate(
                "source", "http://example.test/v1", "K", "auto"))
        self.assertEqual("".join(out), "النص")


# --- 4. the image route must send the model it badges -----------------------

class TestImageRouteSendsTheModelItNames(unittest.TestCase):

    def test_the_image_request_and_the_badge_name_one_model(self):
        """`openai_model` overrode the selected model for images while the
        badge always named the selected one: text went to `vendor/a`, images to
        `vendor/b`, and the badge said `vendor/a`."""
        from PIL import Image
        image = Image.new("RGB", (10, 10), "white")
        s = _settings(model="vendor/a", openai_model="vendor/b",
                      openai_base="https://api.example/v1", openai_key="K")
        with mock.patch("translators.opencode_compat.translate_image",
                        return_value="صورة") as call:
            _out, badge = chain.translate_image(image, s)
        sent = call.call_args.args[3]
        self.assertIn(sent, badge,
                      msg=f"badge names one model, the request sent {sent}")
        self.assertEqual(sent, "vendor/a")

    def test_the_text_route_still_sends_the_selected_model(self):
        """The two routes must not drift apart again."""
        s = _settings(model="vendor/a", openai_model="vendor/b",
                      openai_base="https://api.example/v1", openai_key="K")
        with mock.patch("translators.opencode_compat.translate",
                        return_value="نص") as call:
            chain.translate("source", s)
        self.assertEqual(call.call_args.args[3], "vendor/a")


# --- 5. the Gemini catalogue cache must be keyed by API key ------------------

class TestGeminiCatalogueCacheIsKeyed(unittest.TestCase):
    """`known_models()` cached one module global. `registry.availability()`
    calls it with no key, so the very first picker render permanently cached
    `STARTER_MODELS`, and every other model the key supports showed disabled
    with "not available on your Google key" until the user pressed ⟳. Switching
    keys kept serving the previous key's catalogue."""

    def setUp(self):
        # Snapshot whatever shape the cache currently has, so this file can be
        # run against the broken module and the fixed one alike without leaking
        # a fabricated catalogue into another suite.
        cache = getattr(gemini, "_cache", None)
        self._saved_cache = dict(cache) if isinstance(cache, dict) else None
        self._saved_legacy = getattr(gemini, "_cached", None)
        self._saved_last = getattr(gemini, "_last_key", "")
        if isinstance(cache, dict):
            cache.clear()
        gemini._cached = None

    def tearDown(self):
        cache = getattr(gemini, "_cache", None)
        if isinstance(cache, dict):
            cache.clear()
            if self._saved_cache is not None:
                cache.update(self._saved_cache)
        gemini._cached = self._saved_legacy
        gemini._last_key = self._saved_last

    @staticmethod
    def _catalog(*names):
        return {"models": [
            {"name": f"models/{n}",
             "supportedGenerationMethods": ["generateContent"]}
            for n in names]}

    def test_a_keyless_render_does_not_pin_the_starters_as_a_catalogue(self):
        """`registry.availability()` asks with no key, so the very first picker
        render stored the four starters as though Google had returned them.
        Every other model the key supports then showed disabled with
        "not available on your Google key"."""
        gemini.known_models()  # the keyless call availability() makes
        with mock.patch.object(gemini.requests, "get") as get:
            get.return_value.raise_for_status.return_value = None
            get.return_value.json.return_value = self._catalog(
                "gemini-experimental-flash")
            models = gemini.known_models(api_key="KEY-A")
        self.assertIn("gemini-experimental-flash", models)

    def test_one_keys_catalogue_is_never_served_for_another(self):
        """One module global meant the second key kept getting the first key's
        catalogue, models and all, with no ⟳ in sight."""
        with mock.patch.object(gemini.requests, "get") as get:
            get.return_value.raise_for_status.return_value = None
            get.return_value.json.return_value = self._catalog(
                "gemini-alpha-flash")
            gemini.known_models(refresh=True, api_key="KEY-A")
        with mock.patch.object(gemini.requests, "get") as get:
            get.return_value.raise_for_status.return_value = None
            get.return_value.json.return_value = self._catalog(
                "gemini-beta-flash")
            gemini.known_models(refresh=True, api_key="KEY-B")
        # Back to key A: its own catalogue, not B's.
        back = gemini.known_models(api_key="KEY-A")
        self.assertIn("gemini-alpha-flash", back)
        self.assertNotIn("gemini-beta-flash", back)

    def test_a_failed_fetch_never_becomes_the_cached_catalogue(self):
        """A key that could not be read must not be remembered as supporting
        exactly the four starters."""
        with mock.patch.object(gemini.requests, "get") as get:
            get.return_value.raise_for_status.return_value = None
            get.return_value.json.return_value = self._catalog(
                "gemini-alpha-flash")
            gemini.known_models(refresh=True, api_key="KEY-A")

        def failing_get(*_args, **_kwargs):
            raise OSError("network down")

        with mock.patch.object(gemini.requests, "get",
                               side_effect=failing_get):
            fallback = gemini.known_models(refresh=True, api_key="KEY-A")
        self.assertEqual(fallback, list(gemini.STARTER_MODELS))
        # One network blip must not cost the user their whole catalogue.
        after = gemini.known_models(api_key="KEY-A")
        self.assertIn("gemini-alpha-flash", after)

    def test_the_picker_sees_the_catalog_the_user_fetched(self):
        with mock.patch.object(gemini.requests, "get") as get:
            get.return_value.raise_for_status.return_value = None
            get.return_value.json.return_value = self._catalog(
                "gemini-experimental-flash")
            gemini.known_models(refresh=True, api_key="KEY-A")
        # The rows the picker offers, and the verdict it shows on each.
        self.assertIn("gemini-experimental-flash", gemini.known_models())
        ok, reason = registry.availability(
            "gemini-experimental-flash", {"gemini_key": "KEY-A"})
        self.assertTrue(ok, msg=reason)

    def test_availability_does_not_reach_the_network(self):
        """It runs once per picker row on every repaint; a request per row
        would freeze the Settings dialog."""
        with mock.patch.object(gemini.requests, "get") as get:
            get.side_effect = AssertionError("availability must not fetch")
            registry.availability("gemini-3.5-flash-lite",
                                  {"gemini_key": "KEY-A"})
        self.assertFalse(get.called)


# --- 6. the probe hint must not promise a substitution -----------------------

class TestProbeHintNamesRealSubstitutes(unittest.TestCase):
    """The Settings screen said "If it fails, Gemini (...) translates instead."
    The chain's fallback ladder is `gtx_translate -> mymemory`; Gemini is never
    in it."""

    def test_the_hint_names_the_layers_the_chain_actually_uses(self):
        results = probe.probe_all({"model": "space-bunny-free",
                                   "gemini_key": "k"})
        hints = [detail for name, _ok, detail in results if name == "💡 Hint"]
        self.assertTrue(hints)
        hint = hints[0]
        self.assertNotIn("translates instead", hint)
        self.assertIn("free translation", hint)

    def test_the_hint_still_names_the_key_and_the_model(self):
        """What the existing test locks in, kept: the user must see which key
        and which model the mismatch is about."""
        results = probe.probe_all({"model": "space-bunny-free",
                                   "gemini_key": "k"})
        hint = [d for n, _ok, d in results if n == "💡 Hint"][0]
        self.assertIn("Gemini key", hint)
        self.assertIn("space-bunny-free", hint)


# --- 8. gateway exhaustion must not be retried on the router text path ------

class TestGatewayExhaustionIsNotRetried(unittest.TestCase):
    """`give_up_on_gateway_exhaustion` was passed only by `translate_image`.
    The router text path retried a `routing_exhausted` 429 twice and then
    blamed the user's provider quota -- advice that cannot help, because the
    exhausted quota is the router's."""

    @staticmethod
    def _exhausted():
        response = FakeResponse(
            429,
            body={"error": {
                "code": "routing_exhausted",
                "message": "All models exhausted: 7 routes checked. "
                           "Soonest reset ~23m."}},
        )
        response.raise_for_status_error = RuntimeError(
            "429 Client Error: All models exhausted ... Soonest reset ~23m")
        return response

    def test_the_router_text_path_makes_exactly_one_attempt(self):
        responses = []
        post = mock.Mock(side_effect=lambda *a, **k: (
            responses.append(self._exhausted()) or responses[-1]))
        with mock.patch.object(opencode_compat.requests, "post", post), \
                mock.patch("translators.opencode_compat.time.sleep") as sleep:
            with self.assertRaises(Exception):
                opencode_compat.translate_with_engine(
                    "source", "http://localhost:31415/v1", "K", "auto")
        self.assertEqual(post.call_count, 1,
                         msg="the router already failed over itself")
        sleep.assert_not_called()

    def test_an_ordinary_provider_still_retries(self):
        """Groq and Mistral limits are per key and do clear, so their retries
        must stay."""
        limited = []
        post = mock.Mock(side_effect=lambda *a, **k: (
            limited.append(FakeResponse(429, headers={"Retry-After": "1"}))
            or limited[-1]))
        with mock.patch.object(opencode_compat.requests, "post", post), \
                mock.patch("translators.opencode_compat.time.sleep"):
            with self.assertRaises(RuntimeError):
                opencode_compat.translate(
                    "source", "https://api.groq.com/openai/v1", "k", "m")
        self.assertEqual(post.call_count, opencode_compat.RATE_LIMIT_RETRIES + 1)

    def test_list_error_response_does_not_raise_attribute_error(self):
        response = mock.Mock()
        response.headers = {}
        response.json.return_value = [["unexpected response"]]

        self.assertFalse(opencode_compat._is_gateway_exhaustion(response))


# --- 9. the 429 retry loop must not leak responses --------------------------

class TestRateLimitRetryClosesResponses(unittest.TestCase):
    """Intermediate responses were never closed and their bodies never read,
    holding pooled sockets during exactly the traffic spike the retry is meant
    to survive."""

    def test_every_rate_limited_response_is_closed_before_the_next_attempt(self):
        made = [FakeResponse(429, headers={"Retry-After": "1"})
                for _ in range(opencode_compat.RATE_LIMIT_RETRIES + 1)]
        queue = list(made)
        order = []

        def fake_sleep(_seconds):
            order.append(f"sleep closed={sum(r.closed for r in made)}")

        post = mock.Mock(side_effect=lambda *a, **k: queue.pop(0))
        with mock.patch.object(opencode_compat.requests, "post", post), \
                mock.patch("translators.opencode_compat.time.sleep",
                           side_effect=fake_sleep):
            with self.assertRaises(RuntimeError):
                opencode_compat.translate(
                    "source", "https://api.groq.com/openai/v1", "k", "m")
        # One socket released per backoff, so the retry under load is not
        # stacking the sockets of every attempt that failed.
        self.assertEqual(order, ["sleep closed=1", "sleep closed=2"])
        self.assertEqual(post.call_count,
                         opencode_compat.RATE_LIMIT_RETRIES + 1)
        self.assertTrue(all(r.closed for r in made))

    def test_the_response_is_closed_on_the_raise_paths_too(self):
        for retry_after in ("1", "120"):
            with self.subTest(retry_after=retry_after):
                made = [FakeResponse(429, headers={"Retry-After": retry_after})
                        for _ in range(opencode_compat.RATE_LIMIT_RETRIES + 1)]
                queue = list(made)
                issued = []

                def post(*_a, **_k):
                    response = queue.pop(0)
                    issued.append(response)
                    return response

                with mock.patch.object(opencode_compat.requests, "post",
                                       side_effect=post), \
                        mock.patch("translators.opencode_compat.time.sleep"):
                    with self.assertRaises(RuntimeError):
                        opencode_compat.translate(
                            "source", "https://api.groq.com/openai/v1", "k",
                            "m")
                self.assertTrue(issued)
                self.assertTrue(all(r.closed for r in issued),
                                msg="a 429 that ends in a raise is still open")


# --- 10. valid streamed output is passed through directly --------------------

class TestStreamingPreservesValidOutput(unittest.TestCase):
    """Valid streamed output passes through; plain mode corrects wrong output."""

    def _events(self):
        s = _settings(
            model="mimo-v2.6-flash-free",
            fast_mode=True,
            target_language="en",
        )
        with mock.patch("translators.opencode_bridge.stream",
                        return_value=iter([ENGLISH])), \
                mock.patch("translators.opencode_bridge.translate",
                           return_value=ENGLISH), \
                mock.patch("translators.fallback.gtx_translate",
                           return_value=ARABIC):
            return list(chain.translate_stream("source passage", s))

    def test_the_stream_returns_the_selected_model_text_directly(self):
        events = self._events()
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(events[-1][1], ENGLISH)
        self.assertIn("chosen bridge", events[-1][2])

    def test_plain_mode_corrects_or_falls_back_on_wrong_language(self):
        s = _settings(model="mimo-v2.6-flash-free", fast_mode=False)
        with mock.patch(
            "translators.opencode_bridge.translate",
            return_value=ENGLISH,
        ) as selected_model, mock.patch(
            "translators.fallback.gtx_translate",
            return_value=ARABIC,
        ):
            text, badge = chain.translate("source passage", s)
        self.assertEqual(text, ARABIC)
        self.assertEqual(selected_model.call_count, 2)
        self.assertIn("Free service", badge)


# --- 11. the terminal message must carry the real failures ------------------

class TestFinalMessageCarriesTheRealFailure(unittest.TestCase):
    """Two bare `except Exception: pass` discarded what `gtx_translate` and
    `mymemory.translate` actually said, so an upstream quota rejection was
    reported to the user as a network problem."""

    def test_a_quota_rejection_is_not_reported_as_a_network_problem(self):
        s = _settings(model="space-bunny-free", target_language="fr")
        with mock.patch("translators.opencode_bridge.stream",
                        side_effect=RuntimeError("403")), \
                mock.patch("translators.opencode_bridge.translate",
                           side_effect=RuntimeError("403")), \
                mock.patch("translators.fallback.gtx_translate",
                           side_effect=RuntimeError(
                               "429 from the free translation service: quota")), \
                mock.patch("translators.mymemory.translate",
                           side_effect=RuntimeError(
                               "403 INVALID_EMAIL from MyMemory")):
            with self.assertRaises(RuntimeError) as caught:
                chain.translate("source passage", s)
        message = str(caught.exception)
        self.assertIn("Could not translate into French", message)
        self.assertIn("quota", message)
        self.assertIn("INVALID_EMAIL", message)

    def test_a_plain_network_failure_is_still_reported_honestly(self):
        s = _settings(model="space-bunny-free", target_language="fr")
        with mock.patch("translators.opencode_bridge.stream",
                        side_effect=RuntimeError("403")), \
                mock.patch("translators.opencode_bridge.translate",
                           side_effect=RuntimeError("403")), \
                mock.patch("translators.fallback.gtx_translate",
                           side_effect=ConnectionError("no route to host")), \
                mock.patch("translators.mymemory.translate",
                           side_effect=ConnectionError("no route to host")):
            with self.assertRaises(RuntimeError) as caught:
                chain.translate("source passage", s)
        self.assertIn("Could not translate into French",
                      str(caught.exception))


# --- 12. the Keys-page hint must name the port the app talks to -------------

class TestRouterHintUsesTheDetectedPort(unittest.TestCase):
    """The availability reason hardcoded `http://localhost:31415` while
    `FLM_DEFAULT_BASE` is `http://localhost:3001/v1`; `detect_flm_base()`
    knows the real port and the message did not ask it."""

    def test_the_hint_names_the_detected_router_port(self):
        # No key: that is when the reason (and so the Keys-page hint) is shown.
        with mock.patch.object(
                registry, "detect_flm_base",
                return_value="http://localhost:9999/v1"):
            ok, reason = registry.availability("flm:auto", {})
        self.assertFalse(ok)
        self.assertIn("9999", reason)
        self.assertNotIn("31415", reason)
        self.assertNotIn("/v1", reason)

    def test_a_stored_base_is_honoured_over_the_default(self):
        ok, reason = registry.availability(
            "flm:auto", {"flm_base": "http://localhost:4321/v1"})
        self.assertFalse(ok)
        self.assertIn("4321", reason)

    def test_the_hint_is_never_a_dead_port(self):
        """Every hint must name a port this install can actually talk to."""
        for detected in ("http://localhost:3001/v1", "http://localhost:31415/v1",
                         "http://localhost:8080/v1"):
            with self.subTest(detected=detected):
                with mock.patch.object(registry, "detect_flm_base",
                                       return_value=detected):
                    _ok, reason = registry.availability("flm:auto", {})
                port = detected.split("//")[1].split("/")[0]
                self.assertIn(port, reason)


# --- 13. one catalog must own the one free Zen id ---------------------------

class TestTheFreeZenIdHasOneOwner(unittest.TestCase):
    """`BRIDGE_FREE[0]` and `ZEN_FREE[0]` were both `space-bunny-free`.
    `provider_of` tested the bridge list first, so the Zen branch was
    unreachable and `opencode_zen` was reachable only for ids availability had
    already reported unavailable."""

    def test_the_zen_list_is_a_view_of_the_bridge_catalog(self):
        builder = getattr(registry, "zen_free_ids", None)
        self.assertTrue(
            callable(builder),
            msg="ZEN_FREE must be derived from BRIDGE_FREE: a hand-kept "
                "second copy of the id is what let two catalogs claim one "
                "model")
        with mock.patch.object(registry, "BRIDGE_FREE",
                               ["mimo-v2.6-flash-free"]):
            self.assertEqual(builder(), [],
                             msg="the Zen list follows the bridge catalog")
        with mock.patch.object(registry, "BRIDGE_FREE", ["space-bunny-free"]):
            self.assertEqual(builder(), ["space-bunny-free"])

    def test_the_id_still_routes_to_the_bridge_and_is_still_offered(self):
        self.assertEqual(registry.provider_of("space-bunny-free"), "bridge")
        self.assertIn("space-bunny-free", registry.ZEN_FREE)
        ok, _reason = registry.availability("space-bunny-free", {})
        self.assertTrue(ok)

    def test_an_unoffered_zen_id_keeps_its_honest_reason(self):
        ok, reason = registry.availability("deepseek-v4-flash-free",
                                           {"gemini_key": "k"})
        self.assertFalse(ok)
        self.assertIn("free tier", reason)


# --- 14. probe output stays concise ---------------------------------------

class TestFailedProbeNamesNoEngine(unittest.TestCase):
    """The Settings result contains only the selected model, verdict and speed."""

    def test_a_failed_probe_does_not_claim_the_free_service_is_in_charge(self):
        rendered = probe.format_results([("probe", False, "FAILED: boom")])
        self.assertNotIn("the free service", rendered)
        self.assertEqual(rendered, "❌ Model test — Not tested")

    def test_an_empty_probe_result_does_not_either(self):
        rendered = probe.format_results([])
        self.assertNotIn("the free service", rendered)

    def test_a_probe_that_ran_still_names_the_model(self):
        rendered = probe.format_results(
            [("★ Your model (space-bunny-free)", False, "FAILED: 403")])
        self.assertEqual(rendered, "❌ space-bunny-free — Not OK")

    def test_successful_probe_shows_only_model_verdict_and_speed(self):
        rendered = probe.format_results([
            ("★ Your model (space-bunny-free)", True,
             "OK in 1.4s — internal response text"),
            ("💡 Hint", True, "Choose another model."),
        ])
        self.assertEqual(rendered, "✅ space-bunny-free — OK — 1.4s")

    def test_failed_probe_does_not_show_error_advice_or_provider_details(self):
        rendered = probe.format_results([
            ("★ Your model (space-bunny-free)", False,
             "FAILED: 403 — Try another provider or configure a key"),
            ("💡 Hint", True, "Unrelated recommendation"),
        ])
        self.assertEqual(rendered, "❌ space-bunny-free — Not OK")


# --- 15. one probe definition -----------------------------------------------

class TestProbeConstantsAreDefinedOnce(unittest.TestCase):
    """`probe.PROBE_TEXT/PROBE_TIMEOUT = 25` versus `verify.PROMPT/
    PROBE_TIMEOUT = 20`, so the Settings screen and the picker verdict could
    disagree about the same model."""

    def test_both_probe_paths_use_the_same_prompt_and_timeout(self):
        self.assertEqual(probe.PROBE_TEXT, verify.PROMPT)
        self.assertEqual(probe.PROBE_TIMEOUT, verify.PROBE_TIMEOUT)

    def test_the_stricter_of_the_two_timeouts_is_the_one_kept(self):
        self.assertGreaterEqual(verify.PROBE_TIMEOUT, 25)

    def test_verify_reads_the_prompt_from_the_shared_definition(self):
        self.assertEqual(verify.PROMPT, probe.PROBE_TEXT)


# --- 16. the unreachable "needs the router URL" branch ----------------------

class TestNoDeadRouterUrlBranch(unittest.TestCase):
    """`provider_creds("freellm", settings)` never returns an empty base, so
    the branch could never fire and a user who had not set the URL up was told
    to copy a key instead."""

    def test_a_base_always_exists_for_the_router(self):
        for settings in ({}, {"flm_base": ""}, {"flm_key": "K"},
                         {"openai_base": "https://api.example/v1",
                          "openai_key": "K"}):
            base, _key = registry.provider_creds("freellm", settings)
            self.assertTrue(base, msg=f"no base for {settings}")

    def test_the_missing_router_url_branch_is_gone(self):
        import inspect
        self.assertNotIn("local router URL",
                         inspect.getsource(registry.availability))


if __name__ == "__main__":
    unittest.main()
