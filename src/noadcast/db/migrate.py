"""Apply numbered SQL migrations from ``db/migrations/NNN_name.sql``.

Migrations are append-only: never edit a file after it has been applied on a
real database. Each file runs inside its own transaction together with its
``schema_migrations`` row, so a failure leaves the database unchanged.
"""

from __future__ import annotations

import re
import sqlite3
from importlib import resources

from ..timeutil import now_iso

_NAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


def migration_files() -> list[tuple[int, str, str]]:
    root = resources.files("noadcast.db").joinpath("migrations")
    found: list[tuple[int, str, str]] = []
    for entry in root.iterdir():
        match = _NAME.match(entry.name)
        if match:
            found.append((int(match.group(1)), match.group(2), entry.read_text(encoding="utf-8")))
    found.sort()
    versions = [version for version, _, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"migration numbering must be contiguous from 001, found {versions}")
    return found


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations; returns the versions applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    newly: list[int] = []
    for version, name, sql in migration_files():
        if version in applied:
            continue
        # executescript() commits any open transaction first, so the script
        # carries its own BEGIN/COMMIT to stay atomic with its bookkeeping row.
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{sql}\n;\n"
            f"INSERT INTO schema_migrations (version, name, applied_at) VALUES ({version}, '{name}', '{now_iso()}');\n"
            "COMMIT;\n"
        )
        try:
            conn.executescript(script)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        newly.append(version)
    return newly
