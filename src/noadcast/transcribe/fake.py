"""Test doubles for transcription.

``FakeTranscriber`` implements the ``Transcriber`` protocol in-process by
replaying recorded words: no processes, no faster-whisper, milliseconds per
episode. Typical e2e use::

    fake = FakeTranscriber.from_run_dir("benchmarks/tal/runs/wt-crossover-2-on-20260922")
    fake.fail_next("01-646", "transient", "crash")   # optional: script the next outcomes
    result = await fake.transcribe(TranscribeTask("job-7", "/srv/data/audio/3/41.mp3"), on_progress)

A task's audio is matched to a recording by exact path, then file name
(``01-646.mp3``), then stem (``01-646``), then the sha256 of the file's
bytes, so episodes the server stored under its own ids still replay when the
bytes are the corpus MP3s (benchmark transcripts record their source sha256).
Recordings are benchmark-run transcripts written with ``--word-timestamps``
(``segments[*].words[*]``) or the joiner's compact fixtures
(``tests/fixtures/words_*.json.gz``). An unknown file fails permanently, as
undecodable audio would.

``scripted_worker_main`` is a pool worker target that exercises the real
``TranscriptionPool`` (spawn, pipes, crashes, respawn) without faster-whisper;
the "audio file" it receives is a JSON script, see ``ScriptedEngine``.
"""

from __future__ import annotations

import asyncio
import collections
import functools
import gzip
import hashlib
import json
import os
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Mapping

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
from .worker import ProgressFn, TaskFailed, TaskRequest, WorkerSpec, run_worker

WORD_FIELDS = ("start", "end", "word", "probability", "segment")
SEGMENT_FIELDS = ("index", "start", "end", "compression_ratio", "no_speech_prob", "avg_logprob")
OUTCOMES = ("ok", "transient", "permanent", "crash")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Recording:
    """One replayable transcript."""

    path: str
    words: tuple[Word, ...]
    segments: tuple[AsrSegmentMeta, ...]
    duration_seconds: float
    duration_after_vad: float
    language: str
    source_name: str | None  # the audio file it was made from, e.g. "01-646.mp3"
    source_sha256: str | None
    model_id: str
    model_sha256: str | None
    options: dict[str, Any] = field(default_factory=dict)
    decode_seconds: float = 0.0
    transcribe_seconds: float = 0.0


def load_recording(path: str | os.PathLike[str]) -> Recording:
    """Parse a benchmark transcript or a compact word fixture (``.gz`` allowed)."""
    path = Path(path)
    raw = path.read_bytes()
    data = json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)
    if "words" in data:
        return _from_compact(path, data)
    if "segments" in data:
        return _from_benchmark(path, data)
    raise ValueError(f"{path} is neither a benchmark transcript nor a compact word fixture")


def _from_benchmark(path: Path, data: dict[str, Any]) -> Recording:
    segments, words = [], []
    for index, segment in enumerate(data["segments"]):
        if segment.get("words") is None:
            raise ValueError(f"{path} was transcribed without word timestamps")
        segments.append(AsrSegmentMeta(index, segment["start"], segment["end"], segment["compression_ratio"],
                                       segment["no_speech_prob"], segment["avg_logprob"]))
        words.extend(Word(w["start"], w["end"], w["word"], w["probability"], index) for w in segment["words"])
    episode, config = data.get("episode", {}), data.get("config", {})
    run = _benchmark_run(str(path.parent.parent / "results.json"))
    row = run["episodes"].get(f"transcripts/{path.name}", {})
    options: dict[str, Any] = {"replayed_from": str(path),
                               "recorded_config": {k: v for k, v in config.items() if k != "model_path"}}
    if row:
        transcription = dict(row.get("effective_transcription_options") or {})
        transcription.pop("clip_timestamps", None)  # per-episode VAD chunks, dropped by the real pool too
        options.update(transcription_options=transcription, vad_options=row.get("effective_vad_options"))
    return Recording(
        path=str(path), words=tuple(words), segments=tuple(segments),
        # The run's decoded length is what the real pool reports; the manifest's
        # probe (episode.duration_seconds) is the fallback.
        duration_seconds=row.get("full_decoded_audio_seconds") or episode.get("duration_seconds")
        or (words[-1].end if words else 0.0),
        duration_after_vad=row.get("speech_seconds_after_vad") or sum(s.end - s.start for s in segments),
        language=config.get("language") or "en",
        source_name=Path(episode["path"]).name if episode.get("path") else None,
        source_sha256=episode.get("sha256"), model_id=config.get("model", "Systran/faster-whisper-tiny.en"),
        model_sha256=run["model_sha256"], options=options,
        decode_seconds=row.get("decode_seconds", 0.0), transcribe_seconds=row.get("transcribe_seconds", 0.0),
    )


@functools.lru_cache(maxsize=16)
def _benchmark_run(results_path: str) -> dict[str, Any]:
    """The run's results.json rows by transcript path, if the run directory has one."""
    try:
        results = json.loads(Path(results_path).read_text())
    except (OSError, ValueError):
        return {"episodes": {}, "model_sha256": None}
    return {"episodes": {row["transcript_json"]: row for row in results.get("episodes", []) if "transcript_json" in row},
            "model_sha256": results.get("model", {}).get("model_bin_sha256")}


def _from_compact(path: Path, data: dict[str, Any]) -> Recording:
    word_fields = data.get("word_fields", WORD_FIELDS)
    segment_fields = data.get("segment_fields", SEGMENT_FIELDS)
    words = tuple(Word(**dict(zip(word_fields, row))) for row in data["words"])
    segments = tuple(AsrSegmentMeta(**dict(zip(segment_fields, row))) for row in data.get("segments", []))
    source = data.get("source", {})
    episode, asr = source.get("episode", {}), source.get("asr", {})
    # A fixture cut at N seconds stands for the first N seconds of the episode.
    covered = data.get("cut_seconds") or episode.get("duration_seconds") or 0.0
    return Recording(
        path=str(path), words=words, segments=segments,
        duration_seconds=max(covered, words[-1].end if words else 0.0),
        duration_after_vad=sum(s.end - s.start for s in segments),
        language=asr.get("language") or "en", source_name=None, source_sha256=episode.get("audio_sha256"),
        model_id=asr.get("model", "Systran/faster-whisper-tiny.en"), model_sha256=None,
        options={"replayed_from": str(path), "recorded_config": asr},
    )


class FakeTranscriber:
    """Replays recordings through the ``Transcriber`` protocol; see the module docstring.

    ``recordings`` maps a key (path, file name, stem, or audio sha256) to a
    recording file or a loaded ``Recording``. Progress is reported like the
    real pool: once at 0 s, then every ``progress_every`` ASR segments.
    ``latency_seconds`` is spread across those steps. ``calls`` and
    ``max_in_flight`` are there for assertions.
    """

    def __init__(self, recordings: Mapping[str, str | os.PathLike[str] | Recording], *,
                 latency_seconds: float = 0.0, progress_every: int = 32, crash_limit: int = 4) -> None:
        if not recordings:
            raise ValueError("FakeTranscriber needs at least one recording")
        self._sources = dict(recordings)
        self._loaded: dict[str, Recording] = {}
        self._digests: dict[tuple[str, int, int], str] = {}
        self._scripts: dict[str, collections.deque[str]] = {}
        self._crashes: dict[str, int] = {}
        self.latency_seconds = latency_seconds
        self.progress_every = progress_every
        self.crash_limit = crash_limit
        self.calls: list[TranscribeTask] = []
        self.in_flight = 0
        self.max_in_flight = 0

    @classmethod
    def from_run_dir(cls, run_dir: str | os.PathLike[str], **kwargs: Any) -> FakeTranscriber:
        """Every ``transcripts/<stem>.json`` in a benchmark run, keyed by stem,
        source file name, and source sha256."""
        recordings: dict[str, Recording] = {}
        for path in sorted(Path(run_dir, "transcripts").glob("*.json")):
            recording = load_recording(path)
            for key in (path.stem, recording.source_name, recording.source_sha256):
                if key:
                    recordings[key] = recording
        if not recordings:
            raise ValueError(f"no transcripts under {Path(run_dir) / 'transcripts'}")
        return cls(recordings, **kwargs)

    def fail_next(self, key: str, *outcomes: str) -> None:
        """Script the next calls for one recording (any of its keys): each
        outcome is ``ok``, ``transient``, ``permanent``, or ``crash``
        (``WorkerCrashed``, permanent on the ``crash_limit``-th, like the pool)."""
        unknown = set(outcomes) - set(OUTCOMES)
        if unknown:
            raise ValueError(f"unknown outcomes {sorted(unknown)}; expected {OUTCOMES}")
        self._scripts.setdefault(self._recording(key).path, collections.deque()).extend(outcomes)

    def _recording(self, key: str) -> Recording:
        loaded = self._loaded.get(key)
        if loaded is None:
            source = self._sources[key]
            loaded = self._loaded[key] = source if isinstance(source, Recording) else load_recording(source)
        return loaded

    def _lookup(self, audio_path: str) -> Recording:
        path = Path(audio_path)
        for key in (audio_path, path.name, path.stem):
            if key in self._sources:
                return self._recording(key)
        if path.is_file() and any(_SHA256.fullmatch(key) for key in self._sources):
            stat = path.stat()
            identity = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
            if identity not in self._digests:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    while block := stream.read(8 * 1024 * 1024):
                        digest.update(block)
                self._digests[identity] = digest.hexdigest()
            if self._digests[identity] in self._sources:
                return self._recording(self._digests[identity])
        raise TranscriptionError(f"no recorded transcript for {audio_path}", permanent=True)

    def _failure(self, outcome: str, recording: Recording) -> TranscriptionError:
        if outcome == "crash":
            count = self._crashes[recording.path] = self._crashes.get(recording.path, 0) + 1
            return WorkerCrashed(f"scripted worker crash {count} of {self.crash_limit} for {recording.path}",
                                 permanent=count >= self.crash_limit)
        return TranscriptionError(f"scripted {outcome} failure for {recording.path}", permanent=outcome == "permanent")

    async def transcribe(self, task: TranscribeTask, on_progress: ProgressCallback | None = None) -> TranscribeResult:
        self.calls.append(task)
        recording = self._lookup(task.audio_path)
        script = self._scripts.get(recording.path)
        outcome = script.popleft() if script else "ok"
        marks = [0.0] + [s.end for i, s in enumerate(recording.segments) if (i + 1) % self.progress_every == 0]
        pause = self.latency_seconds / len(marks)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            for step, processed in enumerate(marks):
                if on_progress is not None:
                    on_progress(TranscribeProgress(task.task_id, processed, recording.duration_seconds))
                if step == 0 and outcome != "ok":
                    raise self._failure(outcome, recording)  # mid-task, as a real crash would be
                await asyncio.sleep(pause)
        finally:
            self.in_flight -= 1
        self._crashes.pop(recording.path, None)
        return TranscribeResult(
            task_id=task.task_id, duration_seconds=recording.duration_seconds,
            duration_after_vad=recording.duration_after_vad, language=task.language or recording.language,
            language_probability=1.0, words=list(recording.words), segments=list(recording.segments),
            decode_seconds=recording.decode_seconds, transcribe_seconds=recording.transcribe_seconds,
            engine="faster-whisper", model_id=recording.model_id, model_sha256=recording.model_sha256,
            options=dict(recording.options),
        )


class ScriptedEngine:
    """Pool worker engine for tests: each "audio file" is a JSON script.

    Script keys, all optional: ``words`` (count, default 12), ``segments``
    (default 3), ``duration`` (seconds, default ``words``), ``text`` (word
    prefix, default the file stem), ``delay`` (seconds slept per segment),
    and ``behavior``: ``ok``; ``crash`` (SIGKILLs itself after the first
    progress message); ``exit`` (``os._exit(3)``); ``hang``; ``error``
    (transient); ``permanent``; ``undecodable``; ``empty`` (zero words).
    ``fail_times: N`` misbehaves only on the first N attempts, counted in
    ``<audio>.attempts`` so the count survives respawns. Startup is scripted
    by ``startup.json`` in the model directory: ``{"fail": true}``,
    ``{"fail_generations": [1]}``, or ``{"delay": seconds}``.
    """

    def __init__(self, spec: WorkerSpec) -> None:
        self._spec = spec
        control = Path(spec.model_path) / "startup.json"
        startup = json.loads(control.read_text()) if control.is_file() else {}
        if startup.get("fail") or spec.generation in startup.get("fail_generations", ()):
            raise RuntimeError("scripted startup failure")
        self._startup_delay = float(startup.get("delay", 0.0))
        time.sleep(self._startup_delay)

    def ready_info(self) -> dict[str, Any]:
        return {"engine": "scripted", "model_load_seconds": 0.0, "warmup_seconds": self._startup_delay}

    def transcribe(self, request: TaskRequest, progress: ProgressFn) -> dict[str, Any]:
        path = Path(request.audio_path)
        try:
            script = json.loads(path.read_text())
        except OSError as error:
            raise TaskFailed(f"cannot read audio: {error}", permanent=False) from error
        except ValueError as error:
            raise TaskFailed(f"undecodable audio: {error}", permanent=True) from error
        counter = path.with_name(path.name + ".attempts")
        attempt = int(counter.read_text()) + 1 if counter.is_file() else 1
        counter.write_text(str(attempt))
        behavior = script.get("behavior", "ok")
        if script.get("fail_times") is not None and attempt > script["fail_times"]:
            behavior = "ok"
        n_words, n_segments = int(script.get("words", 12)), max(1, int(script.get("segments", 3)))
        duration = float(script.get("duration", n_words or 1))
        progress(0.0, duration)
        if behavior == "crash":
            os.kill(os.getpid(), signal.SIGKILL)
        elif behavior == "exit":
            os._exit(3)
        elif behavior == "hang":
            threading.Event().wait()
        elif behavior == "error":
            raise RuntimeError("scripted transient failure")
        elif behavior in ("permanent", "undecodable"):
            raise TaskFailed(f"scripted {behavior} failure", permanent=True)
        elif behavior == "empty":
            n_words = 0

        started = time.time()
        text, step = script.get("text", path.stem), duration / max(n_words, 1)
        words = [(round(i * step, 2), round((i + 0.8) * step, 2), f" {text}-{i}", 0.9, i * n_segments // n_words)
                 for i in range(n_words)]
        segments = [(s, s * duration / n_segments, (s + 1) * duration / n_segments, 1.5, 0.01, -0.2)
                    for s in range(n_segments)]
        for s in range(n_segments):
            time.sleep(float(script.get("delay", 0.0)))
            if (s + 1) % self._spec.progress_every == 0:
                progress(segments[s][2], duration)
        return {
            "engine": "scripted", "duration_seconds": duration, "duration_after_vad": duration,
            "language": request.language or "en", "language_probability": 1.0, "words": words,
            "segments": segments, "decode_seconds": 0.0, "transcribe_seconds": time.time() - started,
            "cpu_seconds": 0.0, "peak_rss_mib": 0.0,
            "options": {"pid": os.getpid(), "slot": self._spec.slot, "generation": self._spec.generation,
                        "attempt": attempt, "started": started, "finished": time.time()},
        }


def scripted_worker_main(spec: WorkerSpec, tasks: Connection, results: Connection) -> None:
    """Pool worker target running ``ScriptedEngine`` through the real worker loop."""
    run_worker(spec, tasks, results, ScriptedEngine)
