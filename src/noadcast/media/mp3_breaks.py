"""Conservative structural break hints in raw MP3 files.

An ID3v2 tag or a new Xing/Info encoder header *inside* audio can indicate
that separately encoded pieces were concatenated. Neither proves an ad. This
module only identifies byte boundaries; the decoder assigns their positions
in the same sample timeline used for transcription.
"""

from __future__ import annotations

import mmap
from pathlib import Path


def _frame_length(data: bytes | mmap.mmap, offset: int) -> tuple[int, int, int, int] | None:
    """Return (frame bytes, MPEG version, sample rate, channels) for Layer III."""
    if offset < 0 or offset + 4 > len(data):
        return None
    a, b, c, d = data[offset : offset + 4]
    if a != 0xFF or b & 0xE0 != 0xE0 or b & 0x06 != 0x02:
        return None
    version = (b >> 3) & 3
    bitrate_index, rate_index = c >> 4, (c >> 2) & 3
    if version == 1 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    rates = (44100, 48000, 32000)
    rate = rates[rate_index] // {3: 1, 2: 2, 0: 4}[version]
    bitrates = (
        (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
        if version == 3 else
        (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
    )
    length = (144 if version == 3 else 72) * bitrates[bitrate_index - 1] * 1000 // rate + ((c >> 1) & 1)
    return length, version, rate, 1 if d & 0xC0 == 0xC0 else 2


def _id3_end(data: mmap.mmap, offset: int) -> int | None:
    if offset + 10 > len(data) or data[offset : offset + 3] != b"ID3":
        return None
    version, revision, flags = data[offset + 3 : offset + 6]
    size = data[offset + 6 : offset + 10]
    if version not in (2, 3, 4) or revision == 0xFF or flags & 0x0F or any(byte & 0x80 for byte in size):
        return None
    length = (size[0] << 21) | (size[1] << 14) | (size[2] << 7) | size[3]
    end = offset + 10 + length + (10 if version == 4 and flags & 0x10 else 0)
    return end if end <= len(data) else None


def id3_break_offsets(path: Path, first_audio_offset: int) -> list[int]:
    """Byte offsets just after validated ID3v2 tags embedded after audio.

    Require two consecutive MPEG frames after each tag, making an accidental
    `ID3` sequence in compressed payload very unlikely to become a cue.
    """
    if first_audio_offset < 0:
        return []
    with path.open("rb") as source:
        if not source.seek(0, 2):
            return []
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
            offsets: list[int] = []
            cursor = max(0, first_audio_offset + 4)
            while (start := data.find(b"ID3", cursor)) >= 0:
                end = _id3_end(data, start)
                if end is not None:
                    first = _frame_length(data, end)
                    second = _frame_length(data, end + first[0]) if first else None
                    if first and second and first[1:] == second[1:]:
                        offsets.append(end)
                    cursor = max(start + 3, end)
                else:
                    cursor = start + 3
            return offsets


def has_encoder_header(packet: bytes) -> bool:
    """A Xing/Info VBR header at its specified offset in an MP3 frame."""
    parsed = _frame_length(packet, 0)
    if parsed is None:
        return False
    _, version, _, channels = parsed
    crc_bytes = 0 if packet[1] & 1 else 2
    side_info = (17 if channels == 1 else 32) if version == 3 else (9 if channels == 1 else 17)
    marker = packet[4 + crc_bytes + side_info : 8 + crc_bytes + side_info]
    return marker in (b"Xing", b"Info")


def frame_signature(packet: bytes) -> tuple[int, int, int] | None:
    """Stable properties; bitrate changes alone are ordinary VBR."""
    parsed = _frame_length(packet, 0)
    return parsed[1:] if parsed else None
