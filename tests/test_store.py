"""MediaStore: the on-disk layout under the data directory."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from noadcast.media.downloader import part_path
from noadcast.media.store import MediaStore

ROOT = Path(__file__).resolve().parents[1]


class MediaStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        base = ROOT / ".cache" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.data = Path(tempfile.mkdtemp(prefix="test-store-", dir=base))
        self.addCleanup(shutil.rmtree, self.data, True)
        self.store = MediaStore(self.data)

    def put(self, relpath: str, size: int) -> Path:
        path = self.store.abspath(relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def test_audio_relpath(self) -> None:
        self.assertEqual(self.store.audio_relpath(3, 42, "mp3"), "audio/3/42.mp3")
        self.assertEqual(self.store.audio_relpath(3, 42, ".M4A"), "audio/3/42.m4a")
        for bad in ("", ".", "mp3/../x", "a b", "waytoolong"):
            with self.subTest(ext=bad), self.assertRaises(ValueError):
                self.store.audio_relpath(3, 42, bad)

    def test_abspath(self) -> None:
        path = self.store.abspath("audio/3/42.mp3")
        self.assertTrue(path.is_absolute())
        self.assertEqual(path, self.data.absolute() / "audio" / "3" / "42.mp3")
        for bad in ("", "/etc/passwd", "../outside", "audio/../../outside"):
            with self.subTest(relpath=bad), self.assertRaises(ValueError):
                self.store.abspath(bad)

    def test_relative_data_dir_still_yields_absolute_paths(self) -> None:
        relative = MediaStore(Path(os.path.relpath(self.data)))
        self.assertEqual(relative.abspath("audio/1/2.mp3"), self.data.absolute() / "audio" / "1" / "2.mp3")

    def test_extension_for(self) -> None:
        cases = [
            ("audio/mpeg", None, "mp3"),
            ("audio/MPEG; charset=binary", None, "mp3"),
            ("audio/mp3", None, "mp3"),
            ("audio/mp4", None, "m4a"),
            ("audio/x-m4a", None, "m4a"),
            ("video/mp4", None, "m4a"),
            ("audio/aac", None, "aac"),
            ("audio/ogg", None, "ogg"),
            ("audio/opus", None, "ogg"),
            ("audio/wav", None, "wav"),
            ("audio/x-wav", None, "wav"),
            ("audio/flac", None, "flac"),
            # Unhelpful types fall back to the URL, then to mp3.
            ("application/octet-stream", "https://cdn.example.com/ep/1.M4A?sig=abc", "m4a"),
            (None, "https://cdn.example.com/ep%201.opus", "opus"),
            ("audio/x-mpegurl", "https://cdn.example.com/list.m3u", "mp3"),
            ("binary/octet-stream", "https://cdn.example.com/download.php?id=1.m4a", "mp3"),  # query is not a suffix
            (None, None, "mp3"),
            ("", "", "mp3"),
        ]
        for content_type, url, expected in cases:
            with self.subTest(content_type=content_type, url=url):
                self.assertEqual(MediaStore.extension_for(content_type, url), expected)

    def test_remove_counts_the_file_and_its_part(self) -> None:
        final = self.put("audio/3/42.mp3", 1000)
        part_path(final).write_bytes(b"y" * 234)
        self.assertEqual(self.store.remove("audio/3/42.mp3"), 1234)
        self.assertFalse(final.exists())
        self.assertFalse(part_path(final).exists())
        self.assertEqual(self.store.remove("audio/3/42.mp3"), 0)  # idempotent

    def test_remove_part_only(self) -> None:
        final = self.store.abspath("audio/3/43.mp3")
        final.parent.mkdir(parents=True)
        part_path(final).write_bytes(b"y" * 10)
        self.assertEqual(self.store.remove("audio/3/43.mp3"), 10)

    def test_disk_usage(self) -> None:
        usage = self.store.disk_usage()
        self.assertGreater(usage.total_bytes, 0)
        self.assertGreaterEqual(usage.total_bytes, usage.free_bytes)
        self.assertGreater(usage.used_bytes, 0)
        not_yet = MediaStore(self.data / "later" / "data").disk_usage()  # measured on the parent filesystem
        self.assertEqual(not_yet.total_bytes, usage.total_bytes)

    def test_iter_part_files(self) -> None:
        self.assertEqual(self.store.iter_part_files(), [])  # no audio directory yet
        final = self.put("audio/3/42.mp3", 1)
        parts = [part_path(final), part_path(self.store.abspath("audio/1/7.m4a"))]
        for part in parts:
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(b"p")
        (self.data / "audio" / "9" / "dir.part").mkdir(parents=True)  # not a file
        (self.data / "tmp").mkdir()
        (self.data / "tmp" / "elsewhere.part").write_bytes(b"p")  # outside audio/
        self.assertEqual(self.store.iter_part_files(), sorted(parts))
        self.assertTrue(all(path.is_absolute() for path in self.store.iter_part_files()))


if __name__ == "__main__":
    unittest.main()
