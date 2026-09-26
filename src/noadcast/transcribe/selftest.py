"""``noadcast pool-selftest``: prove the real transcription pool works on this host.

Starts a TranscriptionPool, transcribes one file, and reports startup,
decode, and transcribe timings, the real-time factor, word counts, the
first and last words, and per-worker memory and CPU. ``kill_worker=True``
then SIGKILLs the busy worker mid-task and checks what the watchdog
promises: the task fails with WorkerCrashed, the slot is respawned (with
warmup) under a new pid, and a retry succeeds with the same words.
``reference=`` compares every word against a recorded transcript:
faster-whisper is deterministic for fixed settings, so any difference is a
regression or a settings drift, and the report says where it starts.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .fake import load_recording
from .pool import PoolConfig, PoolStats, TranscriptionPool
from .protocol import TranscribeProgress, TranscribeResult, TranscribeTask, WorkerCrashed
from .worker import WorkerTarget, worker_main

if TYPE_CHECKING:
    from ..config import Settings

Check = Callable[[str, bool, str], None]


def run_selftest(config: PoolConfig | Settings, audio_path: str | os.PathLike[str], *,
                 kill_worker: bool = False, reference: str | os.PathLike[str] | None = None,
                 worker_target: WorkerTarget = worker_main, kill_after: float = 1.0,
                 timeout: float = 900.0) -> dict[str, Any]:
    """Run the self-test and return a JSON-able report; ``report["ok"]`` is the verdict.

    ``kill_after`` is how long after the first progress message (the file is
    decoded) the busy worker is killed; the input must take longer than that.
    """
    if not isinstance(config, PoolConfig):
        config = PoolConfig.from_settings(config)
    audio = Path(audio_path).resolve()
    if not audio.is_file():
        raise FileNotFoundError(f"no audio file at {audio}")
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    report: dict[str, Any] = {
        "audio_path": str(audio),
        "config": {k: str(v) if isinstance(v, Path) else v for k, v in dataclasses.asdict(config).items()},
        "host": {"cpu_count": os.cpu_count(), "loadavg_start": os.getloadavg()},
    }
    pool = TranscriptionPool(config, worker_target=worker_target)
    pids: set[int] = set()
    try:
        started = time.perf_counter()
        pool.start_blocking(timeout=min(timeout, 600.0))
        stats = pool.stats()
        pids.update(w.pid for w in stats.per_worker if w.pid)
        report["startup"] = {"wall_seconds": time.perf_counter() - started, "model": pool.model_info(),
                             "workers": [_worker_row(w) for w in stats.per_worker]}
        check("all workers ready", stats.ready == config.workers, f"{stats.ready} of {config.workers}")

        first, report["transcription"] = asyncio.run(_transcribe(pool, audio, "selftest", timeout))
        check("transcribed words", bool(first.words), f"{len(first.words)} words")
        if reference is not None:
            report["reference"] = comparison = compare_with_reference(first, reference)
            check("identical to reference", comparison["identical"],
                  "" if comparison["identical"] else f"first difference: {comparison['first_difference']}")
        if kill_worker:
            report["kill"] = asyncio.run(_kill_and_retry(pool, audio, first, check, kill_after, timeout))
            pids.update(w.pid for w in pool.stats().per_worker if w.pid)
        report["final_stats"] = _stats_row(pool.stats())
    finally:
        stopping = time.perf_counter()
        pool.shutdown()
        orphans = sorted(pid for pid in pids if _exists(pid))
        report["shutdown"] = {"wall_seconds": time.perf_counter() - stopping, "orphans": orphans}
    check("no worker processes left after shutdown", not orphans, str(orphans or ""))
    report["host"]["loadavg_end"] = os.getloadavg()
    report["checks"] = checks
    report["ok"] = all(item["ok"] for item in checks)
    return report


def compare_with_reference(result: TranscribeResult, reference: str | os.PathLike[str]) -> dict[str, Any]:
    """Word-by-word comparison (start, end, text, probability, segment) with a
    recorded transcript; probabilities are compared exactly too."""
    recorded = load_recording(reference)
    ours = [(w.start, w.end, w.word, w.probability, w.segment) for w in result.words]
    theirs = [(w.start, w.end, w.word, w.probability, w.segment) for w in recorded.words]
    first = next((i for i, (a, b) in enumerate(zip(ours, theirs)) if a != b), None)
    if first is None and len(ours) != len(theirs):
        first = min(len(ours), len(theirs))
    segments_ours = [dataclasses.astuple(s) for s in result.segments]
    segments_theirs = [dataclasses.astuple(s) for s in recorded.segments]
    aligned = len(ours) == len(theirs) and all(a[:3] == b[:3] for a, b in zip(ours, theirs))
    return {
        "reference": str(reference),
        "word_count": [len(ours), len(theirs)],
        "identical": first is None and segments_ours == segments_theirs,
        "text_identical": "".join(w[2] for w in ours).strip() == "".join(w[2] for w in theirs).strip(),
        "word_timings_identical": aligned,
        "max_probability_difference": max((abs(a[3] - b[3]) for a, b in zip(ours, theirs)), default=0.0)
        if aligned else None,
        "segments_identical": segments_ours == segments_theirs,
        "segment_count": [len(segments_ours), len(segments_theirs)],
        "duration_seconds": [result.duration_seconds, recorded.duration_seconds],
        "first_difference": None if first is None else {
            "index": first,
            "ours": ours[first] if first < len(ours) else None,
            "reference": theirs[first] if first < len(theirs) else None,
        },
    }


async def _transcribe(pool: TranscriptionPool, audio: Path, task_id: str,
                      timeout: float) -> tuple[TranscribeResult, dict[str, Any]]:
    events: list[TranscribeProgress] = []
    before = {w.slot: w for w in pool.stats().per_worker}
    started = time.perf_counter()
    result = await asyncio.wait_for(pool.transcribe(TranscribeTask(task_id, str(audio)), events.append), timeout)
    wall = time.perf_counter() - started
    after = pool.stats()
    worker = next((w for w in after.per_worker if w.slot in before and w.completed > before[w.slot].completed
                   and w.pid == before[w.slot].pid), None)
    row: dict[str, Any] = {
        "task_id": task_id,
        "duration_seconds": result.duration_seconds,
        "speech_seconds_after_vad": result.duration_after_vad,
        "wall_seconds": wall,
        "decode_seconds": result.decode_seconds,
        "transcribe_seconds": result.transcribe_seconds,
        "rtf": result.transcribe_seconds / result.duration_seconds,
        "speed_x": result.duration_seconds / result.transcribe_seconds,
        "wall_rtf": wall / result.duration_seconds,
        "words": len(result.words),
        "asr_segments": len(result.segments),
        "progress_events": len(events),
        "first_words": [[w.start, w.end, w.word] for w in result.words[:5]],
        "last_words": [[w.start, w.end, w.word] for w in result.words[-5:]],
        "model_sha256": result.model_sha256,
        "options": result.options,
    }
    if worker is not None:
        previous = before[worker.slot]
        row["worker"] = {**_worker_row(worker), "cpu_seconds_for_task":
                         None if worker.cpu_seconds is None or previous.cpu_seconds is None
                         else worker.cpu_seconds - previous.cpu_seconds}
    return result, row


async def _kill_and_retry(pool: TranscriptionPool, audio: Path, first: TranscribeResult, check: Check,
                          kill_after: float, timeout: float) -> dict[str, Any]:
    decoded = asyncio.Event()
    task = TranscribeTask(f"selftest-kill-{os.getpid()}", str(audio))
    job = asyncio.ensure_future(pool.transcribe(task, lambda _progress: decoded.set()))
    await asyncio.wait_for(decoded.wait(), timeout)
    await asyncio.sleep(kill_after)
    victim = next((w for w in pool.stats().per_worker if w.task_id == task.task_id), None)
    if victim is None or victim.pid is None or job.done():
        check("killed a busy worker mid-task", False, "the task finished first; use a longer input")
        await asyncio.gather(job, return_exceptions=True)
        return {}
    os.kill(victim.pid, signal.SIGKILL)
    killed = time.perf_counter()
    crash: BaseException | None = None
    try:
        await asyncio.wait_for(job, timeout)
    except Exception as error:  # any other failure is a failed check, not a crash of the self-test
        crash = error
    detected = time.perf_counter() - killed
    check("killed task failed with WorkerCrashed (transient)",
          isinstance(crash, WorkerCrashed) and not crash.permanent, repr(crash))

    replacement = None
    while time.perf_counter() - killed < timeout:
        current = pool.stats().per_worker[victim.slot]
        if current.pid != victim.pid and current.state in ("idle", "busy"):
            replacement = current
            break
        await asyncio.sleep(0.05)
    respawned = time.perf_counter() - killed
    check("slot respawned under a new pid", replacement is not None,
          f"pid {victim.pid} -> {replacement.pid if replacement else None}")

    retry, retry_row = await _transcribe(pool, audio, f"selftest-retry-{os.getpid()}", timeout)
    same = [dataclasses.astuple(w) for w in retry.words] == [dataclasses.astuple(w) for w in first.words]
    check("retry succeeded", bool(retry.words), f"{len(retry.words)} words")
    check("retry reproduced the first run's words exactly", same)
    return {
        "victim": {"slot": victim.slot, "pid": victim.pid, "generation": victim.generation},
        "killed_seconds_after_decode": kill_after,
        "crash_detected_seconds": detected,
        "crash": repr(crash),
        "respawned_seconds_after_kill": respawned,
        "replacement": _worker_row(replacement) if replacement else None,
        "retry": retry_row,
        "retry_identical_to_first": same,
        "stats": _stats_row(pool.stats()),
    }


def _worker_row(worker: Any) -> dict[str, Any]:
    return {key: getattr(worker, key) for key in (
        "slot", "pid", "state", "generation", "completed", "model_load_seconds", "warmup_seconds",
        "rss_mib", "pss_mib", "peak_rss_mib", "cpu_seconds")}


def _stats_row(stats: PoolStats) -> dict[str, Any]:
    row = dataclasses.asdict(stats)
    row["per_worker"] = [_worker_row(w) for w in stats.per_worker]
    return row


def _exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
