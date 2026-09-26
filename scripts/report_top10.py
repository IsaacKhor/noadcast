#!/usr/bin/env python3
"""Write benchmarks/top10/README.md from the corpus files and one transcription run."""
import argparse
import collections
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "benchmarks/top10"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="top10-tiny-en-words-gpu-20260926")
    run_id = parser.parse_args().run_id
    run = CORPUS / "runs" / run_id
    r = json.loads((run / "results.json").read_text())
    feeds = json.loads((CORPUS / "feeds.json").read_text())
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    title = {e["index"]: e["podcast_title"] for e in manifest["episodes"]}
    by = collections.defaultdict(lambda: collections.Counter())
    joiner = None
    for e in r["episodes"]:
        g = by[title[e["index"]]]
        g["n"] += 1
        g["audio"] += e["benchmark_audio_seconds"]
        g["tx"] += e["transcribe_seconds"]
        g["words"] += e["word_count"]
        g["skipped"] += e.get("decode_skipped_packets", 0)
        doc = json.loads((run / "sentences" / f"{Path(e['path']).stem}.sentences.json").read_text())
        joiner = doc["joiner"].get("version")
        g["sents"] += len(doc["sentences"])
        g["lowc"] += sum("low_confidence" in s["flags"] for s in doc["sentences"])
    rows = []
    for f in feeds["feeds"]:
        g = by[f["title"]]
        rows.append(f"| {f['apple_rank']} | {f['title']} | {f['genre']} | {g['n']} | {g['audio'] / 3600:.1f} | "
                    f"{g['words']:,} | {g['sents']:,} | {g['sents'] / (g['audio'] / 60):.1f} | "
                    f"{100 * g['lowc'] / g['sents']:.1f}% | {g['skipped']} | {g['audio'] / g['tx']:.0f}× |")
    s = r["summary"]
    words = sum(g["words"] for g in by.values())
    sents = sum(g["sents"] for g in by.values())
    pss = ((r.get("memory") or {}).get("suite") or {}).get("peak_pss_mib") or 0
    text = f"""# Apple Podcasts top-10 corpus

The latest 10 full episodes of each of the 10 most popular shows on Apple Podcasts (US Top Shows chart, updated {feeds['source']['chart_updated']}): 100 episodes, {s['benchmark_audio_seconds'] / 3600:.2f} hours. They're transcribed with the server's pipeline, faster-whisper `tiny.en` with word timestamps on the GPU, and the words are joined into sentences by `noadcast.transcribe.joiner`, as for `benchmarks/tal`.

**The {s['benchmark_audio_seconds'] / 3600:.1f} hours of audio transcribed in {s['suite_wall_seconds'] / 60:.1f} minutes: {s['corpus_wall_speed_x']:.0f}× real time**, including MP3 decoding ({s['sum_worker_decode_seconds']:.0f} s summed across workers). Output: {words:,} words joined into {sents:,} sentences. Every episode decoded to its full ffprobe duration (within 2 s).

## Files

- `chart/apple-us-top-podcasts.json`: the Apple Marketing Tools chart response (`rss.marketingtools.apple.com/api/v2/us/podcasts/top/10/podcasts.json`). `chart/itunes-lookup.json`: the iTunes lookup that maps each show to its RSS feed.
- `feeds.json`: the selection rules and the 10 feeds, with Apple rank and ID. `feeds/*.xml.gz`: pinned feed snapshots.
- `manifest.json`: every episode with its URL, retrieval time, duration, and SHA-256. Episodes are dynamic-ad-insertion renders, so another fetch of the same URL can carry different ads. The manifest pins these exact bytes.
- `audio/`: the MP3s (8.2 GB, not tracked).
- `runs/{run_id}/`, not tracked:
  - `transcripts/`: per-episode words and ASR segments.
  - `sentences/*.sentences.json`: the joined sentences, no-speech regions, and joiner stats.
  - `sentences/*.words.json.gz`: compact word files that `noadcast.transcribe.fake` can replay.
  - `text/*.txt`: one line per sentence, `[22.33-24.88] This is a sentence.` It's the server's `sentences` transcript format: seconds with two decimals, no silence rows, and pause-split fragments merged through the next punctuation.

## Reproduce

```
.venv/bin/python scripts/download_ads_corpus.py --corpus benchmarks/top10        # reuses the pinned snapshots and audio
.venv/bin/python scripts/benchmark_whisper_parallel.py --corpus benchmarks/top10 --input-format mp3 \\
    --device cuda --compute-type float16 --workers 4 --threads-per-worker 4 --batch-size 32 \\
    --model-path benchmarks/tal/models/tiny.en --model-id Systran/faster-whisper-tiny.en --word-timestamps \\
    --memory-interval-ms 500 --run-id <new-run-id>
.venv/bin/python scripts/join_sentences.py benchmarks/top10/runs/<new-run-id>
.venv/bin/python scripts/export_sentence_text.py benchmarks/top10/runs/<new-run-id>
.venv/bin/python scripts/report_top10.py --run-id <new-run-id>
```

To rebuild the corpus from a new chart, fetch the chart and lookup again, regenerate `feeds.json`, and pass `--refresh-feeds`. That changes the corpus.

## Configuration

Same settings as the server's defaults: faster-whisper batched pipeline, `tiny.en` (SHA-256 `1a5afae0…`), CUDA float16 on a TITAN V, 4 workers × 4 threads, batch 32, beam 5, English, VAD on, no previous-text conditioning, word timestamps. Audio is decoded by `noadcast.transcribe.audio` (see below). Sentences use joiner defaults (version `{joiner}`). Suite peak host PSS: {pss / 1024:.1f} GiB.

## Per show

| Rank | Show | Genre | Episodes | Hours | Words | Sentences | Sentences/min | Low-confidence sentences | Corrupt packets skipped | Per-worker speed |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
""" + "\n".join(rows) + f"""

Per-worker speed is one worker's audio / transcribe time. Four workers ran at once, so the corpus throughput ({s['corpus_wall_speed_x']:.0f}×) comes from the suite wall clock, not from these figures.

## Truncated decoding (fixed)

The first transcription pass decoded two Pardon My Take episodes (067 and 070, 197 and 161 minutes) to 55 seconds each, with no error. Their dynamic-ad-insertion splices contain corrupt MP3 frames ("Header missing"). In faster-whisper 1.2.1, `decode_audio` tries to skip `InvalidDataError` by calling `next()` again on `container.decode()`, but a generator that has raised is finished, so the first bad packet ends the decode. The server worker has the same problem. `noadcast.transcribe.audio.decode_audio` decodes packet by packet and drops only the bad ones (23 and 19 here), as the ffmpeg CLI does. On clean files its output is sample-identical to faster-whisper's (`tests/test_audio_decode.py`). Both the server worker and `benchmark_whisper_parallel.py` now use it, and each transcript records `decode_skipped_packets`. This corpus was re-transcribed in full with the fix. The first pass's log is kept as `transcribe-20260926-truncated-decode.log`.

## Selection notes

- The selection rule is the same as `benchmarks/ads`: the newest items by pubDate with an audio enclosure, `itunes:episodeType` full (or absent), and at least 10 minutes long. Trailers and short bonus clips are skipped.
- Feed URLs come from Apple's lookup, not hand-picked. The Daily's Apple-listed feed keeps only its last few weekday episodes plus a long archive of Sunday episodes. So its "latest 10" are 4 weekday episodes (23–26 Sep), one Saturday episode (19 Sep), and 5 Sundays going back to 23 Aug.
- Weekly shows (Crime Junkie, REAL AF, Live Free) reach back to July or August. The Joe Rogan Experience and Pardon My Take average about 3 hours an episode, so together they're about 41% of the audio.
- No accuracy evaluation is claimed. Low-confidence sentences are the joiner's own flag.
"""
    (CORPUS / "README.md").write_text(text)


if __name__ == "__main__":
    main()
