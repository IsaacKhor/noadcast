"""Assembles ``GET /api/v1/sync`` pages from ``repo.sync_page``.

The paging rules (cutoff, referential closure, settings-in-range, 410 below
the tombstone floor) live in the repository; this only renders the page.
Episodes carry their complete live marker set, since markers have no sync
identity of their own.
"""

from __future__ import annotations

from ..context import AppContext
from ..db import repo
from ..timeutil import now_iso
from .schemas import DeletionOut, SyncOut, episode_out, podcast_out, settings_out

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def build_sync_page(ctx: AppContext, *, since: int, limit: int) -> SyncOut:
    """Raises ``repo.CursorExpired`` (410) for a cursor the client must abandon."""
    page = repo.sync_page(ctx.db, since=since, limit=min(max(limit, 1), MAX_LIMIT))
    settings = (
        settings_out(ctx.server_settings(), ctx.classifiers.available()) if page.settings_changed else None
    )
    return SyncOut(
        instance_id=ctx.db.instance_id,
        podcasts=[podcast_out(p) for p in page.podcasts],
        episodes=[episode_out(e, page.markers.get(e.id, ())) for e in page.episodes],
        deletions=[DeletionOut(entity=t.entity, id=t.entity_id) for t in page.deletions],
        settings=settings,
        next_since=page.next_since,
        has_more=page.has_more,
        server_time=now_iso(),
    )
