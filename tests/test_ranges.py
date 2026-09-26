"""HTTP Range serving: RFC 9110 vectors against a tiny Starlette app, then the
same response under GZipMiddleware and under a real uvicorn server."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.routing import Route

from noadcast.media.ranges import (
    CACHE_CONTROL,
    CHUNK_BYTES,
    UNSATISFIABLE,
    audio_file_response,
    content_disposition,
    http_date,
    parse_range,
    strong_etag,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = random.Random(9110).randbytes(4 * CHUNK_BYTES + 123)  # several read chunks, ragged tail
SIZE = len(DATA)
SHA = hashlib.sha256(DATA).hexdigest()
ETAG = f'"{SHA}"'
MODIFIED = dt.datetime(2026, 9, 13, 20, 0, 0, tzinfo=dt.UTC)
MODIFIED_HTTP = "Sun, 13 Sep 2026 20:00:00 GMT"
FILENAME = "646 The Secret of My Death.mp3"


def scratch_dir(case: unittest.TestCase) -> Path:
    base = ROOT / ".cache" / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="test-ranges-", dir=base))
    case.addCleanup(shutil.rmtree, path, True)
    return path


def audio_app(path: Path, *, size: int = SIZE, filename: str = FILENAME, middleware: list[Middleware] | None = None):
    async def audio(request: Request):
        return audio_file_response(
            request, path, size=size, content_type="audio/mpeg", etag=SHA, last_modified=MODIFIED, filename=filename
        )

    return Starlette(routes=[Route("/audio", audio, methods=["GET", "HEAD"])], middleware=middleware)


class ParseRangeTests(unittest.TestCase):
    def test_vectors(self) -> None:
        cases = {
            None: None,
            "bytes=0-": (0, 999),
            "bytes=-500": (500, 999),
            "bytes=500-999": (500, 999),
            "bytes=0-0": (0, 0),
            "bytes=999-": (999, 999),
            "bytes=0-5000": (0, 999),  # end clamped
            "bytes=-5000": (0, 999),  # suffix longer than the file
            "bytes=1000-": UNSATISFIABLE,  # starts past EOF
            "bytes=1000-2000": UNSATISFIABLE,
            "bytes=-0": UNSATISFIABLE,
            "bytes=-000": UNSATISFIABLE,
            "Bytes=0-9": (0, 9),  # unit is case-insensitive
            " bytes = 0-9 ": (0, 9),
            "bytes=0009-0010": (9, 10),
            "bytes=0-9,": (0, 9),  # empty list elements are legal
            "bytes=abc": None,
            "bytes=5-3": None,  # invalid int-range: header ignored
            "bytes=-": None,
            "bytes=": None,
            "bytes 0-5": None,
            "items=0-5": None,
            "bytes=+5-": None,
            "bytes=--5": None,
            "bytes=٣-": None,  # non-ASCII digits
            "bytes=0-1,5-6": None,  # multi-range: full body
            "bytes=0-1,0-1": None,
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(parse_range(header, 1000), expected)

    def test_hostile_numbers(self) -> None:
        huge = "9" * 5000  # past int()'s default digit limit
        self.assertEqual(parse_range(f"bytes={huge}-", 1000), UNSATISFIABLE)
        self.assertEqual(parse_range(f"bytes=0-{huge}", 1000), (0, 999))
        self.assertEqual(parse_range(f"bytes=-{huge}", 1000), (0, 999))
        self.assertIsNone(parse_range(f"bytes={huge}-5", 1000))  # last < first

    def test_empty_file(self) -> None:
        self.assertEqual(parse_range("bytes=0-", 0), UNSATISFIABLE)
        self.assertEqual(parse_range("bytes=-5", 0), UNSATISFIABLE)
        self.assertIsNone(parse_range(None, 0))


class HeaderHelperTests(unittest.TestCase):
    def test_strong_etag(self) -> None:
        self.assertEqual(strong_etag(SHA), ETAG)
        self.assertEqual(strong_etag(f" {ETAG} "), ETAG)
        for bad in ('W/"abc"', 'a"b', "a b", "ta\tb", '"a\x7fb"'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                strong_etag(bad)

    def test_http_date(self) -> None:
        self.assertEqual(http_date(MODIFIED), MODIFIED_HTTP)
        self.assertEqual(http_date(MODIFIED.astimezone(dt.timezone(dt.timedelta(hours=-4)))), MODIFIED_HTTP)
        self.assertEqual(http_date(MODIFIED.replace(tzinfo=None)), MODIFIED_HTTP)

    def test_content_disposition(self) -> None:
        self.assertEqual(content_disposition("ep.mp3"), 'inline; filename="ep.mp3"')
        self.assertEqual(
            content_disposition("Épisode «646».mp3"),
            "inline; filename=\"_pisode _646_.mp3\"; filename*=UTF-8''%C3%89pisode%20%C2%AB646%C2%BB.mp3",
        )
        self.assertEqual(content_disposition('a"b\\c/d\r\nSet-Cookie: x.mp3'), 'inline; filename="a_b_c_d__Set-Cookie: x.mp3"')
        self.assertEqual(content_disposition(" \x00 "), 'inline; filename="_"')
        self.assertEqual(content_disposition(""), 'inline; filename="audio"')
        self.assertEqual(content_disposition("x.mp3", disposition="attachment"), 'attachment; filename="x.mp3"')

    def test_rejects_types_that_compression_would_touch(self) -> None:
        request = Request({"type": "http", "method": "GET", "headers": [], "path": "/", "query_string": b""})
        with self.assertRaises(ValueError):
            audio_file_response(
                request, Path("x"), size=1, content_type="application/octet-stream", etag=SHA, last_modified=None, filename="x"
            )


class RangeResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.path = scratch_dir(self) / "42.mp3"
        self.path.write_bytes(DATA)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(audio_app(self.path)), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def get(self, method: str = "GET", **headers: str) -> httpx.Response:
        return await self.client.request(method, "/audio", headers={k.replace("_", "-"): v for k, v in headers.items()})

    async def assert_partial(self, response: httpx.Response, start: int, end: int) -> None:
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], f"bytes {start}-{end}/{SIZE}")
        self.assertEqual(response.headers["content-length"], str(end - start + 1))
        self.assertEqual(response.content, DATA[start : end + 1])

    async def assert_full(self, response: httpx.Response) -> None:
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("content-range", response.headers)
        self.assertEqual(response.headers["content-length"], str(SIZE))
        self.assertEqual(response.content, DATA)

    async def test_full_body_and_headers(self) -> None:
        response = await self.get()
        await self.assert_full(response)
        headers = response.headers
        self.assertEqual(headers["accept-ranges"], "bytes")
        self.assertEqual(headers["etag"], ETAG)
        self.assertEqual(headers["last-modified"], MODIFIED_HTTP)
        self.assertEqual(headers["content-type"], "audio/mpeg")
        self.assertEqual(headers["content-disposition"], f'inline; filename="{FILENAME}"')
        self.assertEqual(headers["cache-control"], CACHE_CONTROL)
        self.assertNotIn("content-encoding", headers)

    async def test_satisfiable_ranges(self) -> None:
        cases = {
            "bytes=0-": (0, SIZE - 1),
            "bytes=-500": (SIZE - 500, SIZE - 1),
            "bytes=500-999": (500, 999),
            "bytes=0-0": (0, 0),
            f"bytes={SIZE - 1}-": (SIZE - 1, SIZE - 1),
            "bytes=0-999999999": (0, SIZE - 1),
            f"bytes={CHUNK_BYTES - 7}-{3 * CHUNK_BYTES + 7}": (CHUNK_BYTES - 7, 3 * CHUNK_BYTES + 7),  # crosses reads
        }
        for header, (start, end) in cases.items():
            with self.subTest(range=header):
                response = await self.get(range=header)
                await self.assert_partial(response, start, end)
                self.assertEqual(response.headers["etag"], ETAG)
                self.assertEqual(response.headers["accept-ranges"], "bytes")

    async def test_unsatisfiable(self) -> None:
        for header in (f"bytes={SIZE}-", f"bytes={SIZE + 10}-{SIZE + 20}", "bytes=-0"):
            with self.subTest(range=header):
                response = await self.get(range=header)
                self.assertEqual(response.status_code, 416)
                self.assertEqual(response.headers["content-range"], f"bytes */{SIZE}")
                self.assertEqual((response.content, response.headers["content-length"]), (b"", "0"))
                self.assertEqual(response.headers["accept-ranges"], "bytes")
                self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_malformed_and_multi_range_get_the_full_body(self) -> None:
        for header in ("bytes=abc", "bytes=5-3", "items=0-5", "bytes 0-5", "bytes=--1", "bytes=0-1,5-6", "bytes=-1,0-0"):
            with self.subTest(range=header):
                await self.assert_full(await self.get(range=header))

    async def test_if_range(self) -> None:
        hits = (ETAG, MODIFIED_HTTP, "Sunday, 13-Sep-26 20:00:00 GMT")  # an obsolete but equal date
        misses = (
            '"stale-sha"',
            f"W/{ETAG}",  # weak tags never match strongly
            SHA,  # unquoted is not an entity tag, nor a date
            "Sun, 13 Sep 2026 19:59:59 GMT",
            "garbage",
        )
        for value in hits:
            with self.subTest(hit=value):
                await self.assert_partial(await self.get(range="bytes=100-199", if_range=value), 100, 199)
        for value in misses:
            with self.subTest(miss=value):
                await self.assert_full(await self.get(range="bytes=100-199", if_range=value))

    async def test_if_none_match(self) -> None:
        for value in (ETAG, f"W/{ETAG}", f'"other", {ETAG}', "*"):
            with self.subTest(hit=value):
                response = await self.get(if_none_match=value)
                self.assertEqual((response.status_code, response.content), (304, b""))
                self.assertEqual(response.headers["etag"], ETAG)
                self.assertEqual(response.headers["cache-control"], CACHE_CONTROL)
                self.assertNotIn("content-length", response.headers)
                self.assertNotIn("content-type", response.headers)
        await self.assert_full(await self.get(if_none_match='"other"'))
        await self.assert_full(await self.get(if_none_match=SHA))  # unquoted: not an entity tag

    async def test_if_none_match_beats_range(self) -> None:
        self.assertEqual((await self.get(if_none_match=ETAG, range="bytes=0-9")).status_code, 304)

    async def test_head_mirrors_get(self) -> None:
        get = await self.get()
        head = await self.get("HEAD")
        self.assertEqual(head.status_code, 200)
        self.assertEqual(dict(head.headers), dict(get.headers))
        self.assertEqual(head.headers["content-length"], str(SIZE))
        self.assertEqual(head.content, b"")

    async def test_head_ignores_range(self) -> None:
        # RFC 9110 §14.2: range handling is defined only for GET.
        head = await self.get("HEAD", range="bytes=0-1", if_range='"stale"')
        self.assertEqual((head.status_code, head.headers["content-length"]), (200, str(SIZE)))
        self.assertNotIn("content-range", head.headers)

    async def test_head_honours_if_none_match(self) -> None:
        self.assertEqual((await self.get("HEAD", if_none_match=ETAG)).status_code, 304)

    async def test_head_sends_no_body_bytes(self) -> None:
        # httpx discards HEAD bodies, so look at the raw ASGI messages instead.
        messages: list[dict] = []

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            messages.append(message)

        scope = {"type": "http", "method": "HEAD", "path": "/audio", "headers": [], "query_string": b"", "http_version": "1.1"}
        await audio_app(self.path)(scope, receive, send)
        start, *bodies = messages
        self.assertIn((b"content-length", str(SIZE).encode()), start["headers"])
        self.assertEqual([message.get("body", b"") for message in bodies], [b""])

    async def test_missing_file_raises_in_the_handler(self) -> None:
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            await self.get()  # so the router can answer 409 instead of a broken stream
        self.assertEqual((await self.get("HEAD")).status_code, 200)  # HEAD reads no file

    async def test_empty_file(self) -> None:
        empty = self.path.with_name("empty.mp3")
        empty.write_bytes(b"")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(audio_app(empty, size=0)), base_url="http://t") as client:
            full = await client.get("/audio")
            self.assertEqual((full.status_code, full.content, full.headers["content-length"]), (200, b"", "0"))
            ranged = await client.get("/audio", headers={"range": "bytes=0-"})
            self.assertEqual((ranged.status_code, ranged.headers["content-range"]), (416, "bytes */0"))

    async def test_non_ascii_filename(self) -> None:
        app = audio_app(self.path, filename="Café «Noir».mp3")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
            header = (await client.head("/audio")).headers["content-disposition"]
        self.assertEqual(header, "inline; filename=\"Caf_ _Noir_.mp3\"; filename*=UTF-8''Caf%C3%A9%20%C2%ABNoir%C2%BB.mp3")


class CompressionTests(unittest.IsolatedAsyncioTestCase):
    """The guarantee the app relies on: Starlette's GZipMiddleware, with its
    default exclusions, never encodes these responses."""

    async def test_gzip_middleware_leaves_audio_alone(self) -> None:
        path = scratch_dir(self) / "42.mp3"
        path.write_bytes(DATA)
        app = audio_app(path, middleware=[Middleware(GZipMiddleware, minimum_size=1)])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
            gzip_ok = {"accept-encoding": "gzip"}
            full = await client.get("/audio", headers=gzip_ok)
            ranged = await client.get("/audio", headers={**gzip_ok, "range": "bytes=10-19"})
            head = await client.head("/audio", headers=gzip_ok)
        for response in (full, ranged, head):
            self.assertNotIn("content-encoding", response.headers)
        self.assertEqual((full.content, full.headers["content-length"]), (DATA, str(SIZE)))
        self.assertEqual(ranged.content, DATA[10:20])
        self.assertEqual(head.headers["content-length"], str(SIZE))


class UvicornTests(unittest.IsolatedAsyncioTestCase):
    """Behaviour that only a real server shows: HEAD framing and disconnects."""

    async def asyncSetUp(self) -> None:
        self.dir = scratch_dir(self)
        self.path = self.dir / "42.mp3"
        self.path.write_bytes(DATA)

    async def serve(self, app, http: str) -> str:
        config = uvicorn.Config(app, host="127.0.0.1", port=0, http=http, lifespan="off", log_level="warning", access_log=False)
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())

        async def stop() -> None:
            server.should_exit = True
            await task

        self.addAsyncCleanup(stop)
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/audio"

    async def test_head_and_ranges_over_the_wire(self) -> None:
        for http in ("h11", "httptools"):
            with self.subTest(http=http):
                url = await self.serve(audio_app(self.path), http)
                async with httpx.AsyncClient() as client:
                    head = await client.head(url)
                    self.assertEqual((head.status_code, head.headers["content-length"], head.content), (200, str(SIZE), b""))
                    # The connection stays usable after HEAD: framing was right.
                    ranged = await client.get(url, headers={"range": "bytes=-300"})
                    self.assertEqual((ranged.status_code, ranged.content), (206, DATA[-300:]))
                    full = await client.get(url)
                    self.assertEqual(hashlib.sha256(full.content).hexdigest(), SHA)

    async def test_streaming_stops_when_the_client_goes_away(self) -> None:
        big = self.dir / "big.mp3"
        size = 256 * 1024 * 1024
        with big.open("wb") as file:
            file.truncate(size)  # sparse: no disk used
        finished = asyncio.Event()
        inner = audio_app(big, size=size)

        async def app(scope, receive, send):
            try:
                await inner(scope, receive, send)
            finally:
                finished.set()

        read = 0
        real_pread = os.pread

        def counting_pread(fd: int, length: int, offset: int) -> bytes:
            nonlocal read
            chunk = real_pread(fd, length, offset)
            read += len(chunk)
            return chunk

        with mock.patch("os.pread", counting_pread):
            url = await self.serve(app, "httptools")
            async with httpx.AsyncClient() as client:
                async with client.stream("GET", url) as response:
                    self.assertEqual(response.status_code, 200)
                    async for _ in response.aiter_raw():
                        break  # leaving the block closes the unread connection
            await asyncio.wait_for(finished.wait(), 10)
        # Flow control bounds what was read before the hang-up; without
        # disconnect detection the loop would read all 256 MiB into the void.
        self.assertLess(read, 64 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
