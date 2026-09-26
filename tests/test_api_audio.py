"""Audio: signed URLs, 409s for missing audio, Range serving, retention release."""

from __future__ import annotations

import time
from unittest import mock

from starlette.responses import Response

from noadcast.api.routers import audio as audio_routes
from noadcast.db import repo
from noadcast.media import ranges
from noadcast.pipeline import jobs

from tests.api_support import AUDIO_BYTES, ApiTestCase, add_episodes, add_podcast, store_audio


class AudioTestCase(ApiTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.podcast = add_podcast(self.ctx)
        self.present_id, self.absent_id = add_episodes(self.ctx, self.podcast.id, 2)
        self.path = store_audio(self.ctx, self.present_id)

    def episode(self, episode_id: int) -> repo.Episode:
        episode = repo.get_episode(self.ctx.db, episode_id)
        assert episode is not None
        return episode

    def assertAudioMissing(self, response, code: str, episode_id: int) -> int:
        body = self.assertError(response, 409, code)
        self.assertEqual(response.headers["retry-after"], "5")
        job = jobs.get_job(self.ctx.db, body["jobId"])
        self.assertEqual((job.kind, job.subject_id, job.state), ("download", episode_id, "pending"))
        self.assertEqual(job.priority, jobs.PRIORITY_INTERACTIVE)
        return job.id


class AudioUrlTests(AudioTestCase):
    async def test_mints_a_signed_url(self) -> None:
        before = int(time.time())
        response = await self.client.post(f"/api/v1/episodes/{self.present_id}/audio-url")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), {"path", "url", "expiresAt"})
        self.assertRegex(body["path"], rf"^/api/v1/episodes/{self.present_id}/audio\?exp=(\d+)&sig=[0-9a-f]{{64}}$")
        self.assertEqual(body["url"], "http://test" + body["path"])
        exp = int(body["path"].split("exp=")[1].split("&")[0])
        self.assertAlmostEqual(exp - before, self.settings.audio_url_ttl_seconds, delta=2)
        self.assertRegex(body["expiresAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000Z$")
        streamed = await self.anon.get(body["path"])
        self.assertEqual(streamed.content, AUDIO_BYTES)

    async def test_absent_audio_is_409_not_ready_and_enqueues_once(self) -> None:
        response = await self.client.post(f"/api/v1/episodes/{self.absent_id}/audio-url")
        job_id = self.assertAudioMissing(response, "audioNotReady", self.absent_id)
        again = await self.client.post(f"/api/v1/episodes/{self.absent_id}/audio-url")
        self.assertEqual(again.json()["jobId"], job_id)
        self.assertGreaterEqual(self.scheduler.wakes, 2)
        self.assertEqual(self.episode(self.absent_id).pipeline_state, "download_pending")

    async def test_evicted_audio_is_409_evicted(self) -> None:
        self.assertEqual((await self.client.delete(f"/api/v1/episodes/{self.present_id}/audio")).status_code, 204)
        response = await self.client.post(f"/api/v1/episodes/{self.present_id}/audio-url")
        self.assertAudioMissing(response, "audioEvicted", self.present_id)

    async def test_a_file_missing_from_disk_is_recorded_and_redownloaded(self) -> None:
        self.path.unlink()
        with self.assertLogs(audio_routes.log, "WARNING"):
            response = await self.client.post(f"/api/v1/episodes/{self.present_id}/audio-url")
        self.assertAudioMissing(response, "audioEvicted", self.present_id)
        episode = self.episode(self.present_id)
        self.assertEqual((episode.audio_state, episode.audio_evicted_reason), ("evicted", "missing"))
        self.assertEqual(episode.audio_bytes, len(AUDIO_BYTES), "size and hash survive eviction")

    async def test_a_truncated_file_counts_as_missing(self) -> None:
        self.path.write_bytes(AUDIO_BYTES[:100])
        with self.assertLogs(audio_routes.log, "WARNING") as logs:
            response = await self.client.get(f"/api/v1/episodes/{self.present_id}/audio")
        self.assertEqual(logs.records[0].found_bytes, 100)
        self.assertAudioMissing(response, "audioEvicted", self.present_id)

    async def test_unknown_episode_is_404(self) -> None:
        self.assertError(await self.client.post("/api/v1/episodes/999/audio-url"), 404, "notFound")
        self.assertError(await self.client.get("/api/v1/episodes/999/audio"), 404, "notFound")


class AudioServingTests(AudioTestCase):
    async def test_full_get_and_head(self) -> None:
        url = f"/api/v1/episodes/{self.present_id}/audio"
        response = await self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, AUDIO_BYTES)
        episode = self.episode(self.present_id)
        self.assertEqual(response.headers["content-type"], "audio/mpeg")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["etag"], f'"{episode.audio_sha256}"')
        self.assertEqual(response.headers["content-length"], str(len(AUDIO_BYTES)))
        self.assertIn("last-modified", response.headers)
        self.assertIn(f'filename="{self.present_id}.mp3"', response.headers["content-disposition"])
        head = await self.client.head(url)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        for name in ("content-type", "content-length", "etag", "accept-ranges", "last-modified"):
            self.assertEqual(head.headers[name], response.headers[name], name)

    async def test_ranges_and_validators(self) -> None:
        url = f"/api/v1/episodes/{self.present_id}/audio"
        size = len(AUDIO_BYTES)
        partial = await self.client.get(url, headers={"Range": "bytes=100-199"})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, AUDIO_BYTES[100:200])
        self.assertEqual(partial.headers["content-range"], f"bytes 100-199/{size}")
        suffix = await self.client.get(url, headers={"Range": "bytes=-10"})
        self.assertEqual(suffix.content, AUDIO_BYTES[-10:])
        unsatisfiable = await self.client.get(url, headers={"Range": f"bytes={size}-"})
        self.assertEqual(unsatisfiable.status_code, 416)
        self.assertEqual(unsatisfiable.headers["content-range"], f"bytes */{size}")
        etag = partial.headers["etag"]
        self.assertEqual((await self.client.get(url, headers={"If-None-Match": etag})).status_code, 304)
        stale = await self.client.get(url, headers={"Range": "bytes=0-9", "If-Range": '"stale"'})
        self.assertEqual((stale.status_code, len(stale.content)), (200, size))

    async def test_audio_is_never_content_encoded(self) -> None:
        url = f"/api/v1/episodes/{self.present_id}/audio"
        gz = {"Accept-Encoding": "gzip"}
        for headers in (gz, {**gz, "Range": "bytes=0-99"}):
            with self.subTest(headers=headers):
                response = await self.client.get(url, headers=headers)
                self.assertNotIn("content-encoding", response.headers)
        # Even with a type the gzip middleware would otherwise compress.
        def as_octets(request, path, **kwargs) -> Response:
            return Response(path.read_bytes(), media_type="application/octet-stream")

        with mock.patch.object(ranges, "audio_file_response", as_octets):
            response = await self.client.get(url, headers=gz)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertNotIn("content-encoding", response.headers)
        self.assertEqual(response.content, AUDIO_BYTES)

    async def test_access_time_is_throttled_and_never_bumps_seq(self) -> None:
        clock = [1000.0]
        self.app.state.audio_access = audio_routes.AccessThrottle(clock=lambda: clock[0])
        url = f"/api/v1/episodes/{self.present_id}/audio"
        seq = self.ctx.db.current_seq()
        await self.client.get(url, headers={"Range": "bytes=0-0"})
        first = self.episode(self.present_id).audio_last_access_at
        self.assertIsNotNone(first)
        with self.ctx.db.write() as tx:
            repo.touch_audio_access(tx, self.present_id, now="2000-01-01T00:00:00.000Z")
        clock[0] += 59
        await self.client.head(url)
        self.assertEqual(self.episode(self.present_id).audio_last_access_at, "2000-01-01T00:00:00.000Z")
        clock[0] += 2
        await self.client.get(url, headers={"Range": "bytes=0-0"})
        self.assertGreater(self.episode(self.present_id).audio_last_access_at, "2000-01-01T00:00:00.000Z")
        self.assertEqual(self.ctx.db.current_seq(), seq)

    async def test_missing_audio_answers_409(self) -> None:
        response = await self.client.get(f"/api/v1/episodes/{self.absent_id}/audio")
        self.assertAudioMissing(response, "audioNotReady", self.absent_id)
        head = await self.client.head(f"/api/v1/episodes/{self.absent_id}/audio")
        self.assertEqual(head.status_code, 409)

    async def test_file_vanishing_after_the_size_check_is_a_409(self) -> None:
        original = ranges.audio_file_response

        def vanish(request, path, **kwargs):
            path.unlink()
            return original(request, path, **kwargs)

        with mock.patch.object(ranges, "audio_file_response", vanish), self.assertLogs(audio_routes.log, "WARNING"):
            response = await self.client.get(f"/api/v1/episodes/{self.present_id}/audio")
        self.assertAudioMissing(response, "audioEvicted", self.present_id)


class EvictedRedirectTests(AudioTestCase):
    settings_overrides = {"evicted_redirect": True}

    async def test_missing_audio_redirects_to_the_enclosure_and_downloads(self) -> None:
        response = await self.client.get(f"/api/v1/episodes/{self.absent_id}/audio")
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], self.episode(self.absent_id).enclosure_url)
        live = [job for job in jobs.list_jobs(self.ctx.db, kind="download") if job.is_live]
        self.assertEqual([job.subject_id for job in live], [self.absent_id])


class ReleaseAudioTests(AudioTestCase):
    async def test_release_deletes_the_file_but_keeps_skip_data(self) -> None:
        seq = self.episode(self.present_id).updated_seq
        response = await self.client.delete(f"/api/v1/episodes/{self.present_id}/audio")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        episode = self.episode(self.present_id)
        self.assertEqual((episode.audio_state, episode.audio_evicted_reason), ("evicted", "played"))
        self.assertGreater(episode.updated_seq, seq)
        self.assertFalse(self.path.exists())
        synced = (await self.client.get(f"/api/v1/episodes/{self.present_id}")).json()
        self.assertEqual((synced["audioState"], synced["audioBytes"]), ("evicted", len(AUDIO_BYTES)))
        self.assertEqual((await self.client.delete(f"/api/v1/episodes/{self.present_id}/audio")).status_code, 204)

    async def test_manual_reason_and_validation(self) -> None:
        response = await self.client.delete(f"/api/v1/episodes/{self.present_id}/audio", params={"reason": "manual"})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.episode(self.present_id).audio_evicted_reason, "manual")
        bad = await self.client.delete(f"/api/v1/episodes/{self.present_id}/audio", params={"reason": "bored"})
        self.assertError(bad, 422, "invalidRequest")
        self.assertError(await self.client.delete("/api/v1/episodes/999/audio"), 404, "notFound")

    async def test_release_waits_for_a_live_pipeline_job(self) -> None:
        reanalyzed = await self.client.post(f"/api/v1/episodes/{self.present_id}/reanalyze")
        self.assertEqual(reanalyzed.status_code, 202)
        self.assertEqual((await self.client.delete(f"/api/v1/episodes/{self.present_id}/audio")).status_code, 204)
        episode = self.episode(self.present_id)
        self.assertEqual((episode.audio_state, episode.release_reason), ("present", "played"))
        self.assertTrue(self.path.exists())
