"""Playback volume belongs to the OS mixer, never to Capio's settings."""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class FakeAudioOutput:
    def __init__(self, volume: float = 0.25):
        self.volume = volume

    def setVolume(self, volume: float) -> None:
        self.volume = volume


class SystemVolumePolicyTest(unittest.TestCase):
    def test_audio_output_starts_at_full_scale(self):
        from config.settings import configure_playback_volume

        output = FakeAudioOutput()

        configure_playback_volume(output)

        self.assertEqual(output.volume, 1.0)

    def test_volume_is_never_saved_or_restored(self):
        from config import settings as settings_module

        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"font_size": 21, "volume": 0.3}),
                encoding="utf-8",
            )
            with mock.patch.object(
                settings_module, "SETTINGS_PATH", path
            ):
                loaded = settings_module.load()
                self.assertEqual(loaded["font_size"], 21)
                self.assertNotIn("volume", loaded)

                settings_module.save(dict(loaded, volume=0.2))

            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertNotIn("volume", saved)


class AudioOutputWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_chat_panel_uses_the_system_volume_policy(self):
        from ui.chat_panel import ChatPanel

        with mock.patch(
            "ui.chat_panel.configure_playback_volume"
        ) as configure:
            panel = ChatPanel(lambda: {"chat_font_size": 12.0})
            try:
                configure.assert_called_once_with(panel._audio_output)
            finally:
                panel.close()

    def test_translate_popup_uses_the_system_volume_policy(self):
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        with (
            mock.patch(
                "popup.translate_popup.configure_playback_volume"
            ) as configure,
            mock.patch.object(TranslatePopup, "_start_translation_worker"),
        ):
            popup = TranslatePopup(dict(
                DEFAULTS,
                source_text="نص تجريبي",
                target_language="ar",
            ))
            try:
                configure.assert_called_once_with(popup._audio_output)
            finally:
                popup.deleteLater()


if __name__ == "__main__":
    unittest.main()
