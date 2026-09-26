from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from noadcast.classify.base import ClassifierError
from noadcast.classify.claude import ClaudeClassifier
from noadcast.classify.fake import FakeClassifier
from noadcast.classify.gemini import GeminiClassifier
from noadcast.classify.gemini_audio import GeminiAudioClassifier
from noadcast.classify.registry import ClassifierRegistry
from noadcast.config import settings_for_tests
from tests.test_classify_helpers import (
    FIXTURES,
    Recorder,
    RecordingSleep,
    episode,
    gemini_body,
    httpx_client,
    request,
    segments_json,
)


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)

    def registry(self, **overrides) -> ClassifierRegistry:
        registry = ClassifierRegistry(settings_for_tests(self.data, **overrides))
        self.addAsyncCleanup(registry.aclose)
        return registry

    def test_available_reports_configured_keys(self) -> None:
        self.assertEqual(
            self.registry().available(), {"gemini": False, "claude": False, "gemini-audio": False, "fake": True}
        )
        self.assertEqual(
            self.registry(gemini_api_key="g", anthropic_api_key="a").available(),
            {"gemini": True, "claude": True, "gemini-audio": True, "fake": True},
        )

    def test_missing_keys_are_permanent_errors(self) -> None:
        registry = self.registry()
        for provider in ("gemini", "claude", "gemini-audio"):
            with self.subTest(provider=provider), self.assertRaises(ClassifierError) as caught:
                registry.get(provider)
            self.assertTrue(caught.exception.permanent)
            self.assertIn("_API_KEY", str(caught.exception))

    def test_unknown_provider_is_a_permanent_error(self) -> None:
        with self.assertRaises(ClassifierError) as caught:
            self.registry().get("openai")
        self.assertTrue(caught.exception.permanent)

    def test_defaults_come_from_settings(self) -> None:
        registry = self.registry(
            gemini_api_key="g", anthropic_api_key="a", classifier="claude", thinking_level="medium",
            prompt_version="segments-v2", transcript_format="seconds", include_silence=False,
            classifier_max_input_tokens=50_000,
        )
        default = registry.get()
        self.assertIsInstance(default, ClaudeClassifier)
        self.assertEqual((default.model, default.thinking), ("claude-sonnet-5", "medium"))
        self.assertEqual((default.prompt.version, default.prompt.render_format), ("segments-v2", "seconds"))
        self.assertEqual((default.include_silence, default.max_input_tokens), (False, 50_000))
        gemini = registry.get("gemini")
        self.assertIsInstance(gemini, GeminiClassifier)
        self.assertEqual(gemini.model, "gemini-3.5-flash")
        self.assertIsInstance(registry.get("gemini-audio"), GeminiAudioClassifier)
        self.assertIsInstance(registry.get("fake"), FakeClassifier)
        self.assertEqual(registry.get("fake").model, "fake")

    def test_production_defaults_use_joined_sentences_without_silence(self) -> None:
        classifier = self.registry(gemini_api_key="g").get("gemini")
        self.assertEqual((classifier.prompt.version, classifier.prompt.render_format),
                         ("segments-v3", "sentences"))
        self.assertFalse(classifier.include_silence)

    def test_classifiers_are_cached_per_provider_model_and_thinking(self) -> None:
        registry = self.registry(gemini_api_key="g", anthropic_api_key="a")
        first = registry.get("gemini")
        self.assertIs(registry.get("gemini", "gemini-3.5-flash"), first)
        self.assertIsNot(registry.get("gemini", "gemini-3.6-flash"), first)
        self.assertIsNot(registry.get("gemini", thinking="high"), first)
        self.assertIs(registry.get("gemini", thinking="high"), registry.get("gemini", thinking="high"))
        haiku = registry.get("claude", "claude-haiku-4-5", "low")
        self.assertIs(registry.get("claude", "claude-haiku-4-5", "low"), haiku)
        self.assertIsNot(registry.get("claude"), haiku)

    def test_prompt_cache_only_changes_claude(self) -> None:
        registry = self.registry(gemini_api_key="g", anthropic_api_key="a")
        cached = registry.get("claude", prompt_cache=True)
        self.assertTrue(cached.prompt_cache)
        self.assertIsNot(cached, registry.get("claude"))
        self.assertFalse(registry.get("claude").prompt_cache)
        self.assertIs(registry.get("gemini", prompt_cache=True), registry.get("gemini"))

    def test_a_prompt_format_mismatch_fails_at_build_time(self) -> None:
        with self.assertRaises(ClassifierError) as caught:
            self.registry(prompt_version="segments-v1", transcript_format="index").get("fake")
        self.assertTrue(caught.exception.permanent)

    async def test_injected_client_serves_gemini_and_is_left_open(self) -> None:
        recorder = Recorder((200, gemini_body(segments_json())))
        client = httpx_client(recorder)
        self.addAsyncCleanup(client.aclose)
        registry = ClassifierRegistry(settings_for_tests(self.data, gemini_api_key="g"), http_client=client,
                                      sleep=RecordingSleep())
        result = await registry.get("gemini").classify(episode())
        self.assertEqual((result.provider, len(recorder.requests)), ("gemini", 1))
        await registry.aclose()
        self.assertFalse(client.is_closed)

    async def test_aclose_closes_owned_clients(self) -> None:
        registry = ClassifierRegistry(settings_for_tests(self.data, gemini_api_key="g", anthropic_api_key="a"))
        registry.get("gemini")
        claude = registry.get("claude")
        owned_http: httpx.AsyncClient = registry._http  # type: ignore[assignment]
        await registry.aclose()
        self.assertTrue(owned_http.is_closed)
        self.assertTrue(claude._client.is_closed())

    async def test_fake_uses_injected_fixtures(self) -> None:
        registry = ClassifierRegistry(settings_for_tests(self.data), fake_fixtures=FIXTURES, sleep=RecordingSleep())
        self.addAsyncCleanup(registry.aclose)
        req = request(episode().sentences, duration=3566.55, title="449: Middle School")
        result = await registry.get("fake").classify(req)
        self.assertEqual(result.attempts, 2)


if __name__ == "__main__":
    unittest.main()
