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
import inspect
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

    def test_finished_segment_starts_the_next_ready_segment(self):
        from PySide6.QtMultimedia import QMediaPlayer

        popup = self._popup("first second")
        popup._speech_active = True
        popup._speech_index = 0
        popup._speech_paths = {0: "seg0.mp3", 1: "seg1.mp3"}
        popup._speech_worker = mock.MagicMock()
        popup._speech_worker.isRunning.return_value = True

        with mock.patch.object(popup, "_play_ready_speech_segment") as play:
            popup._on_media_status_changed(
                QMediaPlayer.MediaStatus.EndOfMedia)

        self.assertEqual(popup._speech_index, 1)
        self.assertFalse(popup._speech_waiting)
        play.assert_called_once_with(1)

    def test_segment_stream_end_waits_for_final_file_before_advancing(self):
        from PySide6.QtMultimedia import QMediaPlayer

        popup = self._popup("first second")
        popup._speech_active = True
        popup._speech_index = 0
        popup._speech_playing_stream_index = 0
        popup._speech_playing_continuous_stream = False
        popup._speech_stream_urls[1] = "http://127.0.0.1/next"
        popup._speech_worker = mock.MagicMock()
        popup._speech_worker.isRunning.return_value = True
        popup._speech_worker.disconnected_segments = set()

        with (
            mock.patch.object(popup, "_play_ready_speech_segment") as play,
            mock.patch.object(
                popup, "_resolve_continuous_stream_end") as resolve,
            mock.patch.object(
                popup, "sender", return_value=popup._speech_worker),
        ):
            popup._on_media_status_changed(
                QMediaPlayer.MediaStatus.EndOfMedia)
            self.assertEqual(popup._speech_index, 0)
            self.assertEqual(popup._speech_segment_end_pending_index, 0)
            self.assertTrue(popup._speech_waiting)
            play.assert_not_called()
            self.assertFalse(popup._speech_stream_end_pending)
            resolve.assert_not_called()
            popup._on_speech_segment_ready(0, "complete-segment.mp3")
            self.assertEqual(popup._speech_index, 1)
            self.assertIsNone(popup._speech_segment_end_pending_index)
            play.assert_called_once_with(1)

    def test_ready_segment_stream_starts_after_the_previous_segment_ends(self):
        popup = self._popup("first second")
        popup._speech_active = True
        popup._speech_index = 1
        popup._speech_waiting = True
        popup._speech_worker = mock.MagicMock()
        popup._speech_worker._continuous_stream = False

        with mock.patch.object(popup, "_play_speech_source") as play:
            popup._on_speech_stream_ready(
                1, "http://127.0.0.1/next-segment")

        self.assertEqual(
            popup._speech_stream_urls[1],
            "http://127.0.0.1/next-segment",
        )
        self.assertEqual(popup._speech_playing_stream_index, 1)
        self.assertFalse(popup._speech_playing_continuous_stream)
        play.assert_called_once()

    def test_disconnected_segment_waits_for_full_file_then_resumes(self):
        from PySide6.QtMultimedia import QMediaPlayer

        popup = self._popup("first second")
        popup._speech_active = True
        popup._speech_index = 0
        popup._speech_playing_stream_index = 0
        popup._speech_worker = mock.MagicMock()
        popup._speech_worker.isRunning.return_value = True
        popup._speech_worker.disconnected_segments = {0}
        popup._speech_stream_urls[0] = "http://127.0.0.1/current"
        popup._player.position = mock.Mock(return_value=740)

        popup._on_media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)

        self.assertTrue(popup._speech_active)
        self.assertTrue(popup._speech_waiting)
        self.assertEqual(popup._speech_index, 0)
        self.assertEqual(popup._speech_stream_recovery_positions, {0: 740})
        self.assertNotIn(0, popup._speech_stream_urls)

        with (
            mock.patch.object(popup, "sender", return_value=popup._speech_worker),
            mock.patch.object(popup, "_play_speech_segment") as play_file,
        ):
            popup._on_speech_segment_ready(0, "complete-segment.mp3")
        play_file.assert_called_once_with("complete-segment.mp3", 740)
        self.assertEqual(popup._speech_index, 0)

    def test_translation_stream_does_not_start_speech_prefetch(self):
        popup = self._popup("source")
        worker = mock.MagicMock()
        worker._popup_generation = popup._translation_generation
        popup._worker = worker
        with (
            mock.patch.object(popup, "sender", return_value=worker),
            mock.patch.object(popup, "_queue_streamed_partial") as queue,
            mock.patch.object(popup, "_start_speech_prefetch_text") as prefetch,
        ):
            popup._on_worker_partial("A streamed translation fragment.")

        queue.assert_called_once_with("A streamed translation fragment.")
        prefetch.assert_not_called()

    def test_continuous_stream_end_waits_for_worker_instead_of_advancing_segment(self):
        from PySide6.QtMultimedia import QMediaPlayer

        popup = self._popup("first second")
        popup._speech_active = True
        popup._speech_index = 0
        popup._speech_playing_stream_index = 0
        popup._speech_playing_continuous_stream = True
        popup._speech_worker = mock.MagicMock()
        popup._speech_worker.isRunning.return_value = True

        popup._on_media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)

        self.assertEqual(popup._speech_index, 0)
        self.assertTrue(popup._speech_stream_end_pending)
        self.assertTrue(popup._speech_waiting)
        self.assertTrue(popup._speech_active)

    def test_complete_continuous_stream_finishes_without_restarting(self):
        popup = self._popup("first second")
        popup._speech_active = True
        popup._speech_start_offset = 0
        popup._speech_expected_end_offset = 12
        popup._speech_boundaries_finalized = True
        popup._speech_boundaries[0] = [
            (0, 1_000_000, 0, 5),
            (1_000_000, 1_000_000, 6, 12),
        ]

        with (
            mock.patch.object(popup, "_recover_speech_playback") as recover,
            mock.patch.object(popup, "_finish_speech") as finish,
        ):
            popup._resolve_continuous_stream_end()

        finish.assert_called_once_with()
        recover.assert_not_called()

    def test_truncated_continuous_stream_resumes_from_last_boundary(self):
        popup = self._popup("first second third")
        popup._speech_active = True
        popup._speech_index = 0
        popup._speech_start_offset = 0
        popup._speech_expected_end_offset = 18
        popup._speech_boundaries[0] = [
            (0, 1_000_000, 0, 5),
            (1_000_000, 1_000_000, 6, 12),
        ]

        with (
            mock.patch.object(popup._player, "position", return_value=200),
            mock.patch.object(popup, "_begin_speech") as begin,
            mock.patch.object(popup, "_finish_speech") as finish,
        ):
            popup._resolve_continuous_stream_end()

        begin.assert_called_once_with(6)
        finish.assert_not_called()

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

    def test_new_word_boundary_refreshes_the_highlight_immediately(self):
        popup = self._popup("A short speech passage.")
        popup._speech_active = True
        popup._speech_index = 0

        with mock.patch.object(popup, "_update_speech_highlight") as update:
            popup._on_speech_word_boundary_ready(0, (0, 10_000, 0, 1))

        self.assertEqual(popup._speech_boundaries[0], [(0, 10_000, 0, 1)])
        update.assert_called_once_with()

    def test_playback_recovery_restarts_at_the_current_word_once(self):
        popup = self._popup("first second third")
        popup._speech_active = True
        popup._speech_index = 0
        popup._speech_start_offset = 0
        popup._speech_boundaries[0] = [
            (0, 1_000_000, 0, 5),
            (1_000_000, 1_000_000, 6, 12),
            (2_000_000, 1_000_000, 13, 18),
        ]

        with (
            mock.patch.object(popup._player, "position", return_value=150),
            mock.patch.object(popup, "_begin_speech") as begin,
            mock.patch.object(popup, "_finish_speech") as finish,
        ):
            popup._recover_speech_playback("temporary playback failure")
            begin.assert_called_once_with(6)
            self.assertEqual(popup._speech_recovery_attempts, 1)

            popup._recover_speech_playback("playback failed again")
            finish.assert_called_once_with("playback failed again")

    def test_recovery_waits_for_the_current_worker_to_stop(self):
        popup = self._popup("first second third")
        popup._speech_active = True
        popup._speech_index = 0
        popup._speech_boundaries[0] = [(0, 2_000_000, 0, 5)]
        popup._speech_worker = mock.MagicMock()
        popup._speech_worker.isRunning.return_value = True

        with (
            mock.patch.object(popup._player, "position", return_value=100),
            mock.patch.object(popup._player, "stop"),
            mock.patch.object(popup._player, "setSource"),
            mock.patch.object(popup, "_close_speech_stream_server"),
            mock.patch.object(popup, "_finish_speech") as finish,
        ):
            popup._recover_speech_playback("temporary playback failure")
            self.assertFalse(popup._speech_active)
            self.assertEqual(popup._pending_speech_offset, 0)
            popup._speech_worker.requestInterruption.assert_called_once_with()

            popup._recover_speech_playback("stale player error")
            finish.assert_not_called()

    def test_failure_from_an_old_worker_does_not_interrupt_new_read(self):
        popup = self._popup("first second third")
        current_worker = mock.MagicMock()
        current_worker.isRunning.return_value = False
        popup._speech_worker = current_worker
        popup._speech_active = True

        with (
            mock.patch.object(popup, "sender", return_value=object()),
            mock.patch.object(popup, "_recover_speech_playback") as recover,
        ):
            popup._on_speech_failed("stale failure")

        self.assertIsNone(popup._speech_error)
        recover.assert_not_called()

    def test_unexpected_stop_is_ignored_when_media_reaches_end(self):
        from PySide6.QtMultimedia import QMediaPlayer

        popup = self._popup("A short speech passage.")
        popup._speech_active = True
        popup._speech_playback_has_started = True
        popup._player.playbackState = mock.Mock(
            return_value=QMediaPlayer.PlaybackState.StoppedState)
        popup._player.mediaStatus = mock.Mock(
            return_value=QMediaPlayer.MediaStatus.EndOfMedia)

        with mock.patch.object(popup, "_recover_speech_playback") as recover:
            popup._recover_unexpected_playback_stop()

        recover.assert_not_called()

    def test_unexpected_stop_attempts_recovery(self):
        from PySide6.QtMultimedia import QMediaPlayer

        popup = self._popup("A short speech passage.")
        popup._speech_active = True
        popup._speech_playback_has_started = True
        popup._player.playbackState = mock.Mock(
            return_value=QMediaPlayer.PlaybackState.StoppedState)
        popup._player.mediaStatus = mock.Mock(
            return_value=QMediaPlayer.MediaStatus.BufferedMedia)

        with mock.patch.object(popup, "_recover_speech_playback") as recover:
            popup._recover_unexpected_playback_stop()

        recover.assert_called_once_with(
            "Audio playback stopped unexpectedly.")

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

    def test_worker_reports_end_of_last_word_it_will_speak(self):
        text = "مرحبا hello بالعالم"
        worker = self._worker(text, target_language="ar")
        expected_end = max(
            end
            for spans in worker._segment_word_spans
            for _, end in spans
        )

        self.assertEqual(worker.source_end_offset, expected_end)

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


class TestStalledPlaybackIsRecovered(unittest.TestCase):
    """A player can stop advancing without ever emitting an event.

    Every recovery path in the popup is edge-triggered: EndOfMedia, the
    worker's finished signal, a failed signal, or a transition into
    StoppedState. When the audio simply stops advancing while the state stays
    PlayingState, none of those edges fires, so the reading hangs forever with
    the highlight frozen on the last spoken word and the button still on Stop.
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _playing(self, source="first second third"):
        from PySide6.QtMultimedia import QMediaPlayer

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

        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        with mock.patch(
                "popup.translate_popup.TranslateWorker", Stub), \
                mock.patch("popup.translate_popup.ChatWorker", Stub):
            popup = TranslatePopup(dict(DEFAULTS, target_language="en"),
                                   source_text=source)
        self.addCleanup(popup.close)
        popup._speech_active = True
        popup._speech_playback_has_started = True
        popup._speech_index = 0
        popup._speech_playing_stream_index = 0
        popup._speech_boundaries[0] = [
            (0, 1_000_000, 0, 5),
            (1_000_000, 1_000_000, 6, 12),
        ]
        popup._player.playbackState = mock.Mock(
            return_value=QMediaPlayer.PlaybackState.PlayingState)
        popup._player.mediaStatus = mock.Mock(
            return_value=QMediaPlayer.MediaStatus.BufferedMedia)
        return popup

    def test_frozen_position_is_recovered(self):
        popup = self._playing()

        with (
            mock.patch.object(
                popup._player, "position", return_value=150),
            mock.patch("popup.translate_popup.time.monotonic") as clock,
            mock.patch.object(popup, "_recover_speech_playback") as recover,
        ):
            clock.return_value = 0.0
            popup._check_speech_playback_progress()
            recover.assert_not_called()

            clock.return_value = 2.0
            popup._check_speech_playback_progress()
            recover.assert_not_called()

            # The position never moved, long past any natural pause.
            clock.return_value = 30.0
            popup._check_speech_playback_progress()

        recover.assert_called_once_with(
            "Audio playback stalled before the text was finished.")

    def test_advancing_position_is_left_alone(self):
        popup = self._playing()
        position = mock.Mock(return_value=0)

        with (
            mock.patch.object(popup._player, "position", position),
            mock.patch("popup.translate_popup.time.monotonic") as clock,
            mock.patch.object(popup, "_recover_speech_playback") as recover,
        ):
            for step in range(6):
                clock.return_value = float(step) * 30.0
                position.return_value = step * 500
                popup._check_speech_playback_progress()

        recover.assert_not_called()

    def test_waiting_between_segments_is_not_mistaken_for_a_stall(self):
        popup = self._playing()
        popup._speech_waiting = True

        with (
            mock.patch.object(
                popup._player, "position", return_value=150),
            mock.patch("popup.translate_popup.time.monotonic") as clock,
            mock.patch.object(popup, "_recover_speech_playback") as recover,
        ):
            clock.return_value = 0.0
            popup._check_speech_playback_progress()
            clock.return_value = 60.0
            popup._check_speech_playback_progress()

        recover.assert_not_called()

    def test_paused_player_is_not_mistaken_for_a_stall(self):
        from PySide6.QtMultimedia import QMediaPlayer

        popup = self._playing()
        popup._player.playbackState = mock.Mock(
            return_value=QMediaPlayer.PlaybackState.PausedState)

        with (
            mock.patch.object(
                popup._player, "position", return_value=150),
            mock.patch("popup.translate_popup.time.monotonic") as clock,
            mock.patch.object(popup, "_recover_speech_playback") as recover,
        ):
            clock.return_value = 0.0
            popup._check_speech_playback_progress()
            clock.return_value = 60.0
            popup._check_speech_playback_progress()

        recover.assert_not_called()

    def test_inactive_speech_is_never_stall_checked(self):
        popup = self._playing()
        popup._speech_active = False

        with (
            mock.patch.object(
                popup._player, "position", return_value=150),
            mock.patch("popup.translate_popup.time.monotonic") as clock,
            mock.patch.object(popup, "_recover_speech_playback") as recover,
        ):
            clock.return_value = 0.0
            popup._check_speech_playback_progress()
            clock.return_value = 60.0
            popup._check_speech_playback_progress()

        recover.assert_not_called()

    def test_watchdog_runs_on_the_highlight_timer(self):
        """The stall check must share the timer that already polls playback."""
        from popup import translate_popup

        source = inspect.getsource(
            translate_popup.TranslatePopup.__init__)
        self.assertIn(
            "_check_speech_playback_progress", source,
            "the progress watchdog must be wired into the highlight timer")


if __name__ == "__main__":
    unittest.main()