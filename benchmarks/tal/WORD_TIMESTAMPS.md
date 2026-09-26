# Whisper word-timestamp cost crossover

**ACCEPT: `word_timestamps=True` took 1.124× the pooled suite wall time (1.129× and 1.118× pairwise) and 1.115× the pooled summed worker CPU, changed sampled aggregate peak PSS by -175 MiB pooled (largest pairwise change +579 MiB), and left transcript text byte-identical on 10 of 10 episodes.**

This four-run crossover measures the cost of faster-whisper word timestamps on the full ten-episode TAL corpus (10.22 hours). The order was OFF, ON, ON, OFF; the runs were sequential and did not overlap. Throughput is total corpus audio divided by the measured suite wall clock.

| Order | Word timestamps | Suite wall | Throughput | Summed episode CPU | Suite peak PSS | Startup peak PSS | Suite peak RSS | Startup peak RSS | Median episode RTF | Load start → end |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | off | 141.46s | 260.04× | 2402.69s | 10.88 GiB | 5.01 GiB | 11.49 GiB | 5.43 GiB | 0.0346 | 1.07 / 1.14 / 1.27 → 15.31 / 7.71 / 3.75 |
| 2 | on | 159.72s | 230.30× | 2702.44s | 9.97 GiB | 4.42 GiB | 10.59 GiB | 4.83 GiB | 0.0391 | 15.31 / 7.71 / 3.75 → 17.95 / 12.76 / 6.34 |
| 3 | on | 159.08s | 231.23× | 2691.52s | 10.59 GiB | 4.12 GiB | 11.22 GiB | 4.74 GiB | 0.0390 | 17.95 / 12.76 / 6.34 → 15.97 / 14.95 / 8.27 |
| 4 | off | 142.23s | 258.62× | 2436.29s | 10.03 GiB | 4.55 GiB | 10.65 GiB | 5.00 GiB | 0.0352 | 15.97 / 14.95 / 8.27 → 16.75 / 16.31 / 9.78 |

| Comparison | Suite wall ratio | Summed CPU ratio | Worker inference ratio | Suite peak PSS ratio | Suite peak PSS change | Median episode `ru_maxrss` change |
|---|---:|---:|---:|---:|---:|---:|
| Run 2 ON / run 1 OFF | 1.129× | 1.125× | 1.130× | 0.917× | -928.6 MiB | -5.7 MiB |
| Run 3 ON / run 4 OFF | 1.118× | 1.105× | 1.109× | 1.056× | +578.5 MiB | +47.8 MiB |
| Pooled ON / OFF | 1.124× | 1.115× | 1.119× | 0.984× | -175.0 MiB | +21.1 MiB |

| Acceptance criterion | Threshold | Measured | Result |
|---|---|---:|---|
| pooled ON/OFF suite wall ratio | ≤ 1.20 | 1.124× | pass |
| each pairwise ON/OFF suite wall ratio | ≤ 1.25 | 1.129×, 1.118× | pass |
| pooled ON/OFF summed episode CPU ratio | ≤ 1.25 | 1.115× | pass |
| largest aggregate suite peak PSS increase (pairwise or pooled) | ≤ 1024 MiB | +578.5 MiB | pass |
| episodes with identical transcript text SHA-256 in all four runs | 10 of 10 | 10 of 10 | pass |
| episodes with identical segment counts in all four runs | 10 of 10 | 10 of 10 | pass |

Decision: **ACCEPT**. The pooled ratio sums the two ON wall clocks and divides by the two OFF wall clocks; the peak-PSS criterion uses the largest of the two pairwise changes and the pooled change of the mean peaks, so a single favourable pair cannot hide an increase.

Model identity: tiny.en SHA-256 `1a5afae06a4db91c975c9a9d78be5cc110ee4ea022ad57d55492e4550e936b2a`, revision `0d3d19a32d3338f10357c0889762bd8d64bbdeba`, identical in all four runs.

## Per-episode

Text SHA-256 is computed over the concatenation of every emitted segment `text` field and is shown truncated; full digests for each run are in `word-timestamps-comparison.json`. Worker RTF is each episode's inference seconds over its audio seconds. Episodes run concurrently, so these latencies must not be added.

| Episode | Segments | Words (ON) | Text SHA-256, all runs | Worker RTF 1 / 2 / 3 / 4 | Pooled ON/OFF RTF | Last segment end OFF → ON |
|---|---:|---:|---|---:|---:|---:|
| 01 · 646: The Secret of My Death | 122 | 10,428 | `3904459f0f0b` | 0.0346 / 0.0389 / 0.0389 / 0.0345 | 1.126× | 3909.14s → 3908.61s |
| 02 · 449: Middle School | 121 | 10,293 | `e7802f751ebf` | 0.0376 / 0.0415 / 0.0424 / 0.0377 | 1.115× | 3559.12s → 3557.95s |
| 03 · 896: I Know What You Need | 133 | 10,521 | `9eea2d510634` | 0.0347 / 0.0388 / 0.0391 / 0.0348 | 1.122× | 3961.10s → 3960.89s |
| 04 · 206: Somewhere in the Arabian Sea | 115 | 10,615 | `7f6156935371` | 0.0374 / 0.0427 / 0.0421 / 0.0380 | 1.123× | 3556.37s → 3555.91s |
| 05 · 895: Label Maker! | 107 | 8,120 | `becb2bce63d5` | 0.0335 / 0.0378 / 0.0374 / 0.0357 | 1.088× | 3454.61s → 3454.18s |
| 06 · 894: I Couldn't Help but Notice | 119 | 9,046 | `c88e982bb38a` | 0.0363 / 0.0395 / 0.0394 / 0.0350 | 1.107× | 3504.14s → 3501.17s |
| 07 · 893: Testosterone | 117 | 9,437 | `924aba7d408d` | 0.0342 / 0.0393 / 0.0399 / 0.0354 | 1.138× | 3608.50s → 3608.13s |
| 08 · 892: Trapped on a Bus | 120 | 9,709 | `51a0812fab6b` | 0.0354 / 0.0399 / 0.0386 / 0.0356 | 1.105× | 3627.31s → 3626.48s |
| 09 · 891: The Test Case | 139 | 10,819 | `06a6565b74c9` | 0.0334 / 0.0377 / 0.0375 / 0.0335 | 1.124× | 4209.14s → 4208.45s |
| 10 · 890: Maximal Americanness | 102 | 7,804 | `d728524f43ac` | 0.0313 / 0.0377 / 0.0373 / 0.0341 | 1.148× | 3226.51s → 3226.04s |

## Methodology

Each run transcribed every full episode from the same predecoded 16 kHz mono PCM with faster-whisper tiny.en, CPU int8, ten persistent workers × two threads under OS scheduling, batch 8, beam 5, VAD enabled, English forced, no previous-text conditioning, and 250 ms worker PSS/RSS sampling. `word_timestamps` is the only configuration difference: the effective per-episode transcription options faster-whisper recorded differ only in that key, and the effective VAD options and post-VAD speech durations are identical across all four runs. The suite wall clock starts after every worker has loaded the model and finished a 30-second warmup and includes WAV reads and transcript writes. Summed episode CPU is worker process time during inference.

Segment boundaries legitimately differ between the arms. With word timestamps, faster-whisper replaces VAD chunk bounds with the first word start and last word end; this was verified for every segment in both ON runs. Across the 10 episodes with matching segment counts, 1,191 of 1,195 segments had at least one bound move relative to the OFF runs (largest start shift 26.28s, largest end shift 74.71s). The final segment end moved inward by 0.21–2.97s. `last_segment_end` is therefore compared as a word-derived value, not asserted equal. Word arrays were identical between the two ON runs for 10 of 10 episodes, and segment bounds were identical between the two OFF runs for 10 of 10. Runs 2 and 3 contain 2 and 2 word starts that run backwards at VAD chunk splices; the sentence joiner must clamp them.

## Caveats

Each configuration ran twice in one sequence on one host, so no confidence interval is available. The OFF, ON, ON, OFF order cancels linear drift in host load, not arbitrary contention. The host was shared with other work: the 1-minute load average was 1.07 before run 1 and 15.31–17.95 at the end of each run, which is consistent with the benchmark's own twenty inference threads but cannot exclude concurrent external work. CPU seconds also move with cache contention and clock frequency. Sampled peak PSS varied by 869 MiB between the two OFF runs, more than the pooled ON/OFF change, so the memory result means no increase was detectable at this noise level, not a measured zero. Peaks are 250 ms samples of the summed worker PSS with the parent excluded, not continuous high-water marks; `ru_maxrss` is each worker's own high-water mark and double-counts shared pages. Identical text shows that word alignment does not change decoding; it says nothing about recognition accuracy, which was not scored.

All ten source and PCM hashes, full-file inference, the model hash and revision, the episode set and order, summary and per-episode timing arithmetic, memory sample peaks and worker coverage, logged summaries and load-average brackets, sequential run timestamps, transcript artifacts, and word-derived segment bounds were verified. See `word-timestamps-comparison.json` for every check and `word-timestamps-comparison.csv` for per-run, per-episode values.
