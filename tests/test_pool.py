"""TranscriptionPool machinery, exercised with real spawned processes running
``fake.ScriptedEngine`` (no faster-whisper). Each "audio file" is a JSON
script telling the worker what to do; see ScriptedEngine's docstring."""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from noadcast.config import Settings
from noadcast.transcribe.fake import scripted_worker_main
from noadcast.transcribe.pool import PoolConfig, PoolStartError, TranscriptionPool, allocate_affinity
from noadcast.transcribe.protocol import (
    AsrSegmentMeta,
    TranscribeTask,
    TranscriptionError,
    Word,
    WorkerCrashed,
)

MODEL_BYTES = b"not really a model"


def process_exists(pid: int) -> bool:
    """True while ``pid`` is running; a zombie awaiting its reaper counts as gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return stat[stat.rindex(")") + 2] != "Z"


def wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class PoolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="noadcast-pool-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.model = self.tmp / "model"
        self.model.mkdir()
        (self.model / "model.bin").write_bytes(MODEL_BYTES)

    def make_pool(self, workers: int = 2, *, start: bool = True, **overrides) -> TranscriptionPool:
        config = PoolConfig(model_path=self.model, workers=workers, cpu_threads=1, **overrides)
        pool = TranscriptionPool(config, worker_target=scripted_worker_main)
        self.addCleanup(pool.shutdown, 5.0)
        if start:
            pool.start_blocking(timeout=60)
        return pool

    def script(self, name: str, **fields) -> str:
        path = self.tmp / f"{name}.json"
        path.write_text(json.dumps(fields))
        return str(path)

    @staticmethod
    def run_task(pool: TranscriptionPool, task_id: str, path: str, on_progress=None):
        return asyncio.run(pool.transcribe(TranscribeTask(task_id, path), on_progress))


class TranscriptionPoolTests(PoolTestCase):
    def test_starts_without_an_event_loop_and_serves_successive_loops(self) -> None:
        pool = self.make_pool()
        with self.assertRaises(RuntimeError):
            asyncio.get_running_loop()
        for index in range(2):  # each asyncio.run() is a new loop
            result = self.run_task(pool, f"t{index}", self.script(f"a{index}"))
            self.assertEqual(len(result.words), 12)

    def test_transcribe_before_start_is_a_programming_error(self) -> None:
        pool = self.make_pool(start=False)
        with self.assertRaises(RuntimeError):
            self.run_task(pool, "early", self.script("early"))

    def test_result_uses_protocol_types(self) -> None:
        pool = self.make_pool(workers=1)
        result = self.run_task(pool, "t1", self.script("ep", words=10, segments=2, duration=20.0, text="ep"))
        self.assertEqual(result.task_id, "t1")
        self.assertTrue(all(isinstance(word, Word) for word in result.words))
        self.assertEqual([word.word for word in result.words[:2]], [" ep-0", " ep-1"])
        self.assertEqual(sorted({word.segment for word in result.words}), [0, 1])
        self.assertTrue(all(isinstance(segment, AsrSegmentMeta) for segment in result.segments))
        self.assertEqual([segment.index for segment in result.segments], [0, 1])
        self.assertEqual(result.duration_seconds, 20.0)
        self.assertEqual(result.engine, "scripted")
        self.assertEqual(result.model_id, "Systran/faster-whisper-tiny.en")
        self.assertEqual(result.model_sha256, hashlib.sha256(MODEL_BYTES).hexdigest())
        self.assertIn("model_revision", result.options)

    def test_concurrent_tasks_are_correlated_and_capped_at_one_per_worker(self) -> None:
        pool = self.make_pool(workers=2)
        paths = [self.script(f"ep{i}", text=f"ep{i}", segments=3, delay=0.05) for i in range(6)]

        async def main():
            return await asyncio.gather(*(pool.transcribe(TranscribeTask(f"task-{i}", path))
                                          for i, path in enumerate(paths)))

        results = asyncio.run(main())
        for index, result in enumerate(results):
            self.assertEqual(result.task_id, f"task-{index}")
            self.assertTrue(all(word.word.startswith(f" ep{index}-") for word in result.words))
        # Worker-side start/finish times: never more than one task per worker.
        edges = sorted([(r.options["started"], 1) for r in results] + [(r.options["finished"], -1) for r in results])
        running = peak = 0
        for _, delta in edges:
            running += delta
            peak = max(peak, running)
        self.assertEqual(peak, 2)
        self.assertEqual(pool.stats().completed, 6)

    def test_admission_is_fifo(self) -> None:
        pool = self.make_pool(workers=1)
        paths = [self.script(f"f{i}", segments=2, delay=0.02) for i in range(5)]
        finished: list[int] = []
        queued: list[int] = []

        async def one(index: int) -> None:
            await pool.transcribe(TranscribeTask(f"t{index}", paths[index]))
            finished.append(index)

        async def main() -> None:
            jobs = [asyncio.create_task(one(i)) for i in range(5)]
            await asyncio.sleep(0.01)
            queued.append(pool.stats().queued)
            await asyncio.gather(*jobs)

        asyncio.run(main())
        self.assertEqual(queued, [4])
        self.assertEqual(finished, [0, 1, 2, 3, 4])

    def test_progress_is_forwarded_on_the_loop_thread(self) -> None:
        pool = self.make_pool(workers=1, progress_every=32)
        path = self.script("long", words=200, segments=100, duration=1000.0)
        seen = []

        async def main() -> None:
            loop_thread = threading.get_ident()
            await pool.transcribe(TranscribeTask("p1", path),
                                  lambda progress: seen.append((threading.get_ident() == loop_thread, progress)))

        asyncio.run(main())
        self.assertTrue(all(on_loop for on_loop, _ in seen))
        self.assertEqual([p.processed_seconds for _, p in seen], [0.0, 320.0, 640.0, 960.0])
        self.assertTrue(all(p.total_seconds == 1000.0 and p.task_id == "p1" for _, p in seen))

    def test_a_failing_progress_callback_does_not_fail_the_task(self) -> None:
        pool = self.make_pool(workers=1)

        def explode(_progress) -> None:
            raise ValueError("callback bug")

        with self.assertLogs("noadcast.transcribe.pool", "ERROR"):
            result = self.run_task(pool, "cb", self.script("cb"), explode)
        self.assertEqual(len(result.words), 12)

    def test_crash_fails_the_task_respawns_and_later_tasks_succeed(self) -> None:
        pool = self.make_pool(workers=1)
        with self.assertRaises(WorkerCrashed) as caught:
            self.run_task(pool, "c1", self.script("crash", behavior="crash"))
        self.assertFalse(caught.exception.permanent)
        self.assertIn("SIGKILL", str(caught.exception))
        self.assertIn("crash 1 of 4", str(caught.exception))
        result = self.run_task(pool, "ok", self.script("fine"))  # waits for the replacement
        self.assertEqual(len(result.words), 12)
        stats = pool.stats()
        self.assertEqual((stats.crashed, stats.respawns, stats.completed), (1, 1, 1))
        self.assertEqual(stats.per_worker[0].generation, 1)

    def test_fourth_crash_on_one_input_is_permanent_and_success_resets_the_count(self) -> None:
        pool = self.make_pool(workers=2)
        path = self.script("poison", behavior="crash")
        permanence = []
        for index in range(4):
            with self.assertRaises(WorkerCrashed) as caught:
                self.run_task(pool, f"p{index}", path)
            permanence.append(caught.exception.permanent)
        self.assertEqual(permanence, [False, False, False, True])
        self.assertIn("crash 4 of 4", str(caught.exception))

        Path(path).write_text("{}")
        self.run_task(pool, "fixed", path)
        Path(path).write_text(json.dumps({"behavior": "crash"}))
        with self.assertRaises(WorkerCrashed) as caught:
            self.run_task(pool, "again", path)
        self.assertFalse(caught.exception.permanent)
        self.assertIn("crash 1 of 4", str(caught.exception))

    def test_an_unexpected_exit_mid_task_is_a_crash(self) -> None:
        pool = self.make_pool(workers=1)
        with self.assertRaises(WorkerCrashed) as caught:
            self.run_task(pool, "x", self.script("exit", behavior="exit"))
        self.assertIn("exited with status 3", str(caught.exception))

    def test_a_stalled_worker_is_killed_and_replaced(self) -> None:
        pool = self.make_pool(workers=1, stall_timeout=0.5)
        started = time.monotonic()
        with self.assertRaises(WorkerCrashed) as caught:
            self.run_task(pool, "h", self.script("hang", behavior="hang"))
        self.assertIn("stalled", str(caught.exception))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(self.run_task(pool, "after", self.script("fine")).words), 12)

    def test_classified_failures_keep_the_worker(self) -> None:
        pool = self.make_pool(workers=1)
        pid = pool.stats().per_worker[0].pid
        cases = {"error": False, "permanent": True, "undecodable": True, "empty": True}
        for behavior, permanent in cases.items():
            with self.subTest(behavior=behavior), self.assertRaises(TranscriptionError) as caught:
                self.run_task(pool, behavior, self.script(behavior, behavior=behavior))
            self.assertNotIsInstance(caught.exception, WorkerCrashed)
            self.assertEqual(caught.exception.permanent, permanent)
            if behavior == "empty":
                self.assertIn("no words transcribed", str(caught.exception))
        with self.assertRaises(TranscriptionError) as caught:
            self.run_task(pool, "missing", str(self.tmp / "missing.mp3"))
        self.assertFalse(caught.exception.permanent)  # a vanished file is not the content's fault
        stats = pool.stats()
        self.assertEqual((stats.per_worker[0].pid, stats.crashed, stats.failed, stats.respawns), (pid, 0, 5, 0))

    def test_cancelling_a_task_kills_its_worker_without_counting_a_crash(self) -> None:
        pool = self.make_pool(workers=1)
        slow = self.script("slow", segments=50, delay=0.2)  # 10 s of work

        async def main():
            decoded = asyncio.Event()
            job = asyncio.create_task(pool.transcribe(TranscribeTask("slow", slow), lambda _p: decoded.set()))
            await asyncio.wait_for(decoded.wait(), 10)
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job
            return await pool.transcribe(TranscribeTask("next", self.script("next")))

        started = time.monotonic()
        self.assertEqual(len(asyncio.run(main()).words), 12)
        self.assertLess(time.monotonic() - started, 8)  # did not wait out the cancelled task
        stats = pool.stats()
        self.assertEqual((stats.crashed, stats.respawns), (0, 1))

    def test_a_cancelled_waiter_leaves_the_queue(self) -> None:
        pool = self.make_pool(workers=1)

        async def main():
            busy = asyncio.create_task(pool.transcribe(TranscribeTask("busy", self.script("b", segments=5, delay=0.1))))
            waiting = asyncio.create_task(pool.transcribe(TranscribeTask("w", self.script("w"))))
            await asyncio.sleep(0.05)
            self.assertEqual(pool.stats().queued, 1)
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            self.assertEqual(pool.stats().queued, 0)
            await busy
            return await pool.transcribe(TranscribeTask("after", self.script("after")))

        self.assertEqual(len(asyncio.run(main()).words), 12)
        self.assertEqual(pool.stats().respawns, 0)

    def test_stats_report_per_worker_state_and_memory(self) -> None:
        pool = self.make_pool(workers=2)
        stats = pool.stats()
        self.assertEqual((stats.workers, stats.alive, stats.ready, stats.busy, stats.queued), (2, 2, 2, 0, 0))
        for worker in stats.per_worker:
            self.assertEqual((worker.state, worker.generation, worker.task_id), ("idle", 0, None))
            self.assertGreater(worker.rss_mib, 1)
            self.assertGreater(worker.pss_mib, 0)
            self.assertGreaterEqual(worker.peak_rss_mib, worker.rss_mib - 1)
            self.assertGreaterEqual(worker.cpu_seconds, 0)
        self.assertAlmostEqual(stats.rss_mib, sum(worker.rss_mib for worker in stats.per_worker), delta=5)

        async def main():
            job = asyncio.create_task(pool.transcribe(TranscribeTask("s", self.script("s", segments=5, delay=0.1))))
            await asyncio.sleep(0.2)
            during = pool.stats()
            await job
            return during

        during = asyncio.run(main())
        self.assertEqual(during.busy, 1)
        busy = next(worker for worker in during.per_worker if worker.state == "busy")
        self.assertEqual(busy.task_id, "s")
        self.assertGreater(busy.busy_seconds, 0)
        self.assertEqual(pool.stats().per_worker[busy.slot].completed, 1)

    def test_shutdown_is_idempotent_and_leaves_no_processes(self) -> None:
        pool = self.make_pool(workers=2)
        pids = [worker.pid for worker in pool.stats().per_worker]
        pool.shutdown(5)
        pool.shutdown(5)
        self.assertFalse(any(process_exists(pid) for pid in pids))
        self.assertEqual([p for p in multiprocessing.active_children() if p.name.startswith("noadcast-asr")], [])
        self.assertEqual(pool.stats().alive, 0)
        with self.assertRaises(TranscriptionError) as caught:
            self.run_task(pool, "late", self.script("late"))
        self.assertFalse(caught.exception.permanent)

    def test_shutdown_terminates_a_busy_worker_and_fails_pending_tasks(self) -> None:
        pool = self.make_pool(workers=1, stall_timeout=None)
        pid = pool.stats().per_worker[0].pid

        async def main():
            decoded = asyncio.Event()
            hung = asyncio.create_task(pool.transcribe(TranscribeTask("h", self.script("hang", behavior="hang")),
                                                       lambda _p: decoded.set()))
            queued = asyncio.create_task(pool.transcribe(TranscribeTask("q", self.script("q"))))
            await asyncio.wait_for(decoded.wait(), 10)
            started = time.monotonic()
            await asyncio.to_thread(pool.shutdown, 0.2)
            return time.monotonic() - started, await asyncio.gather(hung, queued, return_exceptions=True)

        elapsed, outcomes = asyncio.run(main())
        self.assertLess(elapsed, 5)
        for outcome in outcomes:
            self.assertIsInstance(outcome, TranscriptionError)
            self.assertNotIsInstance(outcome, WorkerCrashed)
            self.assertFalse(outcome.permanent)
        self.assertFalse(process_exists(pid))

    def test_shutdown_from_a_signal_handler_that_interrupted_a_locked_section(self) -> None:
        pool = self.make_pool(workers=1)
        previous = signal.signal(signal.SIGUSR1, lambda *_: pool.shutdown(5))
        self.addCleanup(signal.signal, signal.SIGUSR1, previous)
        with pool._lock:  # a handler interrupting code that holds the pool's lock
            os.kill(os.getpid(), signal.SIGUSR1)
            time.sleep(0.05)  # let the handler run in this (main) thread
        pool.shutdown(5)
        self.assertEqual(pool.stats().alive, 0)

    def test_shutdown_while_start_is_still_preparing_leaves_nothing_behind(self) -> None:
        from unittest import mock

        from noadcast.transcribe import pool as pool_module

        pool = self.make_pool(start=False)
        real = pool_module.model_metadata

        def slow_metadata(path):
            time.sleep(0.3)  # start_blocking is hashing the model when shutdown() arrives
            return real(path)

        stopper = threading.Timer(0.1, pool.shutdown, (5.0,))
        with mock.patch.object(pool_module, "model_metadata", slow_metadata):
            stopper.start()
            with self.assertRaisesRegex(PoolStartError, "shut down during startup"):
                pool.start_blocking(timeout=10)
        stopper.join()
        self.assertEqual(pool.stats().per_worker, ())
        self.assertEqual([p for p in multiprocessing.active_children() if p.name.startswith("noadcast-asr")], [])

    def test_startup_failure_raises_with_the_worker_traceback(self) -> None:
        (self.model / "startup.json").write_text(json.dumps({"fail": True}))
        pool = self.make_pool(start=False)
        with self.assertRaises(PoolStartError) as caught:
            pool.start_blocking(timeout=30)
        self.assertIn("scripted startup failure", str(caught.exception))
        self.assertIn("Traceback", str(caught.exception))
        self.assertTrue(wait_until(lambda: pool.stats().alive == 0))

    def test_a_missing_model_fails_fast(self) -> None:
        (self.model / "model.bin").unlink()
        pool = self.make_pool(start=False)
        with self.assertRaises(PoolStartError) as caught:
            pool.start_blocking()
        self.assertIn("model.bin", str(caught.exception))

    def test_start_times_out_and_cleans_up(self) -> None:
        (self.model / "startup.json").write_text(json.dumps({"delay": 30}))
        pool = self.make_pool(workers=1, start=False)
        started = time.monotonic()
        with self.assertRaises(PoolStartError) as caught:
            pool.start_blocking(timeout=0.5)
        self.assertIn("not ready", str(caught.exception))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(pool.stats().alive, 0)

    def test_a_replacement_that_fails_to_start_is_retried_with_backoff(self) -> None:
        (self.model / "startup.json").write_text(json.dumps({"fail_generations": [1]}))
        pool = self.make_pool(workers=1)
        with self.assertLogs("noadcast.transcribe.pool", "ERROR") as logs:
            with self.assertRaises(WorkerCrashed):
                self.run_task(pool, "c", self.script("crash", behavior="crash"))
            started = time.monotonic()
            result = self.run_task(pool, "ok", self.script("ok"))  # served by generation 2
        self.assertEqual(len(result.words), 12)
        self.assertGreaterEqual(time.monotonic() - started, 0.9)  # the 1 s backoff after a failed start
        self.assertTrue(any("failed during startup" in line for line in logs.output))
        stats = pool.stats()
        self.assertEqual((stats.per_worker[0].generation, stats.respawns, stats.crashed), (2, 2, 2))

    def test_pinned_workers_get_disjoint_physical_cores(self) -> None:
        try:
            expected = allocate_affinity(2, 1)
        except ValueError:
            self.skipTest("not enough physical cores to pin")
        pool = self.make_pool(workers=2, pin_workers=True)
        for worker in pool.stats().per_worker:
            self.assertEqual(sorted(os.sched_getaffinity(worker.pid)), expected[worker.slot])
        self.assertTrue(set(expected[0]).isdisjoint(expected[1]))

    def test_config_rejects_settings_the_service_cannot_honour(self) -> None:
        with self.assertRaises(ValueError):
            PoolConfig(model_path=self.model, word_timestamps=False)
        with self.assertRaises(ValueError):
            PoolConfig(model_path=self.model, workers=0)
        with self.assertRaises(ValueError):
            PoolConfig(model_path=self.model, device="rocm")

    def test_settings_default_to_gpu_and_pick_compute_type_per_device(self) -> None:
        base = {"NOADCAST_ALLOW_NO_AUTH": "1", "NOADCAST_DATA_DIR": str(self.model.parent)}
        gpu = PoolConfig.from_settings(Settings.from_mapping(base))
        self.assertEqual((gpu.device, gpu.compute_type, gpu.workers, gpu.batch_size), ("cuda", "float16", 4, 32))
        cpu = PoolConfig.from_settings(Settings.from_mapping({**base, "NOADCAST_ASR_DEVICE": "cpu"}))
        self.assertEqual((cpu.device, cpu.compute_type), ("cpu", "int8"))
        pinned = PoolConfig.from_settings(Settings.from_mapping({**base, "NOADCAST_ASR_COMPUTE_TYPE": "int8_float16"}))
        self.assertEqual(pinned.compute_type, "int8_float16")
        with self.assertRaises(ValueError):
            Settings.from_mapping({**base, "NOADCAST_ASR_DEVICE": "gpu"})


class ParentExitTests(PoolTestCase):
    """However the server process ends, its workers must not outlive it."""

    CHILD = textwrap.dedent("""
        import asyncio, json, os, signal, sys
        from pathlib import Path
        from noadcast.transcribe.fake import scripted_worker_main
        from noadcast.transcribe.pool import PoolConfig, TranscriptionPool
        from noadcast.transcribe.protocol import TranscribeTask

        model, script, mode = sys.argv[1:4]
        pool = TranscriptionPool(PoolConfig(model_path=Path(model), workers=2, cpu_threads=1),
                                 worker_target=scripted_worker_main)
        pool.start_blocking(timeout=60)

        async def main():
            decoded = asyncio.Event()
            job = asyncio.ensure_future(pool.transcribe(TranscribeTask("h", script), lambda _p: decoded.set()))
            await decoded.wait()
            print(json.dumps([w.pid for w in pool.stats().per_worker]), flush=True)
            os.kill(os.getpid(), signal.SIGKILL)  # one worker busy (hung), one idle

        if mode == "sigkill":
            asyncio.run(main())
        print(json.dumps([w.pid for w in pool.stats().per_worker]), flush=True)
        # mode "exit": fall off the end without shutdown(); the atexit hook must stop the workers
    """)

    def run_child(self, mode: str) -> list[int]:
        hang = self.script("hang", behavior="hang")
        completed = subprocess.run([sys.executable, "-c", self.CHILD, str(self.model), hang, mode],
                                   capture_output=True, text=True, timeout=60)
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_interpreter_exit_without_shutdown_stops_the_workers(self) -> None:
        pids = self.run_child("exit")
        self.assertTrue(wait_until(lambda: not any(process_exists(pid) for pid in pids), 10), pids)

    def test_a_sigkilled_parent_takes_idle_and_busy_workers_with_it(self) -> None:
        pids = self.run_child("sigkill")
        self.assertTrue(wait_until(lambda: not any(process_exists(pid) for pid in pids), 10), pids)


if __name__ == "__main__":
    unittest.main()
