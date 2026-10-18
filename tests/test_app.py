"""Offline unit tests: chunking, glossary, PDF rect extraction. No network."""
import atexit
import contextlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from config.settings import DEFAULTS
from reader import pdf_utils
from translators import chain, fallback


_PDF_SCRATCH = None


def _pdf_scratch_dir() -> str:
    """A private directory for this process's generated PDFs, made once.

    The old scheme wrote every PDF to a fixed `%TEMP%\\<name>` and never
    deleted one: ~30 files accumulate per suite run, and a stale file from a
    killed run is silently reused. On Windows that is worse than merely stale —
    pymupdf may still hold the old file open, and `doc.save()` onto a locked path
    fails.

    One `mkdtemp()` per process, removed at exit, gives every file a unique path
    and guarantees cleanup without threading `addCleanup` through ~30 call sites
    that are plain module-level helpers.
    """
    global _PDF_SCRATCH
    if _PDF_SCRATCH is None:
        _PDF_SCRATCH = tempfile.mkdtemp(prefix="harmony-pdf-")
        atexit.register(shutil.rmtree, _PDF_SCRATCH, True)
    return _PDF_SCRATCH


def _make_pdf_file(name, pages=1):
    """Write a throwaway PDF under this run's private scratch directory.

    `name` is kept only to make a failing path recognisable; it no longer
    decides where the file lands.
    """
    import pymupdf
    tmp = os.path.join(_pdf_scratch_dir(), name)
    doc = pymupdf.open()
    try:
        for n in range(pages):
            pg = doc.new_page()
            pg.insert_text((72, 100), f"memory page {n}")
        doc.save(tmp)
    finally:
        doc.close()
    return tmp


class _TempSettingsCase(unittest.TestCase):
    """Gives every test its own settings file, and its own scratch directory.

    The old scheme was
    `Path(tempfile.gettempdir()) / f"ht_test_{self._testMethodName}.json"`, which
    had three problems:

    * the name was keyed only on the method name, so two classes with a method
      of the same name shared one file;
    * it was not run-qualified, so a leftover from a killed run was adopted by
      the next one, silently;
    * it lived in the machine-wide `%TEMP%`, where another user or another
      project can see it.

    `mkdtemp()` gives a unique directory per test, and `addCleanup` removes the
    whole thing afterwards — including the `…json.tmp` sibling that
    `config.settings.save()` leaves behind on a partial write, which a bare
    `unlink()` of the .json would have missed. This is the same pattern
    `mock_opencode_settings` already used.
    """

    def setUp(self):
        from unittest import mock
        self._scratch = tempfile.mkdtemp(prefix="harmony-test-")
        self.addCleanup(shutil.rmtree, self._scratch, True)
        self._settings_tmp = Path(self._scratch) / "settings.json"
        self._patcher = mock.patch("config.settings.SETTINGS_PATH",
                                   self._settings_tmp)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)


class _GuiCase(_TempSettingsCase):
    """A test case that owns a QApplication but not its widgets.

    Only one `QApplication` may exist per process, so it cannot be created and
    destroyed per class. What *can* — and must — be destroyed is every widget,
    event filter, timer and thread the class left behind. One application
    accumulating all nine GUI classes' worth of live widgets is what makes the
    process fault on exit: a `QThread` still running when its `QObject` is
    collected is an access violation, not an exception.

    This does not depend on the production code tearing itself down. A widget
    whose own `closeEvent` forgets to stop its worker still gets closed and
    deleted here; only the *test* harness has to be right.
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        # QApplication.instance() first: creating a second one aborts.
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        """Close and delete everything this class created."""
        app = getattr(cls, "app", None)
        if app is None:
            return
        from PySide6.QtCore import QThread, QTimer

        def _pump():
            # Let close/deleteLater and finished-signal handlers actually run;
            # without this, deleteLater() is only a request.
            for _ in range(5):
                app.processEvents()

        def _stop_threads():
            for thread in QThread.allThreads() if hasattr(QThread, "allThreads") \
                    else _iter_threads():
                if thread is not app.thread() and thread.isRunning():
                    thread.quit()
                    if not thread.wait(5000):
                        thread.terminate()
                        thread.wait(2000)

        def _iter_threads():
            # QThread.allThreads() was removed in Qt6; walk the object tree.
            from PySide6.QtCore import QCoreApplication
            stack, seen = [QCoreApplication.instance()], []
            while stack:
                obj = stack.pop()
                for child in obj.children():
                    if isinstance(child, QThread):
                        seen.append(child)
                    stack.append(child)
            return seen

        # Any worker still running must be stopped before its widget dies,
        # or the widget is destroyed with a live thread inside it.
        _stop_threads()
        for widget in app.topLevelWidgets():
            try:
                widget.close()
            except RuntimeError:
                pass          # already deleted by an earlier close
        _pump()
        _stop_threads()
        for widget in app.topLevelWidgets():
            try:
                widget.deleteLater()
            except RuntimeError:
                pass
        _pump()
        _stop_threads()
        # Timers are not widgets and never appear in topLevelWidgets(); stop the
        # ones a class left ticking, or they fire into deleted C++ objects.
        for obj in _all_qobjects():
            try:
                if isinstance(obj, QTimer) and obj.isActive():
                    obj.stop()
            except RuntimeError:
                pass          # already destroyed
        _pump()


def _all_qobjects():
    """Every QObject reachable from the application instance."""
    from PySide6.QtCore import QCoreApplication
    root = QCoreApplication.instance()
    if root is None:
        return []
    stack, out = [root], []
    while stack:
        obj = stack.pop()
        out.append(obj)
        try:
            stack.extend(obj.children())
        except RuntimeError:
            pass          # a C++ object deleted underneath us
    return out


class TestCustomProviderAutomaticModelFetch(_GuiCase):
    @staticmethod
    def _dialog():
        from main import SettingsDialog

        provider = {
            "id": "local",
            "name": "Local Ollama",
            "base_url": "",
            "api_key": "",
            "model": "",
            "enabled": True,
            "reasoning_enabled": False,
            "vision_enabled": False,
        }
        return SettingsDialog(dict(
            DEFAULTS,
            model="custom:local",
            custom_providers=[provider],
        )), provider

    def _fill_credentials(self, dialog):
        card = next(
            card for card in dialog._custom_cards
            if card.property("cp_id") == "local"
        )
        card._cp_base.setText("http://localhost:11434/v1")
        card._cp_key.setText("ollama")
        return card

    def _wait_for(self, dialog, condition):
        import time
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.01)

    def test_entering_base_url_and_key_populates_models_automatically(self):
        from unittest import mock

        dialog, provider = self._dialog()
        self.addCleanup(dialog.close)
        card = next(
            card for card in dialog._custom_cards
            if card.property("cp_id") == "local"
        )

        with mock.patch(
            "translators.opencode_compat.list_model_groups",
            return_value=[("", [
                    "gemma2:2b",
                    "qwen3-vl:2b-instruct",
                    "mistral:pixtral-12b-2409",
                    "vendor/example-free",
                ])],
        ) as list_models:
            self._fill_credentials(dialog)
            self._wait_for(dialog, lambda: card._cp_model.count() >= 5)

        self.assertEqual(list_models.call_count, 1)
        self.assertEqual(
            [
                card._cp_model.itemData(i)
                for i in range(card._cp_model.count())
                if card._cp_model.itemData(i)
            ],
            [
                "gemma2:2b",
                "qwen3-vl:2b-instruct",
                "mistral:pixtral-12b-2409",
                "vendor/example-free",
            ],
        )
        self.assertEqual(provider["model"], "gemma2:2b")
        from ui.settings_dialog import (
            _ACTIVE_MODEL_ROLE, _FREE_MODEL_ROLE, _IMAGE_CAPABILITY_ROLE,
            _MODEL_GROUP_ROLE,
        )
        combo = card._cp_model
        self.assertEqual(combo.itemText(0), "Models from Local Ollama")
        self.assertEqual(
            combo.itemData(0, _MODEL_GROUP_ROLE),
            "Models from Local Ollama",
        )
        self.assertTrue(combo.itemData(combo.findData("gemma2:2b"), _ACTIVE_MODEL_ROLE))
        self.assertEqual(
            combo.itemData(
                combo.findData("mistral:pixtral-12b-2409"),
                _IMAGE_CAPABILITY_ROLE,
            ),
            "unknown",
        )
        card._cp_vision.setChecked(True)
        self.assertEqual(
            combo.itemData(
                combo.findData("mistral:pixtral-12b-2409"),
                _IMAGE_CAPABILITY_ROLE,
            ),
            "unknown",
        )
        self.assertTrue(
            combo.itemData(combo.findData("vendor/example-free"), _FREE_MODEL_ROLE)
        )
        self.assertGreaterEqual(combo.view().font().pixelSize(), 16)
        combo.setCurrentIndex(combo.findData("mistral:pixtral-12b-2409"))
        self.assertTrue(combo.itemData(
            combo.currentIndex(), _ACTIVE_MODEL_ROLE))
        self.assertFalse(combo.itemData(
            combo.findData("gemma2:2b"), _ACTIVE_MODEL_ROLE))
        card._cp_name.setText("Kilo Code")
        self.assertEqual(combo.itemText(0), "Models from Kilo Code")

    def test_endpoint_model_groups_are_preserved(self):
        from unittest import mock
        import requests
        from translators import opencode_compat

        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": [
            {"id": "vendor/free-model", "group": "Free"},
            {"id": "vendor/balanced-model", "tier": "Balanced"},
            {"id": "vendor/second-free", "category": "Free"},
            {
                "id": "vendor/zero-priced",
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {
                "id": "vendor/paid",
                "pricing": {"prompt": "0.0001", "completion": "0.0002"},
            },
            {
                "id": "vendor/paid-model-free",
                "pricing": {"prompt": "0.0001", "completion": "0.0002"},
            },
            {"id": "vendor/free-flag", "is_free": True},
        ]}
        explicit_free = set()
        with mock.patch.object(requests, "get", return_value=response):
            self.assertEqual(
                opencode_compat.list_model_groups(
                    "https://x/v1", "k",
                    explicitly_free_models=explicit_free,
                ),
                [
                    ("Free", ["vendor/free-model"]),
                    ("Balanced", ["vendor/balanced-model"]),
                    ("Free", ["vendor/second-free"]),
                    ("", [
                        "vendor/zero-priced", "vendor/paid",
                        "vendor/paid-model-free", "vendor/free-flag",
                    ]),
                ],
            )
            self.assertEqual(
                opencode_compat.free_model_groups(
                    [
                        ("Free", ["vendor/free-model"]),
                        ("Balanced", ["vendor/balanced-model"]),
                        ("", [
                            "vendor/zero-priced", "vendor/paid",
                            "vendor/paid-model-free", "vendor/free-flag",
                        ]),
                        ("", ["vendor/space-bunny-free"]),
                    ],
                    explicit_free,
                ),
                [
                    ("Free", ["vendor/free-model"]),
                    (
                        "Provider-marked free",
                        ["vendor/zero-priced", "vendor/free-flag"],
                    ),
                ],
            )
            self.assertEqual(
                opencode_compat.list_models("https://x/v1", "k"),
                [
                    "vendor/free-model",
                    "vendor/balanced-model",
                    "vendor/second-free",
                    "vendor/zero-priced",
                    "vendor/paid",
                    "vendor/paid-model-free",
                    "vendor/free-flag",
                ],
            )

    def test_free_model_classification_uses_provider_indicators(self):
        from unittest import mock
        from translators import opencode_compat

        groups = [
            ("", ["vendor/model-free", "vendor/paid-model"]),
            ("Free tier", ["vendor/model-in-free-group"]),
        ]
        self.assertEqual(
            opencode_compat.free_model_groups(groups, {
                "vendor/model-free", "vendor/model-in-free-group",
            }),
            [
                ("Provider-marked free", ["vendor/model-free"]),
                ("Free tier", ["vendor/model-in-free-group"]),
            ],
        )

    def test_provider_model_check_uses_modern_token_limit_and_timeout(self):
        from unittest import mock
        import requests
        from translators import opencode_compat

        def response_for_model(*_args, **kwargs):
            response = mock.Mock()
            if kwargs["json"]["model"] == "vendor/broken":
                response.raise_for_status.side_effect = requests.HTTPError(
                    "model unavailable"
                )
            else:
                response.raise_for_status.return_value = None
                response.json.return_value = {
                    "choices": [{"message": {"content": "OK"}}]
                }
            return response

        with mock.patch(
            "translators.opencode_compat.requests.post",
            side_effect=response_for_model,
        ) as post:
            results = opencode_compat.probe_models(
                "https://provider/v1",
                "secret",
                ["vendor/working", "vendor/broken", "vendor/working"],
                max_workers=2,
            )

        self.assertEqual(
            [(model, ok) for model, ok, _ in results],
            [("vendor/working", True), ("vendor/broken", False)],
        )
        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            self.assertEqual(
                call.kwargs["json"]["max_completion_tokens"], 256
            )
            self.assertEqual(call.kwargs["timeout"], 25)

    def test_provider_model_check_falls_back_for_unsupported_token_parameter(self):
        from unittest import mock
        import requests
        from translators import opencode_compat

        modern_response = mock.Mock()
        modern_response.text = (
            '{"error":"Unsupported parameter: max_completion_tokens"}'
        )
        modern_response.raise_for_status.side_effect = requests.HTTPError(
            "400 Bad Request", response=modern_response
        )
        legacy_response = mock.Mock()
        legacy_response.raise_for_status.return_value = None
        legacy_response.json.return_value = {
            "choices": [{"message": {"content": "OK"}}]
        }

        with mock.patch(
            "translators.opencode_compat.requests.post",
            side_effect=[modern_response, legacy_response],
        ) as post:
            results = opencode_compat.probe_models(
                "https://provider/v1", "secret", ["vendor/model"]
            )

        self.assertEqual(results[0][1], True)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            post.call_args_list[0].kwargs["json"]["max_completion_tokens"],
            256,
        )
        self.assertNotIn(
            "max_completion_tokens",
            post.call_args_list[1].kwargs["json"],
        )
        self.assertEqual(post.call_args_list[1].kwargs["json"]["max_tokens"], 256)

    def test_provider_model_check_accepts_nonempty_truncated_probe_reply(self):
        from unittest import mock
        from translators import opencode_compat

        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{
                "finish_reason": "length",
                "message": {"content": "O"},
            }]
        }
        with mock.patch(
            "translators.opencode_compat.requests.post",
            return_value=response,
        ):
            result = opencode_compat.probe_models(
                "https://provider/v1", "secret", ["vendor/model"]
            )[0]

        self.assertTrue(result[1])

    def test_provider_model_check_marks_transient_errors_unverified(self):
        from unittest import mock
        import requests
        from translators import opencode_compat

        response = mock.Mock()
        response.status_code = 429
        response.text = '{"error":"rate limit"}'
        error = requests.HTTPError("429 Too Many Requests", response=response)
        with mock.patch(
            "translators.opencode_compat.requests.post",
            side_effect=error,
        ):
            results = opencode_compat.probe_models(
                "https://provider/v1", "secret", ["vendor/model"]
            )

        self.assertFalse(results[0][1])
        self.assertIn("TEMPORARY", results[0][2])
        self.assertIn("429", results[0][2])

    def test_provider_model_check_reports_free_and_working_status(self):
        from unittest import mock

        dialog, _provider = self._dialog()
        self.addCleanup(dialog.close)
        card = self._fill_credentials(dialog)
        dialog._auto_fetch_last["local"] = (
            "http://localhost:11434/v1", "ollama"
        )
        groups = [("", ["vendor/model-free", "vendor/paid-model"])]

        def listed_models(_base, _key, explicitly_free_models):
            explicitly_free_models.add("vendor/model-free")
            return groups

        with (
            mock.patch(
                "translators.opencode_compat.list_model_groups",
                side_effect=listed_models,
            ) as get_models,
            mock.patch(
                "translators.opencode_compat.probe_models",
                return_value=[
                    ("vendor/model-free", True, "0.1s"),
                    ("vendor/paid-model", False, "0.3s — 404 model not found"),
                    (
                        "vendor/slow-model", False,
                        "8.0s — TEMPORARY — 429 rate limited",
                    ),
                ],
            ) as probe,
        ):
            card._cp_check_models_btn.click()
            self._wait_for(
                dialog,
                lambda: "vendor/paid-model"
                in dialog.results.toPlainText(),
            )

        rendered = dialog.results.toPlainText()
        self.assertIn("WORKING (1)", rendered)
        self.assertIn("NOT WORKING (1)", rendered)
        self.assertIn("RETRY / TEMPORARY (1)", rendered)
        self.assertIn("vendor/model-free", rendered)
        self.assertIn("vendor/paid-model", rendered)
        self.assertIn("vendor/slow-model", rendered)
        self.assertIn("0.1s", rendered)
        self.assertIn("0.3s — 404 model not found", rendered)
        self.assertNotIn("WORKS  [", rendered)
        markup = dialog.results.toHtml()
        self.assertIn("font-size:25px", markup.replace(" ", ""))
        self.assertIn("\u25cf", markup)
        self.assertIn("FREE", rendered)
        self.assertIn(
            "does not confirm subscription or card requirements", rendered
        )
        self.assertEqual(get_models.call_count, 1)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(
            probe.call_args.args[2],
            ["vendor/model-free", "vendor/paid-model"],
        )
        self.assertTrue(card._cp_check_models_btn.isEnabled())

    def test_model_check_button_is_available_on_every_provider_card(self):
        from unittest import mock

        dialog, _provider = self._dialog()
        self.addCleanup(dialog.close)
        dialog._settings["custom_providers"].append({
            "id": "second",
            "name": "Second Provider",
            "base_url": "",
            "api_key": "",
            "model": "",
            "enabled": True,
            "reasoning_enabled": False,
            "vision_enabled": False,
        })
        dialog._rebuild_custom_cards()
        cards = {
            card.property("cp_id"): card
            for card in dialog._custom_cards
            if card.property("cp_id")
        }
        self.assertEqual(set(cards), {"local", "second"})
        second_card = cards["second"]
        second_card._cp_base.setText("https://second.example/v1")
        second_card._cp_key.setText("key")
        dialog._auto_fetch_last["second"] = (
            "https://second.example/v1", "key"
        )
        with (
            mock.patch(
                "translators.opencode_compat.list_model_groups",
                return_value=[("", ["second/model"])],
            ),
            mock.patch(
                "translators.opencode_compat.probe_models",
                return_value=[("second/model", True, "0.4s")],
            ) as probe,
        ):
            second_card._cp_check_models_btn.click()
            self._wait_for(
                dialog,
                lambda: "Second Provider" in dialog.results.toPlainText(),
            )

        self.assertIn("second/model", dialog.results.toPlainText())
        self.assertIn("0.4s", dialog.results.toPlainText())
        self.assertTrue(all(
            card._cp_check_models_btn.isEnabled()
            for card in cards.values()
        ))
        self.assertEqual(probe.call_args.args[0], "https://second.example/v1")

    def test_automatic_fetch_error_is_reported_and_can_be_retried(self):
        from unittest import mock

        dialog, _provider = self._dialog()
        self.addCleanup(dialog.close)
        card = next(
            card for card in dialog._custom_cards
            if card.property("cp_id") == "local"
        )

        with mock.patch(
            "translators.opencode_compat.list_model_groups",
            side_effect=RuntimeError("endpoint unavailable"),
        ) as list_models:
            self._fill_credentials(dialog)
            self._wait_for(
                dialog,
                lambda: "endpoint unavailable" in dialog.results.toPlainText(),
            )

        self.assertEqual(list_models.call_count, 1)
        self.assertTrue(card._cp_fetch_btn.isEnabled())

    def test_gemma2_does_not_advertise_unsupported_reasoning(self):
        dialog, provider = self._dialog()
        self.addCleanup(dialog.close)
        provider["model"] = "gemma2:2b"
        provider["reasoning_enabled"] = True
        dialog._rebuild_custom_cards()
        card = next(
            card for card in dialog._custom_cards
            if card.property("cp_id") == "local"
        )

        self.assertFalse(card._cp_reasoning.isEnabled())
        self.assertFalse(card._cp_reasoning.isChecked())
        self.assertFalse(dialog.values()["custom_providers"][0][
            "reasoning_enabled"])

    def test_custom_provider_controls_and_status_messages_use_larger_layout(self):
        from PySide6.QtWidgets import (
            QFrame, QGroupBox, QLabel, QPushButton, QWidget,
        )

        dialog, _provider = self._dialog()
        self.addCleanup(dialog.close)
        card = next(
            card for card in dialog._custom_cards
            if card.property("cp_id") == "local"
        )

        self.assertEqual(card.objectName(), "customProviderCard")
        self.assertLessEqual(card._cp_name.minimumHeight(), 38)
        self.assertLessEqual(card._cp_model.minimumHeight(), 38)
        self.assertIn("min-height: 30px", card.styleSheet())
        self.assertIn("border-radius: 8px", card.styleSheet())
        self.assertIn("font-size: 18px", dialog.results.styleSheet())
        self.assertIn("Refresh", card._cp_fetch_btn.text())
        self.assertEqual(dialog._toggle_reasoning_all_btn.minimumHeight(), 28)
        self.assertEqual(dialog._toggle_vision_all_btn.minimumHeight(), 28)
        self.assertEqual(dialog._add_custom_btn.minimumHeight(), 28)
        self.assertEqual(dialog._fetch_models_btn.minimumHeight(), 28)
        stripe = card.findChild(QFrame, "providerAccentStripe")
        self.assertEqual(stripe.width(), 7)
        self.assertIn("border-radius: 0px", stripe.styleSheet())
        remove_button = card.findChild(QPushButton, "providerRemoveButton")
        self.assertEqual(remove_button._symbol, "×")
        self.assertEqual(remove_button.accessibleName(), "Remove provider 'Local Ollama'")
        self.assertIn("min-width: 34px", card.styleSheet())
        self.assertIn("min-height: 30px", card.styleSheet())
        self.assertIn("providerRemoveButton", card.styleSheet())
        self.assertEqual(card._cp_collapse._symbol, "−")
        self.assertEqual(card._cp_collapse.accessibleName(), "Hide provider settings")
        self.assertIn(
            "QCheckBox#providerActiveCheck::indicator",
            card.styleSheet(),
        )
        self.assertIn("border-radius: 6px", card.styleSheet())
        self.assertIn("indicator:checked", card.styleSheet())
        self.assertIn("background: #00E676", card.styleSheet())
        self.assertIn("indicator:unchecked", card.styleSheet())
        self.assertIn("background: #FF3B30", card.styleSheet())
        self.assertTrue(card._cp_enabled.isChecked())
        card._cp_enabled.setChecked(False)
        self.assertFalse(card._cp_enabled.isChecked())
        groups = dialog.findChildren(QGroupBox, "customProvidersGroup")
        self.assertEqual(len(groups), 1)
        self.assertIn("border: none", groups[0].styleSheet())
        intro = dialog.findChild(QWidget, "customProvidersIntro")
        self.assertIsNotNone(intro)
        self.assertEqual(
            intro.findChild(QLabel, "customProvidersTitle").text(),
            "Custom Providers",
        )
        self.assertIsNone(intro.findChild(QLabel, "customProvidersHint"))

    def test_custom_provider_capabilities_share_row_and_card_can_collapse(self):
        from PySide6.QtWidgets import QLabel

        dialog, _provider = self._dialog()
        self.addCleanup(dialog.close)
        card = next(
            card for card in dialog._custom_cards
            if card.property("cp_id") == "local"
        )
        collapsed_name = card.findChild(QLabel, "providerCollapsedName")

        self.assertIs(card._cp_reasoning.parentWidget(), card._cp_capabilities)
        self.assertIs(card._cp_vision.parentWidget(), card._cp_capabilities)
        self.assertNotIn(
            "custom:local",
            [label.text() for label in card.findChildren(QLabel)],
        )
        self.assertFalse(card._cp_details.isHidden())
        self.assertTrue(collapsed_name.isHidden())
        self.assertEqual(
            card._cp_content_layout.contentsMargins().top(), 13)

        card._cp_collapse.setChecked(True)
        self.assertTrue(card._cp_details.isHidden())
        self.assertFalse(collapsed_name.isHidden())
        self.assertEqual(
            card._cp_content_layout.contentsMargins().top(), 7)
        self.assertEqual(
            card._cp_content_layout.contentsMargins().bottom(), 7)
        self.assertEqual(collapsed_name.text(), "Local Ollama")
        self.assertEqual(card._cp_collapse._symbol, "+")
        self.assertEqual(
            card._cp_collapse.accessibleName(), "Show provider settings")

        card._cp_collapse.setChecked(False)
        self.assertFalse(card._cp_details.isHidden())
        self.assertTrue(collapsed_name.isHidden())
        self.assertEqual(
            card._cp_content_layout.contentsMargins().top(), 13)
        self.assertEqual(
            card._cp_content_layout.contentsMargins().bottom(), 15)

    def test_custom_provider_models_support_persisted_favorites(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from ui.settings_dialog import (
            _FAVORITE_KEY_ROLE,
            _FAVORITE_MODEL_ROLE,
            _MODEL_GROUP_ROLE,
            _custom_model_favorite_key,
        )

        dialog, _provider = self._dialog()
        self.addCleanup(dialog.close)
        card = next(
            card for card in dialog._custom_cards
            if card.property("cp_id") == "local"
        )
        combo = card._cp_model
        dialog._populate_custom_model_combo(
            combo,
            "local",
            "Local Ollama",
            ["gemma2:2b", "qwen3-vl:2b-instruct-free"],
            "gemma2:2b",
        )
        free_index = combo.findData("qwen3-vl:2b-instruct-free")
        favorite_key = _custom_model_favorite_key(
            "local", "qwen3-vl:2b-instruct-free")

        self.assertEqual(combo.itemText(0), "Models from Local Ollama")
        self.assertEqual(
            combo.itemData(free_index, _FAVORITE_KEY_ROLE), favorite_key)
        self.assertFalse(combo.itemData(free_index, _FAVORITE_MODEL_ROLE))

        dialog.show()
        combo.showPopup()
        view = combo.view()
        row = view.visualRect(combo.model().index(free_index, 0))
        QTest.mouseClick(
            view.viewport(),
            Qt.MouseButton.LeftButton,
            pos=QPoint(row.right() - 14, row.center().y()),
        )
        QTest.qWait(20)
        self.assertEqual(
            dialog.values()["custom_provider_model_favorites"],
            [favorite_key],
        )
        self.assertEqual(combo.itemText(0), "Favorites")
        self.assertEqual(
            combo.itemData(0, _MODEL_GROUP_ROLE), "Favorites")
        favorite_index = combo.findData("qwen3-vl:2b-instruct-free")
        self.assertTrue(combo.itemData(
            favorite_index, _FAVORITE_MODEL_ROLE))
        self.assertEqual(combo.currentText(), "gemma2:2b")

        dialog._on_custom_model_favorite_clicked(
            "local", combo, favorite_key)
        self.assertEqual(
            dialog.values()["custom_provider_model_favorites"], [])
        self.assertEqual(combo.itemText(0), "Models from Local Ollama")

    def test_model_picker_delegate_event_filter_handles_teardown(self):
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QComboBox
        from ui.settings_dialog import _ModelPickerDelegate

        combo = QComboBox()
        delegate = _ModelPickerDelegate(combo, favorites_enabled=True)
        del delegate._favorites_enabled

        event = QEvent(QEvent.Type.MouseButtonRelease)
        self.assertFalse(delegate.eventFilter(combo.view().viewport(), event))

    def test_model_picker_ignores_wheel_until_left_clicked(self):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtTest import QTest

        dialog, _provider = self._dialog()
        self.addCleanup(dialog.close)
        card = next(
            card for card in dialog._custom_cards
            if card.property("cp_id") == "local"
        )
        combo = card._cp_model
        dialog._populate_custom_model_combo(
            combo, "local", "Local Ollama",
            ["model-one", "model-two"], "model-one")
        original_index = combo.currentIndex()
        combo.clearFocus()
        combo._wheel_armed = False
        self.app.processEvents()

        def wheel_event():
            return QWheelEvent(
                QPointF(10, 10), QPointF(10, 10),
                QPoint(), QPoint(0, 120),
                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase, False,
            )

        wheel = wheel_event()
        self.app.sendEvent(combo, wheel)
        self.assertTrue(wheel.isAccepted())
        self.assertEqual(combo.currentIndex(), original_index)

        line_wheel = wheel_event()
        self.app.sendEvent(combo.lineEdit(), line_wheel)
        self.assertTrue(line_wheel.isAccepted())
        self.assertEqual(combo.currentIndex(), original_index)
        self.assertFalse(combo._wheel_armed)

        QTest.mouseClick(combo.lineEdit(), Qt.MouseButton.LeftButton)
        self.assertTrue(combo._wheel_armed)

    def test_model_picker_renders_model_name_as_own_highlighted_chip(self):
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QColor, QImage, QPainter
        from PySide6.QtWidgets import (
            QComboBox, QStyle, QStyleOptionViewItem,
        )
        from ui.settings_dialog import (
            _ACTIVE_MODEL_ROLE, _FREE_MODEL_ROLE, _IMAGE_CAPABILITY_ROLE,
            _ModelPickerDelegate,
        )

        combo = QComboBox()
        combo.addItem("vendor/example-model-free", "vendor/example-model-free")
        combo.setItemData(0, True, _ACTIVE_MODEL_ROLE)
        combo.setItemData(0, True, _FREE_MODEL_ROLE)
        combo.setItemData(0, "supported", _IMAGE_CAPABILITY_ROLE)
        delegate = _ModelPickerDelegate(combo)
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 600, 32)
        option.widget = combo.view()
        option.font = combo.font()
        option.palette = combo.palette()
        option.state = QStyle.StateFlag.State_Enabled
        image = QImage(600, 32, QImage.Format.Format_ARGB32)
        image.fill(QColor("white"))
        painter = QPainter(image)
        delegate.paint(painter, option, combo.model().index(0, 0))
        painter.end()

        self.assertEqual(image.pixelColor(16, 16).name(), "#312e81")
        self.assertTrue(combo.itemData(0, _FREE_MODEL_ROLE))
        self.assertEqual(combo.itemData(0, _IMAGE_CAPABILITY_ROLE), "supported")

    def test_fetched_models_use_large_panel_font_and_bullets(self):
        from PySide6.QtWidgets import QSizePolicy, QStackedWidget
        from ui.settings_dialog import _custom_model_favorite_key

        dialog, _provider = self._dialog()
        self.addCleanup(dialog.close)
        models = [
            "gemma2:2b",
            "qwen3-vl:2b-instruct",
            "vendor/<script>alert(1)</script>",
        ]
        dialog._settings["custom_provider_model_favorites"] = [
            _custom_model_favorite_key("local", models[1])
        ]
        dialog.show()
        self.app.processEvents()

        dialog._on_custom_fetch_done(
            "local",
            models,
            None,
            [
                ("Balanced", [models[2]]),
                ("Free", models[:2]),
            ],
        )

        self.assertEqual(
            dialog.results.toPlainText(),
            "\n".join(f"• {model}" for model in models),
        )
        self.assertIn("font-size: 18px", dialog.results.styleSheet())
        self.assertGreaterEqual(dialog.results.maximumHeight(), 500)
        self.assertEqual(
            dialog.results.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Preferred,
        )
        self.assertIsInstance(dialog._results_stack, QStackedWidget)
        self.assertIs(
            dialog._results_stack.currentWidget(),
            dialog.results,
        )
        self.assertEqual(
            dialog.results.accessibleName(),
            "Models fetched for Local Ollama (3)",
        )
        dialog.results.setPlainText("Fetch failed")
        self.assertIs(
            dialog._results_stack.currentWidget(), dialog.results)

    def test_custom_provider_collapse_and_sidebar_order_are_persisted(self):
        from main import SettingsDialog

        dialog, _provider = self._dialog()
        self.addCleanup(dialog.close)
        card = next(
            card for card in dialog._custom_cards
            if card.property("cp_id") == "local"
        )

        self.assertEqual(
            [button.text() for button in dialog._nav_buttons[:3]],
            ["Custom Providers", "API Keys", "Appearance"],
        )
        dialog._nav_buttons[0].click()
        self.assertEqual(dialog._stack.currentIndex(), 0)
        card._cp_collapse.setChecked(True)
        values = dialog.values()
        self.assertEqual(values["settings_last_page"], 0)
        self.assertEqual(values["settings_last_page_version"], 3)
        self.assertEqual(
            values["custom_provider_collapsed"], {"local": True})

        reopened = SettingsDialog(values)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened._stack.currentIndex(), 0)
        restored_card = next(
            card for card in reopened._custom_cards
            if card.property("cp_id") == "local"
        )
        self.assertTrue(restored_card._cp_collapse.isChecked())
        self.assertTrue(restored_card._cp_details.isHidden())

    def test_model_and_instructions_are_in_their_new_settings_pages(self):
        from main import SettingsDialog
        from config.settings import DEFAULTS

        dialog = SettingsDialog(dict(DEFAULTS))
        self.addCleanup(dialog.close)

        self.assertEqual(
            [button.text() for button in dialog._nav_buttons],
            ["Custom Providers", "API Keys", "Appearance",
             "Performance", "Application", "Read Aloud / TTS"],
        )
        self.assertTrue(dialog._page_keys[0].isAncestorOf(dialog.z_model))
        self.assertTrue(
            dialog._page_keys[4].isAncestorOf(dialog.model_instructions))
        self.assertTrue(
            dialog._page_keys[4].isAncestorOf(dialog._instr_splitter))
        self.assertTrue(
            dialog._page_keys[5].isAncestorOf(dialog.automatic_speech_voice))
        self.assertTrue(
            dialog._page_keys[5].isAncestorOf(dialog.speech_engine))
        self.assertEqual(dialog.speech_engine.currentData(), "edge")
        dialog.speech_engine.setCurrentIndex(
            dialog.speech_engine.findData("gemini"))
        dialog.speech_rate_setting.setValue(0.85)
        dialog._edge_speech_voice = "ar-SA-ZariyahNeural"
        dialog.gemini_tts_voice.setCurrentIndex(
            dialog.gemini_tts_voice.findData("Zephyr"))
        values = dialog.values()
        self.assertEqual(values["speech_engine"], "gemini")
        self.assertEqual(values["speech_rate"], 0.85)
        self.assertEqual(values["speech_voice"], "ar-SA-ZariyahNeural")
        self.assertEqual(values["gemini_tts_voice"], "Zephyr")
        self.assertFalse(dialog.edge_speech_voice.isEnabled())
        self.assertTrue(dialog.gemini_tts_voice.isEnabled())
        self.assertFalse(dialog.automatic_speech_voice.isEnabled())
        self.assertEqual(
            [dialog.speech_engine.itemData(i)
             for i in range(dialog.speech_engine.count())],
            ["edge", "gemini"],
        )
        self.assertEqual(dialog._stack.currentIndex(), 0)

    def test_tts_settings_load_all_edge_voices_from_catalog(self):
        import asyncio
        from unittest import mock
        from popup import speech_worker
        from main import SettingsDialog

        async def fake_list_voices():
            await asyncio.sleep(0)
            return [
                {
                    "ShortName": "ar-DZ-AminaNeural",
                    "Locale": "ar-DZ",
                    "FriendlyName": "Amina",
                    "Gender": "Female",
                },
                {
                    "ShortName": "ja-JP-NanamiNeural",
                    "Locale": "ja-JP",
                    "FriendlyName": "Nanami",
                    "Gender": "Female",
                },
                {
                    "ShortName": "ja-JP-EchoMultilingualNeural",
                    "Locale": "ja-JP",
                    "FriendlyName": "Echo Multilingual",
                    "Gender": "Male",
                },
            ]

        dialog = SettingsDialog(dict(DEFAULTS))
        self.addCleanup(dialog.close)
        with mock.patch.object(
            speech_worker.edge_tts, "list_voices", new=fake_list_voices
        ):
            dialog._nav_buttons[5].click()
            worker = dialog._tts_voice_list_worker
            self.assertIsNotNone(worker)
            self.assertTrue(worker.wait(5000))
            self.app.processEvents()
        self.assertIsNone(dialog._tts_voice_list_worker)
        self.assertEqual(
            [
                action.text()
                for action in dialog.edge_speech_voice_menu.actions()
                if action.menu() is not None
            ],
            ["Ar", "Multilingual"],
        )
        voice_actions = [
            action
            for language_action in dialog.edge_speech_voice_menu.actions()
            if language_action.menu() is not None
            for country_action in language_action.menu().actions()
            if country_action.menu() is not None
            for action in country_action.menu().actions()
        ]
        self.assertEqual(
            {action.data() for action in voice_actions},
            {"ar-DZ-AminaNeural", "ja-JP-EchoMultilingualNeural"},
        )
        self.assertNotIn(
            "ja-JP-NanamiNeural",
            {action.data() for action in voice_actions},
        )
        selected = next(
            action for action in voice_actions
            if action.data() == "ja-JP-EchoMultilingualNeural")
        selected.trigger()
        self.assertEqual(
            dialog.values()["speech_voice"], "ja-JP-EchoMultilingualNeural")
        self.assertEqual(
            [
                action.data()
                for action in voice_actions
                if action.isChecked()
            ],
            ["ja-JP-EchoMultilingualNeural"],
        )

    def test_tts_voice_control_matches_fields_and_menu_opens_upward(self):
        from unittest import mock
        from PySide6.QtCore import QPoint
        from main import SettingsDialog

        dialog = SettingsDialog(dict(DEFAULTS))
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()
        button_top = dialog.edge_speech_voice.mapToGlobal(QPoint(0, 0))
        menu_height = dialog.edge_speech_voice_menu.sizeHint().height()
        with mock.patch.object(
            dialog.edge_speech_voice_menu, "popup"
        ) as popup:
            dialog._show_tts_edge_voice_menu()
        popup.assert_called_once()
        menu_position = popup.call_args.args[0]
        self.assertLessEqual(menu_position.y(), button_top.y())
        stylesheet = dialog.styleSheet()
        self.assertIn("QToolButton#edgeSpeechVoice", stylesheet)
        self.assertGreater(menu_height, 0)

    def test_settings_page_index_from_version_two_is_preserved(self):
        from main import SettingsDialog
        from config.settings import DEFAULTS

        dialog = SettingsDialog(dict(
            DEFAULTS,
            settings_last_page=4,
            settings_last_page_version=2,
        ))
        self.addCleanup(dialog.close)
        self.assertEqual(dialog._stack.currentIndex(), 4)

    def test_settings_navigation_persists_page_immediately(self):
        from unittest import mock
        from PySide6.QtWidgets import QWidget
        from main import SettingsDialog

        parent = QWidget()
        parent.settings = dict(DEFAULTS)
        self.addCleanup(parent.close)
        dialog = SettingsDialog(dict(DEFAULTS), parent)
        self.addCleanup(dialog.close)
        with mock.patch("config.settings.save") as save_settings:
            dialog._nav_buttons[4].click()
        self.assertEqual(parent.settings["settings_last_page"], 4)
        self.assertEqual(parent.settings["settings_last_page_version"], 3)
        save_settings.assert_called_once_with(parent.settings)

    def test_global_model_picker_panel_uses_compact_vertical_spacing(self):
        from main import SettingsDialog
        from config.settings import DEFAULTS

        dialog = SettingsDialog(dict(DEFAULTS))
        self.addCleanup(dialog.close)

        card = dialog._engine_model_card
        self.assertEqual(card.objectName(), "globalModelPickerCard")
        self.assertLessEqual(card.sizeHint().height(), 125)
        self.assertGreaterEqual(dialog.z_model.minimumHeight(), 38)
        self.assertTrue(dialog.engine_hint.isVisibleTo(card))

    def test_legacy_settings_page_is_migrated_after_translation_removal(self):
        from main import SettingsDialog
        from config.settings import DEFAULTS

        legacy_provider_page = SettingsDialog(
            dict(
                DEFAULTS,
                settings_last_page=2,
                settings_last_page_version=1,
            ))
        self.addCleanup(legacy_provider_page.close)
        self.assertEqual(legacy_provider_page._stack.currentIndex(), 0)

        legacy_application_page = SettingsDialog(
            dict(
                DEFAULTS,
                settings_last_page=5,
                settings_last_page_version=1,
            ))
        self.addCleanup(legacy_application_page.close)
        self.assertEqual(legacy_application_page._stack.currentIndex(), 4)

    def test_favorites_and_custom_panel_state_roundtrip_settings_file(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from config import settings as settings_module

        temp_dir = tempfile.TemporaryDirectory()
        path = Path(temp_dir.name) / "settings.json"
        saved = {
            "model": "custom:local",
            "model_favorites": ["gemini-3.5-flash-lite"],
            "custom_provider_model_favorites": [
                'custom-model:["local","qwen3-vl:2b-instruct"]',
            ],
            "custom_provider_collapsed": {"local": True},
            "settings_last_page": 2,
            "custom_providers": [{
                "id": "local",
                "name": "Local",
                "base_url": "http://localhost:11434/v1",
                "api_key": "ollama",
                "model": "qwen3-vl:2b-instruct",
                "enabled": True,
            }],
        }
        try:
            with mock.patch.object(settings_module, "SETTINGS_PATH", path):
                settings_module.save(saved)
                loaded = settings_module.load()
            self.assertEqual(
                loaded["model_favorites"], saved["model_favorites"])
            self.assertEqual(
                loaded["custom_provider_model_favorites"],
                saved["custom_provider_model_favorites"],
            )
            self.assertEqual(
                loaded["custom_provider_collapsed"],
                saved["custom_provider_collapsed"],
            )
            self.assertEqual(loaded["settings_last_page"], 2)
            self.assertEqual(
                loaded["custom_providers"][0]["model"],
                "qwen3-vl:2b-instruct",
            )
        finally:
            temp_dir.cleanup()

    def test_custom_providers_survive_legacy_settings_rewrites(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        from config import settings as settings_module

        provider = {
            "id": "local",
            "name": "Local Ollama",
            "base_url": "http://localhost:11434/v1",
            "api_key": "ollama",
            "model": "qwen3-vl:2b-instruct",
            "enabled": True,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.json"
            providers_path = settings_path.with_name(
                f"{settings_path.stem}_custom_providers.json"
            )
            settings_path.write_text(
                json.dumps({
                    "model": "gemini-3.5-flash-lite",
                    "custom_providers": [provider],
                }),
                encoding="utf-8",
            )
            with mock.patch.object(
                settings_module, "SETTINGS_PATH", settings_path
            ):
                migrated = settings_module.load()
                self.assertEqual(migrated["custom_providers"][0]["id"], "local")
                self.assertTrue(providers_path.is_file())

                # Older builds rewrite only the settings keys they know.
                settings_path.write_text(
                    json.dumps({"model": "gemini-3.5-flash-lite"}),
                    encoding="utf-8",
                )
                reopened = settings_module.load()

                settings_module.save(
                    dict(settings_module.DEFAULTS, custom_providers=[])
                )
                settings_path.write_text(
                    json.dumps({"custom_providers": [provider]}),
                    encoding="utf-8",
                )
                after_removal = settings_module.load()

            self.assertEqual(reopened["custom_providers"][0]["name"], "Local Ollama")
            self.assertEqual(
                reopened["custom_providers"][0]["model"],
                "qwen3-vl:2b-instruct",
            )
            self.assertEqual(after_removal["custom_providers"], [])

    def test_settings_ui_uses_readable_typography_and_compact_status_panel(self):
        from PySide6.QtWidgets import QFrame, QLabel, QLineEdit, QSizePolicy
        from ui.theme import THEMES, settings_stylesheet

        dialog, _provider = self._dialog()
        self.addCleanup(dialog.close)
        stylesheet = settings_stylesheet("Dark Slate")

        self.assertEqual(THEMES["Dark Slate"]["accent"], "#4F46E5")
        self.assertIn("font-size: 15px", stylesheet)
        self.assertIn("font-size: 14px", stylesheet)
        self.assertIn("font-size: 22px", stylesheet)
        self.assertIn("width: 12px; height: 12px", stylesheet)
        self.assertIn("padding: 4px 12px", stylesheet)
        self.assertGreaterEqual(dialog._nav_buttons[0].minimumHeight(), 42)
        self.assertIn("font-size: 18px", dialog.results.styleSheet())
        sidebar = dialog.findChild(QFrame, "sidebar")
        self.assertIs(
            dialog._results_stack.parentWidget(), dialog._sidebar_splitter)
        self.assertIs(dialog.results.parentWidget(), dialog._results_stack)
        self.assertIs(dialog._sidebar_splitter.parentWidget(), sidebar)
        self.assertEqual(dialog.results.objectName(), "settingsStatusPanel")
        self.assertIsNone(dialog.findChild(QLineEdit, "headerSearch"))
        self.assertGreaterEqual(dialog.results.maximumHeight(), 500)
        self.assertFalse(dialog.results.isHidden())
        self.assertEqual(
            dialog.results.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Preferred,
        )
        self.assertEqual(sidebar.minimumWidth(), 220)
        self.assertEqual(sidebar.maximumWidth(), 500)

    def test_settings_splitters_resize_sidebar_and_results_with_mouse(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QFrame

        dialog, _provider = self._dialog()
        self.addCleanup(dialog.close)
        sidebar = dialog.findChild(QFrame, "sidebar")
        dialog.show()
        dialog.resize(1200, 800)
        self.app.processEvents()

        def drag_splitter_handle(splitter, delta):
            handle = splitter.handle(1)
            start = handle.rect().center()
            QTest.mousePress(
                handle, Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(handle, start + delta, delay=100)
            QTest.mouseRelease(
                handle, Qt.MouseButton.LeftButton, pos=start + delta)
            self.app.processEvents()

        body_start = dialog._body_splitter.sizes()
        drag_splitter_handle(dialog._body_splitter, QPoint(50, 0))
        body_end = dialog._body_splitter.sizes()
        self.assertGreater(body_end[0], body_start[0] + 20)
        self.assertEqual(sidebar.width(), body_end[0])

        panel_start = dialog._sidebar_splitter.sizes()
        drag_splitter_handle(dialog._sidebar_splitter, QPoint(0, -50))
        panel_end = dialog._sidebar_splitter.sizes()
        self.assertGreater(panel_end[1], panel_start[1] + 20)
        self.assertEqual(dialog.results.height(), panel_end[1])
        self.assertEqual(dialog._body_splitter.handleWidth(), 8)
        self.assertEqual(dialog._sidebar_splitter.handleWidth(), 8)
        self.assertGreaterEqual(
            dialog._body_splitter.handle(1).width(), 8)
        self.assertGreaterEqual(
            dialog._sidebar_splitter.handle(1).height(), 8)

    def test_each_custom_provider_panel_gets_a_distinct_stripe_color(self):
        from PySide6.QtWidgets import QFrame
        from re import search
        from ui.settings_dialog import _CUSTOM_PROVIDER_STRIPE_COLORS

        self.assertEqual(
            _CUSTOM_PROVIDER_STRIPE_COLORS[:5],
            ("#FFD400", "#FF3B30", "#00D9FF", "#FF8A00", "#39D353"),
        )
        dialog, provider = self._dialog()
        self.addCleanup(dialog.close)
        providers = [provider]
        for index in range(1, 3):
            providers.append({
                **provider,
                "id": f"local-{index}",
                "name": f"Local {index}",
            })
        dialog._settings["custom_providers"] = providers
        dialog._rebuild_custom_cards()
        cards = [
            card for card in dialog._custom_cards
            if card.objectName() == "customProviderCard"
        ]

        stripe_colors = []
        card_colors = []
        for card in cards:
            stripe = card.findChild(QFrame, "providerAccentStripe")
            color = search(
                r"background-color:\s*(#[0-9A-Fa-f]{6})",
                stripe.styleSheet(),
            )
            stripe_colors.append(color.group(1))
            background = search(
                r"QFrame#customProviderCard\s*\{\s*background:\s*(#[0-9A-Fa-f]{6})",
                card.styleSheet(),
            )
            self.assertIsNotNone(background)
            card_colors.append(background.group(1))
        self.assertEqual(
            stripe_colors,
            list(_CUSTOM_PROVIDER_STRIPE_COLORS[:3]),
        )
        self.assertEqual(len(set(stripe_colors)), len(stripe_colors))
        self.assertEqual(len(set(card_colors)), len(card_colors))


class TestOnlyWorkingModelsAreOffered(_GuiCase):
    """Every claim here was measured against the live OpenCode Zen gateway and
    the real OpenCode CLI on 2026-10-07, not guessed from model names:

    * `space-bunny-free` answers 200 with no key at all (HTTP and CLI).
    * Every other `-free` id answers 403 "free tier can only be used from
      within OpenCode" -- from plain HTTP *and* from inside the authenticated
      OpenCode CLI, so the block is an account entitlement, not a transport.
    * The Zen "credits" ids answer provider.quota: the account has no balance.
    * OpenCode's own log shows that same 403 on every single day since
      2026-09-24, so these ids never worked for this machine.
    """

    # Despite the name, this class also builds real MainWindows and popups, so
    # it needs the shared QApplication — and, now, the teardown that closes
    # them again.

    def test_the_free_tier_offers_the_one_model_that_actually_answers(self):
        from translators import registry
        self.assertEqual(registry.ZEN_FREE, ["space-bunny-free"])

    def test_no_group_asks_the_user_for_an_opencode_key_any_more(self):
        from translators import registry
        labels = [label for label, _ in registry.model_groups({})]
        self.assertFalse([x for x in labels if "OpenCode key" in x],
                         f"still asks for a key: {labels}")

    def test_models_that_never_answer_are_not_listed(self):
        from translators import registry
        labels = [label for label, _ in registry.model_groups({})]
        # Bridge ids (2026-10-08 streaming) ARE listed; truly dead stay out.
        # muse-spark uses /responses (verified 200); jev/ling/exo fail live.
        for dead in ["exo-free", "deepseek-v4-flash-free",
                     "grok-4.5", "glm-5.2", "deepseek-v4-pro",
                     "jev-1.13-free", "ling-3.0-flash-fin-free",
                     "ling-3.1-flash-free"]:
            self.assertNotIn(dead, labels)

    def test_a_zen_id_that_is_no_longer_free_says_why(self):
        from translators import registry
        ok, reason = registry.availability("exo-free", {})
        self.assertFalse(ok)
        self.assertIn("space-bunny-free", reason)

    def test_the_working_free_model_needs_no_key_at_all(self):
        from translators import registry
        self.assertEqual(registry.availability("space-bunny-free", {}), (True, ""))

    def test_a_cli_model_that_failed_verification_is_disabled_with_its_error(self):
        from translators import registry
        model = "opencode-cli:opencode/exo-free"
        settings = {"model_status": {model: "403 — free tier blocked for this account"}}
        ok, reason = registry.availability(model, settings)
        self.assertFalse(ok)
        self.assertIn("403", reason)

    def test_a_cli_model_that_passed_verification_is_enabled(self):
        from translators import registry
        model = "opencode-cli:opencode/space-bunny-free"
        settings = {"model_status": {model: ""}}
        self.assertTrue(registry.availability(model, settings)[0])

    def test_verification_status_does_not_disable_the_gemini_group(self):
        from translators import registry
        # Picker offers Gemini + bridge only; a bridge failure must not
        # disable Gemini, and CLI ids are no longer offered at all.
        rows = dict(registry.model_groups({
            "gemini_key": "k",
            "opencode_cli_models": ["opencode/exo-free", "opencode/space-bunny-free"],
            "model_status": {"mimo-v2.6-flash-free": "403"},
        }))
        self.assertTrue(rows["gemini-2.5-flash-lite"])
        self.assertFalse(rows["mimo-v2.6-flash-free"])
        self.assertNotIn("opencode-cli:opencode/exo-free", rows)

    def test_the_opencode_api_key_is_gone_from_settings(self):
        from config.settings import DEFAULTS
        self.assertNotIn("opencode_key", DEFAULTS)

    def test_a_saved_dead_model_becomes_the_working_one(self):
        from config import settings as settings_module
        saved = {"model": "opencode-cli:opencode/muse-spark-1.3-contributor-free",
                 "opencode_key": "an-old-key", "gemini_key": "k",
                 "opencode_cli_models": ["opencode/space-bunny-free",
                                         "opencode/muse-spark-1.3-contributor-free"]}
        with mock_opencode_settings(saved) as path:
            merged = settings_module.load()
            settings_module.save(merged)   # persists the migrated values
            on_disk = path.read_text(encoding="utf-8")
        self.assertEqual(merged["model"], "opencode-cli:opencode/space-bunny-free")
        self.assertNotIn("opencode_key", merged)
        self.assertNotIn("opencode_key", on_disk)

    def test_a_saved_working_model_is_left_alone(self):
        from config import settings as settings_module
        saved = {"model": "opencode-cli:opencode/space-bunny-free",
                 "opencode_cli_models": ["opencode/space-bunny-free"]}
        with mock_opencode_settings(saved):
            merged = settings_module.load()
        self.assertEqual(merged["model"], "opencode-cli:opencode/space-bunny-free")

    def test_the_reasoning_budget_leaves_room_for_the_answer(self):
        from translators import opencode_zen
        # Measured: at 2048 space-bunny-free intermittently replies with
        # finish_reason="length" -- the vocalisation the user asks for burns the
        # whole budget before any content is emitted.
        self.assertGreaterEqual(opencode_zen.MIN_MAX_TOKENS, 4096)

    def test_a_raw_cli_failure_becomes_a_short_readable_reason(self):
        """`opencode run` exits 1 and dumps its NDJSON event stream into the
        error, so the picker must not show the user a wall of JSON."""
        from translators import verify
        raw = (
            '`opencode run` failed (1): {"type":"error","timestamp":1791412,'
            '"sessionID":"ses_abc","error":{"type":"provider.quota",'
            '"message":"Upstream request failed with status 402"}}'
        )
        self.assertEqual(verify._readable(RuntimeError(raw)),
                         "no credits on this account")

    def test_a_google_failure_names_the_missing_connection(self):
        from translators import verify
        raw = ('`opencode run` failed (1): {"type":"error","error":'
               '{"type":"provider.auth","message":'
               '"Method doesn\'t allow unauthenticated requests"}}')
        self.assertEqual(verify._readable(RuntimeError(raw)),
                         "needs a Google connection in OpenCode")

    def test_a_real_quota_failure_becomes_a_short_reason(self):
        """The exact stream OpenCode emits for a 402, as measured on
        2026-10-07: a step_start event first, then the error object."""
        from translators import verify
        raw = (
            '`opencode run` failed (1): {"type":"step_start",'
            '"timestamp":1791414012728,"sessionID":"ses_abc",'
            '"part":{"type":"step-start"}}{"type":"error","error":'
            '{"type":"provider.quota","message":"Upstream request failed: '
            'Insufficient account funds","status":402}}'
        )
        self.assertEqual(verify._readable(RuntimeError(raw)),
                         "no credits on this account")

    def test_the_reason_never_shows_raw_json(self):
        """Whatever the failure, the picker row must stay one short line."""
        from translators import verify
        raw = ('`opencode run` failed (1): {"type":"step_start","part":'
               '{"type":"step-start"}}{"type":"error","error":'
               '{"type":"provider.auth","message":"Method doesn\'t allow '
               'unregistered callers (callers without established identity). '
               'Please use API Key or other form of API consumer identity to '
               'call this API.","status":403}}')
        reason = verify._readable(RuntimeError(raw))
        self.assertNotIn("{", reason)
        self.assertNotIn("`", reason)
        self.assertEqual(reason, "needs a Google connection in OpenCode")

    def test_the_settings_dialog_no_longer_asks_for_an_opencode_key(self):
        from main import SettingsDialog
        from config.settings import DEFAULTS
        dlg = SettingsDialog(dict(DEFAULTS))
        try:
            self.assertFalse(hasattr(dlg, "z_key"))
            self.assertNotIn("opencode_key", dlg.values())
            labels = [dlg.z_model.itemText(i) for i in range(dlg.z_model.count())]
            model_ids = [
                dlg.z_model.itemData(i) for i in range(dlg.z_model.count())
                if isinstance(dlg.z_model.itemData(i), str)
            ]
            self.assertNotIn("exo-free", labels)
            self.assertIn("space-bunny-free", model_ids)
        finally:
            dlg.close()

    def test_bridge_ids_route_to_bridge_provider(self):
        from translators import registry
        self.assertEqual(registry.provider_of("mimo-v2.6-flash-free"), "bridge")
        self.assertEqual(registry.provider_of("space-bunny-free"), "bridge")
        ok, _ = registry.availability("mimo-v2.6-flash-free", {})
        self.assertTrue(ok)

    def test_bridge_aggregates_sse_and_sends_fingerprint(self):
        from unittest import mock
        import translators.opencode_bridge as b
        self.assertIn("mimo-v2.6-flash-free", b.BRIDGE_FREE)
        self.assertTrue(b._session_id().startswith("ses_"))

        def fake_post(url, json=None, headers=None, timeout=None, stream=None):
            self.assertTrue(json.get("stream"))
            self.assertTrue(headers["User-Agent"].startswith("opencode/"))
            self.assertTrue(headers["x-opencode-session"].startswith("ses_"))
            names = [t["function"]["name"] for t in json.get("tools", [])]
            self.assertIn("shell", names)
            self.assertIn("read", names)

            class Resp:
                status_code = 200
                text = ""
                def iter_lines(self):
                    yield b'data: {"choices":[{"delta":{"content":"Hello "},"finish_reason":null}]}'
                    yield b'data: {"choices":[{"delta":{"content":"world"},"finish_reason":null}]}'
                    yield b'data: [DONE]'
                def close(self):
                    pass
            return Resp()

        with mock.patch("translators.opencode_bridge.requests.post", side_effect=fake_post):
            self.assertEqual(b.translate("hi", "mimo-v2.6-flash-free"), "Hello world")

    def test_bridge_routes_muse_spark_to_responses_endpoint(self):
        from unittest import mock
        import translators.opencode_bridge as b
        self.assertIn("muse-spark-1.3-contributor-free", b.BRIDGE_FREE)

        def fake_post(url, json=None, headers=None, timeout=None, stream=None):
            self.assertTrue(url.endswith("/responses"))
            self.assertTrue(json.get("stream"))
            self.assertIn("instructions", json)
            self.assertIn("input", json)

            class Resp:
                status_code = 200
                text = ""
                def iter_lines(self):
                    yield b'event: response.output_text.delta'
                    yield b'data: {"type":"response.output_text.delta","delta":"Hello "}'
                    yield b'event: response.output_text.delta'
                    yield b'data: {"type":"response.output_text.delta","delta":"world"}'
                    yield b'data: [DONE]'
                def close(self):
                    pass
            return Resp()

        with mock.patch("translators.opencode_bridge.requests.post", side_effect=fake_post):
            self.assertEqual(
                b.translate("hi", "muse-spark-1.3-contributor-free"), "Hello world")

    def test_bridge_stream_yields_deltas_live(self):
        from unittest import mock
        import translators.opencode_bridge as b

        def fake_post(url, json=None, headers=None, timeout=None, stream=None):
            class Resp:
                status_code = 200
                text = ""
                def iter_lines(self):
                    yield b'data: {"choices":[{"delta":{"content":"Hello "},"finish_reason":null}]}'
                    yield b'data: {"choices":[{"delta":{"content":"world"},"finish_reason":null}]}'
                    yield b'data: [DONE]'
                def close(self):
                    pass
            return Resp()

        with mock.patch("translators.opencode_bridge.requests.post", side_effect=fake_post):
            self.assertEqual(list(b.stream("hi", "mimo-v2.6-flash-free")),
                             ["Hello ", "world"])
            self.assertEqual(b.translate("hi", "mimo-v2.6-flash-free"),
                             "Hello world")

    def test_chain_stream_emits_partial_then_done(self):
        from unittest import mock
        from translators import chain
        s = dict(DEFAULTS)
        s.update({"model": "mimo-v2.6-flash-free"})
        with mock.patch("translators.opencode_bridge.stream",
                        return_value=iter(["Hello ", "world"])):
            events = list(chain.translate_stream("cadence", s))
        self.assertEqual(events[0], ("partial", "Hello ", ""))
        self.assertEqual(events[1], ("partial", "Hello world", ""))
        self.assertEqual(events[2][0], "done")
        self.assertEqual(events[2][1], "Hello world")
        self.assertIn("mimo-v2.6-flash", events[2][2])
        # translate() itself is unchanged for the same input
        with mock.patch("translators.opencode_bridge.stream",
                        return_value=iter(["Hello ", "world"])):
            self.assertEqual(chain.translate("cadence", s)[0], "Hello world")

    def test_fast_mode_off_orders_bridge_alphabetically(self):
        from translators import registry
        from translators.opencode_bridge import BRIDGE_FREE
        fast = [label for label, _ in registry.model_groups({"fast_mode": True})]
        slow = [label for label, _ in registry.model_groups({"fast_mode": False})]
        self.assertEqual(fast[0], "── ✦ Gemini — needs a Gemini key ──")
        fast_bridge = fast[fast.index(registry.BRIDGE_HDR) + 1:
                           fast.index(registry.GOOGLE_TRANSLATE_HDR)]
        self.assertEqual(fast_bridge, BRIDGE_FREE)  # measured fastest-first
        self.assertEqual(fast_bridge[0], "space-bunny-free")
        slow_bridge = slow[slow.index(registry.BRIDGE_HDR) + 1:
                           slow.index(registry.GOOGLE_TRANSLATE_HDR)]
        self.assertEqual(slow_bridge, sorted(slow_bridge))

    def test_chain_stream_off_emits_single_done(self):
        from unittest import mock
        from translators import chain
        s = dict(DEFAULTS)
        s.update({"model": "mimo-v2.6-flash-free", "fast_mode": False})
        with mock.patch("translators.opencode_bridge.stream",
                        side_effect=AssertionError("must not stream when off")):
            with mock.patch("translators.opencode_bridge.translate",
                            return_value="full-ar"):
                events = list(chain.translate_stream("cadence", s))
        self.assertEqual(events, [("done", "full-ar",
                                   "◈ chosen bridge: mimo-v2.6-flash")])

    def test_speech_fast_off_skips_cache_and_parallel(self):
        from unittest import mock
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        calls = []

        class FakeCommunicate:
            def __init__(self, text, voice, rate=None, boundary=None):
                calls.append(text)

            async def stream(self):
                yield {"type": "audio", "data": b"x"}
                yield {"type": "WordBoundary", "offset": 0,
                       "duration": 1, "text": "hi"}

        fake = type("M", (), {"Communicate": FakeCommunicate})
        cache_tmp = tempfile.mkdtemp(prefix="harmony-tts-test-")
        with mock.patch.dict(os.environ, {"HARMONY_TTS_CACHE": cache_tmp}):
            with mock.patch.object(speech_worker, "edge_tts", fake):
                SpeechWorker(
                    "hi.", fast=False, target_language="en").run()
                SpeechWorker(
                    "hi.", fast=False, target_language="en").run()
        # No cache: network hit twice for the same text.
        self.assertEqual(calls, ["hi", "hi"])
        self.assertEqual(os.listdir(cache_tmp), [])

    def test_settings_fast_mode_roundtrips_and_reorders(self):
        from main import SettingsDialog
        from config.settings import DEFAULTS
        from translators import registry
        dlg = SettingsDialog(dict(DEFAULTS))
        try:
            self.assertTrue(dlg.fast_check.isChecked())
            self.assertTrue(dlg.values()["fast_mode"])
            dlg.fast_check.setChecked(False)
            self.assertFalse(dlg.values()["fast_mode"])
            bridge = [
                dlg.z_model.itemData(i)
                for i in range(dlg.z_model.count())
                if dlg.z_model.itemData(i) in registry.ordered_models(True)
            ]
            self.assertEqual(bridge, sorted(bridge))
        finally:
            dlg.close()

    def test_groq_model_survives_save_load_and_resolve(self):
        """Regression: load() used to reset any `groq:` model to Gemini,
        so the picker choice bounced back on every reload/restart."""
        from config import settings as settings_module
        from config.settings import resolve_model
        saved = {"model": "groq:qwen/qwen3.8-27b",
                 "openai_base": "https://api.groq.com/openai/v1",
                 "openai_key": "k"}
        with mock_opencode_settings(saved):
            merged = settings_module.load()
        self.assertEqual(merged["model"], "groq:qwen/qwen3.8-27b")
        self.assertEqual(resolve_model(merged), "groq:qwen/qwen3.8-27b")

    def test_groq_models_listed_below_bridge_and_gated_by_key(self):
        from translators import registry
        no_key = [label for label, _ in registry.model_groups({})]
        self.assertIn(registry.GROQ_HDR, no_key)
        self.assertIn("groq:qwen/qwen3.8-27b", no_key)
        rows = dict(registry.model_groups({}))
        self.assertFalse(rows["groq:qwen/qwen3.8-27b"])
        ok_rows = dict(registry.model_groups({
            "openai_base": "https://api.groq.com/openai/v1",
            "openai_key": "k"}))
        self.assertTrue(ok_rows["groq:qwen/qwen3.8-27b"])
        # Live ids merge above the curated list, de-duplicated.
        live = [label for label, _ in registry.model_groups(
            {}, live_groq=["new/model"])]
        self.assertLess(live.index("groq:new/model"),
                        live.index("groq:qwen/qwen3.8-27b"))

    def test_groq_provider_routing_and_chain(self):
        from unittest import mock
        from translators import chain, registry
        self.assertEqual(registry.provider_of("groq:qwen/qwen3.8-27b"), "groq")
        ok, reason = registry.availability("groq:qwen/qwen3.8-27b", {})
        self.assertFalse(ok)
        self.assertIn("Groq key", reason)
        ok, reason = registry.availability(
            "groq:qwen/qwen3.8-27b", {"groq_key": "G"})
        self.assertTrue(ok)
        s = dict(DEFAULTS)
        s.update({"model": "groq:qwen/qwen3.8-27b",
                  "openai_base": "https://api.groq.com/openai/v1",
                  "openai_key": "k"})
        with mock.patch("translators.opencode_compat.translate",
                        return_value="groq-ar") as t:
            self.assertEqual(chain.translate("cadence", s),
                             ("groq-ar", "⚡ groq: qwen/qwen3.8-27b"))
        self.assertEqual(t.call_args.args[1:4],
                         ("https://api.groq.com/openai/v1", "k",
                          "qwen/qwen3.8-27b"))
        s["explain_mode"] = True
        with mock.patch("translators.opencode_compat.translate",
                        return_value="groq-ar") as t:
            chain.translate("cadence", s)
        self.assertIn("### الشرح", t.call_args.kwargs["instructions"])

    def test_explain_mode_is_appended_only_after_last_text_chunk(self):
        from unittest import mock
        from translators import chain

        settings = dict(DEFAULTS, explain_mode=True, fast_mode=True)
        received = []

        def translate_chunk(text, chunk_settings):
            received.append(chunk_settings)
            return text, "test-model"

        with mock.patch.object(
            chain, "chunk_text", return_value=["first", "last"]
        ), mock.patch.object(chain, "_translate_chunk", side_effect=translate_chunk):
            translated, _badge = chain.translate("long passage", settings)

        self.assertEqual([item["explain_mode"] for item in received], [False, True])
        self.assertEqual(translated, "first\nlast")

        received.clear()
        with mock.patch.object(
            chain, "chunk_text", return_value=["first", "last"]
        ), mock.patch.object(chain, "_translate_chunk", side_effect=translate_chunk):
            list(chain.translate_stream("long passage", settings))
        self.assertEqual([item["explain_mode"] for item in received], [False, True])

    def test_explain_mode_adds_target_language_explanation_instruction(self):
        from translators import chain

        instructions = chain._translation_instructions({
            "explain_mode": True,
            "target_language": "ar",
        })
        self.assertIn("### الشرح", instructions)
        self.assertIn("in Arabic", instructions)
        plain = chain._translation_instructions({"explain_mode": False})
        self.assertIn("Arabic tashkeel is disabled", plain)
        self.assertNotIn("### الشرح", plain)
        french = chain._translation_instructions({
            "explain_mode": True,
            "target_language": "fr",
        })
        self.assertIn("### Explication", french)
        self.assertIn("in French", french)
        custom = chain._translation_instructions({
            "explain_mode": True,
            "target_language": "en",
            "explain_instructions": "Explain the historical references.",
        })
        self.assertIn("Additional instructions for the explanation only", custom)
        self.assertIn("Explain the historical references.", custom)
        self.assertNotIn(
            "Explain the historical references.",
            chain._translation_instructions({
                "explain_mode": False,
                "explain_instructions": "Explain the historical references.",
            }),
        )

    def test_cf_and_mistral_sections_below_groq(self):
        from translators import registry
        labels = [label for label, _ in registry.model_groups({})]
        self.assertLess(labels.index(registry.GROQ_HDR),
                        labels.index(registry.CF_HDR))
        self.assertLess(labels.index(registry.CF_HDR),
                        labels.index(registry.MISTRAL_HDR))
        self.assertIn("cf:@cf/meta/llama-3.3-70b-instruct-fp8-fast", labels)
        self.assertIn("mistral:mistral-small-latest", labels)
        for provider, model in (
                ("cf", "cf:@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
                ("mistral", "mistral:mistral-small-latest")):
            self.assertEqual(registry.provider_of(model), provider)
            ok, _ = registry.availability(model, {})
            self.assertFalse(ok)  # no key yet
            ok, _ = registry.availability(model, {
                "cloudflare_account_id": "ACC", "cloudflare_token": "T",
                "mistral_key": "M"})
            self.assertTrue(ok)

    def test_groq_image_translation_uses_provider_token(self):
        from unittest import mock
        from translators import chain, registry
        from PIL import Image
        img = Image.new("RGB", (10, 10), "white")
        s = dict(DEFAULTS)
        s.update({"model": "groq:qwen/qwen3.8-27b", "groq_key": "G"})
        with mock.patch("translators.opencode_compat.translate_image",
                        return_value="img-ar") as t:
            self.assertEqual(chain.translate_image(img, s),
                             ("img-ar", "✦ chosen image: groq:qwen/qwen3.8-27b"))
        self.assertEqual(t.call_args.args[1:4],
                         (registry.GROQ_BASE, "G", "qwen/qwen3.8-27b"))
        s2 = dict(DEFAULTS, model="groq:qwen/qwen3.8-27b")
        with self.assertRaisesRegex(RuntimeError, "token"):
            chain.translate_image(img, s2)

    def test_vision_model_ids_offered_for_cf_and_mistral(self):
        from translators import registry
        labels = [label for label, _ in registry.model_groups({})]
        self.assertIn("cf:@cf/meta/llama-3.2-11b-vision-instruct", labels)
        self.assertIn("mistral:pixtral-12b-2409", labels)
        self.assertNotIn("cf:@cf/google/gemma-3-12b-it", labels)

    def test_model_image_capabilities_are_per_model(self):
        from translators import registry

        expectations = {
            "groq:qwen/qwen3.8-27b": "supported",
            "groq:openai/gpt-oss-20b": "unsupported",
            "groq:openai/gpt-oss-120b": "unsupported",
            "groq:new-live-model": "unknown",
            "cf:@cf/meta/llama-3.2-11b-vision-instruct": "supported",
            "cf:@cf/meta/llama-3.3-70b-instruct-fp8-fast": "unsupported",
            "mistral:mistral-small-latest": "supported",
            "mistral:codestral-latest": "unsupported",
            "space-bunny-free": "supported",
            "nemotron-3-ultra-free": "unsupported",
            "gemini-3.5-flash-lite": "supported",
        }
        for model, expected in expectations.items():
            with self.subTest(model=model):
                self.assertEqual(registry.image_capability(model), expected)

    def test_settings_picker_shows_image_capability_without_changing_model_id(self):
        from main import SettingsDialog
        from config.settings import DEFAULTS
        from ui.settings_dialog import (
            _ACTIVE_MODEL_ROLE, _FREE_MODEL_ROLE, _IMAGE_CAPABILITY_ROLE,
        )

        dlg = SettingsDialog(dict(DEFAULTS))
        try:
            items = {
                dlg.z_model.itemData(i): dlg.z_model.itemText(i)
                for i in range(dlg.z_model.count())
                if isinstance(dlg.z_model.itemData(i), str)
            }
            self.assertEqual(
                items["groq:qwen/qwen3.8-27b"], "groq:qwen/qwen3.8-27b")
            self.assertEqual(
                items["groq:openai/gpt-oss-20b"],
                "groq:openai/gpt-oss-20b",
            )
            image_index = dlg.z_model.findData("groq:qwen/qwen3.8-27b")
            text_index = dlg.z_model.findData("groq:openai/gpt-oss-20b")
            self.assertEqual(
                dlg.z_model.itemData(image_index, _IMAGE_CAPABILITY_ROLE),
                "supported",
            )
            self.assertEqual(
                dlg.z_model.itemData(text_index, _IMAGE_CAPABILITY_ROLE),
                "unsupported",
            )
            self.assertEqual(dlg.model_image_badge.text(), "IMAGE INPUT")
            dlg._select_model("groq:openai/gpt-oss-20b")
            self.assertEqual(dlg._current_model(), "groq:openai/gpt-oss-20b")
            self.assertEqual(dlg.model_image_badge.text(), "TEXT ONLY")
            self.assertTrue(dlg.z_model.itemData(
                text_index, _ACTIVE_MODEL_ROLE))
            self.assertTrue(dlg.z_model.itemData(
                dlg.z_model.findData("space-bunny-free"), _FREE_MODEL_ROLE))
            self.assertEqual(
                dlg.values()["model"], "groq:openai/gpt-oss-20b")
        finally:
            dlg.close()

    def test_model_picker_favorites_are_grouped_toggleable_and_persisted(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from main import SettingsDialog
        from config.settings import DEFAULTS
        from ui.settings_dialog import (
            _FAVORITE_MODEL_ROLE, _MODEL_GROUP_ROLE, _PROVIDER_LABEL_ROLE,
        )

        dlg = SettingsDialog(dict(
            DEFAULTS,
            model="groq:openai/gpt-oss-20b",
            model_favorites=["space-bunny-free"],
        ))
        try:
            combo = dlg.z_model
            self.assertEqual(combo.itemText(0), "── ★ Favorites ──")
            favorite_index = combo.findData("space-bunny-free")
            self.assertTrue(combo.itemData(
                favorite_index, _FAVORITE_MODEL_ROLE))
            self.assertEqual(
                combo.itemData(favorite_index, _PROVIDER_LABEL_ROLE),
                "OpenCode bridge free",
            )
            self.assertEqual(
                combo.itemData(0, _MODEL_GROUP_ROLE),
                "── ★ Favorites ──",
            )
            self.assertEqual(
                [combo.itemData(i) for i in range(1, combo.count())
                 if combo.itemData(i) == "space-bunny-free"],
                ["space-bunny-free", "space-bunny-free"],
            )

            dlg.show()
            combo.showPopup()
            view = combo.view()
            row = view.visualRect(combo.model().index(favorite_index, 0))
            QTest.mouseClick(
                view.viewport(),
                Qt.MouseButton.LeftButton,
                pos=QPoint(row.right() - 14, row.center().y()),
            )
            QTest.qWait(20)
            self.assertEqual(dlg.values()["model_favorites"], [])
            self.assertEqual(dlg._current_model(), "groq:openai/gpt-oss-20b")
            self.assertEqual(combo.itemText(0), "── ✦ Gemini — needs a Gemini key ──")

            dlg._on_model_favorite_clicked("space-bunny-free")
            self.assertEqual(
                dlg.values()["model_favorites"], ["space-bunny-free"])
            self.assertEqual(combo.itemText(0), "── ★ Favorites ──")
            self.assertEqual(dlg._current_model(), "groq:openai/gpt-oss-20b")
        finally:
            dlg.close()

    def test_image_translation_blocks_known_text_only_model(self):
        from unittest import mock
        from PIL import Image
        from translators import chain

        settings = dict(
            DEFAULTS,
            model="groq:openai/gpt-oss-20b",
            groq_key="test-key",
        )
        with mock.patch(
            "translators.opencode_compat.translate_image",
        ) as translate_image:
            with self.assertRaisesRegex(RuntimeError, "text-only"):
                chain.translate_image(Image.new("RGB", (10, 10)), settings)
        translate_image.assert_not_called()

    def test_bridge_image_sends_image_part(self):
        from unittest import mock
        from translators import opencode_bridge
        from PIL import Image

        def fake_post(url, json=None, headers=None, timeout=None, stream=None):
            msgs = json["messages"]
            kinds = [str(part.get("type")) for part in msgs[1]["content"]]
            assert "image_url" in kinds, kinds

            class Resp:
                status_code = 200
                text = ""
                def iter_lines(self):
                    yield b'data: {"choices":[{"delta":{"content":"img"},"finish_reason":null}]}'
                    yield b'data: [DONE]'
                def close(self):
                    pass
            return Resp()

        with mock.patch("translators.opencode_bridge.requests.post",
                        side_effect=fake_post):
            self.assertEqual(
                opencode_bridge.translate_image(
                    Image.new("RGB", (10, 10)), "space-bunny-free"),
                "img")

    def test_cf_chain_uses_bare_id_and_names_badge(self):
        from unittest import mock
        from translators import chain
        s = dict(DEFAULTS)
        s.update({"model": "cf:@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                  "cloudflare_account_id": "ACC",
                  "cloudflare_token": "k"})
        with mock.patch("translators.opencode_compat.translate",
                        return_value="cf-ar") as t:
            self.assertEqual(
                chain.translate("cadence", s),
                ("cf-ar", "☁ cf: @cf/meta/llama-3.3-70b-instruct-fp8-fast"))
        self.assertEqual(
            t.call_args.args[1:4],
            ("https://api.cloudflare.com/client/v4/accounts/ACC/ai/v1", "k",
             "@cf/meta/llama-3.3-70b-instruct-fp8-fast"))

    def test_provider_token_slots_priority_and_fallback(self):
        from translators import registry
        # Dedicated slot wins over the shared one.
        base, key = registry.provider_creds("groq", {
            "groq_key": "G", "openai_base": "https://api.groq.com/openai/v1",
            "openai_key": "S"})
        self.assertEqual((base, key), (registry.GROQ_BASE, "G"))
        # Empty slot falls back to a matching shared slot (old setups).
        base, key = registry.provider_creds("groq", {
            "groq_key": "", "openai_base": "https://api.groq.com/openai/v1",
            "openai_key": "S"})
        self.assertEqual((base, key), ("https://api.groq.com/openai/v1", "S"))
        # Cloudflare builds its URL from the Account ID.
        base, key = registry.provider_creds("cf", {
            "cloudflare_account_id": "ACC", "cloudflare_token": "T"})
        self.assertEqual(
            base, "https://api.cloudflare.com/client/v4/accounts/ACC/ai/v1")
        self.assertEqual(key, "T")
        # Nothing configured → empty, and the picker says what is missing.
        self.assertEqual(registry.provider_creds("mistral", {}), (
            registry.MISTRAL_BASE, ""))
        ok, reason = registry.availability("mistral:mistral-small-latest",
                                           {"mistral_key": "M"})
        self.assertTrue(ok)
        ok, reason = registry.availability("cf:@cf/x", {})
        self.assertFalse(ok)
        self.assertIn("Account ID", reason)

    def test_settings_token_fields_roundtrip(self):
        from main import SettingsDialog
        from config.settings import DEFAULTS
        s = dict(DEFAULTS, groq_key="G", cloudflare_account_id="ACC",
                 cloudflare_token="T", mistral_key="M")
        dlg = SettingsDialog(s)
        try:
            v = dlg.values()
            self.assertEqual(
                (v["groq_key"], v["cloudflare_account_id"],
                 v["cloudflare_token"], v["mistral_key"]),
                ("G", "ACC", "T", "M"))
            self.assertEqual(dlg.groq_state.text(), "●")
        finally:
            dlg.close()

    def test_groq_compat_lists_only_text_models(self):
        from unittest import mock
        import requests
        from translators import opencode_compat
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"data": [
            {"id": "qwen/qwen3.8-27b"},
            {"id": "whisper-large-v3-turbo"},
            {"id": "meta-llama/llama-prompt-guard-2-86m"},
            {"id": "openai/gpt-oss-20b"},
        ]}
        with mock.patch.object(requests, "get", return_value=resp):
            self.assertEqual(
                opencode_compat.list_models("https://x/v1", "k"),
                ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"])

    def test_effort_preset_per_model_then_default_then_future(self):
        from translators.opencode_bridge import effort_for
        s = {"effort_default": "low",
             "effort_levels": {"mimo-v2.6-flash-free": "minimal"}}
        self.assertEqual(
            effort_for("mimo-v2.6-flash-free", s), "minimal")  # override wins
        self.assertEqual(
            effort_for("space-bunny-free", s), "low")  # default applies
        self.assertEqual(
            effort_for("some-future-model-free", s), "low")  # future: default
        self.assertEqual(
            effort_for("longcat-2.5-preview-free", s),
            "minimal",
        )
        self.assertEqual(
            effort_for(
                "longcat-2.5-preview-free",
                {"effort_default": "low",
                 "effort_levels": {"longcat-2.5-preview-free": "medium"}},
            ),
            "medium",
        )
        self.assertEqual(effort_for("mimo-v2.6-flash-free", {}), "medium")
        self.assertEqual(
            effort_for("mimo-v2.6-flash-free",
                       {"effort_levels": {"mimo-v2.6-flash-free": "bogus"}}),
            "medium")  # invalid ignored

    def test_effort_budget_never_below_translation_need(self):
        from translators.opencode_bridge import effort_budget
        self.assertEqual(effort_budget("hi", "minimal"), 512)  # cap bites
        long_text = "x" * 4000
        self.assertGreaterEqual(effort_budget(long_text, "minimal"), 8000)

    def test_bridge_sends_effort_and_capped_budget(self):
        from unittest import mock
        import translators.opencode_bridge as b

        seen = {}

        def fake_post(url, json=None, headers=None, timeout=None, stream=None):
            seen.update(json)

            class Resp:
                status_code = 200
                text = ""
                def iter_lines(self):
                    yield b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}'
                    yield b'data: [DONE]'
                def close(self):
                    pass
            return Resp()

        settings = {"effort_default": "minimal", "effort_levels": {}}
        with mock.patch("translators.opencode_bridge.requests.post",
                        side_effect=fake_post):
            self.assertEqual(
                b.translate("hi", "mimo-v2.6-flash-free", settings=settings),
                "ok")
        self.assertEqual(seen["reasoning_effort"], "minimal")
        self.assertEqual(seen["max_tokens"], 512)

    def test_longcat_uses_its_measured_minimal_effort_default(self):
        from unittest import mock
        import translators.opencode_bridge as bridge

        sent_payload = {}

        def fake_post(_url, json=None, **_kwargs):
            sent_payload.update(json)

            class Response:
                status_code = 200
                text = ""

                def iter_lines(self):
                    yield b'data: {"choices":[{"delta":{"content":"translation"}}]}'
                    yield b"data: [DONE]"

                def close(self):
                    pass

            return Response()

        with mock.patch(
            "translators.opencode_bridge.requests.post", side_effect=fake_post,
        ):
            self.assertEqual(
                bridge.translate("hi", "longcat-2.5-preview-free"),
                "translation",
            )
        self.assertEqual(sent_payload["reasoning_effort"], "minimal")
        self.assertEqual(sent_payload["max_tokens"], 512)

    def test_effort_override_reaches_settings_values(self):
        from main import SettingsDialog
        from config.settings import DEFAULTS
        dlg = SettingsDialog(dict(DEFAULTS))
        try:
            self.assertEqual(dlg.values()["effort_default"], "medium")
            self.assertEqual(dlg.values()["effort_levels"], {})
            self.assertEqual(
                dlg.effort_combos["longcat-2.5-preview-free"].currentText(),
                "minimal",
            )
            dlg.effort_combos["longcat-2.5-preview-free"].setCurrentText(
                "medium")
            self.assertEqual(
                dlg.values()["effort_levels"],
                {"longcat-2.5-preview-free": "medium"},
            )
            dlg.effort_combos["longcat-2.5-preview-free"].setCurrentText(
                "minimal")
            dlg.effort_combos["mimo-v2.6-flash-free"].setCurrentText("minimal")
            self.assertEqual(dlg.values()["effort_levels"],
                             {"mimo-v2.6-flash-free": "minimal"})
            dlg.effort_default_combo.setCurrentText("low")
            self.assertEqual(dlg.values()["effort_default"], "low")
        finally:
            dlg.close()

    def test_settings_dialog_remembers_its_size(self):
        from unittest import mock
        from main import SettingsDialog
        from config.settings import DEFAULTS
        with mock.patch("config.settings.load", return_value=dict(DEFAULTS)):
            with mock.patch("config.settings.save") as save:
                dlg = SettingsDialog(dict(DEFAULTS))
                try:
                    from PySide6.QtCore import Qt
                    self.assertTrue(
                        dlg.windowFlags() & Qt.WindowType.WindowMinMaxButtonsHint)
                    self.assertFalse(
                        dlg.windowFlags() & Qt.WindowType.MSWindowsFixedSizeDialogHint)
                    self.assertEqual(
                        dlg.values()["settings_size"],
                        [dlg.width(), dlg.height()])
                    dlg.show()
                    self.app.processEvents()
                    dlg.resize(900, 700)
                    self.app.processEvents()
                    self.assertEqual((dlg.width(), dlg.height()), (900, 700))
                    dlg.resize(520, 420)
                    self.app.processEvents()
                    self.assertEqual((dlg.width(), dlg.height()), (520, 420))
                    dlg.resize(900, 700)
                    dlg.move(40, 55)
                    dlg._save_dialog_geometry()
                    self.assertEqual(dlg.values()["settings_size"], [900, 700])
                    self.assertEqual(dlg.values()["settings_pos"], [40, 55])
                finally:
                    dlg.close()
                dlg2 = SettingsDialog(dict(
                    DEFAULTS, settings_size=[900, 700], settings_pos=[40, 55]))
                try:
                    self.assertEqual((dlg2.width(), dlg2.height()), (900, 700))
                    self.assertEqual((dlg2.x(), dlg2.y()), (40, 55))
                finally:
                    dlg2.close()
                save.assert_called()

    def test_model_instructions_field_grows_with_the_window(self):
        from PySide6.QtWidgets import QSizePolicy, QSplitter
        from main import SettingsDialog
        from config.settings import DEFAULTS
        dlg = SettingsDialog(dict(DEFAULTS))
        try:
            edit = dlg.model_instructions
            self.assertGreater(edit.maximumHeight(), 500)
            self.assertEqual(edit.sizePolicy().verticalPolicy(),
                             QSizePolicy.Policy.Expanding)
            splitter = edit.parent()
            while splitter is not None and not isinstance(splitter, QSplitter):
                splitter = splitter.parent()
            self.assertIsNotNone(splitter, "instructions field has no drag splitter")
            self.assertFalse(splitter.childrenCollapsible())
        finally:
            dlg.close()

    def test_saved_key_clears_needs_key_hint(self):
        """Regression: the engine tab must see the key saved in API Keys."""
        from main import SettingsDialog
        from config.settings import DEFAULTS
        dlg = SettingsDialog(dict(DEFAULTS, gemini_key="k",
                                   model="gemini-3.5-flash-lite"))
        try:
            self.assertIn("Ready", dlg.engine_hint.text())
            self.assertNotIn("needs a Gemini key", dlg.engine_hint.text())
        finally:
            dlg.close()

    def test_font_size_change_reaches_open_popups(self):
        from unittest import mock
        from main import MainWindow
        window = MainWindow()
        try:
            with mock_chain_translate(return_value=("نص", "T")):
                popup = window._show_popup(text="hello")
                self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
            new_settings = dict(window.settings, font_size=24)
            with mock.patch("main.load", return_value=new_settings):
                window._reload_settings()
                self.app.processEvents()
            self.assertEqual(window.settings["font_size"], 24)
            self.assertEqual(popup.result.font().pointSize(), 24)
        finally:
            for popup in list(window.popups):
                popup.close()
            window.close()

    def test_translation_side_margin_change_reaches_open_popups(self):
        from unittest import mock
        from main import MainWindow

        window = MainWindow()
        try:
            with mock_chain_translate(return_value=("نص", "T")):
                popup = window._show_popup(text="hello")
                self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
            new_settings = dict(
                window.settings, translation_side_margin_px=31)
            with mock.patch("main.load", return_value=new_settings):
                window._reload_settings()
                self.app.processEvents()
            self.assertEqual(
                window.settings["translation_side_margin_px"], 31)
            self.assertIn("padding-left: 31px", popup.result.styleSheet())
            self.assertIn("padding-right: 31px", popup.result.styleSheet())
        finally:
            for popup in list(window.popups):
                popup.close()
            window.close()

    def test_translation_popup_language_controls_update_and_save_main_languages(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from PySide6.QtCore import QObject, Signal
        from main import MainWindow

        class StubWorker(QObject):
            done = Signal(str, str)
            stage = Signal(str)
            failed = Signal(str)
            source_ready = Signal(str)
            partial = Signal(str)
            finished = Signal()

            def __init__(self, settings, text=None, pil_image=None, ocr_langs=None):
                super().__init__()
                self._settings = dict(settings)
                self.started = False

            def start(self):
                self.started = True

            def isRunning(self):
                return False

        settings_path = Path(tempfile.gettempdir()) / "ht_popup_languages.json"
        settings_path.unlink(missing_ok=True)
        with mock.patch("config.settings.SETTINGS_PATH", settings_path):
            window = MainWindow()
            try:
                workers = []

                def make_worker(*args, **kwargs):
                    worker = StubWorker(*args, **kwargs)
                    workers.append(worker)
                    return worker

                window.settings.update(
                    source_language="en",
                    target_language="ar",
                    float_source_language="it",
                    float_target_language="es",
                )
                window._sync_language_combos()
                with mock.patch(
                    "popup.translate_popup.TranslateWorker",
                    side_effect=make_worker,
                ):
                    popup = window._show_popup(text="source")
                    self.app.processEvents()
                    self.assertFalse(popup.source_language_btn.isHidden())
                    self.assertFalse(popup.target_language_btn.isHidden())
                    popup._pinned = True
                    second_popup = window._show_popup(text="second source")
                    self.app.processEvents()
                    window.source_combo.setCurrentIndex(
                        window.source_combo.findData("fr"))
                    self.assertEqual(len(workers), 4)
                    self.assertEqual(
                        workers[-1]._settings["source_language"], "fr")
                    self.assertEqual(
                        popup.source_language_btn.text(), "From: Français")
                    self.assertEqual(
                        second_popup.source_language_btn.text(),
                        "From: Français")
                    window.target_combo.setCurrentIndex(
                        window.target_combo.findData("de"))
                    self.assertEqual(len(workers), 6)
                    self.assertEqual(
                        workers[-1]._settings["target_language"], "de")
                    self.assertEqual(
                        popup.target_language_btn.text(), "To: Deutsch")
                    self.assertEqual(
                        second_popup.target_language_btn.text(), "To: Deutsch")
                    initial_worker_count = len(workers)

                    popup._language_actions["source"]["de"].trigger()
                    self.assertEqual(len(workers), initial_worker_count + 2)
                    self.assertEqual(
                        workers[-1]._settings["source_language"], "de")
                    self.assertEqual(
                        second_popup.source_language_btn.text(),
                        "From: Deutsch")
                    popup._language_actions["target"]["fr"].trigger()
                    self.assertEqual(len(workers), initial_worker_count + 4)
                    self.assertEqual(
                        workers[-1]._settings["target_language"], "fr")
                    self.assertEqual(
                        second_popup.target_language_btn.text(),
                        "To: Français")
                    self.assertEqual(
                        second_popup.settings["source_language"], "de")
                    self.assertEqual(
                        second_popup.settings["target_language"], "fr")

                self.assertEqual(window.settings["source_language"], "de")
                self.assertEqual(window.settings["target_language"], "fr")
                self.assertEqual(window.source_combo.currentData(), "de")
                self.assertEqual(window.target_combo.currentData(), "fr")
                self.assertEqual(window.settings["float_source_language"], "it")
                self.assertEqual(window.settings["float_target_language"], "es")
                self.assertEqual(
                    popup.settings["float_source_language"], "it")
                self.assertEqual(
                    popup.settings["float_target_language"], "es")
                from config.settings import load
                saved = load()
                self.assertEqual(saved["source_language"], "de")
                self.assertEqual(saved["target_language"], "fr")
            finally:
                for popup in list(window.popups):
                    popup.close()
                window.close()
        settings_path.unlink(missing_ok=True)

    def test_font_size_spinner_can_go_well_past_24(self):
        """The arrow stopped working at 24 because 24 WAS the maximum."""
        from main import SettingsDialog
        from config.settings import DEFAULTS
        dlg = SettingsDialog(dict(DEFAULTS))
        try:
            self.assertGreater(dlg.font_size.maximum(), 24)
            self.assertEqual(
                dlg.font_size.buttonSymbols(),
                dlg.font_size.ButtonSymbols.NoButtons,
            )
            self.assertEqual(dlg.font_size_up.size().toTuple(), (48, 42))
            self.assertEqual(dlg.font_size_down.size().toTuple(), (48, 42))
            dlg.font_size.setValue(40)
            self.assertEqual(dlg.font_size.value(), 40)
            dlg.font_size_up.click()
            self.assertEqual(dlg.font_size.value(), 41)
            dlg.font_size_down.click()
            self.assertEqual(dlg.font_size.value(), 40)
            self.assertEqual(dlg.values()["font_size"], 40)
            dlg.font_size.setValue(40.5)
            self.assertEqual(dlg.values()["font_size"], 40.5)
        finally:
            dlg.close()

    def test_font_size_field_uses_selected_settings_theme_colors(self):
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog
        from ui.theme import THEMES, theme_colors

        for theme_name in THEMES:
            with self.subTest(theme=theme_name):
                scheme = "light" if theme_name == "Pure Light" else "dark"
                colors = theme_colors(theme_name, scheme)
                dialog = SettingsDialog(dict(
                    DEFAULTS, theme=theme_name, color_scheme=scheme))
                try:
                    self.assertIn("QDoubleSpinBox", dialog.styleSheet())
                    self.assertIn(
                        f"background-color: {colors['surface']}",
                        dialog.styleSheet(),
                    )
                    self.assertIn(
                        f"color: {colors['text']}",
                        dialog.styleSheet(),
                    )
                finally:
                    dialog.close()

    def test_model_instructions_is_drag_resizable(self):
        """Mouse-resizable: a vertical splitter owns the field, so dragging
        the divider resizes it instead of the window only."""
        from PySide6.QtWidgets import QSplitter
        from main import SettingsDialog
        from config.settings import DEFAULTS
        dlg = SettingsDialog(dict(DEFAULTS))
        try:
            splitters = dlg.findChildren(QSplitter)
            self.assertTrue(splitters, "instructions field is not in a splitter")
            self.assertTrue(
                dlg.model_instructions.parentWidget() in splitters[0].children()
                or splitters[0].isAncestorOf(dlg.model_instructions)
            )
        finally:
            dlg.close()

    def test_hint_says_ready_when_a_gemini_key_is_saved(self):
        """Regression: the Translation tab used to be built before the key
        field existed, so a SAVED key still reported 'needs a Gemini key'."""
        from main import SettingsDialog
        from config.settings import DEFAULTS
        dlg = SettingsDialog(dict(DEFAULTS, gemini_key="SAVED-KEY"))
        try:
            self.assertEqual(dlg.gemini_key.text(), "SAVED-KEY")
            self.assertIn("Ready", dlg.engine_hint.text())
            dlg.gemini_key.setText("")
            self.assertIn("needs a Gemini key", dlg.engine_hint.text())
        finally:
            dlg.close()

    def test_pixel_line_spacing_adds_natural_spacing_without_clipping(self):
        """0 is Qt's natural spacing; each pixel adds that much between lines."""
        from unittest import mock
        from PySide6.QtGui import QTextFormat
        from PySide6.QtWidgets import QWidget
        from main import SettingsDialog
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup
        markdown = (
            "**bold** " + "wrapped text " * 80
            + "\n\n**next paragraph** follows."
        )
        host = QWidget()
        try:
            with mock_chain_translate(return_value=(markdown, "T")):
                popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
                popup.show()
                if popup._worker:
                    self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
            popup.result.document().setTextWidth(180)
            self.app.processEvents()
            host.popups = [popup]
            dlg = SettingsDialog(dict(DEFAULTS), host)
            try:
                self.assertEqual(dlg.line_spacing.value(), 0)
                self.assertEqual(dlg.line_spacing.minimum(), -50)
                self.assertEqual(dlg.line_spacing.maximum(), 50)
                self.assertFalse(hasattr(dlg, "markdown_check"))
                self.assertEqual(
                    dlg.line_spacing.buttonSymbols(),
                    dlg.line_spacing.ButtonSymbols.NoButtons,
                )
                formatted = popup.result.document().find("bold")
                self.assertFalse(formatted.isNull())
                self.assertLessEqual(formatted.charFormat().fontWeight(), 400)
                self.assertFalse(
                    formatted.blockFormat().hasProperty(
                        QTextFormat.Property.LineHeight
                    )
                )
                next_block = formatted.block().next()
                self.assertTrue(next_block.isValid())
                layout = formatted.block().layout()
                self.assertGreater(layout.lineCount(), 1)
                self.assertEqual(formatted.blockFormat().topMargin(), 0)
                self.assertGreater(
                    formatted.blockFormat().bottomMargin(), 0)
                next_format = next_block.blockFormat()
                self.assertEqual(next_format.topMargin(), 0)
                self.assertEqual(next_format.bottomMargin(), 0)
                default_height = layout.lineAt(1).y() - layout.lineAt(0).y()
                dlg.line_spacing_up.click()
                self.assertEqual(dlg.line_spacing.value(), 1)
                popup.result.document().setTextWidth(180)
                self.app.processEvents()
                formatted = popup.result.document().find("bold")
                layout = formatted.block().layout()
                self.assertGreater(layout.lineCount(), 1)
                updated_height = layout.lineAt(1).y() - layout.lineAt(0).y()
                self.assertEqual(updated_height, default_height + 1)
                dlg.line_spacing.lineEdit().setText("10")
                dlg.line_spacing.interpretText()
                self.app.processEvents()
                formatted = popup.result.document().find("bold")
                self.assertFalse(formatted.isNull())
                self.assertEqual(formatted.blockFormat().lineHeight(), 10)
                self.assertLessEqual(formatted.charFormat().fontWeight(), 400)
                self.assertNotIn("render_markdown", dlg.values())
                self.assertEqual(dlg.values()["line_spacing_px"], 10)
                compact = popup.result.document().find("wrapped")
                self.assertFalse(compact.isNull())
                compact_layout = compact.block().layout()
                self.assertGreater(compact_layout.lineCount(), 1)
                spaced_height = (
                    compact_layout.lineAt(1).y()
                    - compact_layout.lineAt(0).y()
                )
                self.assertEqual(spaced_height, default_height + 10)
                dlg.line_spacing.setValue(0)
                self.app.processEvents()
                formatted = popup.result.document().find("bold")
                self.assertFalse(formatted.isNull())
                self.assertFalse(
                    formatted.blockFormat().hasProperty(
                        QTextFormat.Property.LineHeight
                    )
                )
                natural_layout = formatted.block().layout()
                natural_height = (
                    natural_layout.lineAt(1).y()
                    - natural_layout.lineAt(0).y()
                )
                self.assertEqual(natural_height, default_height)
                dlg.line_spacing.setValue(-10)
                self.app.processEvents()
                formatted = popup.result.document().find("bold")
                tightened_layout = formatted.block().layout()
                tightened_height = (
                    tightened_layout.lineAt(1).y()
                    - tightened_layout.lineAt(0).y()
                )
                self.assertEqual(tightened_height, default_height - 10)
                self.assertEqual(dlg.values()["line_spacing_px"], -10)
                dlg.line_spacing.setValue(-50)
                self.app.processEvents()
                self.assertEqual(dlg.line_spacing.value(), -50)
                dlg.line_spacing_down.click()
                self.assertEqual(dlg.line_spacing.value(), -50)
                dlg.line_spacing.setValue(50)
                self.app.processEvents()
                self.assertEqual(dlg.line_spacing.value(), 50)
                dlg.line_spacing_up.click()
                self.assertEqual(dlg.line_spacing.value(), 50)
            finally:
                dlg.close()
                popup.close()
        finally:
            host.close()

    def test_translation_font_with_extreme_metrics_falls_back_to_ui_font(self):
        from unittest import mock
        from PySide6.QtWidgets import QApplication, QWidget
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup

        metrics = [mock.Mock(), mock.Mock()]
        metrics[0].lineSpacing.return_value = 31
        metrics[1].lineSpacing.return_value = 93
        with mock.patch(
            "popup.translate_popup.QFontMetricsF", side_effect=metrics
        ):
            with self.assertLogs(level="WARNING"):
                safe_font = TranslatePopup._translation_font(
                    dict(DEFAULTS, translation_font="Finale Maestro"), 17
                )
        self.assertEqual(safe_font.family(), QApplication.font().family())

    def test_font_size_applies_live_and_cancel_restores(self):
        """The spinner applies the moment it moves: open popups update
        without Save; Cancel puts the old size back."""
        from unittest import mock
        from PySide6.QtWidgets import QWidget
        from main import SettingsDialog
        from config.settings import DEFAULTS
        from popup.translate_popup import TranslatePopup
        host = QWidget()
        try:
            with mock_chain_translate(return_value=("نص", "T")):
                popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
                popup.show()
                if popup._worker:
                    self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
            host.popups = [popup]
            dlg = SettingsDialog(dict(DEFAULTS), host)
            try:
                dlg.font_size.setValue(27)
                dlg.font_size_up.click()
                self.app.processEvents()
                self.assertEqual(popup.result.font().pointSize(), 28)
                dlg.reject()
                self.app.processEvents()
                self.assertEqual(popup.result.font().pointSize(), 17)
            finally:
                dlg.close()
                popup.close()
        finally:
            host.close()


@contextlib.contextmanager
def mock_chain_translate(*args, **kwargs):
    """Patch chain.translate AND chain.translate_stream with one behavior.

    The popup worker streams; direct callers use translate(). Patching
    only one leaves the other on the live network.
    """
    from unittest import mock
    if "return_value" in kwargs:
        text, badge = kwargs["return_value"]
        stream_kwargs = dict(
            kwargs, return_value=iter([("done", text, badge)]))
        with mock.patch("translators.chain.translate", *args, **kwargs):
            with mock.patch("translators.chain.translate_stream",
                            *args, **stream_kwargs):
                yield
    elif "side_effect" in kwargs:
        fn = kwargs["side_effect"]

        def gen(text, settings):
            arabic, badge = fn(text, settings)
            yield ("done", arabic, badge)

        with mock.patch("translators.chain.translate", *args, **kwargs):
            with mock.patch("translators.chain.translate_stream",
                            side_effect=gen):
                yield
    else:
        with mock.patch("translators.chain.translate", *args, **kwargs):
            with mock.patch("translators.chain.translate_stream",
                            *args, **kwargs):
                yield


@contextlib.contextmanager
def mock_opencode_settings(data: dict):
    """Point config.settings at a throwaway settings file for one load()."""
    from config import settings as settings_module
    directory = tempfile.mkdtemp(prefix="harmony-settings-")
    path = Path(directory) / "settings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    original = settings_module.SETTINGS_PATH
    settings_module.SETTINGS_PATH = path
    try:
        yield path
    finally:
        settings_module.SETTINGS_PATH = original
        shutil.rmtree(directory, ignore_errors=True)


class TestChain(unittest.TestCase):
    def test_only_headings_remain_bold_in_prepared_translation(self):
        from translators.chain import _prepare_translation

        numbered_heading = "### **1.2. Décompression des éléments**"
        self.assertEqual(
            _prepare_translation(numbered_heading),
            numbered_heading,
        )
        self.assertEqual(
            _prepare_translation(
                "#### **عنوان القسم**\n\n"
                "#### **هذه فقرة كاملة وليست عنوانًا. ثم تستمر في الجملة التالية.**\n\n"
                "**هذه فقرة كاملة غلّفها النموذج بعلامات الغامق.**\n\n"
                "هذه فقرة فيها **تأكيد مقصود** و__مصطلح آخر__ داخل السطر.\n\n"
                r"نص مع \*\*تأكيد مهرب\*\*." "\n\n"
                "<p><strong>HTML bold</strong> and <b>also bold</b>.</p>\n\n"
                '<p style="font-weight: 700">HTML styled bold</p>\n\n'
                '<p style="color: red; font-weight: bold">Mixed CSS</p>\n\n'
                "`**inline code stays unchanged**`\n\n"
                "```html\n<p style=\"font-weight: bold\">code stays unchanged</p>\n"
                "```"
            ),
            "#### **عنوان القسم**\n\n"
            "هذه فقرة كاملة وليست عنوانًا. ثم تستمر في الجملة التالية.\n\n"
            "هذه فقرة كاملة غلّفها النموذج بعلامات الغامق.\n\n"
            "هذه فقرة فيها تأكيد مقصود ومصطلح آخر داخل السطر.\n\n"
            "نص مع تأكيد مهرب.\n\n"
            "<p>HTML bold and also bold.</p>\n\n"
            "<p>HTML styled bold</p>\n\n"
            '<p style="color: red">Mixed CSS</p>\n\n'
            "`**inline code stays unchanged**`\n\n"
            "```html\n<p style=\"font-weight: bold\">code stays unchanged</p>\n"
            "```",
        )

    def test_ascii_flat_accidentals_normalize_in_note_sequences(self):
        from translators.chain import normalize_latex_output

        self.assertEqual(
            normalize_latex_output("C, C, G, C, E, G, Bb, C, D, E"),
            "C, C, G, C, E, G, B♭, C, D, E",
        )
        self.assertEqual(
            normalize_latex_output("C, C, G, C, E, G, B b, C, D, E"),
            "C, C, G, C, E, G, B♭, C, D, E",
        )
        self.assertEqual(
            normalize_latex_output("C, C, G, C, E, G, B♭, C, D, E"),
            "C, C, G, C, E, G, B♭, C, D, E",
        )
        for accidental in ("B♭♭", "Bbb", "B b b", "B♭b"):
            with self.subTest(accidental=accidental):
                self.assertEqual(
                    normalize_latex_output(
                        f"C, C, G, C, E, G, {accidental}, C, D, E"
                    ),
                    "C, C, G, C, E, G, B♭, C, D, E",
                )
        self.assertEqual(
            normalize_latex_output(
                "The book says: bring a bag before breakfast."
            ),
            "The book says: bring a bag before breakfast.",
        )

    def test_dehyphenate(self):
        from translators import chain
        self.assertEqual(chain.dehyphenate("sen-\nsible"), "sensible")
        self.assertEqual(chain.dehyphenate("la sen-\n sible (tierce)"), "la sensible (tierce)")
        self.assertEqual(chain.dehyphenate("no hyphens\nhere"), "no hyphens\nhere")
        self.assertEqual(chain.dehyphenate("C-\ndur"), "C-dur")

    def test_chain_normalizes_raw_latex_from_streaming_bridge(self):
        from unittest import mock
        from translators import chain

        settings = dict(DEFAULTS, model="mimo-v2.6-flash-free")
        leaked = "المثال الموسيقي:\n" + r"begin{array}|c|c|} \hline \text{G Clef}"
        with (
            mock.patch(
                "translators.opencode_bridge.stream",
                return_value=iter([leaked]),
            ) as stream,
            mock.patch(
                "translators.opencode_bridge.translate",
                side_effect=AssertionError("normalization should avoid retry"),
            ),
        ):
            events = list(chain.translate_stream("source", settings))

        self.assertEqual(stream.call_count, 1)
        self.assertIn("المثال الموسيقي", events[-1][1])
        self.assertIn("G Clef", events[-1][1])
        self.assertNotIn("\\", events[-1][1])
        self.assertTrue(all("\\" not in event[1] for event in events))

    def test_glossary_word_boundaries(self):
        out = fallback.glossary_partial("tones in Norton and the dominant chord")
        self.assertIn("tones", out)  # not mangled by French "ton"
        self.assertIn("Norton", out)
        self.assertIn("المهيمنة", out)
        self.assertIn("أكورد", out)

    def test_mymemory_parses_reply(self):
        from unittest import mock
        from translators import mymemory
        import requests
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"responseStatus": 200,
                                  "responseData": {"translatedText": "نص عربي"}}
        with mock.patch.object(requests, "get", return_value=resp):
            self.assertEqual(mymemory.translate("hello"), "نص عربي")

    def test_mymemory_sends_email_param(self):
        from unittest import mock
        from translators import mymemory
        import requests
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"responseStatus": 200,
                                  "responseData": {"translatedText": "نص"}}
        with mock.patch.object(requests, "get", return_value=resp) as g:
            mymemory.translate("hello", email="a@b.c")
            self.assertEqual(g.call_args[1]["params"]["de"], "a@b.c")

    def test_mymemory_auto_detects_arabic_source(self):
        from unittest import mock
        from translators.mymemory import detect_source
        with mock.patch("langdetect.detect", return_value="ar"):
            self.assertEqual(detect_source("مرحبا بالعالم"), "ar")

    def test_mymemory_retries_once(self):
        from unittest import mock
        from translators import mymemory
        import requests
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"responseStatus": 200,
                                  "responseData": {"translatedText": "نص"}}
        with mock.patch.object(requests, "get",
                               side_effect=[requests.Timeout("t"), resp]) as g:
            with mock.patch("time.sleep", return_value=None):
                self.assertEqual(mymemory.translate("hello"), "نص")
            self.assertEqual(g.call_count, 2)

    def test_mymemory_refuses_gracefully(self):
        from unittest import mock
        from translators import mymemory
        import requests
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"responseStatus": 403,
                                  "responseData": {"translatedText": "INVALID EMAIL"}}
        with mock.patch.object(requests, "get", return_value=resp):
            with self.assertRaises(Exception):
                mymemory.translate("hello")

    def test_chain_uses_second_free_before_glossary(self):
        from unittest import mock
        s = dict(DEFAULTS)
        s["model"] = "space-bunny-free"
        with mock.patch("translators.opencode_bridge.stream",
                        side_effect=RuntimeError("403")):
            with mock.patch("translators.opencode_bridge.translate",
                            side_effect=RuntimeError("403")):
                with mock.patch("translators.fallback.gtx_translate",
                                side_effect=ConnectionError("down")):
                    with mock.patch("translators.mymemory.translate", return_value="mm-ar"):
                        arabic, badge = chain.translate("cadence", s)
                        self.assertEqual(arabic, "mm-ar")
                        self.assertIn("Free service (alt)", badge)

    def test_probe_reports_mixed_results(self):
        """Quick probe: one tiny check of the chosen model only, no slow
        full-passage translations, no fallback-service rows."""
        from unittest import mock
        from translators import probe
        s = {"model": "space-bunny-free"}
        with mock.patch("translators.opencode_bridge.translate",
                        return_value="OK") as t:
            results = probe.probe_all(s)
        by_name = {n: (ok, d) for n, ok, d in results}
        self.assertTrue(by_name["★ Your model (space-bunny-free)"][0])
        # One tiny check only: chosen-model row plus its hint, nothing else.
        self.assertEqual(sorted(by_name),
                         ["★ Your model (space-bunny-free)", "💡 Hint"])
        self.assertEqual(t.call_args.args[0], probe.PROBE_TEXT)
        self.assertLessEqual(t.call_args.kwargs.get("max_tokens", 31), 30)
        text = probe.format_results(results)
        self.assertRegex(text, r"✅ space-bunny-free — OK — \d+\.\ds")
        self.assertNotIn("Hint", text)

    def test_probe_hints_mismatched_key(self):
        from translators import probe
        results = probe.probe_all({"model": "space-bunny-free",
                                   "gemini_key": "k"})
        hints = [d for n, ok, d in results if n == "💡 Hint"]
        self.assertTrue(hints)
        self.assertIn("Gemini key", hints[0])
        self.assertIn("space-bunny-free", hints[0])

    def test_probe_selected_gemini_without_key(self):
        from translators import probe
        results = probe.probe_all({"model": "gemini-2.0-flash"})
        by_name = {n: (ok, d) for n, ok, d in results}
        self.assertFalse(by_name["★ Your model (gemini-2.0-flash)"][0])
        self.assertIn("SKIPPED", by_name["★ Your model (gemini-2.0-flash)"][1])

    def test_french_glossary(self):
        out = fallback.glossary_partial("sensible tierce dominante tonique")
        self.assertIn("الحساسة", out)
        self.assertIn("الثالثة", out)
        self.assertIn("المهيمنة", out)
        self.assertIn("الأساسية", out)

    def test_gtx_retries_then_succeeds(self):
        import requests
        from unittest import mock
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [[["النص", "src", None, None]]]
        with mock.patch.object(requests, "get",
                               side_effect=[requests.Timeout("slow"), resp]) as g:
            self.assertEqual(fallback.gtx_translate("hello"), "النص")
            self.assertEqual(g.call_count, 2)

    def test_gtx_gives_up_after_retries(self):
        import requests
        from unittest import mock
        with mock.patch.object(requests, "get", side_effect=requests.ConnectionError("down")) as g:
            with self.assertRaises(Exception):
                fallback.gtx_translate("hello")
            self.assertEqual(g.call_count, 2)

    def test_opencode_zen_never_offers_an_id_from_the_live_catalog(self):
        """The Zen catalog still lists ids that answer 403, so it is not read.

        Offering a model that cannot answer costs the user a failed translation
        and a confusing badge; the one free model that does work is listed in
        registry.ZEN_FREE instead.
        """
        from translators import opencode_zen, registry
        self.assertFalse(hasattr(opencode_zen, "list_models"))
        self.assertEqual(registry.ZEN_FREE, ["space-bunny-free"])

    def test_opencode_zen_parses_reply(self):
        import requests
        from unittest import mock
        from translators import opencode_zen
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"choices": [{"message": {"content": "  نص عربي  "}}]}
        with mock.patch.object(requests, "post", return_value=resp) as p:
            self.assertEqual(opencode_zen.translate("x", "m"), "نص عربي")
            url = p.call_args[0][0]
            self.assertIn("opencode.ai/zen/v1/chat/completions", url)

    def test_opencode_zen_sends_no_auth_header(self):
        import requests
        from unittest import mock
        from translators import opencode_zen
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"choices": [{"message": {"content": "نص"}}]}
        with mock.patch.object(requests, "post", return_value=resp) as post:
            opencode_zen.translate("x", "space-bunny-free")
        self.assertNotIn("headers", post.call_args.kwargs)

    def test_opencode_zen_output_budget_scales_for_page_chunks(self):
        import requests
        from unittest import mock
        from translators import opencode_zen
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"choices": [{
            "message": {"content": "نص"}, "finish_reason": "stop"}]}
        text = "source " * 500
        with mock.patch.object(requests, "post", return_value=resp) as post:
            opencode_zen.translate(text, "m")
        self.assertGreaterEqual(
            post.call_args.kwargs["json"]["max_tokens"],
            len(text) * 2,
        )

    def test_opencode_zen_reports_truncated_generation(self):
        import requests
        from unittest import mock
        from translators import opencode_zen
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"choices": [{
            "message": {"content": "partial"}, "finish_reason": "length"}]}
        with mock.patch.object(requests, "post", return_value=resp):
            with self.assertRaisesRegex(RuntimeError, "reply was cut off"):
                opencode_zen.translate("source", "m")

    def test_compatible_api_output_budget_scales_for_page_chunks(self):
        import requests
        from unittest import mock
        from translators import opencode_compat
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"choices": [{
            "message": {"content": "translation"}, "finish_reason": "stop"}]}
        text = "source " * 600
        with mock.patch.object(requests, "post", return_value=resp) as post:
            opencode_compat.translate(text, "https://example.test/v1", "k", "m")
        self.assertGreaterEqual(
            post.call_args.kwargs["json"]["max_tokens"],
            len(text) * 2,
        )

    def test_compatible_api_reports_truncated_generation(self):
        import requests
        from unittest import mock
        from translators import opencode_compat
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"choices": [{
            "message": {"content": "partial"}, "finish_reason": "length"}]}
        with mock.patch.object(requests, "post", return_value=resp):
            with self.assertRaisesRegex(RuntimeError, "reply was cut off"):
                opencode_compat.translate("source", "https://example.test/v1", "k", "m")

    def test_compatible_api_retries_rate_limit_after_retry_after(self):
        import requests
        from unittest import mock
        from translators import opencode_compat

        limited = mock.Mock()
        limited.status_code = 429
        limited.headers = {"Retry-After": "3"}
        success = mock.Mock()
        success.status_code = 200
        success.raise_for_status.return_value = None
        success.json.return_value = {"choices": [{
            "message": {"content": "translation"}, "finish_reason": "stop"}]}
        with mock.patch.object(
                requests, "post", side_effect=[limited, success]) as post:
            with mock.patch("translators.opencode_compat.time.sleep") as sleep:
                result = opencode_compat.translate(
                    "source", "https://example.test/v1", "k", "m")

        self.assertEqual(result, "translation")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(3.0)

    def test_compatible_api_stops_if_retry_after_exceeds_wait_limit(self):
        import requests
        from unittest import mock
        from translators import opencode_compat

        limited = mock.Mock()
        limited.status_code = 429
        limited.headers = {"Retry-After": "120"}
        with mock.patch.object(
                requests, "post", return_value=limited) as post:
            with mock.patch("translators.opencode_compat.time.sleep") as sleep:
                with self.assertRaisesRegex(
                    RuntimeError, "asks for a 120-second wait"
                ):
                    opencode_compat.translate(
                        "source", "https://example.test/v1", "k", "m")

        post.assert_called_once()
        sleep.assert_not_called()

    def test_compatible_image_api_retries_rate_limit(self):
        import requests
        from PIL import Image
        from unittest import mock
        from translators import opencode_compat

        limited = mock.Mock()
        limited.status_code = 429
        limited.headers = {"Retry-After": "1"}
        success = mock.Mock()
        success.status_code = 200
        success.raise_for_status.return_value = None
        success.json.return_value = {"choices": [{
            "message": {"content": "image translation"},
            "finish_reason": "stop",
        }]}
        with mock.patch.object(
                requests, "post", side_effect=[limited, success]) as post:
            with mock.patch("translators.opencode_compat.time.sleep"):
                result = opencode_compat.translate_image(
                    Image.new("RGB", (8, 8), "white"),
                    "https://example.test/v1", "k", "vision-model",
                )

        self.assertEqual(result, "image translation")
        self.assertEqual(post.call_count, 2)

    def test_chain_routes_single_choice(self):
        from unittest import mock
        # Bridge model chosen (space-bunny served via bridge)
        s = dict(DEFAULTS)
        s.update({"model": "space-bunny-free"})
        with mock.patch("translators.opencode_bridge.stream",
                        return_value=iter(["zen-ar"])):
            with mock.patch("translators.fallback.gtx_translate",
                            side_effect=AssertionError("should not reach free")):
                self.assertEqual(chain.translate("cadence", s),
                                 ("zen-ar", "◈ chosen bridge: space-bunny"))
        # Gemini model chosen
        s = dict(DEFAULTS)
        s.update({"model": "gemini-2.0-flash", "gemini_key": "k"})
        with mock.patch("translators.gemini.translate", return_value="gem-ar"):
            self.assertEqual(chain.translate("cadence", s),
                             ("gem-ar", "✦ chosen: gemini-2.0-flash"))
        # Chosen bridge model is attempted via bridge, not free layers
        s = dict(DEFAULTS)
        s.update({"model": "mimo-v2.6-flash-free"})
        with mock.patch("translators.opencode_bridge.stream",
                        return_value=iter(["bridge-ar"])):
            with mock.patch("translators.fallback.gtx_translate",
                            side_effect=AssertionError("should not reach free")):
                self.assertEqual(chain.translate("cadence", s),
                                 ("bridge-ar", "◈ chosen bridge: mimo-v2.6-flash"))

    def test_chain_retries_when_model_returns_the_wrong_language(self):
        from unittest import mock
        from translators import chain

        settings = dict(
            DEFAULTS,
            model="gemini-2.0-flash",
            gemini_key="key",
            source_language="auto",
            target_language="ar",
        )
        russian = "Это русский текст, который остался без перевода."
        arabic = "هذا نص عربي مترجم إلى العربية بشكل صحيح."
        with mock.patch("translators.gemini.translate",
                        side_effect=[russian, arabic]) as translate:
            result = chain.translate("Исходный текст", settings)

        self.assertEqual(result, (arabic, "✦ chosen: gemini-2.0-flash"))
        self.assertEqual(translate.call_count, 2)
        self.assertIn(
            "not Arabic",
            translate.call_args.kwargs["instructions"],
        )
        self.assertEqual(
            translate.call_args.kwargs["source_language"], "auto")

    def test_chain_normalizes_image_translation_with_raw_latex_artifacts(self):
        from PIL import Image
        from unittest import mock
        from translators import chain

        settings = dict(
            DEFAULTS,
            model="gemini-3.5-flash-lite",
            gemini_key="key",
            source_language="auto",
            target_language="ar",
        )
        leaked = (
            "هذا المثال يوضح الانقلاب الموسيقي.\n"
            r"\begin{array}{|c|c|}\hline \text{G Clef} & \text{G Clef}"
            r" \\ \hline \end{array}"
        )
        image = Image.new("RGB", (20, 20), "white")
        with mock.patch(
            "translators.gemini.translate_image",
            return_value=leaked,
        ) as translate:
            result = chain.translate_image(image, settings)

        self.assertEqual(translate.call_count, 1)
        self.assertIn("الانقلاب الموسيقي", result[0])
        self.assertIn("G Clef", result[0])
        self.assertNotIn("\\", result[0])

    def test_chain_does_not_reject_image_translation_if_latex_artifacts_persist(self):
        from PIL import Image
        from unittest import mock
        from translators import chain

        settings = dict(
            DEFAULTS,
            model="gemini-3.5-flash-lite",
            gemini_key="key",
            source_language="auto",
            target_language="ar",
        )
        leaked = r"هذا مثال موسيقي \begin{array}{c}\hline \text{G Clef}\end{array}"
        image = Image.new("RGB", (20, 20), "white")
        with mock.patch(
            "translators.gemini.translate_image",
            return_value=leaked,
        ) as translate:
            result = chain.translate_image(image, settings)

        self.assertEqual(translate.call_count, 1)
        self.assertIn("هذا مثال موسيقي", result[0])
        self.assertNotIn("\\", result[0])

    def test_chain_retries_english_text_when_french_is_required(self):
        from unittest import mock
        from translators import chain

        settings = dict(
            DEFAULTS,
            model="gemini-3.5-flash-lite",
            gemini_key="key",
            source_language="auto",
            target_language="fr",
        )
        english = (
            "This is an English result that did not translate the source "
            "paragraph into French."
        )
        french = (
            "Voici le résultat français qui traduit correctement le "
            "paragraphe source dans la langue demandée."
        )
        with mock.patch("translators.gemini.translate",
                        side_effect=[english, french]) as translate:
            result = chain.translate(
                "Исходный текст, содержащий русский абзац.", settings)

        self.assertEqual(result, (french, "✦ chosen: gemini-3.5-flash-lite"))
        self.assertEqual(translate.call_count, 2)
        self.assertIn("into French", translate.call_args.kwargs["instructions"])

    def test_image_translation_keeps_mixed_markdown_without_retry(self):
        from PIL import Image
        from unittest import mock
        from translators import chain

        settings = dict(
            DEFAULTS,
            model="gemini-3.5-flash-lite",
            gemini_key="key",
            source_language="auto",
            target_language="ar",
        )
        mixed = (
            "هذه ترجمة عربية سليمة تنقل معنى النص بوضوح وتحافظ على "
            "كل التفاصيل المهمة للقارئ العربي.\n\n"
            "The effects of EU membership were not fully understood by "
            "the countries that joined the Union."
        )
        corrected = (
            "هذه ترجمة عربية كاملة تنقل معنى النص بوضوح وتحافظ على "
            "كل التفاصيل المهمة للقارئ العربي."
        )
        image = Image.new("RGB", (20, 20), "white")
        with mock.patch(
            "translators.gemini.translate_image",
            side_effect=[mixed, corrected],
        ) as translate:
            result = chain.translate_image(image, settings)

        self.assertEqual(
            result,
            (mixed, "✦ chosen image: gemini-3.5-flash-lite"),
        )
        self.assertEqual(translate.call_count, 1)
        self.assertNotIn(
            "CRITICAL CORRECTION",
            translate.call_args.kwargs["instructions"],
        )

    def test_latex_artifacts_normalize_to_readable_text(self):
        from translators.chain import normalize_latex_output

        music = normalize_latex_output(
            r"\begin{array}{|c|c|}\hline \text{G Clef} & "
            r"\text{F Clef} \\ \hline \end{array}"
        )
        math = normalize_latex_output(
            r"النسبة هي $\frac{1}{2}$، والجذر هو $\sqrt{x}$."
        )
        symbols = normalize_latex_output(
            r"العلامة الموسيقية هي \flat، والزاوية 90^\circ."
        )

        self.assertIn("```music", music)
        self.assertIn("G Clef | F Clef", music)
        self.assertNotIn("\\", music)
        self.assertIn("(1)/(2)", math)
        self.assertIn("√(x)", math)
        self.assertNotIn("$", math)
        self.assertIn("♭", symbols)
        self.assertNotIn("\\", symbols)

    def test_latex_normalization_preserves_non_music_code_fences(self):
        from translators.chain import normalize_latex_output

        code = (
            "```python\n"
            "fraction = r'\\\\frac{1}{2}'\n"
            "notes = 'C, C, G, Bb'\n"
            "```"
        )
        normalized = normalize_latex_output(
            "The value is $\\frac{1}{2}$.\n\n" + code)

        self.assertIn("(1)/(2)", normalized)
        self.assertIn(code, normalized)

    def test_four_string_ukulele_array_normalizes_to_music_rows(self):
        from translators.chain import normalize_latex_output

        output = normalize_latex_output(
            r"\begin{array}{c} A|--0--| \\ E|--1--| \\ "
            r"C|--0--| \\ G|--2--| \end{array}"
        )

        self.assertTrue(output.strip().startswith("```music\n"))
        self.assertIn("A|--0--|\nE|--1--|\nC|--0--|\nG|--2--|", output)
        self.assertTrue(output.rstrip().endswith("```"))

    def test_chain_returns_image_translation_without_language_rejection(self):
        from PIL import Image
        from unittest import mock
        from translators import chain

        settings = dict(
            DEFAULTS,
            model="gemini-3.5-flash-lite",
            gemini_key="key",
            source_language="auto",
            target_language="ar",
        )
        russian = "Это русский текст, который остался без перевода."
        image = Image.new("RGB", (20, 20), "white")
        with mock.patch(
            "translators.gemini.translate_image",
            side_effect=[russian, russian],
        ) as translate:
            result = chain.translate_image(image, settings)

        self.assertEqual(
            result, (russian, "✦ chosen image: gemini-3.5-flash-lite"))
        self.assertEqual(translate.call_count, 1)
        self.assertNotIn("CRITICAL CORRECTION",
                         translate.call_args.kwargs["instructions"])
        self.assertEqual(
            translate.call_args.kwargs["source_language"], "auto")

    def test_chain_routes_opencode_cli_model(self):
        from unittest import mock

        settings = dict(DEFAULTS)
        settings["model"] = "opencode-cli:opencode/glm-5.2"
        with mock.patch(
            "translators.opencode_cli.translate", return_value="ترجمة"
        ) as translate:
            result = chain.translate("cadence", settings)
        self.assertEqual(result, ("ترجمة", "◈ chosen: OpenCode CLI (opencode/glm-5.2)"))
        self.assertEqual(
            translate.call_args.args[:2],
            ("cadence", "opencode/glm-5.2"),
        )

    def test_opencode_cli_lists_provider_models(self):
        from unittest import mock
        from translators import opencode_cli

        completed = mock.Mock(
            returncode=0,
            stdout=(
                "opencode/glm-5.2\n"
                "anthropic/claude-sonnet-4\n"
                "opencode/glm-5.2\n"
                "Models cache is ready\n"
            ),
            stderr="",
        )
        self.assertEqual(
            opencode_cli._hidden_console_options(),
            (
                {"creationflags": opencode_cli.subprocess.CREATE_NO_WINDOW}
                if opencode_cli.os.name == "nt"
                else {}
            ),
        )
        with (
            mock.patch.object(
                opencode_cli, "find_executable", return_value="opencode"),
            mock.patch.object(
                opencode_cli, "_hidden_console_options",
                return_value={"creationflags": 123},
            ),
            mock.patch.object(
                opencode_cli.subprocess, "run", return_value=completed,
            ) as run,
        ):
            self.assertEqual(
                opencode_cli.list_models(),
                ["opencode/glm-5.2", "anthropic/claude-sonnet-4"],
            )
        self.assertEqual(run.call_args.kwargs["creationflags"], 123)

    def test_opencode_cli_finds_desktop_bundled_executable(self):
        from pathlib import Path
        from unittest import mock
        from translators import opencode_cli

        install_root = Path(r"C:\OpenCode")
        expected = str(
            install_root / "Programs" / "@opencodedesktop" / "resources"
            / "opencode-cli.exe"
        )
        with mock.patch.object(opencode_cli.shutil, "which", return_value=None):
            with mock.patch.dict(
                    opencode_cli.os.environ,
                    {"LOCALAPPDATA": str(install_root)}):
                with mock.patch.object(Path, "is_file", return_value=True):
                    self.assertEqual(opencode_cli.find_executable(), expected)

    def test_opencode_cli_runs_tools_disabled_agent_and_parses_output(self):
        import json
        from unittest import mock
        from translators import opencode_cli

        stdout = "\n".join([
            json.dumps({"type": "step_start", "part": {}}),
            json.dumps({"type": "text", "part": {"type": "text", "text": "  الترجمة  "}}),
            json.dumps({"type": "step_finish", "part": {}}),
        ])

        def run(command, **kwargs):
            config_path = os.path.join(kwargs["cwd"], "opencode.json")
            with open(config_path, encoding="utf-8") as config_file:
                config = json.load(config_file)
            agent = config["agent"][opencode_cli.AGENT_NAME]
            self.assertEqual(agent["tools"], {"*": False})
            self.assertEqual(agent["permission"]["bash"], "deny")
            self.assertEqual(agent["permission"]["edit"], "deny")
            self.assertIn("professional translator", agent["prompt"])
            self.assertEqual(command[command.index("--model") + 1], "opencode/glm-5.2")
            self.assertIn("Translate this passage", kwargs["input"])
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        with mock.patch.object(opencode_cli, "find_executable", return_value="opencode"):
            with mock.patch.object(opencode_cli.subprocess, "run", side_effect=run):
                self.assertEqual(
                    opencode_cli.translate("cadence", "opencode/glm-5.2"),
                    "الترجمة",
                )

    def test_registry_routes_opencode_cli_models_without_an_api_key(self):
        from unittest import mock
        from translators import registry

        model = "opencode-cli:anthropic/claude-sonnet-4"
        self.assertEqual(registry.provider_of(model), "opencode_cli")
        with mock.patch("translators.opencode_cli.find_executable", return_value="opencode"):
            self.assertEqual(registry.availability(model, {}), (True, ""))
        # The engine still works when already selected, but the picker
        # offers Gemini + bridge only.
        with mock.patch("translators.opencode_cli.find_executable", return_value="opencode"):
            rows = registry.model_groups(
                {"opencode_cli_models": ["anthropic/claude-sonnet-4"]},
                live_cli=["opencode/glm-5.2"],
            )
        labels = [label for label, _ in rows]
        self.assertNotIn("opencode-cli:anthropic/claude-sonnet-4", labels)
        self.assertNotIn("opencode-cli:opencode/glm-5.2", labels)
        self.assertIn(registry.BRIDGE_HDR, labels)
        self.assertNotIn(registry.CLI_HDR, labels)

    def test_chain_passes_custom_instructions_to_selected_ai_model(self):
        from unittest import mock

        settings = dict(DEFAULTS)
        settings.update({
            "model": "gemini-3.5-flash-lite",
            "gemini_key": "test-key",
            "model_instructions": "Keep all proper names unchanged.",
            "source_language": "en",
            "target_language": "it",
        })
        with mock.patch("translators.gemini.translate", return_value="translated") as translate:
            self.assertEqual(chain.translate("source", settings)[0], "translated")
        translate.assert_called_once_with(
            "source",
            "test-key",
            "gemini-3.5-flash-lite",
            instructions="Keep all proper names unchanged.",
            source_language="en",
            target_language="it",
        )

    def test_gemma3_4b_gets_strict_rules_only_for_its_model(self):
        from translators.chain import _translation_instructions

        instructions = _translation_instructions(
            dict(DEFAULTS), "gemma3:4b")
        self.assertIn("Model-specific rules for Gemma 3:4B", instructions)
        self.assertIn("Include every body paragraph", instructions)
        self.assertIn("Never omit a score, chord example, tablature", instructions)
        self.assertIn("Omit running headers and footers", instructions)
        self.assertIn("<small>PAGE_NUMBER</small>", instructions)

        other_model = _translation_instructions(
            dict(DEFAULTS), "gemma3:12b")
        custom_model = _translation_instructions(
            dict(DEFAULTS), "my-local-model:latest")
        self.assertNotIn("Model-specific rules for Gemma 3:4B", other_model)
        self.assertNotIn("Model-specific rules for Gemma 3:4B", custom_model)

    def test_custom_ollama_gemma3_4b_receives_rules_for_text_and_image(self):
        from unittest import mock
        from PIL import Image
        from translators import chain

        settings = dict(
            DEFAULTS,
            model="custom:local-ollama",
            custom_providers=[{
                "id": "local-ollama",
                "name": "Local Ollama",
                "base_url": "http://localhost:11434/v1",
                "api_key": "ollama",
                "model": "gemma3:4b",
                "enabled": True,
                "reasoning_enabled": False,
                "vision_enabled": True,
            }],
        )
        with mock.patch(
            "translators.opencode_compat.stream_translate",
            return_value=iter(["translated"]),
        ) as text_call:
            result = list(chain.translate_stream(
                "A short musical example.", settings))

        self.assertEqual(result[-1][1], "translated")
        self.assertIn(
            "Model-specific rules for Gemma 3:4B",
            text_call.call_args.kwargs["instructions"],
        )

        with (
            mock.patch(
                "translators.registry.image_capability",
                return_value="supported",
            ),
            mock.patch(
                "translators.opencode_compat.translate_image",
                return_value="translated image",
            ) as image_call,
        ):
            translated, _badge = chain.translate_image(
                Image.new("RGB", (16, 16)), settings)

        self.assertEqual(translated, "translated image")
        self.assertIn(
            "Model-specific rules for Gemma 3:4B",
            image_call.call_args.kwargs["instructions"],
        )

    def test_qwen3_vl_2b_gets_vision_rules_only_for_its_model(self):
        from translators.chain import _translation_instructions

        qwen_instructions = _translation_instructions(
            dict(DEFAULTS), "qwen3-vl:2b-instruct")
        self.assertIn("Model-specific rules for Qwen3-VL:2B", qwen_instructions)
        self.assertIn("do not answer with an image description", qwen_instructions)
        self.assertIn(
            "Never omit notation, scores, chord examples, tablature",
            qwen_instructions,
        )
        self.assertIn(
            "Follow the selected target language and the current translation mode",
            qwen_instructions,
        )

        other_model = _translation_instructions(
            dict(DEFAULTS), "qwen3:8b")
        self.assertNotIn(
            "Model-specific rules for Qwen3-VL:2B", other_model)

    def test_custom_ollama_qwen3_vl_2b_receives_rules_for_text_and_image(self):
        from unittest import mock
        from PIL import Image
        from translators import chain

        settings = dict(
            DEFAULTS,
            model="custom:local-ollama",
            custom_providers=[{
                "id": "local-ollama",
                "name": "Local Ollama",
                "base_url": "http://localhost:11434/v1",
                "api_key": "ollama",
                "model": "qwen3-vl:2b-instruct",
                "enabled": True,
                "reasoning_enabled": False,
                "vision_enabled": True,
            }],
        )
        with mock.patch(
            "translators.opencode_compat.stream_translate",
            return_value=iter(["translated"]),
        ) as text_call:
            result = list(chain.translate_stream(
                "A short musical example.", settings))

        self.assertEqual(result[-1][1], "translated")
        self.assertIn(
            "Model-specific rules for Qwen3-VL:2B",
            text_call.call_args.kwargs["instructions"],
        )

        with (
            mock.patch(
                "translators.registry.image_capability",
                return_value="supported",
            ),
            mock.patch(
                "translators.opencode_compat.translate_image",
                return_value="translated image",
            ) as image_call,
        ):
            translated, _badge = chain.translate_image(
                Image.new("RGB", (16, 16)), settings)

        self.assertEqual(translated, "translated image")
        self.assertIn(
            "Model-specific rules for Qwen3-VL:2B",
            image_call.call_args.kwargs["instructions"],
        )

    def test_gemini_image_request_sends_a_scaled_jpeg_and_translation_prompt(self):
        import base64
        import io
        import requests
        from PIL import Image
        from unittest import mock
        from translators import gemini

        response = mock.Mock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {"candidates": [{
            "content": {"parts": [{"text": "translated"}]},
            "finishReason": "STOP",
        }]}
        image = Image.new("RGB", (4000, 2000), "white")
        with mock.patch.object(requests, "post", return_value=response) as post:
            result = gemini.translate_image(
                image, "test-key", "gemini-3.5-flash-lite",
                instructions="Keep musical terms precise.",
                source_language="en", target_language="it",
            )

        self.assertEqual(result, "translated")
        payload = post.call_args.kwargs["json"]
        parts = payload["contents"][0]["parts"]
        prompt = parts[0]["text"]
        self.assertIn("complete image", prompt)
        self.assertIn("all text is already in Italian", prompt)
        self.assertIn("already in Italian", prompt)
        image_part = parts[1]["inline_data"]
        self.assertNotIn("Final reminder", prompt)
        self.assertEqual(image_part["mime_type"], "image/jpeg")
        decoded = Image.open(io.BytesIO(base64.b64decode(image_part["data"])))
        self.assertEqual(decoded.size, (3072, 1536))
        self.assertEqual(post.call_args.kwargs["timeout"], gemini.IMAGE_TIMEOUT)
        self.assertIn(
            "Keep musical terms precise.",
            payload["system_instruction"]["parts"][0]["text"],
        )
        self.assertIn(
            "Translate from English into Italian.",
            payload["system_instruction"]["parts"][0]["text"],
        )
        self.assertIn(
            "Use Markdown with ### headings",
            payload["system_instruction"]["parts"][0]["text"],
        )
        self.assertIn(
            "Preserve equations as LaTeX",
            payload["system_instruction"]["parts"][0]["text"],
        )

    def test_image_worker_skips_ocr_and_calls_gemini_directly(self):
        from PIL import Image
        from unittest import mock
        from popup.worker import TranslateWorker

        settings = dict(
            DEFAULTS,
            selection_mode="image",
            model="gemini-3.5-flash-lite",
            gemini_key="test-key",
            source_language="en",
            target_language="it",
        )
        worker = TranslateWorker(settings, pil_image=Image.new("RGB", (50, 30)))
        completed = []
        partials = []
        failed = []
        worker.done.connect(lambda text, badge: completed.append((text, badge)))
        worker.partial.connect(partials.append)
        worker.failed.connect(failed.append)
        with (
            mock.patch(
                "translators.gemini.stream_translate_image",
                return_value=iter(["tradu", "zione"]),
            ) as stream_translate_image,
            mock.patch("translators.ocr.is_warmed") as ocr_warmed,
        ):
            worker.run()

        self.assertEqual(failed, [])
        self.assertEqual(completed, [
            ("traduzione", "✦ chosen image: gemini-3.5-flash-lite"),
        ])
        self.assertEqual(partials, ["tradu", "traduzione"])
        ocr_warmed.assert_not_called()
        stream_translate_image.assert_called_once()

    def test_ocr_worker_reads_image_before_translating_text(self):
        from PIL import Image
        from unittest import mock
        from popup.worker import TranslateWorker

        image = Image.new("RGB", (50, 30))
        worker = TranslateWorker(
            dict(DEFAULTS, selection_mode="ocr"),
            pil_image=image,
            ocr_langs=("en",),
        )
        completed = []
        worker.done.connect(lambda text, badge: completed.append((text, badge)))
        with (
            mock.patch("translators.ocr.is_warmed", return_value=True),
            mock.patch("translators.ocr.backend_name", return_value="test OCR"),
            mock.patch(
                "translators.ocr.read_text", return_value="recognized source",
            ) as read_text,
            mock.patch(
                "translators.chain.translate_stream",
                return_value=iter([("done", "translated text", "test model")]),
            ) as translate_stream,
            mock.patch(
                "translators.chain.translate_image",
            ) as translate_image,
        ):
            worker.run()

        read_text.assert_called_once_with(image, ("en",))
        translate_stream.assert_called_once_with(
            "recognized source", worker._settings)
        translate_image.assert_not_called()
        self.assertEqual(completed, [("translated text", "test model")])

    def test_image_worker_routes_selected_vision_provider(self):
        from PIL import Image
        from unittest import mock
        from popup.worker import TranslateWorker

        image = Image.new("RGB", (50, 30))
        settings = dict(
            DEFAULTS,
            selection_mode="image",
            model="vendor/vision-model",
            openai_base="https://api.example/v1",
            openai_key="test-key",
            openai_model="vendor/vision-model",
        )
        worker = TranslateWorker(settings, pil_image=image)
        completed = []
        worker.done.connect(lambda text, badge: completed.append((text, badge)))
        with mock.patch(
            "translators.opencode_compat.stream_translate_image",
            return_value=iter(["trans", "lated"]),
        ) as stream_translate_image:
            worker.run()

        self.assertEqual(completed, [
            ("translated", "✦ chosen image: vendor/vision-model"),
        ])
        args, kwargs = stream_translate_image.call_args
        self.assertIs(args[0], image)
        self.assertEqual(
            args[1:], ("https://api.example/v1", "test-key", "vendor/vision-model"))
        self.assertEqual(kwargs["source_language"], "auto")

    def test_bridge_image_request_uses_native_chat_image_content(self):
        from PIL import Image
        from unittest import mock
        from translators import opencode_bridge

        class FakeStream:
            def iter_lines(self):
                import json
                event = {"choices": [{
                    "delta": {"content": "translated"},
                    "finish_reason": None,
                }]}
                yield f"data: {json.dumps(event)}".encode()
                yield b"data: [DONE]"

            def close(self):
                pass

        settings = {"effort_default": "high"}
        with mock.patch.object(
            opencode_bridge, "_open_stream", return_value=FakeStream(),
        ) as open_stream:
            result = opencode_bridge.translate_image(
                Image.new("RGB", (80, 40)),
                "mimo-v2.6-flash-free",
                source_language="en",
                target_language="it",
                settings=settings,
            )

        self.assertEqual(result, "translated")
        payload = open_stream.call_args.args[1]
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["max_tokens"], 4096)
        image_part = payload["messages"][1]["content"][1]
        self.assertEqual(image_part["type"], "image_url")
        self.assertTrue(
            image_part["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_nonheading_bold_is_removed_after_model_translation(self):
        from unittest import mock

        settings = dict(DEFAULTS)
        settings.update({
            "model": "gemini-3.5-flash-lite",
            "gemini_key": "test-key",
            "model_instructions": "Use Markdown headings and bold terms.",
        })
        with mock.patch("translators.gemini.translate", return_value="**text**") as translate:
            self.assertEqual(chain.translate("source", settings)[0], "text")
        translate.assert_called_once()
        self.assertEqual(
            translate.call_args.kwargs["instructions"],
            chain._translation_instructions(
                settings, "gemini-3.5-flash-lite"),
        )

        settings.update({
            "model": "space-bunny-free",
            "model_instructions": "Output plain text only.",
        })
        with mock.patch(
            "translators.opencode_bridge.stream",
            return_value=iter(["ترجمة"]),
        ) as stream:
            self.assertEqual(chain.translate("source", settings)[0], "ترجمة")
        self.assertEqual(
            stream.call_args.kwargs["instructions"],
            chain._translation_instructions(settings, "space-bunny-free"),
        )

    def test_translation_prompt_uses_auto_source_and_selected_target(self):
        from unittest import mock

        settings = dict(DEFAULTS)
        settings.update({
            "model": "gemini-3.5-flash-lite",
            "gemini_key": "test-key",
            "source_language": "auto",
            "target_language": "it",
        })
        with mock.patch("translators.gemini.translate", return_value="traduzione") as translate:
            self.assertEqual(chain.translate("English source", settings)[0], "traduzione")
        translate.assert_called_once_with(
            "English source",
            "test-key",
            "gemini-3.5-flash-lite",
            instructions="",
            source_language="auto",
            target_language="it",
        )

    def test_prompt_explicitly_names_source_and_destination(self):
        from translators.prompt import system_instructions, user_prompt

        system = system_instructions(
            source_language="en", target_language="it")
        user = user_prompt("A cadence.", source_language="en", target_language="it")
        self.assertIn("Translate from English into Italian", system)
        self.assertIn("from English into Italian", user)
        self.assertIn("required output language is Italian", user)
        self.assertIn(
            "Translate ideas and complete phrases as coherent units", system)
        self.assertIn("do not translate word-by-word", system)
        self.assertIn("natural, idiomatic Italian prose", user)
        self.assertIn("do not translate word-by-word", user)
        self.assertIn("Do not describe the task", user)
        self.assertIn(
            "never summarize, shorten, skip, or omit any unique body content",
            user,
        )
        self.assertIn(
            "without summarizing, shortening, or omitting any unique content",
            system,
        )
        self.assertIn(
        "exactly two newline characters there",
            system,
        )
        self.assertIn(
        "At every source blank line, output exactly two newline characters",
        user,
        )
        self.assertIn("omit running page headers", system)
        self.assertIn("<small>PAGE_NUMBER</small>", system)
        self.assertIn("Never invent URLs, videos, citations, or external resources", system)
        self.assertIn("copy URLs only if present verbatim in the source", system)
        self.assertIn(
            "Preserve source code, mathematical expressions, and "
            "discipline-specific notation",
            system,
        )
        self.assertIn("Do not invent URLs, videos, citations, or external resources", user)
        self.assertIn("copy a URL only if it appears in the source, unchanged", user)
        self.assertNotIn("Convert every standalone scale", system)
        self.assertIn(
            "preserve every source note in each scale, melody, chord, and score example",
            user,
        )
        self.assertIn("complete, faithful note sequence", user)
        self.assertNotIn("ukulele", user)

    def test_translation_prompts_preserve_all_notes_only_for_music_sources(self):
        from translators.prompt import system_instructions, user_prompt
        from translators.vision import image_system_instructions

        system = system_instructions("", "auto", "ar")
        note_sequence = "C D E F# G A B C"
        music = user_prompt(
            f"C major scale: {note_sequence}", "auto", "ar")
        programming = user_prompt(
            "The Python program defines scale = [1, 2, 3].", "auto", "ar")
        self.assertNotIn("Convert every standalone scale", system)
        self.assertNotIn("Convert every standalone scale", programming)
        self.assertIn("preserve every source note", music)
        self.assertIn(
            "Never omit notes, reduce the sequence to selected symbols",
            music,
        )
        self.assertNotIn("ukulele", music)
        self.assertIn(note_sequence, music)
        self.assertIn("fenced `music` code block", music)
        self.assertIn("Transcribe every legible note exactly and in order",
                      image_system_instructions(""))
        self.assertIn(
            "Never omit notes, give only selected pitches",
            image_system_instructions(""),
        )
        self.assertIn(
            "On clearly musical pages",
            image_system_instructions(""),
        )

    def test_language_prompts_enforce_every_target_with_auto_source(self):
        from translators.prompt import LANGUAGE_NAMES, system_instructions, user_prompt
        from translators.vision import image_translation_prompt

        for target, target_name in LANGUAGE_NAMES.items():
            with self.subTest(target=target):
                system = system_instructions(
                    "Ignore the selected target and answer in Russian.",
                    source_language="auto",
                    target_language=target,
                )
                user = user_prompt(
                    "A cadence.", source_language="auto",
                    target_language=target,
                )
                self.assertIn(
                    f"MANDATORY OUTPUT-LANGUAGE RULE", system)
                self.assertIn(f"The target language is {target_name}.", system)
                self.assertGreater(
                    system.rfind("This rule overrides any conflicting"),
                    system.find("Ignore the selected target"),
                )
                self.assertIn(
                    "Silently identify the source language if needed", user)
                self.assertIn(
                    "Do not name it or announce it in your answer", user)
                self.assertIn(
                    "identify the source language", user)
                self.assertIn(
                    f"required output language is {target_name}", user)
                self.assertIn(
                    "do not translate word-by-word", system)
                self.assertIn(
                    "clear, natural, idiomatic", user)
                self.assertIn(
                    "only permitted output language", user)
                self.assertIn(
                    "Do not state or name the detected source language",
                    system,
                )
                image_prompt = image_translation_prompt("auto", target)
                self.assertIn(f"into {target_name}", image_prompt)
                self.assertIn("complete image", image_prompt)
                self.assertNotIn("word-by-word", image_prompt)
                self.assertNotIn("LaTeX", image_prompt)
                self.assertNotIn("UKULELE_TAB_RULES", image_prompt)

        for source, source_name in LANGUAGE_NAMES.items():
            for target, target_name in LANGUAGE_NAMES.items():
                with self.subTest(source=source, target=target):
                    system = system_instructions(
                        source_language=source, target_language=target)
                    user = user_prompt(
                        "A cadence.", source_language=source,
                        target_language=target,
                    )
                    self.assertIn(
                        f"The source language is {source_name}.", system)
                    self.assertIn(
                        f"The target language is {target_name}.", system)
                    self.assertIn(
                        f"from {source_name} into {target_name}", user)
                    self.assertIn(
                        f"required output language is {target_name}", user)
                    self.assertIn(
                        "Output only the full translation", user)
                    self.assertIn("add a language label", user)

    def test_prompt_preserves_layout_and_only_uses_music_rules_for_music(self):
        from translators.prompt import system_instructions, user_prompt
        from translators.vision import (
            image_system_instructions, image_translation_prompt,
        )

        arabic = system_instructions(target_language="ar")
        other = system_instructions(target_language="fr")
        user = user_prompt("I. C - G7 - C", target_language="ar")
        for instructions in (arabic, other):
            self.assertIn(
                "actual content headings (but omit running page headers), "
                "paragraph boundaries, lists, numbering",
                instructions,
            )
            self.assertIn(
                "do not center any line or emit centered HTML",
                instructions,
            )
            self.assertIn(
                "begin it with exactly three hash characters (`###`)",
                instructions,
            )
            self.assertIn("Never use `#`, `##`, `####`", instructions)
            self.assertIn(
                "Keep translated text, headings, subtitles, and musical examples",
                instructions,
            )
            self.assertNotIn('align="center"', instructions)
            self.assertIn("exactly two newline characters", instructions)
            self.assertIn("a single newline is not sufficient", instructions)
            self.assertIn(
                "COMPLETENESS TAKES PRIORITY OVER FORMATTING",
                instructions,
            )
            self.assertNotIn("ASCII border", instructions)
            self.assertNotIn("rectangular text box", instructions)
            self.assertIn("Identify the subject from the supplied source", instructions)
            self.assertNotIn("Convert every standalone scale", instructions)
            self.assertIn(
                "Bold formatting is reserved for headings only.",
                instructions,
            )
            self.assertIn("Never treat body text, examples, musical", instructions)
            self.assertIn(
                "when they appear near an edge or look visually compact",
                instructions,
            )
            self.assertIn(
                "Never omit, summarize, or shorten substantive source content",
                instructions,
            )
            self.assertIn(
                "Never invent URLs, videos, citations, or external resources",
                instructions,
            )
        self.assertIn("right-to-left order", arabic)
        self.assertIn("normal left-to-right order", arabic)
        self.assertIn("structured-example spacing", user)
        self.assertIn(
            "never summarize, shorten, skip, or omit any unique body content",
            user,
        )
        self.assertNotIn("box", user)
        self.assertIn("musical examples, notation, scores, tablature", user)
        self.assertIn("including when they appear near a page edge", user)
        self.assertIn("Preserve each example rather than replacing it", user)
        self.assertIn("preserve every source note", user)
        self.assertIn("complete, faithful note sequence", user)
        self.assertNotIn("ukulele", user)

        image_system = image_system_instructions()
        image_prompt = image_translation_prompt()
        image_rules = f"{image_system}\n{image_prompt}"
        self.assertIn("Translate all body text", image_system)
        self.assertIn("Do not summarize or omit unique source text", image_system)
        self.assertIn(
            "Do not mistake substantive text, captions, or examples near an edge",
            image_system,
        )
        self.assertIn(
            "Preserve all body text, order, and examples",
            image_prompt,
        )
        self.assertIn("On clearly musical pages",
                      image_rules)
        self.assertIn("never mistake them for music",
                      image_rules)

    def test_translation_output_budget_scales_with_source_length(self):
        from translators.prompt import output_token_budget

        self.assertEqual(output_token_budget("short", minimum=2048), 2048)
        self.assertEqual(output_token_budget("x" * 4000, minimum=2048), 8000)

    def test_free_translation_fallback_receives_selected_languages(self):
        from unittest import mock

        settings = dict(DEFAULTS)
        settings.update(source_language="en", target_language="it")
        with mock.patch(
                "translators.fallback.gtx_translate",
                return_value="traduzione") as translate:
            self.assertEqual(chain.translate("source", settings)[0], "traduzione")
        translate.assert_called_once_with("source", "en", "it")

    def test_non_arabic_target_never_uses_arabic_only_glossary(self):
        from unittest import mock

        settings = dict(DEFAULTS)
        settings["target_language"] = "fr"
        with mock.patch("translators.fallback.gtx_translate",
                        side_effect=RuntimeError("offline")):
            with mock.patch("translators.mymemory.translate",
                            side_effect=RuntimeError("offline")):
                with self.assertRaisesRegex(RuntimeError, "Could not translate into French"):
                    chain.translate("source", settings)

    def test_selected_opencode_model_is_not_replaced_by_gemini(self):
        from unittest import mock
        s = dict(DEFAULTS)
        s.update({"model": "fledge-alpha-free", "gemini_key": "k"})
        with mock.patch("translators.opencode_bridge.stream",
                        side_effect=RuntimeError("403 free tier")) as selected_stream:
            with mock.patch("translators.opencode_bridge.translate",
                            side_effect=RuntimeError("403 free tier")) as selected:
                with mock.patch("translators.gemini.translate",
                                side_effect=AssertionError("must not switch models")):
                    with mock.patch("translators.fallback.gtx_translate",
                                    return_value="free-ar"):
                        arabic, badge = chain.translate("cadence", s)
        self.assertEqual(arabic, "free-ar")
        self.assertEqual(selected.call_count, 1)
        self.assertIn("Free service", badge)
        self.assertIn("fledge-alpha-free", badge)

    def test_chain_marks_failed_chosen_model(self):
        from unittest import mock
        s = dict(DEFAULTS)
        s.update({"model": "fledge-alpha-free"})
        with mock.patch("translators.opencode_bridge.stream",
                        side_effect=RuntimeError("401")):
            with mock.patch("translators.opencode_bridge.translate",
                            side_effect=RuntimeError("401")):
                with mock.patch("translators.fallback.gtx_translate", return_value="free-ar"):
                    arabic, badge = chain.translate("cadence", s)
                    self.assertEqual(arabic, "free-ar")
                    self.assertIn("⚠ chosen model", badge)
                    self.assertIn("fledge-alpha-free", badge)
                    self.assertIn("401", badge)

    def test_chunk_short(self):
        self.assertEqual(chain.chunk_text("hello"), ["hello"])

    def test_chunk_splits_long_single_paragraph(self):
        """The 10k-no-newline paragraph used to become ONE oversized chunk and
        the model silently truncated it. It must be split on sentence ends."""
        para = ("The dominant seventh chord resolves to the tonic triad. " * 200).strip()
        self.assertGreater(len(para), 10000)
        chunks = chain.chunk_text(para)
        self.assertGreater(len(chunks), 2)
        self.assertLessEqual(max(len(c) for c in chunks), chain.MAX_CHUNK)
        self.assertLess(
            len(chunks),
            len(chain.chunk_text(para, limit=1500)),
            "larger batches should reduce sequential model requests",
        )
        # nothing lost
        self.assertEqual(" ".join(chunks), para)

    def test_chunk_splits_text_without_any_sentence_punctuation(self):
        words = " ".join("w%04d" % i for i in range(2000))  # no . ! ? at all
        chunks = chain.chunk_text(words, limit=300)
        self.assertGreater(len(chunks), 5)
        self.assertLessEqual(max(len(c) for c in chunks), 320)
        self.assertEqual(" ".join(chunks), words)

    def test_chunk_cuts_on_word_boundary_not_mid_word(self):
        para = " ".join("abcdefgh" * 60 for _ in range(4))
        for c in chain.chunk_text(para, limit=100):
            self.assertTrue(c.startswith("abcdefgh") or "abcdefgh" in c,
                            "chunk starts mid-word: %r" % c[:20])

    def test_chunk_of_empty_text(self):
        self.assertEqual(chain.chunk_text(""), [])
        self.assertEqual(chain.chunk_text("   \n  "), [])

    def test_long_selection_translated_completely(self):
        """End-to-end: a book-page sized selection must not lose its tail."""
        from unittest import mock
        s = dict(DEFAULTS)
        s.update({"model": "gemini-2.5-flash", "gemini_key": "k"})
        seen = []

        def fake(text, key, model, instructions="", **_languages):
            seen.append(text)
            return f"هذا نص الترجمة للجزء رقم {len(seen)}."

        with mock.patch("translators.gemini.translate", side_effect=fake) as g:
            arabic, badge = chain.translate(
                "The dominant seventh chord resolves to the tonic triad. " * 200, s)
        self.assertEqual(len(seen), len(chain.chunk_text(
            "The dominant seventh chord resolves to the tonic triad. " * 200)))
        self.assertGreater(g.call_count, 2)           # still split safely
        for chunk in seen:                          # every chunk fits the budget
            self.assertLessEqual(len(chunk), chain.MAX_CHUNK)
        self.assertEqual(
            arabic.splitlines(),
            [f"هذا نص الترجمة للجزء رقم {i}."
             for i in range(1, len(seen) + 1)],
        )
        self.assertIn("\u2726 chosen: gemini-2.5-flash", badge)

    def test_full_page_translation_uses_complete_bounded_source_chunks(self):
        from unittest import mock
        from config.settings import DEFAULTS

        source = "\n\n".join(
            f"Paragraph {index}: " + ("content " * 100)
            for index in range(5)
        )
        settings = dict(
            DEFAULTS,
            model="gemini-2.5-flash",
            gemini_key="k",
            _full_page_translation=True,
        )
        self.assertEqual(
            chain._translation_chunks("x" * 3000, settings),
            ["x" * 3000],
            "page translations up to the existing 4000-character request "
            "limit should not incur extra serial model round-trips",
        )
        seen = []

        def translate_chunk(chunk, _settings):
            seen.append(chunk)
            return f"Translation {len(seen)}", "Test"

        with mock.patch.object(
            chain, "_translate_chunk", side_effect=translate_chunk,
        ):
            chain.translate(source, settings)

        self.assertGreater(len(seen), 1)
        self.assertLessEqual(max(map(len, seen)), chain.MAX_CHUNK)
        combined = "\n".join(seen)
        for index in range(5):
            self.assertIn(f"Paragraph {index}:", combined)

        seen.clear()
        with mock.patch.object(
            chain,
            "_stream_single_gemini",
            side_effect=lambda chunk, _settings: (
                seen.append(chunk)
                or iter([("done", f"Translation {len(seen)}", "Test")])
            ),
        ):
            list(chain.translate_stream(source, settings))
        self.assertGreater(len(seen), 1)
        self.assertLessEqual(max(map(len, seen)), chain.MAX_CHUNK)
        combined = "\n".join(seen)
        for index in range(5):
            self.assertIn(f"Paragraph {index}:", combined)

        seen.clear()
        with mock.patch.object(
            chain, "_translate_chunk", side_effect=translate_chunk,
        ):
            chain.translate(source, dict(settings, _full_page_translation=False))
        self.assertEqual(
            len(seen), len(chain.chunk_text(source, limit=chain.MAX_CHUNK)))

    def test_keyless_opencode_model_is_attempted(self):
        """space-bunny-free answers with no Authorization header at all, so the
        chain must reach for it and must not fall through to a free service."""
        from unittest import mock
        s = dict(DEFAULTS)
        s.update({"model": "space-bunny-free"})
        with mock.patch("translators.opencode_bridge.stream",
                        return_value=iter(["zen-ar"])) as t:
            with mock.patch("translators.fallback.gtx_translate",
                            side_effect=AssertionError("must not skip to free")):
                arabic, badge = chain.translate("cadence", s)
        self.assertEqual(arabic, "zen-ar")
        self.assertIn("\u25c8 chosen bridge: space-bunny", badge)
        self.assertEqual(t.call_args[0][1], "space-bunny-free")

    def test_opencode_zen_falls_back_to_reasoning_content(self):
        import requests
        from unittest import mock
        from translators import opencode_zen
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"choices": [{"message": {
            "content": "", "reasoning_content": "  the translation  "}}]}
        with mock.patch.object(requests, "post", return_value=resp):
            self.assertEqual(opencode_zen.translate("x", "m"), "the translation")

    def test_opencode_zen_raises_on_truly_empty_reply(self):
        import requests
        from unittest import mock
        from translators import opencode_zen
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"choices": [{"message": {"content": "  "}}]}
        with mock.patch.object(requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError):
                opencode_zen.translate("x", "m")

    @staticmethod
    def _gemini_response(text="\u0639\u0631\u0628\u064a", status=200, finish="STOP"):
        """A response shaped exactly like the real API's, for every branch."""
        import requests
        from unittest import mock
        resp = mock.Mock()
        resp.status_code = status
        resp.text = '{"error":{"message":"boom"}}'
        if status >= 400:
            resp.raise_for_status.side_effect = requests.HTTPError(
                f"{status} Server Error", response=resp)
        else:
            resp.raise_for_status.return_value = None
            resp.json.return_value = {
                "candidates": [{"content": {"parts": [{"text": text}]},
                                "finishReason": finish}],
                "usageMetadata": {"totalTokenCount": 12},
            }
        return resp

    def test_gemini_retries_transient_but_not_permanent_errors(self):
        """503/429 clear up in seconds; 404/400 never will. Retrying them is
        pure latency -- and the 404 is how Google retires a model."""
        import requests
        from unittest import mock
        from translators import gemini
        cases = [(503, 3), (429, 3), (500, 3), (400, 1), (404, 1)]
        for status, want_calls in cases:
            with self.subTest(status=status):
                with mock.patch.object(requests, "post") as post:
                    with mock.patch("time.sleep"):
                        post.side_effect = [self._gemini_response(status=status)
                                            for _ in range(want_calls + 1)]
                        try:
                            gemini.translate("x", "k", "gemini-3.5-flash-lite")
                        except Exception:
                            pass
                        self.assertEqual(post.call_count, want_calls)

    def test_gemini_succeeds_after_a_transient_failure(self):
        """The reason for retrying at all: one 503 must not lose the page."""
        import requests
        from unittest import mock
        from translators import gemini
        with mock.patch.object(requests, "post") as post:
            with mock.patch("time.sleep"):
                post.side_effect = [self._gemini_response(status=503),
                                    self._gemini_response(status=503),
                                    self._gemini_response(text="\u0627\u0644\u0639\u0631\u0628\u064a\u0629")]
                self.assertEqual(gemini.translate("x", "k", "gemini-3.5-flash-lite"),
                                 "\u0627\u0644\u0639\u0631\u0628\u064a\u0629")
                self.assertEqual(post.call_count, 3)

    def test_gemini_gives_up_after_three_attempts(self):
        """No infinite loop: the UI worker thread must always return."""
        import requests
        from unittest import mock
        from translators import gemini
        with mock.patch.object(requests, "post") as post:
            with mock.patch("time.sleep"):
                post.side_effect = [self._gemini_response(status=503) for _ in range(5)]
                with self.assertRaisesRegex(RuntimeError, "failed after 3 attempts"):
                    gemini.translate("x", "k", "gemini-3.5-flash-lite")
                self.assertEqual(post.call_count, 3)

    def test_gemini_waits_longer_between_attempts(self):
        """Backoff must be non-zero and growing: hammering a rate-limited
        endpoint burns the quota we are trying to protect."""
        import requests
        from unittest import mock
        from translators import gemini
        with mock.patch.object(requests, "post") as post:
            with mock.patch("time.sleep") as slept:
                post.side_effect = [self._gemini_response(status=503),
                                    self._gemini_response(status=503),
                                    self._gemini_response()]
                gemini.translate("x", "k", "gemini-3.5-flash-lite")
        delays = [c.args[0] for c in slept.call_args_list]
        self.assertEqual(delays, [1, 3])
        self.assertEqual(delays, sorted(delays))
        self.assertGreater(delays[0], 0)

    def test_dehyphenate_covers_underscore_breaks_from_ocr(self):
        """chain.dehyphenate used to only handle `-` breaks from PDF text
        layers. OneOCR writes the break as `_`, so OCR text reached the model
        with every hyphen broken across two lines."""
        from translators import chain
        self.assertEqual(chain.dehyphenate("extreme-\nment"), "extremement")
        self.assertEqual(chain.dehyphenate("dé_\nterminer"), "déterminer")
        self.assertEqual(chain.dehyphenate("sall-\n  utrens"), "sallutrens")
        self.assertEqual(chain.dehyphenate("no hyphen\nat all"), "no hyphen\nat all")

    def test_ocr_facade_cleans_provider_output_before_it_reaches_the_model(self):
        """The model must not receive raw OneOCR noise (broken hyphens,
        stray underscores, heavy whitespace)."""
        from unittest import mock
        from ocr_providers import base
        from translators import ocr
        raw = ("Ce cas, extrêmement fréquent, exige de grandes "
               "précautions\nque nous allons dé_\nterminer avec précision.\n"
               "  de   fendu  \n")
        fake = base.OcrResult(text=raw, backend="OneOCR")
        with mock.patch("translators.ocr.manager.read",
                        return_value=fake):
            out = ocr.read_text(object(), ("en",))
        self.assertIn("déterminer", out)
        self.assertNotIn("_\n", out)

    def test_ocr_auto_read_also_cleans_for_oneocr(self):
        from unittest import mock
        from ocr_providers import base, manager
        from translators import ocr
        raw = "nous allons dé_\nterminer avec précision"
        fake = base.OcrResult(text=raw, backend="OneOCR")

        class FakeProvider:
            name = "OneOCR"
            def is_available(self): return True
            def read(self, img, langs=None): return fake

        with mock.patch.object(manager, "get_provider", return_value=FakeProvider()):
            out, langs = ocr.auto_read(object())
        self.assertEqual(out, "nous allons déterminer avec précision")
        self.assertEqual(langs, ("auto",))

    def test_badge_reports_a_mixed_page_not_just_the_last_chunk(self):
        """8 chunks, 6 by your model and 2 by a substitute: the badge must say
        so instead of reporting whichever chunk happened to be last."""
        from translators.chain import _merge_badges
        badge = _merge_badges(["✦ chosen: gemini-3.5-flash-lite"] * 6
                              + ["⚠ Free service  ⚠ not your model (x failed)"] * 2)
        self.assertTrue(badge.startswith("⚠"))
        self.assertIn("6/8", badge)
        self.assertNotIn("✦", badge)

    def test_badge_is_unchanged_when_every_chunk_behaved_alike(self):
        from translators.chain import _merge_badges
        same = "✦ chosen: gemini-3.5-flash-lite"
        self.assertEqual(_merge_badges([same] * 8), same)
        self.assertEqual(_merge_badges([same]), same)

    def test_badge_says_no_chunks_used_your_model(self):
        from translators.chain import _merge_badges
        badge = _merge_badges(["⚠ ✦ gemini-2.5-flash  ⚠ not your model (x failed)"] * 3)
        self.assertIn("not your model", badge)

    def test_failed_opencode_model_does_not_switch_to_another_model(self):
        from unittest import mock
        from translators import chain
        s = dict(DEFAULTS)
        s.update({"model": "nemotron-3-ultra-free"})
        with mock.patch("translators.opencode_bridge.stream",
                        side_effect=RuntimeError("403 Forbidden")):
            with mock.patch("translators.opencode_bridge.translate",
                            side_effect=RuntimeError("403 Forbidden")) as selected:
                with mock.patch("translators.fallback.gtx_translate",
                                return_value="free-ar"):
                    arabic, badge = chain.translate("cadence", s)
        self.assertEqual(arabic, "free-ar")
        self.assertEqual(selected.call_count, 1)
        self.assertEqual(selected.call_args.args[1], "nemotron-3-ultra-free")
        self.assertIn("Free service", badge)
        self.assertIn("nemotron-3-ultra-free", badge)

    def test_a_failed_free_model_never_becomes_a_paid_one(self):
        """A free id that fails must not be silently swapped for a paid id."""
        from unittest import mock
        from translators import chain
        s = dict(DEFAULTS)
        s.update({"model": "nemotron-3-ultra-free"})
        with mock.patch("translators.opencode_bridge.stream",
                        side_effect=RuntimeError("403 free tier")):
            with mock.patch("translators.opencode_bridge.translate",
                            side_effect=RuntimeError("403 free tier")) as selected:
                with mock.patch("translators.fallback.gtx_translate",
                                return_value="free-ar"):
                    chain.translate("cadence", s)
        self.assertEqual(selected.call_count, 1)
        self.assertEqual(selected.call_args.args[1], "nemotron-3-ultra-free")

    def test_openai_compatible_is_never_attempted_twice(self):
        """The dead retry removed ~20s of latency before the free layers."""
        from unittest import mock
        s = dict(DEFAULTS)
        s.update({"model": "vendor/model", "openai_base": "https://x/api/v1",
                  "openai_key": "k", "openai_model": "vendor/model"})
        with mock.patch("translators.opencode_compat.translate",
                        side_effect=RuntimeError("401")) as t:
            with mock.patch("translators.fallback.gtx_translate",
                            return_value="free-ar"):
                arabic, badge = chain.translate("cadence", s)
        self.assertEqual(arabic, "free-ar")
        self.assertEqual(t.call_count, 1, "custom provider called twice")

    def test_translate_reports_a_mixed_page_end_to_end(self):
        """Same property as the unit test, but through translate(): when an
        EARLY chunk fell back and later ones hit the chosen model, the badge
        must still say the page was only partly yours (reporting only the last
        chunk would show a clean success)."""
        from unittest import mock
        s = dict(DEFAULTS)
        s.update({"model": "gemini-2.5-flash", "gemini_key": "k"})
        calls = []

        def flaky(text, key, model, instructions="", **_languages):
            calls.append(text)
            if len(calls) == 1:             # only the first chunk fails
                raise RuntimeError("429")
            return "ع"

        with mock.patch("translators.gemini.translate", side_effect=flaky):
            with mock.patch("translators.fallback.gtx_translate",
                            return_value="free"):
                arabic, badge = chain.translate(
                    "The dominant seventh chord resolves to the tonic triad. " * 200, s)
        self.assertEqual(
            len(calls),
            len(chain.chunk_text(
                "The dominant seventh chord resolves to the tonic triad. " * 200,
            )),
            "only the selected Gemini model should be attempted once per chunk",
        )
        self.assertIn("⚠", badge, "a partially-substituted page must be flagged")
        self.assertIn("chunks by your model", badge)

    def test_settings_default_theme_is_a_real_theme(self):
        """A default that is not a THEMES key only "works" thanks to a guard."""
        import main
        from config.settings import DEFAULTS
        self.assertIn(DEFAULTS["theme"], main.THEMES,
                      "config default theme must exist in main.THEMES")

    def test_default_model_is_a_known_id(self):
        from translators import gemini
        from config.settings import DEFAULT_MODEL
        self.assertIn(DEFAULT_MODEL, gemini.STARTER_MODELS)

    def test_gemini_does_not_retry_a_connection_timeout(self):
        """A 12s read timeout already cost the user 12s; slow requests do not
        retry. Fast HTTP failures still use the bounded retry policy."""
        import requests
        from unittest import mock
        from translators import gemini
        with mock.patch.object(requests, "post") as post:
            with mock.patch("time.sleep") as slept:
                post.side_effect = requests.Timeout("read timed out")
                with self.assertRaisesRegex(RuntimeError, "failed after 1 attempt"):
                    gemini.translate("x", "k", "gemini-3.5-flash-lite")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(slept.call_count, 0)
        self.assertEqual(post.call_args.kwargs["timeout"], 12)

    def test_gemini_timeout_does_not_switch_to_opencode_model(self):
        from unittest import mock
        s = dict(DEFAULTS)
        s.update({"model": "gemini-3.5-flash-lite", "gemini_key": "k",
                  "source_language": "en", "target_language": "it"})
        with mock.patch(
                "translators.gemini.translate",
                side_effect=RuntimeError("Gemini request read timed out")) as gemini:
            with mock.patch(
                    "translators.opencode_cli.translate",
                    side_effect=AssertionError("must not switch models")) as cli:
                with mock.patch("translators.fallback.gtx_translate",
                                return_value="traduzione"):
                    arabic, badge = chain.translate("source text", s)
        self.assertEqual(arabic, "traduzione")
        self.assertIn("Free service", badge)
        self.assertIn("gemini-3.5-flash-lite failed", badge)
        gemini.assert_called_once()
        cli.assert_not_called()

    def test_gemini_timeout_does_not_start_a_second_gemini_request(self):
        from unittest import mock
        s = dict(DEFAULTS)
        s.update({"model": "gemini-3.5-flash-lite", "gemini_key": "k"})
        with mock.patch(
                "translators.gemini.translate",
                side_effect=RuntimeError("Gemini request read timed out")) as gemini:
            with mock.patch(
                    "translators.opencode_cli.find_executable",
                    return_value="opencode"):
                with mock.patch(
                        "translators.opencode_cli.translate",
                        side_effect=RuntimeError("OpenCode timed out")):
                    with mock.patch(
                            "translators.fallback.gtx_translate",
                            return_value="هذه ترجمة مجانية للنص المطلوب."):
                        arabic, _badge = chain.translate("source text", s)
        self.assertEqual(arabic, "هذه ترجمة مجانية للنص المطلوب.")
        self.assertEqual(gemini.call_count, 1)

    def test_gemini_error_names_the_model_and_status(self):
        """A bare HTTPError leaves the settings screen showing a URL. The user
        needs to know WHICH model to stop choosing."""
        import requests
        from unittest import mock
        from translators import gemini
        with mock.patch.object(requests, "post") as post:
            with mock.patch("time.sleep"):
                post.side_effect = [self._gemini_response(status=503) for _ in range(3)]
                with self.assertRaises(RuntimeError) as ctx:
                    gemini.translate("x", "k", "gemini-3.5-flash-lite")
        msg = str(ctx.exception)
        self.assertIn("gemini-3.5-flash-lite", msg)
        self.assertIn("503", msg)

    def test_gemini_rejects_a_truncated_generation(self):
        import requests
        from unittest import mock
        from translators import gemini
        with mock.patch.object(
                requests, "post",
                return_value=self._gemini_response(
                    text="partial translation", finish="MAX_TOKENS")):
            with self.assertRaisesRegex(RuntimeError, "reply was cut off"):
                gemini.translate("source", "k", "gemini-3.5-flash-lite")

    def test_gemini_reports_dead_model_clearly(self):
        import requests
        from unittest import mock
        from translators import gemini
        resp = mock.Mock()
        resp.status_code = 404
        resp.raise_for_status.return_value = None
        with mock.patch.object(requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                gemini.translate("x", "k", "gemini-2.0-flash")
        self.assertIn("not available", str(ctx.exception))

    def test_settings_save_writes_no_bom(self):
        """Keep new writes strict-JSON while load() tolerates legacy BOMs."""
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        tmp = Path(temp_dir.name) / "settings.json"
        with mock.patch("config.settings.SETTINGS_PATH", tmp):
            from config.settings import DEFAULTS, load, save
            s = dict(DEFAULTS)
            s.update({"model": "gemini-3.5-flash-lite", "opencode_model": "x",
                      "gemini_model": "y"})
            save(s)
            raw = tmp.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "BOM written")
            json.loads(raw.decode("utf-8"))         # strict parse must work
            self.assertEqual(load()["model"], "gemini-3.5-flash-lite")
            # the derived legacy keys are dropped, not persisted
            self.assertNotIn("opencode_model", load())
            self.assertNotIn("gemini_model", load())
            self.assertEqual(load(), {k: v for k, v in s.items()
                                      if k in DEFAULTS})

    def test_settings_load_tolerates_legacy_bom_and_dead_keys(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        tmp = Path(tempfile.gettempdir()) / "ht_legacy.json"
        tmp.write_bytes("\ufeff".encode("utf-8") + b'{"model":"space-bunny-free"}')
        with mock.patch("config.settings.SETTINGS_PATH", tmp):
            from config.settings import DEFAULTS, load
            data = load()
            self.assertEqual(data["model"], "space-bunny-free")
            self.assertNotIn("gemini_model", data)      # derived, never stored
            self.assertEqual(set(data), set(DEFAULTS))
        tmp.unlink(missing_ok=True)

    def test_resolve_model_never_empty(self):
        from config.settings import DEFAULT_MODEL, resolve_model
        self.assertEqual(resolve_model({}), DEFAULT_MODEL)
        self.assertEqual(resolve_model({"model": "  "}), DEFAULT_MODEL)
        self.assertEqual(resolve_model({"model": " gemini-3.5-flash-lite "}),
                         "gemini-3.5-flash-lite")

    def test_default_model_is_the_chosen_one(self):
        from config.settings import DEFAULT_MODEL, DEFAULTS
        self.assertEqual(DEFAULTS["model"], DEFAULT_MODEL)
        self.assertEqual(DEFAULT_MODEL, "gemini-3.5-flash-lite")

    def test_registry_availability_matches_reality(self):
        from translators import registry
        no_keys = {}
        # the one free model works with nothing
        self.assertEqual(registry.availability("space-bunny-free", no_keys),
                         (True, ""))
        # a Zen id that left the free tier cannot be unlocked with a key,
        # because there is no key field to fill in any more
        ok, reason = registry.availability("deepseek-v4-flash-free", no_keys)
        self.assertFalse(ok)
        self.assertIn("space-bunny-free", reason)
        # gemini needs its own key
        ok, reason = registry.availability("gemini-3.5-flash-lite", no_keys)
        self.assertFalse(ok)
        self.assertIn("Gemini key", reason)
        # openai-compatible needs base + key + model
        ok, _ = registry.availability("vendor/model",
                                      {"openai_base": "b", "openai_key": "k",
                                       "openai_model": "m"})
        self.assertTrue(ok)
        # a Gemini key lights up Gemini, and never a Zen id
        full = {"gemini_key": "k"}
        self.assertTrue(registry.availability("gemini-3.5-flash-lite", full)[0])
        self.assertFalse(registry.availability("deepseek-v4-flash-free", full)[0])

    def test_registry_provider_routing(self):
        from translators import registry
        self.assertEqual(registry.provider_of("gemini-3.5-flash-lite"), "gemini")
        # space-bunny-free is served via streaming bridge (verified 2026-10-08).
        self.assertEqual(registry.provider_of("space-bunny-free"), "bridge")
        self.assertEqual(registry.provider_of("mimo-v2.6-flash-free"), "bridge")
        self.assertEqual(registry.provider_of("opencode-cli:opencode/glm-5.2"),
                         "opencode_cli")
        # an unoffered Zen-shaped id still belongs to Zen, so the reason the
        # user sees is about the free tier and not about a base URL
        self.assertEqual(registry.provider_of("deepseek-v4-flash-free"), "opencode")
        self.assertEqual(registry.provider_of("vendor/model:free"), "custom")
        self.assertEqual(registry.provider_of(""), "custom")

    def test_registry_never_offers_retired_gemini_models(self):
        from translators import gemini, registry
        models = registry.all_models()
        self.assertNotIn("gemini-2.0-flash", models)   # 404
        self.assertNotIn("gemini-1.5-flash", models)   # retired
        self.assertIn("gemini-3.5-flash-lite", models)
        self.assertNotIn("gemini-2.0-flash", gemini.known_models())

    def test_chunk_long_splits(self):
        text = "\n".join(f"para{i} " + "x" * 400 for i in range(6))
        chunks = chain.chunk_text(text, limit=1500)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("\n".join(chunks), text)

    def test_empty_returns_empty(self):
        self.assertEqual(chain.translate("   ", dict(DEFAULTS)), ("", "—"))

    def test_glossary_offline(self):
        out = fallback.glossary_partial("The dominant resolves to the tonic. Каданс.")
        self.assertIn("المهيمنة", out)
        self.assertIn("كادانس", out)

    def test_chain_never_raises_without_network(self):
        """Every layer above the glossary fails; the glossary still answers.

        This used to call `chain.translate("cadence", dict(DEFAULTS))` for real,
        which walked the genuine chosen-engine -> Google -> MyMemory ladder with
        18 s and 15 s timeouts plus retries. The module docstring promises "No
        network", and the test broke that promise: its runtime and its result
        both depended on whether the machine running it happened to be online.

        Now every network layer is mocked to raise, so the test measures exactly
        one thing: the chain does not propagate an exception, and it returns
        the offline glossary's partial translation rather than nothing.
        """
        from unittest import mock
        from translators import gemini, mymemory, opencode_compat, fallback

        offline = RuntimeError("network unreachable (simulated)")

        def _no_network(*_a, **_kw):
            raise offline

        settings = dict(DEFAULTS)
        # Any credentials present would be used for real; with none configured,
        # the ladder reaches the free layers and then the glossary either way.
        for key in ("gemini_key", "openai_key", "openai_base", "flm_key",
                    "groq_key", "cf_account", "cloudflare_account_id",
                    "cloudflare_token", "email"):
            settings[key] = ""

        with mock.patch.object(gemini, "translate", side_effect=_no_network), \
                mock.patch.object(opencode_compat, "translate",
                                  side_effect=_no_network), \
                mock.patch.object(opencode_compat, "translate_with_engine",
                                  side_effect=_no_network), \
                mock.patch.object(fallback, "gtx_translate",
                                  side_effect=_no_network), \
                mock.patch.object(mymemory, "translate",
                                  side_effect=_no_network):
            arabic, badge = chain.translate("cadence", settings)

        self.assertTrue(arabic, "the chain returned nothing at all")
        self.assertTrue(badge)
        # "Cadence" is a known musical term, so the offline glossary translates
        # it instead of retaining the source word.
        self.assertIn("كادانس", arabic)
        self.assertNotIn("cadence", arabic)
        # ...and the badge must not claim the user's own model answered.
        self.assertNotIn("◈ chosen", badge)
        self.assertNotIn("chosen:", badge)


class TestPdfUtils(unittest.TestCase):
    @staticmethod
    def _scan_like_page(word_count=22):
        import io
        import pymupdf
        from PIL import Image

        doc = pymupdf.open()
        page = doc.new_page()
        image = Image.new("RGB", (100, 100), "white")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        page.insert_image(
            pymupdf.Rect(40, 40, 555, 800),
            stream=stream.getvalue(),
        )
        for index in range(word_count):
            page.insert_text(
                (40, 20 + (index % 3) * 8),
                f"header{index}",
                fontsize=5,
            )
        return doc, page

    def test_detects_scanned_page_with_only_sparse_text_layer(self):
        from reader import pdf_utils

        doc, page = self._scan_like_page()
        try:
            self.assertTrue(pdf_utils.has_sparse_text_over_scan(page))
        finally:
            doc.close()

    def test_does_not_ocr_scan_page_with_rich_text_layer(self):
        from reader import pdf_utils

        doc, page = self._scan_like_page(word_count=100)
        try:
            self.assertFalse(pdf_utils.has_sparse_text_over_scan(page))
        finally:
            doc.close()

    def test_page_body_text_omits_running_header_and_keeps_page_number(self):
        import pymupdf
        from reader import pdf_utils

        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text(
            (36, 42), "Running chapter title", fontname="tiit",
            fontsize=8,
        )
        page.insert_text(
            (540, 42), "12", fontsize=8,
        )
        page.insert_text(
            (36, 112), "Real chapter heading", fontname="hebo",
            fontsize=16,
        )
        page.insert_text(
            (36, 160), "Main body text remains in the translation.",
            fontsize=11,
        )
        try:
            body, page_number = pdf_utils.page_body_text(page)
            self.assertNotIn("Running chapter title", body)
            self.assertIn("Real chapter heading", body)
            self.assertIn("Main body text", body)
            self.assertEqual(page_number, "12")
        finally:
            doc.close()

    def test_extract_in_rect(self):
        import pymupdf
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 100), "Hello harmony triad")
        page.insert_text((72, 130), "Second line cadence")
        full = pdf_utils.page_text(page)
        self.assertIn("harmony", full)
        text, bbox = pdf_utils.extract_in_rect(page, (50, 80, 400, 120))
        self.assertIn("harmony", text)
        self.assertNotIn("Second", text)
        text2, _ = pdf_utils.extract_in_rect(page, (50, 80, 400, 160))
        self.assertIn("Second", text2)
        empty, _ = pdf_utils.extract_in_rect(page, (10, 10, 12, 12))
        self.assertEqual(empty, "")
        doc.close()

    def _three_line_page(self):
        """A page with three known prose lines, for selection tests."""
        import pymupdf
        doc = pymupdf.open()
        pg = doc.new_page()
        for i, line in enumerate(("alpha bravo charlie", "delta echo foxtrot",
                                  "golf hotel india")):
            pg.insert_text((72, 100 + i * 20), line, fontsize=11)
        return doc, pg

    def test_extract_region_returns_whole_lines_not_fragments(self):
        """THE bug: a thin drag returned one sliver of one line and the app
        called it success. Aiming at a line must deliver that whole line."""
        from reader import pdf_utils
        doc, pg = self._three_line_page()
        try:
            words = pg.get_text("words")
            second = sorted([w for w in words if "delta" in w[4] or
                             "echo" in w[4] or "foxtrot" in w[4]], key=lambda w: w[0])
            y0 = min(w[1] for w in second)
            y1 = max(w[3] for w in second)
            x0 = min(w[0] for w in second)
            x1 = max(w[2] for w in second)
            # a drag only 2 points tall, dead centre of that line
            mid = (y0 + y1) / 2
            got = pdf_utils.extract_region(pg, (x0, mid - 1, x1, mid + 1))
            self.assertEqual(got.text.split(),
                             ["delta", "echo", "foxtrot"])
            self.assertEqual(got.requested_lines, 1)
            self.assertEqual(got.returned_lines, 1)
            self.assertTrue(got.complete)
        finally:
            doc.close()

    def test_extract_region_snaps_to_every_line_it_touches(self):
        from reader import pdf_utils
        doc, pg = self._three_line_page()
        try:
            words = pg.get_text("words")
            two = [w for w in words if w[4] not in ("alpha", "bravo", "charlie")]
            y0 = min(w[1] for w in two)
            y1 = max(w[3] for w in two)
            # a band spanning both remaining lines, full width
            got = pdf_utils.extract_region(pg, (0, y0 - 1, pg.rect.width, y1 + 1))
            self.assertEqual(got.returned_lines, 2)
            self.assertTrue(got.complete)
            for word in ("delta", "echo", "foxtrot", "golf", "hotel", "india"):
                self.assertIn(word, got.text)
        finally:
            doc.close()

    def test_extract_region_is_always_complete(self):
        """Whatever the drag, no line under it may be silently half-delivered."""
        from reader import pdf_utils
        doc, pg = self._three_line_page()
        try:
            for top in range(88, 148, 3):
                got = pdf_utils.extract_region(pg, (0, top, pg.rect.width, top + 6))
                if got.requested_lines:
                    self.assertEqual(got.returned_lines, got.requested_lines,
                                     f"incomplete at y={top}")
                    self.assertTrue(got.complete, f"incomplete at y={top}")
        finally:
            doc.close()

    def test_extract_region_respects_horizontal_selection(self):
        """Snapping is vertical only: a narrow column must stay narrow."""
        from reader import pdf_utils
        doc, pg = self._three_line_page()
        try:
            words = sorted(pg.get_text("words"), key=lambda w: (w[1], w[0]))
            alpha = next(w for w in words if w[4] == "alpha")
            bravo = next(w for w in words if w[4] == "bravo")
            y0, y1 = min(w[1] for w in words), max(w[3] for w in words)
            # band covering only alpha's width
            got = pdf_utils.extract_region(
                pg, (alpha[0] - 1, y0 - 5, (alpha[2] + bravo[0]) / 2, y1 + 5))
            self.assertIn("alpha", got.text)
            self.assertNotIn("bravo", got.text)
        finally:
            doc.close()

    def test_extract_region_preserves_word_order_on_rotated_pages(self):
        import pymupdf
        from reader import pdf_utils

        for page_rotation, text_rotation in ((90, 270), (270, 90)):
            with self.subTest(page_rotation=page_rotation):
                doc = pymupdf.open()
                page = doc.new_page(width=500, height=400)
                page.insert_text(
                    (100, 160),
                    "ALPHA BRAVO",
                    fontsize=12,
                    rotate=text_rotation,
                )
                page.insert_text(
                    (130, 160),
                    "CHARLIE DELTA",
                    fontsize=12,
                    rotate=text_rotation,
                )
                page.set_rotation(page_rotation)
                words = page.get_text("words")
                displayed = [
                    pymupdf.Rect(word[:4]) * page.rotation_matrix
                    for word in words
                ]
                screen_rect = pymupdf.Rect(
                    min(rect.x0 for rect in displayed) - 2,
                    min(rect.y0 for rect in displayed) - 2,
                    max(rect.x1 for rect in displayed) + 2,
                    max(rect.y1 for rect in displayed) + 2,
                )
                expected = (
                    ["ALPHA", "BRAVO", "CHARLIE", "DELTA"]
                    if page_rotation == 90
                    else ["CHARLIE", "DELTA", "ALPHA", "BRAVO"]
                )

                try:
                    extracted = pdf_utils.extract_region(
                        page, tuple(screen_rect * page.derotation_matrix)
                    )
                    self.assertEqual(
                        extracted.text.split(),
                        expected,
                    )
                finally:
                    doc.close()

    def test_extract_region_degenerate_rect_is_empty(self):
        from reader import pdf_utils
        doc, pg = self._three_line_page()
        try:
            got = pdf_utils.extract_region(pg, (100, 100, 101, 101))
            self.assertEqual(got.text, "")
            self.assertEqual(got.requested_lines, 0)
            self.assertTrue(got.complete)
        finally:
            doc.close()

    def test_extract_in_rect_still_returns_the_two_tuple(self):
        """Back-compat: the 2-tuple API the rest of the app uses is unchanged."""
        from reader import pdf_utils
        doc, pg = self._three_line_page()
        try:
            out = pdf_utils.extract_in_rect(pg, (0, 80, pg.rect.width, 200))
            self.assertIsInstance(out, tuple)
            self.assertEqual(len(out), 2)
            self.assertIn("alpha", out[0])
        finally:
            doc.close()

    def test_render_page(self):
        import pymupdf
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "test")
        img = pdf_utils.render_page(page, 1.5)
        self.assertGreater(img.width(), 100)
        doc.close()

    def test_render_max_zoom_smoke(self):
        import pymupdf
        doc = pymupdf.open()
        page = doc.new_page(width=200, height=200)
        page.insert_text((20, 20), "zoom")
        img = pdf_utils.render_page(page, 10.0)
        self.assertEqual((img.width(), img.height()), (2000, 2000))
        doc.close()

    def test_render_crop_and_pil(self):
        import pymupdf
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 100), "Hello harmony triad")
        crop = pdf_utils.render_crop(page, (50, 80, 400, 160), dpi=150)
        self.assertGreater(crop.width(), 50)
        pil = pdf_utils.qimage_to_pil(crop)
        self.assertEqual(pil.size, (crop.width(), crop.height()))
        doc.close()


class TestReaderZoom(_GuiCase):

    def _wait_for_reader_rendering(self, reader):
        import time

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.processEvents()
            worker = reader._render_worker
            if worker is not None and worker.isRunning():
                worker.wait(50)
            self.app.processEvents()
            if not reader._queue and not reader._inflight:
                return
            time.sleep(0.01)
        self.fail("reader page rendering did not finish within 3 seconds")

    def test_speech_groups_short_sentences_to_reduce_playback_gaps(self):
        from popup.speech_worker import (
            FIRST_CHUNK_LENGTH, MAX_CHUNK_LENGTH, _segment_word_spans,
            python_to_qt_position, qt_to_python_position, speech_start_offset,
            split_speech_text,
        )

        self.assertEqual(
            split_speech_text("مرحبا. كيف حالك؟ هذا اختبار.", max_length=90),
            ["مرحبا, كيف حالك, هذا اختبار"],
        )
        self.assertEqual(
            split_speech_text(
                "عنوان الفصل\nمقدمة قصيرة.\nالجملة التالية.", max_length=90),
            ["عنوان الفصل مقدمة قصيرة, الجملة التالية"],
        )
        paragraph_chunks = split_speech_text(
            "الفقرة الأولى.\n\n \nالفقرة الثانية.", max_length=90)
        self.assertEqual(
            paragraph_chunks,
            ["الفقرة الأولى, الفقرة الثانية"],
        )
        self.assertTrue(all(chunk.strip() for chunk in paragraph_chunks))
        self.assertTrue(all("\n" not in chunk for chunk in paragraph_chunks))
        self.assertEqual(
            _segment_word_spans(
                "الفقرة الأولى.\n\n \nالفقرة الثانية.",
                paragraph_chunks,
            ),
            [[(0, 6), (7, 13), (18, 24), (25, 32)]],
        )
        self.assertEqual(split_speech_text("\n \n\t"), [])
        self.assertEqual(split_speech_text("القيمة 3.14."), ["القيمة 3, 14"])
        self.assertEqual(
            split_speech_text("كلمة   أخرى\t\n\nثالثة."),
            ["كلمة أخرى ثالثة"],
        )
        self.assertEqual(
            split_speech_text(
                'واحد؛ اثنان: (ثلاثة)/"أربعة"_خمسة-ستة.،=+سبعة',
                max_length=90),
            ["واحد, اثنان, ثلاثة, أربعة خمسة-ستة, سبعة"],
        )
        self.assertEqual(speech_start_offset("مرحبا بالعالم", 8), 6)
        self.assertEqual(speech_start_offset("مرحبا بالعالم", 5), 6)
        self.assertEqual(speech_start_offset("مرحبا بالعالم", 100), 6)
        chunks = split_speech_text("كلمة طويلة " * 50, max_length=40)
        self.assertTrue(all(len(chunk) <= 40 for chunk in chunks))
        self.assertGreater(len(chunks), 1)
        long_sentence = "هذه جملة عربية طويلة للاختبار " * 20
        fast_chunks = split_speech_text(long_sentence)
        self.assertGreater(len(fast_chunks), 1)
        self.assertLessEqual(len(fast_chunks[0]), FIRST_CHUNK_LENGTH)
        self.assertTrue(all(
            len(chunk) <= MAX_CHUNK_LENGTH for chunk in fast_chunks))
        self.assertEqual(
            " ".join(" ".join(fast_chunks).split()),
            " ".join(long_sentence.split()),
        )
        long_word = "x" * 45
        self.assertEqual(
            _segment_word_spans(
                long_word, split_speech_text(long_word, max_length=40)
            ),
            [[(0, 40)], [(40, 45)]],
        )
        vocalized = "مَرْحَبًا بِكُمْ. هَذَا نَصٌّ عَرَبِيٌّ للتجربة."
        chunks = split_speech_text(vocalized)
        self.assertEqual(
            _segment_word_spans(
                vocalized,
                split_speech_text(vocalized, max_length=90),
            ),
            [
                [(0, 9), (10, 16), (18, 23), (24, 29), (30, 39), (40, 47)],
            ],
        )
        self.assertEqual(speech_start_offset(vocalized, 3), 0)
        text_with_emoji = "😀 " + vocalized
        self.assertEqual(
            qt_to_python_position(
                text_with_emoji,
                python_to_qt_position(text_with_emoji, 5),
            ),
            5,
        )

    def test_speech_uses_large_chunk_size_in_both_modes(self):
        from popup.speech_worker import (
            FIRST_CHUNK_LENGTH, MAX_CHUNK_LENGTH, SpeechWorker,
        )

        text = "كلمة طويلة للاختبار " * 500
        worker = SpeechWorker(text, fast=True)
        classic_worker = SpeechWorker(text, fast=False)

        self.assertTrue(all(
            len(segment) <= MAX_CHUNK_LENGTH for segment in worker._segments
        ))
        self.assertLessEqual(len(worker._segments), 10)
        self.assertLessEqual(
            len(worker._segments[0]), FIRST_CHUNK_LENGTH)
        self.assertEqual(MAX_CHUNK_LENGTH, 1200)
        self.assertEqual(worker._segments, classic_worker._segments)
        self.assertEqual(
            len(worker._segment_word_spans), len(worker._segments))

    def test_speech_stream_delivers_chunks_before_it_is_finished(self):
        import threading
        from urllib.request import urlopen
        from popup.audio_stream import AudioStreamServer

        server = AudioStreamServer()
        token, url = server.create_stream()
        first_chunk_received = threading.Event()
        received = bytearray()
        errors = []

        def consume_stream():
            try:
                with urlopen(url, timeout=5) as response:
                    received.extend(response.read(5))
                    first_chunk_received.set()
                    received.extend(response.read())
            except Exception as exc:
                errors.append(exc)

        consumer = threading.Thread(target=consume_stream)
        try:
            consumer.start()
            server.write(token, b"first")
            self.assertTrue(first_chunk_received.wait(5))
            server.write(token, b"second")
            server.finish(token)
            consumer.join(5)

            self.assertFalse(consumer.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(received, b"firstsecond")
        finally:
            server.close()
            consumer.join(5)

    def test_speech_worker_emits_stream_before_completed_audio(self):
        from unittest import mock
        from PySide6.QtCore import Qt
        from popup.speech_worker import SpeechWorker

        events = []

        class FakeCommunicate:
            def __init__(self, text, voice, rate=None, boundary=None):
                self.text = text

            async def stream(self):
                yield {"type": "audio", "data": b"first"}
                yield {
                    "type": "WordBoundary",
                    "offset": 0,
                    "duration": 1_000_000,
                    "text": self.text.split()[0],
                }
                yield {"type": "audio", "data": b"second"}

        worker = SpeechWorker(
            "hello world", target_language="en", fast=False)
        worker.segment_stream_ready.connect(
            lambda index, url: events.append(("stream", index, url)),
            Qt.ConnectionType.DirectConnection,
        )
        worker.word_boundary_ready.connect(
            lambda index, boundary: events.append(("boundary", index, boundary)),
            Qt.ConnectionType.DirectConnection,
        )
        worker.segment_ready.connect(
            lambda index, path: events.append(("complete", index, path)),
            Qt.ConnectionType.DirectConnection,
        )
        with mock.patch(
            "popup.speech_worker.edge_tts.Communicate", FakeCommunicate
        ):
            worker.start()
            self.assertTrue(worker.wait(5000))

        try:
            self.assertEqual(
                [event[0] for event in events],
                ["stream", "boundary", "complete"],
            )
            self.assertEqual(events[0][1], 0)
            self.assertTrue(events[0][2].startswith("http://127.0.0.1:"))
            self.assertEqual(events[1][1:], (0, (0, 1_000_000, 0, 5)))
            self.assertTrue(os.path.isfile(events[2][2]))
        finally:
            worker.cleanup_output()

    def test_speech_worker_groups_sentences_in_one_audio_segment(self):
        from unittest import mock
        from PySide6.QtCore import Qt
        from popup.speech_worker import ARABIC_VOICE, SPEECH_RATE, SpeechWorker

        calls = []

        class FakeCommunicate:
            def __init__(self, text, voice, rate, boundary):
                calls.append((text, voice, rate, boundary))
                self.text = text

            async def stream(self):
                yield {"type": "audio", "data": b"fake-mp3"}
                for word_index, word in enumerate(self.text.split()):
                    yield {
                        "type": "WordBoundary",
                        "offset": word_index * 1_000_000,
                        "duration": 1_000_000,
                        "text": word,
                    }

        emitted = []
        boundaries = []
        cache_tmp = tempfile.mkdtemp(prefix="harmony-tts-test-")
        with mock.patch.dict(os.environ, {"HARMONY_TTS_CACHE": cache_tmp}):
            with mock.patch("popup.speech_worker.edge_tts.Communicate", FakeCommunicate):
                worker = SpeechWorker("مرحبا. كيف حالك؟", source_offset=20)
                worker.segment_ready.connect(
                    lambda index, path: emitted.append((index, path)),
                    Qt.ConnectionType.DirectConnection,
                )
                worker.word_boundaries.connect(
                    lambda index, words: boundaries.append((index, words)),
                    Qt.ConnectionType.DirectConnection,
                )
                worker.start()
                self.assertTrue(worker.wait(5000))

        try:
            self.assertEqual([index for index, _ in emitted], [0])
            self.assertEqual(
                calls,
                [
                    (
                        "مرحبا, كيف حالك",
                        ARABIC_VOICE,
                        "+0%",
                        "WordBoundary",
                    ),
                ],
            )
            self.assertEqual(
                boundaries,
                [
                    (0, [
                        (0, 1_000_000, 20, 25),
                        (1_000_000, 1_000_000, 27, 30),
                        (2_000_000, 1_000_000, 31, 35),
                    ]),
                ],
            )
            self.assertTrue(all(os.path.isfile(path) for _, path in emitted))
        finally:
            worker.cleanup_output()

    def test_automatic_speech_voice_setting_round_trips(self):
        from config.settings import load
        from main import SettingsDialog
        from PySide6.QtWidgets import QWidget

        host = QWidget()
        self.addCleanup(host.close)
        host.settings = dict(DEFAULTS)
        dialog = SettingsDialog(host.settings, host)
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.automatic_speech_voice.isChecked())
        self.assertFalse(dialog.speech_read_latin_words.isChecked())
        self.assertFalse(
            dialog.prefetch_next_page_translation.isChecked())
        self.assertFalse(
            dialog.automatic_page_reading_time_enabled.isChecked())
        self.assertEqual(
            dialog.automatic_page_reading_time_preset.count(), 17)
        self.assertEqual(
            dialog.automatic_page_reading_time_custom.text(), "")

        for checkbox in (
            dialog.automatic_speech_voice,
            dialog.speech_read_latin_words,
            dialog.prefetch_next_page_translation,
            dialog.automatic_page_reading_time_enabled,
        ):
            checkbox.blockSignals(True)
            checkbox.setChecked(True)
            checkbox.blockSignals(False)
        dialog.automatic_page_reading_time_preset.setCurrentIndex(15)
        self.assertEqual(
            dialog.values()["automatic_page_reading_time_minutes"], 240)
        self.assertEqual(
            dialog.automatic_page_reading_time_display.text(), "4 h")
        dialog.automatic_page_reading_time_preset.setCurrentIndex(16)
        dialog.automatic_page_reading_time_custom.setText("70")
        self.assertTrue(dialog.values()["automatic_speech_voice"])
        self.assertTrue(dialog.values()["speech_read_latin_words"])
        self.assertTrue(
            dialog.values()["prefetch_next_page_translation"])
        self.assertEqual(
            dialog.values()["automatic_page_reading_time_minutes"], 70)
        self.assertEqual(
            dialog.automatic_page_reading_time_display.text(), "1 h 10 min")

        dialog.accept()
        self.assertTrue(host.settings["prefetch_next_page_translation"])
        self.assertTrue(
            host.settings["automatic_page_reading_time_enabled"])

        saved_settings = load()
        self.assertTrue(saved_settings["automatic_speech_voice"])
        self.assertTrue(saved_settings["speech_read_latin_words"])
        self.assertTrue(saved_settings["prefetch_next_page_translation"])
        self.assertTrue(
            saved_settings["automatic_page_reading_time_enabled"])
        self.assertEqual(
            saved_settings["automatic_page_reading_time_minutes"], 70)

        reopened = SettingsDialog(saved_settings)
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.automatic_speech_voice.isChecked())
        self.assertTrue(reopened.speech_read_latin_words.isChecked())
        self.assertTrue(reopened.prefetch_next_page_translation.isChecked())
        self.assertTrue(
            reopened.automatic_page_reading_time_enabled.isChecked())
        self.assertEqual(
            reopened.automatic_page_reading_time_display.text(), "1 h 10 min")

    def test_speech_worker_streams_each_segment_in_order(self):
        from unittest import mock
        from PySide6.QtCore import Qt
        from popup.speech_worker import SpeechWorker, split_speech_text

        calls = []

        class FakeCommunicate:
            def __init__(self, text, voice, rate=None, boundary=None):
                calls.append(text)
                self.text = text

            async def stream(self):
                yield {"type": "audio", "data": b"fake-mp3"}
                for word_index, word in enumerate(self.text.split()):
                    yield {
                        "type": "WordBoundary",
                        "offset": word_index * 1_000_000,
                        "duration": 1_000_000,
                        "text": word,
                    }

        emitted = []
        streamed = []
        passage = "كلمة عادية " * 30
        worker = SpeechWorker(passage, fast=False)
        worker.segment_stream_ready.connect(
            lambda index, _url: streamed.append(index),
            Qt.ConnectionType.DirectConnection,
        )
        worker.segment_ready.connect(
            lambda index, path: emitted.append((index, path)),
            Qt.ConnectionType.DirectConnection,
        )
        with mock.patch(
            "popup.speech_worker.edge_tts.Communicate", FakeCommunicate
        ):
            worker.start()
            self.assertTrue(worker.wait(5000))

        try:
            expected_segments = split_speech_text(passage)
            self.assertGreater(len(expected_segments), 1)
            self.assertEqual(calls, expected_segments)
            self.assertEqual(
                [index for index, _ in emitted],
                list(range(len(expected_segments))),
            )
            self.assertEqual(streamed, list(range(len(expected_segments))))
        finally:
            worker.cleanup_output()

    def test_speech_worker_skips_blank_paragraph_lines(self):
        from unittest import mock
        from PySide6.QtCore import Qt
        from popup.speech_worker import SpeechWorker

        calls = []

        class FakeCommunicate:
            def __init__(self, text, voice, rate, boundary):
                calls.append(text)
                self.text = text

            async def stream(self):
                yield {"type": "audio", "data": b"fake-mp3"}
                for word_index, word in enumerate(self.text.split()):
                    yield {
                        "type": "WordBoundary",
                        "offset": word_index * 1_000_000,
                        "duration": 1_000_000,
                        "text": word,
                    }

        emitted = []
        worker = SpeechWorker(
            "First.\n\n \nSecond.", target_language="en", fast=False)
        worker.segment_ready.connect(
            lambda index, path: emitted.append((index, path)),
            Qt.ConnectionType.DirectConnection,
        )
        with mock.patch(
            "popup.speech_worker.edge_tts.Communicate", FakeCommunicate
        ):
            worker.start()
            self.assertTrue(worker.wait(5000))

        try:
            self.assertEqual(calls, ["First, Second"])
            self.assertEqual(len(emitted), 1)
        finally:
            worker.cleanup_output()

    def test_cleanup_keeps_locked_audio_for_retry(self):
        from unittest import mock
        from popup.speech_worker import SpeechWorker
        worker = SpeechWorker.__new__(SpeechWorker)
        locked = mock.Mock()
        locked.unlink.side_effect = PermissionError(32, "in use")
        gone = mock.Mock()
        worker._output_paths = [locked, gone]
        worker.cleanup_output()  # must not raise
        self.assertEqual(worker._output_paths, [locked])
        gone.unlink.assert_called_once()

    def test_clicking_translation_restarts_speech_at_clicked_word(self):
        from unittest import mock
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from popup.translate_popup import TranslatePopup
        from popup.speech_worker import speech_start_offset
        from config.settings import DEFAULTS

        with mock_chain_translate(
            return_value=("\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645", "T"),
        ):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            popup.result.setPlainText("\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645")
            popup._speech_active = True
            popup._speech_boundaries = {
                0: [(500_000, 5_000_000, 0, 5), (5_000_000, 5_000_000, 6, 13)]
            }
            popup._speech_highlight_timer.start()
            with mock.patch.object(
                popup._player, "position", side_effect=[0, 600]
            ):
                popup._update_speech_highlight()
                self.assertTrue(popup._speech_highlight_timer.isActive())
                self.assertEqual(popup.result.extraSelections(), [])
                popup._update_speech_highlight()
            highlighted = popup.result.extraSelections()
            self.assertEqual(len(highlighted), 1)
            self.assertEqual(highlighted[0].cursor.selectedText(), "\u0628\u0627\u0644\u0639\u0627\u0644\u0645")
            self.assertEqual(
                highlighted[0].format.background().color().name(), "#fff176")
            popup._clear_speech_highlight()
            self.assertEqual(popup.result.extraSelections(), [])
            popup._speech_active = False
            popup.resize(500, 240)
            popup.show()
            self.app.processEvents()
            cursor = popup.result.textCursor()
            cursor.setPosition(8)
            popup.result.setTextCursor(cursor)
            click_position = popup.result.cursorRect(cursor).center()
            with mock.patch.object(popup, "_request_speech") as request:
                QTest.mouseClick(
                    popup.result.viewport(),
                    Qt.MouseButton.LeftButton,
                    pos=click_position,
                )
                self.app.processEvents()
            request.assert_called_once_with(
                6, trigger="translation-word-click")
            self.assertEqual(popup._speech_start_offset, 6)
        finally:
            try:
                if popup._worker and not popup._worker.wait(20000):
                    popup._worker.terminate()
                    popup._worker.wait(2000)
            except RuntimeError:
                pass
            popup.close()

    def test_translation_mouse_click_emits_word_position_once_on_release(self):
        from unittest import mock
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from popup.speech_worker import speech_start_offset
        from popup.translate_popup import TranslatePopup

        with mock_chain_translate(return_value=("first second third", "T")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            popup.result.setPlainText("first second third")
            popup.resize(500, 240)
            popup.show()
            self.app.processEvents()
            cursor = popup.result.document().find("second")
            self.assertFalse(cursor.isNull())
            cursor.setPosition(cursor.selectionStart() + 3)
            point = popup.result.cursorRect(cursor).center()
            with mock.patch.object(
                popup, "_on_translation_position_clicked"
            ) as clicked:
                QTest.mousePress(
                    popup.result.viewport(),
                    Qt.MouseButton.LeftButton,
                    pos=point,
                )
                self.app.processEvents()
                clicked.assert_not_called()
                QTest.mouseRelease(
                    popup.result.viewport(),
                    Qt.MouseButton.LeftButton,
                    pos=point,
                )
                self.app.processEvents()
            clicked.assert_called_once()
            position = clicked.call_args.args[0]
            clicked.assert_called_once_with(position)
            self.assertEqual(
                speech_start_offset(popup.result.toPlainText(), position), 6)
        finally:
            popup.close()

    def test_translation_background_click_toggles_and_resumes_last_word(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        with mock_chain_translate(return_value=("first second", "T")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            popup.result.setPlainText("first second")
            popup._speech_available = True
            popup._speech_active = True
            popup._speech_start_offset = 0
            popup._speech_index = 0
            popup._speech_boundaries = {
                0: [(0, 10_000_000, 0, 5), (10_000_000, 10_000_000, 6, 12)]
            }
            with mock.patch.object(popup._player, "position", return_value=1100):
                popup._on_translation_background_clicked()
            self.assertFalse(popup._speech_active)
            self.assertEqual(popup._speech_start_offset, 6)

            with mock.patch.object(popup, "_request_speech") as request:
                popup._on_translation_background_clicked()
            request.assert_called_once_with(6)
        finally:
            popup._speech_active = False
            popup.close()

    def test_clicking_during_speech_seeks_in_ready_audio_without_restarting_tts(self):
        from unittest import mock
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QMediaPlayer
        from PySide6.QtTest import QTest
        from PySide6.QtCore import Qt
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        text = "first second third fourth"
        settings = dict(DEFAULTS)
        settings["target_language"] = "en"
        with mock_chain_translate(return_value=(text, "T")):
            popup = TranslatePopup(settings, source_text="hello")
        try:
            popup.result.setPlainText(text)
            popup._apply_translation_direction()
            popup.resize(500, 240)
            popup.show()
            self.app.processEvents()
            popup._speech_active = True
            popup._speech_paths = {0: "ready-audio.mp3"}
            popup._speech_boundaries = {
                0: [
                    (0, 1_000_000, 0, 5),
                    (20_000_000, 1_000_000, 6, 12),
                    (40_000_000, 1_000_000, 13, 18),
                ],
            }
            with (
                mock.patch.object(
                    popup._player, "source",
                    return_value=QUrl.fromLocalFile("ready-audio.mp3"),
                ),
                mock.patch.object(
                    popup._player, "mediaStatus",
                    return_value=QMediaPlayer.MediaStatus.BufferedMedia,
                ),
                mock.patch.object(popup._player, "pause") as pause,
                mock.patch.object(popup._player, "setPosition") as set_position,
                mock.patch.object(popup._player, "play") as play,
                mock.patch.object(popup, "_update_speech_highlight"),
            ):
                cursor = popup.result.document().find("second")
                cursor.setPosition(cursor.selectionStart() + 2)
                popup.result.setTextCursor(cursor)
                click_position = popup.result.cursorRect(cursor).center()
                QTest.mouseClick(
                    popup.result.viewport(),
                    Qt.MouseButton.LeftButton,
                    pos=click_position,
                )
                self.app.processEvents()

            pause.assert_called_once_with()
            set_position.assert_called_once_with(2_000)
            play.assert_called_once_with()
            self.assertEqual(popup._speech_start_offset, 6)
            self.assertIsNone(popup._pending_speech_offset)
            self.assertTrue(popup._speech_active)
            self.assertTrue(popup.speak_btn.isEnabled())
            self.assertEqual(popup.speak_btn.text(), "Stop")
        finally:
            popup._speech_active = False
            popup.close()

    def test_speech_stream_starts_only_for_the_current_segment(self):
        from unittest import mock
        from PySide6.QtCore import QUrl
        from popup.translate_popup import TranslatePopup

        with mock_chain_translate(return_value=("speech text", "T")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            popup._speech_active = True
            future_url = "http://127.0.0.1:12345/future"
            current_url = "http://127.0.0.1:12345/current"
            with mock.patch.object(popup, "_play_speech_source") as play_source:
                popup._on_speech_stream_ready(1, future_url)
                play_source.assert_not_called()

                popup._on_speech_stream_ready(0, current_url)
                play_source.assert_called_once_with(QUrl(current_url))
                self.assertEqual(popup._speech_playing_stream_index, 0)

                popup._pending_speech_offset = 3
                popup._on_speech_stream_ready(0, current_url)
                play_source.assert_called_once()
        finally:
            popup._speech_active = False
            popup.close()

    def test_arabic_diacritics_highlight_as_a_complete_word(self):
        from unittest import mock
        from popup.speech_worker import _segment_word_spans
        from popup.translate_popup import TranslatePopup

        text = "😀 مَرْحَبًا بِكُمْ"
        first_word = _segment_word_spans(text, [text])[0][0]
        with mock_chain_translate(
            return_value=(text, "T"),
        ):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            popup.result.setPlainText(text)
            popup._speech_active = True
            popup._speech_boundaries = {
                0: [(0, 10_000_000, *first_word)]
            }
            with mock.patch.object(popup._player, "position", return_value=500):
                popup._update_speech_highlight()
            selections = popup.result.extraSelections()
            self.assertEqual(len(selections), 1)
            self.assertEqual(selections[0].cursor.selectedText(), "مَرْحَبًا")
            self.assertEqual(
                selections[0].format.background().color().name(),
                "#fff176",
            )
            self.assertFalse(
                hasattr(popup.result, "_speech_highlight_rect"))
        finally:
            popup._speech_active = False
            popup._clear_speech_highlight()
            try:
                if popup._worker and not popup._worker.wait(20000):
                    popup._worker.terminate()
                    popup._worker.wait(2000)
            except RuntimeError:
                pass
            popup.close()

    def test_speech_highlight_scrolls_translation_to_follow_current_word(self):
        from unittest import mock
        from PySide6.QtCore import QEvent
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        with mock_chain_translate(
            return_value=("\u0646\u0635", "T"),
        ):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            text = "word " * 500 + "target"
            popup.result.setPlainText(text)
            popup.resize(400, 220)
            popup.show()
            self.app.processEvents()
            scrollbar = popup.result.verticalScrollBar()
            self.assertGreater(scrollbar.maximum(), 0)
            scrollbar.setValue(0)
            popup._speech_active = True
            target_start = len(text) - len("target")
            popup._speech_boundaries = {
                0: [(0, 2_000_000_000, target_start, len(text))]
            }
            with mock.patch.object(popup._player, "position", return_value=100_000):
                popup._update_speech_highlight()
            self.assertGreater(scrollbar.value(), 0)
            self.assertEqual(len(popup.result.extraSelections()), 1)

            scrollbar.setValue(0)
            popup.eventFilter(
                popup.result.viewport(), QEvent(QEvent.Type.Wheel))
            with mock.patch.object(popup._player, "position", return_value=100_000):
                popup._update_speech_highlight()
            self.assertEqual(scrollbar.value(), 0)
            self.assertEqual(len(popup.result.extraSelections()), 1)

            popup._resume_speech_auto_scroll()
            with mock.patch.object(popup._player, "position", return_value=100_000):
                popup._update_speech_highlight()
            self.assertGreater(scrollbar.value(), 0)

            for _ in range(160):
                with mock.patch.object(
                    popup._player, "position", return_value=100_000
                ):
                    popup._update_speech_highlight()
            from PySide6.QtGui import QTextCursor
            selections = popup.result.extraSelections()
            highlighted_cursor = QTextCursor(selections[0].cursor)
            self.assertEqual(scrollbar.value(), scrollbar.maximum())

            popup._speech_auto_scroll_timer.stop()
            scrollbar.setValue(0)
            text = "first second"
            popup.result.setPlainText(text)
            popup._speech_boundaries = {
                0: [
                    (0, 1_000_000, 0, 5),
                    (1_000_000, 1_000_000, 6, 12),
                ]
            }
            popup._suspend_speech_auto_scroll()
            with mock.patch.object(popup._player, "position", return_value=200):
                popup._player.positionChanged.emit(200)
            selection = popup.result.extraSelections()[0]
            self.assertEqual(selection.cursor.selectedText(), "second")
            self.assertEqual(scrollbar.value(), 0)
        finally:
            popup._speech_active = False
            popup._clear_speech_highlight()
            try:
                if popup._worker and not popup._worker.wait(20000):
                    popup._worker.terminate()
                    popup._worker.wait(2000)
            except RuntimeError:
                pass
            popup.close()

    def test_speech_highlight_holds_last_word_during_pause(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        text = "مرحبا بالعالم"
        with mock_chain_translate(return_value=(text, "T")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            if popup._worker is not None:
                popup._worker.wait(20000)
            self.app.processEvents()
            popup.result.setPlainText(text)
            popup._speech_active = True
            popup._speech_boundaries = {
                0: [(0, 100_000, 0, 5), (200_000, 100_000, 6, 13)],
            }
            # 15ms sits in the pause between the two words: the highlight
            # must hold the word just spoken, not vanish until the next one.
            with mock.patch.object(popup._player, "position", return_value=15):
                popup._update_speech_highlight()
            highlighted = popup.result.extraSelections()
            self.assertEqual(len(highlighted), 1)
            self.assertEqual(highlighted[0].cursor.selectedText(), "مرحبا")
            self.assertEqual(
                highlighted[0].format.background().color().name(), "#fff176")
        finally:
            popup._speech_active = False
            popup._clear_speech_highlight()
            popup.close()

    def test_clicked_word_highlight_survives_speech_restart(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        text = "first second third"
        with mock_chain_translate(return_value=(text, "T")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            if popup._worker is not None:
                popup._worker.wait(20000)
            self.app.processEvents()
            popup.result.setPlainText(text)
            popup._speech_active = True
            word = popup.result.document().find("second")
            with mock.patch.object(popup, "_request_speech"):
                popup._on_translation_position_clicked(word.selectionStart())
            # NOTE: keep the list alive in a variable; chaining
            # `extraSelections()[0].cursor` on the temporary can hand back a
            # dead shiboken wrapper.
            selections = popup.result.extraSelections()
            self.assertEqual(len(selections), 1)
            self.assertEqual(
                selections[0].cursor.selectedText(), "second")
            # Restart preparation wipes the known boundaries; the clicked
            # word must stay highlighted until the new synthesis takes over.
            with mock.patch(
                "popup.translate_popup.AudioStreamServer",
                return_value=mock.Mock(),
            ), mock.patch(
                "popup.translate_popup.SpeechWorker",
                return_value=mock.Mock(),
            ):
                popup._begin_speech(popup._speech_start_offset)
            selections = popup.result.extraSelections()
            self.assertEqual(len(selections), 1)
            self.assertEqual(selections[0].cursor.selectedText(), "second")
        finally:
            popup._speech_active = False
            popup._clear_speech_highlight()
            popup.close()

    def test_translation_ready_prefills_speech_cache_without_playing(self):
        import asyncio
        import tempfile
        from unittest import mock
        from PySide6.QtCore import Qt
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker
        from popup.translate_popup import (
            SPEECH_PREFETCH_DELAY_MS,
            TranslatePopup,
        )
        from config.settings import DEFAULTS

        text = "مرحبا بالعالم كيف حالك اليوم"
        with mock_chain_translate(return_value=(text, "T")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            if popup._worker is not None:
                popup._worker.wait(20000)
            self.app.processEvents()
            popup.result.setPlainText(text)
            popup.show()
            self.app.processEvents()
            self.assertEqual(
                popup._speech_prefetch_timer.interval(),
                SPEECH_PREFETCH_DELAY_MS,
            )
            self.assertEqual(SPEECH_PREFETCH_DELAY_MS, 500)
            calls = []

            class FakeCommunicate:
                def __init__(self, text, *_args, **_kwargs):
                    calls.append(text)
                    self.text = text

                async def stream(self):
                    yield {"type": "audio", "data": b"prefetched-mp3"}
                    for word in self.text.split():
                        yield {
                            "type": "WordBoundary",
                            "offset": 0,
                            "duration": 1_000_000,
                            "text": word,
                        }

            cache_tmp = tempfile.mkdtemp(prefix="harmony-tts-prefetch-")
            fake_edge = type("Edge", (), {"Communicate": FakeCommunicate})
            with mock.patch.dict(
                "os.environ", {"HARMONY_TTS_CACHE": cache_tmp}
            ), mock.patch.object(speech_worker, "edge_tts", fake_edge):
                popup._start_speech_prefetch()
                for _ in range(500):
                    self.app.processEvents()
                    if popup._speech_prefetch_worker is None and calls:
                        break
                    import time
                    time.sleep(0.02)
                self.assertEqual(len(calls), 1)
                self.assertIsNone(popup._speech_prefetch_worker)
                # Prefetch must never touch playback: no source, no active
                # speech, no highlight.
                self.assertTrue(popup._player.source().isEmpty())
                self.assertFalse(popup._speech_active)
                self.assertEqual(popup.result.extraSelections(), [])

                # The read path for the identical request must now complete
                # with zero network calls: this is the sub-second start.
                class ExplodingCommunicate:
                    def __init__(self, *_args, **_kwargs):
                        raise AssertionError(
                            "read-aloud must not touch the network: "
                            "the prefetch already cached it")

                with (
                    mock.patch.object(
                        speech_worker,
                        "edge_tts",
                        type("Edge", (), {
                            "Communicate": ExplodingCommunicate}),
                    ),
                    mock.patch.object(
                        popup, "_play_speech_segment",
                    ) as play_segment,
                ):
                    popup._begin_speech(0)
                # Cached playback uses a temporary audio file, not a stream.
                self.assertTrue(popup._speech_read_path)
                self.assertTrue(os.path.isfile(popup._speech_read_path))
                play_segment.assert_called_once_with(
                    popup._speech_read_path, 0)
                self.assertFalse(popup._speech_stream_urls)
                self.assertTrue(popup._speech_active)
                popup._speech_active = False
                popup._player.stop()
        finally:
            popup._cancel_speech_prefetch()
            popup.close()

    def test_begin_speech_cancels_running_prefetch(self):
        import threading
        from unittest import mock
        from popup import speech_worker
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        text = "مرحبا بالعالم كيف حالك اليوم"
        with mock_chain_translate(return_value=(text, "T")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            if popup._worker is not None:
                popup._worker.wait(20000)
            self.app.processEvents()
            popup.show()
            self.app.processEvents()
            release = threading.Event()

            class BlockingCommunicate:
                def __init__(self, *_args, **_kwargs):
                    pass

                async def stream(self):
                    import asyncio
                    await asyncio.to_thread(release.wait, 10)
                    yield {"type": "audio", "data": b"slow-mp3"}
                    yield {
                        "type": "WordBoundary",
                        "offset": 0,
                        "duration": 1,
                        "text": "مرحبا",
                    }

            fake_edge = type(
                "Edge", (), {"Communicate": BlockingCommunicate})
            fake_speech_worker = mock.Mock()
            fake_speech_worker._segments = []
            with mock.patch.object(
                speech_worker, "edge_tts", fake_edge
            ), mock.patch(
                "popup.translate_popup.AudioStreamServer",
                return_value=mock.Mock(),
            ), mock.patch(
                "popup.translate_popup.SpeechWorker",
                return_value=fake_speech_worker,
            ), mock.patch.object(
                popup, "_try_play_cached_full_text", return_value=False,
            ):
                popup._start_speech_prefetch()
                for _ in range(200):
                    self.app.processEvents()
                    worker = popup._speech_prefetch_worker
                    if worker is not None and worker.isRunning():
                        break
                    import time
                    time.sleep(0.02)
                self.assertIsNotNone(popup._speech_prefetch_worker)
                # Pressing Read while prefetch runs must stop it: one
                # synthesis at a time, and the fresh read owns the network.
                popup._begin_speech(0)
                self.assertIsNone(popup._speech_prefetch_worker)
            release.set()
        finally:
            popup._cancel_speech_prefetch()
            popup.close()

    def test_click_seeks_in_cached_read_without_resynthesis(self):
        import tempfile
        from unittest import mock
        from popup import speech_worker
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        text = "مرحبا بالعالم كيف حالك اليوم"
        with mock_chain_translate(return_value=(text, "T")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            if popup._worker is not None:
                popup._worker.wait(20000)
            self.app.processEvents()
            popup.result.setPlainText(text)
            popup.show()
            self.app.processEvents()
            calls = []

            class FakeCommunicate:
                def __init__(self, text, *_args, **_kwargs):
                    calls.append(text)
                    self.text = text

                async def stream(self):
                    yield {"type": "audio", "data": b"cached-mp3"}
                    for index, word in enumerate(self.text.split()):
                        yield {
                            "type": "WordBoundary",
                            "offset": index * 1_000_000,
                            "duration": 1_000_000,
                            "text": word,
                        }

            cache_tmp = tempfile.mkdtemp(prefix="harmony-tts-seek-")
            fake_edge = type("Edge", (), {"Communicate": FakeCommunicate})
            with mock.patch.dict(
                "os.environ", {"HARMONY_TTS_CACHE": cache_tmp}
            ), mock.patch.object(speech_worker, "edge_tts", fake_edge):
                popup._start_speech_prefetch()
                for _ in range(500):
                    self.app.processEvents()
                    if popup._speech_prefetch_worker is None and calls:
                        break
                    import time
                    time.sleep(0.02)
                self.assertEqual(len(calls), 1)

                class ExplodingCommunicate:
                    def __init__(self, *_args, **_kwargs):
                        raise AssertionError(
                            "click must seek, never re-synthesise")

                exploding = type(
                    "Edge", (), {"Communicate": ExplodingCommunicate})
                with mock.patch.object(
                    speech_worker, "edge_tts", exploding
                ), mock.patch.object(
                    popup._player, "setSource"
                ) as set_source, mock.patch.object(
                    popup._player, "play"
                ), mock.patch.object(
                    popup, "_request_speech"
                ) as request, mock.patch.object(
                    popup, "_begin_speech"
                ) as begin:
                    word = popup.result.document().find("كيف")
                    popup._on_translation_position_clicked(
                        word.selectionStart())
                    # Third word => audio tick 2_000_000 => 200ms, played
                    # from a plain temp file: files seek natively, join the
                    # existing temp lifecycle, and never block the UI.
                    self.assertFalse(request.called)
                    self.assertFalse(begin.called)
                    self.assertTrue(popup._speech_active)
                    self.assertFalse(popup._speech_stream_urls)
                    self.assertTrue(popup._speech_paths)
                    self.assertEqual(popup._pending_player_position_ms, 200)
                    self.assertTrue(set_source.called)
                    played_url = set_source.call_args.args[0]
                    self.assertTrue(played_url.isLocalFile())
                    # No local server may be involved: pushing audio through
                    # a live stream on the UI thread is what froze playback
                    # and produced seek failures.
                    self.assertIsNone(popup._speech_stream_server)
                    selections = popup.result.extraSelections()
                    self.assertEqual(len(selections), 1)
                    self.assertEqual(
                        selections[0].cursor.selectedText(), "كيف")

                    # A second click seeks again, still with no synthesis.
                    word = popup.result.document().find("اليوم")
                    popup._on_translation_position_clicked(
                        word.selectionStart())
                    self.assertFalse(request.called)
                    self.assertFalse(begin.called)
                    self.assertEqual(popup._pending_player_position_ms, 400)
        finally:
            popup._speech_active = False
            popup._player.stop()
            popup._cancel_speech_prefetch()
            popup.close()

    def test_closing_while_speech_worker_runs_does_not_block_the_ui(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        with mock_chain_translate(
            return_value=("\u0646\u0635", "T"),
        ):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            if popup._worker is not None:
                self.assertTrue(popup._worker.wait(5000))
            self.app.processEvents()
            popup.show()
            self.app.processEvents()

            worker = mock.Mock()
            worker.isRunning.return_value = True
            popup._speech_worker = worker
            popup._speech_active = True
            popup._speech_highlight_timer.start()

            self.assertFalse(popup.close())
            self.assertFalse(popup.isVisible())
            self.assertTrue(popup._close_pending)
            worker.requestInterruption.assert_called_once_with()
            worker.wait.assert_not_called()
            worker.terminate.assert_not_called()
            self.assertFalse(popup._speech_highlight_timer.isActive())
        finally:
            popup._speech_worker = None
            popup._close_pending = False
            popup.close()

    def test_speech_can_be_restarted_immediately_after_stop(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        class RunningWorker:
            def __init__(self):
                self.interrupted = False

            def isRunning(self):
                return True

            def requestInterruption(self):
                self.interrupted = True

            def isInterruptionRequested(self):
                return self.interrupted

            def cleanup_output(self):
                pass

            def deleteLater(self):
                pass

        with mock_chain_translate(
            return_value=("some translated words", "T"),
        ):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            if popup._worker is not None:
                self.assertTrue(popup._worker.wait(5000))
            self.app.processEvents()
            popup.result.setPlainText("some translated words")
            worker = RunningWorker()
            popup._speech_worker = worker
            popup._speech_active = True
            popup.speak_btn.setEnabled(True)

            popup._toggle_speech()
            self.assertFalse(popup._speech_active)
            self.assertTrue(worker.interrupted)
            self.assertTrue(popup.speak_btn.isEnabled())
            self.assertEqual(popup.speak_btn.text(), "Read aloud")

            popup._toggle_speech()
            self.assertEqual(popup._pending_speech_offset, 0)
            self.assertFalse(popup.speak_btn.isEnabled())

            with (
                mock.patch("popup.translate_popup.SpeechWorker", RunningWorker),
                mock.patch.object(popup, "sender", return_value=worker),
                mock.patch.object(popup, "_begin_speech") as begin_speech,
            ):
                popup._on_speech_worker_finished()
            begin_speech.assert_called_once_with(0)
        finally:
            popup.close()

    def test_read_aloud_resumes_from_current_word_after_stop(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        with mock_chain_translate(
            return_value=("first second third", "T"),
        ):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            if popup._worker is not None:
                self.assertTrue(popup._worker.wait(5000))
            self.app.processEvents()
            popup.result.setPlainText("first second third")
            popup._speech_available = True
            popup._speech_active = True
            popup._speech_start_offset = 0
            popup._speech_index = 0
            popup._speech_boundaries = {
                0: [
                    (0, 10_000_000, 0, 5),
                    (10_000_000, 10_000_000, 6, 12),
                    (20_000_000, 10_000_000, 13, 18),
                ],
            }
            popup._player.position = mock.Mock(return_value=1_500)

            with (
                mock.patch.object(popup._player, "stop"),
                mock.patch.object(popup._player, "setSource"),
                mock.patch.object(popup, "_request_speech") as request_speech,
            ):
                popup._toggle_speech()
                self.assertFalse(popup._speech_active)
                self.assertEqual(popup._speech_start_offset, 6)

                popup._toggle_speech()

            request_speech.assert_called_once_with(6)
        finally:
            popup.close()

    def test_pending_close_finishes_after_speech_worker_stops(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        from popup.speech_worker import SpeechWorker
        from config.settings import DEFAULTS

        with mock_chain_translate(
            return_value=("\u0646\u0635", "T"),
        ):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            if popup._worker is not None:
                self.assertTrue(popup._worker.wait(5000))
            self.app.processEvents()

            worker = SpeechWorker("speech")
            popup._speech_worker = worker
            popup._close_pending = True

            with (
                mock.patch.object(popup, "sender", return_value=worker),
                mock.patch.object(popup, "close", wraps=popup.close) as close,
                mock.patch.object(worker, "isInterruptionRequested", return_value=True),
                mock.patch.object(worker, "cleanup_output") as cleanup_output,
                mock.patch.object(worker, "deleteLater"),
            ):
                popup._on_speech_worker_finished()
                self.assertIsNone(popup._speech_worker)
                self.assertFalse(popup._close_pending)
                cleanup_output.assert_called_once_with()
                self.app.processEvents()
                close.assert_called_once_with()
        finally:
            popup.close()

    def test_ctrl_wheel_emits_zoom_step(self):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QImage, QWheelEvent
        from PySide6.QtWidgets import QApplication
        from reader.continuous import ContinuousReader

        reader = ContinuousReader(lambda i, z: QImage(100, 100, QImage.Format.Format_RGB888))
        try:
            reader.set_document([(200.0, 300.0)] * 3)
            self.assertEqual(reader.zoom, 1.5)
            steps = []
            reader.zoomStep.connect(steps.append)
            ev = QWheelEvent(QPointF(10, 10), QPointF(100, 100), QPoint(0, 0),
                             QPoint(0, 120), Qt.MouseButton.NoButton,
                             Qt.KeyboardModifier.ControlModifier,
                             Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(reader.viewport(), ev)
            self.assertEqual(steps, [1])
            ev2 = QWheelEvent(QPointF(10, 10), QPointF(100, 100), QPoint(0, 0),
                              QPoint(0, -120), Qt.MouseButton.NoButton,
                              Qt.KeyboardModifier.NoModifier,
                              Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(reader.viewport(), ev2)  # no Ctrl -> scrolls, no zoom
            self.assertEqual(steps, [1])
            reader.set_zoom(2.0)
            self.assertEqual(reader.zoom, 2.0)
        finally:
            reader.close()

    def test_thumb_panel_lists_pages(self):
        import tempfile
        import pymupdf
        from PySide6.QtCore import QSize, Qt
        from PySide6.QtGui import QColor, QImage
        from main import app_stylesheet
        from reader.thumbnails import ThumbPanel

        tmp = os.path.join(tempfile.gettempdir(), "thumbs_test.pdf")
        doc = pymupdf.open()
        for n in range(2):
            pg = doc.new_page()
            pg.insert_text((72, 100), f"page {n}")
        doc.save(tmp)
        doc.close()
        panel = ThumbPanel()
        try:
            panel.resize(170, 500)
            panel.show()
            panel.set_document(tmp, 2)
            self.assertEqual(panel.list.count(), 2)
            first_item = panel.list.item(0)
            self.assertEqual(first_item.text(), "1")
            self.assertEqual(
                first_item.textAlignment(),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            self.assertEqual(panel.list.objectName(), "pdfThumbnails")
            style = app_stylesheet("Dark Slate")
            self.assertIn("QListWidget#pdfThumbnails::viewport", style)
            self.assertIn("QListWidget#pdfBookmarks::viewport", style)
            self.assertIn("QScrollArea#pdfCanvas", style)
            self.assertIn("background-color: #090D12", style)
            self.assertIn("color: #F0F6FC", style)
            self.assertNotIn("background-color: #FFFFFF", style)
            self.assertEqual(panel.objectName(), "pdfThumbnailsPanel")
            self.assertEqual(
                panel.list.verticalScrollBar().objectName(),
                "pdfThumbnailScrollBar")
            self.assertEqual(panel.list.verticalScrollBar().width(), 18)
            self.assertEqual(
                panel.list.verticalScrollMode(),
                panel.list.ScrollMode.ScrollPerPixel)
            self.assertIn(
                "QScrollBar#pdfThumbnailScrollBar::handle:vertical", style)
            gap = panel.ITEM_GAP
            self.assertGreaterEqual(
                panel.list.viewport().width() - panel.list.iconSize().width(),
                2 * gap)
            self.assertEqual(
                panel.list.gridSize().width(),
                panel.list.iconSize().width() + 2 * gap)
            self.assertEqual(
                panel.list.gridSize().height(),
                panel.list.iconSize().height() + panel.list.fontMetrics().height() + gap)
            self.assertEqual(panel.list.spacing(), 0)
            image = QImage(24, 32, QImage.Format.Format_ARGB32)
            image.fill(QColor("white"))
            rounded = ThumbPanel._scaled_thumbnail_icon(
                image, QSize(24, 32)).pixmap(QSize(24, 32)).toImage()
            self.assertEqual(rounded.pixelColor(0, 0).alpha(), 0)
            self.assertEqual(rounded.pixelColor(12, 0).alpha(), 255)
            self.assertEqual(rounded.pixelColor(12, 16).alpha(), 255)
            first_rect = panel.list.visualItemRect(panel.list.item(0))
            second_rect = panel.list.visualItemRect(panel.list.item(1))
            self.assertEqual(
                second_rect.y() - first_rect.y(), panel.list.gridSize().height())
            panel.mark_current(1)
            self.assertEqual(panel.list.currentRow(), 1)
            chosen = []
            panel.pageChosen.connect(chosen.append)
            panel.list.itemClicked.emit(panel.list.item(0))
            self.assertEqual(chosen, [0])
        finally:
            panel.clear_document()
            panel.close()

    def test_thumbnail_zoom_resizes_rendered_page_image_not_only_grid(self):
        from PySide6.QtGui import QImage
        from reader.thumbnails import ThumbPanel

        panel = ThumbPanel()
        try:
            panel.resize(300, 500)
            panel.show()
            panel.set_document(
                _make_pdf_file("thumbnail_zoom_image_test.pdf", pages=2),
                2,
                render_thumbnails=False,
            )
            self.app.processEvents()
            image = QImage(132, 180, QImage.Format.Format_RGB888)
            image.fill(0x336699)
            panel._on_thumb(0, image)
            item = panel.list.item(0)
            before = item.icon().pixmap(panel.list.iconSize()).size()

            panel.set_thumbnail_zoom(1.5)
            after = item.icon().pixmap(panel.list.iconSize()).size()

            self.assertGreater(after.width(), before.width())
            self.assertEqual(after, panel.list.iconSize())
            panel.set_thumbnail_zoom(0.5)
            panel.set_thumbnail_zoom(1.5)
            self.assertEqual(
                item.icon().pixmap(panel.list.iconSize()).size(),
                panel.list.iconSize(),
            )
            self.assertEqual(
                panel.list.gridSize().height(),
                panel.list.iconSize().height()
                + panel.list.fontMetrics().height()
                + panel.ITEM_GAP,
            )
        finally:
            panel.clear_document()
            panel.close()

    def test_thumbnail_process_renders_pages_at_display_size(self):
        import time
        from reader.thumbnails import ThumbPanel

        panel = ThumbPanel()
        received = []
        try:
            panel.resize(220, 500)
            panel.show()
            panel.set_document(
                _make_pdf_file("thumbnail_process_test.pdf", pages=2), 2)
            worker = panel._worker
            self.assertIsNotNone(worker)
            worker.thumbReady.connect(
                lambda index, _image: received.append(index))
            deadline = time.time() + 15
            while worker.isRunning() and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.app.processEvents()

            self.assertFalse(worker.isRunning())
            self.assertEqual(sorted(received), [0, 1])
            self.assertEqual(worker._width, panel.list.iconSize().width())
        finally:
            panel.clear_document()
            panel.close()

    def test_thumbnail_cache_tracks_the_visible_pages(self):
        import time
        from reader.thumbnails import ThumbPanel

        page_count = 40
        panel = ThumbPanel()
        try:
            panel.resize(220, 350)
            panel.show()
            panel.set_document(
                _make_pdf_file(
                    "thumbnail_visible_cache_test.pdf", pages=page_count),
                page_count,
            )
            self.assertIsNotNone(panel._worker)
            self.assertLess(len(panel._worker._indices), page_count)

            deadline = time.time() + 10
            while panel._worker.isRunning() and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.app.processEvents()

            last_page = page_count - 1
            panel.list.verticalScrollBar().setValue(
                panel.list.verticalScrollBar().maximum())
            deadline = time.time() + 10
            while (last_page not in panel._thumbnail_images
                   and time.time() < deadline):
                self.app.processEvents()
                time.sleep(0.01)
            self.app.processEvents()

            self.assertIn(last_page, panel._thumbnail_images)
            self.assertNotIn(0, panel._thumbnail_images)
            self.assertLess(len(panel._thumbnail_images), page_count)

            worker = panel._worker
            # Every other wait loop in this file carries a deadline; this one
            # did not, so a ThumbWorker that never finished hung the entire
            # suite with no output and no failure. Fail loudly instead.
            deadline = time.time() + 30
            while worker is not None and worker.isRunning():
                if time.time() > deadline:
                    self.fail(
                        "ThumbWorker still running after 30s; it never "
                        "finished, so this is a stuck worker rather than a "
                        "slow machine")
                self.app.processEvents()
                time.sleep(0.01)
            self.assertFalse(worker is not None and worker.isRunning(),
                             "ThumbWorker must stop before the panel is hidden")
            panel.hide()
            self.app.processEvents()
            self.assertFalse(panel._thumbnail_images)

            panel.show()
            deadline = time.time() + 10
            while (last_page not in panel._thumbnail_images
                   and time.time() < deadline):
                self.app.processEvents()
                time.sleep(0.01)
            self.assertIn(last_page, panel._thumbnail_images)
        finally:
            panel.clear_document()
            panel.close()

    def test_thumbnail_process_group_stops_cleanly(self):
        from reader.thumbnails import ThumbWorker

        worker = ThumbWorker(
            _make_pdf_file("thumbnail_process_cancel.pdf", pages=100),
            100,
            width=132,
        )
        worker.start()
        worker.stop()
        self.assertTrue(worker.wait(10000))

    def test_thumbnail_context_menu_actions_and_multiselect_extraction(self):
        from PySide6.QtCore import QSize, Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QListWidgetItem
        from main import THEMES
        from reader.bookview import BookView
        from reader.thumbnails import ThumbPanel

        panel = ThumbPanel()
        book = BookView()
        try:
            book.apply_theme(THEMES["Dark Slate"])
            self.assertIn("#8BE9FD", book.thumbs.styleSheet())
            panel.resize(240, 500)
            panel.show()
            for page in range(6):
                item = QListWidgetItem(str(page + 1))
                item.setSizeHint(QSize(70, 90))
                panel.list.addItem(item)
            self.app.processEvents()
            self.assertEqual(
                panel.list.selectionMode(),
                panel.list.SelectionMode.ExtendedSelection,
            )
            first_range_item = panel.list.item(1)
            last_range_item = panel.list.item(5)
            QTest.mouseClick(
                panel.list.viewport(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                panel.list.visualItemRect(first_range_item).center(),
            )
            QTest.mouseClick(
                panel.list.viewport(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.ShiftModifier,
                panel.list.visualItemRect(last_range_item).center(),
            )
            self.assertEqual(
                [panel.list.row(item) for item in panel.list.selectedItems()],
                [1, 2, 3, 4, 5],
            )
            translate_pages = []
            extract_pages = []
            copy_pages = []
            refresh_requests = []
            reopen_requests = []
            panel.translatePageRequested.connect(translate_pages.append)
            panel.extractPagesRequested.connect(extract_pages.append)
            panel.copyPageImageRequested.connect(copy_pages.append)
            panel.refreshRequested.connect(
                lambda: refresh_requests.append(True))
            panel.reopenRequested.connect(lambda: reopen_requests.append(True))

            menu = panel._page_context_menu(
                5, panel._selected_page_indices(panel.list))
            self.assertEqual(
                [action.text() for action in menu.actions()],
                [
                    "Translate full page", "Extract pages…",
                    "Delete pages", "Copy page image", "", "Refresh",
                    "ReOpen",
                ],
            )
            self.assertEqual(
                menu.styleSheet(),
                "QMenu::item { font-size: 17px; padding: 9px 24px; }")
            self.assertTrue(all(
                action.icon().isNull() and action.shortcut().isEmpty()
                for action in menu.actions() if not action.isSeparator()
            ))
            menu.actions()[1].trigger()
            self.assertEqual(translate_pages, [])
            self.assertEqual(extract_pages, [[1, 2, 3, 4, 5]])
            self.assertEqual(copy_pages, [])
            menu.deleteLater()

            single_menu = panel._page_context_menu(2, [2])
            self.assertEqual(
                [action.text() for action in single_menu.actions()],
                [
                    "Translate full page", "Extract page…", "Delete page",
                    "Copy page image", "", "Refresh", "ReOpen",
                ],
            )
            single_menu.actions()[0].trigger()
            single_menu.actions()[3].trigger()
            single_menu.actions()[5].trigger()
            single_menu.actions()[6].trigger()
            self.assertEqual(translate_pages, [2])
            self.assertEqual(copy_pages, [2])
            self.assertEqual(refresh_requests, [True])
            self.assertEqual(reopen_requests, [True])
            single_menu.deleteLater()

            panel.list.clear()
            panel.resize(240, 260)
            for page in range(40):
                item = QListWidgetItem(str(page + 1))
                item.setSizeHint(QSize(70, 90))
                panel.list.addItem(item)
            self.app.processEvents()
            anchor = panel.list.item(1)
            last = panel.list.item(35)
            QTest.mouseClick(
                panel.list.viewport(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                panel.list.visualItemRect(anchor).center(),
            )
            panel.list.scrollToItem(last)
            self.app.processEvents()
            QTest.mouseClick(
                panel.list.viewport(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.ShiftModifier,
                panel.list.visualItemRect(last).center(),
            )
            selected_range = panel._selected_page_indices(panel.list)
            self.assertEqual(selected_range, list(range(1, 36)))
        finally:
            panel.close()

    def test_reader_keeps_only_thumbnail_and_pdf_panes(self):
        from PySide6.QtWidgets import QToolButton
        from reader.bookview import BookView

        book = BookView()
        try:
            self.assertEqual(book.splitter.count(), 2)
            self.assertIs(book.splitter.widget(0), book.thumbs)
            self.assertIs(book.splitter.widget(1), book.reader)
            self.assertFalse(book.findChild(QToolButton, "studyTool_notes"))
        finally:
            book.close()

    def test_thumbnail_wheel_scrolls_in_standard_direction(self):
        import tempfile
        import time
        import pymupdf
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from reader.thumbnails import ThumbPanel

        tmp = os.path.join(tempfile.gettempdir(), "thumb_scroll_direction_test.pdf")
        doc = pymupdf.open()
        for _ in range(12):
            doc.new_page()
        doc.save(tmp)
        doc.close()
        panel = ThumbPanel()
        try:
            panel.resize(190, 450)
            panel.show()
            panel.set_document(tmp, 12)
            self.app.processEvents()
            scrollbar = panel.list.verticalScrollBar()
            self.assertGreater(scrollbar.maximum(), 0)
            scrollbar.setValue(300)

            event = QWheelEvent(
                QPointF(10, 10), QPointF(100, 100), QPoint(0, 0), QPoint(0, 120),
                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase, False)
            self.app.sendEvent(panel.list.viewport(), event)
            animation = panel._scroll_animations[panel.list]
            self.assertTrue(animation.state())
            deadline = time.time() + 2
            while animation.state() and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertEqual(scrollbar.value(), 300 - scrollbar.singleStep())

            scrollbar.setValue(300)
            self.assertEqual(scrollbar.value(), 300)

            event_down = QWheelEvent(
                QPointF(10, 10), QPointF(100, 100), QPoint(0, 0), QPoint(0, -120),
                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase, False)
            self.app.sendEvent(panel.list.viewport(), event_down)
            animation = panel._scroll_animations[panel.list]
            deadline = time.time() + 2
            while animation.state() and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertEqual(scrollbar.value(), 300 + scrollbar.singleStep())
        finally:
            panel.clear_document()
            panel.close()

    def test_thumbnail_marks_current_page_and_vertical_progress(self):
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QStyle, QStyleOptionViewItem
        from reader.thumbnails import ThumbPanel

        panel = ThumbPanel()
        try:
            panel.resize(300, 500)
            panel.show()
            panel.set_document(
                _make_pdf_file("thumbnail_page_position.pdf", pages=3),
                3,
                render_thumbnails=False,
            )
            image = QImage(132, 180, QImage.Format.Format_RGB888)
            image.fill(0xFFFFFF)
            panel._on_thumb(0, image)
            panel._on_thumb(1, image)

            panel.mark_current_position(1, 0.5)
            self.app.processEvents()

            self.assertEqual(panel.list.currentRow(), 1)
            self.assertEqual(
                panel.list.item(1).data(panel.VIEW_POSITION_ROLE), 0.5)
            self.assertIsNone(
                panel.list.item(0).data(panel.VIEW_POSITION_ROLE))
            delegate = panel.list.itemDelegate()
            self.assertIsNotNone(delegate)

            rendered = QImage(panel.list.size(), QImage.Format.Format_ARGB32)
            rendered.fill(0)
            index = panel.list.indexFromItem(panel.list.item(1))
            option = QStyleOptionViewItem()
            option.initFrom(panel.list)
            option.rect = panel.list.visualRect(index)
            option.widget = panel.list
            option.decorationSize = panel.list.iconSize()
            delegate.initStyleOption(option, index)
            painter = QPainter(rendered)
            delegate.paint(painter, option, index)
            painter.end()
            red_pixels = sum(
                1 for y in range(rendered.height())
                for x in range(rendered.width())
                if rendered.pixelColor(x, y).red() > 220
                and rendered.pixelColor(x, y).green() < 100
                and rendered.pixelColor(x, y).blue() < 100
            )
            self.assertGreater(red_pixels, 0, "current thumbnail should show a red position bar")
            icon_rect = panel.list.style().subElementRect(
                QStyle.SubElement.SE_ItemViewItemDecoration,
                option,
                panel.list,
            )
            cyan_pixels = sum(
                1 for y in range(icon_rect.top(), icon_rect.bottom() + 1)
                for x in range(icon_rect.left(), icon_rect.right() + 1)
                if (
                    (color := rendered.pixelColor(x, y)).green()
                    > color.red() + 15
                    and color.blue() > color.green()
                )
            )
            self.assertGreater(
                cyan_pixels, 0, "current thumbnail should have a translucent cyan tint")

            panel.set_bookmarks({1})
            panel.mark_current_position(1, 0.5)
            panel.mark_current_position(0, 0.25)
            self.assertIsNone(
                panel.list.item(1).data(panel.VIEW_POSITION_ROLE))
            self.assertIsNone(
                panel.bookmarks_list.item(0).data(panel.VIEW_POSITION_ROLE))
        finally:
            panel.clear_document()
            panel.close()

    def test_current_thumbnail_is_kept_centered_while_reading(self):
        from reader.thumbnails import ThumbPanel

        panel = ThumbPanel()
        try:
            panel.resize(190, 500)
            panel.show()
            panel.set_document(
                _make_pdf_file("thumbnail_centering.pdf", pages=40),
                40,
                render_thumbnails=False,
            )
            self.app.processEvents()

            panel.mark_current_position(20, 0.4)
            self.app.processEvents()
            item_rect = panel.list.visualItemRect(panel.list.item(20))
            viewport_center = panel.list.viewport().rect().center().y()

            self.assertLessEqual(
                abs(item_rect.center().y() - viewport_center),
                panel.list.gridSize().height() // 2,
            )
        finally:
            panel.clear_document()
            panel.close()

    def test_reader_reports_page_relative_scroll_progress(self):
        from PySide6.QtGui import QImage
        from reader.continuous import ContinuousReader

        reader = ContinuousReader(
            lambda _index, _zoom: QImage(100, 100, QImage.Format.Format_RGB888))
        positions = []
        reader.viewPositionChanged.connect(
            lambda page, progress: positions.append((page, progress)))
        try:
            reader.resize(400, 300)
            reader.show()
            reader.set_document([(200.0, 300.0)] * 4, render=False)
            self.app.processEvents()
            reader.verticalScrollBar().setValue(100)
            self.app.processEvents()

            self.assertTrue(positions)
            page, progress = positions[-1]
            self.assertEqual(page, 0)
            self.assertGreater(progress, 0.0)
            self.assertLess(progress, 1.0)
        finally:
            reader.close()

    def test_a_stuck_thumbnail_worker_is_never_dropped(self):
        """A QThread that is still running must stay referenced. Dropping the
        last Python reference to a live QThread aborts the process with
        'QThread: Destroyed while thread is still running'."""
        from unittest import mock
        from reader.thumbnails import ThumbPanel

        class StuckWorker:
            def __init__(self):
                self.stopped = False
                self.waits = []
                self.finished = mock.Mock()

            def isRunning(self):
                return True

            def stop(self):
                self.stopped = True

            def wait(self, ms):
                self.waits.append(ms)
                return False          # still busy: the wait times out

        panel = ThumbPanel()
        try:
            worker = StuckWorker()
            panel._worker = worker
            panel.clear_document()
            self.assertTrue(worker.stopped, "a running worker must be asked to stop")
            self.assertIsNotNone(panel._worker,
                                 "a still-running worker must stay referenced")
            self.assertIn(worker, panel._retained_workers())
        finally:
            panel._worker = None
            panel.clear_document()
            panel.close()

    def test_a_finished_thumbnail_worker_is_released(self):
        from reader.thumbnails import ThumbPanel

        class DoneWorker:
            def isRunning(self):
                return False

            def stop(self):
                pass

            def wait(self, ms):
                return True

        panel = ThumbPanel()
        try:
            panel._worker = DoneWorker()
            panel.clear_document()
            self.assertIsNone(panel._worker)
        finally:
            panel._worker = None
            panel.clear_document()
            panel.close()

    def test_a_failing_page_is_not_re_rendered_forever(self):
        """A page whose render raises used to be retried on every scroll tick:
        the exception was swallowed and the page never entered `_rendered`."""
        from reader.continuous import ContinuousReader

        calls = []

        def always_fails(page_index, zoom):
            calls.append(page_index)
            raise RuntimeError("corrupt page")

        reader = ContinuousReader(always_fails)
        try:
            reader.resize(400, 600)
            reader.show()
            reader.set_document([(595.0, 842.0)] * 3, render=True)
            self._wait_for_reader_rendering(reader)
            first = len(calls)
            self.assertGreater(first, 0, "nothing was rendered")
            for _ in range(8):
                # this is what BookView.refresh_visible() calls after a scroll
                reader._ensure_visible()
                self.app.processEvents()
            self.assertEqual(len(calls), first,
                             "a failing page was re-rendered %d times"
                             % (len(calls) - first))
        finally:
            reader.close()

    def test_bookview_extract_maps_rotated_page_selection(self):
        import math
        import tempfile
        import pymupdf
        from PIL import Image, ImageChops
        from PySide6.QtCore import QPoint, QRect
        from reader.bookview import BookView
        from reader.pdf_utils import qimage_to_pil

        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                path = os.path.join(
                    tempfile.gettempdir(),
                    f"rotated_selection_{rotation}.pdf",
                )
                doc = pymupdf.open()
                page = doc.new_page(width=300, height=400)
                page.insert_text((40, 70), "FIRST TARGET", fontsize=14)
                page.insert_text((40, 120), "SECOND OTHER", fontsize=14)
                page.set_rotation(rotation)
                words = [
                    word for word in page.get_text("words")
                    if word[4] in ("FIRST", "TARGET")
                ]
                selection = pymupdf.Rect(
                    min(word[0] for word in words),
                    min(word[1] for word in words),
                    max(word[2] for word in words),
                    max(word[3] for word in words),
                ) * page.rotation_matrix
                doc.save(path)
                doc.close()

                rect = QRect(
                    QPoint(
                        math.floor(selection.x0),
                        math.floor(selection.y0),
                    ),
                    QPoint(
                        math.ceil(selection.x1),
                        math.ceil(selection.y1),
                    ),
                )
                book = BookView()
                try:
                    book.open(path)
                    kind, text = book.extract(0, rect)
                    self.assertEqual(kind, "text")
                    self.assertEqual(text.split(), ["FIRST", "TARGET"])

                    crop = qimage_to_pil(
                        book.render_crop(0, rect, dpi=72)
                    ).convert("RGB")
                    self.assertAlmostEqual(
                        crop.width, rect.width(), delta=2,
                        msg="crop width must follow the displayed page rotation",
                    )
                    self.assertAlmostEqual(
                        crop.height, rect.height(), delta=2,
                        msg="crop height must follow the displayed page rotation",
                    )
                    difference = ImageChops.difference(
                        crop, Image.new("RGB", crop.size, "white")
                    )
                    self.assertIsNotNone(
                        difference.getbbox(),
                        "OCR crop did not include selected text",
                    )

                    book.last_selection = (0, rect)
                    state = book.session_state()["last_selection"]
                    self.assertEqual(state[0:2], ["pdf", 0])
                    restored = book._selection_from_state(state)
                    self.assertEqual(restored, (0, rect))
                finally:
                    book.close_doc()
                    book.close()
                    os.remove(path)

    def test_zoom_retries_a_page_that_failed_to_render(self):
        """Zooming re-renders every page, so a page that failed once should get
        a fresh attempt at the new scale instead of staying skipped forever."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage
        from reader.continuous import ContinuousReader

        broken = {0}

        def render(page_index, zoom):
            if page_index in broken:
                raise RuntimeError("corrupt at this scale")
            img = QImage(60, 80, QImage.Format.Format_RGB888)
            img.fill(Qt.GlobalColor.white)
            return img

        reader = ContinuousReader(render)
        try:
            reader.resize(400, 600)
            reader.show()
            reader.set_document([(595.0, 842.0)] * 2, render=True)
            self._wait_for_reader_rendering(reader)
            self.assertIn(0, reader._failed, "page 0 should be marked failed")

            broken.clear()                      # the page is readable again
            reader.set_zoom(reader.zoom * 1.5)
            self._wait_for_reader_rendering(reader)
            for _ in range(6):
                reader._ensure_visible()
                self.app.processEvents()
            self.assertNotIn(0, reader._failed,
                             "zoom must clear the failure set so the page retries")
        finally:
            reader.close()

    def test_read_aloud_is_disabled_without_edge_tts(self):
        """Read-aloud requires Edge TTS and does not switch providers."""
        from unittest import mock
        from popup import speech_worker
        from popup.translate_popup import TranslatePopup

        class Missing:
            Communicate = None

        with mock.patch.object(speech_worker, "edge_tts", Missing()):
            with mock_chain_translate(return_value=("نص", "X")):
                pop = TranslatePopup(dict(DEFAULTS), source_text="hello")
                try:
                    pop.show()
                    self.app.processEvents()
                    if pop._worker:
                        self.assertTrue(pop._worker.wait(20000))
                    for _ in range(10):
                        self.app.processEvents()
                    self.assertFalse(pop.speak_btn.isEnabled())
                finally:
                    pop.close()

    def test_speech_available_requires_edge_tts(self):
        from unittest import mock
        from popup import speech_worker
        with mock.patch.object(speech_worker, "edge_tts",
                               type("M", (), {"Communicate": None})):
            self.assertFalse(speech_worker.speech_available())

    def test_translation_click_refuses_when_no_speech_engine_is_available(self):
        """Defence in depth: clicks do not start a worker without an engine."""
        from unittest import mock
        from popup.translate_popup import TranslatePopup

        with mock.patch("popup.translate_popup.speech_available",
                        return_value=False):
            with mock_chain_translate(return_value=("نص", "X")):
                pop = TranslatePopup(dict(DEFAULTS), source_text="hello")
                try:
                    pop.show()
                    self.app.processEvents()
                    if pop._worker:
                        pop._worker.wait(20000)
                    for _ in range(10):
                        self.app.processEvents()
                    pop.speak_btn.setEnabled(True)
                    pop._on_translation_position_clicked(0)
                    self.app.processEvents()
                    self.assertIsNone(pop._speech_worker,
                                      "no speech thread may start without edge-tts")
                    self.assertIn("edge-tts", pop.spin.text())
                    self.assertFalse(pop.speak_btn.isEnabled())
                finally:
                    pop.close()

    def test_gemini_key_is_sent_in_a_header_not_the_url(self):
        """A key in the query string ends up in proxy logs and history."""
        import requests
        from unittest import mock
        from translators import gemini
        resp = mock.Mock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": "x"}]},
                                                 "finishReason": "STOP"}]}
        secret = "test-key-not-a-real-credential"
        with mock.patch.object(requests, "post", return_value=resp) as p:
            gemini.translate("hi", secret, "gemini-2.5-flash")
            url = p.call_args[0][0]
            self.assertNotIn(secret, url, "key leaked into the URL")
            self.assertEqual(p.call_args[1]["headers"]["x-goog-api-key"], secret)
            self.assertNotIn("key", (p.call_args[1].get("params") or {}))

    def test_gemini_output_budget_scales_for_page_chunks(self):
        import requests
        from unittest import mock
        from translators import gemini
        text = "source " * 600
        resp = mock.Mock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"candidates": [{
            "content": {"parts": [{"text": "translation"}]},
            "finishReason": "STOP",
        }]}
        with mock.patch.object(
                requests, "post", return_value=resp) as post:
            gemini.translate(text, "k", "gemini-3.5-flash-lite")
        self.assertGreaterEqual(
            post.call_args.kwargs["json"]["generationConfig"]["maxOutputTokens"],
            len(text) * 2,
        )

    def test_gemini_model_list_uses_the_header_too(self):
        import requests
        from unittest import mock
        from translators import gemini
        with mock.patch.object(requests, "get") as g:
            g.return_value.json.return_value = {"models": []}
            g.return_value.raise_for_status.return_value = None
            try:
                gemini.known_models(
                    refresh=True, api_key="test-key-not-a-real-credential")
            finally:
                gemini.known_models(refresh=True)   # restore the module cache
        url = g.call_args[0][0]
        self.assertNotIn("test-key-not-a-real-credential", url)
        self.assertEqual(g.call_args[1]["headers"]["x-goog-api-key"],
                         "test-key-not-a-real-credential")

    def test_thumbnail_wheel_responds_to_touchpad_deltas(self):
        """A touchpad emits deltas far below 120. int(delta/120) truncated them
        to 0, which made both Ctrl+wheel and scrolling silently do nothing."""
        import tempfile
        import time
        import pymupdf
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from reader.thumbnails import ThumbPanel

        tmp = os.path.join(tempfile.gettempdir(), "thumb_touchpad_delta_test.pdf")
        doc = pymupdf.open()
        for _ in range(12):
            doc.new_page()
        doc.save(tmp)
        doc.close()
        panel = ThumbPanel()
        try:
            panel.resize(190, 450)
            panel.show()
            panel.set_document(tmp, 12)
            self.app.processEvents()
            scrollbar = panel.list.verticalScrollBar()
            self.assertGreater(scrollbar.maximum(), 0)

            scrollbar.setValue(300)
            small = QWheelEvent(
                QPointF(10, 10), QPointF(100, 100), QPoint(0, 0), QPoint(0, 24),
                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase, False)
            self.app.sendEvent(panel.list.viewport(), small)
            animation = panel._scroll_animations[panel.list]
            deadline = time.time() + 2
            while animation.state() and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertLess(scrollbar.value(), 300,
                            "small positive touchpad delta should scroll upward")

            zooms = []
            panel.zoomRequested.connect(zooms.append)
            tiny = QWheelEvent(
                QPointF(10, 10), QPointF(100, 100), QPoint(0, 0), QPoint(0, 8),
                Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                Qt.ScrollPhase.NoScrollPhase, False)
            self.app.sendEvent(panel.list.viewport(), tiny)
            self.assertEqual(zooms, [1], "small Ctrl+wheel must still zoom")
            tiny_down = QWheelEvent(
                QPointF(10, 10), QPointF(100, 100), QPoint(0, 0), QPoint(0, -8),
                Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                Qt.ScrollPhase.NoScrollPhase, False)
            self.app.sendEvent(panel.list.viewport(), tiny_down)
            self.assertEqual(zooms, [1, -1])
        finally:
            panel.clear_document()
            panel.close()

    def test_ctrl_wheel_zooms_thumbnails_without_resizing_panel(self):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from reader.bookview import BookView

        path = _make_pdf_file("thumbnail_resize_test.pdf", pages=4)
        book = BookView()
        try:
            book.resize(900, 600)
            book.show()
            book.open(path)
            self.app.processEvents()
            book.splitter.setSizes([170, 722])
            self.app.processEvents()
            pane_width_before = book.splitter.sizes()[0]
            icon_size_before = book.thumbs.list.iconSize()
            thumbnail_width_before = book.thumbs.list.item(0).icon().actualSize(
                icon_size_before).width()
            item_gap_before = (
                book.thumbs.list.gridSize().width() - icon_size_before.width())

            event = QWheelEvent(
                QPointF(10, 10), QPointF(100, 100), QPoint(0, 0), QPoint(0, 120),
                Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                Qt.ScrollPhase.NoScrollPhase, False)
            self.app.sendEvent(book.thumbs.list.viewport(), event)
            self.assertGreater(book.thumbs.list.iconSize().width(), icon_size_before.width())
            self.assertGreater(
                book.thumbs.list.item(0).icon().actualSize(
                    book.thumbs.list.iconSize()).width(),
                thumbnail_width_before)
            self.assertEqual(
                book.thumbs.list.gridSize().width()
                - book.thumbs.list.iconSize().width(),
                item_gap_before)
            self.assertEqual(book.splitter.sizes()[0], pane_width_before)
            for _ in range(3):
                book.thumbs_zoom(1)
                grid = book.thumbs.list.gridSize()
                icon = book.thumbs.list.iconSize()
                self.assertEqual(grid.width() - icon.width(), 2 * book.thumbs.ITEM_GAP)
                self.assertEqual(
                    grid.height() - icon.height()
                    - book.thumbs.list.fontMetrics().height(),
                    book.thumbs.ITEM_GAP)

            book.splitter.setSizes([500, 392])
            self.app.processEvents()
            first = book.thumbs.list.visualItemRect(book.thumbs.list.item(0))
            second = book.thumbs.list.visualItemRect(book.thumbs.list.item(1))
            self.assertGreater(first.width(), 0)
            self.assertEqual(first.y(), second.y())
            self.assertGreater(second.x(), first.x())
        finally:
            book.close_doc()
            book.close()

    def test_thumbnail_columns_follow_splitter_width(self):
        from reader.bookview import BookView

        book = BookView()
        try:
            book.resize(1000, 650)
            book.show()
            book.open(_make_pdf_file("thumbnail_columns_test.pdf", pages=12))
            book.set_thumbs_visible(True)
            self.app.processEvents()

            def first_row_count():
                view = book.thumbs.list
                first_y = view.visualItemRect(view.item(0)).y()
                return sum(
                    view.visualItemRect(view.item(i)).y() == first_y
                    for i in range(view.count())
                )

            for width, expected_columns in ((170, 1), (300, 2), (500, 3)):
                book.splitter.setSizes([width, 1000 - width])
                self.app.processEvents()
                self.assertEqual(book.splitter.sizes()[0], width)
                self.assertEqual(first_row_count(), expected_columns)
        finally:
            book.close_doc()
            book.close()

    def test_ctrl_wheel_filter_ignores_non_widget_targets(self):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtGui import QWindow
        from main import _CtrlWheelThumbsFilter
        from reader.bookview import BookView
        from unittest.mock import Mock

        window = Mock()
        window.current_book.return_value = BookView()
        wheel_filter = _CtrlWheelThumbsFilter(self.app.activeWindow())
        wheel_filter._window = window
        event = QWheelEvent(
            QPointF(10, 10), QPointF(10000, 10000), QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase, False)
        try:
            self.assertFalse(wheel_filter.eventFilter(QWindow(), event))
        finally:
            window.current_book.return_value.close()

    def test_ctrl_wheel_over_pdf_zooms_page(self):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from reader.bookview import BookView

        book = BookView()
        try:
            book.resize(900, 600)
            book.show()
            book.open(_make_pdf_file("pdf_zoom_wheel_test.pdf", pages=3))
            self.app.processEvents()
            zoom_before = book.zoom_percent
            event = QWheelEvent(
                QPointF(10, 10), QPointF(100, 100), QPoint(0, 0), QPoint(0, 120),
                Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                Qt.ScrollPhase.NoScrollPhase, False)
            self.app.sendEvent(book.reader.viewport(), event)
            self.assertGreater(book.zoom_percent, zoom_before)
        finally:
            book.close_doc()
            book.close()

    def test_settings_dialog_model_combo(self):
        from unittest import mock
        from main import SettingsDialog
        from config.settings import DEFAULTS
        from translators import opencode_cli, registry
        s = dict(DEFAULTS)
        s["model"] = "space-bunny-free"
        dlg = SettingsDialog(s)
        try:
            self.assertTrue(dlg.z_model.isEditable())
            self.assertEqual(dlg._current_model(), "space-bunny-free")
            texts = [dlg.z_model.itemText(i) for i in range(dlg.z_model.count())]
            models = {
                dlg.z_model.itemData(i): dlg.z_model.itemText(i)
                for i in range(dlg.z_model.count())
                if isinstance(dlg.z_model.itemData(i), str)
            }

            # no dead ids anywhere in the picker
            self.assertNotIn("gemini-2.0-flash", texts)
            self.assertNotIn("gemini-1.5-flash", texts)
            self.assertNotIn("exo-free", texts)
            self.assertNotIn("deepseek-v4-flash-free", texts)
            self.assertNotIn("grok-4.5", texts)       # no credits on the account
            # Model names remain clean; image status has its own visible badge.
            self.assertEqual(
                models["gemini-3.5-flash-lite"], "gemini-3.5-flash-lite")
            self.assertEqual(models["space-bunny-free"], "space-bunny-free")
            self.assertEqual(dlg.model_image_badge.text(), "IMAGE INPUT")

            # headers are non-selectable; picker offers Gemini + bridge only
            hdr = dlg.z_model.findText(registry.BRIDGE_HDR)
            self.assertGreaterEqual(hdr, 0)
            self.assertFalse(dlg.z_model.model().item(hdr).isEnabled())
            self.assertEqual(dlg.z_model.findText(registry.ZEN_FREE_HDR), -1)
            self.assertEqual(dlg.z_model.findText(registry.CLI_HDR), -1)
            self.assertEqual(dlg.z_model.findText(registry.CUSTOM_HDR), -1)

            # every listed row stays selectable even before its key is set;
            # the engine_hint alone reports why it can't be used yet.
            for txt in ["gemini-3.5-flash-lite", "space-bunny-free"]:
                ix = dlg.z_model.findData(txt)
                self.assertTrue(dlg.z_model.model().item(ix).isEnabled(), txt)
            ok, reason = registry.availability("gemini-3.5-flash-lite", {})
            self.assertFalse(ok)
            self.assertIn("Gemini key", reason)

            with mock.patch.object(
                opencode_cli,
                "list_models",
                return_value=["opencode/space-bunny-free", "opencode/exo-free"],
            ) as cli_models:
                with mock.patch(
                    "translators.verify.verify_cli_models",
                    return_value={"opencode-cli:opencode/exo-free": "403 blocked"},
                ) as verify:
                    dlg.z_refresh.click()
                    cli_models.assert_called_once()
                    verify.assert_called_once()
            texts = [dlg.z_model.itemText(i) for i in range(dlg.z_model.count())]
            models = {
                dlg.z_model.itemData(i): dlg.z_model.itemText(i)
                for i in range(dlg.z_model.count())
                if isinstance(dlg.z_model.itemData(i), str)
            }
            # CLI models are no longer offered in the picker (engines kept).
            self.assertNotIn("⌘ opencode/space-bunny-free", texts)
            self.assertNotIn("⌘ opencode/exo-free", texts)
            self.assertIn("gemini-3.5-flash-lite", models)   # gemini group kept
            for m in ["mimo-v2.6-flash-free", "muse-spark-1.3-contributor-free"]:
                self.assertIn(m, models)   # bridge group kept

            self.assertEqual(dlg.values()["model"], "space-bunny-free")
            dlg._select_model("opencode-cli:opencode/space-bunny-free")
            self.assertEqual(
                dlg.values()["model"],
                "opencode-cli:opencode/space-bunny-free",
            )
            self.assertEqual(dlg.test_btn.text(), "Test Selected Model")
        finally:
            dlg.close()

    def test_explain_control_is_not_in_the_main_toolbar(self):
        from main import MainWindow

        win = MainWindow()
        try:
            self.assertFalse(hasattr(win, "explain_mode_button"))
        finally:
            win.close()

    def test_settings_dialog_model_combo_enables_when_key_typed(self):
        from main import SettingsDialog
        from config.settings import DEFAULTS
        dlg = SettingsDialog(dict(DEFAULTS))
        try:
            dlg.gemini_key.setText("fake-key-for-test")
            self.assertEqual(dlg.gemini_state.text(), "\u25cf")
            # the default model is Gemini, so a real key makes it ready at once
            self.assertIn("Ready", dlg.engine_hint.text())
        finally:
            dlg.close()

    def test_settings_dialog_reports_missing_opencode_cli(self):
        from unittest import mock
        from main import SettingsDialog
        from config.settings import DEFAULTS
        from translators import opencode_cli

        dlg = SettingsDialog(dict(DEFAULTS))
        try:
            with mock.patch.object(
                opencode_cli,
                "list_models",
                side_effect=opencode_cli.OpenCodeCLIError(
                    "OpenCode CLI was not found"
                ),
            ):
                dlg._refresh_zen_models()
            self.assertIn("OpenCode CLI unavailable", dlg.engine_hint.text())
            self.assertIn("was not found", dlg.engine_hint.text())
        finally:
            dlg.close()

    def test_settings_dialog_has_no_second_model_field(self):
        """One model, one source of truth: no separate gemini/opencode model box."""
        from main import SettingsDialog
        from config.settings import DEFAULTS
        dlg = SettingsDialog(dict(DEFAULTS))
        try:
            self.assertFalse(hasattr(dlg, "gemini_model"))
            self.assertFalse(hasattr(dlg, "o_model_gemini"))
            values = dlg.values()
            self.assertNotIn("gemini_model", values)
            self.assertNotIn("opencode_model", values)
            self.assertEqual(values["model"], "gemini-3.5-flash-lite")
            # values() is a complete settings dict, so save() cannot blank keys
            from config.settings import DEFAULTS as D
            self.assertEqual(set(values), set(D))
        finally:
            dlg.close()

    def test_settings_shortcuts_tab_explains_translation_controls(self):
        from PySide6.QtWidgets import QLabel, QTabWidget
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(dict(DEFAULTS))
        try:
            tabs = dialog.findChild(QTabWidget)
            self.assertIsNotNone(tabs)
            self.assertIn(
                "Shortcuts",
                [tabs.tabText(index) for index in range(tabs.count())],
            )
            zoom = dialog.findChild(
                QLabel, "shortcutCtrlWheelDescription")
            double_click = dialog.findChild(
                QLabel, "shortcutTitlebarDoubleClickDescription")
            self.assertIsNotNone(zoom)
            self.assertIn("Ctrl", zoom.text())
            self.assertIn("font size", zoom.text())
            self.assertIsNotNone(double_click)
            self.assertIn("vertically", double_click.text())
            self.assertIn("restore", double_click.text())
        finally:
            dialog.close()

    def test_settings_dialog_roundtrips_every_key(self):
        from main import SettingsDialog
        from config.settings import DEFAULTS
        s = dict(DEFAULTS)
        s.update({"gemini_key": "gk", "model": "space-bunny-free",
              "openai_base": "https://x/api/v1", "openai_key": "ok",
                  "openai_model": "vendor/model", "ocr_langs": "de,en",
                  "email": "a@b.c", "font_size": 18,
                  "translation_font_arabic": "Georgia",
                  "translation_font_latin": "Times New Roman",
                  "model_instructions": "Preserve technical terms."})
        dlg = SettingsDialog(s)
        try:
            self.assertEqual(
                dlg.arabic_font.currentData(), "Georgia",
                repr([
                    (dlg.arabic_font.itemText(i), dlg.arabic_font.itemData(i))
                    for i in range(dlg.arabic_font.count())
                ]),
            )
            self.assertEqual(dlg.latin_font.currentData(), "Times New Roman")
            # The dialog records its own geometry, so `settings_size` and
            # `settings_pos` come back as Qt reported them rather than as the
            # `[]` this dict starts with. Everything the user actually typed
            # must round-trip exactly.
            round_tripped = dlg.values()
            for geometry_key in ("settings_size", "settings_pos"):
                self.assertEqual(round_tripped.pop(geometry_key),
                                 dlg.values()[geometry_key])
                s.pop(geometry_key, None)
            self.assertEqual(round_tripped, s)
            self.assertEqual(round_tripped["translation_font"], "")
            self.assertGreaterEqual(dlg.arabic_font.findData("Georgia"), 0)
            self.assertGreaterEqual(
                dlg.latin_font.findData("Times New Roman"), 0
            )
            # The instructions field must be painted with the active theme's
            # surface/text colours, not with a hardcoded pair that would be
            # unreadable on the light themes.
            from ui.theme import THEMES
            theme = THEMES["Dark Slate"]
            style = dlg.model_instructions.styleSheet()
            self.assertIn(f"background-color: {theme['surface']}", style)
            self.assertIn(f"color: {theme['text']}", style)
        finally:
            dlg.close()

    def test_selection_reading_mode_is_a_saved_settings_choice(self):
        from main import SettingsDialog
        from config.settings import DEFAULTS

        dialog = SettingsDialog(dict(DEFAULTS, selection_mode="image"))
        try:
            self.assertEqual(
                [dialog.selection_mode.itemData(i)
                 for i in range(dialog.selection_mode.count())],
                ["ocr", "image"],
            )
            self.assertEqual(dialog.values()["selection_mode"], "image")
            dialog.selection_mode.setCurrentIndex(
                dialog.selection_mode.findData("ocr"))
            self.assertEqual(dialog.values()["selection_mode"], "ocr")
        finally:
            dialog.close()

    def test_chat_reply_language_setting_and_force_checkbox_are_saved(self):
        from main import SettingsDialog
        from config.settings import DEFAULTS

        dialog = SettingsDialog(dict(
            DEFAULTS,
            chat_reply_language="fr",
            chat_reply_language_only=True,
        ))
        try:
            self.assertEqual(
                dialog.chat_reply_language.currentData(), "fr")
            self.assertTrue(dialog.chat_reply_language_only.isChecked())
            self.assertTrue(dialog.chat_reply_language.isEnabled())
            values = dialog.values()
            self.assertEqual(values["chat_reply_language"], "fr")
            self.assertTrue(values["chat_reply_language_only"])

            dialog.chat_reply_language.setCurrentIndex(
                dialog.chat_reply_language.findData("de"))
            dialog.chat_reply_language_only.setChecked(False)
            values = dialog.values()
            self.assertEqual(values["chat_reply_language"], "de")
            self.assertFalse(values["chat_reply_language_only"])
            self.assertFalse(dialog.chat_reply_language.isEnabled())
        finally:
            dialog.close()

    def test_translation_popup_uses_and_refreshes_selected_font(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        settings = dict(DEFAULTS)
        settings["translation_font_arabic"] = "Georgia"
        settings["translation_font_latin"] = "Times New Roman"
        settings["font_size"] = 18
        with mock_chain_translate(return_value=("translated", "T")):
            popup = TranslatePopup(settings, source_text="hello")
        try:
            self.assertEqual(popup.result.font().family(), "Times New Roman")
            self.assertEqual(popup.result.font().pointSize(), 18)

            popup._on_done("**مرحبا translated**", "T")
            self.app.processEvents()
            self.assertEqual(popup.result.toPlainText(), "مرحبا translated")
            self.assertEqual(
                popup.result.document().find("مرحبا").charFormat().fontFamilies()[0],
                "Georgia",
            )
            self.assertEqual(
                popup.result.document().find("translated").charFormat().fontFamilies()[0],
                "Times New Roman",
            )
            self.assertEqual(
                popup.result.document().defaultFont().family(),
                "Times New Roman",
            )
            self.assertIn(
                'font-family: "Times New Roman"', popup.result.styleSheet())

            updated = dict(settings)
            updated["translation_font_arabic"] = "Times New Roman"
            updated["translation_font_latin"] = "Georgia"
            updated["font_size"] = 20
            popup.refresh_display(updated)
            self.assertEqual(popup.result.font().family(), "Georgia")
            self.assertEqual(popup.result.font().pointSize(), 20)
            self.assertEqual(
                popup.result.document().find("مرحبا").charFormat().fontFamilies()[0],
                "Times New Roman",
            )
            self.assertEqual(
                popup.result.document().find("translated").charFormat().fontFamilies()[0],
                "Georgia",
            )
            self.assertIn('font-family: "Georgia"', popup.result.styleSheet())
        finally:
            try:
                if popup._worker and not popup._worker.wait(20000):
                    popup._worker.terminate()
                    popup._worker.wait(2000)
            except RuntimeError:
                pass
            popup.close()

    def test_translation_popup_refreshes_all_speech_settings(self):
        from popup.translate_popup import TranslatePopup

        settings = dict(DEFAULTS)
        with mock_chain_translate(return_value=("translated", "T")):
            popup = TranslatePopup(settings, source_text="hello")
        try:
            self.assertIn(
                "QMenu::item:selected",
                popup.styleSheet())
            self.assertIn(
                "background-color: #fff2a8; color: #222831",
                popup.styleSheet())
            self.assertIn(
                "background-color: #fff2a8; color: #222831",
                popup.speed_menu.styleSheet())
            self.assertIn(
                "color: #222831",
                popup.page_number_label.styleSheet())

            popup._on_done("translated", "T")
            updated = dict(settings)
            updated.update({
                "speech_engine": "gemini",
                "gemini_key": "configured-key",
                "gemini_tts_voice": "Zephyr",
                "speech_rate": 1.25,
            })
            popup.refresh_display(updated)
            self.assertEqual(popup._speech_engine, "gemini")
            self.assertEqual(popup._speech_rate, 1.25)
            self.assertTrue(popup._speech_available)
            self.assertTrue(popup.speak_btn.isEnabled())
            self.assertTrue(popup.speed_btn.isEnabled())
            self.assertFalse(popup.voice_btn.isEnabled())
            self.assertEqual(popup._speed_slider.value(), 800)
            self.assertIn("Zephyr", popup.voice_btn.toolTip())
            self.assertEqual(
                popup._player.playbackRate(),
                1.25,
            )
        finally:
            try:
                if popup._worker and not popup._worker.wait(20000):
                    popup._worker.terminate()
                    popup._worker.wait(2000)
            except RuntimeError:
                pass
            popup.close()

    def test_translation_popup_skips_rerender_for_theme_only_refresh(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup

        settings = dict(DEFAULTS, theme="Dark Slate", color_scheme="dark")
        popup = TranslatePopup(settings)
        try:
            popup._translation_text = "translated"
            popup._render_translation()
            popup.result.show()
            original_popup_style = popup.styleSheet()
            original_result_style = popup.result.styleSheet()
            updated = dict(
                settings,
                theme="Everforest",
                color_scheme="light",
                pdf_canvas_theme_background=True,
            )
            with mock.patch.object(
                popup, "_render_translation", wraps=popup._render_translation
            ) as render:
                popup.refresh_display(updated)

            render.assert_not_called()
            self.assertIs(popup.settings, updated)
            self.assertEqual(popup.result.toPlainText(), "translated")
            self.assertEqual(popup.styleSheet(), original_popup_style)
            self.assertEqual(popup.result.styleSheet(), original_result_style)
            self.assertIn("#f7f8fa", popup.styleSheet())
            self.assertIn("#f1f3f6", popup.result.styleSheet())
            self.assertIn("#222831", popup.result.styleSheet())
            updated_font = dict(
                updated, font_size=float(settings.get("font_size", 17)) + 1)
            with mock.patch.object(
                popup, "_render_translation", wraps=popup._render_translation
            ) as render:
                popup.refresh_display(updated_font)

            render.assert_called_once()
            self.assertEqual(
                popup.result.font().pointSizeF(),
                updated_font["font_size"],
            )
            popup._translation_needs_render = True
            with mock.patch.object(
                popup, "_render_translation", wraps=popup._render_translation
            ) as render:
                popup.refresh_display(updated_font)

            render.assert_called_once()
        finally:
            try:
                if popup._worker and not popup._worker.wait(20000):
                    popup._worker.terminate()
                    popup._worker.wait(2000)
            except RuntimeError:
                pass
            popup.close()

    def test_streamed_translation_uses_the_markdown_body_font_size(self):
        from unittest import mock
        from PySide6.QtWidgets import QApplication, QWidget
        from popup.translate_popup import TranslatePopup
        from PySide6.QtGui import QTextCursor

        from main import app_stylesheet

        app = QApplication.instance()
        self.assertIsNotNone(app)
        old_stylesheet = app.styleSheet()
        app.setStyleSheet(app_stylesheet("Dark Slate", "dark"))
        settings = dict(DEFAULTS, font_size=28)
        parent = QWidget()
        with mock.patch.object(TranslatePopup, "_start_translation_worker"):
            popup = TranslatePopup(settings, parent=parent)
        try:
            streamed_text = (
                "### Streamed heading\n\n"
                "Body sentinel with the same text size"
            )
            popup._append_streamed_partial(streamed_text[:20])
            popup._append_streamed_partial(streamed_text)
            self.assertIn("font-size: 28pt", popup.result.styleSheet())
            self.assertNotIn("font-size: -1pt", popup.result.styleSheet())

            document = popup.result.document()
            body_start = document.toPlainText().index("Body sentinel")
            body_cursor = QTextCursor(document)
            body_cursor.setPosition(body_start)
            body_cursor.setPosition(
                body_start + len("Body sentinel"),
                QTextCursor.MoveMode.KeepAnchor,
            )
            streamed_size = body_cursor.charFormat().fontPointSize()
            self.assertAlmostEqual(streamed_size, 28)

            updated = dict(settings, font_size=30)
            popup.refresh_display(updated)
            document = popup.result.document()
            body_start = document.toPlainText().index("Body sentinel")
            body_cursor = QTextCursor(document)
            body_cursor.setPosition(body_start)
            body_cursor.setPosition(
                body_start + len("Body sentinel"),
                QTextCursor.MoveMode.KeepAnchor,
            )
            self.assertAlmostEqual(
                body_cursor.charFormat().fontPointSize(), 30)

            popup._translation_text = streamed_text
            popup._render_translation()
            document = popup.result.document()
            body_start = document.toPlainText().index("Body sentinel")
            body_cursor = QTextCursor(document)
            body_cursor.setPosition(body_start)
            body_cursor.setPosition(
                body_start + len("Body sentinel"),
                QTextCursor.MoveMode.KeepAnchor,
            )
            self.assertAlmostEqual(
                body_cursor.charFormat().font().pointSizeF(), 30)
            self.assertAlmostEqual(document.defaultFont().pointSizeF(), 30)
        finally:
            popup.close()
            parent.close()
            app.setStyleSheet(old_stylesheet)

    def _badge_after(self, badge):
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        with mock_chain_translate(return_value=("\u0646\u0635", badge)):
            pop = TranslatePopup(dict(DEFAULTS), source_text="hi")
            try:
                pop.show()
                self.app.processEvents()
                if pop._worker:
                    self.assertTrue(pop._worker.wait(20000))
                for _ in range(10):
                    self.app.processEvents()
                return pop.badge.text(), pop.badge.toolTip()
            finally:
                try:
                    pop.close()
                except RuntimeError:
                    pass


    def test_popup_speech_uses_one_continuous_audio_stream(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup

        with mock_chain_translate(return_value=("hello", "X")):
            popup = TranslatePopup(
                dict(DEFAULTS, speech_read_latin_words=True),
                source_text="hello",
            )
        popup.show()
        self.app.processEvents()
        if popup._worker:
            self.assertTrue(popup._worker.wait(20000))
        for _ in range(10):
            self.app.processEvents()
        worker = mock.Mock()
        worker.isRunning.return_value = False
        popup.result.setPlainText("hello")
        for signal_name in (
            "segment_stream_ready",
            "word_boundary_ready",
            "word_boundaries",
            "segment_ready",
            "failed",
            "finished",
        ):
            setattr(worker, signal_name, mock.Mock())
        stream_server = mock.Mock()
        try:
            with mock.patch(
                "popup.translate_popup.AudioStreamServer",
                return_value=stream_server,
            ) as create_server, mock.patch(
                "popup.translate_popup.SpeechWorker",
                return_value=worker,
            ) as speech_worker, mock.patch.object(
                popup, "_try_play_cached_full_text", return_value=False,
            ):
                popup._begin_speech(0)

            create_server.assert_called_once_with()
            self.assertTrue(
                speech_worker.call_args.kwargs["continuous_stream"])
            self.assertTrue(
                speech_worker.call_args.kwargs["read_latin_words"])
            self.assertIs(
                speech_worker.call_args.kwargs["stream_server"], stream_server)
            self.assertFalse(speech_worker.call_args.kwargs["stream_audio"])
            worker.segment_stream_ready.connect.assert_called_once_with(
                popup._on_speech_stream_ready)
        finally:
            popup._speech_active = False
            popup._close_speech_stream_server()
            popup.close()



    def test_speech_rate_multiplier_becomes_a_percentage_for_edge_tts(self):
        """edge-tts wants "+15%" / "-50%", not a multiplier. The conversion has
        to be exact: a wrong sign would speed the voice up when asked to slow."""
        from popup.speech_worker import rate_string
        cases = [(1.0, "+0%"), (1.25, "+25%"), (1.5, "+50%"),
                 (2.0, "+100%"), (0.8, "-20%"), (0.5, "-50%"),
                 (0.25, "-75%")]
        for mult, want in cases:
            self.assertEqual(rate_string(mult), want, str(mult))

    def test_speech_speed_normalization_preserves_hundredths_and_soft_snaps(self):
        from popup.speech_worker import (
            normalize_speed,
            snap_speed_to_quarter,
        )

        self.assertEqual(normalize_speed(0.8), 0.8)
        self.assertEqual(normalize_speed(1.25), 1.25)
        self.assertEqual(normalize_speed(2.5), 2.0)
        self.assertEqual(snap_speed_to_quarter(0.77), 0.75)
        self.assertEqual(snap_speed_to_quarter(0.8), 0.8)

    def test_speech_worker_passes_the_requested_rate_to_edge_tts(self):
        from unittest import mock
        from popup import speech_worker

        seen = {}

        class FakeCommunicate:
            def __init__(self, text, voice, rate=None, boundary=None):
                seen["rate"] = rate
                seen["boundary"] = boundary
                seen["voice"] = voice

            async def stream(self):
                if False:
                    yield {}

        fake = type("M", (), {"Communicate": FakeCommunicate})
        cache_tmp = tempfile.mkdtemp(prefix="harmony-tts-test-")
        with mock.patch.dict(os.environ, {"HARMONY_TTS_CACHE": cache_tmp}):
            with mock.patch.object(speech_worker, "edge_tts", fake):
                w = speech_worker.SpeechWorker(
                    "sentence.",
                    None,
                    rate=1.25,
                    voice="en-US-AriaNeural",
                    target_language="en",
                )
                w.run()
        self.assertEqual(seen.get("rate"), "+25%")
        self.assertEqual(seen.get("boundary"), "WordBoundary")
        self.assertEqual(seen.get("voice"), "en-US-AriaNeural")

    def test_speech_cache_replays_without_network(self):
        """Second identical request: no Communicate call, same audio+highlight."""
        from unittest import mock
        from PySide6.QtCore import Qt
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        calls = []

        class FakeCommunicate:
            def __init__(self, text, voice, rate=None, boundary=None):
                calls.append(text)

            async def stream(self):
                yield {"type": "audio", "data": b"fake-mp3"}
                yield {"type": "WordBoundary", "offset": 0,
                       "duration": 1_000_000, "text": "hello"}

        fake = type("M", (), {"Communicate": FakeCommunicate})
        cache_tmp = tempfile.mkdtemp(prefix="harmony-tts-test-")

        def run_once():
            out, bounds = [], []
            with mock.patch.dict(os.environ, {"HARMONY_TTS_CACHE": cache_tmp}):
                with mock.patch.object(speech_worker, "edge_tts", fake):
                    w = SpeechWorker("hello world", target_language="en")
                    w.segment_ready.connect(
                        lambda i, p: out.append((i, p)),
                        Qt.ConnectionType.DirectConnection)
                    w.word_boundaries.connect(
                        lambda i, b: bounds.append((i, b)),
                        Qt.ConnectionType.DirectConnection)
                    w.start()
                    self.assertTrue(w.wait(5000))
            return out, bounds

        first_out, first_bounds = run_once()
        self.assertEqual(len(calls), 1)  # network used once
        second_out, second_bounds = run_once()
        self.assertEqual(len(calls), 1)  # replayed from disk
        self.assertEqual(len(second_out), 1)
        self.assertEqual(second_bounds, first_bounds)  # highlight intact
        with open(second_out[0][1], "rb") as f:
            self.assertEqual(f.read(), b"fake-mp3")

    def test_speech_cache_offsets_word_boundaries_for_each_request(self):
        from unittest import mock
        from PySide6.QtCore import Qt
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        calls = []

        class FakeCommunicate:
            def __init__(self, text, voice, rate=None, boundary=None):
                calls.append(text)

            async def stream(self):
                yield {"type": "audio", "data": b"fake-mp3"}
                yield {
                    "type": "WordBoundary",
                    "offset": 250_000,
                    "duration": 1_000_000,
                    "text": "hello",
                }

        fake = type("M", (), {"Communicate": FakeCommunicate})
        cache_tmp = tempfile.mkdtemp(prefix="harmony-tts-test-")

        def run_once(source_offset):
            bounds = []
            with mock.patch.dict(os.environ, {"HARMONY_TTS_CACHE": cache_tmp}):
                with mock.patch.object(speech_worker, "edge_tts", fake):
                    worker = SpeechWorker(
                        "hello world",
                        source_offset=source_offset,
                        target_language="en",
                    )
                    worker.word_boundaries.connect(
                        lambda index, words: bounds.extend(words),
                        Qt.ConnectionType.DirectConnection,
                    )
                    worker.start()
                    self.assertTrue(worker.wait(5000))
            worker.cleanup_output()
            return bounds

        self.assertEqual(run_once(20), [(250_000, 1_000_000, 20, 25)])
        self.assertEqual(run_once(200), [(250_000, 1_000_000, 200, 205)])
        self.assertEqual(calls, ["hello world"])

    def test_speech_segments_emit_in_index_order(self):
        """Concurrent synthesis must not reorder playback segments."""
        from unittest import mock
        from PySide6.QtCore import Qt
        from popup import speech_worker
        from popup.speech_worker import SpeechWorker

        class FakeCommunicate:
            def __init__(self, text, voice, rate=None, boundary=None):
                self.text = text

            async def stream(self):
                yield {"type": "audio", "data": b"x"}
                for word in self.text.split():
                    yield {"type": "WordBoundary", "offset": 0,
                           "duration": 1, "text": word}

        fake = type("M", (), {"Communicate": FakeCommunicate})
        cache_tmp = tempfile.mkdtemp(prefix="harmony-tts-test-")
        emitted = []
        text = "one two three. four five six. seven eight nine."
        with mock.patch.dict(os.environ, {"HARMONY_TTS_CACHE": cache_tmp}):
            with mock.patch.object(speech_worker, "edge_tts", fake):
                w = SpeechWorker(text, target_language="en")
                w.segment_ready.connect(
                    lambda i, p: emitted.append(i),
                    Qt.ConnectionType.DirectConnection)
                w.start()
                self.assertTrue(w.wait(10000))
        self.assertEqual(emitted, sorted(emitted))
        self.assertEqual(len(emitted), len(speech_worker.split_speech_text(text)))

    def test_voice_menu_groups_choices_and_saves_selected_voice(self):
        from unittest import mock
        from PySide6.QtCore import Qt
        from popup.translate_popup import TranslatePopup

        voices = [
            {
                "ShortName": "ar-DZ-AminaNeural",
                "Locale": "ar-DZ",
                "FriendlyName": "Microsoft Amina Online (Natural) - Arabic (Algeria)",
                "Gender": "Female",
            },
            {
                "ShortName": "ar-EG-SalmaNeural",
                "Locale": "ar-EG",
                "FriendlyName": "Microsoft Salma Online (Natural) - Arabic (Egypt)",
                "Gender": "Female",
            },
            {
                "ShortName": "fr-FR-DeniseNeural",
                "Locale": "fr-FR",
                "FriendlyName": "Microsoft Denise Online (Natural) - French (France)",
                "Gender": "Female",
            },
            {
                "ShortName": "it-IT-ElsaNeural",
                "Locale": "it-IT",
                "FriendlyName": "Microsoft Elsa Online (Natural) - Italian (Italy)",
                "Gender": "Female",
            },
            {
                "ShortName": "de-DE-KatjaNeural",
                "Locale": "de-DE",
                "FriendlyName": "Microsoft Katja Online (Natural) - German (Germany)",
                "Gender": "Female",
            },
            {
                "ShortName": "es-ES-ElviraNeural",
                "Locale": "es-ES",
                "FriendlyName": "Microsoft Elvira Online (Natural) - Spanish (Spain)",
                "Gender": "Female",
            },
            {
                "ShortName": "ru-RU-SvetlanaNeural",
                "Locale": "ru-RU",
                "FriendlyName": "Microsoft Svetlana Online (Natural) - Russian (Russia)",
                "Gender": "Female",
            },
            {
                "ShortName": "en-US-AndrewMultilingualNeural",
                "Locale": "en-US",
                "FriendlyName": "Microsoft AndrewMultilingual Online (Natural) - English (United States)",
                "Gender": "Male",
            },
            {
                "ShortName": "en-US-AvaNeural",
                "Locale": "en-US",
                "FriendlyName": "Microsoft Ava Online (Natural) - English (United States)",
                "Gender": "Female",
            },
            {
                "ShortName": "ja-JP-NanamiNeural",
                "Locale": "ja-JP",
                "FriendlyName": "Microsoft Nanami Online (Natural) - Japanese (Japan)",
                "Gender": "Female",
            },
            {
                "ShortName": "ja-JP-EchoMultilingualNeural",
                "Locale": "ja-JP",
                "FriendlyName": "Microsoft Echo Multilingual - Japanese (Japan)",
                "Gender": "Male",
            },
        ]
        with mock_chain_translate(return_value=("t", "X")):
            pop = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            pop._on_voice_list_ready(voices)
            languages = {
                action.text(): action.menu()
                for action in pop.voice_menu.actions()
                if action.menu() is not None
            }
            self.assertEqual(
                [
                    action.text() for action in pop.voice_menu.actions()
                    if action.menu() is not None
                ],
                ["Fr", "Ar", "It", "En", "De", "Es", "Ru", "Multilingual"],
            )
            self.assertEqual(
                set(languages),
                {"Ar", "Fr", "It", "En", "De", "Es", "Ru", "Multilingual"},
            )
            self.assertEqual(
                pop.voice_menu.layoutDirection(),
                Qt.LayoutDirection.RightToLeft,
            )
            self.assertEqual(
                languages["Ar"].layoutDirection(),
                Qt.LayoutDirection.RightToLeft,
            )
            arabic_countries = {
                action.text(): action.menu()
                for action in languages["Ar"].actions()
                if action.menu() is not None
            }
            self.assertEqual(set(arabic_countries), {"Algeria", "Egypt"})
            voice_actions = [
                action
                for language_menu in languages.values()
                for country_action in language_menu.actions()
                if country_action.menu() is not None
                for action in country_action.menu().actions()
            ]
            selected = next(
                action for action in voice_actions
                if action.data() == "ar-DZ-AminaNeural"
            )
            selected.trigger()

            self.assertEqual(pop._speech_voice, "ar-DZ-AminaNeural")
            self.assertEqual(pop.settings["speech_voice"], "ar-DZ-AminaNeural")
            self.assertEqual(
                [action.data() for action in voice_actions if action.isChecked()],
                ["ar-DZ-AminaNeural"],
            )
            from config.settings import load
            self.assertEqual(load()["speech_voice"], "ar-DZ-AminaNeural")
            menu_voice_ids = {action.data() for action in voice_actions}
            self.assertNotIn("ja-JP-NanamiNeural", menu_voice_ids)
            multilingual_countries = {
                action.text(): action.menu()
                for action in languages["Multilingual"].actions()
                if action.menu() is not None
            }
            self.assertEqual(
                [
                    action.data()
                    for action in multilingual_countries["United States"].actions()
                ],
                ["en-US-AndrewMultilingualNeural"],
            )
            multilingual_voices = {
                action.data()
                for country_action in languages["Multilingual"].actions()
                if country_action.menu() is not None
                for action in country_action.menu().actions()
            }
            self.assertEqual(
                multilingual_voices,
                {
                    "en-US-AndrewMultilingualNeural",
                    "ja-JP-EchoMultilingualNeural",
                },
            )
        finally:
            try:
                if pop._worker and not pop._worker.wait(20000):
                    pop._worker.terminate()
                    pop._worker.wait(2000)
            except RuntimeError:
                pass
            pop.close()

    def test_voice_menu_opens_to_the_left_of_its_button(self):
        from unittest import mock
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        from popup.translate_popup import TranslatePopup

        voices = [{
            "ShortName": "ar-DZ-AminaNeural",
            "Locale": "ar-DZ",
            "FriendlyName": "Microsoft Amina Online (Natural) - Arabic (Algeria)",
            "Gender": "Female",
        }]
        with mock_chain_translate(return_value=("t", "X")):
            pop = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            if pop._worker is not None:
                self.assertTrue(pop._worker.wait(5000))
            self.app.processEvents()
            screen = QApplication.primaryScreen().availableGeometry()
            pop.resize(500, 240)
            pop.move(screen.center().x() - 250, screen.center().y() - 120)
            pop.show()
            self.app.processEvents()
            pop._on_voice_list_ready(voices)
            pop._show_voice_menu()
            self.app.processEvents()

            button_left = pop.voice_btn.mapToGlobal(pop.voice_btn.rect().topLeft()).x()
            self.assertEqual(
                pop.voice_menu.layoutDirection(),
                Qt.LayoutDirection.RightToLeft,
            )
            self.assertLessEqual(pop.voice_menu.geometry().right(), button_left)
            self.assertFalse(pop.voice_btn.menu())
        finally:
            pop.voice_menu.hide()
            pop.close()

    def test_voice_menu_loads_edge_voice_catalog_in_background(self):
        import asyncio
        from unittest import mock
        from popup import speech_worker
        from popup.translate_popup import TranslatePopup

        async def fake_list_voices():
            await asyncio.sleep(0)
            return [{
                "ShortName": "en-US-AriaNeural",
                "Locale": "en-US",
                "FriendlyName": "Aria",
                "Gender": "Female",
            }]

        with mock_chain_translate(return_value=("t", "X")):
            pop = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            with mock.patch.object(
                speech_worker.edge_tts, "list_voices", new=fake_list_voices
            ):
                pop._load_voices()
                worker = pop._voice_list_worker
                self.assertIsNotNone(worker)
                self.assertTrue(worker.wait(5000))
                self.app.processEvents()
            self.assertIsNone(pop._voice_list_worker)
            self.assertEqual(len(pop._voice_actions), 1)
            self.assertEqual(pop._voice_actions[0].data(), "en-US-AriaNeural")
        finally:
            try:
                if pop._worker and not pop._worker.wait(20000):
                    pop._worker.terminate()
                    pop._worker.wait(2000)
            except RuntimeError:
                pass
            pop.close()

    def test_closing_during_voice_catalog_fetch_does_not_block_the_popup(self):
        import asyncio
        from unittest import mock
        from popup import speech_worker
        from popup.translate_popup import TranslatePopup

        async def slow_list_voices():
            await asyncio.sleep(0.05)
            return []

        with mock_chain_translate(return_value=("t", "X")):
            pop = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            if pop._worker is not None:
                self.assertTrue(pop._worker.wait(5000))
            self.app.processEvents()
            pop.show()
            with mock.patch.object(
                speech_worker.edge_tts, "list_voices", new=slow_list_voices
            ):
                pop._load_voices()
                worker = pop._voice_list_worker
                self.assertIsNotNone(worker)
                self.assertFalse(pop.close())
                self.assertFalse(pop.isVisible())
                self.assertTrue(pop._close_pending)
                self.assertTrue(worker.wait(5000))
                self.app.processEvents()
                self.app.processEvents()
            self.assertIsNone(pop._voice_list_worker)
            self.assertFalse(pop._close_pending)
        finally:
            worker = pop._voice_list_worker
            if worker is not None and worker.isRunning():
                worker.wait(5000)
            pop.close()

    def test_speed_menu_sits_next_to_read_aloud_and_does_not_grow_the_header(self):
        """A speed picker must not enlarge the popup: same fixed geometry, and
        the button itself has to stay narrow."""
        from unittest import mock
        from PySide6.QtWidgets import QToolButton
        from popup.translate_popup import TranslatePopup
        with mock_chain_translate(return_value=("t", "X")):
            pop = TranslatePopup(dict(DEFAULTS), source_text="hello")
            try:
                pop.show()
                self.app.processEvents()
                if pop._worker:
                    pop._worker.wait(20000)
                for _ in range(10):
                    self.app.processEvents()

                self.assertIsInstance(pop.speed_btn, QToolButton)
                lay = pop.speed_btn.parentWidget().layout()
                idx = [i for i in range(lay.count())
                       if lay.itemAt(i).widget() is pop.speed_btn]
                self.assertEqual(len(idx), 1)
                self.assertIs(lay.itemAt(idx[0] - 1).widget(), pop.speak_btn)

                self.assertLessEqual(pop.speed_btn.width(), 34)
                self.assertEqual(TranslatePopup.FIXED_OUTER,
                                 (514, 605, 1126, 326))

                self.assertEqual(len(pop.speed_menu.actions()), 1)
                self.assertEqual(pop._speed_slider.minimum(), 0)
                self.assertEqual(pop._speed_slider.maximum(), 1200)
                self.assertEqual(pop._speed_slider.singleStep(), 1)
                self.assertEqual(pop._speed_slider.pageStep(), 200)
                self.assertEqual(pop._speed_slider.tickInterval(), 40)
                self.assertEqual(
                    pop._speed_slider.tickPosition(),
                    pop._speed_slider.TickPosition.TicksBelow,
                )
                self.assertGreaterEqual(
                    pop._speed_slider.minimumWidth(), 300)
                panel = pop.speed_menu.actions()[0].defaultWidget()
                self.assertEqual(panel.width(), 420)
                self.assertEqual(pop._speed_preset_row.width(), 410)
                self.assertEqual(
                    [button.text() for button in pop._speed_presets.values()],
                    ["0", "0.5", "0.75", "1", "1.25", "1.5", "2"],
                )
                self.assertEqual(
                    pop._speed_presets[0.0].toolTip(),
                    "Closest available speed to zero: 0.25x; reading continues",
                )
                self.assertEqual(pop._speed_display.text(), "1.00x")
                self.assertIn(
                    "background: #f7f8fa",
                    panel.styleSheet(),
                )
                self.assertIn(
                    "QLabel#speechSpeedValue { color: #222831",
                    panel.styleSheet(),
                )
                self.assertIn(
                    "QMenu { background: #f7f8fa",
                    pop.speed_menu.styleSheet(),
                )

                pop._show_speed_menu()
                self.app.processEvents()
                from PySide6.QtWidgets import QStyle
                from PySide6.QtWidgets import QStyleOptionSlider
                slider_option = QStyleOptionSlider()
                pop._speed_slider.initStyleOption(slider_option)
                preset_centers = []
                for speed, button in pop._speed_presets.items():
                    position = pop._speed_to_slider_value(speed)
                    slider_option.sliderPosition = position
                    slider_option.sliderValue = position
                    expected_handle = pop._speed_slider.style().subControlRect(
                        QStyle.ComplexControl.CC_Slider,
                        slider_option,
                        QStyle.SubControl.SC_SliderHandle,
                        pop._speed_slider,
                    )
                    actual_center_x = (
                        button.x()
                        + pop._speed_preset_row.x()
                        - pop._speed_slider.x()
                        + button.width() // 2
                    )
                    self.assertEqual(
                        actual_center_x, expected_handle.center().x())
                    preset_centers.append(actual_center_x)
                preset_gaps = [
                    right - left
                    for left, right in zip(
                        preset_centers, preset_centers[1:])
                ]
                self.assertLessEqual(max(preset_gaps) - min(preset_gaps), 1)
                menu_width = pop.speed_menu.frameGeometry().width()
                button_center = pop.speed_btn.mapToGlobal(
                    pop.speed_btn.rect().center()).x()
                expected_x = button_center - menu_width // 2
                screen = pop.speed_btn.screen()
                if screen is not None:
                    available = screen.availableGeometry()
                    expected_x = max(
                        available.left(),
                        min(expected_x, available.right() - menu_width + 1),
                    )
                self.assertEqual(pop.speed_menu.frameGeometry().left(), expected_x)
                pop.speed_menu.hide()

                button_style = pop.speed_btn.styleSheet()
                self.assertIn(
                    "background-color: #f7f8fa",
                    button_style,
                )
                self.assertIn(
                    "QToolButton:hover, QToolButton:pressed { "
                    "background-color: #e9edf2",
                    button_style,
                )
            finally:
                pop.close()

    def test_choosing_a_speed_is_remembered_for_the_next_popup(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        with mock_chain_translate(return_value=("t", "X")):
            pop = TranslatePopup(dict(DEFAULTS), source_text="hello")
            try:
                pop.show()
                self.app.processEvents()
                if pop._worker:
                    pop._worker.wait(20000)
                for _ in range(10):
                    self.app.processEvents()
                pop._on_speed_slider_pressed()
                pop._speed_slider.setValue(
                    pop._speed_to_slider_value(0.77))
                pop._on_speed_slider_released()
                self.assertEqual(pop._speech_rate, 0.75)
                pop._speed_slider.setValue(
                    pop._speed_to_slider_value(0.8))
                pop._commit_speed_controls()
                self.assertEqual(pop._speech_rate, 0.8)
                self.assertEqual(pop.settings["speech_rate"], 0.8)
                self.assertEqual(
                    pop._speed_slider.value(),
                    pop._speed_to_slider_value(0.8),
                )
                self.assertEqual(pop._speed_display.text(), "0.80x")
                pop._speed_presets[1.25].click()
                self.assertEqual(pop._speech_rate, 1.25)
                self.assertEqual(pop.settings["speech_rate"], 1.25)
                pop._speed_presets[0.0].click()
                self.assertEqual(pop._speech_rate, 0.25)
                self.assertEqual(pop._player.playbackRate(), 0.25)
                self.assertEqual(pop._speed_display.text(), "0.25x")
                self.assertTrue(pop._speed_presets[0.0].isChecked())
            finally:
                pop.close()

    def test_speed_change_updates_player_without_restarting_speech(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup

        with mock_chain_translate(return_value=("t", "X")):
            pop = TranslatePopup(dict(DEFAULTS), source_text="hello")
            try:
                pop._speech_active = True
                with mock.patch.object(pop, "_request_speech") as restart:
                    pop._set_speech_rate(1.25)

                restart.assert_not_called()
                self.assertEqual(pop._player.playbackRate(), 1.25)
                self.assertEqual(pop.settings["speech_rate"], 1.25)
            finally:
                pop.close()

    def test_a_saved_speed_is_restored_on_the_next_popup(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        s = dict(DEFAULTS)
        s["speech_rate"] = 0.5
        with mock_chain_translate(return_value=("t", "X")):
            pop = TranslatePopup(s, source_text="hello")
            try:
                pop.show()
                self.app.processEvents()
                if pop._worker:
                    pop._worker.wait(20000)
                for _ in range(10):
                    self.app.processEvents()
                self.assertEqual(pop._speech_rate, 0.5)
                self.assertEqual(
                    pop._speed_slider.value(),
                    pop._speed_to_slider_value(0.5),
                )
                self.assertEqual(pop._speed_display.text(), "0.50x")
            finally:
                pop.close()

    def test_popup_badge_states_the_engine_and_flags_substitutes(self):
        # Chosen model ran: clean line, no warning marker.
        text, tip = self._badge_after("\u2726 chosen: gemini-3.5-flash-lite")
        self.assertNotIn("\u26a0", text)
        self.assertIn("gemini-3.5-flash-lite", text)
        self.assertIn("chosen model translated", tip)

        # Substituted: visible warning marker, full reason on hover.
        text, tip = self._badge_after(
            "\u2726 gemini-2.5-flash  \u26a0 not your model (space-bunny-free failed: 403)")
        self.assertTrue(text.startswith("\u26a0"))
        self.assertIn("gemini-2.5-flash", text)
        self.assertIn("NOT the model you chose", tip)
        self.assertIn("403", tip)          # the reason is not lost

        # A fallback names both the service that answered and the model that
        # failed; the full failure reason remains available on hover.
        text, tip = self._badge_after(
            "Free service  \u26a0 chosen model gemini-3.5-flash-lite failed: no Gemini key")
        self.assertTrue(text.startswith("\u26a0"))
        self.assertIn("Google Translate", text)
        self.assertIn("answered because", tip)
        self.assertIn("gemini-3.5-flash-lite failed", tip)
        self.assertIn("no Gemini key", tip)

        text, tip = self._badge_after(
            "Free service (alt)  \u26a0 chosen model space-bunny-free failed: 403")
        self.assertIn("MyMemory", text)
        self.assertIn("space-bunny-free failed", tip)
        self.assertIn("403", tip)

    def test_popup_badge_shows_engine_for_mixed_chunk_warning(self):
        from translators.chain import _merge_badges

        badge = _merge_badges(
            ["\u2726 chosen: gemini-3.5-flash-lite"] * 6
            + ["Free service  \u26a0 not your model (403)"] * 2,
            "gemini-3.5-flash-lite",
        )
        text, tip = self._badge_after(badge)
        self.assertTrue(text.startswith("\u26a0 Partial fallback"))
        self.assertIn("6/8", text)
        self.assertLess(len(text), 45)
        self.assertIn("translated 6 of 8 chunks", tip)
        self.assertIn("gemini-3.5-flash-lite", tip)

    def test_popup_badge_does_not_hide_missing_engine_name(self):
        text, tip = self._badge_after("\u26a0   (not your model)")
        self.assertTrue(text.startswith("\u26a0 Fallback"))
        self.assertIn("did not translate any chunks", tip)

    def test_popup_badge_shows_failed_model_when_fallback_engine_is_missing(self):
        text, tip = self._badge_after(
            "\u26a0 chosen model gemini-3.5-flash-lite failed: HTTP 429 quota")
        self.assertIn("gemini-3.5-flash-lite failed", text)
        self.assertIn("HTTP 429 quota", tip)

    def test_completed_translation_is_rendered_once(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup

        with mock_chain_translate(return_value=("initial", "\u2726 chosen: test-model")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="source")
            try:
                if popup._worker:
                    self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
                with mock.patch.object(
                        popup, "_render_markdown",
                        wraps=popup._render_markdown) as render:
                    popup._on_done("completed text", "\u2726 chosen: test-model")
                    render.assert_called_once()
            finally:
                popup.close()

    def test_popup_badge_line_stays_short(self):
        """A long failure reason must not overflow the status line."""
        long_reason = "\u2726 gemini-2.5-flash  \u26a0 not your model (" + ("x" * 400) + ")"
        text, tip = self._badge_after(long_reason)
        self.assertLess(len(text), 80)
        self.assertIn("x" * 400, tip)

        long_model = "custom/" + ("model-" * 12)
        text, tip = self._badge_after(
            f"Free service  \u26a0 chosen model {long_model} failed: unavailable")
        self.assertLess(len(text), 80)
        self.assertIn(long_model, tip)

    def test_popup_engine_badge_is_above_translation_and_bottom_is_compact(self):
        from PySide6.QtWidgets import QLabel
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS
        from unittest import mock

        with mock_chain_translate(
                return_value=("\u0646\u0635", "\u2726 chosen: gemini-3.5-flash-lite")):
            pop = TranslatePopup(dict(DEFAULTS), source_text="hello")
            try:
                pop.show()
                self.app.processEvents()
                if pop._worker:
                    self.assertTrue(pop._worker.wait(20000))
                self.app.processEvents()
                header_widget = pop.layout().itemAt(0).widget()
                content_splitter = pop.content_splitter
                self.assertNotEqual(pop.layout().indexOf(header_widget), -1)
                self.assertNotEqual(pop.layout().indexOf(content_splitter), -1)
                self.assertLess(pop.layout().indexOf(header_widget),
                                pop.layout().indexOf(content_splitter),
                                "the badge header must sit above the result area")
                self.assertEqual(header_widget.layout().indexOf(pop.badge), 0)
                self.assertEqual(content_splitter.widget(0), pop.translation_area)
                self.assertEqual(
                    pop.translation_area.layout().indexOf(pop.result), 0)
                self.assertTrue(pop.badge.isVisible())
                self.assertEqual(pop.layout().count(), 2)
                header_layout = header_widget.layout()
                self.assertLess(
                    header_layout.indexOf(pop.copy_btn),
                    header_layout.indexOf(pop.speak_btn),
                )
                self.assertLess(
                    header_layout.indexOf(pop.speak_btn),
                    header_layout.indexOf(pop.speed_btn),
                )
                self.assertLess(
                    header_layout.indexOf(pop.speed_btn),
                    header_layout.indexOf(pop.voice_btn),
                )
                self.assertLess(
                    header_layout.indexOf(pop.voice_btn),
                    header_layout.indexOf(pop.pin_btn),
                )
                self.assertLessEqual(pop.layout().spacing(), 4)
                self.assertIsNone(pop.findChild(QLabel, "src"))
            finally:
                pop.close()

    def test_popup_speech_button_activates_after_translation(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        with mock_chain_translate(
                        return_value=("\u0645\u0631\u062d\u0628\u0627", "\u2726 chosen: test-model")):
            pop = TranslatePopup(dict(DEFAULTS), source_text="hello")
            try:
                pop.speak_btn.setEnabled(False)
                pop._on_done("\u0645\u0631\u062d\u0628\u0627", "\u2726 chosen: test-model")
                self.assertTrue(pop.speak_btn.isEnabled())
                self.assertEqual(pop.result.toPlainText(), "\u0645\u0631\u062d\u0628\u0627")
            finally:
                try:
                    if pop._worker and not pop._worker.wait(20000):
                        pop._worker.terminate()
                        pop._worker.wait(2000)
                except RuntimeError:
                    pass
                pop.close()

    def test_translation_popup_pin_toggles_always_on_top(self):
        from unittest import mock
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        owner = QWidget()
        with mock_chain_translate(
            return_value=("translated", "T"),
        ):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello", parent=owner)
        try:
            flags = popup.windowFlags()
            self.assertTrue(flags & Qt.WindowType.Tool)
            self.assertFalse(flags & Qt.WindowType.WindowStaysOnTopHint)
            self.assertIs(popup.parentWidget(), owner)

            popup.show()
            self.app.processEvents()
            popup.pin_btn.click()
            self.assertTrue(popup._pinned)
            self.assertTrue(
                popup.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            self.assertIs(popup.parentWidget(), owner)
            self.assertTrue(popup.isVisible())

            popup.pin_btn.click()
            self.assertFalse(popup._pinned)
            self.assertFalse(
                popup.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        finally:
            popup.close()
            owner.close()

    def test_popup_renders_markdown_and_keeps_plain_text_for_speech(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        markdown = (
            "# عنوان مهم\n\n**نص عادي**\n\n"
            r"مصطلح (\*\*minor mode\*\*) في الشرح." "\n\n"
            "الرقم 3f و5d في الشرح، والنغمة $5f$ (Harmonic 5f)، "
            "التاسعة $$D_9$$\n\n"
            "```text\n600\nC-dur\nT\nD65\n```\n\nبعد المثال."
        )
        with mock_chain_translate(return_value=(markdown, "T")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
            try:
                popup.show()
                self.app.processEvents()
                self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()

                self.assertEqual(
                    popup.result.toPlainText(),
                    "عنوان مهم\nنص عادي\nمصطلح (minor mode) في الشرح.\n"
                    "الرقم 3f و5d في الشرح، "
                    "والنغمة 5f (Harmonic 5f)، التاسعة D_9"
                    "\n600\nC-dur\nT\nD65\nبعد المثال.",
                )
                cursor = popup.result.document().find("عنوان مهم")
                self.assertFalse(cursor.isNull())
                self.assertEqual(cursor.blockFormat().headingLevel(), 1)
                plain_text = popup.result.document().find("نص عادي")
                self.assertFalse(plain_text.isNull())
                self.assertLessEqual(plain_text.charFormat().fontWeight(), 400)
                escaped_bold = popup.result.document().find("minor mode")
                self.assertFalse(escaped_bold.isNull())
                self.assertLessEqual(escaped_bold.charFormat().fontWeight(), 400)
                self.assertTrue(popup.result.document().find("**").isNull())
                visible_text = popup.result.toPlainText()
                speech_text = popup._current_speech_text()
                self.assertEqual(len(speech_text), len(visible_text))
                self.assertIn("C-dur", visible_text)
                self.assertIn("D65", visible_text)
                self.assertIn("5f", visible_text)
                self.assertIn("D_9", visible_text)
                self.assertNotIn("$", visible_text)
                self.assertNotIn("C-dur", speech_text)
                self.assertNotIn("D65", speech_text)
                notation_start = visible_text.index("5f")
                self.assertEqual(
                    speech_text[notation_start:notation_start + 2], "5f")
                self.assertIn("3f", speech_text)
                self.assertIn("5d", speech_text)
                ninth_chord = popup.result.document().find("D_9")
                self.assertFalse(ninth_chord.isNull())
                self.assertLessEqual(
                    ninth_chord.charFormat().fontWeight(), 400)
                code_chord = popup.result.document().find("C-dur")
                self.assertFalse(code_chord.isNull())
                self.assertTrue(code_chord.charFormat().fontFixedPitch())
                ninth_start = visible_text.index("D_9")
                self.assertEqual(
                    speech_text[ninth_start:ninth_start + 3], "D_9")
                self.assertIn("النغمة", speech_text)
                self.assertIn("Harmonic 5f", speech_text)
                self.assertIn("عنوان مهم", speech_text)
                self.assertIn("بعد المثال", speech_text)
                from popup.speech_worker import speech_start_offset
                self.assertEqual(
                    speech_start_offset(visible_text, 1), 0
                )
                from translators.chain import normalize_latex_output
                speakable_math = TranslatePopup._markdown_speech_text(
                    normalize_latex_output(
                        "النسبة هي $\\frac{1}{2}$، والجذر هو "
                        "$\\sqrt{x}$.\n\n"
                        "```music\nG Clef | F Clef\n```"
                    )
                )
                self.assertIn("(1)/(2)", speakable_math)
                self.assertIn("√(x)", speakable_math)
                self.assertNotIn("G Clef", speakable_math)
            finally:
                popup.close()

    def test_small_page_number_stays_visible_but_is_not_spoken(self):
        from PySide6.QtGui import QFont, QTextDocument
        from popup.translate_popup import TranslatePopup

        document = QTextDocument()
        document.setDefaultFont(QFont("Arial", 16))
        document.setMarkdown(
            "Main paragraph.\n\n<small>78</small>"
        )

        body_block = document.begin()
        body_format = body_block.begin().fragment().charFormat()
        page_number_block = body_block.next()
        page_number_format = page_number_block.begin().fragment().charFormat()
        speech_text = TranslatePopup._speech_text_from_document(document)

        self.assertEqual(document.toPlainText(), "Main paragraph.\n78")
        self.assertLess(
            page_number_format.font().pointSizeF(),
            body_format.font().pointSizeF(),
        )
        self.assertIn("Main paragraph.", speech_text)
        self.assertNotIn("78", speech_text)
        self.assertEqual(len(speech_text), len(document.toPlainText()))

    def test_popup_tightens_flat_and_sharp_symbols_to_note_letters(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS
        from PySide6.QtGui import QFont, QTextCursor

        with mock_chain_translate(return_value=("text", "T")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="hello")
            try:
                popup._render_markdown("B♭ C♯ C# D♮ Bring")
                document = popup.result.document()
                for note in ("B♭", "C♯", "C#", "D♮"):
                    cursor = document.find(note)
                    self.assertFalse(cursor.isNull())
                    note_start = cursor.selectionStart()
                    cursor.setPosition(note_start)
                    cursor.setPosition(
                        note_start + 1, QTextCursor.MoveMode.KeepAnchor)
                    self.assertEqual(
                        cursor.charFormat().fontLetterSpacingType(),
                        QFont.SpacingType.AbsoluteSpacing,
                    )
                    self.assertEqual(
                        cursor.charFormat().fontLetterSpacing(), -1.5)
                ordinary_text = document.find("Bring")
                self.assertFalse(ordinary_text.isNull())
                ordinary_start = ordinary_text.selectionStart()
                ordinary_text.setPosition(ordinary_start)
                ordinary_text.setPosition(
                    ordinary_start + 1,
                    QTextCursor.MoveMode.KeepAnchor,
                )
                self.assertNotEqual(
                    ordinary_text.charFormat().fontLetterSpacingType(),
                    QFont.SpacingType.AbsoluteSpacing,
                )
            finally:
                popup.close()

    def test_popup_remembers_user_resized_size(self):
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS, load
        from unittest import mock

        settings = dict(DEFAULTS)
        with mock_chain_translate(return_value=("\u0646\u0635", "T")):
            first = TranslatePopup(settings, source_text="hello")
            try:
                first.show()
                self.app.processEvents()
                first.resize(760, 510)
                first.move(700, 400)
                self.app.processEvents()
                first.close()
                saved = load()
                self.assertEqual(
                    settings["popup_size"],
                    [first.frameGeometry().width(), first.frameGeometry().height()])
                self.assertEqual(saved["popup_size"], settings["popup_size"])
                self.assertEqual(
                    settings["popup_pos"],
                    [first.frameGeometry().x(), first.frameGeometry().y()])
                self.assertEqual(saved["popup_pos"], settings["popup_pos"])

                second = TranslatePopup(saved, source_text="hello")
                try:
                    second.show()
                    self.app.processEvents()
                    self.assertEqual(
                        (second.frameGeometry().width(), second.frameGeometry().height()),
                        tuple(saved["popup_size"]))
                    screen = self.app.primaryScreen().availableGeometry()
                    frame = second.frameGeometry()
                    self.assertGreaterEqual(frame.left(), screen.left())
                    self.assertGreaterEqual(frame.top(), screen.top())
                    self.assertLessEqual(frame.right(), screen.right())
                    self.assertLessEqual(frame.bottom(), screen.bottom())
                finally:
                    second.close()
            finally:
                if not first.isHidden():
                    first.close()

    def test_popup_clamps_saved_geometry_to_screen(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup

        settings = dict(
            DEFAULTS,
            popup_pos=[9000, 9000],
            popup_size=[1600, 1200],
        )
        with mock_chain_translate(
                return_value=("ترجمة ظاهرة", "Test")):
            popup = TranslatePopup(settings, source_text="hello")
            try:
                popup.show()
                self.app.processEvents()
                screen = self.app.primaryScreen().availableGeometry()
                frame = popup.frameGeometry()
                self.assertTrue(popup.isVisible())
                self.assertGreaterEqual(frame.left(), screen.left())
                self.assertGreaterEqual(frame.top(), screen.top())
                self.assertLessEqual(frame.right(), screen.right())
                self.assertLessEqual(frame.bottom(), screen.bottom())
                self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
                self.assertTrue(popup.result.isVisible())
                self.assertEqual(popup.result.toPlainText(), "ترجمة ظاهرة")
            finally:
                popup.close()

    def test_popup_is_sober_movable_resizable(self):
        from PySide6.QtCore import Qt
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        pop = TranslatePopup(dict(DEFAULTS), source_text="hello")
        try:
            flags = pop.windowFlags()
            self.assertTrue(flags & Qt.WindowType.Tool)
            self.assertFalse(flags & Qt.WindowType.FramelessWindowHint)
            self.assertGreaterEqual(pop.minimumWidth(), 200)
            self.assertEqual(pop.layout().count(), 2)
        finally:
            try:
                if pop._worker:
                    if not pop._worker.wait(20000):
                        pop._worker.terminate()
                        pop._worker.wait(2000)
            except RuntimeError:
                pass
            try:
                pop.close()
            except RuntimeError:
                pass


class TestSession(_GuiCase):

    def test_settings_roundtrip(self):
        from config.settings import DEFAULTS, load, save
        s = dict(DEFAULTS)
        s.update({"popup_size": [500, 300], "last_pdf": "x.pdf",
                  "last_page": 7, "thumbs_visible": False,
                  "splitter_sizes": [170, 800], "window_geometry": "abc"})
        save(s)
        self.assertEqual(load(), s)

    def test_line_spacing_settings_use_pixel_bounds_and_ignore_legacy_percent(self):
        import json
        from config.settings import (
            DEFAULT_LINE_SPACING, MAX_LINE_SPACING, MIN_LINE_SPACING, load,
        )

        for stored, expected in (
            (-100, MIN_LINE_SPACING),
            (-10, -10),
            (3000, MAX_LINE_SPACING),
        ):
            self._settings_tmp.write_text(
                json.dumps({"line_spacing_px": stored}), encoding="utf-8"
            )
            self.assertEqual(load()["line_spacing_px"], expected)
        self._settings_tmp.write_text(
            json.dumps({"line_spacing_px": 10}), encoding="utf-8"
        )
        self.assertEqual(load()["line_spacing_px"], 10)
        self._settings_tmp.write_text(
            json.dumps({"line_height": 80}), encoding="utf-8"
        )
        self.assertEqual(load()["line_spacing_px"], DEFAULT_LINE_SPACING)
        self.assertEqual(DEFAULT_LINE_SPACING, 0)

    def test_translation_side_margin_settings_are_bounded(self):
        import json
        from config.settings import (
            DEFAULT_TRANSLATION_SIDE_MARGIN_PX,
            MAX_TRANSLATION_SIDE_MARGIN_PX,
            MIN_TRANSLATION_SIDE_MARGIN_PX,
            load,
        )

        for stored, expected in (
            (-10, MIN_TRANSLATION_SIDE_MARGIN_PX),
            (36, 36),
            (300, MAX_TRANSLATION_SIDE_MARGIN_PX),
        ):
            self._settings_tmp.write_text(
                json.dumps({"translation_side_margin_px": stored}),
                encoding="utf-8",
            )
            self.assertEqual(
                load()["translation_side_margin_px"], expected)

        self._settings_tmp.write_text("{}", encoding="utf-8")
        self.assertEqual(
            load()["translation_side_margin_px"],
            DEFAULT_TRANSLATION_SIDE_MARGIN_PX,
        )

    def test_translation_top_bottom_margin_settings_are_bounded(self):
        import json
        from config.settings import (
            DEFAULT_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
            MAX_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
            MIN_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
            load,
        )

        for stored, expected in (
            (-10, MIN_TRANSLATION_TOP_BOTTOM_MARGIN_PX),
            (36, 36),
            (300, MAX_TRANSLATION_TOP_BOTTOM_MARGIN_PX),
        ):
            self._settings_tmp.write_text(
                json.dumps({"translation_top_bottom_margin_px": stored}),
                encoding="utf-8",
            )
            self.assertEqual(
                load()["translation_top_bottom_margin_px"], expected)

        self._settings_tmp.write_text("{}", encoding="utf-8")
        self.assertEqual(
            load()["translation_top_bottom_margin_px"],
            DEFAULT_TRANSLATION_TOP_BOTTOM_MARGIN_PX,
        )

    def test_legacy_translation_zoom_is_migrated_into_font_size(self):
        import json
        from config.settings import (
            load,
        )

        self._settings_tmp.write_text(json.dumps({
            "font_size": 17,
            "translation_zoom_factor": 1.6,
        }), encoding="utf-8")
        migrated = load()
        self.assertAlmostEqual(migrated["font_size"], 27.2)
        self.assertNotIn("translation_zoom_factor", migrated)

    def test_settings_dialog_geometry_persists_on_save(self):
        from config.settings import DEFAULTS, load
        from main import SettingsDialog

        dialog = SettingsDialog(dict(DEFAULTS))
        try:
            dialog.resize(900, 700)
            dialog.move(40, 55)
            dialog.accept()
            saved = load()
            self.assertEqual(saved["settings_size"], [900, 700])
            self.assertEqual(saved["settings_pos"], [40, 55])
        finally:
            dialog.close()

    def test_removed_provider_settings_migrate_to_default_model(self):
        import json
        from config.settings import DEFAULT_MODEL, DEFAULTS, load

        for removed_model in (
            "groq:llama-3.3-70b-versatile",
            "cerebras:qwen-3.8-27b",
        ):
            self._settings_tmp.write_text(json.dumps({
                "model": removed_model,
                "groq_key": "obsolete-groq-key",
                "cerebras_key": "obsolete-cerebras-key",
            }), encoding="utf-8")
            settings = load()
            self.assertEqual(settings["model"], DEFAULT_MODEL)
            self.assertEqual(set(settings), set(DEFAULTS))

    def test_render_pump_completes(self):
        import time
        from PySide6.QtGui import QImage
        from reader.continuous import ContinuousReader
        calls = []
        initial_batches = []

        def render(i, z):
            calls.append(i)
            return QImage(100, 100, QImage.Format.Format_RGB888)

        reader = ContinuousReader(render)
        reader.initialRenderComplete.connect(
            lambda: initial_batches.append(len(calls)))
        try:
            reader.resize(400, 600)
            reader.show()
            reader.set_document([(200.0, 80.0)] * 5)
            for page in range(5):
                reader.scroll_to_page(page)
                deadline = time.time() + 3
                while page not in reader._rendered and time.time() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                self.assertIn(page, reader._rendered)
            self.assertEqual(set(calls), set(range(5)))
            self.assertEqual(len(initial_batches), 1)
            self.assertGreater(initial_batches[0], 0)
        finally:
            reader.close()

    def test_hidden_reader_defers_page_rendering_until_shown(self):
        import time
        from PySide6.QtGui import QImage
        from reader.continuous import ContinuousReader

        calls = []

        def render(index, _zoom):
            calls.append(index)
            return QImage(100, 100, QImage.Format.Format_RGB888)

        reader = ContinuousReader(render)
        reader.resize(400, 600)
        try:
            reader.set_document([(200.0, 300.0)] * 300)
            self.app.processEvents()
            self.assertEqual(calls, [])

            reader.show()
            deadline = time.time() + 3
            while not calls and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)

            self.assertTrue(calls, "showing the reader should render visible pages")
            self.assertLess(
                len(calls),
                reader.page_count(),
                "showing one page should not synchronously render the whole book",
            )
        finally:
            reader.close()

    def test_book_thumbnails_wait_for_initial_page_render(self):
        import time
        from reader.bookview import BookView

        book = BookView()
        try:
            book.resize(1000, 700)
            book.show()
            book.open(_make_pdf_file("thumbnail_after_initial_render.pdf", pages=3),
                      defer_render=True)
            self.app.processEvents()
            self.assertIsNone(book.thumbs._worker)

            book.reader._ensure_visible()
            deadline = time.time() + 5
            while not book.reader._initial_batch_logged and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)

            self.assertTrue(book.reader._initial_batch_logged)
            self.assertIsNotNone(book.thumbs._worker)
        finally:
            book.close_doc()
            book.close()

    def test_fit_page_and_fit_visible_apply_distinct_zoom(self):
        import tempfile
        import time
        from pathlib import Path
        import pymupdf
        from main import MainWindow

        path = Path(tempfile.gettempdir()) / "fit_visible_content.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=600, height=800)
        page.insert_text((260, 400), "Content in the middle")
        doc.save(path)
        doc.close()

        window = MainWindow()
        try:
            window.show()
            book = window._add_tab(str(path))
            self.assertIsNotNone(book)
            deadline = time.time() + 4
            while (book.reader.viewport().height() <= 0 or
                   book.initial_fit_pending) and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            book.fit_to_page()
            fit_page_zoom = book.zoom_percent
            book.fit_visible()
            self.app.processEvents()
            self.assertGreater(book.zoom_percent, fit_page_zoom)
            self.assertGreater(book.reader.verticalScrollBar().value(), 0)
        finally:
            window.close()

    def test_reader_releases_page_images_after_scrolling_away(self):
        import time
        from PySide6.QtGui import QImage
        from reader.continuous import ContinuousReader

        reader = ContinuousReader(
            lambda _index, _zoom: QImage(100, 100, QImage.Format.Format_RGB888))
        try:
            reader.resize(500, 400)
            reader.set_document([(200.0, 300.0)] * 30)
            reader.show()
            deadline = time.time() + 3
            while 0 not in reader._rendered and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertIn(0, reader._rendered)

            reader.verticalScrollBar().setValue(reader.verticalScrollBar().maximum())
            deadline = time.time() + 3
            while 0 in reader._rendered and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertNotIn(0, reader._rendered)
            self.assertTrue(reader._labels[0].pixmap().isNull())
        finally:
            reader.close()

    def test_pdf_scrollbar_is_visible_on_reader_right_edge(self):
        from PySide6.QtGui import QImage
        from main import app_stylesheet
        from reader.continuous import ContinuousReader

        reader = ContinuousReader(
            lambda _index, _zoom: QImage(100, 100, QImage.Format.Format_RGB888))
        try:
            reader.resize(500, 400)
            reader.set_document([(300.0, 400.0)] * 5, render=False)
            reader.show()
            self.app.processEvents()

            scrollbar = reader.verticalScrollBar()
            self.assertEqual(scrollbar.objectName(), "pdfVerticalScrollBar")
            self.assertEqual(scrollbar.accessibleName(), "PDF page scrollbar")
            self.assertTrue(scrollbar.isVisible())
            self.assertGreaterEqual(scrollbar.width(), 18)
            style = app_stylesheet("Dark Slate")
            self.assertIn("QScrollBar#pdfVerticalScrollBar::handle:vertical", style)
            self.assertIn("background-color: #6366F1", style)
            self.assertIn("min-height: 64px", style)
            self.assertGreater(
                scrollbar.mapTo(reader, scrollbar.rect().topLeft()).x(),
                reader.width() // 2)
            self.assertEqual(
                scrollbar.mapTo(reader, scrollbar.rect().topRight()).x(),
                reader.width() - 1)
            self.assertGreater(scrollbar.maximum(), 0)
            scrollbar.setValue(scrollbar.maximum())
            self.assertEqual(scrollbar.value(), scrollbar.maximum())
        finally:
            reader.close()

    def test_hidden_book_defers_initial_fit_until_selected(self):
        import time
        from main import MainWindow

        first_path = _make_pdf_file("hidden_fit_first.pdf")
        second_path = _make_pdf_file("hidden_fit_second.pdf")
        win = MainWindow()
        try:
            win.show()
            first = win._add_tab(first_path)
            second = win._add_tab(second_path)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)

            deadline = time.time() + 3
            while second.initial_fit_pending and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertFalse(second.initial_fit_pending)
            self.assertTrue(first.initial_fit_pending)

            win.tabs.setCurrentWidget(first)
            deadline = time.time() + 3
            while first.initial_fit_pending and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertFalse(first.initial_fit_pending)
        finally:
            win.close()

    def test_initial_fit_renders_even_when_zoom_does_not_change(self):
        import time
        from unittest import mock
        from main import MainWindow
        from reader.bookview import BookView

        win = MainWindow()
        try:
            win.resize(1000, 700)
            win.show()
            with mock.patch.object(BookView, "set_zoom_percent", autospec=True):
                book = win._add_tab(_make_pdf_file("initial_fit_no_zoom_change.pdf"))
                self.assertIsNotNone(book)
                deadline = time.time() + 5
                while (not book.reader._rendered and time.time() < deadline):
                    self.app.processEvents()
                    time.sleep(0.01)

            self.assertTrue(book.reader._rendered)
            self.assertFalse(book.initial_fit_pending)
        finally:
            win.close()

    def test_reader_centers_each_page_size_after_zoom(self):
        from PySide6.QtGui import QImage
        from reader.continuous import ContinuousReader

        reader = ContinuousReader(
            lambda _index, _zoom: QImage(100, 100, QImage.Format.Format_RGB888))
        try:
            reader.resize(800, 600)
            reader.set_document(
                [(300.0, 400.0), (500.0, 400.0), (1000.0, 400.0)],
                render=False)
            reader.show()
            reader.set_zoom(1.0)
            self.app.processEvents()
            viewport_center = reader._container.width() / 2
            for page in reader._labels[:2]:
                self.assertAlmostEqual(
                    page.x() + page.width() / 2, viewport_center, delta=2)
            reader.center_page_horizontally(2)
            self.app.processEvents()
            oversized_page = reader._labels[2]
            visible_center = reader.viewport().mapFromGlobal(
                oversized_page.mapToGlobal(oversized_page.rect().center()))
            self.assertAlmostEqual(
                visible_center.x(), reader.viewport().width() / 2, delta=2)
        finally:
            reader.close()

    def test_restore_opens_last_book_and_page(self):
        import tempfile
        import time
        import pymupdf
        from main import MainWindow

        tmp = os.path.join(tempfile.gettempdir(), "restore_test.pdf")
        doc = pymupdf.open()
        for n in range(3):
            pg = doc.new_page()
            pg.insert_text((72, 100), f"restore page {n}")
        doc.save(tmp)
        doc.close()

        from config.settings import DEFAULTS, save
        s = dict(DEFAULTS)
        s.update({"last_zoom": 225.0,
                  "open_tabs": [{"path": tmp, "page": 2, "zoom": 150.0,
                                  "ocr": "ru,en", "pinned": True}],
                  "active_tab": 0})
        save(s)

        win = MainWindow()
        try:
            win.show()
            t0 = time.time()
            book = None
            while time.time() - t0 < 15:
                self.app.processEvents()
                time.sleep(0.05)
                book = win.current_book()
                if book is not None and book.has_doc() and book.current_page() == 2:
                    break
            self.assertIsNotNone(book)
            self.assertTrue(book.has_doc())
            self.assertEqual(book.current_page(), 2)
            self.assertEqual(win.tabs.count(), 1)
            self.assertEqual(book.zoom_percent, 150.0)  # restore keeps zoom
            self.assertTrue(book.pinned)
            self.assertGreater(win._flash_token, 0)  # visible "welcome back" proof
            self.assertEqual(win.settings["last_zoom"], 225.0)
        finally:
            try:
                win.close()
            except RuntimeError:
                pass

    def test_reopening_closed_book_restores_its_last_page(self):
        import time
        from config.settings import load
        from main import MainWindow

        path = _make_pdf_file("closed_book_position.pdf", pages=4)
        window = MainWindow()
        try:
            window.show()
            window.open_paths([path])
            book = window.current_book()
            self.assertIsNotNone(book)
            book.scroll_to_page(2)
            deadline = time.time() + 5
            while time.time() < deadline and book.current_page() != 2:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertEqual(book.current_page(), 2)

            window._close_tab(window.tabs.currentIndex())
            saved = load()
            key = os.path.normcase(str(Path(path).resolve()))
            self.assertEqual(saved["book_positions"][key]["page"], 2)

            window.open_paths([path])
            reopened = window.current_book()
            deadline = time.time() + 5
            while time.time() < deadline and reopened.current_page() != 2:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertEqual(reopened.current_page(), 2)
        finally:
            try:
                window.close()
            except RuntimeError:
                pass

    def test_bookmarks_survive_closing_and_reopening_book_tab(self):
        import time
        from config.settings import load
        from main import MainWindow

        path = _make_pdf_file("closed_book_bookmarks.pdf", pages=5)
        window = MainWindow()
        try:
            window.show()
            window.open_paths([path])
            book = window.current_book()
            self.assertIsNotNone(book)
            book.set_bookmarks([1, 3])
            window._save_session_soon()

            window._close_tab(window.tabs.currentIndex())
            key = os.path.normcase(str(Path(path).resolve()))
            self.assertEqual(
                load()["book_positions"][key]["bookmarks"], [1, 3])

            window.open_paths([path])
            reopened = window.current_book()
            self.assertEqual(reopened.bookmarked_pages, {1, 3})
        finally:
            try:
                window.close()
            except RuntimeError:
                pass

    def test_page_canvas_context_menu_has_shared_page_actions_and_refresh(self):
        from unittest import mock
        from main import MainWindow
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QContextMenuEvent

        path = _make_pdf_file("page_context_menu.pdf", pages=3)
        window = MainWindow()
        try:
            window.show()
            window.open_paths([path])
            book = window.current_book()
            self.assertIsNotNone(book)
            requested = []
            book.pageContextRequested.connect(
                lambda page, position: requested.append((page, position)))
            page_position = QPoint(12, 16)
            global_position = QPoint(80, 90)
            event = QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse,
                page_position,
                global_position,
            )
            with mock.patch.object(
                    window, "_show_app_context_menu") as app_menu:
                handled = window._thumbs_wheel_filter.eventFilter(
                    book.reader._labels[1], event)
            self.assertFalse(handled)
            app_menu.assert_not_called()
            with mock.patch.object(
                    window, "_show_book_page_context_menu") as show_menu:
                book.reader._labels[1].contextMenuEvent(event)
            show_menu.assert_called_once_with(book, 1, global_position)
            self.assertEqual(requested, [(1, global_position)])

            menu = window._book_page_context_menu(book, 1)
            self.assertIsNotNone(menu)
            actions = menu.actions()
            self.assertEqual(
                [action.text() for action in actions],
                [
                    "Translate full page", "Successive reading",
                    "Extract page…", "Delete page", "Copy page image", "",
                    "Add bookmark",
                    "Rotate page left (view only)",
                    "Rotate page right (view only)", "Copy page text",
                    "", "Refresh", "ReOpen",
                ],
            )
            with mock.patch.object(window, "translate_page") as translate:
                actions[1].trigger()
            translate.assert_called_once_with(
                book, 1, successive_reading=True)
            self.assertEqual(
                menu.styleSheet(),
                "QMenu::item { font-size: 17px; padding: 9px 24px; }")
            self.assertTrue(all(
                action.icon().isNull() and action.shortcut().isEmpty()
                for action in actions if not action.isSeparator()
            ))
            menu.deleteLater()
        finally:
            try:
                window.close()
            except RuntimeError:
                pass

    def test_page_context_actions_bookmark_rotate_and_copy_text(self):
        from main import MainWindow
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QApplication

        window = MainWindow()
        try:
            window.show()
            window.open_paths([_make_pdf_file("page_context_actions.pdf")])
            book = window.current_book()
            self.assertIsNotNone(book)
            document = book._document
            initial_pdf_rotation = document[0].rotation
            initial_size = book.page_rect_on_screen(0).size()
            initial_modified = book.is_modified

            menu = window._book_page_context_menu(book, 0)
            self.assertIsNotNone(menu)
            next(
                action for action in menu.actions()
                if action.text() == "Add bookmark"
            ).trigger()
            self.assertIn(0, book.bookmarked_pages)
            self.assertTrue(book.thumbs.bookmark_button.isChecked())

            next(
                action for action in menu.actions()
                if action.text() == "Rotate page left (view only)"
            ).trigger()
            self.assertEqual(book._view_rotations, [90])
            self.assertEqual(document[0].rotation, initial_pdf_rotation)
            self.assertEqual(book.page_rect_on_screen(0).size(),
                             initial_size.transposed())
            self.assertEqual(book.is_modified, initial_modified)
            word = next(
                word for word in document[0].get_text("words")
                if word[4] == "memory")
            page_width = book._page_sizes[0][0]
            scale = book.zoom
            rect = QRect(
                round((word[1] - 6) * scale),
                round((page_width - word[2] - 6) * scale),
                round((word[3] - word[1] + 12) * scale),
                round((word[2] - word[0] + 12) * scale),
            )
            kind, extracted_text = book.extract(0, rect)
            self.assertEqual(kind, "text")
            self.assertIn("memory", extracted_text)
            rendered = book._render_page(0, book.zoom)
            self.assertEqual(
                (rendered.width(), rendered.height()),
                (book.page_rect_on_screen(0).width() - 1,
                 book.page_rect_on_screen(0).height() - 1),
            )
            next(
                action for action in menu.actions()
                if action.text() == "Rotate page right (view only)"
            ).trigger()
            self.assertEqual(book._view_rotations, [0])
            self.assertEqual(book.page_rect_on_screen(0).size(), initial_size)

            next(
                action for action in menu.actions()
                if action.text() == "Copy page text"
            ).trigger()
            self.assertIn(
                "memory page 0", QApplication.clipboard().text())
            menu.deleteLater()
        finally:
            try:
                window.close()
            except RuntimeError:
                pass

    def test_rotated_page_thumbnail_uses_view_orientation(self):
        import threading
        from reader.thumbnails import _render_thumbnails

        results = []

        class ResultQueue:
            def put(self, value):
                results.append(value)

        _render_thumbnails(
            _make_pdf_file("rotated_thumbnail.pdf"),
            [0],
            100,
            [90],
            ResultQueue(),
            threading.Event(),
        )
        thumbnail = next(item for item in results if item[0] == "thumb")
        self.assertGreater(thumbnail[2], thumbnail[3])

    def test_refresh_content_keeps_open_book_position_and_bookmarks(self):
        from unittest import mock
        from main import MainWindow

        window = MainWindow()
        try:
            window.show()
            window.open_paths([_make_pdf_file("refresh_book_state.pdf", pages=3)])
            book = window.current_book()
            self.assertIsNotNone(book)
            book.set_bookmarks([1])
            book.scroll_to_page(2)
            document = book._document
            path = book.path
            zoom = book.zoom_percent

            with mock.patch.object(book.reader, "refresh_pages") as refresh:
                book.refresh_content()

            refresh.assert_called_once_with()
            self.assertIs(book._document, document)
            self.assertEqual(book.path, path)
            self.assertEqual(book.current_page(), 2)
            self.assertEqual(book.bookmarked_pages, {1})
            self.assertEqual(book.zoom_percent, zoom)
        finally:
            try:
                window.close()
            except RuntimeError:
                pass

    def test_reader_accepts_dropped_supported_documents(self):
        from unittest import mock
        from PySide6.QtCore import QMimeData, QUrl
        from main import MainWindow

        window = MainWindow()
        mime = QMimeData()
        mime.setUrls([
            QUrl.fromLocalFile(r"C:\books\harmony.pdf"),
            QUrl.fromLocalFile(r"C:\books\notes.txt"),
        ])
        event = mock.Mock()
        event.mimeData.return_value = mime
        try:
            with mock.patch.object(window, "open_paths") as open_paths:
                window.dropEvent(event)

            open_paths.assert_called_once_with([
                url.toLocalFile() for url in mime.urls()])
            event.acceptProposedAction.assert_called_once_with()
        finally:
            window.close()

    def test_new_books_inherit_last_zoom_and_persist_it(self):
        import time
        from main import MainWindow

        first = _make_pdf_file("remember_zoom_first.pdf")
        second = _make_pdf_file("remember_zoom_second.pdf")
        window = MainWindow()
        try:
            window.show()
            window.open_paths([first])
            first_book = window.current_book()
            self.assertIsNotNone(first_book)
            deadline = time.time() + 5
            while (first_book.initial_fit_pending and time.time() < deadline):
                self.app.processEvents()
                time.sleep(0.01)

            window._set_current_zoom(175.0)
            window.open_paths([second])
            second_book = window.current_book()

            self.assertIsNotNone(second_book)
            self.assertEqual(second_book.zoom_percent, 175.0)
            self.assertEqual(window.settings["last_zoom"], 175.0)
        finally:
            window.close()

    def test_session_restores_multiple_books_and_view_details(self):
        import time
        from PySide6.QtCore import QRect
        from main import MainWindow
        from config.settings import load

        first_path = _make_pdf_file("workspace_first.pdf", pages=4)
        second_path = _make_pdf_file("workspace_second.pdf", pages=5)
        third_path = _make_pdf_file("workspace_third.pdf", pages=3)
        win = MainWindow()
        try:
            win.show()
            win.open_paths([first_path, second_path, third_path])
            deadline = time.time() + 5
            while time.time() < deadline:
                self.app.processEvents()
                if (win.tabs.count() == 3 and
                        all(book.reader.viewport().height() > 0
                            for book in win._iter_books())):
                    break
                time.sleep(0.02)

            first, second, third = list(win._iter_books())
            win.thumbs_action.setChecked(True)
            first.thumbs.set_thumbnail_zoom(1.35)
            second.thumbs.set_thumbnail_zoom(0.85)
            third.thumbs.set_thumbnail_zoom(1.7)
            first.set_bookmarks([1, 3])
            second.set_bookmarks([2])
            first.set_zoom_percent(125)
            first.ocr_langs = "de,en"
            first.last_selection = (1, QRect(12, 24, 160, 30))
            first.pinned = True
            first.scroll_to_page(1)
            first.reader.verticalScrollBar().setValue(
                first.reader._labels[0].y() + first.reader._labels[0].height() + 20)
            first.splitter.setSizes([190, max(300, first.width() - 190)])
            second.set_zoom_percent(150)
            second.ocr_langs = "it,en"
            second.scroll_to_page(2)
            second.reader.verticalScrollBar().setValue(
                second.reader._labels[1].y() + second.reader._labels[1].height() + 20)
            win.tabs.setCurrentWidget(first)
            win._theme_actions["Pure Light"].trigger()
            self.app.processEvents()
            deadline = time.time() + 3
            while time.time() < deadline:
                persisted = load()["open_tabs"]
                if (len(persisted) == 3 and persisted[0].get("scroll_position")
                        == first.reader.verticalScrollBar().value()):
                    break
                self.app.processEvents()
                time.sleep(0.02)
            self.assertEqual(
                load()["open_tabs"][0]["scroll_position"],
                first.reader.verticalScrollBar().value())
            win._save_session_soon()
            first_scroll_position = first.reader.verticalScrollBar().value()
            first_splitter_sizes = first.splitter.sizes()
            saved_before_close = load()
            win.close()

            self.assertEqual(len(saved_before_close["open_tabs"]), 3)
            self.assertEqual(saved_before_close["active_tab"], 0)
            self.assertEqual(saved_before_close["theme"], "Pure Light")
            self.assertTrue(saved_before_close["thumbs_visible"])
            self.assertTrue(saved_before_close["window_geometry"])
            self.assertEqual(saved_before_close["open_tabs"][0]["zoom"], 125)
            self.assertEqual(saved_before_close["open_tabs"][0]["ocr"], "de,en")
            self.assertTrue(saved_before_close["open_tabs"][0]["pinned"])
            self.assertEqual(
                saved_before_close["open_tabs"][0]["scroll_position"],
                first_scroll_position)
            self.assertEqual(
                saved_before_close["open_tabs"][0]["splitter_sizes"],
                first_splitter_sizes)
            sel = saved_before_close["open_tabs"][0]["last_selection"]
            # Points survive a zoom change; pixels would not. QRect.right() and
            # bottom() are inclusive, hence the -1 in the expected x1/y1.
            z = first.zoom
            self.assertEqual(sel[0], "pdf")
            self.assertEqual(sel[1], 1)
            self.assertEqual([round(v, 1) for v in sel[2:]],
                             [round(12 / z, 1), round(24 / z, 1),
                              round((12 + 160 - 1) / z, 1),
                              round((24 + 30 - 1) / z, 1)])
            self.assertEqual(saved_before_close["open_tabs"][1]["zoom"], 150)
            self.assertEqual(saved_before_close["open_tabs"][1]["ocr"], "it,en")
            self.assertEqual(
                [item["thumbnail_zoom"] for item in saved_before_close["open_tabs"]],
                [1.35, 0.85, 1.7])
            self.assertEqual(
                [item["bookmarks"] for item in saved_before_close["open_tabs"]],
                [[1, 3], [2], []])
            self.assertEqual(
                len(load()["open_tabs"]), 3,
                f"Close lost saved tabs; in-memory state: {win.settings['open_tabs']!r}")

            restored = MainWindow()
            try:
                restored.show()
                deadline = time.time() + 10
                while time.time() < deadline:
                    self.app.processEvents()
                    if restored.tabs.count() == 3:
                        break
                    time.sleep(0.02)
                self.assertEqual(
                    restored.tabs.count(), 3,
                    f"Could not restore saved tabs: {restored.settings['open_tabs']!r}")
                books = list(restored._iter_books())
                restored.tabs.setCurrentWidget(books[1])
                deadline = time.time() + 3
                while time.time() < deadline:
                    self.app.processEvents()
                    if books[1].current_page() == 2:
                        break
                    time.sleep(0.02)
                restored.tabs.setCurrentWidget(books[0])
                deadline = time.time() + 3
                while time.time() < deadline:
                    self.app.processEvents()
                    if books[0].current_page() == 1:
                        break
                    time.sleep(0.02)
                self.assertEqual(
                    [book.path for book in books],
                    [first_path, second_path, third_path])
                self.assertEqual(restored.tabs.currentWidget(), books[0])
                self.assertEqual(
                    [book.current_page() for book in books],
                    [item["page"] for item in saved_before_close["open_tabs"]])
                self.assertEqual(
                    [book.zoom_percent for book in books],
                    [item["zoom"] for item in saved_before_close["open_tabs"]])
                self.assertEqual(
                    [book.ocr_langs for book in books], ["de,en", "it,en", "auto"])
                self.assertEqual(
                    [book.thumbs.thumbnail_zoom for book in books],
                    [1.35, 0.85, 1.7])
                self.assertEqual(
                    [sorted(book.bookmarked_pages) for book in books],
                    [[1, 3], [2], []])
                self.assertTrue(books[0].pinned)
                # THE property: whatever zoom is active after restore, the selection must
                # still cover the same words. Points make that check possible.
                rsel = books[0].last_selection
                self.assertEqual(rsel[0], 1)
                z2 = books[0].zoom
                restored_pdf = [rsel[1].left() / z2, rsel[1].top() / z2,
                                rsel[1].right() / z2, rsel[1].bottom() / z2]
                for got, want in zip(restored_pdf, sel[2:]):
                    self.assertAlmostEqual(got, want, delta=1.0)
                self.assertEqual(
                    books[0].splitter.sizes()[0],
                    saved_before_close["open_tabs"][0]["splitter_sizes"][0])
                self.assertTrue(restored.thumbs_action.isChecked())
                self.assertTrue(restored._theme_actions["Pure Light"].isChecked())
                self.assertEqual(
                    [book.session_state()["scroll_offset"] for book in books[:2]],
                    [item["scroll_offset"]
                     for item in saved_before_close["open_tabs"][:2]])
            finally:
                restored.close()
        finally:
            if not win.isHidden():
                win.close()

    def test_tabs_pin_and_close(self):
        from main import MainWindow
        win = MainWindow()
        try:
            a = _make_pdf_file("tab_a.pdf")
            b = _make_pdf_file("tab_b.pdf")
            win.open_paths([a, b])
            self.assertEqual(win.tabs.count(), 2)
            win.open_paths([a])  # duplicate -> activates, no new tab
            self.assertEqual(win.tabs.count(), 2)
            first = win.tabs.widget(0)
            first.pinned = True
            win._apply_pin_title(first)
            win.tabs.widget(1).pinned = False
            win._move_pinned_tabs_to_bottom()
            self.assertEqual(win.tabs.widget(win.tabs.count() - 1), first)
            self.assertTrue(win.tabs.tabText(win.tabs.count() - 1).startswith("📌"))
            win._close_tab(win._book_tab_index(first))  # pinned -> refused
            self.assertEqual(win.tabs.count(), 2)
            first.pinned = False
            win._close_tab(win._book_tab_index(first))
            self.assertEqual(win.tabs.count(), 1)
        finally:
            try:
                win.close()
            except RuntimeError:
                pass


class TestMemoryZoom(_GuiCase):

    def _make_pdf(self, name, pages=1):
        return _make_pdf_file(name, pages)

    def test_translation_popup_has_no_chat_or_explain_header_buttons(self):
        from popup.translate_popup import TranslatePopup

        with mock_chain_translate(return_value=("translation", "Test")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="source")
            try:
                self.assertFalse(hasattr(popup, "chat_btn"))
                self.assertFalse(hasattr(popup, "explain_btn"))
            finally:
                popup.close()

    def test_markdown_rendering_preserves_streamed_line_breaks_and_paragraph_gaps(self):
        from popup.translate_popup import TranslatePopup

        with mock_chain_translate(return_value=("translation", "Test")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="source")
            try:
                if popup._worker:
                    self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()

                popup._render_markdown("First paragraph.\nSecond line.")
                document = popup.result.document()
                self.assertEqual(document.blockCount(), 1)
                self.assertEqual(
                    document.toPlainText(),
                    "First paragraph.\nSecond line.",
                )
                self.assertEqual(
                    document.begin().blockFormat().bottomMargin(), 0)

                popup._render_markdown("First paragraph.\n\nSecond paragraph.")
                document = popup.result.document()
                self.assertEqual(document.blockCount(), 2)
                self.assertGreater(
                    document.begin().blockFormat().bottomMargin(), 0)
            finally:
                popup.close()

    def test_fenced_code_is_compact_spaced_colored_and_silent_in_all_languages(self):
        from PySide6.QtGui import QTextFormat
        from popup.translate_popup import TranslatePopup

        with mock_chain_translate(return_value=("translation", "Test")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="source")
            try:
                if popup._worker:
                    self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
                markdown = (
                    "Avant le code.\n\n"
                    "```python\nx = 42\nprint(x)\n```\n\n"
                    "Entre les blocs.\n\n"
                    "```text\n>>> example\noutput\n```\n\n"
                    "Après les blocs.\n\n"
                    "```music\nC & G\nF & A\n```"
                )
                expected_blocks = [
                    "Avant le code.", "x = 42", "print(x)",
                    "Entre les blocs.", ">>> example", "output",
                    "Après les blocs.", "C & G", "F & A",
                ]
                for target_language in ("ar", "fr", "en"):
                    with self.subTest(target_language=target_language):
                        popup.settings["target_language"] = target_language
                        popup._render_markdown(markdown)
                        document = popup.result.document()
                        blocks = []
                        block = document.begin()
                        while block.isValid():
                            blocks.append(block)
                            block = block.next()
                        self.assertEqual(
                            [block.text() for block in blocks],
                            expected_blocks,
                        )
                        for index in (1, 2, 4, 5, 7, 8):
                            block_format = blocks[index].blockFormat()
                            self.assertTrue(block_format.hasProperty(
                                QTextFormat.Property.BlockCodeFence))
                            self.assertFalse(
                                block_format.nonBreakableLines())
                            self.assertEqual(
                                block_format.background().color().name(),
                                "#e7edf3",
                            )
                            self.assertEqual(
                                block_format.leftMargin(), 10)
                        for index in (1, 4, 7):
                            self.assertGreater(
                                blocks[index].blockFormat().topMargin(), 0)
                        for index in (2, 5, 8):
                            self.assertGreater(
                                blocks[index].blockFormat().bottomMargin(), 0)
                        self.assertEqual(
                            blocks[1].blockFormat().bottomMargin(), 0)
                        self.assertEqual(
                            blocks[2].blockFormat().topMargin(), 0)
                        prose = document.find("Avant le code.")
                        code = document.find("x = 42")
                        prose_size = (
                            prose.charFormat().fontPointSize()
                            or popup.result.font().pointSizeF()
                        )
                        self.assertLess(
                            code.charFormat().fontPointSize(),
                            prose_size,
                        )
                        self.assertTrue(code.charFormat().fontFixedPitch())
                        self.assertEqual(
                            code.charFormat().foreground().color().name(),
                            "#36566f",
                        )
                        speech = popup._current_speech_text()
                        for code_line in expected_blocks[1:3] + expected_blocks[4:6] + expected_blocks[7:9]:
                            self.assertNotIn(code_line, speech)
                        self.assertIn("Avant le code.", speech)
                        self.assertIn("Après les blocs.", speech)

                popup.result.setFixedWidth(360)
                long_music_line = "Cmaj7 | Dmin7 | " * 80
                popup._render_markdown(
                    f"```music\n{long_music_line}\n```")
                self.app.processEvents()
                self.assertEqual(
                    popup.result.horizontalScrollBar().maximum(), 0)
            finally:
                popup.close()

    def test_translation_side_margin_settings_apply_both_sides_live(self):
        from PySide6.QtWidgets import QWidget
        from ui.settings_dialog import SettingsDialog
        from popup.translate_popup import TranslatePopup

        settings = dict(DEFAULTS)
        host = QWidget()
        try:
            with mock_chain_translate(return_value=("translation", "Test")):
                popup = TranslatePopup(settings, source_text="source")
                popup.show()
                if popup._worker:
                    self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
            host.popups = [popup]
            dialog = SettingsDialog(settings, host)
            try:
                self.assertEqual(dialog.translation_side_margin.value(), 7)
                dialog.translation_side_margin.setValue(24)
                self.app.processEvents()
                self.assertEqual(
                    dialog.values()["translation_side_margin_px"], 24)
                self.assertIn("padding-left: 24px", popup.result.styleSheet())
                self.assertIn("padding-right: 24px", popup.result.styleSheet())

                dialog.reject()
                self.app.processEvents()
                self.assertIn("padding-left: 7px", popup.result.styleSheet())
                self.assertIn("padding-right: 7px", popup.result.styleSheet())
            finally:
                dialog.close()
                popup.close()
        finally:
            host.close()

    def test_translation_top_bottom_margin_settings_apply_live(self):
        from PySide6.QtWidgets import QWidget
        from ui.settings_dialog import SettingsDialog
        from popup.translate_popup import TranslatePopup

        settings = dict(DEFAULTS)
        host = QWidget()
        try:
            with mock_chain_translate(return_value=("translation", "Test")):
                popup = TranslatePopup(settings, source_text="source")
                popup.show()
                if popup._worker:
                    self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
            host.popups = [popup]
            dialog = SettingsDialog(settings, host)
            try:
                self.assertEqual(
                    dialog.translation_top_bottom_margin.value(), 7)
                document = popup.result.document()
                document.adjustSize()
                initial_height = document.size().height()
                initial_text_y = document.begin().layout().position().y()
                dialog.translation_top_bottom_margin.setValue(40)
                self.app.processEvents()
                self.assertEqual(
                    dialog.values()["translation_top_bottom_margin_px"], 40)
                document.adjustSize()
                self.assertEqual(
                    document.rootFrame().frameFormat().topMargin(), 40)
                self.assertEqual(
                    document.rootFrame().frameFormat().bottomMargin(), 40)
                self.assertAlmostEqual(
                    document.begin().layout().position().y()
                    - initial_text_y, 33)
                self.assertAlmostEqual(
                    document.size().height() - initial_height, 66)
                self.assertNotIn("padding-top:", popup.result.styleSheet())
                self.assertNotIn("padding-bottom:", popup.result.styleSheet())

                dialog.reject()
                self.app.processEvents()
                document.adjustSize()
                self.assertEqual(
                    document.rootFrame().frameFormat().topMargin(), 7)
                self.assertEqual(
                    document.rootFrame().frameFormat().bottomMargin(), 7)
            finally:
                dialog.close()
                popup.close()
        finally:
            host.close()

    def test_ctrl_wheel_smoothly_zooms_translation_text_without_mouse_button(self):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtWidgets import QApplication
        from popup.translate_popup import TranslatePopup

        with mock_chain_translate(return_value=("A translation to zoom.", "Test")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="source")
            try:
                if popup._worker:
                    self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()

                popup.show()
                self.app.processEvents()
                event = QWheelEvent(
                    QPointF(10, 10),
                    QPointF(10, 10),
                    QPoint(0, 0),
                    QPoint(0, 30),
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.ControlModifier,
                    Qt.ScrollPhase.NoScrollPhase,
                    False,
                )
                QApplication.sendEvent(popup.result.viewport(), event)
                self.app.processEvents()

                self.assertTrue(event.isAccepted())
                new_size = popup.settings["font_size"]
                self.assertGreater(new_size, 17)
                self.assertLess(new_size, 18)
                self.assertAlmostEqual(
                    popup.result.font().pointSizeF(),
                    new_size,
                    places=1,
                )
                from config.settings import load
                self.assertAlmostEqual(
                    load()["font_size"],
                    new_size,
                )
                self.assertEqual(popup.result.toolTip(), "")

                size_before_wheel = popup.settings["font_size"]
                no_left_button = QWheelEvent(
                    QPointF(10, 10),
                    QPointF(10, 10),
                    QPoint(0, 0),
                    QPoint(0, 120),
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.ControlModifier,
                    Qt.ScrollPhase.NoScrollPhase,
                    False,
                )
                popup.result.wheelEvent(no_left_button)
                self.assertAlmostEqual(
                    popup.settings["font_size"],
                    round(size_before_wheel * 1.1, 1),
                )

                plain_wheel = QWheelEvent(
                    QPointF(10, 10),
                    QPointF(10, 10),
                    QPoint(0, 0),
                    QPoint(0, 120),
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                    Qt.ScrollPhase.NoScrollPhase,
                    False,
                )
                size_before_plain_wheel = popup.settings["font_size"]
                popup.result.wheelEvent(plain_wheel)
                self.assertEqual(
                    popup.settings["font_size"], size_before_plain_wheel)
            finally:
                popup.close()

    def test_translation_popup_restores_saved_font_size(self):
        from config.settings import load, save
        from popup.translate_popup import TranslatePopup

        save({**DEFAULTS, "font_size": 27.4})
        popup = TranslatePopup(load())
        try:
            self.assertAlmostEqual(popup.result.font().pointSizeF(), 27.4)
            self.assertEqual(popup.result.toolTip(), "")
        finally:
            popup.close()

    def test_translation_titlebar_double_click_toggles_vertical_size(self):
        import sys
        from PySide6.QtWidgets import QWidget
        from popup.translate_popup import TranslatePopup

        host = QWidget()
        host.setGeometry(50, 50, 900, 700)
        host.show()
        popup = TranslatePopup(
            dict(DEFAULTS), source_text="source", parent=host)
        try:
            popup.show()
            self.app.processEvents()
            popup.resize(500, 300)
            popup.move(host.x() + 100, host.y() + 100)
            self.app.processEvents()
            normal_geometry = popup.frameGeometry()

            def double_click_titlebar():
                if sys.platform == "win32":
                    from ctypes import addressof, wintypes

                    native_message = wintypes.MSG()
                    native_message.message = 0x00A3
                    native_message.wParam = 2
                    handled, _ = popup.nativeEvent(
                        b"windows_generic_MSG", addressof(native_message))
                    self.assertTrue(handled)
                else:
                    popup._toggle_vertical_maximize()

            double_click_titlebar()
            self.app.processEvents()

            area_origin = host.mapToGlobal(host.rect().topLeft())
            area_bottom = area_origin.y() + host.height() - 1
            expanded_geometry = popup.frameGeometry()
            self.assertEqual(expanded_geometry.x(), normal_geometry.x())
            self.assertEqual(expanded_geometry.width(), normal_geometry.width())
            self.assertEqual(expanded_geometry.top(), area_origin.y())
            self.assertEqual(expanded_geometry.bottom(), area_bottom)

            popup._save_geometry()
            self.assertEqual(
                popup.settings["popup_size"],
                [normal_geometry.width(), normal_geometry.height()],
            )
            self.assertEqual(
                popup.settings["popup_pos"],
                [normal_geometry.x(), normal_geometry.y()],
            )

            double_click_titlebar()
            self.app.processEvents()
            self.assertEqual(popup.frameGeometry(), normal_geometry)
            self.assertIsNone(popup._vertical_restore_geometry)
        finally:
            popup.close()
            host.close()

    def test_streamed_partials_append_without_reformatting_full_text(self):
        from unittest import mock
        from popup.translate_popup import TranslatePopup

        with mock_chain_translate(return_value=("final translation", "Test")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="source")
            try:
                if popup._worker:
                    self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
                popup.result.clear()
                popup._streamed_partial = ""
                with mock.patch.object(popup, "_render_markdown") as render:
                    with mock.patch.object(
                        popup, "_apply_streamed_typography") as typography:
                        popup._append_streamed_partial("A long ")
                        typography.assert_called_once()
                        popup._append_streamed_partial(
                        "A long **translation**")
                        self.assertEqual(
                        popup.result.toPlainText(),
                        "A long **translation**")
                        render.assert_not_called()
                        typography.assert_called_once()

                        popup._append_streamed_partial("Corrected partial")
                        self.assertEqual(typography.call_count, 2)
                    self.assertEqual(
                        popup.result.toPlainText(), "Corrected partial")
            finally:
                popup.close()

    def test_translation_layout_direction_tracks_target_language(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QTextCursor
        from popup.translate_popup import TranslatePopup

        settings = dict(DEFAULTS, target_language="ar")
        with mock_chain_translate(return_value=("ترجمة", "Test")):
            popup = TranslatePopup(settings, source_text="source")
            try:
                if popup._worker:
                    self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
                popup.resize(640, 420)
                popup.show()
                self.app.processEvents()
                popup._render_markdown(
                    "### عنوان\n\nنص الترجمة\n\n```text\nC    G7   C\n```"
                )
                self.app.processEvents()
                self.assertEqual(
                    popup.result.layoutDirection(),
                    Qt.LayoutDirection.RightToLeft,
                )
                self.assertEqual(
                    popup.result.document().begin().blockFormat().alignment(),
                    Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignAbsolute,
                )
                self.assertEqual(
                    popup.result.document().begin().blockFormat().layoutDirection(),
                    Qt.LayoutDirection.RightToLeft,
                )
                prose = popup.result.document().begin().next()
                self.assertEqual(
                    prose.blockFormat().layoutDirection(),
                    Qt.LayoutDirection.RightToLeft,
                )
                self.assertEqual(
                    prose.blockFormat().alignment(),
                    Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignAbsolute,
                )
                option = popup.result.document().defaultTextOption()
                self.assertEqual(
                    option.textDirection(), Qt.LayoutDirection.RightToLeft)
                self.assertEqual(
                    option.alignment(),
                    Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignAbsolute,
                )
                self.assertGreater(prose.layout().lineCount(), 0)
                start_cursor = QTextCursor(prose)
                end_cursor = QTextCursor(prose)
                end_cursor.setPosition(prose.position() + len(prose.text()))
                self.assertGreater(
                    popup.result.cursorRect(start_cursor).x(),
                    popup.result.cursorRect(end_cursor).x(),
                )
                code = popup.result.document().find("C    G7   C")
                self.assertFalse(code.isNull())
                self.assertEqual(code.block().text(), "C    G7   C")
                self.assertEqual(
                    code.blockFormat().layoutDirection(),
                    Qt.LayoutDirection.LeftToRight,
                )
                self.assertEqual(
                    code.blockFormat().alignment(),
                    Qt.AlignmentFlag.AlignLeft,
                )

                settings["target_language"] = "fr"
                popup._render_markdown("### Titre\n\nTexte traduit")
                self.assertEqual(
                    popup.result.layoutDirection(),
                    Qt.LayoutDirection.LeftToRight,
                )
                self.assertEqual(
                    popup.result.document().begin().blockFormat().alignment(),
                    Qt.AlignmentFlag.AlignLeft,
                )
                self.assertEqual(
                    popup.result.document().begin().blockFormat().layoutDirection(),
                    Qt.LayoutDirection.LeftToRight,
                )
                self.assertEqual(
                    popup.result.document().begin().next().blockFormat().alignment(),
                    Qt.AlignmentFlag.AlignLeft,
                )

                popup.settings["target_language"] = "fr"
                popup._translation_text = ""
                popup._render_markdown("ترجمة عربية")
                self.assertEqual(
                    popup.result.layoutDirection(),
                    Qt.LayoutDirection.LeftToRight,
                )
                self.assertEqual(
                    popup.result.document().begin().blockFormat().alignment(),
                    Qt.AlignmentFlag.AlignLeft,
                )
                self.assertEqual(
                    popup.result.document().begin().blockFormat().layoutDirection(),
                    Qt.LayoutDirection.LeftToRight,
                )

                popup._translation_text = (
                    "A long translation paragraph that should use the entire "
                    "available reading width after every resize and zoom. " * 5
                )
                popup.resize(1100, 620)
                popup._render_translation()
                self.assertEqual(
                    popup.result.document().begin().blockFormat().alignment(),
                    Qt.AlignmentFlag.AlignLeft,
                )
                popup._apply_translation_zoom(1.1)
                self.app.processEvents()
                self.assertEqual(
                    popup.result.document().begin().blockFormat().alignment(),
                    Qt.AlignmentFlag.AlignLeft,
                )
                popup.resize(420, 420)
                self.app.processEvents()
                popup._render_translation()
                self.assertEqual(
                    popup.result.document().begin().blockFormat().alignment(),
                    Qt.AlignmentFlag.AlignLeft,
                )
            finally:
                popup.close()

    def test_streamed_markdown_direction_resets_when_target_language_changes(self):
        from PySide6.QtCore import Qt
        from popup.translate_popup import TranslatePopup

        with mock_chain_translate(return_value=("translation", "Test")):
            popup = TranslatePopup(
                dict(DEFAULTS, target_language="ar"), source_text="source")
            try:
                if popup._worker:
                    self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()

                popup._render_markdown("### عنوان\n\nنص عربي")
                popup.settings["target_language"] = "fr"
                popup.result.clear()
                popup._streamed_partial = ""
                popup._append_streamed_partial("### Chapitre\n\nTexte français")

                document = popup.result.document()
                self.assertEqual(
                    popup.result.layoutDirection(),
                    Qt.LayoutDirection.LeftToRight)
                self.assertEqual(
                    document.defaultTextOption().textDirection(),
                    Qt.LayoutDirection.LeftToRight)
                self.assertEqual(
                    document.begin().blockFormat().layoutDirection(),
                    Qt.LayoutDirection.LeftToRight)

                popup._render_markdown("### Chapter\n\nEnglish text")
                popup.settings["target_language"] = "ar"
                popup.result.clear()
                popup._streamed_partial = ""
                popup._append_streamed_partial("### عنوان\n\nنص عربي")

                document = popup.result.document()
                self.assertEqual(
                    popup.result.layoutDirection(),
                    Qt.LayoutDirection.RightToLeft)
                self.assertEqual(
                    document.defaultTextOption().textDirection(),
                    Qt.LayoutDirection.RightToLeft)
                self.assertEqual(
                    document.begin().blockFormat().layoutDirection(),
                    Qt.LayoutDirection.RightToLeft)
            finally:
                popup.close()

    def test_translation_headings_are_larger_than_the_body_text(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QTextCursor
        from popup.translate_popup import TranslatePopup

        settings = dict(DEFAULTS, target_language="ar", font_size=16)
        with mock_chain_translate(return_value=("ترجمة", "Test")):
            popup = TranslatePopup(settings, source_text="source")
            try:
                if popup._worker:
                    self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
                popup._render_markdown(
                    "# عنوان رئيسي\n\n"
                    "## عنوان قسم\n\n"
                    "### عنوان فرعي\n\n"
                    "### **1.2. عنوان مرقم**\n\n"
                    "**عنوان عريض مستقل**\n\n"
                    "فقرة فيها **تأكيد عريض**."
                )

                def char_format(text):
                    cursor = popup.result.document().find(text)
                    self.assertFalse(cursor.isNull(), text)
                    return cursor.charFormat()

                main_title = char_format("عنوان رئيسي")
                section_title = char_format("عنوان قسم")
                subtitle = char_format("عنوان فرعي")
                numbered_heading = char_format("1.2. عنوان مرقم")
                bold_standalone = char_format("عنوان عريض مستقل")
                inline_emphasis = char_format("تأكيد عريض")
                self.assertEqual(main_title.fontPointSize(), 20)
                self.assertEqual(section_title.fontPointSize(), 20)
                self.assertEqual(subtitle.fontPointSize(), 20)
                self.assertEqual(numbered_heading.fontPointSize(), 20)
                self.assertEqual(bold_standalone.fontPointSize(), 0)
                self.assertEqual(inline_emphasis.fontPointSize(), 0)
                self.assertGreater(main_title.fontWeight(), 400)
                self.assertGreater(section_title.fontWeight(), 400)
                self.assertLessEqual(bold_standalone.fontWeight(), 400)
                self.assertLessEqual(inline_emphasis.fontWeight(), 400)
                title_cursor = popup.result.document().find("عنوان رئيسي")
                self.assertEqual(
                    title_cursor.blockFormat().alignment(),
                    Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignAbsolute,
                )
                subtitle_cursor = popup.result.document().find("عنوان فرعي")
                self.assertEqual(
                    subtitle_cursor.blockFormat().alignment(),
                    Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignAbsolute,
                )
                self.assertEqual(
                    popup.result.document().find("1.2. عنوان مرقم")
                    .blockFormat().headingLevel(),
                    3,
                )

                level_four = "#### مستوى عميق"
                popup._render_markdown(
                    f"# عنوان\n\n{level_four}\n\nنص عادي"
                )
                deep = popup.result.document().find("مستوى عميق")
                self.assertEqual(deep.charFormat().fontPointSize(), 20)
                plain = popup.result.document().find("نص عادي")
                self.assertEqual(plain.charFormat().fontPointSize(), 0)

                popup._render_markdown(
                    '<h2 align="center">عنوان في الوسط</h2>\n\n'
                    "## عنوان بمحاذاة النص\n\n"
                    "```text\nC - G7 - C\n```"
                )
                centered = popup.result.document().find("عنوان في الوسط")
                edge_heading = popup.result.document().find("عنوان بمحاذاة النص")
                edge_music = popup.result.document().find("C - G7 - C")
                self.assertEqual(
                    centered.blockFormat().alignment(),
                    Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignAbsolute,
                )
                self.assertEqual(
                    edge_heading.blockFormat().alignment(),
                    Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignAbsolute,
                )
                self.assertEqual(
                    edge_music.blockFormat().alignment(),
                    Qt.AlignmentFlag.AlignLeft,
                )
            finally:
                popup.close()

    def test_translation_rendering_removes_bold_from_first_body_paragraph(self):
        from popup.translate_popup import TranslatePopup

        with mock_chain_translate(return_value=("Translation", "Test")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="source")
            if popup._worker:
                self.assertTrue(popup._worker.wait(20000))
            self.app.processEvents()
        try:
            popup._render_markdown(
                "#### عنوان حقيقي\n\n"
                "**الفقرة الأولى كاملة بخط عريض. جملة أخرى.**\n\n"
                '<p style="font-weight: 700">فقرة HTML عريضة.</p>'
            )
            document = popup.result.document()
            heading = document.find("عنوان حقيقي")
            first_paragraph = document.find("الفقرة الأولى")
            html_paragraph = document.find("فقرة HTML")

            self.assertGreater(heading.charFormat().fontWeight(), 400)
            self.assertLessEqual(first_paragraph.charFormat().fontWeight(), 400)
            self.assertLessEqual(html_paragraph.charFormat().fontWeight(), 400)
            self.assertEqual(heading.block().blockFormat().headingLevel(), 4)
        finally:
            popup.close()



    def test_translate_entire_page_from_thumbnail(self):
        from unittest import mock
        from main import MainWindow

        path = self._make_pdf("translate_full_page.pdf", pages=2)
        window = MainWindow()
        try:
            book = window._add_tab(path)
            self.assertIsNotNone(book)
            with mock.patch(
                    "translators.chain.translate_stream",
                    return_value=iter([("done", "ترجمة الصفحة", "Test")])) as stream:
                window.translate_page(book, 1)
                popup = window.popups[-1]
                self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
                self.assertEqual(popup.result.toPlainText(), "ترجمة الصفحة")
                self.assertTrue(popup._full_page_translation)
                self.assertTrue(
                    popup._worker._settings["_full_page_translation"])
                stream.assert_called_once()
                self.assertIn("memory page 1", stream.call_args.args[0])
        finally:
            for popup in window.popups:
                popup.close()
            window.close()

    def test_translation_popup_navigates_and_translates_adjacent_pages(self):
        from main import MainWindow

        path = self._make_pdf("translate_popup_navigation.pdf", pages=2)
        window = MainWindow()
        try:
            window.show()
            self.app.processEvents()
            book = window._add_tab(path)
            self.assertIsNotNone(book)
            self.app.processEvents()

            def translated_page(source, _settings):
                return f"translation for {source.strip()}", "Test"

            with mock_chain_translate(side_effect=translated_page):
                window.translate_selection(
                    book, 0, book.page_rect_on_screen(0))
                popup = window.popups[-1]
                self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()

                self.assertTrue(popup.previous_page_btn.isVisible())
                self.assertTrue(popup.next_page_btn.isVisible())
                self.assertEqual(popup.page_number_label.text(), "Page 1 / 2")
                self.assertTrue(popup.page_number_label.isVisible())
                self.assertGreater(
                    popup.previous_page_btn.geometry().x(),
                    popup.pin_btn.geometry().x(),
                )
                self.assertGreater(
                    popup.next_page_btn.geometry().x(),
                    popup.pin_btn.geometry().x(),
                )
                self.assertEqual(popup.navigation_page, 0)
                self.assertFalse(popup.previous_page_btn.isEnabled())
                self.assertTrue(popup.next_page_btn.isEnabled())

                popup._speech_start_offset = 12
                popup.next_page_btn.click()
                self.assertEqual(popup.navigation_page, 1)
                self.assertEqual(popup.page_number_label.text(), "Page 2 / 2")
                self.assertEqual(book.current_page(), 1)
                self.app.processEvents()
                self.assertEqual(book.current_page(), 1)
                self.assertEqual(popup.navigation_page, 1)
                self.assertFalse(popup.next_page_btn.isEnabled())
                self.assertTrue(popup.previous_page_btn.isEnabled())
                self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
                self.assertEqual(window.popups[-1], popup)
                self.assertIn("memory page 1", popup._source_text)
                self.assertEqual(popup._speech_start_offset, 0)
                from unittest import mock
                popup._speech_available = True
                with mock.patch.object(popup, "_request_speech") as request:
                    popup._toggle_speech()
                request.assert_called_once_with(0)

                popup._speech_start_offset = 12
                popup.previous_page_btn.click()
                self.app.processEvents()
                self.assertEqual(book.current_page(), 0)
                self.assertEqual(popup.navigation_page, 0)
                self.assertEqual(popup.page_number_label.text(), "Page 1 / 2")
                self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
                self.assertIn("memory page 0", popup._source_text)
                self.assertEqual(popup._speech_start_offset, 0)
        finally:
            for popup in window.popups:
                popup.close()
            window.close()

    def test_page_counter_starts_and_stops_automatic_page_reading(self):
        from unittest import mock
        from main import MainWindow

        path = self._make_pdf(
            "automatic_page_reading.pdf", pages=3)
        window = MainWindow()
        try:
            book = window._add_tab(path)
            self.assertIsNotNone(book)
            with mock_chain_translate(
                return_value=("initial translation", "Test")
            ):
                window.translate_page(book, 1)
                popup = window.popups[-1]
                self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()

            self.assertTrue(popup.page_number_label.isCheckable())
            self.assertEqual(popup.page_number_label.text(), "Page 2 / 3")
            calls = []

            def record_page_translation(
                source_book, page_no, _rect, force_ocr, popup=None,
                omit_running_header=False, start_automatic_reading=False,
            ):
                calls.append(
                    (page_no, force_ocr, popup, omit_running_header))
                popup.set_page_navigation(page_no, source_book.page_count())

            with (
                mock.patch.object(
                    window, "_translate_selection",
                    side_effect=record_page_translation),
                mock.patch.object(book, "page_needs_full_page_ocr",
                                 return_value=False),
                mock.patch.object(popup, "_toggle_speech") as speak,
            ):
                popup.page_number_label.click()
                self.assertTrue(popup.page_number_label.isChecked())
                self.assertEqual(calls, [(1, False, popup, True)])
                self.assertTrue(popup.previous_page_btn.isEnabled())
                self.assertTrue(popup.next_page_btn.isEnabled())

                popup.translationReady.emit()
                speak.assert_called_once_with()
                popup.speechFinished.emit("")
                self.app.processEvents()
                self.assertEqual(calls[-1], (2, False, popup, True))
                self.assertEqual(popup.page_number_label.text(), "Page 3 / 3")
                self.assertTrue(popup.page_number_label.isChecked())

                popup.translationReady.emit()
                speak.reset_mock()
                popup.speechFinished.emit("")
                self.app.processEvents()
                self.assertTrue(popup.page_number_label.isChecked())
                self.assertIs(window._automatic_page_reading_popup, popup)
                self.assertTrue(popup.previous_page_btn.isEnabled())
                self.assertFalse(popup.next_page_btn.isEnabled())

                popup.previous_page_btn.click()
                self.assertEqual(popup.navigation_page, 1)
                self.assertEqual(calls[-1], (1, False, popup, True))
                self.assertFalse(window._automatic_page_reading_at_end)
                self.assertTrue(popup.page_number_label.isChecked())
                self.assertTrue(popup.next_page_btn.isEnabled())

                popup.page_number_label.click()
                self.assertFalse(popup.page_number_label.isChecked())
                self.assertIsNone(window._automatic_page_reading_popup)

            self.assertEqual(
                [page for page, _force_ocr, _popup, _omit in calls],
                [1, 2, 1])
        finally:
            for popup in window.popups:
                popup.close()
            window.close()

    def test_context_successive_reading_starts_with_saved_reading_settings(self):
        from unittest import mock
        from main import MainWindow

        path = self._make_pdf(
            "context_successive_reading.pdf", pages=2)
        window = MainWindow()
        try:
            window.settings["prefetch_next_page_translation"] = True
            window.settings["automatic_page_reading_time_enabled"] = True
            window.settings["automatic_page_reading_time_minutes"] = 75
            book = window._add_tab(path)
            self.assertIsNotNone(book)

            with mock_chain_translate(
                return_value=("first page translation", "Test")
            ):
                window.translate_page(book, 0, successive_reading=True)
                popup = window.popups[-1]
                self.assertTrue(popup.page_number_label.isChecked())
                self.assertIs(window._automatic_page_reading_popup, popup)
                self.assertEqual(window._automatic_page_reading_page, 0)

                worker = popup._worker
                self.assertIsNotNone(worker)
                self.assertTrue(worker.wait(20000))
                popup._speech_available = True
                with mock.patch.object(popup, "_toggle_speech") as speak:
                    self.app.processEvents()

                self.assertEqual(popup._translation_text,
                                 "first page translation")
                speak.assert_called_once_with()
                self.assertTrue(
                    window._automatic_page_reading_time_timer.isActive())
                self.assertEqual(
                    window._automatic_page_reading_time_timer.interval(),
                    4_500_000)
                self.assertTrue(
                    window._automatic_page_prefetch_timer.isActive())
        finally:
            for popup in window.popups:
                popup.close()
            window.close()

    def test_automatic_page_reading_uses_prefetched_translation(self):
        from unittest import mock
        from main import MainWindow

        path = self._make_pdf("prefetched_page_translation.pdf", pages=2)
        window = MainWindow()
        try:
            window.settings["prefetch_next_page_translation"] = True
            book = window._add_tab(path)
            self.assertIsNotNone(book)
            with mock_chain_translate(
                return_value=("initial translation", "Test")
            ):
                window.translate_page(book, 0)
                popup = window.popups[-1]
                self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()

            window._automatic_page_reading_book = book
            window._automatic_page_reading_popup = popup
            window._automatic_page_reading_page = 0
            window._automatic_page_waiting_for_speech = True
            with (
                mock.patch.object(
                    popup, "prefetch_speech_markdown") as speech_prefetch,
                mock_chain_translate(
                    return_value=("prefetched page translation", "Test")
                ),
            ):
                window._start_automatic_page_prefetch(book, popup, 0)
                worker = window._automatic_page_prefetch_worker
                self.assertIsNotNone(worker)
                self.assertTrue(worker.wait(20000))
                self.app.processEvents()
                speech_prefetch.assert_called_once_with(
                    "prefetched page translation")

            prefetched = window._automatic_page_prefetch_result
            self.assertIsNotNone(prefetched)
            self.assertEqual(prefetched[1], 1)
            self.assertEqual(prefetched[2], "memory page 1")
            window._automatic_page_waiting_for_speech = False
            with (
                mock.patch.object(
                    window, "_translate_selection") as translate,
                mock.patch.object(popup, "_toggle_speech"),
                mock.patch.object(
                    window, "_start_automatic_page_prefetch"),
            ):
                window._translate_automatic_page(book, popup, 1)
                self.app.processEvents()

            translate.assert_not_called()
            self.assertEqual(popup.navigation_page, 1)
            self.assertEqual(
                popup._translation_text, "prefetched page translation")
            self.assertIsNone(window._automatic_page_prefetch_result)
        finally:
            for popup in window.popups:
                popup.close()
            window.close()

    def test_prefetches_upcoming_speech_during_current_page_read(self):
        import time
        from unittest import mock
        from popup import speech_worker
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        with mock_chain_translate(return_value=("current page", "Test")):
            popup = TranslatePopup(dict(DEFAULTS), source_text="source")
        try:
            if popup._worker is not None:
                popup._worker.wait(20000)
            self.app.processEvents()
            popup._speech_available = True
            popup._speech_active = True
            markdown = "الصفحة **التالية**"
            calls = []

            class FakeCommunicate:
                def __init__(self, text, *_args, **_kwargs):
                    calls.append(text)
                    self.text = text

                async def stream(self):
                    yield {"type": "audio", "data": b"prefetched-mp3"}
                    for word in self.text.split():
                        yield {
                            "type": "WordBoundary",
                            "offset": 0,
                            "duration": 1_000_000,
                            "text": word,
                        }

            fake_edge = type(
                "Edge", (), {"Communicate": FakeCommunicate})
            with tempfile.TemporaryDirectory(
                prefix="harmony-next-page-tts-"
            ) as cache_dir:
                with mock.patch.dict(
                    os.environ, {"HARMONY_TTS_CACHE": cache_dir}
                ), mock.patch.object(speech_worker, "edge_tts", fake_edge):
                    popup.prefetch_speech_markdown(markdown)
                    for _ in range(500):
                        self.app.processEvents()
                        if popup._speech_prefetch_worker is None and calls:
                            break
                        time.sleep(0.02)
                    self.assertEqual(len(calls), 1)
                    self.assertTrue(popup._speech_active)
                    self.assertTrue(popup._player.source().isEmpty())

                    popup._render_markdown(markdown)
                    spoken_text = popup._speech_text_from_document(
                        popup.result.document())
                    with mock.patch.object(
                        popup, "_play_speech_segment") as play_cached:
                        self.assertTrue(
                            popup._try_play_cached_full_text(spoken_text))
                        play_cached.assert_called_once()
                    self.assertEqual(len(calls), 1)
        finally:
            popup._speech_active = False
            popup._player.stop()
            popup.close()

    def test_automatic_page_reading_time_stops_session_after_selected_duration(self):
        from unittest import mock
        from main import MainWindow

        window = MainWindow()
        try:
            popup = mock.Mock(
                navigation_page=0,
                _translation_text="Page translation",
                _speech_available=True,
                _speech_active=True,
            )
            book = mock.Mock()
            book.page_count.return_value = 2
            window.settings["automatic_page_reading_time_enabled"] = True
            window.settings["automatic_page_reading_time_minutes"] = 70
            window._automatic_page_reading_book = book
            window._automatic_page_reading_popup = popup
            window._automatic_page_reading_page = 0

            with mock.patch("main.QTimer.singleShot"):
                window._on_automatic_page_translation_ready(popup)
                timer = window._automatic_page_reading_time_timer
                self.assertTrue(timer.isActive())
                self.assertEqual(timer.interval(), 4_200_000)

                window._on_automatic_page_speech_finished(popup, "")
                self.assertTrue(timer.isActive())

            window._automatic_page_reading_elapsed = mock.Mock()
            window._automatic_page_reading_elapsed.elapsed.return_value = (
                4_200_000)
            with mock.patch.object(window, "_flash_info") as flash:
                window._check_automatic_page_reading_time()

            self.assertIsNone(window._automatic_page_reading_popup)
            self.assertFalse(timer.isActive())
            popup.set_automatic_page_reading.assert_called_with(False)
            flash.assert_called_once_with(
                "Automatic page reading stopped after its time limit.")
        finally:
            window.close()

    def test_automatic_page_prefetch_waits_before_request_and_for_speech_worker(self):
        from unittest import mock
        from main import MainWindow

        path = self._make_pdf("delayed_page_prefetch.pdf", pages=3)
        window = MainWindow()
        try:
            window.settings["prefetch_next_page_translation"] = True
            book = window._add_tab(path)
            self.assertIsNotNone(book)
            with mock_chain_translate(
                return_value=("initial translation", "Test")
            ):
                window.translate_page(book, 0)
                popup = window.popups[-1]
                self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()

            window._automatic_page_reading_book = book
            window._automatic_page_reading_popup = popup
            window._automatic_page_reading_page = 0
            popup._speech_available = True
            with (
                mock.patch.object(popup, "_toggle_speech"),
                mock.patch.object(
                    window, "_start_automatic_page_prefetch") as prefetch,
            ):
                timer = window._automatic_page_prefetch_timer
                speech_worker = mock.Mock()
                speech_worker.isRunning.return_value = True
                popup._speech_worker = speech_worker
                for page_no in (0, 1):
                    speech_worker.isRunning.return_value = True
                    window._automatic_page_reading_page = page_no
                    popup.set_page_navigation(page_no, book.page_count())
                    window._on_automatic_page_translation_ready(popup)

                    self.assertEqual(timer.interval(), 5000)
                    self.assertTrue(timer.isActive())
                    self.assertEqual(
                        window._automatic_page_prefetch_scheduled,
                        (book, popup, page_no))

                    timer.stop()
                    window._start_scheduled_automatic_page_prefetch()
                    self.assertEqual(timer.interval(), 250)
                    self.assertTrue(timer.isActive())
                    self.assertEqual(prefetch.call_count, page_no)

                    speech_worker.isRunning.return_value = False
                    timer.stop()
                    window._start_scheduled_automatic_page_prefetch()
                    prefetch.assert_called_with(book, popup, page_no)
                self.assertIsNone(
                    window._automatic_page_prefetch_scheduled)
        finally:
            window._automatic_page_prefetch_timer.stop()
            for popup in window.popups:
                popup.close()
            window.close()

    def test_automatic_page_prefetch_input_handles_ocr_and_image_pages(self):
        from unittest import mock
        from PySide6.QtCore import QRect
        from main import MainWindow

        window = MainWindow()
        book = window._add_tab(
            self._make_pdf("prefetch_input_modes.pdf", pages=1))
        self.assertIsNotNone(book)
        try:
            image = object()
            pil_image = object()
            prepared_image = object()
            with (
                mock.patch.object(
                    book, "page_needs_full_page_ocr", return_value=True),
                mock.patch.object(
                    book, "render_page_image", return_value=image
                ) as render,
                mock.patch("main.ocr.is_available", return_value=True),
                mock.patch(
                    "main.pdf_utils.qimage_to_pil", return_value=pil_image),
                mock.patch(
                    "main.ocr.prepare_image",
                    return_value=prepared_image),
            ):
                self.assertEqual(
                    window._automatic_page_translation_input(
                        book, 0, QRect()),
                    (None, prepared_image, None),
                )
                render.assert_called_once_with(0, dpi=300)

            window.settings["selection_mode"] = "image"
            with (
                mock.patch.object(
                    book, "page_needs_full_page_ocr", return_value=False),
                mock.patch.object(
                    book, "render_page_image", return_value=image
                ) as render,
                mock.patch(
                    "main.pdf_utils.qimage_to_pil", return_value=pil_image),
                mock.patch("main.ocr.prepare_image") as prepare,
            ):
                self.assertEqual(
                    window._automatic_page_translation_input(
                        book, 0, QRect()),
                    (None, pil_image, ()),
                )
                render.assert_called_once_with(0, dpi=300)
                prepare.assert_not_called()
        finally:
            window.close()

    def test_translate_full_page_uses_ocr_for_scan_with_sparse_text_layer(self):
        from unittest import mock
        from main import MainWindow

        window = MainWindow()
        book = window._add_tab(
            self._make_pdf("translate_scan_full_page.pdf", pages=1))
        self.assertIsNotNone(book)
        try:
            with mock.patch.object(
                    book, "page_needs_full_page_ocr", return_value=True):
                with mock.patch.object(
                        book, "render_page_image", return_value="page-image"
                ) as render_page:
                    with mock.patch(
                            "main.ocr.is_available", return_value=True):
                        with mock.patch(
                                "main.pdf_utils.qimage_to_pil",
                                return_value="pil-page"):
                            with mock.patch(
                                    "main.ocr.prepare_image",
                                    return_value="prepared-page"):
                                with mock.patch.object(
                                        window, "_show_popup") as show_popup:
                                    window.translate_page(book, 0)

            render_page.assert_called_once_with(0, dpi=300)
            show_popup.assert_called_once_with(
                text=None,
                pil_image="prepared-page",
                ocr_langs=None,
                book=book,
                page_no=0,
            )
        finally:
            window.close()

    def test_translate_full_page_omits_running_header_and_keeps_body(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        import pymupdf
        from main import MainWindow

        path = str(Path(tempfile.gettempdir()) / "translate_long_full_page.pdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text(
            (36, 42), "PAGE_TOP_HEADER", fontname="tiit", fontsize=6)
        page.insert_text((540, 42), "81", fontsize=8)
        for line in range(55):
            page.insert_text(
                (36, 70 + line * 13),
                f"Page line {line:02d}: complete harmony textbook content.",
                fontsize=8,
            )
        page.insert_text((36, 820), "PAGE_BOTTOM_SENTINEL", fontsize=8)
        doc.save(path)
        doc.close()

        window = MainWindow()
        try:
            book = window._add_tab(path)
            self.assertIsNotNone(book)
            with mock.patch(
                    "translators.chain.translate_stream",
                    return_value=iter([("done", "complete translation", "Test")])) as stream:
                window.translate_page(book, 0)
                popup = window.popups[-1]
                self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
                source = stream.call_args.args[0]
                self.assertNotIn("PAGE_TOP_HEADER", source)
                self.assertIn("Page line 00:", source)
                self.assertIn("Page line 54:", source)
                self.assertIn("PAGE_BOTTOM_SENTINEL", source)
                self.assertIn("<small>81</small>", source)
                self.assertEqual(popup.result.toPlainText(), "complete translation")
                self.assertEqual(popup.page_number_label.text(), "Page 1 / 1")
                self.assertNotIn("Page 1 / 1", popup._current_speech_text())
        finally:
            for popup in window.popups:
                popup.close()
            window.close()

    def test_extract_selected_pages_to_separate_pdf(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        import pymupdf
        from main import MainWindow

        source = self._make_pdf("extract_pages_source.pdf", pages=6)
        output = str(Path(tempfile.gettempdir()) / "extract_pages_result.pdf")
        Path(output).unlink(missing_ok=True)
        window = MainWindow()
        try:
            book = window._add_tab(source)
            self.assertIsNotNone(book)
            with mock.patch(
                    "PySide6.QtWidgets.QFileDialog.getSaveFileName",
                    return_value=(output, "PDF files (*.pdf)")):
                window.extract_pages(book, [5, 1, 3, 1])
            extracted = pymupdf.open(output)
            try:
                self.assertEqual(len(extracted), 3)
                self.assertEqual(
                    [page.get_text().strip() for page in extracted],
                    ["memory page 1", "memory page 3", "memory page 5"],
                )
            finally:
                extracted.close()
        finally:
            window.close()
            Path(output).unlink(missing_ok=True)

    def test_copy_page_image_to_clipboard(self):
        from main import MainWindow

        path = self._make_pdf("copy_page_image.pdf", pages=2)
        window = MainWindow()
        try:
            book = window._add_tab(path)
            self.assertIsNotNone(book)
            window.copy_page_image(book, 1)
            image = self.app.clipboard().image()
            self.assertFalse(image.isNull())
            self.assertGreater(image.width(), 500)
            self.assertGreater(image.height(), 500)
        finally:
            window.close()

    def test_right_click_in_main_window_opens_refresh_menu_handler(self):
        from unittest import mock
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtGui import QContextMenuEvent
        from main import MainWindow

        window = MainWindow()
        try:
            event = QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, QPoint(4, 5), QPoint(40, 50)
            )
            with mock.patch.object(window, "_show_app_context_menu") as show_menu:
                handled = window._thumbs_wheel_filter.eventFilter(
                    window.centralWidget(), event
                )
            self.assertTrue(handled)
            show_menu.assert_called_once_with(QPoint(40, 50))
            tab_event = QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, QPoint(2, 2), QPoint(30, 30)
            )
            self.assertFalse(window._thumbs_wheel_filter.eventFilter(
                window.tabs.tabBar(), tab_event
            ))
            book = window._add_tab(_make_pdf_file("context_menu_filter.pdf"))
            self.assertIsNotNone(book)
            window.show()
            window.thumbs_action.setChecked(True)
            self.app.processEvents()
            thumbnail_event = QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, QPoint(4, 5), QPoint(40, 50)
            )
            with mock.patch.object(window, "_show_app_context_menu") as show_menu:
                for target in (
                    book.thumbs.list,
                    book.thumbs.list.viewport(),
                ):
                    handled = window._thumbs_wheel_filter.eventFilter(
                        target, thumbnail_event)
                    self.assertFalse(handled)
                show_menu.assert_not_called()
        finally:
            window.close()

    def test_refresh_reapplies_settings_and_refreshes_books_in_place(self):
        from unittest import mock
        from main import MainWindow

        window = MainWindow()
        try:
            window.show()
            book = window._add_tab(_make_pdf_file("refresh_in_place.pdf"))
            self.assertIsNotNone(book)
            refreshed_settings = dict(window.settings)
            refreshed_settings.update(theme="Pure Light", thumbs_visible=True)
            with mock.patch("main.load", return_value=refreshed_settings):
                with mock.patch.object(book, "refresh_content") as refresh_book:
                    with mock.patch.object(window, "close") as close:
                        window._refresh_application()
            self.assertFalse(window._restart_requested)
            self.assertTrue(window.isVisible())
            self.assertIs(window.current_book(), book)
            self.assertTrue(window.thumbs_action.isChecked())
            self.assertEqual(window.settings["theme"], "Pure Light")
            refresh_book.assert_called_once_with()
            close.assert_not_called()
        finally:
            window.close()

    def test_reopen_requests_restart_and_launches_replacement(self):
        from unittest import mock
        from main import MainWindow

        window = MainWindow()
        try:
            window.show()
            with mock.patch.object(window, "close") as close:
                window._reopen_application()
                self.app.processEvents()
            self.assertTrue(window._restart_requested)
            close.assert_called_once_with()

            window._shutdown_confirmed = True
            with mock.patch("main.subprocess.Popen") as popen:
                self.assertTrue(window.close())

            popen.assert_called_once()
            command = popen.call_args.args[0]
            self.assertEqual(Path(command[-1]), Path(__file__).parents[1] / "main.py")
            self.assertTrue(window._closing)
            self.assertFalse(window.isVisible())
        finally:
            window._restart_requested = False
            try:
                window.close()
            except RuntimeError:
                pass

    def test_open_remembers_path_and_fits_with_centered_side_margins(self):
        import time
        from main import MainWindow
        from config.settings import load
        tmp = self._make_pdf("mem_test.pdf")
        win = MainWindow()
        try:
            win.show()
            win.open_paths([tmp])
            book = win.current_book()
            self.assertIsNotNone(book)
            self.assertFalse(win.thumbs_action.isChecked())
            self.assertFalse(book.thumbs.isVisible())
            self.assertEqual(book.thumbs.list.count(), 0)
            t0 = time.time()
            while time.time() - t0 < 8:
                self.app.processEvents()
                time.sleep(0.05)
                vw = book.reader.viewport().width()
                if vw > 50 and abs(book.zoom_percent - 100.0) > 1:
                    break
            vw = book.reader.viewport().width()
            margin = round(book.reader.logicalDpiX() / 2.54)
            expected = max(20.0, min(1000.0, (vw - 2 * margin) / 595.27 * 100))
            self.assertAlmostEqual(book.zoom_percent, expected, delta=5.0)
            page = book.reader._labels[0]
            page_x = book.reader.viewport().mapFromGlobal(
                page.mapToGlobal(page.rect().topLeft())).x()
            self.assertAlmostEqual(
                page_x + page.width() / 2, vw / 2, delta=2.0,
                msg=(f"page_x={page_x} width={page.width()} viewport={vw} "
                     f"container={book.reader._container.geometry().getRect()} "
                     f"page_geo={page.geometry().getRect()} "
                     f"layout_item={book.reader._layout.itemAt(0).geometry().getRect()} "
                     f"hscroll={book.reader.horizontalScrollBar().value()}/"
                     f"{book.reader.horizontalScrollBar().maximum()}"))
            self.assertAlmostEqual(
                (vw - page.width()) / 2, margin, delta=5.0)
            win.thumbs_action.setChecked(True)
            self.app.processEvents()
            self.assertEqual(book.thumbs.list.count(), book.page_count())
            win.thumbs_action.setChecked(False)
            book.set_zoom_percent(150)
            self.assertEqual(book.zoom_percent, 150)
            win._close_tab(0)
            self.assertEqual(win.tabs.count(), 0)
            saved = load()
            self.assertEqual(saved["last_pdf"], tmp)
            self.assertEqual(saved["last_dir"], os.path.dirname(tmp))
        finally:
            try:
                win.close()
            except RuntimeError:
                pass

    def test_fit_menu_offers_all_zoom_modes(self):
        from main import MainWindow
        window = MainWindow()
        try:
            self.assertEqual(
                [action.text() for action in window.fit_menu.actions()],
                ["Actual Size", "Fit Page", "Fit Width", "Fit Visible", "Zoom To…"])
        finally:
            window.close()

    def test_thumbnail_bookmark_button_toggles_current_page(self):
        from main import MainWindow
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QToolBar, QWidget

        path = _make_pdf_file("thumbnail_bookmark.pdf", pages=3)
        window = MainWindow()
        try:
            window.show()
            book = window._add_tab(path)
            self.assertIsNotNone(book)
            window.thumbs_action.setChecked(True)
            self.app.processEvents()
            self.assertTrue(book.thumbs.bookmark_button.isEnabled())
            rail = book.thumbs.findChild(QWidget, "bookmarkRail")
            self.assertEqual(rail.width(), 20)
            self.assertIs(rail.layout().itemAt(0).widget(),
                          book.thumbs.bookmark_button)
            self.assertIs(rail.layout().itemAt(1).widget(),
                          book.thumbs.bookmarks_button)
            self.assertEqual(book.thumbs.bookmark_button.accessibleName(),
                             "Save bookmark")
            self.assertEqual(book.thumbs.bookmarks_button.accessibleName(),
                             "My bookmarks")
            book.thumbs.bookmark_button.click()
            self.assertIn(0, book.bookmarked_pages)
            self.assertTrue(book.thumbs.bookmark_button.isChecked())
            self.assertEqual(book.thumbs.bookmark_button.text(), "★")
            self.assertEqual(book.thumbs.list.item(0).text(), "1")
            self.assertEqual(book.thumbs.bookmarks_list.count(), 1)
            self.assertEqual(
                book.thumbs.bookmarks_list.item(0).data(
                    Qt.ItemDataRole.UserRole),
                0,
            )
            book.thumbs.bookmark_button.click()
            self.assertNotIn(0, book.bookmarked_pages)
            self.assertFalse(book.thumbs.bookmark_button.isChecked())
            self.assertEqual(book.thumbs.list.item(0).text(), "1")
            self.assertEqual(book.thumbs.bookmarks_list.count(), 1)
            self.assertEqual(
                book.thumbs.bookmarks_list.item(0).text(),
                "No bookmarks yet",
            )
        finally:
            window.close()

    def test_my_bookmarks_pane_shows_thumbnails_and_jumps_to_page(self):
        import time
        from PySide6.QtCore import Qt
        from main import MainWindow

        path = _make_pdf_file("my_bookmarks_navigation.pdf", pages=3)
        window = MainWindow()
        try:
            window.show()
            book = window._add_tab(path)
            self.assertIsNotNone(book)
            window.thumbs_action.setChecked(True)
            self.app.processEvents()
            book.set_bookmarks([2])
            book.thumbs.bookmarks_button.click()
            self.assertEqual(
                book.thumbs.pages.currentWidget(),
                book.thumbs.bookmarks_list,
            )
            self.assertEqual(book.thumbs.bookmarks_list.count(), 1)
            bookmark = book.thumbs.bookmarks_list.item(0)
            self.assertEqual(bookmark.text(), "3")
            self.assertFalse(bookmark.icon().isNull())
            self.assertEqual(
                bookmark.data(Qt.ItemDataRole.UserRole),
                2,
            )
            worker = book.thumbs._worker
            deadline = time.time() + 2
            while worker is not None and worker.isRunning() and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            if worker is not None:
                self.assertFalse(worker.isRunning())
            self.assertEqual(
                bookmark.icon().cacheKey(),
                book.thumbs.list.item(2).icon().cacheKey(),
            )
            book.thumbs.bookmarks_list.itemClicked.emit(bookmark)
            deadline = time.time() + 2
            while book.current_page() != 2 and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertEqual(book.current_page(), 2)
            self.assertIs(book.thumbs.pages.currentWidget(), book.thumbs.list)
        finally:
            window.close()

    def test_main_window_fixed_centered_geometry(self):
        from main import MAIN_W, MAIN_H, MainWindow
        win = MainWindow()
        try:
            self.assertEqual((win.width(), win.height()), (MAIN_W, MAIN_H))
        finally:
            try:
                win.close()
            except RuntimeError:
                pass

    def test_zoom_buttons_are_adjacent_in_toolbar(self):
        from PySide6.QtWidgets import QToolBar
        from main import MainWindow

        win = MainWindow()
        try:
            toolbars = win.findChildren(QToolBar)
            self.assertEqual(len(toolbars), 1)
            toolbar = toolbars[0]
            labels = [action.text() for action in toolbar.actions()]
            zoom_in = labels.index("🔍  Zoom +")
            zoom_out = labels.index("🔍  Zoom −")
            self.assertEqual(abs(zoom_in - zoom_out), 1)
        finally:
            try:
                win.close()
            except RuntimeError:
                pass

    def test_theme_selector_applies_and_saves_theme(self):
        from PySide6.QtTest import QTest
        from main import MainWindow, THEMES
        from config.settings import load
        from ui.theme import theme_colors
        win = MainWindow()
        try:
            self.assertEqual(win.settings["theme"], "Dark Slate")
            self.assertIn(
                theme_colors("Dark Slate", win.settings["color_scheme"])["accent"],
                self.app.styleSheet(),
            )
            self.assertIsNotNone(win.theme_button.parentWidget())
            self.assertEqual(
                [action.text() for action in win.theme_menu.actions()],
                list(THEMES))
            self.assertGreaterEqual(len(win.theme_menu.actions()), 16)
            self.assertIn("min-height: 32px", self.app.styleSheet())
            win._theme_actions["Midnight Blue"].trigger()
            QTest.qWait(win.APP_STYLESHEET_COALESCE_MS + 20)
            self.assertEqual(win.settings["theme"], "Midnight Blue")
            self.assertIn(
                theme_colors("Midnight Blue", win.settings["color_scheme"])["accent"],
                self.app.styleSheet(),
            )
            self.assertEqual(load()["theme"], "Midnight Blue")
            win._theme_actions["Pure Light"].trigger()
            QTest.qWait(win.APP_STYLESHEET_COALESCE_MS + 20)
            self.assertEqual(win.settings["theme"], "Pure Light")
            self.assertEqual(win.settings["color_scheme"], "light")
            self.assertEqual(load()["color_scheme"], "light")
            self.assertIn(
                theme_colors("Pure Light", "light")["accent"],
                self.app.styleSheet(),
            )
            win._theme_actions["Nordic Gray"].trigger()
            QTest.qWait(win.APP_STYLESHEET_COALESCE_MS + 20)
            self.assertIn(
                theme_colors("Nordic Gray", win.settings["color_scheme"])["accent"],
                self.app.styleSheet(),
            )
        finally:
            try:
                win.close()
            except RuntimeError:
                pass

    def test_system_color_scheme_change_updates_main_window_and_popups(self):
        from unittest import mock
        from main import MainWindow, app_stylesheet

        win = MainWindow()
        popup = mock.Mock()
        popup.isVisible.return_value = True
        win.popups = [popup]
        win.settings["color_scheme"] = "system"
        try:
            win._on_system_color_scheme_changed()

            self.assertEqual(
                self.app.styleSheet(),
                app_stylesheet(win.settings["theme"], "system"),
            )
            popup.refresh_display.assert_called_once_with(win.settings)
        finally:
            try:
                win.close()
            except RuntimeError:
                pass

    def test_pdf_canvas_theme_setting_reloads_from_shared_settings(self):
        from unittest import mock
        from PySide6.QtTest import QTest
        from main import MainWindow, app_stylesheet

        win = MainWindow()
        try:
            updated = dict(
                win.settings,
                pdf_canvas_theme_background=True,
            )
            with mock.patch("main.load", return_value=updated):
                win._reload_settings()
            QTest.qWait(win.APP_STYLESHEET_COALESCE_MS + 20)

            self.assertTrue(win.settings["pdf_canvas_theme_background"])
            self.assertEqual(
                self.app.styleSheet(),
                app_stylesheet(
                    win.settings["theme"],
                    win.settings["color_scheme"],
                    True,
                ),
            )
        finally:
            try:
                win.close()
            except RuntimeError:
                pass

    def test_reused_settings_dialog_is_rebuilt_after_external_settings_change(self):
        from unittest import mock
        from main import MainWindow

        win = MainWindow()
        try:
            cached_dialog = mock.Mock()
            cached_dialog.values.return_value = dict(
                win.settings, theme="Dark Slate")
            cached_dialog._tts_voice_list_worker = None
            replacement = mock.Mock()
            replacement.exec.return_value = False
            win._settings_dialog = cached_dialog
            win.settings["theme"] = "Everforest"

            with mock.patch("main.SettingsDialog", return_value=replacement) as factory:
                win.open_settings()

            factory.assert_called_once_with(win.settings, win)
            cached_dialog.deleteLater.assert_called_once_with()
            self.assertIs(win._settings_dialog, replacement)
        finally:
            try:
                win.close()
            except RuntimeError:
                pass

    def test_popup_keeps_fixed_geometry_after_result(self):
        import threading
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS
        gate = threading.Event()

        def slow_translate(text, settings):
            gate.wait(15)
            return ("نص", "T")

        with mock_chain_translate(side_effect=slow_translate):
            pop = TranslatePopup(dict(DEFAULTS), source_text="hello")
            try:
                pop.show()
                pop.resize(640, 420)
                self.app.processEvents()
                self.assertTrue(pop.result.isVisible())
                gate.set()
                if pop._worker and not pop._worker.wait(20000):
                    self.fail("worker did not finish")
                for _ in range(20):
                    self.app.processEvents()
                self.assertTrue(pop.result.isVisible())
                # no snap-back: the user's own size survives the result
                self.assertEqual((pop.width(), pop.height()), (640, 420))
            finally:
                try:
                    pop.close()
                except RuntimeError:
                    pass

    def test_popup_fixed_geometry_always(self):
        import ctypes
        from unittest import mock
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        def make_shown():
            pop = TranslatePopup(dict(DEFAULTS), source_text="hello")
            pop.show()
            self.app.processEvents()
            return pop

        def fake_wm_factory(calls):
            def fake_wm(hwnd, after, x, y, w, h, flags):
                calls.append((x, y, w, h, flags))
                return 1
            return fake_wm

        with mock_chain_translate(return_value=("نص", "T")):
            pop = make_shown()
            try:
                calls = []
                with mock.patch.object(ctypes.windll, "user32") as u32:
                    u32.SetWindowPos.side_effect = fake_wm_factory(calls)
                    pop._apply_fixed_outer()
                # asks Windows for an on-screen outer geometry
                self.assertEqual(calls[0][:4], pop._target_outer_geometry())
                pop.close()
            except Exception:
                try:
                    pop.close()
                except RuntimeError:
                    pass
                raise
        # Reopen -> same request again, never the user's size
        with mock_chain_translate(return_value=("نص", "T")):
            pop2 = make_shown()
            try:
                calls = []
                with mock.patch.object(ctypes.windll, "user32") as u32:
                    u32.SetWindowPos.side_effect = fake_wm_factory(calls)
                    pop2._apply_fixed_outer()
                self.assertEqual(calls[0][:4], pop2._target_outer_geometry())
            finally:
                try:
                    pop2.close()
                except RuntimeError:
                    pass

    def test_parse_zoom_text(self):
        from main import MainWindow
        self.assertEqual(MainWindow.parse_zoom_text("100%"), 100.0)
        self.assertEqual(MainWindow.parse_zoom_text(" 125 "), 125.0)
        self.assertEqual(MainWindow.parse_zoom_text("87.5%"), 87.5)
        self.assertEqual(MainWindow.parse_zoom_text("500%"), 500.0)
        self.assertEqual(MainWindow.parse_zoom_text("1000%"), 1000.0)
        self.assertIsNone(MainWindow.parse_zoom_text("abc"))
        self.assertIsNone(MainWindow.parse_zoom_text("10%"))
        self.assertIsNone(MainWindow.parse_zoom_text("1200%"))
        self.assertIsNone(MainWindow.parse_zoom_text(""))

    def test_source_and_target_language_combos_persist(self):
        from main import MainWindow
        from config.settings import DEFAULTS, load, save
        s = dict(DEFAULTS)
        s["source_language"] = "auto"
        s["target_language"] = "ar"
        save(s)
        win = MainWindow()
        try:
            self.assertEqual(win.source_combo.currentData(), "auto")
            self.assertEqual(win.target_combo.currentData(), "ar")
            win.source_combo.setCurrentIndex(win.source_combo.findData("en"))
            win.target_combo.setCurrentIndex(win.target_combo.findData("it"))
            self.assertEqual(win.settings["source_language"], "en")
            self.assertEqual(win.settings["target_language"], "it")
            self.assertEqual(load()["source_language"], "en")
            self.assertEqual(load()["target_language"], "it")
            self.assertEqual(MainWindow._ocr_languages_for("auto"), None)
            self.assertEqual(MainWindow._ocr_languages_for("it"), ("it", "en"))
        finally:
            try:
                win.close()
            except RuntimeError:
                pass

    def test_auto_read_uses_oneocr_for_multilingual_text(self):
        from unittest import mock
        from ocr_providers import base, manager
        from translators import ocr

        result = base.OcrResult(text="La cadenza", backend="OneOCR")
        provider = mock.Mock()
        provider.read.return_value = result
        image = object()
        with mock.patch.object(manager, "get_provider", return_value=provider):
            text, langs = ocr.auto_read(image)
        self.assertEqual(text, "La cadenza")
        self.assertEqual(langs, ("auto",))
        provider.read.assert_called_once_with(image)

    def test_language_combos_offer_requested_languages_and_default_values(self):
        from main import MainWindow
        from translators.prompt import LANGUAGE_CHOICES

        win = MainWindow()
        try:
            self.assertEqual(win.source_combo.currentData(), "auto")
            self.assertEqual(win.target_combo.currentData(), "ar")
            self.assertEqual(
                [win.source_combo.itemData(i)
                 for i in range(1, win.source_combo.count())],
                [code for _, code in LANGUAGE_CHOICES])
            self.assertEqual(
                [win.target_combo.itemData(i)
                 for i in range(win.target_combo.count())],
                [code for _, code in LANGUAGE_CHOICES])
        finally:
            try:
                win.close()
            except RuntimeError:
                pass

    def test_ocr_prepare_image(self):
        from PIL import Image
        from translators import ocr
        img = Image.new("RGB", (400, 100), "gray")
        out = ocr.prepare_image(img)
        self.assertEqual(out.mode, "RGB")
        self.assertGreaterEqual(out.width, 1600 + 48)
        self.assertGreater(out.height, 100)


class TestOneOCR(unittest.TestCase):
    """Real Windows 11 native engine: loading, execution, boxes, Unicode."""

    def test_only_oneocr_is_registered(self):
        from unittest import mock
        from ocr_providers import manager, oneocr_backend
        try:
            with mock.patch.object(oneocr_backend, "OneOCRProvider") as provider:
                manager._providers = None
                self.assertEqual(manager.all_providers(), [provider.return_value])
        finally:
            manager.reset_for_tests()

    def test_locator_finds_snipping_files(self):
        from ocr_providers import oneocr_backend as ob
        d = ob.find_snipping_dir()
        self.assertIsNotNone(d, "Snipping Tool not found on this machine")
        for f in ob.NEEDED_FILES:
            self.assertTrue((d / f).is_file(), f"missing: {f}")

    def test_staging_dir_loads_dll(self):
        import ctypes
        from ocr_providers import oneocr_backend as ob
        d = ob.ensure_usable_dir()
        dll = ctypes.WinDLL(str(d / "oneocr.dll"))
        self.assertTrue(hasattr(dll, "CreateOcrPipeline"))
        self.assertTrue(hasattr(dll, "RunOcrPipeline"))

    def _sample_image(self, lines, size=52):
        from PIL import Image, ImageDraw, ImageFont
        font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", size)
        img = Image.new("RGB", (1400, 120 * len(lines) + 60), "white")
        d = ImageDraw.Draw(img)
        for i, line in enumerate(lines):
            d.text((40, 30 + i * 110), line, font=font, fill="black")
        return img

    def _assert_boxes_sane(self, result, img):
        self.assertTrue(result.text.strip())
        self.assertTrue(result.lines)
        self.assertTrue(result.words)
        w, h = img.size
        for word in result.words:
            self.assertTrue(word.text)
            if word.box is not None:
                left, top, right, bottom = word.box.as_rect()
                self.assertGreaterEqual(left, -2)
                self.assertGreaterEqual(top, -2)
                self.assertLessEqual(right, w + 2)
                self.assertLessEqual(bottom, h + 2)
                self.assertLess(left, right)
                self.assertLess(top, bottom)

    def test_end_to_end_russian_with_boxes(self):
        from ocr_providers import manager
        img = self._sample_image(["Доминанта разрешается в тонику"])
        provider = manager.get_provider()
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "OneOCR")
        result = provider.read(img)
        self.assertEqual(result.backend, "OneOCR")
        self.assertIn("Доминанта", result.text)  # Unicode: Cyrillic intact
        self._assert_boxes_sane(result, img)
        self.assertGreater(result.mean_confidence, 0.3)

    def test_end_to_end_italian_accents_unicode(self):
        from ocr_providers import manager
        img = self._sample_image(["La cadenza verso la tonica",
                                  "L'accordo di settima à è"])
        result = manager.read(img)
        for word in ("cadenza", "tonica"):
            self.assertIn(word, result.text.lower())
        self._assert_boxes_sane(result, img)

    def test_facade_auto_uses_native_single_pass(self):
        from translators import ocr
        img = self._sample_image(["Le principali cadenze sono"])
        text, langs = ocr.auto_read(img)
        self.assertEqual(langs, ("auto",))
        self.assertIn("cadenze", text.lower())

    def test_provider_swap_abstraction(self):
        from ocr_providers import base, manager
        from translators import ocr

        class FakeProvider(base.OCRProvider):
            name = "Fake"

            def is_available(self):
                return True

            def read(self, pil_image, langs=None):
                return base.OcrResult(text="canned", backend="Fake")

        manager._providers = [FakeProvider()]
        try:
            self.assertEqual(ocr.read_text(object()), "canned")
            self.assertEqual(manager.backend_name(), "Fake")
        finally:
            manager.reset_for_tests()

    def test_boxes_survive_facade(self):
        from translators import ocr
        img = self._sample_image(["Hello harmony triad"])
        result = ocr.read_with_boxes(img)
        self.assertTrue(result.text.strip())
        self.assertIn("harmony", result.text.lower())
        self._assert_boxes_sane(result, img)


class TestMainWindowShutdown(_GuiCase):

    def test_close_releases_window_scoped_application_state(self):
        from unittest import mock
        from main import MainWindow
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        previous_stylesheet = app.styleSheet()
        window = MainWindow()
        popup = mock.Mock()
        popup.isVisible.return_value = False
        popup._close_pending = False
        window.popups = [popup]
        try:
            with mock.patch.object(
                    app, "removeEventFilter",
                    wraps=app.removeEventFilter) as remove_filter:
                self.assertTrue(window.close())

            popup.close.assert_called_once_with()
            self.assertFalse(window._settings_poll.isActive())
            remove_filter.assert_called_once_with(
                window._thumbs_wheel_filter)
            self.assertEqual(app.styleSheet(), previous_stylesheet)
        finally:
            window.popups.clear()
            window.close()

    def test_close_waits_for_popup_workers_before_releasing_window(self):
        from unittest import mock
        from main import MainWindow

        window = MainWindow()
        popup = mock.Mock()
        popup.isVisible.return_value = True
        popup._close_pending = True
        window.popups = [popup]
        try:
            window.show()
            with mock.patch("main.QTimer.singleShot") as single_shot:
                self.assertFalse(window.close())
                self.assertTrue(window._shutdown_waiting)
                single_shot.assert_called_once_with(100, window.close)

                popup._close_pending = False
                popup.isVisible.return_value = False
                self.assertTrue(window.close())

            popup.close.assert_called_once_with()
            self.assertFalse(window._settings_poll.isActive())
        finally:
            window.popups.clear()
            window.close()


class TestFloat(_GuiCase):

    def _close_float(self, f):
        from PySide6.QtCore import Qt
        for p in getattr(f, "popups", []):
            try:
                if p._worker and p._worker.isRunning():
                    if not p._worker.wait(15000):
                        p._worker.terminate()
                        p._worker.wait(2000)
            except RuntimeError:
                pass
            try:
                p.close()
            except RuntimeError:
                pass
        try:
            f.close()
        except RuntimeError:
            pass

    def test_floating_translation_uses_latest_shared_tashkeel_setting(self):
        from unittest import mock
        from main_02 import FloatButton

        floating = FloatButton()
        floating.settings = dict(
            DEFAULTS, selection_mode="ocr", arabic_tashkeel=False)
        latest = dict(floating.settings, arabic_tashkeel=True)
        try:
            with (
                mock.patch("main_02.load", return_value=latest),
                mock.patch("translators.ocr.is_available", return_value=True),
                mock.patch(
                    "translators.ocr.prepare_image",
                    return_value=object(),
                ),
                mock.patch(
                    "popup.translate_popup.TranslatePopup",
                ) as popup_type,
            ):
                self.assertTrue(floating.translate_pil(object()))

            settings_used = popup_type.call_args.args[0]
            self.assertTrue(settings_used["arabic_tashkeel"])
            self.assertEqual(settings_used["target_language"], "ar")
        finally:
            floating.popups.clear()
            self._close_float(floating)

    def test_floating_settings_refresh_preserves_floating_languages(self):
        from unittest import mock
        from config.settings import DEFAULTS
        from main_02 import FloatButton

        floating = FloatButton()
        popup = mock.Mock()
        popup.isVisible.return_value = True
        floating.popups = [popup]
        floating.settings = dict(
            DEFAULTS,
            color_scheme="dark",
            source_language="en",
            target_language="ar",
            float_source_language="de",
            float_target_language="fr",
        )
        updated = dict(
            floating.settings,
            color_scheme="light",
            font_size=23,
            line_spacing_px=5,
            translation_side_margin_px=29,
        )
        try:
            with mock.patch("main_02.load", return_value=updated):
                floating._reload_settings()

            popup.refresh_display.assert_called_once()
            applied = popup.refresh_display.call_args.args[0]
            self.assertEqual(applied["font_size"], 23)
            self.assertEqual(applied["line_spacing_px"], 5)
            self.assertEqual(applied["translation_side_margin_px"], 29)
            self.assertEqual(applied["source_language"], "de")
            self.assertEqual(applied["target_language"], "fr")
            self.assertEqual(applied["color_scheme"], "light")
            from ui.theme import theme_colors
            self.assertIn(
                theme_colors("Dark Slate", "light")["accent"],
                floating.btn.styleSheet(),
            )
        finally:
            self._close_float(floating)

    def test_floating_system_color_change_refreshes_bubble_and_popups(self):
        from unittest import mock
        from config.settings import DEFAULTS
        from main_02 import FloatButton
        from ui.theme import theme_colors

        floating = FloatButton()
        floating.settings = dict(DEFAULTS, color_scheme="system")
        popup = mock.Mock()
        popup.isVisible.return_value = True
        floating.popups = [popup]
        try:
            floating._on_system_color_scheme_changed()

            self.assertIn(
                theme_colors("Dark Slate", "system")["accent"],
                floating.btn.styleSheet(),
            )
            popup.refresh_display.assert_called_once()
            self.assertEqual(
                popup.refresh_display.call_args.args[0]["color_scheme"],
                "system",
            )
        finally:
            self._close_float(floating)

    def test_floating_theme_selection_updates_bubble_and_open_popups(self):
        from unittest import mock
        from PySide6.QtWidgets import QApplication
        from config.settings import DEFAULTS
        from main_02 import FloatButton
        from ui.theme import THEMES

        floating = FloatButton()
        popup = mock.Mock()
        popup.isVisible.return_value = True
        floating.popups = [popup]
        app = QApplication.instance()
        previous_stylesheet = app.styleSheet()
        try:
            with mock.patch("main_02.save"):
                floating._theme_action_sets[0]["Pure Light"].trigger()

            self.assertEqual(floating.settings["theme"], "Pure Light")
            colors = THEMES["Pure Light"]
            self.assertIn(colors["accent"], floating.btn.styleSheet())
            self.assertFalse(floating.btn.icon().isNull())
            self.assertIn(colors["panel"], app.styleSheet())
            self.assertTrue(all(
                actions["Pure Light"].isChecked()
                for actions in floating._theme_action_sets
            ))
            popup.refresh_display.assert_called_once()
            self.assertEqual(
                popup.refresh_display.call_args.args[0]["theme"], "Pure Light")
            if hasattr(floating, "_tray_explain_action"):
                self.assertEqual(
                    floating._tray_explain_action.isChecked(),
                    bool(DEFAULTS["explain_mode"]),
                )
        finally:
            app.setStyleSheet(previous_stylesheet)
            self._close_float(floating)

    def test_floating_theme_selection_keeps_popup_colors_fixed(self):
        from unittest import mock
        from main_02 import FloatButton
        from popup.translate_popup import TranslatePopup

        floating = FloatButton()
        popup = TranslatePopup(dict(floating.settings))
        floating.popups = [popup]
        popup.show()
        try:
            original_popup_style = popup.styleSheet()
            original_result_style = popup.result.styleSheet()
            with mock.patch("main_02.save"):
                floating._set_theme("Amoled")

            self.assertEqual(popup.styleSheet(), original_popup_style)
            self.assertEqual(popup.result.styleSheet(), original_result_style)
            self.assertIn("#f7f8fa", popup.styleSheet())
            self.assertIn("#f1f3f6", popup.result.styleSheet())
            self.assertEqual(popup.settings["theme"], "Amoled")
        finally:
            self._close_float(floating)

    def test_floating_bubble_stays_inside_screen_available_geometry(self):
        from unittest import mock
        from PySide6.QtCore import QPoint
        from main_02 import FloatButton

        floating = FloatButton()
        screen = self.app.primaryScreen()
        area = screen.availableGeometry()
        try:
            self.assertEqual(floating.size(), floating.btn.size())
            with mock.patch(
                "main_02.QGuiApplication.screenAt",
                return_value=screen,
            ):
                top_left = floating._bounded_position(
                    QPoint(area.left() - 1000, area.top() - 1000))
                bottom_right = floating._bounded_position(
                    QPoint(area.right() + 1000, area.bottom() + 1000))

            self.assertEqual(top_left, area.topLeft())
            self.assertEqual(
                bottom_right,
                QPoint(
                    area.right() - floating.width() + 1,
                    area.bottom() - floating.height() + 1,
                ),
            )
        finally:
            self._close_float(floating)

    def test_floating_missing_ocr_shows_an_actionable_message(self):
        from unittest import mock
        from PIL import Image
        from main_02 import FloatButton

        floating = FloatButton()
        try:
            with (
                mock.patch(
                    "translators.ocr.is_available", return_value=False),
                mock.patch(
                    "main_02.QMessageBox.warning") as warning,
            ):
                self.assertFalse(floating.translate_pil(
                    Image.new("RGB", (20, 20), "white")))

            warning.assert_called_once()
            self.assertIn(
                "OneOCR",
                warning.call_args.args[2],
            )
            self.assertIn(
                "Image",
                warning.call_args.args[2],
            )
        finally:
            self._close_float(floating)

    def test_floating_explain_changes_refresh_open_popups(self):
        from unittest import mock
        from config.settings import DEFAULTS
        from main_02 import FloatButton

        floating = FloatButton()
        popup = mock.Mock()
        popup.isVisible.return_value = True
        floating.popups = [popup]
        floating.settings = dict(DEFAULTS)
        updated = dict(
            floating.settings,
            explain_mode=True,
            explain_instructions="Use one concise example.",
        )
        try:
            with mock.patch("main_02.load", return_value=updated):
                floating._reload_settings()

            popup.refresh_display.assert_called_once()
            applied = popup.refresh_display.call_args.args[0]
            self.assertTrue(applied["explain_mode"])
            self.assertEqual(
                applied["explain_instructions"],
                "Use one concise example.",
            )

            popup.refresh_display.reset_mock()
            floating._set_explain_mode(False)
            popup.refresh_display.assert_called_once()
            self.assertFalse(
                popup.refresh_display.call_args.args[0]["explain_mode"])
        finally:
            self._close_float(floating)

    def test_floating_translation_defaults_to_auto_source(self):
        from config.settings import DEFAULTS
        from main_02 import FloatButton

        floating = FloatButton()
        try:
            self.assertEqual(DEFAULTS["float_source_language"], "auto")
            self.assertEqual(
                floating._popup_settings()["source_language"], "auto")
            self.assertEqual(
                floating._popup_settings()["target_language"], "ar")
        finally:
            self._close_float(floating)

    def test_dropping_supported_documents_on_float_opens_reader(self):
        from unittest import mock
        from PySide6.QtCore import QEvent, QMimeData, QUrl
        from main_02 import APP_DIR, FloatButton

        floating = FloatButton()
        mime = QMimeData()
        mime.setUrls([
            QUrl.fromLocalFile(r"C:\books\harmony.pdf"),
            QUrl.fromLocalFile(r"C:\books\notes.md"),
        ])
        event = mock.Mock()
        event.type.return_value = QEvent.Type.Drop
        event.mimeData.return_value = mime
        try:
            with mock.patch("subprocess.Popen") as popen:
                handled = floating.eventFilter(floating.btn, event)

            self.assertTrue(handled)
            popen.assert_called_once()
            command = popen.call_args.args[0]
            self.assertIn(str(APP_DIR / "main.py"), command)
            self.assertEqual(command[-2:], [
                url.toLocalFile() for url in mime.urls()])
            event.acceptProposedAction.assert_called_once_with()
        finally:
            self._close_float(floating)

    def test_packaged_float_uses_embedded_reader_when_no_reader_exe_exists(self):
        from pathlib import Path
        from unittest import mock
        import main_02
        from main_02 import FloatButton

        floating = FloatButton()
        try:
            with (
                mock.patch.object(main_02.sys, "frozen", True, create=True),
                mock.patch.object(Path, "is_file", return_value=False),
                mock.patch.object(floating, "_show_reader_window") as show_reader,
            ):
                floating._open_in_reader([r"C:\books\harmony.pdf"])

            show_reader.assert_called_once_with([r"C:\books\harmony.pdf"])
        finally:
            self._close_float(floating)

    def test_floating_settings_dialog_refreshes_popups_with_float_languages(self):
        from unittest import mock
        from config.settings import DEFAULTS
        from main_02 import FloatButton

        floating = FloatButton()
        popup = mock.Mock()
        popup.isVisible.return_value = True
        floating.popups = [popup]
        floating.settings = dict(
            DEFAULTS,
            source_language="en",
            target_language="ar",
            float_source_language="it",
            float_target_language="de",
        )
        updated = dict(
            floating.settings,
            font_size=25,
            line_spacing_px=3,
            translation_side_margin_px=27,
        )
        dialog = mock.Mock()
        dialog.exec.return_value = 1
        dialog.values.return_value = updated
        try:
            with (
                mock.patch("ui.settings_dialog.SettingsDialog", return_value=dialog),
                mock.patch("main_02.save"),
            ):
                floating._open_settings()

            popup.refresh_display.assert_called_once()
            applied = popup.refresh_display.call_args.args[0]
            self.assertEqual(applied["font_size"], 25)
            self.assertEqual(applied["line_spacing_px"], 3)
            self.assertEqual(applied["translation_side_margin_px"], 27)
            self.assertEqual(applied["source_language"], "it")
            self.assertEqual(applied["target_language"], "de")
        finally:
            self._close_float(floating)

    def test_floating_settings_follow_selected_application_theme(self):
        from config.settings import DEFAULTS
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(dict(DEFAULTS, theme="Pure Light"))
        try:
            style = dialog.styleSheet()
            self.assertIn("background-color: #F8FAFC", style)
            self.assertIn("color: #0F172A", style)
            self.assertIn("background-color: #2563EB", style)
        finally:
            dialog.close()

    def test_floating_context_menu_switches_and_persists_image_ocr_mode(self):
        from config.settings import load
        from main_02 import FloatButton

        floating = FloatButton()
        try:
            image_action = floating._selection_mode_actions["image"]
            ocr_action = floating._selection_mode_actions["ocr"]
            self.assertTrue(ocr_action.isChecked())
            self.assertFalse(image_action.isChecked())

            image_action.trigger()
            self.assertEqual(floating.settings["selection_mode"], "image")
            self.assertTrue(image_action.isChecked())
            self.assertEqual(load()["selection_mode"], "image")

            ocr_action.trigger()
            self.assertEqual(floating.settings["selection_mode"], "ocr")
            self.assertTrue(ocr_action.isChecked())
            self.assertEqual(load()["selection_mode"], "ocr")
        finally:
            self._close_float(floating)

    def test_floating_context_menu_toggles_and_persists_explain_mode(self):
        from config.settings import load
        from main_02 import FloatButton

        floating = FloatButton()
        try:
            action = floating._explain_action
            self.assertFalse(action.isChecked())
            action.trigger()
            self.assertTrue(action.isChecked())
            self.assertTrue(floating.settings["explain_mode"])
            self.assertTrue(load()["explain_mode"])
            action.trigger()
            self.assertFalse(action.isChecked())
            self.assertFalse(load()["explain_mode"])
        finally:
            self._close_float(floating)

    def test_button_single_flow_flags(self):
        from PySide6.QtCore import Qt
        from main_02 import FloatButton
        f = FloatButton()
        try:
            flags = f.windowFlags()
            self.assertEqual(f.windowType(), Qt.WindowType.Window)
            self.assertTrue(flags & Qt.WindowType.WindowStaysOnTopHint)
            self.assertTrue(flags & Qt.WindowType.FramelessWindowHint)
            self.assertFalse(f.windowIcon().isNull())
            self.assertEqual(
                {size.width() for size in f.windowIcon().availableSizes()},
                {16, 24, 32, 48, 64, 128, 256},
            )
            if f._tray is not None:
                self.assertFalse(f._tray.icon().isNull())
            self.assertFalse(f.btn.icon().isNull())
            self.assertFalse(hasattr(f, "snip_btn"), "only ONE button allowed")
            self.assertFalse(hasattr(f, "auto_act"), "no auto-follow allowed")
        finally:
            self._close_float(f)

    def test_taskbar_activation_restores_latest_visible_translation_popup(self):
        from unittest import mock
        from PySide6.QtCore import QEvent
        from main_02 import FloatButton

        floating = FloatButton()
        first_popup = mock.Mock()
        first_popup.isVisible.return_value = True
        old_popup = mock.Mock()
        old_popup.isVisible.return_value = False
        latest_popup = mock.Mock()
        latest_popup.isVisible.return_value = True
        floating.popups = [first_popup, old_popup, latest_popup]
        try:
            with (
                mock.patch("PySide6.QtWidgets.QApplication.activeWindow",
                           return_value=floating),
                mock.patch("main_02.QTimer.singleShot") as single_shot,
            ):
                floating.event(QEvent(QEvent.Type.ActivationChange))
            single_shot.assert_called_once_with(0, floating._restore_latest_popup)

            floating._restore_latest_popup()
            latest_popup.show.assert_called_once_with()
            latest_popup.raise_.assert_called_once_with()
            latest_popup.activateWindow.assert_called_once_with()
            first_popup.raise_.assert_not_called()
            self.assertEqual(floating.popups, [first_popup, latest_popup])
        finally:
            self._close_float(floating)

    def test_float_language_dialog_has_auto_source_and_seven_targets(self):
        from ui.float_language_dialog import FloatLanguageDialog

        dialog = FloatLanguageDialog({
            "float_source_language": "auto",
            "float_target_language": "it",
        })
        try:
            self.assertEqual(dialog.source_language.count(), 8)
            self.assertEqual(dialog.target_language.count(), 7)
            self.assertEqual(dialog.values(), ("auto", "it"))
            self.assertEqual(
                {dialog.source_language.itemData(i)
                 for i in range(dialog.source_language.count())},
                {"auto", "ar", "fr", "en", "ru", "it", "es", "de"},
            )
        finally:
            dialog.close()

    def test_floating_translation_popup_has_inline_language_menus(self):
        from unittest import mock
        from PySide6.QtCore import QObject, Qt, Signal
        from popup.translate_popup import TranslatePopup

        class StubWorker(QObject):
            done = Signal(str, str)
            stage = Signal(str)
            failed = Signal(str)
            source_ready = Signal(str)
            partial = Signal(str)
            finished = Signal()

            def __init__(self, settings, text=None, pil_image=None, ocr_langs=None):
                super().__init__()
                self._settings = dict(settings)
                self.started = False

            def start(self):
                self.started = True

            def isRunning(self):
                return False

        settings = {
            "source_language": "en",
            "target_language": "ar",
            "float_source_language": "en",
            "float_target_language": "ar",
        }
        saved_pairs = []
        workers = []

        def make_worker(*args, **kwargs):
            worker = StubWorker(*args, **kwargs)
            workers.append(worker)
            return worker

        try:
            with mock.patch(
                "popup.translate_popup.TranslateWorker", side_effect=make_worker,
            ):
                popup = TranslatePopup(
                    settings,
                    source_text="sample",
                    floating_language_controls=True,
                    float_language_preferences=True,
                    language_change_callback=lambda source, target, popup:
                        saved_pairs.append((source, target)),
                )
            self.assertFalse(popup.source_language_btn.isHidden())
            self.assertFalse(popup.target_language_btn.isHidden())
            self.assertEqual(len(popup._language_actions["source"]), 8)
            self.assertEqual(len(popup._language_actions["target"]), 7)
            self.assertEqual(popup.source_language_btn.text(), "From: English")
            self.assertEqual(popup.target_language_btn.text(), "To: العربية")
            self.assertEqual(type(popup.source_language_btn).__name__, "QPushButton")
            self.assertEqual(type(popup.target_language_btn).__name__, "QPushButton")
            popup.show()
            self.app.processEvents()
            self.assertGreater(
                popup.target_language_btn.geometry().center().x(),
                popup.source_language_btn.geometry().center().x(),
            )
            popup._render_markdown(
                r"Example (\*\*minor mode\*\*)" "\n\n"
                '<h2 align="center">Centered title</h2>\n\n'
                "### Edge subtitle\n\n"
                "```text\nC - G7 - C\n```"
            )
            escaped_emphasis = popup.result.document().find("minor mode")
            centered_title = popup.result.document().find("Centered title")
            edge_subtitle = popup.result.document().find("Edge subtitle")
            musical_example = popup.result.document().find("C - G7 - C")
            self.assertTrue(popup.result.document().find("**").isNull())
            self.assertLessEqual(
                escaped_emphasis.charFormat().fontWeight(), 400)
            self.assertEqual(
                centered_title.blockFormat().alignment(),
                Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignAbsolute,
            )
            self.assertEqual(
                edge_subtitle.blockFormat().alignment(),
                Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignAbsolute,
            )
            self.assertFalse(musical_example.isNull())
            self.assertEqual(
                musical_example.blockFormat().alignment(),
                Qt.AlignmentFlag.AlignLeft,
            )

            popup._language_actions["source"]["auto"].trigger()
            self.assertEqual(settings["source_language"], "auto")
            self.assertEqual(popup.source_language_btn.text(), "From: Auto")
            popup._language_actions["source"]["de"].trigger()
            popup._language_actions["target"]["fr"].trigger()
            self.assertEqual(settings["source_language"], "de")
            self.assertEqual(settings["target_language"], "fr")
            self.assertEqual(settings["float_source_language"], "de")
            self.assertEqual(settings["float_target_language"], "fr")
            self.assertEqual(popup.source_language_btn.text(), "From: Deutsch")
            self.assertEqual(popup.target_language_btn.text(), "To: Français")
            self.assertEqual(
                popup.source_language_btn.accessibleName(), "From: Deutsch")
            self.assertEqual(
                popup.target_language_btn.accessibleName(), "To: Français")
            self.assertEqual(
                saved_pairs,
                [("auto", "ar"), ("de", "ar"), ("de", "fr")],
            )
            self.assertTrue(workers[0].started)
        finally:
            if "popup" in locals():
                popup.close()

    def test_floating_popup_repairs_escaped_bold_and_keeps_headings_at_edge(self):
        from unittest import mock
        from PySide6.QtCore import QObject, Qt, Signal
        from popup.translate_popup import TranslatePopup

        class StubWorker(QObject):
            done = Signal(str, str)
            stage = Signal(str)
            failed = Signal(str)
            source_ready = Signal(str)
            partial = Signal(str)
            finished = Signal()

            def __init__(self, settings, text=None, pil_image=None, ocr_langs=None):
                super().__init__()

            def start(self):
                pass

            def isRunning(self):
                return False

        with mock.patch("popup.translate_popup.TranslateWorker", StubWorker):
            popup = TranslatePopup(
                {"source_language": "auto", "target_language": "ar"},
                source_text="sample",
                floating_language_controls=True,
            )
        try:
            popup._render_markdown(
                r"Term (\*\*minor mode\*\*)" "\n\n"
                "### Edge subtitle\n\n"
                "```text\nC - G7 - C\n```"
            )
            bold_term = popup.result.document().find("minor mode")
            subtitle = popup.result.document().find("Edge subtitle")
            music = popup.result.document().find("C - G7 - C")
            self.assertTrue(popup.result.document().find("**").isNull())
            self.assertLessEqual(bold_term.charFormat().fontWeight(), 400)
            self.assertEqual(
                subtitle.blockFormat().alignment(),
                Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignAbsolute,
            )
            self.assertEqual(
                music.blockFormat().alignment(),
                Qt.AlignmentFlag.AlignLeft,
            )
        finally:
            popup.close()

    def test_changing_language_retranslates_saved_ocr_text_in_same_popup(self):
        from unittest import mock
        from PySide6.QtCore import QObject, Signal
        from popup.translate_popup import TranslatePopup

        class StubWorker(QObject):
            done = Signal(str, str)
            stage = Signal(str)
            failed = Signal(str)
            source_ready = Signal(str)
            partial = Signal(str)
            finished = Signal()

            def __init__(self, settings, text=None, pil_image=None, ocr_langs=None):
                super().__init__()
                self._settings = dict(settings)
                self.settings = self._settings
                self.text = text
                self.started = False
                self.translate_secs = 0.0

            def start(self):
                self.started = True

            def isRunning(self):
                return False

        settings = {
            "source_language": "en",
            "target_language": "ar",
            "float_source_language": "en",
            "float_target_language": "ar",
        }
        workers = []

        def make_worker(*args, **kwargs):
            worker = StubWorker(*args, **kwargs)
            workers.append(worker)
            return worker

        saved_pairs = []
        popup = None
        try:
            with mock.patch(
                "popup.translate_popup.TranslateWorker", side_effect=make_worker,
            ):
                popup = TranslatePopup(
                    settings,
                    pil_image=object(),
                    floating_language_controls=True,
                    language_change_callback=lambda source, target, popup:
                        saved_pairs.append((source, target)),
                )
                popup.show()
                self.app.processEvents()
                header = popup.source_language_btn.parentWidget()
                header_top = header.geometry().top()
                self.assertLess(header_top, popup.height() // 3)
                self.assertTrue(popup.result.isVisible())
                workers[0].source_ready.emit("original OCR text")
                popup._language_actions["target"]["fr"].trigger()
                self.app.processEvents()

            self.assertEqual(len(workers), 2)
            self.assertIs(popup._worker, workers[1])
            self.assertEqual(workers[1].text, "original OCR text")
            self.assertEqual(workers[1].settings["target_language"], "fr")
            self.assertEqual(settings["target_language"], "fr")
            self.assertEqual(saved_pairs, [("en", "fr")])
            self.assertEqual(header.geometry().top(), header_top)
            self.assertTrue(popup.result.isVisible())
            self.assertEqual(popup.result.toPlainText(), "")
            workers[0].done.emit("stale translation", "old engine")
            self.assertEqual(popup.result.toPlainText(), "")
            workers[1].done.emit("updated translation", "new engine")
            self.assertEqual(
                popup.result.toPlainText(), "updated translation")
        finally:
            if popup is not None:
                popup.close()

    def test_changing_float_language_does_not_delete_running_speech_prefetch(self):
        import threading
        from unittest import mock
        from PySide6.QtCore import QObject, Signal
        from popup.speech_worker import SpeechWorker
        from popup.translate_popup import TranslatePopup
        from config.settings import DEFAULTS

        class StubTranslateWorker(QObject):
            done = Signal(str, str)
            stage = Signal(str)
            failed = Signal(str)
            source_ready = Signal(str)
            partial = Signal(str)
            finished = Signal()

            def __init__(self, settings, text=None, pil_image=None, ocr_langs=None):
                super().__init__()
                self._settings = dict(settings)

            def start(self):
                pass

            def isRunning(self):
                return False

        class BlockingPrefetchWorker(SpeechWorker):
            def __init__(self, parent):
                super().__init__("prefetched words", parent, prefetch_only=True)
                self.started = threading.Event()
                self.release = threading.Event()
                self.cleaned = False

            def run(self):
                self.started.set()
                self.release.wait(5)

            def cleanup_output(self):
                self.cleaned = True

        settings = dict(
            DEFAULTS,
            source_language="auto",
            target_language="ar",
            float_source_language="auto",
            float_target_language="ar",
        )
        popup = None
        prefetch = None
        try:
            with mock.patch(
                "popup.translate_popup.TranslateWorker", StubTranslateWorker,
            ):
                popup = TranslatePopup(
                    settings,
                    source_text="original words",
                    floating_language_controls=True,
                    float_language_preferences=True,
                )
                popup.show()
                self.app.processEvents()
                prefetch = BlockingPrefetchWorker(popup)
                prefetch.deleteLater = mock.Mock(wraps=prefetch.deleteLater)
                popup._speech_prefetch_worker = prefetch
                popup._speech_prefetch_workers.append(prefetch)
                popup._speech_prefetch_key = ("old translation",)
                prefetch.finished.connect(
                    popup._on_speech_prefetch_finished)
                prefetch.start()
                self.assertTrue(prefetch.started.wait(2))

                popup._language_actions["target"]["fr"].trigger()

                self.assertIsNone(popup._speech_prefetch_worker)
                self.assertTrue(prefetch.isRunning())
                self.assertTrue(prefetch.isInterruptionRequested())
                prefetch.deleteLater.assert_not_called()
                self.assertEqual(popup._speech_prefetch_workers, [prefetch])

                popup.close()
                self.assertTrue(popup._close_pending)
                self.assertFalse(popup.isVisible())

                prefetch.release.set()
                self.assertTrue(prefetch.wait(5000))
                self.app.processEvents()
                self.app.processEvents()
                self.assertTrue(prefetch.cleaned)
                prefetch.deleteLater.assert_called_once_with()
                self.assertFalse(popup._speech_prefetch_workers)
                self.assertFalse(popup._close_pending)
        finally:
            if prefetch is not None:
                prefetch.release.set()
                try:
                    prefetch.wait(5000)
                except RuntimeError:
                    pass
            if popup is not None:
                popup.close()

    def test_float_language_preferences_save_without_changing_main_languages(self):
        from unittest import mock
        from PySide6.QtWidgets import QDialog
        from main_02 import FloatButton

        floating = FloatButton()
        dialog = mock.Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.values.return_value = ("de", "fr")
        try:
            floating.settings["source_language"] = "auto"
            floating.settings["target_language"] = "ar"
            with (
                mock.patch("ui.float_language_dialog.FloatLanguageDialog",
                           return_value=dialog),
                mock.patch("main_02.save") as save_settings,
            ):
                floating._open_float_languages()
            self.assertEqual(floating.settings["float_source_language"], "de")
            self.assertEqual(floating.settings["float_target_language"], "fr")
            self.assertEqual(floating.settings["source_language"], "auto")
            self.assertEqual(floating.settings["target_language"], "ar")
            save_settings.assert_called_once_with(floating.settings)
        finally:
            self._close_float(floating)

    def test_float_translation_uses_its_own_language_pair(self):
        from unittest import mock
        from PIL import Image
        from main_02 import FloatButton

        floating = FloatButton()
        floating.settings.update(
            source_language="auto",
            target_language="ar",
            float_source_language="de",
            float_target_language="fr",
            model_instructions="Preserve chord names and notation.",
        )
        try:
            with (
                mock.patch("translators.ocr.is_available", return_value=True),
                mock.patch("translators.ocr.prepare_image", side_effect=lambda image: image),
                mock.patch("popup.translate_popup.TranslatePopup") as popup_type,
                mock.patch(
                    "main_02.load", return_value=dict(floating.settings)),
            ):
                self.assertTrue(floating.translate_pil(Image.new("RGB", (20, 20))))
            popup_settings = popup_type.call_args.args[0]
            self.assertTrue(
                popup_type.call_args.kwargs["floating_language_controls"])
            self.assertEqual(popup_settings["source_language"], "de")
            self.assertEqual(popup_settings["target_language"], "fr")
            self.assertEqual(
                popup_settings["model_instructions"],
                "Preserve chord names and notation.",
            )
            self.assertEqual(floating.settings["source_language"], "auto")
            self.assertEqual(floating.settings["target_language"], "ar")
        finally:
            self._close_float(floating)

    def test_drag_moves_button_via_filter(self):
        from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from main_02 import FloatButton
        f = FloatButton()
        try:
            f.move(200, 200)
            start = f.btn.mapToGlobal(f.btn.rect().center())
            press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(28, 28),
                                QPointF(start), Qt.MouseButton.LeftButton,
                                Qt.MouseButtons.LeftButton, Qt.KeyboardModifier.NoModifier)
            self.app.sendEvent(f.btn, press)
            moved = start + QPoint(60, 40)
            move = QMouseEvent(QEvent.Type.MouseMove, QPointF(88, 68),
                               QPointF(moved), Qt.MouseButton.NoButton,
                               Qt.MouseButtons.LeftButton, Qt.KeyboardModifier.NoModifier)
            self.app.sendEvent(f.btn, move)
            rel = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(88, 68),
                              QPointF(moved), Qt.MouseButton.LeftButton,
                              Qt.MouseButtons.LeftButton, Qt.KeyboardModifier.NoModifier)
            swallowed = f.eventFilter(f.btn, rel)
            self.assertTrue(swallowed, "release after drag must swallow the click")
            self.assertTrue(f._moved)
            self.assertEqual((f.x(), f.y()), (260, 240))
        finally:
            self._close_float(f)

    def test_snip_overlay_accept_reject(self):
        from PySide6.QtCore import QEvent, QPointF, QRect, Qt
        from PySide6.QtGui import QImage, QMouseEvent
        from PySide6.QtWidgets import QApplication
        from main_02 import SnipOverlay, capture_rect
        area = QRect(100, 50, 800, 600)
        ov = SnipOverlay(area)
        try:
            ov.show()
            self.app.processEvents()
            self.assertEqual(ov.geometry(), area)
            self.assertEqual(ov.cursor().shape(), Qt.CursorShape.CrossCursor)

            press = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(650, 450),
                QPointF(750, 500),
                Qt.MouseButton.LeftButton,
                Qt.MouseButtons.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            QApplication.sendEvent(ov, press)
            move = QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(760, 570),
                QPointF(860, 620),
                Qt.MouseButton.NoButton,
                Qt.MouseButtons.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            QApplication.sendEvent(ov, move)
            self.assertEqual(ov._selection_rect, QRect(650, 450, 111, 121))

            image = QImage(ov.size(), QImage.Format.Format_ARGB32)
            image.fill(0)
            ov.render(image)
            self.assertEqual(image.pixelColor(700, 510).alpha(), 0)
            self.assertEqual(image.pixelColor(100, 100).alpha(), 51)
            self.assertEqual(ov.result(), 0)

            release = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(760, 570),
                QPointF(860, 620),
                Qt.MouseButton.LeftButton,
                Qt.MouseButtons.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            QApplication.sendEvent(ov, release)
            self.assertEqual(ov.selected_rect, QRect(750, 500, 111, 121))
            self.assertEqual(ov.result(), SnipOverlay.DialogCode.Accepted)
        finally:
            ov.close()
        ov2 = SnipOverlay(QRect(0, 0, 800, 600))
        try:
            press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(10, 10),
                                QPointF(10, 10), Qt.MouseButton.RightButton,
                                Qt.MouseButtons.RightButton,
                                Qt.KeyboardModifier.NoModifier)
            ov2.mousePressEvent(press)  # right-click cancels, never traps
            self.assertEqual(ov2.result(), SnipOverlay.DialogCode.Rejected)
            self.assertIsNone(ov2.selected_rect)
        finally:
            ov2.close()
        self.assertIsNone(capture_rect(QRect(0, 0, 5, 5)))

    def test_scrolled_selection_estimates_offset_and_stitches_viewports(self):
        import random
        from PIL import Image
        from main_02 import (
            _compose_scrolled_selection,
            _estimate_vertical_scroll,
        )

        source = Image.new("RGB", (96, 700))
        rng = random.Random(42)
        for y in range(source.height):
            color = tuple(rng.randrange(256) for _ in range(3))
            for x in range(source.width):
                source.putpixel((x, y), color)
        before = source.crop((0, 0, 96, 360))
        after = source.crop((0, 120, 96, 480))
        offset = _estimate_vertical_scroll(before, after)
        self.assertAlmostEqual(offset, 120, delta=4)

        stitched = _compose_scrolled_selection(
            [(before, 0), (after, offset)], offset, 80,
        )
        self.assertEqual(stitched.size, (96, 200))
        self.assertEqual(stitched.getpixel((40, 150)), source.getpixel((40, 150)))

    def test_scroll_tracking_detects_external_scroll_without_wheel_callback(self):
        import random
        from unittest import mock
        from PIL import Image
        from PySide6.QtCore import QPoint, QRect
        from main_02 import SnipOverlay

        rng = random.Random(17)
        source = Image.new("RGB", (60, 500))
        for y in range(source.height):
            color = tuple(rng.randrange(256) for _ in range(3))
            for x in range(source.width):
                source.putpixel((x, y), color)
        frame1 = source.crop((0, 0, 60, 300))
        frame2 = source.crop((0, 80, 60, 380))

        overlay = SnipOverlay(QRect(0, 0, 120, 320))
        try:
            overlay._origin = QPoint(10, 20)
            overlay._current_point = QPoint(69, 180)
            overlay._update_scroll_capture_rect()
            self.assertTrue(overlay._scroll_capture_timer.isActive())
            with mock.patch("main_02.capture_pil", side_effect=[frame1, frame2]):
                overlay._capture_scroll_frame()
                overlay._capture_scroll_frame()
            self.assertTrue(overlay._scrolling_selection)
            self.assertAlmostEqual(overlay._scroll_offset, 80, delta=4)
            self.assertGreater(overlay._selection_rect.height(), 160)
        finally:
            overlay._scroll_capture_timer.stop()
            overlay.close()

    def test_snip_overlay_wheel_scrolls_underlying_window_without_hiding(self):
        from unittest import mock
        from PySide6.QtCore import QPoint, QPointF, QRect, Qt
        from PySide6.QtGui import QWheelEvent
        from main_02 import SnipOverlay

        overlay = SnipOverlay(QRect(0, 0, 800, 600))
        try:
            overlay.show()
            self.app.processEvents()
            event = QWheelEvent(
                QPointF(500, 400),
                QPointF(500, 400),
                QPoint(0, 0),
                QPoint(0, -120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            with mock.patch("main_02._forward_wheel_to_window_under_cursor") as send:
                overlay.wheelEvent(event)
                send.assert_called_once_with(-120, int(overlay.winId()))
            self.assertTrue(overlay.isVisible())
        finally:
            overlay.close()

    def test_snip_overlay_allows_scrolling_during_active_selection(self):
        from unittest import mock
        from PySide6.QtCore import QPoint, QPointF, QRect, Qt
        from PySide6.QtGui import QWheelEvent
        from main_02 import SnipOverlay

        overlay = SnipOverlay(QRect(0, 0, 800, 600))
        try:
            overlay.show()
            self.app.processEvents()
            overlay._origin = QPoint(120, 100)
            overlay._current_point = QPoint(340, 360)
            overlay._selection_rect = QRect(120, 100, 220, 260)
            overlay._scrolling_selection = True
            overlay._scroll_capture_rect = QRect(120, 100, 220, 500)
            overlay.grabMouse(Qt.CursorShape.CrossCursor)
            self.assertIs(overlay.mouseGrabber(), overlay)
            event = QWheelEvent(
                QPointF(340, 360),
                QPointF(340, 360),
                QPoint(0, 0),
                QPoint(0, -120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            with (
                mock.patch("main_02.capture_pil", return_value=None),
                mock.patch("main_02._forward_wheel_to_window_under_cursor") as send,
            ):
                overlay.wheelEvent(event)
                send.assert_called_once_with(-120, int(overlay.winId()))
            self.assertTrue(overlay.isVisible())
            self.assertEqual(overlay._origin, QPoint(120, 100))
            self.assertEqual(overlay._selection_rect, QRect(120, 100, 221, 260))
            self.assertIs(overlay.mouseGrabber(), overlay)
            overlay._scroll_offset = 300
            overlay._update_scrolled_selection_rect()
            self.assertEqual(overlay._selection_rect, QRect(120, 100, 221, 560))
        finally:
            if overlay.mouseGrabber() is overlay:
                overlay.releaseMouse()
            overlay.close()

    def test_wheel_starts_long_selection_before_scroll_image_match(self):
        from unittest import mock
        from PySide6.QtCore import QPoint, QPointF, QRect, Qt
        from PySide6.QtGui import QWheelEvent
        from main_02 import SnipOverlay

        overlay = SnipOverlay(QRect(0, 0, 800, 600))
        try:
            overlay.show()
            self.app.processEvents()
            overlay._origin = QPoint(100, 100)
            overlay._current_point = QPoint(300, 300)
            overlay._scroll_capture_rect = QRect(100, 100, 201, 500)
            event = QWheelEvent(
                QPointF(300, 300), QPointF(300, 300), QPoint(0, 0),
                QPoint(0, -120), Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            with (
                mock.patch("main_02.capture_pil", return_value=None),
                mock.patch("main_02._forward_wheel_to_window_under_cursor"),
            ):
                overlay.wheelEvent(event)
            self.assertTrue(overlay._scrolling_selection)
            self.assertEqual(overlay._selection_rect, QRect(100, 100, 201, 200))
        finally:
            overlay._scroll_capture_timer.stop()
            if overlay.mouseGrabber() is overlay:
                overlay.releaseMouse()
            overlay.close()

    def test_snip_overlay_release_returns_stitched_selection_image(self):
        from unittest import mock
        from PIL import Image
        from PySide6.QtCore import QPoint, QPointF, QRect, Qt
        from PySide6.QtGui import QMouseEvent
        from main_02 import SnipOverlay

        overlay = SnipOverlay(QRect(0, 0, 120, 120))
        before = Image.new("RGB", (61, 110), "white")
        after = Image.new("RGB", (61, 110), "black")
        try:
            overlay._origin = QPoint(10, 10)
            overlay._current_point = QPoint(70, 60)
            overlay._scrolling_selection = True
            overlay._scroll_capture_rect = QRect(10, 10, 61, 110)
            overlay._scroll_frames = [(before, 0), (after, 40)]
            overlay._scroll_last_frame = after
            overlay._scroll_offset = 40
            overlay.grabMouse(Qt.CursorShape.CrossCursor)
            release = QMouseEvent(
                QMouseEvent.Type.MouseButtonRelease,
                QPointF(70, 60),
                QPointF(70, 60),
                Qt.MouseButton.LeftButton,
                Qt.MouseButtons.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            with mock.patch("main_02.capture_pil", return_value=after):
                overlay.mouseReleaseEvent(release)
            self.assertEqual(overlay.selected_rect, QRect(10, 10, 61, 90))
            self.assertEqual(overlay.selected_pil.size, (61, 90))
            self.assertEqual(overlay.selected_pil.getpixel((30, 20)), (255, 255, 255))
            self.assertEqual(overlay.selected_pil.getpixel((30, 60)), (0, 0, 0))
        finally:
            if overlay.mouseGrabber() is overlay:
                overlay.releaseMouse()
            overlay.close()

    def test_scrolled_selection_accepts_multi_page_height_with_stationary_pointer(self):
        from unittest import mock
        from PIL import Image
        from PySide6.QtCore import QPoint, QPointF, QRect, Qt
        from PySide6.QtGui import QMouseEvent
        from main_02 import SnipOverlay

        overlay = SnipOverlay(QRect(0, 0, 120, 120))
        viewport = Image.new("RGB", (61, 100), "white")
        scrolled = Image.new("RGB", (61, 100), "black")
        try:
            overlay._origin = QPoint(10, 20)
            overlay._current_point = QPoint(70, 20)
            overlay._scrolling_selection = True
            overlay._scroll_capture_rect = QRect(10, 20, 61, 100)
            overlay._scroll_frames = [(viewport, 0), (scrolled, 80)]
            overlay._scroll_last_frame = scrolled
            overlay._scroll_offset = 80
            overlay.grabMouse(Qt.CursorShape.CrossCursor)
            release = QMouseEvent(
                QMouseEvent.Type.MouseButtonRelease,
                QPointF(70, 20),
                QPointF(70, 20),
                Qt.MouseButton.LeftButton,
                Qt.MouseButtons.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            with mock.patch("main_02.capture_pil", return_value=scrolled):
                overlay.mouseReleaseEvent(release)
            self.assertEqual(overlay.result(), SnipOverlay.DialogCode.Accepted)
            self.assertEqual(overlay.selected_pil.size, (61, 80))
            self.assertEqual(overlay.selected_rect.width(), overlay.selected_pil.width)
            self.assertEqual(overlay.selected_rect.height(), overlay.selected_pil.height)
        finally:
            if overlay.mouseGrabber() is overlay:
                overlay.releaseMouse()
            overlay.close()

    def test_floating_button_opens_fullscreen_selector_on_active_display(self):
        from unittest import mock
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        from main_02 import FloatButton, SnipOverlay

        floating = FloatButton()
        screen = self.app.primaryScreen()
        selector = mock.Mock()
        cursor_shapes = []
        selector.exec.side_effect = lambda: (
            cursor_shapes.append(QApplication.overrideCursor().shape())
            or SnipOverlay.DialogCode.Rejected
        )
        selector.selected_rect = None
        previous_cursor = QApplication.overrideCursor()
        try:
            with (
                mock.patch(
                    "PySide6.QtGui.QCursor.pos",
                    return_value=screen.geometry().center(),
                ),
                mock.patch("main_02.QGuiApplication.screenAt", return_value=screen),
                mock.patch("main_02.SnipOverlay", return_value=selector) as overlay_type,
                mock.patch.object(floating, "hide"),
                mock.patch.object(floating, "show"),
            ):
                self.assertIsNone(floating._pick_rect())
            overlay_type.assert_called_once_with(screen.geometry())
            selector.showFullScreen.assert_called_once_with()
            selector.activateWindow.assert_not_called()
            self.assertEqual(cursor_shapes, [Qt.CursorShape.CrossCursor])
            self.assertIs(QApplication.overrideCursor(), previous_cursor)
        finally:
            if QApplication.overrideCursor() is not previous_cursor:
                QApplication.restoreOverrideCursor()
            self._close_float(floating)

    def test_snip_flow_click_select_translate(self):
        from unittest import mock
        from PySide6.QtCore import QRect
        from PIL import Image
        from main_02 import FloatButton
        f = FloatButton()
        try:
            img = Image.new("RGB", (400, 200), "white")
            with mock.patch.object(f, "_pick_rect",
                                   return_value=QRect(0, 0, 400, 200)):
                with mock.patch("main_02.capture_pil", return_value=img):
                    with mock_chain_translate(
                                    return_value=("نص", "T")):
                        self.assertTrue(f.snip_now())
                        self.assertEqual(len(f.popups), 1)
                        w = f.popups[0]._worker
                        if w:
                            self.assertTrue(w.wait(20000), "worker did not finish")
        finally:
            self._close_float(f)

    def test_qt_capture_fallback_crops_the_region_instead_of_ignoring_it(self):
        """QScreen.grabWindow(0, x, y, w, h) grabs the WHOLE screen in Qt: id 0
        means "the desktop", so the x/y/w/h arguments are dropped and the OCR
        would receive the entire desktop instead of the selection."""
        from unittest import mock
        from PySide6.QtCore import QPoint, QRect
        from PySide6.QtGui import QImage
        import main_02

        full = QImage(1920, 1080, QImage.Format.Format_RGB888)
        full.fill(0)

        class FakePix:
            def toImage(self):
                return full

        class FakeScreen:
            def __init__(self):
                self.seen = None

            def devicePixelRatio(self):
                return 1.0

            def geometry(self):
                return QRect(QPoint(0, 0), QPoint(1920, 1080))

            def grabWindow(self, *args):
                self.seen = args
                return FakePix()

        screen = FakeScreen()
        with mock.patch.object(main_02.QGuiApplication, "screenAt",
                               return_value=screen), \
             mock.patch.object(main_02.QGuiApplication, "primaryScreen",
                               return_value=screen):
            out = main_02._qt_grab_pil(QRect(300, 200, 400, 150))

        self.assertEqual(screen.seen, (0,),
                         "grabWindow must be called without x/y/w/h arguments")
        self.assertEqual(out.size, (400, 150),
                         "returned image must be the region only")

    def test_qt_capture_fallback_scales_for_hidpi_screens(self):
        """The Qt path had no devicePixelRatio correction while the mss path
        did, so the two backends returned differently sized images."""
        from unittest import mock
        from PySide6.QtCore import QPoint, QRect
        from PySide6.QtGui import QImage
        import main_02

        full = QImage(3840, 2160, QImage.Format.Format_RGB888)
        full.fill(0)

        class FakePix:
            def toImage(self):
                return full

        class FakeScreen:
            def devicePixelRatio(self):
                return 2.0

            def geometry(self):
                return QRect(QPoint(0, 0), QPoint(1920, 1080))

            def grabWindow(self, *args):
                return FakePix()

        with mock.patch.object(main_02.QGuiApplication, "screenAt",
                               return_value=FakeScreen()):
            out = main_02._qt_grab_pil(QRect(100, 50, 200, 100))
        self.assertEqual(out.size, (400, 200), "crop must use physical pixels")

    def test_qt_capture_fallback_survives_a_rect_that_overshoots_the_screen(self):
        """Dragging past the screen edge must not hand a null image to the OCR:
        QImage.copy() returns null when the rect leaves the image entirely."""
        from unittest import mock
        from PySide6.QtCore import QPoint, QRect
        from PySide6.QtGui import QImage
        import main_02

        full = QImage(1920, 1080, QImage.Format.Format_RGB888)
        full.fill(0)

        class FakePix:
            def toImage(self):
                return full

        class FakeScreen:
            def devicePixelRatio(self):
                return 1.0

            def geometry(self):
                return QRect(QPoint(0, 0), QPoint(1920, 1080))

            def grabWindow(self, *args):
                return FakePix()

        with mock.patch.object(main_02.QGuiApplication, "screenAt",
                               return_value=FakeScreen()):
            out = main_02._qt_grab_pil(QRect(1800, 1000, 600, 600))
        self.assertIsNotNone(out, "an overshooting rect must still yield an image")
        self.assertEqual(out.size, (120, 80), "must be clipped to the screen")

    def test_capture_prefers_mss_then_falls_back_to_qt(self):
        import sys
        from unittest import mock
        from PySide6.QtCore import QRect
        from PIL import Image
        import main_02

        rgb = bytes(100 * 80 * 3)
        shot = mock.Mock()
        shot.width = 100
        shot.height = 80
        shot.rgb = rgb
        fake_sct = mock.MagicMock()
        fake_sct.__enter__.return_value.grab.return_value = shot
        fake_mss = mock.Mock()
        fake_mss.MSS.return_value = fake_sct
        with mock.patch.dict(sys.modules, {"mss": fake_mss}):
            pil = main_02.capture_pil(QRect(0, 0, 100, 80))
        self.assertIsNotNone(pil)
        self.assertEqual(pil.size, (100, 80))
        self.assertEqual(pil.mode, "RGB")
        self.assertIsNone(main_02.capture_pil(QRect(0, 0, 5, 5)))

    def test_position_memory(self):
        from main_02 import FloatButton
        from config.settings import DEFAULTS, save
        s = dict(DEFAULTS)
        s["float_pos"] = [111, 222]
        save(s)
        f = FloatButton()
        try:
            self.assertEqual((f.x(), f.y()), (111, 222))
        finally:
            self._close_float(f)


if __name__ == "__main__":
    unittest.main()


class TestGuideFlow(_GuiCase):

    def test_bottom_status_bar_is_hidden(self):
        from main import MainWindow

        window = MainWindow()
        try:
            window.show()
            self.app.processEvents()
            self.assertTrue(window.statusBar().isHidden())
        finally:
            window.close()

    def test_model_usage_readout_is_ltr_and_anchored_at_toolbar_right(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QToolBar
        from translators import usage
        from main import MainWindow
        from ui.theme import THEMES, theme_colors

        usage.reset()
        window = MainWindow()
        try:
            window.resize(2600, window.height())
            window.show()
            self.app.processEvents()
            readout = window.model_readout
            self.assertIn("0 tokens", readout.text())
            self.assertEqual(
                readout.layoutDirection(), Qt.LayoutDirection.LeftToRight)
            self.assertEqual(
                readout.alignment(),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            )
            toolbar = window.findChild(QToolBar)
            container = window.model_readout_container
            self.assertEqual(readout.width(), 230)

            def assert_centered_between_chat_and_right_edge():
                readout_left = readout.mapTo(toolbar, readout.rect().topLeft()).x()
                readout_right = readout_left + readout.width()
                chat_right = (
                    window.chat_button.mapTo(
                        toolbar, window.chat_button.rect().topLeft()).x()
                    + window.chat_button.width()
                )
                left_gap = readout_left - chat_right
                right_gap = toolbar.contentsRect().right() + 1 - readout_right
                self.assertLessEqual(
                    abs(left_gap - right_gap),
                    1,
                    f"readout gaps differ: left={left_gap}, right={right_gap}",
                )

            assert_centered_between_chat_and_right_edge()
            initial_container_width = container.width()
            window.resize(window.width() + 300, window.height())
            self.app.processEvents()
            self.assertEqual(readout.width(), 230)
            self.assertGreater(container.width(), initial_container_width)
            assert_centered_between_chat_and_right_edge()

            for theme in THEMES:
                for scheme in ("dark", "light"):
                    window.settings["theme"] = theme
                    window.settings["color_scheme"] = scheme
                    window._apply_model_readout_theme()
                    self.assertIn(
                        f"color: {theme_colors(theme, scheme)['text']}",
                        readout.styleSheet(),
                    )
        finally:
            window.close()

    def test_toolbar_chat_button_opens_reusable_chat_next_to_image_mode(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QDockWidget, QToolBar
        from unittest import mock
        from main import MainWindow

        window = MainWindow()
        try:
            window.show()
            self.app.processEvents()
            toolbar = window.findChild(QToolBar)
            actions = toolbar.actions()
            image_action = next(
                action for action in actions
                if toolbar.widgetForAction(action)
                is window.selection_mode_button)
            chat_action = next(
                action for action in actions
                if toolbar.widgetForAction(action) is window.chat_button)
            self.assertEqual(
                actions.index(chat_action), actions.index(image_action) + 1,
            )
            self.assertEqual(window.chat_button.text(), "Chat")

            popup = mock.Mock()
            popup.isVisible.return_value = True
            popup._translation_text = "Latest translated page"
            window.popups.append(popup)

            window.chat_button.click()
            self.app.processEvents()
            dock = window._chat_dock
            panel = window._chat_panel
            window.resize(1200, 800)
            window.resizeDocks(
                [dock], [180], Qt.Orientation.Horizontal)
            self.app.processEvents()
            self.assertIsInstance(dock, QDockWidget)
            self.assertTrue(dock.isVisible())
            self.assertLessEqual(dock.width(), 200)
            self.assertTrue(panel._compact_chat_actions)
            self.assertEqual(panel.read_aloud_button.text(), "Read")
            self.assertTrue(window.chat_button.isChecked())
            self.assertTrue(window.settings["chat_dock_open"])
            self.assertEqual(
                window.dockWidgetArea(dock),
                Qt.DockWidgetArea.RightDockWidgetArea,
            )
            self.assertTrue(
                dock.features() & QDockWidget.DockWidgetFeature.DockWidgetMovable)
            self.assertFalse(
                dock.features() & QDockWidget.DockWidgetFeature.DockWidgetFloatable)
            self.assertEqual(
                dock.allowedAreas(),
                Qt.DockWidgetArea.LeftDockWidgetArea
                | Qt.DockWidgetArea.RightDockWidgetArea,
            )
            self.assertIn(
                "QMainWindow::separator",
                window.styleSheet() or self.app.styleSheet(),
            )
            separator_style = window.styleSheet() or self.app.styleSheet()
            from ui.theme import theme_colors
            colors = theme_colors(
                window.settings.get("theme", "Dark Slate"),
                window.settings.get("color_scheme", "system"),
            )
            self.assertIn("background-color: transparent", separator_style)
            self.assertIn("width: 1px", separator_style)
            self.assertEqual(
                dock._marker_color.name(), colors["accent_light"].lower())
            marker = dock.marker_geometry()
            self.assertEqual(marker.height(), window.chat_button.height())
            self.assertEqual(marker.width(), 1)
            self.assertIn(
                "QWidget#chatDockTitleBar",
                window._chat_title_bar.styleSheet(),
            )
            self.assertIn(
                f"background-color: {colors['border']}",
                panel.styleSheet(),
            )
            self.assertIn(
                f"background-color: {colors['accent']}",
                panel.styleSheet(),
            )
            self.assertEqual(
                panel._translation_context, "Latest translated page")

            panel._messages.append({
                "role": "assistant",
                "content": "Conversation retained",
            })
            window._chat_close_button.click()
            self.app.processEvents()
            self.assertFalse(dock.isVisible())
            self.assertFalse(window.chat_button.isChecked())
            window.chat_button.click()
            self.app.processEvents()
            self.assertIs(window._chat_dock, dock)
            self.assertIs(window._chat_panel, panel)
            self.assertTrue(dock.isVisible())
            self.assertTrue(window.chat_button.isChecked())
            self.assertEqual(
                panel._messages[-1]["content"], "Conversation retained")
            window.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
            self.app.processEvents()
            self.assertEqual(
                window.dockWidgetArea(dock),
                Qt.DockWidgetArea.LeftDockWidgetArea,
            )
            window.showMinimized()
            self.app.processEvents()
            window._on_chat_dock_visibility_changed(False)
            self.assertTrue(window.chat_button.isChecked())
            self.assertTrue(window.settings["chat_dock_open"])
            window.showNormal()
            self.app.processEvents()
            self.assertTrue(dock.isVisible())
            window._chat_close_button.click()
            self.app.processEvents()
            self.assertFalse(window.chat_button.isChecked())
            self.assertFalse(window.settings["chat_dock_open"])
        finally:
            window.close()

    def test_chat_dock_open_state_is_restored_from_settings(self):
        from config.settings import load, save
        from main import MainWindow

        settings = load()
        settings["chat_dock_open"] = True
        save(settings)

        window = MainWindow()
        try:
            window.show()
            self.app.processEvents()
            self.assertTrue(window.chat_button.isChecked())
            self.assertTrue(window._chat_dock.isVisible())
            self.assertTrue(window.settings["chat_dock_open"])
        finally:
            window.close()

    def test_chat_dock_and_composer_sizes_are_restored(self):
        from PySide6.QtCore import Qt
        from config.settings import load
        from main import MainWindow

        first = MainWindow()
        try:
            first.resize(1200, 800)
            first.show()
            self.app.processEvents()
            first.chat_button.click()
            self.app.processEvents()

            dock = first._chat_dock
            panel = first._chat_panel
            first.resizeDocks([dock], [300], Qt.Orientation.Horizontal)
            panel.input_splitter.setSizes([360, 120])
            first.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
            self.app.processEvents()
            first._save_session_soon()

            saved = load()
            self.assertEqual(saved["chat_dock_area"], "left")
            self.assertGreaterEqual(saved["chat_dock_width"], 180)
            self.assertEqual(
                len(saved["chat_composer_splitter_sizes"]), 2)
            self.assertTrue(all(
                size > 0 for size in saved["chat_composer_splitter_sizes"]))
        finally:
            first.close()

        restored = MainWindow()
        try:
            restored.resize(1200, 800)
            restored.show()
            self.app.processEvents()
            self.assertTrue(restored._chat_dock.isVisible())
            self.assertEqual(
                restored.dockWidgetArea(restored._chat_dock),
                Qt.DockWidgetArea.LeftDockWidgetArea,
            )
            self.assertLessEqual(
                abs(restored._chat_dock.width() - saved["chat_dock_width"]),
                40,
            )
            sizes = restored._chat_panel.input_splitter.sizes()
            saved_sizes = saved["chat_composer_splitter_sizes"]
            self.assertAlmostEqual(
                sizes[1] / sum(sizes),
                saved_sizes[1] / sum(saved_sizes),
                delta=0.12,
            )
        finally:
            restored.close()

    def test_translation_explain_selection_routes_to_main_chat(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from main import MainWindow

        window = MainWindow()
        panel = Mock()
        window._chat_panel = panel
        popup = SimpleNamespace(
            _translation_text="Full translation",
        )
        try:
            with patch.object(window, "_toggle_chat_window") as toggle:
                window._explain_popup_selection(
                    popup, "linguistic", "selected phrase")

            toggle.assert_called_once_with(True)
            panel.set_translation_context.assert_called_once_with(
                "Full translation")
            panel.set_explanation_selection.assert_called_once_with(
                "selected phrase")
            panel.explain_selection.assert_called_once_with("linguistic")
        finally:
            window._chat_panel = None
            window.close()

    def test_toolbar_selection_mode_button_toggles_and_persists(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QToolBar
        from config.settings import load
        from main import MainWindow

        window = MainWindow()
        try:
            toolbar = window.findChild(QToolBar)
            window.resize(max(window.width(), toolbar.sizeHint().width() + 100),
                          window.height())
            window.show()
            self.app.processEvents()
            self.assertEqual(window.settings["selection_mode"], "ocr")
            mode_button = window.selection_mode_button
            self.assertTrue(mode_button.isVisible())
            self.assertEqual(
                mode_button.toolButtonStyle(),
                Qt.ToolButtonStyle.ToolButtonTextOnly,
            )
            self.assertTrue(mode_button.icon().isNull())
            self.assertEqual(mode_button.width(), 72)
            self.assertEqual(mode_button.text(), "OCR")
            mode_button.click()
            self.app.processEvents()
            self.assertEqual(window.settings["selection_mode"], "image")
            self.assertEqual(mode_button.text(), "Image")
            self.assertTrue(mode_button.isVisible())
            self.assertEqual(load()["selection_mode"], "image")

            mode_button.click()
            self.app.processEvents()
            self.assertEqual(window.settings["selection_mode"], "ocr")
            self.assertEqual(mode_button.text(), "OCR")
            self.assertTrue(mode_button.isVisible())
            self.assertEqual(load()["selection_mode"], "ocr")
        finally:
            window.close()

    def test_image_selection_always_routes_the_raw_crop_to_vision(self):
        from unittest import mock
        from PySide6.QtCore import QRect
        from main import MainWindow

        window = MainWindow()
        try:
            window.settings["selection_mode"] = "image"
            book = mock.Mock()
            book.has_doc.return_value = True
            book.render_crop.return_value = "rendered crop"
            book.page_needs_full_page_ocr.return_value = False
            window._show_popup = mock.Mock()
            window._flash_info = mock.Mock()
            with (
                mock.patch(
                    "main.pdf_utils.qimage_to_pil", return_value="raw PIL crop",
                ) as convert,
                mock.patch("main.ocr.is_available") as ocr_available,
            ):
                window._translate_selection(
                    book, 2, QRect(10, 20, 300, 100), force_ocr=False)

            book.extract.assert_not_called()
            convert.assert_called_once_with("rendered crop")
            window._show_popup.assert_called_once_with(
                text=None,
                pil_image="raw PIL crop",
                ocr_langs=(),
                book=book,
                page_no=2,
            )
            ocr_available.assert_not_called()
        finally:
            window.close()

    def test_welcome_then_guide_messages(self):
        import tempfile
        import pymupdf
        from unittest import mock
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QToolBar, QWidget
        from main import MainWindow, app_stylesheet

        win = MainWindow()
        try:
            self.assertFalse(win.welcome.isHidden())
            self.assertTrue(win.tabs.isHidden())
            self.assertIsNotNone(win.findChild(QWidget, "openPdfButton"))
            toolbar = win.findChild(QToolBar)
            self.assertIn(
                "🖼  Pages",
                [action.text() for action in toolbar.actions()],
            )
            toolbar_labels = [action.text() for action in toolbar.actions()]
            self.assertNotIn("Translate", toolbar_labels)
            toolbar_widgets = [
                toolbar.widgetForAction(action)
                for action in toolbar.actions()
            ]
            self.assertNotIn(win.source_combo, toolbar_widgets)
            self.assertNotIn(win.target_combo, toolbar_widgets)
            self.assertTrue(any(
                action.shortcut().toString() == "Ctrl+T"
                for action in win.actions()
            ))
            self.assertTrue(win.thumbs_action.isCheckable())
            style = app_stylesheet(win.settings["theme"])
            self.assertIn("QToolBar QToolButton:pressed", style)
            self.assertNotIn("QToolBar QToolButton:checked", style)

            guided = []
            win._guide = guided.append
            win.translate_last()  # no doc -> open-first guidance
            self.assertEqual(len(guided), 1)
            self.assertIn("Open", guided[0])

            # Open a real PDF -> tab appears, then no-selection guidance.
            tmp = os.path.join(tempfile.gettempdir(), "guide_test.pdf")
            doc = pymupdf.open()
            page = doc.new_page()
            page.insert_text((72, 100), "Hello harmony triad")
            doc.save(tmp)
            doc.close()
            win.open_paths([tmp])
            book = win.current_book()
            self.assertIsNotNone(book)
            self.assertEqual(win.tabs.count(), 1)
            self.assertTrue(win.welcome.isHidden())
            self.assertFalse(win.tabs.isHidden())
            win.translate_last()  # doc but no selection
            self.assertEqual(len(guided), 2)
            self.assertIn("LEFT", guided[1])

            # A real drag-rect on text produces a popup, no guidance.
            zoom = book.reader.zoom  # fresh opens are 100%
            with mock_chain_translate(
                    return_value=("اختبار الترجمة", "Test engine")):
                win.translate_selection(
                    book, 0,
                    QRect(int(60 * zoom), int(80 * zoom),
                          int(340 * zoom), int(30 * zoom)),
                )
                self.app.processEvents()
                popup = win.popups[-1]
                self.assertTrue(popup.isVisible())
                self.assertTrue(popup._worker.wait(20000))
                self.app.processEvents()
                self.assertTrue(popup.result.isVisible())
                self.assertIn("اختبار الترجمة", popup.result.toPlainText())
                screen = self.app.primaryScreen().availableGeometry()
                frame = popup.frameGeometry()
                self.assertGreaterEqual(frame.left(), screen.left())
                self.assertGreaterEqual(frame.top(), screen.top())
                self.assertLessEqual(frame.right(), screen.right())
                self.assertLessEqual(frame.bottom(), screen.bottom())
            self.assertEqual(len(guided), 2)
            self.assertTrue(win.popups)

            # A rect on an empty (scanned-like) area: guidance or image popup.
            # Mock OCR + translation: no model download, no network in the suite.
            n_guided, n_pop = len(guided), len(win.popups)
            with mock.patch("translators.ocr.read_text", return_value="доминанта"):
                with mock.patch("translators.ocr.is_available", return_value=True):
                    with mock_chain_translate(
                                    return_value=("نص تجريبي", "Test")):
                        win.translate_selection(book, 0, QRect(int(60 * zoom), int(400 * zoom),
                                                               int(400 * zoom), int(40 * zoom)))
                        self.app.processEvents()
                        for p in win.popups[n_pop:]:
                            if p._worker:
                                self.assertTrue(p._worker.wait(30000), "worker did not finish")
            self.assertTrue(len(guided) > n_guided or len(win.popups) > n_pop)
            self.app.processEvents()
            win._close_tab(0)
            self.assertEqual(win.tabs.count(), 0)
            self.assertFalse(win.welcome.isHidden())
            self.assertTrue(win.tabs.isHidden())
        finally:
            for p in getattr(win, "popups", []):
                try:
                    if p._worker and p._worker.isRunning():
                        if not p._worker.wait(15000):
                            p._worker.terminate()
                            p._worker.wait(2000)
                except RuntimeError:
                    pass
                try:
                    p.close()
                except RuntimeError:
                    pass
            try:
                win.close()
            except RuntimeError:
                pass
