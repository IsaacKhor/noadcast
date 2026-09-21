"""Audit completed benchmark artifacts and write a readable result table."""
import csv
import email.utils
import hashlib
import json
import math
from pathlib import Path
import statistics
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1] / "benchmarks/tal"


def minutes(seconds):
    return f"{seconds / 60:.2f}"


def main():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    results = json.loads((ROOT / "results.json").read_text())
    episodes = results["episodes"]
    assert results.get("completed_at"), "Benchmark still incomplete"
    assert results["sample_seconds"] == 0
    assert len(episodes) == len(manifest["episodes"]) == 10
    feed_items = [item for item in ET.parse(ROOT / "feed.xml").findall("./channel/item") if item.find("enclosure") is not None]
    feed_items.sort(key=lambda item: email.utils.parsedate_to_datetime(item.findtext("pubDate")), reverse=True)
    for episode, item in zip(episodes, feed_items[:10]):
        assert episode["title"] == item.findtext("title")
        assert episode["published"] == item.findtext("pubDate")
        assert episode["url"] == item.find("enclosure").get("url")
    assert [e["sha256"] for e in episodes] == [e["sha256"] for e in manifest["episodes"]]
    model_path = ROOT / "models/small.en/model.bin"
    with model_path.open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").hexdigest() == results["model_sha256"]
    checks = []
    for episode in episodes:
        path = ROOT / episode["path"]
        with path.open("rb") as handle:
            assert hashlib.file_digest(handle, "sha256").hexdigest() == episode["sha256"]
        assert path.stat().st_size == episode["bytes"]
        assert abs(episode["decoded_audio_seconds"] - episode["duration_seconds"]) < 2
        assert math.isclose(episode["rtf"], episode["transcribe_seconds"] / episode["decoded_audio_seconds"])
        assert math.isclose(episode["speed_x"], 1 / episode["rtf"])
        transcript = json.loads((ROOT / episode["transcript"]).read_text())
        segments = transcript["segments"]
        assert transcript["episode"]["sha256"] == episode["sha256"]
        assert len(segments) == episode["segment_count"] > 0
        assert all(0 <= s["start"] <= s["end"] <= episode["decoded_audio_seconds"] + 1 for s in segments)
        assert all(a["start"] <= b["start"] for a, b in zip(segments, segments[1:]))
        assert segments[-1]["end"] >= episode["decoded_audio_seconds"] - 120, "Inspect missing tail"
        assert (ROOT / episode["transcript"]).with_suffix(".txt").read_text().strip()
        checks.append({"title": episode["title"], "sha256_verified": True, "full_audio_decoded": True, "segment_count": len(segments), "last_segment_end": segments[-1]["end"], "tail_seconds": episode["decoded_audio_seconds"] - segments[-1]["end"]})
    summary = results["summary"]
    assert math.isclose(summary["audio_seconds"], sum(e["decoded_audio_seconds"] for e in episodes))
    assert math.isclose(summary["transcribe_seconds"], sum(e["transcribe_seconds"] for e in episodes))
    assert math.isclose(summary["aggregate_speed_x"], summary["audio_seconds"] / summary["transcribe_seconds"])
    (ROOT / "verification.json").write_text(json.dumps({"completed_at": results["completed_at"], "latest_ten_saved_feed_entries_verified": True, "model_sha256_verified": True, "episodes": checks, "summary_math_verified": True}, indent=2) + "\n")
    with (ROOT / "results.csv").open("w", newline="") as handle:
        columns = ["title", "published", "decoded_audio_seconds", "decode_seconds", "transcribe_seconds", "end_to_end_seconds", "rtf", "speed_x", "segment_count", "peak_process_rss_mib"]
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(episodes)
    config = results["config"]
    lines = [
        "# This American Life transcription benchmark", "",
        f"Completed {results['completed_at']}. Latest ten official feed releases from the 2026-09-20 snapshot; download manifest recorded at {manifest['retrieved_at']}. Includes reruns; ordered by feed publication date.", "",
        f"**{summary['audio_seconds'] / 3600:.2f} hours of audio transcribed in {summary['transcribe_seconds'] / 60:.2f} minutes: {summary['aggregate_speed_x']:.2f}× real time (RTF {summary['aggregate_rtf']:.4f}).** Including MP3 decoding and output writes, the suite took {summary['suite_wall_seconds'] / 60:.2f} minutes ({summary['end_to_end_speed_x']:.2f}×).", "",
        "CPU: AMD Ryzen Threadripper PRO 5955WX, 16 physical cores / 32 logical CPUs; 123 GiB RAM; no exposed compute GPU.", "",
        f"Configuration: faster-whisper {results['system']['versions']['faster-whisper']}, CTranslate2 {results['system']['versions']['ctranslate2']}, small.en, CPU int8, {config['cpu_threads']} threads, batch size {config['batch_size']}, beam size {config['beam_size']}, English, VAD enabled, no previous-text conditioning, no word timestamps. Batched output uses VAD chunk timestamps rather than word-aligned timestamps. Model revision `{results['model_revision']}`.", "",
        "| Episode | Published | Audio min | Transcribe min | Speed | RTF |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for e in episodes:
        lines.append(f"| {e['title']} | {e['published'][5:16]} | {minutes(e['decoded_audio_seconds'])} | {minutes(e['transcribe_seconds'])} | {e['speed_x']:.2f}× | {e['rtf']:.4f} |")
    lines += ["", f"Median per-episode speed: {statistics.median(e['speed_x'] for e in episodes):.2f}×. Range: {min(e['speed_x'] for e in episodes):.2f}–{max(e['speed_x'] for e in episodes):.2f}×. Aggregate throughput is duration-weighted, not an average of episode speed ratios.", "",
        f"Model loading: {results['model_load_seconds']:.2f}s. Separate 30-second warmup: {results['warmup_seconds']:.2f}s. Total MP3 decode time: {summary['decode_seconds']:.2f}s. Peak process RSS: {summary['peak_process_rss_mib'] / 1024:.2f} GiB.", "",
        "Timing uses a monotonic wall clock and consumes every lazy transcript segment. Transcription includes VAD, feature extraction, inference, and collecting segments. End-to-end suite time additionally includes decoding and output writes. Downloads, model loading, and warmup are excluded. Memory is the cumulative process high-water mark. One full-corpus run was performed; no repeated-run confidence interval or formal accuracy evaluation is claimed.", "",
        "A preliminary two-minute sample measured 20.81× with 16 threads and 16.41× with 8 threads. The 16-thread configuration was selected for the full corpus. Those short samples are exploratory and are not included in the full-corpus figures.", "",
        "All ten audio hashes, the model hash, recorded decoded durations, transcript existence and timestamp bounds, and aggregate arithmetic were verified. The runner passes each full decoded waveform to the pipeline and exhausts its segment generator. Artifact checks confirm recorded durations match the source files and outputs were saved; they do not establish recognition accuracy. See `verification.json`, `results.json`, `results.csv`, `manifest.json`, `feed.xml`, and `requirements.txt` for the underlying evidence. Audio, models, transcripts, and caches remain local to this project.", "",
    ]
    (ROOT / "REPORT.md").write_text("\n".join(lines))
    print("Verified all ten episodes; wrote REPORT.md, results.csv, verification.json")


if __name__ == "__main__":
    main()
