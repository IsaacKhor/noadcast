#!/usr/bin/env python3
"""Parallel faster-whisper benchmark for the fixed TAL corpus.

The timed suite starts only after every worker has loaded a persistent model and
transcribed a 30-second warmup. Each episode is decoded in full; --sample-seconds
only limits the numpy audio passed to inference. Nothing is downloaded.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import enum
import hashlib
import importlib.metadata
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
import threading
import time
import traceback
import uuid


ROOT = Path(__file__).resolve().parents[1]
TAL_ROOT = ROOT / "benchmarks" / "tal"
DEFAULT_MODEL = TAL_ROOT / "models" / "tiny.en"
PACKAGE_NAMES = ("faster-whisper", "ctranslate2", "av", "numpy", "onnxruntime")


class MemorySampler:
    """Sample concurrent worker RSS/PSS, including startup, without summing peaks."""

    def __init__(self, processes, interval_ms):
        self.processes = processes
        self.interval = interval_ms / 1000
        self.phase = "startup"
        self.samples = []
        self.errors = []
        self.stop_event = threading.Event()
        self.started = time.perf_counter()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop_event.is_set():
            stamp = time.perf_counter()
            phase = self.phase
            rss = pss = observed = 0
            for process in list(self.processes):
                if process.pid is None:
                    continue
                try:
                    values = {}
                    for line in Path(f"/proc/{process.pid}/smaps_rollup").read_text().splitlines():
                        parts = line.split()
                        if parts and parts[0] in ("Rss:", "Pss:"):
                            values[parts[0]] = int(parts[1]) / 1024
                    if len(values) == 2:
                        rss += values["Rss:"]
                        pss += values["Pss:"]
                        observed += 1
                except (FileNotFoundError, ProcessLookupError):
                    pass
                except OSError as error:
                    self.errors.append(str(error))
            if observed:
                self.samples.append({"elapsed_seconds": stamp - self.started,
                                     "phase": phase, "workers_observed": observed,
                                     "rss_mib": rss, "pss_mib": pss,
                                     "collection_seconds": time.perf_counter() - stamp})
            self.stop_event.wait(max(0, self.interval - (time.perf_counter() - stamp)))

    def finish(self):
        self.stop_event.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("memory sampler did not stop")
        summary = {"interval_ms": self.interval * 1000, "source": "/proc/PID/smaps_rollup",
                   "scope": "sum of worker processes sampled in each sweep; parent excluded",
                   "samples": len(self.samples), "errors": self.errors}
        for scope in ("all", "startup", "suite"):
            selected = [row for row in self.samples if scope == "all" or row["phase"] == scope]
            summary[scope] = {"samples": len(selected),
                              "peak_rss_mib": max((row["rss_mib"] for row in selected), default=None),
                              "peak_pss_mib": max((row["pss_mib"] for row in selected), default=None)}
        return summary


def jsonable(value):
    """Convert library dataclasses/enums into durable JSON values."""
    if dataclasses.is_dataclass(value):
        return jsonable(dataclasses.asdict(value))
    if isinstance(value, enum.Enum):
        return jsonable(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(jsonable(value), indent=2, ensure_ascii=False) + "\n")
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


def package_versions() -> dict[str, str | None]:
    versions = {}
    for name in PACKAGE_NAMES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def command_output(command: list[str]) -> str | None:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def physical_cpu_groups(allowed: set[int]) -> list[list[int]]:
    """Return sibling groups, retaining only CPUs allowed by this process."""
    output = command_output(["lscpu", "-p=CPU,CORE,SOCKET"])
    groups: dict[tuple[int, int], list[int]] = {}
    if output:
        for line in output.splitlines():
            if line.startswith("#"):
                continue
            cpu, core, socket = (int(item) for item in line.split(",")[:3])
            if cpu in allowed:
                groups.setdefault((socket, core), []).append(cpu)
    if not groups:
        return [[cpu] for cpu in sorted(allowed)]
    return [sorted(cpus) for _, cpus in sorted(groups.items())]


def allocate_affinity(workers: int, threads: int, physical_cores: int, pin_workers: bool) -> list[list[int] | None]:
    if not pin_workers:
        return [None] * workers
    allowed = set(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else set(range(os.cpu_count() or 1))
    groups = physical_cpu_groups(allowed)
    required = workers * threads
    requested = physical_cores or required
    if requested < required:
        raise ValueError(f"--physical-cores ({requested}) must cover workers * threads-per-worker ({required})")
    if len(groups) < requested:
        raise ValueError(f"requested {requested} physical cores, but affinity exposes only {len(groups)}")
    # Select one logical CPU per physical core to avoid accidental SMT oversubscription.
    cpus = [group[0] for group in groups[:requested]]
    return [cpus[index * threads : (index + 1) * threads] for index in range(workers)]


def model_metadata(model_dir: Path, revision_override: str | None) -> dict:
    binary = model_dir / "model.bin"
    if not binary.is_file():
        raise FileNotFoundError(f"local model is incomplete: {binary} does not exist")
    revision = revision_override
    metadata_file = model_dir / ".cache/huggingface/download/model.bin.metadata"
    metadata_lines = []
    if metadata_file.is_file():
        metadata_lines = metadata_file.read_text().splitlines()
        if not revision and metadata_lines:
            revision = metadata_lines[0]
    return {
        "path": str(model_dir.resolve()),
        "revision": revision,
        "model_bin_sha256": sha256_file(binary),
        "model_bin_bytes": binary.stat().st_size,
        "huggingface_metadata": metadata_lines or None,
    }


def worker_main(worker_id, affinity, config, warmup_path, task_queue, result_queue, start_event):
    """Load one model, report ready, then consume episodes until the sentinel."""
    try:
        os.environ["OMP_NUM_THREADS"] = str(config["cpu_threads"])
        os.environ["MKL_NUM_THREADS"] = str(config["cpu_threads"])
        os.environ["OPENBLAS_NUM_THREADS"] = str(config["cpu_threads"])
        if affinity and hasattr(os, "sched_setaffinity"):
            os.sched_setaffinity(0, affinity)

        if config["device"] == "cuda":
            # The server worker's loader for the pip cuBLAS/cuDNN wheels (no LD_LIBRARY_PATH needed).
            from noadcast.transcribe.worker import preload_cuda_libraries
            preload_cuda_libraries()
        from faster_whisper import BatchedInferencePipeline, WhisperModel
        # The server's decoder: faster-whisper's stops at the first corrupt packet (DAI splices).
        from noadcast.transcribe.audio import decode_audio

        load_start = time.perf_counter()
        model = WhisperModel(
            config["model_path"], device=config["device"], compute_type=config["compute_type"],
            cpu_threads=config["cpu_threads"], num_workers=1, local_files_only=True,
        )
        pipeline = BatchedInferencePipeline(model)
        model_load_seconds = time.perf_counter() - load_start

        decode_start = time.perf_counter()
        warm_audio = decode_audio(warmup_path)[0][: config["warmup_audio_seconds"] * 16000]
        warmup_decode_seconds = time.perf_counter() - decode_start
        warmup_start = time.perf_counter()
        segments, _ = pipeline.transcribe(
            warm_audio, language=config["language"], beam_size=config["beam_size"],
            batch_size=config["batch_size"], vad_filter=config["vad_filter"],
            condition_on_previous_text=config["condition_on_previous_text"],
            word_timestamps=config["word_timestamps"],
        )
        list(segments)
        warmup_transcribe_seconds = time.perf_counter() - warmup_start
        # Slices retain the full decoded episode as their numpy backing array.
        # Release it before the ready barrier so warmup memory cannot skew the run.
        del warm_audio, segments
        result_queue.put({
            "kind": "ready", "worker_id": worker_id, "pid": os.getpid(),
            "affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else affinity,
            "model_load_seconds": model_load_seconds,
            "warmup_decode_seconds": warmup_decode_seconds,
            "warmup_transcribe_seconds": warmup_transcribe_seconds,
        })
        start_event.wait()

        while True:
            task = task_queue.get()
            if task is None:
                break
            episode = task["episode"]
            episode_start = time.perf_counter()
            decode_start = time.perf_counter()
            audio, decode_skipped_packets = decode_audio(task["audio_path"])
            full_decoded_audio_seconds = len(audio) / 16000
            decode_seconds = time.perf_counter() - decode_start
            if config["sample_seconds"]:
                audio = audio[: config["sample_seconds"] * 16000]
            benchmark_audio_seconds = len(audio) / 16000

            cpu_start = time.process_time()
            inference_start = time.perf_counter()
            segments, info = pipeline.transcribe(
                audio, language=config["language"], beam_size=config["beam_size"],
                batch_size=config["batch_size"], vad_filter=config["vad_filter"],
                condition_on_previous_text=config["condition_on_previous_text"],
                word_timestamps=config["word_timestamps"],
            )
            rows = [jsonable(segment) for segment in segments]
            transcribe_seconds = time.perf_counter() - inference_start
            cpu_seconds = time.process_time() - cpu_start
            if not rows or not any(row["text"].strip() for row in rows):
                raise RuntimeError(f"empty transcription for episode {episode['index']}")

            transcript_json = Path(task["transcript_json"])
            transcript_txt = transcript_json.with_suffix(".txt")
            save_json(transcript_json, {
                "schema_version": 1, "episode": episode, "config": config,
                "worker_id": worker_id, "segments": rows,
            })
            save_text(transcript_txt, "\n".join(row["text"].strip() for row in rows) + "\n")
            result_queue.put({
                "kind": "episode", "worker_id": worker_id, "pid": os.getpid(),
                **episode,
                "source_sha256": episode["sha256"],
                "decoded_input_sha256": task["decoded_input_sha256"],
                "full_decoded_audio_seconds": full_decoded_audio_seconds,
                "benchmark_audio_seconds": benchmark_audio_seconds,
                "decode_seconds": decode_seconds,
                "decode_skipped_packets": decode_skipped_packets,
                "transcribe_seconds": transcribe_seconds,
                "cpu_seconds": cpu_seconds,
                "end_to_end_seconds": time.perf_counter() - episode_start,
                "rtf": transcribe_seconds / benchmark_audio_seconds,
                "speed_x": benchmark_audio_seconds / transcribe_seconds,
                "speech_seconds_after_vad": info.duration_after_vad,
                "segment_count": len(rows), "last_segment_end": rows[-1]["end"],
                "word_count": sum(len(row["words"] or []) for row in rows),
                "last_word_end": next((row["words"][-1]["end"] for row in reversed(rows) if row["words"]), None),
                "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
                "transcript_json": str(transcript_json.relative_to(Path(task["run_dir"]))),
                "transcript_text": str(transcript_txt.relative_to(Path(task["run_dir"]))),
                "effective_transcription_options": jsonable(info.transcription_options),
                "effective_vad_options": jsonable(info.vad_options),
            })
    except BaseException as error:
        result_queue.put({
            "kind": "error", "worker_id": worker_id, "pid": os.getpid(),
            "error_type": type(error).__name__, "error": str(error),
            "traceback": traceback.format_exc(),
        })


def get_message(result_queue, processes):
    while True:
        try:
            return result_queue.get(timeout=1)
        except queue.Empty:
            failed = [(process.pid, process.exitcode) for process in processes if process.exitcode not in (None, 0)]
            if failed:
                raise RuntimeError(f"workers exited without reporting an error: {failed}")


def parse_episode_indexes(value: str) -> list[int]:
    indexes = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not indexes or len(indexes) != len(set(indexes)):
        raise argparse.ArgumentTypeError("provide a non-empty comma-separated list with no duplicates")
    return indexes


def make_run_id(label: str | None) -> str:
    if label:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", label):
            raise ValueError("--run-id must be 1-80 safe filename characters")
        return label
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"whisper-tiny-en-{stamp}-{uuid.uuid4().hex[:8]}"


def write_run_readme(run_dir: Path, args) -> None:
    mode = "full episodes" if not args.sample_seconds else f"the first {args.sample_seconds} decoded seconds per episode"
    save_text(run_dir / "README.md", f"""# faster-whisper parallel TAL run

`results.json` is an atomic, checkpointed machine-readable record. `status` is
`running`, `complete`, or `failed`. Startup and each worker's model load and
30-second warmup are outside `summary.suite_wall_seconds`. Each episode's full
source file is decoded and `decode_seconds` is reported separately; inference uses
{mode}. Corpus throughput is `summary.corpus_wall_speed_x`, calculated from total
benchmarked audio divided by the parallel suite wall clock. Per-worker inference
times are also retained but are not used as the corpus wall denominator.

`transcripts/*.json` contains segment metadata and `transcripts/*.txt` contains
plain text. Paths in `results.json` are relative to this run directory. Source
SHA-256 values come from the immutable corpus manifest, whose own SHA-256 is in
`input.manifest_sha256`. The model binary is independently hashed before startup.
""")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark a local faster-whisper model with persistent parallel CPU workers.",
        epilog=("Pilot example: --episodes 1,2,3,4 --sample-seconds 120. "
                "Full run: omit both options. Every selected source is decoded in full."),
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--threads-per-worker", type=int, default=4)
    parser.add_argument("--physical-cores", type=int, default=16,
                        help="core budget recorded in results; enforced when --pin-workers is used (default: 16)")
    parser.add_argument("--pin-workers", action="store_true",
                        help="give each worker a disjoint physical-core affinity (default: OS scheduling)")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--sample-seconds", type=int, default=0,
                        help="inference prefix per selected episode; 0 transcribes full files")
    parser.add_argument("--episodes", type=parse_episode_indexes,
                        help="manifest indexes, comma separated (default: every manifest episode)")
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--model-id", help="model repository identifier recorded in results")
    parser.add_argument("--memory-interval-ms", type=int, default=0,
                        help="sample concurrent worker RSS and PSS; 0 disables sampling")
    parser.add_argument("--input-format", choices=("mp3", "pcm"), default="mp3",
                        help="decode corpus MP3s or matching pcm/<stem>.wav files (default: mp3)")
    parser.add_argument("--model-revision", help="revision ID if absent from local HF metadata")
    parser.add_argument("--run-id", help="unique output directory name (default: timestamp plus nonce)")
    parser.add_argument("--corpus", type=Path, default=TAL_ROOT,
                        help="corpus directory with manifest.json, optional pcm/, and runs/ (default: benchmarks/tal)")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu",
                        help="CTranslate2 device; cuda workers share the GPU (default: cpu)")
    parser.add_argument("--compute-type", default="int8",
                        help="CTranslate2 compute type, e.g. int8, float16, int8_float16 (default: int8)")
    parser.add_argument("--word-timestamps", action="store_true",
                        help="emit cross-attention word alignments (Word.start/end/probability)")
    args = parser.parse_args()
    if args.workers < 1 or args.threads_per_worker < 1 or args.batch_size < 1 or args.sample_seconds < 0:
        parser.error("worker, thread, and batch counts must be positive; sample seconds cannot be negative")
    if args.memory_interval_ms and args.memory_interval_ms < 100:
        parser.error("memory sampling interval must be 0 or at least 100 ms")

    corpus = args.corpus.resolve()
    manifest_path = corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    pcm_manifest_path = corpus / "pcm" / "manifest.json"
    pcm_manifest = json.loads(pcm_manifest_path.read_text()) if args.input_format == "pcm" and pcm_manifest_path.is_file() else None
    pcm_by_index = {episode["index"]: episode for episode in pcm_manifest["episodes"]} if pcm_manifest else {}
    by_index = {episode["index"]: episode for episode in manifest["episodes"]}
    selected_indexes = args.episodes or list(by_index)
    unknown = sorted(set(selected_indexes) - set(by_index))
    if unknown:
        parser.error(f"episode indexes absent from manifest: {unknown}")
    episodes = [by_index[index] for index in selected_indexes]
    def episode_audio_path(episode):
        if args.input_format == "pcm":
            pcm_episode = pcm_by_index.get(episode["index"])
            if not pcm_episode:
                parser.error(f"episode {episode['index']} is absent from {pcm_manifest_path}")
            return corpus / pcm_episode["pcm_path"]
        return corpus / episode["path"]

    def decoded_input_sha256(episode):
        if args.input_format == "pcm":
            return pcm_by_index[episode["index"]]["pcm_sha256"]
        return episode["sha256"]

    for episode in episodes:
        audio_path = episode_audio_path(episode)
        if not audio_path.is_file():
            parser.error(f"missing input: {audio_path}")

    try:
        affinities = allocate_affinity(args.workers, args.threads_per_worker, args.physical_cores, args.pin_workers)
        run_id = make_run_id(args.run_id)
    except ValueError as error:
        parser.error(str(error))
    run_dir = corpus / "runs" / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error(f"run directory already exists: {run_dir}")
    (run_dir / "transcripts").mkdir()
    write_run_readme(run_dir, args)

    invocation = [sys.executable, *sys.argv]
    config = {
        "engine": "faster-whisper", "model": args.model_id or f"Systran/faster-whisper-{args.model_path.name}",
        "model_path": str(args.model_path.resolve()), "device": args.device, "compute_type": args.compute_type,
        "workers": args.workers, "cpu_threads": args.threads_per_worker,
        "physical_cores": args.physical_cores, "pin_workers": args.pin_workers,
        "input_format": args.input_format, "batch_size": args.batch_size,
        "beam_size": 5, "language": "en", "vad_filter": True,
        "condition_on_previous_text": False, "word_timestamps": args.word_timestamps,
        "warmup_audio_seconds": 30, "sample_seconds": args.sample_seconds,
        "memory_interval_ms": args.memory_interval_ms,
    }
    results = {
        "schema_version": 1, "run_id": run_id, "status": "starting",
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "invocation": {"argv": invocation, "shell_display": shlex.join(invocation), "cwd": str(Path.cwd())},
        "config": config,
        "input": {
            "manifest": str(manifest_path.relative_to(ROOT)),
            "manifest_sha256": sha256_file(manifest_path),
            "decoded_input_manifest": str(pcm_manifest_path.relative_to(ROOT)) if pcm_manifest else str(manifest_path.relative_to(ROOT)),
            "decoded_input_manifest_sha256": sha256_file(pcm_manifest_path) if pcm_manifest else sha256_file(manifest_path),
            "feed": manifest.get("feed"), "retrieved_at": manifest.get("retrieved_at"),
            "selected_episode_indexes": selected_indexes,
            "source_sha256": {str(ep["index"]): ep["sha256"] for ep in episodes},
            "decoded_input_sha256": {str(ep["index"]): decoded_input_sha256(ep) for ep in episodes},
        },
        "model": model_metadata(args.model_path.resolve(), args.model_revision),
        "system": {
            "platform": platform.platform(), "python": platform.python_version(),
            "cpu_count": os.cpu_count(), "parent_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
            "worker_affinities": affinities, "lscpu": command_output(["lscpu"]),
            "memory": command_output(["free", "-b"]), "load_start": os.getloadavg(),
            "versions": package_versions(),
            "gpu": command_output(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,compute_cap",
                                   "--format=csv,noheader"]) if args.device == "cuda" else None,
        },
        "workers": [], "episodes": [],
    }
    result_path = run_dir / "results.json"
    save_json(result_path, results)

    context = mp.get_context("spawn")
    task_queue = context.Queue()
    result_queue = context.Queue()
    start_event = context.Event()
    warmup_path = str(episode_audio_path(episodes[0]))
    processes = []
    memory_sampler = MemorySampler(processes, args.memory_interval_ms) if args.memory_interval_ms else None
    if memory_sampler:
        memory_sampler.thread.start()
    setup_start = time.perf_counter()
    try:
        for worker_id, affinity in enumerate(affinities):
            process = context.Process(
                target=worker_main,
                args=(worker_id, affinity, config, warmup_path, task_queue, result_queue, start_event),
                name=f"whisper-{worker_id}",
            )
            process.start()
            processes.append(process)

        while len(results["workers"]) < args.workers:
            message = get_message(result_queue, processes)
            if message["kind"] == "error":
                raise RuntimeError(f"worker {message['worker_id']} failed during setup:\n{message['traceback']}")
            if message["kind"] != "ready":
                raise RuntimeError(f"unexpected pre-suite worker message: {message['kind']}")
            results["workers"].append(message)
            print(f"Worker {message['worker_id']} ready: load {message['model_load_seconds']:.2f}s, "
                  f"warmup {message['warmup_transcribe_seconds']:.2f}s", flush=True)

        results["workers"].sort(key=lambda item: item["worker_id"])
        results["startup"] = {
            "all_workers_ready_wall_seconds": time.perf_counter() - setup_start,
            "model_load_seconds_by_worker": [item["model_load_seconds"] for item in results["workers"]],
            "warmup_decode_seconds_by_worker": [item["warmup_decode_seconds"] for item in results["workers"]],
            "warmup_transcribe_seconds_by_worker": [item["warmup_transcribe_seconds"] for item in results["workers"]],
        }
        for episode in episodes:
            basename = Path(episode["path"]).stem
            task_queue.put({
                "episode": episode, "audio_path": str(episode_audio_path(episode)),
                "decoded_input_sha256": decoded_input_sha256(episode),
                "transcript_json": str(run_dir / "transcripts" / f"{basename}.json"),
                "run_dir": str(run_dir),
            })
        for _ in processes:
            task_queue.put(None)

        results["status"] = "running"
        save_json(result_path, results)
        suite_start = time.perf_counter()
        if memory_sampler:
            memory_sampler.phase = "suite"
        start_event.set()
        while len(results["episodes"]) < len(episodes):
            message = get_message(result_queue, processes)
            if message["kind"] == "error":
                raise RuntimeError(f"worker {message['worker_id']} failed:\n{message['traceback']}")
            if message["kind"] != "episode":
                raise RuntimeError(f"unexpected suite worker message: {message['kind']}")
            results["episodes"].append(message)
            results["episodes"].sort(key=lambda item: selected_indexes.index(item["index"]))
            save_json(result_path, results)
            print(f"Completed {message['index']:02d} on worker {message['worker_id']}: "
                  f"{message['transcribe_seconds']:.2f}s, {message['speed_x']:.2f}x", flush=True)
        suite_wall_seconds = time.perf_counter() - suite_start
        if memory_sampler:
            results["memory"] = memory_sampler.finish()
            save_json(run_dir / "memory-samples.json", memory_sampler.samples)

        for process in processes:
            process.join(timeout=30)
        unclean = [(process.pid, process.exitcode) for process in processes if process.exitcode != 0]
        if unclean:
            raise RuntimeError(f"workers did not exit cleanly: {unclean}")

        total_audio = sum(item["benchmark_audio_seconds"] for item in results["episodes"])
        total_transcribe = sum(item["transcribe_seconds"] for item in results["episodes"])
        total_decode = sum(item["decode_seconds"] for item in results["episodes"])
        results["summary"] = {
            "episode_count": len(results["episodes"]), "benchmark_audio_seconds": total_audio,
            "full_decoded_audio_seconds": sum(item["full_decoded_audio_seconds"] for item in results["episodes"]),
            "sum_worker_transcribe_seconds": total_transcribe,
            "sum_worker_decode_seconds": total_decode,
            "suite_wall_seconds": suite_wall_seconds,
            "corpus_wall_rtf": suite_wall_seconds / total_audio,
            "corpus_wall_speed_x": total_audio / suite_wall_seconds,
            "sum_worker_transcribe_rtf": total_transcribe / total_audio,
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
        if memory_sampler and memory_sampler.thread.is_alive():
            memory_sampler.finish()
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
