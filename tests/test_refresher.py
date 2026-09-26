"""Feed refresh and admission: first-subscribe backfill, later admissions
(new GUID and newer than the watermark or recent, capped), zero sync traffic
for unchanged feeds, per-feed backoff that never gives up, and the inline
subscribe outcomes behind POST /podcasts."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import unittest
from unittest import mock

from noadcast.db import repo
from noadcast.feeds import refresher
from noadcast.feeds.fetcher import FeedFetch, FeedFetchError
from noadcast.feeds.parser import FeedParseError, ParsedEpisode, ParsedFeed
from noadcast.pipeline import jobs
from noadcast.timeutil import iso, now_iso, parse_iso, utc_now

from tests.pipeline_support import close_context, make_context, make_settings, temp_dir

FEED_URL = "https://feeds.example.com/show.xml"


def episode(guid: str, days_ago: float | None, position: int, **fields) -> ParsedEpisode:
    values = {
        "guid": guid,
        "title": f"Episode {guid}",
        "description": "<p>notes</p>",
        "published_at": None if days_ago is None else utc_now() - dt.timedelta(days=days_ago),
        "duration_seconds": 3600.0,
        "enclosure_url": f"https://cdn{position % 2}.example.com/{guid}.mp3",
        "enclosure_type": "audio/mpeg",
        "enclosure_length": 1000,
        "artwork_url": None,
        "feed_position": position,
    }
    values.update(fields)
    return ParsedEpisode(**values)


def feed(episodes: list[ParsedEpisode], title: str = "Show") -> ParsedFeed:
    return ParsedFeed(title=title, author="Host", summary=None, artwork_url=None, language="en", link=None, episodes=episodes)


class FakeFeedServer:
    """Stands in for ``fetch_feed`` + ``parse_feed``: conditional GET on an ETag."""

    def __init__(self, parsed: ParsedFeed) -> None:
        self.parsed = parsed
        self.etag = '"v1"'
        self.calls: list[tuple[str, str | None]] = []
        self.error: Exception | None = None
        self.parse_error = False
        self.delay = 0.0
        self.moved: str | None = None

    async def fetch(self, client, url, *, etag=None, last_modified=None, max_bytes=0, timeout=0.0) -> FeedFetch:
        self.calls.append((url, etag))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if etag == self.etag:
            return FeedFetch(304, None, self.etag, None, url)
        return FeedFetch(200, b"<rss/>", self.etag, None, url, permanent_redirect=self.moved)

    def parse(self, data: bytes, *, feed_url: str) -> ParsedFeed:
        if self.parse_error:
            raise FeedParseError("not a feed")
        return self.parsed

    def publish(self, parsed: ParsedFeed, etag: str) -> None:
        self.parsed, self.etag = parsed, etag


class RefresherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = make_settings(temp_dir(self))
        self.ctx = make_context(self.settings)
        self.addAsyncCleanup(close_context, self.ctx)
        # Fifteen weekly episodes, newest first, like the TAL snapshot.
        self.server = FakeFeedServer(feed([episode(f"g{i}", 7 * i + 1, i) for i in range(15)]))
        for name, fake in (("fetch_feed", self.server.fetch), ("parse_feed", self.server.parse)):
            patcher = mock.patch.object(refresher, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def subscribe(self, url: str = FEED_URL, **kwargs) -> refresher.SubscribeOutcome:
        return await refresher.subscribe(self.ctx, url, **kwargs)

    def episodes(self, podcast_id: int) -> dict[str, repo.Episode]:
        return {e.guid: e for e in repo.episodes_for_podcast(self.ctx.db, podcast_id)}

    def podcast(self, podcast_id: int) -> repo.Podcast:
        found = repo.get_podcast(self.ctx.db, podcast_id)
        assert found is not None
        return found

    async def refresh(self, podcast_id: int) -> tuple[refresher.ApplyResult | None, jobs.Job]:
        with self.ctx.db.write() as tx:
            job_id = jobs.enqueue(tx, "refresh_feed", podcast_id, now=now_iso()).job_id
            job = jobs.claim(tx, "refresh_feed", owner="t", lease_seconds=60, now=utc_now())
        assert job is not None and job.id == job_id
        result = await refresher.refresh_podcast(self.ctx, podcast_id, job=job)
        final = jobs.get_job(self.ctx.db, job_id)
        assert final is not None
        return result, final

    async def test_first_subscribe_inserts_everything_but_admits_only_the_newest(self) -> None:
        outcome = await self.subscribe()
        self.assertEqual(outcome.status, "created")
        podcast = outcome.podcast
        self.assertEqual((podcast.title, podcast.episode_count, podcast.http_etag), ("Show", 15, '"v1"'))
        episodes = self.episodes(podcast.id)
        self.assertEqual(len(episodes), 15)
        admitted = {guid for guid, e in episodes.items() if e.pipeline_state != "discovered"}
        self.assertEqual(admitted, {"g0"})
        self.assertEqual(episodes["g0"].pipeline_state, "download_pending")
        job = jobs.live_job(self.ctx.db, "download", episodes["g0"].id)
        self.assertEqual(job.params["host"], "cdn0.example.com")
        self.assertEqual(podcast.admitted_watermark, episodes["g0"].published_at)
        delay = (parse_iso(podcast.next_fetch_at) - utc_now()).total_seconds()
        self.assertTrue(0.85 * 1800 - 5 <= delay <= 1.15 * 1800, delay)

    async def test_backfill_count_is_per_podcast(self) -> None:
        outcome = await self.subscribe(initial_backfill_count=3)
        admitted = sorted(g for g, e in self.episodes(outcome.podcast.id).items() if e.pipeline_state == "download_pending")
        self.assertEqual(admitted, ["g0", "g1", "g2"])

    async def test_unchanged_feed_allocates_zero_seqs(self) -> None:
        podcast = (await self.subscribe()).podcast
        before = self.ctx.db.current_seq()
        _, job = await self.refresh(podcast.id)
        self.assertEqual(self.server.calls[-1], (FEED_URL, '"v1"'), "conditional GET")
        self.assertEqual(self.ctx.db.current_seq(), before, "a 304 bumps nothing")
        self.assertEqual(job.state, "done")
        self.assertEqual(self.podcast(podcast.id).last_fetch_status, 304)
        self.server.etag = '"v2"'  # same content, new validator: a 200 that changes nothing
        await self.refresh(podcast.id)
        self.assertEqual(self.ctx.db.current_seq(), before)
        self.assertEqual(self.podcast(podcast.id).http_etag, '"v2"')

    async def test_later_refresh_admits_new_recent_items_only(self) -> None:
        podcast = (await self.subscribe()).podcast
        items = list(self.server.parsed.episodes)
        new = [
            episode("fresh", 0.5, 0),  # newer than the watermark
            episode("rerun", 3000, 1),  # a new GUID for an ancient date
            episode("undated", None, 2),  # judged by its discovery
        ]
        shifted = [dataclasses.replace(e, feed_position=e.feed_position + 3) for e in items[:-1]]  # g14 left the feed
        self.server.publish(feed(new + shifted), '"v2"')
        before = self.ctx.db.current_seq()
        result, _ = await self.refresh(podcast.id)
        episodes = self.episodes(podcast.id)
        self.assertEqual(sorted(result.admitted), sorted([episodes["fresh"].id, episodes["undated"].id]))
        self.assertEqual(episodes["rerun"].pipeline_state, "discovered")
        self.assertIsNone(episodes["g14"].feed_position, "dropped items are marked, never deleted")
        self.assertEqual(episodes["g1"].feed_position, 4)
        self.assertEqual(self.podcast(podcast.id).admitted_watermark, episodes["fresh"].published_at)
        self.assertEqual(self.podcast(podcast.id).episode_count, 18)
        # 3 inserts + 2 admissions + the podcast's new count: nothing for the
        # untouched episodes whose positions shifted.
        self.assertEqual(self.ctx.db.current_seq() - before, 3 + 2 + 1)

    async def test_admissions_are_capped_per_refresh(self) -> None:
        podcast = (await self.subscribe()).podcast
        burst = [episode(f"new{i}", 0.1 + i / 100, i) for i in range(8)]
        self.server.publish(feed(burst + list(self.server.parsed.episodes)), '"v2"')
        result, _ = await self.refresh(podcast.id)
        self.assertEqual(len(result.admitted), self.settings.max_admits_per_refresh)
        admitted = {e.guid for e in repo.episodes_by_ids(self.ctx.db, result.admitted)}
        self.assertEqual(admitted, {f"new{i}" for i in range(5)}, "the newest win")

    async def test_auto_process_off_admits_nothing(self) -> None:
        outcome = await self.subscribe(auto_process_enabled=False)
        self.assertTrue(all(e.pipeline_state == "discovered" for e in self.episodes(outcome.podcast.id).values()))
        self.assertIsNone(outcome.podcast.admitted_watermark)
        with self.ctx.db.write() as tx:
            repo.update_server_settings(tx, self.settings, {"auto_process_enabled": False}, now=now_iso())
        other = await self.subscribe("https://other.example/feed")
        self.assertTrue(all(e.pipeline_state == "discovered" for e in self.episodes(other.podcast.id).values()))

    async def test_metadata_changes_update_in_place(self) -> None:
        podcast = (await self.subscribe()).podcast
        edited = [dataclasses.replace(e, title="Better title") if e.guid == "g3" else e for e in self.server.parsed.episodes]
        self.server.publish(feed(edited, title="Show"), '"v2"')
        before = self.ctx.db.current_seq()
        result, _ = await self.refresh(podcast.id)
        self.assertEqual(result.updated, [self.episodes(podcast.id)["g3"].id])
        self.assertEqual(self.ctx.db.current_seq(), before + 1)

    async def test_failures_back_off_and_never_give_up(self) -> None:
        podcast = (await self.subscribe()).podcast
        self.server.error = FeedFetchError("HTTP 503", status=503)
        delays = []
        for _ in range(3):
            _, job = await self.refresh(podcast.id)
            self.assertEqual((job.state, job.last_error), ("failed", "HTTP 503"))
            current = self.podcast(podcast.id)
            delays.append((parse_iso(current.next_fetch_at) - utc_now()).total_seconds())
        self.assertEqual(self.podcast(podcast.id).consecutive_failures, 3)
        for delay, raw in zip(delays, (60, 120, 240)):
            self.assertTrue(0.8 * raw - 2 <= delay <= 1.2 * raw, (delay, raw))
        self.server.error = FeedFetchError("slow down", status=429, retry_after=900.0)
        await self.refresh(podcast.id)
        delay = (parse_iso(self.podcast(podcast.id).next_fetch_at) - utc_now()).total_seconds()
        self.assertTrue(895 <= delay <= 900, "Retry-After is honoured exactly")
        self.assertEqual(self.podcast(podcast.id).last_fetch_error, "slow down")
        self.server.error = None
        _, job = await self.refresh(podcast.id)
        recovered = self.podcast(podcast.id)
        self.assertEqual((job.state, recovered.consecutive_failures, recovered.last_fetch_error), ("done", 0, None))

    async def test_subscribe_outcomes(self) -> None:
        created = await self.subscribe()
        calls = len(self.server.calls)
        existing = await self.subscribe(FEED_URL.upper().replace("/SHOW.XML", "/show.xml"))
        self.assertEqual((existing.status, existing.podcast.id), ("existing", created.podcast.id))
        self.assertEqual(len(self.server.calls), calls, "no fetch for a known feed")

        self.server.parse_error = True
        with self.assertRaises(FeedParseError):
            await self.subscribe("https://broken.example/feed")
        self.server.parse_error = False
        self.server.error = FeedFetchError("HTTP 404", status=404, permanent=True)
        with self.assertRaises(FeedFetchError):
            await self.subscribe("https://missing.example/feed")
        self.server.error = None
        self.assertEqual(len(repo.list_podcasts(self.ctx.db)), 1, "failed subscribes store nothing")
        with self.assertRaises(ValueError):
            await self.subscribe("ftp://example.com/feed")

    async def test_slow_subscribe_continues_as_a_background_refresh(self) -> None:
        self.server.delay = 0.2
        outcome = await self.subscribe("https://slow.example/feed", budget_seconds=0.05)
        self.assertEqual(outcome.status, "accepted")
        self.assertEqual(outcome.podcast.title, "https://slow.example/feed")
        job = jobs.get_job(self.ctx.db, outcome.job_id)
        self.assertEqual((job.kind, job.subject_id, job.state), ("refresh_feed", outcome.podcast.id, "pending"))
        self.assertEqual(self.episodes(outcome.podcast.id), {})
        self.server.delay = 0.0
        await self.refresh(outcome.podcast.id)
        podcast = self.podcast(outcome.podcast.id)
        self.assertEqual(podcast.title, "Show")
        admitted = [e.guid for e in self.episodes(podcast.id).values() if e.pipeline_state == "download_pending"]
        self.assertEqual(admitted, ["g0"], "the background fetch is still a first subscribe")

    async def test_feeds_sharing_guids_keep_all_their_episodes(self) -> None:
        a = (await self.subscribe()).podcast
        b = (await self.subscribe("https://mirror.example/feed")).podcast
        self.assertEqual(len(self.episodes(a.id)), 15)
        self.assertEqual(len(self.episodes(b.id)), 15)
        self.assertEqual(len(repo.guid_collisions(self.ctx.db)), 15)

    async def test_a_permanent_redirect_moves_the_subscription(self) -> None:
        podcast = (await self.subscribe()).podcast
        self.server.moved = "https://NEW.example.com/feed.xml"
        self.server.etag = '"v2"'
        await self.refresh(podcast.id)
        self.assertEqual(self.podcast(podcast.id).feed_url, "https://new.example.com/feed.xml")

    def test_choose_admissions_ranks_newest_first(self) -> None:
        now = utc_now()
        stamp = lambda days: iso(now - dt.timedelta(days=days))  # noqa: E731
        pairs = [
            (1, repo.FeedItem("a", "a", None, stamp(40), None, "u", None, None, None, 2)),
            (2, repo.FeedItem("b", "b", None, stamp(1), None, "u", None, None, None, 1)),
            (3, repo.FeedItem("c", "c", None, None, None, "u", None, None, None, 0)),
        ]
        first = refresher.choose_admissions(
            pairs, first_fetch=True, backfill=2, watermark=None, max_age_days=30, max_admits=5, now=now
        )
        self.assertEqual([episode_id for episode_id, _ in first], [2, 1], "dated items outrank undated ones")
        later = refresher.choose_admissions(
            pairs, first_fetch=False, backfill=1, watermark=stamp(50), max_age_days=30, max_admits=5, now=now
        )
        self.assertEqual([episode_id for episode_id, _ in later], [2, 1, 3], "all newer than the watermark")
        strict = refresher.choose_admissions(
            pairs, first_fetch=False, backfill=1, watermark=stamp(0), max_age_days=30, max_admits=5, now=now
        )
        self.assertEqual([episode_id for episode_id, _ in strict], [2, 3])


if __name__ == "__main__":
    unittest.main()
