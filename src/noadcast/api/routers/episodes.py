"""Episode detail, processing requests, and the classification history."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import Response

from ...db import repo
from ...pipeline import commands
from ...timeutil import now_iso
from ..deps import Ctx, episode_or_404
from ..errors import invalid_request
from ..schemas import ClassificationsOut, JobIdOut, ReanalyzeIn, classification_out, episode_out, json_response

router = APIRouter()


@router.get("/episodes/{episode_id}")
async def get_episode(episode_id: int, ctx: Ctx) -> Response:
    episode = episode_or_404(ctx, episode_id)
    return json_response(episode_out(episode, repo.markers_for_episode(ctx.db, episode_id)))


@router.post("/episodes/{episode_id}/process")
async def process_episode(episode_id: int, ctx: Ctx) -> Response:
    """Idempotent; ``jobId`` is null when nothing is left to do."""
    with ctx.db.write() as tx:
        job_id = commands.process_episode(
            tx, episode_id, server=repo.load_server_settings(tx, ctx.settings), now=now_iso()
        )
    ctx.wake()
    return json_response(JobIdOut(job_id=job_id), status_code=202)


@router.post("/episodes/{episode_id}/reanalyze")
async def reanalyze_episode(episode_id: int, ctx: Ctx, body: ReanalyzeIn | None = None) -> Response:
    """A fresh classification (optionally a fresh transcript first); earlier
    classifications are kept for comparison."""
    request = body or ReanalyzeIn()
    episode_or_404(ctx, episode_id)
    # Refuse up front rather than queue a job that can only fail and leave the
    # episode marked failed.
    if request.provider is not None and not ctx.classifiers.available().get(request.provider, False):
        raise invalid_request(f"classifier {request.provider!r} is not configured on this server")
    with ctx.db.write() as tx:
        job_id = commands.reanalyze_episode(
            tx,
            episode_id,
            server=repo.load_server_settings(tx, ctx.settings),
            now=now_iso(),
            provider=request.provider,
            model=request.model,
            thinking=request.thinking,
            retranscribe=request.retranscribe,
        )
    ctx.wake()
    return json_response(JobIdOut(job_id=job_id), status_code=202)


@router.get("/episodes/{episode_id}/classifications")
async def list_classifications(episode_id: int, ctx: Ctx) -> Response:
    """Every classification ever run for the episode, newest first: the A/B history."""
    episode_or_404(ctx, episode_id)
    records = repo.list_classifications(ctx.db, episode_id)
    return json_response(ClassificationsOut(items=[classification_out(record) for record in records]))
