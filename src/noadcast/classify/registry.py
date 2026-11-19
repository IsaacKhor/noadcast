"""Build and cache the supported classification presets."""

from __future__ import annotations

import asyncio

import httpx

from ..classifier_models import PROVIDER_MODEL_IDS, thinking_for_model
from ..config import Settings
from .base import Classifier, ClassifierError
from .openrouter import REQUEST_TIMEOUT_S, OpenRouterClassifier
from .openai import OpenAIClassifier
from .retry import Sleep

PROVIDERS = tuple(PROVIDER_MODEL_IDS)


class ClassifierRegistry:
    """Server-only provider clients; injected HTTP clients remain caller-owned.

    Presets own their reasoning setting. Legacy thinking and prompt_cache
    arguments are accepted so queued jobs cannot override a preset's effort.
    """

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None,
                 sleep: Sleep = asyncio.sleep) -> None:
        self.settings = settings
        self._http = http_client
        self._owns_http = http_client is None
        self._sleep = sleep
        self._cache: dict[tuple[str, str, bool], Classifier] = {}

    def get(self, provider: str | None = None, model: str | None = None, thinking: str | None = None,
            *, prompt_cache: bool = False, flex_enabled: bool | None = None) -> Classifier:
        provider = provider or self.settings.classifier
        if provider not in PROVIDER_MODEL_IDS:
            raise ClassifierError(f"unsupported classifier provider {provider!r}; expected one of {PROVIDERS}", permanent=True)
        model = model or (self.settings.openrouter_model if provider == "openrouter" else self.settings.openai_model)
        if model not in PROVIDER_MODEL_IDS[provider]:
            raise ClassifierError(f"unsupported classifier model {model!r} for {provider}; expected one of {PROVIDER_MODEL_IDS[provider]}", permanent=True)
        flex = (self.settings.openrouter_flex_enabled if flex_enabled is None else flex_enabled) if provider == "openrouter" else False
        key = (provider, model, flex)
        if key not in self._cache:
            api_key = self.settings.openrouter_api_key if provider == "openrouter" else self.settings.openai_api_key
            if not api_key:
                key_name = "OPENROUTER_API_KEY" if provider == "openrouter" else "OPENAI_API_KEY"
                raise ClassifierError(f"{key_name} is not configured on the server", permanent=True)
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=10.0))
            classifier_type = OpenRouterClassifier if provider == "openrouter" else OpenAIClassifier
            self._cache[key] = classifier_type(
                api_key=api_key,
                client=self._http,
                api_base=self.settings.openrouter_api_base if provider == "openrouter" else self.settings.openai_api_base,
                model=model,
                **({"flex_enabled": flex} if provider == "openrouter" else {}),
                thinking=thinking_for_model(model),
                prompt_version=self.settings.prompt_version,
                render_format=self.settings.transcript_format,
                include_silence=self.settings.include_silence,
                max_input_tokens=self.settings.classifier_max_input_tokens,
                sleep=self._sleep,
            )
        return self._cache[key]

    def available(self) -> dict[str, bool]:
        return {"openrouter": bool(self.settings.openrouter_api_key), "openai": bool(self.settings.openai_api_key)}

    async def aclose(self) -> None:
        classifiers, self._cache = list(self._cache.values()), {}
        for classifier in classifiers:
            await classifier.aclose()
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None
