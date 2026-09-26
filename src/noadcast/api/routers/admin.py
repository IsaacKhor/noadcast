"""``GET /api/v1/admin/stats``: pool, queues, disk, spend, failures, GUID collisions."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import JSONResponse, Response

from ...pipeline import stats
from ..deps import Ctx

router = APIRouter()


@router.get("/admin/stats")
async def admin_stats(ctx: Ctx) -> Response:
    # Only the in-process TranscriptionPool reports stats; fakes and a
    # pool-less server leave the section null.
    pool_stats = getattr(ctx.transcriber, "stats", None)
    snapshot = stats.collect_stats(ctx.db, ctx.store, pool_stats=pool_stats() if callable(pool_stats) else None)
    return JSONResponse(snapshot)
