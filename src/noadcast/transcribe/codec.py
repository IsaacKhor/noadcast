"""Compact word storage: the ``transcript_words`` blob.

One zlib-compressed JSON object of parallel arrays per episode rather than a
row per word — nothing queries individual words, and ~10,000 rows per episode
would dwarf everything else in the database. Times are stored as integer
centiseconds (faster-whisper already rounds to 0.01 s, so its output
round-trips exactly) and probabilities as whole percent. The joiner compares
at exactly this resolution, so re-joining decoded words reproduces the
sentences joined from the original ones. A 10,000-word episode is ~70 KB.
"""

from __future__ import annotations

import dataclasses
import json
import zlib
from typing import Any, Sequence

from .protocol import AsrSegmentMeta, Word

CODEC = "zlib+json-columnar-v1"

_WORDS_VERSION = 1
_SEGMENTS_VERSION = 1
_ZLIB_LEVEL = 6


def centiseconds(seconds: float) -> int:
    return round(seconds * 100)


def percent(probability: float) -> int:
    return min(100, max(0, round(probability * 100)))


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def encode_words(words: Sequence[Word]) -> bytes:
    starts = [centiseconds(w.start) for w in words]
    payload = {
        "v": _WORDS_VERSION,
        "s": starts,
        # End minus start of the *rounded* values, so each bound is off by at
        # most 5 ms rather than the end accumulating both rounding errors.
        "d": [centiseconds(w.end) - start for w, start in zip(words, starts)],
        "w": [w.word for w in words],
        "p": [percent(w.probability) for w in words],
        "g": [w.segment for w in words],
    }
    return zlib.compress(_dumps(payload).encode("utf-8"), _ZLIB_LEVEL)


def decode_words(blob: bytes) -> list[Word]:
    payload = json.loads(zlib.decompress(blob))
    if payload.get("v") != _WORDS_VERSION:
        raise ValueError(f"unsupported word blob version {payload.get('v')!r}")
    columns = [payload[key] for key in ("s", "d", "w", "p", "g")]
    if len({len(column) for column in columns}) != 1:
        raise ValueError("word blob columns differ in length")
    return [
        Word(start=s / 100, end=(s + d) / 100, word=w, probability=p / 100, segment=g)
        for s, d, w, p, g in zip(*columns)
    ]


def encode_segments(segments: Sequence[AsrSegmentMeta]) -> str:
    """Compact JSON for ``transcript_words.segments_json``.

    Floats keep full precision: compression_ratio feeds the joiner's
    high_compression flag, and ~10 KB per episode buys an exact re-join.
    """
    names = [field.name for field in dataclasses.fields(AsrSegmentMeta)]
    return _dumps({
        "v": _SEGMENTS_VERSION,
        "fields": names,
        "rows": [[getattr(segment, name) for name in names] for segment in segments],
    })


def decode_segments(text: str) -> list[AsrSegmentMeta]:
    payload = json.loads(text)
    if payload.get("v") != _SEGMENTS_VERSION:
        raise ValueError(f"unsupported segments_json version {payload.get('v')!r}")
    names = payload["fields"]
    return [AsrSegmentMeta(**dict(zip(names, row, strict=True))) for row in payload["rows"]]
