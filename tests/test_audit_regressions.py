"""Offline regressions for settings concurrency, cancellation and complete output."""
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from tests.transport_support import requires_loopback

from config import settings
from translators import chain, opencode_compat


def _edit_settings(path, key, value, barrier):
    settings.SETTINGS_PATH = Path(path)
    snapshot = settings.load()
    snapshot[key] = value
    barrier.wait(timeout=10)
    settings.save(snapshot)


class TestSettingsConcurrency(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "settings.json"
        patcher = mock.patch.object(settings, "SETTINGS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        settings.save(dict(settings.DEFAULTS))

    def test_stale_floating_save_keeps_new_provider(self):
        reader, floating = settings.load(), settings.load()
        settings.add_custom_provider(reader, "Example", "https://example.invalid/v1", "fake", "model")
        settings.save(reader)
        floating["float_pos"] = [12, 34]
        settings.save(floating)
        self.assertEqual(len(settings.load()["custom_providers"]), 1)
        self.assertEqual(settings.load()["float_pos"], [12, 34])

    def test_editable_copies_do_not_mutate_parent_nested_values(self):
        parent = settings.load()
        settings.add_custom_provider(parent, "Original", "https://example.invalid", "fake", "model")
        settings.save(parent)
        edited = settings.copy_settings(parent)
        edited["custom_providers"][0]["name"] = "Unsaved"
        self.assertEqual(parent["custom_providers"][0]["name"], "Original")
        self.assertEqual(edited.baseline["custom_providers"][0]["name"], "Original")

    def test_two_provider_edits_keep_independent_fields_and_new_entries(self):
        first = settings.load()
        provider = settings.add_custom_provider(first, "Old", "https://example.invalid/v1", "fake", "model")
        settings.save(first)
        other = settings.load()
        first["custom_providers"][0]["name"] = "Renamed"
        settings.add_custom_provider(first, "Second", "https://example.invalid/v1", "fake", "model")
        settings.save(first)
        other["custom_providers"][0]["model"] = "new-model"
        settings.save(other)
        loaded = settings.load()["custom_providers"]
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["name"], "Renamed")
        self.assertEqual(loaded[0]["model"], "new-model")

    def test_simultaneous_process_saves_merge(self):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        processes = [context.Process(target=_edit_settings, args=(
            str(self.path), key, value, barrier)) for key, value in
            [("float_pos", [10, 20]), ("font_size", 23)]]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=15)
                self.assertEqual(process.exitcode, 0)
            result = settings.load()
            self.assertEqual(result["float_pos"], [10, 20])
            self.assertEqual(result["font_size"], 23)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)

    def test_stale_provider_edit_does_not_resurrect_a_removed_provider(self):
        first = settings.load()
        provider = settings.add_custom_provider(first, "Example", "https://example.invalid", "fake", "model")
        settings.save(first)
        stale = settings.load()
        settings.remove_custom_provider(first, provider["id"])
        settings.save(first)
        stale["custom_providers"][0]["model"] = "new-model"
        settings.save(stale)
        self.assertEqual(settings.load()["custom_providers"], [])


class TestTranslationIntegrity(unittest.TestCase):
    def test_every_chunk_obeys_limit_including_unspaced_text(self):
        for source in [("word " * 1000) + ". Short end.", "字" * 10000, "x" * 10000]:
            with self.subTest(source=source[:10]):
                chunks = chain.chunk_text(source, limit=100)
                self.assertTrue(chunks)
                self.assertTrue(all(0 < len(chunk) <= 100 for chunk in chunks))
        with self.assertRaises(ValueError):
            chain.chunk_text("source", limit=0)

    def test_latin_target_still_checks_language(self):
        with mock.patch.object(chain, "_detect_translation_language", return_value="en"):
            self.assertTrue(chain._needs_language_retry(
                {"target_language": "fr", "source_language": "de"}, "English answer " * 10))

    def _response(self, finish=None, done=False):
        response = mock.Mock(status_code=200, headers={})
        events = [{"choices": [{"delta": {"content": "partial text"}, "finish_reason": finish}]}]
        lines = [("data: " + json.dumps(event)).encode() for event in events]
        if done:
            lines.append(b"data: [DONE]")
        response.iter_lines.return_value = lines
        return response

    def test_incomplete_stream_is_rejected_and_closed(self):
        response = self._response()
        with mock.patch.object(opencode_compat, "_post_stream", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "before completion"):
                list(opencode_compat.stream_translate("text", "https://example.invalid", "fake", "model"))
        response.close.assert_called_once()

    def test_stop_or_done_completes_a_stream(self):
        for finish, done in [("stop", False), (None, True)]:
            with mock.patch.object(opencode_compat, "_post_stream", return_value=self._response(finish, done)):
                self.assertEqual(list(opencode_compat.stream_translate(
                    "text", "https://example.invalid", "fake", "model")), ["partial text"])

    def test_auth_failure_never_retries_stream_options(self):
        response = mock.Mock(status_code=401, text="invalid stream_options or API key")
        with mock.patch.object(opencode_compat.requests, "post", return_value=response) as post:
            self.assertIs(opencode_compat._post_stream({"stream_options": {}}, "https://example.invalid", "fake", 1), response)
        self.assertEqual(post.call_count, 1)

    def test_repeated_usage_frames_are_counted_once(self):
        response = self._response("stop")
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        response.iter_lines.return_value = [
            ('data: ' + json.dumps({"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}], "usage": usage})).encode(),
            ('data: ' + json.dumps({"choices": [], "usage": usage})).encode(),
        ]
        report = {}
        with mock.patch.object(opencode_compat, "_post_stream", return_value=response):
            list(opencode_compat.stream_translate("text", "https://example.invalid", "fake", "model", report=report))
        self.assertEqual(report["tokens"]["total"], 15)

    def test_disabled_fallback_sends_nothing_to_other_services(self):
        with mock.patch.object(chain.fallback, "gtx_translate") as google, mock.patch.object(chain.mymemory, "translate") as alternative:
            with self.assertRaisesRegex(RuntimeError, "fallback is disabled"):
                chain._translate_free_fallback("private text", {"allow_translation_fallback": False}, "model", "failed")
        google.assert_not_called()
        alternative.assert_not_called()

    def test_request_prompt_does_not_repeat_the_system_rules(self):
        from translators.prompt import request_user_prompt, system_instructions
        user = request_user_prompt("Hello.")
        self.assertLess(len(user), 300)
        self.assertNotIn("MANDATORY OUTPUT-LANGUAGE RULE", user)
        self.assertIn("Hello.", user)
        self.assertIn("MANDATORY OUTPUT-LANGUAGE RULE", system_instructions())

    def test_glossary_sends_only_matching_terms_for_target_language(self):
        config = {"target_language": "ar", "translation_glossaries": {
            "ar": "cadence = قفلة\ntriad = ثلاثي", "fr": "cadence = cadence parfaite"}}
        instructions = chain._translation_instructions(config, source_text="The cadence ends here.")
        self.assertIn("قفلة", instructions)
        self.assertNotIn("ثلاثي", instructions)
        self.assertNotIn("cadence parfaite", instructions)


class TestHttpCancellation(unittest.TestCase):
    def test_cancellation_stops_a_single_cli_process(self):
        import sys
        from translators import http_client
        started, cancelled = threading.Event(), threading.Event()
        processes, errors = [], []
        original = http_client.subprocess.Popen

        def create(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            started.set()
            return process

        def run():
            try:
                with http_client.cancellation_scope(cancelled.is_set):
                    http_client.run_process(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        capture_output=True, timeout=5,
                        creationflags=(http_client.subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
            except http_client.RequestCancelled:
                pass
            except Exception as error:
                errors.append(error)

        with mock.patch.object(http_client.subprocess, "Popen", side_effect=create):
            thread = threading.Thread(target=run)
            try:
                thread.start()
                self.assertTrue(started.wait(2))
                cancelled.set()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(len(processes), 1)
                self.assertIsNotNone(processes[0].poll())
            finally:
                cancelled.set()
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)
                thread.join(timeout=5)

    def test_cancel_aborts_adapter_socket_during_headers_or_body(self):
        from translators import http_client
        for body in (False, True):
            with self.subTest(body=body):
                entered, aborted, cancelled = threading.Event(), threading.Event(), threading.Event()
                calls = []

                class Socket:
                    def shutdown(self, how):
                        aborted.set()
                    def close(self):
                        pass

                def send(session, method, url, **kwargs):
                    calls.append(url)
                    pool = session.get_adapter(url).poolmanager.connection_from_url(url)
                    connection = pool._new_conn()
                    connection.sock = Socket()
                    response = mock.Mock()
                    def wait():
                        entered.set()
                        if not aborted.wait(2):
                            raise AssertionError("socket was not aborted")
                        raise http_client.RequestCancelled("cancelled")
                    if not body:
                        wait()
                    response.iter_lines.side_effect = wait
                    return response

                def run():
                    try:
                        with http_client.cancellation_scope(cancelled.is_set):
                            response = http_client.post("http://127.0.0.1:1234", stream=True)
                            list(response.iter_lines())
                    except http_client.RequestCancelled:
                        pass

                with mock.patch.object(http_client._requests.Session, "request", new=send):
                    worker = threading.Thread(target=run)
                    worker.start()
                    self.assertTrue(entered.wait(1))
                    cancelled.set()
                    worker.join(timeout=1)
                    self.assertFalse(worker.is_alive())
                self.assertTrue(aborted.is_set())
                self.assertEqual(len(calls), 1)

    @requires_loopback
    def test_cancel_interrupts_headers_and_body_without_a_second_request(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from translators import http_client
        for send_headers in (False, True):
            with self.subTest(send_headers=send_headers):
                entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
                calls, errors = [], []

                class Handler(BaseHTTPRequestHandler):
                    def do_POST(self):
                        calls.append(1)
                        self.rfile.read(int(self.headers.get("Content-Length", 0)))
                        if send_headers:
                            self.send_response(200)
                            self.end_headers()
                            self.wfile.write(b"data: start\n\n")
                            self.wfile.flush()
                        entered.set()
                        release.wait(3)

                    def log_message(self, *args):
                        pass

                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                server.daemon_threads = True
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()

                def run():
                    try:
                        with http_client.cancellation_scope(cancelled.is_set):
                            response = http_client.post(f"http://127.0.0.1:{server.server_port}", json={}, stream=True, timeout=5)
                            try:
                                list(response.iter_lines())
                            finally:
                                response.close()
                    except Exception as error:
                        errors.append(error)

                worker = threading.Thread(target=run)
                try:
                    worker.start()
                    if not entered.wait(2):
                        if any("10013" in str(error) for error in errors):
                            self.skipTest("This Windows sandbox blocks loopback connections")
                        self.fail(repr(errors))
                    cancelled.set()
                    worker.join(timeout=1)
                    self.assertFalse(worker.is_alive(), "cancel must interrupt a blocked socket")
                    self.assertEqual(len(calls), 1)
                finally:
                    release.set()
                    worker.join(timeout=5)
                    server.shutdown()
                    server.server_close()
                    server_thread.join(timeout=2)

    def test_cancelled_chat_never_calls_provider(self):
        from popup.chat_worker import ChatWorker
        worker = ChatWorker([{"role": "user", "content": "hello"}], {}, "", "")
        with mock.patch.object(worker, "isInterruptionRequested", return_value=True), mock.patch("translators.chat.reply_stream") as provider:
            worker.run()
        provider.assert_not_called()


class TestPdfProcess(unittest.TestCase):
    def test_render_text_and_unsaved_changes_use_an_independent_process(self):
        import pymupdf
        from reader.pdf_process import PdfProcess
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.pdf"
            document = pymupdf.open()
            document.new_page().insert_text((72, 72), "first page")
            document.new_page().insert_text((72, 72), "second page")
            document.save(path)
            worker = PdfProcess(path)
            try:
                self.assertIn("first page", worker.request("text", 0))
                self.assertNotEqual(worker._process.pid, os.getpid())
                width, height, stride, data = worker.request("render", 0, 0.5)
                self.assertEqual(len(data), stride * height)
                self.assertGreater(width, 0)
                document.delete_page(0)
                worker.set_source(path, document.tobytes())
                self.assertIn("second page", worker.request("text", 0))
                moved = path.with_name("renamed.pdf")
                path.rename(moved)
            finally:
                worker.close()
                document.close()
            self.assertFalse(worker._process.is_alive())


class TestShutdown(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_waits_for_owned_chat_worker(self):
        from PySide6.QtCore import QThread
        from main import MainWindow
        release = threading.Event()
        class Worker(QThread):
            def run(self):
                release.wait(3)
        with mock.patch.object(MainWindow, "_warm_ocr"), mock.patch.object(MainWindow, "_restore_session"), mock.patch.object(MainWindow, "_save_session_soon"):
            window = MainWindow()
            worker = Worker(window)
            try:
                window.show()
                worker.start()
                self.assertFalse(window.close())
                self.assertTrue(worker.isInterruptionRequested())
                release.set()
                self.assertTrue(worker.wait(2000))
                self.assertTrue(window.close())
            finally:
                release.set()
                worker.wait(4000)
                window.close()
                window.deleteLater()
                self.app.processEvents()

    def test_hidden_pending_popups_survive_until_workers_finish(self):
        from popup.translate_popup import retain_live_popups
        pending = mock.Mock(_close_pending=True)
        pending.isVisible.return_value = False
        closed = mock.Mock(_close_pending=False)
        closed.isVisible.return_value = False
        deleted = mock.Mock()
        deleted.isVisible.side_effect = RuntimeError("deleted Qt object")
        self.assertEqual(retain_live_popups([pending, closed, deleted]), [pending])

    def test_settings_round_trip_separate_language_glossaries(self):
        from ui.settings_dialog import SettingsDialog
        dialog = SettingsDialog(dict(settings.DEFAULTS))
        try:
            dialog.glossary_text.setPlainText("cadence = قفلة")
            dialog.glossary_language.setCurrentIndex(dialog.glossary_language.findData("fr"))
            dialog.glossary_text.setPlainText("cadence = cadence parfaite")
            values = dialog.values()
            self.assertEqual(values["translation_glossaries"]["ar"], "cadence = قفلة")
            self.assertEqual(values["translation_glossaries"]["fr"], "cadence = cadence parfaite")
        finally:
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()

    def test_floating_quit_waits_for_a_popup_that_rejected_close(self):
        from types import SimpleNamespace
        from main_02 import FloatButton
        popup = mock.Mock(_close_pending=True)
        popup.isVisible.return_value = False
        owner = SimpleNamespace(popups=[popup], _quit=mock.Mock())
        app = mock.Mock()
        with mock.patch("main_02.QApplication.instance", return_value=app), mock.patch("main_02.QTimer.singleShot") as later:
            FloatButton._quit(owner)
        app.quit.assert_not_called()
        self.assertTrue(later.called)
