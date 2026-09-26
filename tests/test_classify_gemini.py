from __future__ import annotations

import unittest
from typing import Any

import httpx

from noadcast.classify.base import TokenUsage
from noadcast.classify.core import request_sha256
from noadcast.classify.gemini import GeminiClassifier
from noadcast.classify.prompts import LINE_RESPONSE_SCHEMA, REPAIR_NUDGE, RESPONSE_SCHEMA, V2_INDEX_PROMPT
from noadcast.classify.retry import ClassifyFailed
from tests.test_classify_helpers import (
    Recorder,
    RecordingSleep,
    episode,
    gemini_body,
    gemini_error,
    httpx_client,
    no_jitter,
    segments_json,
)

KEY = "AIza-test-key"


def answer(*rows: dict[str, Any], **usage: int) -> tuple[int, dict[str, Any]]:
    return 200, gemini_body(segments_json(*rows), **usage)


INTRO = {"startLine": 0, "endLine": 3, "startSeconds": 0, "endSeconds": 20, "summary": "Billboard", "kind": "intro"}
AD = {"startLine": 6, "endLine": 8, "startSeconds": 50, "endSeconds": 95, "summary": "Mattress", "kind": "ad"}


class GeminiTestCase(unittest.IsolatedAsyncioTestCase):
    def classifier(self, recorder: Recorder, **options: Any) -> GeminiClassifier:
        self.sleep = RecordingSleep()
        client = httpx_client(recorder)
        self.addAsyncCleanup(client.aclose)
        options = {"prompt_version": "segments-v2", "render_format": "index", "include_silence": True, **options}
        return GeminiClassifier(
            api_key=KEY, client=client, model="gemini-3.5-flash", sleep=self.sleep, rand=no_jitter, **options
        )


class RequestTests(GeminiTestCase):
    async def test_request_shape(self) -> None:
        recorder = Recorder(answer(INTRO))
        result = await self.classifier(recorder, thinking="low").classify(episode())

        [request] = recorder.requests
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            str(request.url), "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
        )
        self.assertEqual(request.headers["x-goog-api-key"], KEY)
        self.assertNotIn(KEY, str(request.url))
        body = recorder.bodies()[0]
        self.assertEqual(body["systemInstruction"], {"parts": [{"text": V2_INDEX_PROMPT}]})
        self.assertEqual(
            body["generationConfig"],
            {
                "responseMimeType": "application/json",
                "responseSchema": LINE_RESPONSE_SCHEMA,
                "thinkingConfig": {"thinkingLevel": "low"},
            },
        )
        [content] = body["contents"]
        self.assertEqual(content["role"], "user")
        text = content["parts"][0]["text"]
        self.assertTrue(text.startswith("Classify only the following transcript."))
        self.assertIn("The complete episode ends at 185.00 seconds.", text)
        self.assertTrue(text.endswith("12|170| --- 15s of no speech (end of audio at 185.00) ---"))
        self.assertEqual(
            result.request_sha256,
            request_sha256(provider="gemini", model="gemini-3.5-flash", thinking="low", prompt_version="segments-v2",
                           render_format="index", include_silence=True, text=text),
        )
        self.assertEqual(
            (result.provider, result.model, result.thinking, result.prompt_version, result.render_format,
             result.include_silence, result.chunk_count, result.attempts),
            ("gemini", "gemini-3.5-flash", "low", "segments-v2", "index", True, 1, 1),
        )

    async def test_no_thinking_config_without_a_level(self) -> None:
        recorder = Recorder(answer())
        await self.classifier(recorder).classify(episode())
        self.assertNotIn("thinkingConfig", recorder.bodies()[0]["generationConfig"])

    async def test_v1_sends_the_recovered_request(self) -> None:
        recorder = Recorder(answer({"startSeconds": 0, "endSeconds": 14, "summary": "Intro", "kind": "intro"}))
        result = await self.classifier(recorder, prompt_version="segments-v1", render_format="seconds",
                                       include_silence=False).classify(episode())
        body = recorder.bodies()[0]
        self.assertEqual(body["generationConfig"]["responseSchema"], RESPONSE_SCHEMA)
        text = body["contents"][0]["parts"][0]["text"]
        self.assertTrue(text.startswith(
            "Classify only the following transcript. Segment starts, intros, and ads must stay within these "
            "transcript ranges.\n\nThe complete episode ends at 185.00 seconds."
        ))
        self.assertIn("\n\n[1.50 - 8.00] From WBEZ Chicago, it's This American Life.\n[8.40 - 14.00] I'm Ira Glass.", text)
        self.assertNotIn("no speech", text)
        self.assertEqual([(s.start_seconds, s.end_seconds) for s in result.segments], [(0.0, 14.0)])
        self.assertNotIn("line_resolution", result.raw_response)


class ResponseTests(GeminiTestCase):
    async def test_usage_mapping(self) -> None:
        recorder = Recorder(answer(INTRO, prompt=19423, thoughts=1180, candidates=212, cached=4096))
        result = await self.classifier(recorder).classify(episode())
        self.assertEqual(
            result.usage,
            TokenUsage(input_tokens=19423, thought_tokens=1180, output_tokens=212, cached_input_tokens=4096),
        )

    async def test_seconds_resolve_from_cited_lines(self) -> None:
        recorder = Recorder(answer(INTRO, AD))
        result = await self.classifier(recorder).classify(episode())
        # Line 3 is the 14-20 s silence and line 6 the 50-60 s one: cited
        # silence lines resolve to the silence's own bounds.
        self.assertEqual(
            [(s.kind, s.start_seconds, s.end_seconds, s.start_line, s.end_line) for s in result.segments],
            [("intro", 0.0, 20.0, 0, 3), ("ad", 50.0, 90.0, 6, 8)],
        )
        metrics = result.raw_response["line_resolution"]
        self.assertEqual(metrics["deltas"], [[0.0, 0.0], [0.0, 5.0]])
        self.assertEqual((metrics["max_abs_delta_s"], metrics["mean_abs_delta_s"], metrics["unresolved_citations"]),
                         (5.0, 1.25, 0))
        [exchange] = result.raw_response["exchanges"]
        self.assertEqual((exchange["attempt"], exchange["format"], exchange["status"]), (1, "gemini", 200))

    async def test_fenced_json_is_accepted(self) -> None:
        fenced = "```json\n" + segments_json(INTRO) + "\n```"
        result = await self.classifier(Recorder((200, gemini_body(fenced)))).classify(episode())
        self.assertEqual([s.kind for s in result.segments], ["intro"])

    async def test_a_blocked_prompt_is_permanent(self) -> None:
        recorder = Recorder((200, {"promptFeedback": {"blockReason": "OTHER"}, "usageMetadata": {"promptTokenCount": 900}}))
        with self.assertRaises(ClassifyFailed) as caught:
            await self.classifier(recorder).classify(episode())
        self.assertTrue(caught.exception.permanent)
        self.assertEqual(caught.exception.usage.input_tokens, 900)
        self.assertEqual(len(recorder.requests), 1)


class RetryTests(GeminiTestCase):
    async def test_429_with_retry_after_then_success(self) -> None:
        recorder = Recorder((429, gemini_error(429, "RESOURCE_EXHAUSTED"), {"Retry-After": "3"}), answer(INTRO))
        result = await self.classifier(recorder).classify(episode())
        self.assertEqual(result.attempts, 2)
        self.assertEqual(self.sleep.delays, [3.0])
        self.assertEqual([e["status"] for e in result.raw_response["exchanges"]], [429, 200])
        self.assertEqual(result.raw_response["exchanges"][0]["headers"], {"retry-after": "3"})
        self.assertEqual(result.raw_response["retries"][0]["kind"], "transient")

    async def test_retry_info_in_the_error_body_is_honoured(self) -> None:
        recorder = Recorder((429, gemini_error(429, "RESOURCE_EXHAUSTED", retry_delay="7s")), answer())
        result = await self.classifier(recorder).classify(episode())
        self.assertEqual((result.attempts, self.sleep.delays), (2, [7.0]))

    async def test_401_is_permanent(self) -> None:
        recorder = Recorder((401, gemini_error(401, "UNAUTHENTICATED", "API key not valid.")))
        with self.assertRaises(ClassifyFailed) as caught:
            await self.classifier(recorder).classify(episode())
        self.assertTrue(caught.exception.permanent)
        self.assertIn("401", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual((len(recorder.requests), self.sleep.delays), (1, []))

    async def test_unavailable_is_retried_until_the_attempts_run_out(self) -> None:
        recorder = Recorder((503, gemini_error(503, "UNAVAILABLE", "The model is overloaded.")))
        with self.assertRaises(ClassifyFailed) as caught:
            await self.classifier(recorder).classify(episode())
        self.assertFalse(caught.exception.permanent)
        self.assertEqual(len(recorder.requests), 4)
        self.assertEqual(self.sleep.delays, [2.0, 4.0, 8.0])

    async def test_timeouts_and_connection_errors_are_retried(self) -> None:
        recorder = Recorder(httpx.ReadTimeout("timed out"), httpx.ConnectError("refused"), answer(INTRO))
        result = await self.classifier(recorder).classify(episode())
        self.assertEqual(result.attempts, 3)
        self.assertEqual([e.get("error") for e in result.raw_response["exchanges"][:2]], ["timeout", "connection"])

    async def test_schema_repair_once_then_permanent(self) -> None:
        recorder = Recorder((200, gemini_body('{"segments": "none"}')), (200, gemini_body("Sorry, I cannot.")))
        with self.assertRaises(ClassifyFailed) as caught:
            await self.classifier(recorder).classify(episode())
        self.assertTrue(caught.exception.permanent)
        self.assertEqual(caught.exception.attempts, 2)
        first, second = (body["contents"][0]["parts"][0]["text"] for body in recorder.bodies())
        self.assertNotIn(REPAIR_NUDGE, first)
        self.assertTrue(second.endswith(REPAIR_NUDGE))
        self.assertEqual(self.sleep.delays, [])

    async def test_schema_repair_success_counts_both_calls_usage(self) -> None:
        recorder = Recorder((200, gemini_body("{}", prompt=1000, candidates=5)), answer(INTRO, prompt=1010, candidates=90))
        result = await self.classifier(recorder).classify(episode())
        self.assertEqual(result.attempts, 2)
        self.assertEqual((result.usage.input_tokens, result.usage.output_tokens), (2010, 95))


if __name__ == "__main__":
    unittest.main()
