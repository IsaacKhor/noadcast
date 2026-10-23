"""The worker's decoder must survive corrupt packets mid-file (DAI splices)."""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import av
import numpy as np

from noadcast.transcribe.audio import decode_audio
from noadcast.media.mp3_breaks import _frame_length


def write_tone_mp3(path: Path, seconds: int = 60, rate: int = 44100) -> None:
    container = av.open(str(path), "w", format="mp3")
    stream = container.add_stream("mp3", rate=rate)
    stream.layout = "mono"
    t = np.arange(seconds * rate) / rate
    samples = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    for start in range(0, len(samples), 1152):
        frame = av.AudioFrame.from_ndarray(samples[None, start:start + 1152], format="flt", layout="mono")
        frame.sample_rate = rate
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()


class DecodeAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.clean = self.tmp / "clean.mp3"
        write_tone_mp3(self.clean)

    def test_clean_file_matches_faster_whisper_exactly(self) -> None:
        from faster_whisper.audio import decode_audio as faster_whisper_decode

        audio, skipped, breaks = decode_audio(str(self.clean))
        self.assertEqual(skipped, 0)
        self.assertEqual(breaks, [])
        # faster-whisper 1.2.x passes a PyAV keyword removed in PyAV 19.
        original_open = av.open
        def compatible_open(*args, **kwargs):
            kwargs.pop("metadata_errors", None)
            return original_open(*args, **kwargs)
        with mock.patch.object(av, "open", side_effect=compatible_open):
            baseline = faster_whisper_decode(str(self.clean))
        self.assertTrue(np.array_equal(audio, baseline))
        self.assertAlmostEqual(len(audio) / 16000, 60.0, delta=0.1)

    def test_corrupt_packets_are_skipped_not_fatal(self) -> None:
        # 2 kB of garbage a third of the way in. faster-whisper 1.2.1 stops
        # there (~20 s); every packet around the damage must still decode.
        data = bytearray(self.clean.read_bytes())
        start = len(data) // 3
        data[start:start + 2000] = bytes(random.Random(0).randrange(256) for _ in range(2000))
        corrupt = self.tmp / "corrupt.mp3"
        corrupt.write_bytes(data)

        audio, skipped, breaks = decode_audio(str(corrupt))
        self.assertGreater(skipped, 0)
        self.assertGreater(len(audio) / 16000, 58.0)
        self.assertTrue(any(item["kind"] == "decode_error" for item in breaks))

    def test_in_band_id3_is_timed_on_decoded_audio(self) -> None:
        # A concatenated render has an ID3v2 header between encoded pieces.
        # The first audio's own ID3 header is not a break.
        second = self.tmp / "second.mp3"
        write_tone_mp3(second, seconds=2)
        second_bytes = second.read_bytes()
        first_frame = second_bytes.index(b"\xff\xfb")
        tag = b"ID3\x04\x00\x00\x00\x00\x00\x05hello"
        spliced = self.tmp / "spliced.mp3"
        spliced.write_bytes(self.clean.read_bytes() + tag + second_bytes[first_frame:])

        audio, _skipped, breaks = decode_audio(str(spliced))
        self.assertAlmostEqual(len(audio) / 16000, 62.0, delta=0.2)
        self.assertEqual(len(breaks), 1)
        self.assertEqual(breaks[0]["kind"], "embedded_id3")
        self.assertAlmostEqual(float(breaks[0]["seconds"]), 60.0, delta=0.1)

    def test_restarted_encoder_header_without_id3_is_cued(self) -> None:
        second = self.tmp / "second.mp3"
        write_tone_mp3(second, seconds=2)
        second_bytes = second.read_bytes()
        first_frame = second_bytes.index(b"\xff\xfb")
        spliced = self.tmp / "spliced-no-tag.mp3"
        spliced.write_bytes(self.clean.read_bytes() + second_bytes[first_frame:])

        _audio, _skipped, breaks = decode_audio(str(spliced))
        self.assertEqual(len(breaks), 1)
        self.assertEqual(breaks[0]["kind"], "encoder_header")
        self.assertAlmostEqual(float(breaks[0]["seconds"]), 60.0, delta=0.1)

    def test_sample_rate_change_decodes_and_is_cued_without_encoder_header(self) -> None:
        second = self.tmp / "different-rate.mp3"
        write_tone_mp3(second, seconds=2, rate=32000)
        second_bytes = second.read_bytes()
        first_frame = second_bytes.index(b"\xff\xfb")
        # Remove the Xing header so the format transition is the sole cue.
        first_audio = first_frame + _frame_length(second_bytes, first_frame)[0]
        spliced = self.tmp / "changed-rate.mp3"
        spliced.write_bytes(self.clean.read_bytes() + second_bytes[first_audio:])

        audio, skipped, breaks = decode_audio(str(spliced))
        self.assertEqual(skipped, 0)
        self.assertAlmostEqual(len(audio) / 16000, 62.0, delta=0.2)
        self.assertEqual(len(breaks), 1)
        self.assertEqual(breaks[0]["kind"], "format_change")
        self.assertAlmostEqual(float(breaks[0]["seconds"]), 60.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
