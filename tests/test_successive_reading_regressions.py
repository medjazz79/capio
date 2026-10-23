"""Natural EOF, next-page preparation, launch paths and volume regressions."""
import asyncio
import os
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class SuccessiveReadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def popup(self):
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup
        with mock.patch.object(TranslatePopup, "_start_translation_worker"):
            popup = TranslatePopup(dict(DEFAULTS, target_language="en"),
                                   source_text="first second third")
        popup._speech_prefetch_timer.stop()
        self.addCleanup(popup.deleteLater)
        self.addCleanup(popup._speech_highlight_timer.stop)
        self.addCleanup(popup._cancel_speech_prefetch)
        return popup

    def host(self, popup, pages=3):
        from main import MainWindow
        book = mock.Mock()
        book.page_count.return_value = pages
        host = SimpleNamespace(
            settings={"prefetch_next_page_translation": True},
            _automatic_page_reading_book=book,
            _automatic_page_reading_popup=popup,
            _automatic_page_reading_page=0,
            _automatic_page_reading_at_end=False,
            _automatic_page_waiting_for_speech=True,
            _automatic_page_waiting_for_prefetch=None,
            _automatic_page_prefetch_scheduled=None,
            _automatic_page_prefetch_timer=mock.Mock(),
            _automatic_page_reading_time_timer=mock.Mock(),
            _start_automatic_page_prefetch=mock.Mock(),
            _translate_automatic_page=mock.Mock(),
            _stop_automatic_page_reading=mock.Mock(),
            _flash_info=mock.Mock(),
        )
        host._automatic_page_prefetch_timer.isActive.return_value = False
        host._on_automatic_page_speech_finished = lambda error="": (
            MainWindow._on_automatic_page_speech_finished(host, popup, error))
        return host

    def test_natural_eof_ignores_reset_position_and_late_word_timestamps(self):
        from PySide6.QtMultimedia import QMediaPlayer
        popup = self.popup()
        popup._speech_active = True
        popup._speech_playing_continuous_stream = True
        popup._speech_synthesis_completed = True
        popup._speech_boundaries = {0: [(600_000_000, 10_000_000, 6, 12)]}
        popup._speech_expected_end_offset = 18  # Missing final metadata is OK.
        with (mock.patch.object(popup._player, "position", return_value=0),
              mock.patch.object(popup, "_recover_speech_playback") as recover):
            finished = mock.Mock()
            popup.speechFinished.connect(finished)
            popup._on_media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            popup._on_media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
        finished.assert_called_once_with("")
        recover.assert_not_called()
        self.assertFalse(popup._speech_active)

    def test_eof_before_finished_signal_uses_completed_worker(self):
        popup = self.popup()
        popup._speech_active = True
        worker = mock.Mock(synthesis_completed=True, disconnected_segments=set())
        worker.isRunning.return_value = False
        worker.isInterruptionRequested.return_value = False
        popup._speech_worker = worker
        with (mock.patch.object(popup, "_finish_speech") as finish,
              mock.patch.object(popup, "_recover_speech_playback") as recover):
            popup._resolve_continuous_stream_end()
        finish.assert_called_once_with()
        recover.assert_not_called()
        popup._speech_worker = None

    def test_incomplete_or_disconnected_stream_still_recovers(self):
        for complete, disconnected in ((False, False), (True, True)):
            with self.subTest(complete=complete, disconnected=disconnected):
                popup = self.popup()
                popup._speech_active = True
                popup._speech_synthesis_completed = complete
                popup._speech_stream_disconnected = disconnected
                with (mock.patch.object(popup, "_finish_speech") as finish,
                      mock.patch.object(popup, "_recover_speech_playback") as recover):
                    popup._resolve_continuous_stream_end()
                recover.assert_called_once()
                finish.assert_not_called()

    def test_recovery_keeps_last_position_when_backend_resets_to_zero(self):
        popup = self.popup()
        popup._speech_active = True
        popup._speech_paths = {0: "completed.mp3"}
        popup._speech_playing_stream_index = 0
        popup._on_speech_position_changed(4300)
        popup._on_speech_position_changed(0)
        with (mock.patch.object(popup._player, "position", return_value=0),
              mock.patch.object(popup, "_play_speech_segment") as play):
            popup._recover_speech_playback("Disconnected")
        play.assert_called_once_with("completed.mp3", 4300)

    def test_page_start_explicitly_reads_first_word_and_not_chat(self):
        from main import MainWindow
        popup = mock.Mock(navigation_page=0, _translation_text="new page",
                          _speech_available=True, _speech_start_offset=500,
                          _speech_text_override="old chat answer")
        host = self.host(popup)
        MainWindow._on_automatic_page_translation_ready(host, popup)
        popup._request_speech.assert_called_once_with(0, trigger="successive-reading")
        popup._toggle_speech.assert_not_called()
        self.assertIsNone(popup._speech_text_override)
        host._automatic_page_prefetch_timer.start.assert_called_once_with(350)

    def test_prefetch_does_not_wait_for_current_synthesis(self):
        from main import MainWindow
        popup = mock.Mock()
        popup._speech_worker.isRunning.return_value = True
        host = self.host(popup)
        host._automatic_page_prefetch_scheduled = (
            host._automatic_page_reading_book, popup, 0)
        MainWindow._start_scheduled_automatic_page_prefetch(host)
        host._start_automatic_page_prefetch.assert_called_once_with(
            host._automatic_page_reading_book, popup, 0)
        self.assertIsNone(host._automatic_page_prefetch_scheduled)

    def test_short_page_advances_once_without_prefetch_delay(self):
        popup = mock.Mock()
        host = self.host(popup)
        book = host._automatic_page_reading_book
        host._automatic_page_prefetch_timer.isActive.return_value = True
        host._automatic_page_prefetch_scheduled = (book, popup, 0)
        host._on_automatic_page_speech_finished()
        host._on_automatic_page_speech_finished()
        self.app.processEvents()
        host._translate_automatic_page.assert_called_once_with(book, popup, 1)
        host._automatic_page_prefetch_timer.stop.assert_called_once()

    def test_stop_or_manual_page_change_invalidates_queued_advance(self):
        for action in ("stop", "navigate"):
            with self.subTest(action=action):
                popup = mock.Mock()
                host = self.host(popup)
                host._on_automatic_page_speech_finished()
                if action == "stop":
                    host._automatic_page_reading_popup = None
                else:
                    host._automatic_page_reading_page = 2
                self.app.processEvents()
                host._translate_automatic_page.assert_not_called()

    def test_last_page_stops_and_errors_do_not_advance(self):
        popup = mock.Mock()
        host = self.host(popup, pages=1)
        host._on_automatic_page_speech_finished()
        self.app.processEvents()
        self.assertTrue(host._automatic_page_reading_at_end)
        host._translate_automatic_page.assert_not_called()
        host = self.host(popup)
        host._on_automatic_page_speech_finished("network error")
        host._stop_automatic_page_reading.assert_called_once()
        host._translate_automatic_page.assert_not_called()

    def test_page_transition_preserves_inflight_prefetch(self):
        popup = self.popup()
        worker = mock.Mock()
        worker.isRunning.return_value = True
        popup._speech_prefetch_worker = worker
        with mock.patch.object(popup, "_on_done"):
            popup.set_pretranslated_content("new translated page", "Test")
        self.assertIs(popup._speech_prefetch_worker, worker)
        worker.requestInterruption.assert_not_called()

    def prepare_wait(self, popup):
        text = "new translated page"
        worker = mock.Mock()
        worker.isRunning.return_value = True
        worker.isInterruptionRequested.return_value = False
        popup._speech_prefetch_worker = worker
        popup._speech_prefetch_key = popup._speech_prefetch_request_key(
            text, popup._speech_worker_kwargs(text, 0))
        popup._speech_active = True
        popup._speech_allow_prefetch_wait = True
        return worker, text

    def test_matching_prefetch_has_bounded_wait_then_starts_from_zero(self):
        popup = self.popup()
        worker, text = self.prepare_wait(popup)
        with mock.patch("popup.translate_popup.QTimer.singleShot") as single_shot:
            self.assertTrue(popup._wait_for_page_speech_prefetch(text, 0))
        self.assertEqual(single_shot.call_args.args[0], 1500)
        callback = single_shot.call_args.args[-1]
        with mock.patch.object(popup, "_begin_speech") as begin:
            callback()
            callback()
        begin.assert_called_once_with(0)
        worker.requestInterruption.assert_called_once()

    def test_stop_invalidates_prefetch_wait_callback(self):
        popup = self.popup()
        _, text = self.prepare_wait(popup)
        with mock.patch("popup.translate_popup.QTimer.singleShot") as single_shot:
            self.assertTrue(popup._wait_for_page_speech_prefetch(text, 0))
        popup._toggle_speech()
        with mock.patch.object(popup, "_begin_speech") as begin:
            single_shot.call_args.args[-1]()
        begin.assert_not_called()

    def test_changed_request_or_word_click_does_not_wait_for_prefetch(self):
        for text, offset in (("other text", 0), ("new translated page", 4)):
            popup = self.popup()
            self.prepare_wait(popup)
            self.assertFalse(popup._wait_for_page_speech_prefetch(text, offset))

    def test_completed_prefetch_resumes_waiting_page_once(self):
        from popup.speech_worker import SpeechWorker
        popup = self.popup()
        worker = SpeechWorker("new translated page", stream_audio=False)
        popup._speech_prefetch_worker = worker
        popup._speech_prefetch_workers.append(worker)
        popup._speech_prefetch_wait_worker = worker
        popup._speech_active = True
        with (mock.patch.object(popup, "sender", return_value=worker),
              mock.patch.object(popup, "_begin_speech") as begin):
            popup._on_speech_prefetch_finished()
        begin.assert_called_once_with(0)
        self.assertIsNone(popup._speech_prefetch_wait_worker)

    def test_real_qt_end_of_media_advances_three_pages_without_replay(self):
        """Silent WAVs exercise the actual Windows media backend and Qt events."""
        from main import MainWindow
        from PySide6.QtCore import QUrl
        popup = self.popup()
        host = self.host(popup, pages=3)
        pages_started = []
        finished = []
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "silent.wav"
            with wave.open(str(audio), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(b"\x00\x00" * 4000)

            def start_page(book, current_popup, page):
                host._automatic_page_reading_page = page
                host._automatic_page_waiting_for_speech = True
                pages_started.append(page)
                popup._speech_active = True
                popup._speech_synthesis_completed = True
                popup._speech_stream_disconnected = False
                popup._speech_boundaries = {0: [(9_000_000_000, 1, 0, 5)]}
                popup._speech_playing_continuous_stream = True
                popup._play_speech_source(QUrl.fromLocalFile(str(audio)))

            def on_finished(error):
                finished.append(error)
                MainWindow._on_automatic_page_speech_finished(host, popup, error)

            host._translate_automatic_page = start_page
            popup.speechFinished.connect(on_finished)
            with mock.patch.object(popup, "_recover_speech_playback") as recover:
                start_page(host._automatic_page_reading_book, popup, 0)
                deadline = time.monotonic() + 10
                while not host._automatic_page_reading_at_end and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                popup._player.stop()
                popup._player.setSource(QUrl())
                self.assertTrue(host._automatic_page_reading_at_end)
                self.assertEqual(pages_started, [0, 1, 2])
                self.assertEqual(finished, ["", "", ""])
                recover.assert_not_called()


class SynthesisCompletionTests(unittest.TestCase):
    def test_completion_only_after_successful_non_interrupted_synthesis(self):
        from popup.speech_worker import SpeechWorker
        for failure in (None, RuntimeError("failed"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                worker = SpeechWorker("test text", fast=False, stream_audio=False)
                with (mock.patch.object(worker, "_synthesize", new=mock.AsyncMock(side_effect=failure)),
                      mock.patch("popup.speech_worker.speech_available", return_value=True)):
                    worker.run()
                self.assertEqual(worker.synthesis_completed, failure is None)


class FloatingPathTests(unittest.TestCase):
    def test_both_entrypoints_use_the_same_current_modules_and_root(self):
        import main
        import main_02
        import popup.speech_worker as speech_worker
        from popup.translate_popup import TranslatePopup
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(main.APP_DIR, root)
        self.assertEqual(main_02.APP_DIR, root)
        self.assertIs(main.TranslatePopup, main_02.TranslatePopup)
        self.assertIs(main_02.TranslatePopup, TranslatePopup)
        self.assertEqual(Path(speech_worker.__file__).resolve().parents[1], root)
        self.assertEqual(main_02.APP_VERSION, main.APP_VERSION)

    def test_float_opens_reader_from_same_project_with_local_python(self):
        import main_02
        from main_02 import FloatButton
        with mock.patch("subprocess.Popen") as launch:
            FloatButton._open_in_reader(SimpleNamespace(), ["example.pdf"])
        command = launch.call_args.args[0]
        self.assertEqual(Path(command[0]), main_02.VENV_PYTHONW)
        self.assertEqual(Path(command[1]), main_02.APP_DIR / "main.py")
        self.assertEqual(launch.call_args.kwargs["cwd"], str(main_02.APP_DIR))


if __name__ == "__main__":
    unittest.main()
