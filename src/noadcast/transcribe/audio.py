"""Audio decoding for the transcription worker: 16 kHz mono float32.

A replacement for ``faster_whisper.audio.decode_audio`` (1.2.x) that survives
corrupt packets. faster-whisper wraps ``container.decode()`` in a generator
that catches ``InvalidDataError`` and calls ``next()`` again, but a generator
that has raised is finished: the first bad packet silently ends the decode.
Dynamic ad insertion splices produce exactly such packets ("Header missing");
two 3-hour Pardon My Take renders decoded to 55 s that way. Here each packet
is decoded on its own and only the bad ones are dropped, as the ffmpeg CLI
does. The resampling (s16 mono, 500 000-sample groups, flushed) matches
faster-whisper's, so clean files decode to identical samples.

Imported only inside the worker process: it loads numpy and PyAV.
"""

from __future__ import annotations

import gc
import io
from pathlib import Path
from typing import Iterator

import av
import numpy as np

from ..media.mp3_breaks import frame_signature, has_encoder_header, id3_break_offsets

SAMPLE_RATE = 16000
GROUP_SAMPLES = 500_000


def decode_audio(path: str, sampling_rate: int = SAMPLE_RATE) -> tuple[np.ndarray, int, list[dict[str, float | str]]]:
    """Return samples, skipped packet count, and MP3 breaks on the sample timeline."""
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=sampling_rate)
    raw = io.BytesIO()
    skipped = 0
    breaks: list[dict[str, float | str]] = []

    with av.open(path, mode="r") as container:
        stream = container.streams.audio[0]  # IndexError when there is no audio stream
        is_mp3 = "mp3" in container.format.name.split(",")
        first_packet = True
        tag_offsets: list[int] = []
        next_tag = 0
        decoded_seconds = 0.0
        last_break_seconds = -1.0
        stable_signature: tuple[int, int, int] | None = None
        candidate_signature: tuple[int, int, int] | None = None
        candidate_seconds = 0.0

        def record_break(kind: str, at: float) -> None:
            nonlocal last_break_seconds
            # An ID3 tag and an encoder header can describe the same splice.
            if at <= 0.5:
                return
            if at - last_break_seconds <= 0.1:
                priority = {"decode_error": 0, "format_change": 1, "encoder_header": 2, "embedded_id3": 3}
                if breaks and priority[kind] > priority[str(breaks[-1]["kind"])]:
                    breaks[-1] = {"seconds": round(at, 3), "kind": kind}
                    last_break_seconds = at
            else:
                breaks.append({"seconds": round(at, 3), "kind": kind})
                last_break_seconds = at

        def frames() -> Iterator[av.AudioFrame]:
            nonlocal skipped, first_packet, next_tag, decoded_seconds, tag_offsets
            nonlocal stable_signature, candidate_signature, candidate_seconds
            for packet in container.demux(stream):
                if is_mp3 and packet.size and packet.pos is not None and packet.pos >= 0:
                    if first_packet:
                        tag_offsets = id3_break_offsets(Path(path), packet.pos)
                        first_packet = False
                    while next_tag < len(tag_offsets) and packet.pos >= tag_offsets[next_tag]:
                        record_break("embedded_id3", decoded_seconds)
                        next_tag += 1
                    packet_bytes = bytes(packet)
                    if has_encoder_header(packet_bytes):
                        record_break("encoder_header", decoded_seconds)
                    signature = frame_signature(packet_bytes)
                    if signature is not None:
                        if stable_signature is None:
                            stable_signature = signature
                        elif signature == stable_signature:
                            candidate_signature = None
                        elif signature == candidate_signature:
                            record_break("format_change", candidate_seconds)
                            stable_signature = signature
                            candidate_signature = None
                        else:
                            candidate_signature = signature
                            candidate_seconds = decoded_seconds
                try:
                    for frame in packet.decode():  # a None flush packet at EOF drains the decoder
                        decoded_seconds += frame.samples / frame.sample_rate
                        yield frame
                except av.error.InvalidDataError:
                    skipped += 1
                    if is_mp3:
                        record_break("decode_error", decoded_seconds)

        fifo = av.audio.fifo.AudioFifo()
        source_format: tuple[str, str, int] | None = None
        for frame in frames():
            frame.pts = None  # splices break timestamp continuity; the fifo would reject them
            frame_format = (frame.format.name, frame.layout.name, frame.sample_rate)
            if source_format is not None and frame_format != source_format:
                # Separately encoded pieces can change sample rate or channel
                # layout. Drain the old resampler before opening the new one;
                # Whisper's input remains a contiguous 16 kHz sample stream.
                if fifo.samples:
                    for out in resampler.resample(fifo.read()):
                        raw.write(out.to_ndarray())
                for out in resampler.resample(None):
                    raw.write(out.to_ndarray())
                resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=sampling_rate)
                fifo = av.audio.fifo.AudioFifo()
            source_format = frame_format
            fifo.write(frame)
            if fifo.samples >= GROUP_SAMPLES:
                for out in resampler.resample(fifo.read()):
                    raw.write(out.to_ndarray())
        if fifo.samples:
            for out in resampler.resample(fifo.read()):
                raw.write(out.to_ndarray())
        for out in resampler.resample(None):
            raw.write(out.to_ndarray())

    # faster-whisper#390: resampler objects are only freed by a collection.
    del resampler
    gc.collect()
    audio = np.frombuffer(raw.getbuffer(), dtype=np.int16).astype(np.float32) / 32768.0
    return audio, skipped, breaks
