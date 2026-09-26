"""Persisting a transcription.

The joiner runs here, in the web process, not in the pool worker: it can be
re-tuned and re-run over stored words without touching the pool. Joining and
compressing ~10,000 words is pure CPU work done before the write
transaction; the write then replaces ``transcripts``,
``transcript_sentences`` and ``transcript_words`` together.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass

from ..db import repo
from ..db.engine import WriteTx
from .codec import CODEC, encode_segments, encode_words
from .joiner import JOINER_VERSION, JoinerParams, join_words
from .protocol import Sentence, TranscribeResult


@dataclass(frozen=True)
class PreparedTranscript:
    record: repo.NewTranscript
    sentences: list[Sentence]
    words_blob: bytes
    segments_json: str


def prepare_transcript(
    result: TranscribeResult, *, audio_sha256: str | None, params: JoinerParams = JoinerParams()
) -> PreparedTranscript:
    """Join and encode; pure and thread-safe (run it via ``asyncio.to_thread``)."""
    sentences = join_words(result.words, result.segments, params)
    record = repo.NewTranscript(
        engine=result.engine,
        model_id=result.model_id,
        model_sha256=result.model_sha256,
        language=result.language,
        language_probability=result.language_probability,
        audio_sha256=audio_sha256,
        audio_duration_seconds=result.duration_seconds,
        speech_duration_seconds=result.duration_after_vad,
        asr_segment_count=len(result.segments),
        word_count=len(result.words),
        sentence_count=len(sentences),
        joiner_version=JOINER_VERSION,
        joiner_params_json=json.dumps(dataclasses.asdict(params), sort_keys=True),
        asr_options_json=json.dumps(result.options, sort_keys=True, default=str),
        decode_seconds=result.decode_seconds,
        transcribe_seconds=result.transcribe_seconds,
    )
    return PreparedTranscript(record, sentences, encode_words(result.words), encode_segments(result.segments))


def store_transcript(tx: WriteTx, episode_id: int, prepared: PreparedTranscript, *, now: str) -> None:
    """Write the transcript and adopt its decoded length as the episode's
    authoritative duration (``itunes:duration`` is often minutes off, and the
    outro extension depends on the true endpoint). The caller's state
    transition in the same transaction bumps the episode's seq."""
    repo.replace_transcript(
        tx,
        episode_id,
        prepared.record,
        prepared.sentences,
        words_codec=CODEC,
        words_blob=prepared.words_blob,
        segments_json=prepared.segments_json,
        now=now,
    )
    repo.set_measured_duration(tx, episode_id, prepared.record.audio_duration_seconds)
