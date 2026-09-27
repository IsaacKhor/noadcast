"""``noadcast`` command line.

Commands that change state (``refresh``, ``reprocess``) write jobs straight
into the database with the same seq-correct functions the API uses; a
running server's scheduler notices them within its poll interval. SQLite
serialises this second writer against the server's, and every seq is
allocated inside the writing transaction, so the sync invariant holds.

Module-level imports stay stdlib-light on purpose: every spawned
transcription worker re-imports the console script (and so this module)
before it sets its thread environment, so anything heavy — the web stack,
numpy — is imported inside the command that needs it.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path
from typing import Any, Sequence

from .classifier_models import MODEL_IDS
from .config import Settings
from .db.engine import Database
from .timeutil import now_iso

SELFTEST_AUDIO = Path("benchmarks/tal/audio/01-646.mp3")


def _settings() -> Settings:
    return Settings.load()


def _print(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(_settings())
    return 0


def _migration_versions(db: Database) -> set[int]:
    if db.read_one("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'") is None:
        return set()
    return {row["version"] for row in db.read("SELECT version FROM schema_migrations")}


def cmd_migrate(args: argparse.Namespace) -> int:
    settings = _settings()
    with Database(settings.db_path, migrate=False) as db:
        before = _migration_versions(db)
    with Database(settings.db_path) as db:
        after = _migration_versions(db)
    _print({"database": str(settings.db_path), "applied": sorted(after - before), "version": max(after)})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from .media.store import MediaStore
    from .pipeline.stats import collect_stats

    settings = _settings()
    with Database(settings.db_path) as db:
        _print(collect_stats(db, MediaStore(settings.data_dir)))
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    from .pipeline import commands

    settings = _settings()
    with Database(settings.db_path) as db, db.write() as tx:
        if args.podcast_id is None:
            job_ids = commands.refresh_all(tx, now=now_iso())
        else:
            job_ids = [commands.refresh_podcast(tx, args.podcast_id, now=now_iso())]
    _print({"jobIds": job_ids})
    return 0


def cmd_reprocess(args: argparse.Namespace) -> int:
    from .db import repo
    from .pipeline import commands

    settings = _settings()
    with Database(settings.db_path) as db, db.write() as tx:
        job_id = commands.reanalyze_episode(
            tx,
            args.episode_id,
            server=repo.load_server_settings(tx, settings),
            now=now_iso(),
            provider=args.provider,
            model=args.model,
            thinking=args.thinking,
            retranscribe=args.retranscribe,
        )
    _print({"jobId": job_id})
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    print(secrets.token_urlsafe(32))
    return 0


def cmd_pool_selftest(args: argparse.Namespace) -> int:
    from .transcribe.pool import PoolConfig
    from .transcribe.selftest import run_selftest

    overrides = {name: value for name, value in (("workers", args.workers), ("cpu_threads", args.threads)) if value}
    config = PoolConfig.from_settings(_settings(), **overrides)
    report = run_selftest(config, args.audio, kill_worker=args.kill_worker, reference=args.reference)
    _print(report)
    return 0 if report["ok"] else 1


def cmd_models(args: argparse.Namespace) -> int:
    from .transcribe import models

    settings = _settings()
    dest = Path(args.dest) if args.dest else settings.asr_model_dir
    try:
        if args.models_command == "fetch":
            info = models.fetch_model(args.model_id or settings.asr_model_id, args.revision, dest, replace=args.replace)
        elif args.models_command == "link":
            info = models.link_model(
                args.source, dest, model_id=args.model_id or settings.asr_model_id, replace=args.replace
            )
        else:
            info = models.verify_model(dest)
    except models.ModelError as exc:
        print(f"noadcast: {exc}", file=sys.stderr)
        return 1
    _print(info.to_dict())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="noadcast", description="Self-hosted Noadcast podcast server.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the server (reads secrets.env and NOADCAST_* variables)").set_defaults(
        func=cmd_serve
    )
    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    sub.add_parser("status", help="print queue, disk, spend, and episode stats as JSON").set_defaults(
        func=cmd_status
    )

    refresh = sub.add_parser("refresh", help="queue a feed refresh (all podcasts by default)")
    refresh.add_argument("podcast_id", nargs="?", type=int)
    refresh.set_defaults(func=cmd_refresh)

    reprocess = sub.add_parser("reprocess", help="queue a fresh classification of an episode")
    reprocess.add_argument("episode_id", type=int)
    reprocess.add_argument("--provider", choices=("openrouter",))
    reprocess.add_argument("--model", choices=MODEL_IDS)
    reprocess.add_argument("--thinking")
    reprocess.add_argument("--retranscribe", action="store_true", help="transcribe again first")
    reprocess.set_defaults(func=cmd_reprocess)

    sub.add_parser("token", help="print a new random API token").set_defaults(func=cmd_token)

    selftest = sub.add_parser("pool-selftest", help="start the transcription pool and transcribe one file")
    selftest.add_argument("audio", nargs="?", type=Path, default=SELFTEST_AUDIO, help=f"default: {SELFTEST_AUDIO}")
    selftest.add_argument("--reference", type=Path, help="recorded transcript JSON to compare every word against")
    selftest.add_argument("--kill-worker", action="store_true", help="kill the busy worker mid-task, then retry")
    selftest.add_argument("--workers", type=int, help="override NOADCAST_POOL_WORKERS")
    selftest.add_argument("--threads", type=int, help="override NOADCAST_POOL_THREADS")
    selftest.set_defaults(func=cmd_pool_selftest)

    model_parser = sub.add_parser("models", help="install or verify the ASR model directory")
    model_sub = model_parser.add_subparsers(dest="models_command", required=True)
    fetch = model_sub.add_parser("fetch", help="download the model from Hugging Face (pinned revision)")
    fetch.add_argument("--model-id")
    fetch.add_argument("--revision")
    link = model_sub.add_parser("link", help="copy an existing model directory (e.g. benchmarks/tal/models/tiny.en)")
    link.add_argument("source", type=Path)
    link.add_argument("--model-id")
    verify = model_sub.add_parser("verify", help="re-hash the installed model against its provenance")
    for command in (fetch, link, verify):
        command.add_argument("--dest", help="model directory (default: NOADCAST_ASR_MODEL_DIR)")
    for command in (fetch, link):
        command.add_argument("--replace", action="store_true", help="replace a different installed model")
    model_parser.set_defaults(func=cmd_models)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from .pipeline.commands import NotFound

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except NotFound as exc:
        print(f"noadcast: {exc} not found", file=sys.stderr)
        return 2
    except ValueError as exc:  # configuration errors (Settings.validate) and bad arguments
        print(f"noadcast: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
