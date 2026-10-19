"""Personal theme data is ignored while built-in themes remain selectable."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class PersonalThemeRemovalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_legacy_personal_themes_are_dropped_without_losing_other_settings(self):
        from config.settings import DEFAULTS, load

        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.json"
            settings_path.write_text(
                json.dumps({
                    "theme": "Forest medium",
                    "user_themes": {"Forest medium": {}},
                    "theme_schemes": {"Forest medium": "light"},
                    "theme_grades": {"Forest medium": {}},
                    "gemini_key": "keep-this-setting",
                }),
                encoding="utf-8",
            )
            with mock.patch("config.settings.SETTINGS_PATH", settings_path):
                loaded = load()

        self.assertEqual(loaded["gemini_key"], "keep-this-setting")
        self.assertEqual(loaded["theme"], "Forest medium")
        for key in ("user_themes", "theme_schemes", "theme_grades"):
            self.assertNotIn(key, DEFAULTS)
            self.assertNotIn(key, loaded)

    def test_settings_only_offers_builtin_themes_and_falls_back_from_legacy_theme(self):
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog
        from ui.theme import THEMES

        legacy_settings = dict(
            DEFAULTS,
            theme="Forest medium",
            user_themes={"Forest medium": {}},
            theme_schemes={"Forest medium": "light"},
            theme_grades={"Forest medium": {}},
        )
        dialog = SettingsDialog(legacy_settings)
        self.addCleanup(dialog.close)

        self.assertEqual(dialog._settings["theme"], "Dark Slate")
        self.assertEqual(
            [dialog.header_theme_combo.itemData(index)
             for index in range(dialog.header_theme_combo.count())],
            list(THEMES),
        )
        self.assertNotIn(
            "My Theme",
            [button.text() for button in dialog._nav_buttons],
        )
        values = dialog.values()
        for key in ("user_themes", "theme_schemes", "theme_grades"):
            self.assertNotIn(key, values)

        dialog.header_theme_combo.setCurrentIndex(
            dialog.header_theme_combo.findData("Everforest"))
        self.assertEqual(dialog.values()["theme"], "Everforest")
