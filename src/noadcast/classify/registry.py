"""Builds and caches classifiers from settings."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import anthropic
import httpx
import httpx2

from ..config import Settings
from .base import Classifier, ClassifierError
from .claude import REQUEST_TIMEOUT_S as CLAUDE_TIMEOUT_S, ClaudeClassifier
from .fake import FakeClassifier
from .gemini import REQUEST_TIMEOUT_S as GEMINI_TIMEOUT_S, GeminiClassifier
from .gemini_audio import GeminiAudioClassifier
from .retry import Sleep

PROVIDERS: tuple[str, ...] = ("gemini", "claude", "gemini-audio", "fake")
FAKE_MODEL = "fake"


class ClassifierRegistry:
    """``get()`` returns a cached classifier per (provider, model, thinking).
    Provider is one of gemini | claude | gemini-audio | fake; None means
    settings.classifier, and model/thinking default from settings too. Prompt
    version, transcript format, silence lines, and the chunking threshold
    come from settings: build a registry from ``settings.with_overrides(...)``
    for another arm. ``prompt_cache`` (eval-only) makes Claude cache its
    prompt for repeated identical requests; Gemini caches repeated prefixes
    implicitly, so the flag changes nothing there.

    The injection points are for tests and the offline e2e: ``http_client``
    (an ``httpx.AsyncClient``, e.g. over ``httpx.MockTransport``) for both
    Gemini classifiers, ``anthropic_http_client`` (an ``httpx2.AsyncClient``:
    the Anthropic SDK is built on httpx2), the fake's ``fake_fixtures``
    directory, and ``sleep`` for retry backoff. Injected clients are left open
    by ``aclose``."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        anthropic_http_client: httpx2.AsyncClient | None = None,
        fake_fixtures: Path | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self._http = http_client
        self._owns_http = http_client is None
        self._anthropic_http = anthropic_http_client
        self._anthropic: anthropic.AsyncAnthropic | None = None
        self._fake_fixtures = fake_fixtures
        self._sleep = sleep
        self._cache: dict[tuple[str, str, str | None, bool], Classifier] = {}

    def get(
        self,
        provider: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
        *,
        prompt_cache: bool = False,
    ) -> Classifier:
        provider = provider or self.settings.classifier
        model = model or self._default_model(provider)
        thinking = thinking if thinking is not None else self.settings.thinking_level
        prompt_cache = prompt_cache and provider == "claude"
        key = (provider, model, thinking, prompt_cache)
        classifier = self._cache.get(key)
        if classifier is None:
            classifier = self._cache[key] = self._build(provider, model, thinking, prompt_cache)
        return classifier

    def available(self) -> dict[str, bool]:
        """Provider -> whether its API key is configured (fake is always True)."""
        gemini = bool(self.settings.gemini_api_key)
        return {"gemini": gemini, "claude": bool(self.settings.anthropic_api_key), "gemini-audio": gemini, "fake": True}

    async def aclose(self) -> None:
        classifiers, self._cache = list(self._cache.values()), {}
        for classifier in classifiers:
            await classifier.aclose()
        if self._anthropic is not None:
            # The SDK's close() closes the httpx2 client it wraps, injected or not.
            if self._anthropic_http is None:
                await self._anthropic.close()
            self._anthropic = None
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    def _default_model(self, provider: str) -> str:
        if provider in ("gemini", "gemini-audio"):
            return self.settings.gemini_model
        if provider == "claude":
            return self.settings.claude_model
        if provider == "fake":
            return FAKE_MODEL
        raise ClassifierError(f"unknown classifier provider {provider!r}; expected one of {PROVIDERS}", permanent=True)

    def _build(self, provider: str, model: str, thinking: str | None, prompt_cache: bool) -> Classifier:
        s = self.settings
        options: dict[str, Any] = dict(
            model=model,
            thinking=thinking,
            prompt_version=s.prompt_version,
            render_format=s.transcript_format,
            include_silence=s.include_silence,
            max_input_tokens=s.classifier_max_input_tokens,
            sleep=self._sleep,
        )
        if provider == "gemini":
            return GeminiClassifier(
                api_key=self._require(s.gemini_api_key, "GEMINI_API_KEY"),
                client=self._gemini_http(),
                api_base=s.gemini_api_base,
                **options,
            )
        if provider == "claude":
            return ClaudeClassifier(client=self._anthropic_client(), prompt_cache=prompt_cache, **options)
        if provider == "gemini-audio":
            return GeminiAudioClassifier(
                model=model,
                api_key=self._require(s.gemini_api_key, "GEMINI_API_KEY"),
                client=self._gemini_http(),
                api_base=s.gemini_api_base,
                thinking=thinking,
                sleep=self._sleep,
            )
        if provider == "fake":
            return FakeClassifier(fixtures_dir=self._fake_fixtures, **options)
        raise ClassifierError(f"unknown classifier provider {provider!r}; expected one of {PROVIDERS}", permanent=True)

    @staticmethod
    def _require(key: str | None, name: str) -> str:
        if not key:
            raise ClassifierError(f"{name} is not configured on the server", permanent=True)
        return key

    def _gemini_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(GEMINI_TIMEOUT_S, connect=10.0))
        return self._http

    def _anthropic_client(self) -> anthropic.AsyncAnthropic:
        if self._anthropic is None:
            self._anthropic = anthropic.AsyncAnthropic(
                api_key=self._require(self.settings.anthropic_api_key, "ANTHROPIC_API_KEY"),
                max_retries=0,
                timeout=CLAUDE_TIMEOUT_S,
                http_client=self._anthropic_http,
            )
        return self._anthropic
