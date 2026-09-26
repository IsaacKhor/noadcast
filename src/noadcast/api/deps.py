"""Request dependencies.

Every dependency and route that touches the database is ``async def``:
FastAPI runs plain ``def`` in a thread pool, and the connection is bound to
the event-loop thread (db/engine.py).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from ..context import AppContext
from ..db import repo
from ..pipeline.commands import NotFound


async def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


def episode_or_404(ctx: AppContext, episode_id: int) -> repo.Episode:
    episode = repo.get_episode(ctx.db, episode_id)
    if episode is None:
        raise NotFound(f"episode {episode_id}")
    return episode


def podcast_or_404(ctx: AppContext, podcast_id: int) -> repo.Podcast:
    podcast = repo.get_podcast(ctx.db, podcast_id)
    if podcast is None:
        raise NotFound(f"podcast {podcast_id}")
    return podcast
