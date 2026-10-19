"""Regression tests for settings preservation and test-run isolation."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestSettingsPackageIsolation(unittest.TestCase):
    def test_plain_unittest_package_import_redirects_settings(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        home = pathlib.Path(tempfile.mkdtemp(prefix="harmony-settings-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        isolated_path = home / "isolated" / "settings.json"
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env.pop("HARMONY_TEST_SETTINGS_PATH", None)
        code = (
            "import tests; import config.settings as s; "
            "print(s.SETTINGS_PATH); s.save({'gemini_key': 'test-only'})"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(pathlib.Path(result.stdout.strip()).is_file())
        self.assertFalse((home / ".harmony_translator.json").exists())
        self.assertFalse(
            (home / ".harmony_translator_custom_providers.json").exists())
        self.assertNotEqual(
            pathlib.Path(result.stdout.strip()),
            home / ".harmony_translator.json",
        )


class TestSettingsTogglePreservation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from config import settings as settings_module
        from ui.settings_dialog import SettingsDialog

        self.settings_module = settings_module
        self.directory = pathlib.Path(
            tempfile.mkdtemp(prefix="harmony-settings-toggle-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.settings_path = self.directory / ".harmony_translator.json"
        patcher = mock.patch.object(
            settings_module, "SETTINGS_PATH", self.settings_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings_path.write_text(
            json.dumps({
                "gemini_key": "opening-key",
                "theme": "Nord",
                "font_size": 20,
                "custom_providers": [{
                    "id": "mine",
                    "name": "Mine",
                    "base_url": "http://localhost",
                    "api_key": "provider-key",
                    "model": "model",
                }],
            }),
            encoding="utf-8",
        )
        self.dialog = SettingsDialog(settings_module.load())
        self.addCleanup(self.dialog.close)

    def _read(self):
        return json.loads(self.settings_path.read_text(encoding="utf-8"))

    def test_theme_choice_does_not_write_settings_before_save(self):
        before = self.settings_path.read_bytes()
        self.dialog.header_theme_combo.setCurrentIndex(
            self.dialog.header_theme_combo.findData("Amoled"))
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertEqual(self.dialog.values()["theme"], "Amoled")
        self.assertEqual(
            self.settings_module.load()["custom_providers"][0]["id"],
            "mine",
        )

    def test_checkbox_persists_only_its_change_over_current_file(self):
        self.settings_path.write_text(
            json.dumps({
                "gemini_key": "updated-elsewhere",
                "theme": "Amoled",
                "font_size": 20,
            }),
            encoding="utf-8",
        )
        self.dialog.arabic_tashkeel.setChecked(True)
        saved = self._read()
        self.assertEqual(saved["gemini_key"], "updated-elsewhere")
        self.assertEqual(saved["theme"], "Amoled")
        self.assertEqual(saved["font_size"], 20)
        self.assertTrue(saved["arabic_tashkeel"])
        self.assertEqual(
            self.settings_module.load()["custom_providers"][0]["id"],
            "mine",
        )


class TestSpeechRateMigration(unittest.TestCase):
    def test_legacy_rate_initializes_both_language_rates(self):
        from config import settings as settings_module

        with tempfile.TemporaryDirectory(
            prefix="harmony-speech-rate-migration-"
        ) as directory:
            path = pathlib.Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"speech_rate": 0.75}),
                encoding="utf-8",
            )
            with mock.patch.object(settings_module, "SETTINGS_PATH", path):
                settings = settings_module.load()

        self.assertEqual(settings["speech_rate_arabic"], 0.75)
        self.assertEqual(settings["speech_rate_other"], 0.75)


if __name__ == "__main__":
    unittest.main()
