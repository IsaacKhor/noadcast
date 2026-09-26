from __future__ import annotations

import datetime as dt
import unittest

from noadcast.classify.base import TokenUsage
from noadcast.classify.costs import PRICE_TABLE_VERSION, PRICES, cost_for, price_for
from noadcast.config import Settings

# The Gemini lineup in `git show c1a53ce:ios_app/Noadcast/Models/AdDetectionProvider.swift`.
APP_GEMINI_MODELS = (
    "gemini-3-flash-preview",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)


class CostTests(unittest.TestCase):
    def test_gemini_prices_cached_input_and_thinking(self) -> None:
        usage = TokenUsage(input_tokens=100_000, thought_tokens=2_000, output_tokens=1_000, cached_input_tokens=40_000)
        cost = cost_for("gemini", "gemini-3.5-flash", usage)
        # 60k uncached × $1.50 + 40k cached × $0.15; thinking and output × $9.00.
        self.assertAlmostEqual(cost.input_usd, 0.096)
        self.assertAlmostEqual(cost.thought_usd, 0.018)
        self.assertAlmostEqual(cost.output_usd, 0.009)
        self.assertAlmostEqual(cost.total_usd, 0.123)
        self.assertEqual(cost.price_table_version, PRICE_TABLE_VERSION)

    def test_claude_prices_cache_reads_and_writes(self) -> None:
        usage = TokenUsage(input_tokens=30_000, output_tokens=2_000, cached_input_tokens=5_000, cache_write_tokens=1_000)
        cost = cost_for("claude", "claude-sonnet-5", usage)
        # 24k × $2 + 5k × $0.20 + 1k × $2.50; output × $10.
        self.assertAlmostEqual(cost.input_usd, 0.0515)
        self.assertEqual(cost.thought_usd, 0.0)
        self.assertAlmostEqual(cost.output_usd, 0.02)
        self.assertAlmostEqual(cost.total_usd, 0.0715)
        haiku = cost_for("claude", "claude-haiku-4-5", TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000))
        self.assertAlmostEqual(haiku.total_usd, 6.0)

    def test_a_typical_hour_long_episode_costs_cents(self) -> None:
        usage = TokenUsage(input_tokens=19_400, thought_tokens=1_500, output_tokens=300)
        for provider, model in (("gemini", "gemini-3.5-flash"), ("claude", "claude-sonnet-5")):
            with self.subTest(model=model):
                self.assertLess(cost_for(provider, model, usage).total_usd, 0.06)

    def test_scheduled_price_changes_apply_by_call_date(self) -> None:
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        before = dt.datetime(2026, 12, 31, 23, 30, tzinfo=dt.UTC)
        after = dt.datetime(2027, 1, 1, 0, 30, tzinfo=dt.UTC)
        self.assertAlmostEqual(cost_for("gemini", "gemini-3.6-flash", usage, at=before).total_usd, 4.50)
        self.assertAlmostEqual(cost_for("gemini", "gemini-3.6-flash", usage, at=after).total_usd, 9.00)

    def test_the_audio_arm_pays_audio_input_rates(self) -> None:
        usage = TokenUsage(input_tokens=1_000_000)
        self.assertAlmostEqual(cost_for("gemini", "gemini-2.5-flash", usage).input_usd, 0.30)
        self.assertAlmostEqual(cost_for("gemini-audio", "gemini-2.5-flash", usage).input_usd, 1.00)

    def test_unknown_models_cost_nothing_and_warn(self) -> None:
        with self.assertLogs("noadcast.classify.costs", level="WARNING") as logs:
            cost = cost_for("gemini", "gemini-9-ultra", TokenUsage(input_tokens=5_000, output_tokens=100))
        self.assertEqual(cost.total_usd, 0.0)
        self.assertIn("gemini-9-ultra", logs.output[0])

    def test_the_fake_is_free_without_warnings(self) -> None:
        with self.assertNoLogs("noadcast.classify.costs", level="WARNING"):
            self.assertEqual(cost_for("fake", "fake", TokenUsage(input_tokens=5_000)).total_usd, 0.0)

    def test_every_configured_model_is_priced(self) -> None:
        defaults = Settings()
        self.assertIsNotNone(price_for("gemini", defaults.gemini_model))
        self.assertIsNotNone(price_for("claude", defaults.claude_model))
        self.assertIsNotNone(price_for("claude", "claude-haiku-4-5"))
        for model in APP_GEMINI_MODELS:
            for provider in ("gemini", "gemini-audio"):
                with self.subTest(provider=provider, model=model):
                    self.assertIsNotNone(price_for(provider, model))
        for entries in PRICES.values():
            for entry in entries:
                self.assertTrue(entry.source.startswith("https://"))


if __name__ == "__main__":
    unittest.main()
