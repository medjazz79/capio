"""The live theme preview must not leave widget-local palette state behind."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QSplitter


class PreviewPaletteDoesNotSurviveSaveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._application_style = self.app.styleSheet()
        self._application_palette = self.app.palette()
        self.addCleanup(self._restore_application_style)

        scratch = Path(tempfile.mkdtemp(prefix="harmony-palette-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        patcher = mock.patch("config.settings.SETTINGS_PATH",
                             scratch / "settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)

        import config.settings as settings_module

        settings_module.save({
            "theme": "Dark Slate",
            "color_scheme": "dark",
            "font_size": 17,
        })

        from main import MainWindow

        self.window = MainWindow()
        self.addCleanup(self.window.close)
        self.central = self.window.centralWidget()

    def _restore_application_style(self):
        self.app.setStyleSheet(self._application_style)
        self.app.setPalette(self._application_palette)

    def _preview(self, restoring: bool = False):
        from config.settings import load

        preview = dict(load())
        self.window._preview_theme_from_settings(preview, restoring)
        self.app.processEvents()

    def _window_colour(self, widget):
        return widget.palette().color(QPalette.ColorRole.Window).name()

    def _save(self):
        """The same two calls open_settings makes after Save Changes."""
        self.window._apply_theme_skin(
            self.window.settings.get("theme", "Dark Slate"))
        self.window._flush_app_skin()
        self.app.processEvents()

    def test_the_fast_preview_is_what_turns_auto_fill_on(self):
        """Documents the mechanism the fix has to undo."""
        self.assertFalse(self.central.autoFillBackground())
        self._preview(False)
        self.assertTrue(
            self.central.autoFillBackground(),
            "the fast preview turns autoFillBackground on the central widget")

    def test_saving_turns_the_auto_fill_back_off(self):
        self._preview(False)
        self._save()
        self.assertFalse(
            self.central.autoFillBackground(),
            "autoFillBackground survived the Save, so the panes keep "
            "painting the default palette (#efefef) instead of the theme")

    def test_saving_removes_the_pinned_palette_override(self):
        self._preview(False)
        self._save()
        self.assertFalse(
            self.central.testAttribute(Qt.WidgetAttribute.WA_SetPalette),
            "the central widget still has a widget-local palette after Save")

    def test_cancelling_also_clears_it(self):
        launch = self._window_colour(self.central)
        self._preview(False)
        self._preview(True)
        self.app.processEvents()
        self.assertFalse(self.central.autoFillBackground())
        self.assertEqual(self._window_colour(self.central), launch)

    def test_the_reader_splitter_edge_is_not_the_default_palette(self):
        """The user's report: the border line turns pale after Save."""
        border = self._window_colour(self.central)
        self._preview(False)
        self._save()
        self.window.resize(1200, 800)
        self.app.processEvents()
        image = self.window.grab().toImage()
        splitters = [
            sp for sp in self.window.findChildren(QSplitter)
            if sp.isVisible() and sp.parent() is not None
            and sp.objectName() != "settingsBodySplitter"
        ]
        if not splitters:
            self.skipTest("no splitter on screen without an open book")
        for splitter in splitters:
            point = splitter.mapTo(self.window, QPoint(0, splitter.height() // 2))
            if not (0 <= point.x() < image.width()
                    and 0 <= point.y() < image.height()):
                continue
            with self.subTest(splitter=splitter.objectName() or "(reader)"):
                self.assertNotEqual(
                    image.pixelColor(point.x(), point.y()).name().upper(),
                    "#EFEFEF",
                    "the splitter edge painted Qt's default palette")


if __name__ == "__main__":
    unittest.main()