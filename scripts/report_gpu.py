#!/usr/bin/env python3
"""Summarise GPU sweep runs and compare them with the matching CPU baselines.

Throughput is corpus audio / suite wall clock, as in every other TAL report.
Transcript agreement is word-level (difflib ratio over lowercased, punctuation-
stripped tokens) against the CPU transcript of the same model and settings; it
measures drift from CPU int8, not accuracy against a reference.
"""
import difflib
import json
from pathlib import Path
import re
import statistics
import sys

RUNS = Path(__file__).resolve().parents[1] / "benchmarks/tal/runs"

# GPU full-run prefix -> CPU baseline run with the same model and word-timestamp setting.
PAIRS = [
    ("gpu-tiny-en-full-", "tiny-en-full-10x2"),
    ("gpu-tiny-en-words-", "wt-crossover-2-on-20260922"),
    ("gpu-small-en-full-", None),  # the CPU small.en no-words baseline predates runs/ (benchmarks/tal/results.json)
    ("gpu-small-en-words-", "small-en-words-20260922"),
    ("gpu-parakeet-tdt-full-", "parakeet-tdt-110m-full-10x2"),
    ("gpu-parakeet-ctc-full-", "parakeet-ctc-110m-full-10x2"),
]
MODELS = {"tiny-en": "tiny.en", "small-en": "small.en",
          "parakeet-tdt": "parakeet-tdt_ctc-110m (TDT)", "parakeet-ctc": "parakeet-tdt_ctc-110m (CTC)"}


def load(run):
    return json.loads((RUNS / run / "results.json").read_text())


def words(path):
    return re.sub(r"[^a-z0-9' ]+", " ", path.read_text().lower()).split()


def agreement(gpu_dir, cpu_txts):
    ratios = []
    for txt in sorted((gpu_dir / "transcripts").glob("*.txt")):
        cpu = cpu_txts / txt.name
        if cpu.is_file():
            ratios.append(difflib.SequenceMatcher(None, words(txt), words(cpu), autojunk=False).ratio())
    return statistics.mean(ratios) if ratios else None


def describe(results):
    c = results["config"]
    return f'{c.get("compute_type") or c["quantization"]} · {c["workers"]}w · b{c["batch_size"]}'


def main():
    out = []
    pilots = sorted(p.name for p in RUNS.glob("gpu-pilot-tiny-en-*") if (p / "results.json").is_file())
    if pilots:
        out += ["## Pilot (tiny.en, episodes 1–4, full length)", "",
                "| Compute type | Workers | Threads/worker | Batch | Suite wall | Throughput |", "|---|---:|---:|---:|---:|---:|"]
        rows = []
        for run in pilots:
            r = load(run)
            if r["status"] != "complete":
                continue
            c, s = r["config"], r["summary"]
            rows.append((s["corpus_wall_speed_x"], f'| {c["compute_type"]} | {c["workers"]} | {c["cpu_threads"]} | {c["batch_size"]} | {s["suite_wall_seconds"]:.1f}s | {s["corpus_wall_speed_x"]:.0f}× |'))
        out += [row for _, row in sorted(rows, reverse=True)] + [""]

    out += ["## Full corpus (10 episodes, 10.22 h)", "",
            "| Model | Word timestamps | GPU config | GPU suite wall | GPU throughput | CPU baseline | CPU throughput | Speedup | Word agreement vs CPU |",
            "|---|---|---|---:|---:|---|---:|---:|---:|"]
    for prefix, cpu_run in PAIRS:
        for gpu_dir in sorted(RUNS.glob(prefix + "*")):
            g = load(gpu_dir.name)
            if g["status"] != "complete":
                continue
            if cpu_run:
                cr = load(cpu_run)
                cpu_speed, cpu_desc = cr["summary"]["corpus_wall_speed_x"], f'{cpu_run} ({cr["config"]["workers"]}×{cr["config"].get("cpu_threads") or cr["config"]["threads_per_worker"]} {cr["config"].get("compute_type") or cr["config"]["quantization"]})'
                cpu_txts = RUNS / cpu_run / "transcripts"
            else:
                base = RUNS.parent
                cr = json.loads((base / "results.json").read_text())
                cpu_speed, cpu_desc = cr["summary"]["end_to_end_speed_x"], "REPORT.md (1×16 int8, MP3)"
                cpu_txts = base / "transcripts"
            gs = g["summary"]
            agree = agreement(gpu_dir, cpu_txts)
            model = next(name for key, name in MODELS.items() if key in prefix)
            timestamps = "n/a" if "parakeet" in prefix else "on" if g["config"]["word_timestamps"] else "off"
            out.append(f'| {model} | {timestamps} | {describe(g)} | {gs["suite_wall_seconds"]:.1f}s | '
                       f'{gs["corpus_wall_speed_x"]:.0f}× | {cpu_desc} | {cpu_speed:.1f}× | **{gs["corpus_wall_speed_x"] / cpu_speed:.1f}×** | '
                       f'{"n/a" if agree is None else f"{agree * 100:.1f}%"} |')
    print("\n".join(out))


if __name__ == "__main__":
    sys.exit(main())
