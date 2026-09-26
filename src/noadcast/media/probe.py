"""Audio probing with PyAV.

Opens the container and reads stream metadata only — nothing is decoded
beyond what FFmpeg's stream-info pass needs — so a 60 MB episode probes in
well under 0.1 s.

The duration is the container's, which for a VBR MP3 without a Xing/VBRI
header is an estimate from the first frames' bit rate and can be off by
minutes. The transcription stage decodes every sample; its length is
authoritative and should replace this one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import av
import av.error

# FFmpeg demuxer name (one element of e.g. "mov,mp4,m4a,3gp,3g2,mj2") -> MIME type.
_CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "aac": "audio/aac",  # raw ADTS
    "ogg": "audio/ogg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "aiff": "audio/aiff",
    "caf": "audio/x-caf",
    "asf": "audio/x-ms-wma",
    "amr": "audio/amr",
    "ac3": "audio/ac3",
    "eac3": "audio/eac3",
}
_MP4_FAMILY = frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"})
_WEBM_AUDIO_CODECS = frozenset({"opus", "vorbis"})


@dataclass(frozen=True)
class AudioProbe:
    duration_seconds: float | None
    codec: str | None
    container: str | None
    content_type: str  # best MIME type for serving, e.g. audio/mpeg
    bit_rate: int | None


class ProbeError(Exception):
    """The file is not decodable audio. Permanent."""


def probe_audio(path: Path) -> AudioProbe:
    """Synchronous; call via ``asyncio.to_thread``.

    Raises ``ProbeError`` if FFmpeg cannot open the file as media, or it has
    no audio stream FFmpeg can decode. A missing or unreadable file raises
    the usual ``OSError`` instead (PyAV's system errors all subclass it):
    that is a storage problem, not proof the bytes are not audio, and must
    not permanently fail an episode.
    """
    try:
        container = av.open(str(path), metadata_errors="ignore")
    except OSError:
        raise
    except av.error.FFmpegError as exc:
        raise ProbeError(f"not a media file: {exc}") from exc
    with container:
        try:
            return _describe(container)
        except av.error.FFmpegError as exc:
            raise ProbeError(f"unreadable media file: {exc}") from exc


def _describe(container: av.container.InputContainer) -> AudioProbe:
    if not container.streams.audio:
        raise ProbeError(f"no audio stream (container {container.format.name})")
    stream = container.streams.audio[0]
    codec = stream.codec_context.codec
    if not codec.is_decoder:
        raise ProbeError(f"no decoder for audio codec {codec.canonical_name}")
    # Cover art shows up as a one-frame video stream; only real video counts.
    has_video = any(not video.disposition & av.stream.Disposition.attached_pic for video in container.streams.video)
    duration: float | None = None
    if container.duration:
        duration = container.duration / av.time_base
    elif stream.duration and stream.time_base:
        duration = float(stream.duration * stream.time_base)
    return AudioProbe(
        duration_seconds=duration if duration and duration > 0 else None,
        codec=codec.canonical_name,
        container=container.format.name,
        content_type=serving_content_type(container.format.name, codec.canonical_name, has_video=has_video),
        bit_rate=container.bit_rate or stream.codec_context.bit_rate or None,
    )


def serving_content_type(container: str, codec: str | None, *, has_video: bool = False) -> str:
    """MIME type to serve a file with, from FFmpeg's demuxer name.

    Always ``audio/*`` or ``video/*``: those are what compression middleware
    leaves alone (see ``media.ranges``), so an unknown container gets an
    ``audio/x-<demuxer>`` label rather than ``application/octet-stream``.
    """
    names = container.split(",")
    major = "video" if has_video else "audio"
    if _MP4_FAMILY.intersection(names):
        return f"{major}/mp4"
    if "matroska" in names or "webm" in names:
        return f"{major}/webm" if codec in _WEBM_AUDIO_CODECS else f"{major}/x-matroska"
    for name in names:
        if name in _CONTENT_TYPES:
            return _CONTENT_TYPES[name]
    return f"{major}/x-{names[0]}"
