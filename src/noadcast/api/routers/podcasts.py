"""Subscriptions: subscribe, switches, unsubscribe, and manual refreshes."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import Response

from ...db import repo
from ...feeds import refresher
from ...feeds.fetcher import FeedFetchError
from ...feeds.parser import FeedParseError
from ...pipeline import commands, eviction
from ...timeutil import now_iso
from ..deps import Ctx
from ..errors import ApiError, invalid_request
from ..schemas import (
    AcceptedPodcast,
    JobIdOut,
    JobIdsOut,
    PodcastEnvelope,
    PodcastsOut,
    PodcastPatchIn,
    SubscribeIn,
    json_response,
    podcast_out,
)

router = APIRouter()


@router.get("/podcasts")
async def list_podcasts(ctx: Ctx) -> Response:
    return json_response(PodcastsOut(items=[podcast_out(p) for p in repo.list_podcasts(ctx.db)]))


@router.post("/podcasts")
async def subscribe(body: SubscribeIn, ctx: Ctx) -> Response:
    """201 after an inline fetch, 200 if already subscribed, 202 if the fetch
    outlived its budget and continues as a background refresh."""
    try:
        feed_url = refresher.normalize_feed_url(body.feed_url)
    except ValueError as exc:
        raise invalid_request(str(exc)) from exc
    try:
        outcome = await refresher.subscribe(
            ctx,
            feed_url,
            auto_process_enabled=body.auto_process_enabled,
            ad_analysis_enabled=body.ad_analysis_enabled,
            initial_backfill_count=body.initial_backfill_count,
        )
    except FeedParseError as exc:
        raise ApiError(422, "invalidFeed", f"not a podcast feed: {exc}") from exc
    except FeedFetchError as exc:
        raise ApiError(502, "upstreamFailed", f"could not fetch the feed: {exc}") from exc
    ctx.wake()
    podcast = podcast_out(outcome.podcast)
    if outcome.status == "accepted":
        assert outcome.job_id is not None
        return json_response(AcceptedPodcast(podcast=podcast, job_id=outcome.job_id), status_code=202)
    return json_response(PodcastEnvelope(podcast=podcast), status_code=201 if outcome.status == "created" else 200)


@router.patch("/podcasts/{podcast_id}")
async def update_podcast(podcast_id: int, body: PodcastPatchIn, ctx: Ctx) -> Response:
    with ctx.db.write() as tx:
        podcast = repo.get_podcast(tx, podcast_id)
        if podcast is None:
            raise commands.NotFound(f"podcast {podcast_id}")
        updated = repo.set_podcast_switches(
            tx,
            podcast,
            auto_process_enabled=body.auto_process_enabled,
            ad_analysis_enabled=body.ad_analysis_enabled,
            now=now_iso(),
        )
    return json_response(podcast_out(updated))


@router.delete("/podcasts/{podcast_id}")
async def delete_podcast(podcast_id: int, ctx: Ctx) -> Response:
    """Episodes, transcripts, and markers go with the row; files after the commit."""
    with ctx.db.write() as tx:
        deleted = commands.delete_podcast(tx, podcast_id, now=now_iso())
    ctx.abort([job.id for job in deleted.canceled_jobs])
    await eviction.remove_podcast_files(ctx, deleted)
    return Response(status_code=204)


@router.post("/podcasts/{podcast_id}/refresh")
async def refresh_podcast(podcast_id: int, ctx: Ctx) -> Response:
    with ctx.db.write() as tx:
        job_id = commands.refresh_podcast(tx, podcast_id, now=now_iso())
    ctx.wake()
    return json_response(JobIdOut(job_id=job_id), status_code=202)


@router.post("/refresh")
async def refresh_all(ctx: Ctx) -> Response:
    with ctx.db.write() as tx:
        job_ids = commands.refresh_all(tx, now=now_iso())
    ctx.wake()
    return json_response(JobIdsOut(job_ids=job_ids), status_code=202)
