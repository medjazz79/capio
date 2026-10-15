"""Focused regression tests for Gemini image translation."""
import unittest
from unittest import mock

import requests
from PIL import Image

from translators import gemini


class TestGeminiImageOutputBudget(unittest.TestCase):
    def test_image_translation_requests_an_expanded_output_budget(self):
        response = mock.Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"candidates": [{
            "content": {"parts": [{"text": "translated"}]},
            "finishReason": "STOP",
        }]}

        with mock.patch.object(requests, "post", return_value=response) as post:
            result = gemini.translate_image(
                Image.new("RGB", (100, 100), "white"),
                "test-key",
                "gemini-3.5-flash-lite",
                source_language="en",
                target_language="ar",
            )

        self.assertEqual(result, "translated")
        generation_config = post.call_args.kwargs["json"]["generationConfig"]
        self.assertEqual(
            generation_config["maxOutputTokens"],
            gemini.IMAGE_MAX_OUTPUT_TOKENS,
        )
        self.assertGreater(gemini.IMAGE_MAX_OUTPUT_TOKENS, 4096)
