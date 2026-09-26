#!/usr/bin/env python3
"""Parallel parakeet.cpp benchmark over locally prepared TAL PCM files.

Each worker is a pinned process with one persistent model context. Episodes are
split into exact, non-overlapping 30-second chunks and decoded through the C
batch API. Model loading and one 30-second warmup per worker precede the suite
readiness barrier. Nothing is downloaded.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import platform
import queue
import re
import resource
import shlex
import subprocess
import sys
import time
import traceback
import wave

# NumPy is imported while spawned workers import this module, before
# worker_main can set process-local environment. Keep its BLAS helper pool from
# competing with the explicitly sized ggml pool.
os.environ["OPENBLAS_NUM_THREADS"] = "1"
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
TAL_ROOT = ROOT / "benchmarks" / "tal"
DEFAULT_MODEL = TAL_ROOT / "models" / "parakeet-tdt_ctc-110m-q8_0.gguf"
DEFAULT_LIBRARY = ROOT / "vendor" / "parakeet.cpp" / "build-cpu" / "libparakeet.so"
DEFAULT_PROVENANCE = TAL_ROOT / "models" / "parakeet-tdt_ctc-110m-q8_0.provenance.json"
SAMPLE_RATE = 16_000
CHUNK_SECONDS = 30
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_SECONDS
DECODERS = {"default": 0, "ctc": 1, "tdt": 2}
SET_THREADS_SYMBOL = "_ZN2pk15set_num_threadsEi"


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def save_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(value)
    temporary.replace(path)


def sha256_file(path: Path, block_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def command_output(command: list[str]) -> str | None:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def physical_cpu_groups(allowed: set[int]) -> list[list[int]]:
    output = command_output(["lscpu", "-p=CPU,CORE,SOCKET"])
    groups: dict[tuple[int, int], list[int]] = {}
    if output:
        for line in output.splitlines():
            if line.startswith("#"):
                continue
            cpu, core, socket = (int(item) for item in line.split(",")[:3])
            if cpu in allowed:
                groups.setdefault((socket, core), []).append(cpu)
    return [sorted(cpus) for _, cpus in sorted(groups.items())] or [[cpu] for cpu in sorted(allowed)]


def allocate_affinity(workers: int, threads: int, pin_workers: bool) -> list[list[int] | None]:
    if not pin_workers:
        return [None] * workers
    allowed = set(os.sched_getaffinity(0))
    groups = physical_cpu_groups(allowed)
    if workers * threads > len(groups):
        raise ValueError(f"workers * threads ({workers * threads}) exceeds {len(groups)} available physical cores")
    # One logical CPU per physical core prevents SMT oversubscription. Sorted
    # core order also keeps 8-thread workers within the two 8-core CCDs here.
    cpus = [group[0] for group in groups]
    return [cpus[i * threads:(i + 1) * threads] for i in range(workers)]


def load_api(library_path: str, threads: int):
    lib = ctypes.CDLL(library_path, mode=ctypes.RTLD_GLOBAL)
    set_threads = getattr(lib, SET_THREADS_SYMBOL)
    set_threads.argtypes = [ctypes.c_int]
    set_threads.restype = None
    set_threads(threads)
    lib.parakeet_capi_abi_version.argtypes = []
    lib.parakeet_capi_abi_version.restype = ctypes.c_int
    lib.parakeet_capi_load.argtypes = [ctypes.c_char_p]
    lib.parakeet_capi_load.restype = ctypes.c_void_p
    lib.parakeet_capi_free.argtypes = [ctypes.c_void_p]
    lib.parakeet_capi_free.restype = None
    float_ptr = ctypes.POINTER(ctypes.c_float)
    lib.parakeet_capi_transcribe_pcm_batch.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(float_ptr), ctypes.POINTER(ctypes.c_int),
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.parakeet_capi_transcribe_pcm_batch.restype = ctypes.c_int
    lib.parakeet_capi_free_string.argtypes = [ctypes.c_void_p]
    lib.parakeet_capi_free_string.restype = None
    lib.parakeet_capi_last_error.argtypes = [ctypes.c_void_p]
    lib.parakeet_capi_last_error.restype = ctypes.c_char_p
    return lib


def transcribe_batch(lib, ctx, chunks: list[np.ndarray], decoder: int) -> list[str]:
    float_ptr = ctypes.POINTER(ctypes.c_float)
    pointers = (float_ptr * len(chunks))(*[chunk.ctypes.data_as(float_ptr) for chunk in chunks])
    lengths = (ctypes.c_int * len(chunks))(*[int(chunk.size) for chunk in chunks])
    outputs = (ctypes.c_void_p * len(chunks))()
    rc = lib.parakeet_capi_transcribe_pcm_batch(
        ctx, pointers, lengths, len(chunks), SAMPLE_RATE, decoder, outputs,
    )
    if rc:
        error = lib.parakeet_capi_last_error(ctx)
        raise RuntimeError(f"parakeet batch failed (rc={rc}): {(error or b'').decode(errors='replace')}")
    texts = []
    try:
        for pointer in outputs:
            texts.append(ctypes.string_at(pointer).decode("utf-8") if pointer else "")
    finally:
        for pointer in outputs:
            if pointer:
                lib.parakeet_capi_free_string(pointer)
    return texts


def read_pcm_chunks(path: str, sample_limit: int, batch_size: int):
    """Yield (chunks, coverage rows, read seconds), covering each selected sample once."""
    with wave.open(path, "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError(f"expected 16 kHz mono signed 16-bit PCM WAV: {path}")
        full_frames = wav.getnframes()
        selected_frames = min(full_frames, sample_limit) if sample_limit else full_frames
        cursor = 0
        while cursor < selected_frames:
            chunks: list[np.ndarray] = []
            coverage = []
            read_seconds = 0.0
            for _ in range(batch_size):
                if cursor >= selected_frames:
                    break
                count = min(CHUNK_SAMPLES, selected_frames - cursor)
                started = time.perf_counter()
                raw = wav.readframes(count)
                read_seconds += time.perf_counter() - started
                actual = len(raw) // 2
                if actual != count:
                    raise EOFError(f"short PCM read at sample {cursor}: wanted {count}, got {actual}")
                pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32)
                pcm *= 1.0 / 32768.0
                chunks.append(pcm)
                coverage.append({
                    "chunk_index": cursor // CHUNK_SAMPLES,
                    "start_sample": cursor, "end_sample": cursor + count,
                    "sample_count": count, "start_seconds": cursor / SAMPLE_RATE,
                    "end_seconds": (cursor + count) / SAMPLE_RATE,
                })
                cursor += count
            yield chunks, coverage, read_seconds, full_frames, selected_frames


def worker_main(worker_id, affinity, config, warmup_path, task_queue, result_queue, start_event):
    ctx = None
    try:
        os.environ["OMP_NUM_THREADS"] = str(config["threads_per_worker"])
        if affinity:
            os.sched_setaffinity(0, affinity)
        lib = load_api(config["library_path"], config["threads_per_worker"])
        if lib.parakeet_capi_abi_version() != 6:
            raise RuntimeError(f"unexpected parakeet C ABI {lib.parakeet_capi_abi_version()} (want 6)")
        load_start = time.perf_counter()
        ctx = lib.parakeet_capi_load(config["model_path"].encode())
        model_load_seconds = time.perf_counter() - load_start
        if not ctx:
            raise RuntimeError("parakeet_capi_load returned NULL")

        warm_read_start = time.perf_counter()
        with wave.open(warmup_path, "rb") as wav:
            raw = wav.readframes(CHUNK_SAMPLES)
        warmup_read_seconds = time.perf_counter() - warm_read_start
        warm = np.frombuffer(raw, dtype="<i2").astype(np.float32) * (1.0 / 32768.0)
        warm_start = time.perf_counter()
        transcribe_batch(lib, ctx, [warm], config["decoder_id"])
        warmup_transcribe_seconds = time.perf_counter() - warm_start
        result_queue.put({
            "kind": "ready", "worker_id": worker_id, "pid": os.getpid(),
            "affinity": sorted(os.sched_getaffinity(0)), "model_load_seconds": model_load_seconds,
            "warmup_read_seconds": warmup_read_seconds,
            "warmup_transcribe_seconds": warmup_transcribe_seconds,
            "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        })
        start_event.wait()

        while True:
            task = task_queue.get()
            if task is None:
                break
            episode_start = time.perf_counter()
            chunks_out = []
            pcm_read_seconds = 0.0
            inference_seconds = 0.0
            cpu_start = time.process_time()
            full_frames = selected_frames = 0
            for chunks, coverage, read_seconds, full_frames, selected_frames in read_pcm_chunks(
                task["pcm_path"], config["sample_limit"], config["batch_size"]
            ):
                pcm_read_seconds += read_seconds
                infer_start = time.perf_counter()
                texts = transcribe_batch(lib, ctx, chunks, config["decoder_id"])
                batch_seconds = time.perf_counter() - infer_start
                inference_seconds += batch_seconds
                for row, text in zip(coverage, texts):
                    row["text"] = text
                    row["batch_inference_seconds"] = batch_seconds
                    chunks_out.append(row)
            cpu_seconds = time.process_time() - cpu_start
            if sum(row["sample_count"] for row in chunks_out) != selected_frames:
                raise RuntimeError("chunk coverage does not equal selected PCM samples")
            if chunks_out and (chunks_out[0]["start_sample"] != 0 or chunks_out[-1]["end_sample"] != selected_frames):
                raise RuntimeError("chunk coverage endpoints are incomplete")
            if not any(row["text"].strip() for row in chunks_out):
                raise RuntimeError(f"empty complete transcript for episode {task['episode']['index']}")

            transcript_json = Path(task["transcript_json"])
            transcript_text = transcript_json.with_suffix(".txt")
            transcript_doc = {
                "schema_version": 1, "episode": task["episode"], "config": config,
                "worker_id": worker_id, "full_pcm_samples": full_frames,
                "benchmark_samples": selected_frames, "chunks": chunks_out,
            }
            save_json(transcript_json, transcript_doc)
            save_text(transcript_text, "\n".join(row["text"].strip() for row in chunks_out if row["text"].strip()) + "\n")
            episode = task["episode"]
            result_queue.put({
                "kind": "episode", "worker_id": worker_id, "pid": os.getpid(),
                "index": episode["index"], "title": episode["title"],
                "path": episode["path"], "sha256": episode["sha256"],
                "pcm_path": task["pcm_relative"], "pcm_sha256": task["pcm_sha256"],
                "full_decoded_audio_seconds": full_frames / SAMPLE_RATE,
                "benchmark_audio_seconds": selected_frames / SAMPLE_RATE,
                "decode_seconds": pcm_read_seconds, "pcm_read_seconds": pcm_read_seconds,
                "transcribe_seconds": inference_seconds, "cpu_seconds": cpu_seconds,
                "end_to_end_seconds": time.perf_counter() - episode_start,
                "rtf": inference_seconds / (selected_frames / SAMPLE_RATE),
                "speed_x": (selected_frames / SAMPLE_RATE) / inference_seconds,
                "chunk_count": len(chunks_out), "coverage_start_sample": 0,
                "coverage_end_sample": chunks_out[-1]["end_sample"] if chunks_out else 0,
                "covered_samples": sum(row["sample_count"] for row in chunks_out),
                "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
                "transcript_json": str(transcript_json.relative_to(Path(task["run_dir"]))),
                "transcript_text": str(transcript_text.relative_to(Path(task["run_dir"]))),
            })
    except BaseException as error:
        result_queue.put({
            "kind": "error", "worker_id": worker_id, "pid": os.getpid(),
            "error_type": type(error).__name__, "error": str(error),
            "traceback": traceback.format_exc(),
        })
    finally:
        if ctx:
            lib.parakeet_capi_free(ctx)
    # ggml-cuda's static destructors run after the CUDA driver has begun
    # tearing down and abort ("driver shutting down"). Flush the queue's feeder
    # thread, then skip interpreter/library teardown; the CPU build is unaffected.
    result_queue.close()
    result_queue.join_thread()
    os._exit(0)


def get_message(result_queue, processes):
    while True:
        try:
            return result_queue.get(timeout=1)
        except queue.Empty:
            failed = [(p.pid, p.exitcode) for p in processes if p.exitcode not in (None, 0)]
            if failed:
                raise RuntimeError(f"workers exited without reporting an error: {failed}")


def model_metadata(path: Path, provenance_path: Path | None) -> dict:
    provenance = json.loads(provenance_path.read_text()) if provenance_path and provenance_path.is_file() else None
    return {
        "path": str(path.resolve()), "bytes": path.stat().st_size,
        "sha256": sha256_file(path), "provenance": provenance,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark parakeet.cpp with persistent, pinned CPU workers.")
    parser.add_argument("--run-id", required=True, help="unique output directory name")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--threads-per-worker", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--sample-seconds", type=float, default=0,
                        help="prefix per episode; 0 covers the full episode")
    parser.add_argument("--episode-limit", type=int, default=0,
                        help="use the first N manifest episodes; 0 uses all ten")
    parser.add_argument("--decoder", choices=DECODERS, default="tdt")
    parser.add_argument("--pin-workers", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--library-path", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument("--provenance-path", type=Path, default=DEFAULT_PROVENANCE)
    args = parser.parse_args()
    if args.workers < 1 or args.threads_per_worker < 1 or args.batch_size < 1:
        parser.error("worker, thread, and batch counts must be positive")
    if args.sample_seconds < 0 or args.episode_limit < 0 or args.episode_limit > 10:
        parser.error("sample seconds cannot be negative; episode limit must be 0..10")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", args.run_id):
        parser.error("--run-id must be 1-80 safe filename characters")
    for needed in (args.model_path, args.library_path):
        if not needed.is_file():
            parser.error(f"missing required local file: {needed}")

    source_manifest_path = TAL_ROOT / "manifest.json"
    pcm_manifest_path = TAL_ROOT / "pcm" / "manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text())
    pcm_manifest = json.loads(pcm_manifest_path.read_text())
    episodes = source_manifest["episodes"][: args.episode_limit or 10]
    pcm_by_index = {row["index"]: row for row in pcm_manifest["episodes"]}
    for episode in episodes:
        pcm = pcm_by_index.get(episode["index"])
        if not pcm or not (TAL_ROOT / pcm["pcm_path"]).is_file():
            parser.error(f"missing prepared PCM for episode {episode['index']}")
        with wave.open(str(TAL_ROOT / pcm["pcm_path"]), "rb") as wav:
            shape = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate())
            if shape != (1, 2, SAMPLE_RATE):
                parser.error(f"unexpected PCM format for episode {episode['index']}: {shape}")
            expected_frames = round(pcm["pcm_duration_seconds"] * SAMPLE_RATE)
            if wav.getnframes() != expected_frames:
                parser.error(
                    f"PCM duration mismatch for episode {episode['index']}: "
                    f"header={wav.getnframes()} manifest={expected_frames} samples"
                )
    try:
        affinities = allocate_affinity(args.workers, args.threads_per_worker, args.pin_workers)
    except ValueError as error:
        parser.error(str(error))

    run_dir = TAL_ROOT / "runs" / args.run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error(f"run directory already exists: {run_dir}")
    (run_dir / "transcripts").mkdir()
    sample_limit = round(args.sample_seconds * SAMPLE_RATE) if args.sample_seconds else 0
    config = {
        "engine": "parakeet.cpp", "model": "nvidia/parakeet-tdt_ctc-110m",
        "model_path": str(args.model_path.resolve()), "library_path": str(args.library_path.resolve()),
        # parakeet.cpp auto-selects the first GPU when built with one, unless PARAKEET_DEVICE=cpu.
        "device": ("cuda" if any(args.library_path.resolve().parent.rglob("libggml-cuda.so"))
                   and os.environ.get("PARAKEET_DEVICE", "").lower() != "cpu" else "cpu"),
        "quantization": "q8_0" if "q8_0" in args.model_path.name else "f16" if "f16" in args.model_path.name else "unknown",
        "decoder": args.decoder,
        "decoder_id": DECODERS[args.decoder], "workers": args.workers,
        "threads_per_worker": args.threads_per_worker, "pin_workers": args.pin_workers,
        "batch_size": args.batch_size, "chunk_seconds": CHUNK_SECONDS,
        "chunk_overlap_seconds": 0, "sample_seconds": args.sample_seconds,
        "sample_limit": sample_limit, "sample_rate": SAMPLE_RATE,
    }
    invocation = [sys.executable, *sys.argv]
    results = {
        "schema_version": 1, "run_id": args.run_id, "status": "starting",
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "invocation": {"argv": invocation, "shell_display": shlex.join(invocation), "cwd": str(Path.cwd())},
        "config": config,
        "input": {
            "manifest": str(source_manifest_path.relative_to(ROOT)),
            "manifest_sha256": sha256_file(source_manifest_path),
            "decoded_input_manifest": str(pcm_manifest_path.relative_to(ROOT)),
            "decoded_input_manifest_sha256": sha256_file(pcm_manifest_path),
            "feed": source_manifest.get("feed"), "retrieved_at": source_manifest.get("retrieved_at"),
            "selected_episode_indexes": [ep["index"] for ep in episodes],
        },
        "model": model_metadata(args.model_path, args.provenance_path),
        "engine": {
            "repository": "https://github.com/mudler/parakeet.cpp.git",
            "revision": command_output(["git", "-C", str(ROOT / "vendor/parakeet.cpp"), "rev-parse", "HEAD"]),
            "capi_abi": 6, "set_threads_symbol": SET_THREADS_SYMBOL,
        },
        "system": {
            "platform": platform.platform(), "python": platform.python_version(),
            "numpy": np.__version__, "cpu_count": os.cpu_count(),
            "parent_affinity": sorted(os.sched_getaffinity(0)), "worker_affinities": affinities,
            "lscpu": command_output(["lscpu"]), "memory": command_output(["free", "-b"]),
            "load_start": os.getloadavg(),
            # A GPU build (e.g. build-cuda) picks the first GPU itself; PARAKEET_DEVICE=cpu overrides.
            "parakeet_device_env": os.environ.get("PARAKEET_DEVICE"),
            "gpu": command_output(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,compute_cap",
                                   "--format=csv,noheader"]),
        },
        "workers": [], "episodes": [],
    }
    result_path = run_dir / "results.json"
    save_json(result_path, results)

    context = mp.get_context("spawn")
    task_queue, result_queue, start_event = context.Queue(), context.Queue(), context.Event()
    warmup_path = str(TAL_ROOT / pcm_by_index[episodes[0]["index"]]["pcm_path"])
    processes = []
    setup_start = time.perf_counter()
    try:
        for worker_id, affinity in enumerate(affinities):
            process = context.Process(
                target=worker_main,
                args=(worker_id, affinity, config, warmup_path, task_queue, result_queue, start_event),
                name=f"parakeet-{worker_id}",
            )
            process.start()
            processes.append(process)
        while len(results["workers"]) < args.workers:
            message = get_message(result_queue, processes)
            if message["kind"] == "error":
                raise RuntimeError(f"worker {message['worker_id']} failed during setup:\n{message['traceback']}")
            if message["kind"] != "ready":
                raise RuntimeError(f"unexpected pre-suite message: {message['kind']}")
            results["workers"].append(message)
            print(f"Worker {message['worker_id']} ready: load {message['model_load_seconds']:.2f}s, "
                  f"warmup {message['warmup_transcribe_seconds']:.2f}s", flush=True)
        results["workers"].sort(key=lambda row: row["worker_id"])
        results["startup"] = {
            "all_workers_ready_wall_seconds": time.perf_counter() - setup_start,
            "model_load_seconds_by_worker": [row["model_load_seconds"] for row in results["workers"]],
            "warmup_transcribe_seconds_by_worker": [row["warmup_transcribe_seconds"] for row in results["workers"]],
        }
        for episode in episodes:
            pcm = pcm_by_index[episode["index"]]
            stem = Path(pcm["pcm_path"]).stem
            task_queue.put({
                "episode": episode, "pcm_path": str(TAL_ROOT / pcm["pcm_path"]),
                "pcm_relative": pcm["pcm_path"], "pcm_sha256": pcm["pcm_sha256"],
                "transcript_json": str(run_dir / "transcripts" / f"{stem}.json"),
                "run_dir": str(run_dir),
            })
        for _ in processes:
            task_queue.put(None)
        results["status"] = "running"
        save_json(result_path, results)
        suite_start = time.perf_counter()
        start_event.set()
        while len(results["episodes"]) < len(episodes):
            message = get_message(result_queue, processes)
            if message["kind"] == "error":
                raise RuntimeError(f"worker {message['worker_id']} failed:\n{message['traceback']}")
            if message["kind"] != "episode":
                raise RuntimeError(f"unexpected suite message: {message['kind']}")
            results["episodes"].append(message)
            results["episodes"].sort(key=lambda row: row["index"])
            save_json(result_path, results)
            print(f"Completed {message['index']:02d} on worker {message['worker_id']}: "
                  f"{message['transcribe_seconds']:.2f}s, {message['speed_x']:.2f}x", flush=True)
        suite_wall_seconds = time.perf_counter() - suite_start
        for process in processes:
            process.join(timeout=30)
        unclean = [(p.pid, p.exitcode) for p in processes if p.exitcode != 0]
        if unclean:
            raise RuntimeError(f"workers did not exit cleanly: {unclean}")
        total_audio = sum(row["benchmark_audio_seconds"] for row in results["episodes"])
        total_inference = sum(row["transcribe_seconds"] for row in results["episodes"])
        results["summary"] = {
            "episode_count": len(results["episodes"]), "benchmark_audio_seconds": total_audio,
            "full_decoded_audio_seconds": sum(row["full_decoded_audio_seconds"] for row in results["episodes"]),
            "sum_worker_transcribe_seconds": total_inference,
            "sum_worker_decode_seconds": sum(row["decode_seconds"] for row in results["episodes"]),
            "suite_wall_seconds": suite_wall_seconds,
            "corpus_wall_rtf": suite_wall_seconds / total_audio,
            "corpus_wall_speed_x": total_audio / suite_wall_seconds,
            "sum_worker_transcribe_rtf": total_inference / total_audio,
            "worker_peak_rss_mib": {
                str(worker): max(row["peak_process_rss_mib"] for row in results["episodes"] if row["worker_id"] == worker)
                for worker in sorted({row["worker_id"] for row in results["episodes"]})
            },
            "load_end": os.getloadavg(),
        }
        results["status"] = "complete"
        results["completed_at"] = dt.datetime.now(dt.UTC).isoformat()
        save_json(result_path, results)
        print(json.dumps(results["summary"], indent=2), flush=True)
        print(f"Results: {result_path}", flush=True)
        return 0
    except BaseException as error:
        results["status"] = "failed"
        results["failed_at"] = dt.datetime.now(dt.UTC).isoformat()
        results["failure"] = {"error_type": type(error).__name__, "error": str(error), "traceback": traceback.format_exc()}
        save_json(result_path, results)
        raise
    finally:
        start_event.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
