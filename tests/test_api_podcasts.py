"""Subscriptions: POST/PATCH/DELETE /podcasts, refreshes, and OPML import/export."""

from __future__ import annotations

import asyncio
import functools
import xml.etree.ElementTree as ET
from unittest import mock

import httpx

from noadcast.api.routers import settings as settings_routes
from noadcast.db import repo
from noadcast.feeds import refresher
from noadcast.pipeline import commands, jobs
from noadcast.timeutil import now_iso

from tests.api_support import ApiTestCase, add_episodes, add_podcast, doc_example, store_audio

FEED_URL = "https://feeds.example.com/show.xml"


def rss(title: str = "Test Show", items: int = 3) -> bytes:
    entries = "".join(
        f"""
        <item>
          <title>Episode {n}</title>
          <guid>ep-{n}</guid>
          <pubDate>Mon, {n + 1:02d} Sep 2026 10:00:00 GMT</pubDate>
          <enclosure url="https://cdn.example.com/ep{n}.mp3" type="audio/mpeg" length="1000"/>
          <itunes:duration>01:00:00</itunes:duration>
        </item>"""
        for n in range(items)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{title}</title>
    <link>https://example.com/show</link>
    <language>en</language>
    <itunes:author>The Host</itunes:author>
    {entries}
  </channel>
</rss>""".encode()


def answer(status: int = 200, body: bytes = b"", **headers: str):
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers)

    return respond


class SubscribeTests(ApiTestCase):
    def live_jobs(self, kind: str) -> list[jobs.Job]:
        return [job for job in jobs.list_jobs(self.ctx.db, kind=kind) if job.is_live]

    async def test_created_fetches_inline_and_admits_the_newest_episode(self) -> None:
        self.remote[FEED_URL] = answer(body=rss(), **{"content-type": "application/rss+xml", "etag": '"v1"'})
        response = await self.client.post("/api/v1/podcasts", json={"feedUrl": "https://FEEDS.example.com/show.xml#top"})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(set(body), {"podcast"})
        podcast = body["podcast"]
        self.assertEqual(set(podcast), set(doc_example("### Podcast")))
        self.assertEqual(podcast["feedUrl"], FEED_URL)
        self.assertEqual(podcast["title"], "Test Show")
        self.assertEqual(podcast["author"], "The Host")
        self.assertEqual(podcast["episodeCount"], 3)
        self.assertIs(podcast["autoProcessEnabled"], True)
        downloads = self.live_jobs("download")
        self.assertEqual(len(downloads), 1)
        newest = repo.get_episode(self.ctx.db, downloads[0].subject_id)
        self.assertEqual(newest.guid, "ep-2")
        self.assertEqual(newest.pipeline_state, "download_pending")
        self.assertGreaterEqual(self.scheduler.wakes, 1)

    async def test_existing_subscription_is_idempotent(self) -> None:
        self.remote[FEED_URL] = answer(body=rss())
        first = await self.client.post("/api/v1/podcasts", json={"feedUrl": FEED_URL})
        del self.remote[FEED_URL]  # a second fetch would now fail the test
        again = await self.client.post("/api/v1/podcasts", json={"feedUrl": "HTTPS://feeds.example.com/show.xml"})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(set(again.json()), {"podcast"})
        self.assertEqual(again.json()["podcast"]["id"], first.json()["podcast"]["id"])

    async def test_backfill_count_and_switches(self) -> None:
        self.remote[FEED_URL] = answer(body=rss(items=4))
        response = await self.client.post(
            "/api/v1/podcasts",
            json={"feedUrl": FEED_URL, "initialBackfillCount": 2, "adAnalysisEnabled": False, "ignored": "field"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIs(response.json()["podcast"]["adAnalysisEnabled"], False)
        self.assertEqual(len(self.live_jobs("download")), 2)

    async def test_auto_process_off_admits_nothing(self) -> None:
        self.remote[FEED_URL] = answer(body=rss())
        response = await self.client.post("/api/v1/podcasts", json={"feedUrl": FEED_URL, "autoProcessEnabled": False})
        self.assertEqual(response.status_code, 201)
        self.assertIs(response.json()["podcast"]["autoProcessEnabled"], False)
        self.assertEqual(self.live_jobs("download"), [])

    async def test_slow_fetch_continues_in_the_background(self) -> None:
        async def stall(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(30)
            raise AssertionError("the fetch should have been abandoned")

        self.remote[FEED_URL] = stall
        quick = functools.partial(refresher.subscribe, budget_seconds=0.05)
        with mock.patch.object(refresher, "subscribe", quick):
            response = await self.client.post("/api/v1/podcasts", json={"feedUrl": FEED_URL})
        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        self.assertEqual(set(body), {"podcast", "jobId"})
        self.assertEqual(body["podcast"]["feedUrl"], FEED_URL)
        job = jobs.get_job(self.ctx.db, body["jobId"])
        self.assertEqual((job.kind, job.subject_id, job.state), ("refresh_feed", body["podcast"]["id"], "pending"))

    async def test_not_a_feed_is_422_invalid_feed(self) -> None:
        self.remote[FEED_URL] = answer(body=b"<html><body>hello</body></html>", **{"content-type": "text/html"})
        response = await self.client.post("/api/v1/podcasts", json={"feedUrl": FEED_URL})
        self.assertError(response, 422, "invalidFeed")
        self.assertEqual(repo.list_podcasts(self.ctx.db), [])

    async def test_upstream_failures_are_502(self) -> None:
        async def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        for responder in (answer(500), answer(404), refuse):
            with self.subTest(responder=responder):
                self.remote[FEED_URL] = responder
                self.assertError(await self.client.post("/api/v1/podcasts", json={"feedUrl": FEED_URL}), 502, "upstreamFailed")
        self.assertEqual(repo.list_podcasts(self.ctx.db), [])

    async def test_bad_urls_and_bodies_are_invalid_requests(self) -> None:
        for body in (
            {"feedUrl": "ftp://feeds.example.com/show.xml"},
            {"feedUrl": "feeds.example.com/show.xml"},
            {"feedUrl": ""},
            {"feedUrl": FEED_URL, "initialBackfillCount": -1},
            {"url": FEED_URL},
        ):
            with self.subTest(body=body):
                self.assertError(await self.client.post("/api/v1/podcasts", json=body), 422, "invalidRequest")


class PodcastCommandTests(ApiTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.podcast = add_podcast(self.ctx)
        self.episode_ids = add_episodes(self.ctx, self.podcast.id, 3)

    async def test_patch_switches(self) -> None:
        response = await self.client.patch(f"/api/v1/podcasts/{self.podcast.id}", json={"adAnalysisEnabled": False})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), set(doc_example("### Podcast")))
        self.assertIs(body["adAnalysisEnabled"], False)
        self.assertIs(body["autoProcessEnabled"], True)
        self.assertGreater(body["seq"], self.podcast.updated_seq)
        unchanged = await self.client.patch(f"/api/v1/podcasts/{self.podcast.id}", json={"somethingElse": 1})
        self.assertEqual(unchanged.json()["seq"], body["seq"])
        both = await self.client.patch(
            f"/api/v1/podcasts/{self.podcast.id}", json={"auto_process_enabled": False, "adAnalysisEnabled": True}
        )
        self.assertEqual((both.json()["autoProcessEnabled"], both.json()["adAnalysisEnabled"]), (False, True))
        self.assertError(
            await self.client.patch(f"/api/v1/podcasts/{self.podcast.id}", json={"adAnalysisEnabled": "maybe"}),
            422,
            "invalidRequest",
        )

    async def test_delete_removes_rows_files_and_jobs(self) -> None:
        audio = store_audio(self.ctx, self.episode_ids[0])
        with self.ctx.db.write() as tx:
            job_id = commands.process_episode(
                tx, self.episode_ids[1], server=repo.load_server_settings(tx, self.settings), now=now_iso()
            )
        llm_dir = self.settings.llm_dir / str(self.episode_ids[0])
        llm_dir.mkdir(parents=True)
        (llm_dir / "1.json.gz").write_bytes(b"x")
        response = await self.client.delete(f"/api/v1/podcasts/{self.podcast.id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertIsNone(repo.get_podcast(self.ctx.db, self.podcast.id))
        self.assertError(await self.client.get(f"/api/v1/episodes/{self.episode_ids[0]}"), 404, "notFound")
        self.assertFalse(audio.exists())
        self.assertFalse(llm_dir.exists())
        self.assertEqual(jobs.get_job(self.ctx.db, job_id).state, "canceled")
        self.assertIn(job_id, self.scheduler.aborted)
        deletions = (await self.client.get("/api/v1/sync")).json()["deletions"]
        self.assertEqual(deletions, [{"entity": "podcast", "id": self.podcast.id}])
        self.assertError(await self.client.delete(f"/api/v1/podcasts/{self.podcast.id}"), 404, "notFound")

    async def test_refresh_one_and_all(self) -> None:
        other = add_podcast(self.ctx, feed_url="https://feeds.example.com/other.xml")
        response = await self.client.post(f"/api/v1/podcasts/{self.podcast.id}/refresh")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(set(response.json()), {"jobId"})
        job_id = response.json()["jobId"]
        self.assertEqual((await self.client.post(f"/api/v1/podcasts/{self.podcast.id}/refresh")).json()["jobId"], job_id)
        job = jobs.get_job(self.ctx.db, job_id)
        self.assertEqual((job.kind, job.subject_id), ("refresh_feed", self.podcast.id))
        everything = await self.client.post("/api/v1/refresh")
        self.assertEqual(everything.status_code, 202)
        job_ids = everything.json()["jobIds"]
        self.assertEqual(len(job_ids), 2)
        self.assertIn(job_id, job_ids)
        self.assertEqual({jobs.get_job(self.ctx.db, j).subject_id for j in job_ids}, {self.podcast.id, other.id})
        self.assertGreaterEqual(self.scheduler.wakes, 3)


OPML = b"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>My podcasts</title></head>
  <body>
    <outline text="News">
      <outline type="rss" text="Alpha" xmlUrl="https://feeds.example.com/alpha.xml"/>
      <outline type="rss" text="Beta" title="Beta Title" xmlURL="https://FEEDS.example.com/beta.xml"/>
    </outline>
    <outline type="rss" text="Alpha again" xmlUrl="https://feeds.example.com/alpha.xml#dup"/>
    <outline type="rss" text="Already here" xmlUrl="https://feeds.example.com/show.xml"/>
    <outline type="rss" text="Broken" xmlUrl="ftp://feeds.example.com/nope.xml"/>
    <outline type="rss" text="Broken again" xmlUrl="ftp://feeds.example.com/nope.xml"/>
    <outline type="rss" xmlUrl="https://feeds.example.com/bare.xml"/>
    <outline text="A folder with no feed"/>
  </body>
</opml>
"""


class OpmlTests(ApiTestCase):
    async def test_import_sorts_feeds_into_added_existing_failed(self) -> None:
        existing = add_podcast(self.ctx, feed_url=FEED_URL, title="Show")
        response = await self.client.post("/api/v1/opml", content=OPML, headers={"Content-Type": "text/x-opml"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), {"added", "existing", "failed"})
        self.assertEqual(
            [(p["feedUrl"], p["title"]) for p in body["added"]],
            [
                ("https://feeds.example.com/alpha.xml", "Alpha"),
                ("https://feeds.example.com/beta.xml", "Beta Title"),
                ("https://feeds.example.com/bare.xml", "https://feeds.example.com/bare.xml"),
            ],
        )
        self.assertEqual([p["id"] for p in body["existing"]], [existing.id])
        self.assertEqual(len(body["failed"]), 1)
        self.assertEqual(set(body["failed"][0]), {"feedUrl", "error"})
        self.assertEqual(body["failed"][0]["feedUrl"], "ftp://feeds.example.com/nope.xml")
        self.assertEqual(set(body["added"][0]), set(doc_example("### Podcast")))
        refreshes = [job for job in jobs.list_jobs(self.ctx.db, kind="refresh_feed") if job.is_live]
        self.assertEqual({job.subject_id for job in refreshes}, {p["id"] for p in body["added"]})
        self.assertGreaterEqual(self.scheduler.wakes, 1)
        again = (await self.client.post("/api/v1/opml", content=OPML)).json()
        self.assertEqual(again["added"], [])
        self.assertEqual(len(again["existing"]), 4)

    async def test_export_round_trips(self) -> None:
        add_podcast(self.ctx, feed_url="https://feeds.example.com/a.xml", title="Alpha & <Friends>")
        add_podcast(self.ctx, feed_url="https://feeds.example.com/b.xml?format=rss&x=1", title="Beta")
        response = await self.client.get("/api/v1/opml")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/x-opml"))
        root = ET.fromstring(response.content)
        self.assertEqual((root.tag, root.get("version")), ("opml", "2.0"))
        outlines = {o.get("xmlUrl"): o.get("text") for o in root.iter("outline")}
        self.assertEqual(
            outlines,
            {"https://feeds.example.com/a.xml": "Alpha & <Friends>", "https://feeds.example.com/b.xml?format=rss&x=1": "Beta"},
        )
        for podcast in repo.list_podcasts(self.ctx.db):
            self.assertEqual((await self.client.delete(f"/api/v1/podcasts/{podcast.id}")).status_code, 204)
        reimported = (await self.client.post("/api/v1/opml", content=response.content)).json()
        self.assertEqual({(p["feedUrl"], p["title"]) for p in reimported["added"]}, set(outlines.items()))
        self.assertEqual((reimported["existing"], reimported["failed"]), ([], []))

    async def test_malformed_documents_are_rejected(self) -> None:
        for document in (b"<opml><body>", b"", rss(), b"not xml at all"):
            with self.subTest(document=document[:20]):
                self.assertError(await self.client.post("/api/v1/opml", content=document), 422, "invalidRequest")
        self.assertEqual(repo.list_podcasts(self.ctx.db), [])

    async def test_oversized_documents_are_rejected(self) -> None:
        with mock.patch.object(settings_routes, "MAX_OPML_BYTES", 64):
            self.assertError(await self.client.post("/api/v1/opml", content=OPML), 413, "payloadTooLarge")
