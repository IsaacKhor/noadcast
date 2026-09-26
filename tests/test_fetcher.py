"""Conditional feed fetching against a real local HTTP origin."""

from __future__ import annotations

import datetime as dt
import email.utils
import gzip
import socket
import time
import unittest

import httpx

from noadcast import __version__
from noadcast.feeds.fetcher import (
    USER_AGENT,
    FeedFetchError,
    fetch_feed,
    is_permanent_status,
    parse_retry_after,
)
from tests.support.httpfixture import HTTPFixture

FEED = b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>' * 50


class FetcherTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.origin = self.enterContext(HTTPFixture())
        # Like the server's shared client: its own UA and redirect following on,
        # both of which fetch_feed must override per request.
        self.client = httpx.AsyncClient(headers={"User-Agent": "shared-client"}, follow_redirects=True)
        self.addAsyncCleanup(self.client.aclose)

    async def fetch(self, path: str, **kwargs):
        return await fetch_feed(self.client, self.origin.url(path), **kwargs)

    async def fetch_error(self, path: str, **kwargs) -> FeedFetchError:
        with self.assertRaises(FeedFetchError) as caught:
            await self.fetch(path, **kwargs)
        return caught.exception


class SuccessTests(FetcherTestCase):
    async def test_plain_fetch_returns_body_and_validators(self) -> None:
        self.origin.serve("/feed.xml", FEED, content_type="application/rss+xml", etag='"v1"', last_modified="Tue, 22 Sep 2026 18:00:00 GMT")
        fetch = await self.fetch("/feed.xml")
        self.assertEqual((fetch.status, fetch.body), (200, FEED))
        self.assertEqual((fetch.etag, fetch.last_modified), ('"v1"', "Tue, 22 Sep 2026 18:00:00 GMT"))
        self.assertEqual((fetch.final_url, fetch.permanent_redirect), (self.origin.url("/feed.xml"), None))
        sent = self.origin.requests_for("/feed.xml")[0].headers
        self.assertEqual(sent["user-agent"], f"Noadcast/{__version__} (+self-hosted)")
        self.assertEqual(sent["user-agent"], USER_AGENT)
        self.assertNotIn("if-none-match", sent)
        self.assertNotIn("if-modified-since", sent)
        self.assertIn("gzip", sent["accept-encoding"])

    async def test_conditional_get_answers_304(self) -> None:
        self.origin.serve("/feed.xml", FEED, etag='"v1"', last_modified="Tue, 22 Sep 2026 18:00:00 GMT")
        fetch = await self.fetch("/feed.xml", etag='"v1"', last_modified="Tue, 22 Sep 2026 18:00:00 GMT")
        self.assertEqual((fetch.status, fetch.body, fetch.etag), (304, None, '"v1"'))
        sent = self.origin.requests_for("/feed.xml")[0].headers
        self.assertEqual((sent["if-none-match"], sent["if-modified-since"]), ('"v1"', "Tue, 22 Sep 2026 18:00:00 GMT"))

    async def test_if_modified_since_alone(self) -> None:
        stamp = "Tue, 22 Sep 2026 18:00:00 GMT"
        self.origin.serve("/feed.xml", FEED, etag=None, last_modified=stamp)
        fetch = await self.fetch("/feed.xml", last_modified=stamp)
        self.assertEqual((fetch.status, fetch.etag, fetch.last_modified), (304, None, stamp))

    async def test_bare_304_keeps_the_validators_sent(self) -> None:
        self.origin.fail("/feed.xml", 304)  # a 304 that re-sends no validators
        fetch = await self.fetch("/feed.xml", etag='"old"', last_modified="Wed, 23 Sep 2026 00:00:00 GMT")
        self.assertEqual((fetch.status, fetch.etag, fetch.last_modified), (304, '"old"', "Wed, 23 Sep 2026 00:00:00 GMT"))

    async def test_changed_feed_answers_200_with_new_validators(self) -> None:
        self.origin.serve("/feed.xml", FEED, etag='"v2"', last_modified=None)
        fetch = await self.fetch("/feed.xml", etag='"v1"')
        self.assertEqual((fetch.status, fetch.body, fetch.etag, fetch.last_modified), (200, FEED, '"v2"', None))

    async def test_gzip_is_decoded(self) -> None:
        self.origin.serve("/feed.xml", FEED, gzip=True)
        fetch = await self.fetch("/feed.xml")
        self.assertEqual(fetch.body, FEED)
        self.assertLess(self.origin.requests_for("/feed.xml")[0].body_bytes, len(FEED) // 10)  # it did travel compressed


class RedirectTests(FetcherTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.origin.serve("/new.xml", FEED)

    async def test_permanent_redirect_is_reported(self) -> None:
        for status in (301, 308):
            with self.subTest(status=status):
                self.origin.redirect("/old.xml", "/new.xml", status=status)
                fetch = await self.fetch("/old.xml")
                self.assertEqual((fetch.body, fetch.final_url), (FEED, self.origin.url("/new.xml")))
                self.assertEqual(fetch.permanent_redirect, self.origin.url("/new.xml"))

    async def test_temporary_redirect_is_followed_but_not_reported(self) -> None:
        for status in (302, 303, 307):
            with self.subTest(status=status):
                self.origin.redirect("/old.xml", self.origin.url("/new.xml"), status=status)
                fetch = await self.fetch("/old.xml")
                self.assertEqual((fetch.final_url, fetch.permanent_redirect), (self.origin.url("/new.xml"), None))

    async def test_permanent_target_is_the_end_of_the_unbroken_permanent_run(self) -> None:
        self.origin.redirect("/a", "/b", status=301)
        self.origin.redirect("/b", "/c", status=302)
        self.origin.redirect("/c", "/new.xml", status=301)
        fetch = await self.fetch("/a")
        self.assertEqual((fetch.final_url, fetch.permanent_redirect), (self.origin.url("/new.xml"), self.origin.url("/b")))

    async def test_temporary_first_hop_means_no_move(self) -> None:
        self.origin.redirect("/a", "/b", status=302)
        self.origin.redirect("/b", "/new.xml", status=301)
        self.assertIsNone((await self.fetch("/a")).permanent_redirect)

    async def test_conditional_headers_follow_the_redirect(self) -> None:
        self.origin.serve("/new.xml", FEED, etag='"v1"')
        self.origin.redirect("/old.xml", "/new.xml", status=301)
        fetch = await self.fetch("/old.xml", etag='"v1"')
        self.assertEqual((fetch.status, fetch.permanent_redirect), (304, self.origin.url("/new.xml")))

    async def test_at_most_five_redirects(self) -> None:
        for hop in range(5):
            self.origin.redirect(f"/r{hop}", f"/r{hop + 1}", status=302)
        self.origin.redirect("/r5", "/new.xml", status=302)
        self.assertEqual((await self.fetch("/r1")).body, FEED)  # five hops
        error = await self.fetch_error("/r0")  # six
        self.assertFalse(error.permanent)
        self.assertIn("redirects", str(error))

    async def test_redirect_loop(self) -> None:
        self.origin.redirect("/a", "/b")
        self.origin.redirect("/b", "/a")
        self.assertIn("redirects", str(await self.fetch_error("/a")))

    async def test_redirect_to_an_unsupported_scheme_is_permanent(self) -> None:
        self.origin.redirect("/a", "ftp://example.com/feed.xml", status=301)
        self.assertTrue((await self.fetch_error("/a")).permanent)


class ErrorTests(FetcherTestCase):
    async def test_permanent_statuses(self) -> None:
        for status in (400, 401, 403, 404, 410, 451):
            with self.subTest(status=status):
                self.origin.fail("/feed.xml", status)
                error = await self.fetch_error("/feed.xml")
                self.assertEqual((error.status, error.permanent), (status, True))

    async def test_transient_statuses(self) -> None:
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.origin.fail("/feed.xml", status)
                error = await self.fetch_error("/feed.xml")
                self.assertEqual((error.status, error.permanent, error.retry_after), (status, False, None))

    async def test_retry_after_seconds(self) -> None:
        self.origin.fail("/feed.xml", 429, headers={"Retry-After": "120"})
        error = await self.fetch_error("/feed.xml")
        self.assertEqual((error.status, error.permanent, error.retry_after), (429, False, 120.0))

    async def test_retry_after_http_date(self) -> None:
        when = email.utils.formatdate(time.time() + 3600, usegmt=True)
        self.origin.fail("/feed.xml", 503, headers={"Retry-After": when})
        error = await self.fetch_error("/feed.xml")
        self.assertAlmostEqual(error.retry_after, 3600, delta=5)

    async def test_the_origin_recovers(self) -> None:
        self.origin.serve("/feed.xml", FEED)
        self.origin.fail("/feed.xml", 503, times=2)
        for _ in range(2):
            await self.fetch_error("/feed.xml")
        self.assertEqual((await self.fetch("/feed.xml")).body, FEED)

    async def test_body_over_the_cap_is_permanent(self) -> None:
        self.origin.serve("/big.xml", FEED)
        error = await self.fetch_error("/big.xml", max_bytes=len(FEED) - 1)
        self.assertTrue(error.permanent)
        self.origin.serve("/exact.xml", FEED)
        self.assertEqual((await self.fetch("/exact.xml", max_bytes=len(FEED))).body, FEED)

    async def test_cap_applies_while_streaming_without_content_length(self) -> None:
        self.origin.serve("/stream.xml", FEED * 20, chunked=True)
        error = await self.fetch_error("/stream.xml", max_bytes=len(FEED))
        self.assertTrue(error.permanent)
        self.assertIn("limit", str(error))

    async def test_cap_applies_to_decoded_bytes(self) -> None:
        bomb = b" " * (4 * 1024 * 1024)  # compresses ~1000x
        self.origin.serve("/bomb.xml", bomb, gzip=True)
        error = await self.fetch_error("/bomb.xml", max_bytes=1024 * 1024)
        self.assertTrue(error.permanent)
        self.assertLess(len(gzip.compress(bomb)), 1024 * 1024)

    async def test_timeout_is_transient(self) -> None:
        self.origin.serve("/slow.xml", FEED)
        self.origin.delay("/slow.xml", 5)
        started = time.monotonic()
        error = await self.fetch_error("/slow.xml", timeout=0.3)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(error.permanent)
        self.assertIn("timed out", str(error))

    async def test_connection_refused_is_transient(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.assertRaises(FeedFetchError) as caught:
            await fetch_feed(self.client, f"http://127.0.0.1:{port}/feed.xml")
        self.assertFalse(caught.exception.permanent)

    async def test_dropped_connection_is_transient(self) -> None:
        self.origin.serve("/feed.xml", FEED)
        self.origin.drop_after("/feed.xml", 100)
        self.assertFalse((await self.fetch_error("/feed.xml")).permanent)

    async def test_unusable_url_is_permanent(self) -> None:
        for url in ("ftp://example.com/feed.xml", "http://exa mple.com:bad/feed"):
            with self.subTest(url=url), self.assertRaises(FeedFetchError) as caught:
                await fetch_feed(self.client, url)
            self.assertTrue(caught.exception.permanent)


class HelperTests(unittest.TestCase):
    def test_parse_retry_after(self) -> None:
        now = dt.datetime(2026, 9, 22, 18, 0, tzinfo=dt.UTC)
        self.assertEqual(parse_retry_after("120", now=now), 120.0)
        self.assertEqual(parse_retry_after(" 0 ", now=now), 0.0)
        self.assertEqual(parse_retry_after("Tue, 22 Sep 2026 18:02:00 GMT", now=now), 120.0)
        self.assertEqual(parse_retry_after("Tue, 22 Sep 2026 17:00:00 GMT", now=now), 0.0)  # already passed
        for value in (None, "", "soon", "-5", "1.5", "9" * 40):
            with self.subTest(value=value):
                self.assertIsNone(parse_retry_after(value, now=now))

    def test_is_permanent_status(self) -> None:
        self.assertEqual(
            [status for status in (400, 401, 403, 404, 405, 408, 410, 425, 429, 451, 500, 503) if is_permanent_status(status)],
            [400, 401, 403, 404, 405, 410, 451],
        )


if __name__ == "__main__":
    unittest.main()
