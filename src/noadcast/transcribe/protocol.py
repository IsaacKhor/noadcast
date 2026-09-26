"""Types shared by the transcription pool, the sentence joiner, the
classifier, and the pipeline. This module is the contract between them; keep
it dependency-free (no faster_whisper import) so the web process and tests can
import it cheaply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass(frozen=True, slots=True)
class Word:
    """One ASR word with wall-clock timing in seconds.

    ``word`` is the raw faster-whisper token text *including* its leading
    space (``" quick"``) and any punctuation faster-whisper merged onto it
    (``" warning,"``). Reconstruct text with ``"".join(w.word for w in ws).strip()``;
    never ``" ".join(...)``.
    """

    start: float
    end: float
    word: str
    probability: float
    segment: int  # index of the ASR segment it came from; provenance only


@dataclass(frozen=True, slots=True)
class AsrSegmentMeta:
    """Per ASR segment diagnostics. In the batched pipeline these are VAD
    chunks, not sentences, and faster-whisper never acts on the quality
    fields — they are informational."""

    index: int
    start: float
    end: float
    compression_ratio: float
    no_speech_prob: float
    avg_logprob: float


@dataclass(frozen=True)
class TranscribeTask:
    task_id: str
    audio_path: str  # absolute path; audio never crosses the process boundary
    language: str | None = None  # None: use the pool's configured language


@dataclass(frozen=True)
class TranscribeProgress:
    task_id: str
    processed_seconds: float  # end time of the last emitted segment
    total_seconds: float


ProgressCallback = Callable[[TranscribeProgress], None]


@dataclass(frozen=True)
class TranscribeResult:
    task_id: str
    duration_seconds: float  # decoded audio length: the authoritative episode duration
    duration_after_vad: float
    language: str
    language_probability: float
    words: list[Word]
    segments: list[AsrSegmentMeta]
    decode_seconds: float
    transcribe_seconds: float
    engine: str  # "faster-whisper"
    model_id: str
    model_sha256: str | None
    options: dict[str, Any] = field(default_factory=dict)  # effective transcription + VAD options, JSON-able


class Transcriber(Protocol):
    """Implemented by ``transcribe.pool.TranscriptionPool`` and by test fakes."""

    async def transcribe(
        self, task: TranscribeTask, on_progress: ProgressCallback | None = None
    ) -> TranscribeResult: ...


class TranscriptionError(Exception):
    """Transcription failed. ``permanent`` failures (corrupt audio, zero
    speech, repeated worker crashes on the same input) must not be retried."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class WorkerCrashed(TranscriptionError):
    """The worker process handling the task died (OOM, segfault)."""


@dataclass(frozen=True, slots=True)
class Sentence:
    """A joined, sentence-level transcript line."""

    index: int
    start: float  # first word start
    end: float  # last word end
    text: str
    word_start: int  # index into the flat word list
    word_count: int
    break_reason: str  # punct | gap | cap | eof
    soft_end: bool  # True when a length cap forced the break
    min_p: float
    mean_p: float
    asr_segments: tuple[int, ...] = ()
    flags: tuple[str, ...] = ()  # low_confidence | repetitive | high_compression | long_span


@dataclass(frozen=True, slots=True)
class SilenceRegion:
    """A stretch with no transcribed words, derived from word gaps."""

    start: float
    end: float
    kind: str  # head | gap | tail
