"""Job progress and operations: ``/jobs/active``, ``/jobs``, retry, cancel."""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from ...pipeline import commands, jobs, states
from ...timeutil import now_iso
from ..deps import Ctx
from ..schemas import ActiveJobOut, ActiveJobsOut, JobIdOut, JobKind, JobState, JobsOut, job_out, json_response

router = APIRouter()

_NOUN = {states.DOWNLOAD: "download", states.TRANSCRIBE: "transcription", states.CLASSIFY: "classification"}
_RUNNING = {states.DOWNLOAD: "Downloading", states.TRANSCRIBE: "Transcribing", states.CLASSIFY: "Classifying"}


def _megabytes(count: float) -> str:
    return f"{count / 1_000_000:.1f}"


def _clock(seconds: float) -> str:
    """``65:19``: minutes are not wrapped into hours, like a podcast player's scrubber."""
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}:{secs:02d}"


def status_text(kind: str, job_state: str, attempts: int, current: float | None, total: float | None) -> str:
    if job_state != "running":
        return f"Waiting to retry {_NOUN[kind]}" if attempts > 0 else f"Queued for {_NOUN[kind]}"
    if kind == states.DOWNLOAD and current is not None:
        if total:
            return f"Downloading {_megabytes(current)} of {_megabytes(total)} MB"
        return f"Downloading {_megabytes(current)} MB"
    if kind == states.TRANSCRIBE and current is not None:
        if total:
            return f"Transcribing {_clock(current)} of {_clock(total)}"
        return f"Transcribing {_clock(current)}"
    return _RUNNING[kind]


def active_item(active: jobs.ActiveStageJob) -> ActiveJobOut:
    job = active.job
    # Progress columns may be left over from an earlier stage; they count only
    # while this job runs and reports under its own kind. Classification has
    # no meaningful units.
    reporting = job.state == "running" and active.progress_stage == job.kind and job.kind != states.CLASSIFY
    current = active.progress_current if reporting else None
    total = active.progress_total if reporting else None
    if reporting and total is None and job.kind == states.TRANSCRIBE:
        total = active.duration_seconds
    return ActiveJobOut(
        episode_id=job.subject_id,
        job_id=job.id,
        state=active.pipeline_state,
        stage=job.kind,
        job_state=job.state,
        current=current,
        total=total,
        status_text=status_text(job.kind, job.state, job.attempts, current, total),
        updated_at=(active.progress_updated_at if reporting else None) or job.updated_at,
    )


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    candidates = {candidate.strip().removeprefix("W/") for candidate in if_none_match.split(",")}
    return "*" in candidates or etag in candidates


@router.get("/jobs/active")
async def active_jobs(request: Request, ctx: Ctx) -> Response:
    """Polled every few seconds; a 304 lets the client skip decoding and every local write."""
    items = [active_item(active) for active in jobs.active_stage_jobs(ctx.db)]
    body = ActiveJobsOut(items=items).model_dump_json(by_alias=True).encode()
    etag = f'"{hashlib.sha256(body).hexdigest()[:32]}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(body, headers=headers, media_type="application/json")


@router.get("/jobs")
async def list_jobs(
    ctx: Ctx,
    state: JobState | None = None,
    kind: JobKind | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> Response:
    found = jobs.list_jobs(ctx.db, state=state, kind=kind, limit=limit)
    return json_response(JobsOut(items=[job_out(job) for job in found]))


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: int, ctx: Ctx) -> Response:
    """The job that will do the work may be another live job covering the same subject."""
    original = jobs.get_job(ctx.db, job_id)
    if original is not None and original.kind in states.EPISODE_STAGES:
        async with ctx.episode_release_lock(original.subject_id):
            with ctx.db.write() as tx:
                job = commands.retry_job(tx, job_id, now=now_iso())
    else:
        with ctx.db.write() as tx:
            job = commands.retry_job(tx, job_id, now=now_iso())
    ctx.wake()
    return json_response(JobIdOut(job_id=job.id), status_code=202)


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: int, ctx: Ctx) -> Response:
    with ctx.db.write() as tx:
        canceled = commands.cancel_job(tx, job_id, now=now_iso())
    if canceled is not None:
        ctx.abort([canceled.id])
    return Response(status_code=204)
