"""FakeTranscriber: recording loaders, key matching, progress, scripted failures."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from noadcast.transcribe.fake import FakeTranscriber, load_recording
from noadcast.transcribe.protocol import TranscribeTask, TranscriptionError, Word, WorkerCrashed

ROOT = Path(__file__).resolve().parents[1]
RECORDED_RUN = ROOT / "benchmarks/tal/runs/wt-crossover-2-on-20260922"
JOINER_FIXTURE = ROOT / "tests/fixtures/words_01-646_first600s.json.gz"


def benchmark_transcript(segment_words: list[list[str]], *, word_timestamps: bool = True) -> dict:
    """A transcript in the benchmark's format: one second per word."""
    segments, clock = [], 0.0
    for index, texts in enumerate(segment_words):
        words = []
        for text in texts:
            words.append({"start": clock, "end": clock + 0.8, "word": text, "probability": 0.9})
            clock += 1.0
        segments.append({"id": index + 1, "start": words[0]["start"], "end": words[-1]["end"],
                         "text": "".join(w["word"] for w in words), "avg_logprob": -0.1,
                         "compression_ratio": 1.4, "no_speech_prob": 0.02,
                         "words": words if word_timestamps else None})
    return {"schema_version": 1,
            "episode": {"index": 1, "path": "audio/01-test.mp3", "duration_seconds": clock + 5.0,
                        "sha256": "ab" * 32},
            "config": {"model": "Systran/faster-whisper-tiny.en", "language": "en", "model_path": "/x",
                       "word_timestamps": word_timestamps},
            "segments": segments}


class FakeTranscriberTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="noadcast-fake-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.run_dir = self.tmp / "run"
        (self.run_dir / "transcripts").mkdir(parents=True)
        self.transcript = self.run_dir / "transcripts" / "01-test.json"
        self.transcript.write_text(json.dumps(benchmark_transcript(
            [[" Hello", " there."], [" This", " is", " a"], [" test."], [" One", " more."], [" Bye."]])))
        (self.run_dir / "results.json").write_text(json.dumps({
            "model": {"model_bin_sha256": "cd" * 32},
            "episodes": [{"transcript_json": "transcripts/01-test.json", "full_decoded_audio_seconds": 14.25,
                          "speech_seconds_after_vad": 9.5, "decode_seconds": 0.1, "transcribe_seconds": 0.4,
                          "effective_transcription_options": {"beam_size": 5, "clip_timestamps": [{"start": 0}]},
                          "effective_vad_options": {"min_silence_duration_ms": 160}}],
        }))

    def transcribe(self, fake: FakeTranscriber, path: str, task_id: str = "t", on_progress=None):
        return asyncio.run(fake.transcribe(TranscribeTask(task_id, path), on_progress))

    def test_replays_a_benchmark_transcript(self) -> None:
        result = self.transcribe(FakeTranscriber({"01-test": self.transcript}), "/data/audio/1/01-test.mp3")
        self.assertEqual("".join(w.word for w in result.words).strip(), "Hello there. This is a test. One more. Bye.")
        self.assertEqual(result.words[2], Word(2.0, 2.8, " This", 0.9, 1))
        self.assertEqual([s.index for s in result.segments], [0, 1, 2, 3, 4])
        # The run's results.json supplies what the real pool would have measured.
        self.assertEqual((result.duration_seconds, result.duration_after_vad), (14.25, 9.5))
        self.assertEqual(result.model_sha256, "cd" * 32)
        self.assertNotIn("clip_timestamps", result.options["transcription_options"])
        self.assertEqual(result.options["replayed_from"], str(self.transcript))
        self.assertNotIn("model_path", result.options["recorded_config"])

    def test_matches_by_path_name_stem_and_audio_sha256(self) -> None:
        audio = self.tmp / "41.mp3"
        audio.write_bytes(b"corpus mp3 bytes")
        digest = hashlib.sha256(audio.read_bytes()).hexdigest()
        for key, path in (("/srv/x.mp3", "/srv/x.mp3"), ("01-test.mp3", "/a/01-test.mp3"),
                          ("01-test", "/b/01-test.wav"), (digest, str(audio))):
            with self.subTest(key=key):
                result = self.transcribe(FakeTranscriber({key: self.transcript}), path)
                self.assertEqual(len(result.words), 9)

    def test_from_run_dir_keys_by_stem_source_name_and_sha256(self) -> None:
        fake = FakeTranscriber.from_run_dir(self.run_dir)
        for path in ("/x/01-test.wav", "/y/01-test.mp3"):
            self.assertEqual(len(self.transcribe(fake, path).words), 9)
        self.assertEqual(len(self.transcribe(fake, "/nowhere/" + "ab" * 32).words), 9)  # the sha256 key

    def test_unknown_audio_fails_permanently(self) -> None:
        with self.assertRaises(TranscriptionError) as caught:
            self.transcribe(FakeTranscriber({"01-test": self.transcript}), "/data/other.mp3")
        self.assertTrue(caught.exception.permanent)

    def test_progress_mirrors_the_pool(self) -> None:
        seen = []
        fake = FakeTranscriber({"01-test": self.transcript}, progress_every=2)
        self.transcribe(fake, "01-test", "p", seen.append)
        self.assertEqual([p.processed_seconds for p in seen], [0.0, 4.8, 7.8])  # 0, then every 2nd segment's end
        self.assertTrue(all(p.total_seconds == 14.25 and p.task_id == "p" for p in seen))

    def test_scripted_outcomes(self) -> None:
        fake = FakeTranscriber({"01-test": self.transcript, "alias": self.transcript})
        fake.fail_next("alias", "transient", "ok", "permanent")  # any key reaches the same recording
        with self.assertRaises(TranscriptionError) as caught:
            self.transcribe(fake, "01-test")
        self.assertFalse(caught.exception.permanent)
        self.transcribe(fake, "01-test")
        with self.assertRaises(TranscriptionError) as caught:
            self.transcribe(fake, "01-test")
        self.assertTrue(caught.exception.permanent)
        self.assertEqual(len(self.transcribe(fake, "01-test").words), 9)  # script exhausted
        with self.assertRaises(ValueError):
            fake.fail_next("01-test", "explode")

    def test_scripted_crashes_turn_permanent_like_the_pool(self) -> None:
        fake = FakeTranscriber({"01-test": self.transcript})
        fake.fail_next("01-test", "crash", "crash", "crash", "crash", "ok", "crash")
        permanence = []
        for _ in range(4):
            with self.assertRaises(WorkerCrashed) as caught:
                self.transcribe(fake, "01-test")
            permanence.append(caught.exception.permanent)
        self.assertEqual(permanence, [False, False, False, True])
        self.transcribe(fake, "01-test")  # success resets the count
        with self.assertRaises(WorkerCrashed) as caught:
            self.transcribe(fake, "01-test")
        self.assertFalse(caught.exception.permanent)

    def test_failures_arrive_after_the_first_progress_callback(self) -> None:
        seen = []
        fake = FakeTranscriber({"01-test": self.transcript})
        fake.fail_next("01-test", "crash")
        with self.assertRaises(WorkerCrashed):
            self.transcribe(fake, "01-test", on_progress=seen.append)
        self.assertEqual([p.processed_seconds for p in seen], [0.0])

    def test_latency_and_concurrency_tracking(self) -> None:
        fake = FakeTranscriber({"01-test": self.transcript}, latency_seconds=0.2)

        async def main():
            return await asyncio.gather(*(fake.transcribe(TranscribeTask(f"t{i}", "01-test")) for i in range(3)))

        started = time.monotonic()
        results = asyncio.run(main())
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertEqual([r.task_id for r in results], ["t0", "t1", "t2"])
        self.assertEqual((fake.max_in_flight, fake.in_flight, len(fake.calls)), (3, 0, 3))

    def test_results_do_not_share_mutable_lists(self) -> None:
        fake = FakeTranscriber({"01-test": self.transcript})
        first = self.transcribe(fake, "01-test")
        first.words.clear()
        self.assertEqual(len(self.transcribe(fake, "01-test").words), 9)

    def test_rejects_transcripts_without_word_timestamps(self) -> None:
        path = self.tmp / "off.json"
        path.write_text(json.dumps(benchmark_transcript([[" a"]], word_timestamps=False)))
        with self.assertRaises(ValueError):
            load_recording(path)

    def test_loads_compact_fixtures(self) -> None:
        path = self.tmp / "words.json.gz"
        path.write_bytes(gzip.compress(json.dumps({
            "source": {"episode": {"audio_sha256": "ef" * 32, "duration_seconds": 3000.0},
                       "asr": {"model": "Systran/faster-whisper-tiny.en", "language": "en"}},
            "cut_seconds": 10.0,
            "segment_fields": ["index", "start", "end", "compression_ratio", "no_speech_prob", "avg_logprob"],
            "segments": [[3, 0.5, 10.4, 1.5, 0.01, -0.2]],
            "word_fields": ["start", "end", "word", "probability", "segment"],
            "words": [[0.5, 0.9, " Hi.", 0.8, 3], [9.9, 10.4, " Bye.", 0.7, 3]],
        }).encode()))
        recording = load_recording(path)
        self.assertEqual(recording.words[1], Word(9.9, 10.4, " Bye.", 0.7, 3))
        self.assertEqual(recording.segments[0].index, 3)
        self.assertEqual(recording.duration_seconds, 10.4)  # the cut, extended to the last word
        self.assertEqual(recording.source_sha256, "ef" * 32)

    @unittest.skipUnless(JOINER_FIXTURE.is_file(), "joiner fixture not generated")
    def test_loads_the_joiner_golden_fixture(self) -> None:
        recording = load_recording(JOINER_FIXTURE)
        self.assertGreater(len(recording.words), 1000)
        indices = {segment.index for segment in recording.segments}
        self.assertTrue(all(word.segment in indices for word in recording.words))

    @unittest.skipUnless((RECORDED_RUN / "transcripts/01-646.json").is_file(), "benchmark run not present")
    def test_replays_the_recorded_benchmark_run(self) -> None:
        fake = FakeTranscriber.from_run_dir(RECORDED_RUN)
        result = self.transcribe(fake, "/srv/data/audio/1/01-646.mp3")
        self.assertEqual((len(result.words), len(result.segments)), (10428, 122))
        self.assertAlmostEqual(result.duration_seconds, 3918.9420625)
        self.assertEqual(result.words[0].word, " A")
        self.assertEqual(result.model_sha256, "1a5afae06a4db91c975c9a9d78be5cc110ee4ea022ad57d55492e4550e936b2a")


if __name__ == "__main__":
    unittest.main()
