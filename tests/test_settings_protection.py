import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config import settings as settings_module


class TestOversizedSettingsProtection(unittest.TestCase):
    def test_oversized_settings_are_backed_up_before_a_later_save(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / ".harmony_translator.json"
            original = b"x" * 32
            settings_path.write_bytes(original)
            with (
                mock.patch.object(settings_module, "SETTINGS_PATH", settings_path),
                mock.patch.object(
                    settings_module, "_MAX_SETTINGS_FILE_BYTES", 16
                ),
            ):
                loaded = settings_module.load()
                backups = list(Path(directory).glob(
                    ".harmony_translator.oversized-*.json"))
                self.assertTrue(backups)
                self.assertTrue(any(path.read_bytes() == original for path in backups))

                settings_module.save(loaded)

                self.assertTrue(any(
                    path.read_bytes() == original
                    for path in Path(directory).glob(
                        ".harmony_translator.oversized-*.json")
                ))
                self.assertNotEqual(settings_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
