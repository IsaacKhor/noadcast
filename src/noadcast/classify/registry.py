"""Build and cache the supported OpenRouter classification presets."""

from __future__ import annotations

import asyncio

import httpx

from ..classifier_models import MODEL_IDS, thinking_for_model
from ..config import Settings
from .base import Classifier, ClassifierError
from .openrouter import REQUEST_TIMEOUT_S, OpenRouterClassifier
from .retry import Sleep

PROVIDERS = ("openrouter",)


class ClassifierRegistry:
    """One server-only OpenRouter client; injected clients remain caller-owned.

    Presets own their reasoning setting. Legacy thinking and prompt_cache
    arguments are accepted so queued jobs cannot override a preset's effort.
    """

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None,
                 sleep: Sleep = asyncio.sleep) -> None:
        self.settings = settings
        self._http = http_client
        self._owns_http = http_client is None
        self._sleep = sleep
        self._cache: dict[str, Classifier] = {}

    def get(self, provider: str | None = None, model: str | None = None, thinking: str | None = None,
            *, prompt_cache: bool = False) -> Classifier:
        provider = provider or self.settings.classifier
        if provider != "openrouter":
            raise ClassifierError(f"unsupported classifier provider {provider!r}; use openrouter", permanent=True)
        model = model or self.settings.openrouter_model
        if model not in MODEL_IDS:
            raise ClassifierError(f"unsupported classifier model {model!r}; expected one of {MODEL_IDS}", permanent=True)
        if model not in self._cache:
            if not self.settings.openrouter_api_key:
                raise ClassifierError("OPENROUTER_API_KEY is not configured on the server", permanent=True)
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=10.0))
            self._cache[model] = OpenRouterClassifier(
                api_key=self.settings.openrouter_api_key,
                client=self._http,
                api_base=self.settings.openrouter_api_base,
                model=model,
                thinking=thinking_for_model(model),
                prompt_version=self.settings.prompt_version,
                render_format=self.settings.transcript_format,
                include_silence=self.settings.include_silence,
                max_input_tokens=self.settings.classifier_max_input_tokens,
                sleep=self._sleep,
            )
        return self._cache[model]

    def available(self) -> dict[str, bool]:
        return {"openrouter": bool(self.settings.openrouter_api_key)}

    async def aclose(self) -> None:
        classifiers, self._cache = list(self._cache.values()), {}
        for classifier in classifiers:
            await classifier.aclose()
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None
