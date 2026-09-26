# This American Life transcription engine comparison

All runs use the same ten episodes (10.22 hours). The headline throughput is total corpus audio divided by the measured suite wall clock. For parallel runs it is never calculated from the sum of worker processing times.

| Engine / model | Quantization / decoder | Input | Workers × threads | Suite wall | Wall throughput | Cumulative worker peak RSS |
|---|---|---|---:|---:|---:|---:|
| faster-whisper / Systran/faster-whisper-small.en | int8 / whisper beam 5 | MP3 | 1 × 16 | 1421.83s | 25.87× | 2.95 GiB |
| faster-whisper / Systran/faster-whisper-tiny.en | int8 / whisper beam 5 | PCM | 10 × 2 | 143.16s | 256.93× | 12.50 GiB |
| parakeet.cpp / nvidia/parakeet-tdt_ctc-110m | q8_0 / tdt | PCM | 10 × 2 | 191.57s | 192.02× | 13.78 GiB |
| parakeet.cpp / nvidia/parakeet-tdt_ctc-110m | q8_0 / ctc | PCM | 10 × 2 | 181.41s | 202.76× | 13.75 GiB |

Model identities:

- small.en baseline: SHA-256 `62b2a45b05ee59acb4a5341b33ee35e041395d378d418a18acfe4c9e768ee37a`, revision `d1d751a5f8271d482d14ca55d9e2deeebbae577f`.
- tiny-en-full-10x2: SHA-256 `1a5afae06a4db91c975c9a9d78be5cc110ee4ea022ad57d55492e4550e936b2a`, revision `0d3d19a32d3338f10357c0889762bd8d64bbdeba`.
- parakeet-tdt-110m-full-10x2: SHA-256 `614feee3a990cf0e672b0314f4da0c80ae8da9094507f5ccb7c42e43b5fc5a12`, revision `431a349f3051ab85c22b9b7a2741b5fe77065665`.
- parakeet-ctc-110m-full-10x2: SHA-256 `614feee3a990cf0e672b0314f4da0c80ae8da9094507f5ccb7c42e43b5fc5a12`, revision `431a349f3051ab85c22b9b7a2741b5fe77065665`.

The original small.en run spent 1390.99s in inference (26.44×) and 1421.83s end to end (25.87×), including 30.82s of MP3 decoding.

The newer runs consume predecoded 16 kHz mono PCM. Their suite wall clocks include reading and decoding the WAV containers, while the one-time PCM preparation (5.78s) is excluded. That makes the input-stage accounting explicit, but the formats are not identical.

The original configuration is faster-whisper small.en, CPU int8, beam 5, batch 8, VAD enabled, with one 16-thread worker. The tiny.en configuration keeps those decoding settings and uses ten 2-thread workers. The Parakeet configurations use the same Q8_0 hybrid TDT/CTC model with greedy TDT and greedy CTC decoding, respectively. Both use batch 8 and ten 2-thread workers, and process fixed contiguous 30-second chunks with no VAD and no overlap.

Because the original uses 1 × 16 threads and the newer runs use 10 × 2, their speed differences are not an isolated model-only comparison. Recognition accuracy and the effect of Parakeet's 30-second chunk boundaries were not measured.

## Per-episode task latency

Each value is `processing / task latency` in seconds. Task latency includes the input read/decode where recorded. Episodes overlap in parallel runs, so these are concurrent latencies and must not be added to obtain corpus wall time.

| Episode | small.en baseline (process / latency) | tiny-en-full-10x2 (process / latency) | parakeet-tdt-110m-full-10x2 (process / latency) | parakeet-ctc-110m-full-10x2 (process / latency) |
|---|---:|---:|---:|---:|
| 01 · 646: The Secret of My Death | 146.75s / 150.07s | 135.97s / 137.99s | 189.39s / 189.44s | 178.77s / 178.82s |
| 02 · 449: Middle School | 143.33s / 146.27s | 133.94s / 134.43s | 174.53s / 174.57s | 164.93s / 164.98s |
| 03 · 896: I Know What You Need | 153.48s / 157.07s | 137.92s / 140.55s | 183.12s / 183.17s | 179.18s / 179.23s |
| 04 · 206: Somewhere in the Arabian Sea | 143.90s / 146.88s | 132.88s / 133.26s | 173.24s / 173.28s | 164.37s / 164.42s |
| 05 · 895: Label Maker! | 121.98s / 124.86s | 113.84s / 114.25s | 169.45s / 169.49s | 165.25s / 165.30s |
| 06 · 894: I Couldn't Help but Notice | 136.02s / 138.93s | 129.01s / 129.49s | 178.69s / 178.73s | 166.00s / 166.04s |
| 07 · 893: Testosterone | 133.06s / 136.01s | 126.11s / 126.60s | 174.62s / 174.67s | 160.70s / 160.75s |
| 08 · 892: Trapped on a Bus | 139.02s / 142.01s | 125.92s / 127.93s | 172.76s / 172.81s | 167.60s / 167.64s |
| 09 · 891: The Test Case | 159.28s / 162.88s | 140.53s / 143.14s | 191.51s / 191.56s | 181.36s / 181.41s |
| 10 · 890: Maximal Americanness | 114.17s / 116.86s | 101.28s / 101.61s | 161.02s / 161.06s | 155.16s / 155.20s |

Peak RSS is a process high-water mark. For multi-worker runs, the table sums each worker's maximum recorded high-water mark; it is a cumulative capacity indicator rather than a simultaneous system-memory sample.

One full-corpus run was performed for each configuration. These measurements have no repeated-run confidence interval and make no transcription-accuracy claim. Transcript checks establish complete, nonempty artifacts and matching inputs; they do not score recognition quality.

See `comparison-verification.json` for input, model, transcript, duration, and arithmetic checks, and `comparison.csv` for machine-readable per-episode timings.
