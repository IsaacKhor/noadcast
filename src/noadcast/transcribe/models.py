"""ASR model directory: copy, fetch, verify, describe.

The pool loads ``settings.asr_model_dir`` (default ``data/models/tiny.en``)
with ``local_files_only=True``, so the server never downloads anything at
startup. The directory holds exactly the four files faster-whisper needs plus
``noadcast-model.json``, which records each file's sha256 and the Hugging Face
revision, so a transcript's ``model_sha256`` traces back to a pinned commit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..timeutil import now_iso

log = logging.getLogger(__name__)

MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
PROVENANCE_NAME = "noadcast-model.json"
HF_METADATA_DIR = Path(".cache/huggingface/download")
# Revisions the benchmarks were measured with (benchmarks/tal/models/*/.cache).
PINNED_REVISIONS = {
    "Systran/faster-whisper-tiny.en": "0d3d19a32d3338f10357c0889762bd8d64bbdeba",
    "Systran/faster-whisper-small.en": "d1d751a5f8271d482d14ca55d9e2deeebbae577f",
}
_COMMIT = re.compile(r"[0-9a-f]{40}")
_CHUNK = 8 * 1024 * 1024


class ModelError(Exception):
    """The model directory is missing, incomplete, or does not match its provenance."""


@dataclass(frozen=True)
class ModelFile:
    name: str
    bytes: int
    sha256: str
    # Hugging Face download etag: the sha256 for LFS files, the git blob sha1 otherwise.
    hf_etag: str | None = None


@dataclass(frozen=True)
class ModelInfo:
    path: str
    model_id: str | None
    revision: str | None
    model_bin_sha256: str
    files: tuple[ModelFile, ...]
    source: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _digest(path: Path, *, etag: str | None = None, copy_to: Path | None = None) -> ModelFile:
    """Stream ``path`` once: sha256, optional durable copy, and an etag check."""
    size = path.stat().st_size
    sha256 = hashlib.sha256()
    # Non-LFS files are identified by git's blob hash, which covers a size header.
    sha1 = hashlib.sha1(b"blob %d\0" % size) if etag and len(etag) == 40 else None
    writer = copy_to.open("wb") if copy_to else None
    try:
        with path.open("rb") as reader:
            while block := reader.read(_CHUNK):
                sha256.update(block)
                if sha1:
                    sha1.update(block)
                if writer:
                    writer.write(block)
        if writer:
            writer.flush()
            os.fsync(writer.fileno())
    finally:
        if writer:
            writer.close()
    digest = sha256.hexdigest()
    if etag and etag != (digest if len(etag) == 64 else sha1.hexdigest() if sha1 else None):
        raise ModelError(f"{path} does not match its Hugging Face etag {etag}")
    return ModelFile(name=path.name, bytes=size, sha256=digest, hf_etag=etag)


def _read_hf_metadata(model_dir: Path) -> dict[str, tuple[str, str]]:
    """{file: (revision, etag)} from a ``snapshot_download(local_dir=...)`` tree.

    Each ``<file>.metadata`` is three lines: commit hash, etag, timestamp.
    """
    found: dict[str, tuple[str, str]] = {}
    for name in MODEL_FILES:
        path = model_dir / HF_METADATA_DIR / f"{name}.metadata"
        if path.is_file():
            lines = path.read_text().splitlines()
            if len(lines) >= 2:
                found[name] = (lines[0].strip(), lines[1].strip())
    return found


def _require_files(model_dir: Path) -> None:
    missing = [name for name in MODEL_FILES if not (model_dir / name).is_file()]
    if missing:
        raise ModelError(f"model directory {model_dir} is missing {', '.join(missing)}")


def _read_provenance(model_dir: Path) -> dict[str, Any] | None:
    path = model_dir / PROVENANCE_NAME
    if not path.is_file():
        return None
    try:
        provenance = json.loads(path.read_text())
        if not all(isinstance(provenance["files"][name]["sha256"], str) for name in MODEL_FILES):
            raise ValueError("file digests must be strings")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ModelError(f"unreadable {path}: {error!r}") from error
    return provenance


def _digests(model_dir: Path) -> dict[str, str]:
    return {name: _digest(model_dir / name).sha256 for name in MODEL_FILES}


def _info_from_provenance(model_dir: Path, provenance: dict[str, Any]) -> ModelInfo:
    files = tuple(
        ModelFile(name=name, bytes=provenance["files"][name]["bytes"], sha256=provenance["files"][name]["sha256"],
                  hf_etag=provenance["files"][name].get("hf_etag"))
        for name in MODEL_FILES
    )
    return ModelInfo(
        path=str(model_dir.resolve()), model_id=provenance.get("model_id"), revision=provenance.get("revision"),
        model_bin_sha256=provenance["files"]["model.bin"]["sha256"], files=files,
        source=provenance.get("source"), created_at=provenance.get("created_at"),
    )


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def link_model(src_dir: str | os.PathLike[str], dest_dir: str | os.PathLike[str], *,
               model_id: str | None = None, replace: bool = False, source: str | None = None) -> ModelInfo:
    """Copy the four model files from ``src_dir`` into ``dest_dir`` with provenance.

    A copy, not a symlink, so the server never depends on benchmark artifacts.
    If ``src_dir`` came from ``snapshot_download(local_dir=...)``, every file is
    checked against its Hugging Face etag and the revision is recorded.
    Idempotent: an identical ``dest_dir`` keeps its files (and its provenance,
    if that still matches); a different one is refused unless ``replace``.
    The copy is staged beside ``dest_dir`` and renamed into place.
    """
    src, dest = Path(src_dir), Path(dest_dir)
    _require_files(src)
    metadata = _read_hf_metadata(src)
    revisions = {revision for revision, _ in metadata.values()}
    if len(revisions) > 1:
        raise ModelError(f"{src} mixes files from revisions {sorted(revisions)}")
    revision = revisions.pop() if revisions else None

    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.parent / f".{dest.name}.partial-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        files = [_digest(src / name, etag=metadata.get(name, (None, None))[1], copy_to=staging / name)
                 for name in MODEL_FILES]
        provenance = {
            "schema_version": 1,
            "model_id": model_id,
            "revision": revision,
            "source": source or str(src.resolve()),
            "created_at": now_iso(),
            "files": {f.name: {"bytes": f.bytes, "sha256": f.sha256, "hf_etag": f.hf_etag} for f in files},
        }
        (staging / PROVENANCE_NAME).write_text(json.dumps(provenance, indent=2) + "\n")
        _fsync_dir(staging)
        wanted = {f.name: f.sha256 for f in files}

        if dest.exists():
            if all((dest / name).is_file() for name in MODEL_FILES) and _digests(dest) == wanted:
                try:
                    current = _read_provenance(dest)
                except ModelError:
                    current = None
                if current is None or {n: current["files"][n]["sha256"] for n in MODEL_FILES} != wanted:
                    os.replace(staging / PROVENANCE_NAME, dest / PROVENANCE_NAME)
                    current = provenance
                return _info_from_provenance(dest, current)
            if not replace:
                raise ModelError(f"{dest} already holds a different model; pass replace=True to swap it")
            retired = dest.parent / f".{dest.name}.old-{os.getpid()}"
            os.replace(dest, retired)
            os.replace(staging, dest)
            shutil.rmtree(retired, ignore_errors=True)
        else:
            os.replace(staging, dest)
        _fsync_dir(dest.parent)
        log.info("installed ASR model %s (revision %s) at %s", model_id, revision, dest)
        return _info_from_provenance(dest, provenance)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def fetch_model(model_id: str, revision: str | None, dest_dir: str | os.PathLike[str], *,
                replace: bool = False) -> ModelInfo:
    """Download the four model files from Hugging Face and install them like ``link_model``.

    ``revision=None`` uses ``PINNED_REVISIONS`` for known models, else the
    default branch; a 40-hex commit is checked after download.
    """
    dest = Path(dest_dir)
    revision = revision or PINNED_REVISIONS.get(model_id)
    # Match how the pinned benchmark models were fetched (README): plain HTTPS,
    # no hf_xet chunk cache outside data/. The flag is read at import time, so
    # also patch the constant in case huggingface_hub is already loaded.
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    import huggingface_hub
    from huggingface_hub import constants

    constants.HF_HUB_DISABLE_XET = True
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.parent / f".{dest.name}.download"
    huggingface_hub.snapshot_download(
        repo_id=model_id, revision=revision, local_dir=staging, allow_patterns=list(MODEL_FILES),
    )
    info = link_model(staging, dest, model_id=model_id, replace=replace,
                      source=f"https://huggingface.co/{model_id}")
    if revision and _COMMIT.fullmatch(revision) and info.revision != revision:
        raise ModelError(f"downloaded revision {info.revision} but asked for {revision}")
    shutil.rmtree(staging, ignore_errors=True)
    return info


def verify_model(model_dir: str | os.PathLike[str]) -> ModelInfo:
    """Re-hash every model file against ``noadcast-model.json`` (or, failing that,
    the Hugging Face download metadata) and describe the model."""
    path = Path(model_dir)
    _require_files(path)
    provenance = _read_provenance(path)
    if provenance is not None:
        actual = _digests(path)
        wrong = [name for name in MODEL_FILES if actual[name] != provenance["files"][name]["sha256"]]
        if wrong:
            raise ModelError(f"{path}: {', '.join(wrong)} no longer match {PROVENANCE_NAME}")
        return _info_from_provenance(path, provenance)

    metadata = _read_hf_metadata(path)
    if set(metadata) != set(MODEL_FILES):
        raise ModelError(f"{path} has no {PROVENANCE_NAME} or Hugging Face metadata to verify against")
    files = {name: _digest(path / name, etag=metadata[name][1]) for name in MODEL_FILES}
    return ModelInfo(path=str(path.resolve()), model_id=None, revision=metadata["model.bin"][0],
                     model_bin_sha256=files["model.bin"].sha256, files=tuple(files.values()))


def model_metadata(model_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """What the pool records per transcript: model.bin's sha256, hashed now
    because that is what the workers load, plus the recorded revision."""
    path = Path(model_dir)
    binary = path / "model.bin"
    if not binary.is_file():
        raise FileNotFoundError(f"local model is incomplete: {binary} does not exist "
                                "(install one with `noadcast models link` or `noadcast models fetch`)")
    model_bin = _digest(binary)
    try:
        provenance = _read_provenance(path) or {}
    except ModelError as error:
        log.warning("%s; recording the model without provenance", error)
        provenance = {}
    revision = provenance.get("revision") or _read_hf_metadata(path).get("model.bin", (None, None))[0]
    recorded = provenance.get("files", {}).get("model.bin", {}).get("sha256")
    if recorded and recorded != model_bin.sha256:
        log.warning("model.bin at %s does not match %s; recording its actual sha256", path, PROVENANCE_NAME)
    return {"path": str(path.resolve()), "model_id": provenance.get("model_id"), "revision": revision,
            "model_bin_sha256": model_bin.sha256, "model_bin_bytes": model_bin.bytes}
