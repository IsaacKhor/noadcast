"""run_selftest's kill/respawn/retry checks and the reference comparison,
driven by scripted workers so no faster-whisper is needed."""

from __future__ import annotations

import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from noadcast.transcribe.fake import load_recording, scripted_worker_main
from noadcast.transcribe.pool import PoolConfig
from noadcast.transcribe.protocol import TranscribeResult
from noadcast.transcribe.selftest import compare_with_reference, run_selftest


class SelftestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="noadcast-selftest-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "model").mkdir()
        (self.tmp / "model/model.bin").write_bytes(b"weights")
        self.config = PoolConfig(model_path=self.tmp / "model", workers=2, cpu_threads=1)

    def audio(self, **script) -> Path:
        path = self.tmp / "episode.json"
        path.write_text(json.dumps(script))
        return path

    def test_kill_worker_selftest_passes(self) -> None:
        report = run_selftest(self.config, self.audio(segments=20, delay=0.05), kill_worker=True,
                              worker_target=scripted_worker_main, kill_after=0.2, timeout=60)
        self.assertTrue(report["ok"], report["checks"])
        self.assertEqual({check["name"] for check in report["checks"]}, {
            "all workers ready", "transcribed words", "killed task failed with WorkerCrashed (transient)",
            "slot respawned under a new pid", "retry succeeded", "retry reproduced the first run's words exactly",
            "no worker processes left after shutdown"})
        kill = report["kill"]
        self.assertNotEqual(kill["victim"]["pid"], kill["replacement"]["pid"])
        self.assertEqual(kill["replacement"]["generation"], 1)
        self.assertEqual((kill["stats"]["crashed"], kill["stats"]["respawns"]), (1, 1))
        self.assertIn("SIGKILL", kill["crash"])
        self.assertEqual(report["transcription"]["words"], 12)
        self.assertEqual(report["shutdown"]["orphans"], [])
        json.dumps(report)  # the CLI prints it

    def test_a_task_too_short_to_kill_fails_the_check_instead_of_hanging(self) -> None:
        report = run_selftest(self.config, self.audio(), kill_worker=True, worker_target=scripted_worker_main,
                              kill_after=0.5, timeout=60)
        self.assertFalse(report["ok"])
        failed = [check["name"] for check in report["checks"] if not check["ok"]]
        self.assertEqual(failed, ["killed a busy worker mid-task"])

    def test_reference_comparison(self) -> None:
        reference = self.tmp / "reference.json"
        reference.write_text(json.dumps({
            "episode": {"path": "audio/x.mp3", "duration_seconds": 3.0},
            "config": {"model": "m", "language": "en"},
            "segments": [{"start": 0.0, "end": 2.0, "avg_logprob": -0.1, "compression_ratio": 1.2,
                          "no_speech_prob": 0.01,
                          "words": [{"start": 0.0, "end": 0.9, "word": " Hi", "probability": 0.9},
                                    {"start": 1.0, "end": 2.0, "word": " there.", "probability": 0.8}]}],
        }))
        recording = load_recording(reference)
        result = TranscribeResult(
            task_id="t", duration_seconds=3.0, duration_after_vad=2.0, language="en", language_probability=1.0,
            words=list(recording.words), segments=list(recording.segments), decode_seconds=0.0,
            transcribe_seconds=0.0, engine="faster-whisper", model_id="m", model_sha256=None)
        same = compare_with_reference(result, reference)
        self.assertTrue(same["identical"] and same["text_identical"] and same["word_timings_identical"])
        self.assertIsNone(same["first_difference"])

        nudged = dataclasses.replace(result, words=[result.words[0], dataclasses.replace(result.words[1],
                                                                                          probability=0.81)])
        differs = compare_with_reference(nudged, reference)
        self.assertFalse(differs["identical"])
        self.assertTrue(differs["text_identical"] and differs["word_timings_identical"])
        self.assertAlmostEqual(differs["max_probability_difference"], 0.01)
        self.assertEqual(differs["first_difference"]["index"], 1)

        shorter = dataclasses.replace(result, words=result.words[:1])
        self.assertEqual(compare_with_reference(shorter, reference)["first_difference"]["index"], 1)


if __name__ == "__main__":
    unittest.main()
