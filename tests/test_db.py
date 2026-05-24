"""Tests for Phase 2: schema creation and database initialization.

Every test writes to pytest's `tmp_path` — none touch the real Application
Support location. The `initialized_db` fixture handles the common
boilerplate of "give me a freshly-initialized DB at a throwaway path."
"""

import sqlite3
from pathlib import Path

import pytest

from app.db import initialize_database


def _open(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with FK enforcement on.

    Mirrors what production code will do in Phase 3. Tests use this helper
    so cascade-delete and FK behavior get exercised under the same pragma
    settings the real app will use.

    Args:
        path: Path to an existing SQLite database file.

    Returns:
        An open connection with `PRAGMA foreign_keys = ON` applied.
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@pytest.fixture
def initialized_db(tmp_path: Path) -> Path:
    """A path to a freshly-initialized DB inside pytest's per-test tmp dir.

    Args:
        tmp_path: Pytest's built-in per-test temp directory fixture.

    Returns:
        Path to a `chats.db` whose schema has been applied.
    """
    path = tmp_path / "chats.db"
    initialize_database(path)
    return path


def test_initialize_creates_file_and_parent_dirs(tmp_path: Path) -> None:
    """First-run initialization creates any missing parent directories.

    Models the real first-run scenario: the user has no Application
    Support/ollama_slowly/ directory yet.
    """
    nested = tmp_path / "does" / "not" / "exist" / "chats.db"
    assert not nested.parent.exists()

    initialize_database(nested)

    assert nested.exists()


def test_initialize_is_idempotent(initialized_db: Path) -> None:
    """Re-running initialize_database on an existing DB does nothing harmful."""
    # Second call must not raise; all expected tables must still be present.
    initialize_database(initialized_db)

    with _open(initialized_db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table';"
            )
        }
    # Phase 12a added rag_servers; phase 13a added app_settings;
    # phase 15 added chat_tool_settings; phase 15b added chat_rag_settings;
    # conversations + messages predate all.
    assert tables == {
        "conversations",
        "messages",
        "rag_servers",
        "app_settings",
        "chat_tool_settings",
        "chat_rag_settings",
        # Phase 17 added the projects table.
        "projects",
    }


def test_role_accepts_documented_roles(initialized_db: Path) -> None:
    """The schema accepts current + legacy roles without error.

    Phase 12a dropped the SQLite CHECK, so validity is enforced only in
    Python (`Role`). This smoke test inserts the current roles plus the
    removed agentic loop's `research_findings` / `review_verdict` — legacy
    rows must still insert cleanly so an old DB doesn't error on read.
    """
    with _open(initialized_db) as conn:
        # Phase 17: every chat needs a project_id. The Default project the
        # migration created has id 1 (the lowest); use it here.
        default_pid = conn.execute(
            "SELECT id FROM projects ORDER BY id LIMIT 1;"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO conversations"
            " (id, name, model, project_id, created_at, updated_at)"
            " VALUES (1, 'c', 'llama3', ?, '2025-01-01', '2025-01-01');",
            (default_pid,),
        )
        for role in (
            "user",
            "assistant",
            "tool_call",
            "tool_result",
            "research_findings",
            "review_verdict",
        ):
            conn.execute(
                "INSERT INTO messages"
                " (conversation_id, role, content, created_at)"
                " VALUES (1, ?, 'hi', '2025-01-01');",
                (role,),
            )


def test_cascade_delete_removes_child_messages(initialized_db: Path) -> None:
    """Deleting a conversation removes its messages via ON DELETE CASCADE.

    Only works because `_open` enables `PRAGMA foreign_keys = ON`. If a
    future change drops that pragma, this test fails — which is the point.
    """
    with _open(initialized_db) as conn:
        # Phase 17: every chat needs a project_id; reuse the Default project.
        default_pid = conn.execute(
            "SELECT id FROM projects ORDER BY id LIMIT 1;"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO conversations"
            " (id, name, model, project_id, created_at, updated_at)"
            " VALUES (1, 'c', 'llama3', ?, '2025-01-01', '2025-01-01');",
            (default_pid,),
        )
        conn.execute(
            "INSERT INTO messages"
            " (conversation_id, role, content, created_at)"
            " VALUES (1, 'user', 'hi', '2025-01-01');"
        )
        conn.execute("DELETE FROM conversations WHERE id = 1;")
        remaining = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = 1;"
        ).fetchone()[0]
    assert remaining == 0


def test_messages_index_exists(initialized_db: Path) -> None:
    """The composite index supporting per-conversation reads is present."""
    with _open(initialized_db) as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index';"
            )
        }
    assert "idx_messages_conversation_created" in indexes


# ---------------------------------------------------------------------------
# Phase 12a: schema + role expansion
# ---------------------------------------------------------------------------


def test_migration_is_idempotent_on_fresh_db(tmp_path: Path) -> None:
    """A brand-new DB doesn't have the legacy CHECK; the migration must
    no-op without errors when init is called twice in a row."""
    db = tmp_path / "chats.db"
    initialize_database(db)
    # Second run on the same path exercises both the CREATE TABLE IF
    # NOT EXISTS branch AND the role-CHECK migration guard. Neither
    # should raise.
    initialize_database(db)


def test_migration_drops_legacy_role_check(tmp_path: Path) -> None:
    """A pre-phase-12 DB has CHECK (role IN ('user','assistant')).
    After init, the CHECK is gone and tool_call rows insert cleanly."""
    db = tmp_path / "chats.db"
    # Hand-craft the legacy schema as it shipped in phases 2-11 so we
    # can verify the migration triggers and copies data across.
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL
                    REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

    initialize_database(db)

    with sqlite3.connect(db) as conn:
        # The migrated table's CREATE TABLE text must no longer mention CHECK.
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='messages';"
        ).fetchone()[0]
        assert "CHECK" not in sql
        # And the new roles must INSERT successfully — direct proof
        # that the old constraint is gone end-to-end. Phase 17: also pass
        # project_id (which the migration backfilled to the Default).
        default_pid = conn.execute(
            "SELECT id FROM projects ORDER BY id LIMIT 1;"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO conversations"
            " (name, model, name_locked, project_id, created_at, updated_at)"
            " VALUES ('x', 'm', 0, ?, 'now', 'now');",
            (default_pid,),
        )
        conn.execute(
            "INSERT INTO messages"
            " (conversation_id, role, content, created_at)"
            " VALUES ((SELECT MAX(id) FROM conversations), 'tool_call', '{}', 'now');"
        )


def test_migration_backfills_tool_iteration_cap_column(tmp_path: Path) -> None:
    """A conversations table that pre-dates this phase gets the
    tool_iteration_cap column backfilled (default 5) on init."""
    db = tmp_path / "chats.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                model TEXT NOT NULL,
                name_locked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO conversations
                (name, model, name_locked, created_at, updated_at)
                VALUES ('legacy', 'm', 0, 'now', 'now');
            """
        )

    initialize_database(db)

    with sqlite3.connect(db) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(conversations);")
        }
        assert "tool_iteration_cap" in columns
        # The pre-existing row picks up the column default.
        cap = conn.execute(
            "SELECT tool_iteration_cap FROM conversations WHERE name = 'legacy';"
        ).fetchone()[0]
        assert cap == 5


def test_migration_backfills_active_agent_column(tmp_path: Path) -> None:
    """A conversations table that pre-dates phase 16 gets the nullable
    active_agent column backfilled (NULL = Normal) on init."""
    db = tmp_path / "chats.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                model TEXT NOT NULL,
                name_locked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO conversations
                (name, model, name_locked, created_at, updated_at)
                VALUES ('legacy', 'm', 0, 'now', 'now');
            """
        )

    initialize_database(db)

    with sqlite3.connect(db) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(conversations);")
        }
        assert "active_agent" in columns
        # The pre-existing row defaults to NULL (the Normal agent).
        value = conn.execute(
            "SELECT active_agent FROM conversations WHERE name = 'legacy';"
        ).fetchone()[0]
        assert value is None


def test_migration_backfills_archived_at_column(tmp_path: Path) -> None:
    """Phase 18: a messages table that pre-dates this phase gets the
    nullable archived_at column backfilled on init (NULL = active row).
    """
    db = tmp_path / "chats.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO messages
                (conversation_id, role, content, created_at)
                VALUES (1, 'user', 'legacy row', 'now');
            """
        )

    initialize_database(db)

    with sqlite3.connect(db) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(messages);")
        }
        assert "archived_at" in columns
        value = conn.execute(
            "SELECT archived_at FROM messages WHERE content = 'legacy row';"
        ).fetchone()[0]
        assert value is None


def test_partial_index_idx_messages_active_present(
    initialized_db: Path,
) -> None:
    """Phase 18: the partial index over active rows is created on init.

    Created by ``_ensure_messages_archived_at_column`` so it lands on
    both fresh DBs and legacy ones after the migration adds the column.
    """
    with _open(initialized_db) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='index' AND tbl_name='messages';"
        ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_messages_active" in names


def test_rag_servers_table_exists_after_init(initialized_db: Path) -> None:
    """Phase 12a introduced the rag_servers table; verify its columns."""
    with _open(initialized_db) as conn:
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(rag_servers);")
        }
    assert cols == {"id", "name", "url", "description", "created_at", "updated_at"}


def test_app_settings_table_exists_after_init(initialized_db: Path) -> None:
    """Phase 13a introduced the app_settings table (key/value store)."""
    with _open(initialized_db) as conn:
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(app_settings);")
        }
    assert cols == {"key", "value"}


# ---------------------------------------------------------------------------
# Phase 17: projects table + per-chat project_id column
# ---------------------------------------------------------------------------


def test_projects_table_created_on_fresh_db(initialized_db: Path) -> None:
    """Phase 17 introduced the projects table; verify its columns are set."""
    with _open(initialized_db) as conn:
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(projects);")
        }
    assert cols == {
        "id",
        "name",
        "description",
        "workspace_subdir",
        "default_model",
        "default_agent",
        "num_ctx",
        "system_prompt",
        "created_at",
        "updated_at",
    }


def test_default_project_inserted_on_fresh_db(initialized_db: Path) -> None:
    """After init, exactly one project named "Default" exists.

    Acts as the home for every chat created without an explicit project (and
    receives every legacy chat the migration backfills).
    """
    with _open(initialized_db) as conn:
        rows = conn.execute(
            "SELECT name, workspace_subdir FROM projects;"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Default"
    assert rows[0][1] == "default"


def test_conversations_get_project_id_column(tmp_path: Path) -> None:
    """Legacy conversations get project_id added + backfilled to Default."""
    db = tmp_path / "chats.db"
    # Build an old-schema DB (no project_id) with one chat row in it.
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                model TEXT NOT NULL,
                name_locked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO conversations
                (name, model, name_locked, created_at, updated_at)
                VALUES ('legacy', 'm', 0, 'now', 'now');
            """
        )

    initialize_database(db)

    with sqlite3.connect(db) as conn:
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(conversations);")
        }
        assert "project_id" in cols
        # The pre-existing chat now points at the Default project.
        row = conn.execute(
            "SELECT project_id FROM conversations WHERE name = 'legacy';"
        ).fetchone()
        default_pid = conn.execute(
            "SELECT id FROM projects WHERE name = 'Default';"
        ).fetchone()[0]
        assert row[0] == default_pid


def test_existing_default_project_preserved(tmp_path: Path) -> None:
    """A second initialize_database call does NOT create another Default."""
    db = tmp_path / "chats.db"
    initialize_database(db)
    initialize_database(db)
    with sqlite3.connect(db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE name = 'Default';"
        ).fetchone()[0]
    assert count == 1


def test_migration_idempotent_on_already_migrated_db(tmp_path: Path) -> None:
    """Running initialize_database on an already-migrated DB is a no-op.

    Specifically: the project_id column addition + table-rewrite must not
    fire twice (would error on the second run because the column is now
    present and the schema is correct).
    """
    db = tmp_path / "chats.db"
    initialize_database(db)
    # Second run must not raise — _ensure_conversations_project_id_column
    # detects the existing column and short-circuits.
    initialize_database(db)
    with sqlite3.connect(db) as conn:
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(conversations);")
        }
        assert "project_id" in cols
