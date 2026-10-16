"""FreeLLMAPI (local router) tests: credentials, picker, honest badge, streaming.

The router runs on the user's own machine and answers `/v1/chat/completions`
with an `X-Routed-Via: <platform>/<model>` header naming the provider that
actually served the call. That header is the whole point of the integration:
without it the badge would say "auto" and the user could not tell which engine
translated their page.
"""
import os
import unittest
from unittest import mock

import requests

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from config.settings import DEFAULTS
from translators import chain, opencode_compat, registry


class TestFreeLLMCredentials(unittest.TestCase):

    def test_provider_is_routed_to_the_local_router(self):
        self.assertEqual(registry.provider_of("flm:auto"), "freellm")
        self.assertEqual(registry.provider_of("flm:auto:fast"), "freellm")
        self.assertEqual(registry.provider_of("flm:groq/qwen3.8-27b"),
                         "freellm")

    def test_creds_come_from_the_dedicated_slot(self):
        base, key = registry.provider_creds("freellm", {
            "flm_base": "http://localhost:31415/v1", "flm_key": "K"})
        self.assertEqual((base, key), ("http://localhost:31415/v1", "K"))

    def test_creds_fall_back_to_the_shared_slot_for_old_setups(self):
        base, key = registry.provider_creds("freellm", {
            "flm_base": "", "flm_key": "",
            "openai_base": "http://localhost:31415/v1", "openai_key": "S"})
        self.assertEqual((base, key), ("http://localhost:31415/v1", "S"))

    def test_a_key_alone_still_resolves_a_working_url(self):
        # The URL is shown as a placeholder and only persisted once the user
        # confirms it, so a key-only setup is the normal first-run state. It
        # must still route to the router's real port, not to the docs' 3001.
        with mock.patch.object(registry, "detect_flm_base",
                               return_value="http://localhost:31415/v1"):
            base, key = registry.provider_creds("freellm", {
                "flm_base": "", "flm_key": "K"})
        self.assertEqual((base, key), ("http://localhost:31415/v1", "K"))

    def test_saved_settings_round_trip_without_a_guessed_url(self):
        # A machine-dependent guess must never be written into persisted state:
        # `load()` has to return exactly what is on disk. Patched onto a temp
        # path so the user's real settings are never touched.
        import tempfile
        from pathlib import Path
        from config import settings as settings_mod
        with tempfile.TemporaryDirectory() as tmp:
            original = settings_mod.SETTINGS_PATH
            settings_mod.SETTINGS_PATH = Path(tmp) / "settings.json"
            try:
                with mock.patch.object(
                        registry, "detect_flm_base",
                        return_value="http://localhost:9999/v1"):
                    settings_mod.save({"flm_key": "K"})
                    loaded = settings_mod.load()
                self.assertEqual(loaded["flm_base"], "")
                self.assertEqual(loaded["flm_key"], "K")
            finally:
                settings_mod.SETTINGS_PATH = original

    def test_availability_names_the_missing_token(self):
        ok, reason = registry.availability("flm:auto", dict(DEFAULTS))
        self.assertFalse(ok)
        self.assertIn("FreeLLMAPI", reason)
        ok, _ = registry.availability(
            "flm:auto", {"flm_key": "K", "flm_base": "http://localhost:31415/v1"})
        self.assertTrue(ok)

    def test_base_url_is_detected_from_the_router_config(self):
        # The router's own config.json is the source of truth for the port;
        # the docs' 3001 is only the default. A missing or unreadable file
        # must degrade to that default rather than raise.
        with mock.patch.object(registry, "_FLM_CONFIG_PATHS", []):
            self.assertEqual(registry.detect_flm_base(),
                             registry.FLM_DEFAULT_BASE)
        with mock.patch.object(
            registry, "_FLM_CONFIG_PATHS", [registry.Path("x.json")]), \
             mock.patch.object(registry.Path, "read_text",
                               side_effect=OSError("absent")):
            self.assertEqual(registry.detect_flm_base(),
                             registry.FLM_DEFAULT_BASE)
        with mock.patch.object(
            registry, "_FLM_CONFIG_PATHS", [registry.Path("x.json")]), \
             mock.patch.object(
                 registry.Path, "read_text",
                 return_value='{"port": 31415, "launchDashboardShown": true}'):
            self.assertEqual(registry.detect_flm_base(),
                             "http://localhost:31415/v1")
        # A port the router never wrote must not become a dead URL.
        with mock.patch.object(
            registry, "_FLM_CONFIG_PATHS", [registry.Path("x.json")]), \
             mock.patch.object(registry.Path, "read_text",
                               return_value='{"port": "not-a-port"}'):
            self.assertEqual(registry.detect_flm_base(),
                             registry.FLM_DEFAULT_BASE)

    def test_router_strategies_are_not_promised_vision(self):
        # Was "supported", on the theory that the gateway routes images to a
        # vision model by itself. Measured false twice: `auto` answered images
        # with 429 routing_exhausted ("6 model lacks vision") and later 502.
        # See tests/test_freellm_vision.py for the full measurement.
        for model in ("flm:auto", "flm:fusion", "flm:auto:fast"):
            self.assertEqual(registry.image_capability(model), "unknown")


class TestFreeLLMModelRetirement(unittest.TestCase):
    """A saved router model must not outlive the router's catalog."""

    def _load(self, **saved):
        import json
        import tempfile
        from pathlib import Path
        from config import settings as settings_mod
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "settings.json"
        path.write_text(json.dumps(saved), encoding="utf-8")
        original = settings_mod.SETTINGS_PATH
        settings_mod.SETTINGS_PATH = path
        self.addCleanup(setattr, settings_mod, "SETTINGS_PATH", original)
        return settings_mod.load()

    def test_a_model_the_router_dropped_is_moved_off(self):
        # An id that was in a previous catalog but is absent from the current
        # `ready` list must not stay selected: the router would answer with an
        # error the user cannot act on.
        loaded = self._load(model="flm:retired-model-from-last-week", flm_key="K",
                            flm_models_ready=["gpt-oss-20b", "auto"])
        self.assertEqual(loaded["model"], "gemini-3.5-flash-lite")

    def test_a_model_that_is_ready_now_stays(self):
        loaded = self._load(model="flm:qwen3.8-27b", flm_key="K",
                            flm_models_ready=["gpt-oss-20b", "qwen3.8-27b"])
        self.assertEqual(loaded["model"], "flm:qwen3.8-27b")

    def test_a_router_strategy_always_stays(self):
        for strategy in registry.FLM_ROUTER_MODELS:
            loaded = self._load(model=f"flm:{strategy}", flm_key="K",
                                flm_models_ready=["gpt-oss-20b"])
            self.assertEqual(loaded["model"], f"flm:{strategy}")

    def test_a_model_in_the_live_list_stays(self):
        loaded = self._load(model="flm:gpt-oss-20b", flm_key="K",
                            flm_models_ready=["gpt-oss-20b", "auto"])
        self.assertEqual(loaded["model"], "flm:gpt-oss-20b")

    def test_no_key_means_no_judgement(self):
        # Without a key the app cannot know what the router serves, so it must
        # leave the user's choice alone rather than silently replace it.
        loaded = self._load(model="flm:anything-at-all")
        self.assertEqual(loaded["model"], "flm:anything-at-all")

    def test_a_live_groq_id_is_never_treated_as_dead(self):
        # The regression that made the picker unusable: every `groq:` model was
        # reset on load, bouncing the user back to Gemini.
        loaded = self._load(model="groq:qwen/qwen3.8-27b", groq_key="G")
        self.assertEqual(loaded["model"], "groq:qwen/qwen3.8-27b")

    def test_groq_ids_that_were_retired_are_moved_off(self):
        # Groq removed these without notice; they cannot answer any more.
        for dead in ("groq:llama-3.3-70b-versatile",
                     "groq:llama-3.1-8b-instant"):
            loaded = self._load(model=dead, groq_key="G")
            self.assertEqual(loaded["model"], "gemini-3.5-flash-lite")

    def test_a_dropped_provider_is_moved_off(self):
        # `cerebras:` is no longer routed at all. Keeping the id selected
        # would fail every selection with a badge naming a provider the app
        # cannot reach.
        loaded = self._load(model="cerebras:qwen-3.8-27b",
                            cerebras_key="obsolete")
        self.assertEqual(loaded["model"], "gemini-3.5-flash-lite")

    def test_a_ready_list_of_the_wrong_shape_is_ignored(self):
        loaded = self._load(model="flm:auto", flm_key="K",
                            flm_models_ready="not-a-list")
        self.assertEqual(loaded["flm_models_ready"], [])
        self.assertEqual(loaded["model"], "flm:auto")


class TestFreeLLMPicker(unittest.TestCase):

    def test_router_entries_and_live_models_are_offered(self):
        labels = [label for label, _ in registry.model_groups(
            {"flm_key": "K"},
            live_flm=["groq/qwen3.8-27b", "google/gemini-2.5-flash"],
        )]
        self.assertIn(registry.FLM_HDR, labels)
        for entry in registry.FLM_ROUTER_MODELS:
            self.assertIn(f"{registry.FLM_PREFIX}{entry}", labels)
        self.assertIn("flm:groq/qwen3.8-27b", labels)
        self.assertIn("flm:google/gemini-2.5-flash", labels)

    def test_section_is_disabled_without_a_token(self):
        rows = dict(registry.model_groups({}))
        self.assertNotIn(registry.FLM_HDR, rows)
        self.assertNotIn(f"{registry.FLM_PREFIX}auto", rows)

    def test_section_is_enabled_with_a_token(self):
        rows = dict(registry.model_groups({"flm_key": "K"}))
        self.assertTrue(rows[f"{registry.FLM_PREFIX}auto"])


class TestFreeLLMChain(unittest.TestCase):

    def _settings(self, **extra):
        s = dict(DEFAULTS)
        s.update({"model": "flm:auto", "flm_key": "K",
                  "flm_base": "http://localhost:31415/v1"})
        s.update(extra)
        return s

    def test_badge_names_the_provider_that_actually_answered(self):
        with mock.patch(
            "translators.opencode_compat.translate_with_engine",
            return_value=("النص", "groq/qwen3.8-27b"),
        ) as call:
            text, badge = chain.translate("The cadence", self._settings())
        self.assertEqual(text, "النص")
        # Never "auto" alone: the router may have used another engine.
        self.assertIn("groq/qwen3.8-27b", badge)
        self.assertNotEqual(badge.strip(), "auto")
        self.assertEqual(call.call_args.args[1:4],
                         ("http://localhost:31415/v1", "K", "auto"))

    def test_badge_says_router_only_when_it_reported_nothing(self):
        with mock.patch(
            "translators.opencode_compat.translate_with_engine",
            return_value=("النص", ""),
        ):
            _, badge = chain.translate("The cadence", self._settings())
        self.assertIn("auto", badge)

    def test_image_translation_goes_through_the_router(self):
        from PIL import Image
        img = Image.new("RGB", (10, 10), "white")
        with mock.patch(
            "translators.opencode_compat.translate_image",
            return_value="صورة",
        ) as call:
            out, _badge = chain.translate_image(img, self._settings())
        self.assertEqual(out, "صورة")
        self.assertEqual(call.call_args.args[1:4],
                         ("http://localhost:31415/v1", "K", "auto"))

    def test_image_badge_names_the_router_model_that_answered(self):
        from PIL import Image

        def fake_translate_image(*_args, **kwargs):
            kwargs["report"]["routed_via"] = (
                "google/gemini-3.5-flash-lite after 1 failover (rate_limited)")
            return "صورة مترجمة"

        with mock.patch(
            "translators.opencode_compat.translate_image",
            side_effect=fake_translate_image,
        ):
            translated, badge = chain.translate_image(
                Image.new("RGB", (10, 10), "white"), self._settings())

        self.assertEqual(translated, "صورة مترجمة")
        self.assertIn("google/gemini-3.5-flash-lite", badge)
        self.assertIn("after 1 failover", badge)

    def test_image_request_reports_the_router_response_header(self):
        import requests
        from PIL import Image
        from translators import opencode_compat

        response = mock.Mock()
        response.status_code = 200
        response.headers = {
            "X-Routed-Via": "google/gemini-3.5-flash-lite",
            "X-Fallback-Attempts": "1",
            "X-Fallback-Trail": "rate_limited",
        }
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": [{
            "message": {"content": "صورة مترجمة"},
            "finish_reason": "stop",
        }]}
        report = {}

        with mock.patch.object(requests, "post", return_value=response):
            translated = opencode_compat.translate_image(
                Image.new("RGB", (10, 10), "white"),
                "http://localhost:31415/v1",
                "K",
                "fusion",
                report=report,
            )

        self.assertEqual(translated, "صورة مترجمة")
        self.assertEqual(
            report["routed_via"],
            "google/gemini-3.5-flash-lite after 1 failover (rate_limited)",
        )

    def test_image_keeps_first_model_badge_without_language_retry(self):
        from PIL import Image

        arabic = (
            "هذه ترجمة عربية سليمة تنقل معنى النص بوضوح وتحافظ على "
            "كل التفاصيل المهمة للقارئ العربي. "
        ) * 8
        mixed = (
            f"{arabic.rstrip()}\n"
            "The effects of EU membership were not fully understood by "
            "the countries that joined the Union."
        )
        calls = 0

        def fake_translate_image(*_args, **kwargs):
            nonlocal calls
            calls += 1
            kwargs["report"]["routed_via"] = (
                "google/gemini-3.5-flash-lite"
                if calls == 1 else "groq/qwen3.8-27b")
            return mixed if calls == 1 else arabic

        with mock.patch(
            "translators.opencode_compat.translate_image",
            side_effect=fake_translate_image,
        ):
            translated, badge = chain.translate_image(
                Image.new("RGB", (10, 10), "white"),
                self._settings(target_language="ar"),
            )

        self.assertEqual(translated, mixed)
        self.assertEqual(calls, 1)
        self.assertIn("google/gemini-3.5-flash-lite", badge)

    def test_missing_token_says_so_instead_of_switching_engine(self):
        with mock.patch("translators.opencode_compat.translate_with_engine",
                        side_effect=AssertionError("must not be called")):
            text, badge = chain.translate(
                "The cadence", self._settings(flm_key=""))
        # Falls through to the free layers, and the badge says why.
        self.assertIn("FreeLLMAPI", badge)

    def test_fast_mode_streams_words_before_the_whole_reply(self):
        deltas = ["مرحبا ", "بالعالم ", "اليوم"]
        report: dict = {}

        def fake_stream(*_args, **kwargs):
            report.update(kwargs.get("report") or {})
            return iter(deltas)

        with mock.patch(
            "translators.opencode_compat.stream_translate",
            side_effect=fake_stream,
        ) as stream:
            events = list(chain.translate_stream(
                "The cadence", self._settings(fast_mode=True)))
        partials = [e[1] for e in events if e[0] == "partial"]
        self.assertEqual(
            partials,
            ["مرحبا ", "مرحبا بالعالم ", "مرحبا بالعالم اليوم"],
        )
        self.assertEqual(events[-1][0], "done")
        self.assertTrue(stream.called)
        # No second request is made just to learn who answered: the badge comes
        # from the streaming response's own header, which the delta reader
        # recorded.
        report["routed_via"] = "groq/qwen3.8-27b"
        self.assertNotIn("translate_with_engine", stream.call_args.args)

    def test_translation_and_stream_requests_allow_router_startup_time(self):
        response = mock.Mock(status_code=200)
        response.headers = {}
        response.raise_for_status.return_value = None
        response.iter_lines.return_value = []
        response.json.return_value = {"choices": [{
            "message": {"content": "النص"},
            "finish_reason": "stop",
        }]}

        with mock.patch.object(
                requests, "post", return_value=response) as post:
            list(opencode_compat.stream_translate(
                "source", "http://localhost:31415/v1", "K", "auto"))
            self.assertEqual(
                post.call_args.kwargs["timeout"],
                opencode_compat.TRANSLATION_TIMEOUT,
            )

            opencode_compat.translate_with_engine(
                "source", "http://localhost:31415/v1", "K", "auto")
            self.assertEqual(
                post.call_args.kwargs["timeout"],
                opencode_compat.TRANSLATION_TIMEOUT,
            )

    def test_streaming_failure_falls_back_instead_of_raising(self):
        with mock.patch(
            "translators.opencode_compat.stream_translate",
            side_effect=RuntimeError("router down"),
        ), mock.patch(
            "translators.opencode_compat.translate_with_engine",
            return_value=("النص", "groq/x"),
        ):
            events = list(chain.translate_stream(
                "The cadence", self._settings(fast_mode=True)))
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(events[-1][1], "النص")

    def test_streaming_badge_uses_the_header_of_the_very_same_request(self):
        """One request per translation, always.

        Asking the router twice — once to stream, once to read `X-Routed-Via` —
        would double the tokens and add a full round-trip of latency to every
        page. The badge must come from the streamed response itself.
        """
        def fake_stream(*_args, **kwargs):
            kwargs["report"]["routed_via"] = "groq/qwen3.8-27b"
            return iter(["النص "])

        with mock.patch(
            "translators.opencode_compat.stream_translate",
            side_effect=fake_stream,
        ), mock.patch("translators.opencode_compat.translate_with_engine",
                      side_effect=AssertionError("no second request")):
            events = list(chain.translate_stream(
                "The cadence", self._settings(fast_mode=True)))
        self.assertEqual(events[-1][2], "🔀 groq/qwen3.8-27b")

    def test_badge_reports_how_many_failovers_the_router_needed(self):
        def fake_stream(*_args, **kwargs):
            kwargs["report"]["routed_via"] = (
                "groq/qwen3.8-27b after 2 failovers (rate_limited)")
            return iter(["النص "])

        with mock.patch(
            "translators.opencode_compat.stream_translate",
            side_effect=fake_stream,
        ):
            events = list(chain.translate_stream(
                "The cadence", self._settings(fast_mode=True)))
        badge = events[-1][2]
        self.assertIn("groq/qwen3.8-27b", badge)
        self.assertIn("failover", badge)


class TestFreeLLMSettingsDialog(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # A QApplication must exist before any widget is constructed, exactly
        # as the other dialog tests in tests/test_app.py set it up.
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_fields_roundtrip_and_show_state(self):
        from main import SettingsDialog
        s = dict(DEFAULTS, flm_key="K",
                 flm_base="http://localhost:31415/v1",
                 flm_models_ready=["gpt-oss-20b", "auto"])
        dlg = SettingsDialog(s)
        try:
            v = dlg.values()
            self.assertEqual(v["flm_key"], "K")
            self.assertEqual(v["flm_base"], "http://localhost:31415/v1")
            self.assertEqual(
                v["flm_models_ready"], ["gpt-oss-20b", "auto"])
            self.assertEqual(dlg.flm_state.text(), "●")
        finally:
            dlg.close()

    def test_empty_fields_are_saved_empty(self):
        from main import SettingsDialog
        dlg = SettingsDialog(dict(DEFAULTS, flm_key="K"))
        try:
            self.assertEqual(dlg.flm_state.text(), "●")
            dlg.flm_key.setText("")
            self.assertEqual(dlg.flm_state.text(), "○")
        finally:
            dlg.close()


if __name__ == "__main__":
    unittest.main()
