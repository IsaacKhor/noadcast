#!/usr/bin/env python3
"""Verify and report the four-run faster-whisper word-timestamp crossover (Experiment A)."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import statistics
from pathlib import Path

from report_tal_comparison import (
    ROOT,
    TAL_ROOT,
    atomic_json,
    atomic_text,
    close,
    input_kind,
    load_json,
    require,
    sha256_file,
    transcript_paths,
    validate_run,
    validate_shared_inputs,
)
from report_tiny_variants import validate_memory


OUTPUT_MD = TAL_ROOT / "WORD_TIMESTAMPS.md"
OUTPUT_JSON = TAL_ROOT / "word-timestamps-comparison.json"
OUTPUT_CSV = TAL_ROOT / "word-timestamps-comparison.csv"
DEFAULT_RUN_IDS = (
    "wt-crossover-1-off-20260922",
    "wt-crossover-2-on-20260922",
    "wt-crossover-3-on-20260922",
    "wt-crossover-4-off-20260922",
)
# Crossover position -> expected word_timestamps: OFF, ON, ON, OFF.
EXPECTED_WORD_TIMESTAMPS = {1: False, 2: True, 3: True, 4: False}
DIFFERING_CONFIG_KEY = "word_timestamps"
# report_tiny_crossover.MATCHED_CONFIG_KEYS without the key under test, plus model identity and scope.
MATCHED_CONFIG_KEYS = (
    "engine", "model", "model_path", "device", "compute_type", "workers", "cpu_threads", "physical_cores",
    "pin_workers", "input_format", "batch_size", "beam_size", "language", "vad_filter",
    "condition_on_previous_text", "warmup_audio_seconds", "sample_seconds", "memory_interval_ms",
)
THRESHOLDS = {
    "pooled_wall_ratio": 1.20, "pairwise_wall_ratio": 1.25, "pooled_cpu_ratio": 1.25,
    "peak_pss_increase_mib": 1024.0,
}
LOG_EVENT = re.compile(r"^\[(?P<at>[0-9T:-]+Z)\] (?P<event>start|end) (?P<run>\S+) "
                       r"loadavg=(?P<one>[0-9.]+) (?P<five>[0-9.]+) (?P<fifteen>[0-9.]+)$")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ratio(numerator: float, denominator: float) -> float:
    require(denominator > 0, "ratio denominator must be positive")
    return numerator / denominator


def validate_transcripts(results_path: Path, results: dict, word_timestamps: bool) -> tuple[list[dict], dict]:
    """Check every transcript artifact; return JSON-able checks and per-episode segment bounds."""
    checks, bounds = [], {}
    for episode in results["episodes"]:
        json_path, text_path = transcript_paths(results_path, episode)
        transcript = load_json(json_path)
        segments = transcript.get("segments")
        require(isinstance(segments, list) and segments, f"empty transcript segments: {json_path}")
        require(len(segments) == episode["segment_count"], f"segment count mismatch: {json_path}")
        require(transcript.get("episode", {}).get("sha256") == episode["sha256"],
                f"transcript source hash mismatch: {json_path}")
        require(transcript.get("config", {}).get(DIFFERING_CONFIG_KEY) is word_timestamps,
                f"transcript records the wrong word_timestamps setting: {json_path}")
        duration = float(episode["benchmark_audio_seconds"])
        words, previous_start, backwards = [], -1.0, 0
        for segment in segments:
            start, end = float(segment["start"]), float(segment["end"])
            require(0 <= start <= end <= duration + 1, f"segment timestamp outside audio: {json_path}")
            require(start >= previous_start, f"segment timestamps are not ordered: {json_path}")
            previous_start = start
            if not word_timestamps:
                require(segment.get("words") is None, f"word-timestamp-off transcript carries words: {json_path}")
                continue
            segment_words = segment.get("words")
            require(isinstance(segment_words, list) and segment_words, f"segment without words: {json_path}")
            require(start == segment_words[0]["start"] and end == segment_words[-1]["end"],
                    f"segment bounds are not word-derived: {json_path}")
            require("".join(word["word"] for word in segment_words) == segment["text"],
                    f"words do not reconstruct segment text: {json_path}")
            for word in segment_words:
                require(0 <= float(word["start"]) <= float(word["end"]) <= duration + 1,
                        f"word timestamp outside audio: {json_path}")
                backwards += bool(words) and word["start"] < words[-1]["start"]
                words.append(word)
        close(float(episode["last_segment_end"]), float(segments[-1]["end"]), "last segment end")
        require(episode.get("word_count") == len(words), f"word count mismatch: {json_path}")
        if word_timestamps:
            close(float(episode["last_word_end"]), float(words[-1]["end"]), "last word end")
        else:
            require(episode.get("last_word_end") is None, f"OFF run records a last word end: {json_path}")
        text = "".join(segment["text"] for segment in segments)
        require(text.strip(), f"empty transcript text: {json_path}")
        require(text_path.read_text() == "\n".join(segment["text"].strip() for segment in segments) + "\n",
                f"text artifact does not match segment text: {text_path}")
        word_rows = [[word["start"], word["end"], word["word"], word["probability"]] for word in words]
        checks.append({
            "index": episode["index"], "json": str(json_path.relative_to(ROOT)),
            "text": str(text_path.relative_to(ROOT)), "segment_count": len(segments),
            "word_count": len(words), "text_sha256": sha256_text(text),
            "text_file_sha256": sha256_file(text_path),
            "words_sha256": sha256_text(json.dumps(word_rows)) if words else None,
            "segment_bounds_sha256": sha256_text(json.dumps([[s["start"], s["end"]] for s in segments])),
            "segment_bounds_word_derived": word_timestamps, "non_monotonic_word_starts": backwards,
            "last_segment_end": segments[-1]["end"], "last_word_end": words[-1]["end"] if words else None,
            "text_artifact_verified": True,
        })
        bounds[episode["index"]] = [(float(s["start"]), float(s["end"])) for s in segments]
    return checks, bounds


def validate_log(run_id: str, results: dict) -> dict:
    """The runner script brackets each run with loadavg lines; the benchmark prints its summary."""
    path = TAL_ROOT / f"{run_id}.log"
    require(path.is_file(), f"missing run log: {path}")
    lines = path.read_text().splitlines()
    events = {}
    for line in lines:
        if match := LOG_EVENT.match(line):
            require(match["run"] == run_id, f"{path} names another run: {match['run']}")
            require(match["event"] not in events, f"duplicate {match['event']} line in {path}")
            events[match["event"]] = {"at": match["at"],
                                      "loadavg": [float(match[key]) for key in ("one", "five", "fifteen")]}
    require(set(events) == {"start", "end"}, f"log lacks start/end loadavg lines: {path}")
    require(events["start"]["at"] <= events["end"]["at"], f"log end precedes start: {path}")
    require("{" in lines and "}" in lines, f"log lacks the printed summary: {path}")
    opening = lines.index("{")
    printed = json.loads("\n".join(lines[opening:lines.index("}", opening) + 1]))
    require(printed == results["summary"], f"logged summary differs from results.json: {path}")
    require(any(line.startswith("Results: ") and line.endswith(f"/runs/{run_id}/results.json") for line in lines),
            f"log does not name this run's results: {path}")
    return {"log": str(path.relative_to(ROOT)), "log_sha256": sha256_file(path), "start": events["start"],
            "end": events["end"], "logged_summary_matches_results": True}


def normalize_run(run_dir: Path, position: int, manifest: dict, pcm_manifest: dict,
                  input_checks: dict) -> tuple[dict, dict, dict]:
    results_path = run_dir / "results.json"
    results = load_json(results_path)
    normalized, base_verification = validate_run(results_path, manifest, pcm_manifest, input_checks)
    config = results["config"]
    word_timestamps = EXPECTED_WORD_TIMESTAMPS[position]
    require(results.get("run_id") == run_dir.name, f"run id does not match its directory: {run_dir}")
    require(config.get("engine") == "faster-whisper", f"unexpected engine: {run_dir}")
    require(config.get("language") == "en", f"language must be forced to en: {run_dir}")
    require(input_kind(results) == "pcm", f"crossover runs must use PCM: {run_dir}")
    require(config.get(DIFFERING_CONFIG_KEY) is word_timestamps,
            f"run {position} has word_timestamps={config.get(DIFFERING_CONFIG_KEY)!r}, expected {word_timestamps}")
    for episode in results["episodes"]:
        close(episode["benchmark_audio_seconds"], episode["full_decoded_audio_seconds"],
              f"episode {episode['index']} full-file inference")
        close(episode["rtf"], episode["transcribe_seconds"] / episode["benchmark_audio_seconds"],
              f"episode {episode['index']} RTF")
        require(episode["effective_transcription_options"].get(DIFFERING_CONFIG_KEY) is word_timestamps,
                f"effective word_timestamps disagrees with config: episode {episode['index']} in {run_dir}")
    memory, memory_verification = validate_memory(run_dir, results, normalized["workers"])
    transcripts, bounds = validate_transcripts(results_path, results, word_timestamps)
    log = validate_log(results["run_id"], results)
    run = {
        "position": position, "run_id": results["run_id"], "word_timestamps": word_timestamps,
        "results": normalized["results"], "started_at": results["started_at"],
        "completed_at": results["completed_at"], "model": normalized["model"],
        "model_sha256": normalized["model_sha256"], "model_revision": normalized["model_revision"],
        "model_bytes": results["model"]["model_bin_bytes"], "audio_seconds": normalized["audio_seconds"],
        "suite_wall_seconds": normalized["suite_wall_seconds"], "corpus_wall_speed_x": normalized["wall_speed_x"],
        "sum_episode_cpu_seconds": sum(float(episode["cpu_seconds"]) for episode in results["episodes"]),
        "sum_worker_transcribe_seconds": normalized["sum_worker_process_seconds"],
        "decode_seconds": normalized["decode_seconds"],
        "median_episode_rtf": statistics.median(float(episode["rtf"]) for episode in results["episodes"]),
        "median_episode_peak_rss_mib": statistics.median(
            float(episode["peak_process_rss_mib"]) for episode in results["episodes"]),
        "load_start": results["system"]["load_start"], "load_end": results["summary"]["load_end"],
        "log_load_start": log["start"]["loadavg"], "log_load_end": log["end"]["loadavg"],
        "memory": memory,
        "episodes": [{
            "index": episode["index"], "title": episode["title"],
            "audio_seconds": float(episode["benchmark_audio_seconds"]), "worker_id": episode["worker_id"],
            "transcribe_seconds": float(episode["transcribe_seconds"]), "cpu_seconds": float(episode["cpu_seconds"]),
            "rtf": float(episode["rtf"]), "segment_count": check["segment_count"],
            "word_count": check["word_count"], "text_sha256": check["text_sha256"],
            "words_sha256": check["words_sha256"], "segment_bounds_sha256": check["segment_bounds_sha256"],
            "last_segment_end": check["last_segment_end"],
            "peak_process_rss_mib": float(episode["peak_process_rss_mib"]),
        } for episode, check in zip(results["episodes"], transcripts)],
        "non_monotonic_word_starts": sum(check["non_monotonic_word_starts"] for check in transcripts),
    }
    verification = {
        **base_verification, "position": position, "word_timestamps": word_timestamps,
        "word_timestamps_setting_verified": True, "full_file_inference_verified": True,
        "episode_rtf_arithmetic_verified": True, "memory": memory_verification,
        "transcript_artifacts": transcripts, "run_log": log,
    }
    return run, verification, {"results": results, "bounds": bounds}


def verify_crossover(runs: list[dict], raw: list[dict]) -> dict:
    """Everything except word_timestamps must match across all four runs."""
    reference = raw[0]["results"]
    for run, candidate in zip(runs[1:], raw[1:]):
        results = candidate["results"]
        for key in MATCHED_CONFIG_KEYS:
            require(results["config"].get(key) == reference["config"].get(key),
                    f"{run['run_id']} differs from run 1 on {key}")
        for section, key in (("model", "model_bin_sha256"), ("model", "revision"), ("model", "model_bin_bytes"),
                             ("input", "manifest_sha256"), ("input", "decoded_input_manifest_sha256"),
                             ("system", "platform"), ("system", "versions"), ("system", "worker_affinities")):
            require(results[section].get(key) == reference[section].get(key),
                    f"{run['run_id']} differs from run 1 on {section}.{key}")
        for episode, first in zip(results["episodes"], reference["episodes"]):
            options = {k: v for k, v in episode["effective_transcription_options"].items() if k != DIFFERING_CONFIG_KEY}
            first_options = {k: v for k, v in first["effective_transcription_options"].items()
                             if k != DIFFERING_CONFIG_KEY}
            require(options == first_options,
                    f"effective transcription options differ beyond word_timestamps: episode {episode['index']}")
            require(episode["effective_vad_options"] == first["effective_vad_options"],
                    f"effective VAD options differ: episode {episode['index']}")
            require(episode["speech_seconds_after_vad"] == first["speech_seconds_after_vad"],
                    f"post-VAD speech duration differs: episode {episode['index']}")
    for earlier, later in zip(runs, runs[1:]):
        require(earlier["completed_at"] <= later["started_at"],
                f"{later['run_id']} started before {earlier['run_id']} completed")
    return {
        "verified": True, "differing_config_key": DIFFERING_CONFIG_KEY,
        "order": [run["word_timestamps"] for run in runs],
        "equal_configuration": {key: reference["config"].get(key) for key in MATCHED_CONFIG_KEYS},
        "effective_options_differ_only_in_word_timestamps": True,
        "effective_vad_options_equal": True, "post_vad_speech_seconds_equal": True,
        "sequential_non_overlapping_runs_verified": True,
        "model_sha256": runs[0]["model_sha256"], "model_revision": runs[0]["model_revision"],
    }


def episode_comparisons(runs: list[dict], raw: list[dict], manifest: dict) -> list[dict]:
    by_position = {run["position"]: run for run in runs}
    bounds = {run["position"]: item["bounds"] for run, item in zip(runs, raw)}
    rows = []
    for offset, source in enumerate(manifest["episodes"]):
        index = source["index"]
        episodes = {position: run["episodes"][offset] for position, run in by_position.items()}
        require(all(episode["index"] == index for episode in episodes.values()), f"episode order mismatch at {index}")
        on_bounds, off_bounds = bounds[2][index], bounds[1][index]
        same_count = len({episode["segment_count"] for episode in episodes.values()}) == 1
        start_shifts = [abs(on[0] - off[0]) for on, off in zip(on_bounds, off_bounds)] if same_count else []
        end_shifts = [abs(on[1] - off[1]) for on, off in zip(on_bounds, off_bounds)] if same_count else []
        rows.append({
            "index": index, "title": source["title"], "audio_seconds": episodes[1]["audio_seconds"],
            "text_sha256": {str(position): episode["text_sha256"] for position, episode in episodes.items()},
            "text_identical": len({episode["text_sha256"] for episode in episodes.values()}) == 1,
            "segment_counts": {str(position): episode["segment_count"] for position, episode in episodes.items()},
            "segment_count_identical": same_count,
            "word_count": episodes[2]["word_count"],
            "on_run_words_identical": episodes[2]["words_sha256"] == episodes[3]["words_sha256"],
            "on_run_segment_bounds_identical": (episodes[2]["segment_bounds_sha256"]
                                                == episodes[3]["segment_bounds_sha256"]),
            "off_run_segment_bounds_identical": (episodes[1]["segment_bounds_sha256"]
                                                 == episodes[4]["segment_bounds_sha256"]),
            "rtf": {str(position): episode["rtf"] for position, episode in episodes.items()},
            "pooled_on_off_rtf_ratio": ratio(episodes[2]["rtf"] + episodes[3]["rtf"],
                                             episodes[1]["rtf"] + episodes[4]["rtf"]),
            "last_segment_end_off": episodes[1]["last_segment_end"],
            "last_segment_end_on": episodes[2]["last_segment_end"],
            "max_abs_start_shift_seconds": max(start_shifts, default=None),
            "max_abs_end_shift_seconds": max(end_shifts, default=None),
            "segments_with_moved_bounds": sum(1 for on, off in zip(on_bounds, off_bounds) if on != off)
            if same_count else None,
        })
    return rows


def make_comparison(runs: list[dict], episodes: list[dict]) -> dict:
    by_position = {run["position"]: run for run in runs}

    def peak_pss(position: int) -> float:
        return by_position[position]["memory"]["suite"]["peak_pss_mib"]

    def pair(on: int, off: int) -> dict:
        return {
            "wall_ratio": ratio(by_position[on]["suite_wall_seconds"], by_position[off]["suite_wall_seconds"]),
            "cpu_ratio": ratio(by_position[on]["sum_episode_cpu_seconds"], by_position[off]["sum_episode_cpu_seconds"]),
            "worker_inference_ratio": ratio(by_position[on]["sum_worker_transcribe_seconds"],
                                            by_position[off]["sum_worker_transcribe_seconds"]),
            "suite_peak_pss_ratio": ratio(peak_pss(on), peak_pss(off)),
            "suite_peak_pss_change_mib": peak_pss(on) - peak_pss(off),
            "median_episode_peak_rss_change_mib": (by_position[on]["median_episode_peak_rss_mib"]
                                                   - by_position[off]["median_episode_peak_rss_mib"]),
        }

    def pooled(field: str) -> float:
        return ratio(by_position[2][field] + by_position[3][field], by_position[1][field] + by_position[4][field])

    pooled_values = {
        "wall_ratio": pooled("suite_wall_seconds"), "cpu_ratio": pooled("sum_episode_cpu_seconds"),
        "worker_inference_ratio": pooled("sum_worker_transcribe_seconds"),
        "suite_peak_pss_ratio": ratio(peak_pss(2) + peak_pss(3), peak_pss(1) + peak_pss(4)),
        "suite_peak_pss_change_mib": (peak_pss(2) + peak_pss(3)) / 2 - (peak_pss(1) + peak_pss(4)) / 2,
        "median_episode_peak_rss_change_mib": (
            (by_position[2]["median_episode_peak_rss_mib"] + by_position[3]["median_episode_peak_rss_mib"]) / 2
            - (by_position[1]["median_episode_peak_rss_mib"] + by_position[4]["median_episode_peak_rss_mib"]) / 2),
        "wall_seconds": {"on": by_position[2]["suite_wall_seconds"] + by_position[3]["suite_wall_seconds"],
                         "off": by_position[1]["suite_wall_seconds"] + by_position[4]["suite_wall_seconds"]},
    }
    pairs = {"run_2_on_over_run_1_off": pair(2, 1), "run_3_on_over_run_4_off": pair(3, 4)}
    pss_increase = max([item["suite_peak_pss_change_mib"] for item in pairs.values()]
                       + [pooled_values["suite_peak_pss_change_mib"]])
    text_mismatches = [row["index"] for row in episodes if not row["text_identical"]]
    count_mismatches = [row["index"] for row in episodes if not row["segment_count_identical"]]
    pairwise_walls = [item["wall_ratio"] for item in pairs.values()]
    criteria = [
        {"criterion": "pooled ON/OFF suite wall ratio", "threshold": f"≤ {THRESHOLDS['pooled_wall_ratio']:.2f}",
         "measured": pooled_values["wall_ratio"],
         "passed": pooled_values["wall_ratio"] <= THRESHOLDS["pooled_wall_ratio"]},
        {"criterion": "each pairwise ON/OFF suite wall ratio",
         "threshold": f"≤ {THRESHOLDS['pairwise_wall_ratio']:.2f}", "measured": pairwise_walls,
         "passed": all(value <= THRESHOLDS["pairwise_wall_ratio"] for value in pairwise_walls)},
        {"criterion": "pooled ON/OFF summed episode CPU ratio", "threshold": f"≤ {THRESHOLDS['pooled_cpu_ratio']:.2f}",
         "measured": pooled_values["cpu_ratio"],
         "passed": pooled_values["cpu_ratio"] <= THRESHOLDS["pooled_cpu_ratio"]},
        {"criterion": "largest aggregate suite peak PSS increase (pairwise or pooled)",
         "threshold": f"≤ {THRESHOLDS['peak_pss_increase_mib']:.0f} MiB", "measured": pss_increase,
         "passed": pss_increase <= THRESHOLDS["peak_pss_increase_mib"]},
        {"criterion": "episodes with identical transcript text SHA-256 in all four runs", "threshold": "10 of 10",
         "measured": len(episodes) - len(text_mismatches), "mismatched_episodes": text_mismatches,
         "passed": not text_mismatches and len(episodes) == 10},
        {"criterion": "episodes with identical segment counts in all four runs", "threshold": "10 of 10",
         "measured": len(episodes) - len(count_mismatches), "mismatched_episodes": count_mismatches,
         "passed": not count_mismatches and len(episodes) == 10},
    ]
    return {
        "pairs": pairs, "pooled_on_over_off": pooled_values, "largest_peak_pss_increase_mib": pss_increase,
        "criteria": criteria, "decision": "ACCEPT" if all(item["passed"] for item in criteria) else "REJECT",
        "total_words_on": sum(row["word_count"] for row in episodes),
    }


def gib(mib: float) -> str:
    return f"{mib / 1024:.2f} GiB"


def load_text(values: list[float]) -> str:
    return " / ".join(f"{value:.2f}" for value in values)


def measured_text(item: dict) -> str:
    value = item["measured"]
    if isinstance(value, list):
        return ", ".join(f"{entry:.3f}×" for entry in value)
    if "MiB" in item["threshold"]:
        return f"{value:+.1f} MiB"
    return f"{value:.3f}×" if isinstance(value, float) else f"{value} of 10"


def render_markdown(runs: list[dict], comparison: dict, episodes: list[dict], crossover: dict) -> str:
    pooled, pairs = comparison["pooled_on_over_off"], comparison["pairs"]
    first, second = pairs["run_2_on_over_run_1_off"], pairs["run_3_on_over_run_4_off"]
    identical = sum(row["text_identical"] for row in episodes)
    decision = comparison["decision"]
    comparable = [row for row in episodes if row["segment_count_identical"]]
    moved = sum(row["segments_with_moved_bounds"] for row in comparable)
    segments = sum(row["segment_counts"]["1"] for row in comparable)
    tail_shifts = [row["last_segment_end_off"] - row["last_segment_end_on"] for row in episodes]
    runs = sorted(runs, key=lambda item: item["position"])
    lines = [
        "# Whisper word-timestamp cost crossover", "",
        f"**{decision}: `word_timestamps=True` took {pooled['wall_ratio']:.3f}× the pooled suite wall time "
        f"({first['wall_ratio']:.3f}× and {second['wall_ratio']:.3f}× pairwise) and {pooled['cpu_ratio']:.3f}× the "
        f"pooled summed worker CPU, changed sampled aggregate peak PSS by {pooled['suite_peak_pss_change_mib']:+.0f} MiB "
        f"pooled (largest pairwise change {max(first['suite_peak_pss_change_mib'], second['suite_peak_pss_change_mib']):+.0f} "
        f"MiB), and left transcript text byte-identical on {identical} of {len(episodes)} episodes.**", "",
        "This four-run crossover measures the cost of faster-whisper word timestamps on the full ten-episode "
        f"TAL corpus ({runs[0]['audio_seconds'] / 3600:.2f} hours). The order was OFF, ON, ON, OFF; the runs were "
        "sequential and did not overlap. Throughput is total corpus audio divided by the measured suite wall clock.", "",
        "| Order | Word timestamps | Suite wall | Throughput | Summed episode CPU | Suite peak PSS | Startup peak PSS | "
        "Suite peak RSS | Startup peak RSS | Median episode RTF | Load start → end |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for run in runs:
        memory = run["memory"]
        lines.append(
            f"| {run['position']} | {'on' if run['word_timestamps'] else 'off'} | {run['suite_wall_seconds']:.2f}s | "
            f"{run['corpus_wall_speed_x']:.2f}× | {run['sum_episode_cpu_seconds']:.2f}s | "
            f"{gib(memory['suite']['peak_pss_mib'])} | {gib(memory['startup']['peak_pss_mib'])} | "
            f"{gib(memory['suite']['peak_rss_mib'])} | {gib(memory['startup']['peak_rss_mib'])} | "
            f"{run['median_episode_rtf']:.4f} | {load_text(run['load_start'])} → {load_text(run['load_end'])} |"
        )
    lines += [
        "", "| Comparison | Suite wall ratio | Summed CPU ratio | Worker inference ratio | Suite peak PSS ratio | "
        "Suite peak PSS change | Median episode `ru_maxrss` change |", "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label, item in (("Run 2 ON / run 1 OFF", first), ("Run 3 ON / run 4 OFF", second),
                        ("Pooled ON / OFF", pooled)):
        lines.append(
            f"| {label} | {item['wall_ratio']:.3f}× | {item['cpu_ratio']:.3f}× | {item['worker_inference_ratio']:.3f}× | "
            f"{item['suite_peak_pss_ratio']:.3f}× | {item['suite_peak_pss_change_mib']:+.1f} MiB | "
            f"{item['median_episode_peak_rss_change_mib']:+.1f} MiB |"
        )
    lines += ["", "| Acceptance criterion | Threshold | Measured | Result |", "|---|---|---:|---|"]
    for item in comparison["criteria"]:
        lines.append(f"| {item['criterion']} | {item['threshold']} | {measured_text(item)} | "
                     f"{'pass' if item['passed'] else 'FAIL'} |")
    lines += [
        "", f"Decision: **{decision}**. The pooled ratio sums the two ON wall clocks and divides by the two OFF wall "
        "clocks; the peak-PSS criterion uses the largest of the two pairwise changes and the pooled change of the mean "
        "peaks, so a single favourable pair cannot hide an increase.", "",
        f"Model identity: tiny.en SHA-256 `{crossover['model_sha256']}`, revision `{crossover['model_revision']}`, "
        "identical in all four runs.", "",
        "## Per-episode", "",
        "Text SHA-256 is computed over the concatenation of every emitted segment `text` field and is shown truncated; "
        "full digests for each run are in `word-timestamps-comparison.json`. Worker RTF is each episode's inference "
        "seconds over its audio seconds. Episodes run concurrently, so these latencies must not be added.", "",
        "| Episode | Segments | Words (ON) | Text SHA-256, all runs | Worker RTF 1 / 2 / 3 / 4 | Pooled ON/OFF RTF | "
        "Last segment end OFF → ON |", "|---|---:|---:|---|---:|---:|---:|",
    ]
    for row in episodes:
        digest = row["text_sha256"]["1"][:12] + ("" if row["text_identical"] else " (DIFFERS)")
        counts = sorted(set(row["segment_counts"].values()))
        lines.append(
            f"| {row['index']:02d} · {row['title']} | {' / '.join(str(count) for count in counts)} | "
            f"{row['word_count']:,} | `{digest}` | "
            + " / ".join(f"{row['rtf'][str(position)]:.4f}" for position in range(1, 5))
            + f" | {row['pooled_on_off_rtf_ratio']:.3f}× | {row['last_segment_end_off']:.2f}s → "
            f"{row['last_segment_end_on']:.2f}s |"
        )
    lines += [
        "", "## Methodology", "",
        "Each run transcribed every full episode from the same predecoded 16 kHz mono PCM with faster-whisper tiny.en, "
        "CPU int8, ten persistent workers × two threads under OS scheduling, batch 8, beam 5, VAD enabled, English "
        "forced, no previous-text conditioning, and 250 ms worker PSS/RSS sampling. `word_timestamps` is the only "
        "configuration difference: the effective per-episode transcription options faster-whisper recorded differ "
        "only in that key, and the effective VAD options and post-VAD speech durations are identical across all four "
        "runs. The suite wall clock starts after every worker has loaded the model and finished a 30-second warmup and "
        "includes WAV reads and transcript writes. Summed episode CPU is worker process time during inference.", "",
        f"Segment boundaries legitimately differ between the arms. With word timestamps, faster-whisper replaces VAD "
        f"chunk bounds with the first word start and last word end; this was verified for every segment in both ON "
        f"runs. Across the {len(comparable)} episodes with matching segment counts, {moved:,} of {segments:,} "
        f"segments had at least one bound move relative to the OFF runs (largest start shift "
        f"{max((row['max_abs_start_shift_seconds'] for row in comparable), default=float('nan')):.2f}s, largest end "
        f"shift {max((row['max_abs_end_shift_seconds'] for row in comparable), default=float('nan')):.2f}s). "
        f"The final segment end moved inward by "
        f"{min(tail_shifts):.2f}–{max(tail_shifts):.2f}s. `last_segment_end` is therefore compared as a word-derived "
        "value, not asserted equal. Word arrays were identical between the two ON runs for "
        f"{sum(row['on_run_words_identical'] for row in episodes)} of {len(episodes)} episodes, and segment bounds "
        f"were identical between the two OFF runs for {sum(row['off_run_segment_bounds_identical'] for row in episodes)} "
        f"of {len(episodes)}. Runs 2 and 3 contain {runs[1]['non_monotonic_word_starts']} and "
        f"{runs[2]['non_monotonic_word_starts']} word starts that run backwards at VAD chunk splices; the sentence "
        "joiner must clamp them.", "",
        "## Caveats", "",
        "Each configuration ran twice in one sequence on one host, so no confidence interval is available. The OFF, ON, "
        "ON, OFF order cancels linear drift in host load, not arbitrary contention. The host was shared with other "
        f"work: the 1-minute load average was {runs[0]['load_start'][0]:.2f} before run 1 and "
        f"{min(run['load_end'][0] for run in runs):.2f}–{max(run['load_end'][0] for run in runs):.2f} at the end of "
        "each run, which is consistent with the benchmark's own twenty inference threads but cannot exclude concurrent "
        "external work. CPU seconds also move with cache contention and clock frequency. Sampled peak PSS varied by "
        f"{abs(runs[0]['memory']['suite']['peak_pss_mib'] - runs[3]['memory']['suite']['peak_pss_mib']):.0f} MiB "
        "between the two OFF runs, more than the pooled ON/OFF change, so the memory result means no increase was "
        "detectable at this noise level, not a measured zero. Peaks are 250 ms samples of the summed worker PSS with "
        "the parent excluded, not continuous high-water marks; `ru_maxrss` is each worker's own high-water mark and "
        "double-counts shared pages. Identical text shows that word alignment does not change "
        "decoding; it says nothing about recognition accuracy, which was not scored.", "",
        "All ten source and PCM hashes, full-file inference, the model hash and revision, the episode set and order, "
        "summary and per-episode timing arithmetic, memory sample peaks and worker coverage, logged summaries and "
        "load-average brackets, sequential run timestamps, transcript artifacts, and word-derived segment bounds were "
        "verified. See `word-timestamps-comparison.json` for every check and `word-timestamps-comparison.csv` for "
        "per-run, per-episode values.", "",
    ]
    return "\n".join(lines)


def write_csv(runs: list[dict]) -> None:
    columns = [
        "position", "run_id", "word_timestamps", "suite_wall_seconds", "corpus_wall_speed_x",
        "sum_episode_cpu_seconds", "suite_peak_pss_mib", "startup_peak_pss_mib", "suite_peak_rss_mib",
        "startup_peak_rss_mib", "load_start", "load_end", "episode_index", "episode_title", "episode_audio_seconds",
        "worker_id", "transcribe_seconds", "cpu_seconds", "rtf", "segment_count", "word_count", "text_sha256",
        "last_segment_end", "peak_process_rss_mib",
    ]
    temporary = OUTPUT_CSV.with_name(OUTPUT_CSV.name + ".part")
    with temporary.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for run in sorted(runs, key=lambda item: item["position"]):
            memory = run["memory"]
            for episode in run["episodes"]:
                writer.writerow({
                    "position": run["position"], "run_id": run["run_id"], "word_timestamps": run["word_timestamps"],
                    "suite_wall_seconds": run["suite_wall_seconds"], "corpus_wall_speed_x": run["corpus_wall_speed_x"],
                    "sum_episode_cpu_seconds": run["sum_episode_cpu_seconds"],
                    "suite_peak_pss_mib": memory["suite"]["peak_pss_mib"],
                    "startup_peak_pss_mib": memory["startup"]["peak_pss_mib"],
                    "suite_peak_rss_mib": memory["suite"]["peak_rss_mib"],
                    "startup_peak_rss_mib": memory["startup"]["peak_rss_mib"],
                    "load_start": json.dumps(run["load_start"]), "load_end": json.dumps(run["load_end"]),
                    "episode_index": episode["index"], "episode_title": episode["title"],
                    "episode_audio_seconds": episode["audio_seconds"], "worker_id": episode["worker_id"],
                    "transcribe_seconds": episode["transcribe_seconds"], "cpu_seconds": episode["cpu_seconds"],
                    "rtf": episode["rtf"], "segment_count": episode["segment_count"],
                    "word_count": episode["word_count"], "text_sha256": episode["text_sha256"],
                    "last_segment_end": episode["last_segment_end"],
                    "peak_process_rss_mib": episode["peak_process_rss_mib"],
                })
    temporary.replace(OUTPUT_CSV)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify and report the OFF/ON/ON/OFF word-timestamp crossover.")
    parser.add_argument("run_dirs", nargs="*", type=Path, metavar="RUN_DIR",
                        help="four crossover run directories in execution order; defaults to the dated run IDs")
    args = parser.parse_args()
    require(len(args.run_dirs) in (0, 4), "provide either zero or exactly four run directories")
    run_dirs = ([TAL_ROOT / "runs" / run_id for run_id in DEFAULT_RUN_IDS]
                if not args.run_dirs else [path.resolve() for path in args.run_dirs])
    manifest, pcm_manifest, input_checks = validate_shared_inputs()
    runs, verifications, raw = [], [], []
    for position, run_dir in enumerate(run_dirs, 1):
        require(run_dir.is_dir(), f"run directory does not exist: {run_dir}")
        run, verification, extra = normalize_run(run_dir, position, manifest, pcm_manifest, input_checks)
        runs.append(run)
        verifications.append(verification)
        raw.append(extra)
    crossover = verify_crossover(runs, raw)
    episodes = episode_comparisons(runs, raw, manifest)
    comparison = make_comparison(runs, episodes)
    atomic_text(OUTPUT_MD, render_markdown(runs, comparison, episodes, crossover))
    write_csv(runs)
    atomic_json(OUTPUT_JSON, {
        "verified": True, "scope": "ten full English TAL episodes, faster-whisper tiny.en, word_timestamps OFF/ON/ON/OFF",
        "decision": comparison["decision"], "accuracy_evaluated": False,
        "run_order": [run["run_id"] for run in runs], "shared_inputs": input_checks, "crossover": crossover,
        "runs": runs, "run_verification": verifications, "episodes": episodes, "comparison": comparison,
        "outputs": [str(OUTPUT_MD.relative_to(ROOT)), str(OUTPUT_CSV.relative_to(ROOT))],
    })
    print(f"Verified four word-timestamp crossover runs ({comparison['decision']}); "
          f"wrote {OUTPUT_MD}, {OUTPUT_JSON}, and {OUTPUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
