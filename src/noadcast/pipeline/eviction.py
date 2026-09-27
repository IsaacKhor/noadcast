"""Audio retention.

The server deletes its copy of an episode's audio when the client reports
the episode played (``DELETE /episodes/{id}/audio``), and a periodic sweep
bounds disk for episodes nobody plays: audio unused for
``keep_unplayed_days`` goes, then least-recently-used audio while free disk
is under ``min_free_bytes`` or stored audio exceeds ``audio_cache_max_bytes``.

Transcripts and markers are never evicted (~100 KB against ~60 MB), so an
evicted episode keeps its skip data and a later request re-downloads the
audio. Sweeps and manual releases defer while a media job still needs the
file. A played release cancels pending/running media jobs, waits for their
runners to close, and removes complete and partial audio. Its stop intent
blocks late audio reads from starting work again until an explicit request.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import shutil
from dataclasses import dataclass, field
from typing import Iterable

from ..context import AppContext
from ..db import repo
from ..timeutil import iso, parse_iso, utc_now
from . import jobs, states
from .commands import DeletedPodcast, NotFound
from .scheduler import RunningJob

log = logging.getLogger(__name__)

RELEASE_REASONS = ("played", "manual")


@dataclass
class SweepReport:
    released: list[int] = field(default_factory=list)
    aged: list[int] = field(default_factory=list)
    for_disk: list[int] = field(default_factory=list)
    freed_bytes: int = 0


async def _remove(ctx: AppContext, relpaths: Iterable[str]) -> int:
    freed = 0
    for relpath in relpaths:
        try:
            freed += await asyncio.to_thread(ctx.store.remove, relpath)
        except OSError:
            log.exception("could not delete audio file", extra={"path": relpath})
    return freed


async def release_audio(ctx: AppContext, episode_id: int, *, reason: str) -> bool:
    """The client's retention command. Played also stops unfinished media work.

    Manual releases keep the previous deferred behavior when a job is live.
    """
    if reason not in RELEASE_REASONS:
        raise ValueError(f"reason must be one of {RELEASE_REASONS}")
    if reason == "played":
        async with ctx.episode_release_lock(episode_id):
            now = iso(utc_now())
            with ctx.db.write() as tx:
                episode = repo.get_episode(tx, episode_id)
                if episode is None:
                    raise NotFound(f"episode {episode_id}")
                canceled = jobs.cancel_live_jobs_for_subjects(tx, states.EPISODE_STAGES, (episode_id,), now=now)
                repo.mark_played_released(tx, episode, now=now)
            canceled_ids = [job.id for job in canceled]
            ctx.abort(canceled_ids)
            await ctx.wait_aborted(canceled_ids)
            if episode.audio_path is not None:
                await _remove(ctx, [episode.audio_path])
            log.info("episode work stopped and audio released", extra={"episode_id": episode_id})
            return episode.audio_path is not None
    now = iso(utc_now())
    with ctx.db.write() as tx:
        episode = repo.get_episode(tx, episode_id)
        if episode is None:
            raise NotFound(f"episode {episode_id}")
        if episode.audio_state != "present" or episode.audio_path is None:
            return False
        if repo.episode_has_live_media_job(tx, episode_id):
            repo.record_release(tx, episode_id, reason=reason, now=now)
            return False
        # The row flips first so no new request is served the file; streams
        # already open keep their descriptor and finish normally.
        repo.mark_audio_evicted(tx, episode_id, reason=reason, now=now)
    await _remove(ctx, [episode.audio_path])
    log.info("audio released", extra={"episode_id": episode_id, "reason": reason})
    return True


async def remove_podcast_files(ctx: AppContext, deleted: DeletedPodcast) -> None:
    """After ``commands.delete_podcast`` commits: its audio and raw LLM responses."""
    await _remove(ctx, deleted.audio_paths)
    for episode_id in deleted.episode_ids:
        await asyncio.to_thread(shutil.rmtree, ctx.settings.llm_dir / str(episode_id), True)


def _last_useful(episode: repo.Episode) -> dt.datetime:
    stamp = episode.audio_last_access_at or episode.audio_downloaded_at or episode.created_at
    parsed = parse_iso(stamp)
    assert parsed is not None
    return parsed


async def sweep(ctx: AppContext, *, now: dt.datetime | None = None) -> SweepReport:
    """One retention pass: deferred releases, then age, then disk pressure."""
    moment = now or utc_now()
    settings = ctx.settings
    report = SweepReport()
    victims: list[tuple[repo.Episode, str]] = []

    for episode in repo.released_episodes_awaiting_eviction(ctx.db):
        victims.append((episode, episode.release_reason or "played"))
        report.released.append(episode.id)

    chosen = {episode.id for episode, _ in victims}
    candidates = [e for e in repo.eviction_candidates(ctx.db) if e.id not in chosen]
    cutoff = moment - dt.timedelta(days=settings.keep_unplayed_days)
    remaining: list[repo.Episode] = []
    for episode in candidates:
        if _last_useful(episode) < cutoff:
            victims.append((episode, "age"))
            report.aged.append(episode.id)
        else:
            remaining.append(episode)

    usage = await asyncio.to_thread(ctx.store.disk_usage)
    pending_free = sum(e.audio_bytes or 0 for e, _ in victims)
    free = usage.free_bytes + pending_free
    stored = repo.stored_audio_bytes(ctx.db) - pending_free
    for episode in remaining:  # least recently useful first
        if free >= settings.min_free_bytes and stored <= settings.audio_cache_max_bytes:
            break
        victims.append((episode, "disk"))
        report.for_disk.append(episode.id)
        free += episode.audio_bytes or 0
        stored -= episode.audio_bytes or 0

    if not victims:
        return report
    stamp = iso(moment)
    evicted: list[str] = []
    with ctx.db.write() as tx:
        for episode, reason in victims:
            # Re-checked inside the write: a job may have been enqueued since.
            current = repo.get_episode(tx, episode.id)
            if current is None or current.audio_state != "present" or current.audio_path is None:
                continue
            if reason in RELEASE_REASONS and (current.release_reason or "played") != reason:
                continue  # an explicit request may have cleared a deferred release
            if repo.episode_has_live_media_job(tx, episode.id):
                continue
            if reason == "played":
                repo.mark_played_released(tx, current, now=stamp)
            else:
                repo.mark_audio_evicted(tx, episode.id, reason=reason, now=stamp)
            evicted.append(current.audio_path)
    report.freed_bytes = await _remove(ctx, evicted)
    log.info(
        "retention sweep",
        extra={
            "released": report.released,
            "aged": report.aged,
            "for_disk": report.for_disk,
            "freed_bytes": report.freed_bytes,
        },
    )
    return report


class EvictStage:
    """The ``evict`` job kind: one sweep per job (subject 0 = the whole library)."""

    kind = states.EVICT

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def run(self, running: RunningJob) -> None:
        await sweep(self.ctx)

    def on_failure(self, tx, job: jobs.Job, error: str, decision: jobs.FailureDecision) -> None:
        """Nothing to record beyond the job row itself."""
