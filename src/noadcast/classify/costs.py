"""Versioned per-model price tables.

A classification's cost is computed once, when the call completes, at the
rates in effect on that date, and stored with ``PRICE_TABLE_VERSION``;
later price changes never rewrite history. Rates are USD per million tokens
at the standard (paid, non-batch) tier.

Sources, all read 2026-09-22:

- Gemini: https://ai.google.dev/gemini-api/docs/pricing (page last updated
  2026-09-22). ``gemini`` sends text, so it uses the text input rate;
  ``gemini-audio`` uploads audio and uses the audio rate where the page
  splits by modality. 3.5/3.6/3.7 Flash have a single input rate for all
  modalities. 3.6/3.7 Flash are on launch pricing through 2026-12-31 with
  the listed increase from 2027-01-01. Thinking bills at the output rate.
  (The app's AdDetectionProvider.swift at c1a53ce listed 3.6/3.7 Flash at
  $1.50/$9.00; the page does not.)
- Anthropic: https://platform.claude.com/docs/en/about-claude/pricing. Sonnet
  5's $2/$10 became its standard price (the increase planned for 2026-09-01
  was cancelled). Cache writes use the 5-minute rate; thinking is billed
  inside output tokens.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from ..timeutil import utc_now
from .base import TokenUsage

log = logging.getLogger(__name__)

PRICE_TABLE_VERSION = "2026-09"

_GEMINI_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing (updated 2026-09-22; retrieved 2026-09-22)"
_ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing (retrieved 2026-09-22)"
_GEMINI_PRICE_RISE = dt.date(2027, 1, 1)


@dataclass(frozen=True)
class CostBreakdown:
    input_usd: float  # uncached, cached, and cache-write input together
    thought_usd: float
    output_usd: float
    total_usd: float
    price_table_version: str = PRICE_TABLE_VERSION


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cached_input: float
    cache_write: float = 0.0

    @property
    def thought(self) -> float:
        # Both providers bill thinking as output.
        return self.output


@dataclass(frozen=True)
class PriceEntry:
    price: Price
    source: str
    effective_from: dt.date | None = None  # inclusive; None: before this table


def _gemini(*prices: tuple[dt.date | None, Price]) -> tuple[PriceEntry, ...]:
    return tuple(PriceEntry(price, _GEMINI_SOURCE, since) for since, price in prices)


def _launch_then_rise(launch: Price, standard: Price) -> tuple[PriceEntry, ...]:
    return _gemini((None, launch), (_GEMINI_PRICE_RISE, standard))


_GEMINI_36_37 = _launch_then_rise(Price(0.75, 3.75, 0.075), Price(1.50, 7.50, 0.15))

PRICES: dict[tuple[str, str], tuple[PriceEntry, ...]] = {
    ("gemini", "gemini-3-flash-preview"): _gemini((None, Price(0.50, 3.00, 0.05))),
    ("gemini", "gemini-3.5-flash"): _gemini((None, Price(1.50, 9.00, 0.15))),
    ("gemini", "gemini-3.6-flash"): _GEMINI_36_37,
    ("gemini", "gemini-3.7-flash"): _GEMINI_36_37,
    ("gemini", "gemini-3.1-flash-lite"): _gemini((None, Price(0.25, 1.50, 0.025))),
    ("gemini", "gemini-2.5-flash"): _gemini((None, Price(0.30, 2.50, 0.03))),
    ("gemini", "gemini-2.5-flash-lite"): _gemini((None, Price(0.10, 0.40, 0.01))),
    ("gemini-audio", "gemini-3-flash-preview"): _gemini((None, Price(1.00, 3.00, 0.10))),
    ("gemini-audio", "gemini-3.5-flash"): _gemini((None, Price(1.50, 9.00, 0.15))),
    ("gemini-audio", "gemini-3.6-flash"): _GEMINI_36_37,
    ("gemini-audio", "gemini-3.7-flash"): _GEMINI_36_37,
    ("gemini-audio", "gemini-3.1-flash-lite"): _gemini((None, Price(0.50, 1.50, 0.05))),
    ("gemini-audio", "gemini-2.5-flash"): _gemini((None, Price(1.00, 2.50, 0.10))),
    ("gemini-audio", "gemini-2.5-flash-lite"): _gemini((None, Price(0.30, 0.40, 0.03))),
    ("claude", "claude-sonnet-5"): (PriceEntry(Price(2.00, 10.00, 0.20, 2.50), _ANTHROPIC_SOURCE),),
    ("claude", "claude-haiku-4-5"): (PriceEntry(Price(1.00, 5.00, 0.10, 1.25), _ANTHROPIC_SOURCE),),
}

FREE_PROVIDERS = frozenset({"fake"})


def price_for(provider: str, model: str, at: dt.datetime | None = None) -> Price | None:
    """The rate in effect at ``at`` (default now), or None if unlisted."""
    entries = PRICES.get((provider, model))
    if not entries:
        return None
    day = (at or utc_now()).astimezone(dt.UTC).date()
    current = None
    for entry in entries:
        if entry.effective_from is None or entry.effective_from <= day:
            current = entry.price
    return current


def cost_for(provider: str, model: str, usage: TokenUsage, *, at: dt.datetime | None = None) -> CostBreakdown:
    """Cost of one classification at the rates in effect at ``at`` (default:
    now, i.e. when the call completes). ``usage.input_tokens`` is the whole
    prompt; cached reads and cache writes within it are priced at their own
    rates. An unlisted model costs zero and logs a warning rather than
    failing a classification that has already been paid for."""
    if provider in FREE_PROVIDERS:
        return CostBreakdown(0.0, 0.0, 0.0, 0.0)
    price = price_for(provider, model, at)
    if price is None:
        log.warning("no %s price for %s/%s; recording zero cost", PRICE_TABLE_VERSION, provider, model)
        return CostBreakdown(0.0, 0.0, 0.0, 0.0)
    uncached = max(0, usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens)
    input_usd = (
        uncached * price.input + usage.cached_input_tokens * price.cached_input + usage.cache_write_tokens * price.cache_write
    ) / 1e6
    thought_usd = usage.thought_tokens * price.thought / 1e6
    output_usd = usage.output_tokens * price.output / 1e6
    return CostBreakdown(input_usd, thought_usd, output_usd, input_usd + thought_usd + output_usd)
