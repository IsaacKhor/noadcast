from __future__ import annotations

import itertools
import json
import unittest

import httpx

from noadcast.classify.chunking import (
    OVERLAP_S,
    WINDOW_S,
    chunk_silences,
    estimate_tokens,
    plan_chunks,
    stitch_chunks,
)
from noadcast.classify.gemini import GeminiClassifier
from noadcast.classify.prompts import get_prompt
from noadcast.classify.render import render_transcript
from tests.test_classify_helpers import RecordingSleep, gemini_body, no_jitter, request, segment, segments_json, sentence, silence

WORDS = "so the thing about this story is that nobody really knew what was going on at the time".split()


def long_episode(hours: float, sentence_s: float = 6.0) -> list:
    """Speech at ~2.7 words/s in sentences of ``sentence_s`` seconds: the density of TAL."""
    words = itertools.cycle(WORDS)
    count = int(hours * 3600 / sentence_s)
    per_sentence = round(2.7 * sentence_s)
    return [
        sentence(i, 0.5 + i * sentence_s, 0.5 + i * sentence_s + sentence_s - 0.4,
                 " ".join(next(words) for _ in range(per_sentence)).capitalize() + ".")
        for i in range(count)
    ]


def rendered_estimate(sents, silences, duration: float) -> int:
    spec = get_prompt("segments-v2", "index")
    rendered = render_transcript(sents, silences, episode_duration=duration)
    return estimate_tokens(spec.system) + estimate_tokens(spec.user_message(rendered.text, "guidance"))


class GuardTests(unittest.IsolatedAsyncioTestCase):
    def test_estimate(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("x" * 36), 10)
        self.assertEqual(estimate_tokens("x" * 37), 11)

    async def test_a_three_hour_episode_is_sent_whole(self) -> None:
        sents = long_episode(3.0)
        end = sents[-1].end
        silences = [silence(0.0, 0.5, "head"), silence(end, end + 20.0, "tail")]
        estimate = rendered_estimate(sents, silences, end + 20.0)
        self.assertGreater(estimate, 40_000)  # a realistic three hours, not a toy
        self.assertLess(estimate, 120_000)

        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            return httpx.Response(200, json=gemini_body(segments_json()))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        classifier = GeminiClassifier(api_key="k", client=client, model="gemini-3.5-flash", sleep=RecordingSleep(),
                                      prompt_version="segments-v2", render_format="index", include_silence=True)
        result = await classifier.classify(request(sents, silences, duration=end + 20.0))
        self.assertEqual((result.chunk_count, len(calls)), (1, 1))
        self.assertNotIn("chunks", result.raw_response)


class PlanTests(unittest.TestCase):
    def test_windows_split_on_sentences_with_overlap(self) -> None:
        sents = long_episode(2.0)
        plans = plan_chunks(sents)
        self.assertEqual([p.role for p in plans], ["first", "middle", "last"])
        self.assertEqual((plans[0].first, plans[-1].last), (0, len(sents) - 1))
        for plan in plans:
            self.assertLessEqual(sents[plan.last].end - sents[plan.first].start, WINDOW_S)
            self.assertEqual(plan.count, 3)
        for previous, current in zip(plans, plans[1:]):
            overlap = sents[previous.last].end - sents[current.first].start
            self.assertGreaterEqual(overlap, OVERLAP_S - 6.0)
            self.assertLessEqual(overlap, OVERLAP_S)
        self.assertTrue(plans[0].allows_intro and not plans[0].allows_outro)
        self.assertFalse(plans[1].allows_intro or plans[1].allows_outro)
        self.assertTrue(plans[2].allows_outro and not plans[2].allows_intro)

    def test_a_short_episode_is_one_chunk(self) -> None:
        [plan] = plan_chunks(long_episode(0.5))
        self.assertEqual(plan.role, "only")
        self.assertTrue(plan.allows_intro and plan.allows_outro)
        self.assertEqual(plan_chunks([]), [])

    def test_head_and_tail_silences_reach_only_the_outer_chunks(self) -> None:
        sents = long_episode(2.0)
        end = sents[-1].end
        regions = [silence(0.0, 0.5, "head"), silence(3000.0, 3004.0), silence(end, end + 30.0, "tail")]
        first, middle, last = plan_chunks(sents)
        self.assertEqual([r.kind for r in chunk_silences(first, regions)], ["head"])
        self.assertEqual([r.start for r in chunk_silences(middle, regions)], [3000.0])
        self.assertEqual([r.kind for r in chunk_silences(last, regions)], ["tail"])


class StitchTests(unittest.TestCase):
    def test_keeps_one_intro_and_outro_and_merges_ads_seen_twice(self) -> None:
        first, middle, last = plan_chunks(long_episode(2.0))
        stitched = stitch_chunks(
            [
                (first, [segment(0.0, 60.0, "intro"), segment(2600.0, 2660.0, "ad", "Mattress", start_line=4),
                         segment(2650.0, 2700.0, "outro")]),
                (middle, [segment(2530.0, 2560.0, "intro"), segment(2604.0, 2662.0, "ad", "Mattress again"),
                          segment(2665.0, 2700.0, "ad", "Next ad, 3 s later"), segment(4000.0, 4060.0, "ad")]),
                (last, [segment(5100.0, 5160.0, "ad"), segment(7100.0, 7210.0, "outro"),
                        segment(7150.0, 7200.0, "outro")]),
            ]
        )
        self.assertEqual(
            [(s.kind, s.start_seconds, s.end_seconds) for s in stitched],
            [("intro", 0.0, 60.0), ("ad", 2600.0, 2700.0), ("ad", 4000.0, 4060.0), ("ad", 5100.0, 5160.0),
             ("outro", 7100.0, 7210.0)],
        )
        self.assertEqual(stitched[1].summary, "Mattress")
        self.assertTrue(all(s.start_line is None and s.end_line is None for s in stitched))


class ForcedChunkingTests(unittest.IsolatedAsyncioTestCase):
    async def test_classifies_in_chunks_and_stitches(self) -> None:
        sents = long_episode(2.0)
        end = sents[-1].end
        duration = end + 14.0
        silences = [silence(0.0, 0.5, "head"), silence(end, duration, "tail")]
        answers = {
            "part 1 of 3": [{"startSeconds": 0, "endSeconds": 60, "summary": "Intro", "kind": "intro"},
                            {"startSeconds": 2600, "endSeconds": 2660, "summary": "Ad", "kind": "ad"}],
            "part 2 of 3": [{"startSeconds": 2603, "endSeconds": 2662, "summary": "Ad", "kind": "ad"},
                            {"startSeconds": 2530, "endSeconds": 2560, "summary": "Not an intro", "kind": "intro"}],
            "part 3 of 3": [{"startSeconds": 7100, "endSeconds": duration, "summary": "Credits", "kind": "outro"}],
        }
        texts: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            text = json.loads(req.content)["contents"][0]["parts"][0]["text"]
            texts.append(text)
            [rows] = [rows for part, rows in answers.items() if part in text]
            return httpx.Response(200, json=gemini_body(segments_json(*rows), prompt=20_000))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        classifier = GeminiClassifier(api_key="k", client=client, model="gemini-3.5-flash", max_input_tokens=1_000,
                                      sleep=RecordingSleep(), rand=no_jitter, prompt_version="segments-v2",
                                      render_format="index", include_silence=True)
        with self.assertLogs("noadcast.classify.core", level="WARNING"):
            result = await classifier.classify(request(sents, silences, duration=duration))

        self.assertEqual(result.chunk_count, 3)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(result.usage.input_tokens, 60_000)
        self.assertEqual(
            [(s.kind, s.start_seconds, s.end_seconds) for s in result.segments],
            [("intro", 0.0, 60.0), ("ad", 2600.0, 2662.0), ("outro", 7100.0, duration)],
        )
        self.assertEqual([c["role"] for c in result.raw_response["chunks"]], ["first", "middle", "last"])
        self.assertEqual([e["chunk"] for e in result.raw_response["exchanges"]], [0, 1, 2])
        self.assertTrue(all("episode ends at" not in text for text in texts[:2]))
        self.assertIn(f"The complete episode ends at {duration:.2f} seconds.", texts[2])
        self.assertIn("\n\n0|0| --- 1s of no speech ---\n1|0| ", texts[0])  # the head reaches only chunk 1
        self.assertIn("(end of audio at", texts[2])
        self.assertNotIn("(end of audio at", texts[0] + texts[1])


if __name__ == "__main__":
    unittest.main()
