"""Authentication, signed audio URLs, the error envelope, and create_app guards."""

from __future__ import annotations

import os
import re
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.routing import iter_route_contexts
from starlette.datastructures import QueryParams

from noadcast import __version__
from noadcast.api import auth
from noadcast.api.app import create_app
from noadcast.config import settings_for_tests

from tests.api_support import TOKEN, ApiTestCase, add_episodes, add_podcast, doc_example, store_audio


class BearerAuthTests(ApiTestCase):
    auth_enabled = True

    async def test_health_is_public_and_matches_the_contract(self) -> None:
        response = await self.anon.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), set(doc_example("### `GET /health`")))
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["version"], __version__)
        self.assertEqual(body["apiVersion"], 1)
        self.assertEqual(body["instanceId"], self.ctx.db.instance_id)
        self.assertIs(body["authRequired"], True)
        self.assertEqual(body["capabilities"], ["sync", "jobsActive", "audioUrl", "releaseAudio", "usage", "opml"])

    async def test_missing_token_is_401_with_challenge(self) -> None:
        response = await self.anon.get("/api/v1/session")
        body = self.assertError(response, 401, "unauthorized")
        self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(set(body), {"error"})

    async def test_wrong_token_or_scheme_is_401(self) -> None:
        for header in (f"Bearer {TOKEN}x", f"Bearer {TOKEN[:-1]}", f"Basic {TOKEN}", TOKEN, "Bearer", "Bearer "):
            with self.subTest(header=header):
                response = await self.anon.get("/api/v1/session", headers={"Authorization": header})
                self.assertError(response, 401, "unauthorized")
                self.assertEqual(response.headers["www-authenticate"], "Bearer")

    async def test_right_token_opens_a_session(self) -> None:
        response = await self.client.get("/api/v1/session")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"authenticated", "serverTime"})
        self.assertIs(body["authenticated"], True)
        self.assertRegex(body["serverTime"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        lowercase = await self.anon.get("/api/v1/session", headers={"Authorization": f"bearer {TOKEN}"})
        self.assertEqual(lowercase.status_code, 200)

    async def test_token_is_never_accepted_in_the_query_string(self) -> None:
        for name in ("token", "access_token", "api_token", "apiToken", "authorization"):
            with self.subTest(name=name):
                response = await self.anon.get("/api/v1/session", params={name: TOKEN})
                self.assertError(response, 401, "unauthorized")

    async def test_every_api_route_requires_auth(self) -> None:
        checked = 0
        for route in iter_route_contexts(self.app.routes):
            if not route.path.startswith("/api/v1"):
                continue
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            for method in route.methods:
                with self.subTest(method=method, path=path):
                    response = await self.anon.request(method, path)
                    self.assertEqual(response.status_code, 401, response.text)
                    checked += 1
        self.assertGreaterEqual(checked, 26)

    async def test_unknown_paths_are_private_too(self) -> None:
        self.assertError(await self.anon.get("/api/v1/nope"), 401, "unauthorized")
        self.assertError(await self.anon.get("/favicon.ico"), 401, "unauthorized")
        self.assertError(await self.client.get("/api/v1/nope"), 404, "notFound")
        self.assertError(await self.client.get("/docs"), 404, "notFound")
        self.assertError(await self.client.get("/openapi.json"), 404, "notFound")


class SignedAudioUrlTests(ApiTestCase):
    auth_enabled = True

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        podcast = add_podcast(self.ctx)
        self.episode_id, self.other_id = add_episodes(self.ctx, podcast.id, 2)
        self.audio = store_audio(self.ctx, self.episode_id).read_bytes()
        store_audio(self.ctx, self.other_id)

    async def mint(self, episode_id: int | None = None) -> str:
        response = await self.client.post(f"/api/v1/episodes/{episode_id or self.episode_id}/audio-url")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["path"]

    def signed(self, episode_id: int, exp: int) -> str:
        return f"exp={exp}&sig={auth.audio_signature(self.settings.signing_secret, episode_id, exp)}"

    async def test_signed_url_streams_without_a_header(self) -> None:
        path = await self.mint()
        response = await self.anon.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, self.audio)
        head = await self.anon.head(path)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.headers["content-length"], str(len(self.audio)))
        ranged = await self.anon.get(path, headers={"Range": "bytes=0-9"})
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, self.audio[:10])

    async def test_signed_audio_read_after_played_does_not_restart_download(self) -> None:
        path = await self.mint()
        self.assertEqual((await self.client.delete(f"/api/v1/episodes/{self.episode_id}/audio")).status_code, 204)
        response = await self.anon.get(path)
        self.assertError(response, 409, "audioEvicted")
        self.assertIsNone(response.json()["jobId"])
        self.assertEqual((await self.anon.head(path)).status_code, 409)
        self.assertIsNone(self.ctx.db.read_one(
            "SELECT id FROM jobs WHERE subject_id = ? AND kind = 'download' AND state IN ('pending', 'running')",
            (self.episode_id,),
        ))

    async def test_bearer_also_streams(self) -> None:
        response = await self.client.get(f"/api/v1/episodes/{self.episode_id}/audio")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.audio)

    async def test_signature_is_bound_to_its_episode(self) -> None:
        path = await self.mint()
        query = path.partition("?")[2]
        response = await self.anon.get(f"/api/v1/episodes/{self.other_id}/audio?{query}")
        body = self.assertError(response, 401, "unauthorized")
        self.assertIn("signature", body["error"]["message"])

    async def test_signature_works_nowhere_but_audio_get_and_head(self) -> None:
        query = (await self.mint()).partition("?")[2]
        attempts = [
            ("GET", f"/api/v1/episodes/{self.episode_id}"),
            ("GET", f"/api/v1/episodes/{self.episode_id}/transcript"),
            ("POST", f"/api/v1/episodes/{self.episode_id}/audio-url"),
            ("DELETE", f"/api/v1/episodes/{self.episode_id}/audio"),
            ("GET", "/api/v1/sync"),
        ]
        for method, path in attempts:
            with self.subTest(method=method, path=path):
                self.assertError(await self.anon.request(method, f"{path}?{query}"), 401, "unauthorized")
        self.assertEqual(self.ctx.db.read_one("SELECT audio_state FROM episodes WHERE id = ?", (self.episode_id,))[0], "present")

    async def test_expired_signature_is_rejected(self) -> None:
        past = int(time.time()) - 1
        response = await self.anon.get(f"/api/v1/episodes/{self.episode_id}/audio?{self.signed(self.episode_id, past)}")
        body = self.assertError(response, 401, "unauthorized")
        self.assertEqual(body["error"]["message"], "audio URL expired")

    async def test_tampered_signatures_are_rejected(self) -> None:
        path = await self.mint()
        exp = int(re.search(r"exp=(\d+)", path).group(1))
        sig = re.search(r"sig=([0-9a-f]+)", path).group(1)
        flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
        base = f"/api/v1/episodes/{self.episode_id}/audio"
        for query in (
            f"exp={exp}&sig={flipped}",
            f"exp={exp + 1}&sig={sig}",
            f"exp={exp}&sig={sig.upper()}",
            f"exp={exp}",
            f"sig={sig}",
            f"exp={exp}&sig={sig}&sig={sig}",
            f"exp=-{exp}&sig={sig}",
            f"exp={exp}.0&sig={sig}",
            f"exp={'9' * 40}&sig={sig}",
        ):
            with self.subTest(query=query):
                self.assertError(await self.anon.get(f"{base}?{query}"), 401, "unauthorized")

    async def test_a_new_signing_secret_invalidates_old_urls(self) -> None:
        query = QueryParams((await self.mint()).partition("?")[2])
        now = time.time()
        self.assertIsNone(auth.signature_failure(query, self.settings.signing_secret, self.episode_id, now=now))
        self.assertEqual(
            auth.signature_failure(query, b"rotated-secret", self.episode_id, now=now), "invalid audio URL signature"
        )

    def test_route_path_strips_root_path_like_starlette(self) -> None:
        self.assertEqual(auth.route_path({"path": "/api/v1/sync", "root_path": ""}), "/api/v1/sync")
        self.assertEqual(auth.route_path({"path": "/nc/api/v1/sync", "root_path": "/nc"}), "/api/v1/sync")
        self.assertEqual(auth.route_path({"path": "/ncx/health", "root_path": "/nc"}), "/ncx/health")


class NoAuthTests(ApiTestCase):
    auth_enabled = False

    async def test_everything_is_open_when_auth_is_disabled(self) -> None:
        self.assertEqual((await self.anon.get("/api/v1/session")).status_code, 200)
        self.assertIs((await self.anon.get("/health")).json()["authRequired"], False)


class ErrorEnvelopeTests(ApiTestCase):
    async def test_unknown_route_and_wrong_method(self) -> None:
        body = self.assertError(await self.client.get("/api/v1/nope"), 404, "notFound")
        self.assertIn("/api/v1/nope", body["error"]["message"])
        response = await self.client.put("/api/v1/session")
        self.assertError(response, 405, "methodNotAllowed")
        self.assertEqual(response.headers["allow"], "GET")

    async def test_validation_errors_use_the_envelope(self) -> None:
        body = self.assertError(await self.client.get("/api/v1/sync", params={"since": "abc"}), 422, "invalidRequest")
        self.assertIn("since", body["error"]["message"])
        self.assertError(await self.client.get("/api/v1/episodes/abc"), 422, "invalidRequest")
        self.assertError(await self.client.get("/api/v1/jobs", params={"limit": 0}), 422, "invalidRequest")
        self.assertError(await self.client.post("/api/v1/podcasts", json={}), 422, "invalidRequest")
        malformed = await self.client.post(
            "/api/v1/podcasts", content=b"{nope", headers={"Content-Type": "application/json"}
        )
        self.assertError(malformed, 422, "invalidRequest")

    async def test_missing_subjects_are_404(self) -> None:
        body = self.assertError(await self.client.get("/api/v1/episodes/999"), 404, "notFound")
        self.assertEqual(body["error"]["message"], "episode 999 not found")
        self.assertError(await self.client.patch("/api/v1/podcasts/999", json={}), 404, "notFound")
        self.assertError(await self.client.delete("/api/v1/podcasts/999"), 404, "notFound")
        self.assertError(await self.client.post("/api/v1/podcasts/999/refresh"), 404, "notFound")
        self.assertError(await self.client.post("/api/v1/episodes/999/process"), 404, "notFound")
        self.assertError(await self.client.post("/api/v1/episodes/999/reanalyze"), 404, "notFound")
        self.assertError(await self.client.post("/api/v1/jobs/999/retry"), 404, "notFound")
        self.assertError(await self.client.delete("/api/v1/jobs/999"), 404, "notFound")

    async def test_uncaught_exception_is_a_500_without_details(self) -> None:
        async def explode() -> None:
            raise RuntimeError("secret internals /home/ikhor")

        self.app.add_api_route("/api/v1/explode", explode)
        with self.assertLogs("noadcast.api", "ERROR") as logs:
            response = await self.client.get("/api/v1/explode")
        body = self.assertError(response, 500, "internal")
        self.assertNotIn("secret", response.text)
        self.assertNotIn("Traceback", response.text)
        self.assertIn(response.headers["x-request-id"], body["error"]["message"])
        self.assertTrue(any("secret internals" in line for line in logs.output))

    async def test_every_response_carries_a_request_id(self) -> None:
        first = (await self.client.get("/health")).headers["x-request-id"]
        second = (await self.client.get("/api/v1/nope")).headers["x-request-id"]
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.assertNotEqual(first, second)


class CreateAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = settings_for_tests(Path(".cache/tmp/unused"))

    def test_refuses_more_than_one_worker(self) -> None:
        with self.assertRaises(ValueError):
            create_app(self.settings, workers=2)
        with self.assertRaises(ValueError):
            create_app(self.settings, workers=0)

    def test_refuses_web_concurrency(self) -> None:
        with mock.patch.dict(os.environ, {"WEB_CONCURRENCY": "4"}):
            with self.assertRaises(ValueError):
                create_app(self.settings)
        with mock.patch.dict(os.environ, {"WEB_CONCURRENCY": "lots"}):
            with self.assertRaises(ValueError):
                create_app(self.settings)
        with mock.patch.dict(os.environ, {"WEB_CONCURRENCY": "1"}):
            create_app(self.settings)
