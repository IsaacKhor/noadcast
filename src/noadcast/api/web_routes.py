"""Small, fixed web UI assets. The API remains bearer authenticated."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from starlette.responses import FileResponse

router = APIRouter()
_ASSETS = Path(__file__).resolve().parent / "web"
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@router.api_route("/", methods=["GET", "HEAD"])
async def index() -> FileResponse:
    return FileResponse(_ASSETS / "index.html", media_type="text/html", headers=_SECURITY_HEADERS)


@router.api_route("/web/app.css", methods=["GET", "HEAD"])
async def style() -> FileResponse:
    return FileResponse(_ASSETS / "app.css", media_type="text/css", headers=_SECURITY_HEADERS)


@router.api_route("/web/app.js", methods=["GET", "HEAD"])
async def script() -> FileResponse:
    return FileResponse(_ASSETS / "app.js", media_type="text/javascript", headers=_SECURITY_HEADERS)
