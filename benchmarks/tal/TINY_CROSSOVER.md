# Whisper tiny crossover benchmark

This four-run crossover checks the relative runtime of tiny.en and multilingual tiny under changing host contention. The order was tiny.en, multilingual tiny, multilingual tiny, tiny.en; all runs were sequential.

| Order | Model | Suite wall | Throughput | Summed episode CPU | Suite peak PSS | All-phase peak PSS | Decode | Load start → end |
|---:|---|---:|---:|---:|---:|---:|---:|---|
| 1 | tiny.en | 62.07s | 48.33× | 342.72s | 7.64 GiB | 7.64 GiB | 14.37s | [36.025390625, 47.99462890625, 41.9560546875] → [53.146484375, 50.72314453125, 43.41796875] |
| 2 | tiny (multilingual) | 66.09s | 45.39× | 337.03s | 8.00 GiB | 8.00 GiB | 12.86s | [53.146484375, 50.72314453125, 43.41796875] → [56.3935546875, 52.76708984375, 44.8388671875] |
| 3 | tiny (multilingual) | 70.69s | 42.44× | 352.10s | 7.32 GiB | 7.32 GiB | 11.88s | [56.3935546875, 52.76708984375, 44.8388671875] → [57.56591796875, 54.2470703125, 46.06591796875] |
| 4 | tiny.en | 75.86s | 39.55× | 362.84s | 7.86 GiB | 7.86 GiB | 14.90s | [57.56591796875, 54.2470703125, 46.06591796875] → [60.78759765625, 56.3076171875, 47.56103515625] |

Multilingual/tiny.en wall ratio was 1.065× for run 2 over run 1 and 0.932× for run 3 over run 4. Pooling wall seconds within each model gave 0.992×.

The corresponding summed episode CPU ratios were 0.983× and 0.970×. Suite peak PSS ratios were 1.047× and 0.931×.

Each run transcribed only the first 300 seconds of every episode, for 3,000 seconds of inference input. Each full WAV was nevertheless decoded before slicing, and suite wall time includes that full-file decode. These excerpt timings are separate from the ten-full-episode benchmark.

The short-run sampled PSS values confirm instrumentation and provide crossover context. They do not replace the full-run RAM measurements because shorter inference can change allocation peaks and worker overlap.

The host carried substantial unrelated CPU work. Wall time includes external scheduling contention, so neither an individual pair nor the pooled ratio is an isolated hardware-idle model effect. Summed episode CPU time is worker process time, not full-machine wall time.

Both variants forced `language=en` on the same English TAL excerpts with CPU int8, 10 workers × 2 threads, beam 5, batch 8, VAD enabled, and the same memory sampler. This benchmark does not test Chinese runtime or recognition accuracy.
