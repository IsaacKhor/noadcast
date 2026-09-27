"""Contract for OpenRouter classification and injected test doubles."""

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
    # Audio context retained for stored/evaluation request compatibility.
    audio_path: str | None = None
    audio_content_type: str | None = None


@dataclass(frozen=True)
class TokenUsage:
    # The whole prompt, including cache reads and writes. The cache fields
    # below are subsets of this number, priced at their own rates.
    input_tokens: int = 0
    # Reasoning is separated from visible output, so their sum equals
    # OpenRouter's completion_tokens without double counting.
    thought_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    # OpenRouter's actual account charge. Kept separate from token counts;
    # retries and chunks accumulate it just as they accumulate billed tokens.
    billed_cost_usd: float | None = None

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        # Empty retry/accumulator usage is neutral. If an actual billed
        # response omitted its cost, keep the aggregate unknown so pricing
        # can fall back for the whole call instead of recording a partial sum.
        missing_charge = any(
            item.billed_cost_usd is None
            and (item.input_tokens or item.thought_tokens or item.output_tokens)
            for item in (self, other)
        )
        billed_cost = None
        if not missing_charge and (self.billed_cost_usd is not None or other.billed_cost_usd is not None):
            billed_cost = (self.billed_cost_usd or 0.0) + (other.billed_cost_usd or 0.0)
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.thought_tokens + other.thought_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            billed_cost,
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
