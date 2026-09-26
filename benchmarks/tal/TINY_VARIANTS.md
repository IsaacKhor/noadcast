# Whisper tiny.en and multilingual tiny benchmark

This matched benchmark measures speed and memory on the same ten English-language This American Life episodes. Both runs force `language=en`; this is not a Chinese-language runtime test and does not evaluate transcription accuracy.

Follow-up checks in both model orders are reported separately in [the crossover report](TINY_CROSSOVER.md). Read those alongside the full-run timings because external CPU contention changed during measurement.

| Model | Model binary | Suite wall | Corpus throughput | Summed episode CPU | All-phase peak PSS | Suite peak PSS | All-phase peak RSS | Suite peak RSS | Median episode `ru_maxrss` | Summed worker `ru_maxrss` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| tiny.en | 72.04 MiB | 664.31s | 55.37× | 3620.86s | 9.89 GiB | 9.89 GiB | 10.49 GiB | 10.49 GiB | 1285.61 MiB | 12.78 GiB |
| tiny (multilingual) | 72.04 MiB | 453.11s | 81.18× | 3281.14s | 10.36 GiB | 10.36 GiB | 10.97 GiB | 10.97 GiB | 1350.23 MiB | 13.07 GiB |

Multilingual tiny took 0.682× the tiny.en suite wall time. Equivalently, tiny.en delivered 0.682× its corpus throughput. These ratios describe these runs; no direction was assumed in advance.

Relative to tiny.en, multilingual tiny's observed elapsed-time change was -31.79%, its summed episode CPU-time change was -9.38%, and its sampled peak PSS change was +484.60 MiB (+4.79%).

The multilingual model binary was 1.000× the tiny.en binary. Its sampled all-phase peak PSS ratio was 1.048× and its suite-only peak PSS ratio was 1.048×.

The ratio of multilingual to tiny.en median episode `ru_maxrss` was 1.050×. Matching episodes individually and then taking the median ratio gave 0.997×.

PSS is the better estimate of physical memory because it apportions shared pages. RSS counts shared pages in every worker and can therefore double-count them. `ru_maxrss` is each worker's historical process high-water mark. The summed worker column adds those separate peaks and is neither simultaneous nor an estimate of unique physical memory.

The all-phase sampled peak includes worker model loading and warmup; the suite peak covers the timed corpus run. The configured sampling interval was 250 ms and the parent process was excluded. Under load, the actual scheduled cadence was slower:

| Model | Suite samples | Mean gap | Median gap | Maximum gap | Mean sweep | Median sweep | Maximum sweep |
|---|---:|---:|---:|---:|---:|---:|---:|
| tiny.en | 1970 | 337.3 ms | 256.0 ms | 920.8 ms | 291.7 ms | 251.2 ms | 920.8 ms |
| tiny (multilingual) | 1327 | 341.7 ms | 263.1 ms | 1116.1 ms | 311.1 ms | 263.1 ms | 1116.0 ms |

Each all-phase and suite peak PSS sample observed all ten workers. The raw cadence and sweep statistics are retained in `tiny-variants-comparison.json`.

The host was under substantial unrelated CPU contention during these runs. Recorded load averages were [38.0078125, 19.46923828125, 9.041015625] at tiny.en start and [49.1142578125, 52.48095703125, 34.6083984375] at its end, and [48.16748046875, 52.16796875, 34.69873046875] at multilingual tiny start and [35.54248046875, 51.81640625, 42.6552734375] at its end. The runs were sequential and matched, but wall time includes scheduling effects from that external workload and must not be compared directly with earlier idle-host model timings or treated as an isolated model-only slowdown. Summed episode CPU time is included as a worker-work measure; it is not full-machine wall time. CPU seconds can also change with cache and memory contention and clock frequency, so they do not fully remove the external-load confound.

Memory instrumentation was identical in both runs. Reading worker `smaps_rollup` records also consumed some CPU during sampling, so the reported wall times describe the instrumented benchmark.

Both runs used faster-whisper on CPU with int8, 10 workers × 2 threads, beam 5, batch 8, VAD enabled, English forced, no previous-text conditioning, no word timestamps, and the same predecoded PCM inputs. Runs were sequential and each configuration was measured once, so no confidence interval is available.

All ten full decoded durations, source and PCM hashes, model hashes and sizes, transcript artifacts and tail coverage, summary arithmetic, and memory sample peaks were verified. These checks do not establish recognition accuracy.
