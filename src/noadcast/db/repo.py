"""Named SQL for podcasts, episodes, transcripts, classifications, markers,
tombstones, server settings, and the sync page. (Job-table SQL lives in
``pipeline/jobs.py``.)

Seq discipline (see db/engine.py): a function that changes something a
client can see stamps the row with a fresh ``tx.next_seq()`` in the same
transaction. Server-internal columns — HTTP validators, the fetch schedule,
``progress_*``, ``audio_last_access_at``, ``feed_position``, release
bookkeeping — are written without a seq, so housekeeping never generates
client sync traffic. Marker changes always bump their episode's seq and
``marker_revision``, because markers travel embedded in the episode.

Variable-length id lists are bound as one JSON array and expanded with
``json_each`` so no statement is ever assembled from strings.
"""

from __future__ import annotations

import dataclasses
import enum
import functools
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence, TypeVar

from ..config import Settings
from ..transcribe.protocol import Sentence
from .engine import Params, WriteTx


class Reader(Protocol):
    """Satisfied by both ``Database`` and ``WriteTx``."""

    def read(self, sql: str, params: Params = ()) -> list[sqlite3.Row]: ...

    def read_one(self, sql: str, params: Params = ()) -> sqlite3.Row | None: ...


_T = TypeVar("_T")


@functools.cache
def _field_spec(cls: type) -> tuple[tuple[str, ...], frozenset[str]]:
    fields = dataclasses.fields(cls)
    return tuple(f.name for f in fields), frozenset(f.name for f in fields if f.type == "bool")


def _load(cls: type[_T], row: sqlite3.Row) -> _T:
    # Selecting only the dataclass's fields keeps rows loadable after a later
    # migration adds columns; INTEGER 0/1 booleans become real bools.
    names, bools = _field_spec(cls)
    return cls(**{name: bool(row[name]) if name in bools else row[name] for name in names})


def _ids(ids: Iterable[int]) -> str:
    return json.dumps(list(dict.fromkeys(ids)))


class _Keep(enum.Enum):
    KEEP = 0


KEEP = _Keep.KEEP
"""Sentinel for optional column updates that may legitimately set NULL."""


# -- row types ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Podcast:
    id: int
    feed_url: str
    title: str
    author: str | None
    summary: str | None
    language: str | None
    link: str | None
    artwork_url: str | None
    auto_process_enabled: bool
    ad_analysis_enabled: bool
    initial_backfill_count: int
    admitted_watermark: str | None
    http_etag: str | None
    http_last_modified: str | None
    last_fetch_at: str | None
    last_fetch_status: int | None
    last_fetch_error: str | None
    consecutive_failures: int
    next_fetch_at: str
    episode_count: int
    latest_episode_at: str | None
    created_at: str
    updated_at: str
    updated_seq: int


@dataclass(frozen=True, slots=True)
class Episode:
    id: int
    podcast_id: int
    guid: str
    title: str
    description: str | None
    published_at: str | None
    feed_position: int | None
    declared_duration_seconds: float | None
    measured_duration_seconds: float | None
    enclosure_url: str
    enclosure_type: str | None
    enclosure_length: int | None
    artwork_url: str | None
    audio_state: str
    audio_path: str | None
    audio_bytes: int | None
    audio_sha256: str | None
    audio_content_type: str | None
    audio_codec: str | None
    origin_etag: str | None
    origin_last_modified: str | None
    audio_downloaded_at: str | None
    audio_last_access_at: str | None
    audio_evicted_at: str | None
    audio_evicted_reason: str | None
    released_at: str | None
    release_reason: str | None
    pipeline_state: str
    pipeline_error: str | None
    progress_stage: str | None
    progress_current: float | None
    progress_total: float | None
    progress_updated_at: str | None
    transcript_state: str
    classify_state: str
    active_marker_count: int
    marker_revision: int
    created_at: str
    updated_at: str
    updated_seq: int

    @property
    def duration_seconds(self) -> float | None:
        """Decoded duration when known; the feed's declared value otherwise."""
        if self.measured_duration_seconds is not None:
            return self.measured_duration_seconds
        return self.declared_duration_seconds


@dataclass(frozen=True, slots=True)
class Marker:
    id: int
    episode_id: int
    start_seconds: float
    end_seconds: float
    kind: str
    summary: str
    source: str
    classification_id: int | None
    deleted: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Tombstone:
    id: int
    entity: str
    entity_id: int
    deleted_at: str
    updated_seq: int


@dataclass(frozen=True, slots=True)
class Transcript:
    episode_id: int
    engine: str
    model_id: str
    model_sha256: str | None
    language: str
    language_probability: float | None
    audio_sha256: str | None
    audio_duration_seconds: float
    speech_duration_seconds: float | None
    asr_segment_count: int
    word_count: int
    sentence_count: int
    joiner_version: int
    joiner_params_json: str
    asr_options_json: str
    decode_seconds: float | None
    transcribe_seconds: float | None
    created_at: str


@dataclass(frozen=True, slots=True)
class TranscriptWords:
    episode_id: int
    codec: str
    word_count: int
    blob: bytes
    segments_json: str


@dataclass(frozen=True, slots=True)
class Classification:
    id: int
    episode_id: int
    provider: str
    model: str
    thinking: str | None
    prompt_version: str
    render_format: str
    include_silence: bool
    joiner_version: int | None
    transcript_audio_sha256: str | None
    chunk_count: int
    input_tokens: int
    thought_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    input_cost_usd: float
    thought_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float
    price_table_version: str
    latency_ms: int | None
    attempts: int
    request_sha256: str | None
    raw_response_path: str | None
    raw_segments_json: str
    segments_json: str
    is_active: bool
    created_at: str


# -- input types ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeedMeta:
    title: str
    author: str | None
    summary: str | None
    artwork_url: str | None
    language: str | None
    link: str | None


@dataclass(frozen=True, slots=True)
class FeedItem:
    guid: str
    title: str
    description: str | None
    published_at: str | None
    declared_duration_seconds: float | None
    enclosure_url: str
    enclosure_type: str | None
    enclosure_length: int | None
    artwork_url: str | None
    feed_position: int


@dataclass(frozen=True, slots=True)
class StoredAudio:
    path: str  # relative to the data directory
    bytes: int
    sha256: str
    content_type: str
    codec: str | None
    origin_etag: str | None
    origin_last_modified: str | None


@dataclass(frozen=True, slots=True)
class Progress:
    stage: str
    current: float | None
    total: float | None


@dataclass(frozen=True, slots=True)
class NewTranscript:
    engine: str
    model_id: str
    model_sha256: str | None
    language: str
    language_probability: float | None
    audio_sha256: str | None
    audio_duration_seconds: float
    speech_duration_seconds: float | None
    asr_segment_count: int
    word_count: int
    sentence_count: int
    joiner_version: int
    joiner_params_json: str
    asr_options_json: str
    decode_seconds: float | None
    transcribe_seconds: float | None


@dataclass(frozen=True, slots=True)
class NewClassification:
    provider: str
    model: str
    thinking: str | None
    prompt_version: str
    render_format: str
    include_silence: bool
    joiner_version: int | None
    transcript_audio_sha256: str | None
    chunk_count: int
    input_tokens: int
    thought_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    input_cost_usd: float
    thought_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float
    price_table_version: str
    latency_ms: int | None
    attempts: int
    request_sha256: str | None
    raw_segments_json: str
    segments_json: str


@dataclass(frozen=True, slots=True)
class NewMarker:
    start_seconds: float
    end_seconds: float
    kind: str
    summary: str


# -- podcasts -------------------------------------------------------------------


def get_podcast(db: Reader, podcast_id: int) -> Podcast | None:
    row = db.read_one("SELECT * FROM podcasts WHERE id = ?", (podcast_id,))
    return None if row is None else _load(Podcast, row)


def get_podcast_by_feed_url(db: Reader, feed_url: str) -> Podcast | None:
    row = db.read_one("SELECT * FROM podcasts WHERE feed_url = ?", (feed_url,))
    return None if row is None else _load(Podcast, row)


def list_podcasts(db: Reader) -> list[Podcast]:
    rows = db.read("SELECT * FROM podcasts ORDER BY title COLLATE NOCASE, id")
    return [_load(Podcast, row) for row in rows]


def podcasts_by_ids(db: Reader, ids: Iterable[int]) -> list[Podcast]:
    rows = db.read(
        "SELECT * FROM podcasts WHERE id IN (SELECT value FROM json_each(?)) ORDER BY updated_seq", (_ids(ids),)
    )
    return [_load(Podcast, row) for row in rows]


def due_podcast_ids(db: Reader, now: str) -> list[int]:
    rows = db.read("SELECT id FROM podcasts WHERE next_fetch_at <= ? ORDER BY next_fetch_at, id", (now,))
    return [row["id"] for row in rows]


def earliest_next_fetch(db: Reader) -> str | None:
    row = db.read_one("SELECT min(next_fetch_at) AS at FROM podcasts")
    return None if row is None else row["at"]


def _allocate_podcast_id(tx: WriteTx) -> int:
    """Ids are never reused (see migration 002): allocate above every id ever issued."""
    row = tx.read_one(
        """
        UPDATE sync_state SET podcast_id_high = max(podcast_id_high, (SELECT coalesce(max(id), 0) FROM podcasts)) + 1
        WHERE id = 1 RETURNING podcast_id_high
        """
    )
    assert row is not None
    return row[0]


def _allocate_episode_ids(tx: WriteTx, count: int) -> int:
    """First of ``count`` consecutive never-used episode ids."""
    row = tx.read_one(
        """
        UPDATE sync_state SET episode_id_high = max(episode_id_high, (SELECT coalesce(max(id), 0) FROM episodes)) + ?
        WHERE id = 1 RETURNING episode_id_high
        """,
        (count,),
    )
    assert row is not None
    return row[0] - count + 1


def insert_podcast(
    tx: WriteTx,
    *,
    feed_url: str,
    title: str,
    auto_process_enabled: bool,
    ad_analysis_enabled: bool,
    initial_backfill_count: int,
    next_fetch_at: str,
    now: str,
) -> Podcast:
    row = tx.read_one(
        """
        INSERT INTO podcasts (id, feed_url, title, auto_process_enabled, ad_analysis_enabled,
                              initial_backfill_count, next_fetch_at, created_at, updated_at, updated_seq)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING *
        """,
        (
            _allocate_podcast_id(tx),
            feed_url,
            title,
            int(auto_process_enabled),
            int(ad_analysis_enabled),
            initial_backfill_count,
            next_fetch_at,
            now,
            now,
            tx.next_seq(),
        ),
    )
    assert row is not None
    return _load(Podcast, row)


def set_podcast_switches(
    tx: WriteTx,
    podcast: Podcast,
    *,
    auto_process_enabled: bool | None,
    ad_analysis_enabled: bool | None,
    now: str,
) -> Podcast:
    """PATCH semantics: None leaves a switch alone; bumps seq only on a real change."""
    auto = podcast.auto_process_enabled if auto_process_enabled is None else auto_process_enabled
    analysis = podcast.ad_analysis_enabled if ad_analysis_enabled is None else ad_analysis_enabled
    if (auto, analysis) == (podcast.auto_process_enabled, podcast.ad_analysis_enabled):
        return podcast
    row = tx.read_one(
        """
        UPDATE podcasts SET auto_process_enabled = ?, ad_analysis_enabled = ?, updated_at = ?, updated_seq = ?
        WHERE id = ? RETURNING *
        """,
        (int(auto), int(analysis), now, tx.next_seq(), podcast.id),
    )
    assert row is not None
    return _load(Podcast, row)


def record_fetch_not_modified(tx: WriteTx, podcast: Podcast, *, next_fetch_at: str, now: str) -> None:
    """A 304 changes nothing a client can see — unless it clears a previous error."""
    if podcast.last_fetch_error is None:
        tx.execute(
            """
            UPDATE podcasts SET last_fetch_at = ?, last_fetch_status = 304, consecutive_failures = 0,
                                next_fetch_at = ?
            WHERE id = ?
            """,
            (now, next_fetch_at, podcast.id),
        )
        return
    tx.execute(
        """
        UPDATE podcasts SET last_fetch_at = ?, last_fetch_status = 304, consecutive_failures = 0,
                            next_fetch_at = ?, last_fetch_error = NULL, updated_at = ?, updated_seq = ?
        WHERE id = ?
        """,
        (now, next_fetch_at, now, tx.next_seq(), podcast.id),
    )


def record_fetch_failure(
    tx: WriteTx, podcast: Podcast, *, status: int | None, error: str, next_fetch_at: str, now: str
) -> None:
    """Counts the failure for backoff; bumps seq only when the visible error text changes."""
    if podcast.last_fetch_error == error:
        tx.execute(
            """
            UPDATE podcasts SET last_fetch_at = ?, last_fetch_status = ?,
                                consecutive_failures = consecutive_failures + 1, next_fetch_at = ?
            WHERE id = ?
            """,
            (now, status, next_fetch_at, podcast.id),
        )
        return
    tx.execute(
        """
        UPDATE podcasts SET last_fetch_at = ?, last_fetch_status = ?,
                            consecutive_failures = consecutive_failures + 1, next_fetch_at = ?,
                            last_fetch_error = ?, updated_at = ?, updated_seq = ?
        WHERE id = ?
        """,
        (now, status, next_fetch_at, error, now, tx.next_seq(), podcast.id),
    )


def record_fetch_success(
    tx: WriteTx,
    podcast: Podcast,
    *,
    meta: FeedMeta,
    feed_url: str,
    etag: str | None,
    last_modified: str | None,
    admitted_watermark: str | None,
    next_fetch_at: str,
    now: str,
) -> None:
    """Store a parsed 200. Recomputes the denormalised episode counts; bumps seq
    only if a client-visible field (metadata, counts, error, feed URL) changed."""
    counts = tx.read_one(
        "SELECT count(*) AS n, max(published_at) AS latest FROM episodes WHERE podcast_id = ?", (podcast.id,)
    )
    assert counts is not None
    visible_before = (
        podcast.feed_url,
        podcast.title,
        podcast.author,
        podcast.summary,
        podcast.artwork_url,
        podcast.language,
        podcast.link,
        podcast.episode_count,
        podcast.latest_episode_at,
        podcast.last_fetch_error,
    )
    visible_after = (
        feed_url,
        meta.title,
        meta.author,
        meta.summary,
        meta.artwork_url,
        meta.language,
        meta.link,
        counts["n"],
        counts["latest"],
        None,
    )
    changed = visible_before != visible_after
    tx.execute(
        """
        UPDATE podcasts SET
          feed_url = ?, title = ?, author = ?, summary = ?, artwork_url = ?, language = ?, link = ?,
          episode_count = ?, latest_episode_at = ?, last_fetch_error = NULL,
          http_etag = ?, http_last_modified = ?, admitted_watermark = ?,
          last_fetch_at = ?, last_fetch_status = 200, consecutive_failures = 0, next_fetch_at = ?,
          updated_at = CASE WHEN ? THEN ? ELSE updated_at END,
          updated_seq = CASE WHEN ? THEN ? ELSE updated_seq END
        WHERE id = ?
        """,
        (
            feed_url,
            meta.title,
            meta.author,
            meta.summary,
            meta.artwork_url,
            meta.language,
            meta.link,
            counts["n"],
            counts["latest"],
            etag,
            last_modified,
            admitted_watermark,
            now,
            next_fetch_at,
            changed,
            now,
            changed,
            tx.next_seq() if changed else 0,
            podcast.id,
        ),
    )


def schedule_fetch(tx: WriteTx, podcast_id: int, *, next_fetch_at: str) -> None:
    tx.execute("UPDATE podcasts SET next_fetch_at = ? WHERE id = ?", (next_fetch_at, podcast_id))


def delete_podcast(tx: WriteTx, podcast_id: int, *, now: str) -> bool:
    """Delete a podcast (episodes, transcripts, classifications, and markers
    cascade) and leave one tombstone; clients cascade the episodes locally."""
    cursor = tx.execute("DELETE FROM podcasts WHERE id = ?", (podcast_id,))
    if cursor.rowcount == 0:
        return False
    tx.execute(
        "INSERT INTO tombstones (entity, entity_id, deleted_at, updated_seq) VALUES ('podcast', ?, ?, ?)",
        (podcast_id, now, tx.next_seq()),
    )
    return True


# -- episodes -------------------------------------------------------------------


def get_episode(db: Reader, episode_id: int) -> Episode | None:
    row = db.read_one("SELECT * FROM episodes WHERE id = ?", (episode_id,))
    return None if row is None else _load(Episode, row)


def episodes_for_podcast(db: Reader, podcast_id: int) -> list[Episode]:
    rows = db.read(
        "SELECT * FROM episodes WHERE podcast_id = ? ORDER BY published_at DESC, id DESC", (podcast_id,)
    )
    return [_load(Episode, row) for row in rows]


def episodes_by_ids(db: Reader, ids: Iterable[int]) -> list[Episode]:
    rows = db.read("SELECT * FROM episodes WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id", (_ids(ids),))
    return [_load(Episode, row) for row in rows]


def episodes_in_pipeline_states(db: Reader, states: Iterable[str]) -> list[Episode]:
    rows = db.read(
        "SELECT * FROM episodes WHERE pipeline_state IN (SELECT value FROM json_each(?)) ORDER BY id",
        (json.dumps(list(states)),),
    )
    return [_load(Episode, row) for row in rows]


def episodes_with_audio_state(db: Reader, audio_state: str) -> list[Episode]:
    rows = db.read("SELECT * FROM episodes WHERE audio_state = ? ORDER BY id", (audio_state,))
    return [_load(Episode, row) for row in rows]


def insert_episodes(tx: WriteTx, podcast_id: int, items: Sequence[FeedItem], *, now: str) -> list[int]:
    """Insert new feed items as ``discovered``, one seq each."""
    ids: list[int] = []
    if not items:
        return ids
    first_seq = tx.next_seq(len(items))
    first_id = _allocate_episode_ids(tx, len(items))
    for offset, item in enumerate(items):
        row = tx.read_one(
            """
            INSERT INTO episodes (id, podcast_id, guid, title, description, published_at, feed_position,
                                  declared_duration_seconds, enclosure_url, enclosure_type, enclosure_length,
                                  artwork_url, created_at, updated_at, updated_seq)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                first_id + offset,
                podcast_id,
                item.guid,
                item.title,
                item.description,
                item.published_at,
                item.feed_position,
                item.declared_duration_seconds,
                item.enclosure_url,
                item.enclosure_type,
                item.enclosure_length,
                item.artwork_url,
                now,
                now,
                first_seq + offset,
            ),
        )
        assert row is not None
        ids.append(row["id"])
    return ids


def update_episode_from_feed(tx: WriteTx, episode: Episode, item: FeedItem, *, now: str) -> bool:
    """Refresh feed-owned fields in place. Returns True if a client-visible
    field changed (and the seq was bumped); feed position and enclosure length
    are tracked quietly."""
    visible_changed = (
        episode.title,
        episode.description,
        episode.published_at,
        episode.declared_duration_seconds,
        episode.enclosure_url,
        episode.enclosure_type,
        episode.artwork_url,
    ) != (
        item.title,
        item.description,
        item.published_at,
        item.declared_duration_seconds,
        item.enclosure_url,
        item.enclosure_type,
        item.artwork_url,
    )
    if visible_changed:
        tx.execute(
            """
            UPDATE episodes SET title = ?, description = ?, published_at = ?, declared_duration_seconds = ?,
                                enclosure_url = ?, enclosure_type = ?, artwork_url = ?,
                                enclosure_length = ?, feed_position = ?, updated_at = ?, updated_seq = ?
            WHERE id = ?
            """,
            (
                item.title,
                item.description,
                item.published_at,
                item.declared_duration_seconds,
                item.enclosure_url,
                item.enclosure_type,
                item.artwork_url,
                item.enclosure_length,
                item.feed_position,
                now,
                tx.next_seq(),
                episode.id,
            ),
        )
    elif (episode.feed_position, episode.enclosure_length) != (item.feed_position, item.enclosure_length):
        tx.execute(
            "UPDATE episodes SET feed_position = ?, enclosure_length = ? WHERE id = ?",
            (item.feed_position, item.enclosure_length, episode.id),
        )
    return visible_changed


def clear_dropped_feed_positions(tx: WriteTx, podcast_id: int, present_ids: Iterable[int]) -> int:
    """Items that left the feed keep their row (and transcripts); only their
    position is cleared. Not client-visible, so no seq."""
    cursor = tx.execute(
        """
        UPDATE episodes SET feed_position = NULL
        WHERE podcast_id = ? AND feed_position IS NOT NULL AND id NOT IN (SELECT value FROM json_each(?))
        """,
        (podcast_id, _ids(present_ids)),
    )
    return cursor.rowcount


def set_episode_states(
    tx: WriteTx,
    episode_id: int,
    *,
    now: str,
    pipeline_state: str | None = None,
    transcript_state: str | None = None,
    classify_state: str | None = None,
    audio_state: str | None = None,
    error: str | None | _Keep = KEEP,
    progress: Progress | None | _Keep = KEEP,
) -> None:
    """The one pipeline-transition write: every state change bumps the seq.
    ``None`` leaves a state column alone; ``error``/``progress`` use ``KEEP``
    because clearing them to NULL is meaningful."""
    set_progress = progress is not KEEP
    stage = current = total = None
    if isinstance(progress, Progress):
        stage, current, total = progress.stage, progress.current, progress.total
    tx.execute(
        """
        UPDATE episodes SET
          pipeline_state   = coalesce(?, pipeline_state),
          transcript_state = coalesce(?, transcript_state),
          classify_state   = coalesce(?, classify_state),
          audio_state      = coalesce(?, audio_state),
          pipeline_error   = CASE WHEN ? THEN ? ELSE pipeline_error END,
          progress_stage   = CASE WHEN ? THEN ? ELSE progress_stage END,
          progress_current = CASE WHEN ? THEN ? ELSE progress_current END,
          progress_total   = CASE WHEN ? THEN ? ELSE progress_total END,
          progress_updated_at = CASE WHEN ? THEN ? ELSE progress_updated_at END,
          updated_at = ?, updated_seq = ?
        WHERE id = ?
        """,
        (
            pipeline_state,
            transcript_state,
            classify_state,
            audio_state,
            error is not KEEP,
            None if error is KEEP else error,
            set_progress,
            stage,
            set_progress,
            current,
            set_progress,
            total,
            set_progress,
            now if isinstance(progress, Progress) else None,
            now,
            tx.next_seq(),
            episode_id,
        ),
    )


def set_progress(tx: WriteTx, episode_id: int, *, current: float | None, total: float | None, now: str) -> None:
    """Fine-grained progress for /jobs/active. Deliberately no seq."""
    tx.execute(
        "UPDATE episodes SET progress_current = ?, progress_total = ?, progress_updated_at = ? WHERE id = ?",
        (current, total, now, episode_id),
    )


def begin_download(
    tx: WriteTx,
    episode_id: int,
    *,
    audio_path: str,
    pipeline_state: str | None,
    total_bytes: int | None,
    now: str,
) -> None:
    """A download (re)starts. The target path is recorded up front so boot
    recovery can find the ``.part`` file of an interrupted transfer."""
    tx.execute(
        """
        UPDATE episodes SET audio_state = 'partial', audio_path = ?, pipeline_state = coalesce(?, pipeline_state),
                            pipeline_error = NULL, progress_stage = 'download', progress_current = 0,
                            progress_total = ?, progress_updated_at = ?, updated_at = ?, updated_seq = ?
        WHERE id = ?
        """,
        (audio_path, pipeline_state, total_bytes, now, now, tx.next_seq(), episode_id),
    )


def mark_audio_present(
    tx: WriteTx,
    episode_id: int,
    audio: StoredAudio,
    *,
    measured_duration_seconds: float | None,
    now: str,
    pipeline_state: str | None = None,
    transcript_state: str | None = None,
    classify_state: str | None = None,
) -> None:
    """Record a completed download. Clears eviction and release bookkeeping —
    a release refers to the copy it was sent for, not to this new one."""
    tx.execute(
        """
        UPDATE episodes SET
          audio_state = 'present', audio_path = ?, audio_bytes = ?, audio_sha256 = ?, audio_content_type = ?,
          audio_codec = ?, origin_etag = ?, origin_last_modified = ?, audio_downloaded_at = ?,
          audio_evicted_at = NULL, audio_evicted_reason = NULL, released_at = NULL, release_reason = NULL,
          measured_duration_seconds = ?,
          pipeline_state = coalesce(?, pipeline_state),
          transcript_state = coalesce(?, transcript_state),
          classify_state = coalesce(?, classify_state),
          pipeline_error = NULL,
          progress_stage = NULL, progress_current = NULL, progress_total = NULL, progress_updated_at = NULL,
          updated_at = ?, updated_seq = ?
        WHERE id = ?
        """,
        (
            audio.path,
            audio.bytes,
            audio.sha256,
            audio.content_type,
            audio.codec,
            audio.origin_etag,
            audio.origin_last_modified,
            now,
            measured_duration_seconds,
            pipeline_state,
            transcript_state,
            classify_state,
            now,
            tx.next_seq(),
            episode_id,
        ),
    )


def mark_audio_evicted(tx: WriteTx, episode_id: int, *, reason: str, now: str) -> None:
    """The file is gone from the server, but size and sha256 stay: they are
    what a client's local copy is checked against before applying markers."""
    tx.execute(
        """
        UPDATE episodes SET audio_state = 'evicted', audio_path = NULL, audio_evicted_at = ?,
                            audio_evicted_reason = ?, released_at = NULL, release_reason = NULL,
                            updated_at = ?, updated_seq = ?
        WHERE id = ?
        """,
        (now, reason, now, tx.next_seq(), episode_id),
    )


def set_origin_validators(tx: WriteTx, episode_id: int, *, etag: str | None, last_modified: str | None) -> None:
    """Validators of the bytes in an interrupted download's ``.part`` file:
    the next attempt passes them back to resume with ``If-Range``. Not
    client-visible, so no seq."""
    tx.execute(
        "UPDATE episodes SET origin_etag = ?, origin_last_modified = ? WHERE id = ?",
        (etag, last_modified, episode_id),
    )


def record_release(tx: WriteTx, episode_id: int, *, reason: str, now: str) -> None:
    """Remember a deferred release for the eviction sweep. Not client-visible."""
    tx.execute("UPDATE episodes SET released_at = ?, release_reason = ? WHERE id = ?", (now, reason, episode_id))


def touch_audio_access(tx: WriteTx, episode_id: int, *, now: str) -> None:
    """LRU input for eviction. Never bumps seq: one play is thousands of requests."""
    tx.execute("UPDATE episodes SET audio_last_access_at = ? WHERE id = ?", (now, episode_id))


def set_measured_duration(tx: WriteTx, episode_id: int, seconds: float) -> None:
    """Callers bump the seq through the accompanying state transition."""
    tx.execute("UPDATE episodes SET measured_duration_seconds = ? WHERE id = ?", (seconds, episode_id))


def episode_has_live_media_job(db: Reader, episode_id: int) -> bool:
    row = db.read_one(
        """
        SELECT EXISTS (SELECT 1 FROM jobs WHERE subject_id = ? AND kind IN ('download', 'transcribe', 'classify')
                       AND state IN ('pending', 'running')) AS live
        """,
        (episode_id,),
    )
    return bool(row and row["live"])


def released_episodes_awaiting_eviction(db: Reader) -> list[Episode]:
    rows = db.read(
        """
        SELECT e.* FROM episodes e
        WHERE e.released_at IS NOT NULL AND e.audio_state = 'present'
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.subject_id = e.id
                          AND j.kind IN ('download', 'transcribe', 'classify') AND j.state IN ('pending', 'running'))
        ORDER BY e.released_at
        """
    )
    return [_load(Episode, row) for row in rows]


def eviction_candidates(db: Reader) -> list[Episode]:
    """Stored audio with no live pipeline job, least recently useful first
    (last stream, else download time)."""
    rows = db.read(
        """
        SELECT e.* FROM episodes e
        WHERE e.audio_state = 'present'
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.subject_id = e.id
                          AND j.kind IN ('download', 'transcribe', 'classify') AND j.state IN ('pending', 'running'))
        ORDER BY coalesce(e.audio_last_access_at, e.audio_downloaded_at, e.created_at), e.id
        """
    )
    return [_load(Episode, row) for row in rows]


def stored_audio_bytes(db: Reader) -> int:
    row = db.read_one("SELECT coalesce(sum(audio_bytes), 0) AS total FROM episodes WHERE audio_state = 'present'")
    return 0 if row is None else int(row["total"])


def audio_paths_for_podcast(db: Reader, podcast_id: int) -> list[tuple[int, str]]:
    rows = db.read(
        "SELECT id, audio_path FROM episodes WHERE podcast_id = ? AND audio_path IS NOT NULL", (podcast_id,)
    )
    return [(row["id"], row["audio_path"]) for row in rows]


def episode_ids_for_podcast(db: Reader, podcast_id: int) -> list[int]:
    return [row["id"] for row in db.read("SELECT id FROM episodes WHERE podcast_id = ?", (podcast_id,))]


# -- markers --------------------------------------------------------------------


def markers_for_episode(db: Reader, episode_id: int) -> list[Marker]:
    rows = db.read(
        "SELECT * FROM ad_markers WHERE episode_id = ? AND deleted = 0 ORDER BY start_seconds, id", (episode_id,)
    )
    return [_load(Marker, row) for row in rows]


def markers_for_episodes(db: Reader, episode_ids: Iterable[int]) -> dict[int, list[Marker]]:
    """Live markers grouped by episode, each list sorted by start."""
    grouped: dict[int, list[Marker]] = {}
    rows = db.read(
        """
        SELECT * FROM ad_markers WHERE deleted = 0 AND episode_id IN (SELECT value FROM json_each(?))
        ORDER BY episode_id, start_seconds, id
        """,
        (_ids(episode_ids),),
    )
    for row in rows:
        marker = _load(Marker, row)
        grouped.setdefault(marker.episode_id, []).append(marker)
    return grouped


def _bump_marker_revision(tx: WriteTx, episode_id: int, now: str) -> None:
    tx.execute(
        """
        UPDATE episodes SET
          active_marker_count = (SELECT count(*) FROM ad_markers WHERE episode_id = ? AND deleted = 0),
          marker_revision = marker_revision + 1, updated_at = ?, updated_seq = ?
        WHERE id = ?
        """,
        (episode_id, now, tx.next_seq(), episode_id),
    )


def replace_auto_markers(
    tx: WriteTx, episode_id: int, markers: Sequence[NewMarker], *, classification_id: int | None, now: str
) -> None:
    """Swap the automatic markers for a new set; manual markers are untouched."""
    tx.execute("DELETE FROM ad_markers WHERE episode_id = ? AND source = 'auto'", (episode_id,))
    tx.executemany(
        """
        INSERT INTO ad_markers (episode_id, start_seconds, end_seconds, kind, summary, source,
                                classification_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'auto', ?, ?, ?)
        """,
        [
            (episode_id, m.start_seconds, m.end_seconds, m.kind, m.summary, classification_id, now, now)
            for m in markers
        ],
    )
    _bump_marker_revision(tx, episode_id, now)


# -- transcripts ----------------------------------------------------------------


def replace_transcript(
    tx: WriteTx,
    episode_id: int,
    transcript: NewTranscript,
    sentences: Sequence[Sentence],
    *,
    words_codec: str,
    words_blob: bytes,
    segments_json: str,
    now: str,
) -> None:
    """One transcript per episode: a re-transcription replaces all three tables."""
    tx.execute("DELETE FROM transcript_sentences WHERE episode_id = ?", (episode_id,))
    tx.execute(
        """
        INSERT OR REPLACE INTO transcripts (
          episode_id, engine, model_id, model_sha256, language, language_probability, audio_sha256,
          audio_duration_seconds, speech_duration_seconds, asr_segment_count, word_count, sentence_count,
          joiner_version, joiner_params_json, asr_options_json, decode_seconds, transcribe_seconds, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            episode_id,
            transcript.engine,
            transcript.model_id,
            transcript.model_sha256,
            transcript.language,
            transcript.language_probability,
            transcript.audio_sha256,
            transcript.audio_duration_seconds,
            transcript.speech_duration_seconds,
            transcript.asr_segment_count,
            transcript.word_count,
            transcript.sentence_count,
            transcript.joiner_version,
            transcript.joiner_params_json,
            transcript.asr_options_json,
            transcript.decode_seconds,
            transcript.transcribe_seconds,
            now,
        ),
    )
    tx.executemany(
        """
        INSERT INTO transcript_sentences (episode_id, idx, start_seconds, end_seconds, text, word_start,
                                          word_count, break_reason, soft_end, min_p, mean_p, flags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                episode_id,
                s.index,
                s.start,
                s.end,
                s.text,
                s.word_start,
                s.word_count,
                s.break_reason,
                int(s.soft_end),
                s.min_p,
                s.mean_p,
                ",".join(s.flags),
            )
            for s in sentences
        ],
    )
    tx.execute(
        """
        INSERT OR REPLACE INTO transcript_words (episode_id, codec, word_count, blob, segments_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (episode_id, words_codec, transcript.word_count, words_blob, segments_json),
    )


def get_transcript(db: Reader, episode_id: int) -> Transcript | None:
    row = db.read_one("SELECT * FROM transcripts WHERE episode_id = ?", (episode_id,))
    return None if row is None else _load(Transcript, row)


def get_sentences(db: Reader, episode_id: int) -> list[Sentence]:
    rows = db.read("SELECT * FROM transcript_sentences WHERE episode_id = ? ORDER BY idx", (episode_id,))
    return [
        Sentence(
            index=row["idx"],
            start=row["start_seconds"],
            end=row["end_seconds"],
            text=row["text"],
            word_start=row["word_start"],
            word_count=row["word_count"],
            break_reason=row["break_reason"],
            soft_end=bool(row["soft_end"]),
            min_p=row["min_p"],
            mean_p=row["mean_p"],
            flags=tuple(flag for flag in row["flags"].split(",") if flag),
        )
        for row in rows
    ]


def get_transcript_words(db: Reader, episode_id: int) -> TranscriptWords | None:
    row = db.read_one("SELECT * FROM transcript_words WHERE episode_id = ?", (episode_id,))
    return None if row is None else _load(TranscriptWords, row)


# -- classifications ------------------------------------------------------------


def insert_classification(
    tx: WriteTx, episode_id: int, record: NewClassification, *, raw_response_dir: str, now: str
) -> Classification:
    """Insert an (inactive) classification. Its raw response lives at
    ``<raw_response_dir>/<episode_id>/<id>.json.gz``, a path that needs the id,
    so it is filled in by a second statement in the same transaction."""
    row = tx.read_one(
        """
        INSERT INTO classifications (
          episode_id, provider, model, thinking, prompt_version, render_format, include_silence, joiner_version,
          transcript_audio_sha256, chunk_count, input_tokens, thought_tokens, output_tokens, cached_input_tokens,
          cache_write_tokens, input_cost_usd, thought_cost_usd, output_cost_usd, total_cost_usd,
          price_table_version, latency_ms, attempts, request_sha256, raw_segments_json, segments_json,
          is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        RETURNING id
        """,
        (
            episode_id,
            record.provider,
            record.model,
            record.thinking,
            record.prompt_version,
            record.render_format,
            int(record.include_silence),
            record.joiner_version,
            record.transcript_audio_sha256,
            record.chunk_count,
            record.input_tokens,
            record.thought_tokens,
            record.output_tokens,
            record.cached_input_tokens,
            record.cache_write_tokens,
            record.input_cost_usd,
            record.thought_cost_usd,
            record.output_cost_usd,
            record.total_cost_usd,
            record.price_table_version,
            record.latency_ms,
            record.attempts,
            record.request_sha256,
            record.raw_segments_json,
            record.segments_json,
            now,
        ),
    )
    assert row is not None
    classification_id = row["id"]
    path = f"{raw_response_dir}/{episode_id}/{classification_id}.json.gz"
    updated = tx.read_one(
        "UPDATE classifications SET raw_response_path = ? WHERE id = ? RETURNING *", (path, classification_id)
    )
    assert updated is not None
    return _load(Classification, updated)


def activate_classification(tx: WriteTx, episode_id: int, classification_id: int | None) -> None:
    """Exactly one (or, with None, no) classification per episode is active."""
    tx.execute(
        "UPDATE classifications SET is_active = (id IS ?) WHERE episode_id = ? AND is_active != (id IS ?)",
        (classification_id, episode_id, classification_id),
    )


def list_classifications(db: Reader, episode_id: int) -> list[Classification]:
    rows = db.read(
        "SELECT * FROM classifications WHERE episode_id = ? ORDER BY created_at DESC, id DESC", (episode_id,)
    )
    return [_load(Classification, row) for row in rows]


# -- server settings ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """Client-visible global switches: stored overrides over config defaults."""

    ad_analysis_enabled: bool
    auto_process_enabled: bool
    classifier: str
    classifier_model: str


SETTING_KEYS = ("ad_analysis_enabled", "auto_process_enabled", "classifier", "classifier_model")


def default_model_for(config: Settings, provider: str) -> str:
    if provider == "claude":
        return config.claude_model
    if provider == "fake":
        return "fake"
    return config.gemini_model  # gemini and the gemini-audio control arm


def load_server_settings(db: Reader, config: Settings) -> ServerSettings:
    stored = {row["key"]: json.loads(row["value_json"]) for row in db.read("SELECT key, value_json FROM settings")}
    classifier = stored.get("classifier", config.classifier)
    return ServerSettings(
        ad_analysis_enabled=bool(stored.get("ad_analysis_enabled", True)),
        auto_process_enabled=bool(stored.get("auto_process_enabled", True)),
        classifier=classifier,
        classifier_model=stored.get("classifier_model") or default_model_for(config, classifier),
    )


def update_server_settings(
    tx: WriteTx, config: Settings, changes: Mapping[str, Any], *, now: str
) -> ServerSettings:
    """Upsert changed keys, one seq per changed row. Switching the classifier
    without naming a model resets the model to that provider's default, so a
    Claude arm is never asked to run a Gemini model id."""
    unknown = set(changes) - set(SETTING_KEYS)
    if unknown:
        raise ValueError(f"unknown settings: {sorted(unknown)}")
    current = load_server_settings(tx, config)
    target = dict(changes)
    if "classifier" in target and "classifier_model" not in target and target["classifier"] != current.classifier:
        target["classifier_model"] = default_model_for(config, target["classifier"])
    for key in SETTING_KEYS:
        if key in target and target[key] != getattr(current, key):
            tx.execute(
                """
                INSERT INTO settings (key, value_json, updated_at, updated_seq) VALUES (?, ?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json,
                  updated_at = excluded.updated_at, updated_seq = excluded.updated_seq
                """,
                (key, json.dumps(target[key]), now, tx.next_seq()),
            )
    return load_server_settings(tx, config)


# -- tombstones and the sync page -----------------------------------------------


def tombstone_floor(db: Reader) -> int:
    row = db.read_one("SELECT tombstone_floor_seq FROM sync_state WHERE id = 1")
    return 0 if row is None else int(row["tombstone_floor_seq"])


def prune_tombstones(tx: WriteTx, *, deleted_before: str) -> int:
    """Drop tombstones past retention and raise the cursor floor to the newest
    pruned seq, so a client that could have missed one gets 410 instead."""
    row = tx.read_one(
        "SELECT count(*) AS n, max(updated_seq) AS top FROM tombstones WHERE deleted_at < ?", (deleted_before,)
    )
    if row is None or not row["n"]:
        return 0
    tx.execute("DELETE FROM tombstones WHERE deleted_at < ?", (deleted_before,))
    tx.execute(
        "UPDATE sync_state SET tombstone_floor_seq = max(tombstone_floor_seq, ?) WHERE id = 1", (row["top"],)
    )
    return int(row["n"])


class CursorExpired(Exception):
    """``since`` is below the tombstone floor, or ahead of this database (a
    restored backup): the client must resync from 0."""


@dataclass(frozen=True)
class SyncPage:
    podcasts: list[Podcast]  # rows in range plus referential closure, by seq
    episodes: list[Episode]
    markers: dict[int, list[Marker]]  # episode id -> live markers
    deletions: list[Tombstone]
    settings_changed: bool  # a settings row is in range (always true for since=0)
    next_since: int
    has_more: bool


_CHANGED_SEQS = """
    SELECT updated_seq FROM podcasts WHERE updated_seq > :since
    UNION ALL SELECT updated_seq FROM episodes WHERE updated_seq > :since
    UNION ALL SELECT updated_seq FROM tombstones WHERE updated_seq > :since
    UNION ALL SELECT updated_seq FROM settings WHERE updated_seq > :since
"""
_NTH_CHANGED_SEQ = "SELECT updated_seq FROM (" + _CHANGED_SEQS + ") ORDER BY updated_seq LIMIT 1 OFFSET :offset"
_MAX_SEQ = """
    SELECT max(top) AS top FROM (
      SELECT max(updated_seq) AS top FROM podcasts
      UNION ALL SELECT max(updated_seq) FROM episodes
      UNION ALL SELECT max(updated_seq) FROM tombstones
      UNION ALL SELECT max(updated_seq) FROM settings)
"""


def sync_page(db: Reader, *, since: int, limit: int) -> SyncPage:
    """One page of the delta feed, exactly as specified in docs/API.md.

    Rows share no seq, so cutting at the ``limit``-th smallest seq above
    ``since`` returns exactly ``limit`` rows (plus closure podcasts) and the
    next page resumes strictly after them. Rows rewritten while a client pages
    move to a higher seq and reappear on a later page; none are skipped.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    state = db.read_one("SELECT seq, tombstone_floor_seq FROM sync_state WHERE id = 1")
    assert state is not None
    if since < 0 or since > state["seq"] or (since > 0 and since < state["tombstone_floor_seq"]):
        raise CursorExpired(f"cursor {since} is outside [{state['tombstone_floor_seq']}, {state['seq']}]")

    top_row = db.read_one(_MAX_SEQ)
    top = top_row["top"] if top_row is not None else None
    if top is None or top <= since:
        return SyncPage([], [], {}, [], since == 0, since, False)
    nth = db.read_one(_NTH_CHANGED_SEQ, {"since": since, "offset": limit - 1})
    cutoff = top if nth is None else nth["updated_seq"]
    bounds = (since, cutoff)

    podcasts = [
        _load(Podcast, row)
        for row in db.read(
            "SELECT * FROM podcasts WHERE updated_seq > ? AND updated_seq <= ? ORDER BY updated_seq", bounds
        )
    ]
    episodes = [
        _load(Episode, row)
        for row in db.read(
            "SELECT * FROM episodes WHERE updated_seq > ? AND updated_seq <= ? ORDER BY updated_seq", bounds
        )
    ]
    deletions = [
        _load(Tombstone, row)
        for row in db.read(
            "SELECT * FROM tombstones WHERE updated_seq > ? AND updated_seq <= ? ORDER BY updated_seq", bounds
        )
    ]
    settings_row = db.read_one(
        "SELECT EXISTS (SELECT 1 FROM settings WHERE updated_seq > ? AND updated_seq <= ?) AS changed", bounds
    )
    have = {p.id for p in podcasts}
    missing = {e.podcast_id for e in episodes} - have
    if missing:
        podcasts = podcasts + podcasts_by_ids(db, missing)
    return SyncPage(
        podcasts=podcasts,
        episodes=episodes,
        markers=markers_for_episodes(db, (e.id for e in episodes)),
        deletions=deletions,
        settings_changed=since == 0 or bool(settings_row and settings_row["changed"]),
        next_since=cutoff,
        has_more=top > cutoff,
    )


# -- reporting ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UsageRow:
    key: str  # a UTC date (YYYY-MM-DD) or "provider/model"
    provider: str | None
    model: str | None
    calls: int
    input_tokens: int
    thought_tokens: int
    output_tokens: int
    cost_usd: float


def _usage(rows: list[sqlite3.Row]) -> list[UsageRow]:
    return [
        UsageRow(
            key=row["key"],
            provider=row["provider"],
            model=row["model"],
            calls=row["calls"],
            input_tokens=row["input_tokens"],
            thought_tokens=row["thought_tokens"],
            output_tokens=row["output_tokens"],
            cost_usd=row["cost_usd"],
        )
        for row in rows
    ]


def usage_by_day(db: Reader, *, created_since: str) -> list[UsageRow]:
    rows = db.read(
        """
        SELECT substr(created_at, 1, 10) AS key, NULL AS provider, NULL AS model, count(*) AS calls,
               sum(input_tokens) AS input_tokens, sum(thought_tokens) AS thought_tokens,
               sum(output_tokens) AS output_tokens, sum(total_cost_usd) AS cost_usd
        FROM classifications WHERE created_at >= ? GROUP BY key ORDER BY key
        """,
        (created_since,),
    )
    return _usage(rows)


def usage_by_model(db: Reader, *, created_since: str) -> list[UsageRow]:
    rows = db.read(
        """
        SELECT provider || '/' || model AS key, provider, model, count(*) AS calls,
               sum(input_tokens) AS input_tokens, sum(thought_tokens) AS thought_tokens,
               sum(output_tokens) AS output_tokens, sum(total_cost_usd) AS cost_usd
        FROM classifications WHERE created_at >= ? GROUP BY provider, model ORDER BY cost_usd DESC, key
        """,
        (created_since,),
    )
    return _usage(rows)


def episode_state_counts(db: Reader) -> dict[str, int]:
    rows = db.read("SELECT pipeline_state AS state, count(*) AS n FROM episodes GROUP BY pipeline_state")
    return {row["state"]: row["n"] for row in rows}


def audio_state_counts(db: Reader) -> dict[str, int]:
    rows = db.read("SELECT audio_state AS state, count(*) AS n FROM episodes GROUP BY audio_state")
    return {row["state"]: row["n"] for row in rows}


@dataclass(frozen=True, slots=True)
class GuidCollision:
    guid: str
    podcast_ids: tuple[int, ...]


def guid_collisions(db: Reader, *, limit: int = 50) -> list[GuidCollision]:
    """GUIDs shared across feeds — legal (uniqueness is per feed) but worth seeing."""
    rows = db.read(
        """
        SELECT guid, json_group_array(podcast_id) AS podcasts FROM episodes
        GROUP BY guid HAVING count(DISTINCT podcast_id) > 1 ORDER BY count(*) DESC, guid LIMIT ?
        """,
        (limit,),
    )
    return [GuidCollision(row["guid"], tuple(sorted(set(json.loads(row["podcasts"]))))) for row in rows]
