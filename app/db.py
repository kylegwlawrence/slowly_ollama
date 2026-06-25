"""SQLite schema and database initialization.

Owns `_SCHEMA_SQL` (fresh-install schema) and `initialize_database`, which
applies it plus a chain of idempotent migrations. Opens a private
connection only long enough to create the file and tables; the long-lived
app connection lives in `app.connection`.
"""

import sqlite3
from pathlib import Path

from app._time import now_iso
from app.config import db_path

# All schema in one string so `executescript` applies it in a single call.
#
# Conventions:
# - id columns: plain INTEGER PRIMARY KEY (SQLite rowid); fine for one user.
# - timestamps: ISO 8601 TEXT in UTC — lexicographic sort = chronological,
#   stays readable in the sqlite3 CLI. Supplied by Python (no SQLite DEFAULT)
#   so all timestamps go through one codepath.
# - FKs use ON DELETE CASCADE, but enforcement is OFF by default in SQLite —
#   every connection must opt in via PRAGMA.
# - messages.role has no CHECK constraint; validation lives in
#   `app.queries.Role` (a typing.Literal), avoiding an ALTER TABLE each time
#   a new role is added.
_SCHEMA_SQL = """
-- A project is the container above chats. Every conversation belongs to
-- exactly one project; `workspace_subdir` (a slug under FILE_TOOL_ROOT)
-- scopes the file tools to a per-project directory. `default_model` /
-- `default_agent` pre-fill the composer for new chats only — not applied
-- retroactively.
CREATE TABLE IF NOT EXISTS projects (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    description       TEXT NOT NULL DEFAULT '',
    -- Path segment under FILE_TOOL_ROOT (a slug). UNIQUE so projects can't
    -- share a workspace. Set at create time; never edited.
    workspace_subdir  TEXT NOT NULL UNIQUE,
    -- NULL = no project default; new chats use the global default.
    default_model     TEXT,
    -- NULL = Normal (no agent) is the default; otherwise an agent name.
    default_agent     TEXT,
    -- Ollama `num_ctx` override (context tokens). NULL = inherit global.
    num_ctx           INTEGER,
    -- System prompt prepended to Normal-chat turns (≤SYSTEM_PROMPT_MAX_CHARS,
    -- capped at the route layer). '' = none. Ignored on agent turns (agent wins).
    system_prompt     TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    model        TEXT NOT NULL,
    -- When 1, the auto-titler leaves the name alone. Set by
    -- `rename_conversation` so a manual rename beats an automated refresh.
    name_locked  INTEGER NOT NULL DEFAULT 0,
    -- Ollama temperature option (0.0–2.0; Ollama's own default is 0.8).
    temperature  REAL NOT NULL DEFAULT 0.8,
    -- Cap on tool-call iterations per turn (1–10).
    tool_iteration_cap INTEGER NOT NULL DEFAULT 5,
    -- Thinking mode. 'default' omits Ollama's `think` key (model decides);
    -- 'off' sends think=false to suppress a reasoning model's <think> phase.
    -- TEXT (not bool) to leave room for 'on'/graduated levels without a
    -- migration. Surfaced only for models whose /api/show lists "thinking".
    think_mode   TEXT NOT NULL DEFAULT 'default',
    -- Selected host (a key in app.hosts.HOSTS, e.g. "host2"), or NULL for
    -- the primary host. A non-primary host's model lives in
    -- `chat_host_models`; `model` above is the primary-host model, NOT NULL
    -- so a chat always has a valid fallback.
    active_host TEXT,
    -- Owning project. NOT NULL — the migration ensures a "Default" project
    -- exists before enforcing this.
    project_id   INTEGER NOT NULL
        REFERENCES projects(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- No role CHECK — validation lives in app.queries.Role. SQLite can't ALTER
-- an existing CHECK, so this is fresh-DB only; legacy DBs are migrated by
-- _migrate_messages_drop_role_check below.
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    -- Per-turn token counts from Ollama's final stream chunk. NULL for
    -- non-assistant turns, unreported counts (e.g. prompt-cache hit), or
    -- pre-existing rows. prompt_tokens = input the model saw (system +
    -- history + new user msg); eval_tokens = output generated. The latest
    -- turn's prompt_tokens is "current context size" — summing double-counts.
    prompt_tokens   INTEGER,
    eval_tokens     INTEGER,
    -- A thinking model's streamed reasoning for this assistant turn (the
    -- final stream_chat call, after any tool loop). NULL on non-assistant
    -- rows, pre-existing rows, and turns where the model didn't reason
    -- (think off, or a non-thinking model). Rebuilt into a collapsed card
    -- above the bubble on historic render.
    thinking        TEXT,
    -- ISO 8601 UTC stamp set when manual compaction archives this row.
    -- NULL = active (sent to Ollama). Non-NULL = hidden from the prompt but
    -- kept in the DB (reversible) and still rendered faded behind a
    -- disclosure so the user can audit what was hidden.
    archived_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
ON messages (conversation_id, created_at);

-- The `idx_messages_active` partial index lives in the archived_at
-- migration helper, not here: on a legacy DB the CREATE TABLE above is a
-- no-op, so `archived_at` doesn't exist yet and a partial-index predicate
-- referencing it would error. The helper adds column + index in order and
-- is idempotent on fresh DBs too.

-- Configured RAG endpoints, one per source queryable via query_rag. `url`
-- is the FULL base URL through the source prefix (e.g.
-- "http://10.0.0.5:8002/arxiv"); the tool appends "/chunks".
CREATE TABLE IF NOT EXISTS rag_servers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Global key/value app settings, one row per setting (e.g.
-- `default_temperature`). New keys appear/disappear via INSERT/DELETE — no
-- schema migration needed.
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Per-chat model for each non-primary host the chat has picked one for. One
-- row per (conversation_id, host_name); host_name is a key in
-- app.hosts.HOSTS (e.g. "host2"). A missing row means "use that host's
-- default model". The primary host's model lives in conversations.model, not
-- here. Cascade-deletes with the chat.
CREATE TABLE IF NOT EXISTS chat_host_models (
    conversation_id INTEGER NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    host_name       TEXT NOT NULL,
    model           TEXT NOT NULL,
    PRIMARY KEY (conversation_id, host_name)
);
"""


def _ensure_name_locked_column(conn: sqlite3.Connection) -> None:
    """Add `conversations.name_locked` on legacy DBs.

    `CREATE TABLE IF NOT EXISTS` won't alter an existing table, so new
    columns reach legacy DBs via `ALTER TABLE ADD COLUMN`, guarded by a
    `PRAGMA table_info` check for idempotency. This is the shared pattern
    for every `_ensure_*_column` helper below.

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
    """Add ``conversations.temperature`` on legacy DBs. See
    :func:`_ensure_name_locked_column`.

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
    """Add ``conversations.tool_iteration_cap`` on legacy DBs. See
    :func:`_ensure_name_locked_column`.

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


def _ensure_conversations_think_mode_column(conn: sqlite3.Connection) -> None:
    """Add ``conversations.think_mode`` on legacy DBs.

    Backfilled rows default to ``'default'`` (Ollama's ``think`` key
    omitted), preserving existing behaviour. See
    :func:`_ensure_name_locked_column`.

    Args:
        conn: Open SQLite connection.
    """
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(conversations);"
    )}
    if "think_mode" not in columns:
        conn.execute(
            "ALTER TABLE conversations"
            " ADD COLUMN think_mode TEXT NOT NULL DEFAULT 'default';"
        )


def _ensure_default_project(conn: sqlite3.Connection) -> int:
    """Ensure at least one project exists; return the Default project's id.

    Idempotent: if any project exists, returns the lowest id (deterministic
    for tests); otherwise inserts ``"Default"`` (``workspace_subdir =
    "default"``) and returns its id.

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
    """Add ``conversations.project_id`` (NOT NULL FK) and backfill it.

    SQLite can't add a NOT NULL column to an existing table, so this uses
    the table-rewrite pattern: step 1 adds it nullable + backfills with
    ``default_project_id``; step 2 rebuilds the table with NOT NULL + FK ON
    DELETE CASCADE. Idempotent: exits early if the column already exists.

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
    # Step 1: add nullable so we can backfill safely.
    conn.execute(
        "ALTER TABLE conversations ADD COLUMN project_id INTEGER;"
    )
    conn.execute(
        "UPDATE conversations SET project_id = ? WHERE project_id IS NULL;",
        (default_project_id,),
    )
    # Step 2: table-rewrite to enforce NOT NULL + FK. executescript wraps
    # the swap in BEGIN/COMMIT so it's atomic — a failure preserves the
    # original table.
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
            active_host TEXT,
            project_id   INTEGER NOT NULL
                REFERENCES projects(id) ON DELETE CASCADE,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        INSERT INTO conversations_new
            (id, name, model, name_locked, temperature, tool_iteration_cap,
             active_host, project_id, created_at, updated_at)
        SELECT id, name, model, name_locked, temperature, tool_iteration_cap,
               active_host, project_id, created_at, updated_at
          FROM conversations;
        DROP TABLE conversations;
        ALTER TABLE conversations_new RENAME TO conversations;
        COMMIT;
        """
    )


def _ensure_projects_num_ctx_column(conn: sqlite3.Connection) -> None:
    """Add ``projects.num_ctx`` on legacy DBs.

    Nullable INTEGER, no default — existing projects read back as NULL
    ("inherit the global default"). See :func:`_ensure_name_locked_column`.

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
    """Add ``projects.system_prompt`` on legacy DBs.

    TEXT NOT NULL DEFAULT '' — existing projects read back as '' ("no
    project prompt"). See :func:`_ensure_name_locked_column`.

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


def _ensure_conversations_active_host_column(conn: sqlite3.Connection) -> None:
    """Ensure ``conversations.active_host`` exists, migrating from ``active_agent``.

    Three idempotent cases (``PRAGMA table_info`` check first):

    1. ``active_host`` present → no-op.
    2. Misnamed ``active_agent`` present (legacy host-picker store) →
       ``RENAME COLUMN`` in place, preserving selections.
    3. Neither present → add ``active_host`` nullable (NULL = primary host).

    Runs BEFORE the project_id rebuild, which copies ``active_host`` — so on
    a legacy DB the rename happens first.

    Args:
        conn: Open SQLite connection.
    """
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(conversations);"
    )}
    if "active_host" in columns:
        return
    if "active_agent" in columns:
        conn.execute(
            "ALTER TABLE conversations"
            " RENAME COLUMN active_agent TO active_host;"
        )
    else:
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN active_host TEXT;"
        )


def _ensure_chat_host_models(conn: sqlite3.Connection) -> None:
    """Migrate the legacy ``conversations.slowly_model`` column to ``chat_host_models``.

    Replaces the single ``slowly_model`` column with the generic per-host
    store:

    1. Ensure ``chat_host_models`` exists (covers DBs whose ``_SCHEMA_SQL``
       predates the table).
    2. Backfill each non-NULL ``slowly_model`` into a ``("host2", model)``
       row. ``INSERT OR IGNORE`` makes a re-run a no-op (PK collides).
    3. Drop ``slowly_model``.

    Idempotent on fresh DBs (no ``slowly_model`` → steps 2-3 skip) and on
    already-migrated ones.

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
    """Add ``rag_servers.description`` on legacy DBs. See
    :func:`_ensure_name_locked_column`.

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
    """Add ``messages.prompt_tokens`` / ``eval_tokens`` on legacy DBs.

    Nullable INTEGERs — pre-existing rows read back as NULL (no counts
    recorded). See :func:`_ensure_name_locked_column`.

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


def _ensure_messages_thinking_column(conn: sqlite3.Connection) -> None:
    """Add ``messages.thinking`` on legacy DBs.

    Nullable TEXT, no default — pre-existing rows read back as NULL (no
    reasoning captured). Mirrors :func:`_ensure_messages_token_count_columns`.
    Must run AFTER :func:`_migrate_messages_drop_role_check`, which rebuilds
    ``messages`` from a fixed column list that omits ``thinking``.

    Args:
        conn: Open SQLite connection.
    """
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(messages);"
    )}
    if "thinking" not in columns:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN thinking TEXT;"
        )


def _ensure_messages_archived_at_column(conn: sqlite3.Connection) -> None:
    """Add ``messages.archived_at`` (and its partial index) on legacy DBs.

    Nullable TEXT, no default — existing rows read back as NULL (active).
    Also creates ``idx_messages_active`` if missing, so a legacy DB ends up
    with the same index a fresh one gets. See
    :func:`_ensure_name_locked_column`.

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
    # Partial index — here (not in _SCHEMA_SQL) so legacy DBs get it too,
    # after the column above exists. IF NOT EXISTS makes it a no-op on rerun.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_active"
        " ON messages (conversation_id, created_at)"
        " WHERE archived_at IS NULL;"
    )


def _migrate_messages_drop_role_check(conn: sqlite3.Connection) -> None:
    """Drop the legacy role CHECK from an existing messages table.

    The original schema had `CHECK (role IN ('user', 'assistant'))`, which
    blocks the newer `tool_call` / `tool_result` roles. SQLite has no `DROP
    CONSTRAINT`, so recreate the table without the CHECK and copy rows over;
    validity is now enforced by the Python `Role` literal. Idempotent: exits
    early when `sqlite_master` shows no CHECK.

    Args:
        conn: Open SQLite connection.
    """
    # sqlite_master.sql holds the CREATE TABLE text. No "CHECK" means either a
    # fresh DB (already CHECK-free) or an already-migrated one — skip.
    row = conn.execute(
        "SELECT sql FROM sqlite_master"
        " WHERE type='table' AND name='messages';"
    ).fetchone()
    if row is None or "CHECK" not in (row[0] or ""):
        return
    # Table-recreate: build messages_new, copy, drop, rename. executescript
    # wraps it in BEGIN/COMMIT so the swap is atomic — a failure preserves the
    # original.
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

    Safe to call repeatedly: the schema uses `IF NOT EXISTS` and the
    migrations are idempotent.

    Args:
        path: Where to put the database. Defaults to the DB_PATH value from
            .env (resolved fresh each call); tests pass a tempfile.

    Returns:
        The path the database was created at.
    """
    target = path if path is not None else db_path()

    # Create the parent dirs; no-op after the first run.
    target.parent.mkdir(parents=True, exist_ok=True)

    # The context manager commits/rolls back on exit but does NOT close — the
    # connection is GC'd when it falls out of scope. Fine for a one-shot init.
    with sqlite3.connect(target) as conn:
        # FK enforcement and row_factory are per-connection; set them for this
        # init connection (the migration helpers index rows positionally, but
        # Row is harmless). Production connections configure their own.
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA_SQL)

        # Migrations for legacy DBs. ORDER MATTERS where noted — the
        # project_id rebuild recreates the conversations table from a fixed
        # column list, so anything that column-rebuild must carry has to run
        # before it, and anything it would drop has to run after.
        _ensure_name_locked_column(conn)
        # Before token-count/archived_at: the role-check drop recreates the
        # messages table without those columns.
        _migrate_messages_drop_role_check(conn)
        _ensure_messages_token_count_columns(conn)
        _ensure_messages_thinking_column(conn)
        _ensure_messages_archived_at_column(conn)
        _ensure_rag_servers_description_column(conn)
        _ensure_conversations_temperature_column(conn)
        _ensure_conversations_tool_iteration_cap_column(conn)
        # Before the project_id rebuild, which copies active_host.
        _ensure_conversations_active_host_column(conn)
        default_project_id = _ensure_default_project(conn)
        _ensure_conversations_project_id_column(conn, default_project_id)
        # After the project_id rebuild — it would otherwise drop a think_mode
        # added beforehand (fixed column list).
        _ensure_conversations_think_mode_column(conn)
        # After the project_id rebuild — that step carries slowly_model
        # forward, and this one reads then drops it.
        _ensure_chat_host_models(conn)
        _ensure_projects_num_ctx_column(conn)
        _ensure_projects_system_prompt_column(conn)

    return target
