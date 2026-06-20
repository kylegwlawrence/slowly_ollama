"""SQLite schema and database initialization for ollama_slowly.

Phase 2 owns the schema and a one-shot `initialize_database` function. Phase 3
will layer a shared long-lived connection on top; until then, this module
opens a private connection only long enough to create the file and tables.
"""

import sqlite3
from pathlib import Path

from app._time import now_iso
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
-- Phase 17: a project is the container above chats. Every conversation
-- belongs to exactly one project, and the project's `workspace_subdir`
-- (a slug under FILE_TOOL_ROOT) scopes the file tools to a per-project
-- directory. `default_model` / `default_agent` pre-fill the composer
-- for new chats in the project and are NOT applied retroactively to
-- existing chats.
CREATE TABLE IF NOT EXISTS projects (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    description       TEXT NOT NULL DEFAULT '',
    -- Path segment under FILE_TOOL_ROOT (a slug). UNIQUE so projects
    -- can't share a workspace. Set at create time; never edited.
    workspace_subdir  TEXT NOT NULL UNIQUE,
    -- NULL = no project default; new chats use the global default.
    default_model     TEXT,
    -- NULL = Normal (no agent) is the default; otherwise an agent name.
    default_agent     TEXT,
    -- Per-project override for the Ollama `num_ctx` (context window in
    -- tokens). NULL = inherit the global default from app_settings.
    num_ctx           INTEGER,
    -- Per-project system prompt prepended to Normal-chat turns in this
    -- project. Capped at 200 chars at the route layer. Empty string =
    -- no project prompt. Ignored on invoked-agent turns (the agent's
    -- own system prompt wins).
    system_prompt     TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

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
    -- Per-chat cap on tool-call iterations per turn (1–10).
    tool_iteration_cap INTEGER NOT NULL DEFAULT 5,
    -- Name of the selected Ollama host for this chat (a key in
    -- app.agents.AGENTS, e.g. "host2"), or NULL for the primary host.
    active_agent TEXT,
    -- Per-chat model for a non-primary host lives in `chat_host_models`
    -- (keyed by host name), not here. `model` above is the primary-host
    -- model, kept NOT NULL so a chat always has a valid model to fall back to.
    -- Phase 17: the project this chat belongs to. NOT NULL — every
    -- chat lives in a project; the migration ensures a "Default"
    -- project exists before this column is enforced.
    project_id   INTEGER NOT NULL
        REFERENCES projects(id) ON DELETE CASCADE,
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
    created_at      TEXT NOT NULL,
    -- Per-turn token counts reported by Ollama on the final stream chunk.
    -- NULL when the message isn't an assistant turn, when Ollama didn't
    -- report counts (e.g. prompt-cache hit), or for pre-existing rows
    -- from before this column existed. prompt_tokens is the input the
    -- model just saw (system + history + new user msg); eval_tokens is
    -- the output it generated. Use the most-recent turn's prompt_tokens
    -- as "current context size" — summing across turns double-counts.
    prompt_tokens   INTEGER,
    eval_tokens     INTEGER,
    -- Phase 18: ISO 8601 UTC stamp set when the manual-compact endpoint
    -- archives this row. NULL = active, included in the prompt sent to
    -- Ollama. Non-NULL = hidden from the prompt; the row stays in the DB
    -- so compaction is reversible. Rendering still shows archived rows
    -- (faded, behind a disclosure) so the user can audit what was hidden.
    archived_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
ON messages (conversation_id, created_at);

-- Phase 18's `idx_messages_active` partial index is created inside the
-- archived_at migration helper (`_ensure_messages_archived_at_column`)
-- rather than here. Reason: on legacy DBs the `CREATE TABLE IF NOT
-- EXISTS` above is a no-op, so the `archived_at` column doesn't exist
-- yet at the time this script runs — referencing it in a partial-index
-- predicate would error. The migration helper adds the column and the
-- index in the right order, and runs idempotently on fresh DBs too.

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

-- Global key/value app settings. One row per setting (e.g.
-- `default_temperature`). Settings reuse this table — no schema
-- migration needed when adding new keys; they appear/disappear via
-- INSERT/DELETE. Purely additive on existing DBs (CREATE TABLE IF
-- NOT EXISTS).
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

-- Per-chat model for each non-primary Ollama host the chat has picked a
-- model for. One row per (conversation_id, host_name); host_name matches a
-- key in app.agents.AGENTS (e.g. "host2"). A missing row means "use that
-- host's default model" (the host's `default_model` from config). The
-- primary host's model is NOT stored here — it lives in conversations.model.
-- Recording the host name (rather than a single conversations column) lets a
-- chat remember a distinct model per machine. Cascade-deletes with the chat.
CREATE TABLE IF NOT EXISTS chat_host_models (
    conversation_id INTEGER NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    host_name       TEXT NOT NULL,
    model           TEXT NOT NULL,
    PRIMARY KEY (conversation_id, host_name)
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


def _ensure_default_project(conn: sqlite3.Connection) -> int:
    """Ensure at least one project exists; return the Default project's id.

    Called as part of the projects migration. Idempotent: if any project
    already exists, returns the id of the lowest-id one (deterministic for
    tests); otherwise inserts a row named ``"Default"`` with
    ``workspace_subdir = "default"`` and returns its id.

    Args:
        conn: Open SQLite connection.

    Returns:
        The id of the Default (or first existing) project.
    """
    row = conn.execute(
        "SELECT id FROM projects ORDER BY id LIMIT 1;"
    ).fetchone()
    if row is not None:
        # row may be a Row or tuple depending on the connection's
        # row_factory; index by position to handle both.
        return row[0]
    now = now_iso()
    cursor = conn.execute(
        "INSERT INTO projects"
        " (name, description, workspace_subdir, default_model, default_agent,"
        "  created_at, updated_at)"
        " VALUES ('Default', '', 'default', NULL, NULL, ?, ?);",
        (now, now),
    )
    return cursor.lastrowid


def _ensure_conversations_project_id_column(
    conn: sqlite3.Connection, default_project_id: int
) -> None:
    """Add ``conversations.project_id`` and backfill it on legacy DBs.

    SQLite can't ``ALTER COLUMN`` to add NOT NULL to an existing column,
    so this uses the table-rewrite pattern: phase 1 adds the column as
    nullable + backfills with ``default_project_id``; phase 2 rebuilds
    the table with NOT NULL + FK ON DELETE CASCADE. Idempotent: detects
    the new column's presence and exits early.

    Args:
        conn: Open SQLite connection.
        default_project_id: Project id to assign to every existing
            conversation row.
    """
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(conversations);")
    }
    if "project_id" in columns:
        return
    # Phase 1: add the column as NULLable so we can backfill safely.
    conn.execute(
        "ALTER TABLE conversations ADD COLUMN project_id INTEGER;"
    )
    conn.execute(
        "UPDATE conversations SET project_id = ? WHERE project_id IS NULL;",
        (default_project_id,),
    )
    # Phase 2: table-rewrite to enforce NOT NULL + FK ON DELETE CASCADE.
    # executescript wraps the whole sequence in BEGIN/COMMIT so the swap
    # is atomic — if any step fails, the original table is preserved.
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE conversations_new (
            id           INTEGER PRIMARY KEY,
            name         TEXT NOT NULL,
            model        TEXT NOT NULL,
            name_locked  INTEGER NOT NULL DEFAULT 0,
            temperature  REAL NOT NULL DEFAULT 0.8,
            tool_iteration_cap INTEGER NOT NULL DEFAULT 5,
            active_agent TEXT,
            project_id   INTEGER NOT NULL
                REFERENCES projects(id) ON DELETE CASCADE,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        INSERT INTO conversations_new
            (id, name, model, name_locked, temperature, tool_iteration_cap,
             active_agent, project_id, created_at, updated_at)
        SELECT id, name, model, name_locked, temperature, tool_iteration_cap,
               active_agent, project_id, created_at, updated_at
          FROM conversations;
        DROP TABLE conversations;
        ALTER TABLE conversations_new RENAME TO conversations;
        COMMIT;
        """
    )


def _ensure_projects_num_ctx_column(conn: sqlite3.Connection) -> None:
    """Backfill the ``num_ctx`` column on projects tables that pre-date this phase.

    Nullable INTEGER with no default — existing projects come back as NULL,
    which the resolution helper reads as "inherit the global default".
    Mirrors the ``_ensure_*_column`` pattern: ``PRAGMA table_info`` check
    first so the ``ALTER TABLE`` is a no-op on fresh DBs where
    ``_SCHEMA_SQL`` already created the column.

    Args:
        conn: Open SQLite connection.
    """
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(projects);"
    )}
    if "num_ctx" not in columns:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN num_ctx INTEGER;"
        )


def _ensure_projects_system_prompt_column(conn: sqlite3.Connection) -> None:
    """Backfill the ``system_prompt`` column on projects tables that pre-date this phase.

    TEXT NOT NULL DEFAULT '' — existing projects come back as the empty
    string, which the generation layer reads as "no project prompt".
    Mirrors the other ``_ensure_*_column`` helpers: ``PRAGMA table_info``
    check first so the ``ALTER TABLE`` is a no-op on fresh DBs where
    ``_SCHEMA_SQL`` already created the column.

    Args:
        conn: Open SQLite connection.
    """
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(projects);"
    )}
    if "system_prompt" not in columns:
        conn.execute(
            "ALTER TABLE projects"
            " ADD COLUMN system_prompt TEXT NOT NULL DEFAULT '';"
        )


def _ensure_conversations_active_agent_column(conn: sqlite3.Connection) -> None:
    """Backfill the ``active_agent`` column on conversations tables that pre-date phase 16.

    Nullable with no default — existing chats come back as NULL, i.e. the
    Normal (plain-chat) agent. Mirrors the temperature / tool_iteration_cap
    backfills: ``PRAGMA table_info`` check first so the ``ALTER TABLE`` is a
    no-op on fresh DBs where ``_SCHEMA_SQL`` already created the column.

    Args:
        conn: Open SQLite connection.
    """
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(conversations);"
    )}
    if "active_agent" not in columns:
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN active_agent TEXT;"
        )


def _ensure_chat_host_models(conn: sqlite3.Connection) -> None:
    """Migrate the legacy ``conversations.slowly_model`` column to ``chat_host_models``.

    Replaces the hardcoded single ``slowly_model`` column with the generic
    per-host store. ``CREATE TABLE IF NOT EXISTS`` for ``chat_host_models``
    already ran in ``_SCHEMA_SQL`` on fresh DBs; this helper handles legacy DBs
    that still carry ``slowly_model``:

    1. Ensure the ``chat_host_models`` table exists (idempotent — covers DBs
       where ``_SCHEMA_SQL`` predates the table).
    2. Backfill every chat with a non-NULL ``slowly_model`` into a
       ``("host2", slowly_model)`` row. ``INSERT OR IGNORE`` makes a re-run a
       no-op (the PK collides).
    3. Drop the now-redundant ``slowly_model`` column. SQLite ≥ 3.35 supports
       ``DROP COLUMN``; Python 3.13 ships ≥ 3.40.

    Idempotent on fresh DBs (no ``slowly_model`` column → steps 2-3 skip) and on
    already-migrated DBs.

    Args:
        conn: Open SQLite connection.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat_host_models ("
        " conversation_id INTEGER NOT NULL"
        "   REFERENCES conversations(id) ON DELETE CASCADE,"
        " host_name TEXT NOT NULL,"
        " model TEXT NOT NULL,"
        " PRIMARY KEY (conversation_id, host_name));"
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(conversations);")
    }
    if "slowly_model" not in columns:
        return
    conn.execute(
        "INSERT OR IGNORE INTO chat_host_models (conversation_id, host_name, model)"
        " SELECT id, 'host2', slowly_model FROM conversations"
        " WHERE slowly_model IS NOT NULL;"
    )
    conn.execute("ALTER TABLE conversations DROP COLUMN slowly_model;")


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


def _ensure_messages_token_count_columns(conn: sqlite3.Connection) -> None:
    """Backfill ``prompt_tokens`` / ``eval_tokens`` on legacy messages tables.

    Nullable INTEGERs — existing rows come back as NULL (we don't have
    counts for messages persisted before the column existed). Mirrors
    the other ``_ensure_*_column`` helpers: ``PRAGMA table_info`` check
    first so the ``ALTER TABLE`` is a no-op on fresh DBs.

    Args:
        conn: Open SQLite connection.
    """
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(messages);"
    )}
    if "prompt_tokens" not in columns:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN prompt_tokens INTEGER;"
        )
    if "eval_tokens" not in columns:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN eval_tokens INTEGER;"
        )


def _ensure_messages_archived_at_column(conn: sqlite3.Connection) -> None:
    """Backfill the ``archived_at`` column on legacy messages tables.

    Phase 18. Nullable TEXT with no default — existing rows come back as
    NULL (i.e. active, included in the prompt). Mirrors the other
    ``_ensure_*_column`` helpers: ``PRAGMA table_info`` check first so the
    ``ALTER TABLE`` is a no-op on fresh DBs where ``_SCHEMA_SQL`` already
    created the column. Adds the partial index too if missing — both
    schema bits travel together so a legacy DB ends up with the same
    index a fresh one gets.

    Args:
        conn: Open SQLite connection.
    """
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(messages);"
    )}
    if "archived_at" not in columns:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN archived_at TEXT;"
        )
    # Partial index. ``CREATE INDEX IF NOT EXISTS`` is a no-op when the
    # index already exists; included here so legacy DBs get the index
    # the schema string creates on fresh installs.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_active"
        " ON messages (conversation_id, created_at)"
        " WHERE archived_at IS NULL;"
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
        # row_factory = Row so the projects migration helpers below can
        # index lookup rows by name as well as by position. Local to this
        # init connection; production connections set their own factory in
        # ``app.connection``.
        conn.row_factory = sqlite3.Row
        # executescript runs multiple `;`-separated statements; it issues an
        # implicit COMMIT first so DDL applies cleanly.
        conn.executescript(_SCHEMA_SQL)
        # One-shot migration for databases created before phase 11d.
        _ensure_name_locked_column(conn)
        # Phase 12a: drop the role CHECK on the legacy messages table
        # so tool_call / tool_result rows can be inserted.
        _migrate_messages_drop_role_check(conn)
        # Per-turn token counts: backfill the prompt_tokens / eval_tokens
        # columns on messages tables created before this phase. Runs
        # AFTER the role-check drop because that migration recreates
        # the table without the new columns.
        _ensure_messages_token_count_columns(conn)
        # Phase 18: backfill archived_at on messages tables created before
        # manual compaction existed. Same ordering rationale — runs after
        # the role-check recreate.
        _ensure_messages_archived_at_column(conn)
        # RAG source descriptions: backfill the description column on
        # rag_servers tables created before this phase.
        _ensure_rag_servers_description_column(conn)
        # Per-chat temperature: backfill the temperature column on
        # conversations tables created before this phase.
        _ensure_conversations_temperature_column(conn)
        # Per-chat tool-iteration cap: backfill the tool_iteration_cap
        # column on conversations tables created before this phase.
        _ensure_conversations_tool_iteration_cap_column(conn)
        # Phase 16: backfill the active_agent column on conversations
        # tables created before user-invoked agents existed.
        _ensure_conversations_active_agent_column(conn)
        # Phase 17: ensure a "Default" project exists, then add
        # conversations.project_id (NOT NULL FK) and backfill every
        # legacy chat to point at Default.
        default_project_id = _ensure_default_project(conn)
        _ensure_conversations_project_id_column(conn, default_project_id)
        # Per-chat host model store: create chat_host_models and migrate any
        # legacy conversations.slowly_model column into it. MUST run AFTER the
        # project_id migration — that step rebuilds the conversations table
        # (legacy DBs) and only preserves columns it knows about, so the
        # slowly_model it carries over is what this step reads + drops.
        _ensure_chat_host_models(conn)
        # Per-project Ollama context-window override: backfill the
        # num_ctx column on projects tables created before this phase.
        _ensure_projects_num_ctx_column(conn)
        # Per-project system prompt: backfill the system_prompt column
        # on projects tables created before this phase.
        _ensure_projects_system_prompt_column(conn)

    return target
