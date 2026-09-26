"""Post-processing of classifier output.

Port of the recovered server's ``sanitize_segments`` with three changes:

1. **Intros start at 0.0.** The original clamped every start to the first
   transcript timestamp. Under whisper.cpp that was ~0; under VAD the first
   word lands 0.69 s in on TAL and 10-30 s into music-heavy shows, so an
   intro would never skip its opening music. An intro that starts at or
   just after the first word therefore starts at 0.0; that also covers the
   index format, where a cited first line resolves to the first word's time.
   An intro after a cold open keeps its start.
2. **The outro extends to the endpoint conditionally**: only when the stretch
   after it holds at most ``OUTRO_MAX_SPEECH_S`` of speech and spans at most
   ``OUTRO_MAX_GAP_S``. Trailing non-speech measured 7.4-39 s on TAL, but a
   wrong duration must not skip real content. Other ends clamp to the episode
   endpoint rather than the transcript end, since ads can run into music.
3. **Boundaries snap into silence** (``snap_to_silence``): mid-word skips are
   the most audible failure mode.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Sequence

from ..transcribe.protocol import Sentence, SilenceRegion
from .base import SEGMENT_KINDS, ClassifyRequest, DetectedSegment
from .prompts import valid_episode_endpoint

# An intro that starts no later than this after the first word starts at 0.0:
# nothing but music or silence precedes it, and echoed seconds round.
INTRO_HEAD_TOLERANCE_S = 1.0
OUTRO_MAX_GAP_S = 300.0
OUTRO_MAX_SPEECH_S = 5.0
SNAP_WINDOW_S = 2.0


def finalize(
    segments: Sequence[DetectedSegment], req: ClassifyRequest, *, snap: bool = True
) -> list[DetectedSegment]:
    """The single entry point the pipeline calls: sanitise, then optionally snap."""
    cleaned = sanitize_segments(segments, req.sentences, req.episode_duration)
    if snap:
        upper = _upper_bound(req.sentences, req.episode_duration)
        cleaned = [_snap_segment(seg, req.silences, upper) for seg in cleaned]
        cleaned = [seg for seg in cleaned if seg.end_seconds > seg.start_seconds]
    return merge_overlapping(cleaned)


def sanitize_segments(
    segments: Sequence[DetectedSegment],
    transcript: Sequence[Sentence],
    episode_duration: float | None = None,
) -> list[DetectedSegment]:
    """Drop unknown kinds and non-finite or inverted segments, clamp the rest
    to the audio, and sort by start."""
    if not transcript:
        return []
    min_start = max(0.0, min(sentence.start for sentence in transcript))
    transcript_end = max(sentence.end for sentence in transcript)
    endpoint = valid_episode_endpoint(episode_duration, transcript)
    upper = endpoint if endpoint is not None else transcript_end

    cleaned: list[DetectedSegment] = []
    for segment in segments:
        if segment.kind not in SEGMENT_KINDS:
            continue
        if not math.isfinite(segment.start_seconds) or not math.isfinite(segment.end_seconds):
            continue
        if segment.kind == "intro" and segment.start_seconds <= min_start + INTRO_HEAD_TOLERANCE_S:
            start = 0.0
        else:
            start = min(max(segment.start_seconds, min_start), transcript_end)
        if segment.kind == "outro":
            end = _outro_end(segment.end_seconds, transcript, min_start, transcript_end, endpoint)
        else:
            end = min(max(segment.end_seconds, min_start), upper)
        if end <= start:
            continue
        cleaned.append(replace(segment, start_seconds=start, end_seconds=end))
    return sorted(cleaned, key=lambda item: item.start_seconds)


def _outro_end(
    end: float,
    transcript: Sequence[Sentence],
    min_start: float,
    transcript_end: float,
    endpoint: float | None,
) -> float:
    if endpoint is None:
        return min(max(end, min_start), transcript_end)
    end = min(max(end, min_start), endpoint)
    if endpoint - end <= OUTRO_MAX_GAP_S and speech_seconds(transcript, end, endpoint) <= OUTRO_MAX_SPEECH_S:
        return endpoint
    return end


def speech_seconds(transcript: Sequence[Sentence], lo: float, hi: float) -> float:
    """Seconds of transcribed speech inside ``[lo, hi]``. The silence map is
    derived from the same word gaps, so the remainder of the span is silence;
    measuring sentences directly also works when no silence map is supplied."""
    return sum(max(0.0, min(s.end, hi) - max(s.start, lo)) for s in transcript)


def snap_to_silence(t: float, regions: Sequence[SilenceRegion], window: float = SNAP_WINDOW_S) -> float:
    """Move ``t`` to the midpoint of the nearest silence within ``window``
    seconds, moving at most ``window``: next to a long music bed the boundary
    goes ``window`` seconds into it rather than to its middle, which would
    discard half of a stretch the model chose to keep (or skip). The target
    always lies inside the region. No region in reach leaves ``t`` alone."""
    best: SilenceRegion | None = None
    best_key: tuple[float, float] | None = None
    for region in regions:
        if region.end <= region.start:
            continue
        distance = max(region.start - t, t - region.end, 0.0)
        if distance > window:
            continue
        key = (distance, abs((region.start + region.end) / 2 - t))
        if best_key is None or key < best_key:
            best, best_key = region, key
    if best is None:
        return t
    target = min(max((best.start + best.end) / 2, t - window), t + window)
    return min(max(target, best.start), best.end)


def _snap_segment(segment: DetectedSegment, regions: Sequence[SilenceRegion], upper: float) -> DetectedSegment:
    def snap(t: float) -> float:
        # A boundary on an audio edge (an intro pinned to 0.0, an ad running
        # to the endpoint) cannot cut a word; pulling it inward would only
        # un-skip audio.
        if t <= 0.0 or t >= upper:
            return t
        return min(max(snap_to_silence(t, regions), 0.0), upper)

    # The outro end is governed by the conditional extension, not by snapping.
    end = segment.end_seconds if segment.kind == "outro" else snap(segment.end_seconds)
    return replace(segment, start_seconds=snap(segment.start_seconds), end_seconds=end)


def _upper_bound(transcript: Sequence[Sentence], episode_duration: float | None) -> float:
    endpoint = valid_episode_endpoint(episode_duration, transcript)
    if endpoint is not None:
        return endpoint
    return max((sentence.end for sentence in transcript), default=0.0)


def merge_overlapping(segments: Sequence[DetectedSegment]) -> list[DetectedSegment]:
    """Merge same-kind segments that overlap or touch; sort by start."""
    merged: list[DetectedSegment] = []
    last_of_kind: dict[str, int] = {}
    for segment in sorted(segments, key=lambda item: (item.start_seconds, item.end_seconds)):
        index = last_of_kind.get(segment.kind)
        if index is not None and segment.start_seconds <= merged[index].end_seconds:
            previous = merged[index]
            merged[index] = replace(
                previous,
                end_seconds=max(previous.end_seconds, segment.end_seconds),
                summary=_join_summaries(previous.summary, segment.summary),
                end_line=_later_line(previous, segment),
            )
            continue
        last_of_kind[segment.kind] = len(merged)
        merged.append(segment)
    return merged


def _join_summaries(first: str, second: str) -> str:
    if not second or second == first:
        return first
    if not first:
        return second
    return f"{first}; {second}"


def _later_line(first: DetectedSegment, second: DetectedSegment) -> int | None:
    return second.end_line if second.end_seconds > first.end_seconds else first.end_line
