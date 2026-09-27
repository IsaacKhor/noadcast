"""Fakes and seeding helpers shared by the server-core tests (repo, sync,
jobs, refresher, stages): a transcriber and classifier registry that answer
instantly, and helpers that put podcasts, episodes, and audio in place."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx

from noadcast.classify.base import ClassifyRequest, ClassifyResult, DetectedSegment, TokenUsage
from noadcast.config import Settings, settings_for_tests
from noadcast.classifier_models import DEFAULT_MODEL, MODEL_IDS
from noadcast.context import AppContext
from noadcast.db import repo
from noadcast.db.engine import Database
from noadcast.media.store import MediaStore
from noadcast.timeutil import now_iso
from noadcast.transcribe.protocol import (
    AsrSegmentMeta,
    ProgressCallback,
    TranscribeProgress,
    TranscribeResult,
    TranscribeTask,
    Word,
)

ROOT = Path(__file__).resolve().parent.parent


def temp_dir(case) -> Path:
    """A per-test directory inside the project cache, removed afterwards."""
    base = ROOT / ".cache" / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="noadcast-test-", dir=base))
    case.addCleanup(shutil.rmtree, path, True)
    return path


def make_settings(data_dir: Path, **overrides: Any) -> Settings:
    return settings_for_tests(data_dir, **overrides)


def make_context(settings: Settings, **overrides: Any) -> AppContext:
    """A context without the HTTP client being used (tests fake the network)."""
    for directory in (settings.data_dir, settings.audio_dir, settings.llm_dir, settings.tmp_dir):
        directory.mkdir(parents=True, exist_ok=True)
    values: dict[str, Any] = {
        "settings": settings,
        "db": Database(settings.db_path),
        "store": MediaStore(settings.data_dir),
        "http": httpx.AsyncClient(),
        "classifiers": FakeRegistry(),
        "transcriber": None,
    }
    values.update(overrides)
    return AppContext(**values)


async def close_context(ctx: AppContext) -> None:
    await ctx.http.aclose()
    ctx.db.close()


# -- seeding --------------------------------------------------------------------


def item(guid: str, *, published_at: str | None = "2026-09-13T20:00:00.000Z", position: int = 0, **fields: Any) -> repo.FeedItem:
    values: dict[str, Any] = {
        "guid": guid,
        "title": f"Episode {guid}",
        "description": None,
        "published_at": published_at,
        "declared_duration_seconds": 3600.0,
        "enclosure_url": f"https://media.example.com/{guid}.mp3",
        "enclosure_type": "audio/mpeg",
        "enclosure_length": None,
        "artwork_url": None,
        "feed_position": position,
    }
    values.update(fields)
    return repo.FeedItem(**values)


def seed_podcast(db: Database, feed_url: str = "https://feeds.example.com/show.xml", **fields: Any) -> repo.Podcast:
    now = now_iso()
    with db.write() as tx:
        return repo.insert_podcast(
            tx,
            feed_url=feed_url,
            title=fields.get("title", "Show"),
            auto_process_enabled=fields.get("auto_process_enabled", True),
            ad_analysis_enabled=fields.get("ad_analysis_enabled", True),
            initial_backfill_count=fields.get("initial_backfill_count", 1),
            next_fetch_at=fields.get("next_fetch_at", "2999-01-01T00:00:00.000Z"),
            now=now,
        )


def seed_episodes(db: Database, podcast_id: int, items: Sequence[repo.FeedItem]) -> list[repo.Episode]:
    with db.write() as tx:
        ids = repo.insert_episodes(tx, podcast_id, items, now=now_iso())
    return repo.episodes_by_ids(db, ids)


def write_audio(ctx: AppContext, episode: repo.Episode, data: bytes = b"ID3" + b"\x00" * 4093) -> repo.StoredAudio:
    """Put a file where the store expects it and return its StoredAudio."""
    relpath = ctx.store.audio_relpath(episode.podcast_id, episode.id, "mp3")
    path = ctx.store.abspath(relpath)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return repo.StoredAudio(
        path=relpath,
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content_type="audio/mpeg",
        codec="mp3",
        origin_etag=None,
        origin_last_modified=None,
    )


def seed_present_audio(ctx: AppContext, episode: repo.Episode, data: bytes | None = None, **states: str) -> repo.Episode:
    stored = write_audio(ctx, episode, data) if data is not None else write_audio(ctx, episode)
    with ctx.db.write() as tx:
        repo.mark_audio_present(tx, episode.id, stored, measured_duration_seconds=3600.0, now=now_iso(), **states)
    fresh = repo.get_episode(ctx.db, episode.id)
    assert fresh is not None
    return fresh


# -- transcription and classification fakes --------------------------------------


def synthetic_words(duration: float = 600.0, *, first: float = 0.7, sentence_every: int = 8) -> list[Word]:
    """Evenly spaced words ending in periods every few words, a 5 s pause every
    40 words, and speech stopping 10 s before the end (trailing music)."""
    words: list[Word] = []
    t = first
    index = 0
    while t < duration - 10.0:
        text = f" word{index}" + ("." if index % sentence_every == sentence_every - 1 else "")
        words.append(Word(start=round(t, 2), end=round(t + 0.3, 2), word=text, probability=0.9, segment=int(t // 30)))
        t += 5.0 if index % 40 == 39 else 0.4
        index += 1
    return words


class FakeTranscriber:
    """Returns canned words; optionally waits on ``gate`` so a test can
    observe (or interrupt) a transcription in flight."""

    def __init__(self, words: Sequence[Word] | None = None, *, duration: float = 600.0) -> None:
        self.words = list(words) if words is not None else synthetic_words(duration)
        self.duration = duration
        self.calls: list[TranscribeTask] = []
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.error: Exception | None = None

    async def transcribe(self, task: TranscribeTask, on_progress: ProgressCallback | None = None) -> TranscribeResult:
        self.calls.append(task)
        self.started.set()
        if on_progress is not None:
            on_progress(TranscribeProgress(task.task_id, self.duration / 2, self.duration))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        segments = sorted({w.segment for w in self.words})
        return TranscribeResult(
            task_id=task.task_id,
            duration_seconds=self.duration,
            duration_after_vad=self.duration - 20.0,
            language="en",
            language_probability=1.0,
            words=list(self.words),
            segments=[AsrSegmentMeta(s, s * 30.0, s * 30.0 + 29.0, 1.5, 0.05, -0.2) for s in segments],
            decode_seconds=0.1,
            transcribe_seconds=0.2,
            engine="fake",
            model_id="fake/tiny.en",
            model_sha256="0" * 64,
            options={"word_timestamps": True},
        )


class ScriptedClassifier:
    """Classifier returning ``segments_for(request)`` (default: an intro from
    the first sentence and an outro over the last)."""

    def __init__(
        self,
        provider: str = "openrouter",
        model: str = DEFAULT_MODEL,
        segments_for: Callable[[ClassifyRequest], list[DetectedSegment]] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.segments_for = segments_for or default_segments
        self.requests: list[ClassifyRequest] = []
        self.error: Exception | None = None

    async def classify(self, req: ClassifyRequest) -> ClassifyResult:
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        segments = self.segments_for(req)
        return ClassifyResult(
            segments=segments,
            usage=TokenUsage(input_tokens=1000, output_tokens=50),
            provider=self.provider,
            model=self.model,
            thinking=None,
            prompt_version="segments-v2",
            render_format="index",
            include_silence=True,
            chunk_count=1,
            latency_ms=5,
            attempts=1,
            request_sha256="f" * 64,
            raw_response={"segments": [dataclasses.asdict(s) for s in segments]},
        )

    async def aclose(self) -> None:
        pass


def default_segments(req: ClassifyRequest) -> list[DetectedSegment]:
    first, last = req.sentences[0], req.sentences[-1]
    return [
        DetectedSegment(first.start, first.end, "Theme and billboard", "intro"),
        DetectedSegment(last.start, last.end, "Credits", "outro"),
    ]


class FakeRegistry:
    """Stands in for ClassifierRegistry: one ScriptedClassifier per provider."""

    def __init__(self) -> None:
        self.classifiers: dict[str, ScriptedClassifier] = {}
        self.requested: list[tuple[str | None, str | None, str | None]] = []

    def get(self, provider: str | None = None, model: str | None = None, thinking: str | None = None) -> ScriptedClassifier:
        self.requested.append((provider, model, thinking))
        name = provider or "openrouter"
        if name != "openrouter" or (model is not None and model not in MODEL_IDS):
            from noadcast.classify.base import ClassifierError
            raise ClassifierError("unsupported classifier selection", permanent=True)
        if name not in self.classifiers:
            self.classifiers[name] = ScriptedClassifier(provider=name, model=model or DEFAULT_MODEL)
        self.classifiers[name].model = model or DEFAULT_MODEL
        return self.classifiers[name]

    def available(self) -> dict[str, bool]:
        return {"openrouter": True}

    async def aclose(self) -> None:
        pass
