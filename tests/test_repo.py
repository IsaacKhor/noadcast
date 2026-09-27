"""Repository writes: which ones stamp a seq (client-visible changes) and
which deliberately do not (housekeeping), plus marker, transcript,
classification, settings, and tombstone bookkeeping."""

from __future__ import annotations

import dataclasses
import unittest

from noadcast.db import repo
from noadcast.db.engine import Database
from noadcast.timeutil import now_iso
from noadcast.transcribe.protocol import Sentence

from tests.pipeline_support import item, make_settings, seed_episodes, seed_podcast, temp_dir

LATER = "2999-01-01T00:00:00.000Z"


def meta(title: str = "Show", **fields) -> repo.FeedMeta:
    values = {"title": title, "author": None, "summary": None, "artwork_url": None, "language": "en", "link": None}
    values.update(fields)
    return repo.FeedMeta(**values)


class RepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings(temp_dir(self))
        self.db = Database(self.settings.db_path)
        self.addCleanup(self.db.close)

    def seq(self) -> int:
        return self.db.current_seq()

    def podcast(self, podcast_id: int) -> repo.Podcast:
        found = repo.get_podcast(self.db, podcast_id)
        assert found is not None
        return found

    def episode(self, episode_id: int) -> repo.Episode:
        found = repo.get_episode(self.db, episode_id)
        assert found is not None
        return found

    def test_one_seq_per_inserted_row(self) -> None:
        podcast = seed_podcast(self.db)
        episodes = seed_episodes(self.db, podcast.id, [item(f"g{i}", position=i) for i in range(5)])
        seqs = [podcast.updated_seq] + [e.updated_seq for e in episodes]
        self.assertEqual(seqs, list(range(1, 7)))
        self.assertTrue(all(e.pipeline_state == "discovered" for e in episodes))

    def test_switches_bump_only_on_change(self) -> None:
        podcast = seed_podcast(self.db)
        before = self.seq()
        with self.db.write() as tx:
            same = repo.set_podcast_switches(tx, podcast, auto_process_enabled=True, ad_analysis_enabled=None, now=now_iso())
        self.assertEqual((self.seq(), same.updated_seq), (before, podcast.updated_seq))
        with self.db.write() as tx:
            changed = repo.set_podcast_switches(
                tx, podcast, auto_process_enabled=None, ad_analysis_enabled=False, now=now_iso()
            )
        self.assertEqual(changed.updated_seq, before + 1)
        self.assertFalse(changed.ad_analysis_enabled)

    def test_fetch_bookkeeping_is_quiet_unless_the_error_changes(self) -> None:
        podcast = seed_podcast(self.db)
        start = self.seq()
        with self.db.write() as tx:
            repo.record_fetch_not_modified(tx, podcast, next_fetch_at=LATER, now=now_iso())
        self.assertEqual(self.seq(), start, "a 304 bumps nothing")
        with self.db.write() as tx:
            repo.record_fetch_failure(tx, self.podcast(podcast.id), status=503, error="HTTP 503", next_fetch_at=LATER, now=now_iso())
        self.assertEqual(self.seq(), start + 1, "a new error is visible")
        with self.db.write() as tx:
            repo.record_fetch_failure(tx, self.podcast(podcast.id), status=503, error="HTTP 503", next_fetch_at=LATER, now=now_iso())
        self.assertEqual(self.seq(), start + 1, "the same error again is not")
        self.assertEqual(self.podcast(podcast.id).consecutive_failures, 2)
        with self.db.write() as tx:
            repo.record_fetch_not_modified(tx, self.podcast(podcast.id), next_fetch_at=LATER, now=now_iso())
        cleared = self.podcast(podcast.id)
        self.assertEqual(self.seq(), start + 2, "clearing the error is visible")
        self.assertEqual((cleared.last_fetch_error, cleared.consecutive_failures), (None, 0))

    def test_fetch_success_bumps_only_for_visible_changes(self) -> None:
        podcast = seed_podcast(self.db, title="Show")
        seed_episodes(self.db, podcast.id, [item("a", published_at="2026-09-01T00:00:00.000Z")])
        with self.db.write() as tx:
            repo.record_fetch_success(
                tx, self.podcast(podcast.id), meta=meta(), feed_url=podcast.feed_url, etag='"v1"',
                last_modified=None, admitted_watermark=None, next_fetch_at=LATER, now=now_iso(),
            )
        counted = self.podcast(podcast.id)
        self.assertEqual((counted.episode_count, counted.latest_episode_at), (1, "2026-09-01T00:00:00.000Z"))
        steady = self.seq()
        with self.db.write() as tx:
            repo.record_fetch_success(
                tx, counted, meta=meta(), feed_url=podcast.feed_url, etag='"v2"', last_modified="x",
                admitted_watermark="2026-09-01T00:00:00.000Z", next_fetch_at=LATER, now=now_iso(),
            )
        self.assertEqual(self.seq(), steady, "validators and watermark are server-internal")
        self.assertEqual(self.podcast(podcast.id).http_etag, '"v2"')
        with self.db.write() as tx:
            repo.record_fetch_success(
                tx, self.podcast(podcast.id), meta=meta("Renamed"), feed_url=podcast.feed_url, etag='"v3"',
                last_modified=None, admitted_watermark=None, next_fetch_at=LATER, now=now_iso(),
            )
        self.assertEqual(self.seq(), steady + 1)
        self.assertEqual(self.podcast(podcast.id).title, "Renamed")

    def test_feed_updates_bump_only_visible_fields(self) -> None:
        podcast = seed_podcast(self.db)
        (episode,) = seed_episodes(self.db, podcast.id, [item("a")])
        start = self.seq()
        with self.db.write() as tx:
            self.assertFalse(repo.update_episode_from_feed(tx, episode, item("a"), now=now_iso()))
            self.assertFalse(repo.update_episode_from_feed(tx, episode, item("a", position=3), now=now_iso()))
        self.assertEqual(self.seq(), start)
        self.assertEqual(self.episode(episode.id).feed_position, 3)
        with self.db.write() as tx:
            self.assertTrue(repo.update_episode_from_feed(tx, self.episode(episode.id), item("a", title="New"), now=now_iso()))
        self.assertEqual(self.seq(), start + 1)
        with self.db.write() as tx:
            repo.clear_dropped_feed_positions(tx, podcast.id, [])
        self.assertIsNone(self.episode(episode.id).feed_position)
        self.assertEqual(self.seq(), start + 1, "leaving the feed is not client-visible")

    def test_progress_access_and_release_never_bump(self) -> None:
        podcast = seed_podcast(self.db)
        (episode,) = seed_episodes(self.db, podcast.id, [item("a")])
        start = self.seq()
        with self.db.write() as tx:
            repo.set_progress(tx, episode.id, current=10.0, total=100.0, now=now_iso())
            repo.touch_audio_access(tx, episode.id, now=now_iso())
            repo.record_release(tx, episode.id, reason="played", now=now_iso())
        self.assertEqual(self.seq(), start)
        with self.db.write() as tx:
            repo.set_episode_states(tx, episode.id, pipeline_state="download_pending", now=now_iso())
        self.assertEqual(self.episode(episode.id).updated_seq, start + 1)

    def test_set_episode_states_keeps_or_clears_optional_columns(self) -> None:
        podcast = seed_podcast(self.db)
        (episode,) = seed_episodes(self.db, podcast.id, [item("a")])
        with self.db.write() as tx:
            repo.set_episode_states(
                tx, episode.id, pipeline_state="downloading", error="boom",
                progress=repo.Progress("download", 5.0, 50.0), now=now_iso(),
            )
        mid = self.episode(episode.id)
        self.assertEqual((mid.pipeline_error, mid.progress_stage, mid.progress_total), ("boom", "download", 50.0))
        with self.db.write() as tx:
            repo.set_episode_states(tx, episode.id, classify_state="skipped", now=now_iso())
        kept = self.episode(episode.id)
        self.assertEqual((kept.pipeline_state, kept.pipeline_error, kept.progress_stage), ("downloading", "boom", "download"))
        with self.db.write() as tx:
            repo.set_episode_states(tx, episode.id, error=None, progress=None, now=now_iso())
        cleared = self.episode(episode.id)
        self.assertEqual((cleared.pipeline_error, cleared.progress_stage, cleared.progress_current), (None, None, None))

    def test_replacing_auto_markers_keeps_manual_ones_and_bumps_revision(self) -> None:
        podcast = seed_podcast(self.db)
        (episode,) = seed_episodes(self.db, podcast.id, [item("a")])
        stamp = now_iso()
        with self.db.write() as tx:
            tx.execute(
                "INSERT INTO ad_markers (episode_id, start_seconds, end_seconds, kind, summary, source, created_at, updated_at)"
                " VALUES (?, 100, 130, 'ad', 'mine', 'manual', ?, ?)",
                (episode.id, stamp, stamp),
            )
            repo.replace_auto_markers(tx, episode.id, [repo.NewMarker(0.0, 20.0, "intro", "theme")], classification_id=None, now=stamp)
        first = self.episode(episode.id)
        self.assertEqual((first.marker_revision, first.active_marker_count), (1, 2))
        with self.db.write() as tx:
            repo.replace_auto_markers(tx, episode.id, [], classification_id=None, now=stamp)
        second = self.episode(episode.id)
        self.assertEqual((second.marker_revision, second.active_marker_count), (2, 1))
        self.assertGreater(second.updated_seq, first.updated_seq)
        self.assertEqual([m.source for m in repo.markers_for_episode(self.db, episode.id)], ["manual"])

    def test_transcript_replace_round_trips_sentences(self) -> None:
        podcast = seed_podcast(self.db)
        (episode,) = seed_episodes(self.db, podcast.id, [item("a")])
        record = repo.NewTranscript(
            engine="fake", model_id="m", model_sha256=None, language="en", language_probability=1.0,
            audio_sha256="abc", audio_duration_seconds=60.0, speech_duration_seconds=50.0, asr_segment_count=1,
            word_count=3, sentence_count=2, joiner_version=1, joiner_params_json="{}", asr_options_json="{}",
            decode_seconds=None, transcribe_seconds=None,
        )
        sentences = [
            Sentence(0, 0.5, 1.0, "Hi.", 0, 1, "punct", False, 0.9, 0.9, flags=("low_confidence",)),
            Sentence(1, 2.0, 3.0, "Bye now.", 1, 2, "eof", True, 0.8, 0.85),
        ]
        for _ in range(2):  # re-transcription replaces, never duplicates
            with self.db.write() as tx:
                repo.replace_transcript(tx, episode.id, record, sentences, words_codec="c", words_blob=b"x", segments_json="{}", now=now_iso())
        loaded = repo.get_sentences(self.db, episode.id)
        self.assertEqual([(s.text, s.flags, s.soft_end) for s in loaded], [("Hi.", ("low_confidence",), False), ("Bye now.", (), True)])
        self.assertEqual(repo.get_transcript(self.db, episode.id).audio_sha256, "abc")
        self.assertEqual(repo.get_transcript_words(self.db, episode.id).blob, b"x")

    def test_classifications_keep_history_with_one_active(self) -> None:
        podcast = seed_podcast(self.db)
        (episode,) = seed_episodes(self.db, podcast.id, [item("a")])
        record = repo.NewClassification(
            provider="gemini", model="g", thinking=None, prompt_version="segments-v2", render_format="index",
            include_silence=True, joiner_version=1, transcript_audio_sha256="abc", chunk_count=1, input_tokens=10,
            thought_tokens=2, output_tokens=3, cached_input_tokens=0, cache_write_tokens=0, input_cost_usd=0.1,
            thought_cost_usd=0.0, output_cost_usd=0.2, total_cost_usd=0.3, price_table_version="t", latency_ms=5,
            attempts=1, request_sha256=None, raw_segments_json="[]", segments_json="[]",
        )
        ids = []
        for provider in ("gemini", "claude"):
            with self.db.write() as tx:
                c = repo.insert_classification(
                    tx, episode.id, dataclasses.replace(record, provider=provider), raw_response_dir="llm", now=now_iso()
                )
                repo.activate_classification(tx, episode.id, c.id)
                ids.append(c.id)
                self.assertEqual(c.raw_response_path, f"llm/{episode.id}/{c.id}.json.gz")
        history = repo.list_classifications(self.db, episode.id)
        self.assertEqual({c.provider for c in history}, {"gemini", "claude"})
        self.assertEqual([c.id for c in history if c.is_active], [ids[1]])
        with self.db.write() as tx:
            repo.activate_classification(tx, episode.id, None)
        self.assertFalse(any(c.is_active for c in repo.list_classifications(self.db, episode.id)))

    def test_server_settings_overlay_defaults_and_bump_per_changed_key(self) -> None:
        defaults = repo.load_server_settings(self.db, self.settings)
        self.assertEqual((defaults.classifier, defaults.classifier_model, defaults.ad_analysis_enabled), ("openrouter", "deepseek/deepseek-v4.1-flash", True))
        start = self.seq()
        with self.db.write() as tx:
            repo.update_server_settings(tx, self.settings, {"ad_analysis_enabled": True}, now=now_iso())
        self.assertEqual(self.seq(), start, "an unchanged value writes nothing")
        with self.db.write() as tx:
            updated = repo.update_server_settings(tx, self.settings, {"classifier_model": "qwen/qwen3.8-flash", "auto_process_enabled": False}, now=now_iso())
        self.assertEqual(self.seq(), start + 2, "one seq per changed row: model, auto-process")
        self.assertEqual((updated.classifier, updated.classifier_model), ("openrouter", "qwen/qwen3.8-flash"))
        with self.assertRaises(ValueError):
            with self.db.write() as tx:
                repo.update_server_settings(tx, self.settings, {"api_key": "x"}, now=now_iso())

    def test_retired_saved_selection_normalizes_once_with_per_row_sequences(self) -> None:
        with self.db.write() as tx:
            for key, value in (("classifier", '"gemini"'), ("classifier_model", '"gemini-3.5-flash"')):
                tx.execute("INSERT INTO settings (key, value_json, updated_at, updated_seq) VALUES (?, ?, ?, ?)",
                           (key, value, now_iso(), tx.next_seq()))
        before = self.seq()
        with self.db.write() as tx:
            repo.normalize_classifier_settings(tx, self.settings, now=now_iso())
        self.assertEqual(self.seq(), before + 2)
        rows = self.db.read("SELECT updated_seq FROM settings ORDER BY updated_seq")
        self.assertEqual([r["updated_seq"] for r in rows], [before + 1, before + 2])
        selected = repo.load_server_settings(self.db, self.settings)
        self.assertEqual((selected.classifier, selected.classifier_model),
                         ("openrouter", "deepseek/deepseek-v4.1-flash"))
        with self.db.write() as tx:
            repo.normalize_classifier_settings(tx, self.settings, now=now_iso())
        self.assertEqual(self.seq(), before + 2)

    def test_supported_saved_model_survives_normalization(self) -> None:
        with self.db.write() as tx:
            repo.normalize_classifier_settings(tx, self.settings, now=now_iso())
            repo.update_server_settings(tx, self.settings, {"classifier_model": "openai/gpt-6-luna"}, now=now_iso())
        before = self.seq()
        with self.db.write() as tx:
            repo.normalize_classifier_settings(tx, self.settings, now=now_iso())
        self.assertEqual(self.seq(), before)
        self.assertEqual(repo.load_server_settings(self.db, self.settings).classifier_model, "openai/gpt-6-luna")

    def test_config_only_upgrade_announces_new_defaults_after_existing_cursor(self) -> None:
        seed_podcast(self.db)
        cursor = self.seq()  # The old client already mirrored its config-only Gemini selection.
        self.assertFalse(repo.sync_page(self.db, since=cursor, limit=100).settings_changed)
        with self.db.write() as tx:
            repo.normalize_classifier_settings(tx, self.settings, now=now_iso())
        delta = repo.sync_page(self.db, since=cursor, limit=100)
        self.assertTrue(delta.settings_changed)
        self.assertEqual(delta.next_since, cursor + 2)
        selected = repo.load_server_settings(self.db, self.settings)
        self.assertEqual((selected.classifier, selected.classifier_model),
                         ("openrouter", "deepseek/deepseek-v4.1-flash"))
        rows = self.db.read("SELECT updated_seq FROM settings ORDER BY updated_seq")
        self.assertEqual([row["updated_seq"] for row in rows], [cursor + 1, cursor + 2])
        with self.db.write() as tx:
            repo.normalize_classifier_settings(tx, self.settings, now=now_iso())
        self.assertEqual(self.seq(), cursor + 2)

    def test_delete_podcast_cascades_and_leaves_one_tombstone(self) -> None:
        podcast = seed_podcast(self.db)
        (episode,) = seed_episodes(self.db, podcast.id, [item("a")])
        with self.db.write() as tx:
            repo.replace_auto_markers(tx, episode.id, [repo.NewMarker(0, 1, "intro", "")], classification_id=None, now=now_iso())
            self.assertTrue(repo.delete_podcast(tx, podcast.id, now=now_iso()))
            self.assertFalse(repo.delete_podcast(tx, podcast.id, now=now_iso()))
        self.assertIsNone(repo.get_episode(self.db, episode.id))
        self.assertEqual(self.db.read_one("SELECT count(*) AS n FROM ad_markers")["n"], 0)
        tombstones = self.db.read("SELECT entity, entity_id FROM tombstones")
        self.assertEqual([(t["entity"], t["entity_id"]) for t in tombstones], [("podcast", podcast.id)])

    def test_guid_collisions_are_reported_not_prevented(self) -> None:
        a = seed_podcast(self.db, "https://a.example/feed")
        b = seed_podcast(self.db, "https://b.example/feed")
        seed_episodes(self.db, a.id, [item("shared"), item("only-a", position=1)])
        seed_episodes(self.db, b.id, [item("shared")])
        collisions = repo.guid_collisions(self.db)
        self.assertEqual([(c.guid, c.podcast_ids) for c in collisions], [("shared", (a.id, b.id))])

    def test_usage_aggregates_by_day_and_model(self) -> None:
        podcast = seed_podcast(self.db)
        (episode,) = seed_episodes(self.db, podcast.id, [item("a")])
        rows = [("gemini", "g", "2026-09-20T10:00:00.000Z", 0.5), ("gemini", "g", "2026-09-21T10:00:00.000Z", 0.25),
                ("claude", "c", "2026-09-21T11:00:00.000Z", 1.0)]
        with self.db.write() as tx:
            for provider, model, created, cost in rows:
                tx.execute(
                    "INSERT INTO classifications (episode_id, provider, model, prompt_version, render_format, include_silence,"
                    " input_tokens, total_cost_usd, price_table_version, raw_segments_json, segments_json, created_at)"
                    " VALUES (?, ?, ?, 'v', 'index', 1, 100, ?, 't', '[]', '[]', ?)",
                    (episode.id, provider, model, cost, created),
                )
        days = repo.usage_by_day(self.db, created_since="2026-09-21T00:00:00.000Z")
        self.assertEqual([(d.key, d.calls, d.cost_usd) for d in days], [("2026-09-21", 2, 1.25)])
        models = repo.usage_by_model(self.db, created_since="2026-01-01T00:00:00.000Z")
        self.assertEqual([(m.provider, m.model, m.calls, m.input_tokens) for m in models], [("claude", "c", 1, 100), ("gemini", "g", 2, 200)])


if __name__ == "__main__":
    unittest.main()
