"""Word and segment codec: exact round trips at ASR resolution, bounded loss
otherwise, and blob size on real tiny.en words."""

from __future__ import annotations

import gzip
import json
import random
import unittest
import zlib
from pathlib import Path

from noadcast.transcribe.codec import CODEC, decode_segments, decode_words, encode_segments, encode_words
from noadcast.transcribe.protocol import AsrSegmentMeta, Word

FIXTURE = Path(__file__).parent / "fixtures" / "words_01-646_first600s.json.gz"


def load_fixture() -> tuple[list[Word], list[AsrSegmentMeta]]:
    fixture = json.loads(gzip.decompress(FIXTURE.read_bytes()))
    words = [Word(**dict(zip(fixture["word_fields"], row))) for row in fixture["words"]]
    segments = [AsrSegmentMeta(**dict(zip(fixture["segment_fields"], row))) for row in fixture["segments"]]
    return words, segments


class WordCodecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.words, cls.segments = load_fixture()

    def test_codec_name(self):
        self.assertEqual(CODEC, "zlib+json-columnar-v1")

    def test_asr_resolution_round_trips_exactly(self):
        # faster-whisper rounds times to 0.01 s, so only probability is quantised.
        decoded = decode_words(encode_words(self.words))
        self.assertEqual(len(decoded), len(self.words))
        for original, copy in zip(self.words, decoded):
            self.assertEqual((copy.start, copy.end, copy.word, copy.segment),
                             (original.start, original.end, original.word, original.segment))
            self.assertLessEqual(abs(copy.probability - original.probability), 0.005 + 1e-12)

    def test_centisecond_loss_is_bounded_at_5_ms(self):
        rng = random.Random(646)
        words = []
        for k in range(2000):
            start = rng.uniform(0, 4000)
            words.append(Word(start, start + rng.uniform(0, 3), f" w{k}", rng.random(), k // 100))
        for original, copy in zip(words, decode_words(encode_words(words))):
            self.assertLessEqual(abs(copy.start - original.start), 0.005 + 1e-9)
            self.assertLessEqual(abs(copy.end - original.end), 0.005 + 1e-9)
            self.assertLessEqual(abs(copy.probability - original.probability), 0.005 + 1e-9)

    def test_blob_layout(self):
        words = self.words[:3] + [Word(700.0, 700.4, "今天", 0.5, 99)]
        text = zlib.decompress(encode_words(words)).decode("utf-8")
        payload = json.loads(text)
        self.assertEqual(text, json.dumps(payload, separators=(",", ":"), ensure_ascii=False))  # compact
        self.assertEqual(list(payload), ["v", "s", "d", "w", "p", "g"])
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["s"][:3], [69, 125, 141])  # centiseconds
        self.assertEqual(payload["d"][:3], [56, 16, 38])
        self.assertEqual(payload["w"], [" A", " quick", " warning,", "今天"])  # leading space kept, UTF-8 raw
        self.assertEqual(payload["p"], [78, 99, 97, 50])
        self.assertEqual(payload["g"], [0, 0, 0, 99])

    def test_ten_thousand_words_compress_well_under_150_kb(self):
        # Six copies of the fixture, shifted 600 s each: 9,726 real words. Whole
        # episodes of 7,804-10,819 words encode to 54-74 KB.
        words = [Word(w.start + 600 * copy, w.end + 600 * copy, w.word, w.probability, w.segment + 19 * copy)
                 for copy in range(6) for w in self.words]
        self.assertGreater(len(words), 9_700)
        self.assertLess(len(encode_words(words)), 100_000)

    def test_probability_is_clamped_to_percent(self):
        words = [Word(0.0, 0.1, " a", 1.2, 0), Word(0.1, 0.2, " b", -0.1, 0), Word(0.2, 0.3, " c", 0.994, 0)]
        self.assertEqual([w.probability for w in decode_words(encode_words(words))], [1.0, 0.0, 0.99])

    def test_inverted_word_survives(self):
        # The codec stores what ASR said; the joiner does the clamping.
        (copy,) = decode_words(encode_words([Word(2.0, 1.5, " x", 0.5, 0)]))
        self.assertEqual((copy.start, copy.end), (2.0, 1.5))

    def test_empty(self):
        self.assertEqual(decode_words(encode_words([])), [])

    def test_rejects_malformed_payloads(self):
        good = json.loads(zlib.decompress(encode_words(self.words[:2])))
        for label, payload in (("version", {**good, "v": 2}), ("ragged", {**good, "p": good["p"][:1]})):
            with self.subTest(label):
                blob = zlib.compress(json.dumps(payload).encode())
                with self.assertRaises(ValueError):
                    decode_words(blob)


class SegmentCodecTests(unittest.TestCase):
    def test_round_trip_is_exact(self):
        _, segments = load_fixture()
        text = encode_segments(segments)
        self.assertNotIn(" ", text)
        self.assertEqual(decode_segments(text), segments)

    def test_decodes_by_field_name(self):
        text = json.dumps({"v": 1, "fields": ["avg_logprob", "index", "start", "end", "no_speech_prob",
                                              "compression_ratio"],
                           "rows": [[-0.25, 4, 1.5, 29.85, 0.05, 1.64]]})
        self.assertEqual(decode_segments(text), [AsrSegmentMeta(4, 1.5, 29.85, 1.64, 0.05, -0.25)])

    def test_empty_and_bad_version(self):
        self.assertEqual(decode_segments(encode_segments([])), [])
        with self.assertRaises(ValueError):
            decode_segments(json.dumps({"v": 9, "fields": [], "rows": []}))


if __name__ == "__main__":
    unittest.main()
