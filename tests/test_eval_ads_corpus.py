"""Ad-corpus feed selection and snapshot reuse (no network)."""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import download_ads_corpus as corpus  # noqa: E402

FEED = b"""<?xml version="1.0"?>
<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>Show</title>
<item><title>Bonus</title><pubDate>Tue, 22 Sep 2026 10:00:00 -0000</pubDate>
  <itunes:episodeType>bonus</itunes:episodeType><itunes:duration>40:00</itunes:duration>
  <enclosure url="https://example.com/bonus.mp3" type="audio/mpeg" length="1"/></item>
<item><title>Trailer</title><pubDate>Tue, 22 Sep 2026 09:00:00 -0000</pubDate>
  <itunes:episodeType>full</itunes:episodeType><itunes:duration>00:02:00</itunes:duration>
  <enclosure url="https://example.com/trailer.mp3" type="audio/mpeg" length="1"/></item>
<item><title>Video</title><pubDate>Tue, 22 Sep 2026 08:00:00 -0000</pubDate>
  <enclosure url="https://example.com/video.mp4" type="video/mp4" length="1"/></item>
<item><title>Untyped older</title><pubDate>Sun, 20 Sep 2026 20:00:00 -0400</pubDate>
  <itunes:duration>3600</itunes:duration>
  <enclosure url="https://example.com/untyped.mp3" type="audio/mpeg" length="1"/></item>
<item><title>Newest full</title><pubDate>Mon, 21 Sep 2026 10:00:00 +0000</pubDate>
  <itunes:episodeType>full</itunes:episodeType><itunes:duration>01:02:03</itunes:duration>
  <enclosure url="https://example.com/newest.mp3" type="audio/mpeg" length="1"/></item>
<item><title>Oldest</title><pubDate>Sat, 19 Sep 2026 10:00:00 +0000</pubDate>
  <enclosure url="https://example.com/oldest.mp3" type="audio/mpeg" length="1"/></item>
<item><title>No enclosure</title><pubDate>Wed, 23 Sep 2026 10:00:00 +0000</pubDate></item>
</channel></rss>
"""


class ParsingTest(unittest.TestCase):
    def test_itunes_duration_formats(self) -> None:
        self.assertEqual(corpus.parse_duration("3600"), 3600)
        self.assertEqual(corpus.parse_duration("48:39"), 2919)
        self.assertEqual(corpus.parse_duration("01:02:03"), 3723)
        self.assertEqual(corpus.parse_duration("2942.5"), 2942.5)
        self.assertIsNone(corpus.parse_duration(""))
        self.assertIsNone(corpus.parse_duration(None))

    def test_publication_dates_are_utc(self) -> None:
        self.assertEqual(corpus.published_utc("Sun, 13 Sep 2026 20:00:00 -0400"),
                         dt.datetime(2026, 9, 14, 0, 0, tzinfo=dt.UTC))
        # "-0000" carries no zone information; the feeds that use it publish UTC.
        self.assertEqual(corpus.published_utc("Tue, 22 Sep 2026 10:00:00 -0000"),
                         dt.datetime(2026, 9, 22, 10, 0, tzinfo=dt.UTC))


class SelectionTest(unittest.TestCase):
    feed = {"slug": "show", "title": "Show"}

    def test_newest_full_audio_episodes_above_the_minimum(self) -> None:
        title, count, items = corpus.select_items(FEED, self.feed, 3, 600)
        self.assertEqual((title, count), ("Show", 7))
        self.assertEqual([item.findtext("title") for item in items], ["Newest full", "Untyped older", "Oldest"])

    def test_channel_title_must_match_the_feed_list(self) -> None:
        with self.assertRaisesRegex(corpus.CorpusError, "does not match feeds.json"):
            corpus.select_items(FEED, {"slug": "show", "title": "Another show"}, 1, 600)

    def test_too_few_eligible_items(self) -> None:
        with self.assertRaisesRegex(corpus.CorpusError, "only 3 eligible items"):
            corpus.select_items(FEED, self.feed, 4, 600)


class SnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch.object(corpus, "ADS_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.root / "feeds").mkdir()
        (self.root / "feeds" / "show.xml.gz").write_bytes(gzip.compress(FEED, mtime=0))
        self.feed = {"slug": "show", "title": "Show", "url": "https://example.com/feed.xml"}

    def test_saved_snapshot_is_reused_without_fetching(self) -> None:
        prior = {"slug": "show", "snapshot_sha256": hashlib.sha256(FEED).hexdigest(), "retrieved_at": "then"}
        with mock.patch.object(corpus, "fetch", side_effect=AssertionError("must not fetch")):
            raw, record = corpus.snapshot_feed(self.feed, {"show": prior}, refresh=False)
        self.assertEqual((raw, record), (FEED, prior))

    def test_snapshot_must_match_its_manifest_hash(self) -> None:
        with self.assertRaisesRegex(corpus.CorpusError, "snapshot hash mismatch"):
            corpus.snapshot_feed(self.feed, {"show": {"snapshot_sha256": "0" * 64}}, refresh=False)

    def test_snapshot_without_a_manifest_record_is_refused(self) -> None:
        with self.assertRaisesRegex(corpus.CorpusError, "no manifest record"):
            corpus.snapshot_feed(self.feed, {}, refresh=False)

    def test_refresh_stores_a_deterministic_gzip_and_records_the_raw_hash(self) -> None:
        def fake_fetch(url, dest, *, compressed):
            self.assertTrue(compressed)
            dest.write_bytes(FEED)
            return {"http_status": 200, "resolved_url": url, "content_type": "application/xml", "redirects": 0}

        with mock.patch.object(corpus, "fetch", fake_fetch):
            raw, record = corpus.snapshot_feed(self.feed, {}, refresh=True)
        stored = (self.root / "feeds" / "show.xml.gz").read_bytes()
        self.assertEqual(raw, FEED)
        self.assertEqual(gzip.decompress(stored), FEED)
        self.assertEqual(stored, gzip.compress(FEED, compresslevel=9, mtime=0))
        self.assertEqual(record["snapshot_sha256"], hashlib.sha256(FEED).hexdigest())
        self.assertEqual(record["snapshot_gzip_sha256"], hashlib.sha256(stored).hexdigest())
        self.assertFalse((self.root / "feeds" / "show.xml").exists(), "the raw download is not kept")


if __name__ == "__main__":
    unittest.main()
