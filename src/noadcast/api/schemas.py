"""Wire DTOs (docs/API.md) and their construction from repository rows.

Fields are snake_case in Python and camelCase on the wire through the alias
generator, so Swift decodes them without ``CodingKeys`` remapping. Request
bodies accept camelCase (or snake_case) and ignore unknown fields.
Timestamps are already stored in the wire format (``timeutil.iso``) and pass
through untouched.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from starlette.responses import Response

from ..db import repo
from ..pipeline import jobs

Provider = Literal["gemini", "claude", "gemini-audio", "fake"]
JobState = Literal["pending", "running", "done", "failed", "canceled"]
# Spelled out for validation; a test pins it to pipeline.states.JOB_KINDS.
JobKind = Literal["refresh_feed", "download", "transcribe", "classify", "evict"]


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


def json_response(
    model: BaseModel, *, status_code: int = 200, headers: dict[str, str] | None = None
) -> Response:
    return Response(
        model.model_dump_json(by_alias=True), status_code=status_code, headers=headers, media_type="application/json"
    )


# -- objects --------------------------------------------------------------------


class PodcastOut(ApiModel):
    id: int
    feed_url: str
    title: str
    author: str | None
    summary: str | None
    artwork_url: str | None
    language: str | None
    link: str | None
    auto_process_enabled: bool
    ad_analysis_enabled: bool
    episode_count: int
    latest_episode_at: str | None
    last_fetch_at: str | None
    last_fetch_error: str | None
    created_at: str
    updated_at: str
    seq: int


class MarkerOut(ApiModel):
    id: int
    start_seconds: float
    end_seconds: float
    kind: str
    summary: str
    source: str


class EpisodeOut(ApiModel):
    id: int
    podcast_id: int
    guid: str
    title: str
    description: str | None
    published_at: str | None
    duration_seconds: float | None
    duration_is_measured: bool
    enclosure_url: str
    enclosure_type: str | None
    artwork_url: str | None
    audio_state: str
    audio_bytes: int | None
    audio_sha256: str | None
    audio_content_type: str | None
    state: str
    error: str | None
    transcript_state: str
    classify_state: str
    marker_revision: int
    ad_markers: list[MarkerOut]
    updated_at: str
    seq: int


class SettingsOut(ApiModel):
    ad_analysis_enabled: bool
    auto_process_enabled: bool
    classifier: str
    classifier_model: str
    available_classifiers: dict[str, bool]


def podcast_out(podcast: repo.Podcast) -> PodcastOut:
    return PodcastOut(
        id=podcast.id,
        feed_url=podcast.feed_url,
        title=podcast.title,
        author=podcast.author,
        summary=podcast.summary,
        artwork_url=podcast.artwork_url,
        language=podcast.language,
        link=podcast.link,
        auto_process_enabled=podcast.auto_process_enabled,
        ad_analysis_enabled=podcast.ad_analysis_enabled,
        episode_count=podcast.episode_count,
        latest_episode_at=podcast.latest_episode_at,
        last_fetch_at=podcast.last_fetch_at,
        last_fetch_error=podcast.last_fetch_error,
        created_at=podcast.created_at,
        updated_at=podcast.updated_at,
        seq=podcast.updated_seq,
    )


def episode_out(episode: repo.Episode, markers: Sequence[repo.Marker]) -> EpisodeOut:
    """``markers`` must be the episode's complete live set, sorted by start (as the repo returns them)."""
    return EpisodeOut(
        id=episode.id,
        podcast_id=episode.podcast_id,
        guid=episode.guid,
        title=episode.title,
        description=episode.description,
        published_at=episode.published_at,
        duration_seconds=episode.duration_seconds,
        duration_is_measured=episode.measured_duration_seconds is not None,
        enclosure_url=episode.enclosure_url,
        enclosure_type=episode.enclosure_type,
        artwork_url=episode.artwork_url,
        audio_state=episode.audio_state,
        audio_bytes=episode.audio_bytes,
        audio_sha256=episode.audio_sha256,
        audio_content_type=episode.audio_content_type,
        state=episode.pipeline_state,
        error=episode.pipeline_error,
        transcript_state=episode.transcript_state,
        classify_state=episode.classify_state,
        marker_revision=episode.marker_revision,
        ad_markers=[
            MarkerOut(
                id=m.id,
                start_seconds=m.start_seconds,
                end_seconds=m.end_seconds,
                kind=m.kind,
                summary=m.summary,
                source=m.source,
            )
            for m in markers
        ],
        updated_at=episode.updated_at,
        seq=episode.updated_seq,
    )


def settings_out(settings: repo.ServerSettings, available: dict[str, bool]) -> SettingsOut:
    return SettingsOut(
        ad_analysis_enabled=settings.ad_analysis_enabled,
        auto_process_enabled=settings.auto_process_enabled,
        classifier=settings.classifier,
        classifier_model=settings.classifier_model,
        available_classifiers=available,
    )


# -- endpoint bodies ------------------------------------------------------------


class HealthOut(ApiModel):
    status: str
    version: str
    api_version: int
    instance_id: str
    auth_required: bool
    capabilities: list[str]


class SessionOut(ApiModel):
    authenticated: bool
    server_time: str


class DeletionOut(ApiModel):
    entity: str
    id: int


class SyncOut(ApiModel):
    instance_id: str
    podcasts: list[PodcastOut]
    episodes: list[EpisodeOut]
    deletions: list[DeletionOut]
    settings: SettingsOut | None
    next_since: int
    has_more: bool
    server_time: str


class ActiveJobOut(ApiModel):
    episode_id: int
    job_id: int
    state: str  # the episode's pipeline state
    stage: str  # the job kind
    job_state: str  # pending | running
    current: float | None
    total: float | None
    status_text: str
    updated_at: str


class ActiveJobsOut(ApiModel):
    items: list[ActiveJobOut]


class PodcastEnvelope(ApiModel):
    podcast: PodcastOut


class AcceptedPodcast(ApiModel):
    podcast: PodcastOut
    job_id: int


class JobIdOut(ApiModel):
    job_id: int | None


class JobIdsOut(ApiModel):
    job_ids: list[int]


class SentenceOut(ApiModel):
    index: int
    start_seconds: float
    end_seconds: float
    text: str
    flags: list[str]


class WordOut(ApiModel):
    start: float
    end: float
    word: str  # raw ASR token with its leading space; join with "" to rebuild text
    probability: float


class TranscriptOut(ApiModel):
    episode_id: int
    model_id: str
    language: str
    duration_seconds: float
    joiner_version: int
    sentences: list[SentenceOut]


class TranscriptWithWordsOut(TranscriptOut):
    words: list[WordOut]


class SegmentOut(ApiModel):
    start_seconds: float
    end_seconds: float
    kind: str
    summary: str


class ClassificationOut(ApiModel):
    id: int
    provider: str
    model: str
    prompt_version: str
    render_format: str
    is_active: bool
    input_tokens: int
    thought_tokens: int
    output_tokens: int
    total_cost_usd: float
    latency_ms: int | None
    created_at: str
    segments: list[SegmentOut]


class ClassificationsOut(ApiModel):
    items: list[ClassificationOut]


def classification_out(record: repo.Classification) -> ClassificationOut:
    # segments_json holds dataclasses.asdict(DetectedSegment) dicts.
    segments: list[dict[str, Any]] = json.loads(record.segments_json)
    return ClassificationOut(
        id=record.id,
        provider=record.provider,
        model=record.model,
        prompt_version=record.prompt_version,
        render_format=record.render_format,
        is_active=record.is_active,
        input_tokens=record.input_tokens,
        thought_tokens=record.thought_tokens,
        output_tokens=record.output_tokens,
        total_cost_usd=record.total_cost_usd,
        latency_ms=record.latency_ms,
        created_at=record.created_at,
        segments=[
            SegmentOut(
                start_seconds=s["start_seconds"], end_seconds=s["end_seconds"], kind=s["kind"], summary=s["summary"]
            )
            for s in segments
        ],
    )


class AudioUrlOut(ApiModel):
    path: str
    url: str
    expires_at: str


class UsageDayOut(ApiModel):
    date: str
    calls: int
    input_tokens: int
    thought_tokens: int
    output_tokens: int
    cost_usd: float


class UsageModelOut(ApiModel):
    provider: str
    model: str
    calls: int
    input_tokens: int
    thought_tokens: int
    output_tokens: int
    cost_usd: float


class UsageTotalsOut(ApiModel):
    calls: int
    input_tokens: int
    thought_tokens: int
    output_tokens: int
    cost_usd: float


class UsageOut(ApiModel):
    days: list[UsageDayOut]
    by_model: list[UsageModelOut]
    totals: UsageTotalsOut


class OpmlFailureOut(ApiModel):
    feed_url: str
    error: str


class OpmlImportOut(ApiModel):
    added: list[PodcastOut]
    existing: list[PodcastOut]
    failed: list[OpmlFailureOut]


class JobOut(ApiModel):
    id: int
    kind: str
    subject_id: int
    state: str
    priority: int
    attempts: int
    max_attempts: int
    available_at: str
    lease_expires_at: str | None
    params: dict[str, Any]
    last_error: str | None
    last_error_at: str | None
    created_at: str
    updated_at: str
    finished_at: str | None


class JobsOut(ApiModel):
    items: list[JobOut]


def job_out(job: jobs.Job) -> JobOut:
    return JobOut(
        id=job.id,
        kind=job.kind,
        subject_id=job.subject_id,
        state=job.state,
        priority=job.priority,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        available_at=job.available_at,
        lease_expires_at=job.lease_expires_at,
        params=job.params,
        last_error=job.last_error,
        last_error_at=job.last_error_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
        finished_at=job.finished_at,
    )


# -- request bodies -------------------------------------------------------------


class SubscribeIn(ApiModel):
    feed_url: str
    auto_process_enabled: bool = True
    ad_analysis_enabled: bool = True
    initial_backfill_count: int | None = Field(default=None, ge=0)


class PodcastPatchIn(ApiModel):
    auto_process_enabled: bool | None = None
    ad_analysis_enabled: bool | None = None


class ReanalyzeIn(ApiModel):
    provider: Provider | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    thinking: str | None = Field(default=None, min_length=1, max_length=64)
    retranscribe: bool = False


class SettingsPatchIn(ApiModel):
    """Absent and null both leave a setting unchanged."""

    ad_analysis_enabled: bool | None = None
    auto_process_enabled: bool | None = None
    classifier: Provider | None = None
    classifier_model: str | None = Field(default=None, min_length=1, max_length=200)
