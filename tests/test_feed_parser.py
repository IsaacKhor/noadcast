"""Feed parsing: the real This American Life snapshot plus hand-written edge cases."""

from __future__ import annotations

import datetime as dt
import email.utils
import json
import unittest
from pathlib import Path

from noadcast.feeds.parser import FeedParseError, parse_date, parse_duration, parse_feed

ROOT = Path(__file__).resolve().parents[1]
TAL = ROOT / "benchmarks" / "tal"
FEED_URL = "https://example.com/podcast/feed.xml"

NAMESPACES = (
    'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
    'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
    'xmlns:atom="http://www.w3.org/2005/Atom"'
)


def rss(items: str = "", channel: str = "<title>Show</title>", *, namespaces: str = NAMESPACES) -> bytes:
    return f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0" {namespaces}><channel>{channel}{items}</channel></rss>'.encode()


def item(body: str = "", *, enclosure: str = '<enclosure url="https://cdn.example.com/a.mp3" type="audio/mpeg"/>') -> str:
    return f"<item>{body}{enclosure}</item>"


def only_episode(data: bytes):
    feed = parse_feed(data, feed_url=FEED_URL)
    assert len(feed.episodes) == 1, feed.episodes
    return feed.episodes[0]


class TalSnapshotTests(unittest.TestCase):
    """benchmarks/tal/feed.xml, cross-checked against the download manifest."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.feed = parse_feed((TAL / "feed.xml").read_bytes(), feed_url="https://www.thisamericanlife.org/podcast/rss.xml")
        cls.manifest = json.loads((TAL / "manifest.json").read_text())["episodes"]

    def test_channel_fields(self) -> None:
        feed = self.feed
        self.assertEqual(feed.title, "This American Life")
        self.assertEqual(feed.author, "This American Life")
        self.assertTrue(feed.summary.startswith("Each week we choose a theme."))  # description; no itunes:summary
        self.assertEqual(feed.artwork_url, "https://thisamericanlife.org/sites/all/themes/thislife/img/tal-logo-3000x3000.png")
        self.assertEqual(feed.language, "en")
        self.assertEqual(feed.link, "https://www.thisamericanlife.org/podcast/rss.xml")

    def test_every_item_is_kept_in_document_order(self) -> None:
        self.assertEqual(len(self.feed.episodes), 15)
        self.assertEqual([e.feed_position for e in self.feed.episodes], list(range(15)))
        self.assertEqual(len({e.guid for e in self.feed.episodes}), 15)

    def test_newest_ten_match_the_manifest(self) -> None:
        for episode, expected in zip(self.feed.episodes, self.manifest, strict=False):
            with self.subTest(expected["title"]):
                self.assertEqual(episode.title, expected["title"])
                self.assertEqual(episode.enclosure_url, expected["url"])  # &amp; decoded
                self.assertEqual(episode.published_at, email.utils.parsedate_to_datetime(expected["published"]))
                # itunes:duration is the feed's claim; this snapshot overstates the
                # downloaded audio by 1-2 minutes, which is why the server measures.
                self.assertGreater(episode.duration_seconds, expected["duration_seconds"])
                self.assertLess(episode.duration_seconds - expected["duration_seconds"], 150)

    def test_first_episode_fields(self) -> None:
        first = self.feed.episodes[0]
        self.assertEqual(first.guid, "43933 at https://www.thisamericanlife.org")
        self.assertEqual(first.duration_seconds, 67 * 60 + 10)  # "01:07:10"
        self.assertEqual(first.published_at.utcoffset(), dt.timedelta(hours=-4))
        self.assertTrue(first.description.startswith("<p>Cryptic messages on a cell phone"))  # HTML kept
        self.assertEqual(first.enclosure_type, "audio/mpeg")
        self.assertIsNone(first.enclosure_length)  # TAL sends no length attribute
        self.assertTrue(first.artwork_url.endswith("/tal-646.jpg?itok=p7CzzXF5"))

    def test_entities_are_decoded(self) -> None:
        self.assertEqual(self.feed.episodes[5].title, "894: I Couldn't Help but Notice")  # &#039; in the feed


class ItemFieldTests(unittest.TestCase):
    def test_missing_or_blank_guid_falls_back_to_the_enclosure_url(self) -> None:
        for guid in ("", "<guid></guid>", "<guid>   </guid>"):
            with self.subTest(guid=guid):
                self.assertEqual(only_episode(rss(item(guid))).guid, "https://cdn.example.com/a.mp3")

    def test_guid_is_stripped(self) -> None:
        self.assertEqual(only_episode(rss(item('<guid isPermaLink="false">\n  abc-1 \n</guid>'))).guid, "abc-1")

    def test_title_defaults_to_untitled(self) -> None:
        self.assertEqual(only_episode(rss(item())).title, "Untitled")
        self.assertEqual(only_episode(rss(item("<title> \n </title>"))).title, "Untitled")
        self.assertEqual(only_episode(rss(item("<title>\n  Episode 1\t</title>"))).title, "Episode 1")

    def test_description_preference_and_html(self) -> None:
        both = item("<itunes:summary>summary</itunes:summary><description>desc</description><content:encoded>c</content:encoded>")
        self.assertEqual(only_episode(rss(both)).description, "desc")
        blank = item("<description>  </description><itunes:summary>summary</itunes:summary>")
        self.assertEqual(only_episode(rss(blank)).description, "summary")
        encoded = item("<content:encoded><![CDATA[<p>Show &amp; tell</p>]]></content:encoded>")
        self.assertEqual(only_episode(rss(encoded)).description, "<p>Show &amp; tell</p>")  # CDATA is verbatim
        escaped = item("<description>&lt;b&gt;bold&lt;/b&gt; &amp; more</description>")
        self.assertEqual(only_episode(rss(escaped)).description, "<b>bold</b> & more")
        self.assertIsNone(only_episode(rss(item())).description)

    def test_cdata_title(self) -> None:
        self.assertEqual(only_episode(rss(item("<title><![CDATA[Q&A <live>]]></title>"))).title, "Q&A <live>")

    def test_items_without_an_enclosure_are_skipped(self) -> None:
        items = "".join(
            [
                item("<title>one</title>"),
                item("<title>no enclosure</title>", enclosure=""),
                item("<title>no url</title>", enclosure='<enclosure type="audio/mpeg"/>'),
                item("<title>two</title>"),
            ]
        )
        feed = parse_feed(rss(items), feed_url=FEED_URL)
        self.assertEqual([(e.title, e.feed_position) for e in feed.episodes], [("one", 0), ("two", 1)])

    def test_enclosure_attributes(self) -> None:
        episode = only_episode(rss(item(enclosure='<enclosure url=" https://cdn.example.com/b.m4a\n" type=" audio/x-m4a " length="12345"/>')))
        self.assertEqual(episode.enclosure_url, "https://cdn.example.com/b.m4a")
        self.assertEqual(episode.enclosure_type, "audio/x-m4a")
        self.assertEqual(episode.enclosure_length, 12345)
        for length in ('length="0"', 'length=""', 'length="12.5"', 'length="-1"', ""):
            with self.subTest(length=length):
                enclosure = f'<enclosure url="https://cdn.example.com/a.mp3" {length}/>'
                self.assertIsNone(only_episode(rss(item(enclosure=enclosure))).enclosure_length)

    def test_audio_enclosure_preferred_over_other_attachments(self) -> None:
        enclosures = (
            '<enclosure url="https://cdn.example.com/cover.jpg" type="image/jpeg"/>'
            '<enclosure url="https://cdn.example.com/untyped"/>'
            '<enclosure url="https://cdn.example.com/a.mp3" type="audio/mpeg"/>'
        )
        self.assertEqual(only_episode(rss(item(enclosure=enclosures))).enclosure_url, "https://cdn.example.com/a.mp3")
        no_audio = '<enclosure url="https://cdn.example.com/cover.jpg" type="image/jpeg"/><enclosure url="https://cdn.example.com/x"/>'
        self.assertEqual(only_episode(rss(item(enclosure=no_audio))).enclosure_url, "https://cdn.example.com/x")

    def test_relative_urls_resolve_against_xml_base_then_the_feed_url(self) -> None:
        relative = '<enclosure url="/media/a.mp3" type="audio/mpeg"/>'
        self.assertEqual(only_episode(rss(item(enclosure=relative))).enclosure_url, "https://example.com/media/a.mp3")
        based = (
            '<?xml version="1.0"?><rss version="2.0" xml:base="https://files.example.org/show/">'
            '<channel><title>t</title><item><enclosure url="ep1.mp3"/></item></channel></rss>'
        ).encode()
        self.assertEqual(only_episode(based).enclosure_url, "https://files.example.org/show/ep1.mp3")

    def test_a_malformed_url_does_not_sink_the_feed(self) -> None:
        items = (
            item(enclosure='<enclosure url="http://[::1/a.mp3"/>')
            + item("<title>fine</title>")
            + item(enclosure='<enclosure url="relative.mp3"/>')
        )
        data = rss(items).replace(b"<rss ", b'<rss xml:base="http://[bad/" ')  # ignored
        broken, fine, relative = parse_feed(data, feed_url=FEED_URL).episodes
        self.assertEqual(broken.enclosure_url, "http://[::1/a.mp3")  # its download will fail, alone
        self.assertEqual(fine.enclosure_url, "https://cdn.example.com/a.mp3")
        self.assertEqual(relative.enclosure_url, "https://example.com/podcast/relative.mp3")

    def test_absolute_urls_are_not_rewritten(self) -> None:
        # urljoin would drop the empty query; the URL doubles as a GUID.
        episode = only_episode(rss(item(enclosure='<enclosure url="https://cdn.example.com/a.mp3?"/>')))
        self.assertEqual(episode.enclosure_url, "https://cdn.example.com/a.mp3?")
        self.assertEqual(episode.guid, "https://cdn.example.com/a.mp3?")

    def test_item_artwork(self) -> None:
        episode = only_episode(rss(item('<itunes:image href=" https://img.example.com/ep.jpg "/>')))
        self.assertEqual(episode.artwork_url, "https://img.example.com/ep.jpg")
        self.assertIsNone(only_episode(rss(item())).artwork_url)

    def test_pubdate_through_the_feed(self) -> None:
        episode = only_episode(rss(item("<pubDate>Tue, 22 Sep 2026 18:03:11 GMT</pubDate>")))
        self.assertEqual(episode.published_at, dt.datetime(2026, 9, 22, 18, 3, 11, tzinfo=dt.UTC))
        self.assertIsNone(only_episode(rss(item("<pubDate>yesterday</pubDate>"))).published_at)


class NamespaceTests(unittest.TestCase):
    """Elements match by namespace URI, not by the prefix a feed happens to use."""

    def test_itunes_bound_to_another_prefix(self) -> None:
        data = rss(
            item("<it:duration>10:00</it:duration><it:image href='https://img/e.jpg'/>"),
            "<title>t</title><it:author>Someone</it:author><it:image href='https://img/c.jpg'/>",
            namespaces='xmlns:it="http://www.itunes.com/dtds/podcast-1.0.dtd"',
        )
        feed = parse_feed(data, feed_url=FEED_URL)
        self.assertEqual((feed.author, feed.artwork_url), ("Someone", "https://img/c.jpg"))
        self.assertEqual((feed.episodes[0].duration_seconds, feed.episodes[0].artwork_url), (600.0, "https://img/e.jpg"))

    def test_uri_spelling_variants(self) -> None:
        for uri in ("https://www.itunes.com/dtds/podcast-1.0.dtd", "http://www.itunes.com/DTDs/Podcast-1.0.dtd"):
            with self.subTest(uri=uri):
                data = rss(channel="<title>t</title><itunes:author>A</itunes:author>", namespaces=f'xmlns:itunes="{uri}"')
                self.assertEqual(parse_feed(data, feed_url=FEED_URL).author, "A")

    def test_a_familiar_prefix_on_an_unknown_namespace_is_not_matched(self) -> None:
        data = rss(channel="<title>t</title><itunes:author>A</itunes:author>", namespaces='xmlns:itunes="urn:not-itunes"')
        self.assertIsNone(parse_feed(data, feed_url=FEED_URL).author)

    def test_plain_elements_do_not_stand_in_for_namespaced_ones(self) -> None:
        feed = parse_feed(rss(item("<duration>10:00</duration>"), "<title>t</title><author>Plain</author>"), feed_url=FEED_URL)
        self.assertIsNone(feed.author)
        self.assertIsNone(feed.episodes[0].duration_seconds)

    def test_atom_link_is_not_the_rss_link(self) -> None:
        channel = '<title>t</title><atom:link href="https://example.com/self.xml" rel="self"/><link>https://example.com/</link>'
        self.assertEqual(parse_feed(rss(channel=channel), feed_url=FEED_URL).link, "https://example.com/")


class ChannelFieldTests(unittest.TestCase):
    def test_summary_prefers_itunes_summary(self) -> None:
        channel = "<title>t</title><description>desc</description><itunes:summary>summary</itunes:summary>"
        self.assertEqual(parse_feed(rss(channel=channel), feed_url=FEED_URL).summary, "summary")
        channel = "<title>t</title><itunes:summary> </itunes:summary><description>desc</description>"
        self.assertEqual(parse_feed(rss(channel=channel), feed_url=FEED_URL).summary, "desc")

    def test_artwork_falls_back_to_image_url(self) -> None:
        channel = "<title>t</title><image><url> https://img.example.com/logo.png </url><title>logo</title></image>"
        feed = parse_feed(rss(channel=channel), feed_url=FEED_URL)
        self.assertEqual(feed.artwork_url, "https://img.example.com/logo.png")
        self.assertEqual(feed.title, "t")  # the image's <title> is not the channel's

    def test_first_nonempty_channel_title_else_the_feed_url(self) -> None:
        self.assertEqual(parse_feed(rss(channel="<title></title><title>Second</title>"), feed_url=FEED_URL).title, "Second")
        self.assertEqual(parse_feed(rss(channel=""), feed_url=FEED_URL).title, FEED_URL)

    def test_empty_channel_is_not_an_error(self) -> None:
        feed = parse_feed(rss(), feed_url=FEED_URL)
        self.assertEqual((feed.title, feed.author, feed.summary, feed.artwork_url, feed.episodes), ("Show", None, None, None, []))


class AtomTests(unittest.TestCase):
    DOC = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xml:lang="en-GB">
  <title type="text">Atom Show</title>
  <subtitle>About things</subtitle>
  <author><name>Jo Host</name></author>
  <link rel="self" href="https://example.com/atom.xml"/>
  <link href="https://example.com/show"/>
  <logo>https://example.com/logo.png</logo>
  <id>urn:show</id>
  <entry>
    <title>First</title>
    <id>urn:ep:1</id>
    <published>2026-09-20T10:00:00Z</published>
    <updated>2026-09-21T10:00:00Z</updated>
    <summary type="html">&lt;p&gt;hi&lt;/p&gt;</summary>
    <itunes:duration>1:00:00</itunes:duration>
    <link rel="alternate" href="https://example.com/ep1"/>
    <link rel="enclosure" type="audio/mpeg" length="999" href="https://cdn.example.com/1.mp3"/>
  </entry>
  <entry>
    <title>No audio</title>
    <id>urn:ep:2</id>
    <link href="https://example.com/ep2"/>
  </entry>
  <entry>
    <id>urn:ep:3</id>
    <updated>2026-09-22T08:30:00+02:00</updated>
    <content type="html">body</content>
    <link rel="enclosure" href="/3.m4a"/>
  </entry>
</feed>"""

    def test_feed_and_entries(self) -> None:
        feed = parse_feed(self.DOC, feed_url=FEED_URL)
        self.assertEqual(
            (feed.title, feed.author, feed.summary, feed.artwork_url, feed.language, feed.link),
            ("Atom Show", "Jo Host", "About things", "https://example.com/logo.png", "en-GB", "https://example.com/show"),
        )
        first, third = feed.episodes
        self.assertEqual((first.guid, first.title, first.description), ("urn:ep:1", "First", "<p>hi</p>"))
        self.assertEqual(first.published_at, dt.datetime(2026, 9, 20, 10, tzinfo=dt.UTC))  # published beats updated
        self.assertEqual((first.duration_seconds, first.enclosure_type, first.enclosure_length), (3600.0, "audio/mpeg", 999))
        self.assertEqual((first.enclosure_url, first.feed_position), ("https://cdn.example.com/1.mp3", 0))
        self.assertEqual((third.title, third.description, third.feed_position), ("Untitled", "body", 1))
        self.assertEqual(third.enclosure_url, "https://example.com/3.m4a")
        self.assertEqual(third.published_at, dt.datetime(2026, 9, 22, 6, 30, tzinfo=dt.UTC))


class DurationTests(unittest.TestCase):
    def test_formats(self) -> None:
        cases = {
            "3600": 3600.0,
            "3600.5": 3600.5,
            " 45 ": 45.0,
            ".5": 0.5,
            "59:30": 3570.0,
            "90:00": 5400.0,  # minutes are not range-checked
            "1:02:03": 3723.0,
            "01:02:03.25": 3723.25,
            "00:00:07": 7.0,
        }
        for text, seconds in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text), seconds)

    def test_rejects(self) -> None:
        for text in (None, "", "   ", "abc", "1:xx:30", "1:2:3:4", "0", "00:00", "-5", "1e3", "inf", "nan", "1_000", ":30", "٣"):
            with self.subTest(text=text):
                self.assertIsNone(parse_duration(text))


class DateTests(unittest.TestCase):
    def test_rfc822_variants(self) -> None:
        eastern = dt.timezone(dt.timedelta(hours=-4))
        cases = {
            "Sun, 13 Sep 2026 20:00:00 -0400": dt.datetime(2026, 9, 13, 20, tzinfo=eastern),
            "Sun, 13 Sep 2026 20:00:00 EDT": dt.datetime(2026, 9, 13, 20, tzinfo=eastern),
            "13 Sep 2026 20:00:00 GMT": dt.datetime(2026, 9, 13, 20, tzinfo=dt.UTC),
            "  Sun, 13 Sep 2026 20:00:00 +0000\n": dt.datetime(2026, 9, 13, 20, tzinfo=dt.UTC),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_date(text), expected)

    def test_unknown_or_absent_zone_is_utc(self) -> None:
        for text in ("Sun, 13 Sep 2026 20:00:00 -0000", "Sun, 13 Sep 2026 20:00:00 CEST", "2026-09-13T20:00:00"):
            with self.subTest(text=text):
                parsed = parse_date(text)
                self.assertEqual(parsed, dt.datetime(2026, 9, 13, 20, tzinfo=dt.UTC))
                self.assertIsNotNone(parsed.tzinfo)

    def test_iso8601_fallback(self) -> None:
        self.assertEqual(parse_date("2026-09-13T20:00:00Z"), dt.datetime(2026, 9, 13, 20, tzinfo=dt.UTC))
        self.assertEqual(parse_date("2026-09-13T22:00:00.5+02:00"), dt.datetime(2026, 9, 13, 20, 0, 0, 500000, tzinfo=dt.UTC))
        self.assertEqual(parse_date("2026-09-13"), dt.datetime(2026, 9, 13, tzinfo=dt.UTC))

    def test_invalid(self) -> None:
        for text in (None, "", "yesterday", "Sun, 31 Feb 2026 20:00:00 GMT", "2026-13-01", "13/09/2026"):
            with self.subTest(text=text):
                self.assertIsNone(parse_date(text))


class DocumentTests(unittest.TestCase):
    def test_not_a_feed(self) -> None:
        cases = {
            "malformed": b"<rss><channel><title>t</title></rss>",
            "empty": b"",
            "html": b"<html><body>Not found</body></html>",
            "no channel": b'<rss version="2.0"><item/></rss>',
            "text": b"just text",
            "undefined entity": b"<rss><channel><title>a&nbsp;b</title></channel></rss>",
        }
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(FeedParseError):
                parse_feed(data, feed_url=FEED_URL)

    def test_bom_and_leading_whitespace_are_tolerated(self) -> None:
        feed = parse_feed(b"\xef\xbb\xbf\n\n  " + rss(item()), feed_url=FEED_URL)
        self.assertEqual(feed.title, "Show")

    def test_declared_legacy_encodings(self) -> None:
        for encoding, title in (("ISO-8859-1", "Café"), ("windows-1252", "“Quoted”"), ("Shift_JIS", "日本のポッドキャスト"), ("GB2312", "播客")):
            with self.subTest(encoding=encoding):
                data = f'<?xml version="1.0" encoding="{encoding}"?><rss version="2.0"><channel><title>{title}</title></channel></rss>'
                self.assertEqual(parse_feed(data.encode(encoding), feed_url=FEED_URL).title, title)

    def test_bad_encodings_are_parse_errors(self) -> None:
        cases = {
            "unknown": b'<?xml version="1.0" encoding="x-no-such"?><rss/>',
            "wrong multi-byte": b'<?xml version="1.0" encoding="Shift_JIS"?><rss><channel><title>\xff\xff</title></channel></rss>',
            "lying utf-16": b'<?xml version="1.0" encoding="UTF-16"?><rss/>',
        }
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(FeedParseError):
                parse_feed(data, feed_url=FEED_URL)

    def test_comments_and_processing_instructions_are_ignored(self) -> None:
        data = rss(item("<!-- note --><title>t<!-- inline --></title><?pi data?>"), "<!-- c --><title>Show</title>")
        self.assertEqual(only_episode(data).title, "t")


if __name__ == "__main__":
    unittest.main()
