"""The worker's decoder must survive corrupt packets mid-file (DAI splices)."""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

import av
import numpy as np

from noadcast.transcribe.audio import decode_audio


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

        audio, skipped = decode_audio(str(self.clean))
        self.assertEqual(skipped, 0)
        self.assertTrue(np.array_equal(audio, faster_whisper_decode(str(self.clean))))
        self.assertAlmostEqual(len(audio) / 16000, 60.0, delta=0.1)

    def test_corrupt_packets_are_skipped_not_fatal(self) -> None:
        # 2 kB of garbage a third of the way in. faster-whisper 1.2.1 stops
        # there (~20 s); every packet around the damage must still decode.
        data = bytearray(self.clean.read_bytes())
        start = len(data) // 3
        data[start:start + 2000] = bytes(random.Random(0).randrange(256) for _ in range(2000))
        corrupt = self.tmp / "corrupt.mp3"
        corrupt.write_bytes(data)

        audio, skipped = decode_audio(str(corrupt))
        self.assertGreater(skipped, 0)
        self.assertGreater(len(audio) / 16000, 58.0)


if __name__ == "__main__":
    unittest.main()
