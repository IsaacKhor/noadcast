"""Episode pipeline states and the job kinds that move episodes between them.

``discovered → download_pending → downloading → downloaded →
transcribe_pending → transcribing → transcribed → classify_pending →
classifying → ready``, plus ``failed``. ``audio_state`` is orthogonal: an
episode stays ``ready`` after its audio is released or evicted, and a
re-download of a finished episode moves only ``audio_state``.
"""

from __future__ import annotations

REFRESH_FEED = "refresh_feed"
DOWNLOAD = "download"
TRANSCRIBE = "transcribe"
CLASSIFY = "classify"
EVICT = "evict"

JOB_KINDS = (REFRESH_FEED, DOWNLOAD, TRANSCRIBE, CLASSIFY, EVICT)
EPISODE_STAGES = (DOWNLOAD, TRANSCRIBE, CLASSIFY)

PENDING_STATE = {DOWNLOAD: "download_pending", TRANSCRIBE: "transcribe_pending", CLASSIFY: "classify_pending"}
RUNNING_STATE = {DOWNLOAD: "downloading", TRANSCRIBE: "transcribing", CLASSIFY: "classifying"}
# Where an episode rests when its stage job is canceled: the last completed step.
RESTING_STATE = {DOWNLOAD: "discovered", TRANSCRIBE: "downloaded", CLASSIFY: "transcribed"}

# States in which a download is itself the next pipeline step. From any later
# state a download only restores audio and leaves pipeline_state alone.
DOWNLOAD_IS_PIPELINE_STEP = frozenset({"discovered", "download_pending", "downloading", "downloaded", "failed"})
