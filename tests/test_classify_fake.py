from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.support.classifier_replay import FakeClassifier, load_fixtures, slugify
from noadcast.classify.retry import ClassifyFailed
from noadcast.classify.sanitize import finalize
from tests.test_classify_helpers import (
    FIXTURES,
    RecordingSleep,
    claude_body,
    episode,
    gemini_body,
    gemini_error,
    request,
    segments_json,
    sentences,
    silence,
)

AD = {"startSeconds": 60, "endSeconds": 90, "summary": "Mattress", "kind": "ad"}


def ok(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"format": "gemini", "status": 200, "body": gemini_body(segments_json(*rows))}


class FakeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.sleep = RecordingSleep()

    def fixture(self, name: str, responses: list[dict[str, Any]], **fields: Any) -> None:
        (self.dir / f"{name}.json").write_text(json.dumps({**fields, "responses": responses}))

    def fake(self, **options: Any) -> FakeClassifier:
        options = {"prompt_version": "segments-v2", "render_format": "index", "include_silence": True, **options}
        return FakeClassifier(fixtures_dir=self.dir, sleep=self.sleep, **options)


class LookupTests(FakeTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_request_hash_beats_title_beats_synthetic(self) -> None:
        req = episode()
        sha = self.fake().prepare(req).request_sha256
        self.fixture("by-title", [ok(AD)], episode_title="Test Episode")
        self.fixture("by-hash", [ok({**AD, "summary": "Pinned"})], request_sha256=sha)

        pinned = await self.fake().classify(req)
        self.assertEqual([s.summary for s in pinned.segments], ["Pinned"])
        self.assertEqual(pinned.request_sha256, sha)
        self.assertTrue(pinned.raw_response["fixture"].endswith("by-hash.json"))

        # The title is not part of the request identity, so these differ in content (no silence map).
        titled = await self.fake().classify(request(req.sentences, (), 185.0, title="Test Episode"))
        self.assertEqual([s.summary for s in titled.segments], ["Mattress"])

        synthetic = await self.fake().classify(request(req.sentences, (), 185.0, title="Unknown"))
        self.assertTrue(synthetic.raw_response["synthetic"])

    async def test_file_stem_is_the_default_title_key(self) -> None:
        self.fixture("449-middle-school", [ok(AD)])
        result = await self.fake().classify(request(episode().sentences, title="449: Middle School"))
        self.assertEqual([s.kind for s in result.segments], ["ad"])

    async def test_synthetic_answer(self) -> None:
        result = await FakeClassifier().classify(episode())
        self.assertEqual(
            [(s.kind, s.start_seconds, s.end_seconds) for s in result.segments],
            [("intro", 0.0, 30.0), ("outro", 150.2, 185.0)],
        )
        self.assertEqual((result.provider, result.model, result.attempts, result.usage.input_tokens),
                         ("openrouter", "deepseek/deepseek-v4.1-flash", 1, 0))
        final = finalize(result.segments, episode())
        self.assertEqual((final[0].start_seconds, final[-1].end_seconds), (0.0, 185.0))

    async def test_no_synthetic_intro_when_speech_starts_late(self) -> None:
        late = request(sentences((75.0, 90.0), (91.0, 400.0)), duration=420.0)
        result = await FakeClassifier().classify(late)
        self.assertEqual([(s.kind, s.start_seconds, s.end_seconds) for s in result.segments],
                         [("outro", 91.0, 420.0)])

    def test_slugify(self) -> None:
        self.assertEqual(slugify("449: Middle School"), "449-middle-school")
        self.assertEqual(slugify("894: I Couldn't Help but Notice"), "894-i-couldnt-help-but-notice")
        self.assertEqual(slugify("Ira (Reluctantly) Gives a Graduation Speech"), "ira-reluctantly-gives-a-graduation-speech")
        self.assertEqual(slugify("Café — Naïve!"), "cafe-naive")


class ScriptedFailureTests(FakeTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_429_then_success(self) -> None:
        self.fixture("t", [{"status": 429, "headers": {"Retry-After": "0"}, "body": gemini_error(429, "RESOURCE_EXHAUSTED")},
                           ok(AD)], episode_title="T")
        result = await self.fake().classify(request(episode().sentences, title="T"))
        self.assertEqual(result.attempts, 2)
        self.assertEqual(self.sleep.delays, [0.0])

    async def test_fenced_json(self) -> None:
        fenced = "```json\n" + segments_json(AD) + "\n```"
        self.fixture("t", [{"body": gemini_body(fenced)}], episode_title="T")
        result = await self.fake().classify(request(episode().sentences, title="T"))
        self.assertEqual([s.summary for s in result.segments], ["Mattress"])

    async def test_schema_violation_then_repair(self) -> None:
        self.fixture("t", [{"format": "claude", "body": claude_body('{"segments": [{"start": 1}]}')},
                           {"format": "claude", "body": claude_body(segments_json(AD))}], episode_title="T")
        result = await self.fake().classify(request(episode().sentences, title="T"))
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.usage.input_tokens, 2000)

    async def test_repeated_schema_violation_is_permanent(self) -> None:
        self.fixture("t", [{"body": gemini_body("[]")}], episode_title="T")
        with self.assertRaises(ClassifyFailed) as caught:
            await self.fake().classify(request(episode().sentences, title="T"))
        self.assertTrue(caught.exception.permanent)

    async def test_the_cursor_persists_across_calls_for_job_level_retries(self) -> None:
        unavailable = {"status": 503, "body": gemini_error(503, "UNAVAILABLE")}
        self.fixture("t", [unavailable] * 4 + [{"error": "timeout"}, ok(AD)], episode_title="T")
        fake = self.fake()
        req = request(episode().sentences, title="T")
        with self.assertRaises(ClassifyFailed) as caught:
            await fake.classify(req)
        self.assertFalse(caught.exception.permanent)
        result = await fake.classify(req)
        self.assertEqual(result.attempts, 2)
        again = await fake.classify(req)  # the last response repeats
        self.assertEqual(again.attempts, 1)


class FixtureFileTests(FakeTestCase, unittest.IsolatedAsyncioTestCase):
    def test_invalid_fixtures_fail_to_load(self) -> None:
        self.fixture("empty", [])
        with self.assertRaises(ValueError):
            load_fixtures(self.dir)

    def test_a_missing_directory_or_bad_json_fails_to_load(self) -> None:
        with self.assertRaises(ValueError):
            load_fixtures(self.dir / "missing")
        (self.dir / "broken.json").write_text("{")
        with self.assertRaisesRegex(ValueError, "broken.json"):
            load_fixtures(self.dir)

    def test_duplicate_keys_fail_to_load(self) -> None:
        self.fixture("a", [ok()], episode_title="Same Title")
        self.fixture("b", [ok()], episode_title="same title")
        with self.assertRaises(ValueError):
            load_fixtures(self.dir)

    async def test_committed_fixtures_replay_to_markers(self) -> None:
        by_sha, by_slug = load_fixtures(FIXTURES)
        self.assertEqual(sorted(by_slug), ["206-somewhere-in-the-arabian-sea", "449-middle-school",
                                           "896-i-know-what-you-need"])
        expected_attempts = {"449: Middle School": 2, "896: I Know What You Need": 1,
                             "206: Somewhere in the Arabian Sea": 2}
        durations = {"449: Middle School": 3566.55, "896: I Know What You Need": 3984.2,
                     "206: Somewhere in the Arabian Sea": 3566.86}
        fake = FakeClassifier(fixtures_dir=FIXTURES, sleep=self.sleep, prompt_version="segments-v2",
                              render_format="index", include_silence=True)
        for title, attempts in expected_attempts.items():
            with self.subTest(title=title):
                duration = durations[title]
                req = request(
                    sentences((2.1, 40.0), (41.0, duration - 70.0), (duration - 69.0, duration - 20.0)),
                    [silence(0.0, 2.1, "head"), silence(duration - 20.0, duration, "tail")],
                    duration=duration,
                    title=title,
                )
                result = await fake.classify(req)
                self.assertEqual(result.attempts, attempts)
                final = finalize(result.segments, req)
                self.assertEqual([s.kind for s in final], ["intro", "outro"])
                self.assertEqual(final[0].start_seconds, 0.0)
                self.assertEqual(final[-1].end_seconds, duration)


if __name__ == "__main__":
    unittest.main()
