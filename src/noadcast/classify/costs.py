"""OpenRouter charges, with versioned fallback estimates for supported models.

Actual ``usage.cost`` is preferred and accumulated across retries/chunks.
Fallback rates are a snapshot of https://openrouter.ai/api/v1/models retrieved
2026-09-26, in USD per million tokens. Routing, context tiers and later price
changes can differ; the provider-reported charge remains authoritative.
Historical classifications retain their previously stored costs and versions.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from .base import TokenUsage

log = logging.getLogger(__name__)

PRICE_TABLE_VERSION = "2026-09-26-openrouter"
OPENROUTER_REPORTED_VERSION = "openrouter-reported-v1"


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
        # Reasoning tokens are part of billed completion tokens.
        return self.output


@dataclass(frozen=True)
class PriceEntry:
    price: Price
    source: str


_OPENROUTER_SOURCE = "https://openrouter.ai/api/v1/models (retrieved 2026-09-26)"
PRICES: dict[tuple[str, str], tuple[PriceEntry, ...]] = {
    ("openrouter", "deepseek/deepseek-v4.1-flash"): (
        PriceEntry(Price(0.035, 0.29, 0.001), _OPENROUTER_SOURCE),
    ),
    ("openrouter", "qwen/qwen3.8-flash"): (
        PriceEntry(Price(0.15, 0.47, 0.016, 0.20), _OPENROUTER_SOURCE),
    ),
    ("openrouter", "openai/gpt-6-luna"): (
        PriceEntry(Price(0.10, 0.50, 0.01, 0.125), _OPENROUTER_SOURCE),
    ),
}


def price_for(provider: str, model: str, at: dt.datetime | None = None) -> Price | None:
    """The versioned fallback estimate, or None for an unsupported model."""
    entries = PRICES.get((provider, model))
    return entries[0].price if entries else None


def cost_for(provider: str, model: str, usage: TokenUsage, *, at: dt.datetime | None = None) -> CostBreakdown:
    """Actual charge, or a versioned estimate when usage.cost is unavailable.

    Input includes cached reads/writes, which get their respective rates.
    An unlisted model logs a warning rather than failing a completed call.
    """
    if provider == "openrouter" and usage.billed_cost_usd is not None:
        # OpenRouter reports one total charge, not a per-token-type split.
        # Zero component amounts mean unavailable, never a fabricated split.
        return CostBreakdown(0.0, 0.0, 0.0, usage.billed_cost_usd, OPENROUTER_REPORTED_VERSION)
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
