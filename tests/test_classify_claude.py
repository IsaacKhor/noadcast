from __future__ import annotations

import unittest
from typing import Any

import httpx2

from noadcast.classify.base import ClassifierError, TokenUsage
from noadcast.classify.claude import MAX_TOKENS, ClaudeClassifier, thinking_params
from noadcast.classify.prompts import REPAIR_NUDGE, V2_INDEX_PROMPT, get_prompt
from noadcast.classify.retry import ClassifyFailed
from tests.test_classify_helpers import (
    Recorder,
    RecordingSleep,
    claude_body,
    claude_error,
    episode,
    httpx2_client,
    no_jitter,
    segments_json,
)

INTRO = {"startLine": 0, "endLine": 3, "startSeconds": 0, "endSeconds": 20, "summary": "Billboard", "kind": "intro"}


def answer(*rows: dict[str, Any], **usage: Any) -> tuple[int, dict[str, Any]]:
    return 200, claude_body(segments_json(*rows), **usage)


class ClaudeTestCase(unittest.IsolatedAsyncioTestCase):
    def classifier(self, recorder: Recorder, model: str = "claude-sonnet-5", **options: Any) -> ClaudeClassifier:
        self.sleep = RecordingSleep()
        options = {"prompt_version": "segments-v2", "render_format": "index", "include_silence": True, **options}
        classifier = ClaudeClassifier(
            api_key="sk-ant-test", http_client=httpx2_client(recorder), model=model, sleep=self.sleep,
            rand=no_jitter, **options,
        )
        self.addAsyncCleanup(classifier.aclose)
        return classifier


class RequestTests(ClaudeTestCase):
    async def test_request_shape_for_sonnet_5(self) -> None:
        recorder = Recorder(answer(INTRO))
        result = await self.classifier(recorder).classify(episode())

        [request] = recorder.requests
        self.assertEqual(request.url.path, "/v1/messages")
        self.assertEqual(request.headers["x-api-key"], "sk-ant-test")
        self.assertEqual(request.headers["x-stainless-retry-count"], "0")
        body = recorder.bodies()[0]
        self.assertEqual(body["model"], "claude-sonnet-5")
        self.assertEqual(body["max_tokens"], MAX_TOKENS)
        self.assertEqual(body["system"], V2_INDEX_PROMPT)
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertEqual(
            body["output_config"],
            {"format": {"type": "json_schema", "schema": get_prompt("segments-v2", "index").claude_schema}},
        )
        [message] = body["messages"]
        self.assertEqual(message["role"], "user")
        self.assertIn("The complete episode ends at 185.00 seconds.", message["content"])
        self.assertNotIn("temperature", body)
        self.assertNotIn("cache_control", str(body))
        self.assertEqual([(s.start_seconds, s.end_seconds) for s in result.segments], [(0.0, 20.0)])
        self.assertEqual((result.provider, result.model, result.attempts), ("claude", "claude-sonnet-5", 1))

    async def test_sonnet_effort_follows_the_thinking_level(self) -> None:
        for level, effort in (("minimal", "low"), ("low", "low"), ("medium", "medium"), ("high", "high")):
            with self.subTest(level=level):
                recorder = Recorder(answer())
                await self.classifier(recorder, thinking=level).classify(episode())
                body = recorder.bodies()[0]
                self.assertEqual(body["thinking"], {"type": "adaptive"})
                self.assertEqual(body["output_config"]["effort"], effort)

    async def test_haiku_takes_a_budget_and_no_effort(self) -> None:
        recorder = Recorder(answer(model="claude-haiku-4-5"))
        await self.classifier(recorder, model="claude-haiku-4-5", thinking="low").classify(episode())
        body = recorder.bodies()[0]
        self.assertEqual(body["thinking"], {"type": "enabled", "budget_tokens": 2048})
        self.assertNotIn("effort", body["output_config"])

        recorder = Recorder(answer(model="claude-haiku-4-5"))
        await self.classifier(recorder, model="claude-haiku-4-5").classify(episode())
        body = recorder.bodies()[0]
        self.assertNotIn("thinking", body)
        self.assertNotIn("effort", body["output_config"])

    async def test_prompt_cache_is_opt_in(self) -> None:
        recorder = Recorder(answer(cache_write=900))
        result = await self.classifier(recorder, prompt_cache=True).classify(episode())
        self.assertEqual(recorder.bodies()[0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(result.usage.cache_write_tokens, 900)

    def test_haiku_budgets_stay_inside_the_api_limits(self) -> None:
        for level in ("minimal", "low", "medium", "high"):
            thinking, effort = thinking_params("claude-haiku-4-5", level)
            self.assertIsNone(effort)
            self.assertTrue(1024 <= thinking["budget_tokens"] < MAX_TOKENS)
        with self.assertRaises(ClassifierError):
            thinking_params("claude-haiku-4-5", "max")

    def test_a_missing_key_is_permanent(self) -> None:
        with self.assertRaises(ClassifierError) as caught:
            ClaudeClassifier(api_key=None, model="claude-sonnet-5")
        self.assertTrue(caught.exception.permanent)


class ResponseTests(ClaudeTestCase):
    async def test_usage_mapping(self) -> None:
        recorder = Recorder(answer(INTRO, input_tokens=21000, output_tokens=1500, cache_read=300, cache_write=200,
                                   thinking_tokens=1200))
        result = await self.classifier(recorder).classify(episode())
        # input_tokens is the whole prompt; thinking is inside output_tokens.
        self.assertEqual(
            result.usage,
            TokenUsage(input_tokens=21500, thought_tokens=0, output_tokens=1500, cached_input_tokens=300,
                       cache_write_tokens=200),
        )
        body = result.raw_response["exchanges"][0]["body"]
        self.assertEqual(body["usage"]["output_tokens_details"], {"thinking_tokens": 1200})

    async def test_a_refusal_is_permanent(self) -> None:
        refusal = claude_body(None, stop_reason="refusal")
        refusal["stop_details"] = {"type": "refusal", "category": "cyber", "explanation": None}
        recorder = Recorder((200, refusal))
        with self.assertRaises(ClassifyFailed) as caught:
            await self.classifier(recorder).classify(episode())
        self.assertTrue(caught.exception.permanent)
        self.assertIn("cyber", str(caught.exception))

    async def test_a_truncated_answer_gets_one_repair_attempt(self) -> None:
        truncated = claude_body('{"segments": [{"startLine": 0, "endLi', stop_reason="max_tokens")
        recorder = Recorder((200, truncated), answer(INTRO))
        result = await self.classifier(recorder).classify(episode())
        self.assertEqual(result.attempts, 2)
        self.assertTrue(recorder.bodies()[1]["messages"][0]["content"].endswith(REPAIR_NUDGE))
        self.assertEqual(result.usage.input_tokens, 2000)


class RetryTests(ClaudeTestCase):
    async def test_429_with_retry_after_then_success(self) -> None:
        recorder = Recorder((429, claude_error(429, "rate_limit_error"), {"retry-after": "4"}), answer(INTRO))
        result = await self.classifier(recorder).classify(episode())
        self.assertEqual(result.attempts, 2)
        self.assertEqual(self.sleep.delays, [4.0])
        self.assertEqual(len(recorder.requests), 2)  # the SDK itself never retried

    async def test_overloaded_is_retried(self) -> None:
        recorder = Recorder((529, claude_error(529, "overloaded_error")), answer(INTRO))
        result = await self.classifier(recorder).classify(episode())
        self.assertEqual((result.attempts, self.sleep.delays), (2, [2.0]))

    async def test_401_is_permanent(self) -> None:
        recorder = Recorder((401, claude_error(401, "authentication_error", "invalid x-api-key")))
        with self.assertRaises(ClassifyFailed) as caught:
            await self.classifier(recorder).classify(episode())
        self.assertTrue(caught.exception.permanent)
        self.assertEqual(len(recorder.requests), 1)

    async def test_connection_errors_are_retried(self) -> None:
        recorder = Recorder(httpx2.ConnectError("refused"), answer(INTRO))
        result = await self.classifier(recorder).classify(episode())
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.raw_response["exchanges"][0]["error"], "connection")


if __name__ == "__main__":
    unittest.main()
