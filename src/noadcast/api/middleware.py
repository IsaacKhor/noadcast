"""Access logging with request ids, and gzip that never touches audio.

Both are pure ASGI rather than ``BaseHTTPMiddleware``: audio responses
stream for as long as an episode plays, and ``BaseHTTPMiddleware`` would
relay every chunk through an extra task and memory stream.
"""

from __future__ import annotations

import logging
import random
import secrets
import time

from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..logging_setup import log_context
from .auth import AUDIO_PATH, route_path
from .errors import error_response

access_log = logging.getLogger("noadcast.api.access")
error_log = logging.getLogger("noadcast.api")

# One episode played is thousands of Range requests; log a sample of the
# successful ones. Failures are rare and always logged.
AUDIO_LOG_SAMPLE_RATE = 0.01


def _is_audio(scope: Scope) -> bool:
    return scope["method"] in ("GET", "HEAD") and AUDIO_PATH.fullmatch(route_path(scope)) is not None


def _sampled_out() -> bool:
    return random.random() >= AUDIO_LOG_SAMPLE_RATE


class AccessLogMiddleware:
    """Outermost layer: gives each request a correlation id (``X-Request-Id``
    and the ``request_id`` log field), turns uncaught exceptions into the
    500 envelope without leaking a traceback, and writes one access line.
    The query string is never logged: it carries audio URL signatures."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = secrets.token_hex(6)
        began = time.perf_counter()
        status = 500
        response_started = False

        async def send_with_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                MutableHeaders(scope=message).append("X-Request-Id", request_id)
            await send(message)

        with log_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            except Exception:
                error_log.exception(
                    "unhandled error", extra={"method": scope["method"], "path": route_path(scope)}
                )
                if response_started:
                    raise
                response = error_response(500, "internal", f"internal server error (request {request_id})")
                await response(scope, receive, send_with_id)
            finally:
                self._log(scope, status, time.perf_counter() - began)

    @staticmethod
    def _log(scope: Scope, status: int, elapsed: float) -> None:
        audio = _is_audio(scope)
        if audio and status < 400 and _sampled_out():
            return
        path = route_path(scope)
        duration_ms = round(elapsed * 1000, 1)
        extra: dict[str, object] = {
            "method": scope["method"],
            "path": path,
            "status": status,
            "duration_ms": duration_ms,
        }
        if audio and status < 400:
            extra["sample_rate"] = AUDIO_LOG_SAMPLE_RATE
        access_log.info("%s %s %d %.1fms", scope["method"], path, status, duration_ms, extra=extra)


class GZipExceptAudio:
    """gzip for JSON; audio bypasses compression entirely. Starlette already
    skips ``audio/*`` and 206 responses, but a content-encoded audio body
    would break Range arithmetic whatever type the file was stored with."""

    def __init__(self, app: ASGIApp, *, minimum_size: int = 1024, compresslevel: int = 6) -> None:
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and AUDIO_PATH.fullmatch(route_path(scope)):
            await self.app(scope, receive, send)
        else:
            await self.gzip(scope, receive, send)
