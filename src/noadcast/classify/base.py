"""Classifier contract. Every provider implementation (Gemini, Claude, the
Gemini audio control arm, and the replaying fake) satisfies ``Classifier``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, Sequence

from ..transcribe.protocol import Sentence, SilenceRegion

SegmentKind = Literal["ad", "intro", "outro"]
SEGMENT_KINDS: tuple[str, ...] = ("ad", "intro", "outro")


@dataclass(frozen=True)
class ClassifyRequest:
    sentences: Sequence[Sentence]
    silences: Sequence[SilenceRegion]
    episode_duration: float | None  # measured duration when known
    episode_title: str | None = None
    podcast_title: str | None = None
    language: str = "en"
    # Only the audio control-arm classifier reads these.
    audio_path: str | None = None
    audio_content_type: str | None = None


@dataclass(frozen=True)
class TokenUsage:
    # The whole prompt for every provider, including cache reads and cache
    # writes (Anthropic reports those separately; they are summed in here so
    # totals compare with Gemini's promptTokenCount). The cached_* fields
    # below are subsets of this number, priced at their own rates.
    input_tokens: int = 0
    # Gemini reports thinking tokens separately. Anthropic bills thinking
    # inside output_tokens and reports no separate count, so this is 0 for
    # Claude — render it as provider-dependent, never as a universal metric.
    thought_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.thought_tokens + other.thought_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True)
class DetectedSegment:
    start_seconds: float
    end_seconds: float
    summary: str
    kind: SegmentKind
    # Transcript line indices the model cited (index render format only).
    start_line: int | None = None
    end_line: int | None = None


@dataclass(frozen=True)
class ClassifyResult:
    segments: list[DetectedSegment]  # as returned, seconds resolved from line indices; NOT yet sanitised
    usage: TokenUsage
    provider: str
    model: str
    thinking: str | None
    prompt_version: str
    render_format: str
    include_silence: bool
    chunk_count: int
    latency_ms: int
    attempts: int
    request_sha256: str
    raw_response: dict[str, Any] = field(default_factory=dict)


class ClassifierError(Exception):
    """Classification failed. ``permanent`` errors (auth, bad request, repeated
    schema violations) must not be retried; ``retry_after`` honours provider hints."""

    def __init__(self, message: str, *, permanent: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.retry_after = retry_after


class Classifier(Protocol):
    provider: str
    model: str

    async def classify(self, req: ClassifyRequest) -> ClassifyResult: ...

    async def aclose(self) -> None: ...
