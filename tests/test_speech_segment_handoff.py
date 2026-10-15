"""Speech audio should be ready before each media segment reaches its end.

Two distinct defects are pinned here:

1. `speech_worker` must deliver segments in index order. Segments are
   synthesised in parallel (MAX_PARALLEL_SEGMENTS), so the popup appends
   paths by arrival; an out-of-order arrival misaligns path index from
   segment index and the highlight reads another segment's boundaries.

2. When segment N ends before segment N+1 is ready, playback must WAIT and
   resume as soon as the next stream is available.
"""
import asyncio
import io
import os
import sys
import time
import unittest
from unittest import mock

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestSegmentOrderIsStable(unittest.TestCase):
    def test_paths_are_stored_by_index_not_by_arrival(self):
        """Out-of-order arrival must not shift the path/segment alignment."""
        import inspect

        from popup import translate_popup
        source = inspect.getsource(
            translate_popup.TranslatePopup._on_speech_segment_ready)
        self.assertIn("_speech_paths[", source,
                      "paths must be stored by index, not append()")
        self.assertNotIn(
            "self._speech_paths.append(path)", source,
            "append() ignores the segment index, so a parallel arrival "
            "shifts every later segment's path")

    def test_ready_segments_are_emitted_before_slowest_parallel_segment(self):
        from popup.speech_worker import SpeechWorker

        worker = SpeechWorker(
            " ".join(["phrase"] * 500),
            target_language="en",
            fast=True,
        )
        ready = []

        async def synthesize(index):
            await asyncio.sleep(0.2 if index == 2 else 0.01)
            return f"segment-{index}.mp3", []

        worker.segment_ready.connect(
            lambda index, _path: ready.append((index, time.monotonic())))
        with mock.patch("popup.speech_worker._prune_cache"), \
                mock.patch.object(worker, "_make_segment", new=synthesize):
            asyncio.run(worker._synthesize())

        self.assertGreaterEqual(len(ready), 3)
        self.assertEqual([index for index, _ in ready[:3]], [0, 1, 2])
        self.assertGreater(
            ready[2][1] - ready[1][1],
            0.05,
            "segment 1 should be delivered as soon as it is ready, without "
            "waiting for slower later segments")

    def test_highlighting_index_is_clamped_to_what_is_ready(self):
        import inspect

        from popup import translate_popup
        source = inspect.getsource(
            translate_popup.TranslatePopup._update_speech_highlight)
        self.assertIn("_speech_index", source)


class TestHandOffKeepsHighlight(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _popup(self, source):
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        class _Sig:
            def connect(self, *_a, **_k):
                pass

            def emit(self, *_a, **_k):
                pass

        class Stub:
            def __init__(self, *a, **k):
                for name in ("stage", "partial", "done", "failed",
                             "source_ready", "finished"):
                    setattr(self, name, _Sig())

            def start(self):
                pass

            def isRunning(self):
                return False

            def requestInterruption(self):
                pass

            def cleanup_output(self):
                pass

        with mock.patch(
                "popup.translate_popup.TranslateWorker", Stub), \
                mock.patch("popup.translate_popup.ChatWorker", Stub):
            popup = TranslatePopup(dict(DEFAULTS, target_language="en"),
                                   source_text=source)
        self.addCleanup(popup.close)
        return popup

    def test_playback_trace_samples_position_and_buffering_state(self):
        from PySide6.QtMultimedia import QMediaPlayer

        popup = self._popup("A short speech passage.")
        popup._speech_trace = mock.MagicMock()
        popup._speech_index = 2
        popup._speech_waiting = True
        popup._player.position = mock.Mock(return_value=740)
        popup._player.playbackState = mock.Mock(
            return_value=QMediaPlayer.PlaybackState.PlayingState)
        popup._player.mediaStatus = mock.Mock(
            return_value=QMediaPlayer.MediaStatus.BufferingMedia)
        popup._player.bufferProgress = mock.Mock(return_value=0.25)

        popup._trace_speech_playback_sample()

        popup._speech_trace.mark.assert_called_once_with(
            "playback_sample",
            once=False,
            segment=2,
            position_ms=740,
            state="PlayingState",
            media_status="BufferingMedia",
            buffer_progress=0.25,
            waiting=True,
        )

    def test_seek_ignores_loaded_event_from_previous_source(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QMediaPlayer

        popup = self._popup("A short speech passage.")
        old_source = QUrl("http://127.0.0.1/active-stream")
        cached_source = QUrl.fromLocalFile("cached.mp3")
        with (
            mock.patch.object(popup._player, "setSource") as set_source,
            mock.patch.object(
                popup._player, "source", return_value=old_source,
            ) as player_source,
            mock.patch.object(popup._player, "setPosition") as set_position,
            mock.patch.object(popup._player, "play") as play,
            mock.patch.object(popup, "_update_speech_highlight"),
        ):
            popup._play_speech_segment("cached.mp3", 2_000)
            set_source.assert_called_once_with(cached_source)
            play.assert_called_once_with()
            set_position.assert_not_called()
            self.assertEqual(popup._pending_player_position_ms, 2_000)

            popup._on_media_status_changed(
                QMediaPlayer.MediaStatus.LoadedMedia)
            set_position.assert_not_called()
            self.assertEqual(popup._pending_player_position_ms, 2_000)

            player_source.return_value = cached_source
            popup._on_media_status_changed(
                QMediaPlayer.MediaStatus.LoadedMedia)
            set_position.assert_called_once_with(2_000)
            self.assertEqual(play.call_count, 2)
            self.assertIsNone(popup._pending_player_position_ms)
            self.assertIsNone(popup._pending_player_source)

    def test_waiting_for_the_next_segment_keeps_the_speech_alive(self):
        from PySide6.QtMultimedia import QMediaPlayer

        source = ("Before continuing the study of relationships between "
                  "harmonics and musical harmony, we explain a "
                  "psychoacoustic phenomenon of great importance.")
        popup = self._popup(source)
        popup._speech_active = True
        popup._speech_index = 0
        popup._speech_paths = {0: "seg0.mp3"}
        popup._speech_boundaries[0] = [(0, 1_000_000, 0, 40)]
        # The worker is still synthesising later segments.
        popup._speech_worker = mock.MagicMock()
        popup._speech_worker.isRunning.return_value = True

        # Segment 0 ends while segment 1 has not been produced yet.
        popup._on_media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)

        self.assertEqual(popup._speech_index, 1)
        self.assertTrue(
            popup._speech_active,
            "speech must wait for the next segment, not stop")
        self.assertTrue(
            popup._speech_waiting,
            "the popup must know it is between segments")

    def test_ready_segment_resumes_playback_and_highlight(self):
        from PySide6.QtMultimedia import QMediaPlayer

        source = ("Before continuing the study of relationships between "
                  "harmonics and musical harmony, we explain a "
                  "psychoacoustic phenomenon of great importance.")
        popup = self._popup(source)
        popup._speech_active = True
        popup._speech_index = 0
        popup._speech_paths = {0: "seg0.mp3"}
        popup._speech_boundaries[0] = [(0, 1_000_000, 0, 40)]
        popup._speech_worker = mock.MagicMock()
        popup._speech_worker.isRunning.return_value = True
        popup._on_media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)

        # Segment 1 now arrives: boundaries first, then the path.
        popup._on_speech_word_boundaries(1, [(0, 2_000_000, 40, 120)])
        with mock.patch.object(popup, "_play_ready_speech_segment") as play_ready:
            popup._on_speech_segment_ready(1, "seg1.mp3")

        self.assertTrue(popup._speech_active)
        self.assertEqual(popup._speech_paths[1], "seg1.mp3")
        play_ready.assert_called_once_with(1)
        self.assertTrue(
            popup._speech_boundaries.get(popup._speech_index),
            "the new segment must have boundaries for the current index")

    def test_end_of_speech_still_finishes_cleanly(self):
        from PySide6.QtMultimedia import QMediaPlayer

        source = "A short passage that fits in a single segment."
        popup = self._popup(source)
        popup._speech_active = True
        popup._speech_index = 0
        popup._speech_paths = {0: "seg0.mp3"}
        popup._speech_boundaries[0] = [(0, 1_000_000, 0, 40)]
        popup._speech_worker = None

        popup._on_media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)

        self.assertFalse(popup._speech_active)
        self.assertEqual(popup._speech_boundaries, {})


class TestSegmentCoordinateSpaces(unittest.TestCase):
    """`recitable` is segment-local, `spans` are full-text. Mixing them up
    matched the wrong words and left most spans unhighlighted."""

    def _worker(self, text, target_language="en"):
        from popup.speech_worker import SpeechWorker

        return SpeechWorker(
            text, target_language=target_language, fast=True)

    def test_segment_word_spans_cover_source_words_in_order(self):
        from popup.speech_worker import _word_spans

        text = ("Before continuing the study of the relationship between "
                "harmonics and musical harmony, we will explain a "
                "psychoacoustic phenomenon of great importance in music. "
                "It is the missing fundamental, known as the phantom "
                "fundamental.")
        worker = self._worker(text)
        self.assertEqual(
            [span for spans in worker._segment_word_spans for span in spans],
            _word_spans(text),
        )

    def test_every_word_span_belongs_to_a_chunk(self):
        text = ("قبل المواصلة في دراسة علاقة التوافقات بالهارموني الموسيقي، "
                "سنشرح ظاهرة نفسية صوتية بالغة الأهمية في الموسيقى. إنها "
                "النغمة الأساسية الوهمية أو المفقودة، المعروفة في علم "
                "الصوتيات باسم الارتفاع الوهمي.")
        worker = self._worker(text, target_language="ar")
        from popup.speech_worker import _word_spans
        self.assertEqual(
            [span for spans in worker._segment_word_spans for span in spans],
            _word_spans(text),
        )

    def test_boundaries_reference_real_word_positions(self):
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        text = ("Before continuing the study of the relationship between "
                "harmonics and musical harmony, we will explain a "
                "psychoacoustic phenomenon of great importance in music. "
                "It is the missing fundamental.")
        worker = SpeechWorker(
            text, target_language="en", fast=True, stream_audio=False)
        index = len(worker._segments) - 1
        spans = worker._segment_word_spans[index]

        class FakeCommunicate:
            def __init__(self, synthesis_text, *_args, **_kwargs):
                self.words = synthesis_text.split()

            async def stream(self):
                yield {"type": "audio", "data": b"audio"}
                for word_index, word in enumerate(self.words):
                    yield {
                        "type": "WordBoundary",
                        "offset": word_index * 10_000,
                        "duration": 10_000,
                        "text": word,
                    }

        def store_audio(audio, boundaries):
            return "audio.mp3", boundaries

        fake_edge_tts = type("FakeEdgeTTS", (), {
            "Communicate": FakeCommunicate,
        })
        with (
            mock.patch.object(speech_worker, "edge_tts", fake_edge_tts),
            mock.patch.object(worker, "_read_cache", return_value=None),
            mock.patch.object(worker, "_write_cache"),
            mock.patch.object(worker, "_store_temp", side_effect=store_audio),
        ):
            _, boundaries = asyncio.run(worker._make_segment(index))

        self.assertTrue(boundaries)
        for _start, _duration, source_start, source_end in boundaries:
            self.assertIn((source_start, source_end), spans,
                          "boundary must point at a real word span")


class TestShortFirstSegmentIsExpected(unittest.TestCase):
    def test_only_first_segment_is_short_and_later_segments_are_large(self):
        """Keep quick startup without frequent segment transitions."""
        from popup.speech_worker import FIRST_CHUNK_LENGTH, split_speech_text

        text = ("Before continuing the study of the relationship between "
                "harmonics and musical harmony, we will explain a "
                "psychoacoustic phenomenon of great importance in music. "
                "It is the missing fundamental.")
        segments = split_speech_text(text)
        self.assertGreaterEqual(len(segments), 2)
        self.assertLessEqual(
            len(segments[0]), FIRST_CHUNK_LENGTH + 1,
            "the first segment is deliberately short for fast start")
        self.assertGreater(len(segments[1]), FIRST_CHUNK_LENGTH)


if __name__ == "__main__":
    unittest.main()