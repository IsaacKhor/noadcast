"""Operations invoked by the HTTP API and the CLI.

Each is a synchronous, seq-correct write meant to run inside one
``db.write()`` transaction. None of them does I/O: callers delete files and
wake the scheduler (``ctx.wake()``) after the transaction commits. The CLI
runs the same functions against the same database, and the running server's
poll picks their jobs up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..classifier_models import MODEL_IDS
from ..db import repo
from ..db.engine import WriteTx
from . import jobs, states

# Keys carried in stage-job params from one stage to the next.
#   provider/model/thinking: classifier override for this run (reanalyze)
#   force:        run analysis even where it is switched off (explicit request)
#   retranscribe: redo transcription although a transcript exists
#   reclassify:   redo classification although markers exist
#   host:         download jobs only; enclosure host for the per-host limit
CHAIN_KEYS = ("provider", "model", "thinking", "force", "retranscribe", "reclassify")


class NotFound(LookupError):
    """The episode, podcast, or job named by a command does not exist. The
    message names the thing ("episode 42"); callers add "not found"."""


def analysis_enabled(podcast: repo.Podcast, server: repo.ServerSettings, params: Mapping[str, Any]) -> bool:
    """Global and per-podcast switches must both be on; an explicit request overrides."""
    return bool(params.get("force")) or (server.ad_analysis_enabled and podcast.ad_analysis_enabled)


def _chain(params: Mapping[str, Any] | None) -> dict[str, Any]:
    return {key: value for key, value in (params or {}).items() if key in CHAIN_KEYS and value is not None}


def _enqueue_stage(
    tx: WriteTx,
    episode: repo.Episode,
    kind: str,
    *,
    priority: int,
    params: Mapping[str, Any],
    move_state: bool,
    now: str,
) -> int:
    job_params = _chain(params)
    if kind == states.DOWNLOAD:
        job_params["host"] = jobs.host_of(episode.enclosure_url)
    enqueued = jobs.enqueue(tx, kind, episode.id, priority=priority, params=job_params, now=now)
    if enqueued.created and move_state:
        repo.set_episode_states(
            tx, episode.id, pipeline_state=states.PENDING_STATE[kind], error=None, progress=None, now=now
        )
    return enqueued.job_id


def _next_step(episode: repo.Episode, podcast: repo.Podcast, server: repo.ServerSettings, chain: Mapping[str, Any]) -> str | None:
    """The stage that would run next if the audio were present, or None when done."""
    if not analysis_enabled(podcast, server, chain):
        return None
    if episode.transcript_state != "ready" or chain.get("retranscribe"):
        return states.TRANSCRIBE
    if episode.classify_state != "ready" or chain.get("reclassify"):
        return states.CLASSIFY
    return None


def _needs_audio(step: str | None, server: repo.ServerSettings, chain: Mapping[str, Any]) -> bool:
    return step == states.TRANSCRIBE


def advance(
    tx: WriteTx,
    episode: repo.Episode,
    *,
    podcast: repo.Podcast,
    server: repo.ServerSettings,
    now: str,
    priority: int = jobs.PRIORITY_DEFAULT,
    params: Mapping[str, Any] | None = None,
    want_audio: bool = False,
) -> int | None:
    """Enqueue the episode's next pipeline step, or mark it finished.

    Called on admission, by each stage when it succeeds (with the params it
    was run with, minus what it consumed), and by /process and /reanalyze.
    Missing audio is downloaded first when the caller wants it (playback,
    /process, admission) or the next step consumes it; re-classifying an
    evicted episode from its stored transcript fetches nothing — fresh
    bytes could even carry different ads and stale the transcript. Returns
    the job id of the next step, or None when nothing is left to do.
    """
    chain = _chain(params)
    step = _next_step(episode, podcast, server, chain)
    if episode.audio_state != "present" and (want_audio or _needs_audio(step, server, chain)):
        return _enqueue_stage(
            tx,
            episode,
            states.DOWNLOAD,
            priority=priority,
            params=chain,
            move_state=episode.pipeline_state in states.DOWNLOAD_IS_PIPELINE_STEP,
            now=now,
        )
    if step is not None:
        return _enqueue_stage(tx, episode, step, priority=priority, params=chain, move_state=True, now=now)
    if not analysis_enabled(podcast, server, chain):
        # Markers computed before analysis was switched off stay valid.
        classify_state = None if episode.classify_state == "ready" else "skipped"
        if episode.pipeline_state != "ready" or (classify_state and episode.classify_state != classify_state):
            repo.set_episode_states(
                tx, episode.id, pipeline_state="ready", classify_state=classify_state, error=None, progress=None,
                now=now,
            )
    elif episode.pipeline_state != "ready":
        repo.set_episode_states(tx, episode.id, pipeline_state="ready", error=None, progress=None, now=now)
    return None


def _load(tx: WriteTx, episode_id: int) -> tuple[repo.Episode, repo.Podcast]:
    episode = repo.get_episode(tx, episode_id)
    if episode is None:
        raise NotFound(f"episode {episode_id}")
    podcast = repo.get_podcast(tx, episode.podcast_id)
    assert podcast is not None, "episode without podcast"
    return episode, podcast


def process_episode(
    tx: WriteTx, episode_id: int, *, server: repo.ServerSettings, now: str, priority: int = jobs.PRIORITY_INTERACTIVE
) -> int | None:
    """Make sure the audio is on the server and, if analysis is on, that the
    episode is transcribed and classified. Idempotent: returns the live job
    if one is already working on the episode, None if nothing is left to do."""
    episode, podcast = _load(tx, episode_id)
    repo.clear_played_release(tx, episode_id)
    if episode.audio_state == "present":
        live = jobs.live_stage_job(tx, episode.id)
        if live is not None:
            return jobs.enqueue(tx, live.kind, episode.id, priority=priority, params=live.params, now=now).job_id
    # Missing audio comes first even if another stage is queued: the caller is
    # about to play or download it.
    return advance(tx, episode, podcast=podcast, server=server, now=now, priority=priority, want_audio=True)


def request_audio(tx: WriteTx, episode_id: int, *, server: repo.ServerSettings, now: str) -> int | None:
    """Priority (re-)download for a client asking for audio the server lacks."""
    return process_episode(tx, episode_id, server=server, now=now)


def reanalyze_episode(
    tx: WriteTx,
    episode_id: int,
    *,
    server: repo.ServerSettings,
    now: str,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    retranscribe: bool = False,
) -> int:
    """Run a fresh classification (optionally a fresh transcription first).
    Earlier classifications are kept for comparison. If a job of the needed
    kind is already live its id is returned and this request folds into it."""
    if provider is not None and provider != "openrouter":
        raise ValueError("only openrouter classification is supported")
    if model is not None and model not in MODEL_IDS:
        raise ValueError("unsupported classifier model")
    episode, podcast = _load(tx, episode_id)
    repo.clear_played_release(tx, episode_id)
    params: dict[str, Any] = {"force": True, "reclassify": True}
    if provider:
        params["provider"] = provider
    if model:
        params["model"] = model
    if thinking:
        params["thinking"] = thinking
    if retranscribe:
        params["retranscribe"] = True
    job_id = advance(
        tx, episode, podcast=podcast, server=server, now=now, priority=jobs.PRIORITY_INTERACTIVE, params=params
    )
    assert job_id is not None, "reclassify always leaves work to do"
    return job_id


def refresh_podcast(tx: WriteTx, podcast_id: int, *, now: str, priority: int = jobs.PRIORITY_INTERACTIVE) -> int:
    if repo.get_podcast(tx, podcast_id) is None:
        raise NotFound(f"podcast {podcast_id}")
    return jobs.enqueue(tx, states.REFRESH_FEED, podcast_id, priority=priority, now=now).job_id


def refresh_all(tx: WriteTx, *, now: str, priority: int = jobs.PRIORITY_INTERACTIVE) -> list[int]:
    return [
        jobs.enqueue(tx, states.REFRESH_FEED, podcast.id, priority=priority, now=now).job_id
        for podcast in repo.list_podcasts(tx)
    ]


def add_podcast(
    tx: WriteTx,
    *,
    feed_url: str,
    title: str,
    auto_process_enabled: bool,
    ad_analysis_enabled: bool,
    initial_backfill_count: int,
    now: str,
    priority: int = jobs.PRIORITY_INTERACTIVE,
) -> tuple[repo.Podcast, int]:
    """Subscribe without fetching: the row (titled ``title`` until the first
    fetch) plus an immediate refresh job. Used by OPML import and by a
    POST /podcasts whose inline fetch outlived its budget."""
    podcast = repo.insert_podcast(
        tx,
        feed_url=feed_url,
        title=title,
        auto_process_enabled=auto_process_enabled,
        ad_analysis_enabled=ad_analysis_enabled,
        initial_backfill_count=initial_backfill_count,
        next_fetch_at=now,
        now=now,
    )
    job_id = jobs.enqueue(tx, states.REFRESH_FEED, podcast.id, priority=priority, now=now).job_id
    return podcast, job_id


@dataclass(frozen=True)
class DeletedPodcast:
    podcast_id: int
    episode_ids: list[int]
    audio_paths: list[str]  # relative to the data dir; delete after commit
    canceled_jobs: list[jobs.Job] = field(default_factory=list)  # abort their runners after commit


def delete_podcast(tx: WriteTx, podcast_id: int, *, now: str) -> DeletedPodcast:
    if repo.get_podcast(tx, podcast_id) is None:
        raise NotFound(f"podcast {podcast_id}")
    episode_ids = repo.episode_ids_for_podcast(tx, podcast_id)
    audio_paths = [path for _, path in repo.audio_paths_for_podcast(tx, podcast_id)]
    canceled = jobs.cancel_live_jobs_for_subjects(tx, states.EPISODE_STAGES, episode_ids, now=now)
    canceled += jobs.cancel_live_jobs_for_subjects(tx, (states.REFRESH_FEED,), (podcast_id,), now=now)
    repo.delete_podcast(tx, podcast_id, now=now)
    return DeletedPodcast(podcast_id, episode_ids, audio_paths, canceled)


def retry_job(tx: WriteTx, job_id: int, *, now: str) -> jobs.Job:
    """Run a job again now. A pending job loses its backoff delay; a finished
    one is re-queued in place unless another live job already covers it."""
    job = jobs.get_job(tx, job_id)
    if job is None:
        raise NotFound(f"job {job_id}")
    if job.kind in states.EPISODE_STAGES:
        repo.clear_played_release(tx, job.subject_id)
    if job.state == "pending":
        jobs.make_available_now(tx, job.id, now=now)
        return job
    if job.state == "running":
        return job
    other = jobs.live_job(tx, job.kind, job.subject_id)
    if other is not None:
        return other
    requeued = jobs.requeue(tx, job, now=now, priority=jobs.PRIORITY_INTERACTIVE)
    if job.kind in states.EPISODE_STAGES:
        episode = repo.get_episode(tx, job.subject_id)
        if episode is not None and (
            job.kind != states.DOWNLOAD or episode.pipeline_state in states.DOWNLOAD_IS_PIPELINE_STEP
        ):
            repo.set_episode_states(
                tx, episode.id, pipeline_state=states.PENDING_STATE[job.kind], error=None, progress=None, now=now
            )
    return requeued


def cancel_job(tx: WriteTx, job_id: int, *, now: str) -> jobs.Job | None:
    """Cancel a live job; its episode rests at the last completed step. Returns
    the job as it was (the caller aborts its runner), or None if not live."""
    if jobs.get_job(tx, job_id) is None:
        raise NotFound(f"job {job_id}")
    before = jobs.cancel(tx, job_id, now=now)
    if before is None or before.kind not in states.EPISODE_STAGES:
        return before
    episode = repo.get_episode(tx, before.subject_id)
    if episode is not None and episode.pipeline_state in (
        states.PENDING_STATE[before.kind],
        states.RUNNING_STATE[before.kind],
    ):
        repo.set_episode_states(
            tx, episode.id, pipeline_state=states.RESTING_STATE[before.kind], progress=None, now=now
        )
    return before
