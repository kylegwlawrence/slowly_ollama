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
# - messages.role: no CHECK constraint as of phase 12a. Validation lives in
#   `app.queries.Role` (a typing.Literal). Tool-calling adds two new roles
#   (`tool_call`, `tool_result`) and we expect more in future phases; the
#   Python-level enum avoids painful SQLite ALTER TABLE migrations each time.
# - composite index on messages(conversation_id, created_at): supports the
#   primary read pattern, "give me this conversation's messages in order."
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    model        TEXT NOT NULL,
    -- Phase 11d: when 1, the auto-titler must leave the name alone.
    -- Set to 1 by `rename_conversation` so a manual rename always wins
    -- over a subsequent automated title refresh.
    name_locked  INTEGER NOT NULL DEFAULT 0,
    -- Per-chat temperature passed to Ollama's options dict (0.0–2.0).
    -- Ollama's own default is 0.8.
    temperature  REAL NOT NULL DEFAULT 0.8,
    -- Per-chat cap on single-agent tool-call iterations per turn (1–10).
    -- The agentic loop's caps are separate and not stored here.
    tool_iteration_cap INTEGER NOT NULL DEFAULT 5,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- The role CHECK has been removed (phase 12a). Validation now lives
-- in app.queries.Role (a typing.Literal). SQLite can't ALTER an
-- existing CHECK, so this only takes effect for fresh DBs; existing
-- DBs are migrated by _migrate_messages_drop_role_check below.
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
ON messages (conversation_id, created_at);

-- Phase 12a: configured RAG endpoints. Each row is one source the
-- chat model can query via the query_rag tool. `url` is the FULL
-- base URL up through the source prefix (e.g.
-- "http://10.0.0.5:8002/arxiv"); the tool appends "/chunks" itself.
CREATE TABLE IF NOT EXISTS rag_servers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Phase 13: global key/value app settings. One row per setting.
-- Currently the only key is `agentic_mode` ("on" or "off"); future
-- settings reuse the table. No schema migration needed when adding
-- new keys — they appear/disappear via INSERT/DELETE. Purely
-- additive on existing DBs (CREATE TABLE IF NOT EXISTS).
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Phase 15: per-chat tool enablement. One row per (conversation_id, tool_name).
-- A missing row means enabled (unseeded chats default to all tools on).
-- Cascade-deletes with the parent conversation.
CREATE TABLE IF NOT EXISTS chat_tool_settings (
    conversation_id INTEGER NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    tool_name       TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (conversation_id, tool_name)
);

-- Phase 15b: per-chat RAG server enablement. One row per (conversation_id, server_name).
-- A missing row means enabled (default on). Cascade-deletes with the parent conversation.
-- server_name matches rag_servers.name — no FK enforced; server deletions orphan rows
-- that are harmlessly ignored on lookup.
CREATE TABLE IF NOT EXISTS chat_rag_settings (
    conversation_id INTEGER NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    server_name     TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (conversation_id, server_name)
);
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


def _ensure_conversations_temperature_column(conn: sqlite3.Connection) -> None:
    """Backfill the ``temperature`` column on conversations tables that pre-date this phase.

    Args:
        conn: Open SQLite connection.
    """
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(conversations);"
    )}
    if "temperature" not in columns:
        conn.execute(
            "ALTER TABLE conversations"
            " ADD COLUMN temperature REAL NOT NULL DEFAULT 0.8;"
        )


def _ensure_conversations_tool_iteration_cap_column(conn: sqlite3.Connection) -> None:
    """Backfill the ``tool_iteration_cap`` column on conversations tables that pre-date this phase.

    Args:
        conn: Open SQLite connection.
    """
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(conversations);"
    )}
    if "tool_iteration_cap" not in columns:
        conn.execute(
            "ALTER TABLE conversations"
            " ADD COLUMN tool_iteration_cap INTEGER NOT NULL DEFAULT 5;"
        )


def _ensure_rag_servers_description_column(conn: sqlite3.Connection) -> None:
    """Backfill the ``description`` column on rag_servers tables that pre-date this phase.

    Mirrors the ``_ensure_name_locked_column`` pattern: ``PRAGMA table_info``
    check first so the ``ALTER TABLE`` is a no-op on fresh DBs where
    ``_SCHEMA_SQL`` already created the column.

    Args:
        conn: Open SQLite connection.
    """
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(rag_servers);"
    )}
    if "description" not in columns:
        conn.execute(
            "ALTER TABLE rag_servers"
            " ADD COLUMN description TEXT NOT NULL DEFAULT '';"
        )


def _migrate_messages_drop_role_check(conn: sqlite3.Connection) -> None:
    """Drop the role CHECK from an existing messages table.

    The original schema (phases 2-11) had `CHECK (role IN ('user',
    'assistant'))` on `messages.role`. Phase 12a expands the allowed
    roles to include `tool_call` and `tool_result`; the cleanest
    approach is to drop the CHECK entirely and let the Python `Role`
    literal enforce validity at the app layer.

    SQLite has no `ALTER TABLE ... DROP CONSTRAINT`. The portable
    workaround is to recreate the table without the CHECK and copy
    rows over. Idempotent: re-running detects the absence of the
    CHECK in `sqlite_master` and exits early.

    Args:
        conn: Open SQLite connection.
    """
    # sqlite_master.sql holds the original CREATE TABLE text. If the
    # word "CHECK" is missing, either the table doesn't exist yet
    # (fresh DB — the CREATE TABLE in _SCHEMA_SQL already produced a
    # CHECK-free table) or we already migrated. Either way: skip.
    row = conn.execute(
        "SELECT sql FROM sqlite_master"
        " WHERE type='table' AND name='messages';"
    ).fetchone()
    if row is None or "CHECK" not in (row[0] or ""):
        return
    # Table-recreate dance: build messages_new with the new schema,
    # copy data, drop the original, rename. executescript wraps the
    # whole thing in BEGIN/COMMIT so the swap is atomic — if any
    # step fails partway, the original table is preserved.
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE messages_new (
            id              INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL
                REFERENCES conversations(id) ON DELETE CASCADE,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            created_at      TEXT NOT NULL
        );
        INSERT INTO messages_new (id, conversation_id, role, content, created_at)
            SELECT id, conversation_id, role, content, created_at FROM messages;
        DROP TABLE messages;
        ALTER TABLE messages_new RENAME TO messages;
        CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
            ON messages (conversation_id, created_at);
        COMMIT;
        """
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
        # Phase 12a: drop the role CHECK on the legacy messages table
        # so tool_call / tool_result rows can be inserted.
        _migrate_messages_drop_role_check(conn)
        # RAG source descriptions: backfill the description column on
        # rag_servers tables created before this phase.
        _ensure_rag_servers_description_column(conn)
        # Per-chat temperature: backfill the temperature column on
        # conversations tables created before this phase.
        _ensure_conversations_temperature_column(conn)
        # Per-chat tool-iteration cap: backfill the tool_iteration_cap
        # column on conversations tables created before this phase.
        _ensure_conversations_tool_iteration_cap_column(conn)

    return target
