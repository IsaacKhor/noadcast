"""Resumable downloads against a real local HTTP origin (and a few scripted
misbehaving servers via httpx.MockTransport)."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import random
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from noadcast.feeds.fetcher import USER_AGENT
from noadcast.media.downloader import CHUNK_BYTES, DownloadError, download_audio, part_path
from tests.support.httpfixture import HTTPFixture

ROOT = Path(__file__).resolve().parents[1]
TAL_EPISODE = ROOT / "benchmarks" / "tal" / "audio" / "01-646.mp3"  # untracked; present on the development host
AUDIO = random.Random(646).randbytes(3 * CHUNK_BYTES + 12345)  # spans several write chunks
SHA = hashlib.sha256(AUDIO).hexdigest()
STAMP = "Sun, 13 Sep 2026 20:00:00 GMT"


def scratch_dir(case: unittest.TestCase) -> Path:
    base = ROOT / ".cache" / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="test-downloader-", dir=base))
    case.addCleanup(shutil.rmtree, path, True)
    return path


class DownloaderTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.origin = self.enterContext(HTTPFixture())
        self.origin.serve("/ep.mp3", AUDIO, content_type="audio/mpeg", etag='"v1"', last_modified=STAMP)
        self.client = httpx.AsyncClient(follow_redirects=False)  # download_audio must follow redirects itself
        self.addAsyncCleanup(self.client.aclose)
        self.dest = scratch_dir(self) / "audio" / "3" / "42.mp3"  # parent does not exist yet
        self.part = part_path(self.dest)
        self.progress: list[tuple[int, int | None]] = []

    async def download(self, path: str = "/ep.mp3", **kwargs):
        kwargs.setdefault("on_progress", lambda done, total: self.progress.append((done, total)))
        return await download_audio(self.client, self.origin.url(path), self.dest, **kwargs)

    async def download_error(self, path: str = "/ep.mp3", **kwargs) -> DownloadError:
        with self.assertRaises(DownloadError) as caught:
            await self.download(path, **kwargs)
        return caught.exception

    def assert_complete(self, result, content: bytes = AUDIO) -> None:
        self.assertEqual(self.dest.read_bytes(), content)
        self.assertEqual((result.path, result.bytes), (self.dest, len(content)))
        self.assertEqual(result.sha256, hashlib.sha256(content).hexdigest())
        self.assertFalse(self.part.exists())

    def sent(self, path: str = "/ep.mp3") -> list[dict[str, str]]:
        return [request.headers for request in self.origin.requests_for(path)]


class CleanDownloadTests(DownloaderTestCase):
    async def test_download(self) -> None:
        result = await self.download()
        self.assert_complete(result)
        self.assertEqual((result.content_type, result.etag, result.last_modified), ("audio/mpeg", '"v1"', STAMP))
        self.assertEqual((result.resumed, result.final_url), (False, self.origin.url("/ep.mp3")))
        (headers,) = self.sent()
        self.assertEqual((headers["user-agent"], headers["accept-encoding"]), (USER_AGENT, "identity"))
        self.assertNotIn("range", headers)

    async def test_progress_is_reported_per_chunk(self) -> None:
        await self.download()
        done = [done for done, _ in self.progress]
        self.assertEqual(self.progress[-1], (len(AUDIO), len(AUDIO)))
        self.assertTrue(all(total == len(AUDIO) for _, total in self.progress))
        # Once per >= 1 MiB written, not once per network read.
        steps = [after - before for before, after in zip([0, *done], done)]
        self.assertTrue(all(step >= CHUNK_BYTES for step in steps[:-1]), steps)
        self.assertIn(len(steps), (3, 4))  # 3 MiB + 12 KB; 4 if reads align to exact MiBs

    async def test_existing_file_is_replaced(self) -> None:
        self.dest.parent.mkdir(parents=True)
        self.dest.write_bytes(b"old render")
        self.assert_complete(await self.download())

    async def test_redirects_are_followed(self) -> None:
        self.origin.redirect("/feed-link.mp3", "/tracker", status=302)
        self.origin.redirect("/tracker", self.origin.url("/ep.mp3"), status=307)
        result = await self.download("/feed-link.mp3")
        self.assert_complete(result)
        self.assertEqual(result.final_url, self.origin.url("/ep.mp3"))

    async def test_content_type_parameters_are_dropped(self) -> None:
        self.origin.serve("/ep.mp3", AUDIO, content_type="Audio/MPEG; charset=binary")
        self.assertEqual((await self.download()).content_type, "audio/mpeg")

    async def test_chunked_response_without_length(self) -> None:
        self.origin.serve("/ep.mp3", AUDIO, content_type="audio/mpeg", chunked=True)
        self.assert_complete(await self.download())
        self.assertTrue(all(total is None for _, total in self.progress))


class ResumeTests(DownloaderTestCase):
    async def drop_midway(self, at: int = 1_500_000) -> DownloadError:
        self.origin.drop_after("/ep.mp3", at)
        error = await self.download_error()
        self.assertFalse(error.permanent)
        self.assertEqual(self.part.read_bytes(), AUDIO[:at])  # every byte that arrived was kept
        self.assertFalse(self.dest.exists())
        return error

    async def test_resume_after_a_mid_stream_drop(self) -> None:
        error = await self.drop_midway()
        self.assertEqual((error.etag, error.last_modified), ('"v1"', STAMP))
        self.progress.clear()
        result = await self.download(etag=error.etag, last_modified=error.last_modified)
        self.assert_complete(result)  # identical bytes and sha256, prefix re-hashed
        self.assertEqual((result.resumed, result.etag), (True, '"v1"'))
        resumed = self.sent()[-1]
        self.assertEqual((resumed["range"], resumed["if-range"]), ("bytes=1500000-", '"v1"'))
        self.assertEqual(self.origin.requests_for("/ep.mp3")[-1].status, 206)
        self.assertGreater(self.progress[0][0], 1_500_000)
        self.assertEqual(self.progress[-1], (len(AUDIO), len(AUDIO)))

    async def test_resume_twice(self) -> None:
        first = await self.drop_midway(1_000_000)
        self.origin.drop_after("/ep.mp3", 1_000_000)
        second = await self.download_error(etag=first.etag, last_modified=first.last_modified)
        self.assertEqual(self.part.stat().st_size, 2_000_000)
        result = await self.download(etag=second.etag, last_modified=second.last_modified)
        self.assert_complete(result)
        self.assertEqual([h.get("range") for h in self.sent()], [None, "bytes=1000000-", "bytes=2000000-"])

    async def test_if_range_uses_last_modified_when_the_etag_is_weak(self) -> None:
        self.origin.serve("/ep.mp3", AUDIO, etag='W/"weak"', last_modified=STAMP)
        error = await self.drop_midway()
        result = await self.download(etag=error.etag, last_modified=error.last_modified)
        self.assert_complete(result)
        self.assertTrue(result.resumed)
        self.assertEqual(self.sent()[-1]["if-range"], STAMP)

    async def test_no_validator_means_no_resume(self) -> None:
        await self.drop_midway()
        result = await self.download()  # the caller lost the validators
        self.assert_complete(result)
        self.assertFalse(result.resumed)
        self.assertNotIn("range", self.sent()[-1])

    async def test_server_ignoring_range_restarts_from_zero(self) -> None:
        self.origin.serve("/ep.mp3", AUDIO, etag='"v1"', ranges=False)
        error = await self.drop_midway()
        result = await self.download(etag=error.etag)
        self.assert_complete(result)
        self.assertFalse(result.resumed)
        self.assertEqual(self.sent()[-1]["range"], "bytes=1500000-")
        self.assertEqual(self.origin.requests_for("/ep.mp3")[-1].status, 200)

    async def test_if_range_mismatch_after_the_file_changed(self) -> None:
        error = await self.drop_midway()
        rerendered = random.Random(7).randbytes(len(AUDIO) + 999)  # dynamic ad insertion
        self.origin.serve("/ep.mp3", rerendered, etag='"v2"', last_modified=STAMP)
        result = await self.download(etag=error.etag, last_modified=error.last_modified)
        self.assert_complete(result, rerendered)
        self.assertEqual((result.resumed, result.etag), (False, '"v2"'))
        self.assertEqual(self.origin.requests_for("/ep.mp3")[-1].status, 200)

    async def test_416_discards_the_part_and_retries_once_without_range(self) -> None:
        self.dest.parent.mkdir(parents=True)
        self.part.write_bytes(AUDIO + b"trailing junk")  # longer than the resource
        result = await self.download(etag='"v1"')
        self.assert_complete(result)
        self.assertFalse(result.resumed)
        log = self.origin.requests_for("/ep.mp3")
        self.assertEqual([(r.status, r.headers.get("range")) for r in log], [(416, f"bytes={len(AUDIO) + 13}-"), (200, None)])

    async def test_transient_status_keeps_the_part_and_its_validators(self) -> None:
        error = await self.drop_midway()
        self.origin.fail("/ep.mp3", 503, headers={"Retry-After": "30"})
        retry = await self.download_error(etag=error.etag, last_modified=error.last_modified)
        self.assertEqual((retry.status, retry.permanent, retry.retry_after), (503, False, 30.0))
        self.assertEqual((retry.etag, retry.last_modified), ('"v1"', STAMP))
        self.assertEqual(self.part.stat().st_size, 1_500_000)

    async def test_validators_are_reported_before_any_byte_is_written(self) -> None:
        seen: list[tuple[str | None, str | None, int]] = []

        def record(etag: str | None, last_modified: str | None) -> None:
            seen.append((etag, last_modified, self.part.stat().st_size))

        self.origin.drop_after("/ep.mp3", 1_500_000)
        await self.download_error(on_validators=record)
        self.assertEqual(seen, [('"v1"', STAMP, 0)])

    async def test_a_cancelled_transfer_resumes_from_the_hooks_validators(self) -> None:
        # A crash never raises a DownloadError carrying the validators; the
        # early report from on_validators is all that survives it.
        reported: list[tuple[str | None, str | None]] = []
        first_chunk = asyncio.Event()

        def progress(done: int, total: int | None) -> None:
            if done >= CHUNK_BYTES:
                first_chunk.set()

        task = asyncio.create_task(
            self.download(on_progress=progress, on_validators=lambda etag, lm: reported.append((etag, lm)))
        )
        await first_chunk.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        kept = self.part.stat().st_size
        self.assertTrue(CHUNK_BYTES <= kept < len(AUDIO))
        self.assertEqual(self.part.read_bytes(), AUDIO[:kept])
        ((etag, last_modified),) = reported
        result = await self.download(etag=etag, last_modified=last_modified)
        self.assert_complete(result)
        self.assertTrue(result.resumed)
        self.assertEqual(self.sent()[-1]["range"], f"bytes={kept}-")


class ErrorTests(DownloaderTestCase):
    async def test_404_is_permanent_and_removes_the_part(self) -> None:
        self.dest.parent.mkdir(parents=True)
        self.part.write_bytes(AUDIO[:1000])
        error = await self.download_error("/gone.mp3", etag='"v1"')
        self.assertEqual((error.status, error.permanent, error.etag), (404, True, None))
        self.assertFalse(self.part.exists())

    async def test_permanent_statuses(self) -> None:
        for status in (401, 403, 410):
            with self.subTest(status=status):
                self.origin.fail("/ep.mp3", status)
                error = await self.download_error()
                self.assertEqual((error.status, error.permanent), (status, True))

    async def test_429_carries_retry_after(self) -> None:
        self.origin.fail("/ep.mp3", 429, headers={"Retry-After": "120"})
        error = await self.download_error()
        self.assertEqual((error.status, error.permanent, error.retry_after), (429, False, 120.0))

    async def test_5xx_is_transient(self) -> None:
        self.origin.fail("/ep.mp3", 502)
        error = await self.download_error()
        self.assertEqual((error.status, error.permanent), (502, False))

    async def test_max_bytes_from_content_length(self) -> None:
        error = await self.download_error(max_bytes=len(AUDIO) - 1)
        self.assertTrue(error.permanent)
        self.assertFalse(self.part.exists())
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.progress, [])  # refused before writing anything

    async def test_max_bytes_while_streaming(self) -> None:
        self.origin.serve("/ep.mp3", AUDIO, chunked=True)
        error = await self.download_error(max_bytes=CHUNK_BYTES + 1)
        self.assertTrue(error.permanent)
        self.assertFalse(self.part.exists())

    async def test_max_bytes_exactly(self) -> None:
        self.assert_complete(await self.download(max_bytes=len(AUDIO)))

    async def test_stall_is_transient(self) -> None:
        self.origin.delay("/ep.mp3", 5)
        started = time.monotonic()
        error = await self.download_error(timeout=0.3)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(error.permanent)

    async def test_empty_body_is_transient(self) -> None:
        self.origin.serve("/ep.mp3", b"")
        error = await self.download_error()
        self.assertFalse(error.permanent)
        self.assertFalse(self.dest.exists())

    async def test_unusable_url_is_permanent(self) -> None:
        with self.assertRaises(DownloadError) as caught:
            await download_audio(self.client, "ftp://example.com/a.mp3", self.dest)
        self.assertTrue(caught.exception.permanent)


class FileBackedOriginTests(DownloaderTestCase):
    """The origin serving files from disk, as the end-to-end test does."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.source = scratch_dir(self) / "source.mp3"
        self.source.write_bytes(AUDIO)
        self.origin.serve("/file.mp3", self.source, content_type="audio/mpeg")  # validators from stat()

    async def test_resume(self) -> None:
        self.origin.drop_after("/file.mp3", 2_000_000)
        error = await self.download_error("/file.mp3")
        self.assertIsNotNone(error.etag)
        result = await self.download("/file.mp3", etag=error.etag, last_modified=error.last_modified)
        self.assert_complete(result)
        self.assertTrue(result.resumed)

    async def test_rewritten_file_is_downloaded_afresh(self) -> None:
        self.origin.drop_after("/file.mp3", 2_000_000)
        error = await self.download_error("/file.mp3")
        rerendered = AUDIO[::-1] + b"extra ad"
        self.source.write_bytes(rerendered)
        result = await self.download("/file.mp3", etag=error.etag)
        self.assert_complete(result, rerendered)
        self.assertFalse(result.resumed)


@unittest.skipUnless(TAL_EPISODE.exists(), "benchmarks/tal/audio is not downloaded")
class TalEpisodeTests(DownloaderTestCase):
    async def test_resumed_download_matches_the_manifest(self) -> None:
        expected = json.loads((ROOT / "benchmarks" / "tal" / "manifest.json").read_text())["episodes"][0]
        self.origin.serve("/646.mp3", TAL_EPISODE, content_type="audio/mpeg")
        self.origin.drop_after("/646.mp3", 20_000_000)
        error = await self.download_error("/646.mp3")
        result = await self.download("/646.mp3", etag=error.etag, last_modified=error.last_modified)
        self.assertEqual((result.bytes, result.sha256, result.resumed), (expected["bytes"], expected["sha256"], True))


class MisbehavingServerTests(unittest.IsolatedAsyncioTestCase):
    """Responses a correct origin never sends, scripted with MockTransport."""

    async def asyncSetUp(self) -> None:
        self.dest = scratch_dir(self) / "42.mp3"
        self.part = part_path(self.dest)
        self.requests: list[httpx.Request] = []

    async def run_download(self, respond, **kwargs):
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return respond(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await download_audio(client, "https://cdn.example.com/42.mp3", self.dest, **kwargs)

    async def test_206_at_the_wrong_offset_restarts_without_range(self) -> None:
        self.part.write_bytes(AUDIO[:1000])

        def respond(request: httpx.Request) -> httpx.Response:
            if "range" in request.headers:  # claims to resume but starts over
                return httpx.Response(206, headers={"content-range": f"bytes 0-{len(AUDIO) - 1}/{len(AUDIO)}"}, content=AUDIO)
            return httpx.Response(200, content=AUDIO)

        result = await self.run_download(respond, etag='"v1"')
        self.assertEqual((self.dest.read_bytes(), result.sha256, result.resumed), (AUDIO, SHA, False))
        self.assertEqual([r.headers.get("range") for r in self.requests], ["bytes=1000-", None])

    async def test_206_with_unknown_length_restarts(self) -> None:
        self.part.write_bytes(AUDIO[:1000])

        def respond(request: httpx.Request) -> httpx.Response:
            if "range" in request.headers:
                return httpx.Response(206, headers={"content-range": f"bytes 1000-{len(AUDIO) - 1}/*"}, content=AUDIO[1000:])
            return httpx.Response(200, content=AUDIO)

        self.assertFalse((await self.run_download(respond, etag='"v1"')).resumed)
        self.assertEqual(self.dest.read_bytes(), AUDIO)

    async def test_short_206_keeps_the_part_for_the_next_attempt(self) -> None:
        self.part.write_bytes(AUDIO[:1000])

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(206, headers={"content-range": f"bytes 1000-1999/{len(AUDIO)}", "etag": '"v1"'}, content=AUDIO[1000:2000])

        with self.assertRaises(DownloadError) as caught:
            await self.run_download(respond, etag='"v1"')
        self.assertFalse(caught.exception.permanent)
        self.assertEqual(caught.exception.etag, '"v1"')
        self.assertEqual(self.part.read_bytes(), AUDIO[:2000])

    async def test_206_to_a_request_without_range_is_an_error(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(206, headers={"content-range": f"bytes 0-99/{len(AUDIO)}"}, content=AUDIO[:100])

        with self.assertRaises(DownloadError) as caught:
            await self.run_download(respond)
        self.assertFalse(caught.exception.permanent)
        self.assertFalse(self.dest.exists())

    async def test_gzip_despite_identity_is_decoded(self) -> None:
        packed = gzip.compress(AUDIO)

        def respond(request: httpx.Request) -> httpx.Response:
            headers = {"content-encoding": "gzip", "content-length": str(len(packed))}
            return httpx.Response(200, headers=headers, content=packed)

        result = await self.run_download(respond)
        self.assertEqual((self.dest.read_bytes(), result.sha256, result.bytes), (AUDIO, SHA, len(AUDIO)))


if __name__ == "__main__":
    unittest.main()
