# Apple Podcasts top-10 corpus

The latest 10 full episodes of each of the 10 most popular shows on Apple Podcasts (US Top Shows chart, updated Sat, 26 Sep 2026 17:33:13 +0000): 100 episodes, 136.42 hours. They're transcribed with the server's pipeline, faster-whisper `tiny.en` with word timestamps on the GPU, and the words are joined into sentences by `noadcast.transcribe.joiner`, as for `benchmarks/tal`.

**The 136.4 hours of audio transcribed in 8.4 minutes: 974× real time**, including MP3 decoding (403 s summed across workers). Output: 1,470,766 words joined into 127,767 sentences. Every episode decoded to its full ffprobe duration (within 2 s).

## Files

- `chart/apple-us-top-podcasts.json`: the Apple Marketing Tools chart response (`rss.marketingtools.apple.com/api/v2/us/podcasts/top/10/podcasts.json`). `chart/itunes-lookup.json`: the iTunes lookup that maps each show to its RSS feed.
- `feeds.json`: the selection rules and the 10 feeds, with Apple rank and ID. `feeds/*.xml.gz`: pinned feed snapshots.
- `manifest.json`: every episode with its URL, retrieval time, duration, and SHA-256. Episodes are dynamic-ad-insertion renders, so another fetch of the same URL can carry different ads. The manifest pins these exact bytes.
- `audio/`: the MP3s (8.2 GB, not tracked).
- `runs/top10-tiny-en-words-gpu-20260926/`, not tracked:
  - `transcripts/`: per-episode words and ASR segments.
  - `sentences/*.sentences.json`: the joined sentences, no-speech regions, and joiner stats.
  - `sentences/*.words.json.gz`: compact word files that `noadcast.transcribe.fake` can replay.
  - `text/*.txt`: one line per sentence, `[22.33-24.88] This is a sentence.` It's the server's `sentences` transcript format: seconds with two decimals, no silence rows, and pause-split fragments merged through the next punctuation.

## Reproduce

```
.venv/bin/python scripts/download_ads_corpus.py --corpus benchmarks/top10        # reuses the pinned snapshots and audio
.venv/bin/python scripts/benchmark_whisper_parallel.py --corpus benchmarks/top10 --input-format mp3 \
    --device cuda --compute-type float16 --workers 4 --threads-per-worker 4 --batch-size 32 \
    --model-path benchmarks/tal/models/tiny.en --model-id Systran/faster-whisper-tiny.en --word-timestamps \
    --memory-interval-ms 500 --run-id <new-run-id>
.venv/bin/python scripts/join_sentences.py benchmarks/top10/runs/<new-run-id>
.venv/bin/python scripts/export_sentence_text.py benchmarks/top10/runs/<new-run-id>
.venv/bin/python scripts/report_top10.py --run-id <new-run-id>
```

To rebuild the corpus from a new chart, fetch the chart and lookup again, regenerate `feeds.json`, and pass `--refresh-feeds`. That changes the corpus.

## Configuration

Same settings as the server's defaults: faster-whisper batched pipeline, `tiny.en` (SHA-256 `1a5afae0…`), CUDA float16 on a TITAN V, 4 workers × 4 threads, batch 32, beam 5, English, VAD on, no previous-text conditioning, word timestamps. Audio is decoded by `noadcast.transcribe.audio` (see below). Sentences use joiner defaults (version `1`). Suite peak host PSS: 12.1 GiB.

## Per show

| Rank | Show | Genre | Episodes | Hours | Words | Sentences | Sentences/min | Low-confidence sentences | Corrupt packets skipped | Per-worker speed |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | The Daily | Daily News | 10 | 7.8 | 69,173 | 6,064 | 13.0 | 3.1% | 0 | 345× |
| 2 | Crime Junkie | True Crime | 10 | 10.4 | 104,894 | 7,745 | 12.5 | 1.9% | 0 | 368× |
| 3 | Dateline NBC | True Crime | 10 | 6.8 | 64,915 | 5,705 | 14.0 | 2.5% | 0 | 366× |
| 4 | The Joe Rogan Experience | Comedy | 10 | 26.9 | 277,210 | 24,574 | 15.2 | 3.8% | 0 | 309× |
| 5 | Morbid | True Crime | 10 | 12.0 | 133,332 | 11,971 | 16.6 | 3.5% | 0 | 296× |
| 6 | REAL AF with Andy Frisella | Entrepreneurship | 10 | 11.3 | 117,734 | 11,108 | 16.3 | 2.9% | 0 | 331× |
| 7 | Pardon My Take | Football | 10 | 29.6 | 347,149 | 32,224 | 18.1 | 4.4% | 42 | 270× |
| 8 | Pod Save America | Politics | 10 | 14.9 | 163,823 | 12,399 | 13.9 | 3.6% | 0 | 321× |
| 9 | Live Free with Josh Howerton | Christianity | 10 | 13.2 | 156,547 | 13,093 | 16.6 | 3.1% | 0 | 287× |
| 10 | Up First from NPR | Daily News | 10 | 3.6 | 35,989 | 2,884 | 13.2 | 2.4% | 0 | 304× |

Per-worker speed is one worker's audio / transcribe time. Four workers ran at once, so the corpus throughput (974×) comes from the suite wall clock, not from these figures.

## Truncated decoding (fixed)

The first transcription pass decoded two Pardon My Take episodes (067 and 070, 197 and 161 minutes) to 55 seconds each, with no error. Their dynamic-ad-insertion splices contain corrupt MP3 frames ("Header missing"). In faster-whisper 1.2.1, `decode_audio` tries to skip `InvalidDataError` by calling `next()` again on `container.decode()`, but a generator that has raised is finished, so the first bad packet ends the decode. The server worker has the same problem. `noadcast.transcribe.audio.decode_audio` decodes packet by packet and drops only the bad ones (23 and 19 here), as the ffmpeg CLI does. On clean files its output is sample-identical to faster-whisper's (`tests/test_audio_decode.py`). Both the server worker and `benchmark_whisper_parallel.py` now use it, and each transcript records `decode_skipped_packets`. This corpus was re-transcribed in full with the fix. The first pass's log is kept as `transcribe-20260926-truncated-decode.log`.

## Selection notes

- The selection rule is the same as `benchmarks/ads`: the newest items by pubDate with an audio enclosure, `itunes:episodeType` full (or absent), and at least 10 minutes long. Trailers and short bonus clips are skipped.
- Feed URLs come from Apple's lookup, not hand-picked. The Daily's Apple-listed feed keeps only its last few weekday episodes plus a long archive of Sunday episodes. So its "latest 10" are 4 weekday episodes (23–26 Sep), one Saturday episode (19 Sep), and 5 Sundays going back to 23 Aug.
- Weekly shows (Crime Junkie, REAL AF, Live Free) reach back to July or August. The Joe Rogan Experience and Pardon My Take average about 3 hours an episode, so together they're about 41% of the audio.
- No accuracy evaluation is claimed. Low-confidence sentences are the joiner's own flag.
