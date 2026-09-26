#!/usr/bin/env python3
"""Write each joined episode as plain text, one sentence per line.

For every ``sentences/<stem>.sentences.json`` in a run directory (written by
``join_sentences.py``), writes ``<out>/<stem>.txt`` (default ``RUN_DIR/text/``):

    [22.33-24.88] This is a sentence.
    [24.88-27.10] And this is the next one.

Lines come from the server's ``sentences`` transcript format
(``noadcast.classify.render``), so they match what the classifier is sent:
times in seconds with two decimals, no silence rows, and pause-split fragments
coalesced through the next punctuation. Every line is validated before the
file is written. Example:

    .venv/bin/python scripts/export_sentence_text.py benchmarks/top10/runs/top10-tiny-en-words-gpu-20260926
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from join_sentences import load_sentences  # noqa: E402

from noadcast.classify.render import render_transcript  # noqa: E402

LINE = re.compile(r"\[(\d+\.\d{2})-(\d+\.\d{2})\] (\S.*)")


def validate(text: str, source: Path) -> int:
    previous_start = -1.0
    lines = text.split("\n")
    for number, line in enumerate(lines, 1):
        match = LINE.fullmatch(line)
        if not match:
            raise ValueError(f"{source}: line {number} is not '[start-end] text': {line!r}")
        start, end = float(match[1]), float(match[2])
        if end < start or start < previous_start:
            raise ValueError(f"{source}: line {number} times are out of order: {line!r}")
        previous_start = start
    return len(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run_dir", type=Path, help="run directory containing sentences/*.sentences.json")
    parser.add_argument("--out", type=Path, help="output directory (default: RUN_DIR/text)")
    args = parser.parse_args()
    sources = sorted((args.run_dir / "sentences").glob("*.sentences.json"))
    if not sources:
        parser.error(f"no sentences/*.sentences.json in {args.run_dir}; run join_sentences.py first")
    out = args.out or args.run_dir / "text"
    out.mkdir(parents=True, exist_ok=True)
    total = 0
    for source in sources:
        sentences, _ = load_sentences(source)
        text = render_transcript(sentences, (), fmt="sentences", include_silence=False).text
        total += validate(text, source)
        target = out / source.name.replace(".sentences.json", ".txt")
        temporary = target.with_name(target.name + ".part")
        temporary.write_text(text + "\n")
        temporary.replace(target)
    print(f"Wrote {len(sources)} files, {total:,} lines, to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
