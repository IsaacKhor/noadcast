"""Transcription worker process: one faster-whisper model per process.

Runs as a ``spawn`` child of the server (pool.py explains why never fork).
Heavy libraries are imported only inside the child, *after* the thread-count
environment is set: OpenBLAS and OpenMP read ``*_NUM_THREADS`` once, when the
library loads, and ``import faster_whisper`` loads numpy. That is also why this
module imports only the standard library at top level — the child imports it
to find its target before any of our code runs.

Wire protocol, pickled over two per-worker pipes:

- parent -> child: ``TaskRequest``, or ``None`` to exit once idle.
- child -> parent: ``("ready", info)`` | ``("startup_error", error)`` |
  ``("progress", task_id, processed_seconds, total_seconds)`` |
  ``("result", task_id, payload)`` | ``("error", task_id, error)``.

Payload words are ``(start, end, word, probability, segment)`` tuples of plain
Python scalars: ~10k per hour of audio, and never numpy values, so the parent
can unpickle a result without importing numpy.
"""

from __future__ import annotations

import ctypes
import dataclasses
import enum
import importlib.metadata
import math
import multiprocessing
import multiprocessing.connection
import os
import resource
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Callable, Protocol

SAMPLE_RATE = 16000
THREAD_ENV_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")
WARMUP_FILE_SECONDS = 30
WARMUP_NOISE_SECONDS = 10
PACKAGE_NAMES = ("faster-whisper", "ctranslate2", "av", "onnxruntime", "numpy")


@dataclass(frozen=True)
class WorkerSpec:
    """Everything one worker process needs; pickled into the spawn child."""

    slot: int
    generation: int  # 0 for the first process in a slot, +1 per respawn
    model_path: str
    cpu_threads: int
    compute_type: str
    batch_size: int
    beam_size: int
    language: str | None
    word_timestamps: bool
    warmup_audio: str | None
    progress_every: int
    affinity: tuple[int, ...] | None = None
    device: str = "cpu"  # cpu | cuda


@dataclass(frozen=True)
class TaskRequest:
    task_id: str
    audio_path: str
    language: str | None


class TaskFailed(Exception):
    """A classified task failure; the worker survives it."""

    def __init__(self, message: str, *, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent


ProgressFn = Callable[[float, float], None]


class Engine(Protocol):
    def ready_info(self) -> dict[str, Any]: ...

    def transcribe(self, request: TaskRequest, progress: ProgressFn) -> dict[str, Any]: ...


EngineFactory = Callable[[WorkerSpec], Engine]
WorkerTarget = Callable[[WorkerSpec, Connection, Connection], None]


def worker_main(spec: WorkerSpec, tasks: Connection, results: Connection) -> None:
    """Default pool worker target: faster-whisper."""
    run_worker(spec, tasks, results, WhisperEngine)


def run_worker(spec: WorkerSpec, tasks: Connection, results: Connection, engine_factory: EngineFactory) -> None:
    """Load one engine, report ready, then consume tasks until the sentinel.

    Shared by the real worker and test fakes, so the fakes exercise this loop too.
    """
    numpy_preloaded = "numpy" in sys.modules
    # Ctrl-C in a terminal signals the whole foreground process group. The
    # parent coordinates shutdown; a worker must not die mid-task on SIGINT.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    for name in THREAD_ENV_VARS:
        os.environ[name] = str(spec.cpu_threads)
    if spec.affinity and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, spec.affinity)
    _exit_with_parent()

    try:
        engine = engine_factory(spec)
    except BaseException as error:
        _send(results, ("startup_error", describe_error(error, permanent=True)))
        raise SystemExit(1) from None
    affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    info = {"pid": os.getpid(), "affinity": affinity, "numpy_preloaded": numpy_preloaded, **engine.ready_info()}
    if not _send(results, ("ready", info)):
        return

    while True:
        try:
            request = tasks.recv()
        except (EOFError, OSError):
            return  # the parent is gone
        if request is None:
            return

        def progress(processed: float, total: float, task_id: str = request.task_id) -> None:
            _send(results, ("progress", task_id, float(processed), float(total)))

        try:
            message = ("result", request.task_id, engine.transcribe(request, progress))
        except TaskFailed as error:
            message = ("error", request.task_id, describe_error(error, permanent=error.permanent))
        except Exception as error:
            message = ("error", request.task_id, describe_error(error, permanent=False))
        sent = _send(results, message)
        del message
        _return_freed_memory()
        if not sent:
            return


def describe_error(error: BaseException, *, permanent: bool) -> dict[str, Any]:
    """Picklable description of the exception being handled."""
    return {
        "type": type(error).__name__,
        "message": str(error),
        "permanent": permanent,
        "traceback": traceback.format_exc(),
    }


def _send(results: Connection, message: tuple) -> bool:
    try:
        results.send(message)
        return True
    except (OSError, EOFError):
        return False  # the parent is gone; the caller exits


def _return_freed_memory() -> None:
    """Give the finished task's freed heap back to the OS.

    glibc keeps freed arena pages for reuse: after episode 01 (65 min) a
    worker idled at 723 MiB RSS, trimmed it idles at 178 MiB (~20 ms). Six
    mostly idle workers would otherwise hold ~3 GiB between episodes.
    """
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except (OSError, AttributeError):  # not glibc
        pass


def _exit_with_parent() -> None:
    """Exit promptly if the server dies without shutting the pool down.

    An idle worker would notice anyway (EOF on its task pipe), but a busy one
    would otherwise finish a minutes-long episode as an orphan holding ~1.5 GiB.
    The spawn sentinel reaches EOF when the parent process dies, whichever of
    its threads started this worker.
    """
    parent = multiprocessing.parent_process()
    if parent is None or parent.sentinel is None:
        return

    def watch() -> None:
        multiprocessing.connection.wait([parent.sentinel])
        os._exit(1)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def jsonable(value: Any) -> Any:
    """Convert library dataclasses/enums into durable JSON values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    if isinstance(value, enum.Enum):
        return jsonable(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)  # strict JSON (and Starlette's encoder) rejects inf/nan
    return value


def package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in PACKAGE_NAMES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def preload_cuda_libraries() -> None:
    """Load the pip-installed cuBLAS and cuDNN so CTranslate2 finds them.

    CTranslate2 dlopens ``libcublas.so.12`` and ``libcudnn*.so.9`` by soname
    on first CUDA use. The nvidia-* wheels put them under site-packages, which
    is not on the loader path, and LD_LIBRARY_PATH is read only at process
    start. Loading them RTLD_GLOBAL first satisfies the later dlopen by soname.
    System-wide libraries (if any) still work: then the wheels are absent.
    """
    import importlib.util

    for package, pattern in (("nvidia.cublas", "libcublas*.so.12"), ("nvidia.cudnn", "libcudnn*.so.9")):
        found = importlib.util.find_spec(package)
        if found is None or not found.submodule_search_locations:
            continue
        lib_dir = Path(next(iter(found.submodule_search_locations))) / "lib"
        # Dependencies first: libcublas needs libcublasLt; the cudnn_* sub-libraries need libcudnn.
        first = ("libcublasLt.so.12", "libcudnn.so.9")
        for path in sorted(lib_dir.glob(pattern), key=lambda p: (p.name not in first, p.name)):
            ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)


class WhisperEngine:
    """faster-whisper's BatchedInferencePipeline with the benchmark's settings."""

    def __init__(self, spec: WorkerSpec) -> None:
        import tqdm
        from faster_whisper import BatchedInferencePipeline, WhisperModel

        from .audio import decode_audio  # not faster-whisper's: it stops at the first corrupt packet

        # faster-whisper builds a tqdm bar on every call, even with progress
        # off, and tqdm's default lock is a multiprocessing RLock: a named
        # semaphore that a SIGKILLed worker leaks. One process needs a thread lock.
        tqdm.tqdm.set_lock(threading.RLock())
        self._spec = spec
        self._decode_audio = decode_audio
        if spec.device == "cuda":
            preload_cuda_libraries()
        load_start = time.perf_counter()
        model = WhisperModel(
            spec.model_path, device=spec.device, compute_type=spec.compute_type,
            cpu_threads=spec.cpu_threads, num_workers=1, local_files_only=True,
        )
        self._pipeline = BatchedInferencePipeline(model)
        self._model_load_seconds = time.perf_counter() - load_start
        self._versions = package_versions()
        self._warmup = self._run_warmup()

    def ready_info(self) -> dict[str, Any]:
        return {"engine": "faster-whisper", "device": self._spec.device,
                "compute_type": self._spec.compute_type, "model_load_seconds": self._model_load_seconds,
                "versions": self._versions, **self._warmup}

    def _run(self, audio: Any, language: str | None, *, vad_filter: bool) -> tuple[Any, Any]:
        # The pipeline carries word-alignment state across calls and zeroes it
        # only when a segment generator is exhausted; a failed task would
        # otherwise leave it stale and shift the next episode's word timings.
        self._pipeline.last_speech_timestamp = 0.0
        # vad_parameters stays None: faster-whisper then builds
        # VadOptions(max_speech_duration_s=chunk_length, min_silence_duration_ms=160).
        # Never pass a dict containing max_speech_duration_s (the batched
        # pipeline pops it and silently drops every other key set alongside
        # it), and never pass clip_timestamps (each speech chunk becomes its own
        # 30 s-padded batch item: 122 -> 1,348 encoder chunks on episode 01).
        return self._pipeline.transcribe(
            audio, language=language, beam_size=self._spec.beam_size,
            batch_size=self._spec.batch_size, vad_filter=vad_filter,
            condition_on_previous_text=False, word_timestamps=self._spec.word_timestamps,
        )

    def _run_warmup(self) -> dict[str, Any]:
        """Run the full inference path once so "ready" means "can transcribe".

        With ``warmup_audio`` set, its first 30 s go through the exact task
        path (as in the benchmark); pick a clip that opens with speech. The
        default is 10 s of seeded white noise with the VAD *off*: Silero
        rejects noise, so with it on the encoder would never run, whereas off,
        the one 30 s window goes through the encoder, beam search, and word
        alignment (tiny.en deterministically decodes it as " you"; the whole
        warmup takes ~0.35 s at 2 threads). The VAD's ONNX session is exercised
        separately so a broken onnxruntime fails here, not on the first episode.
        """
        import numpy as np

        start = time.perf_counter()
        if self._spec.warmup_audio:
            warm_audio = self._decode_audio(self._spec.warmup_audio)[0][: WARMUP_FILE_SECONDS * SAMPLE_RATE]
            segments, _ = self._run(warm_audio, self._spec.language, vad_filter=True)
            source = str(self._spec.warmup_audio)
        else:
            from faster_whisper.vad import VadOptions, get_speech_timestamps

            rng = np.random.default_rng(0)
            warm_audio = (rng.standard_normal(WARMUP_NOISE_SECONDS * SAMPLE_RATE) * 0.1).astype(np.float32)
            get_speech_timestamps(warm_audio, VadOptions())
            segments, _ = self._run(warm_audio, self._spec.language, vad_filter=False)
            source = "noise"
        warmup_words = sum(len(segment.words or ()) for segment in segments)
        # Slices retain the full decoded episode as their numpy backing array.
        # Release it before the ready barrier so an idle worker does not pin it.
        del warm_audio, segments
        return {"warmup_seconds": time.perf_counter() - start, "warmup_source": source,
                "warmup_words": warmup_words}

    def transcribe(self, request: TaskRequest, progress: ProgressFn) -> dict[str, Any]:
        decode_start = time.perf_counter()
        try:
            audio, skipped_packets = self._decode_audio(request.audio_path)
        except MemoryError:
            raise
        except OSError as error:  # missing/unreadable file: environmental, not the content
            raise TaskFailed(f"cannot read audio: {error}", permanent=False) from error
        except Exception as error:  # PyAV InvalidDataError, no audio stream (IndexError), ...
            raise TaskFailed(f"undecodable audio: {type(error).__name__}: {error}", permanent=True) from error
        decode_seconds = time.perf_counter() - decode_start
        if not len(audio):
            raise TaskFailed("audio decoded to zero samples", permanent=True)
        duration = len(audio) / SAMPLE_RATE  # the authoritative episode duration
        progress(0.0, duration)

        cpu_start = time.process_time()
        start = time.perf_counter()
        segments, info = self._run(audio, request.language or self._spec.language, vad_filter=True)
        words: list[tuple[float, float, str, float, int]] = []
        metas: list[tuple[int, float, float, float, float, float]] = []
        for index, segment in enumerate(segments):
            metas.append((index, float(segment.start), float(segment.end), float(segment.compression_ratio),
                          float(segment.no_speech_prob), float(segment.avg_logprob)))
            for word in segment.words or ():
                words.append((float(word.start), float(word.end), str(word.word), float(word.probability), index))
            if (index + 1) % self._spec.progress_every == 0:
                progress(float(segment.end), duration)
        transcribe_seconds = time.perf_counter() - start

        transcription = jsonable(info.transcription_options)
        # The batched pipeline stores this episode's VAD speech chunks here.
        # That is data, not configuration, and would make every episode's
        # options differ; the joiner derives silences from word gaps instead.
        transcription.pop("clip_timestamps", None)
        return {
            "engine": "faster-whisper",
            "duration_seconds": duration,
            "duration_after_vad": float(info.duration_after_vad),
            "language": str(info.language),
            "language_probability": float(info.language_probability),
            "words": words,
            "segments": metas,
            "decode_seconds": decode_seconds,
            "transcribe_seconds": transcribe_seconds,
            "cpu_seconds": time.process_time() - cpu_start,
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            "options": {
                "pipeline": "batched", "device": self._spec.device, "compute_type": self._spec.compute_type,
                "cpu_threads": self._spec.cpu_threads, "batch_size": self._spec.batch_size,
                "decode_skipped_packets": skipped_packets,
                "vad_filter": True, "transcription_options": transcription,
                "vad_options": jsonable(info.vad_options), "versions": self._versions,
            },
        }
