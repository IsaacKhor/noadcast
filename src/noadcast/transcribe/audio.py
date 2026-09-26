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
from typing import Iterator

import av
import numpy as np

SAMPLE_RATE = 16000
GROUP_SAMPLES = 500_000


def decode_audio(path: str, sampling_rate: int = SAMPLE_RATE) -> tuple[np.ndarray, int]:
    """Return ``(samples, skipped_packets)``; raises what ``av.open`` raises for unreadable files."""
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=sampling_rate)
    raw = io.BytesIO()
    skipped = 0

    with av.open(path, mode="r", metadata_errors="ignore") as container:
        stream = container.streams.audio[0]  # IndexError when there is no audio stream

        def frames() -> Iterator[av.AudioFrame]:
            nonlocal skipped
            for packet in container.demux(stream):
                try:
                    yield from packet.decode()  # a None flush packet at EOF drains the decoder
                except av.error.InvalidDataError:
                    skipped += 1

        fifo = av.audio.fifo.AudioFifo()
        for frame in frames():
            frame.pts = None  # splices break timestamp continuity; the fifo would reject them
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
    return audio, skipped
