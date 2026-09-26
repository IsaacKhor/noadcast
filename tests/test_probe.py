"""Audio probing: a real TAL episode, generated clips, and things that are not audio."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
import wave
from pathlib import Path

import av
import numpy as np

from noadcast.media.probe import ProbeError, probe_audio, serving_content_type

ROOT = Path(__file__).resolve().parents[1]
TAL = ROOT / "benchmarks" / "tal"
EPISODE = TAL / "audio" / "01-646.mp3"  # untracked; present on the development host


def scratch_dir(case: unittest.TestCase) -> Path:
    base = ROOT / ".cache" / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="test-probe-", dir=base))
    case.addCleanup(shutil.rmtree, path, True)
    return path


def write_wav(path: Path, *, seconds: float = 1.0, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def encode(path: Path, container: str, codec: str, *, rate: int = 48000, seconds: float = 1.0) -> Path:
    """A short mono tone, encoded with PyAV's bundled FFmpeg."""
    with av.open(str(path), "w", format=container) as out:
        stream = out.add_stream(codec, rate=rate, layout="mono")
        frame_size = stream.codec_context.frame_size or 1024
        tone = (np.sin(np.arange(int(rate * seconds)) * 2 * np.pi * 440 / rate) * 0.3).astype(np.float32)
        tone = np.pad(tone, (0, -len(tone) % frame_size))  # fixed-frame codecs need whole frames
        for start in range(0, len(tone), frame_size):
            frame = av.AudioFrame.from_ndarray(tone[None, start : start + frame_size], format="flt", layout="mono")
            frame.sample_rate, frame.pts = rate, start
            out.mux(stream.encode(frame))
        out.mux(stream.encode(None))
    return path


@unittest.skipUnless(EPISODE.exists(), "benchmarks/tal/audio is not downloaded")
class TalEpisodeTests(unittest.TestCase):
    def test_mp3(self) -> None:
        manifest = json.loads((TAL / "manifest.json").read_text())["episodes"][0]
        started = time.perf_counter()
        probe = probe_audio(EPISODE)
        elapsed = time.perf_counter() - started
        self.assertEqual((probe.container, probe.codec, probe.content_type), ("mp3", "mp3", "audio/mpeg"))
        self.assertAlmostEqual(probe.duration_seconds, manifest["duration_seconds"], delta=2.0)
        self.assertAlmostEqual(probe.bit_rate, 128_000, delta=5_000)
        self.assertLess(elapsed, 1.0)  # headers only; decoding 65 minutes would take seconds


class GeneratedAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = scratch_dir(self)

    def test_wav(self) -> None:
        probe = probe_audio(write_wav(self.dir / "tone.wav", seconds=2.5))
        self.assertEqual((probe.container, probe.codec, probe.content_type), ("wav", "pcm_s16le", "audio/wav"))
        self.assertAlmostEqual(probe.duration_seconds, 2.5, places=2)
        self.assertAlmostEqual(probe.bit_rate, 16000 * 16, delta=2000)

    def test_container_and_codec_to_content_type(self) -> None:
        cases = [
            ("a.mp3", "mp3", "libmp3lame", 44100, "mp3", "audio/mpeg"),
            ("a.m4a", "ipod", "aac", 44100, "aac", "audio/mp4"),
            ("b.m4a", "ipod", "alac", 44100, "alac", "audio/mp4"),
            ("a.aac", "adts", "aac", 44100, "aac", "audio/aac"),
            ("a.opus", "ogg", "libopus", 48000, "opus", "audio/ogg"),
            ("a.flac", "flac", "flac", 44100, "flac", "audio/flac"),
            ("a.webm", "webm", "libopus", 48000, "opus", "audio/webm"),
        ]
        for name, container, encoder, rate, codec, content_type in cases:
            with self.subTest(name):
                probe = probe_audio(encode(self.dir / name, container, encoder, rate=rate))
                self.assertEqual((probe.codec, probe.content_type), (codec, content_type))
                self.assertAlmostEqual(probe.duration_seconds, 1.0, delta=0.1)
                self.assertIsNotNone(probe.bit_rate)

    def test_serving_content_type_table(self) -> None:
        self.assertEqual(serving_content_type("mov,mp4,m4a,3gp,3g2,mj2", "aac"), "audio/mp4")
        self.assertEqual(serving_content_type("mov,mp4,m4a,3gp,3g2,mj2", "aac", has_video=True), "video/mp4")
        self.assertEqual(serving_content_type("matroska,webm", "flac"), "audio/x-matroska")
        self.assertEqual(serving_content_type("ogg", "flac"), "audio/ogg")
        self.assertEqual(serving_content_type("w64", "pcm_s16le"), "audio/x-w64")  # never application/octet-stream


class NotAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = scratch_dir(self)

    def test_rejected_as_not_audio(self) -> None:
        pixel = np.zeros((8, 8, 3), dtype=np.uint8)
        image = self.dir / "cover.png"
        with av.open(str(image), "w", format="image2") as out:
            stream = out.add_stream("png", rate=1)
            stream.width = stream.height = 8
            stream.pix_fmt = "rgb24"
            out.mux(stream.encode(av.VideoFrame.from_ndarray(pixel, format="rgb24")))
            out.mux(stream.encode(None))
        cases = {
            "garbage.mp3": bytes(range(256)) * 64,
            "empty.mp3": b"",
            "error-page.mp3": b"<!doctype html><html><body>" + b"Not Found " * 200 + b"</body></html>",
            "notes.txt": b"plain text is a 'tty' video stream to FFmpeg\n" * 50,
        }
        for name, content in cases.items():
            (self.dir / name).write_bytes(content)
        for name in [*cases, "cover.png"]:
            with self.subTest(name), self.assertRaises(ProbeError):
                probe_audio(self.dir / name)

    def test_storage_problems_are_not_probe_errors(self) -> None:
        # A vanished file must not permanently fail an episode as "not audio".
        with self.assertRaises(FileNotFoundError) as caught:
            probe_audio(self.dir / "missing.mp3")
        self.assertNotIsInstance(caught.exception, ProbeError)


if __name__ == "__main__":
    unittest.main()
