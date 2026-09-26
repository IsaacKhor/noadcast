#!/usr/bin/env bash
# Experiment A: cost of faster-whisper word timestamps on the TAL corpus.
# Four sequential full-corpus runs in OFF, ON, ON, OFF order (the crossover
# design from TINY_CROSSOVER.md), so slow drift in host load cancels out.
# Usage: scripts/run_word_timestamps_crossover.sh [DATE_TAG]
set -euo pipefail
cd "$(dirname "$0")/.."
tag="${1:-$(date -u +%Y%m%d)}"
export TMPDIR="$PWD/.cache/tmp" HF_HOME="$PWD/.cache/huggingface" XDG_CACHE_HOME="$PWD/.cache"
export OPENBLAS_NUM_THREADS=1
mkdir -p "$TMPDIR"
for i in 1 2 3 4; do
  case $i in
    1|4) flag=""; state=off ;;
    2|3) flag="--word-timestamps"; state=on ;;
  esac
  run_id="wt-crossover-$i-$state-$tag"
  log="benchmarks/tal/$run_id.log"
  echo "[$(date -u +%FT%TZ)] start $run_id loadavg=$(cut -d' ' -f1-3 /proc/loadavg)" | tee -a "$log"
  .venv/bin/python -u scripts/benchmark_whisper_parallel.py \
    --workers 10 --threads-per-worker 2 --input-format pcm \
    --model-path benchmarks/tal/models/tiny.en \
    --model-id Systran/faster-whisper-tiny.en \
    --memory-interval-ms 250 $flag --run-id "$run_id" >>"$log" 2>&1
  echo "[$(date -u +%FT%TZ)] end $run_id loadavg=$(cut -d' ' -f1-3 /proc/loadavg)" | tee -a "$log"
done
echo "crossover complete: $tag"
