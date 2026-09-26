#!/usr/bin/env python3
"""Join the word timestamps of a benchmark run into sentences.

For each ``transcripts/<stem>.json`` in a run directory written by
``benchmark_whisper_parallel.py --word-timestamps``, writes to ``--out``
(default ``<run_dir>/sentences/``):

- ``<stem>.sentences.json``: the source pins (transcript and audio sha256,
  ASR config and model hash), joiner version and params, the sentences, the
  no-speech regions, and stats.
- ``<stem>.words.json.gz``: the compact word file — the same pins plus every
  word and ASR segment as unrounded rows under ``word_fields`` /
  ``segment_fields``. ``tests/fixtures/words_*.json.gz`` use this layout (see
  ``make_joiner_fixture.py``), and ``noadcast.transcribe.fake`` replays it.

Both files are compact JSON; ``load_sentences`` and ``load_words`` read them
back. Corpus statistics are printed at the end. Example:

    .venv/bin/python scripts/join_sentences.py benchmarks/tal/runs/wt-crossover-2-on-20260922 --param gap_hard=0.8
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import gzip
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Sequence

from noadcast.transcribe.joiner import JOINER_VERSION, JoinerParams, derive_silences, join_words
from noadcast.transcribe.protocol import AsrSegmentMeta, Sentence, SilenceRegion, Word

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
WORD_FIELDS = tuple(field.name for field in dataclasses.fields(Word))
SEGMENT_FIELDS = tuple(field.name for field in dataclasses.fields(AsrSegmentMeta))
FLAGS = ("low_confidence", "repetitive", "high_compression", "long_span")
BREAKS = ("punct", "gap", "cap", "eof")


def parse_params(pairs: Sequence[str]) -> JoinerParams:
    types = {field.name: type(field.default) for field in dataclasses.fields(JoinerParams)}
    overrides: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or key not in types:
            raise ValueError(f"--param expects one of {sorted(types)} as KEY=VALUE, got {pair!r}")
        overrides[key] = types[key](value)
    return JoinerParams(**overrides)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_transcript(path: Path) -> tuple[dict[str, Any], list[Word], list[AsrSegmentMeta]]:
    """The benchmark transcript plus its flat word stream and segment metadata.
    Segment indices are positions in ``segments`` (0-based), not whisper's ``id``."""
    transcript = json.loads(path.read_text())
    words: list[Word] = []
    segments: list[AsrSegmentMeta] = []
    for index, segment in enumerate(transcript["segments"]):
        segments.append(AsrSegmentMeta(
            index=index, start=segment["start"], end=segment["end"],
            compression_ratio=segment["compression_ratio"], no_speech_prob=segment["no_speech_prob"],
            avg_logprob=segment["avg_logprob"],
        ))
        for word in segment["words"] or []:
            words.append(Word(word["start"], word["end"], word["word"], word["probability"], index))
    return transcript, words, segments


def run_metadata(run_dir: Path) -> tuple[dict[int, float], str | None]:
    """Decoded audio lengths by episode index — authoritative over the manifest's
    probe — and the model binary's sha256, from the run's results.json."""
    results = run_dir / "results.json"
    if not results.is_file():
        return {}, None
    data = json.loads(results.read_text())
    durations = {episode["index"]: episode["full_decoded_audio_seconds"] for episode in data.get("episodes", [])}
    return durations, data.get("model", {}).get("model_bin_sha256")


def source_block(path: Path, transcript: dict[str, Any], duration: float | None,
                 model_sha256: str | None) -> dict[str, Any]:
    """The provenance every output file carries."""
    try:
        shown = str(path.resolve().relative_to(ROOT))
    except ValueError:
        shown = str(path)
    episode = transcript["episode"]
    return {
        "path": shown,
        "sha256": sha256_file(path),
        "episode": {"index": episode["index"], "title": episode["title"], "audio_sha256": episode["sha256"],
                    "duration_seconds": duration},
        # model_path is machine-specific; everything else pins the transcript.
        "asr": {**{key: value for key, value in transcript["config"].items() if key != "model_path"},
                "model_sha256": model_sha256},
    }


def words_document(source: dict[str, Any], words: Sequence[Word], segments: Sequence[AsrSegmentMeta],
                   **extra: Any) -> dict[str, Any]:
    """The compact word file: unrounded rows, so it stays a faithful copy of the ASR output."""
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        **extra,
        "segment_fields": list(SEGMENT_FIELDS),
        "segments": [[getattr(segment, name) for name in SEGMENT_FIELDS] for segment in segments],
        "word_fields": list(WORD_FIELDS),
        "words": [[getattr(word, name) for name in WORD_FIELDS] for word in words],
    }


def load_words(path: Path) -> tuple[list[Word], list[AsrSegmentMeta]]:
    """Read a compact word file (``<stem>.words.json.gz`` or a joiner fixture)."""
    document = json.loads(gzip.decompress(path.read_bytes()))
    words = [Word(**dict(zip(document["word_fields"], row, strict=True))) for row in document["words"]]
    segments = [AsrSegmentMeta(**dict(zip(document["segment_fields"], row, strict=True)))
                for row in document["segments"]]
    return words, segments


def sentence_record(sentence: Sentence) -> dict[str, Any]:
    return {
        "i": sentence.index, "start": sentence.start, "end": sentence.end, "text": sentence.text,
        "break": sentence.break_reason, "word_start": sentence.word_start, "word_count": sentence.word_count,
        "asr_segments": list(sentence.asr_segments), "min_p": sentence.min_p, "mean_p": sentence.mean_p,
        "flags": list(sentence.flags),
    }


def load_sentences(path: Path) -> tuple[list[Sentence], list[SilenceRegion]]:
    """Read ``<stem>.sentences.json`` back into protocol objects."""
    document = json.loads(path.read_text())
    sentences = [
        Sentence(index=r["i"], start=r["start"], end=r["end"], text=r["text"], word_start=r["word_start"],
                 word_count=r["word_count"], break_reason=r["break"], soft_end=r["break"] == "cap",
                 min_p=r["min_p"], mean_p=r["mean_p"], asr_segments=tuple(r["asr_segments"]), flags=tuple(r["flags"]))
        for r in document["sentences"]
    ]
    silences = [SilenceRegion(r["start"], r["end"], r["kind"]) for r in document["no_speech"]]
    return sentences, silences


def percentiles(values: Sequence[float]) -> dict[str, float]:
    ordered = sorted(values)

    def at(q: float) -> float:
        position = (len(ordered) - 1) * q
        low = int(position)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return {"p50": round(at(0.5), 2), "p90": round(at(0.9), 2), "max": round(ordered[-1], 2)} if ordered else {}


def sentence_stats(sentences: Sequence[Sentence], silences: Sequence[SilenceRegion],
                   word_count: int, duration: float | None) -> dict[str, Any]:
    minutes = (duration or (sentences[-1].end if sentences else 0.0)) / 60
    return {
        "sentences": len(sentences),
        "words": word_count,
        "audio_minutes": round(minutes, 2),
        "sentences_per_minute": round(len(sentences) / minutes, 2) if minutes else None,
        "breaks": {reason: sum(s.break_reason == reason for s in sentences) for reason in BREAKS},
        "flags": {flag: sum(flag in s.flags for s in sentences) for flag in FLAGS},
        "duration_seconds": percentiles([s.end - s.start for s in sentences]),
        "chars": percentiles([len(s.text) for s in sentences]),
        "multi_segment": sum(len(s.asr_segments) > 1 for s in sentences),
        "no_speech": {"regions": len(silences), "seconds": round(sum(r.end - r.start for r in silences), 2),
                      "by_kind": dict(collections.Counter(r.kind for r in silences))},
    }


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)


def compact(value: Any) -> bytes:
    return (json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


@dataclasses.dataclass(frozen=True)
class EpisodeJoin:
    stem: str
    sentences: list[Sentence]
    stats: dict[str, Any]


def join_run(run_dir: Path, out_dir: Path, params: JoinerParams) -> list[EpisodeJoin]:
    transcripts = sorted((run_dir / "transcripts").glob("*.json"))
    if not transcripts:
        raise SystemExit(f"no transcripts/*.json under {run_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    durations, model_sha256 = run_metadata(run_dir)
    joins = []
    for path in transcripts:
        transcript, words, segments = load_transcript(path)
        if not transcript["config"].get("word_timestamps") or not words:
            raise SystemExit(f"{path} has no word timestamps; rerun the benchmark with --word-timestamps")
        episode = transcript["episode"]
        duration = durations.get(episode["index"], episode.get("duration_seconds"))
        started = time.perf_counter()
        sentences = join_words(words, segments, params)
        silences = derive_silences(words, duration, params)
        join_ms = (time.perf_counter() - started) * 1000
        stats = {**sentence_stats(sentences, silences, len(words), duration), "join_ms": round(join_ms, 1)}
        source = source_block(path, transcript, duration, model_sha256)
        document = {
            "schema_version": SCHEMA_VERSION,
            "source": source,
            "joiner": {"version": JOINER_VERSION, "params": dataclasses.asdict(params)},
            "sentences": [sentence_record(sentence) for sentence in sentences],
            "no_speech": [{"start": r.start, "end": r.end, "kind": r.kind} for r in silences],
            "stats": stats,
        }
        atomic_write(out_dir / f"{path.stem}.sentences.json", compact(document))
        atomic_write(out_dir / f"{path.stem}.words.json.gz",
                     gzip.compress(compact(words_document(source, words, segments)), mtime=0))
        joins.append(EpisodeJoin(path.stem, sentences, stats))
    return joins


def print_summary(joins: Sequence[EpisodeJoin]) -> None:
    print(f"{'episode':8} {'min':>6} {'sent':>5} {'/min':>5} {'punct':>6} {'gap':>5} {'cap':>4} "
          f"{'lowc':>5} {'rep':>4} {'hcr':>4} {'long':>4} {'dur50':>6} {'dur90':>6} {'durmax':>6} "
          f"{'ch50':>5} {'ch90':>5} {'chmax':>5} {'multi':>5} {'ms':>6}")
    for join in joins:
        s = join.stats
        b, f, d, c = s["breaks"], s["flags"], s["duration_seconds"], s["chars"]
        print(f"{join.stem:8} {s['audio_minutes']:6.1f} {s['sentences']:5d} {s['sentences_per_minute']:5.1f} "
              f"{b['punct']:6d} {b['gap']:5d} {b['cap']:4d} {f['low_confidence']:5d} {f['repetitive']:4d} "
              f"{f['high_compression']:4d} {f['long_span']:4d} {d['p50']:6.2f} {d['p90']:6.2f} {d['max']:6.2f} "
              f"{c['p50']:5.0f} {c['p90']:5.0f} {c['max']:5.0f} {s['multi_segment']:5d} {s['join_ms']:6.1f}")
    sentences = [sentence for join in joins for sentence in join.sentences]
    minutes = sum(join.stats["audio_minutes"] for join in joins)
    breaks = collections.Counter(s.break_reason for s in sentences)
    flags = collections.Counter(flag for s in sentences for flag in s.flags)
    print(f"\ncorpus: {len(sentences)} sentences over {minutes:.1f} min = {len(sentences) / minutes:.2f}/min; "
          f"{sum(join.stats['words'] for join in joins)} words")
    print("breaks: " + ", ".join(f"{r} {breaks[r]} ({breaks[r] / len(sentences):.1%})" for r in BREAKS))
    print("flags: " + ", ".join(f"{flag} {flags[flag]} ({flags[flag] / len(sentences):.1%})" for flag in FLAGS))
    print(f"duration s: {percentiles([s.end - s.start for s in sentences])}; "
          f"chars: {percentiles([len(s.text) for s in sentences])}; "
          f"multi-segment: {sum(len(s.asr_segments) > 1 for s in sentences)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Join benchmark word timestamps into sentences.")
    parser.add_argument("run_dir", type=Path, help="benchmark run directory containing transcripts/*.json")
    parser.add_argument("--out", type=Path, help="output directory (default: RUN_DIR/sentences)")
    parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                        help="override a JoinerParams field; repeatable")
    args = parser.parse_args()
    try:
        params = parse_params(args.param)
    except ValueError as error:
        parser.error(str(error))
    print_summary(join_run(args.run_dir, args.out or args.run_dir / "sentences", params))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
