"""Prompt versions and their response contracts.

A prompt version pins the system prompt text, the user-message framing, the
response schema (Gemini's uppercase ``responseSchema`` dialect and an
equivalent JSON Schema for Claude's structured outputs), and the parser for
the model's answer. Versions are A/B arms recorded in
``classifications.prompt_version``; never edit a published version's text,
add a new one.

- ``segments-v1``: the recovered server's ``SEGMENTS_ONLY_PROMPT`` and
  ``RESPONSE_SCHEMA`` verbatim (``git show 3e6d713:server/main.py``), for the
  ``seconds`` transcript format.
- ``segments-v2``: v1 plus three rules the older Swift prompt had
  (``git show bc60779^:Noadcast/Services/AdDetectionService.swift``): the
  affirmative intro exclusion, "editorial mentions ... are NOT ads", and the
  adjacent-ad merge rule. Written for the ``index`` format, where the model
  cites line numbers and the server resolves seconds from them. It also runs
  on the ``seconds`` format (same rules, v1 schema) so "rules" and "format"
  can be compared independently.
- ``segments-v3``: production prompt for joined sentence lines with precise
  start/end timestamps and a required content summary for each segment.
- ``audio-v1``: the iOS app's audio prompt verbatim (``segmentsOnlyPrompt``
  in ``git show c1a53ce:ios_app/Noadcast/Services/CloudAdDetectionService.swift``),
  used only by the Gemini audio control arm.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from ..transcribe.protocol import Sentence
from .base import ClassifierError, DetectedSegment

# --------------------------------------------------------------------------
# segments-v1: recovered verbatim.

SEGMENTS_ONLY_PROMPT = """
You are analyzing a timestamped podcast transcript. Return a single JSON
object with one field, `segments`, containing every contiguous portion of the
episode the listener would want to skip.

Each segment has a `kind`:

- "intro": one contiguous segment near the BEGINNING of the episode covering
  theme music, branding, and any preroll ads. At most one per episode. Spans
  from the start of the episode through to where substantive content begins.
  Do NOT include introductory content that may be substantive, like host
  banter, guest introductions, or setup for the main topic.

- "outro": one contiguous segment at the very END of the episode covering
  closing music, credits, next-episode teasers, postroll ads, and farewells.
  At most one per episode. Spans from where substantive content finishes
  through to the end of the audio. Closing material and any postroll ads belong
  in this single outro segment, not in separate segments.

- "ad": a mid-episode advertisement, sponsored message, host-read ad, promo
  code, paid endorsement, or cross-promotion of another podcast that appears
  BETWEEN the intro and outro.

Before finalizing the response, deliberately inspect the final transcript
ranges for a farewell, credits, a next-episode teaser, a postroll ad, or another
transition away from substantive content. Do not omit an outro merely because
the transcript ends before trailing music or silence that has no spoken words.
Return no outro only when there is no evidence that substantive content has
ended. Segment starts must be grounded in transcript timestamps. When the
complete episode duration is supplied, use that audio endpoint as the outro's
`endSeconds`; other segment timestamps must stay within transcript ranges.

Be conservative. Return an empty `segments` array if nothing should be skipped.
Do not include any fields other than `segments`.
""".strip()


RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "segments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "startSeconds": {"type": "NUMBER"},
                    "endSeconds": {"type": "NUMBER"},
                    "summary": {"type": "STRING"},
                    "kind": {"type": "STRING", "enum": ["ad", "intro", "outro"]},
                },
                "required": ["startSeconds", "endSeconds", "summary", "kind"],
            },
        }
    },
    "required": ["segments"],
}

V1_USER_PREAMBLE = (
    "Classify only the following transcript. "
    "Segment starts, intros, and ads must stay within these "
    "transcript ranges."
)

# --------------------------------------------------------------------------
# segments-v2: v1 plus the three restored rules.

_V2_INTRO = """\
- "intro": one contiguous segment near the BEGINNING of the episode covering
  theme music, branding, any preroll ads, and host or show introductions that
  are likely the same in every episode. At most one per episode. Spans from the
  start of the episode through to where substantive content begins. Do NOT
  include host, interviewee, guest, or episode introductions that are specific
  to this episode, host banter, or setup for the main topic."""

_V2_OUTRO = """\
- "outro": one contiguous segment at the very END of the episode covering
  closing music, credits, next-episode teasers, postroll ads, and farewells.
  At most one per episode. Spans from where substantive content finishes
  through to the end of the audio. Closing material and any postroll ads belong
  in this single outro segment, not in separate segments."""

_V2_AD = """\
- "ad": a mid-episode advertisement, sponsored message, host-read ad, promo
  code, paid endorsement, or cross-promotion of another podcast that appears
  BETWEEN the intro and outro. Editorial mentions, listener mail, the host's
  own products discussed editorially, and interview segments are NOT ads.
  Merge multiple ads that are adjacent to each other into a single segment
  covering the entire stretch of ads, but only if they are not separated by
  substantive content."""

_V2_CLOSING = """\
Be conservative. Return an empty `segments` array if nothing should be skipped.
Do not include any fields other than `segments`."""

_V2_INDEX_OPENING = """\
You are analyzing a podcast transcript. Each line has the form
`line|start| text`, where `line` is the line number and `start` is the whole
second of the episode at which the line begins. A line whose text reads
`--- Ns of no speech ---` marks N seconds without spoken words, such as music,
sound effects, or silence; the final one may also give the end of the audio.
Return a single JSON object with one field, `segments`, containing every
contiguous portion of the episode the listener would want to skip."""

_V2_INDEX_INSPECT = """\
Before finalizing the response, deliberately inspect the final transcript
lines for a farewell, credits, a next-episode teaser, a postroll ad, or another
transition away from substantive content. Do not omit an outro merely because
the transcript ends before trailing music or silence that has no spoken words.
Return no outro only when there is no evidence that substantive content has
ended.

Ground every segment in transcript lines. Set `startLine` to the line on which
the segment begins and `endLine` to the line on which it ends, inclusive; a
segment may begin or end on a no-speech line. Set `startSeconds` to the start
of `startLine`, and `endSeconds` to the start of the line after `endLine`, or
to the end of the audio when `endLine` is the last line. When the complete
episode duration is supplied, use that audio endpoint as the outro's
`endSeconds`."""

_V2_SECONDS_OPENING = """\
You are analyzing a timestamped podcast transcript. Each line has the form
`[start - end] text`, with times in seconds. A line whose text reads
`--- Ns of no speech ---` marks N seconds without spoken words, such as music,
sound effects, or silence; the final one may also give the end of the audio.
Return a single JSON object with one field, `segments`, containing every
contiguous portion of the episode the listener would want to skip."""

_V2_SECONDS_INSPECT = """\
Before finalizing the response, deliberately inspect the final transcript
ranges for a farewell, credits, a next-episode teaser, a postroll ad, or another
transition away from substantive content. Do not omit an outro merely because
the transcript ends before trailing music or silence that has no spoken words.
Return no outro only when there is no evidence that substantive content has
ended. Segment starts must be grounded in transcript timestamps. When the
complete episode duration is supplied, use that audio endpoint as the outro's
`endSeconds`; other segment timestamps must stay within transcript ranges."""


def _v2_system(opening: str, inspect: str) -> str:
    return "\n\n".join(
        (opening, "Each segment has a `kind`:", _V2_INTRO, _V2_OUTRO, _V2_AD, inspect, _V2_CLOSING)
    )


V2_INDEX_PROMPT = _v2_system(_V2_INDEX_OPENING, _V2_INDEX_INSPECT)
V2_SECONDS_PROMPT = _v2_system(_V2_SECONDS_OPENING, _V2_SECONDS_INSPECT)

V2_INDEX_USER_PREAMBLE = "Classify only the following transcript. Segments must start and end on its lines."

V3_SENTENCES_PROMPT = "\n\n".join(
    (
        "You are analyzing a podcast transcript. Each line is one joined sentence in "
        "the form `[22.24-23.88] A complete sentence.`, where the two numbers "
        "are the start and end times in seconds. Return one JSON object with "
        "a `segments` array containing every contiguous portion a listener "
        "would want to skip. Each segment must have `startSeconds`, "
        "`endSeconds`, `kind`, and a nonempty `summary` describing what is "
        "actually said or promoted in that segment. Do not use a generic "
        "label such as 'ad break' as the summary.",
        "Each segment has a `kind`:",
        _V2_INTRO,
        _V2_OUTRO,
        _V2_AD,
        "Ground each boundary in the timestamps of the joined sentences. "
        "Inspect the final sentences for farewells, credits, teasers, "
        "postroll ads, and transitions away from substantive content. "
        "Do not omit an outro just because untranscribed music or silence "
        "follows the last word. Return no outro when there is no evidence "
        "that substantive content has ended. When supplied, use the "
        "complete audio duration as the outro's `endSeconds`.",
        _V2_CLOSING,
    )
)

V3_SENTENCES_USER_PREAMBLE = "Classify the following joined-sentence transcript."

LINE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "segments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "startLine": {"type": "INTEGER"},
                    "endLine": {"type": "INTEGER"},
                    "startSeconds": {"type": "NUMBER"},
                    "endSeconds": {"type": "NUMBER"},
                    "summary": {"type": "STRING"},
                    "kind": {"type": "STRING", "enum": ["ad", "intro", "outro"]},
                },
                "required": ["startLine", "endLine", "startSeconds", "endSeconds", "summary", "kind"],
            },
        }
    },
    "required": ["segments"],
}

# --------------------------------------------------------------------------
# audio-v1: the iOS app's `CloudAdDetectionService.segmentsOnlyPrompt` (c1a53ce),
# as the Swift multi-line literal evaluates (its two unescaped line breaks included).

AUDIO_SEGMENTS_PROMPT = (
    "You are analyzing a podcast episode audio file. Return a single JSON "
    "object with one field, `segments`, containing every contiguous portion "
    "of the audio the listener would want to skip.\n"
    "\n"
    "Each segment has a `kind`:\n"
    "\n"
    '- "intro": one contiguous segment near the BEGINNING of the episode '
    "covering theme music, branding, and any preroll ads. At most one per episode. Spans from the start of "
    "the episode through to where the substantive content begins. Do NOT "
    "include introductory content that may be substantive, like host banter,\n"
    "guest introductions, or introductory material to the episode's main\n"
    'topic — only the "front matter" that would be safe to skip without missing '
    "anything important.\n"
    "\n"
    '- "outro": one contiguous segment at the very END of the episode '
    "covering closing music, credits, next-episode teasers, postroll ads, "
    "and farewells. At most one per episode. Spans from where the "
    "substantive content finishes through the physical end of the audio file. "
    "Fold every farewell, credit, closing theme, next-episode teaser, and "
    "postroll ad in that final tail into this single outro rather than returning "
    "separate entries. If the user supplies the complete episode duration, an "
    "outro's `endSeconds` must equal that endpoint.\n"
    "\n"
    '- "ad": a mid-episode advertisement, sponsored message, host-read ad, '
    "promo code, paid endorsement, or cross-promotion of another podcast "
    "that appears BETWEEN the intro and outro. Editorial mentions, "
    "listener mail, the host's own products discussed editorially, and "
    "interview segments are NOT ads.\n"
    "\n"
    "Before returning, deliberately inspect the final portion of the episode "
    "and make an explicit outro decision. Podcasts commonly end with a "
    "farewell, credits, closing music, or a postroll ad. Return no outro only "
    "when substantive episode content genuinely continues to the physical "
    "endpoint and there is no safe-to-skip final tail.\n"
    "\n"
    "Use only timestamps that match the audio. Be conservative — flag segments "
    "only when you're confident. Return an empty `segments` array if "
    "nothing should be skipped. Do not include any fields other than `segments`."
)

AUDIO_INSTRUCTION = "Produce the JSON object as specified."

# --------------------------------------------------------------------------
# Shared fragments.

REPAIR_NUDGE = (
    "Your previous response could not be parsed. Return only the JSON object "
    "described above: a single object with a `segments` array and no other text."
)

CHUNK_NOTES = {
    "first": (
        "This excerpt is part {part} of {total} of the episode transcript. It contains "
        "the beginning of the episode but not its end, so do not return an outro."
    ),
    "middle": (
        "This excerpt is part {part} of {total} of the episode transcript. It contains "
        "neither the beginning nor the end of the episode, so return only ads."
    ),
    "last": (
        "This excerpt is part {part} of {total} of the episode transcript. It contains "
        "the end of the episode but not its beginning, so do not return an intro."
    ),
}


def valid_episode_endpoint(
    episode_duration: float | None,
    transcript: Sequence[Sentence],
) -> float | None:
    """Return a usable physical endpoint that does not precede the transcript."""
    if episode_duration is None or not math.isfinite(episode_duration) or episode_duration <= 0:
        return None
    if transcript and episode_duration < max(segment.end for segment in transcript):
        return None
    return episode_duration


def episode_duration_guidance(
    episode_duration: float | None,
    transcript: Sequence[Sentence],
) -> str:
    endpoint = valid_episode_endpoint(episode_duration, transcript)
    if endpoint is None:
        return "The complete episode duration is unknown; use the final transcript timestamp as the endpoint."
    return (
        f"The complete episode ends at {endpoint:.2f} seconds. "
        "Deliberately inspect the final portion for closing material. If an outro is detected, "
        f"set its endSeconds to the audio endpoint, {endpoint:.2f}."
    )


def audio_duration_context(episode_duration: float | None) -> str:
    """Port of the app's ``durationPromptContext``; empty when the duration is unusable."""
    if episode_duration is None or not math.isfinite(episode_duration) or episode_duration <= 0:
        return ""
    endpoint = f"{episode_duration:.2f}"
    return (
        f"The complete episode duration is {endpoint} seconds. Treat {endpoint} "
        "seconds as the physical audio endpoint and deliberately inspect the "
        f"final portion. If an outro exists, its endSeconds must be {endpoint}, "
        "including any trailing music, silence, or postroll audio."
    )


def strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


# --------------------------------------------------------------------------
# Parsing. The models are deliberately lenient where the sanitiser already
# copes: an unknown `kind` is dropped there rather than failing the whole
# answer, and a missing line citation falls back to the echoed seconds.


class _Segment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    startSeconds: float
    endSeconds: float
    summary: str = ""
    kind: str


class _LineSegment(_Segment):
    startLine: int | None = None
    endLine: int | None = None


class _ContentSegment(_Segment):
    summary: str

    @field_validator("summary")
    @classmethod
    def nonblank_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("each segment needs a content summary")
        return value


class SegmentsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    segments: list[_Segment]


class LineSegmentsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    segments: list[_LineSegment]


class ContentSegmentsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    segments: list[_ContentSegment]


class SchemaViolation(ValueError):
    """The model's answer is not a JSON object matching the prompt's schema."""


def json_schema_for_claude(gemini_schema: dict[str, Any]) -> dict[str, Any]:
    """Translate a Gemini ``responseSchema`` into the JSON Schema structured
    outputs accept: lowercase types and ``additionalProperties: false`` on
    every object. One source of truth keeps the two providers' contracts equal."""
    out: dict[str, Any] = {}
    for key, value in gemini_schema.items():
        if key == "type":
            out[key] = value.lower()
        elif key == "properties":
            out[key] = {name: json_schema_for_claude(prop) for name, prop in value.items()}
        elif key == "items":
            out[key] = json_schema_for_claude(value)
        else:
            out[key] = value
    if out.get("type") == "object":
        out["additionalProperties"] = False
    return out


@dataclass(frozen=True)
class PromptSpec:
    version: str
    render_format: str  # index | seconds | sentences | audio
    system: str
    user_preamble: str
    gemini_schema: dict[str, Any]
    response_model: type[BaseModel]
    cites_lines: bool

    @property
    def claude_schema(self) -> dict[str, Any]:
        return json_schema_for_claude(self.gemini_schema)

    def user_message(self, transcript_text: str, guidance: str, chunk_note: str | None = None) -> str:
        """The recovered server's framing: preamble, duration guidance, transcript."""
        parts = [self.user_preamble, *(p for p in (chunk_note, guidance) if p), transcript_text]
        return "\n\n".join(parts)

    def parse(self, text: str) -> list[DetectedSegment]:
        """Parse the model's answer into segments carrying the echoed seconds
        and, for line-citing prompts, the cited lines (resolved later)."""
        try:
            payload = json.loads(strip_json_fence(text))
        except json.JSONDecodeError as exc:
            raise SchemaViolation(f"response is not JSON: {exc}") from exc
        try:
            parsed = self.response_model.model_validate(payload)
        except ValidationError as exc:
            raise SchemaViolation(f"response does not match the schema: {str(exc)[:500]}") from exc
        return [
            DetectedSegment(
                start_seconds=row.startSeconds,
                end_seconds=row.endSeconds,
                summary=row.summary,
                kind=row.kind,  # type: ignore[arg-type]  # unknown kinds are dropped by the sanitiser
                start_line=getattr(row, "startLine", None),
                end_line=getattr(row, "endLine", None),
            )
            for row in parsed.segments  # type: ignore[attr-defined]
        ]


PROMPTS: dict[tuple[str, str], PromptSpec] = {
    ("segments-v1", "seconds"): PromptSpec(
        version="segments-v1",
        render_format="seconds",
        system=SEGMENTS_ONLY_PROMPT,
        user_preamble=V1_USER_PREAMBLE,
        gemini_schema=RESPONSE_SCHEMA,
        response_model=SegmentsResponse,
        cites_lines=False,
    ),
    ("segments-v2", "index"): PromptSpec(
        version="segments-v2",
        render_format="index",
        system=V2_INDEX_PROMPT,
        user_preamble=V2_INDEX_USER_PREAMBLE,
        gemini_schema=LINE_RESPONSE_SCHEMA,
        response_model=LineSegmentsResponse,
        cites_lines=True,
    ),
    ("segments-v2", "seconds"): PromptSpec(
        version="segments-v2",
        render_format="seconds",
        system=V2_SECONDS_PROMPT,
        user_preamble=V1_USER_PREAMBLE,
        gemini_schema=RESPONSE_SCHEMA,
        response_model=SegmentsResponse,
        cites_lines=False,
    ),
    ("segments-v3", "sentences"): PromptSpec(
        version="segments-v3",
        render_format="sentences",
        system=V3_SENTENCES_PROMPT,
        user_preamble=V3_SENTENCES_USER_PREAMBLE,
        gemini_schema=RESPONSE_SCHEMA,
        response_model=ContentSegmentsResponse,
        cites_lines=False,
    ),
    ("audio-v1", "audio"): PromptSpec(
        version="audio-v1",
        render_format="audio",
        system=AUDIO_SEGMENTS_PROMPT,
        user_preamble=AUDIO_INSTRUCTION,
        gemini_schema=RESPONSE_SCHEMA,
        response_model=SegmentsResponse,
        cites_lines=False,
    ),
}


def get_prompt(version: str, render_format: str) -> PromptSpec:
    """The spec for ``version`` rendered as ``render_format``; a permanent
    error for combinations no prompt was written for."""
    spec = PROMPTS.get((version, render_format))
    if spec is not None:
        return spec
    formats = sorted(fmt for v, fmt in PROMPTS if v == version)
    if not formats:
        raise ClassifierError(f"unknown prompt version {version!r}", permanent=True)
    raise ClassifierError(
        f"prompt {version} is written for transcript_format {' or '.join(map(repr, formats))}, "
        f"not {render_format!r}; set NOADCAST_TRANSCRIPT_FORMAT accordingly",
        permanent=True,
    )
