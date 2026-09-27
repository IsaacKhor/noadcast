"""Audio: signed-URL minting, Range serving, and the retention release.

Audio missing from the server answers ``409`` (``audioNotReady`` /
``audioEvicted``) with ``Retry-After`` and the id of the priority download
it just enqueued, so a client can show progress and retry.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path, PurePosixPath
from typing import Callable, Literal

from fastapi import APIRouter, Request
from starlette.responses import RedirectResponse, Response

from ...context import AppContext
from ...db import repo
from ...media import ranges
from ...pipeline import commands, eviction
from ...timeutil import iso, now_iso, parse_iso
from ..auth import signed_audio_path
from ..deps import Ctx, episode_or_404
from ..errors import ApiError
from ..schemas import AudioUrlOut, json_response

log = logging.getLogger(__name__)

router = APIRouter()

RETRY_AFTER_SECONDS = 5
ACCESS_TOUCH_INTERVAL_SECONDS = 60.0


class AccessThrottle:
    """Rate-limits ``audio_last_access_at`` writes to one per episode per
    interval: a single play is thousands of Range requests, and the value
    only feeds least-recently-used eviction."""

    def __init__(
        self, interval: float = ACCESS_TOUCH_INTERVAL_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.interval = interval
        self.clock = clock
        self._last: dict[int, float] = {}

    def due(self, episode_id: int) -> bool:
        now = self.clock()
        last = self._last.get(episode_id)
        if last is not None and now - last < self.interval:
            return False
        self._last[episode_id] = now
        return True


def _record_missing(ctx: AppContext, episode: repo.Episode, found_bytes: int | None) -> None:
    """A ``present`` row without its file (or with the wrong size) becomes
    evicted (``missing``), as boot recovery does, so the 409 that follows
    re-downloads it."""
    with ctx.db.write() as tx:
        current = repo.get_episode(tx, episode.id)
        if current is not None and current.audio_state == "present" and current.audio_path == episode.audio_path:
            repo.mark_audio_evicted(tx, episode.id, reason="missing", now=now_iso())
    log.warning(
        "stored audio missing or truncated",
        extra={"episode_id": episode.id, "expected_bytes": episode.audio_bytes, "found_bytes": found_bytes},
    )


async def stored_audio(ctx: AppContext, episode: repo.Episode) -> Path | None:
    """The episode's file if the server really has it at the recorded size."""
    if episode.audio_state != "present" or episode.audio_path is None:
        return None
    path = ctx.store.abspath(episode.audio_path)
    try:
        size: int | None = (await asyncio.to_thread(os.stat, path)).st_size
    except FileNotFoundError:
        size = None
    if size == episode.audio_bytes:
        return path
    _record_missing(ctx, episode, size)
    return None


async def request_audio(
    ctx: AppContext, episode_id: int, *, implicit: bool = False
) -> tuple[repo.Episode, int | None]:
    """Enqueue audio work unless a late GET follows a played release."""
    async with ctx.episode_release_lock(episode_id):
        episode = repo.get_episode(ctx.db, episode_id)
        if episode is None:
            raise commands.NotFound(f"episode {episode_id}")
        if implicit and episode.release_reason == "played":
            return episode, None
        with ctx.db.write() as tx:
            job_id = commands.request_audio(
                tx, episode_id, server=repo.load_server_settings(tx, ctx.settings), now=now_iso()
            )
    if job_id is not None:
        ctx.wake()
    return episode, job_id


def _audio_unavailable_error(episode: repo.Episode, job_id: int | None) -> ApiError:
    if episode.audio_state == "evicted":
        code, message = "audioEvicted", "the server does not have this audio"
    else:
        code, message = "audioNotReady", "the server does not have this audio yet"
    return ApiError(
        409, code, message,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)} if job_id is not None else None,
        extra={"jobId": job_id},
    )


async def audio_unavailable(ctx: AppContext, episode_id: int, *, implicit: bool = False) -> ApiError:
    episode, job_id = await request_audio(ctx, episode_id, implicit=implicit)
    return _audio_unavailable_error(episode, job_id)


@router.post("/episodes/{episode_id}/audio-url")
async def audio_url(request: Request, episode_id: int, ctx: Ctx) -> Response:
    """A signed URL for players that cannot send an Authorization header."""
    episode = episode_or_404(ctx, episode_id)
    if await stored_audio(ctx, episode) is None:
        raise await audio_unavailable(ctx, episode_id)
    path, expires = signed_audio_path(ctx.settings, episode_id, now=time.time())
    url = str(request.base_url).rstrip("/") + path
    return json_response(AudioUrlOut(path=path, url=url, expires_at=iso(expires)))


@router.api_route("/episodes/{episode_id}/audio", methods=["GET", "HEAD"])
async def stream_audio(request: Request, episode_id: int, ctx: Ctx) -> Response:
    """Authenticated by the bearer header or a signed URL (see auth.py). Never gzip-encoded."""
    episode = episode_or_404(ctx, episode_id)
    path = await stored_audio(ctx, episode)
    if path is None:
        if ctx.settings.evicted_redirect:
            current, job_id = await request_audio(ctx, episode_id, implicit=True)
            if current.release_reason != "played":
                return RedirectResponse(current.enclosure_url, status_code=307)
            raise _audio_unavailable_error(current, job_id)
        raise await audio_unavailable(ctx, episode_id, implicit=True)
    # mark_audio_present always records these alongside the path.
    assert episode.audio_path is not None and episode.audio_bytes is not None and episode.audio_sha256 is not None
    if request.app.state.audio_access.due(episode_id):
        with ctx.db.write() as tx:
            repo.touch_audio_access(tx, episode_id, now=now_iso())
    try:
        return ranges.audio_file_response(
            request,
            path,
            size=episode.audio_bytes,
            # The probe always records an audio/* or video/* type, which is what ranges requires.
            content_type=episode.audio_content_type or "audio/mpeg",
            etag=episode.audio_sha256,
            last_modified=parse_iso(episode.audio_downloaded_at),
            filename=f"{episode.id}{PurePosixPath(episode.audio_path).suffix}",
        )
    except FileNotFoundError:
        # Deleted between the size check and the open (a release or a sweep).
        _record_missing(ctx, episode, None)
        raise await audio_unavailable(ctx, episode_id, implicit=True) from None


@router.delete("/episodes/{episode_id}/audio")
async def release_audio(episode_id: int, ctx: Ctx, reason: Literal["played", "manual"] = "played") -> Response:
    """The retention release: transcript and markers stay; a later request re-downloads."""
    await eviction.release_audio(ctx, episode_id, reason=reason)
    return Response(status_code=204)
