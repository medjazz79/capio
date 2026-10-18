import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config import settings as settings_module


class TestOversizedSettingsProtection(unittest.TestCase):
    def test_oversized_settings_are_backed_up_and_not_replaced(self):
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
                with self.assertRaises(settings_module.SettingsLoadError):
                    settings_module.load()
                backups = list(Path(directory).glob(
                    ".harmony_translator.oversized-*.json"))
                self.assertTrue(any(
                    path.read_bytes() == original
                    for path in backups
                ))
                self.assertEqual(settings_path.read_bytes(), original)

    def test_transient_settings_access_denial_recovers_without_defaults(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / ".harmony_translator.json"
            settings_path.write_text(
                json.dumps({"gemini_key": "preserve-me", "font_size": 23}),
                encoding="utf-8",
            )
            original_read_text = Path.read_text
            denied_once = True

            def read_text(path, *args, **kwargs):
                nonlocal denied_once
                if path == settings_path and denied_once:
                    denied_once = False
                    raise PermissionError("temporary access denial")
                return original_read_text(path, *args, **kwargs)

            with (
                mock.patch.object(settings_module, "SETTINGS_PATH", settings_path),
                mock.patch.object(Path, "read_text", read_text),
                mock.patch.object(settings_module.time, "sleep"),
            ):
                loaded = settings_module.load()

            self.assertEqual(loaded["gemini_key"], "preserve-me")
            self.assertEqual(loaded["font_size"], 23)

    def test_unreadable_settings_recover_from_preserved_copy(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / ".harmony_translator.json"
            settings_path.write_text(
                json.dumps({"gemini_key": "preserve-me", "font_size": 24}),
                encoding="utf-8",
            )
            original_read_text = Path.read_text

            def deny_primary(path, *args, **kwargs):
                if path == settings_path:
                    raise PermissionError("persistent access denial")
                return original_read_text(path, *args, **kwargs)

            with (
                mock.patch.object(settings_module, "SETTINGS_PATH", settings_path),
                mock.patch.object(Path, "read_text", deny_primary),
                mock.patch.object(settings_module.time, "sleep"),
            ):
                loaded = settings_module.load()

            self.assertEqual(loaded["gemini_key"], "preserve-me")
            self.assertEqual(loaded["font_size"], 24)
            recovery_copies = list(Path(directory).glob(
                ".harmony_translator.corrupt-*.json"))
            self.assertTrue(recovery_copies)

    def test_unreadable_settings_without_recovery_fails_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / ".harmony_translator.json"
            original = b"not valid json"
            settings_path.write_bytes(original)
            with (
                mock.patch.object(settings_module, "SETTINGS_PATH", settings_path),
                mock.patch.object(
                    settings_module, "_settings_recovery_candidates", return_value=[]
                ),
            ):
                with self.assertRaises(settings_module.SettingsLoadError):
                    settings_module.load()

            self.assertEqual(settings_path.read_bytes(), original)

    def test_existing_recovery_copy_is_used_and_never_replaced(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / ".harmony_translator.json"
            recovery_path = settings_path.with_suffix(".corrupt.json")
            settings_path.write_bytes(b"not valid json")
            recovery_data = {"gemini_key": "preserve-me", "font_size": 25}
            recovery_path.write_text(json.dumps(recovery_data), encoding="utf-8")
            recovery_bytes = recovery_path.read_bytes()
            with mock.patch.object(settings_module, "SETTINGS_PATH", settings_path):
                loaded = settings_module.load()

            self.assertEqual(loaded["gemini_key"], "preserve-me")
            self.assertEqual(loaded["font_size"], 25)
            self.assertEqual(recovery_path.read_bytes(), recovery_bytes)
            self.assertEqual(settings_path.read_bytes(), b"not valid json")

    def test_custom_provider_access_denial_prevents_unsafe_load(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / ".harmony_translator.json"
            providers_path = Path(directory) / ".harmony_translator_custom_providers.json"
            settings_path.write_text(
                json.dumps({"gemini_key": "preserve-me"}), encoding="utf-8"
            )
            providers_path.write_text("[]", encoding="utf-8")
            original_read_text = Path.read_text

            def deny_providers(path, *args, **kwargs):
                if path == providers_path:
                    raise PermissionError("temporary access denial")
                return original_read_text(path, *args, **kwargs)

            with (
                mock.patch.object(settings_module, "SETTINGS_PATH", settings_path),
                mock.patch.object(Path, "read_text", deny_providers),
                mock.patch.object(settings_module.time, "sleep"),
            ):
                with self.assertRaises(settings_module.SettingsLoadError):
                    settings_module.load()

            self.assertEqual(
                json.loads(settings_path.read_text(encoding="utf-8"))["gemini_key"],
                "preserve-me",
            )
            self.assertEqual(providers_path.read_text(encoding="utf-8"), "[]")

    def test_invalid_custom_provider_shape_prevents_unsafe_load(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / ".harmony_translator.json"
            providers_path = Path(directory) / ".harmony_translator_custom_providers.json"
            settings_path.write_text("{}", encoding="utf-8")
            providers_path.write_text("{}", encoding="utf-8")
            with mock.patch.object(settings_module, "SETTINGS_PATH", settings_path):
                with self.assertRaises(settings_module.SettingsLoadError):
                    settings_module.load()

            self.assertEqual(json.loads(providers_path.read_text()), {})

    def test_atomic_json_replace_retries_temporary_access_denial(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "settings.json"
            original_replace = Path.replace
            denied_once = True

            def replace_once(path, destination):
                nonlocal denied_once
                if path.name.startswith("settings.json.") and denied_once:
                    denied_once = False
                    raise PermissionError("temporary access denial")
                return original_replace(path, destination)

            with (
                mock.patch.object(Path, "replace", replace_once),
                mock.patch.object(settings_module.time, "sleep"),
            ):
                settings_module._write_json_atomically(target, {"ok": True})

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"ok": True})


if __name__ == "__main__":
    unittest.main()
