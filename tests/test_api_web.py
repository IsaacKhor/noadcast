"""Dashboard assets and bounded list endpoints."""

from __future__ import annotations

import httpx

from noadcast.db import repo
from noadcast.timeutil import now_iso

from tests.api_support import ApiTestCase, add_episodes, add_podcast, set_markers


class WebDashboardTests(ApiTestCase):
    auth_enabled = True

    async def test_only_fixed_get_head_assets_are_public(self) -> None:
        for path, content_type in (
            ("/", "text/html"),
            ("/web/app.css", "text/css"),
            ("/web/app.js", "text/javascript"),
        ):
            with self.subTest(path=path):
                response = await self.anon.get(path, headers={"Accept-Encoding": "identity"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn(content_type, response.headers["content-type"])
                self.assertTrue(response.content)
                self.assertIn("default-src 'none'", response.headers["content-security-policy"])
                self.assertEqual(response.headers["cache-control"], "no-store")
                head = await self.anon.head(path, headers={"Accept-Encoding": "identity"})
                self.assertEqual(head.status_code, 200)
                self.assertEqual(head.content, b"")
                self.assertEqual(head.headers["content-length"], response.headers["content-length"])
                self.assertError(await self.anon.post(path), 401, "unauthorized")
        for path in ("/web/missing.js", "/web/app.js.map", "/favicon.ico", "/api/v1/settings"):
            with self.subTest(path=path):
                self.assertError(await self.anon.get(path), 401, "unauthorized")

    async def test_shell_works_under_root_path(self) -> None:
        transport = httpx.ASGITransport(app=self.app, root_path="/nc")
        async with httpx.AsyncClient(transport=transport, base_url="http://test/nc") as client:
            response = await client.get("/")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn(b'web/app.js', response.content)
            self.assertEqual((await client.get("/web/app.js")).status_code, 200)
            self.assertError(await client.get("/api/v1/podcasts"), 401, "unauthorized")

    async def test_podcast_and_episode_lists_filter_page_and_keep_markers(self) -> None:
        first = add_podcast(self.ctx, feed_url="https://example.com/a.xml", title="A Show")
        second = add_podcast(self.ctx, feed_url="https://example.com/b.xml", title="B Show")
        old_id, new_id = add_episodes(self.ctx, first.id, 2, title="A 100% match")
        other_id = add_episodes(self.ctx, second.id, 1, start=9, title="Other title")[0]
        set_markers(self.ctx, new_id, [(0.0, 12.0, "intro", "Opening")])
        with self.ctx.db.write() as tx:
            repo.set_episode_states(tx, new_id, pipeline_state="ready", now=now_iso())

        self.assertError(await self.anon.get("/api/v1/podcasts"), 401, "unauthorized")
        self.assertError(await self.anon.get("/api/v1/episodes"), 401, "unauthorized")
        podcasts = (await self.client.get("/api/v1/podcasts")).json()["items"]
        self.assertEqual([p["id"] for p in podcasts], [first.id, second.id])
        self.assertTrue(all("adAnalysisEnabled" in p for p in podcasts))

        response = await self.client.get("/api/v1/episodes", params={"limit": 2})
        self.assertEqual(response.status_code, 200, response.text)
        page = response.json()
        self.assertEqual((page["total"], page["limit"], page["offset"]), (3, 2, 0))
        self.assertEqual([e["id"] for e in page["items"]], [other_id, new_id])
        self.assertEqual(page["items"][1]["adMarkers"][0]["summary"], "Opening")
        next_page = (await self.client.get("/api/v1/episodes", params={"limit": 2, "offset": 2})).json()
        self.assertEqual([e["id"] for e in next_page["items"]], [old_id])
        filtered = (await self.client.get("/api/v1/episodes", params={"podcastId": first.id, "state": "ready"})).json()
        self.assertEqual((filtered["total"], [e["id"] for e in filtered["items"]]), (1, [new_id]))
        literal = (await self.client.get("/api/v1/episodes", params={"q": "100%"})).json()
        self.assertEqual(literal["total"], 2)
        no_wildcard = (await self.client.get("/api/v1/episodes", params={"q": "%_"})).json()
        self.assertEqual(no_wildcard["total"], 0)
        past_end = (await self.client.get("/api/v1/episodes", params={"offset": 100})).json()
        self.assertEqual((past_end["total"], past_end["items"]), (3, []))

    async def test_list_query_bounds(self) -> None:
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"podcastId": 0}, {"q": "x" * 201}):
            with self.subTest(params=params):
                self.assertError(await self.client.get("/api/v1/episodes", params=params), 422, "invalidRequest")

    async def test_analysis_settings_and_podcast_exclusion_workflow(self) -> None:
        podcast = add_podcast(self.ctx)
        episode_id = add_episodes(self.ctx, podcast.id, 1)[0]
        set_markers(self.ctx, episode_id, [(0.0, 9.0, "intro", "Sponsor")])
        model = "openai/gpt-6-luna"

        response = await self.client.patch("/api/v1/settings", json={"classifierModel": model})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.client.get("/api/v1/settings")).json()["classifierModel"], model)
        seq_after_model = self.ctx.db.current_seq()
        self.assertEqual((await self.client.patch("/api/v1/settings", json={"classifierModel": model})).status_code, 200)
        self.assertEqual(self.ctx.db.current_seq(), seq_after_model)

        response = await self.client.patch(f"/api/v1/podcasts/{podcast.id}", json={"adAnalysisEnabled": False})
        self.assertEqual(response.status_code, 200, response.text)
        listed = (await self.client.get("/api/v1/podcasts")).json()["items"]
        self.assertEqual(len(listed), 1)
        self.assertIs(listed[0]["adAnalysisEnabled"], False)
        markers = (await self.client.get("/api/v1/episodes", params={"podcastId": podcast.id})).json()["items"][0]["adMarkers"]
        self.assertEqual([marker["summary"] for marker in markers], ["Sponsor"])
        seq_after_exclusion = self.ctx.db.current_seq()
        self.assertEqual(
            (await self.client.patch(f"/api/v1/podcasts/{podcast.id}", json={"adAnalysisEnabled": False})).status_code,
            200,
        )
        self.assertEqual(self.ctx.db.current_seq(), seq_after_exclusion)
