"""Replaying classifier (provider ``fake``) for tests and the offline e2e.

It runs the same flow as the real transcript classifiers (rendering, request
hashing, the chunking guard, retry, parsing, line resolution), but answers
from recorded provider responses instead of the network. Replies go through
the real providers' parsers, so fenced JSON, schema violations (one repair
attempt, then permanent) and every retry path behave as in production.

Lookup, per request:

1. a fixture whose ``request_sha256`` equals the request's, computed by
   ``core.request_sha256`` with provider ``fake`` and this classifier's model
   (``FakeClassifier.prepare(req).request_sha256`` gives it for a request);
2. a fixture whose title slug equals ``slugify(req.episode_title)``;
3. otherwise a deterministic synthetic answer: an intro over 0-30 s when the
   first sentence starts within 60 s, and an outro from the start of the
   last sentence (the one before the tail silence) to the end of the audio.

Fixture format: one JSON object per ``*.json`` file, anywhere under the
fixtures directory::

    {
      "episode_title": "449: Middle School",  # optional title key; default: the file stem, as a slug
      "request_sha256": "…",                  # optional exact-request key
      "description": "…",                     # optional, ignored
      "responses": [envelope, …]              # one per provider call, in order; the last repeats
    }

An envelope is one provider-shaped HTTP exchange, the same record shape as
``ClassifyResult.raw_response["exchanges"]``, so a live result becomes a
fixture by copying its exchanges::

    {"format": "gemini",               # "gemini" (generateContent JSON) or "claude" (Messages API JSON); default gemini
     "status": 200,                    # default 200; 408/409/429/5xx are retried, other errors are permanent
     "headers": {"Retry-After": "0"},  # optional; retry hints are honoured exactly
     "body": {...}}                    # the response JSON (or error JSON)
    {"error": "timeout"}               # or "connection": a transport failure, retried

Each fixture keeps one cursor for the classifier's lifetime, across
``classify()`` calls: ``[500, 500, 500, 500, 200]`` fails one call after its
four in-call attempts and succeeds on the next, which is how job-level
retries get exercised. ``slugify("894: I Couldn't Help but Notice")`` is
``894-i-couldnt-help-but-notice``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import claude, gemini
from .base import ClassifyRequest, ClassifyResult, DetectedSegment, TokenUsage
from .core import CallResult, PreparedRequest, TranscriptClassifier
from .prompts import PromptSpec, valid_episode_endpoint
from .retry import AttemptError

SYNTHETIC_INTRO_END_S = 30.0
SYNTHETIC_INTRO_IF_SPEECH_BY_S = 60.0
_FORMATS = ("gemini", "claude")
_TRANSPORT_ERRORS = ("timeout", "connection")


def slugify(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_title.lower().replace("'", "")).strip("-")


@dataclass
class Fixture:
    path: Path
    slug: str
    request_sha256: str | None
    responses: list[dict[str, Any]]
    cursor: int = 0

    def next_response(self) -> dict[str, Any]:
        response = self.responses[min(self.cursor, len(self.responses) - 1)]
        self.cursor += 1
        return response


def load_fixture(path: Path) -> Fixture:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    responses = payload.get("responses") if isinstance(payload, dict) else None
    if not isinstance(responses, list) or not responses:
        raise ValueError(f"{path}: a fixture needs a non-empty 'responses' list")
    for number, envelope in enumerate(responses):
        if not isinstance(envelope, dict):
            raise ValueError(f"{path}: response {number} is not an object")
        if "error" in envelope:
            if envelope["error"] not in _TRANSPORT_ERRORS:
                raise ValueError(f"{path}: response {number}: error must be one of {_TRANSPORT_ERRORS}")
        elif envelope.get("format", "gemini") not in _FORMATS or "body" not in envelope:
            raise ValueError(f"{path}: response {number} needs a 'body' and a format in {_FORMATS}")
    title = payload.get("episode_title")
    return Fixture(
        path=path,
        slug=slugify(title) if title else path.stem,
        request_sha256=payload.get("request_sha256"),
        responses=responses,
    )


def load_fixtures(directory: Path) -> tuple[dict[str, Fixture], dict[str, Fixture]]:
    """(by request_sha256, by slug); duplicate keys are an error, not a coin toss."""
    if not directory.is_dir():
        # A mistyped path must not silently turn every answer synthetic.
        raise ValueError(f"fixtures directory {directory} does not exist")
    by_sha: dict[str, Fixture] = {}
    by_slug: dict[str, Fixture] = {}
    for path in sorted(directory.rglob("*.json")):
        fixture = load_fixture(path)
        for index, key in ((by_sha, fixture.request_sha256), (by_slug, fixture.slug)):
            if key is None:
                continue
            if key in index:
                raise ValueError(f"fixtures {index[key].path} and {path} share the key {key!r}")
            index[key] = fixture
    return by_sha, by_slug


def replay(envelope: dict[str, Any], spec: PromptSpec) -> CallResult:
    error = envelope.get("error")
    if error is not None:
        raise AttemptError(f"scripted {error}", kind="transient", exchange=dict(envelope))
    status = int(envelope.get("status", 200))
    headers = envelope.get("headers") or {}
    body = envelope["body"]
    if envelope.get("format", "gemini") == "claude":
        return claude.interpret_response(status, headers, body, spec)
    return gemini.interpret_response(status, headers, body, spec)


def synthetic_segments(req: ClassifyRequest) -> list[DetectedSegment]:
    sentences = req.sentences
    transcript_end = max(sentence.end for sentence in sentences)
    segments = []
    if sentences[0].start < SYNTHETIC_INTRO_IF_SPEECH_BY_S:
        segments.append(
            DetectedSegment(0.0, min(SYNTHETIC_INTRO_END_S, transcript_end), "Synthetic intro (fake classifier)", "intro")
        )
    end = valid_episode_endpoint(req.episode_duration, sentences) or transcript_end
    segments.append(DetectedSegment(sentences[-1].start, end, "Synthetic outro (fake classifier)", "outro"))
    return segments


class FakeClassifier(TranscriptClassifier):
    """``options`` are ``TranscriptClassifier``'s; ``fixtures_dir`` may be None
    (every answer synthetic)."""

    provider = "fake"

    def __init__(self, *, fixtures_dir: Path | None = None, model: str = "fake", **options: Any) -> None:
        super().__init__(model=model, **options)
        self._by_sha, self._by_slug = load_fixtures(fixtures_dir) if fixtures_dir is not None else ({}, {})

    def fixture_for(self, prepared: PreparedRequest) -> Fixture | None:
        fixture = self._by_sha.get(prepared.request_sha256)
        if fixture is None and prepared.req.episode_title:
            fixture = self._by_slug.get(slugify(prepared.req.episode_title))
        return fixture

    async def classify(self, req: ClassifyRequest) -> ClassifyResult:
        prepared = self.prepare(req)
        fixture = self.fixture_for(prepared)
        if fixture is None:
            return self._synthetic(prepared)

        async def call(user_text: str, repair: bool) -> CallResult:
            return replay(fixture.next_response(), self.prompt)

        result = await self.execute(prepared, call)
        result.raw_response["fixture"] = str(fixture.path)
        return result

    def _synthetic(self, prepared: PreparedRequest) -> ClassifyResult:
        return ClassifyResult(
            segments=synthetic_segments(prepared.req),
            usage=TokenUsage(),
            provider=self.provider,
            model=self.model,
            thinking=self.thinking,
            prompt_version=self.prompt.version,
            render_format=self.prompt.render_format,
            include_silence=self.include_silence,
            chunk_count=1,
            latency_ms=0,
            attempts=1,
            request_sha256=prepared.request_sha256,
            raw_response={"synthetic": True},
        )
