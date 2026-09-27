"""The FastAPI application.

``create_app`` builds the app; its lifespan opens the service graph and,
normally, the pipeline scheduler. The runtime ``Database`` is opened inside
the lifespan, on the event-loop thread that will serve every request,
because the sqlite connection is thread-affine. The transcription pool is
never created here: server.py builds it before the event loop exists (so
no worker is forked from a process holding a loop and a listening socket)
and passes it in as ``transcriber``.
"""

from __future__ import annotations

import contextlib
import os
from typing import AsyncIterator

import httpx
from fastapi import FastAPI

from .. import __version__
from ..classify.registry import ClassifierRegistry
from ..config import Settings
from ..context import open_context
from ..pipeline.scheduler import SchedulerConfig, run_pipeline
from ..pipeline.stages import build_stages
from ..transcribe.protocol import Transcriber
from .auth import AuthMiddleware
from .errors import install_error_handlers
from .middleware import AccessLogMiddleware, GZipExceptAudio
from .routers import admin, audio, episodes, health, jobs, podcasts, sync, transcripts
from .routers import settings as settings_routes
from . import web_routes

API_PREFIX = "/api/v1"


def _require_single_process(workers: int) -> None:
    """Several workers would mean several schedulers racing on the job table
    and several sqlite writers, which breaks the sync cursor: a reader could
    see seq N+1 committed before N and skip a row forever."""
    raw = os.environ.get("WEB_CONCURRENCY", "").strip()
    try:
        env_workers = int(raw) if raw else 1
    except ValueError as exc:
        raise ValueError(f"WEB_CONCURRENCY must be an integer, got {raw!r}") from exc
    if workers != 1 or env_workers > 1:
        raise ValueError("noadcast serves from exactly one process: run with workers=1 (and WEB_CONCURRENCY unset)")


def create_app(
    settings: Settings,
    *,
    transcriber: Transcriber | None = None,
    classifiers: ClassifierRegistry | None = None,
    http: httpx.AsyncClient | None = None,
    scheduler_config: SchedulerConfig | None = None,
    run_scheduler: bool = True,
    workers: int = 1,
) -> FastAPI:
    """``run_scheduler=False`` serves the API without running jobs (tests;
    the CLI's jobs are then picked up by whichever server runs them)."""
    _require_single_process(workers)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_context(settings, transcriber=transcriber, classifiers=classifiers, http=http) as ctx:
            app.state.ctx = ctx
            app.state.audio_access = audio.AccessThrottle()
            if not run_scheduler:
                yield
                return
            async with run_pipeline(ctx, build_stages(ctx), scheduler_config):
                yield

    app = FastAPI(
        title="Noadcast",
        version=__version__,
        lifespan=lifespan,
        # Only /health and the fixed web shell/assets are unauthenticated.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    install_error_handlers(app)
    app.include_router(health.public_router)
    app.include_router(web_routes.router)
    for module in (health, sync, jobs, podcasts, episodes, transcripts, audio, settings_routes, admin):
        app.include_router(module.router, prefix=API_PREFIX)
    # Added innermost first: requests pass access log -> auth -> gzip -> routes.
    app.add_middleware(GZipExceptAudio)
    app.add_middleware(AuthMiddleware, settings=settings)
    app.add_middleware(AccessLogMiddleware)
    return app
