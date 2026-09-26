"""The request flow every transcript classifier shares.

``TranscriptClassifier.classify`` renders the transcript, derives the request
identity, applies the chunking guard, runs each provider call under the
shared retry policy, parses the answer against the prompt version's schema,
and resolves cited line indices to seconds. Providers only implement
``_call``: one request, returning parsed (unresolved) segments or raising
``AttemptError``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import time
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Awaitable, Callable, Sequence

from ..transcribe.protocol import Sentence, SilenceRegion
from .base import ClassifierError, ClassifyRequest, ClassifyResult, DetectedSegment, TokenUsage
from .chunking import ChunkPlan, chunk_silences, estimate_tokens, plan_chunks, stitch_chunks
from .prompts import CHUNK_NOTES, REPAIR_NUDGE, PromptSpec, episode_duration_guidance, get_prompt
from .render import RenderedTranscript, coalesce_sentences, render_transcript
from .retry import ClassifyFailed, Jitter, RetryPolicy, Sleep, run_with_retry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallResult:
    segments: list[DetectedSegment]  # echoed seconds and cited lines, unresolved
    usage: TokenUsage
    exchange: dict[str, Any]  # raw record; see fake.py for the envelope format


CallFn = Callable[[str, bool], Awaitable[CallResult]]  # (user_text, repair) -> result


def request_sha256(
    *,
    provider: str,
    model: str,
    thinking: str | None,
    prompt_version: str,
    render_format: str,
    include_silence: bool,
    text: str,
) -> str:
    """Identity of a classification request. ``text`` is the user message as
    sent (duration guidance plus the rendered transcript): the prompt version
    pins the system prompt, so this covers everything the model sees."""
    payload = json.dumps(
        [provider, model, thinking, prompt_version, render_format, include_silence, text],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def with_repair_nudge(user_text: str, repair: bool) -> str:
    return f"{user_text}\n\n{REPAIR_NUDGE}" if repair else user_text


def resolve_lines(
    segments: Sequence[DetectedSegment], line_times: dict[int, tuple[float, float]]
) -> tuple[list[DetectedSegment], list[tuple[float | None, float | None]], int]:
    """Replace echoed seconds with the bounds of the cited lines (start of
    ``start_line``, end of ``end_line``). Returns the segments, the per-segment
    ``|resolved - echoed|`` deltas, and how many citations named no line."""
    resolved: list[DetectedSegment] = []
    deltas: list[tuple[float | None, float | None]] = []
    unresolved = 0
    for segment in segments:
        start, end = segment.start_seconds, segment.end_seconds
        start_delta = end_delta = None
        if segment.start_line is not None:
            span = line_times.get(segment.start_line)
            if span is None:
                unresolved += 1
            else:
                start_delta = _delta(span[0], start)
                start = span[0]
        if segment.end_line is not None:
            span = line_times.get(segment.end_line)
            if span is None:
                unresolved += 1
            else:
                end_delta = _delta(span[1], end)
                end = span[1]
        resolved.append(replace(segment, start_seconds=start, end_seconds=end))
        if segment.start_line is not None or segment.end_line is not None:
            deltas.append((start_delta, end_delta))
    return resolved, deltas, unresolved


def _delta(resolved: float, echoed: float) -> float | None:
    return round(abs(resolved - echoed), 3) if math.isfinite(echoed) else None


def line_resolution_metrics(deltas: Sequence[tuple[float | None, float | None]], unresolved: int) -> dict[str, Any]:
    values = [d for pair in deltas for d in pair if d is not None]
    return {
        "cited_segments": len(deltas),
        "unresolved_citations": unresolved,
        "max_abs_delta_s": max(values, default=None),
        "mean_abs_delta_s": round(sum(values) / len(values), 3) if values else None,
        "deltas": [list(pair) for pair in deltas],
    }


@dataclass(frozen=True)
class PreparedRequest:
    req: ClassifyRequest
    rendered: RenderedTranscript
    user_text: str
    request_sha256: str


@dataclass(frozen=True)
class _Job:
    plan: ChunkPlan | None
    rendered: RenderedTranscript
    user_text: str


class TranscriptClassifier:
    """Base for classifiers that send transcript text (Gemini, Claude, fake)."""

    provider: str = ""

    def __init__(
        self,
        *,
        model: str,
        thinking: str | None = None,
        prompt_version: str = "segments-v3",
        render_format: str = "sentences",
        include_silence: bool = False,
        max_input_tokens: int = 120_000,
        retry_policy: RetryPolicy = RetryPolicy(),
        sleep: Sleep = asyncio.sleep,
        rand: Jitter = random.uniform,
    ) -> None:
        self.model = model
        self.thinking = thinking
        self.prompt: PromptSpec = get_prompt(prompt_version, render_format)
        if self.prompt.render_format == "audio":
            raise ClassifierError("the audio prompt needs the gemini-audio classifier", permanent=True)
        if self.prompt.render_format == "sentences" and include_silence:
            raise ClassifierError("the sentences format cannot include silence rows", permanent=True)
        self.include_silence = include_silence
        self.max_input_tokens = max_input_tokens
        self.retry_policy = retry_policy
        self._sleep = sleep
        self._rand = rand

    @property
    def request_identity(self) -> tuple[str, str]:
        """(provider, model) hashed into ``request_sha256``."""
        return self.provider, self.model

    async def classify(self, req: ClassifyRequest) -> ClassifyResult:
        return await self.execute(self.prepare(req), self._call)

    async def _call(self, user_text: str, repair: bool) -> CallResult:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None

    def prepare(self, req: ClassifyRequest) -> PreparedRequest:
        if not req.sentences:
            raise ClassifierError("transcript has no sentences to classify", permanent=True)
        if self.prompt.render_format == "sentences":
            # Use the same complete-sentence units for rendering and chunk
            # boundaries; otherwise a chunk seam could cut a joined line.
            req = replace(req, sentences=coalesce_sentences(req.sentences))
        rendered = self._render(req.sentences, req.silences, req.episode_duration)
        guidance = episode_duration_guidance(req.episode_duration, req.sentences)
        user_text = self.prompt.user_message(rendered.text, guidance)
        provider, model = self.request_identity
        sha = request_sha256(
            provider=provider,
            model=model,
            thinking=self.thinking,
            prompt_version=self.prompt.version,
            render_format=self.prompt.render_format,
            include_silence=self.include_silence,
            text=user_text,
        )
        return PreparedRequest(req, rendered, user_text, sha)

    def _render(
        self, sentences: Sequence[Sentence], silences: Sequence[SilenceRegion], duration: float | None
    ) -> RenderedTranscript:
        return render_transcript(
            sentences, silences, fmt=self.prompt.render_format, include_silence=self.include_silence, episode_duration=duration
        )

    def _jobs(self, prepared: PreparedRequest) -> list[_Job]:
        size = estimate_tokens(self.prompt.system) + estimate_tokens(prepared.user_text)
        if size <= self.max_input_tokens:
            return [_Job(None, prepared.rendered, prepared.user_text)]
        req = prepared.req
        plans = plan_chunks(req.sentences)
        log.warning(
            "transcript of ~%d tokens exceeds %d; classifying in %d chunks",
            size,
            self.max_input_tokens,
            len(plans),
        )
        jobs = []
        for plan in plans:
            sentences = list(req.sentences[plan.first : plan.last + 1])
            duration = req.episode_duration if plan.allows_outro else None
            rendered = self._render(sentences, chunk_silences(plan, req.silences), duration)
            # The endpoint only concerns the chunk that can hold the outro.
            guidance = episode_duration_guidance(req.episode_duration, req.sentences) if plan.allows_outro else ""
            note = CHUNK_NOTES[plan.role].format(part=plan.index + 1, total=plan.count)
            jobs.append(_Job(plan, rendered, self.prompt.user_message(rendered.text, guidance, note)))
        return jobs

    async def execute(self, prepared: PreparedRequest, call: CallFn) -> ClassifyResult:
        started = time.monotonic()
        jobs = self._jobs(prepared)
        usage = TokenUsage()
        attempts = 0
        exchanges: list[dict[str, Any]] = []
        retries: list[dict[str, Any]] = []
        deltas: list[tuple[float | None, float | None]] = []
        unresolved = 0
        parts: list[tuple[ChunkPlan | None, list[DetectedSegment]]] = []
        for job in jobs:
            chunk = None if job.plan is None else job.plan.index
            try:
                outcome = await run_with_retry(
                    partial(call, job.user_text), policy=self.retry_policy, sleep=self._sleep, rand=self._rand
                )
            except ClassifyFailed as exc:
                raise ClassifyFailed(
                    str(exc),
                    permanent=exc.permanent,
                    retry_after=exc.retry_after,
                    attempts=attempts + exc.attempts,
                    usage=usage + exc.usage,
                    log=retries + exc.log,
                ) from exc
            attempts += outcome.attempts
            usage = usage + outcome.failed_usage + outcome.value.usage
            final = {"attempt": outcome.attempts, **outcome.value.exchange}
            for record in [*outcome.exchanges, final]:
                exchanges.append(record if chunk is None else {"chunk": chunk, **record})
            retries.extend(entry if chunk is None else {"chunk": chunk, **entry} for entry in outcome.log)
            segments, chunk_deltas, chunk_unresolved = resolve_lines(outcome.value.segments, job.rendered.line_times)
            deltas += chunk_deltas
            unresolved += chunk_unresolved
            parts.append((job.plan, segments))

        if jobs[0].plan is None:
            segments = parts[0][1]
        else:
            segments = stitch_chunks([(plan, segs) for plan, segs in parts if plan is not None])
        raw: dict[str, Any] = {"exchanges": exchanges}
        if retries:
            raw["retries"] = retries
        if self.prompt.cites_lines:
            raw["line_resolution"] = line_resolution_metrics(deltas, unresolved)
        if jobs[0].plan is not None:
            raw["chunks"] = [
                {"index": p.index, "role": p.role, "first_sentence": p.first, "last_sentence": p.last}
                for p, _ in parts
                if p is not None
            ]
        result = ClassifyResult(
            segments=segments,
            usage=usage,
            provider=self.provider,
            model=self.model,
            thinking=self.thinking,
            prompt_version=self.prompt.version,
            render_format=self.prompt.render_format,
            include_silence=self.include_silence,
            chunk_count=len(jobs),
            latency_ms=round((time.monotonic() - started) * 1000),
            attempts=attempts,
            request_sha256=prepared.request_sha256,
            raw_response=raw,
        )
        log.info(
            "classified with %s/%s: %d segments, %d attempts, %d in / %d thought / %d out tokens, %d ms",
            self.provider,
            self.model,
            len(segments),
            attempts,
            usage.input_tokens,
            usage.thought_tokens,
            usage.output_tokens,
            result.latency_ms,
        )
        return result
