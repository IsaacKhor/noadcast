# This American Life transcription benchmark

Completed 2026-09-20T17:24:35.690662+00:00. Latest ten official feed releases from the 2026-09-20 snapshot; download manifest recorded at 2026-09-20T16:57:58.839923+00:00. Includes reruns; ordered by feed publication date.

**10.22 hours of audio transcribed in 23.18 minutes: 26.44× real time (RTF 0.0378).** Including MP3 decoding and output writes, the suite took 23.70 minutes (25.87×).

CPU: AMD Ryzen Threadripper PRO 5955WX, 16 physical cores / 32 logical CPUs; 123 GiB RAM; no exposed compute GPU.

Configuration: faster-whisper 1.2.1, CTranslate2 4.8.2, small.en, CPU int8, 16 threads, batch size 8, beam size 5, English, VAD enabled, no previous-text conditioning, no word timestamps. Batched output uses VAD chunk timestamps rather than word-aligned timestamps. Model revision `d1d751a5f8271d482d14ca55d9e2deeebbae577f`.

| Episode | Published | Audio min | Transcribe min | Speed | RTF |
|---|---|---:|---:|---:|---:|
| 646: The Secret of My Death | 13 Sep 2026 | 65.32 | 2.45 | 26.70× | 0.0374 |
| 449: Middle School | 06 Sep 2026 | 59.44 | 2.39 | 24.88× | 0.0402 |
| 896: I Know What You Need | 30 Aug 2026 | 66.40 | 2.56 | 25.96× | 0.0385 |
| 206: Somewhere in the Arabian Sea | 23 Aug 2026 | 59.45 | 2.40 | 24.79× | 0.0403 |
| 895: Label Maker! | 16 Aug 2026 | 57.74 | 2.03 | 28.40× | 0.0352 |
| 894: I Couldn't Help but Notice | 02 Aug 2026 | 59.05 | 2.27 | 26.05× | 0.0384 |
| 893: Testosterone | 26 Jul 2026 | 60.31 | 2.22 | 27.19× | 0.0368 |
| 892: Trapped on a Bus | 19 Jul 2026 | 60.87 | 2.32 | 26.27× | 0.0381 |
| 891: The Test Case | 12 Jul 2026 | 70.31 | 2.65 | 26.48× | 0.0378 |
| 890: Maximal Americanness | 05 Jul 2026 | 54.17 | 1.90 | 28.47× | 0.0351 |

Median per-episode speed: 26.38×. Range: 24.79–28.47×. Aggregate throughput is duration-weighted, not an average of episode speed ratios.

Model loading: 0.72s. Separate 30-second warmup: 1.73s. Total MP3 decode time: 30.82s. Peak process RSS: 2.95 GiB.

Timing uses a monotonic wall clock and consumes every lazy transcript segment. Transcription includes VAD, feature extraction, inference, and collecting segments. End-to-end suite time additionally includes decoding and output writes. Downloads, model loading, and warmup are excluded. Memory is the cumulative process high-water mark. One full-corpus run was performed; no repeated-run confidence interval or formal accuracy evaluation is claimed.

A preliminary two-minute sample measured 20.81× with 16 threads and 16.41× with 8 threads. The 16-thread configuration was selected for the full corpus. Those short samples are exploratory and are not included in the full-corpus figures.

All ten audio hashes, the model hash, recorded decoded durations, transcript existence and timestamp bounds, and aggregate arithmetic were verified. The runner passes each full decoded waveform to the pipeline and exhausts its segment generator. Artifact checks confirm recorded durations match the source files and outputs were saved; they do not establish recognition accuracy. See `verification.json`, `results.json`, `results.csv`, `manifest.json`, `feed.xml`, and `requirements.txt` for the underlying evidence. Audio, models, transcripts, and caches remain local to this project.
