"""Language changes in a reused popup must supersede its previous request.

Workers are controlled locally: these tests never contact a translation API.
"""
from types import SimpleNamespace
from unittest import mock

from PySide6.QtCore import QObject, Signal

from config.settings import DEFAULTS
from tests.test_app import _GuiCase


TRANSLATIONS = {
    "ar": "هذه هي الترجمة الجديدة.",
    "fr": "Voici la nouvelle traduction.",
    "en": "This is the new translation.",
    "ru": "Это новый перевод.",
    "it": "Questa è la nuova traduzione.",
    "es": "Esta es la nueva traducción.",
    "de": "Dies ist die neue Übersetzung.",
    "pt": "Esta é a nova tradução.",
    "ja": "これは新しい翻訳です。",
    "ko": "이것은 새로운 번역입니다.",
    "zh": "这是新的翻译。",
    "hi": "यह नया हिन्दी अनुवाद है।",
}


class ControlledWorker(QObject):
    done = Signal(str, str)
    stage = Signal(str)
    failed = Signal(str)
    source_ready = Signal(str)
    partial = Signal(str)
    finished = Signal()

    def __init__(self, settings, text=None, pil_image=None, ocr_langs=None):
        super().__init__()
        self._settings = dict(settings)
        self.text = text
        self.image = pil_image
        self.ocr_langs = ocr_langs
        self.translate_secs = 0
        self.running = False
        self.cancelled = False

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running

    def requestInterruption(self):
        self.cancelled = True

    def complete(self, text):
        self.done.emit(text, "mock provider")
        self.running = False
        self.finished.emit()


class TestPopupLanguageSwitch(_GuiCase):
    def setUp(self):
        super().setUp()
        self.workers = []
        self.popups = []

        def make_worker(*args, **kwargs):
            worker = ControlledWorker(*args, **kwargs)
            self.workers.append(worker)
            return worker

        patcher = mock.patch(
            "popup.translate_popup.TranslateWorker", side_effect=make_worker)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._close_popups)

    def _close_popups(self):
        for worker in self.workers:
            worker.running = False
            worker.finished.emit()
        for popup in self.popups:
            popup.close()

    def popup(self, *, floating=False, **kwargs):
        from popup.translate_popup import TranslatePopup

        settings = dict(DEFAULTS, source_language="en", target_language="ar")
        settings.update(kwargs.pop("settings", {}))
        if "pil_image" not in kwargs:
            kwargs.setdefault("source_text", "Original source passage")
        popup = TranslatePopup(
            settings, floating_language_controls=True,
            float_language_preferences=floating, **kwargs)
        self.popups.append(popup)
        popup.show()
        return popup

    def test_settings_refresh_retranslates_and_updates_language_controls(self):
        for floating in (False, True):
            with self.subTest(floating=floating):
                popup = self.popup(floating=floating)
                old = self.workers[-1]
                old.complete("old Arabic translation")
                count = len(self.workers)
                popup.refresh_display(dict(popup.settings, target_language="fr"))
                self.assertEqual(len(self.workers), count + 1)
                self.assertEqual(self.workers[-1]._settings["target_language"], "fr")
                self.assertEqual(popup.target_language_btn.text(), "To: Français")
                self.assertEqual(popup.result.toPlainText(), "")

    def test_old_source_signal_cannot_replace_current_source(self):
        popup = self.popup()
        old = self.workers[-1]
        popup.set_source_content(text="New page original text")
        old.source_ready.emit("Old page original text")
        popup._language_actions["target"]["fr"].trigger()
        self.assertEqual(self.workers[-1].text, "New page original text")

    def test_chinese_is_cleared_before_language_callback_can_process_events(self):
        chinese = "这是上一次显示的中文翻译。"
        for floating in (False, True):
            with self.subTest(floating=floating):
                popup = self.popup(floating=floating, settings={"target_language": "zh"})
                old = self.workers[-1]
                old.complete(chinese)
                observations = []

                def save_language(source, target, origin):
                    observations.append(origin.result.toPlainText())
                    # Saving/settings callbacks can enter a nested Qt loop.
                    old.partial.emit(chinese)
                    old.done.emit(chinese, "old queued reply")
                    observations.append(origin.result.toPlainText())

                popup._language_change_callback = save_language
                popup._language_actions["target"]["hi"].trigger()
                self.assertEqual(observations, ["", ""])
                self.workers[-1].complete("यह नया हिन्दी अनुवाद है।")
                self.assertEqual(popup.result.toPlainText(), "यह नया हिन्दी अनुवाद है।")

    def test_audio_reset_cannot_repaint_a_superseded_language(self):
        for floating in (False, True):
            with self.subTest(floating=floating):
                popup = self.popup(floating=floating, settings={"target_language": "zh"})
                old = self.workers[-1]
                old.complete("这是旧的中文翻译。")
                observations = []
                reset_audio = popup._reset_speech_for_new_translation

                def reset_with_queued_reply():
                    old.partial.emit("这是旧的中文翻译。")
                    old.done.emit("这是旧的中文翻译。", "old")
                    observations.append(popup.result.toPlainText())
                    reset_audio()

                with mock.patch.object(popup, "_reset_speech_for_new_translation",
                                       side_effect=reset_with_queued_reply):
                    popup.sync_languages("en", "hi")
                self.assertEqual(observations, [""])

    def test_all_target_languages_drop_queued_previous_replies_before_saving(self):
        for floating in (False, True):
            with self.subTest(floating=floating):
                popup = self.popup(floating=floating, settings={"target_language": "hi"})
                self.workers[-1].complete(TRANSLATIONS["hi"])
                for target, translation in TRANSLATIONS.items():
                    with self.subTest(target=target):
                        old = self.workers[-1]
                        previous = popup.result.toPlainText()
                        observations = []

                        def save_language(source, code, origin):
                            observations.append(origin.result.toPlainText())
                            old.stage.emit("stale status")
                            old.source_ready.emit("stale original")
                            old.partial.emit(previous)
                            old.done.emit(previous, "old queued reply")
                            old.failed.emit("stale failure")
                            observations.append(origin.result.toPlainText())

                        popup._language_change_callback = save_language
                        popup._language_actions["target"][target].trigger()
                        self.assertEqual(observations, ["", ""])
                        self.assertEqual(popup.spin.text(), "Starting…")
                        self.assertEqual(self.workers[-1].text, "Original source passage")
                        self.workers[-1].complete(translation)
                        self.assertEqual(popup.result.toPlainText(), translation)

    def test_nested_language_change_during_save_starts_only_latest_request(self):
        for floating in (False, True):
            with self.subTest(floating=floating):
                popup = self.popup(floating=floating, settings={"target_language": "zh"})
                self.workers[-1].complete(TRANSLATIONS["zh"])

                def save_language(source, target, origin):
                    if target == "hi":
                        origin._language_actions["target"]["ko"].trigger()

                popup._language_change_callback = save_language
                count = len(self.workers)
                popup._language_actions["target"]["hi"].trigger()
                self.assertEqual(len(self.workers), count + 1)
                self.assertEqual(self.workers[-1]._settings["target_language"], "ko")

    def test_nested_language_change_during_audio_reset_starts_only_latest_request(self):
        popup = self.popup(settings={"target_language": "zh"})
        self.workers[-1].complete(TRANSLATIONS["zh"])
        reset_audio = popup._reset_speech_for_new_translation

        def reset_and_select_new_language():
            with mock.patch.object(popup, "_reset_speech_for_new_translation", reset_audio):
                popup._language_actions["target"]["ko"].trigger()
            reset_audio()

        count = len(self.workers)
        with mock.patch.object(popup, "_reset_speech_for_new_translation",
                               side_effect=reset_and_select_new_language):
            popup._language_actions["target"]["hi"].trigger()
        self.assertEqual(len(self.workers), count + 1)
        self.assertEqual(self.workers[-1]._settings["target_language"], "ko")

    def test_language_change_during_ocr_cancels_old_request_immediately(self):
        popup = self.popup(floating=True, pil_image=object(),
                           settings={"selection_mode": "ocr"})
        old = self.workers[-1]
        popup._language_actions["target"]["fr"].trigger()
        self.assertTrue(old.cancelled)
        self.assertEqual(len(self.workers), 2)
        self.assertEqual(self.workers[-1]._settings["target_language"], "fr")
        old.source_ready.emit("stale OCR")
        old.partial.emit("stale Arabic output")
        old.complete("stale Arabic output")
        old.failed.emit("stale failure")
        self.assertEqual(popup.result.toPlainText(), "")
        self.assertIsNone(popup._source_text)

    def test_refresh_does_not_share_mutable_language_settings_with_owner(self):
        popup = self.popup()
        self.workers[-1].complete("translated output")
        shared = dict(popup.settings)
        popup.refresh_display(shared)
        shared["target_language"] = "fr"
        popup.refresh_display(shared)
        self.assertEqual(self.workers[-1]._settings["target_language"], "fr")

    def test_display_refresh_while_retranslating_cannot_restore_old_language(self):
        popup = self.popup()
        self.workers[-1].complete("old Arabic translation")
        popup.sync_languages("en", "fr")
        popup.refresh_display(dict(popup.settings, font_size=25))
        self.assertEqual(popup.result.toPlainText(), "")
        self.assertEqual(popup._translation_text, "")

    def test_repeated_sync_does_not_send_duplicate_request(self):
        popup = self.popup()
        popup.sync_languages("en", "fr")
        count = len(self.workers)
        current = self.workers[-1]
        popup.sync_languages("en", "fr")
        self.assertEqual(len(self.workers), count)
        self.assertFalse(current.cancelled)

    def test_rapid_image_language_changes_accept_only_latest_result(self):
        for floating in (False, True):
            with self.subTest(floating=floating):
                image = object()
                popup = self.popup(floating=floating, pil_image=image,
                                   settings={"selection_mode": "image"})
                initial = self.workers[-1]
                popup._language_actions["target"]["fr"].trigger()
                french = self.workers[-1]
                popup._language_actions["target"]["de"].trigger()
                german = self.workers[-1]
                self.assertIs(german.image, image)
                self.assertEqual(german._settings["target_language"], "de")
                german.complete("Aktuelle Übersetzung")
                french.partial.emit("ancienne traduction")
                french.complete("ancienne traduction")
                initial.complete("old Arabic translation")
                self.assertEqual(popup.result.toPlainText(), "Aktuelle Übersetzung")

    def test_reader_settings_poll_updates_open_popup_language(self):
        from main import MainWindow

        popup = self.popup()
        old = dict(popup.settings)
        owner = SimpleNamespace(
            settings=old, popups=[popup], _chat_panel=None,
            _keep_live_theme_preview=lambda value: value,
            _sync_language_combos=mock.Mock(),
            _sync_selection_mode_button=mock.Mock(),
            _refresh_engine_label=mock.Mock(),
            _cancel_automatic_page_prefetch=mock.Mock(),
        )
        with mock.patch("main.load", return_value=dict(old, target_language="fr")):
            MainWindow._reload_settings(owner)
        self.assertEqual(self.workers[-1]._settings["target_language"], "fr")
        owner._cancel_automatic_page_prefetch.assert_called_once()

    def test_float_settings_poll_maps_float_language_to_open_popup(self):
        from main_02 import FloatButton

        popup = self.popup(floating=True)
        old = dict(popup.settings, float_source_language="en",
                   float_target_language="ar")
        owner = SimpleNamespace(
            settings=old, popups=[popup], _settings_load_error_logged=False,
            _sync_selection_mode_actions=mock.Mock(),
            _sync_explain_action=mock.Mock(),
        )
        owner._popup_settings = lambda: FloatButton._popup_settings(owner)
        with mock.patch("main_02.load", return_value=dict(old, float_target_language="fr")):
            FloatButton._reload_settings(owner)
        self.assertEqual(self.workers[-1]._settings["target_language"], "fr")

    def test_popup_callback_updates_both_open_windows_once(self):
        from main import MainWindow
        from main_02 import FloatButton

        for floating in (False, True):
            with self.subTest(floating=floating):
                origin = self.popup(floating=floating)
                other = self.popup(floating=floating)
                settings = dict(origin.settings, float_source_language="en",
                                float_target_language="ar")
                owner = SimpleNamespace(
                    settings=settings, popups=[origin, other],
                    _automatic_page_reading_popup=None,
                    _sync_language_combos=mock.Mock(),
                    _cancel_automatic_page_prefetch=mock.Mock(),
                )
                callback = (FloatButton._save_float_languages if floating
                            else MainWindow._on_popup_languages_changed)
                origin._language_change_callback = (
                    lambda source, target, popup: callback(owner, source, target, popup))
                count = len(self.workers)
                with mock.patch("main_02.save" if floating else "main.save"):
                    origin._language_actions["target"]["fr"].trigger()
                self.assertEqual(len(self.workers), count + 2)
                for popup in (origin, other):
                    self.assertEqual(popup._worker._settings["target_language"], "fr")
                    self.assertEqual(popup.target_language_btn.text(), "To: Français")
                key = "float_target_language" if floating else "target_language"
                self.assertEqual(owner.settings[key], "fr")
                if floating:
                    self.assertEqual(owner.settings["target_language"], "ar")
                else:
                    owner._cancel_automatic_page_prefetch.assert_called_once()

    def test_toolbar_change_discards_next_page_translation_in_old_language(self):
        from main import MainWindow

        popup = self.popup()
        owner = SimpleNamespace(
            settings=dict(popup.settings, target_language="fr"), popups=[popup],
            _cancel_automatic_page_prefetch=mock.Mock(),
        )
        MainWindow._sync_popup_languages(owner)
        owner._cancel_automatic_page_prefetch.assert_called_once()
        self.assertEqual(self.workers[-1]._settings["target_language"], "fr")

    def test_prefetched_image_retains_source_for_language_change(self):
        popup = self.popup(settings={"selection_mode": "image"})
        image = object()
        popup.set_pretranslated_content("old Arabic output", "mock", pil_image=image)
        count = len(self.workers)
        popup._language_actions["target"]["fr"].trigger()
        self.assertEqual(len(self.workers), count + 1)
        self.assertIs(self.workers[-1].image, image)
        self.assertEqual(self.workers[-1]._settings["target_language"], "fr")


class TestLanguageSwitchRealWorker(_GuiCase):
    def test_reused_popup_passes_each_language_to_text_and_image_requests(self):
        from popup.translate_popup import TranslatePopup

        for floating in (False, True):
            for image_mode in (False, True):
                with self.subTest(floating=floating, image=image_mode):
                    requests = []

                    def respond(content, settings):
                        pair = (settings["source_language"], settings["target_language"])
                        requests.append((content, pair))
                        yield "partial", TRANSLATIONS[pair[1]], "mock"
                        yield "done", TRANSLATIONS[pair[1]], "mock"

                    content = object() if image_mode else "Original source passage"
                    kwargs = ({"pil_image": content} if image_mode
                              else {"source_text": content})
                    settings = dict(DEFAULTS, source_language="en", target_language="ar",
                                    selection_mode="image" if image_mode else "ocr")
                    with (
                        mock.patch("translators.chain.translate_stream", side_effect=respond),
                        mock.patch("translators.chain.translate_image_stream", side_effect=respond),
                    ):
                        popup = TranslatePopup(
                            settings, floating_language_controls=True,
                            float_language_preferences=floating, **kwargs)
                        try:
                            for target in TRANSLATIONS:
                                if target != "ar":
                                    popup._language_actions["target"][target].trigger()
                                self.assertTrue(popup._worker.wait(3000))
                                self.app.processEvents()
                                self.assertEqual(popup.result.toPlainText(), TRANSLATIONS[target])
                            self.assertEqual(
                                requests,
                                [(content, ("en", target)) for target in TRANSLATIONS],
                            )
                        finally:
                            for worker in popup._translation_workers:
                                worker.requestInterruption()
                                worker.wait(3000)
                            self.app.processEvents()
                            popup.close()
