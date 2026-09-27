"""The server's service graph.

``AppContext`` bundles everything a request handler or pipeline stage
needs. It is built on — and must only be used from — the event-loop thread,
because the ``Database`` connection is thread-affine (db/engine.py).
``open_context`` owns construction and teardown; the scheduler is attached
by ``pipeline.scheduler.run_pipeline``, and the transcription pool is built
before the event loop exists (server.py) and only attached here.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import socket
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterable, Protocol

import httpx

from .classify.registry import ClassifierRegistry
from .config import Settings
from .db import repo
from .db.engine import Database
from .feeds.fetcher import USER_AGENT
from .media.store import MediaStore
from .timeutil import iso, utc_now
from .transcribe.protocol import Transcriber


class SchedulerHandle(Protocol):
    def wake_all(self) -> None:
        """New jobs were committed; let idle workers look now instead of at the next poll."""

    def abort(self, job_ids: Iterable[int]) -> None:
        """Stop this process's runners for jobs that were just canceled."""


@dataclass
class AppContext:
    settings: Settings
    db: Database
    store: MediaStore
    http: httpx.AsyncClient
    classifiers: ClassifierRegistry
    transcriber: Transcriber | None
    # Lease owner id: unique per process lifetime, so a lease can be told
    # apart from one held by a previous (crashed) run of the server.
    owner: str = field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}")
    started_at: dt.datetime = field(default_factory=utc_now)
    scheduler: SchedulerHandle | None = None

    def wake(self) -> None:
        if self.scheduler is not None:
            self.scheduler.wake_all()

    def abort(self, job_ids: Iterable[int]) -> None:
        if self.scheduler is not None:
            self.scheduler.abort(job_ids)

    def server_settings(self) -> repo.ServerSettings:
        return repo.load_server_settings(self.db, self.settings)


def build_http_client() -> httpx.AsyncClient:
    """Shared by feed fetches and audio downloads. Per-request timeouts and
    size caps are applied by feeds.fetcher and media.downloader."""
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=60.0),
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
    )


@contextlib.asynccontextmanager
async def open_context(
    settings: Settings,
    *,
    transcriber: Transcriber | None = None,
    classifiers: ClassifierRegistry | None = None,
    http: httpx.AsyncClient | None = None,
) -> AsyncIterator[AppContext]:
    """Open the database (applying migrations) and the shared clients.
    Injected ``classifiers``/``http`` are closed by their owner, not here."""
    for directory in (settings.data_dir, settings.audio_dir, settings.llm_dir, settings.tmp_dir):
        directory.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    with db.write() as tx:
        repo.normalize_classifier_settings(tx, settings, now=iso(utc_now()))
    own_http = http is None
    own_classifiers = classifiers is None
    client = build_http_client() if http is None else http
    registry = ClassifierRegistry(settings) if classifiers is None else classifiers
    try:
        yield AppContext(
            settings=settings,
            db=db,
            store=MediaStore(settings.data_dir),
            http=client,
            classifiers=registry,
            transcriber=transcriber,
        )
    finally:
        try:
            if own_classifiers:
                await registry.aclose()
            if own_http:
                await client.aclose()
        finally:
            db.close()
