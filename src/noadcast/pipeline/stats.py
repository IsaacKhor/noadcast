"""Operational snapshot for ``GET /api/v1/admin/stats`` and ``noadcast status``.

Reads the database directly, so the CLI can report on a running server
without talking to it; the pool section is only available in-process.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

from ..db import repo
from ..db.engine import Database
from ..media.store import MediaStore
from ..timeutil import iso, parse_iso, utc_now
from . import jobs


def _age_seconds(stamp: str | None, now: dt.datetime) -> float | None:
    parsed = parse_iso(stamp)
    return None if parsed is None else max(0.0, (now - parsed).total_seconds())


def collect_stats(
    db: Database, store: MediaStore, *, pool_stats: Any | None = None, now: dt.datetime | None = None
) -> dict[str, Any]:
    """JSON-ready (camelCase) stats. ``pool_stats`` is a ``PoolStats`` dataclass when the pool is attached."""
    moment = now or utc_now()
    stamp = iso(moment)
    disk = store.disk_usage()
    month_ago = iso(moment - dt.timedelta(days=30))
    pool: dict[str, Any] | None = None
    if pool_stats is not None:
        raw = dataclasses.asdict(pool_stats)
        pool = {
            "workers": raw.get("workers"),
            "alive": raw.get("alive"),
            "ready": raw.get("ready"),
            "busy": raw.get("busy"),
            "queued": raw.get("queued"),
            "completed": raw.get("completed"),
            "failed": raw.get("failed"),
            "crashed": raw.get("crashed"),
            "respawns": raw.get("respawns"),
            "rssMib": raw.get("rss_mib"),
            "pssMib": raw.get("pss_mib"),
            "perWorker": [
                {
                    "slot": worker.get("slot"),
                    "pid": worker.get("pid"),
                    "state": worker.get("state"),
                    "completed": worker.get("completed"),
                    "rssMib": worker.get("rss_mib"),
                    "pssMib": worker.get("pss_mib"),
                    "peakRssMib": worker.get("peak_rss_mib"),
                    "cpuSeconds": worker.get("cpu_seconds"),
                }
                for worker in raw.get("per_worker", ())
            ],
        }
    return {
        "serverTime": stamp,
        "instanceId": db.instance_id,
        "seq": db.current_seq(),
        "pool": pool,
        "queues": [
            {
                "kind": q.kind,
                "pending": q.pending,
                "running": q.running,
                "available": q.available,
                "oldestPendingAgeSeconds": _age_seconds(q.oldest_available_at, moment),
            }
            for q in jobs.queue_stats(db, now=stamp)
        ],
        "disk": {"totalBytes": disk.total_bytes, "usedBytes": disk.used_bytes, "freeBytes": disk.free_bytes},
        "audio": {"storedBytes": repo.stored_audio_bytes(db), "byState": repo.audio_state_counts(db)},
        "episodesByState": repo.episode_state_counts(db),
        "podcasts": len(repo.list_podcasts(db)),
        "spend30d": [
            {
                "provider": row.provider,
                "model": row.model,
                "calls": row.calls,
                "inputTokens": row.input_tokens,
                "thoughtTokens": row.thought_tokens,
                "outputTokens": row.output_tokens,
                "costUsd": row.cost_usd,
            }
            for row in repo.usage_by_model(db, created_since=month_ago)
        ],
        "recentFailures": [
            {
                "jobId": job.id,
                "kind": job.kind,
                "subjectId": job.subject_id,
                "attempts": job.attempts,
                "error": job.last_error,
                "at": job.finished_at or job.updated_at,
            }
            for job in jobs.recent_failures(db)
        ],
        "guidCollisions": [
            {"guid": collision.guid, "podcastIds": list(collision.podcast_ids)}
            for collision in repo.guid_collisions(db)
        ],
    }
