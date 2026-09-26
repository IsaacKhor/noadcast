"""``GET /api/v1/episodes/{id}/transcript`` as sentences, sentences + words, or plain text."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from starlette.responses import PlainTextResponse, Response

from ...db import repo
from ...pipeline.commands import NotFound
from ...transcribe import codec
from ..deps import Ctx, episode_or_404
from ..schemas import SentenceOut, TranscriptOut, TranscriptWithWordsOut, WordOut, json_response

router = APIRouter()


@router.get("/episodes/{episode_id}/transcript")
async def get_transcript(
    episode_id: int,
    ctx: Ctx,
    fmt: Annotated[Literal["sentences", "words", "text"], Query(alias="format")] = "sentences",
) -> Response:
    episode_or_404(ctx, episode_id)
    transcript = repo.get_transcript(ctx.db, episode_id)
    if transcript is None:
        raise NotFound(f"transcript for episode {episode_id}")
    sentences = repo.get_sentences(ctx.db, episode_id)
    if fmt == "text":
        return PlainTextResponse("\n".join(sentence.text for sentence in sentences))
    fields = dict(
        episode_id=episode_id,
        model_id=transcript.model_id,
        language=transcript.language,
        duration_seconds=transcript.audio_duration_seconds,
        joiner_version=transcript.joiner_version,
        sentences=[
            SentenceOut(index=s.index, start_seconds=s.start, end_seconds=s.end, text=s.text, flags=list(s.flags))
            for s in sentences
        ],
    )
    if fmt == "sentences":
        return json_response(TranscriptOut(**fields))
    stored = repo.get_transcript_words(ctx.db, episode_id)
    if stored is None:
        raise NotFound(f"word timings for episode {episode_id}")
    # ~10k words per episode: decompress and decode off the event loop.
    words = await asyncio.to_thread(codec.decode_words, stored.blob)
    return json_response(
        TranscriptWithWordsOut(
            **fields,
            words=[WordOut(start=w.start, end=w.end, word=w.word, probability=w.probability) for w in words],
        )
    )
