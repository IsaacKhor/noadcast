"""Settings, usage, admin stats, response compression, access logging, and the lifespan."""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
from unittest import mock

from noadcast.api import middleware
from noadcast.api.app import create_app
from noadcast.logging_setup import current_context
from noadcast.pipeline.scheduler import DataDirLocked, SchedulerConfig
from noadcast.timeutil import iso, utc_now
from noadcast.transcribe.pool import PoolStats

from tests.api_support import (
    ApiTestCase,
    FakeRegistry,
    add_classification,
    add_episodes,
    add_podcast,
    doc_example,
    store_audio,
)


class SettingsTests(ApiTestCase):
    async def test_defaults_come_from_config(self) -> None:
        response = await self.client.get("/api/v1/settings")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), set(doc_example("### Settings")))
        self.assertEqual(
            body,
            {
                "adAnalysisEnabled": True,
                "autoProcessEnabled": True,
                "classifier": "openrouter",
                "classifierModel": self.settings.openrouter_model,
                "availableClassifiers": {"openrouter": False},
            },
        )

    async def test_patch_updates_and_is_idempotent(self) -> None:
        seq = self.ctx.db.current_seq()
        response = await self.client.patch("/api/v1/settings", json={"adAnalysisEnabled": False, "unknown": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["adAnalysisEnabled"], False)
        self.assertIs((await self.client.get("/api/v1/settings")).json()["adAnalysisEnabled"], False)
        self.assertEqual(self.ctx.db.current_seq(), seq + 1)
        for body in ({"adAnalysisEnabled": False}, {}, {"classifier": None, "classifierModel": None}):
            with self.subTest(body=body):
                self.assertEqual((await self.client.patch("/api/v1/settings", json=body)).status_code, 200)
                self.assertEqual(self.ctx.db.current_seq(), seq + 1, "no change, no seq")

    async def test_each_supported_model_can_be_selected(self) -> None:
        from noadcast.classifier_models import MODEL_IDS
        for model in MODEL_IDS:
            response = await self.client.patch("/api/v1/settings", json={"classifier": "openrouter", "classifierModel": model})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["classifierModel"], model)
            self.assertEqual((await self.client.get("/api/v1/settings")).json(), response.json())

    async def test_invalid_values_are_rejected(self) -> None:
        for body in ({"classifier": "gpt"}, {"classifier": "gemini"}, {"classifier": "fake"}, {"classifierModel": "google/gemini-3.5-flash"}, {"classifierModel": ""}, {"autoProcessEnabled": "sometimes"}):
            with self.subTest(body=body):
                self.assertError(await self.client.patch("/api/v1/settings", json=body), 422, "invalidRequest")


class UsageTests(ApiTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        podcast = add_podcast(self.ctx)
        [self.episode_id] = add_episodes(self.ctx, podcast.id, 1)
        now = utc_now()
        self.today = iso(now)
        self.three_days_ago = iso(now - dt.timedelta(days=3))
        add_classification(self.ctx, self.episode_id, created_at=self.today, cost=0.02, tokens=(1000, 200, 100))
        add_classification(self.ctx, self.episode_id, created_at=self.today, cost=0.01, tokens=(500, 0, 50))
        add_classification(
            self.ctx, self.episode_id, provider="claude", model="claude-sonnet-5", created_at=self.three_days_ago,
            cost=0.05, tokens=(2000, 0, 300),
        )
        add_classification(self.ctx, self.episode_id, created_at=iso(now - dt.timedelta(days=40)), cost=1.0)

    async def test_aggregates_by_day_model_and_total(self) -> None:
        response = await self.client.get("/api/v1/usage")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"days", "byModel", "totals"})
        self.assertEqual(
            body["days"],
            [
                {"date": self.three_days_ago[:10], "calls": 1, "inputTokens": 2000, "thoughtTokens": 0, "outputTokens": 300, "costUsd": 0.05},
                {"date": self.today[:10], "calls": 2, "inputTokens": 1500, "thoughtTokens": 200, "outputTokens": 150, "costUsd": 0.03},
            ],
        )
        self.assertEqual(
            [(m["provider"], m["model"], m["calls"]) for m in body["byModel"]],
            [("claude", "claude-sonnet-5", 1), ("gemini", "gemini-3.5-flash", 2)],
        )
        self.assertEqual(
            set(body["byModel"][0]), {"provider", "model", "calls", "inputTokens", "thoughtTokens", "outputTokens", "costUsd"}
        )
        totals = body["totals"]
        self.assertEqual(
            (totals["calls"], totals["inputTokens"], totals["thoughtTokens"], totals["outputTokens"]), (3, 3500, 200, 450)
        )
        self.assertAlmostEqual(totals["costUsd"], 0.08)

    async def test_window_and_bounds(self) -> None:
        wide = (await self.client.get("/api/v1/usage", params={"days": 60})).json()
        self.assertEqual(wide["totals"]["calls"], 4)
        narrow = (await self.client.get("/api/v1/usage", params={"days": 1})).json()
        self.assertEqual(narrow["totals"]["calls"], 2)
        for days in (0, 367, "week"):
            with self.subTest(days=days):
                self.assertError(await self.client.get("/api/v1/usage", params={"days": days}), 422, "invalidRequest")


class FakePool:
    def stats(self) -> PoolStats:
        return PoolStats(workers=6, alive=6, ready=5, busy=1, completed=12, crashed=0, rss_mib=6400.0)


class AdminStatsTests(ApiTestCase):
    def build_app(self):
        return create_app(
            self.settings, transcriber=FakePool(), classifiers=self.registry, http=self.outbound, run_scheduler=False
        )

    async def test_snapshot_shape(self) -> None:
        podcast = add_podcast(self.ctx)
        [episode_id] = add_episodes(self.ctx, podcast.id, 1)
        store_audio(self.ctx, episode_id)
        await self.client.post(f"/api/v1/episodes/{episode_id}/reanalyze")
        response = await self.client.get("/api/v1/admin/stats")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body),
            {
                "serverTime", "instanceId", "seq", "pool", "queues", "disk", "audio", "episodesByState",
                "podcasts", "spend30d", "recentFailures", "guidCollisions",
            },
        )
        self.assertEqual(body["pool"]["workers"], 6)
        self.assertEqual(body["pool"]["rssMib"], 6400.0)
        self.assertEqual(body["podcasts"], 1)
        self.assertEqual([q["kind"] for q in body["queues"]], ["transcribe"])  # no transcript yet
        self.assertGreater(body["disk"]["freeBytes"], 0)
        self.assertEqual(body["audio"]["byState"], {"present": 1})

    async def test_pool_section_is_null_without_a_pool(self) -> None:
        self.ctx.transcriber = None
        self.assertIsNone((await self.client.get("/api/v1/admin/stats")).json()["pool"])


class CompressionTests(ApiTestCase):
    async def test_large_json_is_gzipped_small_json_is_not(self) -> None:
        podcast = add_podcast(self.ctx)
        add_episodes(self.ctx, podcast.id, 20, description="<p>" + "Show notes. " * 200 + "</p>")
        large = await self.client.get("/api/v1/sync", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(large.headers["content-encoding"], "gzip")
        self.assertEqual(len(large.json()["episodes"]), 20)
        plain = await self.client.get("/api/v1/sync", headers={"Accept-Encoding": "identity"})
        self.assertNotIn("content-encoding", plain.headers)
        small = await self.client.get("/api/v1/session", headers={"Accept-Encoding": "gzip"})
        self.assertNotIn("content-encoding", small.headers)


class _Capture(logging.Handler):
    """Keeps each record with the correlation context it was logged under."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.entries: list[tuple[logging.LogRecord, dict]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.entries.append((record, current_context()))


class AccessLogTests(ApiTestCase):
    auth_enabled = True

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.capture = _Capture()
        logger = middleware.access_log
        logger.addHandler(self.capture)
        previous = logger.level
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.setLevel, previous)
        self.addCleanup(logger.removeHandler, self.capture)
        podcast = add_podcast(self.ctx)
        [self.episode_id] = add_episodes(self.ctx, podcast.id, 1)
        store_audio(self.ctx, self.episode_id)

    async def test_one_line_per_request_with_its_request_id(self) -> None:
        response = await self.client.get("/api/v1/sync", params={"since": 0})
        [(record, context)] = self.capture.entries
        self.assertEqual((record.method, record.path, record.status), ("GET", "/api/v1/sync", 200))
        self.assertIsInstance(record.duration_ms, float)
        self.assertTrue(record.getMessage().startswith("GET /api/v1/sync 200 "))
        self.assertEqual(context["request_id"], response.headers["x-request-id"])

    async def test_signatures_never_reach_the_log(self) -> None:
        path = (await self.client.post(f"/api/v1/episodes/{self.episode_id}/audio-url")).json()["path"]
        signature = path.rsplit("sig=", 1)[1]
        self.capture.entries.clear()
        with mock.patch.object(middleware, "_sampled_out", return_value=False):
            self.assertEqual((await self.anon.get(path, headers={"Range": "bytes=0-1"})).status_code, 206)
            self.assertEqual((await self.anon.get(path.replace(signature, "0" * 64))).status_code, 401)
        self.assertEqual(len(self.capture.entries), 2)
        for record, context in self.capture.entries:
            line = record.getMessage() + repr(record.__dict__) + repr(context)
            self.assertNotIn(signature, line)
            self.assertNotIn("sig=", line)
        self.assertEqual(self.capture.entries[0][0].sample_rate, middleware.AUDIO_LOG_SAMPLE_RATE)

    async def test_audio_successes_are_sampled_failures_are_not(self) -> None:
        url = f"/api/v1/episodes/{self.episode_id}/audio"
        with mock.patch.object(middleware, "_sampled_out", return_value=True):
            await self.client.get(url, headers={"Range": "bytes=0-1"})
            await self.client.head(url)
            self.assertEqual(self.capture.entries, [])
            await self.client.get("/api/v1/episodes/999/audio")
            await self.anon.get(url)
        self.assertEqual([record.status for record, _ in self.capture.entries], [404, 401])


class LifespanTests(ApiTestCase):
    async def test_scheduler_runs_and_holds_the_data_dir(self) -> None:
        config = SchedulerConfig.from_settings(self.settings, poll_seconds=0.05, drain_seconds=5.0)
        app = create_app(self.settings.with_overrides(data_dir=self.data_dir / "served"), classifiers=FakeRegistry(),
                         http=self.outbound, scheduler_config=config)
        rival = create_app(self.settings.with_overrides(data_dir=self.data_dir / "served"), classifiers=FakeRegistry(),
                           http=self.outbound, scheduler_config=config)
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(app.router.lifespan_context(app))
            ctx = app.state.ctx
            self.assertIsNotNone(ctx.scheduler)
            with self.assertRaises(DataDirLocked):
                async with rival.router.lifespan_context(rival):
                    pass
        self.assertIsNone(ctx.scheduler)
