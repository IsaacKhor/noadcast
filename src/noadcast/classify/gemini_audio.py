"""Gemini audio classifier: the eval's control arm (plan, Risks #2).

Transcript-only classification discards the strongest ad signal (music
stings, loudness jumps), so this reproduces the iOS app's audio path to
measure what that costs: upload the episode through the Files API resumable
protocol, ask for segments with the app's audio prompt and duration context,
then delete the upload. It is eval-only: every call re-uploads the whole file
and audio input tokens are priced well above text.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import random
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx

from .base import ClassifierError, ClassifyRequest, ClassifyResult
from .core import CallResult, request_sha256, with_repair_nudge
from .gemini import (
    DEFAULT_API_BASE,
    REQUEST_TIMEOUT_S,
    error_for_status,
    exchange_record,
    generate_content_body,
    generate_content_url,
    post_generate_content,
)
from .prompts import AUDIO_INSTRUCTION, audio_duration_context, get_prompt
from .retry import AttemptError, Jitter, RetryPolicy, Sleep, run_with_retry

log = logging.getLogger(__name__)

UPLOAD_CHUNK_BYTES = 1 << 20


@dataclass(frozen=True)
class UploadedFile:
    name: str | None  # "files/abc123"; needed to delete it
    uri: str
    mime_type: str


def file_name_from_uri(uri: str) -> str | None:
    """Port of the app's ``fileName(fromURI:)``."""
    if uri.startswith("files/"):
        return uri
    marker = uri.find("/files/")
    if marker < 0 or not uri[marker + len("/files/") :]:
        return None
    return "files/" + uri[marker + len("/files/") :]


def audio_instruction(episode_duration: float | None) -> str:
    return "\n\n".join(part for part in (AUDIO_INSTRUCTION, audio_duration_context(episode_duration)) if part)


class GeminiAudioClassifier:
    provider = "gemini-audio"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        api_base: str = DEFAULT_API_BASE,
        thinking: str | None = None,
        retry_policy: RetryPolicy = RetryPolicy(),
        sleep: Sleep = asyncio.sleep,
        rand: Jitter = random.uniform,
    ) -> None:
        self.model = model
        self.thinking = thinking
        self.prompt = get_prompt("audio-v1", "audio")
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._url = generate_content_url(api_base, model)
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
        self._retry = partial(run_with_retry, policy=retry_policy, sleep=sleep, rand=rand)

    async def classify(self, req: ClassifyRequest) -> ClassifyResult:
        started = time.monotonic()
        path = Path(req.audio_path) if req.audio_path else None
        if path is None or not path.is_file():
            raise ClassifierError(f"{self.provider} needs the episode audio; got {req.audio_path!r}", permanent=True)
        mime_type = req.audio_content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        instruction = audio_instruction(req.episode_duration)
        audio_sha256 = await asyncio.to_thread(_file_sha256, path)

        upload = await self._retry(lambda _repair: self._upload(path, mime_type))
        uploaded = upload.value
        try:
            outcome = await self._retry(partial(self._generate, uploaded, instruction))
        finally:
            await self._delete(uploaded)

        final = {"attempt": outcome.attempts, **outcome.value.exchange}
        raw: dict[str, Any] = {
            "exchanges": [*outcome.exchanges, final],
            "upload": {"attempts": upload.attempts, "file": uploaded.name, "mime_type": mime_type, "audio_sha256": audio_sha256},
        }
        retries = upload.log + outcome.log
        if retries:
            raw["retries"] = retries
        return ClassifyResult(
            segments=outcome.value.segments,
            usage=outcome.failed_usage + outcome.value.usage,
            provider=self.provider,
            model=self.model,
            thinking=self.thinking,
            prompt_version=self.prompt.version,
            render_format=self.prompt.render_format,
            include_silence=False,
            chunk_count=1,
            latency_ms=round((time.monotonic() - started) * 1000),
            attempts=outcome.attempts,
            request_sha256=request_sha256(
                provider=self.provider,
                model=self.model,
                thinking=self.thinking,
                prompt_version=self.prompt.version,
                render_format=self.prompt.render_format,
                include_silence=False,
                text=f"{instruction}\n{audio_sha256}",
            ),
            raw_response=raw,
        )

    async def _upload(self, path: Path, mime_type: str) -> UploadedFile:
        size = path.stat().st_size
        start = await self._send(
            "POST",
            f"{self._api_base}/upload/v1beta/files",
            headers={
                "x-goog-api-key": self._api_key,
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": mime_type,
            },
            json={"file": {"display_name": path.name}},
        )
        upload_url = start.headers.get("x-goog-upload-url")
        if not upload_url:
            raise AttemptError("Gemini Files API returned no upload URL", kind="transient", status=start.status_code)
        # The session URL authorises the upload itself, so the key is not resent.
        finished = await self._send(
            "POST",
            upload_url,
            headers={"Content-Length": str(size), "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"},
            content=_read_chunks(path),
        )
        try:
            info = finished.json()["file"]
            uri = info["uri"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AttemptError(f"unexpected Files API response: {finished.text[:300]}", kind="transient") from exc
        if info.get("state") == "FAILED":
            raise AttemptError(f"Gemini could not process {path.name}", kind="permanent", status=finished.status_code)
        return UploadedFile(info.get("name") or file_name_from_uri(uri), uri, info.get("mimeType") or mime_type)

    async def _generate(self, uploaded: UploadedFile, instruction: str, repair: bool) -> CallResult:
        parts = [
            {"file_data": {"mime_type": uploaded.mime_type, "file_uri": uploaded.uri}},
            {"text": with_repair_nudge(instruction, repair)},
        ]
        body = generate_content_body(
            system=self.prompt.system, parts=parts, schema=self.prompt.gemini_schema, thinking=self.thinking
        )
        return await post_generate_content(self._client, self._url, self._api_key, body, self.prompt)

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise AttemptError(f"Gemini upload timed out: {exc!r}", kind="transient") from exc
        except httpx.TransportError as exc:
            raise AttemptError(f"Gemini upload connection failed: {exc!r}", kind="transient") from exc
        if not 200 <= response.status_code < 300:
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text[:2000]
            raise error_for_status(response.status_code, response.headers, body, exchange_record(response.status_code, response.headers, body))
        return response

    async def _delete(self, uploaded: UploadedFile) -> None:
        """Best effort: uploads expire after 48 h anyway."""
        if not uploaded.name:
            log.warning("Gemini upload %s has no file name; leaving it to expire", uploaded.uri)
            return
        try:
            response = await self._client.delete(
                f"{self._api_base}/v1beta/{quote(uploaded.name, safe='/')}", headers={"x-goog-api-key": self._api_key}
            )
        except httpx.HTTPError as exc:
            log.warning("could not delete Gemini upload %s: %r", uploaded.name, exc)
            return
        if not 200 <= response.status_code < 300:
            log.warning("could not delete Gemini upload %s: HTTP %d", uploaded.name, response.status_code)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


async def _read_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as handle:
        while chunk := await asyncio.to_thread(handle.read, UPLOAD_CHUNK_BYTES):
            yield chunk


def _file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
