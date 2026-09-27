from __future__ import annotations

import unittest

from noadcast.config import Settings


class ClassificationConfigTests(unittest.TestCase):
    def test_feed_interval_environment_default_and_bounds(self) -> None:
        base = {"NOADCAST_ALLOW_NO_AUTH": "1"}
        self.assertEqual(Settings.from_mapping(base).feed_interval_minutes, 30)
        self.assertEqual(Settings.from_mapping({**base, "NOADCAST_FEED_INTERVAL_MINUTES": "7"}).feed_interval_minutes, 7)
        for value in ("0", "1441", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings.from_mapping({**base, "NOADCAST_FEED_INTERVAL_MINUTES": value})

    def test_openrouter_configuration_and_secret_redaction(self) -> None:
        settings = Settings.from_mapping({
            "NOADCAST_ALLOW_NO_AUTH": "1",
            "NOADCAST_CLASSIFIER": "openrouter",
            "OPENROUTER_API_KEY": "test-openrouter-secret",
            "NOADCAST_OPENROUTER_MODEL": "qwen/qwen3.8-flash",
            "OPENROUTER_API_BASE": "https://router.example/api/v1",
        })
        self.assertEqual(settings.classifier, "openrouter")
        self.assertEqual(settings.openrouter_api_key, "test-openrouter-secret")
        self.assertEqual(settings.openrouter_model, "qwen/qwen3.8-flash")
        self.assertEqual(settings.openrouter_api_base, "https://router.example/api/v1")
        self.assertNotIn("test-openrouter-secret", repr(settings))

    def test_openrouter_defaults_and_empty_key(self) -> None:
        settings = Settings.from_mapping({"NOADCAST_ALLOW_NO_AUTH": "1", "OPENROUTER_API_KEY": ""})
        self.assertEqual(settings.classifier, "openrouter")
        self.assertIsNone(settings.openrouter_api_key)
        self.assertEqual(settings.openrouter_model, "deepseek/deepseek-v4.1-flash")
        self.assertEqual(settings.openrouter_api_base, "https://openrouter.ai/api/v1")

    def test_rejects_retired_provider_or_unknown_model(self) -> None:
        for override in ({"NOADCAST_CLASSIFIER": "gemini"}, {"NOADCAST_CLASSIFIER": "fake"},
                         {"NOADCAST_OPENROUTER_MODEL": "google/gemini-3.5-flash"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                Settings.from_mapping({"NOADCAST_ALLOW_NO_AUTH": "1", **override})
