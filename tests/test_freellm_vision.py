"""Vision capability must reflect what the router actually does.

Measured live 2026-10-09 against this machine's FreeLLMAPI install, with the
Google + Cloudflare + HuggingFace keys configured:

  * `auto` FAILED for images twice: HTTP 429 routing_exhausted ("6 model lacks
    vision") before a vision key existed, and HTTP 502 after one existed
    (routed to gemini-3.7-flash, which Google answered 503). So `auto` is not a
    promise of vision.
  * `gemma-4-26b-a4b-it` and `gemma-4-26b-it` read the image (8.0s / 8.8s).
  * `gemini-3.1-flash-lite` (3.6s), `gemini-3.5-flash` (4.1s),
    `gemini-3-flash-preview` (4.2s), `gemini-3.8-flash` (5.4s) read images.
  * `llava-1.5-7b`, `llama-4-scout`, `moondream-3.1-9b-a2b`, `gemma-2b-it-lora`,
    `gemma-7b-it-lora`, `gemma-sea-lion-v4-27b` all FAILED for images, several
    in ~2s, i.e. rejected rather than attempted.

The catalog's `ready` flag only means "a key is configured"; it says nothing
about vision. These tests lock the measured truth in.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from translators import chain, registry


class TestRouterVisionCapability(unittest.TestCase):

    def test_auto_is_not_promised_vision(self):
        """`auto` is a routing strategy, and it has failed on images.

        Labelling it `supported` is what sent the user into a 429 with no
        explanation. `unknown` is the honest answer: the gateway decides, and
        whether it can decide depends on the keys configured.
        """
        for strategy in registry.FLM_ROUTER_MODELS:
            self.assertEqual(
                registry.image_capability(f"{registry.FLM_PREFIX}{strategy}"),
                "unknown",
                msg=f"{strategy} is a strategy, not a measured vision model")

    def test_measured_vision_models_are_supported(self):
        for bare in ("gemma-4-26b-a4b-it", "gemma-4-26b-it",
                     "gemini-3.1-flash-lite", "gemini-3.5-flash",
                     "gemini-3.5-flash-lite", "gemini-3-flash-preview",
                     "gemini-3.8-flash", "gemini-3.6-flash"):
            self.assertEqual(
                registry.image_capability(f"{registry.FLM_PREFIX}{bare}"),
                "supported", msg=f"{bare} read an image live")

    def test_measured_text_only_models_are_unsupported(self):
        """These carry a vision-looking name but rejected every image.

        `llava-1.5-7b` is the clearest trap: it is a LLaVA model, and it still
        fails on this gateway, so guessing from the name would be wrong.
        """
        for bare in ("llava-1.5-7b", "llama-4-scout",
                     "moondream-3.1-9b-a2b", "gemma-2b-it-lora",
                     "gemma-7b-it-lora", "gemma-sea-lion-v4-27b",
                     "gemma-4-31b-it"):
            self.assertEqual(
                registry.image_capability(f"{registry.FLM_PREFIX}{bare}"),
                "unsupported", msg=f"{bare} rejected an image live")


class TestRouterImageFailureMessage(unittest.TestCase):
    """A 429 from the gateway must not read as "your quota is empty"."""

    def _settings(self, **extra):
        from config.settings import DEFAULTS
        s = dict(DEFAULTS)
        s.update({"model": "flm:auto", "flm_key": "K",
                  "flm_base": "http://localhost:31415/v1"})
        s.update(extra)
        return s

    def test_exhausted_routes_explains_the_real_cause(self):
        from unittest import mock
        from PIL import Image
        img = Image.new("RGB", (10, 10), "white")
        message = (
            "All models exhausted: 7 routes checked (1 rate-limited or on "
            "cooldown, 6 model lacks vision). Add more API keys or wait for "
            "rate limits to reset. Soonest reset ~23m."
        )
        with mock.patch(
            "translators.openai_compat.translate_image",
            side_effect=RuntimeError(f"400 Client Error: {message}"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                chain.translate_image(img, self._settings())
        text = str(caught.exception)
        # The upstream sentence is preserved: it names why (no vision model)
        # and when it clears (~23m), which "check your quota" never did.
        self.assertIn("lacks vision", text)
        self.assertIn("23m", text)

    def test_a_missing_vision_key_suggests_a_model_that_works(self):
        from unittest import mock
        from PIL import Image
        img = Image.new("RGB", (10, 10), "white")
        with mock.patch(
            "translators.openai_compat.translate_image",
            side_effect=RuntimeError("502 upstream_error"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                chain.translate_image(img, self._settings())
        text = str(caught.exception)
        suggestion = registry.FLM_MEASURED_VISION[0]
        self.assertIn(suggestion, text)
        self.assertIn(f"{registry.FLM_PREFIX}{suggestion}", text)

    def test_no_blind_retry_when_the_router_says_when_it_resets(self):
        """The router already failed over; retrying 3x only adds ~7s.

        Its own message carries the reset time, so the retry loop must not run.
        """
        calls = []

        def always_429(*_args, **_kwargs):
            calls.append(1)
            raise RuntimeError(
                "429 Client Error: All models exhausted ... Soonest reset ~23m")

        from unittest import mock
        from PIL import Image
        img = Image.new("RGB", (10, 10), "white")
        with mock.patch("translators.openai_compat.translate_image",
                        side_effect=always_429):
            with self.assertRaises(RuntimeError):
                chain.translate_image(img, self._settings())
        self.assertEqual(
            len(calls), 1,
            msg="one attempt: the gateway did its own failover already")


if __name__ == "__main__":
    unittest.main()
