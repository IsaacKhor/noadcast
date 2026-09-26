"""On-disk layout under the data directory.

Stored audio lives at ``audio/<podcast_id>/<episode_id>.<ext>``. The database
records paths relative to the data directory, always with ``/`` separators,
so moving the data directory never invalidates a row.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .downloader import part_path

AUDIO_DIR = "audio"
DEFAULT_EXTENSION = "mp3"

# Keyed by MIME subtype; the major type is irrelevant here (feeds label MP4
# audio as both audio/mp4 and video/mp4).
_EXTENSIONS_BY_SUBTYPE = {
    **dict.fromkeys(("mpeg", "mp3", "mpeg3", "x-mpeg", "x-mp3", "x-mpeg3", "mpg"), "mp3"),
    **dict.fromkeys(("mp4", "m4a", "x-m4a"), "m4a"),
    **dict.fromkeys(("aac", "x-aac", "aacp"), "aac"),
    **dict.fromkeys(("ogg", "x-ogg", "opus", "vorbis"), "ogg"),
    **dict.fromkeys(("wav", "x-wav", "wave", "vnd.wave"), "wav"),
    **dict.fromkeys(("flac", "x-flac"), "flac"),
}
_KNOWN_AUDIO_EXTENSIONS = frozenset({"mp3", "m4a", "m4b", "mp4", "aac", "ogg", "oga", "opus", "wav", "flac"})
_EXTENSION = re.compile(r"[a-z0-9]{1,8}")


@dataclass(frozen=True)
class DiskUsage:
    total_bytes: int
    used_bytes: int
    free_bytes: int


class MediaStore:
    """``audio/<podcast_id>/<episode_id>.<ext>``; all paths stored in the DB are
    relative to ``data_dir``."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def audio_relpath(self, podcast_id: int, episode_id: int, ext: str) -> str:
        suffix = ext.strip().lstrip(".").lower()
        if not _EXTENSION.fullmatch(suffix):
            raise ValueError(f"bad audio extension {ext!r}")
        return f"{AUDIO_DIR}/{podcast_id}/{episode_id}.{suffix}"

    def abspath(self, relpath: str) -> Path:
        """Absolute path for a stored relative path. Rejects absolute paths and
        ``..`` so a bad row can never address anything outside ``data_dir``."""
        rel = PurePosixPath(relpath)
        if not rel.parts or rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"not a data-relative path: {relpath!r}")
        return self.data_dir.joinpath(*rel.parts).absolute()

    @staticmethod
    def extension_for(content_type: str | None, url: str | None) -> str:
        """File extension from the response's MIME type, else a known audio
        extension on the URL path (``application/octet-stream`` is a common
        CDN answer), else ``mp3``, by far the most common podcast format."""
        if content_type:
            subtype = content_type.split(";", 1)[0].strip().lower().rpartition("/")[2]
            if subtype in _EXTENSIONS_BY_SUBTYPE:
                return _EXTENSIONS_BY_SUBTYPE[subtype]
        if url:
            suffix = PurePosixPath(unquote(urlsplit(url).path)).suffix.lstrip(".").lower()
            if suffix in _KNOWN_AUDIO_EXTENSIONS:
                return suffix
        return DEFAULT_EXTENSION

    def remove(self, relpath: str) -> int:
        """Delete a stored file (and its .part); returns bytes freed."""
        path = self.abspath(relpath)
        freed = 0
        for candidate in (path, part_path(path)):
            try:
                size = candidate.stat().st_size
                candidate.unlink()
            except FileNotFoundError:
                continue
            freed += size
        return freed

    def disk_usage(self) -> DiskUsage:
        # The data directory may not exist yet; measure the filesystem it will live on.
        probe = self.data_dir.absolute()
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        usage = shutil.disk_usage(probe)
        return DiskUsage(total_bytes=usage.total, used_bytes=usage.used, free_bytes=usage.free)

    def iter_part_files(self) -> list[Path]:
        """Every in-progress download under ``audio/``, as absolute paths."""
        root = self.data_dir / AUDIO_DIR
        return sorted(path.absolute() for path in root.rglob("*.part") if path.is_file())
