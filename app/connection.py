"""SQLite connection factory.

Opens a connection with the pragmas and settings the rest of the app
expects. The FastAPI lifespan holds one instance for the app's lifetime;
other callers (e.g. tests, one-shot tools) open and close their own.
"""

import sqlite3
from pathlib import Path

from app.config import db_path


def open_connection(path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with the app's standard configuration.

    Sets, in one place:

    - ``PRAGMA foreign_keys = ON`` — per-connection and OFF by default;
      without it ``REFERENCES`` and ``ON DELETE CASCADE`` silently no-op.
    - ``PRAGMA journal_mode = WAL`` — lets readers run concurrently with the
      single writer (the UI reads while a response streams). WAL is
      persistent on the file, so re-applying it later is a no-op.
    - ``row_factory = sqlite3.Row`` — rows addressable by column name.
    - ``check_same_thread=False`` — FastAPI runs sync endpoints in a
      threadpool, so the connection is used across threads. SQLite's default
      "serialized" mode keeps concurrent calls safe (they queue, they don't
      corrupt); the per-call serialization is irrelevant for one local user.

    Args:
        path: Database file location. Defaults to the `DB_PATH` value from
            `.env` (resolved fresh each call); tests should pass an explicit
            path.

    Returns:
        A configured ``sqlite3.Connection``. The caller owns its lifecycle.
    """
    target = path if path is not None else db_path()

    # check_same_thread=False relaxes Python's sqlite3 guard; SQLite itself
    # (serialized threading mode) stays responsible for concurrent-call safety.
    conn = sqlite3.connect(target, check_same_thread=False)

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.row_factory = sqlite3.Row
    return conn
