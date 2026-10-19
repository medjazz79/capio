"""The floating translator must share the reader's settings, skin and cache.

The bubble is a thin front-end: it only opens the same translation popup the
reader uses. Everything it can do -- theme, fonts, voices, OCR hints, the TTS
cache -- must come from the one shared settings file and the one shared cache,
never from a private copy.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _FloatSharingCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import config.settings as settings_module

        self._previous_style = self.app.styleSheet()
        self.addCleanup(self.app.setStyleSheet, self._previous_style)

        scratch = Path(tempfile.mkdtemp(prefix="harmony-float-shared-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        self.scratch = scratch
        patcher = mock.patch("config.settings.SETTINGS_PATH",
                             scratch / "settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        settings_module.save({
            "theme": "Dark Slate",
            "color_scheme": "dark",
            "font_size": 17,
            "selection_mode": "ocr",
        })

    def _float(self, **overrides):
        from main_02 import FloatButton

        floating = FloatButton()
        floating.settings.update(overrides)
        self.addCleanup(self._close_float, floating)
        self.app.processEvents()
        return floating

    @staticmethod
    def _close_float(floating):
        for popup in list(getattr(floating, "popups", [])):
            try:
                close = getattr(popup, "close", None)
                if close is not None:
                    close()
            except RuntimeError:
                pass
        try:
            floating.close()
        except RuntimeError:
            pass


class SharedSkinTest(_FloatSharingCase):
    """Every window the bubble opens must wear the shared theme."""

    def test_the_language_dialog_wears_the_shared_theme(self):
        from ui.float_language_dialog import FloatLanguageDialog
        from ui.theme import theme_colors

        floating = self._float()
        dialog = FloatLanguageDialog(dict(floating.settings), floating)
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        colors = theme_colors("Dark Slate", "dark")
        painted = dialog.grab().toImage().pixelColor(4, 4).name().upper()

        self.assertEqual(
            painted, colors["window"].upper(),
            "the language dialog ignored the shared theme")

    def test_a_warning_box_wears_the_shared_theme(self):
        from PySide6.QtWidgets import QMessageBox

        from ui.theme import theme_colors

        floating = self._float()
        box = QMessageBox(floating)
        box.setWindowTitle("Could not save settings")
        box.setText("Settings could not be saved.")
        box.show()
        self.app.processEvents()

        colors = theme_colors("Dark Slate", "dark")
        painted = box.grab().toImage().pixelColor(4, 4).name().upper()

        self.assertEqual(
            painted, colors["panel"].upper(),
            "a warning box ignored the shared theme")


class SharedSettingsRefreshTest(_FloatSharingCase):
    """An open popup must follow every shared display setting."""

    def test_a_shared_display_change_refreshes_visible_popups(self):
        class FakePopup:
            def __init__(self):
                self.refreshed = []

            def isVisible(self):
                return True

            def refresh_display(self, settings):
                self.refreshed.append(dict(settings))

        floating = self._float()
        for key, value in (
            ("translation_top_bottom_margin_px", 48),
            ("speech_engine", "gemini"),
            ("speech_rate", 0.75),
            ("speech_read_latin_words", True),
            ("chat_reply_language", "en"),
            ("gemini_key", "test-key"),
        ):
            with self.subTest(key=key):
                popup = FakePopup()
                floating.popups = [popup]
                latest = dict(floating.settings, **{key: value})
                with mock.patch("main_02.load", return_value=latest):
                    floating._reload_settings()
                self.assertTrue(
                    popup.refreshed,
                    f"changing {key} did not refresh the open popup")
                self.assertEqual(popup.refreshed[-1][key], value)

    def test_changing_the_float_languages_retranslates_other_open_popups(self):
        class FakePopup:
            def __init__(self):
                self.synced = []

            def isVisible(self):
                return True

            def sync_languages(self, source, target):
                self.synced.append((source, target))

        floating = self._float()
        origin = FakePopup()
        other = FakePopup()
        floating.popups = [origin, other]

        with mock.patch("main_02.save"):
            floating._save_float_languages("fr", "it", origin)

        self.assertEqual(floating.settings["float_source_language"], "fr")
        self.assertEqual(floating.settings["float_target_language"], "it")
        self.assertEqual(other.synced, [("fr", "it")])
        self.assertEqual(
            origin.synced, [],
            "the popup that changed its own language must not retranslate twice")


class SharedCacheTest(_FloatSharingCase):
    def test_startup_prunes_the_shared_speech_cache(self):
        import main_02

        cache = self.scratch / "tts-cache"
        cache.mkdir()
        stale = cache / "stale.mp3"
        stale.write_bytes(b"old audio")
        os.utime(stale, (0, 0))

        with mock.patch.dict(
            os.environ, {"HARMONY_TTS_CACHE": str(cache)},
        ):
            main_02._prune_shared_speech_cache()

        self.assertFalse(
            stale.exists(),
            "the floating translator never pruned the shared TTS cache")

    def test_the_float_process_prunes_the_cache_at_startup(self):
        import main_02

        fake_app = mock.Mock()
        fake_app.exec.return_value = 0
        with (
            mock.patch.object(main_02, "setup_logging"),
            mock.patch("translators.usage.start_session"),
            mock.patch.object(main_02, "_prune_shared_speech_cache") as prune,
            mock.patch.object(main_02, "QApplication") as app_type,
            mock.patch.object(main_02, "FloatButton", return_value=mock.Mock()),
            mock.patch.object(main_02, "sys") as sys_module,
        ):
            app_type.return_value = fake_app
            main_02.main()

        prune.assert_called_once()
        sys_module.exit.assert_called_once()


class SharedOcrLanguageTest(_FloatSharingCase):
    def test_ocr_language_hints_are_shared_with_the_reader(self):
        from translators import ocr

        self.assertIsNone(ocr.languages_for("auto"))
        self.assertEqual(ocr.languages_for("ar"), ("ar",))
        self.assertEqual(ocr.languages_for("fr"), ("fr", "en"))
        self.assertEqual(ocr.languages_for("ru"), ("ru", "en"))
        self.assertIsNone(ocr.languages_for("unknown-language"))

    def test_a_float_translation_asks_ocr_for_the_shared_source_language(self):
        from PIL import Image

        floating = self._float(
            float_source_language="de", float_target_language="fr")

        with (
            mock.patch("translators.ocr.is_available", return_value=True),
            mock.patch(
                "translators.ocr.prepare_image", side_effect=lambda image: image),
            mock.patch("popup.translate_popup.TranslatePopup") as popup_type,
            mock.patch("main_02.load", return_value=dict(floating.settings)),
        ):
            self.assertTrue(
                floating.translate_pil(Image.new("RGB", (20, 20))))

        self.assertEqual(
            popup_type.call_args.kwargs["ocr_langs"], ("de", "en"),
            "the float tool ignored the shared source language for OCR")


class SharedThemePreviewTest(_FloatSharingCase):
    def test_choosing_a_theme_in_settings_previews_the_bubble(self):
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QDialog

        from ui.theme import theme_colors

        class PreviewDialog(QObject):
            previewSettingsChanged = Signal(dict, bool)

            def __init__(self, settings, parent=None):
                super().__init__(parent)
                self.settings = dict(settings)

            def exec(self):
                self.previewSettingsChanged.emit(
                    {"theme": "Nordic Gray", "color_scheme": "dark"}, False)
                return QDialog.DialogCode.Rejected

        floating = self._float()
        with mock.patch("ui.settings_dialog.SettingsDialog", PreviewDialog):
            floating._open_settings()

        preview_accent = theme_colors("Nordic Gray", "dark")["accent"]
        self.assertIn(
            preview_accent, floating.btn.styleSheet(),
            "the settings dialog did not preview the bubble's theme")


if __name__ == "__main__":
    unittest.main()
