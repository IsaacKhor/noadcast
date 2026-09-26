"""Pipeline stages: download, transcribe, classify (plus the refresh and
evict stages defined next to their domain logic).

Every stage follows the same shape: read, do the slow work (network, pool,
LLM) with no transaction open, then one write that records the result,
enqueues the next step via ``commands.advance`` and completes the job, so a
crash anywhere leaves either the old state or the new one. Stage-specific
failure bookkeeping lives in ``on_failure``, which the scheduler calls in the
same transaction as the job's reschedule-or-fail.
"""

from __future__ import annotations

import asyncio
import dataclasses
import gzip
import json
import logging
import math
import os
import threading
import time
from pathlib import Path

from ..classify import costs, sanitize
from ..classify.base import ClassifyRequest
from ..context import AppContext
from ..db import repo
from ..db.engine import Database, WriteTx
from ..feeds.refresher import RefreshStage
from ..media.downloader import DownloadError, download_audio
from ..media.probe import ProbeError, probe_audio
from ..timeutil import iso, utc_now
from ..transcribe import service
from ..transcribe.codec import decode_words
from ..transcribe.joiner import derive_silences
from ..transcribe.protocol import SilenceRegion, TranscribeProgress, TranscribeTask, TranscriptionError
from . import commands, jobs, states
from .eviction import EvictStage
from .scheduler import RunningJob, Stage

log = logging.getLogger(__name__)

RAW_RESPONSE_DIR = "llm"  # relative to the data dir: llm/<episode_id>/<classification_id>.json.gz


class ProgressReporter:
    """Throttled progress for GET /jobs/active: a write at most every
    ``min_interval`` seconds unless progress moved ``min_fraction`` of the
    total. Progress never bumps the seq, but each write is still a
    transaction. Safe to call from another thread (it hops to the loop)."""

    def __init__(
        self, db: Database, episode_id: int, *, min_interval: float = 2.0, min_fraction: float = 0.05
    ) -> None:
        self._db = db
        self._episode_id = episode_id
        self._min_interval = min_interval
        self._min_fraction = min_fraction
        self._loop = asyncio.get_running_loop()
        self._thread = threading.get_ident()
        self._last_at = -math.inf
        self._last_value: float | None = None

    def __call__(self, current: float, total: float | None) -> None:
        if threading.get_ident() != self._thread:
            self._loop.call_soon_threadsafe(self, current, total)
            return
        now = time.monotonic()
        moved = (
            self._last_value is None
            or (total is not None and total > 0 and abs(current - self._last_value) >= self._min_fraction * total)
        )
        if not moved and now - self._last_at < self._min_interval:
            return
        self._last_at, self._last_value = now, current
        with self._db.write() as tx:
            repo.set_progress(tx, self._episode_id, current=current, total=total, now=iso(utc_now()))


def _load(tx: repo.Reader, episode_id: int) -> tuple[repo.Episode, repo.Podcast] | None:
    episode = repo.get_episode(tx, episode_id)
    if episode is None:
        return None
    podcast = repo.get_podcast(tx, episode.podcast_id)
    return None if podcast is None else (episode, podcast)


def _audio_file(ctx: AppContext, episode: repo.Episode) -> Path | None:
    """The stored file, if the row says present and the disk agrees."""
    if episode.audio_state != "present" or not episode.audio_path:
        return None
    path = ctx.store.abspath(episode.audio_path)
    return path if path.is_file() else None


def _advance_instead(ctx: AppContext, running: RunningJob, *, audio_missing: bool = False) -> None:
    """This job has nothing to do (analysis off, audio or transcript missing):
    let ``advance`` pick the right next step and finish the job with it."""
    job = running.job
    stamp = iso(utc_now())
    with ctx.db.write() as tx:
        loaded = _load(tx, job.subject_id)
        if loaded is not None:
            episode, podcast = loaded
            if audio_missing and episode.audio_state == "present":
                # The row claimed a file the disk no longer has.
                repo.mark_audio_evicted(tx, episode.id, reason="missing", now=stamp)
                episode = repo.get_episode(tx, episode.id) or episode
            commands.advance(
                tx,
                episode,
                podcast=podcast,
                server=repo.load_server_settings(tx, ctx.settings),
                now=stamp,
                priority=job.priority,
                params=job.params,
            )
        jobs.complete(tx, job.id, now=stamp)


def _fail_episode(
    tx: WriteTx, job: jobs.Job, error: str, decision: jobs.FailureDecision, **final_states: str
) -> None:
    """Retry: back to *_pending with the error shown. Final: ``failed``."""
    episode = repo.get_episode(tx, job.subject_id)
    if episode is None:
        return
    stamp = iso(utc_now())
    in_stage = episode.pipeline_state in (states.PENDING_STATE[job.kind], states.RUNNING_STATE[job.kind])
    if decision.retry:
        repo.set_episode_states(
            tx,
            episode.id,
            pipeline_state=states.PENDING_STATE[job.kind] if in_stage else None,
            error=error,
            progress=None,
            now=stamp,
        )
    else:
        repo.set_episode_states(
            tx,
            episode.id,
            pipeline_state="failed" if in_stage else None,
            error=error,
            progress=None,
            now=stamp,
            **final_states,
        )


class DownloadStage:
    kind = states.DOWNLOAD

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def run(self, running: RunningJob) -> None:
        ctx, job = self.ctx, running.job
        loaded = _load(ctx.db, job.subject_id)
        if loaded is None:
            return  # deleted since it was queued
        episode, _ = loaded
        if _audio_file(ctx, episode) is not None:
            _advance_instead(ctx, running)  # a duplicate request; the audio is already here
            return
        relpath = (
            episode.audio_path
            if episode.audio_state == "partial" and episode.audio_path
            else ctx.store.audio_relpath(
                episode.podcast_id,
                episode.id,
                ctx.store.extension_for(episode.enclosure_type, episode.enclosure_url),
            )
        )
        advances_pipeline = episode.pipeline_state in states.DOWNLOAD_IS_PIPELINE_STEP
        with ctx.db.write() as tx:
            repo.begin_download(
                tx,
                episode.id,
                audio_path=relpath,
                pipeline_state="downloading" if advances_pipeline else None,
                total_bytes=episode.enclosure_length,
                now=iso(utc_now()),
            )
        report = ProgressReporter(ctx.db, episode.id)

        def save_validators(etag: str | None, last_modified: str | None) -> None:
            # Recorded as soon as the .part holds these bytes, not only when a
            # transient error reports them, so a crash mid-transfer resumes too.
            # Called synchronously between awaits, so no other write is open.
            with ctx.db.write() as tx:
                repo.set_origin_validators(tx, episode.id, etag=etag, last_modified=last_modified)

        try:
            result = await download_audio(
                ctx.http,
                episode.enclosure_url,
                ctx.store.abspath(relpath),
                etag=episode.origin_etag,
                last_modified=episode.origin_last_modified,
                max_bytes=ctx.settings.max_audio_bytes,
                on_progress=report,
                on_validators=save_validators,
            )
            # A file that does not decode is not audio: permanent (ProbeError).
            probe = await asyncio.to_thread(probe_audio, result.path)
        except (DownloadError, ProbeError) as exc:
            if isinstance(exc, ProbeError) or exc.permanent:
                # Nothing worth resuming; the failure hook records the state.
                await asyncio.to_thread(ctx.store.remove, relpath)
            else:
                # The .part is kept; its validators let the retry resume
                # with If-Range instead of starting over.
                with ctx.db.write() as tx:
                    repo.set_origin_validators(tx, episode.id, etag=exc.etag, last_modified=exc.last_modified)
            raise

        transcript = repo.get_transcript(ctx.db, episode.id)
        # Dynamic ad insertion: different bytes put the ads somewhere else,
        # so a transcript of other bytes (and markers from it) no longer apply.
        rendered_differently = transcript is not None and transcript.audio_sha256 != result.sha256
        stamp = iso(utc_now())
        with ctx.db.write() as tx:
            loaded = _load(tx, episode.id)
            if loaded is None:
                return
            current, podcast = loaded
            if rendered_differently:
                repo.replace_auto_markers(tx, current.id, [], classification_id=None, now=stamp)
                repo.activate_classification(tx, current.id, None)
            repo.mark_audio_present(
                tx,
                current.id,
                repo.StoredAudio(
                    path=relpath,
                    bytes=result.bytes,
                    sha256=result.sha256,
                    # From the probe, never the origin's header (often
                    # application/octet-stream): Range serving and the gzip
                    # bypass both need a real audio/* or video/* type.
                    content_type=probe.content_type,
                    codec=probe.codec,
                    origin_etag=result.etag,
                    origin_last_modified=result.last_modified,
                ),
                # Provisional until transcription decodes the whole file.
                measured_duration_seconds=(
                    transcript.audio_duration_seconds
                    if transcript is not None and not rendered_differently
                    else probe.duration_seconds
                ),
                pipeline_state="downloaded" if advances_pipeline else None,
                transcript_state="stale" if rendered_differently else None,
                classify_state="stale" if rendered_differently and current.classify_state == "ready" else None,
                now=stamp,
            )
            fresh = repo.get_episode(tx, current.id)
            assert fresh is not None
            commands.advance(
                tx,
                fresh,
                podcast=podcast,
                server=repo.load_server_settings(tx, ctx.settings),
                now=stamp,
                priority=job.priority,
                params=job.params,
            )
            jobs.complete(tx, job.id, now=stamp)
        if rendered_differently:
            log.info("re-downloaded audio differs; re-transcribing", extra={"episode_id": episode.id})

    def on_failure(self, tx: WriteTx, job: jobs.Job, error: str, decision: jobs.FailureDecision) -> None:
        episode = repo.get_episode(tx, job.subject_id)
        if episode is None:
            return
        extra: dict[str, str] = {}
        if not decision.retry and decision.reason == "permanent" and episode.audio_state == "partial":
            # run() deleted the partial file; an exhausted transient failure
            # keeps it (state "partial") so a later request can resume.
            extra["audio_state"] = "evicted" if episode.audio_evicted_at else "absent"
        _fail_episode(tx, job, error, decision, **extra)


class TranscribeStage:
    kind = states.TRANSCRIBE

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def run(self, running: RunningJob) -> None:
        ctx, job = self.ctx, running.job
        loaded = _load(ctx.db, job.subject_id)
        if loaded is None:
            return
        episode, podcast = loaded
        if not commands.analysis_enabled(podcast, ctx.server_settings(), job.params):
            _advance_instead(ctx, running)  # switched off after this was queued
            return
        audio = _audio_file(ctx, episode)
        if audio is None:
            _advance_instead(ctx, running, audio_missing=True)  # re-download first
            return
        if ctx.transcriber is None:
            raise TranscriptionError("no transcription pool is attached to this server")
        with ctx.db.write() as tx:
            repo.set_episode_states(
                tx,
                episode.id,
                pipeline_state=states.RUNNING_STATE[self.kind],
                error=None,
                progress=repo.Progress("transcribe", 0.0, episode.duration_seconds),
                now=iso(utc_now()),
            )
        report = ProgressReporter(ctx.db, episode.id)

        def on_progress(progress: TranscribeProgress) -> None:
            report(progress.processed_seconds, progress.total_seconds)

        result = await ctx.transcriber.transcribe(
            TranscribeTask(task_id=f"e{episode.id}-j{job.id}-a{job.attempts}", audio_path=str(audio.resolve())),
            on_progress,
        )
        if not result.words:
            raise TranscriptionError("no words transcribed", permanent=True)
        prepared = await asyncio.to_thread(service.prepare_transcript, result, audio_sha256=episode.audio_sha256)

        stamp = iso(utc_now())
        with ctx.db.write() as tx:
            loaded = _load(tx, episode.id)
            if loaded is None:
                return
            current, podcast = loaded
            service.store_transcript(tx, current.id, prepared, now=stamp)
            repo.set_episode_states(
                tx,
                current.id,
                pipeline_state="transcribed",
                transcript_state="ready",
                # Markers from an older transcript are due for a fresh look.
                classify_state="stale" if current.classify_state == "ready" else None,
                error=None,
                progress=None,
                now=stamp,
            )
            fresh = repo.get_episode(tx, current.id)
            assert fresh is not None
            commands.advance(
                tx,
                fresh,
                podcast=podcast,
                server=repo.load_server_settings(tx, ctx.settings),
                now=stamp,
                priority=job.priority,
                params={key: value for key, value in job.params.items() if key != "retranscribe"},
            )
            jobs.complete(tx, job.id, now=stamp)

    def on_failure(self, tx: WriteTx, job: jobs.Job, error: str, decision: jobs.FailureDecision) -> None:
        episode = repo.get_episode(tx, job.subject_id)
        if episode is None:
            return
        final = {} if decision.retry or episode.transcript_state == "ready" else {"transcript_state": "failed"}
        _fail_episode(tx, job, error, decision, **final)


class ClassifyStage:
    kind = states.CLASSIFY

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def run(self, running: RunningJob) -> None:
        ctx, job = self.ctx, running.job
        loaded = _load(ctx.db, job.subject_id)
        if loaded is None:
            return
        episode, podcast = loaded
        server = ctx.server_settings()
        if not commands.analysis_enabled(podcast, server, job.params):
            _advance_instead(ctx, running)
            return
        transcript = repo.get_transcript(ctx.db, episode.id)
        words_row = repo.get_transcript_words(ctx.db, episode.id)
        sentences = repo.get_sentences(ctx.db, episode.id)
        if episode.transcript_state != "ready" or transcript is None or words_row is None or not sentences:
            _advance_instead(ctx, running)  # transcribe first
            return
        provider = job.params.get("provider") or server.classifier
        audio = _audio_file(ctx, episode)
        if provider in commands.AUDIO_CLASSIFIERS and audio is None:
            # The control arm listens to the file: download it again first; the
            # chain params bring this provider back once the audio is here.
            _advance_instead(ctx, running, audio_missing=True)
            return
        with ctx.db.write() as tx:
            repo.set_episode_states(
                tx,
                episode.id,
                pipeline_state=states.RUNNING_STATE[self.kind],
                error=None,
                progress=repo.Progress("classify", None, None),
                now=iso(utc_now()),
            )
        duration = episode.measured_duration_seconds or transcript.audio_duration_seconds
        request = ClassifyRequest(
            sentences=sentences,
            silences=await asyncio.to_thread(_silences, words_row.blob, duration),
            episode_duration=duration,
            episode_title=episode.title,
            podcast_title=podcast.title,
            language=transcript.language,
            audio_path=str(audio) if audio is not None else None,
            audio_content_type=episode.audio_content_type,
        )
        model = job.params.get("model") or (server.classifier_model if provider == server.classifier else None)
        thinking = job.params.get("thinking") or ctx.settings.thinking_level
        result = await ctx.classifiers.get(provider, model, thinking).classify(request)
        segments = sanitize.finalize(result.segments, request, snap=ctx.settings.snap_to_silence)
        cost = costs.cost_for(result.provider, result.model, result.usage)
        raw = await asyncio.to_thread(
            gzip.compress, json.dumps(result.raw_response, ensure_ascii=False, default=str).encode("utf-8")
        )

        stamp = iso(utc_now())
        with ctx.db.write() as tx:
            if repo.get_episode(tx, episode.id) is None:
                return
            classification = repo.insert_classification(
                tx,
                episode.id,
                repo.NewClassification(
                    provider=result.provider,
                    model=result.model,
                    thinking=result.thinking,
                    prompt_version=result.prompt_version,
                    render_format=result.render_format,
                    include_silence=result.include_silence,
                    joiner_version=transcript.joiner_version,
                    transcript_audio_sha256=transcript.audio_sha256,
                    chunk_count=result.chunk_count,
                    input_tokens=result.usage.input_tokens,
                    thought_tokens=result.usage.thought_tokens,
                    output_tokens=result.usage.output_tokens,
                    cached_input_tokens=result.usage.cached_input_tokens,
                    cache_write_tokens=result.usage.cache_write_tokens,
                    input_cost_usd=cost.input_usd,
                    thought_cost_usd=cost.thought_usd,
                    output_cost_usd=cost.output_usd,
                    total_cost_usd=cost.total_usd,
                    price_table_version=cost.price_table_version,
                    latency_ms=result.latency_ms,
                    attempts=result.attempts,
                    request_sha256=result.request_sha256,
                    raw_segments_json=json.dumps([dataclasses.asdict(s) for s in result.segments]),
                    segments_json=json.dumps([dataclasses.asdict(s) for s in segments]),
                ),
                raw_response_dir=RAW_RESPONSE_DIR,
                now=stamp,
            )
            repo.activate_classification(tx, episode.id, classification.id)
            repo.replace_auto_markers(
                tx,
                episode.id,
                [repo.NewMarker(s.start_seconds, s.end_seconds, s.kind, s.summary) for s in segments],
                classification_id=classification.id,
                now=stamp,
            )
            repo.set_episode_states(
                tx, episode.id, pipeline_state="ready", classify_state="ready", error=None, progress=None, now=stamp
            )
            jobs.complete(tx, job.id, now=stamp)
        assert classification.raw_response_path is not None
        await asyncio.to_thread(_write_atomic, ctx.settings.data_dir / classification.raw_response_path, raw)
        log.info(
            "classified",
            extra={
                "episode_id": episode.id,
                "classification_id": classification.id,
                "provider": result.provider,
                "model": result.model,
                "segments": len(segments),
                "cost_usd": cost.total_usd,
            },
        )

    def on_failure(self, tx: WriteTx, job: jobs.Job, error: str, decision: jobs.FailureDecision) -> None:
        episode = repo.get_episode(tx, job.subject_id)
        if episode is None:
            return
        final = {} if decision.retry or episode.classify_state == "ready" else {"classify_state": "failed"}
        _fail_episode(tx, job, error, decision, **final)


def _silences(words_blob: bytes, duration: float | None) -> list[SilenceRegion]:
    """The silence map from the stored words: ~20 ms of CPU per hour of
    audio, run off the event loop that serves Range requests."""
    return derive_silences(decode_words(words_blob), duration)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def build_stages(ctx: AppContext) -> dict[str, Stage]:
    return {
        states.REFRESH_FEED: RefreshStage(ctx),
        states.DOWNLOAD: DownloadStage(ctx),
        states.TRANSCRIBE: TranscribeStage(ctx),
        states.CLASSIFY: ClassifyStage(ctx),
        states.EVICT: EvictStage(ctx),
    }
