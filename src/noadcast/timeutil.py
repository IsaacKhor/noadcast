"""Timestamps.

Every stored and transmitted timestamp is RFC 3339 UTC with exactly
millisecond precision and a ``Z`` suffix, e.g. ``2026-09-22T18:03:11.123Z``.
The fixed width means lexicographic order equals chronological order, which
SQL comparisons such as ``available_at <= ?`` rely on. Always go through
``iso()``; never store ``datetime.isoformat()`` output directly.
"""

from __future__ import annotations

import datetime as dt


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetime; attach a timezone before storing")
    return value.astimezone(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_or_none(value: dt.datetime | None) -> str | None:
    return None if value is None else iso(value)


def now_iso() -> str:
    return iso(utc_now())


def parse_iso(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def after(seconds: float, start: dt.datetime | None = None) -> str:
    """ISO timestamp ``seconds`` from ``start`` (default: now)."""
    return iso((start or utc_now()) + dt.timedelta(seconds=seconds))
