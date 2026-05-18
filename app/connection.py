"""Long-lived database connection factory for ollama_slowly.

Phase 3: opens a SQLite connection with the pragmas and settings the rest of
the app expects. Phase 6 will hold one instance via FastAPI's lifespan and
expose it as a dependency; until then, callers (e.g. tests) open and close
their own.
"""

import sqlite3
from pathlib import Path

from app.db import DEFAULT_DB_PATH


def open_connection(path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with the app's standard configuration.

    Sets, in one place:

    - ``PRAGMA foreign_keys = ON`` — per-connection in SQLite and OFF by
      default; without it ``REFERENCES`` clauses become documentation-only
      and ``ON DELETE CASCADE`` silently stops working.
    - ``PRAGMA journal_mode = WAL`` — Write-Ahead Logging lets readers run
      concurrently with the single writer, which matters once Phase 5
      starts streaming assistant responses while the UI may read other
      conversations. WAL is persistent on the file, so re-applying it on
      later connections is a no-op.
    - ``row_factory = sqlite3.Row`` — rows are addressable by column name
      as well as by index. Phase 4's dataclass mapping reads cleaner with
      this on.
    - ``check_same_thread=False`` — allows the connection to be used from
      threads other than the one that opened it. FastAPI runs sync
      endpoints in a threadpool, so without this every cross-thread call
      would raise. SQLite's default "serialized" threading mode keeps
      concurrent calls safe: they queue, they don't corrupt. For a
      single-user local app the per-call serialization is irrelevant.

    Args:
        path: Where the database file lives. Defaults to
            ``DEFAULT_DB_PATH``; tests should pass an explicit path.

    Returns:
        A configured ``sqlite3.Connection``. The caller owns its
        lifecycle — typically Phase 6's FastAPI lifespan holds a single
        instance for the duration of the app and closes it on shutdown.
    """
    db_path = path if path is not None else DEFAULT_DB_PATH

    # check_same_thread=False relaxes Python's sqlite3 module guard; SQLite
    # itself (in default "serialized" threading mode) remains responsible
    # for concurrent-call safety on the connection object.
    conn = sqlite3.connect(db_path, check_same_thread=False)

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.row_factory = sqlite3.Row
    return conn
