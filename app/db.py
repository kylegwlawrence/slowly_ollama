"""SQLite schema and database initialization for ollama_slowly.

Phase 2 owns the schema and a one-shot `initialize_database` function. Phase 3
will layer a shared long-lived connection on top; until then, this module
opens a private connection only long enough to create the file and tables.
"""

import sqlite3
from pathlib import Path

from app.config import db_path

# All schema lives in one string so the file reads top-to-bottom and so
# `executescript` can apply it in a single call.
#
# Design notes:
# - id columns: plain INTEGER PRIMARY KEY — SQLite auto-assigns rowids;
#   sufficient for a single-user local app.
# - timestamps: ISO 8601 TEXT in UTC. Lexicographic sort = chronological sort,
#   and values stay human-readable when poking around with the `sqlite3` CLI.
#   Phase 4 query code is responsible for supplying these values; we
#   deliberately do not use SQLite DEFAULT so all timestamp creation goes
#   through one Python codepath.
# - messages.conversation_id: FK with ON DELETE CASCADE so deleting a
#   conversation cleans up its messages. Note: FK enforcement is OFF by
#   default in SQLite — every connection must opt in via PRAGMA.
# - role CHECK: limited to v1's two roles. Add 'system' here when/if a
#   system-prompt feature is introduced (currently a non-goal per PLAN.md).
# - composite index on messages(conversation_id, created_at): supports the
#   primary read pattern, "give me this conversation's messages in order."
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    model       TEXT NOT NULL,
    -- Phase 11d: when 1, the auto-titler must leave the name alone.
    -- Set to 1 by `rename_conversation` so a manual rename always wins
    -- over a subsequent automated title refresh.
    name_locked INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
ON messages (conversation_id, created_at);
"""


def _ensure_name_locked_column(conn: sqlite3.Connection) -> None:
    """Backfill the `name_locked` column on databases that pre-date 11d.

    `CREATE TABLE IF NOT EXISTS` is a no-op when the table exists, even
    with a different schema, so adding a column to the SQL above
    doesn't reach existing databases. Apply the change via `ALTER TABLE
    ADD COLUMN`, guarded by a `PRAGMA table_info` check so re-runs are
    safe.

    Args:
        conn: Open SQLite connection.
    """
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(conversations);"
    )}
    if "name_locked" not in columns:
        conn.execute(
            "ALTER TABLE conversations"
            " ADD COLUMN name_locked INTEGER NOT NULL DEFAULT 0;"
        )


def initialize_database(path: Path | None = None) -> Path:
    """Create the database file and schema if they don't already exist.

    Safe to call repeatedly: `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF
    NOT EXISTS` are no-ops once the objects are present.

    Args:
        path: Where to put the database. Defaults to the DB_PATH value from
            .env (resolved fresh on each call). The parameter exists
            primarily so tests can point at a tempfile.

    Returns:
        The path the database was created at.
    """
    target = path if path is not None else db_path()

    # parents=True creates Application Support/ and ollama_slowly/ as needed;
    # exist_ok=True makes this a no-op after the first run.
    target.parent.mkdir(parents=True, exist_ok=True)

    # sqlite3.Connection's context manager commits/rolls back on exit but does
    # NOT close the connection — close happens via CPython GC when `conn`
    # falls out of scope at function return. Acceptable for a one-shot init;
    # Phase 3 will manage a long-lived connection explicitly.
    with sqlite3.connect(target) as conn:
        # FK enforcement is per-connection. Setting it here documents intent
        # for this init connection; every connection Phase 3+ opens must set
        # it again, otherwise REFERENCES clauses become documentation-only.
        conn.execute("PRAGMA foreign_keys = ON;")
        # executescript runs multiple `;`-separated statements; it issues an
        # implicit COMMIT first so DDL applies cleanly.
        conn.executescript(_SCHEMA_SQL)
        # One-shot migration for databases created before phase 11d.
        _ensure_name_locked_column(conn)

    return target
