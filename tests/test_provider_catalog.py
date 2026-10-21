import unittest

from translators import registry


class TestCredentialDrivenProviderCatalog(unittest.TestCase):
    def test_no_provider_models_are_shown_without_credentials(self):
        settings = {
            "provider_models": {
                "gemini": ["gemini-live"],
                "groq": ["groq-live"],
            },
        }

        self.assertEqual(registry.model_groups(settings), [])

    def test_only_cached_live_models_are_shown_after_connection(self):
        settings = {
            "gemini_key": "key",
            "groq_key": "key",
            "provider_models": {
                "gemini": ["gemini-live"],
                "groq": ["groq-live"],
            },
        }

        rows = dict(registry.model_groups(settings))

        self.assertTrue(rows["gemini-live"])
        self.assertTrue(rows["groq:groq-live"])
        self.assertNotIn("gemini-2.5-flash-lite", rows)
        self.assertNotIn("groq:openai/gpt-oss-20b", rows)

    def test_custom_provider_models_require_complete_connection(self):
        provider = {
            "id": "local",
            "name": "Local",
            "base_url": "http://localhost:11434/v1",
            "api_key": "key",
            "enabled": True,
            "model": "qwen3-vl:2b-instruct",
            "models": [
                "qwen3-vl:2b-instruct",
                "previously-selected-model",
                "another-old-model",
            ],
        }
        disconnected = dict(custom_providers=[{**provider, "api_key": ""}])
        connected = dict(custom_providers=[provider])

        self.assertEqual(registry.model_groups(disconnected), [])
        rows = dict(registry.model_groups(connected))
        self.assertTrue(
            rows[registry.custom_model_id("local", "qwen3-vl:2b-instruct")])
        self.assertEqual(sum(label.startswith("custom:") for label in rows), 1)
        self.assertNotIn(
            registry.custom_model_id("local", "previously-selected-model"),
            rows,
        )
        self.assertNotIn(
            registry.custom_model_id("local", "another-old-model"),
            rows,
        )

    def test_ai_provider_cards_show_selected_models_but_api_key_catalog_stays_full(self):
        providers = [
            {
                "id": "cloudflare",
                "name": "Cloudflare",
                "base_url": "https://cloudflare.example/v1",
                "api_key": "cloudflare-key",
                "model": "@cf/meta/llama-4-scout",
                "models": [
                    "@cf/meta/llama-4-scout",
                    "@cf/meta/llama-3.3-70b",
                ],
                "enabled": True,
            },
            {
                "id": "router",
                "name": "Router",
                "base_url": "https://router.example/v1",
                "api_key": "router-key",
                "model": "router/active",
                "models": ["router/active", "router/previous"],
                "enabled": True,
            },
        ]
        settings = {
            "groq_key": "groq-key",
            "provider_models": {
                "groq": ["groq/active", "groq/previous"],
            },
            "custom_providers": providers,
        }

        rows = dict(registry.model_groups(settings))

        self.assertIn("groq:groq/active", rows)
        self.assertIn("groq:groq/previous", rows)
        self.assertIn(
            registry.custom_model_id("cloudflare", "@cf/meta/llama-4-scout"),
            rows,
        )
        self.assertNotIn(
            registry.custom_model_id("cloudflare", "@cf/meta/llama-3.3-70b"),
            rows,
        )
        self.assertIn(
            registry.custom_model_id("router", "router/active"), rows)
        self.assertNotIn(
            registry.custom_model_id("router", "router/previous"), rows)


if __name__ == "__main__":
    unittest.main()
