"""Transcript → prompt text.

The production ``sentences`` format has one joined sentence per line:
``[22.24-23.88] A complete sentence.`` It has no silence rows. The older
formats remain available for evaluation:

- ``seconds``: ``[12.34 - 15.67] text`` per sentence. Without silence this is
  byte-for-byte the recovered server's ``format_transcript`` output.
- ``index``: ``i|start| text`` per sentence. The model cites line indices and
  the server resolves seconds from ``line_times``, which removes timestamp
  hallucination structurally and is ~22% cheaper per line (20.2 vs 25.9
  GPT-2 BPE tokens per line on TAL episode 01).

With silence on, the silence map is interleaved in time order as
``--- Ns of no speech ---`` lines (the tail adds ``(end of audio at T)``),
sharing one line index with the sentences, so a cited silence line resolves
to the silence's own bounds.

Sentences flagged ``repetitive`` (whisper hallucination loops) are collapsed
here, at render time, so stored transcripts keep the raw ASR output.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Sequence

from ..transcribe.protocol import Sentence, SilenceRegion

MAX_REPEATS = 3
MAX_REPEAT_UNIT = 8  # longest n-gram considered a repeating unit
MAX_SENTENCE_LINE_SECONDS = 90.0
MAX_SENTENCE_LINE_CHARS = 1600
_WORD_CHARS = re.compile(r"[^\w']+")


@dataclass(frozen=True)
class RenderedTranscript:
    text: str
    format: str  # index | seconds | sentences
    include_silence: bool
    # line index -> (start_seconds, end_seconds) for resolving cited lines.
    line_times: dict[int, tuple[float, float]]


def render_transcript(
    sentences: Sequence[Sentence],
    silences: Sequence[SilenceRegion],
    *,
    fmt: str = "index",
    include_silence: bool = True,
    episode_duration: float | None = None,
) -> RenderedTranscript:
    if fmt not in ("index", "seconds", "sentences"):
        raise ValueError(f"unknown transcript format {fmt!r}")
    if fmt == "sentences" and include_silence:
        raise ValueError("the sentences format cannot include silence rows")
    if fmt == "sentences":
        sentences = coalesce_sentences(sentences)
    lines: list[str] = []
    line_times: dict[int, tuple[float, float]] = {}
    for index, item in enumerate(_timeline(sentences, silences if include_silence else ())):
        if isinstance(item, SilenceRegion):
            body = _silence_text(item, episode_duration)
        else:
            body = sentence_text(item)
            if fmt == "sentences":
                body = " ".join(body.split())
        if fmt == "index":
            prefix = f"{index}|{int(item.start)}|"
        elif fmt == "seconds":
            prefix = f"[{item.start:.2f} - {item.end:.2f}]"
        else:
            prefix = f"[{item.start:.2f}-{item.end:.2f}]"
        lines.append(f"{prefix} {body}")
        line_times[index] = (item.start, item.end)
    return RenderedTranscript("\n".join(lines), fmt, include_silence, line_times)


def coalesce_sentences(sentences: Sequence[Sentence]) -> list[Sentence]:
    """Join gap/cap fragments through the next punctuation boundary for v3.

    The joiner cuts on pauses and length even when ASR punctuation arrives in
    a later row. A production prompt line should span that whole sentence.
    Extremely long unpunctuated speech is bounded so one line cannot swallow
    a whole episode or defeat the classifier's chunking guard. Such a fallback
    stays verbatim and is marked ``render_cap`` for idempotent rendering.
    """
    complete: list[Sentence] = []
    pending: list[Sentence] = []
    for sentence in sentences:
        if pending and (
            sentence.end - pending[0].start > MAX_SENTENCE_LINE_SECONDS
            or sum(len(part.text) for part in pending) + len(sentence.text) > MAX_SENTENCE_LINE_CHARS
        ):
            complete.append(_merge_fragments(pending, break_reason="render_cap"))
            pending = []
        pending.append(sentence)
        if sentence.break_reason in {"punct", "eof", "render_cap"}:
            complete.append(_merge_fragments(pending))
            pending = []
    if pending:
        complete.append(_merge_fragments(pending))
    return complete


def _merge_fragments(parts: Sequence[Sentence], *, break_reason: str | None = None) -> Sentence:
    first, last = parts[0], parts[-1]
    if len(parts) == 1 and break_reason is None:
        return first
    count = sum(max(1, part.word_count) for part in parts)
    return replace(
        first,
        end=last.end,
        text=" ".join(part.text.strip() for part in parts if part.text.strip()),
        word_count=last.word_start + last.word_count - first.word_start,
        break_reason=break_reason or last.break_reason,
        soft_end=break_reason == "render_cap" or last.soft_end,
        min_p=min(part.min_p for part in parts),
        mean_p=round(sum(part.mean_p * max(1, part.word_count) for part in parts) / count, 4),
        asr_segments=tuple(dict.fromkeys(seg for part in parts for seg in part.asr_segments)),
        flags=tuple(dict.fromkeys(flag for part in parts for flag in part.flags)),
    )


def _timeline(
    sentences: Sequence[Sentence], silences: Sequence[SilenceRegion]
) -> list[Sentence | SilenceRegion]:
    """Sentences in their given order, each silence placed before the first
    sentence that starts at or after it (a region ends where speech begins)."""
    pending = sorted((region for region in silences if region.end > region.start), key=lambda region: region.start)
    timeline: list[Sentence | SilenceRegion] = []
    next_region = 0
    for sentence in sentences:
        while next_region < len(pending) and pending[next_region].start <= sentence.start:
            timeline.append(pending[next_region])
            next_region += 1
        timeline.append(sentence)
    timeline.extend(pending[next_region:])
    return timeline


def _silence_text(region: SilenceRegion, episode_duration: float | None) -> str:
    seconds = max(1, round(region.end - region.start))
    if region.kind != "tail":
        return f"--- {seconds}s of no speech ---"
    end = region.end
    if episode_duration is not None and math.isfinite(episode_duration) and episode_duration >= region.start:
        end = episode_duration
    return f"--- {seconds}s of no speech (end of audio at {end:.2f}) ---"


def sentence_text(sentence: Sentence) -> str:
    text = sentence.text
    if "\n" in text or "\r" in text:
        # One sentence must stay one line, or the line structure breaks.
        text = " ".join(text.split())
    if "repetitive" in sentence.flags:
        text = collapse_repeats(text)
    return text


def collapse_repeats(text: str, max_repeats: int = MAX_REPEATS) -> str:
    """Shorten every run of a repeated word n-gram to ``max_repeats`` copies
    followed by ``[repeated ×N]``. Matching ignores case and punctuation, so
    ``Thank you. Thank you, thank you.`` is one run."""
    tokens = text.split()
    keys = [_WORD_CHARS.sub("", token.lower()) for token in tokens]
    out: list[str] = []
    i = 0
    while i < len(tokens):
        run = _longest_run(keys, i, min_count=max_repeats + 1)
        if run is None:
            out.append(tokens[i])
            i += 1
            continue
        unit, count = run
        out.extend(tokens[i : i + unit * max_repeats])
        out.append(f"[repeated ×{count}]")
        i += unit * count
    return " ".join(out)


def _longest_run(keys: Sequence[str], i: int, min_count: int) -> tuple[int, int] | None:
    """(unit length, repetitions) of the run of at least ``min_count`` copies
    starting at ``i`` that covers the most tokens; the shortest unit wins ties,
    so ``a a a a`` is four copies of ``a`` rather than two of ``a a``."""
    best: tuple[int, int] | None = None
    for unit in range(1, MAX_REPEAT_UNIT + 1):
        pattern = keys[i : i + unit]
        if len(pattern) < unit:
            break
        count = 1
        while keys[i + count * unit : i + (count + 1) * unit] == pattern:
            count += 1
        if count >= min_count and (best is None or count * unit > best[0] * best[1]):
            best = (unit, count)
    return best
