"""HTTP Range serving for stored audio (RFC 9110 §13-14).

AVPlayer streams by issuing byte-range GETs and seeks by issuing new ones, so
this is on the playback path of every episode. Conditional evaluation follows
RFC 9110 §13.2.2: ``If-None-Match`` first (a hit is 304), then ``If-Range``
(a miss serves the full 200), then ``Range``.

Deliberate choices:

- One range only. A multi-range request gets the whole body with 200, which
  RFC 9110 permits: AVFoundation never sends one, and ``multipart/byteranges``
  would be the riskiest code here for no client.
- HEAD returns exactly the headers of the corresponding GET, Content-Length
  included, with no body. Range handling is defined only for GET (RFC 9110
  §14.2), so a HEAD's Range and If-Range are ignored. Uvicorn (h11 and
  httptools) keeps an explicit Content-Length on HEAD and drops any body.
- Bodies are read with ``os.pread`` in 256 KiB chunks on worker threads, so
  a cold disk never stalls the event loop, and streaming stops as soon as
  the client disconnects (AVPlayer abandons requests constantly while
  seeking). Uvicorn speaks ASGI 2.3, where a send after a disconnect is
  silently dropped, so the stream watches for ``http.disconnect`` itself.

Compression would break Range semantics — offsets would address the
compressed bytes — so these responses must never be content-encoded. Nothing
in a response can forbid a middleware from compressing it; what makes it
safe is that Starlette's ``GZipMiddleware`` (1.6) passes through any 206, any
response that already has a Content-Encoding, and any media type in its
``exclude_content_types``, whose default includes ``audio/*`` and
``video/*``. ``audio_file_response`` therefore insists on such a type, and
adds ``Cache-Control: no-transform`` for proxies.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import os
import re
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.types import Send

UNSATISFIABLE = "unsatisfiable"
CHUNK_BYTES = 256 * 1024
CACHE_CONTROL = "private, max-age=31536000, immutable, no-transform"

_RANGE_SPEC = re.compile(r"([0-9]*)-([0-9]*)")
_ENTITY_TAG = re.compile(r'(W/)?"([^"]*)"')
_ETAG_CHARS = re.compile(r"[\x21\x23-\x7e]*")


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """Return ``(start, end_inclusive)`` for a satisfiable single range, ``None``
    to serve the full body (no header, or multi-range), or ``"unsatisfiable"``.

    Per RFC 9110 §14.1-14.2: ``bytes=a-b``, ``bytes=a-`` and the suffix form
    ``bytes=-n`` (the last n bytes; ``-0`` selects nothing). ``b`` is clamped
    to the last byte; ``a >= size`` is unsatisfiable. A malformed header, a
    ``b < a`` range, an unknown unit, or several ranges are ignored (None),
    which the RFC allows for any Range header.
    """
    if header is None:
        return None
    unit, equals, range_set = header.partition("=")
    if not equals or unit.strip().lower() != "bytes":
        return None
    specs = [spec.strip() for spec in range_set.split(",") if spec.strip()]  # empty list elements are legal
    if len(specs) != 1:
        return None
    match = _RANGE_SPEC.fullmatch(specs[0])
    if match is None:
        return None
    first, last = (_canonical(digits) for digits in match.groups())
    length = str(size)
    if first:
        if last and _less(last, first):
            return None  # an invalid int-range voids the header
        if not _less(first, length):
            return UNSATISFIABLE
        end = int(last) if last and _less(last, length) else size - 1
        return int(first), end
    if not last:
        return None  # "bytes=-"
    if last == "0" or size == 0:
        return UNSATISFIABLE
    start = size - int(last) if _less(last, length) else 0
    return start, size - 1


def _canonical(digits: str) -> str:
    """Leading zeros stripped ("" stays absent, "000" becomes "0")."""
    return (digits.lstrip("0") or "0") if digits else ""


def _less(a: str, b: str) -> bool:
    """Numeric ``a < b`` for canonical digit strings, without ``int()``: a
    hostile 5000-digit position must neither hit int()'s digit limit nor
    cost a bignum conversion."""
    return (len(a), a) < (len(b), b)


def audio_file_response(
    request: Request,
    path: Path,
    *,
    size: int,
    content_type: str,
    etag: str,
    last_modified: dt.datetime | None,
    filename: str,
) -> Response:
    """200/206/304/416 response honouring Range, If-Range, If-None-Match, and HEAD.

    ``etag`` is a strong validator, bare (the sha256 hex) or already quoted.
    ``content_type`` must be ``audio/*`` or ``video/*`` (see the module
    docstring); ``size`` is the file's length. For a GET with a body the file
    is opened here, so a file that has vanished raises ``FileNotFoundError``
    inside the route handler, where it can become a 409 rather than a
    broken stream.
    """
    if size < 0:
        raise ValueError("size must not be negative")
    if not content_type.strip().lower().startswith(("audio/", "video/")):
        raise ValueError(f"audio responses must be audio/* or video/* to stay uncompressed, not {content_type!r}")
    tag = strong_etag(etag)
    headers = {"accept-ranges": "bytes", "etag": tag, "cache-control": CACHE_CONTROL}
    if last_modified is not None:
        headers["last-modified"] = http_date(last_modified)

    if _none_match(request.headers.get("if-none-match"), tag):
        return Response(status_code=304, headers=headers)

    selection = None
    if request.method == "GET":
        ranges = request.headers.getlist("range")
        if len(ranges) == 1 and _if_range_holds(request.headers.get("if-range"), tag, last_modified):
            selection = parse_range(ranges[0], size)
    if selection == UNSATISFIABLE:
        headers |= {"content-range": f"bytes */{size}", "cache-control": "no-store"}
        return Response(status_code=416, headers=headers)

    headers |= {"content-type": content_type, "content-disposition": content_disposition(filename)}
    if isinstance(selection, tuple):
        start, end = selection
        status = 206
        headers["content-range"] = f"bytes {start}-{end}/{size}"
    else:
        start, end, status = 0, size - 1, 200
    headers["content-length"] = str(end - start + 1)
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers)
    return _FileSliceResponse(open(path, "rb", buffering=0), start, end - start + 1, status_code=status, headers=headers)


def strong_etag(value: str) -> str:
    """Quote a bare validator; reject weak or malformed ones (Range needs a strong ETag)."""
    tag = value.strip()
    if tag.startswith("W/"):
        raise ValueError("a weak ETag cannot validate byte ranges")
    if not (len(tag) >= 2 and tag[0] == tag[-1] == '"'):
        tag = f'"{tag}"'
    if not _ETAG_CHARS.fullmatch(tag[1:-1]):
        raise ValueError(f"invalid ETag {value!r}")
    return tag


def http_date(value: dt.datetime) -> str:
    """RFC 9110 IMF-fixdate. Naive datetimes are taken as UTC."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
    return email.utils.format_datetime(aware.astimezone(dt.UTC), usegmt=True)


def content_disposition(filename: str, *, disposition: str = "inline") -> str:
    """``inline; filename="…"`` safe for any input: control characters,
    quotes, backslashes and slashes become ``_``, and a non-ASCII name also
    travels as RFC 5987 ``filename*`` with an ASCII fallback."""
    cleaned = "".join("_" if ch in '"\\/' or ch < " " or "\x7f" <= ch <= "\x9f" else ch for ch in filename).strip()
    cleaned = cleaned or "audio"
    fallback = "".join(ch if ch <= "~" else "_" for ch in cleaned)
    value = f'{disposition}; filename="{fallback}"'
    if fallback != cleaned:
        value += "; filename*=UTF-8''" + quote(cleaned, safe="", errors="replace")
    return value


def _none_match(header: str | None, tag: str) -> bool:
    """If-None-Match uses the weak comparison: ``W/"x"`` matches ``"x"``."""
    if header is None:
        return False
    if header.strip() == "*":
        return True
    opaque = tag[1:-1]
    return any(match[2] == opaque for match in _ENTITY_TAG.finditer(header))


def _if_range_holds(header: str | None, tag: str, last_modified: dt.datetime | None) -> bool:
    """RFC 9110 §13.1.5: an entity tag must match strongly (a weak one never
    does); a date must equal Last-Modified exactly. Anything else fails, so
    the client gets the full, current representation."""
    if header is None:
        return True
    value = header.strip()
    if value.startswith(('"', "W/")):
        return value == tag
    if last_modified is None:
        return False
    try:
        claimed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return False
    if claimed.tzinfo is None:
        claimed = claimed.replace(tzinfo=dt.UTC)
    actual = last_modified if last_modified.tzinfo is not None else last_modified.replace(tzinfo=dt.UTC)
    return int(claimed.timestamp()) == int(actual.timestamp())  # HTTP-dates have 1 s resolution


class _FileSliceResponse(StreamingResponse):
    """Streams ``length`` bytes of an open file from ``offset``.

    Reuses StreamingResponse's ``__call__``, which races the body against a
    ``http.disconnect`` listener (or handles ASGI 2.4's OSError on send), and
    replaces only the body loop. The file is closed on every path, including
    cancellation by that listener.
    """

    def __init__(self, file: BinaryIO, offset: int, length: int, *, status_code: int, headers: dict[str, str]) -> None:
        self.file = file
        self.offset = offset
        self.length = length
        self.status_code = status_code
        self.background = None
        self.init_headers(headers)

    async def stream_response(self, send: Send) -> None:
        try:
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            fd = self.file.fileno()
            position, remaining = self.offset, self.length
            while remaining > 0:
                chunk = await anyio.to_thread.run_sync(os.pread, fd, min(CHUNK_BYTES, remaining), position)
                if not chunk:
                    # Headers promised more; abort the connection rather than end short.
                    raise RuntimeError(f"{self.file.name} ended {remaining} bytes early")
                position += len(chunk)
                remaining -= len(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": remaining > 0})
            if self.length == 0:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            self.file.close()
