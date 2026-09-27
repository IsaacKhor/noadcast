"""Offline end-to-end: the whole server driven through its HTTP API.

A local origin (tests/support/httpfixture.py: real sockets, ETag/304, Range,
and one deliberate mid-stream drop) serves ``benchmarks/tal/feed.xml`` with
its three newest enclosures pointed at the corpus MP3s. Transcription
replays the recorded tiny.en words of ``benchmarks/tal/runs/
wt-crossover-2-on-20260922``; classification replays the recorded provider
responses in ``tests/fixtures/llm`` (a 429-then-success for 449) or answers
synthetically. Everything else — fetcher, parser, admission, scheduler,
resumable downloader, probe, joiner, sanitiser, sync, signed Range serving,
retention — is the real code.

Fast mode (default) needs no model and runs in seconds.
``NOADCAST_E2E_REAL_ASR=1`` swaps in a real TranscriptionPool on tiny.en
for one episode (minutes of CPU; run it on an idle host).
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import os
import re
import unittest
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from noadcast.api.app import create_app
from tests.support.classifier_replay import ReplayRegistry
from noadcast.config import Settings, settings_for_tests
from noadcast.db import repo
from noadcast.pipeline import jobs as job_table
from noadcast.pipeline.scheduler import SchedulerConfig
from noadcast.transcribe.fake import FakeTranscriber
from noadcast.transcribe.protocol import Transcriber

from tests.pipeline_support import temp_dir
from tests.support.httpfixture import HTTPFixture

ROOT = Path(__file__).resolve().parents[2]
TAL = ROOT / "benchmarks" / "tal"
RUN_DIR = TAL / "runs" / "wt-crossover-2-on-20260922"
LLM_FIXTURES = ROOT / "tests" / "fixtures" / "llm"
TOKEN = "e2e-" + "t" * 40
LOCAL_ITEMS = 3  # the feed's three newest items get local enclosures
DROP_AT = 5_000_000  # the first download of the newest episode dies here

MANIFEST = json.loads((TAL / "manifest.json").read_text())
BY_URL = {entry["url"]: entry for entry in MANIFEST["episodes"]}
FEED_XML = (TAL / "feed.xml").read_text(encoding="utf-8")
_ENCLOSURE = re.compile(r'(<enclosure url=")([^"]+)(")')


def corpus_available() -> bool:
    return all((TAL / entry["path"]).is_file() for entry in MANIFEST["episodes"][:LOCAL_ITEMS]) and RUN_DIR.is_dir()


def local_feed(base_url: str) -> tuple[bytes, list[dict[str, Any]]]:
    """feed.xml with its first LOCAL_ITEMS enclosures served by the fixture;
    GUIDs, dates, and every other item are untouched."""
    served: list[dict[str, Any]] = []

    def swap(match: re.Match[str]) -> str:
        if len(served) >= LOCAL_ITEMS:
            return match.group(0)
        entry = BY_URL[match.group(2).replace("&amp;", "&")]
        served.append({**entry, "local_path": "/" + entry["path"]})
        return f"{match.group(1)}{base_url}/{entry['path']}{match.group(3)}"

    return _ENCLOSURE.sub(swap, FEED_XML).encode("utf-8"), served


def e2e_settings(data_dir: Path) -> Settings:
    return settings_for_tests(
        data_dir, allow_no_auth=False, api_token=TOKEN, classifier="openrouter", signing_secret=b"e2e-signing-secret"
    )


FAST = SchedulerConfig(
    concurrency={"refresh_feed": 2, "download": 2, "transcribe": 2, "classify": 2, "evict": 1},
    per_host_downloads=2,
    poll_seconds=0.05,
    lease_renew_seconds=0.5,
    sweep_seconds=3600,
    maintenance_seconds=3600,
    drain_seconds=0.2,
    retry_policies={kind: job_table.RetryPolicy(0.05, 0.2) for kind in job_table.RETRY_POLICIES},
)


class Server:
    """One server instance on a data directory, driven over ASGI with the
    lifespan run by hand (everything shares the test's event loop, so the
    thread-affine sqlite connection stays on one thread)."""

    def __init__(self, settings: Settings, transcriber: Transcriber) -> None:
        self.settings = settings
        self.app = create_app(
            settings,
            transcriber=transcriber,
            classifiers=ReplayRegistry(settings, LLM_FIXTURES),
            scheduler_config=FAST,
        )
        self.api = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://noadcast.test",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        self.anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://noadcast.test")

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator["Server"]:
        async with self.app.router.lifespan_context(self.app):
            try:
                yield self
            finally:
                await self.api.aclose()
                await self.anonymous.aclose()

    @property
    def ctx(self):
        return self.app.state.ctx

    async def json(self, method: str, path: str, expect: int = 200, **kwargs: Any) -> Any:
        response = await self.api.request(method, path, **kwargs)
        if response.status_code != expect:
            raise AssertionError(f"{method} {path}: {response.status_code} {response.text}")
        return response.json() if response.content else None

    async def episode(self, episode_id: int) -> dict[str, Any]:
        return await self.json("GET", f"/api/v1/episodes/{episode_id}")

    async def wait(self, episode_id: int, predicate, *, timeout: float = 30.0) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            episode = await self.episode(episode_id)
            if predicate(episode):
                return episode
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"episode {episode_id} stuck: {episode}")
            await asyncio.sleep(0.05)

    async def wait_idle(self, timeout: float = 30.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.ctx.scheduler.idle():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("pipeline never went idle")
            await asyncio.sleep(0.05)

    async def full_sync(self, limit: int) -> tuple[dict[int, dict], dict[int, dict], int, int]:
        """Page /sync from 0 like the client: podcasts before episodes, closure checked."""
        podcasts: dict[int, dict] = {}
        episodes: dict[int, dict] = {}
        since, pages = 0, 0
        while True:
            page = await self.json("GET", "/api/v1/sync", params={"since": since, "limit": limit})
            for podcast in page["podcasts"]:
                podcasts[podcast["id"]] = podcast
            for episode in page["episodes"]:
                if episode["podcastId"] not in podcasts:
                    raise AssertionError("page broke referential closure")
                episodes[episode["id"]] = episode
            for deletion in page["deletions"]:
                if deletion["entity"] == "podcast":
                    podcasts.pop(deletion["id"], None)
                    episodes = {k: e for k, e in episodes.items() if e["podcastId"] != deletion["id"]}
                else:
                    episodes.pop(deletion["id"], None)
            if pages == 0 and page["settings"] is None:
                raise AssertionError("a full sync must carry settings")
            since, pages = page["nextSince"], pages + 1
            if not page["hasMore"]:
                return podcasts, episodes, since, pages


def outro_and_intro(testcase: unittest.TestCase, episode: dict[str, Any]) -> None:
    markers = episode["adMarkers"]
    testcase.assertTrue(markers, "no markers")
    testcase.assertEqual([m["startSeconds"] for m in markers], sorted(m["startSeconds"] for m in markers))
    intro = [m for m in markers if m["kind"] == "intro"]
    outro = [m for m in markers if m["kind"] == "outro"]
    testcase.assertEqual(len(intro), 1)
    testcase.assertEqual(intro[0]["startSeconds"], 0.0, "an intro skips the opening music too")
    testcase.assertEqual(len(outro), 1)
    testcase.assertTrue(episode["durationIsMeasured"])
    testcase.assertAlmostEqual(outro[0]["endSeconds"], episode["durationSeconds"], places=2)


@unittest.skipUnless(corpus_available(), "benchmarks/tal corpus audio or recorded run missing")
class TalOfflineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.origin = HTTPFixture().start()
        self.addCleanup(self.origin.close)
        feed, self.local = local_feed(self.origin.base_url)
        self.feed_url = self.origin.url("/feed.xml")
        self.origin.serve("/feed.xml", feed, content_type="application/rss+xml")
        for entry in self.local:
            self.origin.serve(entry["local_path"], TAL / entry["path"], content_type="audio/mpeg")
        self.newest = self.local[0]  # 646: The Secret of My Death

    def fake_transcriber(self, **kwargs: Any) -> FakeTranscriber:
        return FakeTranscriber.from_run_dir(RUN_DIR, **kwargs)

    async def test_subscribe_process_sync_stream_release(self) -> None:
        self.origin.drop_after(self.newest["local_path"], DROP_AT)
        server = Server(e2e_settings(temp_dir(self)), self.fake_transcriber())
        async with server.running():
            health = (await server.anonymous.get("/health")).json()
            self.assertEqual((health["status"], health["authRequired"]), ("ok", True))
            self.assertEqual((await server.anonymous.get("/api/v1/session")).status_code, 401)

            # Subscribe: inline fetch and parse; every item stored, only the newest admitted.
            created = await server.json("POST", "/api/v1/podcasts", 201, json={"feedUrl": self.feed_url})
            podcast = created["podcast"]
            self.assertEqual((podcast["title"], podcast["episodeCount"]), ("This American Life", 15))
            podcasts, episodes, cursor, pages = await server.full_sync(limit=4)
            # The two persisted model defaults also consume sync-page slots.
            self.assertEqual((list(podcasts), len(episodes), pages), ([podcast["id"]], 15, 5))
            by_guid = {e["guid"]: e for e in episodes.values()}
            admitted = [e for e in episodes.values() if e["state"] != "discovered"]
            self.assertEqual(len(admitted), 1, "first subscribe admits only the newest episode")
            newest = admitted[0]
            self.assertEqual(newest["title"], self.newest["title"])

            ready = await server.wait(newest["id"], lambda e: e["state"] == "ready")
            self.assertEqual((ready["audioState"], ready["transcriptState"], ready["classifyState"]), ("present", "ready", "ready"))
            self.assertEqual((ready["audioSha256"], ready["audioBytes"]), (self.newest["sha256"], self.newest["bytes"]))
            self.assertAlmostEqual(ready["durationSeconds"], self.newest["duration_seconds"], places=2)
            outro_and_intro(self, ready)
            # The drop at 5 MB was resumed with a Range request, not restarted.
            gets = [r for r in self.origin.requests_for(self.newest["local_path"]) if r.method == "GET"]
            self.assertEqual(len(gets), 2)
            self.assertEqual(gets[1].headers.get("range"), f"bytes={DROP_AT}-")
            self.assertEqual(gets[1].status, 206)

            # A second refresh is a conditional GET answered 304: zero seqs.
            await server.wait_idle()
            head = server.ctx.db.current_seq()
            job = await server.json("POST", f"/api/v1/podcasts/{podcast['id']}/refresh", 202)
            await server.wait_idle()
            self.assertEqual(job_table.get_job(server.ctx.db, job["jobId"]).state, "done")
            feed_gets = self.origin.requests_for("/feed.xml")
            self.assertEqual(feed_gets[-1].status, 304)
            self.assertIn("if-none-match", feed_gets[-1].headers)
            self.assertEqual(server.ctx.db.current_seq(), head)
            delta = await server.json("GET", "/api/v1/sync", params={"since": head})
            self.assertEqual((delta["podcasts"], delta["episodes"], delta["nextSince"]), ([], [], head))

            # Processing another episode on demand: the 449 fixture answers 429 first.
            second = by_guid[self._guid_for(self.local[1], by_guid)]
            processed = await server.json("POST", f"/api/v1/episodes/{second['id']}/process", 202)
            self.assertIsNotNone(processed["jobId"])
            done = await server.wait(second["id"], lambda e: e["state"] == "ready")
            outro_and_intro(self, done)
            history = await server.json("GET", f"/api/v1/episodes/{second['id']}/classifications")
            self.assertEqual([c["isActive"] for c in history["items"]], [True])
            again = await server.json("POST", f"/api/v1/episodes/{second['id']}/process", 202)
            self.assertIsNone(again["jobId"], "nothing left to do")
            transcript = await server.json("GET", f"/api/v1/episodes/{second['id']}/transcript")
            self.assertGreater(len(transcript["sentences"]), 100)

            # Progress polling is ETag-conditional.
            active = await server.api.get("/api/v1/jobs/active")
            self.assertEqual((active.status_code, active.json()), (200, {"items": []}))
            cached = await server.api.get("/api/v1/jobs/active", headers={"If-None-Match": active.headers["etag"]})
            self.assertEqual((cached.status_code, cached.content), (304, b""))

            # Streaming: a signed URL works without the bearer token; Range bytes are the file's bytes.
            signed = await server.json("POST", f"/api/v1/episodes/{newest['id']}/audio-url")
            self.assertTrue(signed["url"].endswith(signed["path"]))
            corpus = (TAL / self.newest["path"]).read_bytes()
            ranged = await server.anonymous.get(signed["path"], headers={"Range": "bytes=1000000-1065535", "Accept-Encoding": "gzip"})
            self.assertEqual(ranged.status_code, 206)
            self.assertEqual(ranged.content, corpus[1_000_000:1_065_536])
            self.assertEqual(ranged.headers["content-range"], f"bytes 1000000-1065535/{len(corpus)}")
            self.assertNotIn("content-encoding", ranged.headers)
            head_response = await server.anonymous.head(signed["path"])
            self.assertEqual((head_response.status_code, head_response.headers["content-length"], head_response.content), (200, str(len(corpus)), b""))
            tampered = signed["path"][:-1] + ("0" if signed["path"][-1] != "0" else "1")
            self.assertEqual((await server.anonymous.get(tampered)).status_code, 401)
            self.assertEqual((await server.anonymous.get(f"/api/v1/episodes/{newest['id']}")).status_code, 401)

            # Retention: releasing deletes the audio but keeps the skip data.
            revision = ready["markerRevision"]
            released = await server.api.delete(f"/api/v1/episodes/{newest['id']}/audio", params={"reason": "played"})
            self.assertEqual(released.status_code, 204)
            evicted = await server.episode(newest["id"])
            self.assertEqual((evicted["audioState"], evicted["state"]), ("evicted", "ready"))
            self.assertEqual((evicted["adMarkers"], evicted["audioSha256"]), (ready["adMarkers"], ready["audioSha256"]))
            missing = await server.api.post(f"/api/v1/episodes/{newest['id']}/audio-url")
            self.assertEqual(missing.status_code, 409)
            self.assertEqual(missing.json()["error"]["code"], "audioEvicted")
            self.assertIn("retry-after", missing.headers)
            self.assertIsNotNone(missing.json()["jobId"])
            restored = await server.wait(newest["id"], lambda e: e["audioState"] == "present")
            self.assertEqual((restored["state"], restored["markerRevision"]), ("ready", revision), "same bytes: markers stand")
            await server.wait_idle()

            # GUID collisions: a second feed reusing every GUID keeps all of its episodes.
            self.origin.serve("/mirror.xml", local_feed(self.origin.base_url)[0], content_type="application/rss+xml")
            mirror = await server.json(
                "POST", "/api/v1/podcasts", 201, json={"feedUrl": self.origin.url("/mirror.xml"), "autoProcessEnabled": False}
            )
            self.assertEqual(mirror["podcast"]["episodeCount"], 15)
            podcasts, episodes, _, _ = await server.full_sync(limit=7)
            per_podcast = {pid: sum(1 for e in episodes.values() if e["podcastId"] == pid) for pid in podcasts}
            self.assertEqual(per_podcast, {podcast["id"]: 15, mirror["podcast"]["id"]: 15})
            stats = await server.json("GET", "/api/v1/admin/stats")
            self.assertEqual(len(stats["guidCollisions"]), 15)

    @staticmethod
    def _guid_for(entry: dict[str, Any], by_guid: dict[str, dict]) -> str:
        (guid,) = [g for g, e in by_guid.items() if e["title"] == entry["title"]]
        return guid

    async def test_restart_mid_transcription_recovers_to_the_same_state(self) -> None:
        baseline = await self._run_to_ready(restart=False)
        recovered = await self._run_to_ready(restart=True)
        self.assertEqual(recovered, baseline)

    async def _run_to_ready(self, *, restart: bool) -> dict[str, Any]:
        """Subscribe and process the newest episode; with ``restart`` the first
        server instance is stopped mid-transcription (its job left running,
        as after a crash) and a second one recovers it on the same data."""
        settings = e2e_settings(temp_dir(self))
        first = Server(settings, self.fake_transcriber(latency_seconds=30.0 if restart else 0.0))
        async with first.running():
            podcast = (await first.json("POST", "/api/v1/podcasts", 201, json={"feedUrl": self.feed_url}))["podcast"]
            (episode_id,) = [
                e.id for e in _episodes(first.ctx) if e.pipeline_state != "discovered" and e.podcast_id == podcast["id"]
            ]
            if restart:
                await first.wait(episode_id, lambda e: e["state"] == "transcribing")
            else:
                final = await first.wait(episode_id, lambda e: e["state"] == "ready")
        if restart:
            second = Server(settings, self.fake_transcriber())
            async with second.running():
                final = await second.wait(episode_id, lambda e: e["state"] == "ready")
                (job,) = [j for j in job_table.list_jobs(second.ctx.db, kind="transcribe")]
                self.assertEqual((job.state, job.attempts), ("done", 2), "the interrupted attempt counted once")
        return {
            key: final[key]
            for key in ("state", "audioState", "audioSha256", "audioBytes", "durationSeconds", "transcriptState", "classifyState", "markerRevision")
        } | {"markers": [(m["kind"], m["startSeconds"], m["endSeconds"], m["source"]) for m in final["adMarkers"]]}


def _episodes(ctx) -> list[repo.Episode]:
    return [episode for podcast in repo.list_podcasts(ctx.db) for episode in repo.episodes_for_podcast(ctx.db, podcast.id)]


@unittest.skipUnless(os.environ.get("NOADCAST_E2E_REAL_ASR") == "1", "set NOADCAST_E2E_REAL_ASR=1 to transcribe with tiny.en")
@unittest.skipUnless(corpus_available(), "benchmarks/tal corpus audio or recorded run missing")
class TalRealAsrTest(unittest.IsolatedAsyncioTestCase):
    """The same subscribe-to-ready path on a real TranscriptionPool. The pool
    is started in setUpClass, before any event loop exists, as server.py does."""

    pool = None

    @classmethod
    def setUpClass(cls) -> None:
        from noadcast.transcribe.pool import PoolConfig, TranscriptionPool

        model = Path(os.environ.get("NOADCAST_ASR_MODEL_DIR", TAL / "models" / "tiny.en"))
        cls.pool = TranscriptionPool(PoolConfig(model_path=model, workers=1, cpu_threads=4))
        cls.pool.start_blocking()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.pool is not None:
            cls.pool.shutdown()

    async def test_newest_episode_with_real_transcription(self) -> None:
        origin = HTTPFixture().start()
        self.addCleanup(origin.close)
        feed, local = local_feed(origin.base_url)
        origin.serve("/feed.xml", feed, content_type="application/rss+xml")
        origin.serve(local[0]["local_path"], TAL / local[0]["path"], content_type="audio/mpeg")
        server = Server(e2e_settings(temp_dir(self)), self.pool)
        async with server.running():
            await server.json("POST", "/api/v1/podcasts", 201, json={"feedUrl": origin.url("/feed.xml")})
            (episode_id,) = [e.id for e in _episodes(server.ctx) if e.pipeline_state != "discovered"]
            ready = await server.wait(episode_id, lambda e: e["state"] in ("ready", "failed"), timeout=1800)
            self.assertEqual(ready["state"], "ready", ready.get("error"))
            outro_and_intro(self, ready)
            self.assertEqual(ready["audioSha256"], local[0]["sha256"])
            self.assertAlmostEqual(ready["durationSeconds"], local[0]["duration_seconds"], delta=0.1)
            transcript = await server.json("GET", f"/api/v1/episodes/{episode_id}/transcript", params={"format": "words"})
            produced = [w["word"] for w in transcript["words"]]
            reference = json.loads((RUN_DIR / "transcripts" / "01-646.json").read_text())
            recorded = [w["word"] for s in reference["segments"] for w in s["words"]]
            # The recording transcribed ffmpeg-decoded 16-bit PCM; the pool
            # decodes the MP3 itself (PyAV, float32), so the words agree
            # closely rather than exactly.
            self.assertLess(abs(len(produced) - len(recorded)) / len(recorded), 0.02)
            self.assertGreater(difflib.SequenceMatcher(None, produced, recorded, autojunk=False).ratio(), 0.95)


if __name__ == "__main__":
    unittest.main()
