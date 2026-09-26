"""OPML 2.0 subscription lists: parse on import, render on export."""

from __future__ import annotations

import datetime as dt
import email.utils
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Sequence

from ..db import repo


@dataclass(frozen=True)
class OpmlFeed:
    url: str
    title: str | None


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _attr(element: ET.Element, name: str) -> str | None:
    """Attribute lookup ignoring case: exporters disagree on ``xmlUrl`` vs ``xmlURL``."""
    lowered = name.lower()
    for key, value in element.attrib.items():
        if key.lower() == lowered:
            return value.strip() or None
    return None


def parse_opml(data: bytes) -> list[OpmlFeed]:
    """Every ``outline`` carrying an ``xmlUrl``, at any nesting depth, in
    document order. Raises ``ValueError`` if this is not an OPML document."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"malformed OPML: {exc}") from exc
    if _local(root.tag) != "opml":
        raise ValueError(f"not an OPML document (root element <{_local(root.tag)}>)")
    feeds: list[OpmlFeed] = []
    for element in root.iter():
        if _local(element.tag) != "outline":
            continue
        url = _attr(element, "xmlUrl")
        if url:
            feeds.append(OpmlFeed(url=url, title=_attr(element, "title") or _attr(element, "text")))
    return feeds


def render_opml(podcasts: Sequence[repo.Podcast], *, now: dt.datetime) -> bytes:
    root = ET.Element("opml", version="2.0")
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = "Noadcast subscriptions"
    ET.SubElement(head, "dateCreated").text = email.utils.format_datetime(now.astimezone(dt.UTC), usegmt=True)
    body = ET.SubElement(root, "body")
    for podcast in podcasts:
        attributes = {"type": "rss", "text": podcast.title, "title": podcast.title, "xmlUrl": podcast.feed_url}
        if podcast.link:
            attributes["htmlUrl"] = podcast.link
        ET.SubElement(body, "outline", attributes)
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"
