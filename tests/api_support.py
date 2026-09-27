"""Shared harness for the API tests (tests/test_api_*.py). Not a test module.

Each test gets a fresh data directory, an app built with
``run_scheduler=False`` and its lifespan entered, and httpx clients over
``ASGITransport``. Data is seeded straight through ``repo`` inside
``ctx.db.write()``; a recording scheduler handle makes ``ctx.wake()`` and
``ctx.abort()`` observable.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

import httpx

from noadcast.api.app import create_app
from noadcast.config import settings_for_tests
from noadcast.context import AppContext
from noadcast.db import repo
from noadcast.timeutil import now_iso
from noadcast.transcribe import codec
from noadcast.transcribe.protocol import Sentence, Word

REPO_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO_ROOT / ".cache" / "tmp"
API_DOC = REPO_ROOT / "docs" / "API.md"
TOKEN = "t0ken-" + "x" * 40
FAR_FUTURE = "2099-01-01T00:00:00.000Z"
AUDIO_BYTES = b"ID3" + bytes(range(256)) * 64


class FakeRegistry:
    """Stands in for classify.registry.ClassifierRegistry; the API only asks what is configured."""

    def __init__(self, available: dict[str, bool] | None = None) -> None:
        self.available_map = available or {"openrouter": False}

    def get(self, provider: str | None = None, model: str | None = None, thinking: str | None = None) -> Any:
        raise AssertionError("API tests never classify")

    def available(self) -> dict[str, bool]:
        return dict(self.available_map)

    async def aclose(self) -> None:
        pass


@dataclass
class RecordingScheduler:
    """The SchedulerHandle the API talks to after its writes commit."""

    wakes: int = 0
    aborted: list[int] = field(default_factory=list)

    def wake_all(self) -> None:
        self.wakes += 1

    def abort(self, job_ids: Iterable[int]) -> None:
        self.aborted.extend(job_ids)


class ApiTestCase(unittest.IsolatedAsyncioTestCase):
    auth_enabled = False
    settings_overrides: dict[str, Any] = {}

    async def asyncSetUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT, prefix="api-")
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        overrides = dict(self.settings_overrides)
        if self.auth_enabled:
            overrides.update(allow_no_auth=False, api_token=TOKEN)
        self.settings = settings_for_tests(self.data_dir, **overrides)
        self.registry = FakeRegistry()
        # Outbound HTTP (feed fetches) is answered from here; anything else fails the test.
        self.remote: dict[str, Callable[[httpx.Request], Awaitable[httpx.Response]]] = {}
        self.outbound = httpx.AsyncClient(transport=httpx.MockTransport(self._answer_outbound))
        self.addAsyncCleanup(self.outbound.aclose)
        self.app = self.build_app()
        stack = contextlib.AsyncExitStack()
        self.addAsyncCleanup(stack.aclose)
        await stack.enter_async_context(self.app.router.lifespan_context(self.app))
        self.ctx: AppContext = self.app.state.ctx
        self.scheduler = RecordingScheduler()
        self.ctx.scheduler = self.scheduler
        transport = httpx.ASGITransport(app=self.app)
        headers = {"Authorization": f"Bearer {TOKEN}"} if self.auth_enabled else {}
        self.client = await stack.enter_async_context(
            httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers)
        )
        self.anon = await stack.enter_async_context(httpx.AsyncClient(transport=transport, base_url="http://test"))

    async def _answer_outbound(self, request: httpx.Request) -> httpx.Response:
        responder = self.remote.get(str(request.url))
        if responder is None:
            raise AssertionError(f"unexpected outbound request to {request.url}")
        return await responder(request)

    def build_app(self):
        return create_app(self.settings, classifiers=self.registry, http=self.outbound, run_scheduler=False)

    # -- assertions -------------------------------------------------------------

    def assertError(self, response: httpx.Response, status: int, code: str) -> dict[str, Any]:
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(body["error"]["code"], code, body)
        self.assertIsInstance(body["error"]["message"], str)
        return body


# -- seeding -----------------------------------------------------------------------


def add_podcast(ctx: AppContext, *, feed_url: str = "https://feeds.example.com/show.xml", title: str = "Show") -> repo.Podcast:
    with ctx.db.write() as tx:
        return repo.insert_podcast(
            tx,
            feed_url=feed_url,
            title=title,
            auto_process_enabled=True,
            ad_analysis_enabled=True,
            initial_backfill_count=1,
            next_fetch_at=FAR_FUTURE,
            now=now_iso(),
        )


def feed_item(n: int, **overrides: Any) -> repo.FeedItem:
    values: dict[str, Any] = dict(
        guid=f"guid-{n}",
        title=f"Episode {n}",
        description=f"<p>Notes for episode {n}</p>",
        published_at=f"2026-08-{n % 28 + 1:02d}T12:00:00.000Z",
        declared_duration_seconds=3600.0 + n,
        enclosure_url=f"https://cdn.example.com/{n}.mp3",
        enclosure_type="audio/mpeg",
        enclosure_length=None,
        artwork_url=None,
        feed_position=n,
    )
    values.update(overrides)
    return repo.FeedItem(**values)


def add_episodes(ctx: AppContext, podcast_id: int, count: int, *, start: int = 0, **overrides: Any) -> list[int]:
    with ctx.db.write() as tx:
        return repo.insert_episodes(
            tx, podcast_id, [feed_item(start + i, **overrides) for i in range(count)], now=now_iso()
        )


def store_audio(
    ctx: AppContext,
    episode_id: int,
    data: bytes = AUDIO_BYTES,
    *,
    content_type: str = "audio/mpeg",
    **states: str,
) -> Path:
    """Write a file where the store keeps audio and record it as present."""
    episode = repo.get_episode(ctx.db, episode_id)
    assert episode is not None
    relpath = f"audio/{episode.podcast_id}/{episode_id}.mp3"
    path = ctx.store.abspath(relpath)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    audio = repo.StoredAudio(
        path=relpath,
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content_type=content_type,
        codec="mp3",
        origin_etag=None,
        origin_last_modified=None,
    )
    with ctx.db.write() as tx:
        repo.mark_audio_present(tx, episode_id, audio, measured_duration_seconds=3599.5, now=now_iso(), **states)
    return path


def set_markers(ctx: AppContext, episode_id: int, spans: Sequence[tuple[float, float, str, str]]) -> None:
    with ctx.db.write() as tx:
        repo.replace_auto_markers(
            tx,
            episode_id,
            [repo.NewMarker(start, end, kind, summary) for start, end, kind, summary in spans],
            classification_id=None,
            now=now_iso(),
        )


def words_for(text: str, *, start: float = 0.0, step: float = 0.4) -> list[Word]:
    return [
        Word(start=round(start + i * step, 2), end=round(start + i * step + 0.3, 2), word=f" {token}", probability=0.9, segment=0)
        for i, token in enumerate(text.split())
    ]


def store_transcript(ctx: AppContext, episode_id: int, lines: Sequence[str]) -> tuple[list[Sentence], list[Word]]:
    """One sentence per line, words laid out back to back."""
    sentences: list[Sentence] = []
    words: list[Word] = []
    for index, line in enumerate(lines):
        line_words = words_for(line, start=len(words) * 0.4)
        sentences.append(
            Sentence(
                index=index,
                start=line_words[0].start,
                end=line_words[-1].end,
                text=line,
                word_start=len(words),
                word_count=len(line_words),
                break_reason="punct",
                soft_end=False,
                min_p=0.9,
                mean_p=0.9,
                flags=("low_confidence",) if index == 0 else (),
            )
        )
        words.extend(line_words)
    transcript = repo.NewTranscript(
        engine="faster-whisper",
        model_id="Systran/faster-whisper-tiny.en",
        model_sha256=None,
        language="en",
        language_probability=1.0,
        audio_sha256=None,
        audio_duration_seconds=3599.5,
        speech_duration_seconds=3000.0,
        asr_segment_count=1,
        word_count=len(words),
        sentence_count=len(sentences),
        joiner_version=1,
        joiner_params_json="{}",
        asr_options_json="{}",
        decode_seconds=1.0,
        transcribe_seconds=10.0,
    )
    with ctx.db.write() as tx:
        repo.replace_transcript(
            tx,
            episode_id,
            transcript,
            sentences,
            words_codec=codec.CODEC,
            words_blob=codec.encode_words(words),
            segments_json=codec.encode_segments([]),
            now=now_iso(),
        )
        repo.set_episode_states(tx, episode_id, transcript_state="ready", now=now_iso())
    return sentences, words


def add_classification(
    ctx: AppContext,
    episode_id: int,
    *,
    provider: str = "gemini",
    model: str = "gemini-3.5-flash",
    created_at: str | None = None,
    cost: float = 0.01,
    tokens: tuple[int, int, int] = (1000, 100, 50),
    segments: Sequence[dict[str, Any]] = (),
) -> repo.Classification:
    input_tokens, thought_tokens, output_tokens = tokens
    record = repo.NewClassification(
        provider=provider,
        model=model,
        thinking=None,
        prompt_version="segments-v2",
        render_format="index",
        include_silence=True,
        joiner_version=1,
        transcript_audio_sha256=None,
        chunk_count=1,
        input_tokens=input_tokens,
        thought_tokens=thought_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=0,
        cache_write_tokens=0,
        input_cost_usd=cost / 2,
        thought_cost_usd=0.0,
        output_cost_usd=cost / 2,
        total_cost_usd=cost,
        price_table_version="2026-09",
        latency_ms=1234,
        attempts=1,
        request_sha256=None,
        raw_segments_json=json.dumps(list(segments)),
        segments_json=json.dumps(list(segments)),
    )
    with ctx.db.write() as tx:
        return repo.insert_classification(tx, episode_id, record, raw_response_dir="llm", now=created_at or now_iso())


def segment(start: float, end: float, kind: str = "ad", summary: str = "Sponsor read") -> dict[str, Any]:
    """A stored segment as the pipeline writes it: ``dataclasses.asdict(DetectedSegment)``."""
    return {"start_seconds": start, "end_seconds": end, "summary": summary, "kind": kind, "start_line": 1, "end_line": 2}


# -- the contract document ------------------------------------------------------------


def doc_example(heading: str) -> Any:
    """Parse the first ```json block after ``heading`` in docs/API.md."""
    text = API_DOC.read_text(encoding="utf-8")
    start = text.index(heading)
    match = re.search(r"```json\n(.*?)```", text[start:], re.S)
    assert match is not None, heading
    return json.loads(match.group(1))
