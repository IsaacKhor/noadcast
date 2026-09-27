"""GET /api/v1/sync through HTTP, and DTO shapes against docs/API.md."""

from __future__ import annotations

import datetime as dt

from noadcast.db import repo
from noadcast.timeutil import iso, utc_now

from tests.api_support import ApiTestCase, add_episodes, add_podcast, doc_example, set_markers, store_audio


class ContractShapeTests(ApiTestCase):
    """Key sets must match the documented examples exactly; clients decode by these names."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.podcast = add_podcast(self.ctx)
        [self.episode_id] = add_episodes(self.ctx, self.podcast.id, 1)
        set_markers(self.ctx, self.episode_id, [(0.0, 78.4, "intro", "Theme music")])

    async def test_podcast_episode_and_settings_objects(self) -> None:
        body = (await self.client.get("/api/v1/sync")).json()
        podcast, episode, settings = body["podcasts"][0], body["episodes"][0], body["settings"]
        self.assertEqual(set(podcast), set(doc_example("### Podcast")))
        documented_episode = doc_example("### Episode")
        self.assertEqual(set(episode), set(documented_episode))
        self.assertEqual(set(episode["adMarkers"][0]), set(documented_episode["adMarkers"][0]))
        self.assertEqual(set(settings), set(doc_example("### Settings")))
        self.assertEqual(set(settings["availableClassifiers"]), set(doc_example("### Settings")["availableClassifiers"]))

    async def test_sync_envelope(self) -> None:
        body = (await self.client.get("/api/v1/sync")).json()
        self.assertEqual(
            set(body),
            {"instanceId", "podcasts", "episodes", "deletions", "settings", "nextSince", "hasMore", "serverTime"},
        )
        self.assertEqual(body["instanceId"], self.ctx.db.instance_id)

    async def test_episode_values(self) -> None:
        episode = (await self.client.get(f"/api/v1/episodes/{self.episode_id}")).json()
        self.assertEqual(episode["podcastId"], self.podcast.id)
        self.assertEqual(episode["durationSeconds"], 3600.0)
        self.assertIs(episode["durationIsMeasured"], False)
        self.assertEqual(episode["state"], "discovered")
        self.assertEqual(episode["audioState"], "absent")
        self.assertIsNone(episode["error"])
        self.assertEqual(episode["markerRevision"], 1)
        self.assertEqual(
            episode["adMarkers"],
            [
                {
                    "id": episode["adMarkers"][0]["id"],
                    "startSeconds": 0.0,
                    "endSeconds": 78.4,
                    "kind": "intro",
                    "summary": "Theme music",
                    "source": "auto",
                }
            ],
        )
        store_audio(self.ctx, self.episode_id)
        measured = (await self.client.get(f"/api/v1/episodes/{self.episode_id}")).json()
        self.assertEqual(measured["durationSeconds"], 3599.5)
        self.assertIs(measured["durationIsMeasured"], True)
        self.assertEqual(measured["audioState"], "present")
        self.assertEqual(measured["audioContentType"], "audio/mpeg")
        self.assertEqual(len(measured["audioSha256"]), 64)
        self.assertGreater(measured["seq"], episode["seq"])


class SyncPagingTests(ApiTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.shows = [add_podcast(self.ctx, feed_url=f"https://feeds.example.com/{n}.xml", title=f"Show {n}") for n in range(2)]
        self.episodes = {show.id: add_episodes(self.ctx, show.id, 3, start=10 * n) for n, show in enumerate(self.shows)}
        doomed = add_podcast(self.ctx, feed_url="https://feeds.example.com/doomed.xml")
        add_episodes(self.ctx, doomed.id, 1, start=90)
        self.assertEqual((await self.client.delete(f"/api/v1/podcasts/{doomed.id}")).status_code, 204)
        self.doomed_id = doomed.id
        patched = await self.client.patch("/api/v1/settings", json={"adAnalysisEnabled": False})
        self.assertEqual(patched.status_code, 200)

    async def page(self, since: int, limit: int) -> dict:
        response = await self.client.get("/api/v1/sync", params={"since": since, "limit": limit})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_paging_sees_every_row_exactly_once_and_keeps_closure(self) -> None:
        since, pages, seen_episodes, seen_podcasts, deletions, settings = 0, 0, [], set(), [], []
        while True:
            body = await self.page(since, 2)
            pages += 1
            page_podcasts = {p["id"] for p in body["podcasts"]}
            self.assertLessEqual({e["podcastId"] for e in body["episodes"]}, page_podcasts, "referential closure")
            rows = [p["seq"] for p in body["podcasts"] if p["seq"] > since] + [e["seq"] for e in body["episodes"]]
            self.assertTrue(all(since < seq <= body["nextSince"] for seq in rows))
            self.assertGreater(body["nextSince"], since)
            seen_episodes += [e["id"] for e in body["episodes"]]
            seen_podcasts |= page_podcasts
            deletions += body["deletions"]
            settings.append(body["settings"])
            since = body["nextSince"]
            if not body["hasMore"]:
                break
        self.assertGreater(pages, 3)
        expected = [episode_id for ids in self.episodes.values() for episode_id in ids]
        self.assertEqual(sorted(seen_episodes), sorted(expected))
        self.assertEqual(seen_podcasts, {show.id for show in self.shows})
        self.assertEqual(deletions, [{"entity": "podcast", "id": self.doomed_id}])
        self.assertIsNotNone(settings[0], "since=0 always carries settings")
        self.assertIs(settings[-1]["adAnalysisEnabled"], False)
        self.assertTrue(all(s is None for s in settings[1:-1]))
        self.assertEqual(since, self.ctx.db.current_seq())
        idle = await self.page(since, 2)
        self.assertEqual((idle["podcasts"], idle["episodes"], idle["deletions"]), ([], [], []))
        self.assertIsNone(idle["settings"])
        self.assertEqual(idle["nextSince"], since)
        self.assertIs(idle["hasMore"], False)

    async def test_delta_includes_the_changed_episodes_podcast(self) -> None:
        cursor = (await self.page(0, 1000))["nextSince"]
        episode_id = self.episodes[self.shows[1].id][0]
        set_markers(self.ctx, episode_id, [(600.0, 660.0, "ad", "Mattress"), (0.0, 30.0, "intro", "Theme")])
        body = await self.page(cursor, 200)
        self.assertEqual([e["id"] for e in body["episodes"]], [episode_id])
        self.assertEqual([p["id"] for p in body["podcasts"]], [self.shows[1].id])
        self.assertEqual([m["startSeconds"] for m in body["episodes"][0]["adMarkers"]], [0.0, 600.0])
        self.assertIsNone(body["settings"])
        set_markers(self.ctx, episode_id, [(5.0, 25.0, "intro", "Theme")])
        replaced = await self.page(body["nextSince"], 200)
        self.assertEqual([m["summary"] for m in replaced["episodes"][0]["adMarkers"]], ["Theme"])

    async def test_settings_change_arrives_with_the_next_page(self) -> None:
        cursor = (await self.page(0, 1000))["nextSince"]
        await self.client.patch("/api/v1/settings", json={"classifierModel": "qwen/qwen3.8-flash"})
        body = await self.page(cursor, 200)
        self.assertEqual(body["settings"]["classifier"], "openrouter")
        self.assertEqual(body["settings"]["classifierModel"], "qwen/qwen3.8-flash")

    async def test_limit_is_clamped(self) -> None:
        tiny = await self.page(0, 0)
        self.assertEqual(tiny["nextSince"], 1, "limit clamps to one settings row")
        self.assertIsNotNone(tiny["settings"])
        self.assertIs(tiny["hasMore"], True)
        huge = await self.page(0, 50_000)
        self.assertIs(huge["hasMore"], False)
        self.assertEqual(len(huge["episodes"]), 6)

    async def test_cursor_below_the_tombstone_floor_is_410(self) -> None:
        with self.ctx.db.write() as tx:
            pruned = repo.prune_tombstones(tx, deleted_before=iso(utc_now() + dt.timedelta(days=1)))
        self.assertEqual(pruned, 1)
        floor = repo.tombstone_floor(self.ctx.db)
        response = await self.client.get("/api/v1/sync", params={"since": floor - 1})
        self.assertError(response, 410, "cursorExpired")
        self.assertEqual((await self.client.get("/api/v1/sync", params={"since": floor})).status_code, 200)
        self.assertEqual((await self.client.get("/api/v1/sync")).status_code, 200)

    async def test_cursor_ahead_of_the_database_is_410(self) -> None:
        ahead = self.ctx.db.current_seq() + 1
        self.assertError(await self.client.get("/api/v1/sync", params={"since": ahead}), 410, "cursorExpired")

    async def test_negative_cursor_is_invalid(self) -> None:
        self.assertError(await self.client.get("/api/v1/sync", params={"since": -1}), 422, "invalidRequest")

    async def test_access_bookkeeping_does_not_bump_seq(self) -> None:
        episode_id = self.episodes[self.shows[0].id][0]
        store_audio(self.ctx, episode_id)
        cursor = (await self.page(0, 1000))["nextSince"]
        self.assertEqual((await self.client.get(f"/api/v1/episodes/{episode_id}/audio")).status_code, 200)
        row = repo.get_episode(self.ctx.db, episode_id)
        self.assertIsNotNone(row.audio_last_access_at)
        self.assertEqual(self.ctx.db.current_seq(), cursor)
        self.assertEqual((await self.page(cursor, 200))["episodes"], [])
