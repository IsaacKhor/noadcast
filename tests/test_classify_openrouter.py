from __future__ import annotations

import json
import unittest
from typing import Any

import httpx

from noadcast.classify.core import request_sha256
from noadcast.classify.openrouter import OpenRouterClassifier, usage_from_openrouter
from noadcast.classify.prompts import REPAIR_NUDGE, get_prompt
from noadcast.classify.retry import ClassifyFailed
from tests.test_classify_helpers import (
    Recorder,
    RecordingSleep,
    episode,
    httpx_client,
    no_jitter,
    request,
    sentence,
    segments_json,
)

KEY = "sk-or-test-key"
MODEL = "google/gemini-3.5-flash"
AD = {"startSeconds": 60, "endSeconds": 90, "summary": "Mattresses", "kind": "ad"}


def response(text: str | None = None, *, finish: str = "stop", cost: float = 0.012) -> dict[str, Any]:
    return {
        "id": "gen-test",
        "model": MODEL,
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": text if text is not None else segments_json()}}],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 180,
            "prompt_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 20},
            "completion_tokens_details": {"reasoning_tokens": 80},
            "cost": cost,
            "cost_details": {"upstream_inference_cost": 99.0},
        },
    }


class OpenRouterTests(unittest.IsolatedAsyncioTestCase):
    def classifier(self, recorder: Recorder, **options: Any) -> OpenRouterClassifier:
        self.sleep = RecordingSleep()
        client = httpx_client(recorder)
        self.addAsyncCleanup(client.aclose)
        return OpenRouterClassifier(api_key=KEY, client=client, model=MODEL,
                                    sleep=self.sleep, rand=no_jitter, **options)

    async def test_request_uses_strict_schema_and_server_header(self) -> None:
        recorder = Recorder((200, response(segments_json(AD))))
        result = await self.classifier(recorder, thinking="low").classify(episode())
        [sent] = recorder.requests
        self.assertEqual(sent.method, "POST")
        self.assertEqual(str(sent.url), "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(sent.headers["authorization"], f"Bearer {KEY}")
        self.assertNotIn(KEY, str(sent.url))
        body = recorder.bodies()[0]
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertEqual(body["provider"], {"require_parameters": True})
        self.assertFalse(body["stream"])
        spec = get_prompt("segments-v3", "sentences")
        self.assertEqual(body["response_format"], {
            "type": "json_schema",
            "json_schema": {"name": "podcast_segments", "strict": True, "schema": spec.claude_schema},
        })
        self.assertEqual(body["messages"][0], {"role": "system", "content": spec.system})
        text = body["messages"][1]["content"]
        self.assertIn("[60.00-75.00]", text)
        self.assertEqual(result.request_sha256, request_sha256(
            provider="openrouter", model=MODEL, thinking="low", prompt_version="segments-v3",
            render_format="sentences", include_silence=False, text=text,
        ))
        self.assertEqual(result.provider, "openrouter")
        self.assertEqual([(s.start_seconds, s.end_seconds) for s in result.segments], [(60, 90)])
        self.assertNotIn(KEY, json.dumps(result.raw_response))

    async def test_unspecified_reasoning_is_omitted(self) -> None:
        recorder = Recorder((200, response()))
        await self.classifier(recorder).classify(episode())
        self.assertNotIn("reasoning", recorder.bodies()[0])

    async def test_usage_excludes_reasoning_from_visible_output_and_preserves_charge(self) -> None:
        result = await self.classifier(Recorder((200, response()))).classify(episode())
        self.assertEqual((result.usage.input_tokens, result.usage.thought_tokens, result.usage.output_tokens),
                         (1000, 80, 100))
        self.assertEqual((result.usage.cached_input_tokens, result.usage.cache_write_tokens), (100, 20))
        self.assertEqual(result.usage.billed_cost_usd, 0.012)

    async def test_schema_repair_keeps_usage_and_charge_of_both_attempts(self) -> None:
        recorder = Recorder((200, response("not json", cost=0.01)), (200, response(segments_json(AD), cost=0.02)))
        result = await self.classifier(recorder).classify(episode())
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.usage.input_tokens, 2000)
        self.assertEqual(result.usage.thought_tokens + result.usage.output_tokens, 360)
        self.assertAlmostEqual(result.usage.billed_cost_usd, 0.03)
        self.assertTrue(recorder.bodies()[1]["messages"][1]["content"].endswith(REPAIR_NUDGE))
        self.assertEqual(len(result.raw_response["exchanges"]), 2)
        self.assertEqual(self.sleep.delays, [])

    async def test_parseable_but_truncated_answer_is_rejected(self) -> None:
        recorder = Recorder((200, response(finish="length")))
        with self.assertRaises(ClassifyFailed) as caught:
            await self.classifier(recorder).classify(episode())
        self.assertTrue(caught.exception.permanent)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertAlmostEqual(caught.exception.usage.billed_cost_usd, 0.024)

    async def test_refusal_and_content_filter_are_permanent(self) -> None:
        for refusal in (True, False):
            with self.subTest(refusal=refusal):
                body = response(finish="stop" if refusal else "content_filter")
                if refusal:
                    body["choices"][0]["message"]["refusal"] = "Cannot classify"
                recorder = Recorder((200, body))
                with self.assertRaises(ClassifyFailed) as caught:
                    await self.classifier(recorder).classify(episode())
                self.assertTrue(caught.exception.permanent)
                self.assertEqual(len(recorder.requests), 1)
                self.assertEqual(caught.exception.usage.billed_cost_usd, 0.012)

    async def test_missing_and_malformed_choices_never_succeed(self) -> None:
        for choices in (None, {}, [], [None], [{"finish_reason": "stop", "message": {"content": None}}]):
            with self.subTest(choices=choices):
                body = response()
                body["choices"] = choices
                with self.assertRaises(ClassifyFailed) as caught:
                    await self.classifier(Recorder((200, body))).classify(episode())
                self.assertTrue(caught.exception.permanent)

    async def test_transient_http_error_honors_retry_after(self) -> None:
        recorder = Recorder((429, {"error": {"message": "Rate limited"}}, {"Retry-After": "3"}), (200, response()))
        result = await self.classifier(recorder).classify(episode())
        self.assertEqual(result.attempts, 2)
        self.assertEqual(self.sleep.delays, [3.0])
        self.assertEqual(result.usage.billed_cost_usd, 0.012)

    async def test_http_200_error_envelope_uses_embedded_status(self) -> None:
        recorder = Recorder((200, {"error": {"code": 503, "message": "Provider unavailable"}}), (200, response()))
        result = await self.classifier(recorder).classify(episode())
        self.assertEqual(result.attempts, 2)
        self.assertEqual(self.sleep.delays, [2.0])

    async def test_auth_payment_and_model_errors_are_not_retried(self) -> None:
        for status in (400, 401, 402, 403, 404):
            with self.subTest(status=status):
                recorder = Recorder((status, {"error": {"message": "Request rejected"}}))
                with self.assertRaises(ClassifyFailed) as caught:
                    await self.classifier(recorder).classify(episode())
                self.assertTrue(caught.exception.permanent)
                self.assertEqual(len(recorder.requests), 1)

    async def test_transport_failures_and_non_json_response_are_retried(self) -> None:
        for failure in (httpx.ReadTimeout("timeout"), httpx.ConnectError("connection"), (200, "unavailable")):
            with self.subTest(failure=type(failure).__name__):
                recorder = Recorder(failure, (200, response()))
                result = await self.classifier(recorder).classify(episode())
                self.assertEqual(result.attempts, 2)
                self.assertEqual(self.sleep.delays, [2.0])

    async def test_chunks_aggregate_reported_cost_and_usage(self) -> None:
        req = request([sentence(i, i * 600, (i + 1) * 600) for i in range(7)], duration=4200)
        result = await self.classifier(Recorder((200, response())), max_input_tokens=1).classify(req)
        self.assertGreater(result.chunk_count, 1)
        self.assertEqual(result.attempts, result.chunk_count)
        self.assertAlmostEqual(result.usage.billed_cost_usd, result.chunk_count * 0.012)
        self.assertEqual(result.usage.input_tokens, result.chunk_count * 1000)

    async def test_failure_in_later_chunk_keeps_earlier_usage_and_charge(self) -> None:
        req = request([sentence(i, i * 600, (i + 1) * 600) for i in range(7)], duration=4200)
        refused = response()
        refused["choices"][0]["message"]["refusal"] = "Refused"
        recorder = Recorder((200, response()), (200, refused))
        with self.assertRaises(ClassifyFailed) as caught:
            await self.classifier(recorder, max_input_tokens=1).classify(req)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(caught.exception.usage.input_tokens, 2000)
        self.assertAlmostEqual(caught.exception.usage.billed_cost_usd, 0.024)

    async def test_shared_client_stays_open(self) -> None:
        classifier = self.classifier(Recorder((200, response())))
        await classifier.aclose()
        self.assertFalse(classifier._client.is_closed)


class UsageTests(unittest.TestCase):
    def test_missing_usage_is_unknown_charge(self) -> None:
        for metadata in (None, {}, [], "invalid"):
            with self.subTest(metadata=metadata):
                usage = usage_from_openrouter(metadata)
                self.assertEqual(usage.input_tokens, 0)
                self.assertIsNone(usage.billed_cost_usd)

    def test_invalid_cost_is_not_saved_as_real_charge(self) -> None:
        for cost in (None, "0.1", True, -1, float("nan"), float("inf")):
            with self.subTest(cost=cost):
                self.assertIsNone(usage_from_openrouter({"cost": cost}).billed_cost_usd)
        self.assertEqual(usage_from_openrouter({"cost": 0}).billed_cost_usd, 0.0)

    def test_reasoning_is_not_counted_twice_when_metadata_is_inconsistent(self) -> None:
        usage = usage_from_openrouter({"completion_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 20}})
        self.assertEqual((usage.thought_tokens, usage.output_tokens), (10, 0))


if __name__ == "__main__":
    unittest.main()
