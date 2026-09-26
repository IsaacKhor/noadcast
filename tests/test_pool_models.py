"""Model directory management: link (copy) with provenance, verify, fetch."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from noadcast.transcribe.models import (
    MODEL_FILES,
    PINNED_REVISIONS,
    PROVENANCE_NAME,
    ModelError,
    fetch_model,
    link_model,
    model_metadata,
    verify_model,
)

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_MODEL = ROOT / "benchmarks/tal/models/tiny.en"
REVISION = "0123456789abcdef0123456789abcdef01234567"


def write_model(directory: Path, *, revision: str | None = REVISION, weights: bytes = b"weights") -> dict[str, bytes]:
    """Four model files, plus Hugging Face download metadata when ``revision`` is set."""
    contents = {name: f"{name} contents".encode() for name in MODEL_FILES}
    contents["model.bin"] = weights
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in contents.items():
        (directory / name).write_bytes(data)
    if revision:
        meta = directory / ".cache/huggingface/download"
        meta.mkdir(parents=True, exist_ok=True)
        for name, data in contents.items():
            # LFS files carry their sha256 as the etag; small files their git blob sha1.
            etag = (hashlib.sha256(data).hexdigest() if name == "model.bin"
                    else hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest())
            (meta / f"{name}.metadata").write_text(f"{revision}\n{etag}\n1789925291.7\n")
    return contents


class ModelFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="noadcast-models-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "src"
        self.dest = self.tmp / "data/models/tiny.en"

    def test_link_copies_the_four_files_and_records_provenance(self) -> None:
        contents = write_model(self.src)
        info = link_model(self.src, self.dest, model_id="Systran/faster-whisper-tiny.en")
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), sorted([*MODEL_FILES, PROVENANCE_NAME]))
        self.assertFalse((self.dest / "model.bin").is_symlink())
        self.assertEqual((self.dest / "model.bin").read_bytes(), contents["model.bin"])
        self.assertEqual(info.revision, REVISION)
        self.assertEqual(info.model_id, "Systran/faster-whisper-tiny.en")
        self.assertEqual(info.model_bin_sha256, hashlib.sha256(b"weights").hexdigest())
        provenance = json.loads((self.dest / PROVENANCE_NAME).read_text())
        self.assertEqual(provenance["source"], str(self.src.resolve()))
        self.assertEqual(provenance["files"]["config.json"]["bytes"], len(contents["config.json"]))
        self.assertEqual([p.name for p in self.dest.parent.iterdir()], ["tiny.en"])  # no staging left behind

    def test_link_without_hugging_face_metadata_records_no_revision(self) -> None:
        write_model(self.src, revision=None)
        self.assertIsNone(link_model(self.src, self.dest).revision)
        self.assertEqual(verify_model(self.dest).revision, None)

    def test_link_is_idempotent_and_refuses_a_different_model(self) -> None:
        write_model(self.src)
        first = link_model(self.src, self.dest)
        again = link_model(self.src, self.dest)
        self.assertEqual((again.created_at, again.model_bin_sha256), (first.created_at, first.model_bin_sha256))
        other = self.tmp / "other"
        write_model(other, weights=b"different weights")
        with self.assertRaises(ModelError):
            link_model(other, self.dest)
        self.assertEqual((self.dest / "model.bin").read_bytes(), b"weights")
        replaced = link_model(other, self.dest, replace=True)
        self.assertEqual(replaced.model_bin_sha256, hashlib.sha256(b"different weights").hexdigest())
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ["tiny.en"])

    def test_link_rejects_a_source_that_does_not_match_its_etags(self) -> None:
        write_model(self.src)
        (self.src / "tokenizer.json").write_bytes(b"corrupted")
        with self.assertRaises(ModelError):
            link_model(self.src, self.dest)
        self.assertFalse(self.dest.exists())

    def test_link_rejects_an_incomplete_source(self) -> None:
        write_model(self.src)
        (self.src / "vocabulary.txt").unlink()
        with self.assertRaisesRegex(ModelError, "vocabulary.txt"):
            link_model(self.src, self.dest)

    def test_verify_detects_tampering(self) -> None:
        write_model(self.src)
        link_model(self.src, self.dest)
        self.assertEqual(verify_model(self.dest).revision, REVISION)
        (self.dest / "model.bin").write_bytes(b"tampered")
        with self.assertRaisesRegex(ModelError, "model.bin"):
            verify_model(self.dest)

    def test_verify_falls_back_to_hugging_face_metadata(self) -> None:
        write_model(self.src)
        info = verify_model(self.src)
        self.assertEqual((info.revision, info.model_bin_sha256), (REVISION, hashlib.sha256(b"weights").hexdigest()))
        (self.src / "config.json").write_bytes(b"{}")
        with self.assertRaises(ModelError):
            verify_model(self.src)

    def test_model_metadata_hashes_model_bin(self) -> None:
        write_model(self.src)
        link_model(self.src, self.dest, model_id="m")
        metadata = model_metadata(self.dest)
        self.assertEqual(metadata["model_bin_sha256"], hashlib.sha256(b"weights").hexdigest())
        self.assertEqual((metadata["revision"], metadata["model_id"], metadata["model_bin_bytes"]), (REVISION, "m", 7))
        with self.assertRaisesRegex(FileNotFoundError, "models link"):
            model_metadata(self.tmp / "absent")

    def test_fetch_downloads_only_the_model_files_over_plain_https(self) -> None:
        calls = []

        def fake_snapshot_download(**kwargs):
            calls.append((kwargs, os.environ.get("HF_HUB_DISABLE_XET")))
            write_model(Path(kwargs["local_dir"]), revision=kwargs["revision"])
            return str(kwargs["local_dir"])

        import huggingface_hub

        with mock.patch.dict(os.environ), mock.patch.object(huggingface_hub, "snapshot_download", fake_snapshot_download), \
                mock.patch.object(huggingface_hub.constants, "HF_HUB_DISABLE_XET", False):
            info = fetch_model("Systran/faster-whisper-tiny.en", None, self.dest)
            self.assertTrue(huggingface_hub.constants.HF_HUB_DISABLE_XET)
        (kwargs, xet_disabled), = calls
        self.assertEqual(kwargs["repo_id"], "Systran/faster-whisper-tiny.en")
        self.assertEqual(kwargs["revision"], PINNED_REVISIONS["Systran/faster-whisper-tiny.en"])  # pinned by default
        self.assertEqual(sorted(kwargs["allow_patterns"]), sorted(MODEL_FILES))
        self.assertEqual(xet_disabled, "1")
        self.assertEqual(info.revision, PINNED_REVISIONS["Systran/faster-whisper-tiny.en"])
        self.assertEqual(info.source, "https://huggingface.co/Systran/faster-whisper-tiny.en")
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ["tiny.en"])

    def test_fetch_rejects_a_different_revision(self) -> None:
        import huggingface_hub

        def wrong_revision(**kwargs):
            write_model(Path(kwargs["local_dir"]), revision="f" * 40)

        with mock.patch.dict(os.environ), mock.patch.object(huggingface_hub, "snapshot_download", wrong_revision), \
                mock.patch.object(huggingface_hub.constants, "HF_HUB_DISABLE_XET", False):
            with self.assertRaises(ModelError):
                fetch_model("some/model", REVISION, self.dest)

    @unittest.skipUnless((BENCHMARK_MODEL / "model.bin").is_file(), "benchmark model not present")
    def test_the_benchmark_model_verifies_against_its_download_metadata(self) -> None:
        info = verify_model(BENCHMARK_MODEL)
        self.assertEqual(info.revision, PINNED_REVISIONS["Systran/faster-whisper-tiny.en"])
        self.assertEqual(info.model_bin_sha256, "1a5afae06a4db91c975c9a9d78be5cc110ee4ea022ad57d55492e4550e936b2a")


if __name__ == "__main__":
    unittest.main()
