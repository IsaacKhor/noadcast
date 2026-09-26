"""Resumable audio downloads.

Bytes stream into ``part_path(dest)`` with a running sha256 and land at
``dest`` only once complete (flush, fsync, ``os.replace``, fsync the
directory), so a crash never leaves a truncated file under the final name.

Resume is HTTP-native: ``Range: bytes=<part size>-`` guarded by ``If-Range``,
so a server whose file changed answers 200 with the new bytes rather than
206 with a tail that no longer fits the prefix. That needs a validator for
the bytes already on disk, which is why a transient ``DownloadError`` carries
``etag``/``last_modified``: store them, pass them to the next call, and it
resumes. Without validators an existing part is overwritten from byte 0.

All disk work runs in worker threads; the event loop only shuffles chunks.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import BinaryIO, Callable

import anyio.to_thread
import httpx

from ..feeds.fetcher import USER_AGENT, status_error

CHUNK_BYTES = 1024 * 1024

ProgressFn = Callable[[int, int | None], None]
ValidatorsFn = Callable[[str | None, str | None], None]  # (etag, last_modified)  # (bytes_so_far, total_or_None)

_CONTENT_RANGE = re.compile(r"bytes\s+([0-9]{1,18})-([0-9]{1,18})/([0-9]{1,18}|\*)", re.IGNORECASE)


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    bytes: int
    sha256: str
    content_type: str | None
    etag: str | None
    last_modified: str | None
    resumed: bool
    final_url: str


class DownloadError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        permanent: bool = False,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.permanent = permanent
        # Validators of the bytes left in the .part file (None when no part
        # was kept, or the server sent none): pass them back to resume.
        self.etag = etag
        self.last_modified = last_modified


def part_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".part")


async def download_audio(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    max_bytes: int = 3 * 1024**3,
    on_progress: ProgressFn | None = None,
    timeout: float = 60.0,
    on_validators: ValidatorsFn | None = None,
) -> DownloadResult:
    """Download ``url`` to ``dest`` via ``part_path(dest)``, resuming an existing
    part file with ``Range`` + ``If-Range`` when validators are supplied.

    ``etag``/``last_modified`` describe the bytes already in the part file
    (from a failed attempt's ``DownloadError``). A 206 is appended after
    re-hashing the prefix; a 200 (range ignored, or ``If-Range`` saw a
    changed file) restarts from zero; a 416 or a 206 starting anywhere but
    the part's end discards the part and retries once without ``Range``.

    ``timeout`` is httpx's per-operation timeout, i.e. stall detection; a
    long download is never cut off for being long. ``on_progress`` is called
    after every chunk written (1 MiB) with ``(bytes_so_far, total)``; the
    caller throttles.

    ``on_validators(etag, last_modified)`` is called as soon as the part file
    starts holding a response's bytes, before any are written. Persisting
    them there lets a download interrupted by a crash — which never gets to
    raise a ``DownloadError`` carrying them — resume with ``If-Range``.

    Raises ``DownloadError``. Permanent (part removed): 4xx other than
    408/425/429, an unusable URL, or more than ``max_bytes``. Transient:
    429/5xx (with ``retry_after``), timeouts, connection drops (part kept,
    validators on the error), and local disk errors (part removed).
    """
    transfer = _Transfer(
        client, url, dest, max_bytes=max_bytes, on_progress=on_progress, on_validators=on_validators, timeout=timeout
    )
    await transfer.disk(partial(dest.parent.mkdir, parents=True, exist_ok=True))
    validator = _if_range_validator(etag, last_modified)
    offset = await transfer.disk(_size_or_zero, transfer.part) if validator else 0
    if 0 < offset <= max_bytes:
        transfer.etag, transfer.last_modified = etag, last_modified
        result = await transfer.run(offset=offset, if_range=validator)
        if result is not None:
            return result
        await transfer.discard()
    result = await transfer.run(offset=0, if_range=None)
    assert result is not None, "only a ranged request can ask for a restart"
    return result


def _if_range_validator(etag: str | None, last_modified: str | None) -> str | None:
    # RFC 9110 §13.1.5: a client must not send a weak entity tag in If-Range.
    if etag and etag.strip() and not etag.strip().startswith("W/"):
        return etag.strip()
    return last_modified.strip() if last_modified and last_modified.strip() else None


def _size_or_zero(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _media_type(value: str | None) -> str | None:
    media_type = (value or "").split(";", 1)[0].strip().lower()
    return media_type or None


def _is_encoded(response: httpx.Response) -> bool:
    # We ask for identity; a server that compresses anyway gets its body
    # decoded, but its Content-Length and byte offsets are then meaningless.
    return response.headers.get("content-encoding", "").strip().lower() not in ("", "identity")


class _PartFile:
    """The part file plus a running sha256. Blocking; used from worker threads."""

    def __init__(self, path: Path, file: BinaryIO, digest: "hashlib._Hash") -> None:
        self.path = path
        self.file = file
        self.digest = digest
        self.size = file.tell()

    @classmethod
    def create(cls, path: Path) -> "_PartFile":
        return cls(path, open(path, "wb"), hashlib.sha256())

    @classmethod
    def reopen(cls, path: Path) -> "_PartFile":
        """Open for appending after hashing the bytes already there."""
        file = open(path, "r+b")
        try:
            digest = hashlib.file_digest(file, "sha256")
            file.seek(0, os.SEEK_END)
        except BaseException:
            file.close()
            raise
        return cls(path, file, digest)

    def write(self, data: bytes) -> None:
        self.file.write(data)
        self.digest.update(data)
        self.size += len(data)

    def commit(self, dest: Path) -> str:
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        os.replace(self.path, dest)
        directory = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)  # makes the rename itself durable
        finally:
            os.close(directory)
        return self.digest.hexdigest()

    def close(self) -> None:
        self.file.close()


class _Transfer:
    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        dest: Path,
        *,
        max_bytes: int,
        on_progress: ProgressFn | None,
        on_validators: ValidatorsFn | None = None,
        timeout: float,
    ) -> None:
        self.client = client
        self.url = url
        self.dest = dest
        self.part = part_path(dest)
        self.max_bytes = max_bytes
        self.on_progress = on_progress
        self.on_validators = on_validators
        self.timeout = timeout
        # Validators of whatever the part file currently holds.
        self.etag: str | None = None
        self.last_modified: str | None = None

    def error(
        self, message: str, *, status: int | None = None, retry_after: float | None = None, permanent: bool = False
    ) -> DownloadError:
        """An error that remembers which validators go with the part file."""
        return DownloadError(
            message,
            status=status,
            retry_after=retry_after,
            permanent=permanent,
            etag=self.etag,
            last_modified=self.last_modified,
        )

    async def disk[T](self, fn: Callable[..., T], *args: object) -> T:
        """Run blocking file I/O in a worker thread; a local failure (disk
        full, permissions) discards the part, whose tail may be torn."""
        try:
            return await anyio.to_thread.run_sync(fn, *args)
        except OSError as exc:
            with contextlib.suppress(OSError):
                await anyio.to_thread.run_sync(partial(self.part.unlink, missing_ok=True))
            raise DownloadError(f"local disk error: {exc}") from exc

    async def discard(self) -> None:
        await self.disk(partial(self.part.unlink, missing_ok=True))
        self.etag = self.last_modified = None

    async def run(self, *, offset: int, if_range: str | None) -> DownloadResult | None:
        """One request. None means "discard the part and retry without Range"."""
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if offset:
            assert if_range is not None
            headers["Range"] = f"bytes={offset}-"
            headers["If-Range"] = if_range
        try:
            async with self.client.stream(
                "GET", self.url, headers=headers, timeout=self.timeout, follow_redirects=True
            ) as response:
                return await self._receive(response, offset)
        except httpx.TimeoutException as exc:
            raise self.error(f"download stalled: {type(exc).__name__}") from exc
        except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
            await self.discard()
            raise DownloadError(f"unusable audio URL: {exc}", permanent=True) from exc
        except httpx.TooManyRedirects as exc:
            raise self.error("too many redirects") from exc
        except httpx.HTTPError as exc:
            raise self.error(f"download failed: {exc or type(exc).__name__}") from exc

    async def _receive(self, response: httpx.Response, offset: int) -> DownloadResult | None:
        status = response.status_code
        if offset and status == 416:
            return None
        if status == 206:
            if not offset:
                raise self.error("206 Partial Content for a request without Range", status=status)
            match = _CONTENT_RANGE.fullmatch(response.headers.get("content-range", "").strip())
            if match is None or int(match[1]) != offset or match[3] == "*" or _is_encoded(response):
                return None
            total: int | None = int(match[3])
            append = True
        elif 200 <= status < 300:
            declared = response.headers.get("content-length", "").strip()
            total = int(declared) if declared.isdigit() and len(declared) <= 18 and not _is_encoded(response) else None
            append = False
        else:
            error = status_error(response, self.error)
            if error.permanent:
                await self.discard()
                error.etag = error.last_modified = None
            raise error

        if total is not None and total > self.max_bytes:
            await self.discard()
            raise DownloadError(f"audio is {total} bytes; the limit is {self.max_bytes}", status=status, permanent=True)

        writer = await self.disk(_PartFile.reopen if append else _PartFile.create, self.part)
        try:
            if append and writer.size != offset:  # the part changed under us
                return None
            # From here the part holds this response's bytes.
            self.etag = response.headers.get("etag") or (self.etag if append else None)
            self.last_modified = response.headers.get("last-modified") or (self.last_modified if append else None)
            if self.on_validators is not None:
                self.on_validators(self.etag, self.last_modified)
            await self._stream(response, writer, total)
            if total is not None and writer.size != total:
                raise self.error(f"transfer ended at byte {writer.size} of {total}", status=status)
            if writer.size == 0:
                raise self.error("empty response body", status=status)
            digest = await self.disk(writer.commit, self.dest)
        finally:
            writer.close()
        return DownloadResult(
            path=self.dest,
            bytes=writer.size,
            sha256=digest,
            content_type=_media_type(response.headers.get("content-type")),
            etag=self.etag,
            last_modified=self.last_modified,
            resumed=append,
            final_url=str(response.url),
        )

    async def _stream(self, response: httpx.Response, writer: _PartFile, total: int | None) -> None:
        pending = bytearray()

        async def flush() -> None:
            if pending:
                await self.disk(writer.write, bytes(pending))
                pending.clear()
                if self.on_progress is not None:
                    self.on_progress(writer.size, total)

        try:
            async for piece in response.aiter_bytes():
                pending += piece
                if writer.size + len(pending) > self.max_bytes:
                    writer.close()
                    await self.discard()
                    raise DownloadError(f"audio exceeds the {self.max_bytes}-byte limit", permanent=True)
                if len(pending) >= CHUNK_BYTES:
                    await flush()
        except httpx.HTTPError:
            await flush()  # every byte that did arrive is a valid prefix to resume from
            raise
        await flush()
