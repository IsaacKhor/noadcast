#!/usr/bin/env python3
"""Snapshot ad-supported podcast feeds and download their newest full episodes.

Generalises download_tal.py to the feed list in benchmarks/ads/feeds.json. Each
enclosure is served through dynamic ad insertion (DAI): the same URL can carry
different ads on another day, from another network location, or for another
User-Agent. The manifest therefore pins each downloaded render by SHA-256, and
saved feed snapshots are reused so reruns never silently change the corpus.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import email.utils
import gzip
import hashlib
import json
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# Rebound by --corpus; benchmarks/top10 uses the same layout and feeds.json schema.
ADS_ROOT = ROOT / "benchmarks" / "ads"
FEEDS_JSON = ADS_ROOT / "feeds.json"
MANIFEST = ADS_ROOT / "manifest.json"
INDEX_WIDTH = 2  # widened for corpora of 100+ episodes so filenames sort by index
USER_AGENT = "Noadcast/0.2 benchmark-corpus"
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
AUDIO_SUFFIXES = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
                  "audio/aac": ".aac"}
RETRIEVAL_KEYS = ("retrieved_at", "http_status", "resolved_url", "content_type", "redirects")
DAI_NOTE = (
    "Each audio file is a single dynamic-ad-insertion render, fetched once at its retrieved_at time with "
    "user_agent from this host's network location. Another fetch of the same enclosure URL may carry different "
    "ads, ad positions, and duration, so transcripts, markers, and evaluations apply only to these exact bytes "
    "(sha256). resolved_url omits the final query string, which can carry per-request ad-decision tokens."
)


class CorpusError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CorpusError(message)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)


def fetch(url: str, dest: Path, *, compressed: bool) -> dict:
    """curl with retries into ``dest.part``, then an atomic rename. Returns the final hop's metadata."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + ".part")
    command = [
        "curl", "--location", "--fail", "--silent", "--show-error", "--retry", "5", "--retry-all-errors",
        "--retry-delay", "3", "--connect-timeout", "30", "--max-time", "1800", "--user-agent", USER_AGENT,
        "--output", str(temporary), "--write-out", "%{http_code}\t%{url_effective}\t%{content_type}\t%{num_redirects}",
        *(["--compressed"] if compressed else []), url,
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise CorpusError(f"curl exit {completed.returncode} for {url}: {completed.stderr.strip()}")
    status, effective, content_type, redirects = completed.stdout.split("\t")
    require(temporary.stat().st_size > 0, f"empty download: {url}")
    temporary.replace(dest)
    parts = urllib.parse.urlsplit(effective)
    return {"http_status": int(status), "resolved_url": f"{parts.scheme}://{parts.netloc}{parts.path}",
            "content_type": content_type or None, "redirects": int(redirects)}


def parse_duration(text: str | None) -> float | None:
    """itunes:duration as seconds: ``SS``, ``MM:SS``, or ``HH:MM:SS``."""
    if not text or not text.strip():
        return None
    seconds = 0.0
    for part in text.strip().split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def published_utc(text: str) -> dt.datetime:
    parsed = email.utils.parsedate_to_datetime(text)
    # RFC 2822 "-0000" means "no zone information"; the feeds that use it publish UTC.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def select_items(raw: bytes, feed: dict, count: int, min_seconds: float) -> tuple[str, int, list[ET.Element]]:
    channel = ET.fromstring(raw).find("channel")
    require(channel is not None, f"{feed['slug']}: feed has no channel")
    title = (channel.findtext("title") or "").strip()
    require(title == feed["title"], f"{feed['slug']}: channel title {title!r} does not match feeds.json")
    candidates = []
    items = channel.findall("item")
    for item in items:
        enclosure = item.find("enclosure")
        episode_type = (item.findtext(f"{ITUNES}episodeType") or "full").strip().lower()
        declared = parse_duration(item.findtext(f"{ITUNES}duration"))
        if (enclosure is None or not (enclosure.get("type") or "").startswith("audio/") or episode_type != "full"
                or (declared is not None and declared < min_seconds)):
            continue
        candidates.append((published_utc(item.findtext("pubDate")), item))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    require(len(candidates) >= count, f"{feed['slug']}: only {len(candidates)} eligible items")
    return title, len(items), [item for _, item in candidates[:count]]


def snapshot_feed(feed: dict, previous: dict, refresh: bool) -> tuple[bytes, dict]:
    """Reuse the saved gzip snapshot unless refreshing; the raw-XML SHA-256 is the snapshot identity."""
    path = ADS_ROOT / "feeds" / f"{feed['slug']}.xml.gz"
    prior = previous.get(feed["slug"])
    if path.is_file() and not refresh:
        require(prior is not None, f"{path} has no manifest record; rerun with --refresh-feeds")
        raw = gzip.decompress(path.read_bytes())
        require(sha256_bytes(raw) == prior["snapshot_sha256"], f"snapshot hash mismatch: {path}")
        return raw, prior
    raw_path = path.with_suffix("")
    retrieved_at = dt.datetime.now(dt.UTC).isoformat()
    retrieval = fetch(feed["url"], raw_path, compressed=True)
    raw = raw_path.read_bytes()
    raw_path.unlink()
    atomic_bytes(path, gzip.compress(raw, compresslevel=9, mtime=0))
    return raw, {
        "slug": feed["slug"], "title": feed["title"], "url": feed["url"],
        "snapshot": str(path.relative_to(ADS_ROOT)), "snapshot_sha256": sha256_bytes(raw),
        "snapshot_bytes": len(raw), "snapshot_gzip_sha256": sha256_file(path),
        "retrieved_at": retrieved_at, **{key: retrieval[key] for key in RETRIEVAL_KEYS[1:]},
    }


def ffprobe(path: Path) -> dict:
    output = subprocess.check_output(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json",
                                      str(path)])
    metadata = json.loads(output)
    audio = [stream for stream in metadata["streams"] if stream.get("codec_type") == "audio"]
    require(len(audio) == 1, f"expected exactly one audio stream: {path}")
    return {"duration_seconds": float(metadata["format"]["duration"]), "codec": audio[0].get("codec_name"),
            "sample_rate": int(audio[0]["sample_rate"]), "channels": audio[0].get("channels"),
            "bit_rate": int(metadata["format"]["bit_rate"]) if metadata["format"].get("bit_rate") else None}


def download_episode(index: int, feed: dict, podcast_title: str, item: ET.Element, previous: dict,
                     min_seconds: float) -> dict:
    enclosure = item.find("enclosure")
    url, content_type = enclosure.get("url"), enclosure.get("type")
    published = item.findtext("pubDate").strip()
    stamp = published_utc(published)
    suffix = AUDIO_SUFFIXES.get(content_type) or Path(urllib.parse.urlsplit(url).path).suffix or ".mp3"
    path = ADS_ROOT / "audio" / f"{index:0{INDEX_WIDTH}d}-{feed['slug']}-{stamp:%Y%m%d}{suffix}"
    relative = str(path.relative_to(ADS_ROOT))
    prior = previous.get(relative)
    if prior and prior["url"] == url and path.is_file() and sha256_file(path) == prior["sha256"]:
        retrieval = {key: prior[key] for key in RETRIEVAL_KEYS}
    else:
        retrieved_at = dt.datetime.now(dt.UTC).isoformat()
        retrieval = {"retrieved_at": retrieved_at, **fetch(url, path, compressed=False)}
    require((retrieval["content_type"] or "").startswith("audio/"),
            f"{relative}: served content type {retrieval['content_type']!r} is not audio")
    probe = ffprobe(path)
    require(probe["duration_seconds"] >= min_seconds, f"{relative}: {probe['duration_seconds']:.1f}s is too short")
    declared_bytes = int(enclosure.get("length") or 0)
    result = {
        "index": index, "podcast_slug": feed["slug"], "podcast_title": podcast_title, "feed_url": feed["url"],
        "title": item.findtext("title").strip(), "guid": (item.findtext("guid") or "").strip() or None,
        "published": published, "published_utc": stamp.astimezone(dt.UTC).isoformat(), "url": url,
        "path": relative, "duration_seconds": probe["duration_seconds"],
        "declared_duration_seconds": parse_duration(item.findtext(f"{ITUNES}duration")),
        "bytes": path.stat().st_size, "declared_bytes": declared_bytes or None, "sha256": sha256_file(path),
        "codec": probe["codec"], "sample_rate": probe["sample_rate"], "channels": probe["channels"],
        "bit_rate": probe["bit_rate"], "user_agent": USER_AGENT, **retrieval,
    }
    print(f"{relative}: {result['title']} — {result['duration_seconds'] / 60:.1f} min, "
          f"{result['bytes'] / 1e6:.1f} MB", flush=True)
    return result


def main() -> int:
    global ADS_ROOT, FEEDS_JSON, MANIFEST, INDEX_WIDTH
    parser = argparse.ArgumentParser(description="Snapshot a corpus's feeds.json feeds and download their newest episodes.")
    parser.add_argument("--refresh-feeds", action="store_true",
                        help="refetch feed snapshots instead of reusing saved ones (changes the corpus)")
    parser.add_argument("--corpus", type=Path, default=ADS_ROOT,
                        help="corpus directory holding feeds.json (default: benchmarks/ads)")
    args = parser.parse_args()
    ADS_ROOT = args.corpus.resolve()
    FEEDS_JSON, MANIFEST = ADS_ROOT / "feeds.json", ADS_ROOT / "manifest.json"
    config = json.loads(FEEDS_JSON.read_text())
    count, min_seconds = int(config["episodes_per_feed"]), float(config["min_duration_seconds"])
    old = json.loads(MANIFEST.read_text()) if MANIFEST.is_file() else {"feeds": [], "episodes": []}
    previous_feeds = {row["slug"]: row for row in old["feeds"]}
    previous_episodes = {row["path"]: row for row in old["episodes"]}
    require(len({feed["slug"] for feed in config["feeds"]}) == len(config["feeds"]), "feed slugs must be unique")

    feeds, jobs = [], []
    for feed in config["feeds"]:
        raw, record = snapshot_feed(feed, previous_feeds, args.refresh_feeds)
        podcast_title, item_count, items = select_items(raw, feed, count, min_seconds)
        feeds.append({**record, "item_count": item_count})
        jobs += [(feed, podcast_title, item) for item in items]
    INDEX_WIDTH = max(2, len(str(len(jobs))))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        episodes = list(pool.map(
            lambda job: download_episode(job[0] + 1, *job[1], previous_episodes, min_seconds), enumerate(jobs)))
    require(len({row["sha256"] for row in episodes}) == len(episodes), "duplicate audio renders in corpus")
    manifest = {
        "schema_version": 1, "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "feeds_json": str(FEEDS_JSON.relative_to(ROOT)), "feeds_json_sha256": sha256_file(FEEDS_JSON),
        "user_agent": USER_AGENT, "note": DAI_NOTE, "feeds": feeds, "episodes": episodes,
    }
    atomic_bytes(MANIFEST, (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode())
    hours = sum(row["duration_seconds"] for row in episodes) / 3600
    print(f"Wrote {MANIFEST}: {len(episodes)} episodes from {len(feeds)} feeds, {hours:.2f} hours", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
