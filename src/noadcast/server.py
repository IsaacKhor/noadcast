"""``noadcast serve``: process layout that keeps the transcription pool safe.

Order matters. The pool is built and warmed while no event loop, listening
socket, or thread pool exists yet: its workers are spawned (never forked),
and building it first means there is nothing a child could inherit even by
accident. Only then is the app created and uvicorn started, with exactly one
worker — a second would mean a second pool and a second scheduler racing on
the job table, breaking the sync-cursor invariant. Sockets are bound per
configured address (loopback and the tailnet address), never 0.0.0.0.

Shutdown (SIGTERM from systemd): uvicorn stops accepting and closes
connections, the lifespan drains the scheduler, and only then are the pool's
workers joined — all inside the unit's TimeoutStopSec.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import socket
from typing import Iterator

import uvicorn

from .api.app import create_app
from .config import Settings
from .db.engine import Database
from .logging_setup import configure_logging
from .pipeline.scheduler import SchedulerConfig
from .transcribe.pool import PoolConfig, TranscriptionPool

log = logging.getLogger(__name__)

# Budget within systemd's TimeoutStopSec=90: connections, then jobs, then workers.
HTTP_SHUTDOWN_SECONDS = 10
DRAIN_SECONDS = 40.0
POOL_SHUTDOWN_SECONDS = 30.0


def bind_sockets(hosts: tuple[str, ...], port: int) -> list[socket.socket]:
    sockets: list[socket.socket] = []
    try:
        for host in hosts:
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sockets.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((host, port))
    except OSError:
        for sock in sockets:
            sock.close()
        raise
    return sockets


@contextlib.contextmanager
def _signals_deferred(captured: list[int]) -> Iterator[None]:
    """uvicorn catches SIGINT/SIGTERM for its graceful shutdown, then restores
    the previous handler and re-raises the signal. Under the default
    disposition that re-raise kills the process before the pool is shut
    down, so the previous handler becomes one that only records it; ``serve``
    re-raises it once the workers are joined."""

    def record(signum: int, frame: object) -> None:
        captured.append(signum)

    previous = {signum: signal.signal(signum, record) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def serve(settings: Settings | None = None) -> None:
    settings = settings or Settings.load()
    configure_logging(settings.log_level, settings.log_format)
    Database(settings.db_path).close()  # apply migrations before anything else opens the database
    # Bound before the pool warms up so a bind failure (the tailnet address
    # is not configured yet at boot) costs a restart, not a pool warmup per
    # restart. Safe: workers are spawned, not forked, and sockets are created
    # non-inheritable (PEP 446), so no child ever holds the listener.
    sockets = bind_sockets(settings.hosts, settings.port)
    pool = TranscriptionPool(PoolConfig.from_settings(settings))
    captured: list[int] = []
    try:
        pool.start_blocking()
        app = create_app(
            settings,
            transcriber=pool,
            scheduler_config=SchedulerConfig.from_settings(settings, drain_seconds=DRAIN_SECONDS),
            workers=1,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                workers=1,
                lifespan="on",
                log_config=None,  # keep configure_logging's JSON handler
                log_level=settings.log_level,
                access_log=False,  # the app logs (sampled) access lines itself
                server_header=False,
                timeout_graceful_shutdown=HTTP_SHUTDOWN_SECONDS,
            )
        )
        log.info("serving", extra={"hosts": list(settings.hosts), "port": settings.port})
        with _signals_deferred(captured):
            server.run(sockets=sockets)
    finally:
        pool.shutdown(timeout=POOL_SHUTDOWN_SECONDS)
        for sock in sockets:
            sock.close()
    if captured:
        # Now die of the signal, as uvicorn meant to: systemd counts a SIGTERM
        # death as a clean stop, and a shell sees the usual Ctrl-C exit.
        signal.raise_signal(captured[0])
