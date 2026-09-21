"""Full-episode local transcription benchmark; model/audio downloads are excluded."""
import argparse
import dataclasses
import datetime
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1] / "benchmarks/tal"


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--sample-seconds", type=int, default=0)
    args = parser.parse_args()
    (ROOT / "transcripts").mkdir(parents=True, exist_ok=True)
    import ctranslate2
    from faster_whisper import BatchedInferencePipeline, WhisperModel
    from faster_whisper.audio import decode_audio

    manifest = json.loads((ROOT / "manifest.json").read_text())
    config = dict(model="Systran/faster-whisper-small.en", device="cpu", compute_type="int8", cpu_threads=args.threads, batch_size=args.batch_size, beam_size=5, language="en", vad_filter=True, condition_on_previous_text=False, word_timestamps=False)
    system = {"platform": platform.platform(), "python": platform.python_version(), "cpu": subprocess.check_output(["lscpu"], text=True), "memory": subprocess.check_output(["free", "-b"], text=True), "affinity": sorted(os.sched_getaffinity(0)), "load_start": os.getloadavg(), "versions": {p: importlib.metadata.version(p) for p in ["faster-whisper", "ctranslate2", "av", "numpy", "onnxruntime"]}, "supported_compute_types": sorted(ctranslate2.get_supported_compute_types("cpu"))}
    started = time.perf_counter()
    model = WhisperModel(str(ROOT / "models/small.en"), device="cpu", compute_type="int8", cpu_threads=args.threads, num_workers=1, local_files_only=True)
    pipeline = BatchedInferencePipeline(model)
    model_load = time.perf_counter() - started

    def transcribe(audio):
        return pipeline.transcribe(audio, language="en", beam_size=5, batch_size=args.batch_size, vad_filter=True, condition_on_previous_text=False, word_timestamps=False)

    warm_audio = decode_audio(str(ROOT / manifest["episodes"][0]["path"]))[:30 * 16000]
    started = time.perf_counter()
    segments, _ = transcribe(warm_audio)
    list(segments)
    warmup = time.perf_counter() - started
    print(f"Model loaded in {model_load:.2f}s; 30s warmup in {warmup:.2f}s", flush=True)
    model_metadata = (ROOT / "models/small.en/.cache/huggingface/download/model.bin.metadata").read_text().splitlines()
    results = {"started_at": datetime.datetime.now(datetime.UTC).isoformat(), "config": config, "model_revision": model_metadata[0], "model_sha256": model_metadata[1], "system": system, "model_load_seconds": model_load, "warmup_seconds": warmup, "warmup_audio_seconds": 30, "sample_seconds": args.sample_seconds, "episodes": []}
    result_path = ROOT / (f"sample-{args.threads}threads.json" if args.sample_seconds else "results.json")
    save(result_path, results)
    suite_start = time.perf_counter()
    for episode in manifest["episodes"][:1 if args.sample_seconds else 10]:
        print(f"Starting {episode['title']}", flush=True)
        episode_start = time.perf_counter()
        audio = decode_audio(str(ROOT / episode["path"]))
        if args.sample_seconds:
            audio = audio[:args.sample_seconds * 16000]
        decoded_seconds = len(audio) / 16000
        decode_seconds = time.perf_counter() - episode_start
        cpu_start = time.process_time()
        inference_start = time.perf_counter()
        segments, info = transcribe(audio)
        if not results["episodes"]:
            results["effective_transcription_options"] = dataclasses.asdict(info.transcription_options)
            results["effective_vad_options"] = dataclasses.asdict(info.vad_options)
        rows = []
        progress = time.perf_counter()
        for segment in segments:
            rows.append(dataclasses.asdict(segment))
            if time.perf_counter() - progress >= 30:
                print(f"  {episode['index']:02d}: reached {segment.end / 60:.1f}/{decoded_seconds / 60:.1f} audio min in {(time.perf_counter() - inference_start):.1f}s", flush=True)
                progress = time.perf_counter()
        inference_seconds = time.perf_counter() - inference_start
        cpu_seconds = time.process_time() - cpu_start
        assert rows and any(x["text"].strip() for x in rows), "Empty transcription"
        basename = Path(episode["path"]).stem + (f"-sample-{args.threads}" if args.sample_seconds else "")
        transcript = ROOT / "transcripts" / f"{basename}.json"
        save(transcript, {"episode": episode, "config": config, "segments": rows})
        transcript.with_suffix(".txt").write_text("\n".join(x["text"].strip() for x in rows) + "\n")
        wall_seconds = time.perf_counter() - episode_start
        result = {**episode, "decoded_audio_seconds": decoded_seconds, "decode_seconds": decode_seconds, "transcribe_seconds": inference_seconds, "cpu_seconds": cpu_seconds, "end_to_end_seconds": wall_seconds, "rtf": inference_seconds / decoded_seconds, "speed_x": decoded_seconds / inference_seconds, "speech_seconds_after_vad": info.duration_after_vad, "segment_count": len(rows), "last_segment_end": rows[-1]["end"], "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, "transcript": str(transcript.relative_to(ROOT))}
        results["episodes"].append(result)
        save(result_path, results)
        print(f"Completed {episode['title']}: {inference_seconds:.2f}s, {result['speed_x']:.2f}x realtime, {len(rows)} segments", flush=True)
    elapsed = time.perf_counter() - suite_start
    total_audio = sum(e["decoded_audio_seconds"] for e in results["episodes"])
    total_transcribe = sum(e["transcribe_seconds"] for e in results["episodes"])
    results["summary"] = {"episode_count": len(results["episodes"]), "audio_seconds": total_audio, "transcribe_seconds": total_transcribe, "decode_seconds": sum(e["decode_seconds"] for e in results["episodes"]), "suite_wall_seconds": elapsed, "aggregate_rtf": total_transcribe / total_audio, "aggregate_speed_x": total_audio / total_transcribe, "end_to_end_speed_x": total_audio / elapsed, "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, "load_end": os.getloadavg()}
    results["completed_at"] = datetime.datetime.now(datetime.UTC).isoformat()
    save(result_path, results)
    print(json.dumps(results["summary"], indent=2), flush=True)


if __name__ == "__main__":
    main()
