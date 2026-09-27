# noadcast

A self-hosted podcast system that skips ads, intros, and outros. A Python
server polls RSS feeds, downloads each episode once, transcribes it locally
with faster-whisper `tiny.en` using word-level timestamps joined into
sentences, and asks a cloud model through OpenRouter to find the skippable
segments from the transcript text alone — audio never leaves the machine
except to the phone. The iOS app is a thin client: it syncs podcasts,
episodes, and skip markers from the server, and streams or downloads the
audio the server stores.

```
            RSS feeds                         OpenRouter
                │                         (transcript text only)
                ▼                                   ▲
 ┌──────────────────────────── laurel ──────────────┼──────────────┐
 │ refresh ─▶ download ─▶ transcribe ─▶ join ─▶ classify ─▶ markers │
 │ (SQLite job table, asyncio scheduler, spawned whisper workers)   │
 │                  HTTP API + Range audio                          │
 └──────────────────────────────┬───────────────────────────────────┘
                                │ Tailscale
                                ▼
                     iOS app (SwiftData mirror,
                     stream or download, skip)
```

- Server code: `src/noadcast/` (see `docs/API.md` for the wire contract).
- iOS app: `ios_app/` (see `AGENTS.md` for app conventions).
- Design record: the transcription benchmarks below, plus
  `benchmarks/tal/WORD_TIMESTAMPS.md` for the cost of word timestamps.

## Server

The server runs on `laurel`, reachable from the phone over Tailscale at
`http://laurel.turkey-galaxy.ts.net:8765`. One process owns everything: an
asyncio scheduler over a SQLite job table (refresh, download, transcribe,
classify, evict), a pool of spawned faster-whisper workers, and the HTTP API.
Configuration lives in `secrets.env` (see `secrets.env.example` and
`src/noadcast/config.py`); API keys never leave the server.

Set `OPENROUTER_API_KEY`; OpenRouter is the only classification provider.
The default is DeepSeek 4.1 Flash (`deepseek/deepseek-v4.1-flash`). Select
Qwen 3.8 Flash (`qwen/qwen3.8-flash`) or GPT 6 Luna (`openai/gpt-6-luna`)
in iOS Settings or with `NOADCAST_OPENROUTER_MODEL`. GPT 6 Luna always uses
high reasoning effort. Only transcript text is sent for classification.

```bash
export UV_CACHE_DIR="$PWD/.cache/uv" TMPDIR="$PWD/.cache/tmp"
uv pip install --python .venv/bin/python -e .
cp secrets.env.example secrets.env && chmod 600 secrets.env
.venv/bin/noadcast token        # paste into NOADCAST_API_TOKEN; add OPENROUTER_API_KEY
.venv/bin/noadcast models link benchmarks/tal/models/tiny.en   # or: noadcast models fetch
.venv/bin/noadcast migrate
.venv/bin/noadcast serve
```

To run it as a systemd user service, and for day-to-day operation (status,
logs, reprocessing, backups), see [deploy/README.md](deploy/README.md).

How an episode flows:

1. **Refresh** polls each feed every ~30 minutes with conditional GETs. A new
   subscription admits only its newest episode; later, only genuinely new
   episodes are admitted, so an archive is never processed wholesale.
2. **Download** fetches the audio once, resumably, and serves it back to
   the phone with HTTP Range, so streaming and offline copies are the same
   bytes the markers were computed on.
3. **Transcribe** runs faster-whisper `tiny.en` on the GPU (CUDA, float16,
   4 workers × batch 32; ~950× real time on a TITAN V, see
   `benchmarks/tal/GPU.md`) with word timestamps; words are joined into sentences
   (`src/noadcast/transcribe/joiner.py`) and stored along with the words.
4. **Classify** joins pause and length fragments through their next punctuation
   boundary, then sends lines such as `[22.24-23.88] A complete sentence.`
   to OpenRouter. Each returned intro, ad, or outro has start and end
   timestamps and a summary of its content. The server sanitises boundaries
   against the transcript and true audio length, then exposes the segments
   as episode markers for the app.
   Every classification is kept, so providers and prompts can be compared
   on identical transcripts.
5. **Retention**: when the app reports an episode played, the server
   deletes its audio but keeps the transcript and markers. Age and
   free-space sweeps bound disk for episodes that are never played.

## iOS app

Open `ios_app/Noadcast.xcodeproj` in Xcode on a Mac (the app targets
iOS 26), build, and in **Settings → Server** enter the server URL and the
token from `secrets.env`, then **Test connection**. The app mirrors the
server's podcasts, episodes, and markers into SwiftData for offline use,
streams episodes that aren't downloaded, and keeps playback position,
played state, and queue order on the device. `docs/API.md` is the contract
between the two halves; `AGENTS.md` describes the app's conventions.

## Development

```bash
export TMPDIR="$PWD/.cache/tmp" HF_HOME="$PWD/.cache/huggingface" XDG_CACHE_HOME="$PWD/.cache"
.venv/bin/python -m unittest discover -s tests -t .
```

The suite runs in about 35 seconds with the network, transcription pool,
and LLMs faked; `tests/e2e/test_tal_offline.py` drives the real HTTP API
against a local copy of the This American Life feed. Set
`NOADCAST_E2E_REAL_ASR=1` to run one episode through the real
transcription pool as well.

## Evaluation

Experiment A measured the cost of faster-whisper word timestamps with four
sequential full-corpus `tiny.en` runs in OFF, ON, ON, OFF order (10 workers ×
2 threads, PCM, 250 ms memory sampling). Word timestamps took **1.124×** the
pooled suite wall time (1.129× and 1.118× pairwise) and 1.115× the summed
worker CPU, with no detectable aggregate peak-PSS increase and
**byte-identical transcript text** on all ten episodes, so they are accepted;
segment bounds become word-derived. See
[the word-timestamp report](benchmarks/tal/WORD_TIMESTAMPS.md). Rerun with
`scripts/run_word_timestamps_crossover.sh <tag>` and regenerate with
`.venv/bin/python scripts/report_word_timestamps.py` (pass the four new run
directories in execution order; the default is the 20260922 runs).

Experiment B asks whether `tiny.en` transcripts give the classifier the same
skip segments as `small.en`, measured against the LLM's own repeat-to-repeat
noise; no human labels exist, so it reports agreement, not accuracy. The
`small.en` arm is `benchmarks/tal/runs/small-en-words-20260922`, produced by
`.venv/bin/python -u scripts/benchmark_whisper_parallel.py --workers 4
--threads-per-worker 2 --input-format pcm --model-path
benchmarks/tal/models/small.en --model-id Systran/faster-whisper-small.en
--model-revision d1d751a5f8271d482d14ca55d9e2deeebbae577f --word-timestamps
--run-id small-en-words-20260922` on a shared host, so its timings are not a
benchmark. Recording requires `OPENROUTER_API_KEY` in `secrets.env`;
`--arm openrouter:deepseek/deepseek-v4.1-flash` selects the default model.
Record once, then score and report offline:

```bash
.venv/bin/python scripts/eval_ad_segments.py --eval-id tal-asr-v1 --record
.venv/bin/python scripts/eval_ad_segments.py --eval-id tal-asr-v1   # replay only
.venv/bin/python scripts/score_ad_eval.py --eval-id tal-asr-v1
.venv/bin/python scripts/report_ad_eval.py --eval-id tal-asr-v1
```

Everything lands in `benchmarks/evals/tal-asr-v1/` (`config.json` pins,
cassettes, prompts, `AD_EVAL.md`, `ad-eval-verification.json`,
`ad-eval.csv`). TAL contains no third-party ads, so that eval measures
intro/outro agreement only. The ad-heavy corpus in `benchmarks/ads/` (8
dynamic-ad-insertion episodes from 4 feeds, 6.98 hours) is fetched with
`.venv/bin/python scripts/download_ads_corpus.py`, which reuses the saved
feed snapshots (`--refresh-feeds` refetches and changes the corpus). It has
been transcribed with both models (`benchmarks/ads/runs/ads-{tiny,small}-en-words-20260923`,
via `benchmark_whisper_parallel.py --corpus benchmarks/ads --input-format mp3`);
run the same eval over it with `--eval-id ads-asr-v1 --corpus benchmarks/ads
--variant tiny.en=benchmarks/ads/runs/ads-tiny-en-words-20260923 --variant
small.en=benchmarks/ads/runs/ads-small-en-words-20260923`. Harness tests:
`.venv/bin/python -m unittest discover -s tests -t . -p 'test_eval_*.py'`.

## Transcription benchmarks

Local transcription speed benchmark of the latest ten This American Life feed
releases, downloaded on 2026-09-20. The corpus contains 10 hours 13 minutes of
audio (595 MB). Feed releases include reruns; selection is by publication date,
not episode number.

Measured result: **26.44× real time** — 10:13:03.778 of audio transcribed in
23:10.986, or 23:41.833 including MP3 decoding and output writes. Peak process
RSS was 2.95 GiB on an AMD Ryzen Threadripper PRO 5955WX using 16 CPU threads.
See [the full report](benchmarks/tal/REPORT.md) and
[per-episode results](benchmarks/tal/results.csv).

Additional parallel benchmarks compare faster-whisper `tiny.en` and
parakeet.cpp `parakeet-tdt_ctc-110m` against the original baseline. See
[the engine comparison](benchmarks/tal/COMPARISON.md). Each new run uses ten
persistent worker processes with two inference threads each. Runs execute
separately to avoid competing for the CPU. The new runs read prepared 16 kHz
mono PCM; corpus throughput uses the actual parallel wall clock, including
input reads and transcript writes, excluding startup and warmup.

For all ten episodes, `tiny.en` took **143.16 seconds (256.93× real time)**;
Parakeet TDT 110M took **191.57 seconds (192.02×)** and its CTC decoder took
**181.41 seconds (202.76×)**. All passed checks for the full episode set,
input/model hashes, saved transcripts, and timing arithmetic. The two Parakeet
runs use matching model weights, inputs, worker counts, batch sizes, chunking,
and engine revisions.
This comparison measures throughput; recognition accuracy was not scored.

A fresh comparison of multilingual `tiny` and `tiny.en` uses the same ten full
episodes, CPU int8, and 10 workers × 2 threads, with worker memory sampling.
`tiny.en` took **664.31 seconds** with **9.89 GiB** sampled aggregate peak PSS;
`tiny` took **453.11 seconds** with **10.36 GiB**. That is **31.79% less elapsed
time** and **484.60 MiB (4.79%) more sampled peak RAM** for `tiny` in these runs.
Substantial, changing CPU load from unrelated jobs prevents interpreting the
wall-time difference as an isolated model speed advantage or comparing these
times directly with the earlier idle-host results. Summed episode CPU time was
9.38% lower for `tiny`, but also remains sensitive to host contention.
Both variants transcribed English with `language=en`; Chinese performance and
recognition accuracy were not measured. See [the tiny variant report](benchmarks/tal/TINY_VARIANTS.md)
for measurements, verification, model identities, and memory methodology.

Four shorter checks transcribed the first 300 seconds of every episode, in
`tiny.en → tiny → tiny → tiny.en` order. `tiny` took 6.47% longer in the first
pair and 6.81% less time in the reverse-order pair; pooled elapsed time was
0.83% lower and pooled CPU time was 2.33% lower. These checks found no consistent
slowdown for `tiny` under the observed contention. See
[the crossover report](benchmarks/tal/TINY_CROSSOVER.md). Full-episode memory
measurements above remain the RAM comparison; the shorter checks do not replace
them.

The variant runs use `--model-path benchmarks/tal/models/tiny.en` or
`--model-path benchmarks/tal/models/tiny`, the corresponding
`--model-id Systran/faster-whisper-tiny.en` or `Systran/faster-whisper-tiny`,
and `--memory-interval-ms 250` with the parallel Whisper command below.
Regenerate the verified full-run report with:

```bash
.venv/bin/python scripts/report_tiny_variants.py \
  benchmarks/tal/runs/tiny-en-memory-20260921 \
  benchmarks/tal/runs/tiny-multilingual-memory-20260921
```

The new scripts preserve earlier results in unique directories under
`benchmarks/tal/runs/`. To repeat on this prepared workspace, choose unused
run IDs:

```bash
export TMPDIR="$PWD/.cache/tmp"
export HF_HOME="$PWD/.cache/huggingface"
export XDG_CACHE_HOME="$PWD/.cache"
export OPENBLAS_NUM_THREADS=1
.venv/bin/python scripts/prepare_tal_pcm.py
.venv/bin/python -u scripts/benchmark_whisper_parallel.py \
  --workers 10 --threads-per-worker 2 --input-format pcm --run-id tiny-repeat
.venv/bin/python -u scripts/benchmark_parakeet_parallel.py \
  --workers 10 --threads-per-worker 2 --no-pin-workers --batch-size 8 \
  --decoder tdt --run-id parakeet-tdt-repeat
.venv/bin/python -u scripts/benchmark_parakeet_parallel.py \
  --workers 10 --threads-per-worker 2 --no-pin-workers --batch-size 8 \
  --decoder ctc --run-id parakeet-ctc-repeat
.venv/bin/python scripts/report_tal_comparison.py \
  benchmarks/tal/runs/tiny-repeat benchmarks/tal/runs/parakeet-tdt-repeat \
  benchmarks/tal/runs/parakeet-ctc-repeat
```

Parakeet uses the published Q8_0 GGUF model with separate TDT and CTC runs and batches of
eight non-overlapping 30-second chunks. Every input sample is processed,
including the final partial chunk. Its model and source revisions are recorded
in each run's JSON. The build lives in `vendor/parakeet.cpp/build-cpu`; downloads,
builds, caches, and all benchmark artifacts stay inside this project.

All benchmark artifacts live under `benchmarks/tal/`:

- `feed.xml` and `manifest.json`: source snapshot, URLs, dates, durations, hashes.
- `audio/`: the ten original MP3 files.
- `models/small.en/`: downloaded local speech recognition model.
- `transcripts/`: generated text and JSON segments with timestamps.
- `results.json` and `run.log`: full-corpus measurements and progress.
- `requirements.txt`: exact installed dependency versions.

The benchmark uses faster-whisper's batched CPU inference with `small.en`, int8,
English, beam size 5, batch size 8, and voice activity detection. Episodes run
sequentially through one loaded model. A separate 30-second warmup precedes the
timed run. Each inference timer includes VAD, features, and fully consuming the
segment generator; MP3 decoding is measured separately. End-to-end episode
timings also include decoding and writing transcripts. Downloads, model loading,
and warmup are excluded from aggregate throughput and reported separately where
applicable. Peak RSS is the process high-water mark, not an isolated per-episode
allocation. This measures speed for this configuration, not transcription
accuracy or the maximum possible speed of the hardware.

The exploratory 120-second samples decode the entire first MP3 before slicing
the waveform, so only their transcription timings compare sample inference
speed; sample decode and end-to-end figures are not bounded-clip measurements.
Warmup also decodes the first full MP3; its backing buffer remains allocated
during this run and is included in the process memory high-water mark.
Each run overwrites the canonical results and transcript files; preserve
`benchmarks/tal/` before trying another configuration.

To reproduce from the saved feed and model:

```bash
mkdir -p .cache/tmp .cache/huggingface
export TMPDIR="$PWD/.cache/tmp"
export UV_CACHE_DIR="$PWD/.cache/uv"
export HF_HOME="$PWD/.cache/huggingface"
export XDG_CACHE_HOME="$PWD/.cache"
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r benchmarks/tal/requirements.txt
.venv/bin/python scripts/download_tal.py
HF_HUB_DISABLE_XET=1 .venv/bin/python -c 'from huggingface_hub import snapshot_download; snapshot_download("Systran/faster-whisper-small.en", revision="d1d751a5f8271d482d14ca55d9e2deeebbae577f", local_dir="benchmarks/tal/models/small.en", allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"])'
OMP_NUM_THREADS=16 OPENBLAS_NUM_THREADS=1 \
  .venv/bin/python -u scripts/benchmark_tal.py --threads 16 \
  > benchmarks/tal/run.log 2>&1
.venv/bin/python scripts/report_tal.py
```

The download script uses the saved feed snapshot so rerunning it preserves the
corpus. To fetch a new snapshot, use `curl -L --fail
https://www.thisamericanlife.org/podcast/rss.xml -o benchmarks/tal/feed.xml` and
move existing benchmark artifacts aside first. Audio, models, transcripts, and
caches are ignored by Git but retained in this project directory.
