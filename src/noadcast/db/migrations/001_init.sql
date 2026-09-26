-- Noadcast server schema, version 1.
--
-- Conventions:
--   * Timestamps are TEXT in noadcast.timeutil.iso() format (fixed-width UTC,
--     millisecond precision, "Z"), so string comparison is time comparison.
--   * Booleans are INTEGER 0/1.
--   * Every client-visible row carries updated_seq, allocated from sync_state
--     inside the same write transaction as the row change (see db/engine.py).
--     Ad markers have no seq of their own: they travel embedded in their
--     episode, and any marker change bumps the episode's seq.

CREATE TABLE sync_state (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  seq         INTEGER NOT NULL,
  instance_id TEXT    NOT NULL   -- changes only if the database is recreated
);
INSERT INTO sync_state (id, seq, instance_id) VALUES (1, 0, lower(hex(randomblob(16))));

CREATE TABLE podcasts (
  id                      INTEGER PRIMARY KEY,
  feed_url                TEXT    NOT NULL UNIQUE,
  title                   TEXT    NOT NULL,
  author                  TEXT,
  summary                 TEXT,
  language                TEXT,
  link                    TEXT,
  artwork_url             TEXT,
  -- Server-side processing switches. Client-side download/playback
  -- preferences (auto-download to the phone, playback speed) are device-local.
  auto_process_enabled    INTEGER NOT NULL DEFAULT 1,  -- new episodes flow through download/transcribe/classify
  ad_analysis_enabled     INTEGER NOT NULL DEFAULT 1,  -- transcribe + classify after download
  initial_backfill_count  INTEGER NOT NULL DEFAULT 1,
  admitted_watermark      TEXT,                        -- newest published_at ever auto-admitted
  http_etag               TEXT,
  http_last_modified      TEXT,
  last_fetch_at           TEXT,
  last_fetch_status       INTEGER,
  last_fetch_error        TEXT,
  consecutive_failures    INTEGER NOT NULL DEFAULT 0,
  next_fetch_at           TEXT    NOT NULL,
  episode_count           INTEGER NOT NULL DEFAULT 0,
  latest_episode_at       TEXT,
  created_at              TEXT    NOT NULL,
  updated_at              TEXT    NOT NULL,
  updated_seq             INTEGER NOT NULL
);
CREATE INDEX podcasts_seq        ON podcasts(updated_seq);
CREATE INDEX podcasts_next_fetch ON podcasts(next_fetch_at);

CREATE TABLE episodes (
  id                        INTEGER PRIMARY KEY,
  podcast_id                INTEGER NOT NULL REFERENCES podcasts(id) ON DELETE CASCADE,
  guid                      TEXT    NOT NULL,   -- <guid>, falling back to the enclosure URL
  title                     TEXT    NOT NULL,
  description               TEXT,
  published_at              TEXT,
  feed_position             INTEGER,            -- index in the latest fetch; NULL once dropped from the feed
  declared_duration_seconds REAL,               -- itunes:duration; often wrong
  measured_duration_seconds REAL,               -- decoded length; authoritative when present
  enclosure_url             TEXT    NOT NULL,
  enclosure_type            TEXT,
  enclosure_length          INTEGER,
  artwork_url               TEXT,

  audio_state               TEXT    NOT NULL DEFAULT 'absent'
                              CHECK (audio_state IN ('absent', 'partial', 'present', 'evicted')),
  audio_path                TEXT,               -- relative to the data directory
  audio_bytes               INTEGER,
  audio_sha256              TEXT,
  audio_content_type        TEXT,
  audio_codec               TEXT,
  origin_etag               TEXT,               -- validators from the podcast host, for resumable downloads
  origin_last_modified      TEXT,
  audio_downloaded_at       TEXT,
  audio_last_access_at      TEXT,               -- does NOT bump updated_seq
  audio_evicted_at          TEXT,
  audio_evicted_reason      TEXT,               -- played | age | disk | manual
  released_at               TEXT,               -- client reported the episode played

  pipeline_state            TEXT    NOT NULL DEFAULT 'discovered'
                              CHECK (pipeline_state IN (
                                'discovered', 'download_pending', 'downloading', 'downloaded',
                                'transcribe_pending', 'transcribing', 'transcribed',
                                'classify_pending', 'classifying', 'ready', 'failed')),
  pipeline_error            TEXT,
  progress_stage            TEXT,               -- progress_* do NOT bump updated_seq; see GET /jobs/active
  progress_current          REAL,
  progress_total            REAL,
  progress_updated_at       TEXT,
  transcript_state          TEXT    NOT NULL DEFAULT 'none'
                              CHECK (transcript_state IN ('none', 'ready', 'stale', 'failed')),
  classify_state            TEXT    NOT NULL DEFAULT 'none'
                              CHECK (classify_state IN ('none', 'ready', 'stale', 'failed', 'skipped')),
  active_marker_count       INTEGER NOT NULL DEFAULT 0,
  marker_revision           INTEGER NOT NULL DEFAULT 0,

  created_at                TEXT    NOT NULL,
  updated_at                TEXT    NOT NULL,
  updated_seq               INTEGER NOT NULL,
  UNIQUE (podcast_id, guid)   -- per feed, not global: two feeds may reuse a GUID
);
CREATE INDEX episodes_seq         ON episodes(updated_seq);
CREATE INDEX episodes_podcast_pub ON episodes(podcast_id, published_at DESC);
CREATE INDEX episodes_state       ON episodes(pipeline_state);
CREATE INDEX episodes_audio       ON episodes(audio_state, audio_last_access_at);
CREATE INDEX episodes_guid        ON episodes(guid);   -- collision reporting only; enforces nothing

CREATE TABLE transcripts (
  episode_id              INTEGER PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
  engine                  TEXT    NOT NULL,
  model_id                TEXT    NOT NULL,
  model_sha256            TEXT,
  language                TEXT    NOT NULL,
  language_probability    REAL,
  audio_sha256            TEXT,      -- which audio bytes this transcript describes
  audio_duration_seconds  REAL    NOT NULL,
  speech_duration_seconds REAL,
  asr_segment_count       INTEGER NOT NULL,
  word_count              INTEGER NOT NULL,
  sentence_count          INTEGER NOT NULL,
  joiner_version          INTEGER NOT NULL,
  joiner_params_json      TEXT    NOT NULL,
  asr_options_json        TEXT    NOT NULL,
  decode_seconds          REAL,
  transcribe_seconds      REAL,
  created_at              TEXT    NOT NULL
);

CREATE TABLE transcript_sentences (
  episode_id    INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  idx           INTEGER NOT NULL,
  start_seconds REAL    NOT NULL,
  end_seconds   REAL    NOT NULL,
  text          TEXT    NOT NULL,
  word_start    INTEGER NOT NULL,
  word_count    INTEGER NOT NULL,
  break_reason  TEXT    NOT NULL,        -- punct | gap | cap | eof
  soft_end      INTEGER NOT NULL DEFAULT 0,
  min_p         REAL,
  mean_p        REAL,
  flags         TEXT    NOT NULL DEFAULT '',  -- comma-separated
  PRIMARY KEY (episode_id, idx)
) WITHOUT ROWID;

-- One compressed columnar blob per episode (see transcribe/codec.py) rather
-- than a row per word: nothing queries individual words.
CREATE TABLE transcript_words (
  episode_id    INTEGER PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
  codec         TEXT    NOT NULL,
  word_count    INTEGER NOT NULL,
  blob          BLOB    NOT NULL,
  segments_json TEXT    NOT NULL   -- ASR segment metadata: bounds, compression ratio, no-speech prob
);

-- Every classification is kept; is_active marks the one that produced the
-- live auto markers. This table is the A/B substrate.
CREATE TABLE classifications (
  id                      INTEGER PRIMARY KEY,
  episode_id              INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  provider                TEXT    NOT NULL,
  model                   TEXT    NOT NULL,
  thinking                TEXT,
  prompt_version          TEXT    NOT NULL,
  render_format           TEXT    NOT NULL,
  include_silence         INTEGER NOT NULL,
  joiner_version          INTEGER,
  transcript_audio_sha256 TEXT,
  chunk_count             INTEGER NOT NULL DEFAULT 1,
  input_tokens            INTEGER NOT NULL DEFAULT 0,
  thought_tokens          INTEGER NOT NULL DEFAULT 0,
  output_tokens           INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens     INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens      INTEGER NOT NULL DEFAULT 0,
  input_cost_usd          REAL    NOT NULL DEFAULT 0,
  thought_cost_usd        REAL    NOT NULL DEFAULT 0,
  output_cost_usd         REAL    NOT NULL DEFAULT 0,
  total_cost_usd          REAL    NOT NULL DEFAULT 0,
  price_table_version     TEXT    NOT NULL,
  latency_ms              INTEGER,
  attempts                INTEGER NOT NULL DEFAULT 1,
  request_sha256          TEXT,
  raw_response_path       TEXT,              -- relative to the data directory, gzipped JSON
  raw_segments_json       TEXT    NOT NULL,  -- as returned by the model
  segments_json           TEXT    NOT NULL,  -- after sanitising and snapping
  is_active               INTEGER NOT NULL DEFAULT 0,
  created_at              TEXT    NOT NULL
);
CREATE INDEX classifications_episode ON classifications(episode_id, created_at DESC);
CREATE INDEX classifications_created ON classifications(created_at);

CREATE TABLE ad_markers (
  id                INTEGER PRIMARY KEY,
  episode_id        INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  start_seconds     REAL    NOT NULL,
  end_seconds       REAL    NOT NULL,
  kind              TEXT    NOT NULL CHECK (kind IN ('ad', 'intro', 'outro')),
  summary           TEXT    NOT NULL DEFAULT '',
  source            TEXT    NOT NULL CHECK (source IN ('auto', 'manual')),
  classification_id INTEGER REFERENCES classifications(id) ON DELETE SET NULL,
  deleted           INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT    NOT NULL,
  updated_at        TEXT    NOT NULL
);
CREATE INDEX ad_markers_episode ON ad_markers(episode_id, start_seconds);

CREATE TABLE jobs (
  id               INTEGER PRIMARY KEY,
  kind             TEXT    NOT NULL,   -- refresh_feed | download | transcribe | classify | evict
  subject_id       INTEGER NOT NULL,   -- podcast id for refresh_feed, episode id otherwise, 0 for global
  state            TEXT    NOT NULL CHECK (state IN ('pending', 'running', 'done', 'failed', 'canceled')),
  priority         INTEGER NOT NULL DEFAULT 100,   -- lower runs first
  attempts         INTEGER NOT NULL DEFAULT 0,
  max_attempts     INTEGER NOT NULL DEFAULT 5,
  available_at     TEXT    NOT NULL,
  lease_owner      TEXT,
  lease_expires_at TEXT,
  params_json      TEXT    NOT NULL DEFAULT '{}',
  last_error       TEXT,
  last_error_at    TEXT,
  created_at       TEXT    NOT NULL,
  updated_at       TEXT    NOT NULL,
  finished_at      TEXT
);
-- Idempotent enqueue: at most one live job per (kind, subject).
CREATE UNIQUE INDEX jobs_live    ON jobs(kind, subject_id) WHERE state IN ('pending', 'running');
CREATE INDEX        jobs_claim   ON jobs(state, kind, available_at, priority);
CREATE INDEX        jobs_subject ON jobs(subject_id, kind);

CREATE TABLE tombstones (
  id          INTEGER PRIMARY KEY,
  entity      TEXT    NOT NULL CHECK (entity IN ('podcast', 'episode')),
  entity_id   INTEGER NOT NULL,
  deleted_at  TEXT    NOT NULL,
  updated_seq INTEGER NOT NULL
);
CREATE INDEX tombstones_seq ON tombstones(updated_seq);

-- Global server settings exposed to the client (e.g. adAnalysisEnabled).
CREATE TABLE settings (
  key         TEXT    PRIMARY KEY,
  value_json  TEXT    NOT NULL,
  updated_at  TEXT    NOT NULL,
  updated_seq INTEGER NOT NULL
);
