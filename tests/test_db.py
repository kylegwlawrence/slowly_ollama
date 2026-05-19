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
    # Phase 12a added rag_servers; conversations + messages predate it.
    assert tables == {"conversations", "messages", "rag_servers"}


def test_role_accepts_documented_roles(initialized_db: Path) -> None:
    """The four documented roles all insert without error.

    Phase 12a dropped the SQLite CHECK so this is now a smoke test that
    the schema doesn't reject any of the Python-side `Role` values.
    """
    with _open(initialized_db) as conn:
        conn.execute(
            "INSERT INTO conversations"
            " (id, name, model, created_at, updated_at)"
            " VALUES (1, 'c', 'llama3', '2025-01-01', '2025-01-01');"
        )
        for role in ("user", "assistant", "tool_call", "tool_result"):
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
        conn.execute(
            "INSERT INTO conversations"
            " (id, name, model, created_at, updated_at)"
            " VALUES (1, 'c', 'llama3', '2025-01-01', '2025-01-01');"
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
        # that the old constraint is gone end-to-end.
        conn.execute(
            "INSERT INTO conversations"
            " (name, model, name_locked, created_at, updated_at)"
            " VALUES ('x', 'm', 0, 'now', 'now');"
        )
        conn.execute(
            "INSERT INTO messages"
            " (conversation_id, role, content, created_at)"
            " VALUES (1, 'tool_call', '{}', 'now');"
        )


def test_rag_servers_table_exists_after_init(initialized_db: Path) -> None:
    """Phase 12a introduced the rag_servers table; verify its columns."""
    with _open(initialized_db) as conn:
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(rag_servers);")
        }
    assert cols == {"id", "name", "url", "created_at", "updated_at"}
