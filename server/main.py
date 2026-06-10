from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field


ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / "secrets.env")

app = FastAPI(title="Noadcast Ad Detection Server")


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
  through to the end of the audio.

- "ad": a mid-episode advertisement, sponsored message, host-read ad, promo
  code, paid endorsement, or cross-promotion of another podcast that appears
  BETWEEN the intro and outro.

Use only timestamps from the transcript. Be conservative. Return an empty
`segments` array if nothing should be skipped. Do not include any fields other
than `segments`.
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


@dataclass(frozen=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str


class SegmentResponse(BaseModel):
    startSeconds: float
    endSeconds: float
    summary: str
    kind: str


class TokenUsageResponse(BaseModel):
    inputTokens: int = 0
    thoughtTokens: int = 0
    outputTokens: int = 0


class AnalyzeResponse(BaseModel):
    segments: list[SegmentResponse] = Field(default_factory=list)
    usage: TokenUsageResponse | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(
    audio: UploadFile = File(...),
    model: str = Form("gemini-3.5-flash"),
    mime_type: str | None = Form(None),
    thinking_level: str | None = Form(None),
    google_api_key: str | None = Form(None),
) -> AnalyzeResponse:
    api_key = google_api_key or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="Missing Gemini API key. Send google_api_key or set GEMINI_API_KEY.",
        )

    with tempfile.TemporaryDirectory(prefix="noadcast-analysis-") as tmp:
        work_dir = Path(tmp)
        suffix = Path(audio.filename or "episode").suffix or suffix_for_mime_type(mime_type)
        uploaded_path = work_dir / f"uploaded{suffix}"
        wav_path = work_dir / "input.wav"

        try:
            await save_upload(audio, uploaded_path)
            await asyncio.to_thread(convert_to_wav, uploaded_path, wav_path)
            transcript = await asyncio.to_thread(run_whisper_cpp, wav_path, work_dir)
            if not transcript:
                raise HTTPException(status_code=422, detail="whisper.cpp returned an empty transcript.")
            segments, usage = await call_gemini(
                transcript=transcript,
                model=model,
                api_key=api_key,
                thinking_level=thinking_level,
            )
            cleaned = sanitize_segments(segments, transcript)
            return AnalyzeResponse(segments=cleaned, usage=usage)
        finally:
            await audio.close()


async def save_upload(upload: UploadFile, destination: Path) -> None:
    with destination.open("wb") as out:
        while chunk := await upload.read(1024 * 1024):
            out.write(chunk)


def convert_to_wav(source: Path, destination: Path) -> None:
    ffmpeg = os.getenv("FFMPEG_BINARY", "ffmpeg")
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        str(destination),
    ]
    run_command(command, "ffmpeg conversion failed")


def run_whisper_cpp(wav_path: Path, work_dir: Path) -> list[TranscriptSegment]:
    binary = os.getenv("WHISPER_CPP_BINARY", "whisper-cli")
    model_path = os.getenv("WHISPER_CPP_MODEL")
    if not model_path:
        raise HTTPException(status_code=500, detail="Set WHISPER_CPP_MODEL to a whisper.cpp model path.")

    output_base = work_dir / "transcript"
    common = [binary, "-m", model_path, "-f", str(wav_path), "-of", str(output_base)]
    language = os.getenv("WHISPER_CPP_LANGUAGE")
    if language:
        common.extend(["-l", language])

    last_error: str | None = None
    for json_flag in ("-ojf", "-oj"):
        try:
            run_command([*common, json_flag], "whisper.cpp transcription failed")
            transcript_path = output_base.with_suffix(".json")
            return parse_whisper_json(transcript_path)
        except HTTPException as exc:
            last_error = str(exc.detail)
            try:
                output_base.with_suffix(".json").unlink()
            except FileNotFoundError:
                pass

    raise HTTPException(status_code=500, detail=last_error or "whisper.cpp transcription failed")


def run_command(command: list[str], failure_message: str) -> None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"{command[0]} not found") from exc

    if completed.returncode != 0:
        stderr = decode_output(completed.stderr).strip() or decode_output(completed.stdout).strip()
        raise HTTPException(status_code=500, detail=f"{failure_message}: {stderr[-2000:]}")


def decode_output(output: bytes) -> str:
    return output.decode("utf-8", errors="replace")


def parse_whisper_json(path: Path) -> list[TranscriptSegment]:
    try:
        payload = json.loads(decode_output(path.read_bytes()))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="whisper.cpp did not write transcript.json") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid whisper.cpp JSON: {exc}") from exc

    rows = payload.get("transcription") or payload.get("segments") or []
    transcript: list[TranscriptSegment] = []
    for row in rows:
        parsed = parse_transcript_row(row)
        if parsed:
            transcript.append(parsed)
    return transcript


def parse_transcript_row(row: dict[str, Any]) -> TranscriptSegment | None:
    text = str(row.get("text") or "").strip()
    if not text:
        return None

    offsets = row.get("offsets") if isinstance(row.get("offsets"), dict) else {}
    timestamps = row.get("timestamps") if isinstance(row.get("timestamps"), dict) else {}

    start = first_number(
        row.get("start"),
        row.get("startSeconds"),
        milliseconds_to_seconds(offsets.get("from")),
        parse_timestamp(timestamps.get("from")),
    )
    end = first_number(
        row.get("end"),
        row.get("endSeconds"),
        milliseconds_to_seconds(offsets.get("to")),
        parse_timestamp(timestamps.get("to")),
    )
    if start is None or end is None or end <= start:
        return None
    return TranscriptSegment(start_seconds=start, end_seconds=end, text=text)


def first_number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, (int, float)) and math.isfinite(value):
            return float(value)
    return None


def milliseconds_to_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value) / 1000
    return None


def parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[,.](\d{1,3}))?", value.strip())
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    millis = int((match.group(4) or "0").ljust(3, "0"))
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


async def call_gemini(
    transcript: list[TranscriptSegment],
    model: str,
    api_key: str,
    thinking_level: str | None,
) -> tuple[list[SegmentResponse], TokenUsageResponse | None]:
    base_url = os.getenv("GEMINI_API_BASE", "https://generativelanguage.googleapis.com")
    url = f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent"
    transcript_text = format_transcript(transcript)

    generation_config: dict[str, Any] = {
        "responseMimeType": "application/json",
        "responseSchema": RESPONSE_SCHEMA,
    }
    if thinking_level:
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}

    body = {
        "systemInstruction": {"parts": [{"text": SEGMENTS_ONLY_PROMPT}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "Classify only the following transcript. "
                            "All returned timestamps must be within these transcript ranges.\n\n"
                            f"{transcript_text}"
                        )
                    }
                ],
            }
        ],
        "generationConfig": generation_config,
    }

    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(url, params={"key": api_key}, json=body)
    if response.status_code < 200 or response.status_code >= 300:
        raise HTTPException(
            status_code=502,
            detail=f"Gemini returned HTTP {response.status_code}: {response.text[:1000]}",
        )

    payload = response.json()
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        segments_payload = json.loads(strip_json_fence(text))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not parse Gemini response: {exc}") from exc

    segments = [SegmentResponse(**row) for row in segments_payload.get("segments", [])]
    usage_payload = payload.get("usageMetadata") or {}
    usage = TokenUsageResponse(
        inputTokens=int(usage_payload.get("promptTokenCount") or 0),
        thoughtTokens=int(usage_payload.get("thoughtsTokenCount") or 0),
        outputTokens=int(usage_payload.get("candidatesTokenCount") or 0),
    )
    return segments, usage


def format_transcript(transcript: list[TranscriptSegment]) -> str:
    return "\n".join(
        f"[{segment.start_seconds:.2f} - {segment.end_seconds:.2f}] {segment.text}"
        for segment in transcript
    )


def strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def sanitize_segments(
    segments: list[SegmentResponse],
    transcript: list[TranscriptSegment],
) -> list[SegmentResponse]:
    if not transcript:
        return []
    min_start = max(0.0, min(segment.start_seconds for segment in transcript))
    max_end = max(segment.end_seconds for segment in transcript)

    cleaned: list[SegmentResponse] = []
    for segment in segments:
        if segment.kind not in {"ad", "intro", "outro"}:
            continue
        if not math.isfinite(segment.startSeconds) or not math.isfinite(segment.endSeconds):
            continue
        start = min(max(segment.startSeconds, min_start), max_end)
        end = min(max(segment.endSeconds, min_start), max_end)
        if end <= start:
            continue
        cleaned.append(
            SegmentResponse(
                startSeconds=start,
                endSeconds=end,
                summary=segment.summary,
                kind=segment.kind,
            )
        )
    return sorted(cleaned, key=lambda item: item.startSeconds)


def suffix_for_mime_type(mime_type: str | None) -> str:
    if not mime_type:
        return ".audio"
    lowered = mime_type.lower()
    if "mpeg" in lowered:
        return ".mp3"
    if "mp4" in lowered or "m4a" in lowered:
        return ".m4a"
    if "aac" in lowered:
        return ".aac"
    if "ogg" in lowered:
        return ".ogg"
    if "wav" in lowered:
        return ".wav"
    return ".audio"


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8765"))
    uvicorn.run("main:app", host=host, port=port)


if __name__ == "__main__":
    main()
