"""Conditional feed fetching.

One GET per refresh: ``If-None-Match`` / ``If-Modified-Since`` so an
unchanged feed costs a 304, gzip, a streamed size cap (a decompression bomb
is caught on the decoded bytes), and an overall deadline rather than only
httpx's per-read timeout, which a server dripping a byte every few seconds
would never trip.

Redirects are followed here (at most ``MAX_REDIRECTS``) instead of by httpx,
because the refresher must learn whether the feed moved *permanently*. The
error helpers at the bottom are shared with ``media.downloader``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
from dataclasses import dataclass
from typing import Callable

import httpx

from .. import __version__

USER_AGENT = f"Noadcast/{__version__} (+self-hosted)"
MAX_REDIRECTS = 5
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8"

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_PERMANENT_REDIRECTS = frozenset({301, 308})


@dataclass(frozen=True)
class FeedFetch:
    status: int  # 200 or 304
    body: bytes | None  # None on 304
    etag: str | None
    last_modified: str | None
    final_url: str
    permanent_redirect: str | None = None  # new URL if the feed moved (301/308)


class FeedFetchError(Exception):
    def __init__(
        self, message: str, *, status: int | None = None, retry_after: float | None = None, permanent: bool = False
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.permanent = permanent


async def fetch_feed(
    client: httpx.AsyncClient,
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    max_bytes: int = 10 * 1024**2,
    timeout: float = 30.0,
) -> FeedFetch:
    """Fetch ``url``, conditionally when validators are given.

    ``timeout`` bounds the whole fetch, redirects included. On a 304 the
    returned validators are the server's if it re-sent them, else the ones
    passed in. ``permanent_redirect`` is the last URL reached through an
    unbroken run of 301/308 hops from ``url``: a temporary hop anywhere
    before it means the subscription URL is still the right one to poll.

    Raises ``FeedFetchError``: permanent for 4xx other than 408/425/429, an
    unusable URL, or a body over ``max_bytes``; transient (with
    ``retry_after`` from ``Retry-After`` where sent) for 429, 5xx, timeouts,
    connection failures and redirect loops.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": FEED_ACCEPT, "Accept-Encoding": "gzip"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        async with asyncio.timeout(timeout):
            return await _fetch(client, url, headers, max_bytes=max_bytes, timeout=timeout)
    except TimeoutError as exc:
        raise FeedFetchError(f"feed fetch timed out after {timeout:g} s") from exc
    except httpx.TimeoutException as exc:
        raise FeedFetchError(f"feed fetch timed out: {type(exc).__name__}") from exc
    except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
        raise FeedFetchError(f"unusable feed URL: {exc}", permanent=True) from exc
    except httpx.TransportError as exc:
        raise FeedFetchError(f"connection failed: {exc or type(exc).__name__}") from exc
    except httpx.DecodingError as exc:
        raise FeedFetchError(f"undecodable response body: {exc}") from exc


async def _fetch(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], *, max_bytes: int, timeout: float
) -> FeedFetch:
    current = httpx.URL(url)
    moved_to: str | None = None
    all_permanent = True
    for _ in range(MAX_REDIRECTS + 1):
        request = client.build_request("GET", current, headers=headers, timeout=timeout)
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            location = response.headers.get("location")
            if response.status_code in _REDIRECTS and location:
                current = current.join(location.strip())
                all_permanent = all_permanent and response.status_code in _PERMANENT_REDIRECTS
                if all_permanent:
                    moved_to = str(current)
                continue
            return await _read(response, headers, max_bytes=max_bytes, moved_to=moved_to)
        finally:
            await response.aclose()
    raise FeedFetchError(f"more than {MAX_REDIRECTS} redirects")


async def _read(response: httpx.Response, sent: dict[str, str], *, max_bytes: int, moved_to: str | None) -> FeedFetch:
    status = response.status_code
    final_url = str(response.url)
    if status == 304:
        return FeedFetch(
            status=304,
            body=None,
            etag=response.headers.get("etag") or sent.get("If-None-Match"),
            last_modified=response.headers.get("last-modified") or sent.get("If-Modified-Since"),
            final_url=final_url,
            permanent_redirect=moved_to,
        )
    if not 200 <= status < 300:
        raise status_error(response, FeedFetchError)
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise FeedFetchError(f"feed is {declared} bytes; the limit is {max_bytes}", status=status, permanent=True)
    body = bytearray()
    async for chunk in response.aiter_bytes():  # decoded, so the cap also covers gzip bombs
        body += chunk
        if len(body) > max_bytes:
            raise FeedFetchError(f"feed exceeds the {max_bytes}-byte limit", status=status, permanent=True)
    return FeedFetch(
        status=200,
        body=bytes(body),
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
        final_url=final_url,
        permanent_redirect=moved_to,
    )


# -- shared with media.downloader ------------------------------------------------


def parse_retry_after(value: str | None, *, now: dt.datetime | None = None) -> float | None:
    """Seconds to wait per a ``Retry-After`` header: delta-seconds or an
    HTTP-date (RFC 9110 §10.2.3). A date in the past means 0; unparseable
    means None. Not capped: callers honour the host's wish exactly."""
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        return float(int(text)) if len(text) <= 12 else None
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return max(0.0, (when - (now or dt.datetime.now(dt.UTC))).total_seconds())


def is_permanent_status(status: int) -> bool:
    """A 4xx means repeating the same request cannot succeed — except the
    explicitly retryable request timeout, too-early and rate-limit codes."""
    return 400 <= status < 500 and status not in (408, 425, 429)


def status_error[E: Exception](response: httpx.Response, error: Callable[..., E]) -> E:
    """``error`` (FeedFetchError or DownloadError) for a response that is
    neither a success nor a redirect."""
    status = response.status_code
    reason = f"HTTP {status} {response.reason_phrase}".rstrip()
    return error(
        reason,
        status=status,
        retry_after=parse_retry_after(response.headers.get("retry-after")),
        permanent=is_permanent_status(status),
    )
