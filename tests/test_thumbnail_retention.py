"""Prepared page previews must survive long scrolls with bounded rasters."""
import time
from unittest import mock

from PySide6.QtGui import QImage
from tests.test_app import _GuiCase, _make_pdf_file
from reader.thumbnails import ThumbPanel, ThumbWorker


class TestThumbnailRetention(_GuiCase):
    def panel(self, count):
        panel = ThumbPanel()
        panel.resize(220, 350)
        panel.show()
        panel.set_document("book.pdf", count, render_thumbnails=False)
        self.app.processEvents()
        self.addCleanup(panel.close)
        self.addCleanup(panel.clear_document)
        return panel

    def test_scrolling_and_hiding_preserve_prepared_icons(self):
        panel = self.panel(200)
        image = QImage(132, 180, QImage.Format.Format_RGB888)
        image.fill(0x336699)
        for index in (0, 90, 199):
            panel._on_thumb(index, image, panel._document_generation)
        keys = {index: panel.list.item(index).icon().cacheKey() for index in (0, 90, 199)}
        panel._prune_thumbnail_cache({199})
        panel.list.verticalScrollBar().setValue(panel.list.verticalScrollBar().maximum())
        panel.hide()
        self.app.processEvents()
        self.assertEqual(set(panel._thumbnail_images), {0, 90, 199})
        for index, key in keys.items():
            self.assertEqual(panel.list.item(index).icon().cacheKey(), key)

    def test_stale_document_results_are_ignored(self):
        panel = self.panel(2)
        image = QImage(132, 180, QImage.Format.Format_RGB888)
        image.fill(0)
        panel._on_thumb(0, image, panel._document_generation - 1)
        self.assertFalse(panel._thumbnail_images)

    def test_background_worker_prepares_entire_book_visible_pages_first(self):
        panel = self.panel(100)
        panel._thumbnail_rendering_enabled = True
        panel._thumbnail_keep = {50, 51}
        with mock.patch.object(ThumbWorker, "start"):
            panel._start_worker()
        worker = panel._worker
        self.assertEqual(worker._indices[:2], [50, 51])
        self.assertEqual(set(worker._indices), set(range(100)))

    def test_raster_budget_scales_to_large_books_and_large_zoom(self):
        panel = self.panel(2000)
        panel.set_thumbnail_zoom(3)
        image = QImage(396, 540, QImage.Format.Format_RGB888)
        image.fill(0)
        panel._on_thumb(0, image)
        cached = panel._thumbnail_images[0]
        # Icons keep bounded QImages too, and scale on demand while painting.
        estimated = cached.sizeInBytes() * 2 * 2000
        self.assertLessEqual(estimated, panel.THUMBNAIL_PIXEL_BUDGET)
        self.assertLess(cached.width(), 132)

    def test_real_background_render_finishes_before_scrolling_to_last_page(self):
        panel = self.panel(40)
        panel.set_document(_make_pdf_file("all_previews.pdf", pages=40), 40)
        deadline = time.monotonic() + 20
        while len(panel._thumbnail_images) < 40 and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertEqual(len(panel._thumbnail_images), 40)
        panel.list.verticalScrollBar().setValue(panel.list.verticalScrollBar().maximum())
        self.app.processEvents()
        self.assertNotEqual(panel.list.item(39).icon().cacheKey(), panel._placeholder_icon.cacheKey())
