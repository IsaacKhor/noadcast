"""Server settings, LLM usage, and OPML import/export."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from ...db import repo
from ...feeds import refresher
from ...pipeline import commands
from ...timeutil import iso, now_iso, utc_now
from .. import opml
from ..deps import Ctx
from ..errors import ApiError, invalid_request
from ..schemas import (
    OpmlFailureOut,
    OpmlImportOut,
    SettingsPatchIn,
    UsageDayOut,
    UsageModelOut,
    UsageOut,
    UsageTotalsOut,
    json_response,
    podcast_out,
    settings_out,
)

router = APIRouter()

MAX_OPML_BYTES = 5 * 1024**2


@router.get("/settings")
async def get_settings(ctx: Ctx) -> Response:
    return json_response(settings_out(ctx.server_settings(), ctx.classifiers.available()))


@router.patch("/settings")
async def update_settings(body: SettingsPatchIn, ctx: Ctx) -> Response:
    # model_dump uses field names, which are exactly repo.SETTING_KEYS.
    changes = body.model_dump(exclude_none=True)
    with ctx.db.write() as tx:
        updated = repo.update_server_settings(tx, ctx.settings, changes, now=now_iso())
    return json_response(settings_out(updated, ctx.classifiers.available()))


@router.get("/usage")
async def usage(ctx: Ctx, days: Annotated[int, Query(ge=1, le=366)] = 30) -> Response:
    since = iso(utc_now() - dt.timedelta(days=days))
    by_day = repo.usage_by_day(ctx.db, created_since=since)
    by_model = repo.usage_by_model(ctx.db, created_since=since)
    totals = UsageTotalsOut(
        calls=sum(row.calls for row in by_day),
        input_tokens=sum(row.input_tokens for row in by_day),
        thought_tokens=sum(row.thought_tokens for row in by_day),
        output_tokens=sum(row.output_tokens for row in by_day),
        cost_usd=sum(row.cost_usd for row in by_day),
    )
    return json_response(
        UsageOut(
            days=[
                UsageDayOut(
                    date=row.key,
                    calls=row.calls,
                    input_tokens=row.input_tokens,
                    thought_tokens=row.thought_tokens,
                    output_tokens=row.output_tokens,
                    cost_usd=row.cost_usd,
                )
                for row in by_day
            ],
            by_model=[
                UsageModelOut(
                    provider=row.provider or "",
                    model=row.model or "",
                    calls=row.calls,
                    input_tokens=row.input_tokens,
                    thought_tokens=row.thought_tokens,
                    output_tokens=row.output_tokens,
                    cost_usd=row.cost_usd,
                )
                for row in by_model
            ],
            totals=totals,
        )
    )


async def _read_capped(request: Request, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(413, "payloadTooLarge", f"OPML document larger than {limit // 1024**2} MiB")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/opml")
async def import_opml(request: Request, ctx: Ctx) -> Response:
    """Subscribes to every feed in the document without fetching inline: each
    new podcast gets an immediate background refresh."""
    data = await _read_capped(request, MAX_OPML_BYTES)
    try:
        feeds = await asyncio.to_thread(opml.parse_opml, data)
    except ValueError as exc:
        raise invalid_request(str(exc)) from exc
    added: list[repo.Podcast] = []
    existing: list[repo.Podcast] = []
    failed: list[OpmlFailureOut] = []
    seen: set[str] = set()
    now = now_iso()
    with ctx.db.write() as tx:
        for feed in feeds:
            try:
                url = refresher.normalize_feed_url(feed.url)
            except ValueError as exc:
                if feed.url not in seen:
                    seen.add(feed.url)
                    failed.append(OpmlFailureOut(feed_url=feed.url, error=str(exc)))
                continue
            if url in seen:
                continue
            seen.add(url)
            podcast = repo.get_podcast_by_feed_url(tx, url)
            if podcast is not None:
                existing.append(podcast)
                continue
            podcast, _ = commands.add_podcast(
                tx,
                feed_url=url,
                title=feed.title or url,
                auto_process_enabled=True,
                ad_analysis_enabled=True,
                initial_backfill_count=ctx.settings.initial_backfill,
                now=now,
            )
            added.append(podcast)
    ctx.wake()
    return json_response(
        OpmlImportOut(
            added=[podcast_out(p) for p in added],
            existing=[podcast_out(p) for p in existing],
            failed=failed,
        )
    )


@router.get("/opml")
async def export_opml(ctx: Ctx) -> Response:
    body = opml.render_opml(repo.list_podcasts(ctx.db), now=utc_now())
    return Response(
        body, media_type="text/x-opml", headers={"Content-Disposition": 'attachment; filename="noadcast.opml"'}
    )
