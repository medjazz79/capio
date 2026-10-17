"""Regression tests for the reader / OCR defects fixed in this pass.

Every test here was written to FAIL against the unfixed code first; the module
docstring of each test records the observed failure. Run with:

    .venv\\Scripts\\python.exe -m unittest tests.test_reader_fixes -v

Qt needs an offscreen platform before any Qt import.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_APP = None


def _app():
    global _APP
    from PySide6.QtWidgets import QApplication

    _APP = QApplication.instance() or QApplication([])
    return _APP


def _pump(app, predicate, timeout=5.0):
    """Spin the event loop until `predicate` is true (or the timeout expires)."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return predicate()


class TestThumbnailWorkerConsoleVisibility(unittest.TestCase):
    def test_windows_thumbnail_workers_use_pythonw_and_restore_spawn_state(self):
        from reader import thumbnails

        executable = r"C:\Python313\python.exe"
        pythonw = os.fsencode(r"C:\Python313\pythonw.exe")
        original_launcher = os.environ.get("__PYVENV_LAUNCHER__")
        starts = []

        class FakeProcess:
            def start(self):
                starts.append((
                    multiprocessing_spawn_get_executable(),
                    os.environ.get("__PYVENV_LAUNCHER__"),
                ))

        def multiprocessing_spawn_get_executable():
            return set_executable.call_args.args[0]

        with (
            mock.patch.object(thumbnails.os, "name", "nt"),
            mock.patch.object(thumbnails.sys, "frozen", False, create=True),
            mock.patch.object(thumbnails.sys, "_base_executable", executable),
            mock.patch.object(
                thumbnails.sys, "executable",
                r"C:\app\.venv\Scripts\python.exe"),
            mock.patch.object(thumbnails.sys, "prefix", r"C:\app\.venv"),
            mock.patch.object(thumbnails.sys, "base_prefix", r"C:\Python313"),
            mock.patch.object(thumbnails.Path, "is_file", return_value=True),
            mock.patch.object(
                thumbnails.multiprocessing.spawn,
                "get_executable",
                return_value=b"previous-python.exe",
            ),
            mock.patch.object(
                thumbnails.multiprocessing, "set_executable") as set_executable,
            mock.patch.dict(
                os.environ, {"__PYVENV_LAUNCHER__": "previous-launcher"}),
        ):
            thumbnails._start_thumbnail_processes([FakeProcess()])

        self.assertEqual(
            starts, [(pythonw, r"C:\app\.venv\Scripts\python.exe")])
        self.assertEqual(
            [call.args[0] for call in set_executable.call_args_list],
            [pythonw, b"previous-python.exe"],
        )
        self.assertEqual(
            os.environ.get("__PYVENV_LAUNCHER__"), original_launcher,
            "the previous virtual-environment launcher must be restored",
        )


# ---------------------------------------------------------------------------
# Defect 1 + 4: retired thumbnail workers must outlive the panel, and
# clear_document() must not block the GUI thread.
# ---------------------------------------------------------------------------

class _FakeFinishedSignal:
    """Stands in for QThread.finished."""

    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def fire(self):
        for callback in list(self._callbacks):
            callback()


class _FakeWorker:
    """A ThumbWorker stand-in that records what the panel asks of it."""

    def __init__(self, running=True, wait_blocks=False):
        self.stopped = False
        self.waits = []
        self.running = running
        self.wait_blocks = wait_blocks
        self.finished = _FakeFinishedSignal()

    def isRunning(self):
        return self.running

    def stop(self):
        self.stopped = True

    def wait(self, ms):
        self.waits.append(ms)
        if self.wait_blocks:
            # Stand-in for ThumbWorker.run's teardown (process.join(timeout=1.0)
            # then terminate + join(timeout=1.0) per process), which routinely
            # overruns whatever timeout the panel passes.
            time.sleep(ms / 1000.0)
        return not self.running

    def emit_finished(self):
        self.running = False
        self.finished.fire()


class TestRetiredThumbWorkers(unittest.TestCase):
    def setUp(self):
        self.app = _app()

    def test_retired_worker_is_parked_outside_the_panel(self):
        """A worker that ignores stop() must be kept by module-level storage.

        Observed failure: `AttributeError: module 'reader.thumbnails' has no
        attribute '_RETIRED_WORKERS'`. The panel parked the thread on
        `self._retired`, an attribute of a widget that main.py deletes with
        `deleteLater()`, so the still-running QThread was collected and the
        process aborted with "QThread: Destroyed while thread is still running".
        """
        from reader import thumbnails

        panel = thumbnails.ThumbPanel()
        try:
            worker = _FakeWorker(wait_blocks=False)
            panel._worker = worker
            panel.clear_document()

            self.assertTrue(
                hasattr(thumbnails, "_RETIRED_WORKERS"),
                "retired workers must be parked in module-level storage that "
                "outlives the ThumbPanel",
            )
            self.assertIn(
                worker, thumbnails._RETIRED_WORKERS,
                "a still-running worker was not parked outside the panel",
            )
        finally:
            panel._worker = None
            panel.clear_document()

    def test_retired_registry_never_grows_unbounded(self):
        """Entries are reaped once the worker really finished.

        Observed failure: the registry (module-level storage introduced by the
        fix) kept every worker ever retired, so a long session leaked a
        reference per thumbnail batch.
        """
        from reader import thumbnails

        panel = thumbnails.ThumbPanel()
        try:
            live = _FakeWorker(wait_blocks=False)
            dead = _FakeWorker(wait_blocks=False)
            dead.running = False
            panel._worker = live
            panel.clear_document()
            panel._retire_worker(dead)
            # A finished worker is not retained at all; a live one is.
            self.assertIn(live, thumbnails._RETIRED_WORKERS)
            self.assertNotIn(dead, thumbnails._RETIRED_WORKERS)

            live.emit_finished()
            panel._reap_retired_workers()
            self.assertNotIn(
                live, thumbnails._RETIRED_WORKERS,
                "a finished worker stayed in the retired registry",
            )
        finally:
            panel._worker = None
            panel.clear_document()

    def test_close_event_stops_and_joins_retired_workers(self):
        """closeEvent must give workers a chance to stop before teardown.

        Observed failure: `AttributeError: 'ThumbPanel' object has no attribute
        'closeEvent'` — the panel never told retired workers to stop, so a
        close during a thumbnail batch left live QThreads behind.
        """
        from reader import thumbnails

        panel = thumbnails.ThumbPanel()
        worker = _FakeWorker(wait_blocks=False)
        panel._worker = worker
        panel.clear_document()
        try:
            self.assertIn(worker, thumbnails._RETIRED_WORKERS)
            panel.close()
            self.assertTrue(worker.stopped)
            self.assertTrue(
                worker.waits,
                "closeEvent must join retired workers, not just signal them",
            )
        finally:
            panel._worker = None
            thumbnails._RETIRED_WORKERS.clear()
            panel.close()

    def test_clear_document_does_not_block_the_gui_thread(self):
        """clear_document() must return immediately.

        Observed failure: `clear_document` blocked for 2.05s — `_retire_worker`
        called `worker.wait(2000)`, and ThumbWorker.run's teardown
        (process.join(timeout=1.0) then terminate + join(timeout=1.0) per
        process) routinely overruns that.
        """
        from reader import thumbnails

        panel = thumbnails.ThumbPanel()
        try:
            worker = _FakeWorker(running=True, wait_blocks=True)
            panel._worker = worker
            started = time.perf_counter()
            panel.clear_document()
            elapsed = time.perf_counter() - started
            self.assertLess(
                elapsed, 0.5,
                "clear_document() blocked the GUI thread for %.2fs" % elapsed,
            )
            self.assertTrue(
                worker.stopped,
                "the worker must still be asked to stop",
            )
        finally:
            panel._worker = None
            thumbnails._RETIRED_WORKERS.clear()
            panel.clear_document()


# ---------------------------------------------------------------------------
# Defect 2: the page raster must not run on the GUI thread.
# ---------------------------------------------------------------------------

class TestPageRasterOffGuiThread(unittest.TestCase):
    def setUp(self):
        self.app = _app()

    def _reader(self, render, pages=3, size=(200.0, 300.0)):
        from reader.continuous import ContinuousReader

        reader = ContinuousReader(render)
        reader.resize(400, 600)
        reader.show()
        reader.set_document([size] * pages, render=True)
        self.addCleanup(reader.close)
        return reader

    def test_raster_runs_off_the_gui_thread(self):
        """The raster must not run on the thread that drives the QTimer pump.

        Observed failure: `AssertionError: [<Thread(...)>] unexpectedly
        contains <Thread(...)>` — `_render_page_index` called
        `self._render_fn(...)` inline from `_ensure_visible`/`_pump_one`, both
        on the GUI thread (23 ms at 100% zoom, 130 ms at 1000%).
        """
        from PySide6.QtGui import QImage

        gui = threading.current_thread()
        threads = []

        def render(index, zoom):
            threads.append(threading.current_thread())
            return QImage(40, 60, QImage.Format.Format_RGB888)

        reader = self._reader(render)
        self.assertTrue(
            _pump(self.app, lambda: bool(reader._rendered)),
            "no page was rendered",
        )
        self.assertTrue(threads, "the render function was never called")
        self.assertNotIn(
            gui, threads,
            "the raster ran on the GUI thread: %r" % (threads,),
        )

    def test_async_render_still_fills_labels_and_bookkeeping(self):
        """Delivering the QImage by signal must preserve the visible result."""
        from PySide6.QtGui import QImage, QColor

        def render(index, zoom):
            img = QImage(40, 60, QImage.Format.Format_RGB888)
            img.fill(QColor(10, 20, 30))
            return img

        reader = self._reader(render)
        self.assertTrue(_pump(self.app, lambda: bool(reader._rendered)))
        page = sorted(reader._rendered)[0]
        label = reader._labels[page]
        self.assertFalse(
            label.pixmap().isNull(),
            "the label was left without a pixmap after an async render",
        )
        # QPixmap must be built on the GUI thread; check the visible size.
        self.assertEqual(
            (label.pixmap().width(), label.pixmap().height()), (40, 60))
        self.assertNotIn(page, reader._failed)

    def test_refresh_clears_page_rasters_without_moving_the_view(self):
            from unittest import mock
            from PySide6.QtGui import QPixmap

            reader = self._reader(lambda *_: None)
            reader.scroll_to_page(1)
            scroll_position = reader.verticalScrollBar().value()
            reader._rendered.add(0)
            reader._failed.add(1)
            reader._labels[0].setPixmap(QPixmap(20, 20))
            old_generation = reader._generation

            with mock.patch.object(reader, "_ensure_visible") as ensure_visible:
                reader.refresh_pages()

            self.assertEqual(reader._generation, old_generation + 1)
            self.assertFalse(reader._rendered)
            self.assertFalse(reader._failed)
            self.assertTrue(reader._labels[0].pixmap().isNull())
            self.assertEqual(reader.verticalScrollBar().value(), scroll_position)
            ensure_visible.assert_called_once_with()

    def test_async_failure_marks_the_page_and_is_not_retried(self):
        """A raising render_fn must still black-list the page, exactly once."""
        calls = []

        def render(index, zoom):
            calls.append(index)
            raise RuntimeError("corrupt page")

        reader = self._reader(render, pages=3)
        self.assertTrue(
            _pump(self.app, lambda: bool(reader._failed)),
            "the failing page was never marked failed",
        )
        first = len(calls)
        self.assertGreater(first, 0)
        for _ in range(8):
            reader._ensure_visible()
            self.app.processEvents()
        self.assertEqual(
            len(calls), first,
            "a failing page was re-rendered %d times" % (len(calls) - first),
        )

    def test_initial_render_complete_still_fires_once(self):
        """`initialRenderComplete` drives thumbnail rendering in BookView."""
        batches = []

        from PySide6.QtGui import QImage

        def render(index, zoom):
            return QImage(40, 60, QImage.Format.Format_RGB888)

        reader = self._reader(render, pages=4)
        reader.initialRenderComplete.connect(
            lambda: batches.append(len(reader._rendered)))
        reader._ensure_visible()
        self.assertTrue(
            _pump(self.app, lambda: bool(batches)),
            "initialRenderComplete never fired for an async render",
        )
        _pump(self.app, lambda: not reader._queue and not reader._inflight)
        self.app.processEvents()
        self.assertEqual(len(batches), 1, "initialRenderComplete fired twice")
        self.assertGreater(batches[0], 0)

    def test_eviction_still_releases_distant_pages(self):
        """Async delivery must not defeat `_evict_outside`."""
        from PySide6.QtGui import QImage

        reader = self._reader(
            lambda _i, _z: QImage(60, 60, QImage.Format.Format_RGB888),
            pages=40, size=(200.0, 200.0),
        )
        self.assertTrue(_pump(self.app, lambda: 0 in reader._rendered))
        bar = reader.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.assertTrue(
            _pump(self.app, lambda: 0 not in reader._rendered, timeout=8),
            "scrolling away did not evict page 0",
        )
        self.assertTrue(reader._labels[0].pixmap().isNull())

    def test_zoom_change_discards_results_from_the_old_scale(self):
        """A raster finished after a zoom change must not be painted.

        Without the generation check the stale image lands on the label that
        has already been resized, so the page shows the old scale.
        """
        from PySide6.QtGui import QImage

        release = threading.Event()
        started = threading.Event()
        old_zoom_rendered = []

        def render(index, zoom):
            old_zoom_rendered.append(zoom)
            if zoom < 2.0:
                started.set()
                release.wait(5.0)
                # Distinct size per zoom, so a stale result is detectable.
                return QImage(11, 17, QImage.Format.Format_RGB888)
            return QImage(40, 60, QImage.Format.Format_RGB888)

        reader = self._reader(render, pages=1)
        self.assertTrue(
            _pump(self.app, started.is_set, timeout=5),
            "the first raster never started",
        )
        self.assertTrue(old_zoom_rendered)

        reader.set_zoom(3.0)          # while the old raster is still blocked
        release.set()

        self.assertTrue(
            _pump(self.app, lambda: bool(reader._rendered), timeout=8),
            "no page was rendered at the new zoom",
        )
        page = sorted(reader._rendered)[0]
        pixmap = reader._labels[page].pixmap()
        self.assertEqual(
            (pixmap.width(), pixmap.height()), (40, 60),
            "a raster from the previous zoom was painted over the new one",
        )
        self.assertIn(
            3.0, old_zoom_rendered,
            "the page was never re-rendered at the new zoom",
        )

    def test_zoom_keeps_the_same_page_and_relative_vertical_position(self):
        from reader.continuous import ContinuousReader

        reader = ContinuousReader(lambda *_: None)
        reader.resize(900, 700)
        reader.show()
        reader.set_document(
            [(595.0, 842.0)] * 100,
            render=False,
        )
        self.addCleanup(reader.close)
        self.app.processEvents()

        page_index = 53
        reader.verticalScrollBar().setValue(
            reader._labels[page_index].y() + 420)
        self.app.processEvents()
        before = reader._anchor_state()
        self.assertIsNotNone(before)
        self.assertEqual(before[0], page_index)

        reader.set_zoom(reader.zoom * 1.1)
        self.app.processEvents()
        self.app.processEvents()

        after = reader._anchor_state()
        self.assertIsNotNone(after)
        self.assertEqual(after[0], page_index)
        self.assertAlmostEqual(after[1], before[1], delta=0.02)


# ---------------------------------------------------------------------------
# Defect 12: the render pump must not run while the tab is hidden.
# ---------------------------------------------------------------------------

class TestHiddenReaderStopsPump(unittest.TestCase):
    def setUp(self):
        self.app = _app()

    def test_hide_event_stops_the_pump_and_clears_the_queue(self):
        """A hidden tab must not keep allocating pixmaps.

        Observed failure: `AttributeError: 'ContinuousReader' object has no
        attribute 'hideEvent'` and `AttributeError: ... no attribute
        '_inflight'` — `_pump_one` ran regardless of `isVisible()` and
        `_evict_outside` is only called from `_ensure_visible`, which bails on
        a hidden widget, so nothing ever released the memory again.
        """
        from PySide6.QtGui import QImage

        from reader.continuous import ContinuousReader

        reader = ContinuousReader(
            lambda _i, _z: QImage(40, 60, QImage.Format.Format_RGB888))
        reader.resize(400, 600)
        reader.show()
        reader.set_document([(200.0, 200.0)] * 60)
        self.app.processEvents()
        reader._queue = [10, 11, 12]
        reader._pump.start()
        try:
            reader.hide()
            self.app.processEvents()
            self.assertFalse(
                reader._pump.isActive(),
                "the render pump kept running on a hidden tab",
            )
            self.assertEqual(
                reader._queue, [],
                "the render queue kept growing on a hidden tab",
            )
        finally:
            reader.close()


# ---------------------------------------------------------------------------
# Defect 3 + 5: qimage_to_pil buffer path and the rendered pixel budget.
# ---------------------------------------------------------------------------

class TestQImageToPil(unittest.TestCase):
    def setUp(self):
        self.app = _app()

    @staticmethod
    def _png_reference(img):
        """The old PNG round-trip, kept here as the equality oracle."""
        from io import BytesIO

        from PIL import Image
        from PySide6.QtCore import QBuffer, QIODevice

        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        return Image.open(BytesIO(bytes(buf.data())))

    def _assert_matches_png(self, img):
        from reader import pdf_utils

        reference = self._png_reference(img)
        got = pdf_utils.qimage_to_pil(img)
        self.assertEqual(got.size, (img.width(), img.height()))
        self.assertEqual(got.mode, reference.mode)
        self.assertEqual(
            got.tobytes(), reference.tobytes(),
            "buffer conversion differs from the PNG path for %s"
            % (img.format(),),
        )

    def test_buffer_path_is_pixel_identical_to_png(self):
        """RGB / RGBA / Indexed / grayscale / 32-bit must match the PNG path."""
        from PySide6.QtGui import QColor, QImage

        rgb = QImage(37, 21, QImage.Format.Format_RGB888)
        for y in range(21):
            for x in range(37):
                rgb.setPixelColor(x, y, QColor((x * 7) % 256, (y * 11) % 256,
                                               (x + y) % 256))
        self._assert_matches_png(rgb)

        rgba = QImage(16, 9, QImage.Format.Format_RGBA8888)
        for y in range(9):
            for x in range(16):
                rgba.setPixelColor(x, y, QColor(x * 15 % 256, y * 27 % 256,
                                                200, x * 16 % 256))
        self._assert_matches_png(rgba)

        indexed = QImage(16, 9, QImage.Format.Format_Indexed8)
        indexed.setColorTable([
            0xFFFF0000, 0xFF00FF00, 0xFF0000FF, 0xFF123456,
            0xFF010203, 0xFFA0B0C0, 0xFF102030, 0xFF405060,
        ])
        for y in range(9):
            for x in range(16):
                indexed.setPixel(x, y, (x + y) % 8)
        self._assert_matches_png(indexed)

        grey = QImage(11, 7, QImage.Format.Format_Grayscale8)
        for y in range(7):
            for x in range(11):
                grey.setPixel(x, y, (x * 23 + y * 31) % 256)
        self._assert_matches_png(grey)

        for fmt in (QImage.Format.Format_RGB32,
                    QImage.Format.Format_ARGB32):
            img = QImage(9, 5, fmt)
            for y in range(5):
                for x in range(9):
                    img.setPixelColor(x, y, QColor(x * 25 % 256,
                                                   y * 51 % 256, 128, 255))
            self._assert_matches_png(img)

    def test_premultiplied_format_is_not_taken_by_the_fast_path(self):
        """Premultiplied colour cannot be read straight out of the buffer.

        Its stored RGB is scaled by alpha, so the buffer is not what PNG
        decodes to; mapping it would silently darken every pixel.
        """
        from unittest import mock

        from PySide6.QtGui import QColor, QImage

        from reader import pdf_utils

        img = QImage(4, 2, QImage.Format.Format_ARGB32_Premultiplied)
        for y in range(2):
            for x in range(4):
                img.setPixelColor(x, y, QColor(200, 100, 50, 128))
        with mock.patch.object(
                pdf_utils, "_qimage_to_pil_via_png",
                wraps=pdf_utils._qimage_to_pil_via_png) as png:
            got = pdf_utils.qimage_to_pil(img)
        png.assert_called_once()
        self.assertEqual(got.tobytes(), self._png_reference(img).tobytes())

    def test_buffer_path_avoids_png_for_mapped_formats(self):
        """The PNG fallback must not be used for formats we can map."""
        from unittest import mock

        from PySide6.QtGui import QColor, QImage

        from reader import pdf_utils

        self.assertTrue(
            hasattr(pdf_utils, "_qimage_to_pil_via_png"),
            "the PNG path must remain as a documented fallback",
        )
        img = QImage(8, 4, QImage.Format.Format_RGB888)
        img.fill(QColor(1, 2, 3))
        with mock.patch.object(
                pdf_utils, "_qimage_to_pil_via_png",
                side_effect=AssertionError("PNG round-trip was used")) as png:
            pdf_utils.qimage_to_pil(img)
        png.assert_not_called()

    def test_unmapped_formats_still_fall_back_to_png(self):
        """Format_Mono has no byte-per-pixel buffer mapping here."""
        from PySide6.QtGui import QImage

        from reader import pdf_utils

        mono = QImage(8, 4, QImage.Format.Format_Mono)
        mono.fill(1)
        got = pdf_utils.qimage_to_pil(mono)
        self.assertEqual(got.size, (8, 4))
        reference = self._png_reference(mono)
        self.assertEqual(got.tobytes(), reference.tobytes())


class TestRenderedPixelBudget(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        import tempfile

        import pymupdf

        self._tmp = tempfile.mkdtemp(prefix="ht_budget_")
        self.path = os.path.join(self._tmp, "letter.pdf")
        doc = pymupdf.open()
        page = doc.new_page(width=612, height=792)   # US Letter, in points
        page.insert_text((72, 72), "budget")
        doc.save(self.path)
        doc.close()

    def tearDown(self):
        try:
            os.remove(self.path)
            os.rmdir(self._tmp)
        except OSError:
            pass

    def test_full_page_raster_is_capped_by_a_pixel_budget(self):
        """1000% on Letter must not allocate a 145 MB QImage.

        Observed failure: `AttributeError: 'BookView' object has no attribute
        'MAX_RASTER_PIXELS'` and the raster came back 6120x7920 = 48.5 Mpx
        (145 MB at 3 bytes/px, duplicated into a QPixmap by
        continuous.py:328) for a measured 290-580 MB resident.
        """
        from PySide6.QtGui import QImage

        from reader.bookview import BookView

        book = BookView()
        try:
            book.open(self.path, zoom_percent=1000.0)
            self.assertEqual(
                book.zoom_percent, 1000.0,
                "the zoom percentage range must keep working for the user",
            )
            self.assertTrue(
                hasattr(book, "MAX_RASTER_PIXELS"),
                "a rendered pixel budget is missing from BookView",
            )
            image = book._render_page(0, book.zoom)
            self.assertIsInstance(image, QImage)
            pixels = image.width() * image.height()
            self.assertLessEqual(
                pixels, book.MAX_RASTER_PIXELS,
                "raster is %d px, over the %d px budget"
                % (pixels, book.MAX_RASTER_PIXELS),
            )
            # Aspect ratio must survive the downscale.
            self.assertAlmostEqual(
                image.width() / image.height(), 612 / 792, delta=0.02)
        finally:
            book.close_doc()
            book.close()

    def test_budget_does_not_shrink_ordinary_zooms(self):
        from reader.bookview import BookView

        book = BookView()
        try:
            book.open(self.path, zoom_percent=100.0)
            image = book._render_page(0, book.zoom)
            self.assertEqual((image.width(), image.height()), (612, 792))
        finally:
            book.close_doc()
            book.close()

    def test_capped_raster_selection_maps_to_the_visible_pdf_region(self):
        from PySide6.QtCore import QPoint, QRect
        from PySide6.QtGui import QPixmap

        from reader.bookview import BookView

        book = BookView()
        try:
            book.open(self.path, zoom_percent=1000.0)
            image = book._render_page(0, book.zoom)
            label = book.reader._labels[0]
            label.setPixmap(QPixmap.fromImage(image))
            content = label.contentsRect()
            offset_x = content.x() + (content.width() - image.width()) / 2
            offset_y = content.y() + (content.height() - image.height()) / 2
            scale_x = image.width() / 612
            scale_y = image.height() / 792
            left = round(offset_x + 120 * scale_x)
            top = round(offset_y + 180 * scale_y)
            rect = QRect(QPoint(left, top), QPoint(left + 8, top + 8))

            x0, y0, x1, y1 = book._display_rect(0, rect)

            self.assertAlmostEqual(x0, 120, delta=1)
            self.assertAlmostEqual(y0, 180, delta=1)
            self.assertAlmostEqual(x1, 120 + 8 / scale_x, delta=1)
            self.assertAlmostEqual(y1, 180 + 8 / scale_y, delta=1)
        finally:
            book.close_doc()
            book.close()

    def test_pdf_rasterization_keeps_the_document_lock_until_done(self):
        from unittest import mock

        from PySide6.QtGui import QImage

        from reader import pdf_utils
        from reader.bookview import BookView

        book = BookView()
        try:
            book.open(self.path, zoom_percent=100.0)
            observed_lock_state = []

            def render(_page, _zoom):
                observed_lock_state.append(book._doc_lock._is_owned())
                return QImage(1, 1, QImage.Format.Format_RGB888)

            with mock.patch.object(pdf_utils, "render_page", side_effect=render):
                book._render_page(0, book.zoom)

            self.assertEqual(observed_lock_state, [True])
        finally:
            book.close_doc()
            book.close()


# ---------------------------------------------------------------------------
# Defect 13 + 14: thumbnail queue draining and failure expiry.
# ---------------------------------------------------------------------------

class TestThumbnailQueueDrain(unittest.TestCase):
    def setUp(self):
        self.app = _app()

    def test_queue_is_drained_after_the_last_child_exits(self):
        """multiprocessing.Queue has a feeder thread; buffered payloads survive.

        Observed failure: `AttributeError: 'ThumbWorker' object has no
        attribute '_drain_output_queue'` — the receive loop broke as soon as
        no child was alive, throwing away thumbnails the feeder thread had
        already written into the pipe.
        """
        import queue as queue_mod

        from reader.thumbnails import ThumbWorker

        worker = ThumbWorker("x.pdf", 3)
        received = []
        worker._handle_result = received.append

        payload = ("thumb", 1, 4, 3, 12, b"\x00" * 36)
        buffered = queue_mod.Queue()
        buffered.put(payload)
        self.assertEqual(
            worker._drain_output_queue(buffered), 1,
            "a buffered payload was discarded when no child was alive",
        )
        self.assertEqual(received, [payload])

    def test_drain_stops_at_empty(self):
        import queue as queue_mod

        from reader.thumbnails import ThumbWorker

        worker = ThumbWorker("x.pdf", 1)
        worker._handle_result = lambda payload: None
        self.assertEqual(worker._drain_output_queue(queue_mod.Queue()), 0)

    def test_transient_failure_is_not_blacklisted_for_the_session(self):
        """A page that failed once must be retried when it comes back.

        Observed failure: `AssertionError: 3 unexpectedly found in
        {_thumbnail_failed}` — `_thumbnail_failed` was only cleared by
        `clear_document()`, so one transient error disabled the thumbnail for
        the rest of the session.
        """
        from reader.thumbnails import ThumbPanel

        panel = ThumbPanel()
        try:
            panel._thumbnail_failed.add(3)
            panel._prune_thumbnail_cache({1, 2, 4})
            self.assertNotIn(
                3, panel._thumbnail_failed,
                "a transient thumbnail failure stayed blacklisted",
            )
        finally:
            panel.clear_document()
            panel.close()


# ---------------------------------------------------------------------------
# Defect 6 + 7 + 8 + 11: the OneOCR backend.
# ---------------------------------------------------------------------------

class _FakeFunction:
    def __init__(self, name, owner):
        self.name = name
        self.owner = owner
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.owner.invoke(self.name, args)


class _FakeDll:
    def __init__(self, results=None, previous_dir=""):
        self.calls = []
        self.args = []
        self.results = dict(results or {})
        self.previous_dir = previous_dir
        self._functions = {}

    def invoke(self, name, args):
        self.calls.append(name)
        self.args.append(args)
        if name == "GetDllDirectoryW":
            previous = self.previous_dir
            length = len(previous)
            if args and args[0] and args[0] >= length + 1 and len(args) > 1:
                try:
                    args[1].value = previous
                except Exception:
                    pass
            return length
        return self.results.get(name, 0)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._functions.setdefault(name, _FakeFunction(name, self))

    def calls_named(self, name):
        return [c for c in self.calls if c == name]

    def args_of(self, name):
        return [a[0] if a else None
                for n, a in zip(self.calls, self.args) if n == name]


class _FakeDllHandle:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeClock:
    """Stand-in for the `time` module, so cache TTLs can be advanced."""

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class OneOCRTestCase(unittest.TestCase):
    """Shared fake-DLL scaffolding for the OneOCR backend tests."""

    def setUp(self):
        import tempfile

        self._engine_dir = Path(tempfile.mkdtemp(prefix="ht_oneocr_"))
        self.addCleanup(shutil.rmtree, self._engine_dir, True)

    def fake_dll(self, results=None, previous_dir=""):
        """Patch ctypes/os so a _NativeEngine builds against a fake DLL."""
        import ctypes
        from unittest import mock

        fake = _FakeDll(results, previous_dir)
        dll_handle = _FakeDllHandle()
        patches = (
            mock.patch.object(ctypes, "WinDLL", return_value=fake),
            mock.patch.object(os, "add_dll_directory", return_value=dll_handle),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return fake, dll_handle

    def build_engine(self, results=None, previous_dir=""):
        """Instantiate _NativeEngine against a fake oneocr.dll."""
        from ocr_providers import oneocr_backend as ob

        fake, dll_handle = self.fake_dll(results, previous_dir)
        return ob._NativeEngine(self._engine_dir), fake, dll_handle

    def build_engine_expecting_failure(self, results=None):
        """Build an engine that must fail, and hand back the fake for asserts."""
        from ocr_providers.base import OcrUnavailable
        from ocr_providers.oneocr_backend import _NativeEngine

        fake, dll_handle = self.fake_dll(results)
        with self.assertRaises(OcrUnavailable):
            _NativeEngine(self._engine_dir)
        return fake, dll_handle


class TestOneOCRBackend(OneOCRTestCase):
    def test_negative_probe_is_not_cached_forever(self):
        """One transient probe failure must not disable OCR for the process.

        Observed failure: `AssertionError: None != WindowsPath(...)` —
        `_dir_cache["dir"] = found` memoised `None` with no TTL, so the second
        call returned the cached `None` and the user was told to install the
        Snipping Tool.
        """
        from pathlib import Path
        from unittest import mock

        from ocr_providers import oneocr_backend as ob

        wanted = Path("C:/fake/SnippingTool")
        try:
            with mock.patch.object(ob, "_find_snipping_dir_uncached",
                                   return_value=None):
                self.assertIsNone(ob.find_snipping_dir())
            # A hit is cached for the process lifetime.
            with mock.patch.object(ob, "_find_snipping_dir_uncached",
                                   return_value=wanted) as probe:
                ob._dir_cache.clear()
                self.assertEqual(ob.find_snipping_dir(), wanted)
                self.assertEqual(ob.find_snipping_dir(), wanted)
                probe.assert_called_once()

            # A miss must stop being believed once its TTL is up.
            with mock.patch.object(ob, "_find_snipping_dir_uncached",
                                   return_value=None):
                ob._dir_cache.clear()
                self.assertIsNone(ob.find_snipping_dir())
            clock = _FakeClock(start=time.monotonic())
            with mock.patch.object(ob, "time", clock):
                with mock.patch.object(ob, "_find_snipping_dir_uncached",
                                       return_value=wanted) as probe:
                    self.assertIsNone(
                        ob.find_snipping_dir(),
                        "a fresh negative probe must not re-run PowerShell",
                    )
                    probe.assert_not_called()
                    clock.advance(ob._DIR_MISS_TTL_SECONDS + 1)
                    self.assertEqual(
                        ob.find_snipping_dir(), wanted,
                        "a negative probe result was cached forever",
                    )
                    self.assertGreater(probe.call_count, 0)
        finally:
            ob._dir_cache.clear()

    def test_failed_probe_logs_the_real_reason(self):
        """The swallowed exception is what made the failure unexplainable."""
        from unittest import mock

        from ocr_providers import oneocr_backend as ob

        try:
            with mock.patch("subprocess.run",
                            side_effect=FileNotFoundError("powershell")):
                with self.assertLogs(ob.log, level="WARNING") as captured:
                    self.assertIsNone(ob.find_snipping_dir())
            self.assertTrue(
                any("powershell" in line for line in captured.output),
                "the real reason was swallowed: %r" % (captured.output,),
            )
        finally:
            ob._dir_cache.clear()

    def test_partial_init_releases_handles_it_already_acquired(self):
        """A failure after CreateOcrInitOptions must not leak it.

        Observed failure: `AssertionError: 0 != 1` — `_NativeEngine.__init__`
        had no cleanup, so the init-options handle leaked on every retry.
        """
        fake, _handle = self.build_engine_expecting_failure(
            {"CreateOcrPipeline": 7})
        self.assertTrue(
            fake.calls_named("ReleaseOcrInitOptions"),
            "the init-options handle leaked on a failed pipeline stage",
        )
        self.assertFalse(
            fake.calls_named("ReleaseOcrPipeline"),
            "a pipeline that was never created must not be released",
        )

    def test_failed_pipeline_releases_the_process_options_handle(self):
        fake, _handle = self.build_engine_expecting_failure(
            {"OcrProcessOptionsSetMaxRecognitionLineCount": 3})
        self.assertTrue(
            fake.calls_named("ReleaseOcrPipeline"),
            "the pipeline handle leaked when process options failed",
        )
        self.assertTrue(
            fake.calls_named("ReleaseOcrProcessOptions"),
            "the process-options handle leaked when its setter failed",
        )
        self.assertTrue(
            fake.calls_named("ReleaseOcrInitOptions"),
            "the init-options handle leaked when process options failed",
        )

    def test_native_pipeline_error_raises_instead_of_returning_empty(self):
        """A non-zero RunOcrPipeline must surface, not look like blank paper.

        Observed failure: `AssertionError: 'OneOCR engine failed' not found in
        ''` — `recognize` returned `OcrResult(text="")`, and
        popup/worker.py turned that into "Could not read any text from this
        image. Try selecting a bigger area", which is wrong advice.
        """
        from ocr_providers.base import OcrUnavailable

        engine, fake, _ = self.build_engine()
        bgra = bytes(bytearray([1, 2, 3, 255]) * 16)
        fake.results["RunOcrPipeline"] = 0x80004005
        with self.assertRaises(OcrUnavailable) as ctx:
            engine.recognize(bgra, 4, 4)
        self.assertIn("0x80004005", str(ctx.exception))
        self.assertIn("OneOCR", str(ctx.exception))

    def test_native_line_count_error_raises(self):
        from ocr_providers.base import OcrUnavailable

        engine, fake, _ = self.build_engine()
        bgra = bytes(bytearray([1, 2, 3, 255]) * 16)
        fake.results["RunOcrPipeline"] = 0
        fake.results["GetOcrLineCount"] = 5
        with self.assertRaises(OcrUnavailable) as ctx:
            engine.recognize(bgra, 4, 4)
        self.assertIn("5", str(ctx.exception))

    def test_close_restores_the_dll_search_path(self):
        """close() must undo the process-wide SetDllDirectoryW change.

        Observed failure: `AssertionError: Lists differ:
        ['SetDllDirectoryW'] != ['SetDllDirectoryW', 'SetDllDirectoryW',
        'SetDllDirectoryW']` and `dll_handle.closed` was False —
        SetDllDirectoryW was never undone and os.add_dll_directory's handle
        was discarded, so the search path stayed mutated for the whole process.
        """
        engine, fake, dll_handle = self.build_engine(previous_dir="C:\\prev")
        self.assertEqual(
            fake.args_of("GetDllDirectoryW"),
            [1024],
            "the previous search directory was never read back",
        )
        self.assertEqual(
            fake.args_of("SetDllDirectoryW"),
            [str(self._engine_dir)],
            "the DLL search directory was not pointed at the engine",
        )
        self.assertFalse(dll_handle.closed)

        engine.close()
        self.assertTrue(
            dll_handle.closed,
            "the os.add_dll_directory handle was never closed",
        )
        self.assertEqual(
            fake.args_of("SetDllDirectoryW")[-1], "C:\\prev",
            "the previous SetDllDirectoryW value was never restored",
        )


# ---------------------------------------------------------------------------
# Defect 9 + 10: the provider manager.
# ---------------------------------------------------------------------------

class TestProviderManager(unittest.TestCase):
    def setUp(self):
        from ocr_providers import manager

        manager.reset_for_tests()
        self.manager = manager

    def tearDown(self):
        self.manager.reset_for_tests()

    def test_concurrent_all_providers_builds_once(self):
        """Two callers must not each load a native engine.

        Observed failure: `AssertionError: 8 != 1` — `_warm_ocr`'s thread and
        `TranslateWorker` both saw `_providers is None` and both built a list,
        so two OneOCR pipelines and two copies of the model were loaded.
        """
        from unittest import mock

        built = []
        barrier = threading.Barrier(8)

        def slow_build():
            built.append(1)
            time.sleep(0.05)
            return ["provider"]

        with mock.patch.object(self.manager, "_build", slow_build):
            results = []
            errors = []

            def worker():
                try:
                    barrier.wait(2.0)
                    results.append(self.manager.all_providers())
                except Exception as exc:  # pragma: no cover - diagnostics
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5.0)
        self.assertEqual(errors, [])
        self.assertEqual(
            len(built), 1,
            "all_providers() built %d provider lists from 8 threads" % len(built),
        )
        self.assertEqual(len(results), 8)

    def test_close_is_part_of_the_provider_interface(self):
        """Nothing released the native engine before this.

        Observed failure: `AttributeError: 'OCRProvider' object has no
        attribute 'close'` — the abstraction had no close(), so
        `_NativeEngine.close()` was unreachable and the OneOCR resources
        pinned for the process lifetime.
        """
        from ocr_providers import base
        from ocr_providers.oneocr_backend import OneOCRProvider

        self.assertTrue(
            callable(getattr(base.OCRProvider, "close", None)),
            "OCRProvider has no close()",
        )
        self.assertIn("close", vars(OneOCRProvider))

    def test_reset_for_tests_closes_providers(self):
        from ocr_providers.base import OcrResult, OCRProvider

        released = []

        class Provider(OCRProvider):
            name = "Fake"

            def is_available(self):
                return True

            def read(self, pil_image, langs=None):
                return OcrResult(text="canned")

            def close(self):
                released.append(self)

        self.manager._providers = [Provider()]
        self.manager.reset_for_tests()
        self.assertEqual(
            len(released), 1,
            "reset_for_tests() dropped the provider without closing it",
        )

    def test_oneocr_provider_close_releases_the_engine(self):
        from ocr_providers.oneocr_backend import OneOCRProvider

        released = []

        class Engine:
            def close(self):
                released.append(self)

        provider = OneOCRProvider()
        provider._engine = Engine()
        provider.close()
        self.assertEqual(len(released), 1)
        self.assertIsNone(provider._engine)
        provider.close()  # idempotent
        self.assertEqual(len(released), 1)


# ---------------------------------------------------------------------------
# Defect 15: close_doc() must clear per-document state.
# ---------------------------------------------------------------------------

class TestBookViewCloseDoc(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        import tempfile

        import pymupdf

        self._tmp = tempfile.mkdtemp(prefix="ht_close_")
        self.paths = []
        for name in ("a.pdf", "b.pdf"):
            path = os.path.join(self._tmp, name)
            doc = pymupdf.open()
            for _ in range(2):
                doc.new_page()
            doc.save(path)
            doc.close()
            self.paths.append(path)

    def tearDown(self):
        for path in self.paths:
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(self._tmp)
        except OSError:
            pass

    def test_reopening_a_book_drops_the_previous_selection(self):
        """session_state() must not serialise the old book's rectangle.

        Observed failure: `AssertionError: (0, QRect(...)) is not None` —
        `close_doc()` left `last_selection` and every `pending_*` field set, so
        a reused BookView would Ctrl+T the new book with the old rectangle.
        """
        from PySide6.QtCore import QRect

        from reader.bookview import BookView

        book = BookView()
        try:
            book.open(self.paths[0])
            book.last_selection = (0, QRect(10, 20, 120, 40))
            book.pending_page = 1
            book.pending_scroll_position = 123
            book.pending_scroll_offset = 7
            book.pending_horizontal_scroll_position = 45

            book.open(self.paths[1])

            self.assertIsNone(book.last_selection)
            self.assertIsNone(book.pending_page)
            self.assertIsNone(book.pending_scroll_position)
            self.assertIsNone(book.pending_scroll_offset)
            self.assertIsNone(book.pending_horizontal_scroll_position)
            self.assertIsNone(
                book.session_state()["last_selection"],
                "a stale selection survived into the new document",
            )
        finally:
            book.close_doc()
            book.close()


if __name__ == "__main__":
    unittest.main()