"""RSS 2.0 and Atom podcast feed parsing.

Produces the field set the iOS app's former ``FeedService`` produced
(``git show c1a53ce:ios_app/Noadcast/Services/FeedService.swift``):
channel title, ``itunes:author``, ``itunes:summary`` falling back to
``description``, artwork from ``itunes:image@href`` or ``image/url``; per item
the title (default "Untitled"), the first non-empty of ``description`` /
``itunes:summary`` / ``content:encoded`` (HTML kept verbatim), ``pubDate``,
``itunes:duration`` in all three spellings, and ``guid`` falling back to the
enclosure URL. Items without an enclosure are skipped.

Unlike the Swift parser, which string-matched prefixed names such as
``"itunes:image"`` and so broke whenever a feed bound the namespace to another
prefix, elements are matched by namespace URI: every tag is normalised to a
canonical ``prefix:local`` name (see ``_NAMESPACE_PREFIXES``). Three Swift
quirks are deliberately not reproduced: an empty ``<guid/>`` falls back to the
enclosure URL instead of making every such item collide on ``""``; a partly
numeric duration such as ``"1:xx:30"`` is rejected rather than silently read
as 90 s; and a zero duration is reported as unknown.

GUIDs are not de-duplicated here — the refresher keeps the first occurrence.
Parsing is CPU-bound (≈0.1 s for a 10 MB feed); call it via
``asyncio.to_thread``.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import email.utils
import functools
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin, urlsplit


@dataclass(frozen=True)
class ParsedEpisode:
    guid: str
    title: str
    description: str | None
    published_at: dt.datetime | None  # timezone-aware
    duration_seconds: float | None
    enclosure_url: str
    enclosure_type: str | None
    enclosure_length: int | None
    artwork_url: str | None
    feed_position: int  # 0 = first item in the document


@dataclass(frozen=True)
class ParsedFeed:
    title: str
    author: str | None
    summary: str | None
    artwork_url: str | None
    language: str | None
    link: str | None
    episodes: list[ParsedEpisode] = field(default_factory=list)


class FeedParseError(Exception):
    """The document is not a parseable podcast feed. Permanent for this body."""


# Keys are namespace URIs with the scheme and any trailing slash removed and
# lower-cased, so the variants seen in the wild (https, Apple's old
# "DTDs/Podcast-1.0.dtd" capitalisation, mrss without its slash) all match.
_NAMESPACE_PREFIXES = {
    "www.itunes.com/dtds/podcast-1.0.dtd": "itunes",
    "purl.org/rss/1.0/modules/content": "content",
    "www.w3.org/2005/atom": "atom",
    "search.yahoo.com/mrss": "media",
    "podcastindex.org/namespace/1.0": "podcast",
    "github.com/podcastindex-org/podcast-namespace/blob/main/docs/1.0.md": "podcast",
    "purl.org/dc/elements/1.1": "dc",
}
_XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_UTF8_BOM = b"\xef\xbb\xbf"
_DECLARED_ENCODING = re.compile(rb"""<\?xml[^>]*?\bencoding\s*=\s*["']([A-Za-z][A-Za-z0-9._-]*)["']""")
_DURATION_PART = re.compile(r"[0-9]+(?:\.[0-9]*)?|\.[0-9]+")

Children = dict[str, list[ET.Element]]


@functools.lru_cache(maxsize=512)
def _name(tag: str) -> str:
    """``{uri}local`` -> ``prefix:local`` for known namespaces. Unknown
    namespaces keep the Clark form so they can never shadow an RSS element."""
    if not tag.startswith("{"):
        return tag
    uri, _, local = tag[1:].partition("}")
    key = uri.lower().removeprefix("http://").removeprefix("https://").rstrip("/")
    prefix = _NAMESPACE_PREFIXES.get(key)
    return f"{prefix}:{local}" if prefix else tag


def _children(element: ET.Element) -> Children:
    grouped: Children = {}
    for child in element:
        if isinstance(child.tag, str):  # skips comments and processing instructions
            grouped.setdefault(_name(child.tag), []).append(child)
    return grouped


def _text(element: ET.Element) -> str:
    # itertext() also covers the rare feed that inlines unescaped XHTML.
    return "".join(element.itertext()).strip()


def _first_text(children: Children, *names: str) -> str | None:
    """First non-empty text, trying ``names`` in preference order."""
    for name in names:
        for element in children.get(name, ()):
            text = _text(element)
            if text:
                return text
    return None


def _first_attr(children: Children, name: str, attr: str) -> str | None:
    for element in children.get(name, ()):
        value = (element.get(attr) or "").strip()
        if value:
            return value
    return None


def _base_url(feed_url: str, *elements: ET.Element) -> str:
    base = feed_url
    for element in elements:
        declared = (element.get(_XML_BASE) or "").strip()
        if declared:
            with contextlib.suppress(ValueError):  # a malformed xml:base is ignored
                base = urljoin(base, declared)
    return base


def _resolve(base: str, url: str | None) -> str | None:
    if not url:
        return None
    try:
        # Absolute URLs are returned untouched: urljoin would re-serialise them
        # (dropping an empty "?"), and the enclosure URL doubles as a GUID.
        return url if urlsplit(url).scheme else urljoin(base, url)
    except ValueError:
        return url  # e.g. a broken IPv6 literal: fail that one download, not the whole feed


def parse_date(text: str | None) -> dt.datetime | None:
    """RFC 822 (``pubDate``), falling back to ISO 8601 (Atom, and feeds that
    ignore the RSS spec). Always timezone-aware: a missing or unknown zone
    is taken as UTC. Unparseable -> None."""
    if not text or not text.strip():
        return None
    text = text.strip()
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def parse_duration(text: str | None) -> float | None:
    """``itunes:duration`` as seconds, ``MM:SS`` or ``HH:MM:SS``; fractional
    parts allowed. Minutes are not range-checked ("90:00" is 5400 s).
    Unparseable, zero or non-finite -> None."""
    if not text:
        return None
    parts = [part.strip() for part in text.strip().split(":")]
    if not 1 <= len(parts) <= 3 or not all(_DURATION_PART.fullmatch(part) for part in parts):
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _length(value: str | None) -> int | None:
    # Feeds commonly write length="0" or leave it empty for "unknown".
    digits = (value or "").strip()
    if not digits.isascii() or not digits.isdigit() or len(digits) > 18:
        return None
    return int(digits) or None


def _pick_enclosure(candidates: Iterable[tuple[str, str | None, str | None]]) -> tuple[str, str | None, str | None] | None:
    """RSS allows one enclosure per item, but some feeds attach a cover image
    or transcript alongside the audio. Prefer audio/video, then untyped, in
    document order."""

    def rank(candidate: tuple[str, str | None, str | None]) -> int:
        kind = (candidate[1] or "").lower()
        return 0 if kind.startswith(("audio/", "video/")) else 1 if not kind else 2

    usable = [candidate for candidate in candidates if candidate[0]]
    return min(usable, key=rank) if usable else None


def _episode(
    *,
    base: str,
    title: str | None,
    guid: str | None,
    description: str | None,
    published: str | None,
    duration: str | None,
    enclosure: tuple[str, str | None, str | None],
    artwork: str | None,
    position: int,
) -> ParsedEpisode:
    url, kind, length = enclosure
    enclosure_url = _resolve(base, url)
    assert enclosure_url is not None
    return ParsedEpisode(
        guid=guid or enclosure_url,
        title=title or "Untitled",
        description=description,
        published_at=parse_date(published),
        duration_seconds=parse_duration(duration),
        enclosure_url=enclosure_url,
        enclosure_type=(kind or "").strip() or None,
        enclosure_length=_length(length),
        artwork_url=_resolve(base, artwork),
        feed_position=position,
    )


def _parse_rss(root: ET.Element, channel: ET.Element, feed_url: str) -> ParsedFeed:
    base = _base_url(feed_url, root, channel)
    meta = _children(channel)
    image_url = next(
        (url for image in meta.get("image", ()) if (url := _first_text(_children(image), "url"))), None
    )
    episodes: list[ParsedEpisode] = []
    for item in meta.get("item", ()):
        fields = _children(item)
        enclosure = _pick_enclosure(
            ((e.get("url") or "").strip(), e.get("type"), e.get("length")) for e in fields.get("enclosure", ())
        )
        if enclosure is None:
            continue
        episodes.append(
            _episode(
                base=_base_url(base, item),
                title=_first_text(fields, "title"),
                guid=_first_text(fields, "guid"),
                description=_first_text(fields, "description", "itunes:summary", "content:encoded"),
                published=_first_text(fields, "pubDate"),
                duration=_first_text(fields, "itunes:duration"),
                enclosure=enclosure,
                artwork=_first_attr(fields, "itunes:image", "href"),
                position=len(episodes),
            )
        )
    return ParsedFeed(
        title=_first_text(meta, "title") or feed_url,
        author=_first_text(meta, "itunes:author"),
        summary=_first_text(meta, "itunes:summary", "description"),
        artwork_url=_resolve(base, _first_attr(meta, "itunes:image", "href") or image_url),
        language=_first_text(meta, "language"),
        link=_resolve(base, _first_text(meta, "link")),
        episodes=episodes,
    )


def _atom_links(children: Children, rel: str) -> list[ET.Element]:
    # An Atom link without rel is an "alternate" link (RFC 4287 §4.2.7.2).
    return [
        link
        for link in children.get("atom:link", ())
        if (link.get("rel") or "alternate").strip().lower() == rel and (link.get("href") or "").strip()
    ]


def _parse_atom(root: ET.Element, feed_url: str) -> ParsedFeed:
    base = _base_url(feed_url, root)
    meta = _children(root)
    authors = [_first_text(_children(author), "atom:name") for author in meta.get("atom:author", ())]
    alternate = _atom_links(meta, "alternate")
    episodes: list[ParsedEpisode] = []
    for entry in meta.get("atom:entry", ()):
        fields = _children(entry)
        enclosure = _pick_enclosure(
            ((link.get("href") or "").strip(), link.get("type"), link.get("length"))
            for link in _atom_links(fields, "enclosure")
        )
        if enclosure is None:
            continue
        episodes.append(
            _episode(
                base=_base_url(base, entry),
                title=_first_text(fields, "atom:title"),
                guid=_first_text(fields, "atom:id"),
                description=_first_text(fields, "atom:summary", "itunes:summary", "atom:content"),
                published=_first_text(fields, "atom:published", "atom:updated"),
                duration=_first_text(fields, "itunes:duration"),
                enclosure=enclosure,
                artwork=_first_attr(fields, "itunes:image", "href"),
                position=len(episodes),
            )
        )
    return ParsedFeed(
        title=_first_text(meta, "atom:title") or feed_url,
        author=_first_text(meta, "itunes:author") or next((name for name in authors if name), None),
        summary=_first_text(meta, "itunes:summary", "atom:subtitle"),
        artwork_url=_resolve(
            base, _first_attr(meta, "itunes:image", "href") or _first_text(meta, "atom:logo", "atom:icon")
        ),
        language=(root.get(_XML_LANG) or "").strip() or None,
        link=_resolve(base, alternate[0].get("href", "").strip() if alternate else None),
        episodes=episodes,
    )


def _parse_xml(body: bytes) -> ET.Element:
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedParseError(f"malformed XML: {exc}") from exc
    except LookupError as exc:
        raise FeedParseError(f"unknown XML encoding: {exc}") from exc
    except ValueError as exc:
        # Expat decodes only single-byte legacy encodings; Shift_JIS, GB2312
        # and the like are decoded by Python first (a str is parsed as UTF-8).
        declared = _DECLARED_ENCODING.match(body)
        if declared is None:
            raise FeedParseError(f"undecodable XML: {exc}") from exc
        try:
            return ET.fromstring(body.decode(declared[1].decode("ascii")))
        except (LookupError, UnicodeDecodeError, ET.ParseError) as retry:
            raise FeedParseError(f"undecodable XML: {retry}") from retry


def parse_feed(data: bytes, *, feed_url: str) -> ParsedFeed:
    """Parse an RSS 2.0 or Atom document. ``feed_url`` (the URL the body was
    fetched from) resolves relative URLs and titles a feed that has none."""
    # A BOM followed by blank lines, or blank lines before the XML
    # declaration, is a common server-side templating slip that expat rejects.
    root = _parse_xml(data.removeprefix(_UTF8_BOM).lstrip(b" \t\r\n"))
    kind = _name(root.tag)
    if kind == "rss":
        channel = next((child for child in root if isinstance(child.tag, str) and _name(child.tag) == "channel"), None)
        if channel is None:
            raise FeedParseError("RSS document has no <channel>")
        return _parse_rss(root, channel, feed_url)
    if kind == "atom:feed":
        return _parse_atom(root, feed_url)
    raise FeedParseError(f"not an RSS or Atom feed (root element <{kind}>)")
