"""In-process asyncio scheduler over the SQLite ``jobs`` table.

One loop per job kind claims work while its semaphore has a free slot, and
sleeps on an ``asyncio.Event`` (set by ``wake``) with a short fallback poll,
which is also how jobs enqueued by the CLI in another process get noticed.
Each claimed job runs in its own task with a heartbeat that renews its lease;
a lost lease (the job was canceled or reclaimed elsewhere) cancels the task.

No broker: one box, a few episodes a day, and durability already lives in
the table. The single-writer rule (db/engine.py) holds because every
database call here is synchronous on the event-loop thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import fcntl
import logging
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterable, Iterator, Mapping, Protocol

from ..config import Settings
from ..context import AppContext
from ..db import repo
from ..db.engine import WriteTx
from ..logging_setup import log_context
from ..timeutil import iso, utc_now
from . import jobs, states

log = logging.getLogger(__name__)


@dataclass
class RunningJob:
    """A claimed job plus the lease its heartbeat keeps renewing."""

    job: jobs.Job
    owner: str
    lease_seconds: float


class Stage(Protocol):
    kind: str

    async def run(self, running: RunningJob) -> None:
        """Do the work. Ideally complete the job (``jobs.complete``) inside the
        stage's final write so the result and the completion commit together;
        otherwise the runner completes it after ``run`` returns."""

    def on_failure(self, tx: WriteTx, job: jobs.Job, error: str, decision: jobs.FailureDecision) -> None:
        """Record the failure on the job's subject, in the same transaction as
        the job being rescheduled or failed."""


@dataclass(frozen=True)
class SchedulerConfig:
    concurrency: Mapping[str, int]
    per_host_downloads: int = 2
    poll_seconds: float = 5.0
    lease_renew_seconds: float = jobs.LEASE_RENEW_SECONDS
    sweep_seconds: float = 60.0
    maintenance_seconds: float = 600.0
    drain_seconds: float = 60.0
    tombstone_retention_days: int = 90
    retry_policies: Mapping[str, jobs.RetryPolicy] = field(default_factory=lambda: dict(jobs.RETRY_POLICIES))

    @classmethod
    def from_settings(cls, settings: Settings, **overrides) -> "SchedulerConfig":
        concurrency = {
            states.REFRESH_FEED: settings.refresh_concurrency,
            states.DOWNLOAD: settings.download_concurrency,
            states.TRANSCRIBE: settings.pool_workers,
            states.CLASSIFY: settings.classify_concurrency,
            states.EVICT: 1,
        }
        base = {
            "concurrency": concurrency,
            "per_host_downloads": settings.download_per_host,
            "tombstone_retention_days": settings.tombstone_retention_days,
        }
        base.update(overrides)
        return cls(**base)


class Scheduler:
    def __init__(
        self,
        ctx: AppContext,
        stages: Mapping[str, Stage],
        config: SchedulerConfig,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.stages = dict(stages)
        self.config = config
        self._rng = rng or random.Random()
        self._wake = {kind: asyncio.Event() for kind in self.stages}
        self._slots = {kind: asyncio.Semaphore(max(1, config.concurrency.get(kind, 1))) for kind in self.stages}
        self._host_load: Counter[str] = Counter()
        self._running: dict[int, asyncio.Task[None]] = {}
        self._loops: list[asyncio.Task[None]] = []
        self._stopping = False

    # -- control ------------------------------------------------------------------

    def start(self) -> None:
        for kind in self.stages:
            self._loops.append(asyncio.create_task(self._claim_loop(kind), name=f"claim-{kind}"))
        self._loops.append(asyncio.create_task(self._sweep_loop(), name="lease-sweeper"))
        self._loops.append(asyncio.create_task(self._feed_loop(), name="feed-due"))
        self._loops.append(asyncio.create_task(self._maintenance_loop(), name="maintenance"))

    def wake(self, kind: str) -> None:
        event = self._wake.get(kind)
        if event is not None:
            event.set()

    def wake_all(self) -> None:
        for event in self._wake.values():
            event.set()

    def abort(self, job_ids: Iterable[int]) -> None:
        for job_id in job_ids:
            task = self._running.get(job_id)
            if task is not None:
                task.cancel()

    async def wait_aborted(self, job_ids: Iterable[int]) -> None:
        """Wait until canceled runners close their transfers before file cleanup."""
        tasks = [task for job_id in job_ids if (task := self._running.get(job_id)) is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def running_job_ids(self) -> set[int]:
        return set(self._running)

    def idle(self) -> bool:
        """Nothing running and nothing runnable right now (tests, drain checks)."""
        if self._running:
            return False
        row = self.db.read_one(
            "SELECT EXISTS (SELECT 1 FROM jobs WHERE state = 'pending' AND available_at <= ?) AS busy",
            (iso(utc_now()),),
        )
        return not (row and row["busy"])

    async def shutdown(self) -> None:
        """Stop claiming, let running jobs finish for up to ``drain_seconds``,
        then cancel the rest. Canceled jobs stay ``running`` in the table, the
        same as after a crash, and ``recover_leases`` requeues them at the next
        boot — one recovery path to trust instead of two."""
        self._stopping = True
        for task in self._loops:
            task.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
        self._loops.clear()
        pending = list(self._running.values())
        if pending:
            log.info("draining running jobs", extra={"jobs": sorted(self._running)})
            _, still_running = await asyncio.wait(pending, timeout=self.config.drain_seconds)
            for task in still_running:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    # -- claiming -----------------------------------------------------------------

    async def _claim_loop(self, kind: str) -> None:
        slots = self._slots[kind]
        wake = self._wake[kind]
        while not self._stopping:
            await slots.acquire()
            # Cleared before claiming, so a wake that races the claim is not lost.
            wake.clear()
            try:
                running = self._claim(kind)
            except Exception:
                log.exception("claim failed", extra={"kind": kind})
                running = None
            if running is None:
                slots.release()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), self.config.poll_seconds)
                continue
            task = asyncio.create_task(self._run(running, slots), name=f"job-{running.job.id}-{kind}")
            self._running[running.job.id] = task

    def _claim(self, kind: str) -> RunningJob | None:
        now = utc_now()
        saturated = (
            {host for host, load in self._host_load.items() if load >= self.config.per_host_downloads}
            if kind == states.DOWNLOAD
            else set()
        )
        with self.db.write() as tx:
            job = jobs.claim(
                tx, kind, owner=self.ctx.owner, lease_seconds=jobs.LEASE_SECONDS, now=now,
                exclude_hosts=saturated, exclude_ids=self.running_job_ids,
            )
            if job is None:
                return None
            lease = self._lease_for(tx, job)
            if lease != jobs.LEASE_SECONDS:
                jobs.renew_lease(tx, job.id, owner=self.ctx.owner, lease_seconds=lease, now=now)
        if kind == states.DOWNLOAD:
            self._host_load[job.params.get("host", "")] += 1
        return RunningJob(job=job, owner=self.ctx.owner, lease_seconds=lease)

    def _lease_for(self, tx: WriteTx, job: jobs.Job) -> float:
        if job.kind == states.TRANSCRIBE:
            episode = repo.get_episode(tx, job.subject_id)
            return jobs.transcription_lease_seconds(episode.duration_seconds if episode else None)
        return float(jobs.LEASE_SECONDS)

    # -- running ------------------------------------------------------------------

    async def _run(self, running: RunningJob, slots: asyncio.Semaphore) -> None:
        job = running.job
        stage = self.stages[job.kind]
        heartbeat = asyncio.create_task(self._heartbeat(running), name=f"lease-{job.id}")
        try:
            with log_context(job_id=job.id, job_kind=job.kind, subject_id=job.subject_id, attempt=job.attempts):
                try:
                    await stage.run(running)
                except asyncio.CancelledError:
                    # Shutdown leaves the row running for recover_leases; an
                    # abort follows a cancel that already rewrote the row.
                    log.info("job interrupted", extra={"shutdown": self._stopping})
                    raise
                except Exception as exc:
                    self._record_failure(running, exc)
                else:
                    with self.db.write() as tx:
                        current = jobs.get_job(tx, job.id)
                        if (current is not None and current.state == "running"
                                and current.lease_owner == running.owner and current.attempts == job.attempts):
                            jobs.complete(tx, job.id, now=iso(utc_now()))
        except Exception:
            # Bookkeeping itself failed (e.g. the database stayed locked): the
            # row keeps its lease, which lapses, and the sweeper requeues it.
            log.exception("could not record the outcome of a job", extra={"job_id": job.id})
        finally:
            heartbeat.cancel()
            self._running.pop(job.id, None)
            if job.kind == states.DOWNLOAD:
                host = job.params.get("host", "")
                self._host_load[host] -= 1
                if self._host_load[host] <= 0:
                    del self._host_load[host]
            slots.release()
            # The stage may have enqueued follow-up work (or freed a host slot).
            if not self._stopping:
                self.wake_all()

    def _record_failure(self, running: RunningJob, exc: Exception) -> None:
        job = running.job
        now = utc_now()
        decision = jobs.decide_failure(job, exc, now=now, rng=self._rng, policies=self.config.retry_policies)
        error = jobs.describe_error(exc)
        level = logging.WARNING if decision.retry else logging.ERROR
        log.log(
            level,
            "job failed",
            exc_info=decision.reason == "transient" and not _expected(exc),
            extra={"error": error, "decision": decision.reason, "retry_at": decision.available_at},
        )
        with self.db.write() as tx:
            current = jobs.get_job(tx, job.id)
            if (current is None or current.state != "running" or current.lease_owner != running.owner
                    or current.attempts != job.attempts):
                return  # canceled or reclaimed while failing; the new owner decides
            if decision.retry:
                assert decision.available_at is not None
                jobs.reschedule(tx, job.id, error=error, available_at=decision.available_at, now=iso(now))
            else:
                jobs.fail(tx, job.id, error=error, now=iso(now))
            self.stages[job.kind].on_failure(tx, current, error, decision)

    async def _heartbeat(self, running: RunningJob) -> None:
        while True:
            await asyncio.sleep(self.config.lease_renew_seconds)
            with self.db.write() as tx:
                current = jobs.get_job(tx, running.job.id)
                held = bool(current is not None and current.state == "running"
                            and current.lease_owner == running.owner and current.attempts == running.job.attempts)
                if held:
                    held = jobs.renew_lease(
                        tx, running.job.id, owner=running.owner, lease_seconds=running.lease_seconds, now=utc_now()
                    )
            if not held:
                current = jobs.get_job(self.db, running.job.id)
                if current is not None and current.state == "done":
                    return  # the stage completed it and is finishing post-commit work
                log.warning("lease lost; stopping job", extra={"job_id": running.job.id})
                self.abort([running.job.id])
                return

    # -- periodic work --------------------------------------------------------------

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.sweep_seconds)
            try:
                report = jobs.reclaim_expired_leases(self.db, exclude_ids=self.running_job_ids)
                if report.requeued:
                    self.wake_all()
            except Exception:
                log.exception("lease sweep failed")

    async def _feed_loop(self) -> None:
        """Enqueue a refresh for every podcast whose ``next_fetch_at`` passed.
        The refresher sets the next time (interval with jitter, or backoff)."""
        while True:
            try:
                stamp = iso(utc_now())
                due = repo.due_podcast_ids(self.db, stamp)
                if due:
                    with self.db.write() as tx:
                        created = [
                            jobs.enqueue(tx, states.REFRESH_FEED, podcast_id, now=stamp).created for podcast_id in due
                        ]
                    if any(created):
                        self.wake(states.REFRESH_FEED)
            except Exception:
                log.exception("feed scheduling failed")
            await asyncio.sleep(self.config.poll_seconds)

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                now = utc_now()
                with self.db.write() as tx:
                    jobs.enqueue(
                        tx, states.EVICT, 0, priority=jobs.PRIORITY_BACKGROUND, params={"sweep": True}, now=iso(now)
                    )
                    pruned = repo.prune_tombstones(
                        tx, deleted_before=iso(now - dt.timedelta(days=self.config.tombstone_retention_days))
                    )
                if pruned:
                    log.info("pruned tombstones", extra={"count": pruned})
                self.wake(states.EVICT)
            except Exception:
                log.exception("maintenance failed")
            await asyncio.sleep(self.config.maintenance_seconds)


def _expected(exc: Exception) -> bool:
    """Errors from the modules' own taxonomies need no traceback in the log."""
    traits = jobs.error_traits(exc)
    return traits.permanent or traits.retry_after is not None or type(exc).__module__.startswith("noadcast.")


class DataDirLocked(RuntimeError):
    pass


@contextlib.contextmanager
def data_dir_lock(settings: Settings) -> Iterator[None]:
    """One scheduler per data directory. A second server (or a multi-worker
    uvicorn) would race on the job table and break the sync-cursor
    invariant, so it must fail to start instead."""
    path = settings.data_dir / "server.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DataDirLocked(f"another noadcast server holds {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.asynccontextmanager
async def run_pipeline(
    ctx: AppContext, stages: Mapping[str, Stage], config: SchedulerConfig | None = None
) -> AsyncIterator[Scheduler]:
    """Recover after the previous run, start the scheduler, and drain it on
    exit. ``stages`` normally comes from ``pipeline.stages.build_stages(ctx)``."""
    with data_dir_lock(ctx.settings):
        # Older servers deferred a played release while a media job ran.
        # Resolve those rows before lease recovery can requeue or heal them.
        from . import eviction

        for episode in repo.legacy_played_releases(ctx.db):
            await eviction.release_audio(ctx, episode.id, reason="played")
        jobs.recover_leases(ctx.db, ctx.store)
        scheduler = Scheduler(ctx, stages, config or SchedulerConfig.from_settings(ctx.settings))
        ctx.scheduler = scheduler
        scheduler.start()
        try:
            yield scheduler
        finally:
            await scheduler.shutdown()
            ctx.scheduler = None
