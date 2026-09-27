"""Pipeline stages (download → transcribe → classify), the scheduler that
runs them, and audio retention — with the network, pool, and LLM faked."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import gzip
import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from noadcast.classify import costs
from noadcast.classify.base import ClassifierError, DetectedSegment
from noadcast.db import repo
from noadcast.media.downloader import DownloadError, DownloadResult, part_path
from noadcast.media.probe import AudioProbe, ProbeError
from noadcast.media.store import DiskUsage
from noadcast.pipeline import commands, eviction, jobs, stages, states
from noadcast.pipeline.scheduler import RunningJob, Scheduler, SchedulerConfig, run_pipeline
from noadcast.timeutil import iso, now_iso, utc_now
from noadcast.transcribe.codec import decode_words

from tests.pipeline_support import (
    FakeTranscriber,
    close_context,
    item,
    make_context,
    make_settings,
    seed_episodes,
    seed_podcast,
    seed_present_audio,
    temp_dir,
)

AUDIO = b"ID3" + bytes(range(256)) * 64


class FakeOrigin:
    """Stands in for media.downloader.download_audio and media.probe.probe_audio."""

    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}
        self.errors: list[Exception] = []
        self.calls: list[str] = []
        self.validators: list[str | None] = []
        self.gate: asyncio.Event | None = None
        self.in_flight = 0
        self.peak = 0
        self.probe_error: Exception | None = None
        self.crash_after_validators = False

    async def download(
        self, client, url, dest: Path, *, etag=None, last_modified=None, max_bytes=0, on_progress=None, on_validators=None
    ):
        self.calls.append(url)
        self.validators.append(etag)
        if self.crash_after_validators:
            # The process dies mid-transfer: the part holds bytes and the
            # validators were reported, but no DownloadError is ever raised.
            self.crash_after_validators = False
            on_validators('"crash-v1"', None)
            part = part_path(dest)
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(b"partial")
            raise asyncio.CancelledError
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.errors:
                part = part_path(dest)
                part.parent.mkdir(parents=True, exist_ok=True)
                part.write_bytes(b"partial")
                raise self.errors.pop(0)
            body = self.bodies.get(url, AUDIO)
            if on_progress is not None:
                on_progress(len(body) // 2, len(body))
                on_progress(len(body), len(body))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
            return DownloadResult(
                path=dest,
                bytes=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                content_type="audio/mpeg",
                etag='"origin"',
                last_modified=None,
                resumed=False,
                final_url=url,
            )
        finally:
            self.in_flight -= 1

    def probe(self, path: Path) -> AudioProbe:
        if self.probe_error is not None:
            raise self.probe_error
        return AudioProbe(duration_seconds=601.5, codec="mp3", container="mp3", content_type="audio/mpeg", bit_rate=128000)


def free_costs(provider: str, model: str, usage) -> costs.CostBreakdown:
    return costs.CostBreakdown(0.001, 0.0, 0.002, 0.003)


class StageTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = make_settings(temp_dir(self))
        self.transcriber = FakeTranscriber(duration=600.0)
        self.ctx = make_context(self.settings, transcriber=self.transcriber)
        self.addAsyncCleanup(close_context, self.ctx)
        self.origin = FakeOrigin()
        for target, fake in (
            ("noadcast.pipeline.stages.download_audio", self.origin.download),
            ("noadcast.pipeline.stages.probe_audio", self.origin.probe),
            ("noadcast.classify.costs.cost_for", free_costs),
        ):
            patcher = mock.patch(target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stages = stages.build_stages(self.ctx)
        self.podcast = seed_podcast(self.ctx.db)
        (self.episode,) = seed_episodes(self.ctx.db, self.podcast.id, [item("ep1")])

    def fresh(self, episode_id: int | None = None) -> repo.Episode:
        found = repo.get_episode(self.ctx.db, episode_id or self.episode.id)
        assert found is not None
        return found

    def process(self, episode_id: int | None = None, **kwargs) -> int | None:
        with self.ctx.db.write() as tx:
            return commands.process_episode(
                tx, episode_id or self.episode.id, server=self.ctx.server_settings(), now=now_iso(), **kwargs
            )

    async def run_next(self, kind: str) -> jobs.Job:
        """Claim the next job of ``kind`` and run its stage the way the scheduler would."""
        with self.ctx.db.write() as tx:
            job = jobs.claim(tx, kind, owner=self.ctx.owner, lease_seconds=60, now=utc_now())
        self.assertIsNotNone(job, f"no {kind} job queued")
        running = RunningJob(job, self.ctx.owner, 60)
        stage = self.stages[kind]
        try:
            await stage.run(running)
        except Exception as exc:
            decision = jobs.decide_failure(job, exc, now=utc_now())
            with self.ctx.db.write() as tx:
                if decision.retry:
                    jobs.reschedule(tx, job.id, error=str(exc), available_at=decision.available_at, now=now_iso())
                else:
                    jobs.fail(tx, job.id, error=str(exc), now=now_iso())
                stage.on_failure(tx, job, str(exc), decision)
        else:
            with self.ctx.db.write() as tx:
                jobs.complete(tx, job.id, now=now_iso())
        final = jobs.get_job(self.ctx.db, job.id)
        assert final is not None
        return final

    async def run_chain(self) -> None:
        for kind in ("download", "transcribe", "classify"):
            if jobs.live_job(self.ctx.db, kind, self.episode.id):
                await self.run_next(kind)


class DownloadTranscribeClassifyTests(StageTestCase):
    async def test_played_stop_intent_blocks_stale_queued_download(self) -> None:
        await eviction.release_audio(self.ctx, self.episode.id, reason="played")
        with self.ctx.db.write() as tx:
            stale_id = jobs.enqueue(tx, "download", self.episode.id, now=now_iso()).job_id
        stale = await self.run_next("download")
        self.assertEqual((stale.id, stale.state), (stale_id, "canceled"))
        self.assertEqual(self.origin.calls, [])
        self.assertEqual(self.fresh().release_reason, "played")
        fresh_id = self.process()
        self.assertNotEqual(fresh_id, stale_id)
        self.assertIsNone(self.fresh().release_reason)

    async def test_the_happy_path_end_to_end(self) -> None:
        self.process()
        job = await self.run_next("download")
        self.assertEqual(job.state, "done")
        downloaded = self.fresh()
        self.assertEqual(
            (downloaded.audio_state, downloaded.audio_bytes, downloaded.audio_sha256, downloaded.audio_codec),
            ("present", len(AUDIO), hashlib.sha256(AUDIO).hexdigest(), "mp3"),
        )
        self.assertEqual(downloaded.measured_duration_seconds, 601.5, "provisional duration from the probe")
        self.assertEqual(downloaded.pipeline_state, "transcribe_pending")
        self.assertEqual(downloaded.origin_etag, '"origin"')
        self.assertTrue(self.ctx.store.abspath(downloaded.audio_path).is_file())

        await self.run_next("transcribe")
        transcribed = self.fresh()
        self.assertEqual((transcribed.transcript_state, transcribed.pipeline_state), ("ready", "classify_pending"))
        self.assertEqual(transcribed.measured_duration_seconds, 600.0, "the decoded length wins")
        transcript = repo.get_transcript(self.ctx.db, self.episode.id)
        self.assertEqual((transcript.audio_sha256, transcript.word_count), (downloaded.audio_sha256, len(self.transcriber.words)))
        words = decode_words(repo.get_transcript_words(self.ctx.db, self.episode.id).blob)
        self.assertEqual(len(words), len(self.transcriber.words))
        sentences = repo.get_sentences(self.ctx.db, self.episode.id)
        self.assertEqual(len(sentences), transcript.sentence_count)
        self.assertTrue(self.transcriber.calls[0].audio_path.endswith(".mp3"))

        await self.run_next("classify")
        ready = self.fresh()
        self.assertEqual((ready.pipeline_state, ready.classify_state, ready.marker_revision), ("ready", "ready", 1))
        markers = repo.markers_for_episode(self.ctx.db, self.episode.id)
        self.assertEqual([m.kind for m in markers], ["intro", "outro"])
        self.assertEqual([m.summary for m in markers], ["Theme and billboard", "Credits"])
        self.assertEqual(markers[0].start_seconds, 0.0, "an intro starts at the top of the audio")
        self.assertEqual(markers[-1].end_seconds, 600.0, "the outro runs to the measured end")
        (classification,) = repo.list_classifications(self.ctx.db, self.episode.id)
        self.assertTrue(classification.is_active)
        self.assertEqual([s["summary"] for s in json.loads(classification.segments_json)],
                         [m.summary for m in markers])
        self.assertEqual((classification.total_cost_usd, classification.input_tokens), (0.003, 1000))
        raw = json.loads(gzip.decompress((self.settings.data_dir / classification.raw_response_path).read_bytes()))
        self.assertIn("segments", raw)
        request = self.ctx.classifiers.classifiers["openrouter"].requests[0]
        self.assertEqual(request.silences[-1].kind, "tail")
        self.assertEqual(request.episode_duration, 600.0)

    async def test_reclassification_keeps_history_and_manual_markers(self) -> None:
        self.process()
        await self.run_chain()
        stamp = now_iso()
        with self.ctx.db.write() as tx:
            tx.execute(
                "INSERT INTO ad_markers (episode_id, start_seconds, end_seconds, kind, summary, source, created_at, updated_at)"
                " VALUES (?, 200, 230, 'ad', 'mine', 'manual', ?, ?)",
                (self.episode.id, stamp, stamp),
            )
            job_id = commands.reanalyze_episode(
                tx, self.episode.id, server=self.ctx.server_settings(), now=stamp, provider="openrouter", model="qwen/qwen3.8-flash"
            )
        self.assertEqual(jobs.get_job(self.ctx.db, job_id).kind, "classify")
        await self.run_next("classify")
        history = repo.list_classifications(self.ctx.db, self.episode.id)
        self.assertEqual([(c.model, c.is_active) for c in history], [("qwen/qwen3.8-flash", True), ("deepseek/deepseek-v4.1-flash", False)])
        self.assertEqual(self.ctx.classifiers.requested[-1][:2], ("openrouter", "qwen/qwen3.8-flash"))
        sources = sorted(m.source for m in repo.markers_for_episode(self.ctx.db, self.episode.id))
        self.assertEqual(sources, ["auto", "auto", "manual"])
        self.assertEqual(self.fresh().marker_revision, 2)

    async def test_different_bytes_on_redownload_invalidate_markers(self) -> None:
        self.process()
        await self.run_chain()
        path = self.fresh().audio_path
        await eviction.release_audio(self.ctx, self.episode.id, reason="played")
        self.assertFalse(self.ctx.store.abspath(path).exists())
        self.assertEqual(len(repo.markers_for_episode(self.ctx.db, self.episode.id)), 2, "eviction keeps markers")
        self.origin.bodies[self.episode.enclosure_url] = AUDIO + b"dynamic ad"
        self.process()
        await self.run_next("download")
        stale = self.fresh()
        self.assertEqual((stale.transcript_state, stale.classify_state, stale.pipeline_state), ("stale", "stale", "transcribe_pending"))
        self.assertEqual(repo.markers_for_episode(self.ctx.db, self.episode.id), [], "markers for other bytes are gone")
        self.assertFalse(any(c.is_active for c in repo.list_classifications(self.ctx.db, self.episode.id)))
        await self.run_next("transcribe")
        await self.run_next("classify")
        final = self.fresh()
        self.assertEqual((final.pipeline_state, final.classify_state, final.active_marker_count), ("ready", "ready", 2))
        self.assertEqual(repo.get_transcript(self.ctx.db, self.episode.id).audio_sha256, final.audio_sha256)

    async def test_same_bytes_on_redownload_keep_everything(self) -> None:
        self.process()
        await self.run_chain()
        await eviction.release_audio(self.ctx, self.episode.id, reason="manual")
        self.assertEqual(self.fresh().audio_evicted_reason, "manual")
        self.process()
        self.assertEqual(self.fresh().pipeline_state, "ready", "a re-download only moves audio_state")
        await self.run_next("download")
        again = self.fresh()
        self.assertEqual((again.audio_state, again.pipeline_state, again.active_marker_count), ("present", "ready", 2))
        self.assertEqual(again.measured_duration_seconds, 600.0, "the transcript's decoded length is kept")
        self.assertIsNone(jobs.live_stage_job(self.ctx.db, self.episode.id))

    async def test_analysis_off_finishes_after_download(self) -> None:
        with self.ctx.db.write() as tx:
            repo.update_server_settings(tx, self.settings, {"ad_analysis_enabled": False}, now=now_iso())
        self.process()
        await self.run_next("download")
        done = self.fresh()
        self.assertEqual((done.pipeline_state, done.classify_state, done.transcript_state), ("ready", "skipped", "none"))
        self.assertIsNone(jobs.live_stage_job(self.ctx.db, self.episode.id))

    async def test_analysis_switched_off_while_queued_skips_the_stage(self) -> None:
        self.process()
        await self.run_next("download")
        with self.ctx.db.write() as tx:
            repo.set_podcast_switches(tx, self.podcast, auto_process_enabled=None, ad_analysis_enabled=False, now=now_iso())
        job = await self.run_next("transcribe")
        self.assertEqual(job.state, "done")
        self.assertEqual(self.transcriber.calls, [])
        self.assertEqual((self.fresh().pipeline_state, self.fresh().classify_state), ("ready", "skipped"))

    async def test_transient_download_failure_retries_with_the_part_kept(self) -> None:
        self.origin.errors.append(DownloadError("connection reset", etag='"part-v1"'))
        self.process()
        job = await self.run_next("download")
        self.assertEqual((job.state, job.last_error), ("pending", "connection reset"))
        self.assertGreater(job.available_at, now_iso())
        episode = self.fresh()
        self.assertEqual((episode.pipeline_state, episode.pipeline_error, episode.audio_state), ("download_pending", "connection reset", "partial"))
        self.assertTrue(part_path(self.ctx.store.abspath(episode.audio_path)).exists())
        self.assertEqual(episode.origin_etag, '"part-v1"', "kept so the retry can resume")
        with self.ctx.db.write() as tx:
            jobs.make_available_now(tx, job.id, now=now_iso())
        await self.run_next("download")
        self.assertEqual(self.origin.validators, [None, '"part-v1"'])
        self.assertEqual((self.fresh().audio_state, self.fresh().origin_etag), ("present", '"origin"'))

    async def test_crash_mid_download_resumes_after_recovery(self) -> None:
        self.origin.crash_after_validators = True
        self.process()
        with self.assertRaises(asyncio.CancelledError):
            await self.run_next("download")
        episode = self.fresh()
        self.assertEqual((episode.audio_state, episode.origin_etag), ("partial", '"crash-v1"'), "recorded before the crash")
        jobs.recover_leases(self.ctx.db, self.ctx.store)
        self.assertTrue(part_path(self.ctx.store.abspath(self.fresh().audio_path)).exists(), "recovery keeps the part")
        await self.run_next("download")
        self.assertEqual(self.origin.validators, [None, '"crash-v1"'], "the retry resumes with If-Range")
        self.assertEqual(self.fresh().audio_state, "present")

    async def test_permanent_download_failure_fails_and_cleans_up(self) -> None:
        self.origin.errors.append(DownloadError("HTTP 410", status=410, permanent=True))
        self.process()
        job = await self.run_next("download")
        self.assertEqual(job.state, "failed")
        episode = self.fresh()
        self.assertEqual((episode.pipeline_state, episode.audio_state), ("failed", "absent"))
        self.assertFalse(part_path(self.ctx.store.abspath(episode.audio_path)).exists())

    async def test_undecodable_file_is_a_permanent_failure(self) -> None:
        self.origin.probe_error = ProbeError("not audio")
        self.process()
        job = await self.run_next("download")
        self.assertEqual(job.state, "failed")
        episode = self.fresh()
        self.assertEqual((episode.pipeline_state, episode.pipeline_error, episode.audio_state), ("failed", "not audio", "absent"))
        self.assertFalse(self.ctx.store.abspath(episode.audio_path).exists())

    async def test_transcription_without_its_file_downloads_again(self) -> None:
        self.process()
        await self.run_next("download")
        self.ctx.store.abspath(self.fresh().audio_path).unlink()
        job = await self.run_next("transcribe")
        self.assertEqual(job.state, "done")
        episode = self.fresh()
        self.assertEqual((episode.audio_state, episode.audio_evicted_reason), ("evicted", "missing"))
        self.assertIsNotNone(jobs.live_job(self.ctx.db, "download", self.episode.id))
        await self.run_chain()
        self.assertEqual(self.fresh().pipeline_state, "ready")

    async def test_reclassifying_an_evicted_episode_needs_no_download(self) -> None:
        self.process()
        await self.run_chain()
        await eviction.release_audio(self.ctx, self.episode.id, reason="played")
        with self.ctx.db.write() as tx:
            job_id = commands.reanalyze_episode(tx, self.episode.id, server=self.ctx.server_settings(), now=now_iso())
        self.assertEqual(jobs.get_job(self.ctx.db, job_id).kind, "classify", "the stored transcript is enough")
        await self.run_next("classify")
        final = self.fresh()
        self.assertEqual((final.audio_state, final.pipeline_state, final.marker_revision), ("evicted", "ready", 2))
        self.assertEqual(len(self.origin.calls), 1)

    async def test_retired_backend_in_pending_job_fails_without_fetching_audio(self) -> None:
        self.process()
        await self.run_chain()
        with self.ctx.db.write() as tx:
            job_id = jobs.enqueue(tx, "classify", self.episode.id, params={"provider": "gemini-audio", "force": True}, now=now_iso()).job_id
        await self.run_next("classify")
        failed = jobs.get_job(self.ctx.db, job_id)
        self.assertEqual(failed.state, "failed")
        self.assertIn("unsupported classifier", failed.last_error)
        self.assertIsNone(jobs.live_job(self.ctx.db, "download", self.episode.id))

    async def test_classifier_errors_follow_the_retry_policy(self) -> None:
        self.process()
        await self.run_next("download")
        await self.run_next("transcribe")
        classifier = self.ctx.classifiers.get("openrouter")
        classifier.error = ClassifierError("rate limited", retry_after=7.0)
        job = await self.run_next("classify")
        self.assertEqual(job.state, "pending")
        self.assertEqual(self.fresh().pipeline_state, "classify_pending")
        classifier.error = ClassifierError("invalid key", permanent=True)
        with self.ctx.db.write() as tx:
            jobs.make_available_now(tx, job.id, now=now_iso())
        job = await self.run_next("classify")
        self.assertEqual(job.state, "failed")
        self.assertEqual((self.fresh().pipeline_state, self.fresh().classify_state), ("failed", "failed"))

    async def test_progress_is_recorded_without_seq(self) -> None:
        self.process()
        self.origin.gate = asyncio.Event()
        task = asyncio.create_task(self.run_next("download"))
        await asyncio.sleep(0.05)
        during = self.fresh()
        self.assertEqual((during.pipeline_state, during.progress_stage), ("downloading", "download"))
        seq = self.ctx.db.current_seq()
        self.origin.gate.set()
        await task
        self.assertGreater(self.ctx.db.current_seq(), seq)
        self.assertIsNone(self.fresh().progress_stage, "cleared when the stage finishes")


class SchedulerTests(StageTestCase):
    def scheduler(self, **overrides) -> Scheduler:
        fast = {kind: jobs.RetryPolicy(0.01, 0.05) for kind in states.JOB_KINDS}
        config = SchedulerConfig.from_settings(
            self.settings,
            poll_seconds=0.02,
            lease_renew_seconds=0.05,
            sweep_seconds=3600,
            maintenance_seconds=3600,
            drain_seconds=0.2,
            retry_policies=fast,
            **overrides,
        )
        scheduler = Scheduler(self.ctx, self.stages, config)
        self.ctx.scheduler = scheduler
        scheduler.start()
        return scheduler

    async def wait_for(self, predicate, timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > deadline:
                self.fail("timed out waiting for the pipeline")
            await asyncio.sleep(0.01)

    async def test_the_chain_runs_through_retries_to_ready(self) -> None:
        self.origin.errors.append(DownloadError("flaky"))
        scheduler = self.scheduler()
        self.addAsyncCleanup(scheduler.shutdown)
        self.process()
        self.ctx.wake()
        await self.wait_for(lambda: self.fresh().pipeline_state == "ready")
        self.assertEqual(len(self.origin.calls), 2)
        self.assertEqual(self.fresh().active_marker_count, 2)
        await self.wait_for(scheduler.idle)

    async def test_downloads_respect_the_per_host_limit(self) -> None:
        episodes = seed_episodes(self.ctx.db, self.podcast.id, [item(f"h{i}", position=i + 1) for i in range(4)])
        self.origin.gate = asyncio.Event()
        scheduler = self.scheduler(per_host_downloads=2)
        self.addAsyncCleanup(scheduler.shutdown)
        for episode in episodes:
            self.process(episode.id)
        self.ctx.wake()
        await self.wait_for(lambda: self.origin.in_flight == 2)
        await asyncio.sleep(0.1)
        self.assertEqual(self.origin.in_flight, 2, "same host: at most two at once")
        self.origin.gate.set()
        await self.wait_for(lambda: all(self.fresh(e.id).audio_state == "present" for e in episodes))
        self.assertEqual(self.origin.peak, 2)

    async def test_canceling_a_running_job_stops_its_runner(self) -> None:
        self.transcriber.gate = asyncio.Event()
        scheduler = self.scheduler()
        self.addAsyncCleanup(scheduler.shutdown)
        self.process()
        self.ctx.wake()
        await asyncio.wait_for(self.transcriber.started.wait(), 5)
        job = jobs.live_job(self.ctx.db, "transcribe", self.episode.id)
        with self.ctx.db.write() as tx:
            commands.cancel_job(tx, job.id, now=now_iso())
        self.ctx.abort([job.id])
        await self.wait_for(lambda: job.id not in scheduler.running_job_ids)
        self.assertEqual(jobs.get_job(self.ctx.db, job.id).state, "canceled")
        self.assertEqual(self.fresh().pipeline_state, "downloaded")

    async def test_played_release_stops_running_transcription_without_chaining(self) -> None:
        self.transcriber.gate = asyncio.Event()
        scheduler = self.scheduler()
        self.addAsyncCleanup(scheduler.shutdown)
        self.process()
        self.ctx.wake()
        await asyncio.wait_for(self.transcriber.started.wait(), 5)
        job = jobs.live_job(self.ctx.db, "transcribe", self.episode.id)
        self.assertIsNotNone(job)
        path = self.ctx.store.abspath(self.fresh().audio_path)
        self.assertTrue(await eviction.release_audio(self.ctx, self.episode.id, reason="played"))
        self.assertEqual(jobs.get_job(self.ctx.db, job.id).state, "canceled")
        self.assertNotIn(job.id, scheduler.running_job_ids)
        self.assertFalse(path.exists())
        self.assertEqual((self.fresh().pipeline_state, self.fresh().classify_state), ("ready", "skipped"))
        self.assertIsNone(repo.get_transcript(self.ctx.db, self.episode.id))
        self.assertIsNone(jobs.live_stage_job(self.ctx.db, self.episode.id))
        self.assertEqual(jobs.recover_leases(self.ctx.db, self.ctx.store).healed, [])

    async def test_played_release_stops_running_download_without_chaining(self) -> None:
        self.origin.gate = asyncio.Event()
        scheduler = self.scheduler()
        self.addAsyncCleanup(scheduler.shutdown)
        job_id = self.process()
        self.ctx.wake()
        await self.wait_for(lambda: self.origin.in_flight == 1)
        self.assertEqual(self.fresh().audio_state, "partial")
        self.assertTrue(await eviction.release_audio(self.ctx, self.episode.id, reason="played"))
        self.assertEqual(self.origin.in_flight, 0)
        self.assertEqual(jobs.get_job(self.ctx.db, job_id).state, "canceled")
        self.assertEqual((self.fresh().audio_state, self.fresh().pipeline_state), ("evicted", "ready"))
        self.assertIsNone(jobs.live_stage_job(self.ctx.db, self.episode.id))
        self.assertFalse(self.ctx.store.abspath(self.ctx.store.audio_relpath(self.podcast.id, self.episode.id, "mp3")).exists())

    async def test_played_release_stops_active_classification_and_keeps_history(self) -> None:
        self.process()
        await self.run_chain()
        old_history = repo.list_classifications(self.ctx.db, self.episode.id)
        old_markers = repo.markers_for_episode(self.ctx.db, self.episode.id)
        self.assertEqual(len(old_history), 1)
        classifier = self.ctx.classifiers.get("openrouter")
        entered, canceled, gate = asyncio.Event(), asyncio.Event(), asyncio.Event()
        classify_now = classifier.classify

        async def blocked_classify(request):
            entered.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                canceled.set()
                raise
            return await classify_now(request)

        classifier.classify = blocked_classify
        scheduler = self.scheduler()
        self.addAsyncCleanup(scheduler.shutdown)
        with self.ctx.db.write() as tx:
            job_id = commands.reanalyze_episode(
                tx, self.episode.id, server=self.ctx.server_settings(), now=now_iso()
            )
        self.ctx.wake()
        await asyncio.wait_for(entered.wait(), 5)
        self.assertEqual(jobs.get_job(self.ctx.db, job_id).state, "running")
        self.assertTrue(await eviction.release_audio(self.ctx, self.episode.id, reason="played"))
        self.assertTrue(canceled.is_set())
        self.assertEqual(jobs.get_job(self.ctx.db, job_id).state, "canceled")
        self.assertEqual(repo.list_classifications(self.ctx.db, self.episode.id), old_history)
        self.assertEqual(repo.markers_for_episode(self.ctx.db, self.episode.id), old_markers)
        self.assertIsNone(jobs.live_stage_job(self.ctx.db, self.episode.id))
        await scheduler.shutdown()
        with self.ctx.db.write() as tx:
            restarted = commands.reanalyze_episode(
                tx, self.episode.id, server=self.ctx.server_settings(), now=now_iso()
            )
        self.assertNotEqual(restarted, job_id)
        self.assertEqual(jobs.get_job(self.ctx.db, restarted).state, "pending")
        self.assertIsNone(self.fresh().release_reason)

    async def test_canceled_stage_cannot_commit_or_report_late_progress(self) -> None:
        self.transcriber.gate = asyncio.Event()
        self.process()
        await self.run_next("download")
        task = asyncio.create_task(self.run_next("transcribe"))
        await asyncio.wait_for(self.transcriber.started.wait(), 5)
        job = jobs.live_job(self.ctx.db, "transcribe", self.episode.id)
        self.assertIsNotNone(job)
        reporter = stages.ProgressReporter(self.ctx.db, self.episode.id, job_id=job.id, owner=self.ctx.owner)
        await eviction.release_audio(self.ctx, self.episode.id, reason="played")
        reporter(500.0, 600.0)
        self.transcriber.gate.set()
        await task
        self.assertEqual(jobs.get_job(self.ctx.db, job.id).state, "canceled")
        self.assertIsNone(self.fresh().progress_stage)
        self.assertIsNone(self.fresh().progress_updated_at)
        self.assertIsNone(repo.get_transcript(self.ctx.db, self.episode.id))
        self.assertIsNone(jobs.live_stage_job(self.ctx.db, self.episode.id))

    async def test_a_lost_lease_stops_the_job(self) -> None:
        self.transcriber.gate = asyncio.Event()
        scheduler = self.scheduler()
        self.addAsyncCleanup(scheduler.shutdown)
        self.process()
        self.ctx.wake()
        await asyncio.wait_for(self.transcriber.started.wait(), 5)
        job = jobs.live_job(self.ctx.db, "transcribe", self.episode.id)
        with self.ctx.db.write() as tx:  # e.g. canceled by the CLI in another process
            jobs.cancel(tx, job.id, now=now_iso())
        await self.wait_for(lambda: job.id not in scheduler.running_job_ids)

    async def test_shutdown_leaves_interrupted_jobs_for_recovery(self) -> None:
        self.transcriber.gate = asyncio.Event()
        scheduler = self.scheduler()
        self.process()
        self.ctx.wake()
        await asyncio.wait_for(self.transcriber.started.wait(), 5)
        await scheduler.shutdown()
        job = jobs.live_job(self.ctx.db, "transcribe", self.episode.id)
        self.assertEqual((job.state, self.fresh().pipeline_state), ("running", "transcribing"))
        report = jobs.recover_leases(self.ctx.db, self.ctx.store)
        self.assertEqual(report.requeued, [job.id])
        self.assertEqual(self.fresh().pipeline_state, "transcribe_pending")
        self.transcriber.gate.set()
        scheduler = self.scheduler()
        self.addAsyncCleanup(scheduler.shutdown)
        await self.wait_for(lambda: self.fresh().pipeline_state == "ready")
        self.assertEqual(jobs.get_job(self.ctx.db, job.id).attempts, 2)

    async def test_due_feeds_are_refreshed(self) -> None:
        with self.ctx.db.write() as tx:
            repo.schedule_fetch(tx, self.podcast.id, next_fetch_at=iso(utc_now() - dt.timedelta(seconds=1)))
        refreshed = asyncio.Event()

        async def fake_refresh(ctx, podcast_id, *, job=None):
            with ctx.db.write() as tx:
                repo.schedule_fetch(tx, podcast_id, next_fetch_at="2999-01-01T00:00:00.000Z")
                jobs.complete(tx, job.id, now=now_iso())
            refreshed.set()

        with mock.patch("noadcast.feeds.refresher.refresh_podcast", fake_refresh):
            scheduler = self.scheduler()
            self.addAsyncCleanup(scheduler.shutdown)
            await asyncio.wait_for(refreshed.wait(), 5)


class EvictionTests(StageTestCase):
    async def ready_with_audio(self, guid: str, **audio_fields) -> repo.Episode:
        (episode,) = seed_episodes(self.ctx.db, self.podcast.id, [item(guid)])
        episode = seed_present_audio(self.ctx, episode, pipeline_state="ready")
        if audio_fields:
            with self.ctx.db.write() as tx:
                for column, value in audio_fields.items():
                    self.assertIn(column, ("audio_downloaded_at", "audio_last_access_at"))
                    tx.execute(f"UPDATE episodes SET {column} = ? WHERE id = ?", (value, episode.id))
        return self.fresh(episode.id)

    async def test_release_is_immediate_without_live_jobs(self) -> None:
        episode = await self.ready_with_audio("r1")
        path = self.ctx.store.abspath(episode.audio_path)
        self.assertTrue(await eviction.release_audio(self.ctx, episode.id, reason="played"))
        evicted = self.fresh(episode.id)
        self.assertEqual((evicted.audio_state, evicted.audio_evicted_reason, evicted.audio_path), ("evicted", "played", None))
        self.assertEqual((evicted.audio_sha256, evicted.audio_bytes), (episode.audio_sha256, episode.audio_bytes))
        self.assertFalse(path.exists())
        with self.assertRaises(commands.NotFound):
            await eviction.release_audio(self.ctx, 9999, reason="played")

    async def test_sweep_preserves_legacy_played_stop_intent(self) -> None:
        episode = await self.ready_with_audio("legacy-sweep")
        path = self.ctx.store.abspath(episode.audio_path)
        with self.ctx.db.write() as tx:
            repo.record_release(tx, episode.id, reason="played", now=now_iso())
        report = await eviction.sweep(self.ctx)
        self.assertIn(episode.id, report.released)
        fresh = self.fresh(episode.id)
        self.assertEqual((fresh.audio_state, fresh.pipeline_state, fresh.release_reason), ("evicted", "ready", "played"))
        self.assertFalse(path.exists())

    async def test_boot_drains_legacy_played_release_before_job_recovery(self) -> None:
        self.process()
        await self.run_chain()
        old_history = repo.list_classifications(self.ctx.db, self.episode.id)
        old_markers = repo.markers_for_episode(self.ctx.db, self.episode.id)
        path = self.ctx.store.abspath(self.fresh().audio_path)
        with self.ctx.db.write() as tx:
            job_id = commands.reanalyze_episode(tx, self.episode.id, server=self.ctx.server_settings(), now=now_iso())
            claimed = jobs.claim(tx, "classify", owner="old-server", lease_seconds=60, now=utc_now())
            self.assertEqual(claimed.id, job_id)
            repo.set_episode_states(tx, self.episode.id, pipeline_state="classifying", now=now_iso())
            repo.record_release(tx, self.episode.id, reason="played", now=now_iso())
        config = SchedulerConfig.from_settings(self.settings, poll_seconds=0.02, maintenance_seconds=3600)
        async with run_pipeline(self.ctx, self.stages, config):
            fresh = self.fresh()
            self.assertEqual((fresh.audio_state, fresh.pipeline_state, fresh.release_reason), ("evicted", "ready", "played"))
            self.assertEqual(jobs.get_job(self.ctx.db, job_id).state, "canceled")
            self.assertFalse(path.exists())
            self.assertEqual(repo.list_classifications(self.ctx.db, self.episode.id), old_history)
            self.assertEqual(repo.markers_for_episode(self.ctx.db, self.episode.id), old_markers)
            self.assertIsNone(jobs.live_stage_job(self.ctx.db, self.episode.id))
        self.assertEqual(repo.legacy_played_releases(self.ctx.db), [])
        restarted = self.process()
        self.assertIsNotNone(restarted)
        self.assertIsNone(self.fresh().release_reason)

    async def test_release_waits_for_a_live_job(self) -> None:
        episode = await self.ready_with_audio("r2")
        with self.ctx.db.write() as tx:
            job_id = commands.reanalyze_episode(tx, episode.id, server=self.ctx.server_settings(), now=now_iso())
        self.assertFalse(await eviction.release_audio(self.ctx, episode.id, reason="manual"))
        self.assertEqual(self.fresh(episode.id).audio_state, "present")
        await eviction.sweep(self.ctx)
        self.assertEqual(self.fresh(episode.id).audio_state, "present", "still live")
        with self.ctx.db.write() as tx:
            jobs.cancel(tx, job_id, now=now_iso())
        report = await eviction.sweep(self.ctx)
        self.assertEqual(report.released, [episode.id])
        self.assertEqual((self.fresh(episode.id).audio_state, self.fresh(episode.id).audio_evicted_reason), ("evicted", "manual"))

    async def test_age_and_disk_sweeps_evict_least_recently_useful_first(self) -> None:
        now = utc_now()
        old = await self.ready_with_audio("old", audio_downloaded_at=iso(now - dt.timedelta(days=90)))
        stale_access = await self.ready_with_audio("lru", audio_downloaded_at=iso(now - dt.timedelta(days=20)))
        recent = await self.ready_with_audio("new", audio_downloaded_at=iso(now - dt.timedelta(days=10)),
                                             audio_last_access_at=iso(now - dt.timedelta(days=1)))
        busy = await self.ready_with_audio("busy", audio_downloaded_at=iso(now - dt.timedelta(days=200)))
        with self.ctx.db.write() as tx:
            commands.reanalyze_episode(tx, busy.id, server=self.ctx.server_settings(), now=now_iso())
        # Plenty of disk: only the age rule applies.
        report = await eviction.sweep(self.ctx)
        self.assertEqual(report.aged, [old.id])
        self.assertEqual(self.fresh(busy.id).audio_state, "present", "never while a job needs it")
        # Disk pressure: the least recently useful goes next, then no more than needed.
        tight = DiskUsage(total_bytes=10**12, used_bytes=10**12, free_bytes=self.settings.min_free_bytes - 1)
        with mock.patch.object(self.ctx.store, "disk_usage", return_value=tight):
            report = await eviction.sweep(self.ctx)
        self.assertEqual(report.for_disk, [stale_access.id])
        self.assertEqual(self.fresh(stale_access.id).audio_evicted_reason, "disk")
        self.assertEqual(self.fresh(recent.id).audio_state, "present")


class ProgressReporterTests(StageTestCase):
    async def test_writes_are_throttled_by_time_or_fraction(self) -> None:
        reporter = stages.ProgressReporter(self.ctx.db, self.episode.id, min_interval=3600, min_fraction=0.05)
        seq = self.ctx.db.current_seq()
        reporter(1, 100)
        self.assertEqual(self.fresh().progress_current, 1)
        reporter(3, 100)
        self.assertEqual(self.fresh().progress_current, 1, "under 5% and under the interval")
        reporter(7, 100)
        self.assertEqual(self.fresh().progress_current, 7)
        await asyncio.to_thread(reporter, 50, 100)  # from a worker thread: hops to the loop
        await asyncio.sleep(0)
        self.assertEqual(self.fresh().progress_current, 50)
        self.assertEqual(self.ctx.db.current_seq(), seq, "progress never bumps the seq")


if __name__ == "__main__":
    unittest.main()
