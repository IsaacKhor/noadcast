-- Sync cursor floor, id high-water marks, and deferred audio release.

-- Highest seq of any tombstone pruned after the retention window. A client
-- cursor below it may have missed a deletion, so /sync answers 410
-- cursorExpired and the client resyncs from 0.
ALTER TABLE sync_state ADD COLUMN tombstone_floor_seq INTEGER NOT NULL DEFAULT 0;

-- Podcast and episode ids are never reused. Without AUTOINCREMENT, SQLite
-- hands a deleted max-id row's id to the next insert; a client applying a
-- page (podcasts, episodes, then deletions) would then delete the re-created
-- podcast along with the old one's tombstone, and a reused episode id would
-- inherit the old episode's device-local playback state. Inserts allocate
-- ids above these marks (db/repo.py).
ALTER TABLE sync_state ADD COLUMN podcast_id_high INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_state ADD COLUMN episode_id_high INTEGER NOT NULL DEFAULT 0;
UPDATE sync_state SET
  podcast_id_high = (SELECT coalesce(max(id), 0) FROM podcasts),
  episode_id_high = (SELECT coalesce(max(id), 0) FROM episodes);

-- Why the client released the audio (played | manual). A release that
-- arrives while a pipeline job still needs the file is recorded here and
-- carried out by the eviction sweep once the job is gone.
ALTER TABLE episodes ADD COLUMN release_reason TEXT;
