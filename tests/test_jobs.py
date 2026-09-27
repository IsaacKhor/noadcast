"""Job queue: idempotent enqueue, atomic claim (each job exactly once, even
across connections), leases, backoff policy, permanent-vs-transient errors,
boot recovery with disk reconciliation, and the retry/cancel commands."""

from __future__ import annotations

import datetime as dt
import random
import threading
import unittest

from noadcast.classify.base import ClassifierError
from noadcast.db import repo
from noadcast.db.engine import Database
from noadcast.feeds.fetcher import FeedFetchError
from noadcast.feeds.parser import FeedParseError
from noadcast.media.downloader import DownloadError
from noadcast.media.probe import ProbeError
from noadcast.media.store import MediaStore
from noadcast.pipeline import commands, jobs
from noadcast.timeutil import iso, now_iso, utc_now
from noadcast.transcribe.protocol import TranscriptionError, WorkerCrashed

from tests.pipeline_support import (
    item,
    make_settings,
    seed_episodes,
    seed_podcast,
    temp_dir,
    write_audio,
)


class JobTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings(temp_dir(self))
        self.db = Database(self.settings.db_path)
        self.addCleanup(self.db.close)
        self.now = utc_now()

    def enqueue(self, kind: str = "download", subject: int = 1, **kwargs) -> jobs.Enqueued:
        with self.db.write() as tx:
            return jobs.enqueue(tx, kind, subject, now=kwargs.pop("now", iso(self.now)), **kwargs)

    def claim(self, kind: str = "download", **kwargs) -> jobs.Job | None:
        with self.db.write() as tx:
            return jobs.claim(tx, kind, owner="test", lease_seconds=60, now=kwargs.pop("now", self.now), **kwargs)

    def test_enqueue_is_idempotent_per_kind_and_subject(self) -> None:
        first = self.enqueue()
        again = self.enqueue()
        self.assertTrue(first.created)
        self.assertEqual((again.job_id, again.created), (first.job_id, False))
        self.assertTrue(self.enqueue(kind="transcribe").created, "another kind is another job")
        self.claim()
        self.assertEqual(self.enqueue().job_id, first.job_id, "a running job still blocks a duplicate")
        with self.db.write() as tx:
            jobs.complete(tx, first.job_id, now=now_iso())
        self.assertNotEqual(self.enqueue().job_id, first.job_id, "finished jobs do not")

    def test_duplicate_enqueue_raises_priority_but_never_lowers_it(self) -> None:
        job_id = self.enqueue(priority=100).job_id
        self.enqueue(priority=10)
        self.assertEqual(jobs.get_job(self.db, job_id).priority, 10)
        self.enqueue(priority=200)
        self.assertEqual(jobs.get_job(self.db, job_id).priority, 10)

    def test_refresh_jobs_are_one_shot(self) -> None:
        job_id = self.enqueue(kind="refresh_feed").job_id
        self.assertEqual(jobs.get_job(self.db, job_id).max_attempts, 1)
        self.assertEqual(jobs.get_job(self.db, self.enqueue(subject=2).job_id).max_attempts, 5)

    def test_claim_order_availability_and_host_exclusion(self) -> None:
        later = iso(self.now + dt.timedelta(minutes=5))
        slow = self.enqueue(subject=1, priority=100, params={"host": "a.example"}).job_id
        urgent = self.enqueue(subject=2, priority=10, params={"host": "a.example"}).job_id
        other_host = self.enqueue(subject=3, priority=100, params={"host": "b.example"}).job_id
        self.enqueue(subject=4, priority=1, available_at=later)
        self.assertEqual(self.claim(exclude_hosts={"a.example"}).id, other_host)
        claimed = self.claim()
        self.assertEqual((claimed.id, claimed.state, claimed.attempts, claimed.lease_owner), (urgent, "running", 1, "test"))
        self.assertEqual(self.claim().id, slow)
        self.assertIsNone(self.claim(), "not yet available")
        self.assertEqual(self.claim(now=self.now + dt.timedelta(minutes=6)).subject_id, 4)

    def test_concurrent_claims_from_separate_connections_take_each_job_once(self) -> None:
        with self.db.write() as tx:
            for subject in range(200):
                jobs.enqueue(tx, "classify", subject, now=iso(self.now))
        claimed: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            db = Database(self.settings.db_path)
            try:
                while True:
                    with db.write() as tx:
                        job = jobs.claim(tx, "classify", owner=threading.current_thread().name, lease_seconds=60, now=self.now)
                    if job is None:
                        return
                    with lock:
                        claimed.append(job.id)
            finally:
                db.close()

        threads = [threading.Thread(target=worker, name=f"w{i}") for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(claimed), 200)
        self.assertEqual(len(set(claimed)), 200)

    def test_leases_renew_only_for_their_owner_and_live_job(self) -> None:
        job = self.enqueue()
        self.claim()
        with self.db.write() as tx:
            self.assertTrue(jobs.renew_lease(tx, job.job_id, owner="test", lease_seconds=60, now=self.now))
            self.assertFalse(jobs.renew_lease(tx, job.job_id, owner="someone-else", lease_seconds=60, now=self.now))
            jobs.cancel(tx, job.job_id, now=now_iso())
            self.assertFalse(jobs.renew_lease(tx, job.job_id, owner="test", lease_seconds=60, now=self.now))

    def test_reclaim_expired_leases_skips_jobs_running_here(self) -> None:
        mine = self.enqueue(subject=1).job_id
        theirs = self.enqueue(subject=2).job_id
        self.claim()
        self.claim()
        later = self.now + dt.timedelta(seconds=120)
        report = jobs.reclaim_expired_leases(self.db, now=later, exclude_ids={mine})
        self.assertEqual(report.requeued, [theirs])
        self.assertEqual(jobs.get_job(self.db, mine).state, "running")
        self.assertEqual(jobs.get_job(self.db, theirs).state, "pending")

    def test_backoff_formula_jitter_and_cap(self) -> None:
        policy = jobs.RETRY_POLICIES["download"]
        rng = random.Random(1)
        for attempt, raw in ((1, 30), (2, 60), (3, 120), (8, 3600), (30, 3600)):
            for _ in range(50):
                self.assertTrue(0.8 * raw <= policy.delay(attempt, rng) <= 1.2 * raw, (attempt, raw))
        refresh = jobs.RETRY_POLICIES["refresh_feed"]
        self.assertLessEqual(refresh.delay(40, rng), 1.2 * 6 * 3600)
        self.assertGreaterEqual(refresh.delay(1, rng), 0.8 * 60)

    def test_failure_decisions_follow_the_error_types(self) -> None:
        self.enqueue()
        job = self.claim()
        permanent = [
            FeedParseError("not rss"),
            ProbeError("not audio"),
            DownloadError("gone", status=410, permanent=True),
            ClassifierError("bad key", permanent=True),
            TranscriptionError("no words", permanent=True),
        ]
        for exc in permanent:
            self.assertEqual(jobs.decide_failure(job, exc, now=self.now).reason, "permanent", exc)
        for exc in (DownloadError("reset"), WorkerCrashed("oom"), RuntimeError("bug"), FeedFetchError("503", status=503)):
            decision = jobs.decide_failure(job, exc, now=self.now, rng=random.Random(0))
            self.assertTrue(decision.retry, exc)
        honoured = jobs.decide_failure(job, ClassifierError("429", retry_after=42.0), now=self.now)
        self.assertEqual(honoured.available_at, iso(self.now + dt.timedelta(seconds=42)))
        exhausted = jobs.Job(**{**job.__dict__, "attempts": job.max_attempts})
        self.assertEqual(jobs.decide_failure(exhausted, DownloadError("reset"), now=self.now).reason, "exhausted")


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings(temp_dir(self))
        self.db = Database(self.settings.db_path)
        self.addCleanup(self.db.close)
        self.store = MediaStore(self.settings.data_dir)
        podcast = seed_podcast(self.db)
        self.podcast = podcast
        self.episodes = seed_episodes(self.db, podcast.id, [item(f"g{i}", position=i) for i in range(6)])

    def running_job(self, kind: str, episode: repo.Episode, state: str, *, attempts: int = 1) -> int:
        with self.db.write() as tx:
            job_id = jobs.enqueue(tx, kind, episode.id, now=now_iso()).job_id
            tx.execute(
                "UPDATE jobs SET state = 'running', attempts = ?, lease_owner = 'dead', lease_expires_at = ? WHERE id = ?",
                (attempts, "2999-01-01T00:00:00.000Z", job_id),
            )
            repo.set_episode_states(tx, episode.id, pipeline_state=state, now=now_iso())
        return job_id

    def test_boot_recovery_requeues_resets_and_reconciles_disk(self) -> None:
        e_dl, e_tr, e_crash, e_gone, e_orphan, e_present = self.episodes
        # An interrupted download with its .part on disk: resumes.
        dl_job = self.running_job("download", e_dl, "downloading")
        relpath = self.store.audio_relpath(self.podcast.id, e_dl.id, "mp3")
        part = self.store.abspath(relpath + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"half")
        with self.db.write() as tx:
            repo.begin_download(tx, e_dl.id, audio_path=relpath, pipeline_state=None, total_bytes=None, now=now_iso())
        # An interrupted transcription.
        tr_job = self.running_job("transcribe", e_tr, "transcribing")
        # A job that has crashed the server on every attempt: fails.
        crash_job = self.running_job("classify", e_crash, "classifying", attempts=5)
        # A partial row whose .part vanished.
        with self.db.write() as tx:
            repo.begin_download(tx, e_gone.id, audio_path="audio/1/x.mp3", pipeline_state=None, total_bytes=None, now=now_iso())
        # An episode waiting for a stage with no job at all.
        with self.db.write() as tx:
            repo.set_episode_states(tx, e_orphan.id, pipeline_state="classify_pending", now=now_iso())
        # A present row whose file was deleted behind the server's back.
        stored = write_audio(_Ctx(self.store), e_present)
        with self.db.write() as tx:
            repo.mark_audio_present(tx, e_present.id, stored, measured_duration_seconds=1.0, now=now_iso())
        self.store.abspath(stored.path).unlink()
        stray = self.store.abspath("audio/1/999.mp3.part")
        stray.write_bytes(b"nobody's")

        report = jobs.recover_leases(self.db, self.store)

        self.assertEqual(sorted(report.requeued), sorted([dl_job, tr_job]))
        self.assertEqual(report.failed, [crash_job])
        self.assertEqual(jobs.get_job(self.db, dl_job).state, "pending")
        self.assertEqual(jobs.get_job(self.db, dl_job).attempts, 1, "the interrupted attempt still counts")
        episode = lambda e: repo.get_episode(self.db, e.id)  # noqa: E731
        self.assertEqual(episode(e_dl).pipeline_state, "download_pending")
        self.assertEqual(episode(e_dl).audio_state, "partial")
        self.assertTrue(part.exists(), "a resumable .part is kept")
        self.assertEqual(episode(e_tr).pipeline_state, "transcribe_pending")
        self.assertEqual(episode(e_crash).pipeline_state, "failed")
        self.assertEqual(report.lost_partials, [e_gone.id])
        self.assertEqual(episode(e_gone).audio_state, "absent")
        self.assertEqual(report.healed, [e_orphan.id])
        self.assertEqual(jobs.live_job(self.db, "classify", e_orphan.id).state, "pending")
        self.assertEqual(report.missing_audio, [e_present.id])
        self.assertEqual((episode(e_present).audio_state, episode(e_present).audio_evicted_reason), ("evicted", "missing"))
        self.assertEqual(episode(e_present).audio_sha256, stored.sha256, "size and hash stay for client checks")
        self.assertFalse(stray.exists())
        self.assertEqual(report.removed_part_files, [str(stray)])

    def test_recovery_is_a_no_op_on_a_clean_database(self) -> None:
        before = self.db.current_seq()
        report = jobs.recover_leases(self.db, self.store)
        self.assertEqual(report, jobs.RecoveryReport())
        self.assertEqual(self.db.current_seq(), before)


class _Ctx:
    """Just enough of AppContext for write_audio."""

    def __init__(self, store: MediaStore) -> None:
        self.store = store


class RetryCancelCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings(temp_dir(self))
        self.db = Database(self.settings.db_path)
        self.addCleanup(self.db.close)
        podcast = seed_podcast(self.db)
        (self.episode,) = seed_episodes(self.db, podcast.id, [item("a")])
        self.server = repo.load_server_settings(self.db, self.settings)

    def test_process_retry_and_cancel_move_the_episode(self) -> None:
        with self.db.write() as tx:
            job_id = commands.process_episode(tx, self.episode.id, server=self.server, now=now_iso())
        self.assertEqual(repo.get_episode(self.db, self.episode.id).pipeline_state, "download_pending")
        job = jobs.get_job(self.db, job_id)
        self.assertEqual((job.kind, job.priority, job.params["host"]), ("download", jobs.PRIORITY_INTERACTIVE, "media.example.com"))
        with self.db.write() as tx:
            self.assertEqual(commands.process_episode(tx, self.episode.id, server=self.server, now=now_iso()), job_id)
            commands.cancel_job(tx, job_id, now=now_iso())
        self.assertEqual(jobs.get_job(self.db, job_id).state, "canceled")
        self.assertEqual(repo.get_episode(self.db, self.episode.id).pipeline_state, "discovered")
        with self.db.write() as tx:
            retried = commands.retry_job(tx, job_id, now=now_iso())
        self.assertEqual((retried.id, retried.state, retried.attempts), (job_id, "pending", 0))
        self.assertEqual(repo.get_episode(self.db, self.episode.id).pipeline_state, "download_pending")
        with self.db.write() as tx, self.assertRaises(commands.NotFound):
            commands.retry_job(tx, 999, now=now_iso())

    def test_reanalyze_carries_the_override_through_the_chain(self) -> None:
        with self.db.write() as tx:
            job_id = commands.reanalyze_episode(
                tx, self.episode.id, server=self.server, now=now_iso(), provider="openrouter", retranscribe=True
            )
        job = jobs.get_job(self.db, job_id)
        self.assertEqual(job.kind, "download", "audio first")
        self.assertEqual(
            {k: job.params[k] for k in ("provider", "force", "reclassify", "retranscribe")},
            {"provider": "openrouter", "force": True, "reclassify": True, "retranscribe": True},
        )


if __name__ == "__main__":
    unittest.main()
