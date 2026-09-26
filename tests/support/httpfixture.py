"""A local HTTP origin for tests: real sockets, real HTTP/1.1, scripted faults.

``HTTPFixture`` runs a threaded ``http.server`` on 127.0.0.1 at an ephemeral
port for the length of a ``with`` block, standing in for the podcast hosts
and CDNs the server talks to (keep-alive included). It is an independent
implementation of Range and conditional requests on purpose: code under
test is never checked against itself.

    with HTTPFixture() as origin:
        origin.serve("/feed.xml", feed_bytes, content_type="application/rss+xml")
        origin.serve("/ep1.mp3", Path("benchmarks/tal/audio/01-646.mp3"), content_type="audio/mpeg")
        origin.redirect("/old.xml", "/feed.xml", status=301)
        origin.drop_after("/ep1.mp3", 5_000_000)                 # the next GET dies mid-body
        origin.fail("/ep2.mp3", 503, times=2, headers={"Retry-After": "7"})
        ...fetch origin.url("/feed.xml")...
        assert origin.requests_for("/ep1.mp3")[-1].headers["range"] == "bytes=5000000-"

Resources — ``serve(path, content, **options)``; serving a path again replaces it
    content        bytes (held in memory) or a Path (re-read on every request,
                   so a test may rewrite the file between requests).
    content_type   Content-Type header (default application/octet-stream).
    etag           Full header value such as '"v1"', None for none, or AUTO:
                   bytes -> quoted sha256 prefix; files -> size and mtime, so
                   rewriting a file changes its ETag.
    last_modified  HTTP-date string, None, or AUTO (file mtime; for bytes, the
                   time of the serve() call).
    ranges         True: honour Range (206/416) and If-Range, advertise
                   Accept-Ranges. False: a server that ignores Range (always 200).
    conditional    True: If-None-Match / If-Modified-Since hits answer 304.
    gzip           Gzip the body when Accept-Encoding allows it (never a 206).
    chunked        Omit Content-Length; use chunked transfer coding.
    headers        Extra response headers.
``remove(path)`` makes later requests 404. ``redirect(path, location, status=302)``
always redirects; ``location`` is sent verbatim (relative or absolute).

Faults — per path, checked in registration order; ``times=None`` is forever
    fail(path, status, times=1, headers=None, body=b"")
                   Answer ``status`` instead (429 + Retry-After, 5xx, 404, ...).
    drop_after(path, nbytes, times=1)
                   The next GET gets its headers and exactly ``nbytes`` of body,
                   then a FIN: a mid-stream connection drop.
    delay(path, seconds, times=1)
                   Wait before answering (client timeouts). Cut short on exit.

Inspection
    url(path), base_url, port
    requests       Snapshot list of ``LoggedRequest``: method, path (no query),
                   target (as sent), headers (lower-case keys), status and
                   body_bytes. Entries are logged on arrival and completed
                   before the response is written, so once a client has a
                   response its entry is final.
    requests_for(path), clear_log()

Paths match exactly as sent, minus the query string. Only GET and HEAD are
implemented; anything else gets http.server's 501.
"""

from __future__ import annotations

import email.utils
import enum
import gzip as gzip_codec
import hashlib
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

__all__ = ["AUTO", "HTTPFixture", "LoggedRequest"]

_CHUNK = 256 * 1024
_RANGE = re.compile(r"bytes=([0-9]*)-([0-9]*)")


class _Auto(enum.Enum):
    AUTO = "auto"


AUTO = _Auto.AUTO


@dataclass
class LoggedRequest:
    method: str
    path: str
    target: str
    headers: dict[str, str]
    status: int | None = None
    body_bytes: int = 0


@dataclass(frozen=True)
class _Resource:
    content: bytes | Path
    content_type: str
    etag: str | None | _Auto
    last_modified: str | None | _Auto
    ranges: bool
    conditional: bool
    gzip: bool
    chunked: bool
    headers: dict[str, str]
    served_at: float


@dataclass(frozen=True)
class _Redirect:
    location: str
    status: int


@dataclass
class _Fault:
    kind: str  # fail | drop | delay
    remaining: int | None
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    nbytes: int = 0
    seconds: float = 0.0


class HTTPFixture:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._routes: dict[str, _Resource | _Redirect] = {}
        self._faults: dict[str, list[_Fault]] = {}
        self._log: list[LoggedRequest] = []
        self._connections: set[socket.socket] = set()
        self._stopping = threading.Event()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> "HTTPFixture":
        self._server = _Server(self)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, name="httpfixture", daemon=True
        )
        self._thread.start()
        return self

    def close(self) -> None:
        """Stop accepting, cut every open connection, and join all threads."""
        if self._server is None:
            return
        with self._lock:
            self._stopping.set()
            connections = list(self._connections)
        self._server.shutdown()
        for connection in connections:
            _hang_up(connection)
        self._server.server_close()  # joins handler threads (block_on_close)
        assert self._thread is not None
        self._thread.join()
        self._server = None

    def __enter__(self) -> "HTTPFixture":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def port(self) -> int:
        assert self._server is not None, "not started"
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, path: str) -> str:
        return self.base_url + path

    # -- configuration -------------------------------------------------------------

    def serve(
        self,
        path: str,
        content: bytes | Path,
        *,
        content_type: str = "application/octet-stream",
        etag: str | None | _Auto = AUTO,
        last_modified: str | None | _Auto = AUTO,
        ranges: bool = True,
        conditional: bool = True,
        gzip: bool = False,
        chunked: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        resource = _Resource(
            content=content,
            content_type=content_type,
            etag=etag,
            last_modified=last_modified,
            ranges=ranges,
            conditional=conditional,
            gzip=gzip,
            chunked=chunked,
            headers=dict(headers or {}),
            served_at=time.time(),
        )
        with self._lock:
            self._routes[path] = resource

    def remove(self, path: str) -> None:
        with self._lock:
            self._routes.pop(path, None)

    def redirect(self, path: str, location: str, *, status: int = 302) -> None:
        with self._lock:
            self._routes[path] = _Redirect(location, status)

    def fail(
        self, path: str, status: int, *, times: int | None = 1, headers: dict[str, str] | None = None, body: bytes = b""
    ) -> None:
        self._add_fault(path, _Fault("fail", times, status=status, headers=dict(headers or {}), body=body))

    def drop_after(self, path: str, nbytes: int, *, times: int | None = 1) -> None:
        self._add_fault(path, _Fault("drop", times, nbytes=nbytes))

    def delay(self, path: str, seconds: float, *, times: int | None = 1) -> None:
        self._add_fault(path, _Fault("delay", times, seconds=seconds))

    def _add_fault(self, path: str, fault: _Fault) -> None:
        with self._lock:
            self._faults.setdefault(path, []).append(fault)

    # -- inspection ----------------------------------------------------------------

    @property
    def requests(self) -> list[LoggedRequest]:
        with self._lock:
            return list(self._log)

    def requests_for(self, path: str) -> list[LoggedRequest]:
        return [request for request in self.requests if request.path == path]

    def clear_log(self) -> None:
        with self._lock:
            self._log.clear()

    # -- serving (handler threads) ---------------------------------------------------

    def _track(self, connection: socket.socket) -> bool:
        with self._lock:
            if self._stopping.is_set():
                return False
            self._connections.add(connection)
            return True

    def _untrack(self, connection: socket.socket) -> None:
        with self._lock:
            self._connections.discard(connection)

    def _take_faults(self, path: str, method: str) -> tuple[list[float], _Fault | None, int | None]:
        """Consume the faults that apply to this request: delays, then the
        first fail (which ends the request) or drop (GET only)."""
        delays: list[float] = []
        with self._lock:
            for fault in self._faults.get(path, []):
                if fault.remaining == 0 or (fault.kind == "drop" and method != "GET"):
                    continue
                if fault.remaining is not None:
                    fault.remaining -= 1
                if fault.kind == "delay":
                    delays.append(fault.seconds)
                elif fault.kind == "fail":
                    return delays, fault, None
                else:
                    return delays, None, fault.nbytes
        return delays, None, None

    def _handle(self, handler: _Handler) -> None:
        method = handler.command
        path = urlsplit(handler.path).path
        entry = LoggedRequest(method, path, handler.path, {k.lower(): v for k, v in handler.headers.items()})
        with self._lock:
            self._log.append(entry)
            route = self._routes.get(path)
        delays, failure, drop = self._take_faults(path, method)
        for seconds in delays:
            if self._stopping.wait(seconds):
                handler.close_connection = True
                return
        if failure is not None:
            self._simple(handler, entry, failure.status, failure.headers, failure.body)
        elif route is None:
            self._simple(handler, entry, 404, {"Content-Type": "text/plain"}, b"not found\n")
        elif isinstance(route, _Redirect):
            self._simple(handler, entry, route.status, {"Location": route.location}, b"")
        else:
            self._resource(handler, entry, route, drop)

    def _simple(
        self, handler: _Handler, entry: LoggedRequest, status: int, headers: dict[str, str], body: bytes
    ) -> None:
        entry.status = status
        entry.body_bytes = 0 if handler.command == "HEAD" else len(body)
        handler.send_response(status)
        for name, value in headers.items():
            handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)

    def _resource(self, handler: _Handler, entry: LoggedRequest, resource: _Resource, drop: int | None) -> None:
        request = entry.headers
        if isinstance(resource.content, Path):
            try:
                stat = resource.content.stat()
            except FileNotFoundError:
                self._simple(handler, entry, 404, {"Content-Type": "text/plain"}, b"not found\n")
                return
            size = stat.st_size
            auto_etag = f'"{stat.st_size:x}-{stat.st_mtime_ns:x}"'
            auto_date = email.utils.formatdate(stat.st_mtime, usegmt=True)
        else:
            size = len(resource.content)
            auto_etag = f'"{hashlib.sha256(resource.content).hexdigest()[:16]}"'
            auto_date = email.utils.formatdate(resource.served_at, usegmt=True)
        etag = auto_etag if resource.etag is AUTO else resource.etag
        last_modified = auto_date if resource.last_modified is AUTO else resource.last_modified
        validators = {"ETag": etag, "Last-Modified": last_modified}

        if resource.conditional and _not_modified(request, etag, last_modified):
            entry.status = 304
            handler.send_response(304)
            _send_headers(handler, {**validators, **resource.headers})
            return

        status, start, length = 200, 0, size
        if resource.ranges and handler.command == "GET" and "range" in request:
            if_range = request.get("if-range")
            if if_range is None or if_range.strip() in (etag, last_modified):
                selected = _single_range(request["range"], size)
                if selected == "unsatisfiable":
                    self._simple(handler, entry, 416, {"Content-Range": f"bytes */{size}", **resource.headers}, b"")
                    return
                if selected is not None:
                    start, end = selected
                    status, length = 206, end - start + 1

        headers: dict[str, str | None] = {"Content-Type": resource.content_type, **validators}
        if resource.ranges:
            headers["Accept-Ranges"] = "bytes"
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{size}"
        body: bytes | None = None  # None: stream the file slice
        if resource.gzip and status == 200 and "gzip" in request.get("accept-encoding", ""):
            body = gzip_codec.compress(_read_all(resource.content), mtime=0)
            start, length = 0, len(body)
            headers |= {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
        elif isinstance(resource.content, bytes):
            body = resource.content
        headers |= resource.headers
        if resource.chunked:
            headers["Transfer-Encoding"] = "chunked"
        else:
            headers["Content-Length"] = str(length)

        sent = length if drop is None else min(length, drop)
        entry.status = status
        entry.body_bytes = 0 if handler.command == "HEAD" else sent
        handler.send_response(status)
        _send_headers(handler, headers)
        if handler.command == "HEAD":
            return
        pieces = _slices(body, start, sent) if body is not None else _file_slices(resource.content, start, sent)
        for piece in pieces:
            if resource.chunked:
                handler.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
            else:
                handler.wfile.write(piece)
        if sent < length:
            _hang_up(handler.connection, write_only=True)
            handler.close_connection = True
        elif resource.chunked:
            handler.wfile.write(b"0\r\n\r\n")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, like real hosts; every response is framed
    timeout = 60  # a safety net only: close() hangs up on live connections itself
    server: "_Server"

    def setup(self) -> None:
        super().setup()
        if not self.server.fixture._track(self.connection):
            _hang_up(self.connection)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.server.fixture._untrack(self.connection)

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            self.server.fixture._handle(self)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True  # the client hung up mid-response

    def log_message(self, format: str, *args: object) -> None:
        """Silence the per-request stderr lines."""


class _Server(ThreadingHTTPServer):
    block_on_close = True  # server_close() joins handler threads

    def __init__(self, fixture: HTTPFixture) -> None:
        self.fixture = fixture
        super().__init__(("127.0.0.1", 0), _Handler)

    def handle_error(self, request: object, client_address: object) -> None:
        if isinstance(sys.exc_info()[1], OSError):
            return  # sockets cut by clients or by close() are expected
        super().handle_error(request, client_address)


def _hang_up(connection: socket.socket, *, write_only: bool = False) -> None:
    try:
        connection.shutdown(socket.SHUT_WR if write_only else socket.SHUT_RDWR)
    except OSError:
        pass  # already closed


def _send_headers(handler: BaseHTTPRequestHandler, headers: dict[str, str | None]) -> None:
    for name, value in headers.items():
        if value is not None:
            handler.send_header(name, value)
    handler.end_headers()


def _not_modified(request: dict[str, str], etag: str | None, last_modified: str | None) -> bool:
    if_none_match = request.get("if-none-match")
    if if_none_match is not None:
        tags = {tag.strip() for tag in if_none_match.split(",")}
        return etag is not None and ("*" in tags or etag in tags)
    since = request.get("if-modified-since")
    if since is None or last_modified is None:
        return False
    try:
        return email.utils.parsedate_to_datetime(last_modified) <= email.utils.parsedate_to_datetime(since)
    except (TypeError, ValueError):
        return False


def _single_range(header: str, size: int) -> tuple[int, int] | str | None:
    """bytes=a-b | a- | -n; anything else (including multiple ranges) is ignored."""
    match = _RANGE.fullmatch(header.strip())
    if match is None or match[0] == "bytes=-":
        return None
    first, last = match[1], match[2]
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        if last and int(last) < start:
            return None
        return (start, end) if start < size else "unsatisfiable"
    suffix = int(last)
    return (max(size - suffix, 0), size - 1) if suffix and size else "unsatisfiable"


def _read_all(content: bytes | Path) -> bytes:
    return content.read_bytes() if isinstance(content, Path) else content


def _slices(body: bytes, start: int, length: int) -> Iterator[bytes]:
    view = memoryview(body)
    for offset in range(start, start + length, _CHUNK):
        yield bytes(view[offset : min(offset + _CHUNK, start + length)])


def _file_slices(path: Path, start: int, length: int) -> Iterator[bytes]:
    with path.open("rb") as file:
        file.seek(start)
        while length > 0 and (piece := file.read(min(_CHUNK, length))):
            length -= len(piece)
            yield piece
