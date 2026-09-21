"""Download the newest ten audio entries from a saved official RSS snapshot."""
import concurrent.futures
import datetime
import email.utils
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1] / "benchmarks/tal"


def download(pair):
    index, item = pair
    title = item.findtext("title")
    url = item.find("enclosure").get("url")
    path = ROOT / "audio" / f"{index:02d}-{title.split(':')[0]}.mp3"
    if not path.exists():
        temporary = path.with_suffix(".part")
        subprocess.run(["curl", "-L", "--fail", "--retry", "3", "--max-time", "600", "--silent", "--show-error", url, "-o", str(temporary)], check=True)
        temporary.replace(path)
    metadata = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]))
    duration = float(metadata["format"]["duration"])
    assert duration > 600, (title, duration)
    result = {"index": index, "title": title, "published": item.findtext("pubDate"), "url": url, "path": str(path.relative_to(ROOT)), "duration_seconds": duration, "bytes": path.stat().st_size, "sha256": hashlib.file_digest(path.open("rb"), "sha256").hexdigest()}
    print(f"Downloaded {title}: {duration / 60:.1f} min, {result['bytes'] / 1e6:.1f} MB", flush=True)
    return result


if __name__ == "__main__":
    (ROOT / "audio").mkdir(parents=True, exist_ok=True)
    items = [x for x in ET.parse(ROOT / "feed.xml").findall("./channel/item") if x.find("enclosure") is not None]
    items.sort(key=lambda x: email.utils.parsedate_to_datetime(x.findtext("pubDate")), reverse=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        episodes = list(pool.map(download, enumerate(items[:10], 1)))
    (ROOT / "manifest.json").write_text(json.dumps({"feed": "https://www.thisamericanlife.org/podcast/rss.xml", "retrieved_at": datetime.datetime.now(datetime.UTC).isoformat(), "episodes": episodes}, indent=2) + "\n")
