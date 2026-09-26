from __future__ import annotations

import datetime as dt
import unittest

from noadcast.classify.base import ClassifierError, TokenUsage
from noadcast.classify.retry import (
    AttemptError,
    ClassifyFailed,
    RetryPolicy,
    is_transient_status,
    parse_retry_after,
    run_with_retry,
)
from tests.test_classify_helpers import RecordingSleep, no_jitter


class Script:
    """A provider call that fails with the scripted errors, then returns "ok"."""

    def __init__(self, *errors: AttemptError) -> None:
        self.errors = list(errors)
        self.repairs: list[bool] = []

    async def __call__(self, repair: bool) -> str:
        self.repairs.append(repair)
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


def transient(status: int = 503, retry_after: float | None = None) -> AttemptError:
    return AttemptError(f"HTTP {status}", kind="transient", status=status, retry_after=retry_after)


def schema(tokens: int = 0) -> AttemptError:
    return AttemptError("bad JSON", kind="schema", status=200, usage=TokenUsage(input_tokens=tokens, output_tokens=10))


class RunWithRetryTests(unittest.IsolatedAsyncioTestCase):
    async def run_script(self, script: Script, **kwargs):
        sleep = RecordingSleep()
        outcome = await run_with_retry(script, sleep=sleep, rand=no_jitter, **kwargs)
        return outcome, sleep

    async def test_429_with_retry_after_then_success_waits_exactly_that_long(self) -> None:
        outcome, sleep = await self.run_script(Script(transient(429, retry_after=3.0)))
        self.assertEqual((outcome.value, outcome.attempts), ("ok", 2))
        self.assertEqual(sleep.delays, [3.0])
        self.assertEqual(outcome.log[0]["delay_s"], 3.0)

    async def test_permanent_errors_are_never_retried(self) -> None:
        script = Script(AttemptError("HTTP 401", kind="permanent", status=401))
        sleep = RecordingSleep()
        with self.assertRaises(ClassifyFailed) as caught:
            await run_with_retry(script, sleep=sleep)
        self.assertTrue(caught.exception.permanent)
        self.assertIsInstance(caught.exception, ClassifierError)
        self.assertEqual((caught.exception.attempts, len(script.repairs), sleep.delays), (1, 1, []))

    async def test_one_immediate_repair_attempt_on_a_schema_violation(self) -> None:
        script = Script(schema(tokens=500))
        outcome, sleep = await self.run_script(script)
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(script.repairs, [False, True])
        self.assertEqual(sleep.delays, [])
        self.assertEqual(outcome.failed_usage, TokenUsage(input_tokens=500, output_tokens=10))

    async def test_a_second_schema_violation_is_permanent(self) -> None:
        script = Script(schema(), schema())
        with self.assertRaises(ClassifyFailed) as caught:
            await run_with_retry(script, sleep=RecordingSleep())
        self.assertTrue(caught.exception.permanent)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(caught.exception.usage.output_tokens, 20)

    async def test_the_repair_nudge_stays_on_after_a_transient_failure(self) -> None:
        script = Script(schema(), transient())
        outcome, _ = await self.run_script(script)
        self.assertEqual((outcome.attempts, script.repairs), (3, [False, True, True]))

    async def test_transient_failures_back_off_then_hand_over_to_the_job(self) -> None:
        script = Script(*(transient(503) for _ in range(4)))
        sleep = RecordingSleep()
        with self.assertRaises(ClassifyFailed) as caught:
            await run_with_retry(script, sleep=sleep, rand=no_jitter)
        self.assertFalse(caught.exception.permanent)
        self.assertIsNone(caught.exception.retry_after)
        self.assertEqual(caught.exception.attempts, 4)
        self.assertEqual(sleep.delays, [2.0, 4.0, 8.0])

    async def test_last_retry_after_is_passed_to_the_job_scheduler(self) -> None:
        script = Script(*(transient(429, retry_after=1.0) for _ in range(4)))
        with self.assertRaises(ClassifyFailed) as caught:
            await run_with_retry(script, sleep=RecordingSleep())
        self.assertEqual(caught.exception.retry_after, 1.0)

    async def test_a_long_retry_after_goes_straight_to_the_job_scheduler(self) -> None:
        script = Script(transient(429, retry_after=120.0))
        sleep = RecordingSleep()
        with self.assertRaises(ClassifyFailed) as caught:
            await run_with_retry(script, sleep=sleep)
        self.assertFalse(caught.exception.permanent)
        self.assertEqual((caught.exception.retry_after, caught.exception.attempts, sleep.delays), (120.0, 1, []))

    async def test_a_schema_violation_on_the_last_attempt_is_not_permanent(self) -> None:
        script = Script(transient(), transient(), transient(), schema())
        with self.assertRaises(ClassifyFailed) as caught:
            await run_with_retry(script, sleep=RecordingSleep(), rand=no_jitter)
        self.assertFalse(caught.exception.permanent)


class BackoffTests(unittest.TestCase):
    def test_backoff_doubles_from_two_seconds_and_caps_at_sixty(self) -> None:
        policy = RetryPolicy()
        self.assertEqual([policy.backoff(n, no_jitter) for n in range(7)], [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0])

    def test_jitter_scales_between_half_and_one_and_a_half(self) -> None:
        policy = RetryPolicy()
        self.assertEqual(policy.backoff(0, lambda low, high: low), 1.0)
        self.assertEqual(policy.backoff(10, lambda low, high: high), 90.0)


class StatusPolicyTests(unittest.TestCase):
    def test_transient_and_permanent_statuses(self) -> None:
        for status in (408, 409, 429, 500, 502, 503, 504, 529):
            with self.subTest(status=status):
                self.assertTrue(is_transient_status(status))
        for status in (400, 401, 403, 404, 413, 422):
            with self.subTest(status=status):
                self.assertFalse(is_transient_status(status))

    def test_gemini_rpc_statuses(self) -> None:
        self.assertTrue(is_transient_status(400, "RESOURCE_EXHAUSTED"))
        self.assertTrue(is_transient_status(400, "UNAVAILABLE"))
        self.assertFalse(is_transient_status(400, "INVALID_ARGUMENT"))

    def test_parse_retry_after(self) -> None:
        now = dt.datetime(2026, 9, 22, 12, 0, 0, tzinfo=dt.UTC)
        self.assertEqual(parse_retry_after({"Retry-After": "7"}), 7.0)
        self.assertEqual(parse_retry_after({"retry-after": "1.5"}), 1.5)
        self.assertEqual(parse_retry_after({"retry-after-ms": "250", "retry-after": "9"}), 0.25)
        self.assertEqual(parse_retry_after({"Retry-After": "Tue, 22 Sep 2026 12:00:30 GMT"}, now=now), 30.0)
        self.assertEqual(parse_retry_after({"Retry-After": "Tue, 22 Sep 2026 11:00:00 GMT"}, now=now), 0.0)
        self.assertIsNone(parse_retry_after({"Retry-After": "soon"}))
        self.assertIsNone(parse_retry_after({}))


if __name__ == "__main__":
    unittest.main()
