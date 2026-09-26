"""Chunking guard for transcripts too long for one request.

Measured need is nil: ~19.4k input tokens for a 60-minute episode and ~58k
for three hours, against a 120k threshold. Chunking also degrades exactly
the whole-episode judgement (intro and outro placement) the prompt cares
most about, so this only runs when the estimate exceeds
``settings.classifier_max_input_tokens``. Windows split on sentence
boundaries; an intro is accepted only from the first window and an outro
only from the last.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Sequence

from ..transcribe.protocol import Sentence, SilenceRegion
from .base import DetectedSegment

WINDOW_S = 45 * 60.0
OVERLAP_S = 3 * 60.0
CHARS_PER_TOKEN = 3.6
AD_MERGE_GAP_S = 5.0


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@dataclass(frozen=True)
class ChunkPlan:
    index: int
    count: int
    role: str  # only | first | middle | last
    first: int  # sentence indices, inclusive
    last: int
    start_s: float  # silences overlapping [start_s, end_s) belong to this chunk
    end_s: float

    @property
    def allows_intro(self) -> bool:
        return self.role in ("only", "first")

    @property
    def allows_outro(self) -> bool:
        return self.role in ("only", "last")


def plan_chunks(
    sentences: Sequence[Sentence], *, window_s: float = WINDOW_S, overlap_s: float = OVERLAP_S
) -> list[ChunkPlan]:
    """Windows of whole sentences spanning at most ``window_s``, each starting
    ``overlap_s`` before its predecessor ends so an ad straddling a seam is
    seen whole at least once."""
    if not sentences:
        return []
    spans: list[tuple[int, int]] = []
    first = 0
    while True:
        last = first
        while last + 1 < len(sentences) and sentences[last + 1].end - sentences[first].start <= window_s:
            last += 1
        spans.append((first, last))
        if last == len(sentences) - 1:
            break
        resume = sentences[last].end - overlap_s
        first = next(i for i in range(first + 1, last + 2) if i == last + 1 or sentences[i].start >= resume)

    count = len(spans)
    plans = []
    for index, (first, last) in enumerate(spans):
        if count == 1:
            role = "only"
        elif index == 0:
            role = "first"
        elif index == count - 1:
            role = "last"
        else:
            role = "middle"
        plans.append(
            ChunkPlan(
                index=index,
                count=count,
                role=role,
                first=first,
                last=last,
                start_s=0.0 if index == 0 else sentences[first].start,
                end_s=math.inf if index == count - 1 else sentences[last].end,
            )
        )
    return plans


def chunk_silences(plan: ChunkPlan, silences: Sequence[SilenceRegion]) -> list[SilenceRegion]:
    """Regions inside the chunk; the head only reaches the first chunk and the
    tail only the last, and a gap on a seam goes to the chunk it lies within."""
    return [region for region in silences if region.end > plan.start_s and region.start < plan.end_s]


def stitch_chunks(parts: Sequence[tuple[ChunkPlan, Sequence[DetectedSegment]]]) -> list[DetectedSegment]:
    """Merge per-chunk answers (seconds already resolved): keep the earliest
    intro from the first chunk and the latest outro from the last, and merge
    ads that overlap (so any pair with IoU > 0.5) or sit < 5 s apart, which
    is how one ad seen from both sides of a seam looks. Line citations are
    chunk-local, so they are cleared."""
    intros: list[DetectedSegment] = []
    outros: list[DetectedSegment] = []
    ads: list[DetectedSegment] = []
    for plan, segments in parts:
        for segment in segments:
            if segment.kind == "intro" and plan.allows_intro:
                intros.append(segment)
            elif segment.kind == "outro" and plan.allows_outro:
                outros.append(segment)
            elif segment.kind == "ad":
                ads.append(segment)

    merged: list[DetectedSegment] = []
    for ad in sorted(ads, key=lambda item: item.start_seconds):
        if merged and ad.start_seconds - merged[-1].end_seconds < AD_MERGE_GAP_S:
            previous = merged[-1]
            merged[-1] = replace(previous, end_seconds=max(previous.end_seconds, ad.end_seconds))
        else:
            merged.append(ad)
    if intros:
        merged.append(min(intros, key=lambda item: item.start_seconds))
    if outros:
        merged.append(max(outros, key=lambda item: item.end_seconds))
    return sorted(
        (replace(segment, start_line=None, end_line=None) for segment in merged),
        key=lambda item: item.start_seconds,
    )
