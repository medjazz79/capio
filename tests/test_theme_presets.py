import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestThemePresets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_every_builtin_theme_has_light_and_dark_variants(self):
        from main import app_stylesheet
        from ui.theme import THEMES, theme_colors
        import re

        self.assertGreaterEqual(len(THEMES), 16)
        for name in THEMES:
            with self.subTest(theme=name):
                dark = theme_colors(name, "dark")
                light = theme_colors(name, "light")
                self.assertEqual(set(dark), set(THEMES["Dark Slate"]))
                self.assertEqual(set(light), set(THEMES["Dark Slate"]))
                for scheme, colors in (("dark", dark), ("light", light)):
                    default_style = app_stylesheet(name, scheme)
                    themed_style = app_stylesheet(name, scheme, True)
                    canvas_pattern = (
                        r"QScrollArea#pdfCanvas\s*\{\s*"
                        r"background-color:\s*([^;]+);"
                    )
                    self.assertEqual(
                        re.search(canvas_pattern, default_style).group(1),
                        "#F1F3F5",
                    )
                    self.assertEqual(
                        re.search(canvas_pattern, themed_style).group(1),
                        colors["canvas"],
                    )
                if name != "Pure Light":
                    self.assertEqual(dark, THEMES[name])

    def test_settings_dialog_exposes_the_system_dark_light_preference(self):
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(dict(DEFAULTS, color_scheme="dark"))
        try:
            self.assertEqual(
                [dialog.color_scheme_combo.itemData(index)
                 for index in range(dialog.color_scheme_combo.count())],
                ["system", "dark", "light"],
            )
            dialog.color_scheme_combo.setCurrentIndex(
                dialog.color_scheme_combo.findData("light"))
            self.assertEqual(dialog.values()["color_scheme"], "light")
            self.assertFalse(dialog.pdf_canvas_theme_check.isChecked())
            dialog.pdf_canvas_theme_check.setChecked(True)
            self.assertTrue(
                dialog.values()["pdf_canvas_theme_background"])
        finally:
            dialog.close()

    def test_legacy_theme_settings_keep_their_existing_appearance(self):
        from config.settings import load

        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.json"
            with mock.patch("config.settings.SETTINGS_PATH", settings_path):
                self.assertEqual(load()["color_scheme"], "system")

                settings_path.write_text(
                    json.dumps({"theme": "Dark Slate"}), encoding="utf-8")
                self.assertEqual(load()["color_scheme"], "dark")

                settings_path.write_text(
                    json.dumps({"theme": "Pure Light"}), encoding="utf-8")
                self.assertEqual(load()["color_scheme"], "light")

    def test_pdf_page_surroundings_render_the_selected_canvas_color(self):
        from PySide6.QtGui import QImage
        from main import app_stylesheet
        from reader.continuous import ContinuousReader
        from ui.theme import theme_colors

        app = self.app
        previous_style = app.styleSheet()
        reader = ContinuousReader(
            lambda _index, _zoom: QImage(
                80, 100, QImage.Format.Format_RGB32))
        try:
            app.setStyleSheet(
                app_stylesheet("Everforest", "dark", True))
            reader.resize(300, 220)
            reader.set_document([(80.0, 100.0)], render=False)
            reader.show()
            app.processEvents()

            canvas_pixel = reader.viewport().grab().toImage().pixelColor(
                10, 10).name().upper()
            self.assertEqual(
                canvas_pixel,
                theme_colors("Everforest", "dark")["canvas"].upper(),
            )
        finally:
            reader.close()
            app.setStyleSheet(previous_style)
