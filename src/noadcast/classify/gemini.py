"""Gemini transcript classifier: the recovered server's ``call_gemini``.

Request shape is unchanged (``systemInstruction``, the prompt version's
``responseSchema`` with ``responseMimeType: application/json``, and
``thinkingConfig.thinkingLevel`` when set). The key travels in the
``x-goog-api-key`` header, never as ``?key=``, so it cannot land in URLs,
access logs, or exception messages.
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx

from .base import TokenUsage
from .core import CallResult, TranscriptClassifier, with_repair_nudge
from .prompts import PromptSpec, SchemaViolation
from .retry import AttemptError, is_transient_status, parse_retry_after

DEFAULT_API_BASE = "https://generativelanguage.googleapis.com"
REQUEST_TIMEOUT_S = 300.0  # the recovered server's value
_RETRY_HEADERS = ("retry-after", "retry-after-ms")


def generate_content_url(api_base: str, model: str) -> str:
    return f"{api_base.rstrip('/')}/v1beta/models/{model}:generateContent"


def generate_content_body(
    *, system: str, parts: list[dict[str, Any]], schema: dict[str, Any], thinking: str | None
) -> dict[str, Any]:
    generation_config: dict[str, Any] = {"responseMimeType": "application/json", "responseSchema": schema}
    if thinking:
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking}
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": generation_config,
    }


def usage_from_gemini(metadata: Mapping[str, Any] | None) -> TokenUsage:
    """``promptTokenCount`` already includes ``cachedContentTokenCount``;
    thinking is reported apart from ``candidatesTokenCount``."""
    metadata = metadata or {}
    return TokenUsage(
        input_tokens=int(metadata.get("promptTokenCount") or 0),
        thought_tokens=int(metadata.get("thoughtsTokenCount") or 0),
        output_tokens=int(metadata.get("candidatesTokenCount") or 0),
        cached_input_tokens=int(metadata.get("cachedContentTokenCount") or 0),
    )


def google_retry_delay(body: Any) -> float | None:
    """``google.rpc.RetryInfo.retryDelay`` (e.g. ``"37s"``) from an error body:
    Gemini's 429s carry their hint here rather than in ``Retry-After``."""
    error = body.get("error") if isinstance(body, dict) else None
    for detail in (error.get("details") if isinstance(error, dict) else None) or ():
        if isinstance(detail, dict) and str(detail.get("@type", "")).endswith("google.rpc.RetryInfo"):
            delay = detail.get("retryDelay")
            if isinstance(delay, str) and delay.endswith("s"):
                try:
                    return max(0.0, float(delay[:-1]))
                except ValueError:
                    return None
    return None


def exchange_record(status: int, headers: Mapping[str, str], body: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"format": "gemini", "status": status, "body": body}
    kept = {key.lower(): value for key, value in headers.items() if key.lower() in _RETRY_HEADERS}
    if kept:
        record["headers"] = kept
    return record


def error_for_status(status: int, headers: Mapping[str, str], body: Any, exchange: dict[str, Any]) -> AttemptError:
    error = body.get("error") if isinstance(body, dict) else None
    rpc_status = error.get("status") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else str(body)[:500]
    retry_after = parse_retry_after(headers)
    if retry_after is None:
        retry_after = google_retry_delay(body)
    return AttemptError(
        f"Gemini HTTP {status} {rpc_status or ''}: {message}"[:1000],
        kind="transient" if is_transient_status(status, rpc_status) else "permanent",
        status=status,
        retry_after=retry_after,
        exchange=exchange,
    )


def candidate_text(body: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """The first candidate's answer text (thought parts excluded) and its finishReason."""
    candidates = body.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        return None, None
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    texts = [
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str) and not part.get("thought")
    ]
    return ("".join(texts) if texts else None), candidate.get("finishReason")


def interpret_response(status: int, headers: Mapping[str, str], body: Any, spec: PromptSpec) -> CallResult:
    """One generateContent HTTP response → parsed segments, or an ``AttemptError``."""
    exchange = exchange_record(status, headers, body)
    if not 200 <= status < 300:
        raise error_for_status(status, headers, body, exchange)
    if not isinstance(body, dict):
        raise AttemptError("Gemini returned a non-JSON body", kind="transient", status=status, exchange=exchange)
    usage = usage_from_gemini(body.get("usageMetadata"))
    block_reason = (body.get("promptFeedback") or {}).get("blockReason")
    if block_reason:
        # The same prompt is blocked every time; retrying only spends quota.
        raise AttemptError(
            f"Gemini blocked the prompt: {block_reason}", kind="permanent", status=status, usage=usage, exchange=exchange
        )
    text, finish_reason = candidate_text(body)
    if text is None:
        raise AttemptError(
            f"Gemini returned no answer text (finishReason={finish_reason})",
            kind="schema",
            status=status,
            usage=usage,
            exchange=exchange,
        )
    try:
        segments = spec.parse(text)
    except SchemaViolation as exc:
        raise AttemptError(
            f"{exc} (finishReason={finish_reason})", kind="schema", status=status, usage=usage, exchange=exchange
        ) from exc
    return CallResult(segments, usage, exchange)


async def post_generate_content(
    client: httpx.AsyncClient, url: str, api_key: str, body: dict[str, Any], spec: PromptSpec
) -> CallResult:
    try:
        response = await client.post(url, headers={"x-goog-api-key": api_key}, json=body)
    except httpx.TimeoutException as exc:
        raise AttemptError(
            f"Gemini request timed out: {exc!r}", kind="transient", exchange={"format": "gemini", "error": "timeout"}
        ) from exc
    except httpx.TransportError as exc:
        raise AttemptError(
            f"Gemini connection failed: {exc!r}", kind="transient", exchange={"format": "gemini", "error": "connection"}
        ) from exc
    try:
        payload: Any = response.json()
    except ValueError:
        payload = response.text[:2000]
    return interpret_response(response.status_code, response.headers, payload, spec)


class GeminiClassifier(TranscriptClassifier):
    """``options`` are ``TranscriptClassifier``'s (model, thinking, prompt
    version, render format, silence, chunking threshold, retry hooks). A
    shared ``client`` is left open by ``aclose``."""

    provider = "gemini"

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
        self._url = generate_content_url(api_base, self.model)
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)

    async def _call(self, user_text: str, repair: bool) -> CallResult:
        body = generate_content_body(
            system=self.prompt.system,
            parts=[{"text": with_repair_nudge(user_text, repair)}],
            schema=self.prompt.gemini_schema,
            thinking=self.thinking,
        )
        return await post_generate_content(self._client, self._url, self._api_key, body, self.prompt)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
