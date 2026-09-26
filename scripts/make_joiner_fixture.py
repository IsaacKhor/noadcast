#!/usr/bin/env python3
"""Regenerate the joiner's golden input, tests/fixtures/words_01-646_first600s.json.gz.

Takes every word starting in the first 600 s of episode 01-646, plus the ASR
segments they came from, from a word-timestamp benchmark transcript, and
writes them in ``join_sentences.py``'s compact word-file layout: unrounded
rows, the source transcript's sha256 and ASR pins, and ``cut_seconds``.
gzip runs with a zero mtime, so regenerating from the same source is
byte-identical.
"""

from __future__ import annotations

import argparse
import gzip
from pathlib import Path

# A sibling script: run this file directly so scripts/ is on sys.path.
from join_sentences import ROOT, compact, load_transcript, run_metadata, source_block, words_document

DEFAULT_SOURCE = ROOT / "benchmarks/tal/runs/wt-crossover-2-on-20260922/transcripts/01-646.json"
DEFAULT_OUT = ROOT / "tests/fixtures/words_01-646_first600s.json.gz"


def build_fixture(source: Path, cut_seconds: float) -> dict:
    transcript, words, segments = load_transcript(source)
    if not transcript["config"].get("word_timestamps"):
        raise SystemExit(f"{source} was transcribed without word timestamps")
    words = [word for word in words if word.start < cut_seconds]
    used = {word.segment for word in words}
    segments = [segment for segment in segments if segment.index in used]
    durations, model_sha256 = run_metadata(source.parent.parent)
    episode = transcript["episode"]
    duration = durations.get(episode["index"], episode["duration_seconds"])
    return words_document(source_block(source, transcript, duration, model_sha256), words, segments,
                          cut_seconds=cut_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate the joiner golden fixture.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="word-timestamp transcript JSON")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--cut-seconds", type=float, default=600.0, help="keep words starting before this time")
    args = parser.parse_args()
    fixture = build_fixture(args.source, args.cut_seconds)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_name(args.out.name + ".part")
    temporary.write_bytes(gzip.compress(compact(fixture), compresslevel=9, mtime=0))
    temporary.replace(args.out)
    print(f"{args.out}: {len(fixture['words'])} words, {len(fixture['segments'])} segments, "
          f"{args.out.stat().st_size} bytes; source sha256 {fixture['source']['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
