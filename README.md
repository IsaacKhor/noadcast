# noadcast

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
