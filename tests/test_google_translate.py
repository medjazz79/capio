"""Tests for the selectable keyless Google Translate engine."""
import unittest
from unittest import mock

from config.settings import DEFAULTS
from translators import chain, registry


class TestGoogleTranslateOption(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_google_translate_is_listed_and_available_without_credentials(self):
        model = registry.GOOGLE_TRANSLATE_MODEL
        rows = dict(registry.model_groups(dict(DEFAULTS)))

        self.assertEqual(registry.provider_of(model), "google_translate")
        self.assertEqual(registry.availability(model, dict(DEFAULTS)), (True, ""))
        self.assertTrue(rows[model])
        self.assertEqual(registry.image_capability(model), "unsupported")

    def test_settings_picker_shows_a_friendly_google_label(self):
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(dict(DEFAULTS))
        try:
            index = dialog.z_model.findData(registry.GOOGLE_TRANSLATE_MODEL)
            self.assertGreaterEqual(index, 0)
            self.assertEqual(
                dialog.z_model.itemText(index),
                "\U0001f310 Google Translate",
            )
        finally:
            dialog.close()

    def test_selected_google_translate_is_used_and_badged_as_chosen(self):
        settings = dict(
            DEFAULTS,
            model=registry.GOOGLE_TRANSLATE_MODEL,
            source_language="en",
            target_language="fr",
        )
        with mock.patch(
            "translators.fallback.gtx_translate",
            return_value="Bonjour.",
        ) as translate:
            text, badge = chain.translate("Hello.", settings)

        self.assertEqual(text, "Bonjour.")
        self.assertEqual(badge, "🌐 chosen: Google Translate")
        translate.assert_called_once_with("Hello.", "en", "fr")

    def test_google_translate_uses_normal_stream_translation_contract(self):
        settings = dict(
            DEFAULTS,
            model=registry.GOOGLE_TRANSLATE_MODEL,
            source_language="en",
            target_language="fr",
        )
        with mock.patch(
            "translators.fallback.gtx_translate",
            return_value="Bonjour.",
        ) as translate:
            events = list(chain.translate_stream("Hello.", settings))

        self.assertEqual(
            events,
            [
                ("partial", "Bonjour.", ""),
                ("done", "Bonjour.", "🌐 chosen: Google Translate"),
            ],
        )
        translate.assert_called_once_with("Hello.", "en", "fr")

    def test_engine_probe_checks_google_directly(self):
        from translators.probe import probe_all

        settings = dict(
            DEFAULTS,
            model=registry.GOOGLE_TRANSLATE_MODEL,
        )
        with mock.patch(
            "translators.fallback.gtx_translate",
            return_value="OK",
        ) as translate:
            results = probe_all(settings)

        self.assertTrue(results[0][1])
        self.assertIn("OK", results[0][2])
        translate.assert_called_once_with("Say OK", "en", "en")

    def test_failed_selected_google_is_not_retried_as_automatic_fallback(self):
        settings = dict(
            DEFAULTS,
            model=registry.GOOGLE_TRANSLATE_MODEL,
            source_language="en",
            target_language="fr",
        )
        with mock.patch(
            "translators.fallback.gtx_translate",
            side_effect=RuntimeError("rate limited"),
        ) as google, mock.patch(
            "translators.mymemory.translate",
            return_value="Bonjour.",
        ) as mymemory:
            text, badge = chain.translate("Hello.", settings)

        self.assertEqual(text, "Bonjour.")
        self.assertIn("Free service (alt)", badge)
        google.assert_called_once()
        mymemory.assert_called_once()

    def test_no_glossary_match_is_reported_as_failure_not_translation(self):
        settings = dict(
            DEFAULTS,
            model=registry.GOOGLE_TRANSLATE_MODEL,
            source_language="en",
            target_language="ar",
        )
        with mock.patch(
            "translators.fallback.gtx_translate",
            side_effect=RuntimeError("Google rate limited"),
        ), mock.patch(
            "translators.mymemory.translate",
            side_effect=RuntimeError("MyMemory unavailable"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "no matching terms; no translation could be produced",
            ) as caught:
                chain.translate("SCENE OF THE CRIME", settings)

        message = str(caught.exception)
        self.assertIn("Google rate limited", message)
        self.assertIn("MyMemory unavailable", message)
        self.assertNotIn("SCENE OF THE CRIME", message)

    def test_google_translate_rejects_images(self):
        settings = dict(
            DEFAULTS,
            model=registry.GOOGLE_TRANSLATE_MODEL,
        )
        with self.assertRaisesRegex(RuntimeError, "does not support image input"):
            chain.translate_image(object(), settings)


if __name__ == "__main__":
    unittest.main()
