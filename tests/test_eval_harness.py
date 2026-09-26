"""Experiment B harness: pinning, cassettes, refusal on mismatch, and a full synthetic dry run.

Two synthetic episodes are "transcribed" by two variants with identical word timings; the tiny.en
variant garbles one ad word. The real joiner and renderer build the pinned transcripts and prompts; a
scripted, FakeClassifier-shaped classifier answers every cell. ``finalize`` is replaced by a recording
identity (its behaviour is the classifier's own test subject) and ``cost_for`` by a zero price.
"""

from __future__ import annotations

import collections
import contextlib
import csv
import dataclasses
import gzip
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import eval_ad_segments as harness  # noqa: E402
import report_ad_eval  # noqa: E402
import score_ad_eval  # noqa: E402
from report_tal_comparison import VerificationError, atomic_json  # noqa: E402

from noadcast.classify.base import ClassifierError, ClassifyResult, DetectedSegment, TokenUsage  # noqa: E402
from noadcast.classify.costs import CostBreakdown  # noqa: E402
from noadcast.config import Settings  # noqa: E402

DURATION = 200.0
AD_WORD = {"tiny.en": "Akmee", "small.en": "Acme"}
TITLES = {1: "Episode one", 2: "Episode two"}
ARM = "fake:fake-model"


def sections(variant: str) -> list[tuple[float, list[str]]]:
    """(first word start, sentences); each gap between sections is a >= 3 s silence."""
    return [
        (10.0, ["Welcome to the show.", "Today we talk about bridges."]),
        (40.0, [f"Support for this show comes from {AD_WORD[variant]} widgets.", "Visit acme dot com slash bridges."]),
        (70.0, ["Bridges carry loads.", "Engineers test them every year."]),
        (180.0, ["Thanks for listening.", "See you next week."]),
    ]


def transcript(variant: str) -> dict:
    segments = []
    for start, sentences in sections(variant):
        words, t = [], start
        for sentence in sentences:
            for token in sentence.split():
                words.append({"start": round(t, 2), "end": round(t + 0.4, 2), "word": " " + token,
                              "probability": 0.9})
                t += 0.5
            t += 0.3
        segments.append({"start": words[0]["start"], "end": words[-1]["end"],
                         "text": "".join(word["word"] for word in words), "compression_ratio": 1.2,
                         "no_speech_prob": 0.01, "avg_logprob": -0.2, "words": words})
    return {"segments": segments}


def build_corpus(root: Path) -> tuple[Path, dict[str, Path]]:
    corpus = root / "tal"  # named like the TAL corpus, so the TAL caveat applies
    corpus.mkdir()
    manifest = {"episodes": [{"index": index, "title": title, "sha256": hashlib.sha256(title.encode()).hexdigest(),
                              "duration_seconds": DURATION, "podcast_title": "Synthetic Show"}
                             for index, title in TITLES.items()]}
    (corpus / "manifest.json").write_text(json.dumps(manifest))
    manifest_sha256 = hashlib.sha256((corpus / "manifest.json").read_bytes()).hexdigest()
    runs = {}
    for variant in AD_WORD:
        run = root / "runs" / variant
        (run / "transcripts").mkdir(parents=True)
        episodes = []
        for source in manifest["episodes"]:
            relative = f"transcripts/{source['index']:02d}.json"
            (run / relative).write_text(json.dumps(transcript(variant)))
            episodes.append({"index": source["index"], "title": source["title"], "sha256": source["sha256"],
                             "full_decoded_audio_seconds": DURATION, "transcript_json": relative})
        (run / "results.json").write_text(json.dumps({
            "run_id": f"synthetic-{variant}", "status": "complete",
            "config": {"model": f"Systran/faster-whisper-{variant}", "compute_type": "int8", "workers": 1,
                       "cpu_threads": 2, "batch_size": 8, "beam_size": 5, "language": "en",
                       "word_timestamps": True, "sample_seconds": 0},
            "input": {"manifest_sha256": manifest_sha256},
            "model": {"model_bin_sha256": hashlib.sha256(variant.encode()).hexdigest(), "revision": "synthetic"},
            "system": {"versions": {"faster-whisper": "1.2.1"}}, "episodes": episodes,
        }))
        runs[variant] = run
    return corpus, runs


def segment(start: float, end: float, kind: str, summary: str) -> DetectedSegment:
    return DetectedSegment(start_seconds=start, end_seconds=end, summary=summary, kind=kind)


def agreeing(variant: str, episode: int, render: str, repeat: int) -> list[DetectedSegment]:
    """Every cell finds the same three segments, except small.en repeat 2 on episode 2 ends its ad 4 s late."""
    ad_end = 52.0 if (variant, episode, repeat) == ("small.en", 2, 2) else 48.0
    return [segment(0.0, 25.0, "intro", "Theme"), segment(40.0, ad_end, "ad", "Acme widgets read"),
            segment(180.0, DURATION, "outro", "Credits")]


def tiny_misses_the_ad(variant: str, episode: int, render: str, repeat: int) -> list[DetectedSegment]:
    found = agreeing(variant, episode, render, repeat)
    return [item for item in found if item.kind != "ad"] if variant == "tiny.en" and episode == 1 else found


class ScriptedClassifiers:
    """FakeClassifier-shaped responses from a script of (variant, episode, render, repeat) -> segments."""

    def __init__(self, script, *, fail: tuple | None = None, vary_request: bool = False, render_format=None):
        self.script, self.fail, self.vary_request, self.render_format = script, fail, vary_request, render_format
        self.calls: collections.Counter = collections.Counter()
        self.closed = False

    def __call__(self, render: str, arm: dict):
        owner = self

        class Scripted:
            provider, model = arm["provider"], arm["model"]

            async def classify(self, req) -> ClassifyResult:
                variant = "tiny.en" if any(AD_WORD["tiny.en"] in s.text for s in req.sentences) else "small.en"
                episode = next(index for index, title in TITLES.items() if title == req.episode_title)
                group = (variant, episode, render)
                if group == owner.fail:
                    raise ClassifierError("scripted outage")
                owner.calls[group] += 1
                repeat = owner.calls[group]
                spec = harness.RENDERS[render]
                found = owner.script(variant, episode, render, repeat)
                raw = {"exchanges": [{"attempt": 1, "text": "{}"}]}
                if spec["transcript_format"] == "index":
                    raw["line_resolution"] = {"cited_segments": len(found), "unresolved_citations": 0,
                                              "max_abs_delta_s": 0.5, "mean_abs_delta_s": 0.25,
                                              "deltas": [[0.0, 0.5]] * len(found)}
                identity = f"{render}|{variant}|{episode}|{repeat if owner.vary_request else ''}"
                return ClassifyResult(
                    segments=found, usage=TokenUsage(input_tokens=1000, output_tokens=40), provider=self.provider,
                    model=self.model, thinking=None, prompt_version="segments-v2",
                    render_format=owner.render_format or spec["transcript_format"],
                    include_silence=spec["include_silence"], chunk_count=1, latency_ms=7, attempts=1,
                    request_sha256=hashlib.sha256(identity.encode()).hexdigest(), raw_response=raw)

        return Scripted()

    async def aclose(self) -> None:
        self.closed = True


class HarnessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.corpus, self.runs = build_corpus(root)
        self.eval_dir = root / "evals" / "synthetic-eval"
        self.finalized: list[tuple] = []

        def finalize(segments, req, *, snap=True):
            self.finalized.append((req.episode_title, req.sentences[0].text, snap))
            return list(segments)

        for target, name, replacement in (
            (harness.sanitize, "finalize", finalize),
            (harness.costs, "cost_for", lambda provider, model, usage: CostBreakdown(0.0, 0.0, 0.0, 0.0)),
        ):
            patcher = mock.patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def create(self) -> dict:
        return harness.prepare(self.eval_dir, self.corpus, self.runs, "small.en", [harness.parse_arm(ARM)], 3, None,
                               Settings(prompt_version="segments-v2", transcript_format="index", include_silence=True))

    def record(self, classifiers: ScriptedClassifiers, mode: str = "record") -> dict:
        with contextlib.redirect_stdout(io.StringIO()):
            return harness.run_eval(self.eval_dir, mode, create=self.create, classifiers=classifiers)

    def score_and_report(self) -> dict:
        scored = score_ad_eval.score_eval(self.eval_dir)
        atomic_json(self.eval_dir / "scores.json", scored)
        with contextlib.redirect_stdout(io.StringIO()):
            report_ad_eval.write_report(self.eval_dir)
        return scored

    def decision(self, scored: dict, render: str) -> dict:
        group = next(item for item in scored["scores"]["groups"] if item["render"] == render)
        return group["decisions"]["tiny.en"]


class DryRunTest(HarnessTestCase):
    def test_full_dry_run_where_the_variants_agree(self) -> None:
        classifiers = ScriptedClassifiers(agreeing)
        results = self.record(classifiers)
        cell_count = 2 * 2 * len(harness.RENDERS) * 3
        self.assertEqual(len(results["cells"]), cell_count)
        self.assertEqual(sum(classifiers.calls.values()), cell_count)
        self.assertTrue(classifiers.closed)
        self.assertEqual(len(self.finalized), cell_count)
        self.assertTrue(all(snap for _, _, snap in self.finalized))

        first = (self.eval_dir / "results.json").read_bytes()
        harness.run_eval(self.eval_dir, "replay")
        self.assertEqual((self.eval_dir / "results.json").read_bytes(), first, "replay must be deterministic")
        self.assertEqual(sum(classifiers.calls.values()), cell_count, "replay must not call the classifier")

        scored = self.score_and_report()
        for render in harness.RENDERS:
            decision = self.decision(scored, render)
            self.assertEqual(decision["variant_frame_jaccard"], 1.0)
            self.assertEqual(decision["verdict"], "suffices")
        primary = next(item for item in scored["scores"]["groups"] if item["primary"])
        self.assertEqual(primary["render"], "index+silence")
        # Pooled floor: 3 of 4 self comparisons are perfect; small.en episode 2 repeat 2 adds 4 ad frames to 53.
        floor = primary["self_floor"]["tiny.en"]["frame_jaccard"]["any"]
        self.assertEqual(floor["median"], 1.0)
        self.assertAlmostEqual(floor["mean"], (3 + 53 / 57) / 4)
        self.assertEqual(scored["scores"]["usage"][ARM]["calls"], cell_count)
        self.assertEqual(scored["scores"]["usage"][ARM]["input_tokens"], 1000 * cell_count)
        self.assertEqual({item["render"] for item in scored["scores"]["line_resolution"]}, {"index+silence", "index-silence"})

        # Only small.en's one jittered repeat disagrees: 4 s at a 1-of-3 vote margin, over silence.
        [span] = scored["scores"]["worst_spans"][ARM]["tiny.en"]
        self.assertEqual((span["episode"], span["start_seconds"], span["end_seconds"]), (2, 48.0, 52.0))
        self.assertAlmostEqual(span["score"], 4 / 3)
        self.assertEqual(span["mean_votes"], {"tiny.en": 0.0, "small.en": 1.0})
        self.assertEqual(span["text"], {"tiny.en": "", "small.en": ""})
        report = (self.eval_dir / "AD_EVAL.md").read_text()
        self.assertTrue(report.split("\n")[2].startswith("**SUFFICES: with fake:fake-model and the index+silence"))
        self.assertIn("The TAL corpus contains no third-party advertisements", report)
        self.assertIn("measures intro/outro/house-promo agreement, not ad detection", report)
        verification = json.loads((self.eval_dir / "ad-eval-verification.json").read_text())
        self.assertIs(verification["verified"], True)
        self.assertIs(verification["scores_recomputed_match_verified"], True)
        self.assertIs(verification["accuracy_evaluated"], False)
        self.assertEqual(verification["asr_transcripts_rechecked"], 4)
        with (self.eval_dir / "ad-eval.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), len(harness.RENDERS) * 5 * 2)  # renders × pairings × episodes

    def test_dry_run_where_tiny_loses_an_ad(self) -> None:
        self.record(ScriptedClassifiers(tiny_misses_the_ad))
        scored = self.score_and_report()
        decision = self.decision(scored, "index+silence")
        # Episode 1: tiny skips intro + outro (25 + 20 frames), small also the 8 s ad; episode 2 agrees.
        self.assertAlmostEqual(decision["variant_frame_jaccard"], (45 / 53 + 1.0) / 2)
        self.assertEqual(decision["verdict"], "inconclusive")  # gap < 0.15, but one segment lost per episode 1
        self.assertEqual(decision["mean_count_diff"], 0.5)
        spans = scored["scores"]["worst_spans"][ARM]["tiny.en"]
        # The consistent loss (every tiny.en repeat keeps the ad, no small.en repeat skips it) outranks
        # episode 2's single jittered small.en repeat.
        self.assertEqual([(span["episode"], span["start_seconds"], span["end_seconds"]) for span in spans],
                         [(1, 40.0, 48.0), (2, 48.0, 52.0)])
        span = spans[0]
        self.assertEqual((span["score"], spans[1]["score"]), (8.0, 4 / 3))
        self.assertEqual(span["skipped_more_by"], "small.en")
        self.assertEqual(span["mean_votes"], {"tiny.en": 0.0, "small.en": 3.0})
        self.assertEqual(span["segments"], [{"kind": "ad", "summary": "Acme widgets read"}])
        self.assertIn("comes from Acme widgets.", span["text"]["small.en"])
        self.assertIn("comes from Akmee widgets.", span["text"]["tiny.en"])
        report = (self.eval_dir / "AD_EVAL.md").read_text()
        self.assertIn("**INCONCLUSIVE: ", report)
        self.assertIn("Episode 01 · Episode one, 40–48 s (8 s), skipped more by small.en", report)

    def test_large_gap_asks_for_manual_review(self) -> None:
        def ad_heavy(variant, episode, render, repeat):  # a long ad that tiny.en misses in both episodes
            found = [segment(0.0, 25.0, "intro", "Theme"), segment(40.0, 175.0, "ad", "Acme widgets read")]
            return found[:1] if variant == "tiny.en" else found

        self.record(ScriptedClassifiers(ad_heavy))
        decision = self.decision(self.score_and_report(), "index+silence")
        self.assertAlmostEqual(decision["jaccard_gap"], 1 - 25 / 160)
        self.assertEqual(decision["verdict"], "review")
        self.assertIn("**REVIEW: ", (self.eval_dir / "AD_EVAL.md").read_text())


class PinningTest(HarnessTestCase):
    def test_config_pins_inputs_and_content_addresses_prompts(self) -> None:
        config = self.create()
        self.assertEqual(config["primary_render"], "index+silence")
        self.assertEqual(sorted(config["renders"]), sorted(harness.RENDERS))
        self.assertEqual(config["reference_variant"], "small.en")
        self.assertEqual(config["joiner"]["params"], dataclasses.asdict(harness.joiner.JoinerParams()))
        self.assertEqual(config["classifier"]["prompt_version"], "segments-v2")
        self.assertIn("no third-party advertisements", config["corpus"]["caveat"])
        pins = config["variants"]["tiny.en"]["episodes"]["1"]
        self.assertNotEqual(pins["sentences_sha256"], config["variants"]["small.en"]["episodes"]["1"]["sentences_sha256"])
        for render, address in config["prompts"]["tiny.en"]["1"].items():
            record = json.loads(gzip.decompress((self.eval_dir / "prompts" / f"{address}.json.gz").read_bytes()))
            self.assertEqual(harness.digest(record), address)
            self.assertEqual(record["transcript_format"], harness.RENDERS[render]["transcript_format"])
        index_text = json.loads(gzip.decompress(
            (self.eval_dir / "prompts" / f"{config['prompts']['tiny.en']['1']['index+silence']}.json.gz").read_bytes()))
        self.assertIn("of no speech", index_text["text"])

    def test_tampered_sentences_are_refused(self) -> None:
        self.record(ScriptedClassifiers(agreeing))
        path = self.eval_dir / "transcripts" / "tiny.en" / "01.json.gz"
        payload = json.loads(gzip.decompress(path.read_bytes()))
        payload["sentences"][0]["text"] = " Tampered."
        harness.write_gzip_json(path, payload)
        with self.assertRaisesRegex(VerificationError, "pinned transcript sha256 mismatch for tiny.en episode 1"):
            score_ad_eval.score_eval(self.eval_dir)
        with self.assertRaisesRegex(VerificationError, "pinned transcript sha256 mismatch"):
            harness.run_eval(self.eval_dir, "replay")

    def test_changed_asr_transcript_is_refused(self) -> None:
        self.record(ScriptedClassifiers(agreeing))
        path = self.runs["small.en"] / "transcripts" / "02.json"
        data = json.loads(path.read_text())
        data["segments"][0]["words"][0]["probability"] = 0.5
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(VerificationError, "ASR transcript no longer matches its pinned sha256"):
            score_ad_eval.score_eval(self.eval_dir)

    def test_tampered_prompt_and_config_are_refused(self) -> None:
        self.record(ScriptedClassifiers(agreeing))
        config_path = self.eval_dir / "config.json"
        config = json.loads(config_path.read_text())
        prompt = self.eval_dir / "prompts" / f"{config['prompts']['small.en']['2']['seconds']}.json.gz"
        original = prompt.read_bytes()
        harness.write_gzip_json(prompt, {**json.loads(gzip.decompress(original)), "text": "edited"})
        with self.assertRaisesRegex(VerificationError, "prompt file does not match its address"):
            score_ad_eval.score_eval(self.eval_dir)
        prompt.write_bytes(original)
        config["repeats_note"] = "edited after recording"
        config_path.write_text(json.dumps(config))
        with self.assertRaisesRegex(VerificationError, "different config.json"):
            score_ad_eval.score_eval(self.eval_dir)

    def test_cassette_edited_after_replay_is_refused(self) -> None:
        results = self.record(ScriptedClassifiers(agreeing))
        cassette = self.eval_dir / results["cells"][5]["cassette"]
        data = json.loads(cassette.read_text())
        data["recorded_at"] = "2000-01-01T00:00:00.000Z"
        cassette.write_text(json.dumps(data))
        with self.assertRaisesRegex(VerificationError, "cassette changed since results.json was written"):
            score_ad_eval.score_eval(self.eval_dir)

    def test_stale_scores_are_not_reported(self) -> None:
        self.record(ScriptedClassifiers(agreeing))
        scored = self.score_and_report()
        scored["scores"]["groups"][0]["decisions"]["tiny.en"]["verdict"] = "review"
        atomic_json(self.eval_dir / "scores.json", scored)
        with self.assertRaisesRegex(VerificationError, "does not match a fresh scoring"):
            report_ad_eval.write_report(self.eval_dir)


class CassetteTest(HarnessTestCase):
    def test_replay_needs_a_pinned_eval(self) -> None:
        with self.assertRaisesRegex(VerificationError, "no pinned eval"):
            harness.run_eval(self.eval_dir, "replay", create=self.create)

    def test_missing_cassette_fails_loudly(self) -> None:
        results = self.record(ScriptedClassifiers(agreeing))
        row = results["cells"][7]
        (self.eval_dir / row["cassette"]).unlink()
        label = harness.Cell(**row["cell"]).label()
        with self.assertRaisesRegex(VerificationError, f"missing cassette for {label}.*--record"):
            harness.run_eval(self.eval_dir, "replay")

    def test_record_fills_only_missing_cells_and_refresh_rerecords_all(self) -> None:
        classifiers = ScriptedClassifiers(agreeing)
        results = self.record(classifiers)
        total = len(results["cells"])
        again = ScriptedClassifiers(agreeing)
        self.record(again)
        self.assertEqual(sum(again.calls.values()), 0)
        for row in results["cells"][:2]:  # repeats 1 and 2 of one request
            (self.eval_dir / row["cassette"]).unlink()
        refill = ScriptedClassifiers(agreeing)
        self.record(refill)
        self.assertEqual(sum(refill.calls.values()), 2)
        refresh = ScriptedClassifiers(agreeing)
        self.record(refresh, mode="refresh")
        self.assertEqual(sum(refresh.calls.values()), total)

    def test_failed_cells_are_reported_and_resumable(self) -> None:
        failing = ScriptedClassifiers(agreeing, fail=("tiny.en", 2, "seconds"))
        with self.assertRaisesRegex(VerificationError, "(?s)3 cells have no cassette.*repeat 1 and its later repeats"):
            self.record(failing)
        resumed = ScriptedClassifiers(agreeing)
        self.record(resumed)
        self.assertEqual(dict(resumed.calls), {("tiny.en", 2, "seconds"): 3})

    def test_cassette_is_bound_to_its_cell(self) -> None:
        results = self.record(ScriptedClassifiers(agreeing))
        first, second = (self.eval_dir / results["cells"][i]["cassette"] for i in (0, 1))
        second.write_bytes(first.read_bytes())
        with self.assertRaisesRegex(VerificationError, "does not belong to"):
            harness.run_eval(self.eval_dir, "replay")

    def test_repeats_must_send_one_request(self) -> None:
        with self.assertRaisesRegex(VerificationError, "repeats sent different requests"):
            self.record(ScriptedClassifiers(agreeing, vary_request=True))

    def test_classifier_must_honour_the_render_variant(self) -> None:
        with self.assertRaisesRegex(VerificationError, "classifier used render"):
            self.record(ScriptedClassifiers(agreeing, render_format="audio"))


if __name__ == "__main__":
    unittest.main()
