"""Prepare lossless 16 kHz mono PCM inputs, outside transcription timings."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import time
import wave

ROOT = Path(__file__).resolve().parents[1] / "benchmarks/tal"


def convert(episode):
    dest = ROOT / "pcm" / (Path(episode["path"]).stem + ".wav")
    start = time.perf_counter()
    if not dest.exists():
        temp = dest.with_suffix(".part.wav")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-i", str(ROOT / episode["path"]), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-threads", "1", "-y", str(temp)], check=True)
        temp.replace(dest)
    with wave.open(str(dest)) as wav:
        assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) == (1, 16000, 2)
        seconds = wav.getnframes() / wav.getframerate()
    assert abs(seconds - episode["duration_seconds"]) < 2
    with dest.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    print(f"Prepared {episode['title']}: {seconds:.3f}s", flush=True)
    return {**episode, "pcm_path": str(dest.relative_to(ROOT)), "pcm_sha256": digest, "pcm_duration_seconds": seconds, "preparation_seconds": time.perf_counter() - start}


if __name__ == "__main__":
    (ROOT / "pcm").mkdir(exist_ok=True)
    manifest = json.loads((ROOT / "manifest.json").read_text())
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as executor:
        episodes = list(executor.map(convert, manifest["episodes"]))
    (ROOT / "pcm/manifest.json").write_text(json.dumps({"episodes": episodes, "preparation_wall_seconds": time.perf_counter() - started, "format": "16000 Hz mono signed 16-bit PCM WAV"}, indent=2) + "\n")
