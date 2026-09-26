"""Long-lived faster-whisper process pool.

Construct and ``start_blocking()`` it *before* uvicorn starts; the web app
only attaches to the running pool. Workers are started with ``spawn``:
forking a process that already has an event loop, a listening socket, and
thread-pool state would copy the listener into every child and duplicate
locks held by threads that do not exist there. A fresh interpreter is also
what lets each worker set its thread-count environment before importing
faster-whisper (see worker.py).

How the parent side works:

- Every worker has its own task pipe and result pipe. A shared ``mp.Queue``
  holds a cross-process lock while reading and writing, so one worker
  SIGKILLed at the wrong moment (a busy worker is the OOM killer's favourite
  target) could wedge or tear the channel for every other worker.
- One daemon thread (the supervisor) waits on all result pipes *and* process
  sentinels with ``multiprocessing.connection.wait``, so a death is seen the
  moment it happens. It resolves each task's future with
  ``call_soon_threadsafe`` on the loop that awaits it; the pool binds to no
  loop until ``transcribe()`` runs in one.
- Admission is one task per worker, strictly FIFO: a freed worker is handed
  to the oldest waiter directly, so a newcomer can never overtake it.
- A worker that dies mid-task fails that task with ``WorkerCrashed`` and is
  replaced (with warmup). Crashes are counted per audio path and the
  ``crash_limit``-th on one input is permanent: that is OOM or a file that
  reliably kills the decoder, and the job would otherwise crash-loop.
"""

from __future__ import annotations

import asyncio
import atexit
import collections
import logging
import multiprocessing as mp
import multiprocessing.connection as mpc
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..logging_setup import current_context, log_context
from .models import model_metadata
from .protocol import (
    AsrSegmentMeta,
    ProgressCallback,
    TranscribeProgress,
    TranscribeResult,
    TranscribeTask,
    TranscriptionError,
    Word,
    WorkerCrashed,
)
from .worker import TaskRequest, WorkerSpec, WorkerTarget, worker_main

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger(__name__)

RESPAWN_BACKOFF_MAX = 60.0  # seconds between attempts for a slot that keeps failing to start
AT_EXIT_TIMEOUT = 10.0
_CANCELLED = "was killed because its task was cancelled"


@dataclass(frozen=True)
class PoolConfig:
    model_path: Path
    model_id: str = "Systran/faster-whisper-tiny.en"
    workers: int = 6
    cpu_threads: int = 2
    device: str = "cpu"  # the server's Settings default to cuda
    compute_type: str = "int8"
    batch_size: int = 8
    beam_size: int = 5
    language: str | None = "en"
    word_timestamps: bool = True
    pin_workers: bool = False
    warmup_audio: Path | None = None  # None: 10 s of seeded noise (worker.WhisperEngine._run_warmup)
    # Pool mechanics rather than ASR settings.
    crash_limit: int = 4  # the crash_limit-th worker crash on one input is permanent
    # Kill a busy worker that sends nothing for this long (None disables). It
    # must cover decode + VAD + the first 32 segments of the longest episode
    # under load: ~2 min for 3 h of audio at full speed.
    stall_timeout: float | None = 600.0
    startup_timeout: float = 180.0  # a replacement worker must be ready within this
    progress_every: int = 32  # ASR segments between progress messages

    def __post_init__(self) -> None:
        for name in ("workers", "cpu_threads", "batch_size", "beam_size", "crash_limit", "progress_every"):
            if getattr(self, name) < 1:
                raise ValueError(f"PoolConfig.{name} must be positive")
        if self.device not in ("cpu", "cuda"):
            raise ValueError(f"PoolConfig.device must be cpu or cuda, not {self.device!r}")
        if not self.word_timestamps:
            # The result's only text is its words; without them every episode
            # would fail as "no words transcribed".
            raise ValueError("the transcription service requires word_timestamps=True")
        if self.stall_timeout is not None and self.stall_timeout <= 0:
            raise ValueError("PoolConfig.stall_timeout must be positive or None")

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> PoolConfig:
        values: dict[str, Any] = {
            "model_path": settings.asr_model_dir,
            "model_id": settings.asr_model_id,
            "workers": settings.pool_workers,
            "cpu_threads": settings.pool_threads,
            "device": settings.asr_device,
            "compute_type": settings.asr_compute_type or ("float16" if settings.asr_device == "cuda" else "int8"),
            "batch_size": settings.asr_batch_size,
            "beam_size": settings.asr_beam_size,
            "language": settings.asr_language,
            "word_timestamps": settings.word_timestamps,
            "pin_workers": settings.pool_pin,
        }
        values.update(overrides)
        return cls(**values)


@dataclass(frozen=True)
class WorkerStats:
    slot: int
    pid: int | None
    state: str  # starting | idle | busy | dying | dead
    generation: int  # respawns of this slot so far
    completed: int
    task_id: str | None
    busy_seconds: float | None
    model_load_seconds: float | None
    warmup_seconds: float | None
    rss_mib: float | None  # read on demand from /proc/PID/smaps_rollup
    pss_mib: float | None
    peak_rss_mib: float | None  # VmHWM
    cpu_seconds: float | None  # user + system since the process started


@dataclass(frozen=True)
class PoolStats:
    workers: int
    alive: int
    ready: int
    busy: int
    completed: int
    crashed: int
    rss_mib: float | None  # sum over live workers; the parent is excluded
    pss_mib: float | None = None
    queued: int = 0  # transcribe() calls waiting for a free worker
    failed: int = 0  # tasks the worker reported as failed (not crashes)
    respawns: int = 0
    per_worker: tuple[WorkerStats, ...] = ()


class PoolStartError(RuntimeError):
    """The pool could not get every worker ready; nothing is left running."""


@dataclass(frozen=True)
class ProcessSample:
    rss_mib: float | None
    pss_mib: float | None
    peak_rss_mib: float | None
    cpu_seconds: float | None


_CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def sample_process(pid: int) -> ProcessSample | None:
    """RSS/PSS from ``/proc/PID/smaps_rollup`` (read as the benchmark's
    MemorySampler reads it), peak RSS, and CPU time. None once the process is gone."""
    try:
        memory = {}
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            parts = line.split()
            if parts and parts[0] in ("Rss:", "Pss:"):
                memory[parts[0]] = int(parts[1]) / 1024
        peak = next((int(line.split()[1]) / 1024 for line in Path(f"/proc/{pid}/status").read_text().splitlines()
                     if line.startswith("VmHWM:")), None)
        stat = Path(f"/proc/{pid}/stat").read_text()
        after_comm = stat[stat.rindex(")") + 2:].split()  # comm may contain spaces
        cpu = (int(after_comm[11]) + int(after_comm[12])) / _CLOCK_TICKS
    except (OSError, ValueError, IndexError):
        return None
    return ProcessSample(memory.get("Rss:"), memory.get("Pss:"), peak, cpu)


def _command_output(command: list[str]) -> str | None:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def physical_cpu_groups(allowed: set[int]) -> list[list[int]]:
    """Return sibling groups, retaining only CPUs allowed by this process."""
    output = _command_output(["lscpu", "-p=CPU,CORE,SOCKET"])
    groups: dict[tuple[int, int], list[int]] = {}
    for line in (output or "").splitlines():
        if line.startswith("#"):
            continue
        try:
            cpu, core, socket = (int(item) for item in line.split(",")[:3])
        except ValueError:  # lscpu leaves CORE empty on some virtual machines
            return [[cpu] for cpu in sorted(allowed)]
        if cpu in allowed:
            groups.setdefault((socket, core), []).append(cpu)
    if not groups:
        return [[cpu] for cpu in sorted(allowed)]
    return [sorted(cpus) for _, cpus in sorted(groups.items())]


def allocate_affinity(workers: int, threads: int) -> list[list[int]]:
    """Disjoint CPU lists, ``threads`` physical cores per worker."""
    allowed = set(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else set(range(os.cpu_count() or 1))
    groups = physical_cpu_groups(allowed)
    required = workers * threads
    if len(groups) < required:
        raise ValueError(f"pinning {workers} workers x {threads} threads needs {required} physical cores, "
                         f"but affinity exposes only {len(groups)}")
    # Select one logical CPU per physical core to avoid accidental SMT oversubscription.
    cpus = [group[0] for group in groups[:required]]
    return [cpus[index * threads: (index + 1) * threads] for index in range(workers)]


def _describe_exit(exitcode: int | None) -> str:
    if exitcode is None:
        return "exited (status unknown)"
    if exitcode < 0:
        try:
            return f"was killed by {signal.Signals(-exitcode).name}"
        except ValueError:
            return f"was killed by signal {-exitcode}"
    return f"exited with status {exitcode}"


def _settle(future: asyncio.Future[Any], outcome: Any) -> None:
    if future.done():
        return
    if isinstance(outcome, BaseException):
        future.set_exception(outcome)
    else:
        future.set_result(outcome)


@dataclass(eq=False)
class _InFlight:
    task: TranscribeTask
    request: TaskRequest
    future: asyncio.Future[TranscribeResult]
    loop: asyncio.AbstractEventLoop
    on_progress: ProgressCallback | None
    log_ctx: dict[str, Any]


@dataclass(eq=False)
class _Worker:
    slot: int
    generation: int
    process: mp.process.BaseProcess
    tasks: mpc.Connection  # the parent's write end
    results: mpc.Connection  # the parent's read end
    pid: int | None
    spawned_at: float
    # starting -> idle <-> reserved (handed to a waiter) -> busy -> idle ...;
    # dying once the pool has killed it; dead once reaped.
    state: str = "starting"
    ready_at: float | None = None
    ready_info: dict[str, Any] = field(default_factory=dict)
    inflight: _InFlight | None = None
    busy_since: float = 0.0
    last_activity: float = 0.0
    results_open: bool = True
    completed: int = 0
    kill_reason: str | None = None


@dataclass(eq=False)
class _Waiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[_Worker]


class TranscriptionPool:
    """Implements ``protocol.Transcriber`` over a pool of spawned worker processes.

    ``worker_target`` is the child entry point; tests inject
    ``fake.scripted_worker_main`` to run all of this machinery without
    faster-whisper.
    """

    def __init__(self, config: PoolConfig, *, worker_target: WorkerTarget = worker_main) -> None:
        self.config = config
        self._target = worker_target
        self._ctx = mp.get_context("spawn")
        # Re-entrant so shutdown() can run from a signal handler that
        # interrupted this thread while it held the lock.
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._slots: list[_Worker | None] = [None] * config.workers
        self._affinity: list[tuple[int, ...] | None] = [None] * config.workers
        self._idle: collections.deque[_Worker] = collections.deque()
        self._waiters: collections.deque[_Waiter] = collections.deque()
        self._respawn_at: dict[int, float] = {}
        self._start_failures: dict[int, int] = {}
        self._crashes_by_path: dict[str, int] = {}
        self._model: dict[str, Any] = {}
        self._started = False
        self._running = False  # every initial worker became ready; respawns allowed
        self._closed = False
        self._startup_error: str | None = None
        self._completed = self._failed = self._crashed = self._respawns = 0
        self._warned_numpy = False
        self._supervisor: threading.Thread | None = None
        self._wake_fds: tuple[int, int] | None = None
        self._shutdown_owner: int | None = None
        self._shutdown_done = threading.Event()

    # -- lifecycle -------------------------------------------------------------

    def start_blocking(self, timeout: float = 180.0) -> None:
        """Spawn every worker and wait until each has loaded the model and warmed up.

        Needs no event loop. On failure or timeout everything is shut down and
        ``PoolStartError`` carries the first worker traceback.
        """
        with self._lock:
            if self._started:
                raise RuntimeError("transcription pool already started")
            self._started = True
        try:
            self._model = model_metadata(self.config.model_path)
            if self.config.pin_workers:
                self._affinity = [tuple(cpus) for cpus in allocate_affinity(self.config.workers, self.config.cpu_threads)]
        except (OSError, ValueError) as error:
            self.shutdown(timeout=0)
            raise PoolStartError(str(error)) from error
        if self._model.get("model_id") not in (None, self.config.model_id):
            log.warning("model at %s was installed as %s but is configured as %s",
                        self.config.model_path, self._model["model_id"], self.config.model_id)

        started = time.monotonic()
        spawn_error: Exception | None = None
        # One critical section, so a concurrent shutdown() either runs before
        # anything exists or sees every worker and the supervisor.
        with self._lock:
            if self._closed:
                raise PoolStartError("shut down during startup")
            read_fd, write_fd = os.pipe()
            os.set_blocking(read_fd, False)
            os.set_blocking(write_fd, False)
            self._wake_fds = (read_fd, write_fd)
            try:
                for slot in range(self.config.workers):
                    self._slots[slot] = self._spawn(slot, generation=0)
            except Exception as error:
                spawn_error = error
            self._supervisor = threading.Thread(target=self._supervise, name="transcription-pool", daemon=True)
            self._supervisor.start()
        if spawn_error is not None:
            self.shutdown(timeout=0)
            raise PoolStartError(f"could not spawn transcription workers: {spawn_error}") from spawn_error
        atexit.register(self._shutdown_at_exit)

        deadline = started + timeout
        with self._lock:
            while not (self._startup_error or self._closed):
                if all(w is not None and w.ready_at is not None for w in self._slots):
                    self._running = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
            waiting = sum(1 for w in self._slots if w is None or w.ready_at is None)
            error = self._startup_error or ("shut down during startup" if self._closed else
                                            f"{waiting} worker(s) not ready after {timeout:.0f}s")
        if not self._running:
            self.shutdown(timeout=0)  # nothing to preserve in a failed start
            raise PoolStartError(error)
        log.info("transcription pool ready: %d workers x %d threads on %s %s in %.1fs (model %s, sha256 %s)",
                 self.config.workers, self.config.cpu_threads, self.config.device, self.config.compute_type,
                 time.monotonic() - started, self.config.model_id, self._model["model_bin_sha256"][:12])

    def shutdown(self, timeout: float = 60.0) -> None:
        """Stop every worker and fail whatever is still pending.

        Idle workers exit on their ``None`` sentinel at once; a busy worker
        exits after its current task, so ``timeout`` bounds how long in-flight
        work may finish before stragglers are terminated (their tasks fail with
        a transient ``TranscriptionError``). Idempotent, never needs the event
        loop, and safe from an atexit hook or a signal handler.
        """
        me = threading.get_ident()
        with self._lock:
            owner = self._shutdown_owner
            if owner is None:
                self._shutdown_owner = me
        if owner is not None:
            if owner != me:  # another thread is shutting down; a re-entrant signal handler must not wait on itself
                self._shutdown_done.wait(timeout)
            return
        try:
            self._shutdown(timeout)
        finally:
            self._shutdown_done.set()

    def _shutdown_at_exit(self) -> None:
        self.shutdown(timeout=AT_EXIT_TIMEOUT)

    def _shutdown(self, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            self._closed = True
            self._respawn_at.clear()
            waiters = list(self._waiters)
            self._waiters.clear()
            workers = [w for w in self._slots if w is not None and w.state != "dead"]
            for w in workers:
                try:
                    w.tasks.send(None)
                except (OSError, ValueError):
                    pass
            self._changed.notify_all()
        for waiter in waiters:
            self._call_soon(waiter.loop, _settle, waiter.future, TranscriptionError("transcription pool is shut down"))

        if self._supervisor is not None:
            self._wake()
            for escalate in ("terminate", "kill", None):
                with self._lock:
                    self._changed.wait_for(lambda: all(w.state == "dead" for w in workers),
                                           max(0.0, deadline - time.monotonic()))
                    stragglers = [w for w in workers if w.state != "dead"]
                    if not stragglers or escalate is None:
                        break
                    log.warning("%s %d transcription worker(s) still running at shutdown",
                                "terminating" if escalate == "terminate" else "killing", len(stragglers))
                    for w in stragglers:
                        getattr(w.process, escalate)()
                deadline = time.monotonic() + 2.0
            self._wake()
            self._supervisor.join(timeout=5.0)

        # The supervisor has normally reaped, settled, and closed everything by
        # now; anything left means it is wedged.
        for w in workers:
            with self._lock:
                if w.state == "dead":
                    continue
                inflight, w.inflight = w.inflight, None
                w.state = "dead"
            if inflight is not None:
                self._resolve(inflight, TranscriptionError("transcription pool shut down while this task was running"))
            w.process.join(timeout=max(0.0, deadline - time.monotonic()))
            if w.process.exitcode is None:
                w.process.kill()
                w.process.join(timeout=2.0)
            self._close_worker(w)
        if self._wake_fds:
            for fd in self._wake_fds:
                os.close(fd)
            self._wake_fds = None
        atexit.unregister(self._shutdown_at_exit)
        if self._started:
            log.info("transcription pool shut down")

    # -- transcription ----------------------------------------------------------

    async def transcribe(self, task: TranscribeTask, on_progress: ProgressCallback | None = None) -> TranscribeResult:
        """Run one task on the next free worker (FIFO) and return its words.

        ``on_progress`` is called on the event loop thread. Cancelling the
        caller kills the worker (it is respawned, not counted as a crash)
        rather than letting it grind through minutes of discarded work.
        """
        loop = asyncio.get_running_loop()
        request = TaskRequest(task.task_id, os.path.abspath(task.audio_path), task.language or self.config.language)
        inflight = _InFlight(task, request, loop.create_future(), loop, on_progress, current_context())
        front = False
        while not self._dispatch(await self._acquire(loop, front=front), inflight):
            front = True  # the worker died after being handed over; keep our place in line
        try:
            return await inflight.future
        except asyncio.CancelledError:
            with self._lock:
                for w in self._slots:
                    if w is not None and w.inflight is inflight and w.state == "busy":
                        self._kill(w, _CANCELLED)
            raise

    async def _acquire(self, loop: asyncio.AbstractEventLoop, *, front: bool) -> _Worker:
        with self._lock:
            if not self._started:
                raise RuntimeError("TranscriptionPool.start_blocking() has not been called")
            if self._closed:
                raise TranscriptionError("transcription pool is shut down")
            if self._idle and (front or not self._waiters):
                w = self._idle.popleft()
                w.state = "reserved"
                return w
            waiter = _Waiter(loop, loop.create_future())
            if front:
                self._waiters.appendleft(waiter)
            else:
                self._waiters.append(waiter)
        try:
            return await waiter.future
        except BaseException:
            with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                handed = waiter.future
                if handed.done() and not handed.cancelled() and handed.exception() is None:
                    self._release(handed.result())  # a worker arrived as we gave up
            raise

    def _dispatch(self, w: _Worker, inflight: _InFlight) -> bool:
        with self._lock:
            if w.state != "reserved":
                return False  # it died, or was killed, after being handed over
            if self._closed:
                w.state = "idle"
                raise TranscriptionError("transcription pool is shut down")
            w.inflight = inflight
            w.state = "busy"
            w.busy_since = w.last_activity = time.monotonic()
            try:
                w.tasks.send(inflight.request)
            except (OSError, ValueError):
                w.inflight = None
                self._kill(w, "could not be sent a task and was killed")
                return False
        self._wake()  # the supervisor must start this worker's stall timer
        return True

    def _release(self, w: _Worker) -> None:
        """Hand a free worker to the oldest live waiter, else park it. Lock held."""
        if w.state in ("dying", "dead"):
            return
        if not self._closed:
            while self._waiters:
                waiter = self._waiters.popleft()
                if waiter.future.done():
                    continue
                w.state = "reserved"
                if self._call_soon(waiter.loop, self._hand_over, waiter, w):
                    return
        w.state = "idle"
        if not self._closed:
            self._idle.append(w)

    def _hand_over(self, waiter: _Waiter, w: _Worker) -> None:
        """On the waiter's loop thread."""
        if waiter.future.done():  # it gave up while the hand-off was queued
            with self._lock:
                if w.state == "reserved":
                    self._release(w)
            return
        waiter.future.set_result(w)

    @staticmethod
    def _call_soon(loop: asyncio.AbstractEventLoop, callback: Callable[..., None], *args: Any) -> bool:
        try:
            loop.call_soon_threadsafe(callback, *args)
            return True
        except RuntimeError:  # the loop is closed: nobody is waiting any more
            return False

    def _resolve(self, inflight: _InFlight, outcome: TranscribeResult | BaseException) -> None:
        self._call_soon(inflight.loop, _settle, inflight.future, outcome)

    # -- supervisor thread ------------------------------------------------------

    def _supervise(self) -> None:
        """Route worker messages, notice deaths, run stall/startup/respawn timers."""
        while True:
            with self._lock:
                live = [w for w in self._slots if w is not None and w.state != "dead"]
                if self._closed and not live:
                    return
                waitables: dict[Any, _Worker] = {}
                for w in live:
                    if w.results_open:
                        waitables[w.results] = w
                    waitables[w.process.sentinel] = w
                timeout = self._next_timer(live)
                wake_fd = self._wake_fds[0] if self._wake_fds else None
            try:
                for ready in mpc.wait([wake_fd, *waitables] if wake_fd is not None else list(waitables), timeout):
                    if ready == wake_fd:
                        self._drain_wakeups()
                        continue
                    w = waitables[ready]
                    if ready is w.results:
                        self._read_messages(w)
                    else:
                        self._on_exit(w)
                self._run_timers()
            except Exception:
                log.exception("transcription pool supervisor error")
                time.sleep(0.5)

    def _wake(self) -> None:
        fds = self._wake_fds
        if fds:
            try:
                os.write(fds[1], b"x")
            except OSError:  # full (a wakeup is already pending) or closed
                pass

    def _drain_wakeups(self) -> None:
        fds = self._wake_fds
        try:
            while fds and os.read(fds[0], 4096):
                pass
        except OSError:
            pass

    def _next_timer(self, live: list[_Worker]) -> float | None:
        """Seconds until the next stall/startup/respawn deadline. Lock held."""
        deadlines = list(self._respawn_at.values())
        for w in live:
            if w.kill_reason:
                continue
            if w.state == "busy" and self.config.stall_timeout is not None:
                deadlines.append(w.last_activity + self.config.stall_timeout)
            elif w.state == "starting" and self._running:
                deadlines.append(w.spawned_at + self.config.startup_timeout)
        return max(0.0, min(deadlines) - time.monotonic()) if deadlines else None

    def _run_timers(self) -> None:
        now = time.monotonic()
        with self._lock:
            for w in self._slots:
                if w is None or w.kill_reason:
                    continue
                if (w.state == "busy" and self.config.stall_timeout is not None
                        and now - w.last_activity >= self.config.stall_timeout):
                    self._kill(w, f"sent nothing for {now - w.last_activity:.0f}s and was killed as stalled")
                elif w.state == "starting" and self._running and now - w.spawned_at >= self.config.startup_timeout:
                    self._kill(w, f"was not ready within {self.config.startup_timeout:.0f}s and was killed")
            due = [slot for slot, at in self._respawn_at.items() if at <= now]
            for slot in due:
                del self._respawn_at[slot]
                self._respawn(slot)

    def _read_messages(self, w: _Worker) -> None:
        while w.results_open:
            try:
                if not w.results.poll():
                    return
                message = w.results.recv()
            except (EOFError, OSError):  # dead worker; a torn final message lands here too
                w.results_open = False
                return
            except Exception:  # unpicklable payload from a misbehaving target
                log.exception("unreadable message from transcription worker %d", w.slot)
                w.results_open = False
                with self._lock:
                    self._kill(w, "sent an unreadable message and was killed")
                return
            w.last_activity = time.monotonic()
            kind = message[0]
            if kind == "progress":
                self._on_progress(w, *message[1:])
            elif kind in ("result", "error"):
                self._on_finished(w, message[1], message[2] if kind == "result" else None,
                                  message[2] if kind == "error" else None)
            elif kind == "ready":
                self._on_ready(w, message[1])
            elif kind == "startup_error":
                self._on_startup_error(w, message[1])
            else:
                log.warning("unknown message %r from transcription worker %d", kind, w.slot)

    def _on_ready(self, w: _Worker, info: dict[str, Any]) -> None:
        with self._lock:
            if w.state != "starting":
                return
            w.ready_at = time.monotonic()
            w.ready_info = info
            w.pid = info.get("pid", w.pid)
            self._start_failures.pop(w.slot, None)
            self._release(w)
            self._changed.notify_all()
            warn_numpy = bool(info.get("numpy_preloaded")) and not self._warned_numpy
            self._warned_numpy |= warn_numpy
        log.info("transcription worker %d ready (pid %s, generation %d) in %.2fs: model load %.2fs, warmup %.2fs",
                 w.slot, w.pid, w.generation, w.ready_at - w.spawned_at,
                 info.get("model_load_seconds", 0.0), info.get("warmup_seconds", 0.0))
        if warn_numpy:
            log.warning("numpy was imported in the worker before its thread limits were set: spawn re-runs the "
                        "entry point's module-level imports (%s) in every worker, so keep numpy and faster_whisper "
                        "out of them", getattr(sys.modules.get("__main__"), "__file__", "__main__"))

    def _on_startup_error(self, w: _Worker, error: dict[str, Any]) -> None:
        text = f"transcription worker {w.slot} failed during startup: {error['type']}: {error['message']}"
        with self._lock:
            if not self._running and self._startup_error is None:
                self._startup_error = f"{text}\n{error['traceback']}"
                self._changed.notify_all()
        log.error("%s\n%s", text, error["traceback"])

    def _on_progress(self, w: _Worker, task_id: str, processed: float, total: float) -> None:
        inflight = w.inflight
        if inflight is None or inflight.request.task_id != task_id or inflight.on_progress is None:
            return
        self._call_soon(inflight.loop, self._deliver_progress, inflight, TranscribeProgress(task_id, processed, total))

    @staticmethod
    def _deliver_progress(inflight: _InFlight, progress: TranscribeProgress) -> None:
        """On the task's loop thread."""
        if inflight.future.done() or inflight.on_progress is None:
            return
        try:
            inflight.on_progress(progress)
        except Exception:
            with log_context(**inflight.log_ctx):
                log.exception("transcription progress callback failed for task %s", progress.task_id)

    def _on_finished(self, w: _Worker, task_id: str, payload: dict[str, Any] | None,
                     error: dict[str, Any] | None) -> None:
        with self._lock:
            inflight = w.inflight
            if inflight is None or inflight.request.task_id != task_id:
                log.warning("transcription worker %d reported on unknown task %s", w.slot, task_id)
                return
            w.inflight = None
        outcome: TranscribeResult | TranscriptionError
        if payload is not None:
            try:
                outcome = self._build_result(inflight, payload)
            except TranscriptionError as failure:
                outcome = failure
            except Exception as failure:  # a malformed payload is a worker bug
                outcome = TranscriptionError(f"malformed worker result: {failure!r}")
        else:
            assert error is not None
            outcome = TranscriptionError(f"{error['type']}: {error['message']}", permanent=bool(error["permanent"]))
        with self._lock:
            if isinstance(outcome, TranscribeResult):
                self._completed += 1
                w.completed += 1
                self._crashes_by_path.pop(inflight.request.audio_path, None)
            else:
                self._failed += 1
            self._release(w)
        self._resolve(inflight, outcome)

        with log_context(**inflight.log_ctx):
            if isinstance(outcome, TranscribeResult):
                assert payload is not None
                log.info("transcribed %s: %.0fs of audio in %.1fs (%.1fx realtime), %d words, worker %d, "
                         "peak RSS %.0f MiB", inflight.request.audio_path, outcome.duration_seconds,
                         outcome.transcribe_seconds, outcome.duration_seconds / max(outcome.transcribe_seconds, 1e-9),
                         len(outcome.words), w.slot, payload.get("peak_rss_mib", 0.0),
                         extra={"task_id": task_id, "cpu_seconds": payload.get("cpu_seconds")})
            else:
                log.warning("transcription of %s failed (%s): %s", inflight.request.audio_path,
                            "permanent" if outcome.permanent else "transient", outcome,
                            extra={"task_id": task_id})
                if error is not None and not error["permanent"]:
                    log.debug("worker traceback:\n%s", error["traceback"])

    def _build_result(self, inflight: _InFlight, payload: dict[str, Any]) -> TranscribeResult:
        words = [Word(start, end, text, probability, segment)
                 for start, end, text, probability, segment in payload["words"]]
        if not words:
            raise TranscriptionError(
                f"no words transcribed from {payload['duration_seconds']:.0f}s of audio "
                f"({payload['duration_after_vad']:.0f}s passed the VAD)", permanent=True)
        return TranscribeResult(
            task_id=inflight.task.task_id,
            duration_seconds=payload["duration_seconds"],
            duration_after_vad=payload["duration_after_vad"],
            language=payload["language"],
            language_probability=payload["language_probability"],
            words=words,
            segments=[AsrSegmentMeta(*segment) for segment in payload["segments"]],
            decode_seconds=payload["decode_seconds"],
            transcribe_seconds=payload["transcribe_seconds"],
            engine=payload.get("engine", "faster-whisper"),
            model_id=self.config.model_id,
            model_sha256=self._model.get("model_bin_sha256"),
            options={**payload["options"], "model_revision": self._model.get("revision")},
        )

    def _on_exit(self, w: _Worker) -> None:
        if w.state == "dead":
            return
        self._read_messages(w)  # whatever it sent before dying (even a result) still counts
        w.process.join(timeout=5.0)
        exitcode = w.process.exitcode
        with self._lock:
            if w.state == "dead":
                return
            w.state = "dead"
            w.results_open = False
            if w in self._idle:
                self._idle.remove(w)
            inflight, w.inflight = w.inflight, None
            how = w.kill_reason or _describe_exit(exitcode)
            requested = self._closed or w.kill_reason == _CANCELLED
            if not requested:
                self._crashed += 1
            outcome: TranscriptionError | None = None
            if inflight is not None:
                if self._closed:
                    outcome = TranscriptionError("transcription pool shut down while this task was running")
                elif w.kill_reason != _CANCELLED:
                    path = inflight.request.audio_path
                    count = self._crashes_by_path[path] = self._crashes_by_path.get(path, 0) + 1
                    outcome = WorkerCrashed(
                        f"transcription worker {w.slot} (pid {w.pid}) {how} while transcribing {path} "
                        f"(crash {count} of {self.config.crash_limit} allowed for this input)",
                        permanent=count >= self.config.crash_limit)
            failures = 0
            if w.ready_at is None:
                failures = self._start_failures[w.slot] = self._start_failures.get(w.slot, 0) + 1
            if not self._running and not self._closed:
                # Initial startup: fail start_blocking() rather than respawn.
                self._startup_error = self._startup_error or f"transcription worker {w.slot} {how} during startup"
            elif not self._closed:
                delay = min(RESPAWN_BACKOFF_MAX, 2.0 ** (failures - 1)) if failures else 0.0
                self._respawn_at[w.slot] = time.monotonic() + delay
            self._changed.notify_all()
        self._close_worker(w)
        if inflight is not None:
            with log_context(**inflight.log_ctx):
                if outcome is not None:
                    log.warning("%s", outcome)
            if outcome is not None:
                self._resolve(inflight, outcome)
        elif not requested:
            log.warning("transcription worker %d (pid %s) %s", w.slot, w.pid, how)

    def _kill(self, w: _Worker, reason: str) -> None:
        """Lock held. The sentinel then fires and ``_on_exit`` does the rest."""
        if w.state == "dead" or w.kill_reason:
            return
        w.kill_reason = reason
        w.state = "dying"
        if w in self._idle:
            self._idle.remove(w)
        try:
            w.process.kill()
        except (OSError, ValueError):
            pass

    def _spawn(self, slot: int, generation: int) -> _Worker:
        """Lock held (so shutdown can never miss a worker being born)."""
        config = self.config
        spec = WorkerSpec(
            slot=slot, generation=generation, model_path=str(Path(config.model_path).resolve()),
            cpu_threads=config.cpu_threads, compute_type=config.compute_type, batch_size=config.batch_size,
            beam_size=config.beam_size, language=config.language, word_timestamps=config.word_timestamps,
            warmup_audio=str(Path(config.warmup_audio).resolve()) if config.warmup_audio else None,
            progress_every=config.progress_every, affinity=self._affinity[slot], device=config.device,
        )
        task_reader, task_writer = self._ctx.Pipe(duplex=False)
        result_reader, result_writer = self._ctx.Pipe(duplex=False)
        process = self._ctx.Process(target=self._target, args=(spec, task_reader, result_writer),
                                    name=f"noadcast-asr-{slot}", daemon=True)
        try:
            process.start()
        except BaseException:
            task_writer.close()
            result_reader.close()
            raise
        finally:
            # Keep only the parent's ends: EOF on the result pipe then means the
            # child is gone, and recv() can never block on a torn message.
            task_reader.close()
            result_writer.close()
        now = time.monotonic()
        return _Worker(slot=slot, generation=generation, process=process, tasks=task_writer,
                       results=result_reader, pid=process.pid, spawned_at=now, last_activity=now)

    def _respawn(self, slot: int) -> None:
        """Lock held."""
        if self._closed:
            return
        old = self._slots[slot]
        generation = old.generation + 1 if old is not None else 0
        try:
            self._slots[slot] = self._spawn(slot, generation)
        except Exception:
            log.exception("could not respawn transcription worker %d", slot)
            self._respawn_at[slot] = time.monotonic() + RESPAWN_BACKOFF_MAX
            return
        self._respawns += 1
        log.info("respawning transcription worker %d (generation %d)", slot, generation)

    @staticmethod
    def _close_worker(w: _Worker) -> None:
        for conn in (w.tasks, w.results):
            conn.close()
        try:
            w.process.close()
        except ValueError:  # not reaped; the Process finalizer handles it
            pass

    # -- introspection ---------------------------------------------------------

    def stats(self) -> PoolStats:
        now = time.monotonic()
        with self._lock:
            rows = [(w.slot, w.pid, w.state, w.generation, w.completed,
                     w.inflight.task.task_id if w.inflight else None,
                     now - w.busy_since if w.state == "busy" else None,
                     w.ready_info.get("model_load_seconds"), w.ready_info.get("warmup_seconds"))
                    for w in self._slots if w is not None]
            counters = (self._completed, self._crashed, len(self._waiters), self._failed, self._respawns)
        per_worker = []
        for slot, pid, state, generation, completed, task_id, busy, load, warmup in rows:
            sample = sample_process(pid) if pid is not None and state != "dead" else None
            per_worker.append(WorkerStats(
                slot=slot, pid=pid, state="busy" if state == "reserved" else state, generation=generation,
                completed=completed, task_id=task_id, busy_seconds=busy, model_load_seconds=load,
                warmup_seconds=warmup, rss_mib=sample.rss_mib if sample else None,
                pss_mib=sample.pss_mib if sample else None, peak_rss_mib=sample.peak_rss_mib if sample else None,
                cpu_seconds=sample.cpu_seconds if sample else None,
            ))

        def total(name: str) -> float | None:
            values = [getattr(row, name) for row in per_worker if getattr(row, name) is not None]
            return sum(values) if values else None

        completed, crashed, queued, failed, respawns = counters
        return PoolStats(
            workers=self.config.workers,
            alive=sum(1 for row in per_worker if row.state != "dead"),
            ready=sum(1 for row in per_worker if row.state in ("idle", "busy")),
            busy=sum(1 for row in per_worker if row.state == "busy"),
            completed=completed, crashed=crashed, rss_mib=total("rss_mib"), pss_mib=total("pss_mib"),
            queued=queued, failed=failed, respawns=respawns, per_worker=tuple(per_worker),
        )

    def model_info(self) -> dict[str, Any]:
        """sha256/revision of the loaded model (empty before ``start_blocking``)."""
        return dict(self._model)

