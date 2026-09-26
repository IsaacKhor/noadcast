"""The error envelope every non-2xx response uses.

``{"error": {"code": "camelCaseCode", "message": "human text"}}`` plus the
top-level extras docs/API.md documents per endpoint (``jobId`` on the 409
audio responses). Handlers cover the API's own ``ApiError``, Starlette's
routing 404/405, FastAPI validation errors, and the domain exceptions that
routes deliberately let propagate. Uncaught exceptions become a 500 in
``middleware.AccessLogMiddleware``, which knows the request id.
"""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from ..db.repo import CursorExpired
from ..pipeline.commands import NotFound

# Codes for statuses Starlette raises on its own (routing, HTTPException).
_STATUS_CODES = {
    400: "invalidRequest",
    401: "unauthorized",
    403: "forbidden",
    404: "notFound",
    405: "methodNotAllowed",
    413: "payloadTooLarge",
    422: "invalidRequest",
    429: "rateLimited",
    502: "upstreamFailed",
}
_MAX_VALIDATION_ERRORS = 5


class ApiError(Exception):
    """Raised by routes for any documented error response."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = dict(headers or {})
        self.extra = dict(extra or {})


def error_response(
    status: int,
    code: str,
    message: str,
    *,
    headers: Mapping[str, str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> JSONResponse:
    body = {"error": {"code": code, "message": message}, **(extra or {})}
    return JSONResponse(body, status_code=status, headers=dict(headers or {}))


def invalid_request(message: str) -> ApiError:
    return ApiError(422, "invalidRequest", message)


async def _api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(exc.status, exc.code, exc.message, headers=exc.headers, extra=exc.extra)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    status = exc.status_code
    if status == 404:
        message = f"no such resource: {request.url.path}"
    elif status == 405:
        message = f"{request.method} is not allowed on {request.url.path}"
    else:
        message = str(exc.detail)
    return error_response(status, _STATUS_CODES.get(status, "httpError"), message, headers=exc.headers)


def describe_validation_errors(errors: list[dict[str, Any]]) -> str:
    """``"query.limit: Input should be …; body.feedUrl: Field required"``."""
    parts = []
    for error in errors[:_MAX_VALIDATION_ERRORS]:
        where = ".".join(str(part) for part in error.get("loc", ()))
        parts.append(f"{where}: {error.get('msg', 'invalid')}" if where else str(error.get("msg", "invalid")))
    if len(errors) > _MAX_VALIDATION_ERRORS:
        parts.append(f"and {len(errors) - _MAX_VALIDATION_ERRORS} more")
    return "; ".join(parts) or "invalid request"


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return error_response(422, "invalidRequest", describe_validation_errors(list(exc.errors())))


async def _not_found(request: Request, exc: Exception) -> JSONResponse:
    return error_response(404, "notFound", f"{exc} not found")


async def _cursor_expired(request: Request, exc: Exception) -> JSONResponse:
    return error_response(410, "cursorExpired", f"sync cursor expired ({exc}); resync from since=0")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(NotFound, _not_found)
    app.add_exception_handler(CursorExpired, _cursor_expired)
