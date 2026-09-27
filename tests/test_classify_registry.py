from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from noadcast.classifier_models import DEFAULT_MODEL, MODEL_IDS
from noadcast.classify.base import ClassifierError
from noadcast.classify.openrouter import OpenRouterClassifier
from noadcast.classify.registry import ClassifierRegistry
from noadcast.config import settings_for_tests
from tests.test_classify_helpers import Recorder, RecordingSleep, episode, httpx_client
from tests.test_classify_openrouter import response


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)

    def registry(self, **overrides) -> ClassifierRegistry:
        registry = ClassifierRegistry(settings_for_tests(self.data, **overrides))
        self.addAsyncCleanup(registry.aclose)
        return registry

    def test_available_reports_only_openrouter(self) -> None:
        self.assertEqual(self.registry().available(), {"openrouter": False})
        self.assertEqual(self.registry(openrouter_api_key="test").available(), {"openrouter": True})

    def test_missing_key_is_permanent(self) -> None:
        with self.assertRaises(ClassifierError) as caught:
            self.registry().get()
        self.assertTrue(caught.exception.permanent)
        self.assertIn("OPENROUTER_API_KEY", str(caught.exception))

    def test_retired_providers_and_unsupported_models_fail_before_network(self) -> None:
        registry = self.registry(openrouter_api_key="test")
        for provider in ("gemini", "claude", "fake", "gemini-audio"):
            with self.subTest(provider=provider), self.assertRaises(ClassifierError) as caught:
                registry.get(provider)
            self.assertTrue(caught.exception.permanent)
        with self.assertRaises(ClassifierError):
            registry.get("openrouter", "google/gemini-3.5-flash")
        self.assertIsNone(registry._http)

    def test_defaults_use_deepseek_and_joined_sentences(self) -> None:
        classifier = self.registry(openrouter_api_key="test").get()
        self.assertIsInstance(classifier, OpenRouterClassifier)
        self.assertEqual(classifier.model, DEFAULT_MODEL)
        self.assertEqual((classifier.prompt.version, classifier.prompt.render_format), ("segments-v3", "sentences"))
        self.assertFalse(classifier.include_silence)

    def test_three_presets_enforce_reasoning_and_cache_by_model(self) -> None:
        registry = self.registry(openrouter_api_key="test")
        for model in MODEL_IDS:
            classifier = registry.get(model=model, thinking="low")
            self.assertEqual(classifier.thinking, "high" if model == "openai/gpt-6-luna" else None)
            self.assertIs(classifier, registry.get(model=model, thinking="high", prompt_cache=True))
        self.assertEqual(len(registry._cache), 3)

    async def test_injected_client_classifies_and_stays_open(self) -> None:
        recorder = Recorder((200, response()))
        client = httpx_client(recorder)
        self.addAsyncCleanup(client.aclose)
        registry = ClassifierRegistry(settings_for_tests(self.data, openrouter_api_key="test"),
                                      http_client=client, sleep=RecordingSleep())
        self.addAsyncCleanup(registry.aclose)
        result = await registry.get(model="openai/gpt-6-luna", thinking="low").classify(episode())
        self.assertEqual(result.provider, "openrouter")
        self.assertEqual(recorder.bodies()[0]["reasoning"], {"effort": "high"})
        self.assertEqual(result.thinking, "high")
        await registry.aclose()
        self.assertFalse(client.is_closed)

    async def test_owned_client_closes(self) -> None:
        registry = self.registry(openrouter_api_key="test")
        registry.get()
        client = registry._http
        await registry.aclose()
        self.assertTrue(client.is_closed)
