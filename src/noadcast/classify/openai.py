"""Direct OpenAI Responses API transcript classification."""

from __future__ import annotations

import math
from typing import Any, Mapping

import httpx

from .base import TokenUsage
from .core import CallResult, TranscriptClassifier, with_repair_nudge
from .prompts import PromptSpec, SchemaViolation
from .retry import AttemptError, is_transient_status, parse_retry_after

DEFAULT_API_BASE = "https://api.openai.com/v1"
REQUEST_TIMEOUT_S = 300.0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value)) if math.isfinite(value) else 0


def usage_from_openai(metadata: Any) -> TokenUsage:
    """Keep cached input and reasoning output as subsets of their totals."""
    usage = _mapping(metadata)
    input_tokens = _count(usage.get("input_tokens"))
    output_tokens = _count(usage.get("output_tokens"))
    input_details = _mapping(usage.get("input_tokens_details"))
    cached = min(input_tokens, _count(input_details.get("cached_tokens")))
    cache_write = min(input_tokens - cached, _count(input_details.get("cache_write_tokens")))
    reasoning = min(output_tokens, _count(_mapping(usage.get("output_tokens_details")).get("reasoning_tokens")))
    return TokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        cache_write_tokens=cache_write,
        thought_tokens=reasoning,
        output_tokens=output_tokens - reasoning,
    )


def interpret_response(status: int, headers: Mapping[str, str], body: Any, spec: PromptSpec) -> CallResult:
    exchange: dict[str, Any] = {"format": "openai", "status": status, "body": body}
    retry_headers = {k.lower(): v for k, v in headers.items() if k.lower() in {"retry-after", "retry-after-ms"}}
    if retry_headers:
        exchange["headers"] = retry_headers
    payload = _mapping(body)
    usage = usage_from_openai(payload.get("usage"))
    error = _mapping(payload.get("error"))
    if not 200 <= status < 300 or payload.get("error") is not None:
        code = error.get("code")
        transient_code = code in {"server_error", "rate_limit_exceeded"}
        raise AttemptError(
            f"OpenAI HTTP {status}: {error.get('message', 'request failed')}"[:1000],
            kind="transient" if is_transient_status(status) or transient_code else "permanent",
            status=status,
            retry_after=parse_retry_after(headers),
            usage=usage,
            exchange=exchange,
        )
    if not isinstance(body, dict):
        raise AttemptError("OpenAI returned a non-JSON body", kind="transient", status=status, exchange=exchange)
    response_status = payload.get("status")
    if response_status != "completed":
        reason = _mapping(payload.get("incomplete_details")).get("reason")
        kind = "permanent" if reason == "content_filter" else "schema" if response_status == "incomplete" else "transient"
        raise AttemptError(
            f"OpenAI response did not complete (status={response_status}, reason={reason})",
            kind=kind, status=status, usage=usage, exchange=exchange,
        )
    output = payload.get("output")
    if not isinstance(output, list):
        raise AttemptError("OpenAI returned no output", kind="schema", status=status, usage=usage, exchange=exchange)
    texts: list[str] = []
    for item in output:
        message = _mapping(item)
        if message.get("type") != "message":
            continue  # Reasoning items precede assistant messages.
        if message.get("role") != "assistant" or message.get("status") not in (None, "completed"):
            raise AttemptError("OpenAI returned an incomplete message", kind="schema", status=status, usage=usage, exchange=exchange)
        content = message.get("content")
        if not isinstance(content, list):
            raise AttemptError("OpenAI returned malformed message content", kind="schema", status=status, usage=usage, exchange=exchange)
        for part in content:
            piece = _mapping(part)
            if piece.get("type") == "refusal" or piece.get("refusal"):
                raise AttemptError("OpenAI refused the classification", kind="permanent", status=status, usage=usage, exchange=exchange)
            if piece.get("type") == "output_text" and isinstance(piece.get("text"), str):
                texts.append(piece["text"])
    if not texts or not "".join(texts).strip():
        raise AttemptError("OpenAI returned no answer text", kind="schema", status=status, usage=usage, exchange=exchange)
    try:
        segments = spec.parse("".join(texts))
    except SchemaViolation as exc:
        raise AttemptError(str(exc), kind="schema", status=status, usage=usage, exchange=exchange) from exc
    return CallResult(segments, usage, exchange)


class OpenAIClassifier(TranscriptClassifier):
    provider = "openai"

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
        self._url = f"{api_base.rstrip('/')}/responses"
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)

    async def _call(self, user_text: str, repair: bool) -> CallResult:
        body: dict[str, Any] = {
            "model": self.model,
            "instructions": self.prompt.system,
            "input": with_repair_nudge(user_text, repair),
            "text": {"format": {
                "type": "json_schema", "name": "podcast_segments", "strict": True,
                "schema": self.prompt.claude_schema,
            }},
            "store": False,
            "service_tier": "default",
        }
        if self.thinking:
            body["reasoning"] = {"effort": self.thinking}
        try:
            response = await self._client.post(self._url, headers={"Authorization": f"Bearer {self._api_key}"}, json=body)
        except httpx.TimeoutException as exc:
            raise AttemptError("OpenAI request timed out", kind="transient", exchange={"format": "openai", "error": "timeout"}) from exc
        except httpx.TransportError as exc:
            raise AttemptError("OpenAI connection failed", kind="transient", exchange={"format": "openai", "error": "connection"}) from exc
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text[:2000]
        return interpret_response(response.status_code, response.headers, payload, self.prompt)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
