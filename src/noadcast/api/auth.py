"""Request authentication and signed audio URLs.

Every request except ``/health`` and the fixed web shell/assets must carry ``Authorization: Bearer
<token>``, compared in constant time. The one other credential is a signed
audio URL, ``?exp=<unix>&sig=<hex>`` with ``sig = HMAC-SHA256(signing_secret,
"<episode_id>\\n<exp>")``, accepted only on GET/HEAD of that episode's audio:
AVPlayer has no supported way to attach a header. The raw token is never
read from a query string, where it would persist in logs.

This is ASGI middleware rather than a per-route dependency so that it is
default-deny: a route added later, an unknown path, or a malformed body is
refused before FastAPI routes or parses anything.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import time

from starlette.datastructures import Headers, QueryParams
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import Settings
from .errors import error_response

PUBLIC_PATHS = frozenset({"/health"})
PUBLIC_WEB_PATHS = frozenset({"/", "/web/app.css", "/web/app.js"})
AUDIO_PATH = re.compile(r"/api/v1/episodes/(\d+)/audio")
_SIGNED_METHODS = frozenset({"GET", "HEAD"})
_MAX_EXP_DIGITS = 12


def route_path(scope: Scope) -> str:
    """The path routes are matched against (``root_path`` stripped), as Starlette computes it."""
    path: str = scope["path"]
    root = scope.get("root_path") or ""
    if root and path.startswith(root) and (len(path) == len(root) or path[len(root)] == "/"):
        return path[len(root) :]
    return path


def audio_signature(secret: bytes, episode_id: int, exp: int) -> str:
    return hmac.new(secret, f"{episode_id}\n{exp}".encode(), hashlib.sha256).hexdigest()


def signed_audio_path(settings: Settings, episode_id: int, *, now: float) -> tuple[str, dt.datetime]:
    """Path (with query) of a signed audio URL and the moment it expires."""
    exp = int(now) + settings.audio_url_ttl_seconds
    sig = audio_signature(settings.signing_secret, episode_id, exp)
    return f"/api/v1/episodes/{episode_id}/audio?exp={exp}&sig={sig}", dt.datetime.fromtimestamp(exp, dt.UTC)


def bearer_failure(headers: Headers, token: str | None) -> str | None:
    """Why the Authorization header does not authenticate, or None if it does."""
    header = headers.get("authorization")
    if not header:
        return "missing bearer token"
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return "invalid bearer token"
    if not hmac.compare_digest(presented.strip().encode(), token.encode()):
        return "invalid bearer token"
    return None


def signature_failure(query: QueryParams, secret: bytes, episode_id: int, *, now: float) -> str | None:
    """Why ``?exp&sig`` does not authorise this episode's audio, or None if it does."""
    exps, sigs = query.getlist("exp"), query.getlist("sig")
    if len(exps) != 1 or len(sigs) != 1:
        return "invalid audio URL signature"
    exp_text, sig = exps[0], sigs[0]
    if not exp_text.isascii() or not exp_text.isdigit() or len(exp_text) > _MAX_EXP_DIGITS:
        return "invalid audio URL signature"
    exp = int(exp_text)
    expected = audio_signature(secret, episode_id, exp)
    if not hmac.compare_digest(expected.encode(), sig.encode()):
        return "invalid audio URL signature"
    if exp <= now:
        return "audio URL expired"
    return None


class AuthMiddleware:
    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.settings.auth_enabled:
            await self.app(scope, receive, send)
            return
        path = route_path(scope)
        public = path in PUBLIC_PATHS or (path in PUBLIC_WEB_PATHS and scope["method"] in _SIGNED_METHODS)
        failure = None if public else self._failure(scope, path)
        if failure is None:
            await self.app(scope, receive, send)
            return
        response = error_response(401, "unauthorized", failure, headers={"WWW-Authenticate": "Bearer"})
        await response(scope, receive, send)

    def _failure(self, scope: Scope, path: str) -> str | None:
        failure = bearer_failure(Headers(scope=scope), self.settings.api_token)
        if failure is None:
            return None
        audio = AUDIO_PATH.fullmatch(path)
        query = QueryParams(scope.get("query_string", b""))
        if audio is None or scope["method"] not in _SIGNED_METHODS or ("sig" not in query and "exp" not in query):
            return failure
        return signature_failure(query, self.settings.signing_secret, int(audio.group(1)), now=time.time())
