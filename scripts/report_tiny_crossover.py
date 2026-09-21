#!/usr/bin/env python3
"""Verify and report the four-run tiny.en/multilingual tiny crossover."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from report_tal_comparison import ROOT, TAL_ROOT, atomic_json, atomic_text, load_json, require, transcript_paths
from report_tiny_variants import identify_variant, validate_memory


OUTPUT_MD = TAL_ROOT / "TINY_CROSSOVER.md"
OUTPUT_JSON = TAL_ROOT / "tiny-crossover-comparison.json"
DEFAULT_RUN_IDS = (
    "tiny-crossover-1-tiny-en-20260921",
    "tiny-crossover-2-tiny-20260921",
    "tiny-crossover-3-tiny-20260921",
    "tiny-crossover-4-tiny-en-20260921",
)
FULL_RUN_IDS = {
    "tiny.en": "tiny-en-memory-20260921",
    "tiny (multilingual)": "tiny-multilingual-memory-20260921",
}
MATCHED_CONFIG_KEYS = (
    "engine", "device", "compute_type", "workers", "cpu_threads", "physical_cores",
    "pin_workers", "input_format", "batch_size", "beam_size", "language", "vad_filter",
    "condition_on_previous_text", "word_timestamps", "warmup_audio_seconds", "memory_interval_ms",
)


def close(actual: float, expected: float, label: str, *, rel_tol=1e-7, abs_tol=1e-5) -> None:
    require(math.isclose(float(actual), float(expected), rel_tol=rel_tol, abs_tol=abs_tol),
            f"{label}: got {actual!r}, expected {expected!r}")


def model_hash(results: dict) -> str:
    value = results.get("model", {}).get("model_bin_sha256")
    require(isinstance(value, str) and len(value) == 64, "missing model binary SHA-256")
    return value


def validate_transcripts(results_path: Path, results: dict) -> list[dict]:
    checks = []
    for episode in results["episodes"]:
        json_path, text_path = transcript_paths(results_path, episode)
        transcript = load_json(json_path)
        segments = transcript.get("segments")
        require(isinstance(segments, list) and segments, f"empty transcript segments: {json_path}")
        require(len(segments) == episode.get("segment_count"), f"segment count mismatch: {json_path}")
        require(text_path.is_file() and text_path.read_text().strip(), f"empty transcript text: {text_path}")
        previous_start = -1.0
        for segment in segments:
            start, end = float(segment["start"]), float(segment["end"])
            require(0 <= start <= end <= 301, f"segment outside 300-second excerpt: {json_path}")
            require(start >= previous_start, f"unordered transcript segments: {json_path}")
            previous_start = start
        close(float(episode["last_segment_end"]), float(segments[-1]["end"]), "last segment end")
        require(transcript.get("episode", {}).get("sha256") == episode["sha256"],
                f"transcript source hash mismatch: {json_path}")
        checks.append({
            "index": episode["index"], "json": str(json_path.relative_to(ROOT)),
            "text": str(text_path.relative_to(ROOT)), "segment_count": len(segments),
            "last_segment_end": segments[-1]["end"], "nonempty": True,
        })
    return checks


def validate_run(run_dir: Path, position: int, manifest: dict, pcm_manifest: dict,
                 full_results: dict[str, dict]) -> tuple[dict, dict]:
    results_path = run_dir / "results.json"
    results = load_json(results_path)
    require(results.get("status") == "complete", f"run is incomplete: {run_dir}")
    config, summary = results.get("config", {}), results.get("summary", {})
    require(config.get("sample_seconds") == 300, f"run is not a 300-second excerpt: {run_dir}")
    require(config.get("language") == "en", f"language is not forced to en: {run_dir}")
    require(len(results.get("episodes", [])) == 10, f"run does not contain ten episodes: {run_dir}")
    require([episode.get("index") for episode in results["episodes"]] == list(range(1, 11)),
            f"episode set/order mismatch: {run_dir}")
    variant = identify_variant(str(config.get("model")))
    expected_variant = "tiny.en" if position in (1, 4) else "tiny (multilingual)"
    require(variant == expected_variant, f"run {position} has {variant}, expected {expected_variant}")

    source_by_index = {row["index"]: row for row in manifest["episodes"]}
    pcm_by_index = {row["index"]: row for row in pcm_manifest["episodes"]}
    for episode in results["episodes"]:
        source, pcm = source_by_index[episode["index"]], pcm_by_index[episode["index"]]
        require(episode.get("sha256") == source["sha256"], f"source hash mismatch: episode {episode['index']}")
        require(episode.get("decoded_input_sha256") == pcm["pcm_sha256"],
                f"PCM hash mismatch: episode {episode['index']}")
        close(episode["benchmark_audio_seconds"], 300, f"episode {episode['index']} excerpt duration")
        require(abs(float(episode["full_decoded_audio_seconds"]) - float(pcm["pcm_duration_seconds"])) < 2,
                f"episode {episode['index']} was not decoded in full")

    audio_seconds = sum(float(row["benchmark_audio_seconds"]) for row in results["episodes"])
    transcribe_seconds = sum(float(row["transcribe_seconds"]) for row in results["episodes"])
    cpu_seconds = sum(float(row["cpu_seconds"]) for row in results["episodes"])
    close(audio_seconds, 3000, "crossover corpus excerpt seconds")
    close(summary.get("benchmark_audio_seconds"), audio_seconds, "summary audio seconds")
    close(summary.get("sum_worker_transcribe_seconds"), transcribe_seconds, "summary transcribe seconds")
    suite_wall = float(summary["suite_wall_seconds"])
    close(summary.get("corpus_wall_speed_x"), audio_seconds / suite_wall, "wall throughput")
    memory, memory_check = validate_memory(run_dir, results, int(config["workers"]))
    transcript_check = validate_transcripts(results_path, results)

    full = full_results[variant]
    require(model_hash(results) == model_hash(full), f"model hash differs from full run: {run_dir}")
    for key in MATCHED_CONFIG_KEYS:
        require(config.get(key) == full["config"].get(key),
                f"{key} differs from matching full run: {run_dir}")
    require(results["input"].get("manifest_sha256") == full["input"].get("manifest_sha256"),
            f"source manifest hash differs from full run: {run_dir}")
    require(results["input"].get("decoded_input_manifest_sha256") ==
            full["input"].get("decoded_input_manifest_sha256"),
            f"PCM manifest hash differs from full run: {run_dir}")

    run = {
        "position": position, "run_id": results["run_id"], "variant": variant,
        "results": str(results_path.relative_to(ROOT)), "model_sha256": model_hash(results),
        "suite_wall_seconds": suite_wall, "corpus_wall_speed_x": audio_seconds / suite_wall,
        "sum_episode_cpu_seconds": cpu_seconds,
        "all_peak_pss_mib": memory["all"]["peak_pss_mib"],
        "suite_peak_pss_mib": memory["suite"]["peak_pss_mib"],
        "all_peak_rss_mib": memory["all"]["peak_rss_mib"],
        "suite_peak_rss_mib": memory["suite"]["peak_rss_mib"],
        "load_start": results["system"].get("load_start"), "load_end": summary.get("load_end"),
        "decode_seconds": float(summary.get("sum_worker_decode_seconds", 0)),
        "memory": memory,
    }
    verification = {
        "run_id": results["run_id"], "complete": True, "ten_episodes_verified": True,
        "source_and_pcm_hashes_verified": True, "full_decode_and_300_second_excerpt_verified": True,
        "summary_arithmetic_verified": True, "model_hash_matches_full_run": True,
        "settings_match_full_run": True, "memory": memory_check, "transcripts": transcript_check,
    }
    return run, verification


def ratio(numerator: float, denominator: float) -> float:
    require(denominator > 0, "ratio denominator must be positive")
    return numerator / denominator


def make_comparison(runs: list[dict]) -> dict:
    by_position = {run["position"]: run for run in runs}
    pair_2_over_1 = {
        "wall_ratio": ratio(by_position[2]["suite_wall_seconds"], by_position[1]["suite_wall_seconds"]),
        "cpu_ratio": ratio(by_position[2]["sum_episode_cpu_seconds"], by_position[1]["sum_episode_cpu_seconds"]),
        "suite_peak_pss_ratio": ratio(by_position[2]["suite_peak_pss_mib"], by_position[1]["suite_peak_pss_mib"]),
    }
    pair_3_over_4 = {
        "wall_ratio": ratio(by_position[3]["suite_wall_seconds"], by_position[4]["suite_wall_seconds"]),
        "cpu_ratio": ratio(by_position[3]["sum_episode_cpu_seconds"], by_position[4]["sum_episode_cpu_seconds"]),
        "suite_peak_pss_ratio": ratio(by_position[3]["suite_peak_pss_mib"], by_position[4]["suite_peak_pss_mib"]),
    }
    multilingual_wall = by_position[2]["suite_wall_seconds"] + by_position[3]["suite_wall_seconds"]
    english_wall = by_position[1]["suite_wall_seconds"] + by_position[4]["suite_wall_seconds"]
    return {
        "pair_2_multilingual_over_1_tiny_en": pair_2_over_1,
        "pair_3_multilingual_over_4_tiny_en": pair_3_over_4,
        "pooled_multilingual_to_tiny_en_wall_ratio": ratio(multilingual_wall, english_wall),
        "pooled_wall_seconds": {"tiny (multilingual)": multilingual_wall, "tiny.en": english_wall},
    }


def render_markdown(runs: list[dict], comparison: dict) -> str:
    lines = [
        "# Whisper tiny crossover benchmark", "",
        "This four-run crossover checks the relative runtime of tiny.en and multilingual tiny under changing host contention. "
        "The order was tiny.en, multilingual tiny, multilingual tiny, tiny.en; all runs were sequential.", "",
        "| Order | Model | Suite wall | Throughput | Summed episode CPU | Suite peak PSS | All-phase peak PSS | Decode | Load start → end |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for run in sorted(runs, key=lambda item: item["position"]):
        lines.append(
            f"| {run['position']} | {run['variant']} | {run['suite_wall_seconds']:.2f}s | "
            f"{run['corpus_wall_speed_x']:.2f}× | {run['sum_episode_cpu_seconds']:.2f}s | "
            f"{run['suite_peak_pss_mib'] / 1024:.2f} GiB | {run['all_peak_pss_mib'] / 1024:.2f} GiB | "
            f"{run['decode_seconds']:.2f}s | {run['load_start']} → {run['load_end']} |"
        )
    pair_one = comparison["pair_2_multilingual_over_1_tiny_en"]
    pair_two = comparison["pair_3_multilingual_over_4_tiny_en"]
    lines += [
        "",
        f"Multilingual/tiny.en wall ratio was {pair_one['wall_ratio']:.3f}× for run 2 over run 1 and "
        f"{pair_two['wall_ratio']:.3f}× for run 3 over run 4. Pooling wall seconds within each model gave "
        f"{comparison['pooled_multilingual_to_tiny_en_wall_ratio']:.3f}×.", "",
        f"The corresponding summed episode CPU ratios were {pair_one['cpu_ratio']:.3f}× and "
        f"{pair_two['cpu_ratio']:.3f}×. Suite peak PSS ratios were {pair_one['suite_peak_pss_ratio']:.3f}× and "
        f"{pair_two['suite_peak_pss_ratio']:.3f}×.", "",
        "Each run transcribed only the first 300 seconds of every episode, for 3,000 seconds of inference input. "
        "Each full WAV was nevertheless decoded before slicing, and suite wall time includes that full-file decode. "
        "These excerpt timings are separate from the ten-full-episode benchmark.", "",
        "The short-run sampled PSS values confirm instrumentation and provide crossover context. They do not replace the "
        "full-run RAM measurements because shorter inference can change allocation peaks and worker overlap.", "",
        "The host carried substantial unrelated CPU work. Wall time includes external scheduling contention, so neither an "
        "individual pair nor the pooled ratio is an isolated hardware-idle model effect. Summed episode CPU time is worker "
        "process time, not full-machine wall time.", "",
        "Both variants forced `language=en` on the same English TAL excerpts with CPU int8, 10 workers × 2 threads, beam 5, "
        "batch 8, VAD enabled, and the same memory sampler. This benchmark does not test Chinese runtime or recognition accuracy.", "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify and report the four-run tiny variant crossover.")
    parser.add_argument("run_dirs", nargs="*", type=Path, metavar="RUN_DIR",
                        help="four crossover run directories in execution order; defaults to the dated run IDs")
    args = parser.parse_args()
    require(len(args.run_dirs) in (0, 4), "provide either zero or exactly four run directories")
    run_dirs = ([TAL_ROOT / "runs" / run_id for run_id in DEFAULT_RUN_IDS]
                if not args.run_dirs else [path.resolve() for path in args.run_dirs])
    manifest = load_json(TAL_ROOT / "manifest.json")
    pcm_manifest = load_json(TAL_ROOT / "pcm" / "manifest.json")
    full_results = {
        variant: load_json(TAL_ROOT / "runs" / run_id / "results.json")
        for variant, run_id in FULL_RUN_IDS.items()
    }
    runs, verifications = [], []
    for position, run_dir in enumerate(run_dirs, 1):
        run, verification = validate_run(run_dir, position, manifest, pcm_manifest, full_results)
        runs.append(run)
        verifications.append(verification)
    reference_config = load_json(run_dirs[0] / "results.json")["config"]
    for run_dir in run_dirs[1:]:
        candidate = load_json(run_dir / "results.json")["config"]
        for key in MATCHED_CONFIG_KEYS + ("sample_seconds",):
            require(candidate.get(key) == reference_config.get(key), f"crossover settings differ on {key}")
    comparison = make_comparison(runs)
    atomic_text(OUTPUT_MD, render_markdown(runs, comparison))
    atomic_json(OUTPUT_JSON, {
        "verified": True, "scope": "300-second prefixes of ten English TAL episodes",
        "run_order": list(DEFAULT_RUN_IDS) if not args.run_dirs else [path.name for path in run_dirs],
        "full_run_references": FULL_RUN_IDS, "runs": runs, "run_verification": verifications,
        "comparison": comparison, "accuracy_evaluated": False, "chinese_runtime_evaluated": False,
        "short_run_memory_replaces_full_run_memory": False,
    })
    print(f"Verified four crossover runs; wrote {OUTPUT_MD} and {OUTPUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
