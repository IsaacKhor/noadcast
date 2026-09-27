"""Builders shared by the test_classify_* suites (no tests here)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import httpx

from noadcast.classify.base import ClassifyRequest, DetectedSegment
from noadcast.transcribe.protocol import Sentence, SilenceRegion

FIXTURES = Path(__file__).parent / "fixtures" / "llm"


def sentence(index: int, start: float, end: float, text: str = "Some words here.", flags: Sequence[str] = ()) -> Sentence:
    return Sentence(
        index=index,
        start=start,
        end=end,
        text=text,
        word_start=index * 10,
        word_count=len(text.split()),
        break_reason="punct",
        soft_end=False,
        min_p=0.8,
        mean_p=0.95,
        flags=tuple(flags),
    )


def sentences(*spans: tuple[float, float] | tuple[float, float, str]) -> list[Sentence]:
    return [sentence(i, *span) for i, span in enumerate(spans)]


def silence(start: float, end: float, kind: str = "gap") -> SilenceRegion:
    return SilenceRegion(start, end, kind)


def request(
    sents: Sequence[Sentence],
    silences: Sequence[SilenceRegion] = (),
    duration: float | None = None,
    title: str | None = None,
    **extra: Any,
) -> ClassifyRequest:
    return ClassifyRequest(sentences=sents, silences=silences, episode_duration=duration, episode_title=title, **extra)


def episode() -> ClassifyRequest:
    """A small episode: head silence, intro, a music break, an ad, content, credits, tail."""
    sents = sentences(
        (1.5, 8.0, "From WBEZ Chicago, it's This American Life."),
        (8.4, 14.0, "I'm Ira Glass."),
        (20.0, 34.0, "Today on our program, a story about a house."),
        (34.3, 50.0, "It starts in a small town."),
        (60.0, 75.0, "Support for this show comes from Acme Mattresses."),
        (75.4, 90.0, "Use promo code SLEEP for ten percent off."),
        (95.0, 150.0, "And so the story continues for a while."),
        (150.2, 170.0, "Our program was produced by a team of people."),
    )
    silences = [
        silence(0.0, 1.5, "head"),
        silence(14.0, 20.0),
        silence(50.0, 60.0),
        silence(90.0, 95.0),
        silence(170.0, 185.0, "tail"),
    ]
    return request(sents, silences, duration=185.0, title="Test Episode")


def segment(start: float, end: float, kind: str = "ad", summary: str = "", **lines: int | None) -> DetectedSegment:
    return DetectedSegment(start, end, summary, kind, **lines)  # type: ignore[arg-type]


def segments_json(*rows: dict[str, Any]) -> str:
    return json.dumps({"segments": list(rows)})


def gemini_body(text: str, *, prompt: int = 1000, thoughts: int = 50, candidates: int = 80, cached: int = 0,
                finish: str = "STOP") -> dict[str, Any]:
    usage = {"promptTokenCount": prompt, "candidatesTokenCount": candidates, "thoughtsTokenCount": thoughts,
             "totalTokenCount": prompt + candidates + thoughts}
    if cached:
        usage["cachedContentTokenCount"] = cached
    return {
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish, "index": 0}],
        "usageMetadata": usage,
        "modelVersion": "gemini-3.5-flash",
    }


def gemini_error(status: int, rpc_status: str, message: str = "error", retry_delay: str | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": status, "message": message, "status": rpc_status}
    if retry_delay is not None:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}]
    return {"error": error}


def claude_body(text: str | None, *, input_tokens: int = 1000, output_tokens: int = 120, cache_read: int = 0,
                cache_write: int = 0, thinking_tokens: int = 40, stop_reason: str = "end_turn",
                model: str = "claude-sonnet-5") -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    if text is not None:
        content.append({"type": "text", "text": text})
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "output_tokens_details": {"thinking_tokens": thinking_tokens},
        },
    }


def claude_error(status: int, error_type: str, message: str = "error") -> dict[str, Any]:
    return {"type": "error", "error": {"type": error_type, "message": message}}


class Recorder:
    """Scripted transport handler: replays ``replies`` in order (the last one
    repeats) and records every request. A reply is ``(status, json_body)``,
    ``(status, json_body, headers)``, or an exception instance to raise."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.requests: list[Any] = []

    def next_reply(self) -> Any:
        return self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


def httpx_client(recorder: Recorder) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        reply = recorder.next_reply()
        if isinstance(reply, BaseException):
            raise reply
        status, body, *headers = reply
        return httpx.Response(status, json=body, headers=headers[0] if headers else None)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def no_jitter(low: float, high: float) -> float:
    return 1.0
