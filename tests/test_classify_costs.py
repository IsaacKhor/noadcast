from __future__ import annotations

import unittest

from noadcast.classify.base import TokenUsage
from noadcast.classify.costs import PRICE_TABLE_VERSION, PRICES, cost_for, price_for


class CostTests(unittest.TestCase):
    def test_reported_charge_includes_retries_and_chunks(self) -> None:
        usage = TokenUsage() + TokenUsage(input_tokens=100, billed_cost_usd=0.001)
        usage += TokenUsage(input_tokens=200, billed_cost_usd=0.002)
        cost = cost_for("openrouter", "deepseek/deepseek-v4.1-flash", usage)
        self.assertEqual(usage.input_tokens, 300)
        self.assertAlmostEqual(cost.total_usd, 0.003)
        self.assertEqual(cost.price_table_version, "openrouter-reported-v1")
        self.assertEqual((cost.input_usd, cost.thought_usd, cost.output_usd), (0, 0, 0))

    def test_zero_charge_and_missing_charge_are_distinct(self) -> None:
        free = cost_for("openrouter", "deepseek/deepseek-v4.1-flash", TokenUsage(input_tokens=100, billed_cost_usd=0.0))
        self.assertEqual(free.total_usd, 0.0)
        self.assertEqual(free.price_table_version, "openrouter-reported-v1")
        missing = TokenUsage() + TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        self.assertIsNone(missing.billed_cost_usd)
        fallback = cost_for("openrouter", "deepseek/deepseek-v4.1-flash", missing)
        self.assertAlmostEqual(fallback.total_usd, 0.325)
        self.assertEqual(fallback.price_table_version, PRICE_TABLE_VERSION)
        partial = TokenUsage(input_tokens=100, billed_cost_usd=0.001) + TokenUsage(input_tokens=200)
        self.assertIsNone(partial.billed_cost_usd)

    def test_fallback_prices_cache_and_reasoning_without_double_counting(self) -> None:
        usage = TokenUsage(input_tokens=100_000, cached_input_tokens=40_000,
                           cache_write_tokens=10_000, thought_tokens=2_000, output_tokens=1_000)
        cost = cost_for("openrouter", "qwen/qwen3.8-flash", usage)
        self.assertAlmostEqual(cost.input_usd, (50_000 * .15 + 40_000 * .016 + 10_000 * .20) / 1e6)
        self.assertAlmostEqual(cost.thought_usd, 2_000 * .47 / 1e6)
        self.assertAlmostEqual(cost.output_usd, 1_000 * .47 / 1e6)
        self.assertAlmostEqual(cost.total_usd, cost.input_usd + cost.thought_usd + cost.output_usd)

    def test_every_supported_model_has_a_documented_fallback(self) -> None:
        models = {"deepseek/deepseek-v4.1-flash", "qwen/qwen3.8-flash", "openai/gpt-6-luna"}
        self.assertEqual(set(PRICES), {("openrouter", model) for model in models})
        for model in models:
            self.assertIsNotNone(price_for("openrouter", model))
            self.assertTrue(PRICES[("openrouter", model)][0].source.startswith("https://openrouter.ai/"))

    def test_unknown_model_warns_when_no_charge_was_reported(self) -> None:
        with self.assertLogs("noadcast.classify.costs", level="WARNING"):
            cost = cost_for("openrouter", "unlisted", TokenUsage(input_tokens=100))
        self.assertEqual(cost.total_usd, 0)


if __name__ == "__main__":
    unittest.main()
