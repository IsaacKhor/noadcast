#!/usr/bin/env python3
"""Verify and compare matched tiny.en and multilingual tiny TAL runs."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import statistics

from report_tal_comparison import (
    ROOT,
    TAL_ROOT,
    atomic_json,
    atomic_text,
    input_kind,
    load_json,
    require,
    transcript_paths,
    validate_run,
    validate_shared_inputs,
)


OUTPUT_MD = TAL_ROOT / "TINY_VARIANTS.md"
OUTPUT_JSON = TAL_ROOT / "tiny-variants-comparison.json"
OUTPUT_CSV = TAL_ROOT / "tiny-variants-comparison.csv"


def close(actual: float, expected: float, label: str, *, rel_tol=1e-7, abs_tol=1e-5) -> None:
    require(math.isclose(float(actual), float(expected), rel_tol=rel_tol, abs_tol=abs_tol),
            f"{label}: got {actual!r}, expected {expected!r}")


def identify_variant(model_name: str) -> str:
    lowered = model_name.lower().rstrip("/")
    if lowered.endswith("tiny.en") or "tiny-en" in lowered:
        return "tiny.en"
    if lowered.endswith("tiny") and not lowered.endswith("tiny.en"):
        return "tiny (multilingual)"
    raise RuntimeError(f"cannot identify tiny model variant from {model_name!r}")


def validate_memory(run_dir: Path, results: dict, workers: int) -> tuple[dict, dict]:
    memory = results.get("memory")
    require(isinstance(memory, dict), f"missing sampled memory summary: {run_dir}")
    require(memory.get("interval_ms") == 250, f"memory interval is not 250 ms: {run_dir}")
    require(memory.get("source") == "/proc/PID/smaps_rollup", f"unexpected memory source: {run_dir}")
    require(memory.get("errors") == [], f"memory sampling errors recorded: {run_dir}")
    samples_path = run_dir / "memory-samples.json"
    samples = json.loads(samples_path.read_text())
    require(isinstance(samples, list) and samples, f"empty memory sample stream: {samples_path}")
    require(len(samples) == memory.get("samples"), f"memory sample count mismatch: {run_dir}")

    prior_elapsed = -1.0
    phase_rows = {"startup": [], "suite": []}
    for position, sample in enumerate(samples):
        require(isinstance(sample, dict), f"invalid memory sample {position}: {samples_path}")
        elapsed = float(sample.get("elapsed_seconds", -1))
        require(elapsed >= prior_elapsed, f"memory timestamps are not monotonic: {samples_path}")
        prior_elapsed = elapsed
        phase = sample.get("phase")
        require(phase in phase_rows, f"unexpected memory phase {phase!r}: {samples_path}")
        require(0 <= int(sample.get("workers_observed", -1)) <= workers,
                f"invalid workers_observed in {samples_path}")
        for field in ("rss_mib", "pss_mib", "collection_seconds"):
            require(float(sample.get(field, -1)) >= 0, f"invalid {field} in {samples_path}")
        phase_rows[phase].append(sample)

    def check_group(name: str, rows: list[dict]) -> dict:
        recorded = memory.get(name)
        require(isinstance(recorded, dict), f"missing memory.{name}: {run_dir}")
        require(recorded.get("samples") == len(rows), f"memory.{name}.samples mismatch: {run_dir}")
        require(rows, f"no {name} memory samples: {run_dir}")
        peak_rss = max(float(row["rss_mib"]) for row in rows)
        peak_pss = max(float(row["pss_mib"]) for row in rows)
        close(recorded.get("peak_rss_mib"), peak_rss, f"memory.{name}.peak_rss_mib")
        close(recorded.get("peak_pss_mib"), peak_pss, f"memory.{name}.peak_pss_mib")
        gaps = [float(current["elapsed_seconds"]) - float(previous["elapsed_seconds"])
                for previous, current in zip(rows, rows[1:])]
        collections = [float(row["collection_seconds"]) for row in rows]
        peak_pss_workers = max(int(row["workers_observed"]) for row in rows
                               if float(row["pss_mib"]) == peak_pss)
        require(peak_pss_workers == workers,
                f"memory.{name} PSS peak did not observe all {workers} workers: {run_dir}")
        return {
            "samples": len(rows), "peak_rss_mib": peak_rss, "peak_pss_mib": peak_pss,
            "peak_pss_workers_observed": peak_pss_workers,
            "cadence_seconds": {
                "gap_count": len(gaps),
                "mean": statistics.fmean(gaps) if gaps else None,
                "median": statistics.median(gaps) if gaps else None,
                "max": max(gaps) if gaps else None,
            },
            "collection_seconds": {
                "mean": statistics.fmean(collections),
                "median": statistics.median(collections),
                "max": max(collections),
            },
        }

    all_checked = check_group("all", samples)
    startup_checked = check_group("startup", phase_rows["startup"])
    suite_checked = check_group("suite", phase_rows["suite"])
    require(any(row["workers_observed"] == workers for row in phase_rows["suite"]),
            f"sampler never observed all workers during the suite: {run_dir}")
    return {
        "interval_ms": memory["interval_ms"], "source": memory["source"],
        "scope": memory.get("scope"), "samples_path": str(samples_path.relative_to(ROOT)),
        "all": all_checked, "startup": startup_checked, "suite": suite_checked,
    }, {
        "sample_count_verified": True, "monotonic_timestamps_verified": True,
        "phase_counts_verified": True, "sampled_peaks_verified": True,
        "workers_observed_bounds_verified": True, "errors": [],
        "all_workers_observed_during_suite": True,
    }


def validate_transcript_coverage(results_path: Path, results: dict) -> list[dict]:
    checks = []
    for episode in results["episodes"]:
        transcript_json, _ = transcript_paths(results_path, episode)
        transcript = load_json(transcript_json)
        segments = transcript.get("segments")
        require(isinstance(segments, list) and segments, f"missing Whisper segments: {transcript_json}")
        require(len(segments) == episode.get("segment_count"), f"segment count mismatch: {transcript_json}")
        duration = float(episode["benchmark_audio_seconds"])
        previous_start = -1.0
        for segment in segments:
            start, end = float(segment["start"]), float(segment["end"])
            require(0 <= start <= end <= duration + 1, f"segment timestamp outside audio: {transcript_json}")
            require(start >= previous_start, f"segment timestamps are not ordered: {transcript_json}")
            previous_start = start
        close(float(episode["last_segment_end"]), float(segments[-1]["end"]), "last segment end")
        require(segments[-1]["end"] >= duration - 120, f"transcript tail requires inspection: {transcript_json}")
        require(transcript.get("episode", {}).get("sha256") == episode["sha256"],
                f"transcript source SHA mismatch: {transcript_json}")
        checks.append({
            "index": episode["index"], "segment_count": len(segments),
            "last_segment_end": segments[-1]["end"], "audio_seconds": duration,
            "tail_seconds": duration - segments[-1]["end"], "coverage_verified": True,
        })
    return checks


def normalize_run(run_dir: Path, manifest: dict, pcm_manifest: dict, input_checks: dict) -> tuple[dict, dict]:
    results_path = run_dir / "results.json"
    results = load_json(results_path)
    normalized, base_verification = validate_run(
        results_path, manifest, pcm_manifest, input_checks, baseline=False,
    )
    config = results["config"]
    require(config.get("engine") == "faster-whisper", f"unexpected engine: {run_dir}")
    require(config.get("language") == "en", f"language must be forced to en: {run_dir}")
    require(input_kind(results) == "pcm", f"matched runs must use PCM: {run_dir}")
    variant = identify_variant(normalized["model"])
    memory, memory_verification = validate_memory(run_dir, results, normalized["workers"])
    coverage = validate_transcript_coverage(results_path, results)
    model_path = Path(results["model"]["path"])
    if model_path.is_dir():
        model_path = model_path / "model.bin"
    model_bytes = int(results["model"].get("model_bin_bytes", model_path.stat().st_size))
    worker_peak_rss = {
        str(worker): max(float(episode["peak_process_rss_mib"])
                         for episode in results["episodes"] if episode["worker_id"] == worker)
        for worker in sorted({episode["worker_id"] for episode in results["episodes"]})
    }
    episode_peak_rss = {str(episode["index"]): float(episode["peak_process_rss_mib"])
                        for episode in results["episodes"]}
    run = {
        **normalized,
        "variant": variant, "model_bytes": model_bytes, "memory": memory,
        "sum_episode_cpu_seconds": sum(float(episode["cpu_seconds"]) for episode in results["episodes"]),
        "worker_cumulative_peak_rss_mib": worker_peak_rss,
        "episode_peak_rss_mib": episode_peak_rss,
        "median_episode_peak_rss_mib": statistics.median(episode_peak_rss.values()),
        "load_start": results["system"].get("load_start"),
        "load_end": results["summary"].get("load_end"),
        "matched_config": {
            "engine": config.get("engine"), "device": config.get("device"),
            "compute_type": config.get("compute_type"), "workers": config.get("workers"),
            "cpu_threads": config.get("cpu_threads"), "physical_cores": config.get("physical_cores"),
            "pin_workers": config.get("pin_workers"), "input_format": config.get("input_format"),
            "batch_size": config.get("batch_size"), "beam_size": config.get("beam_size"),
            "language": config.get("language"), "vad_filter": config.get("vad_filter"),
            "condition_on_previous_text": config.get("condition_on_previous_text"),
            "word_timestamps": config.get("word_timestamps"),
            "warmup_audio_seconds": config.get("warmup_audio_seconds"),
            "sample_seconds": config.get("sample_seconds"),
            "memory_interval_ms": config.get("memory_interval_ms"),
            "decoded_input_manifest_sha256": results["input"].get("decoded_input_manifest_sha256"),
            "platform": results["system"].get("platform"),
            "parent_affinity": results["system"].get("parent_affinity"),
            "versions": results["system"].get("versions"),
        },
    }
    verification = {
        **base_verification, "variant": variant, "forced_language_en_verified": True,
        "memory": memory_verification, "transcript_coverage": coverage,
        "model_bytes_verified": model_path.stat().st_size == model_bytes,
    }
    return run, verification


def verify_matched_pair(runs: list[dict]) -> dict:
    require(len(runs) == 2, "exactly two runs are required")
    by_variant = {run["variant"]: run for run in runs}
    require(set(by_variant) == {"tiny.en", "tiny (multilingual)"},
            "provide exactly one tiny.en and one multilingual tiny run")
    english, multilingual = by_variant["tiny.en"], by_variant["tiny (multilingual)"]
    require(english["model_sha256"] != multilingual["model_sha256"],
            "tiny.en and multilingual tiny unexpectedly have the same model hash")
    for key, english_value in english["matched_config"].items():
        require(english_value is not None, f"tiny.en lacks matched configuration field {key}")
        require(english_value == multilingual["matched_config"].get(key),
                f"runs are not matched on {key}: {english_value!r} != {multilingual['matched_config'].get(key)!r}")
    close(english["audio_seconds"], multilingual["audio_seconds"], "matched corpus audio seconds")
    return {
        "verified": True, "different_field": "model variant",
        "equal_configuration": english["matched_config"],
        "source_model_hashes": {
            english["variant"]: english["model_sha256"],
            multilingual["variant"]: multilingual["model_sha256"],
        },
    }


def ratio(numerator: float, denominator: float) -> float:
    require(denominator > 0, "ratio denominator must be positive")
    return numerator / denominator


def comparisons(runs: list[dict]) -> dict:
    by_variant = {run["variant"]: run for run in runs}
    english, multilingual = by_variant["tiny.en"], by_variant["tiny (multilingual)"]
    paired_episode_rss_ratios = {
        index: ratio(multilingual["episode_peak_rss_mib"][index], english["episode_peak_rss_mib"][index])
        for index in english["episode_peak_rss_mib"]
    }
    return {
        "multilingual_to_tiny_en_suite_wall_ratio": ratio(
            multilingual["suite_wall_seconds"], english["suite_wall_seconds"]),
        "multilingual_to_tiny_en_cpu_seconds_ratio": ratio(
            multilingual["sum_episode_cpu_seconds"], english["sum_episode_cpu_seconds"]),
        "multilingual_minus_tiny_en_peak_pss_mib": (
            multilingual["memory"]["all"]["peak_pss_mib"] - english["memory"]["all"]["peak_pss_mib"]),
        "tiny_en_to_multilingual_wall_throughput_ratio": ratio(
            english["wall_speed_x"], multilingual["wall_speed_x"]),
        "multilingual_to_tiny_en_model_size_ratio": ratio(
            multilingual["model_bytes"], english["model_bytes"]),
        "multilingual_to_tiny_en_all_peak_pss_ratio": ratio(
            multilingual["memory"]["all"]["peak_pss_mib"], english["memory"]["all"]["peak_pss_mib"]),
        "multilingual_to_tiny_en_suite_peak_pss_ratio": ratio(
            multilingual["memory"]["suite"]["peak_pss_mib"], english["memory"]["suite"]["peak_pss_mib"]),
        "multilingual_to_tiny_en_all_peak_rss_ratio": ratio(
            multilingual["memory"]["all"]["peak_rss_mib"], english["memory"]["all"]["peak_rss_mib"]),
        "multilingual_to_tiny_en_suite_peak_rss_ratio": ratio(
            multilingual["memory"]["suite"]["peak_rss_mib"], english["memory"]["suite"]["peak_rss_mib"]),
        "multilingual_to_tiny_en_median_episode_peak_rss_ratio": ratio(
            multilingual["median_episode_peak_rss_mib"], english["median_episode_peak_rss_mib"]),
        "paired_episode_peak_rss_ratios": paired_episode_rss_ratios,
        "median_paired_episode_peak_rss_ratio": statistics.median(paired_episode_rss_ratios.values()),
    }


def gib(mib: float) -> str:
    return f"{mib / 1024:.2f} GiB"


def render_markdown(runs: list[dict], comparison: dict) -> str:
    by_variant = {run["variant"]: run for run in runs}
    english, multilingual = by_variant["tiny.en"], by_variant["tiny (multilingual)"]
    lines = [
        "# Whisper tiny.en and multilingual tiny benchmark", "",
        "This matched benchmark measures speed and memory on the same ten English-language This American Life episodes. "
        "Both runs force `language=en`; this is not a Chinese-language runtime test and does not evaluate transcription accuracy.", "",
        "Follow-up checks in both model orders are reported separately in [the crossover report](TINY_CROSSOVER.md). "
        "Read those alongside the full-run timings because external CPU contention changed during measurement.", "",
        "| Model | Model binary | Suite wall | Corpus throughput | Summed episode CPU | All-phase peak PSS | Suite peak PSS | All-phase peak RSS | Suite peak RSS | Median episode `ru_maxrss` | Summed worker `ru_maxrss` |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in (english, multilingual):
        lines.append(
            f"| {run['variant']} | {run['model_bytes'] / (1024 ** 2):.2f} MiB | "
            f"{run['suite_wall_seconds']:.2f}s | {run['wall_speed_x']:.2f}× | "
            f"{run['sum_episode_cpu_seconds']:.2f}s | "
            f"{gib(run['memory']['all']['peak_pss_mib'])} | {gib(run['memory']['suite']['peak_pss_mib'])} | "
            f"{gib(run['memory']['all']['peak_rss_mib'])} | {gib(run['memory']['suite']['peak_rss_mib'])} | "
            f"{run['median_episode_peak_rss_mib']:.2f} MiB | "
            f"{gib(run['cumulative_worker_peak_rss_mib'])} |"
        )
    lines += [
        "",
        f"Multilingual tiny took {comparison['multilingual_to_tiny_en_suite_wall_ratio']:.3f}× the tiny.en suite wall time. "
        f"Equivalently, tiny.en delivered {comparison['tiny_en_to_multilingual_wall_throughput_ratio']:.3f}× its corpus throughput. "
        "These ratios describe these runs; no direction was assumed in advance.", "",
        f"Relative to tiny.en, multilingual tiny's observed elapsed-time change was "
        f"{(comparison['multilingual_to_tiny_en_suite_wall_ratio'] - 1) * 100:+.2f}%, its summed episode CPU-time change was "
        f"{(comparison['multilingual_to_tiny_en_cpu_seconds_ratio'] - 1) * 100:+.2f}%, and its sampled peak PSS change was "
        f"{comparison['multilingual_minus_tiny_en_peak_pss_mib']:+.2f} MiB "
        f"({(comparison['multilingual_to_tiny_en_all_peak_pss_ratio'] - 1) * 100:+.2f}%).", "",
        f"The multilingual model binary was {comparison['multilingual_to_tiny_en_model_size_ratio']:.3f}× the tiny.en binary. "
        f"Its sampled all-phase peak PSS ratio was {comparison['multilingual_to_tiny_en_all_peak_pss_ratio']:.3f}× and its "
        f"suite-only peak PSS ratio was {comparison['multilingual_to_tiny_en_suite_peak_pss_ratio']:.3f}×.", "",
        f"The ratio of multilingual to tiny.en median episode `ru_maxrss` was "
        f"{comparison['multilingual_to_tiny_en_median_episode_peak_rss_ratio']:.3f}×. Matching episodes individually and "
        f"then taking the median ratio gave {comparison['median_paired_episode_peak_rss_ratio']:.3f}×.", "",
        "PSS is the better estimate of physical memory because it apportions shared pages. RSS counts shared pages in every "
        "worker and can therefore double-count them. `ru_maxrss` is each worker's historical process high-water mark. "
        "The summed worker column adds those separate peaks and is neither simultaneous nor an estimate of unique physical memory.", "",
        "The all-phase sampled peak includes worker model loading and warmup; the suite peak covers the timed corpus run. "
        "The configured sampling interval was 250 ms and the parent process was excluded. Under load, the actual scheduled "
        "cadence was slower:", "",
        "| Model | Suite samples | Mean gap | Median gap | Maximum gap | Mean sweep | Median sweep | Maximum sweep |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in (english, multilingual):
        cadence = run["memory"]["suite"]["cadence_seconds"]
        sweep = run["memory"]["suite"]["collection_seconds"]
        lines.append(
            f"| {run['variant']} | {run['memory']['suite']['samples']} | {cadence['mean'] * 1000:.1f} ms | "
            f"{cadence['median'] * 1000:.1f} ms | {cadence['max'] * 1000:.1f} ms | "
            f"{sweep['mean'] * 1000:.1f} ms | {sweep['median'] * 1000:.1f} ms | {sweep['max'] * 1000:.1f} ms |"
        )
    lines += [
        "", "Each all-phase and suite peak PSS sample observed all ten workers. The raw cadence and sweep statistics "
        "are retained in `tiny-variants-comparison.json`.", "",
        f"The host was under substantial unrelated CPU contention during these runs. Recorded load averages were "
        f"{english['load_start']} at tiny.en start and {english['load_end']} at its end, and "
        f"{multilingual['load_start']} at multilingual tiny start and {multilingual['load_end']} at its end. "
        "The runs were sequential and matched, but wall time includes scheduling effects from that external workload and "
        "must not be compared directly with earlier idle-host model timings or treated as an isolated model-only slowdown. "
        "Summed episode CPU time is included as a "
        "worker-work measure; it is not full-machine wall time. CPU seconds can also change with cache and memory "
        "contention and clock frequency, so they do not fully remove the external-load confound.", "",
        "Memory instrumentation was identical in both runs. Reading worker `smaps_rollup` records also consumed some CPU "
        "during sampling, so the reported wall times describe the instrumented benchmark.", "",
        "Both runs used faster-whisper on CPU with int8, 10 workers × 2 threads, beam 5, batch 8, VAD enabled, "
        "English forced, no previous-text conditioning, no word timestamps, and the same predecoded PCM inputs. "
        "Runs were sequential and each configuration was measured once, so no confidence interval is available.", "",
        "All ten full decoded durations, source and PCM hashes, model hashes and sizes, transcript artifacts and tail coverage, "
        "summary arithmetic, and memory sample peaks were verified. These checks do not establish recognition accuracy.", "",
    ]
    return "\n".join(lines)


def write_csv(runs: list[dict]) -> None:
    columns = [
        "variant", "run", "model", "model_sha256", "model_bytes", "audio_seconds",
        "suite_wall_seconds", "corpus_wall_speed_x", "all_peak_pss_mib", "suite_peak_pss_mib",
        "all_peak_rss_mib", "suite_peak_rss_mib", "cumulative_worker_peak_rss_mib",
        "median_episode_peak_rss_mib", "sum_episode_cpu_seconds", "load_start", "load_end", "memory_samples",
        "suite_cadence_mean_ms", "suite_cadence_median_ms", "suite_cadence_max_ms",
        "suite_collection_mean_ms", "suite_collection_median_ms", "suite_collection_max_ms",
        "workers", "threads_per_worker", "language",
    ]
    temporary = OUTPUT_CSV.with_name(OUTPUT_CSV.name + ".part")
    with temporary.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for run in sorted(runs, key=lambda item: item["variant"], reverse=True):
            writer.writerow({
                "variant": run["variant"], "run": run["run"], "model": run["model"],
                "model_sha256": run["model_sha256"], "model_bytes": run["model_bytes"],
                "audio_seconds": run["audio_seconds"], "suite_wall_seconds": run["suite_wall_seconds"],
                "corpus_wall_speed_x": run["wall_speed_x"],
                "all_peak_pss_mib": run["memory"]["all"]["peak_pss_mib"],
                "suite_peak_pss_mib": run["memory"]["suite"]["peak_pss_mib"],
                "all_peak_rss_mib": run["memory"]["all"]["peak_rss_mib"],
                "suite_peak_rss_mib": run["memory"]["suite"]["peak_rss_mib"],
                "cumulative_worker_peak_rss_mib": run["cumulative_worker_peak_rss_mib"],
                "median_episode_peak_rss_mib": run["median_episode_peak_rss_mib"],
                "sum_episode_cpu_seconds": run["sum_episode_cpu_seconds"],
                "load_start": json.dumps(run["load_start"]), "load_end": json.dumps(run["load_end"]),
                "memory_samples": run["memory"]["all"]["samples"],
                "suite_cadence_mean_ms": run["memory"]["suite"]["cadence_seconds"]["mean"] * 1000,
                "suite_cadence_median_ms": run["memory"]["suite"]["cadence_seconds"]["median"] * 1000,
                "suite_cadence_max_ms": run["memory"]["suite"]["cadence_seconds"]["max"] * 1000,
                "suite_collection_mean_ms": run["memory"]["suite"]["collection_seconds"]["mean"] * 1000,
                "suite_collection_median_ms": run["memory"]["suite"]["collection_seconds"]["median"] * 1000,
                "suite_collection_max_ms": run["memory"]["suite"]["collection_seconds"]["max"] * 1000,
                "workers": run["workers"], "threads_per_worker": run["threads_per_worker"],
                "language": run["matched_config"]["language"],
            })
    temporary.replace(OUTPUT_CSV)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify and compare matched multilingual tiny and tiny.en full TAL run directories.")
    parser.add_argument("run_dirs", nargs=2, type=Path, metavar="RUN_DIR")
    args = parser.parse_args()
    manifest, pcm_manifest, input_checks = validate_shared_inputs()
    runs, verifications = [], []
    for supplied in args.run_dirs:
        run_dir = supplied.resolve()
        require(run_dir.is_dir(), f"run directory does not exist: {run_dir}")
        run, verification = normalize_run(run_dir, manifest, pcm_manifest, input_checks)
        runs.append(run)
        verifications.append(verification)
    pair_verification = verify_matched_pair(runs)
    comparison = comparisons(runs)
    atomic_text(OUTPUT_MD, render_markdown(runs, comparison))
    write_csv(runs)
    atomic_json(OUTPUT_JSON, {
        "verified": True, "scope": "English TAL corpus with language=en forced",
        "accuracy_evaluated": False, "chinese_runtime_evaluated": False,
        "shared_inputs": input_checks, "matched_pair": pair_verification,
        "runs": runs, "run_verification": verifications, "comparison": comparison,
        "outputs": [str(OUTPUT_MD.relative_to(ROOT)), str(OUTPUT_CSV.relative_to(ROOT))],
    })
    print(f"Verified matched tiny variants; wrote {OUTPUT_MD}, {OUTPUT_JSON}, and {OUTPUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
