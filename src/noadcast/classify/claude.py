"""Claude transcript classifier on the Anthropic SDK (``anthropic`` 1.x).

Structured outputs constrain decoding to the prompt version's JSON Schema,
which removes the "was the JSON fenced" class of bug. The request is
``messages.create(output_config={"format": ...})`` rather than the
``messages.parse`` helper because ``parse`` validates inside the SDK and, on
a violation, raises without the ``Message``, losing the usage a rejected
answer still bills and its ``stop_reason``. Validation here is the same
pydantic step ``parse`` would run.

Thinking differs per model: Claude Sonnet 5 (and every current model except
Haiku 4.5) thinks adaptively, takes ``output_config.effort``, and rejects
``budget_tokens``; Haiku 4.5 takes ``budget_tokens`` and errors on
``effort``. Production requests are not cached: the system prompt is below
the minimum cacheable prefix and each transcript is unique, so a cache write
would cost 1.25x with nothing ever read back. The eval's repeats send the
same request several times in a row, so they may opt in (``prompt_cache``):
the first call writes the prompt at 1.25x and repeats within five minutes
read it at 0.1x. The SDK's own retries are off (``max_retries=0``); retry.py
owns retrying.
"""

from __future__ import annotations

from typing import Any, Mapping

import anthropic
import httpx2
from anthropic.types import Message, Usage

from .base import ClassifierError, TokenUsage
from .core import CallResult, TranscriptClassifier, with_repair_nudge
from .prompts import PromptSpec, SchemaViolation
from .retry import AttemptError, is_transient_status, parse_retry_after

# Room for adaptive thinking plus the JSON answer, and below the ~21k tokens
# the SDK allows without streaming.
MAX_TOKENS = 16_000
REQUEST_TIMEOUT_S = 600.0  # the SDK's own non-streaming default
_BUDGET_THINKING_MODELS = ("claude-haiku-4-5",)
# Haiku 4.5 budgets: at least 1024 and below MAX_TOKENS.
HAIKU_THINKING_BUDGETS = {"minimal": 1024, "low": 2048, "medium": 4096, "high": 8192}
# Effort has no "minimal"; low is its floor.
EFFORT_FOR_LEVEL = {"minimal": "low", "low": "low", "medium": "medium", "high": "high"}
_RETRY_HEADERS = ("retry-after", "retry-after-ms")


def thinking_params(model: str, level: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """(``thinking`` parameter, ``output_config.effort``) for a thinking level;
    None means omit."""
    if model.startswith(_BUDGET_THINKING_MODELS):
        if level is None:
            return None, None
        budget = HAIKU_THINKING_BUDGETS.get(level)
        if budget is None:
            raise ClassifierError(f"thinking level {level!r} has no token budget for {model}", permanent=True)
        return {"type": "enabled", "budget_tokens": budget}, None
    effort = None if level is None else EFFORT_FOR_LEVEL.get(level, level)
    return {"type": "adaptive"}, effort


def usage_from_anthropic(usage: Usage) -> TokenUsage:
    """Anthropic's ``input_tokens`` excludes cache reads and writes; add them
    so ``input_tokens`` is the whole prompt for every provider. Thinking is
    billed inside ``output_tokens`` with no separate count, so
    ``thought_tokens`` stays 0 (``output_tokens_details`` is kept in the raw
    response for observability)."""
    cache_read = usage.cache_read_input_tokens or 0
    cache_write = usage.cache_creation_input_tokens or 0
    return TokenUsage(
        input_tokens=usage.input_tokens + cache_read + cache_write,
        thought_tokens=0,
        output_tokens=usage.output_tokens,
        cached_input_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def interpret_message(message: Message, spec: PromptSpec) -> CallResult:
    exchange = {"format": "claude", "status": 200, "body": message.to_dict()}
    usage = usage_from_anthropic(message.usage)
    if message.stop_reason == "refusal":
        category = message.stop_details.category if message.stop_details else None
        raise AttemptError(
            f"Claude declined the request (category={category})", kind="permanent", status=200, usage=usage, exchange=exchange
        )
    text = next((block.text for block in message.content if block.type == "text"), None)
    if text is None:
        raise AttemptError(
            f"Claude returned no text block (stop_reason={message.stop_reason})",
            kind="schema",
            status=200,
            usage=usage,
            exchange=exchange,
        )
    try:
        segments = spec.parse(text)
    except SchemaViolation as exc:
        # Constrained decoding makes this rare; a max_tokens stop truncates the JSON.
        raise AttemptError(
            f"{exc} (stop_reason={message.stop_reason})", kind="schema", status=200, usage=usage, exchange=exchange
        ) from exc
    return CallResult(segments, usage, exchange)


def error_for_status(status: int, headers: Mapping[str, str], body: Any) -> AttemptError:
    error = body.get("error") if isinstance(body, dict) else None
    error_type = error.get("type") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else str(body)[:500]
    exchange: dict[str, Any] = {"format": "claude", "status": status, "body": body}
    kept = {key.lower(): value for key, value in headers.items() if key.lower() in _RETRY_HEADERS}
    if kept:
        exchange["headers"] = kept
    return AttemptError(
        f"Anthropic HTTP {status} {error_type or ''}: {message}"[:1000],
        kind="transient" if is_transient_status(status) else "permanent",
        status=status,
        retry_after=parse_retry_after(headers),
        exchange=exchange,
    )


def interpret_response(status: int, headers: Mapping[str, str], body: Any, spec: PromptSpec) -> CallResult:
    """A Messages API HTTP exchange as recorded (used when replaying fixtures)."""
    if not 200 <= status < 300:
        raise error_for_status(status, headers, body)
    return interpret_message(Message.model_validate(body), spec)


def attempt_error(exc: anthropic.APIError) -> AttemptError:
    if isinstance(exc, anthropic.APITimeoutError):
        return AttemptError(
            f"Anthropic request timed out: {exc}", kind="transient", exchange={"format": "claude", "error": "timeout"}
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return AttemptError(
            f"Anthropic connection failed: {exc}", kind="transient", exchange={"format": "claude", "error": "connection"}
        )
    if isinstance(exc, anthropic.APIStatusError):
        return error_for_status(exc.status_code, exc.response.headers, exc.body)
    return AttemptError(f"Anthropic client error: {exc}", kind="transient")


class ClaudeClassifier(TranscriptClassifier):
    """``options`` are ``TranscriptClassifier``'s. Pass either ``api_key`` (a
    client is created and owned) or a shared ``client`` (left open by
    ``aclose``). ``http_client`` injects an ``httpx2`` transport for tests;
    ``prompt_cache`` is for the eval's repeated identical requests."""

    provider = "claude"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
        http_client: httpx2.AsyncClient | None = None,
        prompt_cache: bool = False,
        **options: Any,
    ) -> None:
        super().__init__(**options)
        self._thinking, self._effort = thinking_params(self.model, self.thinking)
        self.prompt_cache = prompt_cache
        self._owns_client = client is None
        if client is None:
            if not api_key:
                # Without an explicit key the SDK would fall back to ambient
                # credentials (env vars, CLI profiles); never do that silently.
                raise ClassifierError("ANTHROPIC_API_KEY is not configured", permanent=True)
            client = anthropic.AsyncAnthropic(
                api_key=api_key, max_retries=0, timeout=REQUEST_TIMEOUT_S, http_client=http_client
            )
        self._client = client

    async def _call(self, user_text: str, repair: bool) -> CallResult:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": self.prompt.claude_schema}}
        if self._effort is not None:
            output_config["effort"] = self._effort
        extra: dict[str, Any] = {} if self._thinking is None else {"thinking": self._thinking}
        if self.prompt_cache:
            extra["cache_control"] = {"type": "ephemeral"}
        try:
            message = await self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=self.prompt.system,
                messages=[{"role": "user", "content": with_repair_nudge(user_text, repair)}],
                output_config=output_config,  # type: ignore[arg-type]
                **extra,
            )
        except anthropic.APIError as exc:
            raise attempt_error(exc) from exc
        return interpret_message(message, self.prompt)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.close()
