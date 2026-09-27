"""Feed refresh, admission policy, and inline subscribe.

Each podcast is fetched every ``feed_interval_minutes`` (±15% jitter so feeds
drift apart) with a conditional GET. A 304 bumps no seq, so an unchanged feed
costs clients nothing. Failures back off per feed —
``min(60 s · 2^(n−1), 6 h) · jitter`` — and never give up; a refresh job is
one-shot and the podcast's ``next_fetch_at`` is the retry mechanism.

Admission (what gets downloaded and processed automatically) stops an archive
stampede: the first fetch inserts every item as ``discovered`` (text is free)
but admits only the newest ``initial_backfill_count``; later fetches admit an
item only if its GUID is new and it is newer than the admitted watermark or
within ``new_episode_max_age_days``, at most ``max_admits_per_refresh`` per
fetch. Items that leave the feed keep their rows — deleting them would
destroy transcripts for reasons outside our control.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from ..context import AppContext
from ..db import repo
from ..db.engine import WriteTx
from ..pipeline import commands, jobs, states
from ..pipeline.scheduler import RunningJob
from ..timeutil import iso, iso_or_none, utc_now
from .fetcher import FeedFetch, FeedFetchError, fetch_feed
from .parser import FeedParseError, ParsedEpisode, ParsedFeed, parse_feed

log = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 30.0
SUBSCRIBE_BUDGET_SECONDS = 20.0
INTERVAL_JITTER = 0.15


def normalize_feed_url(url: str) -> str:
    """Absolute http(s) only; scheme and host lower-cased, fragment dropped,
    so trivially different spellings of one feed do not subscribe twice."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("feedUrl must be an absolute http(s) URL")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def next_interval_fetch(ctx: AppContext, now: dt.datetime, rng: random.Random | None = None) -> str:
    jitter = (rng or random).uniform(1 - INTERVAL_JITTER, 1 + INTERVAL_JITTER)
    return iso(now + dt.timedelta(minutes=ctx.server_settings().feed_interval_minutes * jitter))


def next_backoff_fetch(
    failures: int, now: dt.datetime, *, retry_after: float | None = None, rng: random.Random | None = None
) -> str:
    """``failures`` counts this one. A host's Retry-After is honoured exactly."""
    delay = retry_after if retry_after is not None else jobs.RETRY_POLICIES[states.REFRESH_FEED].delay(failures, rng)
    return iso(now + dt.timedelta(seconds=delay))


def _feed_item(item: ParsedEpisode) -> repo.FeedItem:
    return repo.FeedItem(
        guid=item.guid,
        title=item.title,
        description=item.description,
        published_at=iso_or_none(item.published_at),
        declared_duration_seconds=item.duration_seconds,
        enclosure_url=item.enclosure_url,
        enclosure_type=item.enclosure_type,
        enclosure_length=item.enclosure_length,
        artwork_url=item.artwork_url,
        feed_position=item.feed_position,
    )


def _meta(feed: ParsedFeed) -> repo.FeedMeta:
    return repo.FeedMeta(
        title=feed.title,
        author=feed.author,
        summary=feed.summary,
        artwork_url=feed.artwork_url,
        language=feed.language,
        link=feed.link,
    )


def _newest_first(item: repo.FeedItem) -> tuple[bool, str, int]:
    # Dated items by date; undated ones after, in feed order (feeds list newest first).
    return (item.published_at is not None, item.published_at or "", -item.feed_position)


def choose_admissions(
    new_items: list[tuple[int, repo.FeedItem]],
    *,
    first_fetch: bool,
    backfill: int,
    watermark: str | None,
    max_age_days: int,
    max_admits: int,
    now: dt.datetime,
) -> list[tuple[int, repo.FeedItem]]:
    """Pure admission policy over ``(episode_id, item)`` pairs of new GUIDs."""
    ranked = sorted(new_items, key=lambda pair: _newest_first(pair[1]), reverse=True)
    if first_fetch:
        return ranked[: max(backfill, 0)]
    recent_cutoff = iso(now - dt.timedelta(days=max_age_days))
    eligible = [
        (episode_id, item)
        for episode_id, item in ranked
        # A new GUID without a date is judged by its discovery, i.e. now.
        if item.published_at is None
        or (watermark is not None and item.published_at > watermark)
        or item.published_at >= recent_cutoff
    ]
    return eligible[: max(max_admits, 0)]


@dataclass
class ApplyResult:
    inserted: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    admitted: list[int] = field(default_factory=list)
    enqueued_jobs: list[int] = field(default_factory=list)
    dropped: int = 0


def apply_feed(
    tx: WriteTx,
    ctx: AppContext,
    podcast: repo.Podcast,
    feed: ParsedFeed,
    fetch: FeedFetch,
    *,
    now: dt.datetime,
) -> ApplyResult:
    """Store a parsed 200 in one transaction: upsert items, admit, and schedule the next fetch."""
    stamp = iso(now)
    result = ApplyResult()
    existing = {episode.guid: episode for episode in repo.episodes_for_podcast(tx, podcast.id)}
    first_fetch = not existing
    fresh: list[repo.FeedItem] = []
    present: list[int] = []
    seen: set[str] = set()
    for parsed in feed.episodes:
        item = _feed_item(parsed)
        if item.guid in seen:
            continue  # a feed listing one GUID twice: the first (newest) occurrence wins
        seen.add(item.guid)
        episode = existing.get(item.guid)
        if episode is None:
            fresh.append(item)
            continue
        present.append(episode.id)
        if repo.update_episode_from_feed(tx, episode, item, now=stamp):
            result.updated.append(episode.id)
    result.inserted = repo.insert_episodes(tx, podcast.id, fresh, now=stamp)
    result.dropped = repo.clear_dropped_feed_positions(tx, podcast.id, present + result.inserted)

    server = repo.load_server_settings(tx, ctx.settings)
    watermark = podcast.admitted_watermark
    if podcast.auto_process_enabled and server.auto_process_enabled:
        admitted = choose_admissions(
            list(zip(result.inserted, fresh)),
            first_fetch=first_fetch,
            backfill=podcast.initial_backfill_count,
            watermark=watermark,
            max_age_days=ctx.settings.new_episode_max_age_days,
            max_admits=ctx.settings.max_admits_per_refresh,
            now=now,
        )
        by_id = {episode.id: episode for episode in repo.episodes_by_ids(tx, [eid for eid, _ in admitted])}
        for episode_id, item in admitted:
            # Admitted means downloaded and processed, even with analysis off.
            job_id = commands.advance(
                tx, by_id[episode_id], podcast=podcast, server=server, now=stamp, want_audio=True
            )
            result.admitted.append(episode_id)
            if job_id is not None:
                result.enqueued_jobs.append(job_id)
            if item.published_at is not None and (watermark is None or item.published_at > watermark):
                watermark = item.published_at

    feed_url = podcast.feed_url
    moved = fetch.permanent_redirect
    if moved and moved != podcast.feed_url:
        try:
            moved = normalize_feed_url(moved)
        except ValueError:
            moved = None
        if moved and repo.get_podcast_by_feed_url(tx, moved) is None:
            feed_url = moved
        elif moved:
            log.warning("feed moved to a URL another subscription uses", extra={"to": moved})
    repo.record_fetch_success(
        tx,
        podcast,
        meta=_meta(feed),
        feed_url=feed_url,
        etag=fetch.etag,
        last_modified=fetch.last_modified,
        admitted_watermark=watermark,
        next_fetch_at=next_interval_fetch(ctx, now),
        now=stamp,
    )
    return result


def _record_failure(tx: WriteTx, podcast: repo.Podcast, exc: Exception, *, now: dt.datetime) -> None:
    status = getattr(exc, "status", None)
    retry_after = getattr(exc, "retry_after", None)
    repo.record_fetch_failure(
        tx,
        podcast,
        status=status,
        error=jobs.describe_error(exc),
        next_fetch_at=next_backoff_fetch(podcast.consecutive_failures + 1, now, retry_after=retry_after),
        now=iso(now),
    )


async def _fetch_and_parse(
    ctx: AppContext, url: str, *, etag: str | None = None, last_modified: str | None = None
) -> tuple[FeedFetch, ParsedFeed | None]:
    fetch = await fetch_feed(
        ctx.http,
        url,
        etag=etag,
        last_modified=last_modified,
        max_bytes=ctx.settings.max_feed_bytes,
        timeout=FETCH_TIMEOUT_SECONDS,
    )
    if fetch.status == 304 or fetch.body is None:
        return fetch, None
    # ElementTree over a multi-megabyte feed would stall the event loop.
    feed = await asyncio.to_thread(parse_feed, fetch.body, feed_url=fetch.final_url)
    return fetch, feed


async def refresh_podcast(ctx: AppContext, podcast_id: int, *, job: jobs.Job | None = None) -> ApplyResult | None:
    """One conditional fetch. Feed errors are recorded on the podcast (with
    backoff) and fail ``job`` in the same transaction; they do not raise."""
    podcast = repo.get_podcast(ctx.db, podcast_id)
    if podcast is None:
        return None
    try:
        fetch, feed = await _fetch_and_parse(
            ctx, podcast.feed_url, etag=podcast.http_etag, last_modified=podcast.http_last_modified
        )
    except (FeedFetchError, FeedParseError) as exc:
        now = utc_now()
        with ctx.db.write() as tx:
            current = repo.get_podcast(tx, podcast_id)
            if current is not None:
                _record_failure(tx, current, exc, now=now)
            if job is not None:
                jobs.fail(tx, job.id, error=jobs.describe_error(exc), now=iso(now))
        log.warning("feed refresh failed", extra={"podcast_id": podcast_id, "error": jobs.describe_error(exc)})
        return None
    now = utc_now()
    with ctx.db.write() as tx:
        current = repo.get_podcast(tx, podcast_id)
        if current is None:  # unsubscribed while fetching
            return None
        if feed is None:
            repo.record_fetch_not_modified(tx, current, next_fetch_at=next_interval_fetch(ctx, now), now=iso(now))
            result = ApplyResult()
        else:
            result = apply_feed(tx, ctx, current, feed, fetch, now=now)
        if job is not None:
            jobs.complete(tx, job.id, now=iso(now))
    if result.inserted or result.admitted:
        log.info(
            "feed refreshed",
            extra={"podcast_id": podcast_id, "inserted": len(result.inserted), "admitted": result.admitted},
        )
    return result


@dataclass(frozen=True)
class SubscribeOutcome:
    status: Literal["created", "existing", "accepted"]  # -> 201, 200, 202
    podcast: repo.Podcast
    job_id: int | None = None  # the background refresh when "accepted"


async def subscribe(
    ctx: AppContext,
    feed_url: str,
    *,
    auto_process_enabled: bool = True,
    ad_analysis_enabled: bool = True,
    initial_backfill_count: int | None = None,
    budget_seconds: float = SUBSCRIBE_BUDGET_SECONDS,
) -> SubscribeOutcome:
    """POST /podcasts: fetch and parse inline within ``budget_seconds``.

    Raises ``ValueError`` for a malformed URL, ``FeedParseError`` (not a feed:
    422) or ``FeedFetchError`` (upstream failure: 502); nothing is stored in
    those cases. A fetch that outlives the budget is abandoned and the
    subscription continues as a background refresh job ("accepted", 202).
    The caller wakes the scheduler afterwards.
    """
    url = normalize_feed_url(feed_url)
    existing = repo.get_podcast_by_feed_url(ctx.db, url)
    if existing is not None:
        return SubscribeOutcome("existing", existing)
    backfill = ctx.settings.initial_backfill if initial_backfill_count is None else initial_backfill_count
    try:
        fetch, feed = await asyncio.wait_for(_fetch_and_parse(ctx, url), budget_seconds)
    except TimeoutError:
        now = iso(utc_now())
        with ctx.db.write() as tx:
            raced = repo.get_podcast_by_feed_url(tx, url)
            if raced is not None:
                return SubscribeOutcome("existing", raced)
            podcast, job_id = commands.add_podcast(
                tx,
                feed_url=url,
                title=url,
                auto_process_enabled=auto_process_enabled,
                ad_analysis_enabled=ad_analysis_enabled,
                initial_backfill_count=backfill,
                now=now,
            )
        return SubscribeOutcome("accepted", podcast, job_id)
    if feed is None:
        raise FeedFetchError("feed answered 304 to an unconditional request", status=304)
    now = utc_now()
    stamp = iso(now)
    target = url
    if fetch.permanent_redirect:
        try:
            target = normalize_feed_url(fetch.permanent_redirect)
        except ValueError:
            target = url
    with ctx.db.write() as tx:
        # Another request may have subscribed while this one fetched.
        for candidate in dict.fromkeys((url, target)):
            raced = repo.get_podcast_by_feed_url(tx, candidate)
            if raced is not None:
                return SubscribeOutcome("existing", raced)
        podcast = repo.insert_podcast(
            tx,
            feed_url=target,
            title=feed.title or target,
            auto_process_enabled=auto_process_enabled,
            ad_analysis_enabled=ad_analysis_enabled,
            initial_backfill_count=backfill,
            next_fetch_at=stamp,
            now=stamp,
        )
        apply_feed(tx, ctx, podcast, feed, fetch, now=now)
        stored = repo.get_podcast(tx, podcast.id)
    assert stored is not None
    return SubscribeOutcome("created", stored)


class RefreshStage:
    kind = states.REFRESH_FEED

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def run(self, running: RunningJob) -> None:
        await refresh_podcast(self.ctx, running.job.subject_id, job=running.job)

    def on_failure(self, tx: WriteTx, job: jobs.Job, error: str, decision: jobs.FailureDecision) -> None:
        # Only unexpected errors reach here; still back the feed off so the
        # due-feed loop does not re-enqueue it immediately.
        podcast = repo.get_podcast(tx, job.subject_id)
        if podcast is not None:
            now = utc_now()
            repo.record_fetch_failure(
                tx,
                podcast,
                status=None,
                error=error,
                next_fetch_at=next_backoff_fetch(podcast.consecutive_failures + 1, now),
                now=iso(now),
            )
