"""SQLite access with a single-writer discipline.

Invariants the rest of the server relies on:

1. **Thread affinity.** A ``Database`` is used only on the thread that opened
   it (sqlite3's ``check_same_thread`` enforces this). In the server that is
   the event-loop thread, so every route and dependency that touches the
   database must be ``async def`` — FastAPI runs plain ``def`` handlers in a
   thread pool, where they would fail loudly.
2. **No await inside a write.** ``write()`` is a synchronous context manager.
   Do all I/O (network, disk, subprocess) first, then open the transaction,
   write, and leave. Opening a second write while one is open — which is what
   happens if a coroutine awaits inside ``with db.write()`` and another
   coroutine writes — raises immediately rather than corrupting ordering.
3. **Sync sequence.** ``tx.next_seq()`` allocates ``updated_seq`` values from
   ``sync_state`` inside the same ``BEGIN IMMEDIATE`` transaction as the row
   change. SQLite serialises write transactions, so commit order equals seq
   order and a reader can never observe seq N+1 committed while N is not —
   which is what lets clients page with ``since=<seq>`` without losing rows.
   Allocate one seq per changed row: rows must never share a seq, or paging
   by ``updated_seq > since`` could split a group across pages and skip it.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .migrate import apply_migrations

Params = Sequence[Any] | dict[str, Any]


class WriteTx:
    """Handle for one open write transaction. Valid only inside ``Database.write()``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._open = True

    def _check(self) -> None:
        if not self._open:
            raise RuntimeError("write transaction already closed")

    def execute(self, sql: str, params: Params = ()) -> sqlite3.Cursor:
        self._check()
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Params]) -> sqlite3.Cursor:
        self._check()
        return self._conn.executemany(sql, rows)

    def read(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        self._check()
        return self._conn.execute(sql, params).fetchall()

    def read_one(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        self._check()
        return self._conn.execute(sql, params).fetchone()

    def next_seq(self, count: int = 1) -> int:
        """Allocate ``count`` consecutive seqs; returns the first."""
        self._check()
        if count < 1:
            raise ValueError("count must be positive")
        (last,) = self._conn.execute(
            "UPDATE sync_state SET seq = seq + ? WHERE id = 1 RETURNING seq", (count,)
        ).fetchone()
        return last - count + 1


class Database:
    def __init__(self, path: Path | str, *, migrate: bool = True) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=True)
        self._conn.row_factory = sqlite3.Row
        for pragma in (
            "PRAGMA journal_mode = WAL",
            "PRAGMA synchronous = NORMAL",
            "PRAGMA foreign_keys = ON",
            "PRAGMA busy_timeout = 5000",
            "PRAGMA temp_store = MEMORY",
        ):
            self._conn.execute(pragma)
        self._writing = False
        if migrate:
            apply_migrations(self._conn)

    # -- reads ---------------------------------------------------------------

    def read(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def read_one(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Params = ()) -> Any:
        row = self._conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    # -- writes --------------------------------------------------------------

    @contextlib.contextmanager
    def write(self) -> Iterator[WriteTx]:
        if self._writing:
            raise RuntimeError(
                "a write transaction is already open on this connection; "
                "never await inside `with db.write()`"
            )
        self._writing = True
        tx = WriteTx(self._conn)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield tx
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
        finally:
            tx._open = False
            self._writing = False

    # -- metadata ------------------------------------------------------------

    @property
    def instance_id(self) -> str:
        return self.scalar("SELECT instance_id FROM sync_state WHERE id = 1")

    def current_seq(self) -> int:
        return self.scalar("SELECT seq FROM sync_state WHERE id = 1")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
