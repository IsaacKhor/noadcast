"""Episode commands, transcripts, classifications, and the jobs endpoints."""

from __future__ import annotations

import typing

from noadcast.api.routers.jobs import status_text
from noadcast.api.schemas import JobKind
from noadcast.db import repo
from noadcast.pipeline import jobs, states
from noadcast.timeutil import now_iso, utc_now

from tests.api_support import (
    ApiTestCase,
    add_classification,
    add_episodes,
    add_podcast,
    doc_example,
    segment,
    store_audio,
    store_transcript,
)


class ProcessingCommandTests(ApiTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.podcast = add_podcast(self.ctx)
        self.episode_id, self.ready_id = add_episodes(self.ctx, self.podcast.id, 2)
        store_audio(self.ctx, self.ready_id, pipeline_state="ready")
        store_transcript(self.ctx, self.ready_id, ["Hello there.", "This is a show."])
        with self.ctx.db.write() as tx:
            repo.set_episode_states(tx, self.ready_id, classify_state="ready", now=now_iso())

    async def test_process_enqueues_a_priority_download_once(self) -> None:
        response = await self.client.post(f"/api/v1/episodes/{self.episode_id}/process")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(set(response.json()), {"jobId"})
        job_id = response.json()["jobId"]
        job = jobs.get_job(self.ctx.db, job_id)
        self.assertEqual((job.kind, job.subject_id, job.priority), ("download", self.episode_id, jobs.PRIORITY_INTERACTIVE))
        self.assertEqual((await self.client.post(f"/api/v1/episodes/{self.episode_id}/process")).json()["jobId"], job_id)
        self.assertEqual(len([j for j in jobs.list_jobs(self.ctx.db) if j.is_live]), 1)
        episode = (await self.client.get(f"/api/v1/episodes/{self.episode_id}")).json()
        self.assertEqual(episode["state"], "download_pending")
        self.assertGreaterEqual(self.scheduler.wakes, 2)

    async def test_process_with_nothing_left_returns_null(self) -> None:
        response = await self.client.post(f"/api/v1/episodes/{self.ready_id}/process")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"jobId": None})

    async def test_reanalyze_folds_repeats_into_one_classify_job(self) -> None:
        self.registry.available_map["claude"] = True
        response = await self.client.post(
            f"/api/v1/episodes/{self.ready_id}/reanalyze",
            json={"provider": "claude", "model": "claude-haiku-4-5", "thinking": "low", "extra": True},
        )
        self.assertEqual(response.status_code, 202, response.text)
        job = jobs.get_job(self.ctx.db, response.json()["jobId"])
        self.assertEqual(job.kind, "classify")
        self.assertEqual(
            {k: job.params.get(k) for k in ("provider", "model", "thinking", "force", "reclassify")},
            {"provider": "claude", "model": "claude-haiku-4-5", "thinking": "low", "force": True, "reclassify": True},
        )
        again = await self.client.post(f"/api/v1/episodes/{self.ready_id}/reanalyze")
        self.assertEqual(again.json()["jobId"], job.id)

    async def test_reanalyze_with_retranscribe_starts_at_transcription(self) -> None:
        response = await self.client.post(f"/api/v1/episodes/{self.ready_id}/reanalyze", json={"retranscribe": True})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(jobs.get_job(self.ctx.db, response.json()["jobId"]).kind, "transcribe")

    async def test_reanalyze_validates_the_provider(self) -> None:
        unconfigured = await self.client.post(f"/api/v1/episodes/{self.ready_id}/reanalyze", json={"provider": "gemini"})
        self.assertError(unconfigured, 422, "invalidRequest")
        unknown = await self.client.post(f"/api/v1/episodes/{self.ready_id}/reanalyze", json={"provider": "gpt"})
        self.assertError(unknown, 422, "invalidRequest")
        self.assertEqual([j for j in jobs.list_jobs(self.ctx.db) if j.is_live], [])
        fake = await self.client.post(f"/api/v1/episodes/{self.ready_id}/reanalyze", json={"provider": "fake"})
        self.assertEqual(fake.status_code, 202)


class TranscriptTests(ApiTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        podcast = add_podcast(self.ctx)
        self.episode_id, self.bare_id = add_episodes(self.ctx, podcast.id, 2)
        self.sentences, self.words = store_transcript(self.ctx, self.episode_id, ["Hello there.", "Welcome to the show."])

    async def test_sentences(self) -> None:
        response = await self.client.get(f"/api/v1/episodes/{self.episode_id}/transcript")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body), {"episodeId", "modelId", "language", "durationSeconds", "joinerVersion", "sentences"}
        )
        self.assertEqual(
            (body["episodeId"], body["modelId"], body["language"], body["durationSeconds"], body["joinerVersion"]),
            (self.episode_id, "Systran/faster-whisper-tiny.en", "en", 3599.5, 1),
        )
        self.assertEqual(
            body["sentences"][0],
            {"index": 0, "startSeconds": 0.0, "endSeconds": 0.7, "text": "Hello there.", "flags": ["low_confidence"]},
        )
        self.assertEqual(body["sentences"][1]["flags"], [])

    async def test_words(self) -> None:
        body = (await self.client.get(f"/api/v1/episodes/{self.episode_id}/transcript", params={"format": "words"})).json()
        self.assertEqual(len(body["sentences"]), 2)
        self.assertEqual(len(body["words"]), len(self.words))
        self.assertEqual(body["words"][0], {"start": 0.0, "end": 0.3, "word": " Hello", "probability": 0.9})
        self.assertEqual("".join(w["word"] for w in body["words"]).strip(), "Hello there. Welcome to the show.")

    async def test_text(self) -> None:
        response = await self.client.get(f"/api/v1/episodes/{self.episode_id}/transcript", params={"format": "text"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        self.assertEqual(response.text, "Hello there.\nWelcome to the show.")

    async def test_missing_transcript_or_episode_is_404(self) -> None:
        self.assertError(await self.client.get(f"/api/v1/episodes/{self.bare_id}/transcript"), 404, "notFound")
        self.assertError(await self.client.get("/api/v1/episodes/999/transcript"), 404, "notFound")
        self.assertError(
            await self.client.get(f"/api/v1/episodes/{self.episode_id}/transcript", params={"format": "srt"}),
            422,
            "invalidRequest",
        )


class ClassificationTests(ApiTestCase):
    async def test_history_newest_first_with_camel_case_segments(self) -> None:
        podcast = add_podcast(self.ctx)
        [episode_id] = add_episodes(self.ctx, podcast.id, 1)
        older = add_classification(self.ctx, episode_id, created_at="2026-09-01T00:00:00.000Z", segments=[segment(0.0, 30.0, "intro", "Theme")])
        newer = add_classification(
            self.ctx, episode_id, provider="claude", model="claude-sonnet-5", segments=[segment(600.0, 660.0)]
        )
        with self.ctx.db.write() as tx:
            repo.activate_classification(tx, episode_id, newer.id)
        response = await self.client.get(f"/api/v1/episodes/{episode_id}/classifications")
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual([item["id"] for item in items], [newer.id, older.id])
        self.assertEqual(
            set(items[0]),
            {
                "id", "provider", "model", "promptVersion", "renderFormat", "isActive", "inputTokens",
                "thoughtTokens", "outputTokens", "totalCostUsd", "latencyMs", "createdAt", "segments",
            },
        )
        self.assertEqual((items[0]["provider"], items[0]["isActive"], items[1]["isActive"]), ("claude", True, False))
        self.assertEqual((items[0]["inputTokens"], items[0]["thoughtTokens"], items[0]["outputTokens"]), (1000, 100, 50))
        self.assertEqual(items[0]["latencyMs"], 1234)
        self.assertEqual(items[0]["segments"], [{"startSeconds": 600.0, "endSeconds": 660.0, "kind": "ad", "summary": "Sponsor read"}])
        self.assertEqual(items[1]["segments"][0]["kind"], "intro")
        self.assertEqual(items[1]["segments"][0]["summary"], "Theme")
        # The same content summary reaches clients through both history and active markers.
        with self.ctx.db.write() as tx:
            repo.replace_auto_markers(
                tx, episode_id, [repo.NewMarker(600.0, 660.0, "ad", "Sponsor read")],
                classification_id=newer.id, now=now_iso(),
            )
        detail = (await self.client.get(f"/api/v1/episodes/{episode_id}")).json()
        synced = (await self.client.get("/api/v1/sync")).json()
        self.assertEqual(detail["adMarkers"][0]["summary"], "Sponsor read")
        self.assertEqual(synced["episodes"][0]["adMarkers"][0]["summary"], "Sponsor read")
        self.assertError(await self.client.get("/api/v1/episodes/999/classifications"), 404, "notFound")


class JobsEndpointTests(ApiTestCase):
    def test_job_kind_filter_matches_the_pipeline(self) -> None:
        self.assertEqual(typing.get_args(JobKind), states.JOB_KINDS)

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        podcast = add_podcast(self.ctx)
        self.episode_ids = add_episodes(self.ctx, podcast.id, 3)

    async def process(self, episode_id: int) -> int:
        return (await self.client.post(f"/api/v1/episodes/{episode_id}/process")).json()["jobId"]

    async def test_list_filters_and_shape(self) -> None:
        first, second = await self.process(self.episode_ids[0]), await self.process(self.episode_ids[1])
        await self.client.post("/api/v1/refresh")
        body = (await self.client.get("/api/v1/jobs")).json()
        self.assertEqual([item["id"] for item in body["items"]][-2:], [second, first])
        self.assertEqual(
            set(body["items"][0]),
            {
                "id", "kind", "subjectId", "state", "priority", "attempts", "maxAttempts", "availableAt",
                "leaseExpiresAt", "params", "lastError", "lastErrorAt", "createdAt", "updatedAt", "finishedAt",
            },
        )
        downloads = (await self.client.get("/api/v1/jobs", params={"kind": "download"})).json()["items"]
        self.assertEqual({item["id"] for item in downloads}, {first, second})
        self.assertEqual(downloads[0]["params"], {"host": "cdn.example.com"})
        self.assertEqual(len((await self.client.get("/api/v1/jobs", params={"limit": 1})).json()["items"]), 1)
        self.assertEqual((await self.client.get("/api/v1/jobs", params={"state": "done"})).json()["items"], [])
        for params in ({"state": "sleeping"}, {"kind": "mine"}, {"limit": 1001}):
            with self.subTest(params=params):
                self.assertError(await self.client.get("/api/v1/jobs", params=params), 422, "invalidRequest")

    async def test_cancel_and_retry(self) -> None:
        job_id = await self.process(self.episode_ids[0])
        response = await self.client.delete(f"/api/v1/jobs/{job_id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.scheduler.aborted, [job_id])
        self.assertEqual(jobs.get_job(self.ctx.db, job_id).state, "canceled")
        self.assertEqual(repo.get_episode(self.ctx.db, self.episode_ids[0]).pipeline_state, "discovered")
        self.assertEqual((await self.client.delete(f"/api/v1/jobs/{job_id}")).status_code, 204)
        self.assertEqual(self.scheduler.aborted, [job_id], "a finished job has no runner to abort")
        retried = await self.client.post(f"/api/v1/jobs/{job_id}/retry")
        self.assertEqual(retried.status_code, 202)
        self.assertEqual(retried.json(), {"jobId": job_id})
        self.assertEqual(jobs.get_job(self.ctx.db, job_id).state, "pending")
        self.assertEqual(repo.get_episode(self.ctx.db, self.episode_ids[0]).pipeline_state, "download_pending")

    async def test_retry_points_at_the_live_job_covering_the_work(self) -> None:
        job_id = await self.process(self.episode_ids[0])
        await self.client.delete(f"/api/v1/jobs/{job_id}")
        replacement = await self.process(self.episode_ids[0])
        self.assertNotEqual(replacement, job_id)
        self.assertEqual((await self.client.post(f"/api/v1/jobs/{job_id}/retry")).json(), {"jobId": replacement})


class ActiveJobsTests(ApiTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        podcast = add_podcast(self.ctx)
        self.episode_ids = add_episodes(self.ctx, podcast.id, 3)

    async def active(self, **headers: str):
        return await self.client.get("/api/v1/jobs/active", headers=headers)

    def set_state(self, episode_id: int, state: str, progress: repo.Progress | None) -> None:
        with self.ctx.db.write() as tx:
            repo.set_episode_states(tx, episode_id, pipeline_state=state, progress=progress, now=now_iso())

    async def test_items_progress_and_etag(self) -> None:
        empty = await self.active()
        self.assertEqual(empty.json(), {"items": []})
        download = (await self.client.post(f"/api/v1/episodes/{self.episode_ids[0]}/process")).json()["jobId"]
        queued = (await self.active()).json()["items"]
        self.assertEqual(len(queued), 1)
        documented = set(doc_example("### `GET /api/v1/jobs/active`")["items"][0])
        self.assertLessEqual(documented, set(queued[0]))
        self.assertLessEqual(set(queued[0]), documented | {"jobId", "jobState"})
        self.assertEqual(
            {k: queued[0][k] for k in ("episodeId", "jobId", "state", "stage", "jobState", "current", "total", "statusText")},
            {
                "episodeId": self.episode_ids[0],
                "jobId": download,
                "state": "download_pending",
                "stage": "download",
                "jobState": "pending",
                "current": None,
                "total": None,
                "statusText": "Queued for download",
            },
        )

        self.claim_and_report("download", self.episode_ids[0], "downloading", 12_345_678, 63_346_363)
        first = await self.active()
        etag = first.headers["etag"]
        self.assertRegex(etag, r'^"[0-9a-f]{32}"$')
        item = first.json()["items"][0]
        self.assertEqual((item["jobState"], item["current"], item["total"]), ("running", 12_345_678, 63_346_363))
        self.assertEqual(item["statusText"], "Downloading 12.3 of 63.3 MB")

        not_modified = await self.active(**{"If-None-Match": etag})
        self.assertEqual(not_modified.status_code, 304)
        self.assertEqual(not_modified.content, b"")
        self.assertEqual(not_modified.headers["etag"], etag)
        self.assertEqual((await self.active(**{"If-None-Match": f'"other", W/{etag}'})).status_code, 304)

        with self.ctx.db.write() as tx:
            repo.set_progress(tx, self.episode_ids[0], current=20_000_000, total=63_346_363, now=now_iso())
        moved = await self.active(**{"If-None-Match": etag})
        self.assertEqual(moved.status_code, 200)
        self.assertNotEqual(moved.headers["etag"], etag)
        self.assertEqual(moved.json()["items"][0]["current"], 20_000_000)

    def claim_and_report(self, kind: str, episode_id: int, state: str, current: float | None, total: float | None) -> None:
        with self.ctx.db.write() as tx:
            job = jobs.claim(tx, kind, owner="test-owner", lease_seconds=600, now=utc_now())
            assert job is not None and job.subject_id == episode_id
            repo.set_episode_states(tx, episode_id, pipeline_state=state, progress=repo.Progress(kind, current, total), now=now_iso())

    async def test_transcribe_and_classify_units(self) -> None:
        for episode_id in self.episode_ids[1:]:
            store_audio(self.ctx, episode_id, pipeline_state="downloaded")
        await self.client.post(f"/api/v1/episodes/{self.episode_ids[1]}/process")
        self.claim_and_report("transcribe", self.episode_ids[1], "transcribing", 1200.0, 3918.9)
        store_transcript(self.ctx, self.episode_ids[2], ["Hi."])
        await self.client.post(f"/api/v1/episodes/{self.episode_ids[2]}/process")
        self.claim_and_report("classify", self.episode_ids[2], "classifying", None, None)
        items = {item["episodeId"]: item for item in (await self.active()).json()["items"]}
        transcribing, classifying = items[self.episode_ids[1]], items[self.episode_ids[2]]
        self.assertEqual((transcribing["stage"], transcribing["current"], transcribing["total"]), ("transcribe", 1200.0, 3918.9))
        self.assertEqual(transcribing["statusText"], "Transcribing 20:00 of 65:19")
        self.assertEqual((classifying["stage"], classifying["current"], classifying["total"]), ("classify", None, None))
        self.assertEqual(classifying["statusText"], "Classifying")

    async def test_progress_left_over_from_another_stage_is_ignored(self) -> None:
        store_audio(self.ctx, self.episode_ids[0], pipeline_state="downloaded")
        await self.client.post(f"/api/v1/episodes/{self.episode_ids[0]}/process")
        with self.ctx.db.write() as tx:
            jobs.claim(tx, "transcribe", owner="test-owner", lease_seconds=600, now=utc_now())
            repo.set_episode_states(
                tx, self.episode_ids[0], pipeline_state="transcribing", progress=repo.Progress("download", 5.0, 9.0), now=now_iso()
            )
        item = (await self.active()).json()["items"][0]
        self.assertEqual((item["current"], item["total"], item["statusText"]), (None, None, "Transcribing"))

    def test_status_text(self) -> None:
        self.assertEqual(status_text("download", "pending", 0, None, None), "Queued for download")
        self.assertEqual(status_text("download", "pending", 2, None, None), "Waiting to retry download")
        self.assertEqual(status_text("transcribe", "pending", 0, None, None), "Queued for transcription")
        self.assertEqual(status_text("classify", "pending", 0, None, None), "Queued for classification")
        self.assertEqual(status_text("download", "running", 1, None, None), "Downloading")
        self.assertEqual(status_text("download", "running", 1, 1_500_000, None), "Downloading 1.5 MB")
        self.assertEqual(status_text("transcribe", "running", 1, 59.6, None), "Transcribing 1:00")
        self.assertEqual(status_text("transcribe", "running", 1, 7322.0, 7400.0), "Transcribing 122:02 of 123:20")
