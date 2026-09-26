#!/usr/bin/env bash
# GPU (CUDA) faster-whisper sweep on the TAL corpus. Runs are sequential: they
# share one GPU, so overlapping them would contaminate every timing.
# Usage: scripts/run_gpu_sweep.sh pilot|full|parakeet-pilot|parakeet-full
set -euo pipefail
cd "$(dirname "$0")/.."
export TMPDIR=$PWD/.cache/tmp HF_HOME=$PWD/.cache/hf XDG_CACHE_HOME=$PWD/.cache
# cuBLAS 12 / cuDNN 9 installed with: uv pip install --target .cache/cuda-libs nvidia-cublas-cu12 'nvidia-cudnn-cu12>=9,<10'
export LD_LIBRARY_PATH=$PWD/.cache/cuda-libs/nvidia/cublas/lib:$PWD/.cache/cuda-libs/nvidia/cudnn/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
mkdir -p "$TMPDIR"
M=benchmarks/tal/models
LOG=benchmarks/tal/gpu-sweep-$1-$(date -u +%Y%m%d).log

run() {  # run-id, then harness args
    local id=$1; shift
    echo "[$(date -u +%FT%TZ)] start $id loadavg=$(cut -d' ' -f1-3 /proc/loadavg)" | tee -a "$LOG"
    .venv/bin/python scripts/benchmark_whisper_parallel.py --device cuda --input-format pcm \
        --threads-per-worker 4 --memory-interval-ms 500 --run-id "$id" "$@" 2>&1 | tee -a "$LOG" | grep -E "Completed|corpus_wall_speed_x|Error"
    echo "[$(date -u +%FT%TZ)] end $id" | tee -a "$LOG"
}

prun() {  # parakeet.cpp: run-id, then harness args
    local id=$1; shift
    echo "[$(date -u +%FT%TZ)] start $id loadavg=$(cut -d' ' -f1-3 /proc/loadavg)" | tee -a "$LOG"
    .venv/bin/python scripts/benchmark_parakeet_parallel.py --run-id "$id" \
        --library-path vendor/parakeet.cpp/build-cuda/libparakeet.so "$@" 2>&1 \
        | grep -v cuda_graph_set_enabled | tee -a "$LOG" | grep --line-buffered -E "Completed|corpus_wall_speed_x|Error"
    echo "[$(date -u +%FT%TZ)] end $id" | tee -a "$LOG"
}

case $1 in
pilot)
    for ct in float16 int8_float16; do
        for w in 1 2 4; do
            for b in 8 16 32; do
                run "gpu-pilot-tiny-en-$ct-w$w-b$b" --model-path $M/tiny.en --episodes 1,2,3,4 --compute-type $ct --workers $w --batch-size $b
            done
        done
    done ;;
full)
    # tiny.en is CPU-feed-bound and saturates at 4 workers; small.en is GPU-bound
    # and 4 x b32 exceeds a 12 GiB card, so it uses 2 workers.
    run gpu-tiny-en-full-float16-w4-b32 --model-path $M/tiny.en --compute-type float16 --workers 4 --batch-size 32
    run gpu-tiny-en-words-float16-w4-b32 --model-path $M/tiny.en --compute-type float16 --workers 4 --batch-size 32 --word-timestamps
    run gpu-small-en-full-float16-w2-b32 --model-path $M/small.en --model-id Systran/faster-whisper-small.en --compute-type float16 --workers 2 --batch-size 32
    run gpu-small-en-words-float16-w2-b32 --model-path $M/small.en --model-id Systran/faster-whisper-small.en --compute-type float16 --workers 2 --batch-size 32 --word-timestamps
    ;;
parakeet-pilot)
    # parakeet.cpp CUDA build: see benchmarks/tal/GPU.md for the local CUDA 12.9 toolchain.
    for w in 1 2 4; do
        for b in 8 16 32; do
            prun "gpu-parakeet-pilot-tdt-w$w-b$b" --decoder tdt --workers $w --threads-per-worker 4 --batch-size $b --episode-limit 4
        done
    done
    prun gpu-parakeet-pilot-tdt-w8-b16 --decoder tdt --workers 8 --threads-per-worker 2 --batch-size 16 --episode-limit 4 ;;
parakeet-full)
    W=${W:-2} B=${B:-16}
    prun "gpu-parakeet-tdt-full-w$W-b$B" --decoder tdt --workers $W --threads-per-worker 4 --batch-size $B
    prun "gpu-parakeet-ctc-full-w$W-b$B" --decoder ctc --workers $W --threads-per-worker 4 --batch-size $B ;;
esac
