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
                self.assertEqual(dark, THEMES[name])
        self.assertNotIn("Pure Light", THEMES)

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

    def test_unchecked_settings_checkboxes_are_visible_in_every_theme(self):
        from ui.theme import THEMES, settings_stylesheet, theme_colors

        for theme in THEMES:
            for scheme in ("dark", "light"):
                with self.subTest(theme=theme, scheme=scheme):
                    colors = theme_colors(theme, scheme)
                    stylesheet = settings_stylesheet(theme, scheme)
                    indicator_style = (
                        "QCheckBox::indicator {\n"
                        "            width: 14px; height: 14px;\n"
                        f"            border: 1.5px solid {colors['accent_light']};\n"
                        "            border-radius: 3px;\n"
                        f"            background-color: {colors['surface_hover']};"
                    )
                    self.assertIn(indicator_style, stylesheet)

    def test_settings_dialog_replaces_the_removed_legacy_theme(self):
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(dict(DEFAULTS, theme="Pure Light"))
        try:
            offered_themes = [
                dialog.header_theme_combo.itemData(index)
                for index in range(dialog.header_theme_combo.count())
            ]
            self.assertNotIn("Pure Light", offered_themes)
            self.assertEqual(
                dialog.header_theme_combo.currentData(), "Dark Slate")
            self.assertEqual(dialog.values()["theme"], "Dark Slate")
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
                self.assertEqual(load()["theme"], "Dark Slate")

                settings_path.write_text(
                    json.dumps({
                        "theme": "Pure Light",
                        "color_scheme": "dark",
                    }), encoding="utf-8")
                self.assertEqual(load()["color_scheme"], "dark")
                self.assertEqual(load()["theme"], "Dark Slate")

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
