"""In-call retry shared by every provider.

Provider adapters turn each failed call into an ``AttemptError`` of one of
three kinds, using ``is_transient_status`` so the policy lives in one place:

- ``transient``: 408/409/429/5xx, Gemini ``RESOURCE_EXHAUSTED``/``UNAVAILABLE``,
  timeouts, connection errors. Retried with backoff
  ``min(2**n * 2 s, 60 s) * jitter(0.5-1.5)``, or after exactly the
  provider's ``Retry-After``. A hint longer than the backoff cap is handed
  straight to the job scheduler (``ClassifierError.retry_after``) rather than
  held in-call.
- ``schema``: the answer arrived but is not the JSON object the schema
  describes. Retried once, immediately, with a "return only the JSON object"
  nudge; a second violation is permanent.
- ``permanent``: 400/401/403/404 and every other status. Never retried.

At most ``max_attempts`` calls are made; after that job-level backoff takes
over. Sleeping and jitter are injectable so tests never wait.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Generic, Literal, Mapping, TypeVar

from .base import ClassifierError, TokenUsage

log = logging.getLogger(__name__)

T = TypeVar("T")
AttemptKind = Literal["transient", "schema", "permanent"]
Sleep = Callable[[float], Awaitable[None]]
Jitter = Callable[[float, float], float]

TRANSIENT_STATUSES = frozenset({408, 409, 429})
TRANSIENT_RPC_STATUSES = frozenset({"RESOURCE_EXHAUSTED", "UNAVAILABLE"})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 2.0
    max_delay_s: float = 60.0
    jitter: tuple[float, float] = (0.5, 1.5)

    def backoff(self, retry_number: int, rand: Jitter = random.uniform) -> float:
        """Delay before retry ``retry_number`` (0-based) when the provider gave no hint."""
        return min(self.base_delay_s * 2**retry_number, self.max_delay_s) * rand(*self.jitter)


class AttemptError(Exception):
    """One provider call failed. ``usage`` is what that call billed (a schema
    violation still costs its tokens); ``exchange`` is its raw record."""

    def __init__(
        self,
        message: str,
        *,
        kind: AttemptKind,
        status: int | None = None,
        retry_after: float | None = None,
        usage: TokenUsage = TokenUsage(),
        exchange: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.usage = usage
        self.exchange = exchange


class ClassifyFailed(ClassifierError):
    """A ``ClassifierError`` that also reports what the failed call cost."""

    def __init__(
        self,
        message: str,
        *,
        permanent: bool,
        retry_after: float | None = None,
        attempts: int = 0,
        usage: TokenUsage = TokenUsage(),
        log: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message, permanent=permanent, retry_after=retry_after)
        self.attempts = attempts
        self.usage = usage
        self.log = log or []


@dataclass
class RetryOutcome(Generic[T]):
    value: T
    attempts: int
    failed_usage: TokenUsage  # billed by attempts whose answer was rejected
    exchanges: list[dict[str, Any]] = field(default_factory=list)  # raw records of failed attempts
    log: list[dict[str, Any]] = field(default_factory=list)


def is_transient_status(status: int, rpc_status: str | None = None) -> bool:
    return status in TRANSIENT_STATUSES or status >= 500 or rpc_status in TRANSIENT_RPC_STATUSES


def parse_retry_after(headers: Mapping[str, str], now: dt.datetime | None = None) -> float | None:
    """Seconds to wait from ``retry-after-ms`` or ``Retry-After`` (seconds or
    an HTTP date); None when absent or unparseable."""
    lowered = {key.lower(): value for key, value in headers.items()}
    millis = lowered.get("retry-after-ms")
    if millis is not None:
        try:
            return max(0.0, float(millis) / 1000)
        except ValueError:
            pass
    value = lowered.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return max(0.0, (when - (now or dt.datetime.now(dt.UTC))).total_seconds())


async def run_with_retry(
    call: Callable[[bool], Awaitable[T]],
    *,
    policy: RetryPolicy = RetryPolicy(),
    sleep: Sleep = asyncio.sleep,
    rand: Jitter = random.uniform,
) -> RetryOutcome[T]:
    """Run ``call(repair)`` until it succeeds or the policy gives up.
    ``repair`` is True once a schema violation has been seen, and stays True,
    so every later attempt carries the nudge. Raises ``ClassifyFailed``."""
    failed_usage = TokenUsage()
    exchanges: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    repair = False
    retries = 0
    last: AttemptError | None = None

    def failure(message: str, *, permanent: bool, attempts: int, retry_after: float | None = None) -> ClassifyFailed:
        return ClassifyFailed(
            message, permanent=permanent, retry_after=retry_after, attempts=attempts, usage=failed_usage, log=history
        )

    for attempt in range(1, policy.max_attempts + 1):
        try:
            value = await call(repair)
        except AttemptError as err:
            last = err
            failed_usage += err.usage
            if err.exchange is not None:
                exchanges.append({"attempt": attempt, **err.exchange})
            entry: dict[str, Any] = {"attempt": attempt, "kind": err.kind, "status": err.status, "message": str(err)[:500]}
            history.append(entry)
            if err.kind == "permanent":
                raise failure(str(err), permanent=True, attempts=attempt) from err
            if err.kind == "schema":
                if repair:
                    raise failure(f"schema violation after a repair attempt: {err}", permanent=True, attempts=attempt) from err
                log.warning("attempt %d returned an unusable answer (%s); retrying with a repair nudge", attempt, err)
                repair = True
                continue
            if attempt == policy.max_attempts:
                break
            if err.retry_after is not None and err.retry_after > policy.max_delay_s:
                raise failure(
                    f"provider asked to wait {err.retry_after:.0f}s: {err}",
                    permanent=False,
                    attempts=attempt,
                    retry_after=err.retry_after,
                ) from err
            delay = err.retry_after if err.retry_after is not None else policy.backoff(retries, rand)
            retries += 1
            entry["retry_after"] = err.retry_after
            entry["delay_s"] = delay
            log.warning("attempt %d failed (%s); retrying in %.1f s", attempt, err, delay)
            await sleep(delay)
        else:
            return RetryOutcome(value, attempt, failed_usage, exchanges, history)

    assert last is not None
    raise failure(
        f"gave up after {policy.max_attempts} attempts: {last}",
        permanent=False,
        attempts=policy.max_attempts,
        retry_after=last.retry_after,
    ) from last
