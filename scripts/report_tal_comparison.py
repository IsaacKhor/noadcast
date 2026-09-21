#!/usr/bin/env python3
"""Verify full TAL benchmark runs and write cross-engine comparison artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TAL_ROOT = ROOT / "benchmarks" / "tal"
OUTPUT_MD = TAL_ROOT / "COMPARISON.md"
OUTPUT_CSV = TAL_ROOT / "comparison.csv"
OUTPUT_VERIFICATION = TAL_ROOT / "comparison-verification.json"


class VerificationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def close(actual: float, expected: float, label: str, *, rel_tol=1e-7, abs_tol=1e-5) -> None:
    require(math.isclose(actual, expected, rel_tol=rel_tol, abs_tol=abs_tol),
            f"{label}: got {actual!r}, expected {expected!r}")


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise VerificationError(f"cannot read JSON {path}: {error}") from error
    require(isinstance(value, dict), f"expected a JSON object: {path}")
    return value


def sha256_file(path: Path) -> str:
    require(path.is_file(), f"missing file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(text)
    temporary.replace(path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def first(mapping: dict, *keys, default=None):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def validate_shared_inputs() -> tuple[dict, dict, dict]:
    manifest_path = TAL_ROOT / "manifest.json"
    pcm_manifest_path = TAL_ROOT / "pcm" / "manifest.json"
    manifest = load_json(manifest_path)
    pcm_manifest = load_json(pcm_manifest_path)
    episodes = manifest.get("episodes", [])
    pcm_episodes = pcm_manifest.get("episodes", [])
    require(len(episodes) == len(pcm_episodes) == 10, "both manifests must contain exactly ten episodes")
    require([item["index"] for item in episodes] == list(range(1, 11)), "source manifest indexes must be 1..10")
    require([item["index"] for item in pcm_episodes] == list(range(1, 11)), "PCM manifest indexes must be 1..10")
    checks = []
    for source, pcm in zip(episodes, pcm_episodes):
        require(source["index"] == pcm["index"], f"manifest index mismatch at {source['index']}")
        require(source["sha256"] == pcm["sha256"], f"upstream SHA mismatch at episode {source['index']}")
        source_path = TAL_ROOT / source["path"]
        pcm_path = TAL_ROOT / pcm["pcm_path"]
        require(sha256_file(source_path) == source["sha256"], f"MP3 SHA mismatch: {source_path}")
        require(sha256_file(pcm_path) == pcm["pcm_sha256"], f"PCM SHA mismatch: {pcm_path}")
        require(abs(source["duration_seconds"] - pcm["pcm_duration_seconds"]) < 2,
                f"manifest duration mismatch at episode {source['index']}")
        checks.append({
            "index": source["index"], "mp3": str(source_path.relative_to(ROOT)),
            "mp3_sha256": source["sha256"], "pcm": str(pcm_path.relative_to(ROOT)),
            "pcm_sha256": pcm["pcm_sha256"], "hashes_verified": True,
        })
    return manifest, pcm_manifest, {
        "manifest": str(manifest_path.relative_to(ROOT)),
        "manifest_sha256": sha256_file(manifest_path),
        "pcm_manifest": str(pcm_manifest_path.relative_to(ROOT)),
        "pcm_manifest_sha256": sha256_file(pcm_manifest_path),
        "episodes": checks,
    }


def input_kind(results: dict) -> str:
    config = results.get("config", {})
    if any("pcm_path" in episode or "pcm_sha256" in episode for episode in results.get("episodes", [])):
        return "pcm"
    value = str(first(config, "input_format", "audio_format", default="mp3")).lower()
    return "pcm" if "pcm" in value or "wav" in value else "mp3"


def episode_audio_seconds(episode: dict) -> float:
    value = first(episode, "benchmark_audio_seconds", "decoded_audio_seconds", "audio_seconds", "duration_seconds")
    require(value is not None and float(value) > 0, f"missing episode audio duration for {episode.get('index')}")
    return float(value)


def episode_process_seconds(episode: dict) -> float:
    value = first(episode, "transcribe_seconds", "processing_seconds", "inference_seconds")
    require(value is not None and float(value) > 0, f"missing processing time for episode {episode.get('index')}")
    return float(value)


def episode_latency_seconds(episode: dict) -> float:
    value = first(episode, "end_to_end_seconds", "latency_seconds", "processing_seconds", "transcribe_seconds")
    require(value is not None and float(value) > 0, f"missing latency for episode {episode.get('index')}")
    return float(value)


def transcript_paths(results_path: Path, episode: dict) -> tuple[Path, Path]:
    json_name = first(episode, "transcript_json", "transcript")
    text_name = first(episode, "transcript_text", "transcript_txt")
    require(json_name, f"episode {episode.get('index')} has no transcript JSON path")
    json_path = Path(json_name)
    if not json_path.is_absolute():
        base = results_path.parent if "transcript_json" in episode else TAL_ROOT
        json_path = base / json_path
    text_path = Path(text_name) if text_name else json_path.with_suffix(".txt")
    if not text_path.is_absolute():
        text_path = results_path.parent / text_path
    return json_path, text_path


def model_identity(results: dict) -> tuple[str, str, str | None, Path | None]:
    config = results.get("config", {})
    model = results.get("model")
    metadata = model if isinstance(model, dict) else {}
    label = str(first(config, "model", "model_name", default=first(metadata, "name", "id", default=model or "unknown")))
    expected_hash = first(metadata, "model_bin_sha256", "sha256", "model_sha256", default=results.get("model_sha256"))
    raw_path = first(metadata, "path", "model_path", default=first(config, "model_path"))
    path = Path(raw_path) if raw_path else None
    if path and not path.is_absolute():
        path = ROOT / path
    if path and path.is_dir():
        path = path / "model.bin"
    if path is None and "small.en" in label:
        path = TAL_ROOT / "models" / "small.en" / "model.bin"
    provenance = metadata.get("provenance", {}) if isinstance(metadata.get("provenance"), dict) else {}
    revision = first(metadata, "revision", default=first(provenance, "source_model_revision",
                                                           default=results.get("model_revision")))
    return label, str(expected_hash) if expected_hash else None, str(revision) if revision else None, path


def memory_total_mib(episodes: list[dict], workers: int) -> float | None:
    values = [(item.get("worker_id"), first(item, "peak_process_rss_mib", "peak_rss_mib")) for item in episodes]
    values = [(worker, float(value)) for worker, value in values if value is not None]
    if not values:
        return None
    if workers <= 1 or all(worker is None for worker, _ in values):
        return max(value for _, value in values)
    by_worker: dict[Any, float] = {}
    for worker, value in values:
        by_worker[worker] = max(value, by_worker.get(worker, 0.0))
    return sum(by_worker.values())


def validate_run(results_path: Path, manifest: dict, pcm_manifest: dict, manifest_hashes: dict,
                 *, baseline=False) -> tuple[dict, dict]:
    results = load_json(results_path)
    config = results.get("config", {})
    summary = results.get("summary", {})
    episodes = results.get("episodes", [])
    if baseline:
        require(bool(results.get("completed_at")), "baseline is incomplete")
        sample_seconds = results.get("sample_seconds", config.get("sample_seconds", 0))
    else:
        require(results.get("status") == "complete", f"run is not complete: {results_path}")
        sample_seconds = config.get("sample_seconds", results.get("sample_seconds", 0))
    require(sample_seconds == 0, f"sample run cannot be compared: {results_path}")
    require(len(episodes) == 10, f"run must contain exactly ten episodes: {results_path}")
    require([item.get("index") for item in episodes] == list(range(1, 11)),
            f"episodes must be ordered exactly 1..10: {results_path}")
    recorded_input = results.get("input", {})
    if recorded_input:
        require(recorded_input.get("manifest_sha256") == manifest_hashes["manifest_sha256"],
                f"recorded source manifest hash mismatch: {results_path}")
        if input_kind(results) == "pcm":
            require(recorded_input.get("decoded_input_manifest_sha256") == manifest_hashes["pcm_manifest_sha256"],
                    f"recorded PCM manifest hash mismatch: {results_path}")

    sources = manifest["episodes"]
    pcm_sources = pcm_manifest["episodes"]
    transcript_checks = []
    for episode, source, pcm in zip(episodes, sources, pcm_sources):
        require(episode.get("title") == source["title"],
                f"episode title mismatch at index {source['index']}: {results_path}")
        require(episode.get("sha256", episode.get("source_sha256")) == source["sha256"],
                f"source SHA mismatch in episode {source['index']}: {results_path}")
        if input_kind(results) == "pcm":
            recorded_pcm_sha = first(episode, "decoded_input_sha256", "pcm_sha256")
            require(recorded_pcm_sha == pcm["pcm_sha256"],
                    f"PCM input SHA mismatch in episode {source['index']}: {results_path}")
        seconds = episode_audio_seconds(episode)
        require(abs(seconds - source["duration_seconds"]) < 2,
                f"full duration mismatch in episode {source['index']}: {seconds}")
        json_path, text_path = transcript_paths(results_path, episode)
        require(json_path.stat().st_size > 2, f"empty transcript JSON: {json_path}")
        transcript = load_json(json_path)
        segments = first(transcript, "segments", "tokens", "chunks")
        require(isinstance(segments, list) and segments, f"no transcript segments: {json_path}")
        require(text_path.is_file() and text_path.read_text().strip(), f"empty transcript text: {text_path}")
        if "chunks" in transcript:
            full_samples = transcript.get("full_pcm_samples")
            benchmark_samples = transcript.get("benchmark_samples")
            require(isinstance(full_samples, int) and benchmark_samples == full_samples,
                    f"full PCM sample coverage is not recorded: {json_path}")
            cursor = 0
            for chunk in segments:
                require(chunk.get("start_sample") == cursor, f"noncontiguous chunk start in {json_path}")
                require(chunk.get("end_sample") - chunk.get("start_sample") == chunk.get("sample_count"),
                        f"chunk sample count mismatch in {json_path}")
                cursor = chunk["end_sample"]
            require(cursor == full_samples, f"chunk coverage does not reach final PCM sample: {json_path}")
        recorded_count = first(episode, "segment_count", "chunk_count", "token_count")
        if recorded_count is not None:
            require(int(recorded_count) == len(segments), f"segment count mismatch: {json_path}")
        transcript_checks.append({
            "index": source["index"], "json": str(json_path.relative_to(ROOT)),
            "text": str(text_path.relative_to(ROOT)), "item_count": len(segments), "nonempty": True,
        })

    audio_seconds = sum(episode_audio_seconds(item) for item in episodes)
    sum_process_seconds = sum(episode_process_seconds(item) for item in episodes)
    sum_latency_seconds = sum(episode_latency_seconds(item) for item in episodes)
    recorded_audio = first(summary, "benchmark_audio_seconds", "audio_seconds", "total_audio_seconds")
    recorded_process = first(summary, "sum_worker_transcribe_seconds", "transcribe_seconds",
                             "sum_worker_processing_seconds", "processing_seconds")
    suite_wall = first(summary, "suite_wall_seconds", "wall_seconds", "corpus_wall_seconds")
    require(recorded_audio is not None and recorded_process is not None and suite_wall is not None,
            f"summary is missing required arithmetic fields: {results_path}")
    suite_wall = float(suite_wall)
    close(float(recorded_audio), audio_seconds, "summary audio seconds")
    close(float(recorded_process), sum_process_seconds, "summary summed processing seconds")
    require(suite_wall > 0, "suite wall must be positive")
    require(suite_wall <= sum_latency_seconds + 1,
            "suite wall cannot exceed summed episode latency by more than scheduling tolerance")
    recorded_wall_speed = first(summary, "corpus_wall_speed_x", "end_to_end_speed_x", "wall_speed_x")
    if recorded_wall_speed is not None:
        close(float(recorded_wall_speed), audio_seconds / suite_wall, "wall throughput")
    recorded_wall_rtf = first(summary, "corpus_wall_rtf", "end_to_end_rtf", "wall_rtf")
    if recorded_wall_rtf is not None:
        close(float(recorded_wall_rtf), suite_wall / audio_seconds, "wall RTF")

    workers = int(first(config, "workers", "worker_count", default=1))
    threads = int(first(config, "cpu_threads", "threads_per_worker", "threads", default=1))
    if "threads_per_worker" in config:
        threads = int(config["threads_per_worker"])
    model_label, model_hash, revision, model_path = model_identity(results)
    require(model_hash, f"missing model hash: {results_path}")
    require(model_path is not None and model_path.is_file(), f"cannot locate model artifact for hash check: {model_path}")
    require(sha256_file(model_path) == model_hash, f"model hash mismatch: {model_path}")
    engine = str(first(config, "engine", default="faster-whisper" if "whisper" in model_label.lower() else "unknown"))
    quantization = str(first(config, "compute_type", "quantization", "precision", default="unspecified"))
    decoder = str(first(config, "decoder", default="whisper beam 5" if engine == "faster-whisper" else "unspecified"))
    decode_seconds = float(first(summary, "sum_worker_decode_seconds", "decode_seconds", default=0.0))
    normalized = {
        "run": "small.en baseline" if baseline else results.get("run_id", results_path.parent.name),
        "engine": engine, "model": model_label, "model_sha256": model_hash,
        "model_revision": revision, "input_format": input_kind(results),
        "workers": workers, "threads_per_worker": threads, "quantization": quantization,
        "decoder": decoder,
        "batch_size": first(config, "batch_size"),
        "chunk_seconds": first(config, "chunk_seconds"),
        "chunk_overlap_seconds": first(config, "chunk_overlap_seconds"),
        "sample_seconds": sample_seconds,
        "sample_limit": first(config, "sample_limit"),
        "sample_rate": first(config, "sample_rate"),
        "engine_revision": first(results.get("engine", {}), "revision"),
        "decoded_input_manifest_sha256": recorded_input.get("decoded_input_manifest_sha256"),
        "audio_seconds": audio_seconds, "suite_wall_seconds": suite_wall,
        "wall_speed_x": audio_seconds / suite_wall, "wall_rtf": suite_wall / audio_seconds,
        "sum_worker_process_seconds": sum_process_seconds,
        "sum_worker_speed_x": audio_seconds / sum_process_seconds,
        "decode_seconds": decode_seconds, "summed_episode_latency_seconds": sum_latency_seconds,
        "cumulative_worker_peak_rss_mib": memory_total_mib(episodes, workers),
        "episodes": [{
            "index": item["index"], "title": item["title"],
            "audio_seconds": episode_audio_seconds(item),
            "process_seconds": episode_process_seconds(item),
            "latency_seconds": episode_latency_seconds(item),
            "worker_id": item.get("worker_id"),
        } for item in episodes],
        "results": str(results_path.relative_to(ROOT)),
    }
    verification = {
        "results": normalized["results"], "complete_full_corpus": True,
        "exact_episode_set_verified": True, "source_hashes_verified": True,
        "durations_verified": True, "transcripts_verified": transcript_checks,
        "summary_arithmetic_verified": True, "parallel_throughput_denominator": "suite_wall_seconds",
        "model_hash_verified": True, "model_sha256": model_hash, "model_revision": revision,
        "normalized_configuration": {
            "engine": engine, "engine_revision": normalized["engine_revision"],
            "model": model_label, "decoder": decoder, "quantization": quantization,
            "workers": workers, "threads_per_worker": threads,
            "batch_size": normalized["batch_size"], "chunk_seconds": normalized["chunk_seconds"],
            "chunk_overlap_seconds": normalized["chunk_overlap_seconds"],
            "sample_seconds": sample_seconds, "sample_limit": normalized["sample_limit"],
            "sample_rate": normalized["sample_rate"], "input_format": normalized["input_format"],
            "decoded_input_manifest_sha256": normalized["decoded_input_manifest_sha256"],
        },
        "model_path": str(model_path.relative_to(ROOT)),
    }
    return normalized, verification


def verify_parakeet_decoder_pair(runs: list[dict]) -> dict | None:
    variants = {run["decoder"].lower(): run for run in runs if run["engine"] == "parakeet.cpp"}
    if not {"tdt", "ctc"}.issubset(variants):
        return None
    tdt, ctc = variants["tdt"], variants["ctc"]
    fields = [
        "model_sha256", "decoded_input_manifest_sha256", "workers", "threads_per_worker",
        "batch_size", "chunk_seconds", "chunk_overlap_seconds", "sample_seconds", "sample_limit",
        "sample_rate", "engine_revision",
    ]
    for field in fields:
        require(tdt[field] is not None, f"TDT run lacks decoder-comparison metadata: {field}")
        require(tdt[field] == ctc[field],
                f"TDT and CTC runs differ in {field}: {tdt[field]!r} != {ctc[field]!r}")
    return {
        "verified": True, "decoders": ["tdt", "ctc"],
        "equal_fields": {field: tdt[field] for field in fields},
        "differing_field": "decoder",
    }


def fmt_seconds(seconds: float) -> str:
    return f"{seconds:.2f}"


def render_markdown(runs: list[dict], manifest: dict, pcm_manifest: dict) -> str:
    lines = [
        "# This American Life transcription engine comparison", "",
        f"All runs use the same ten episodes ({sum(item['duration_seconds'] for item in manifest['episodes']) / 3600:.2f} hours). "
        "The headline throughput is total corpus audio divided by the measured suite wall clock. "
        "For parallel runs it is never calculated from the sum of worker processing times.", "",
        "| Engine / model | Quantization / decoder | Input | Workers × threads | Suite wall | Wall throughput | Cumulative worker peak RSS |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for run in runs:
        rss = run["cumulative_worker_peak_rss_mib"]
        rss_text = f"{rss / 1024:.2f} GiB" if rss is not None else "n/a"
        lines.append(
            f"| {run['engine']} / {run['model']} | {run['quantization']} / {run['decoder']} | {run['input_format'].upper()} | "
            f"{run['workers']} × {run['threads_per_worker']} | {run['suite_wall_seconds']:.2f}s | "
            f"{run['wall_speed_x']:.2f}× | {rss_text} |"
        )
    lines += ["", "Model identities:", ""]
    for run in runs:
        revision = f", revision `{run['model_revision']}`" if run["model_revision"] else ""
        lines.append(f"- {run['run']}: SHA-256 `{run['model_sha256']}`{revision}.")
    baseline = runs[0]
    lines += [
        "",
        f"The original small.en run spent {baseline['sum_worker_process_seconds']:.2f}s in inference "
        f"({baseline['sum_worker_speed_x']:.2f}×) and {baseline['suite_wall_seconds']:.2f}s end to end "
        f"({baseline['wall_speed_x']:.2f}×), including {baseline['decode_seconds']:.2f}s of MP3 decoding.", "",
        "The newer runs consume predecoded 16 kHz mono PCM. Their suite wall clocks include reading and decoding "
        f"the WAV containers, while the one-time PCM preparation ({pcm_manifest.get('preparation_wall_seconds', 0):.2f}s) is excluded. "
        "That makes the input-stage accounting explicit, but the formats are not identical.", "",
        "The original configuration is faster-whisper small.en, CPU int8, beam 5, batch 8, VAD enabled, with one "
        "16-thread worker. The tiny.en configuration keeps those decoding settings and uses ten 2-thread workers. "
        "The Parakeet configurations use the same Q8_0 hybrid TDT/CTC model with greedy TDT and greedy CTC decoding, "
        "respectively. Both use batch 8 and ten 2-thread workers, and process fixed contiguous 30-second chunks with "
        "no VAD and no overlap.", "",
        "Because the original uses 1 × 16 threads and the newer runs use 10 × 2, their speed differences are not an "
        "isolated model-only comparison. Recognition accuracy and the effect of Parakeet's 30-second chunk boundaries "
        "were not measured.", "",
        "## Per-episode task latency", "",
        "Each value is `processing / task latency` in seconds. Task latency includes the input read/decode where recorded. "
        "Episodes overlap in parallel runs, so these are concurrent latencies and must not be added to obtain corpus wall time.", "",
        "| Episode | " + " | ".join(run["run"] + " (process / latency)" for run in runs) + " |",
        "|---|" + "---:|" * len(runs),
    ]
    for position, source in enumerate(manifest["episodes"]):
        values = " | ".join(
            fmt_seconds(run["episodes"][position]["process_seconds"]) + "s / "
            + fmt_seconds(run["episodes"][position]["latency_seconds"]) + "s"
            for run in runs
        )
        lines.append(f"| {source['index']:02d} · {source['title']} | {values} |")
    lines += [
        "", "Peak RSS is a process high-water mark. For multi-worker runs, the table sums each worker's maximum "
        "recorded high-water mark; it is a cumulative capacity indicator rather than a simultaneous system-memory sample.", "",
        "One full-corpus run was performed for each configuration. These measurements have no repeated-run confidence "
        "interval and make no transcription-accuracy claim. Transcript checks establish complete, nonempty artifacts and "
        "matching inputs; they do not score recognition quality.", "",
        "See `comparison-verification.json` for input, model, transcript, duration, and arithmetic checks, and "
        "`comparison.csv` for machine-readable per-episode timings.", "",
    ]
    return "\n".join(lines)


def write_csv(runs: list[dict]) -> None:
    columns = [
        "run", "engine", "model", "quantization", "decoder", "input_format", "workers", "threads_per_worker",
        "corpus_audio_seconds", "suite_wall_seconds", "corpus_wall_speed_x", "sum_worker_process_seconds",
        "sum_worker_speed_x", "decode_seconds", "cumulative_worker_peak_rss_mib", "episode_index",
        "episode_title", "episode_audio_seconds", "episode_process_seconds", "episode_latency_seconds", "worker_id",
    ]
    temporary = OUTPUT_CSV.with_name(OUTPUT_CSV.name + ".part")
    with temporary.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for run in runs:
            for episode in run["episodes"]:
                writer.writerow({
                    "run": run["run"], "engine": run["engine"], "model": run["model"],
                    "quantization": run["quantization"], "decoder": run["decoder"],
                    "input_format": run["input_format"],
                    "workers": run["workers"], "threads_per_worker": run["threads_per_worker"],
                    "corpus_audio_seconds": run["audio_seconds"], "suite_wall_seconds": run["suite_wall_seconds"],
                    "corpus_wall_speed_x": run["wall_speed_x"],
                    "sum_worker_process_seconds": run["sum_worker_process_seconds"],
                    "sum_worker_speed_x": run["sum_worker_speed_x"], "decode_seconds": run["decode_seconds"],
                    "cumulative_worker_peak_rss_mib": run["cumulative_worker_peak_rss_mib"],
                    "episode_index": episode["index"], "episode_title": episode["title"],
                    "episode_audio_seconds": episode["audio_seconds"],
                    "episode_process_seconds": episode["process_seconds"],
                    "episode_latency_seconds": episode["latency_seconds"], "worker_id": episode["worker_id"],
                })
    temporary.replace(OUTPUT_CSV)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify and compare full-corpus tiny.en and Parakeet TAL benchmark run directories.")
    parser.add_argument("run_dirs", nargs="+", type=Path, metavar="RUN_DIR",
                        help="completed full-run directories in display order")
    args = parser.parse_args()
    manifest, pcm_manifest, input_verification = validate_shared_inputs()
    runs = []
    run_verifications = []
    baseline, check = validate_run(TAL_ROOT / "results.json", manifest, pcm_manifest, input_verification,
                                   baseline=True)
    runs.append(baseline)
    run_verifications.append(check)
    for directory in args.run_dirs:
        path = directory.resolve()
        result_path = path if path.name == "results.json" else path / "results.json"
        run, check = validate_run(result_path, manifest, pcm_manifest, input_verification)
        runs.append(run)
        run_verifications.append(check)
    require(len({run["engine"] + "\0" + run["model"] + "\0" + run["decoder"] for run in runs}) == len(runs),
            "comparison inputs must identify distinct engine/model/decoder configurations")
    decoder_pair = verify_parakeet_decoder_pair(runs)

    atomic_text(OUTPUT_MD, render_markdown(runs, manifest, pcm_manifest))
    write_csv(runs)
    atomic_json(OUTPUT_VERIFICATION, {
        "verified": True, "shared_inputs": input_verification, "runs": run_verifications,
        "comparison": {
            "headline_denominator": "suite_wall_seconds", "full_corpus_only": True,
            "one_run_per_configuration": True, "accuracy_evaluated": False,
            "parakeet_decoder_pair": decoder_pair,
            "outputs": [str(OUTPUT_MD.relative_to(ROOT)), str(OUTPUT_CSV.relative_to(ROOT))],
        },
    })
    print(f"Verified {len(runs)} full-corpus runs; wrote {OUTPUT_MD}, {OUTPUT_CSV}, and {OUTPUT_VERIFICATION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
