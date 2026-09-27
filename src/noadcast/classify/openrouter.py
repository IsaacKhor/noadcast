"""OpenRouter transcript classification using its OpenAI-compatible API.

Usage accounting: https://openrouter.ai/docs/guides/guides/usage-accounting
Reasoning tokens are a subset of completion_tokens; cached tokens are a
subset of prompt_tokens. Keep both totals disjoint in our provider-neutral
usage representation. The account charge is usage.cost, not upstream cost.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

import httpx

from .base import TokenUsage
from .core import CallResult, TranscriptClassifier, with_repair_nudge
from .prompts import PromptSpec, SchemaViolation
from .retry import AttemptError, is_transient_status, parse_retry_after

DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
REQUEST_TIMEOUT_S = 300.0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value)) if math.isfinite(value) else 0


def usage_from_openrouter(metadata: Any) -> TokenUsage:
    metadata = _mapping(metadata)
    completion = _count(metadata.get("completion_tokens"))
    thoughts = min(completion, _count(_mapping(metadata.get("completion_tokens_details")).get("reasoning_tokens")))
    prompt = _count(metadata.get("prompt_tokens"))
    prompt_details = _mapping(metadata.get("prompt_tokens_details"))
    cached = min(prompt, _count(prompt_details.get("cached_tokens")))
    cost = metadata.get("cost")
    billed = (
        float(cost)
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0
        else None
    )
    return TokenUsage(
        input_tokens=prompt,
        thought_tokens=thoughts,
        output_tokens=completion - thoughts,
        cached_input_tokens=cached,
        cache_write_tokens=min(prompt - cached, _count(prompt_details.get("cache_write_tokens"))),
        billed_cost_usd=billed,
    )


def interpret_response(status: int, headers: Mapping[str, str], body: Any, spec: PromptSpec) -> CallResult:
    exchange: dict[str, Any] = {"format": "openrouter", "status": status, "body": body}
    retry_headers = {k.lower(): v for k, v in headers.items() if k.lower() in {"retry-after", "retry-after-ms"}}
    if retry_headers:
        exchange["headers"] = retry_headers
    payload = _mapping(body)
    usage = usage_from_openrouter(payload.get("usage"))
    error = _mapping(payload.get("error"))
    if not 200 <= status < 300 or payload.get("error") is not None:
        # OpenRouter can return an error envelope under HTTP 200 after an
        # upstream connection has started. Classify its embedded status too.
        code = error.get("code")
        effective_status = code if isinstance(code, int) and 400 <= code <= 599 else status
        raise AttemptError(
            f"OpenRouter HTTP {status}: {error.get('message', 'request failed')}"[:1000],
            kind="transient" if is_transient_status(effective_status) else "permanent",
            status=effective_status,
            retry_after=parse_retry_after(headers),
            usage=usage,
            exchange=exchange,
        )
    if not isinstance(body, dict):
        raise AttemptError("OpenRouter returned a non-JSON body", kind="transient", status=status, exchange=exchange)
    choices = payload.get("choices")
    choice = _mapping(choices[0]) if isinstance(choices, list) and choices else {}
    message = _mapping(choice.get("message"))
    finish = choice.get("finish_reason")
    if message.get("refusal") or finish == "content_filter":
        raise AttemptError(
            "OpenRouter refused the classification", kind="permanent", status=status, usage=usage, exchange=exchange
        )
    # Even parseable JSON can be an incomplete list when a generation hits
    # its token cap. Only a normally completed answer may publish markers.
    if finish != "stop":
        raise AttemptError(
            f"OpenRouter did not complete the answer (finish_reason={finish})",
            kind="schema", status=status, usage=usage, exchange=exchange,
        )
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise AttemptError(
            "OpenRouter returned no answer text", kind="schema", status=status, usage=usage, exchange=exchange
        )
    try:
        segments = spec.parse(text)
    except SchemaViolation as exc:
        raise AttemptError(str(exc), kind="schema", status=status, usage=usage, exchange=exchange) from exc
    return CallResult(segments, usage, exchange)


class OpenRouterClassifier(TranscriptClassifier):
    provider = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        api_base: str = DEFAULT_API_BASE,
        **options: Any,
    ) -> None:
        super().__init__(**options)
        self._api_key = api_key
        self._url = f"{api_base.rstrip('/')}/chat/completions"
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)

    async def _call(self, user_text: str, repair: bool) -> CallResult:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.prompt.system},
                {"role": "user", "content": with_repair_nudge(user_text, repair)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "podcast_segments",
                    "strict": True,
                    # This property is standard JSON Schema, also used by Claude.
                    "schema": self.prompt.claude_schema,
                },
            },
            "provider": {"require_parameters": True},
            "stream": False,
        }
        if self.thinking:
            body["reasoning"] = {"effort": self.thinking}
        try:
            response = await self._client.post(self._url, headers={"Authorization": f"Bearer {self._api_key}"}, json=body)
        except httpx.TimeoutException as exc:
            raise AttemptError(
                "OpenRouter request timed out", kind="transient", exchange={"format": "openrouter", "error": "timeout"}
            ) from exc
        except httpx.TransportError as exc:
            raise AttemptError(
                "OpenRouter connection failed", kind="transient", exchange={"format": "openrouter", "error": "connection"}
            ) from exc
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text[:2000]
        return interpret_response(response.status_code, response.headers, payload, self.prompt)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
