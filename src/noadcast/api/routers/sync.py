"""``GET /api/v1/sync``: the one delta feed the client keeps a cursor for."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from starlette.responses import Response

from ..deps import Ctx
from ..schemas import json_response
from ..sync import DEFAULT_LIMIT, build_sync_page

router = APIRouter()


@router.get("/sync")
async def sync(
    ctx: Ctx,
    since: Annotated[int, Query(ge=0)] = 0,
    limit: int = DEFAULT_LIMIT,
) -> Response:
    """``limit`` is clamped to 1..1000 rather than rejected; clients follow ``nextSince`` either way."""
    return json_response(build_sync_page(ctx, since=since, limit=limit))
