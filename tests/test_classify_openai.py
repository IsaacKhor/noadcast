from __future__ import annotations

import json
import unittest
from typing import Any

import httpx

from noadcast.classify.openai import OpenAIClassifier, usage_from_openai
from noadcast.classify.prompts import REPAIR_NUDGE, get_prompt
from noadcast.classify.retry import ClassifyFailed
from tests.test_classify_helpers import Recorder, RecordingSleep, episode, httpx_client, no_jitter, segments_json

KEY = "sk-openai-test-key"
MODEL = "gpt-6-luna"
AD = {"startSeconds": 60, "endSeconds": 90, "summary": "Mattresses", "kind": "ad"}


def response(text: str | None = None, *, status: str = "completed") -> dict[str, Any]:
    return {
        "id": "resp_test", "model": MODEL, "status": status,
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "assistant", "status": "completed", "content": [
                {"type": "output_text", "text": text if text is not None else segments_json()}
            ]},
        ],
        "usage": {
            "input_tokens": 1000, "input_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 20},
            "output_tokens": 180, "output_tokens_details": {"reasoning_tokens": 80},
        },
    }


class OpenAITests(unittest.IsolatedAsyncioTestCase):
    def classifier(self, recorder: Recorder, **options: Any) -> OpenAIClassifier:
        self.sleep = RecordingSleep()
        client = httpx_client(recorder)
        self.addAsyncCleanup(client.aclose)
        return OpenAIClassifier(api_key=KEY, client=client, model=MODEL, thinking="high",
                                sleep=self.sleep, rand=no_jitter, **options)

    async def test_request_uses_responses_strict_schema_and_private_key_header(self) -> None:
        recorder = Recorder((200, response(segments_json(AD))))
        result = await self.classifier(recorder).classify(episode())
        [sent] = recorder.requests
        self.assertEqual(sent.method, "POST")
        self.assertEqual(str(sent.url), "https://api.openai.com/v1/responses")
        self.assertEqual(sent.headers["authorization"], f"Bearer {KEY}")
        self.assertNotIn(KEY, str(sent.url))
        body = recorder.bodies()[0]
        spec = get_prompt("segments-v4", "sentences")
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["instructions"], spec.system)
        self.assertIn("[60.00-75.00]", body["input"])
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["text"], {"format": {
            "type": "json_schema", "name": "podcast_segments", "strict": True, "schema": spec.claude_schema,
        }})
        self.assertIs(body["store"], False)
        self.assertEqual(body["service_tier"], "default")
        self.assertNotIn("flex", json.dumps(body))
        self.assertEqual(result.provider, "openai")
        self.assertEqual((result.usage.input_tokens, result.usage.cached_input_tokens,
                          result.usage.cache_write_tokens, result.usage.thought_tokens,
                          result.usage.output_tokens), (1000, 100, 20, 80, 100))
        self.assertEqual([(s.start_seconds, s.end_seconds) for s in result.segments], [(60, 90)])
        self.assertNotIn(KEY, json.dumps(result.raw_response))

    async def test_schema_repair_keeps_both_attempts_usage(self) -> None:
        recorder = Recorder((200, response("not json")), (200, response()))
        result = await self.classifier(recorder).classify(episode())
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.usage.input_tokens, 2000)
        self.assertEqual(result.usage.thought_tokens, 160)
        self.assertTrue(recorder.bodies()[1]["input"].endswith(REPAIR_NUDGE))

    async def test_incomplete_even_with_valid_json_never_publishes_markers(self) -> None:
        body = response(status="incomplete")
        body["incomplete_details"] = {"reason": "max_output_tokens"}
        recorder = Recorder((200, body))
        with self.assertRaises(ClassifyFailed) as caught:
            await self.classifier(recorder).classify(episode())
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(caught.exception.usage.input_tokens, 2000)

    async def test_refusal_and_content_filter_are_permanent(self) -> None:
        for reason in ("refusal", "content_filter"):
            with self.subTest(reason=reason):
                body = response()
                if reason == "refusal":
                    body["output"][1]["content"] = [{"type": "refusal", "refusal": "No"}]
                else:
                    body["status"] = "incomplete"
                    body["incomplete_details"] = {"reason": "content_filter"}
                recorder = Recorder((200, body))
                with self.assertRaises(ClassifyFailed) as caught:
                    await self.classifier(recorder).classify(episode())
                self.assertTrue(caught.exception.permanent)
                self.assertEqual(len(recorder.requests), 1)

    async def test_rate_limit_honors_retry_after(self) -> None:
        recorder = Recorder((429, {"error": {"message": "Rate limited"}}, {"Retry-After": "3"}), (200, response()))
        result = await self.classifier(recorder).classify(episode())
        self.assertEqual(result.attempts, 2)
        self.assertEqual(self.sleep.delays, [3.0])

    async def test_auth_error_is_permanent(self) -> None:
        recorder = Recorder((401, {"error": {"message": "Invalid API key"}}))
        with self.assertRaises(ClassifyFailed) as caught:
            await self.classifier(recorder).classify(episode())
        self.assertTrue(caught.exception.permanent)
        self.assertEqual(len(recorder.requests), 1)

    async def test_timeout_and_non_json_response_are_retried(self) -> None:
        for failure in (httpx.ReadTimeout("timeout"), (200, "bad response")):
            with self.subTest(failure=failure):
                recorder = Recorder(failure, (200, response()))
                result = await self.classifier(recorder).classify(episode())
                self.assertEqual(result.attempts, 2)

    async def test_shared_client_stays_open(self) -> None:
        classifier = self.classifier(Recorder((200, response())))
        await classifier.aclose()
        self.assertFalse(classifier._client.is_closed)


class UsageTests(unittest.TestCase):
    def test_usage_subsets_are_bounded_and_disjoint(self) -> None:
        usage = usage_from_openai({
            "input_tokens": 10, "input_tokens_details": {"cached_tokens": 8, "cache_write_tokens": 8},
            "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 20},
        })
        self.assertEqual((usage.input_tokens, usage.cached_input_tokens, usage.cache_write_tokens,
                          usage.thought_tokens, usage.output_tokens), (10, 8, 2, 5, 0))
        self.assertIsNone(usage.billed_cost_usd)


if __name__ == "__main__":
    unittest.main()
