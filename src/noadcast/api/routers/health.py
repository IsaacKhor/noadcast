"""``GET /health`` (the only unauthenticated route) and ``GET /api/v1/session``."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import Response

from ... import __version__
from ...timeutil import now_iso
from ..deps import Ctx
from ..schemas import HealthOut, SessionOut, json_response

API_VERSION = 1
# Clients feature-detect by these; add one whenever an optional endpoint lands.
CAPABILITIES = ("sync", "jobsActive", "audioUrl", "releaseAudio", "usage", "opml")

public_router = APIRouter()
router = APIRouter()


@public_router.get("/health")
async def health(ctx: Ctx) -> Response:
    return json_response(
        HealthOut(
            status="ok",
            version=__version__,
            api_version=API_VERSION,
            instance_id=ctx.db.instance_id,
            auth_required=ctx.settings.auth_enabled,
            capabilities=list(CAPABILITIES),
        )
    )


@router.get("/session")
async def session() -> Response:
    """A cheap token check for the client's "Test connection" button."""
    return json_response(SessionOut(authenticated=True, server_time=now_iso()))
