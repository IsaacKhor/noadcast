"""Durable job queue over the ``jobs`` table.

Durability lives in the table, not the event loop: a job is claimed with one
atomic ``UPDATE … RETURNING``, holds a lease that its runner renews, and is
finished, rescheduled with backoff, or failed in the same transaction as the
stage's own writes. After a crash, ``recover_leases()`` puts every running
job back in the queue; ``attempts`` was already incremented by the claim, so
a job that crashes the server every time still runs out of attempts.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import random
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Mapping
from urllib.parse import urlsplit

from ..classify.base import ClassifierError
from ..db import repo
from ..db.engine import Database, WriteTx
from ..feeds.fetcher import FeedFetchError
from ..feeds.parser import FeedParseError
from ..media.downloader import DownloadError, part_path
from ..media.probe import ProbeError
from ..media.store import MediaStore
from ..timeutil import iso, utc_now
from ..transcribe.protocol import TranscriptionError
from . import states

log = logging.getLogger(__name__)

PRIORITY_INTERACTIVE = 10  # a person is waiting (play, process, reanalyze, manual refresh)
PRIORITY_DEFAULT = 100
PRIORITY_BACKGROUND = 200  # housekeeping

DEFAULT_MAX_ATTEMPTS = 5
# A refresh is one-shot: the podcast's next_fetch_at (with per-feed backoff)
# is its retry mechanism, so job-level retries would only double it.
MAX_ATTEMPTS_BY_KIND = {states.REFRESH_FEED: 1}
LEASE_SECONDS = 15 * 60
LEASE_RENEW_SECONDS = 60.0
LIVE_STATES = ("pending", "running")


def transcription_lease_seconds(duration_seconds: float | None) -> float:
    """Long enough to cover a whole transcription at 2x real time even if
    renewals stall, so a busy host never has a healthy job reclaimed."""
    return max(float(LEASE_SECONDS), 0.5 * (duration_seconds or 0.0))


@dataclass(frozen=True)
class RetryPolicy:
    base_seconds: float
    cap_seconds: float

    def delay(self, attempt: int, rng: random.Random | None = None) -> float:
        """``min(base * 2^(attempt-1), cap) * jitter(0.8-1.2)``."""
        raw = min(self.base_seconds * 2 ** max(attempt - 1, 0), self.cap_seconds)
        return raw * (rng or random).uniform(0.8, 1.2)


RETRY_POLICIES: Mapping[str, RetryPolicy] = {
    states.DOWNLOAD: RetryPolicy(30.0, 3600.0),
    states.TRANSCRIBE: RetryPolicy(30.0, 3600.0),
    states.CLASSIFY: RetryPolicy(30.0, 3600.0),
    # Feed backoff is per podcast (next_fetch_at), never giving up; see feeds/refresher.py.
    states.REFRESH_FEED: RetryPolicy(60.0, 6 * 3600.0),
    states.EVICT: RetryPolicy(60.0, 3600.0),
}


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    subject_id: int
    state: str
    priority: int
    attempts: int
    max_attempts: int
    available_at: str
    lease_owner: str | None
    lease_expires_at: str | None
    params: dict[str, Any]
    last_error: str | None
    last_error_at: str | None
    created_at: str
    updated_at: str
    finished_at: str | None

    @property
    def is_live(self) -> bool:
        return self.state in LIVE_STATES


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        kind=row["kind"],
        subject_id=row["subject_id"],
        state=row["state"],
        priority=row["priority"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        available_at=row["available_at"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        params=json.loads(row["params_json"]),
        last_error=row["last_error"],
        last_error_at=row["last_error_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )


# -- reads ----------------------------------------------------------------------


def get_job(db: repo.Reader, job_id: int) -> Job | None:
    row = db.read_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return None if row is None else _job(row)


def live_job(db: repo.Reader, kind: str, subject_id: int) -> Job | None:
    row = db.read_one(
        "SELECT * FROM jobs WHERE kind = ? AND subject_id = ? AND state IN ('pending', 'running')", (kind, subject_id)
    )
    return None if row is None else _job(row)


def live_stage_job(db: repo.Reader, episode_id: int) -> Job | None:
    """The live download/transcribe/classify job for an episode, if any."""
    row = db.read_one(
        """
        SELECT * FROM jobs WHERE subject_id = ? AND kind IN ('download', 'transcribe', 'classify')
          AND state IN ('pending', 'running')
        ORDER BY id LIMIT 1
        """,
        (episode_id,),
    )
    return None if row is None else _job(row)


def list_jobs(
    db: repo.Reader, *, state: str | None = None, kind: str | None = None, limit: int = 100
) -> list[Job]:
    rows = db.read(
        """
        SELECT * FROM jobs WHERE (? IS NULL OR state = ?) AND (? IS NULL OR kind = ?)
        ORDER BY id DESC LIMIT ?
        """,
        (state, state, kind, kind, limit),
    )
    return [_job(row) for row in rows]


def live_jobs_for_subjects(db: repo.Reader, kinds: Collection[str], subject_ids: Collection[int]) -> list[Job]:
    rows = db.read(
        """
        SELECT * FROM jobs WHERE state IN ('pending', 'running')
          AND kind IN (SELECT value FROM json_each(?)) AND subject_id IN (SELECT value FROM json_each(?))
        """,
        (json.dumps(list(kinds)), json.dumps(list(subject_ids))),
    )
    return [_job(row) for row in rows]


@dataclass(frozen=True)
class ActiveStageJob:
    """A live stage job joined with its episode's pipeline state and progress."""

    job: Job
    pipeline_state: str
    progress_stage: str | None
    progress_current: float | None
    progress_total: float | None
    progress_updated_at: str | None
    duration_seconds: float | None


def active_stage_jobs(db: repo.Reader) -> list[ActiveStageJob]:
    rows = db.read(
        """
        SELECT j.*, e.pipeline_state AS e_pipeline_state, e.progress_stage AS e_progress_stage,
               e.progress_current AS e_progress_current, e.progress_total AS e_progress_total,
               e.progress_updated_at AS e_progress_updated_at,
               coalesce(e.measured_duration_seconds, e.declared_duration_seconds) AS e_duration
        FROM jobs j JOIN episodes e ON e.id = j.subject_id
        WHERE j.state IN ('pending', 'running') AND j.kind IN ('download', 'transcribe', 'classify')
        ORDER BY j.state = 'pending', j.priority, j.id
        """
    )
    return [
        ActiveStageJob(
            job=_job(row),
            pipeline_state=row["e_pipeline_state"],
            progress_stage=row["e_progress_stage"],
            progress_current=row["e_progress_current"],
            progress_total=row["e_progress_total"],
            progress_updated_at=row["e_progress_updated_at"],
            duration_seconds=row["e_duration"],
        )
        for row in rows
    ]


@dataclass(frozen=True)
class QueueStats:
    kind: str
    pending: int
    running: int
    available: int  # pending and past available_at
    oldest_available_at: str | None


def queue_stats(db: repo.Reader, *, now: str) -> list[QueueStats]:
    rows = db.read(
        """
        SELECT kind,
               sum(state = 'pending') AS pending, sum(state = 'running') AS running,
               sum(state = 'pending' AND available_at <= :now) AS available,
               min(CASE WHEN state = 'pending' THEN available_at END) AS oldest
        FROM jobs WHERE state IN ('pending', 'running') GROUP BY kind ORDER BY kind
        """,
        {"now": now},
    )
    return [QueueStats(r["kind"], r["pending"], r["running"], r["available"], r["oldest"]) for r in rows]


def recent_failures(db: repo.Reader, *, limit: int = 20) -> list[Job]:
    rows = db.read(
        "SELECT * FROM jobs WHERE state = 'failed' ORDER BY coalesce(finished_at, updated_at) DESC, id DESC LIMIT ?",
        (limit,),
    )
    return [_job(row) for row in rows]


# -- writes ---------------------------------------------------------------------


@dataclass(frozen=True)
class Enqueued:
    job_id: int
    created: bool  # False: an identical live job already existed and was returned


def enqueue(
    tx: WriteTx,
    kind: str,
    subject_id: int,
    *,
    now: str,
    priority: int = PRIORITY_DEFAULT,
    params: Mapping[str, Any] | None = None,
    available_at: str | None = None,
    max_attempts: int | None = None,
) -> Enqueued:
    """Idempotent: the partial unique index allows one live job per
    (kind, subject), so a hammered button returns the existing job — raised to
    the more urgent priority — instead of fanning out."""
    if kind not in states.JOB_KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    if max_attempts is None:
        max_attempts = MAX_ATTEMPTS_BY_KIND.get(kind, DEFAULT_MAX_ATTEMPTS)
    row = tx.read_one(
        """
        INSERT INTO jobs (kind, subject_id, state, priority, max_attempts, available_at, params_json,
                          created_at, updated_at)
        VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)
        ON CONFLICT (kind, subject_id) WHERE state IN ('pending', 'running') DO NOTHING
        RETURNING id
        """,
        (kind, subject_id, priority, max_attempts, available_at or now, json.dumps(dict(params or {})), now, now),
    )
    if row is not None:
        return Enqueued(row["id"], True)
    existing = live_job(tx, kind, subject_id)
    assert existing is not None, "conflict without a live job"
    if existing.state == "pending" and priority < existing.priority:
        tx.execute("UPDATE jobs SET priority = ?, updated_at = ? WHERE id = ?", (priority, now, existing.id))
    return Enqueued(existing.id, False)


def claim(
    tx: WriteTx,
    kind: str,
    *,
    owner: str,
    lease_seconds: float,
    now: dt.datetime,
    exclude_hosts: Collection[str] = (),
) -> Job | None:
    """Atomically take the most urgent available job of ``kind``. Download jobs
    carry their enclosure host in ``params.host`` so a saturated host can be
    skipped without holding a global slot."""
    row = tx.read_one(
        """
        UPDATE jobs SET state = 'running', attempts = attempts + 1, lease_owner = ?, lease_expires_at = ?,
                        updated_at = ?
        WHERE id = (
          SELECT id FROM jobs
          WHERE state = 'pending' AND kind = ? AND available_at <= ?
            AND coalesce(json_extract(params_json, '$.host'), '') NOT IN (SELECT value FROM json_each(?))
          ORDER BY priority, available_at, id LIMIT 1)
        RETURNING *
        """,
        (
            owner,
            iso(now + dt.timedelta(seconds=lease_seconds)),
            iso(now),
            kind,
            iso(now),
            json.dumps(sorted(exclude_hosts)),
        ),
    )
    return None if row is None else _job(row)


def renew_lease(tx: WriteTx, job_id: int, *, owner: str, lease_seconds: float, now: dt.datetime) -> bool:
    """False means the job is no longer ours (canceled or reclaimed): stop
    working on it. ``updated_at`` is left alone: it marks state changes, and
    GET /jobs/active derives its ETag from it."""
    cursor = tx.execute(
        "UPDATE jobs SET lease_expires_at = ? WHERE id = ? AND state = 'running' AND lease_owner = ?",
        (iso(now + dt.timedelta(seconds=lease_seconds)), job_id, owner),
    )
    return cursor.rowcount == 1


def complete(tx: WriteTx, job_id: int, *, now: str) -> bool:
    """Idempotent; a stage completes its job inside its final transaction and
    the runner's own call then finds nothing to do."""
    cursor = tx.execute(
        """
        UPDATE jobs SET state = 'done', lease_owner = NULL, lease_expires_at = NULL, finished_at = ?, updated_at = ?
        WHERE id = ? AND state = 'running'
        """,
        (now, now, job_id),
    )
    return cursor.rowcount == 1


def reschedule(tx: WriteTx, job_id: int, *, error: str, available_at: str, now: str) -> None:
    tx.execute(
        """
        UPDATE jobs SET state = 'pending', lease_owner = NULL, lease_expires_at = NULL, available_at = ?,
                        last_error = ?, last_error_at = ?, updated_at = ?
        WHERE id = ? AND state = 'running'
        """,
        (available_at, error, now, now, job_id),
    )


def fail(tx: WriteTx, job_id: int, *, error: str, now: str) -> None:
    tx.execute(
        """
        UPDATE jobs SET state = 'failed', lease_owner = NULL, lease_expires_at = NULL, last_error = ?,
                        last_error_at = ?, finished_at = ?, updated_at = ?
        WHERE id = ? AND state IN ('pending', 'running')
        """,
        (error, now, now, now, job_id),
    )


def cancel(tx: WriteTx, job_id: int, *, now: str) -> Job | None:
    """Cancel a live job; returns it as it was, or None if it was not live."""
    before = get_job(tx, job_id)
    if before is None or not before.is_live:
        return None
    tx.execute(
        """
        UPDATE jobs SET state = 'canceled', lease_owner = NULL, lease_expires_at = NULL, finished_at = ?,
                        updated_at = ?
        WHERE id = ?
        """,
        (now, now, job_id),
    )
    return before


def cancel_live_jobs_for_subjects(
    tx: WriteTx, kinds: Collection[str], subject_ids: Collection[int], *, now: str
) -> list[Job]:
    canceled = live_jobs_for_subjects(tx, kinds, subject_ids)
    for job in canceled:
        cancel(tx, job.id, now=now)
    return canceled


def requeue(tx: WriteTx, job: Job, *, now: str, priority: int | None = None) -> Job:
    """Make a finished job runnable again in place (manual retry). Attempts
    restart from zero: a person decided it is worth trying again."""
    tx.execute(
        """
        UPDATE jobs SET state = 'pending', attempts = 0, available_at = ?, priority = coalesce(?, priority),
                        lease_owner = NULL, lease_expires_at = NULL, finished_at = NULL, updated_at = ?
        WHERE id = ?
        """,
        (now, priority, now, job.id),
    )
    requeued = get_job(tx, job.id)
    assert requeued is not None
    return requeued


def make_available_now(tx: WriteTx, job_id: int, *, now: str) -> None:
    tx.execute(
        "UPDATE jobs SET available_at = ?, updated_at = ? WHERE id = ? AND state = 'pending' AND available_at > ?",
        (now, now, job_id, now),
    )


# -- failure policy ---------------------------------------------------------------


@dataclass(frozen=True)
class ErrorTraits:
    permanent: bool
    retry_after: float | None


def error_traits(exc: BaseException) -> ErrorTraits:
    """Permanent vs transient comes from the error types each module raises:
    an unparseable feed or undecodable file never gets better by waiting;
    everything else retries, honouring a provider's Retry-After exactly."""
    if isinstance(exc, (FeedParseError, ProbeError)):
        return ErrorTraits(True, None)
    if isinstance(exc, (FeedFetchError, DownloadError, ClassifierError)):
        return ErrorTraits(exc.permanent, exc.retry_after)
    if isinstance(exc, TranscriptionError):
        return ErrorTraits(exc.permanent, None)
    return ErrorTraits(False, None)


@dataclass(frozen=True)
class FailureDecision:
    retry: bool
    available_at: str | None
    reason: str  # permanent | exhausted | transient


def decide_failure(
    job: Job, exc: BaseException, *, now: dt.datetime, rng: random.Random | None = None,
    policies: Mapping[str, RetryPolicy] = RETRY_POLICIES,
) -> FailureDecision:
    traits = error_traits(exc)
    if traits.permanent:
        return FailureDecision(False, None, "permanent")
    if job.attempts >= job.max_attempts:
        return FailureDecision(False, None, "exhausted")
    delay = traits.retry_after if traits.retry_after is not None else policies[job.kind].delay(job.attempts, rng)
    return FailureDecision(True, iso(now + dt.timedelta(seconds=delay)), "transient")


def describe_error(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text if len(text) <= 500 else text[:497] + "..."


# -- recovery -------------------------------------------------------------------


@dataclass
class RecoveryReport:
    requeued: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    healed: list[int] = field(default_factory=list)  # episodes re-enqueued after losing their job
    missing_audio: list[int] = field(default_factory=list)
    lost_partials: list[int] = field(default_factory=list)
    removed_part_files: list[str] = field(default_factory=list)


def _requeue_interrupted(tx: WriteTx, interrupted: list[Job], report: RecoveryReport, *, why: str, now: str) -> None:
    """Running jobs whose runner is gone go back to pending (or fail once out of
    attempts); their episodes return from the running state to *_pending."""
    for job in interrupted:
        exhausted = job.attempts >= job.max_attempts
        if exhausted:
            fail(tx, job.id, error=f"{why}; attempts exhausted", now=now)
            report.failed.append(job.id)
        else:
            tx.execute(
                """
                UPDATE jobs SET state = 'pending', lease_owner = NULL, lease_expires_at = NULL, available_at = ?,
                                last_error = ?, last_error_at = ?, updated_at = ?
                WHERE id = ? AND state = 'running'
                """,
                (now, why, now, now, job.id),
            )
            report.requeued.append(job.id)
        if job.kind not in states.EPISODE_STAGES:
            continue
        episode = repo.get_episode(tx, job.subject_id)
        if episode is None or episode.pipeline_state != states.RUNNING_STATE[job.kind]:
            continue
        if exhausted:
            repo.set_episode_states(
                tx, episode.id, pipeline_state="failed", error=f"{job.kind}: {why}", progress=None, now=now
            )
        else:
            repo.set_episode_states(
                tx, episode.id, pipeline_state=states.PENDING_STATE[job.kind], progress=None, now=now
            )


def _heal_orphaned_episodes(tx: WriteTx, report: RecoveryReport, *, now: str) -> None:
    """An episode waiting in (or stuck in) a stage with no live job for it —
    e.g. its job was canceled mid-transition — gets the job back."""
    kind_for_state = {state: kind for kind, state in states.PENDING_STATE.items()}
    kind_for_state.update({state: kind for kind, state in states.RUNNING_STATE.items()})
    for episode in repo.episodes_in_pipeline_states(tx, kind_for_state):
        kind = kind_for_state[episode.pipeline_state]
        if live_job(tx, kind, episode.id) is not None:
            continue
        params = {"host": host_of(episode.enclosure_url)} if kind == states.DOWNLOAD else {}
        enqueue(tx, kind, episode.id, params=params, now=now)
        if episode.pipeline_state != states.PENDING_STATE[kind]:
            repo.set_episode_states(tx, episode.id, pipeline_state=states.PENDING_STATE[kind], progress=None, now=now)
        report.healed.append(episode.id)


def host_of(url: str) -> str:
    """The per-host download limit keys on the enclosure's first host."""
    return (urlsplit(url).hostname or "").lower()


def recover_leases(db: Database, store: MediaStore, *, now: dt.datetime | None = None) -> RecoveryReport:
    """Boot recovery. Only one server runs per data directory, so every
    ``running`` row belongs to a dead process. Also reconciles the audio
    bookkeeping against the disk: a ``partial`` row needs its ``.part`` file,
    a ``present`` row needs its file at the recorded size, and ``.part`` files
    nobody is downloading are deleted."""
    stamp = iso(now or utc_now())
    report = RecoveryReport()

    # Disk checks happen before the write transaction (no I/O inside writes).
    partial = repo.episodes_with_audio_state(db, "partial")
    present = repo.episodes_with_audio_state(db, "present")
    expected_parts: set[Path] = set()
    lost_partials: list[repo.Episode] = []
    for episode in partial:
        part = part_path(store.abspath(episode.audio_path)) if episode.audio_path else None
        if part is not None and part.exists():
            expected_parts.add(part.resolve())
        else:
            lost_partials.append(episode)
    missing: list[repo.Episode] = []
    for episode in present:
        path = store.abspath(episode.audio_path) if episode.audio_path else None
        try:
            ok = path is not None and os.stat(path).st_size == episode.audio_bytes
        except FileNotFoundError:
            ok = False
        if not ok:
            missing.append(episode)
    for part in store.iter_part_files():
        if part.resolve() not in expected_parts:
            part.unlink(missing_ok=True)
            report.removed_part_files.append(str(part))

    with db.write() as tx:
        running = [_job(row) for row in tx.read("SELECT * FROM jobs WHERE state = 'running' ORDER BY id")]
        _requeue_interrupted(tx, running, report, why="interrupted by a server restart", now=stamp)
        for episode in lost_partials:
            repo.set_episode_states(
                tx, episode.id, audio_state="evicted" if episode.audio_evicted_at else "absent", now=stamp
            )
            report.lost_partials.append(episode.id)
        for episode in missing:
            repo.mark_audio_evicted(tx, episode.id, reason="missing", now=stamp)
            report.missing_audio.append(episode.id)
        _heal_orphaned_episodes(tx, report, now=stamp)
    if any((report.requeued, report.failed, report.healed, report.missing_audio, report.lost_partials)):
        log.info(
            "recovered job state",
            extra={
                "requeued": report.requeued,
                "failed": report.failed,
                "healed": report.healed,
                "missing_audio": report.missing_audio,
                "lost_partials": report.lost_partials,
            },
        )
    return report


def reclaim_expired_leases(
    db: Database, *, now: dt.datetime | None = None, exclude_ids: Collection[int] = ()
) -> RecoveryReport:
    """The periodic sweeper: a running job whose lease lapsed has lost its
    runner. Jobs this process is actively running are never reclaimed."""
    stamp = iso(now or utc_now())
    report = RecoveryReport()
    with db.write() as tx:
        expired = [
            job
            for job in (
                _job(row)
                for row in tx.read(
                    "SELECT * FROM jobs WHERE state = 'running' AND lease_expires_at < ? ORDER BY id", (stamp,)
                )
            )
            if job.id not in exclude_ids
        ]
        _requeue_interrupted(tx, expired, report, why="lease expired", now=stamp)
    if report.requeued or report.failed:
        log.warning("reclaimed expired leases", extra={"requeued": report.requeued, "failed": report.failed})
    return report
