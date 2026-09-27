"""Isolated, offline dashboard fixture for browser QA."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn

from noadcast.api.app import create_app
from noadcast.config import settings_for_tests
from noadcast.db import repo
from noadcast.timeutil import now_iso

_MARKER = ".noadcast-dashboard-fixture"
_SIGNATURE = "Synthetic Noadcast dashboard fixture v1\n"
_FAR_FUTURE = "2099-01-01T00:00:00.000Z"


def prepare_data_dir(path: Path) -> Path:
    path = path.resolve()
    cache = (Path(__file__).resolve().parents[2] / ".cache").resolve()
    if path == cache or not path.is_relative_to(cache):
        raise ValueError(f"fixture data directory must be a child of {cache}")
    if path.exists() and (not path.is_dir() or (any(path.iterdir()) and not (path / _MARKER).is_file())):
        raise ValueError(f"refusing unmarked existing data directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    marker = path / _MARKER
    if marker.exists():
        if marker.read_text() != _SIGNATURE:
            raise ValueError(f"refusing data directory with a different marker: {path}")
    else:
        marker.write_text(_SIGNATURE)
    return path


def seed(ctx) -> None:
    existing = repo.list_podcasts(ctx.db)
    if existing:
        if any(not podcast.feed_url.endswith(".invalid/feed.xml") for podcast in existing):
            raise ValueError("refusing database with non-fixture podcasts")
        return
    now = now_iso()
    shows = (
        ("https://atlas.invalid/feed.xml", "Fixture: Atlas Audio", True),
        ("https://bluebird.invalid/feed.xml", "Fixture: Bluebird Briefing", False),
    )
    with ctx.db.write() as tx:
        for feed, title, enabled in shows:
            repo.insert_podcast(
                tx, feed_url=feed, title=title, auto_process_enabled=False,
                ad_analysis_enabled=enabled, initial_backfill_count=1,
                next_fetch_at=_FAR_FUTURE, now=now,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18766)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be 1..65535")
    try:
        data_dir = prepare_data_dir(args.data_dir)
    except ValueError as exc:
        parser.error(str(exc))
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    app = create_app(settings_for_tests(data_dir), http=http, run_scheduler=False)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def fixture_lifespan(application):
        async with http:
            async with original_lifespan(application):
                seed(application.state.ctx)
                yield

    app.router.lifespan_context = fixture_lifespan
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
