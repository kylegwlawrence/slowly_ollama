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
    # Second call must not raise; both tables must still be present.
    initialize_database(initialized_db)

    with _open(initialized_db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table';"
            )
        }
    assert tables == {"conversations", "messages"}


def test_role_check_accepts_documented_roles(initialized_db: Path) -> None:
    """The two v1 roles ('user', 'assistant') insert without error."""
    with _open(initialized_db) as conn:
        conn.execute(
            "INSERT INTO conversations"
            " (id, name, model, created_at, updated_at)"
            " VALUES (1, 'c', 'llama3', '2025-01-01', '2025-01-01');"
        )
        for role in ("user", "assistant"):
            conn.execute(
                "INSERT INTO messages"
                " (conversation_id, role, content, created_at)"
                " VALUES (1, ?, 'hi', '2025-01-01');",
                (role,),
            )


def test_role_check_rejects_unknown_role(initialized_db: Path) -> None:
    """Anything outside the documented v1 roles is rejected at insert."""
    with _open(initialized_db) as conn:
        conn.execute(
            "INSERT INTO conversations"
            " (id, name, model, created_at, updated_at)"
            " VALUES (1, 'c', 'llama3', '2025-01-01', '2025-01-01');"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO messages"
                " (conversation_id, role, content, created_at)"
                " VALUES (1, 'system', 'hi', '2025-01-01');"
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
