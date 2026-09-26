"""Sync paging (docs/API.md "GET /sync"): no row is ever skipped, pages
honour referential closure, deletions and settings travel, and cursors below
the tombstone floor (or ahead of the database) expire."""

from __future__ import annotations

import random
import unittest

from noadcast.db import repo
from noadcast.db.engine import Database
from noadcast.timeutil import now_iso

from tests.pipeline_support import item, make_settings, seed_episodes, seed_podcast, temp_dir


class Mirror:
    """A client applying pages the way docs/API.md prescribes."""

    def __init__(self) -> None:
        self.podcasts: dict[int, int] = {}  # id -> seq
        self.episodes: dict[int, tuple[int, int]] = {}  # id -> (podcast id, seq)
        self.settings_seen = 0
        self.cursor = 0
        self.pages = 0

    def apply(self, page: repo.SyncPage) -> None:
        for podcast in page.podcasts:
            self.podcasts[podcast.id] = max(podcast.updated_seq, self.podcasts.get(podcast.id, 0))
        for episode in page.episodes:
            assert episode.podcast_id in self.podcasts, "episode arrived before its podcast"
            self.episodes[episode.id] = (episode.podcast_id, episode.updated_seq)
        for tombstone in page.deletions:
            if tombstone.entity == "podcast":
                self.podcasts.pop(tombstone.entity_id, None)
                for episode_id in [e for e, (p, _) in self.episodes.items() if p == tombstone.entity_id]:
                    del self.episodes[episode_id]
            else:
                self.episodes.pop(tombstone.entity_id, None)
        if page.settings_changed:
            self.settings_seen += 1
        assert page.next_since >= self.cursor, "cursor went backwards"
        self.cursor = page.next_since
        self.pages += 1

    def sync(self, db: Database, limit: int) -> None:
        while True:
            page = repo.sync_page(db, since=self.cursor, limit=limit)
            self.apply(page)
            if not page.has_more:
                return


def server_state(db: Database) -> tuple[dict[int, int], dict[int, tuple[int, int]]]:
    podcasts = {row["id"]: row["updated_seq"] for row in db.read("SELECT id, updated_seq FROM podcasts")}
    episodes = {
        row["id"]: (row["podcast_id"], row["updated_seq"])
        for row in db.read("SELECT id, podcast_id, updated_seq FROM episodes")
    }
    return podcasts, episodes


class SyncCursorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings(temp_dir(self))
        self.db = Database(self.settings.db_path)
        self.addCleanup(self.db.close)

    def test_empty_database_full_sync_carries_settings(self) -> None:
        page = repo.sync_page(self.db, since=0, limit=10)
        self.assertEqual((page.podcasts, page.episodes, page.deletions), ([], [], []))
        self.assertTrue(page.settings_changed)
        self.assertEqual((page.next_since, page.has_more), (0, False))

    def test_pages_cut_at_the_limit_th_seq(self) -> None:
        podcast = seed_podcast(self.db)
        seed_episodes(self.db, podcast.id, [item(f"g{i}", position=i) for i in range(9)])
        seqs = sorted(
            [podcast.updated_seq] + [e.updated_seq for e in repo.episodes_for_podcast(self.db, podcast.id)]
        )
        self.assertEqual(len(set(seqs)), 10, "every row has its own seq")
        since, sizes = 0, []
        while True:
            page = repo.sync_page(self.db, since=since, limit=4)
            rows = [p.updated_seq for p in page.podcasts] + [e.updated_seq for e in page.episodes]
            in_range = [s for s in rows if since < s <= page.next_since]
            sizes.append(len(in_range))
            self.assertEqual(page.next_since, sorted(in_range)[-1])
            since = page.next_since
            if not page.has_more:
                break
        self.assertEqual(sizes, [4, 4, 2])
        self.assertEqual(since, seqs[-1])

    def test_closure_includes_a_parent_whose_seq_is_past_the_cutoff(self) -> None:
        podcast = seed_podcast(self.db)
        (episode,) = seed_episodes(self.db, podcast.id, [item("g1")])
        with self.db.write() as tx:
            repo.set_podcast_switches(
                tx, podcast, auto_process_enabled=False, ad_analysis_enabled=None, now=now_iso()
            )
        page = repo.sync_page(self.db, since=0, limit=1)
        self.assertEqual([e.id for e in page.episodes], [episode.id])
        self.assertEqual([p.id for p in page.podcasts], [podcast.id], "parent included despite seq > cutoff")
        self.assertEqual(page.next_since, episode.updated_seq)
        self.assertTrue(page.has_more)
        follow = repo.sync_page(self.db, since=page.next_since, limit=1)
        self.assertEqual([p.id for p in follow.podcasts], [podcast.id])
        self.assertFalse(follow.has_more)

    def test_markers_ride_with_their_episode(self) -> None:
        podcast = seed_podcast(self.db)
        (episode,) = seed_episodes(self.db, podcast.id, [item("g1")])
        with self.db.write() as tx:
            repo.replace_auto_markers(
                tx,
                episode.id,
                [repo.NewMarker(30.0, 60.0, "ad", "b"), repo.NewMarker(0.0, 10.0, "intro", "a")],
                classification_id=None,
                now=now_iso(),
            )
        page = repo.sync_page(self.db, since=episode.updated_seq, limit=10)
        self.assertEqual([e.id for e in page.episodes], [episode.id])
        self.assertEqual([m.start_seconds for m in page.markers[episode.id]], [0.0, 30.0])
        self.assertEqual(page.episodes[0].marker_revision, 1)

    def test_deletions_and_cascade(self) -> None:
        keep = seed_podcast(self.db, "https://a.example/feed")
        drop = seed_podcast(self.db, "https://b.example/feed")
        seed_episodes(self.db, keep.id, [item("k1")])
        seed_episodes(self.db, drop.id, [item("d1"), item("d2", position=1)])
        mirror = Mirror()
        mirror.sync(self.db, limit=2)
        self.assertEqual(len(mirror.episodes), 3)
        with self.db.write() as tx:
            repo.delete_podcast(tx, drop.id, now=now_iso())
        page = repo.sync_page(self.db, since=mirror.cursor, limit=10)
        self.assertEqual([(t.entity, t.entity_id) for t in page.deletions], [("podcast", drop.id)])
        mirror.apply(page)
        self.assertEqual(set(mirror.podcasts), {keep.id})
        self.assertEqual(len(mirror.episodes), 1)

    def test_ids_are_never_reused_after_deleting_the_newest_rows(self) -> None:
        """SQLite hands a deleted max rowid to the next insert. If a podcast
        re-created that way shared a page with the old one's tombstone, the
        client (deletions applied last) would delete the new podcast."""
        mirror = Mirror()
        old = seed_podcast(self.db, "https://old.example/feed")
        (old_episode,) = seed_episodes(self.db, old.id, [item("x")])
        mirror.sync(self.db, limit=10)
        with self.db.write() as tx:
            repo.delete_podcast(tx, old.id, now=now_iso())
        new = seed_podcast(self.db, "https://new.example/feed")
        (new_episode,) = seed_episodes(self.db, new.id, [item("x")])
        self.assertGreater(new.id, old.id)
        self.assertGreater(new_episode.id, old_episode.id)
        page = repo.sync_page(self.db, since=mirror.cursor, limit=10)
        self.assertEqual([t.entity_id for t in page.deletions], [old.id])
        mirror.apply(page)
        self.assertEqual(set(mirror.podcasts), {new.id})
        self.assertEqual(set(mirror.episodes), {new_episode.id})

    def test_settings_only_when_a_settings_row_is_in_the_page(self) -> None:
        seed_podcast(self.db)
        head = self.db.current_seq()
        self.assertFalse(repo.sync_page(self.db, since=head, limit=10).settings_changed)
        with self.db.write() as tx:
            repo.update_server_settings(tx, self.settings, {"ad_analysis_enabled": False}, now=now_iso())
        page = repo.sync_page(self.db, since=head, limit=10)
        self.assertTrue(page.settings_changed)
        self.assertEqual((page.podcasts, page.episodes), ([], []))
        self.assertEqual(page.next_since, head + 1)
        self.assertFalse(repo.sync_page(self.db, since=page.next_since, limit=10).settings_changed)
        self.assertTrue(repo.sync_page(self.db, since=0, limit=10).settings_changed)

    def test_cursor_expires_below_the_pruned_tombstone_floor(self) -> None:
        podcasts = [seed_podcast(self.db, f"https://{i}.example/feed") for i in range(3)]
        with self.db.write() as tx:
            repo.delete_podcast(tx, podcasts[0].id, now="2026-01-01T00:00:00.000Z")
            repo.delete_podcast(tx, podcasts[1].id, now="2026-09-01T00:00:00.000Z")
        tombstones = self.db.read("SELECT updated_seq FROM tombstones ORDER BY updated_seq")
        old_seq, new_seq = (row["updated_seq"] for row in tombstones)
        with self.db.write() as tx:
            self.assertEqual(repo.prune_tombstones(tx, deleted_before="2026-06-01T00:00:00.000Z"), 1)
        self.assertEqual(repo.tombstone_floor(self.db), old_seq)
        with self.assertRaises(repo.CursorExpired):
            repo.sync_page(self.db, since=old_seq - 1, limit=10)
        repo.sync_page(self.db, since=0, limit=10)  # a full sync is always allowed
        page = repo.sync_page(self.db, since=old_seq, limit=10)  # saw the pruned one already
        self.assertEqual([t.updated_seq for t in page.deletions], [new_seq])
        with self.assertRaises(repo.CursorExpired):
            repo.sync_page(self.db, since=self.db.current_seq() + 5, limit=10)  # restored backup

    def test_reader_never_observes_an_uncommitted_seq(self) -> None:
        podcast = seed_podcast(self.db)
        reader = Database(self.settings.db_path)
        self.addCleanup(reader.close)
        before = repo.sync_page(reader, since=0, limit=100)
        with self.db.write() as tx:
            repo.insert_episodes(tx, podcast.id, [item("late")], now=now_iso())
            during = repo.sync_page(reader, since=0, limit=100)
            self.assertEqual(during.next_since, before.next_since)
            self.assertEqual(during.episodes, [])
        after = repo.sync_page(reader, since=before.next_since, limit=100)
        self.assertEqual([e.guid for e in after.episodes], ["late"])

    def test_interleaved_writes_and_pages_never_lose_a_row(self) -> None:
        """Random writes between pages; once writes stop, the client mirror
        must equal the server exactly — the property the seq cursor exists for."""
        for seed in range(24):
            with self.subTest(seed=seed):
                self._interleave(random.Random(seed))

    def _interleave(self, rng: random.Random) -> None:
        settings = make_settings(temp_dir(self))
        db = Database(settings.db_path)
        self.addCleanup(db.close)
        counter = 0

        def write_something() -> None:
            nonlocal counter
            counter += 1
            podcasts = repo.list_podcasts(db)
            choice = rng.random()
            with db.write() as tx:
                now = now_iso()
                if not podcasts or choice < 0.15:
                    repo.insert_podcast(
                        tx,
                        feed_url=f"https://p{counter}.example/feed",
                        title=f"P{counter}",
                        auto_process_enabled=True,
                        ad_analysis_enabled=True,
                        initial_backfill_count=1,
                        next_fetch_at=now,
                        now=now,
                    )
                    return
                podcast = rng.choice(podcasts)
                episodes = repo.episodes_for_podcast(tx, podcast.id)
                if choice < 0.45 or not episodes:
                    repo.insert_episodes(
                        tx, podcast.id, [item(f"e{counter}-{i}", position=i) for i in range(rng.randint(1, 4))], now=now
                    )
                elif choice < 0.70:
                    repo.set_episode_states(
                        tx, rng.choice(episodes).id, pipeline_state=rng.choice(["download_pending", "ready"]), now=now
                    )
                elif choice < 0.80:
                    repo.replace_auto_markers(
                        tx, rng.choice(episodes).id, [repo.NewMarker(0.0, 5.0, "intro", "")], classification_id=None,
                        now=now,
                    )
                elif choice < 0.88:
                    repo.set_podcast_switches(
                        tx, podcast, auto_process_enabled=not podcast.auto_process_enabled,
                        ad_analysis_enabled=None, now=now,
                    )
                elif choice < 0.95:
                    repo.delete_podcast(tx, podcast.id, now=now)
                else:
                    repo.update_server_settings(tx, settings, {"auto_process_enabled": rng.random() < 0.5}, now=now)

        for _ in range(30):
            write_something()
        mirror = Mirror()
        for _ in range(60):
            page = repo.sync_page(db, since=mirror.cursor, limit=rng.randint(1, 7))
            mirror.apply(page)
            for _ in range(rng.randint(0, 3)):
                write_something()
        mirror.sync(db, limit=rng.randint(1, 7))
        podcasts, episodes = server_state(db)
        self.assertEqual(mirror.podcasts, podcasts)
        self.assertEqual(mirror.episodes, episodes)


if __name__ == "__main__":
    unittest.main()
