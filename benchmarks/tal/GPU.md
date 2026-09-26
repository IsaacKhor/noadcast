# This American Life transcription benchmark — GPU (CUDA)

Same ten episodes (10.22 hours), same harness (`scripts/benchmark_whisper_parallel.py`), now with `--device cuda`. Throughput is total corpus audio divided by suite wall clock; worker model load and warmup are excluded, and per-episode PCM WAV decoding is included, as in the CPU runs. Runs were strictly sequential because they share one GPU. Reproduce with `scripts/run_gpu_sweep.sh`, then `scripts/report_gpu.py`.

GPU: NVIDIA TITAN V (Volta, 12 GiB HBM2, compute 7.0), driver 580.178.04. CUDA libraries come from pip wheels installed under `.cache/cuda-libs` (cuBLAS 12.9, cuDNN 9.26). CTranslate2 4.8.2 and faster-whisper 1.2.1 are unchanged. CPU: Ryzen Threadripper PRO 5955WX, as in the CPU baselines. Decoding settings match those runs: beam 5, English, VAD on, no previous-text conditioning, PCM input.

**Headline: tiny.en runs 1046× real time on the GPU, 4.1× the best CPU configuration, which used all 16 cores. small.en runs 297×, 11.5× the CPU. The whole 10.22-hour corpus transcribes in 35 s with tiny.en and 2 min with small.en.** On the GPU, small.en with word timestamps (274×) is faster than CPU tiny.en with word timestamps (230×). Parakeet TDT/CTC 110M q8_0 (parakeet.cpp CUDA build) runs 846× with TDT and 1029× with CTC, 4.4× and 5.1× its CPU runs (see the Parakeet section below).

## Full corpus (10 episodes, 10.22 h)

| Model | Word timestamps | GPU config | GPU suite wall | GPU throughput | CPU baseline | CPU throughput | Speedup | Word agreement vs CPU |
|---|---|---|---:|---:|---|---:|---:|---:|
| tiny.en | off | float16 · 4w · b32 | 35.2s | 1046× | tiny-en-full-10x2 (10×2 int8) | 256.9× | **4.1×** | 98.4% |
| tiny.en | on | float16 · 4w · b32 | 38.8s | 948× | wt-crossover-2-on-20260922 (10×2 int8) | 230.3× | **4.1×** | 98.4% |
| small.en | off | float16 · 2w · b32 | 123.8s | 297× | REPORT.md (1×16 int8, MP3) | 25.9× | **11.5×** | 99.1% |
| small.en | on | float16 · 2w · b32 | 134.5s | 274× | small-en-words-20260922 (4×2 int8) | 22.9× | **11.9×** | 99.1% |
| parakeet-tdt_ctc-110m (TDT) | n/a | q8_0 · 2w · b32 | 43.5s | 846× | parakeet-tdt-110m-full-10x2 (10×2 q8_0) | 192.0× | **4.4×** | 99.9% |
| parakeet-tdt_ctc-110m (CTC) | n/a | q8_0 · 2w · b32 | 35.8s | 1029× | parakeet-ctc-110m-full-10x2 (10×2 q8_0) | 202.8× | **5.1×** | 99.8% |

Word agreement is a difflib ratio over normalised words against the CPU transcript of the same model (int8 for Whisper, the same q8_0 GGUF for Parakeet). It measures drift from float16 vs int8 numerics, not accuracy against a reference. Whisper segment counts are identical to the CPU runs (1195 across the corpus).

Baseline caveats:
- The small.en no-word-timestamps baseline is the original single-process run. It used MP3 input with 1 × 16 threads, and its 25.87× includes 30.8 s of MP3 decoding. Against its inference-only figure (26.44×), the GPU speedup is 11.2×.
- The small.en word-timestamps CPU baseline used 4 workers × 2 threads, which is only 8 of the 16 cores. A full-core CPU run would likely narrow that 11.9×.
- The tiny.en CPU baselines are the tuned 10 × 2 configurations, the best CPU setups measured.

## Tuning

tiny.en saturates at about 1000–1050× from 4 workers upward. With 8 or 12 workers, or batch 64, throughput stays within noise. The limit is the CPU-side per-worker work (VAD, feature extraction, and beam-search bookkeeping) that feeds a small model, not the GPU. float16 beat int8_float16 in every configuration on this Volta card, since it has no int8 tensor-core path.

small.en is GPU-bound at about 250–300×, and adding workers barely helps. With 4 workers × batch 32 it ran out of the 12 GiB of VRAM. Two workers × batch 32 peaked at 7.2 GiB.

### tiny.en pilot (episodes 1–4, full length)

| Compute type | Workers | Threads/worker | Batch | Suite wall | Throughput |
|---|---:|---:|---:|---:|---:|
| float16 | 4 | 4 | 64 | 14.3s | 1054× |
| float16 | 8 | 2 | 64 | 14.5s | 1037× |
| float16 | 4 | 4 | 32 | 15.2s | 989× |
| float16 | 8 | 2 | 32 | 15.3s | 985× |
| float16 | 8 | 4 | 32 | 15.3s | 983× |
| float16 | 12 | 2 | 32 | 15.3s | 982× |
| float16 | 4 | 4 | 16 | 17.0s | 885× |
| int8_float16 | 4 | 4 | 32 | 17.0s | 884× |
| int8_float16 | 4 | 4 | 16 | 19.7s | 762× |
| float16 | 4 | 4 | 8 | 20.0s | 750× |
| float16 | 2 | 4 | 32 | 20.6s | 730× |
| int8_float16 | 2 | 4 | 32 | 22.0s | 684× |
| float16 | 2 | 4 | 16 | 22.3s | 675× |
| int8_float16 | 4 | 4 | 8 | 23.4s | 643× |
| float16 | 2 | 4 | 8 | 24.9s | 603× |
| int8_float16 | 2 | 4 | 16 | 25.5s | 590× |
| int8_float16 | 2 | 4 | 8 | 29.1s | 516× |
| float16 | 1 | 4 | 32 | 33.0s | 456× |
| float16 | 1 | 4 | 16 | 34.8s | 432× |
| int8_float16 | 1 | 4 | 32 | 34.9s | 431× |
| float16 | 1 | 4 | 8 | 36.7s | 409× |
| int8_float16 | 1 | 4 | 16 | 36.9s | 407× |
| int8_float16 | 1 | 4 | 8 | 40.7s | 370× |

### small.en pilot (float16, episodes 1–4)

| Workers | Batch | Throughput | Peak VRAM |
|---:|---:|---:|---:|
| 2 | 32 | 276× | 7.2 GiB |
| 2 | 16 | 249× | 4.4 GiB |
| 1 | 32 | 247× | 3.7 GiB |
| 1 | 16 | 226× | 2.4 GiB |
| 3 | 8 | 226× | 4.6 GiB |
| 2 | 8 | 215× | 3.1 GiB |
| 4 | 32 | OOM | >12 GiB |

## Parakeet TDT/CTC 110M q8_0 (parakeet.cpp, CUDA)

The same Q8_0 GGUF (`parakeet-tdt_ctc-110m-q8_0.gguf`, SHA-256 `614feee3…`) and harness as the CPU runs (`scripts/benchmark_parakeet_parallel.py`): fixed 30-second chunks, no VAD, no overlap, greedy decoding, predecoded PCM input. The only change is `--library-path vendor/parakeet.cpp/build-cuda/libparakeet.so`, and the ggml CUDA backend selects CUDA0 by itself.

**TDT runs at 846× (4.4× the 10×2 CPU run) and CTC at 1029× (5.1×). Both transcripts match the CPU q8_0 output almost word for word.** CTC is now as fast as GPU tiny.en without word timestamps (1046×). Neither Parakeet decoder produces word timestamps through this C API, so they aren't a drop-in replacement for the server's tiny.en with word timestamps (948×).

Parakeet is GPU-bound, with a best case of about 850×. ggml's compute buffer grows about 130 MiB per 30-second chunk in a batch: 4.1 GiB at batch 32 and 8.3 GiB at batch 64. So 4×32, 2×64 and 1×128 don't fit in 12 GiB. Where memory ran out, the worker either reported `cudaMalloc failed` or segfaulted (4×32). Batch 8 with 2 or more workers was slower than 1 worker, because concurrent small batches contend for the GPU without filling it. Volta has CUDA graphs disabled by ggml; the logs show that notice for every graph.

### Parakeet TDT pilot (episodes 1–4)

| Workers | Threads/worker | Batch | Throughput |
|---:|---:|---:|---:|
| 2 | 4 | 32 | 836× |
| 1 | 4 | 32 | 777× |
| 1 | 4 | 64 | 762× |
| 1 | 4 | 16 | 740× |
| 1 | 4 | 8 | 681× |
| 4 | 4 | 16 | 664× |
| 8 | 2 | 16 | 663× |
| 2 | 4 | 16 | 661× |
| 2 | 4 | 8 | 523× |
| 4 | 4 | 8 | 517× |
| 4 | 4 | 32 | OOM |
| 2 | 4 | 64 | OOM |
| 1 | 4 | 128 | OOM |

### Building the CUDA library

The host has only the NVIDIA driver. CUDA 13 dropped Volta (sm_70) and the system GCC 15 is too new for CUDA 12, so a local CUDA 12.9 toolchain with GCC 14 comes from conda-forge through a static micromamba, all under `.cache/`:

```
curl -fsSL https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xj -C .cache/micromamba bin/micromamba
MAMBA_ROOT_PREFIX=$PWD/.cache/micromamba/root .cache/micromamba/bin/micromamba create -y -p $PWD/.cache/cuda-12.9 \
    -c conda-forge cuda-version=12.9 cuda-nvcc cuda-cudart-dev libcublas-dev cuda-cccl gxx_linux-64=14 gcc_linux-64=14
C=$PWD/.cache/cuda-12.9
cmake -S vendor/parakeet.cpp -B vendor/parakeet.cpp/build-cuda -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON \
    -DGGML_LLAMAFILE=ON -DPARAKEET_SHARED=ON -DPARAKEET_BUILD_CLI=ON -DPARAKEET_BUILD_SERVER=OFF \
    -DPARAKEET_BUILD_TESTS=OFF -DPARAKEET_GGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=70 -DCUDAToolkit_ROOT=$C \
    -DCMAKE_CUDA_COMPILER=$C/bin/nvcc -DCMAKE_CUDA_HOST_COMPILER=$C/bin/x86_64-conda-linux-gnu-g++
cmake --build vendor/parakeet.cpp/build-cuda -j 32
```

The resulting libraries carry an RPATH into `.cache/cuda-12.9/lib` (cudart, cuBLAS), so no environment is needed at run time. ggml-cuda aborts in its static destructors at process exit ("CUDA error: driver shutting down"). The harness worker therefore flushes its result queue and calls `os._exit(0)` after freeing the context. Reproduce with `scripts/run_gpu_sweep.sh parakeet-pilot`, then `W=2 B=32 scripts/run_gpu_sweep.sh parakeet-full`.

One run per configuration; no repeated-run confidence intervals.
