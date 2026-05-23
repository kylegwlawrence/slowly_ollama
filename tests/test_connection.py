"""Tests for Phase 3: the long-lived connection factory.

Every test points at a tempfile and runs Phase 2's `initialize_database`
first, so the schema is in place before the factory hands out a connection.
"""

import sqlite3
import threading
from pathlib import Path

from app.connection import open_connection
from app.db import initialize_database


def test_open_connection_enables_foreign_keys(tmp_path: Path) -> None:
    """FK enforcement is on for every connection from the factory.

    Phase 4's queries (and Phase 2's CASCADE behavior) assume FKs are on;
    if a future edit drops the pragma here, cascade deletes silently stop
    working — this test catches that.
    """
    db_path = tmp_path / "chats.db"
    initialize_database(db_path)

    with open_connection(db_path) as conn:
        result = conn.execute("PRAGMA foreign_keys;").fetchone()

    assert result[0] == 1


def test_open_connection_uses_wal_journal_mode(tmp_path: Path) -> None:
    """WAL journal mode is enabled for concurrent reads-during-write."""
    db_path = tmp_path / "chats.db"
    initialize_database(db_path)

    with open_connection(db_path) as conn:
        result = conn.execute("PRAGMA journal_mode;").fetchone()

    assert result[0] == "wal"


def test_open_connection_uses_row_factory(tmp_path: Path) -> None:
    """Rows from this connection are addressable by column name."""
    db_path = tmp_path / "chats.db"
    initialize_database(db_path)

    with open_connection(db_path) as conn:
        # Phase 17: every conversation needs a project_id (FK NOT NULL).
        # The migration created a Default project; reuse it.
        default_pid = conn.execute(
            "SELECT id FROM projects ORDER BY id LIMIT 1;"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO conversations"
            " (id, name, model, project_id, created_at, updated_at)"
            " VALUES (1, 'c', 'llama3', ?, '2025-01-01', '2025-01-01');",
            (default_pid,),
        )
        row = conn.execute(
            "SELECT id, name, model FROM conversations WHERE id = 1;"
        ).fetchone()

    assert isinstance(row, sqlite3.Row)
    assert row["name"] == "c"
    assert row["model"] == "llama3"


def test_open_connection_usable_across_threads(tmp_path: Path) -> None:
    """The connection works from a thread other than the one that opened it.

    FastAPI runs sync endpoints in a threadpool, so a long-lived shared
    connection will be touched from different worker threads. Without
    `check_same_thread=False` this insert would raise a ProgrammingError.
    """
    db_path = tmp_path / "chats.db"
    initialize_database(db_path)

    # Collect the worker's result in a list so the assertion runs on the
    # main thread — pytest doesn't surface assertion failures from
    # spawned threads otherwise.
    counts: list[int] = []

    with open_connection(db_path) as conn:
        # Phase 17: read the Default project id on the main thread; the
        # worker thread reuses it for the chat insert (FK NOT NULL).
        default_pid = conn.execute(
            "SELECT id FROM projects ORDER BY id LIMIT 1;"
        ).fetchone()[0]

        def worker() -> None:
            conn.execute(
                "INSERT INTO conversations"
                " (id, name, model, project_id, created_at, updated_at)"
                " VALUES (1, 'c', 'llama3', ?, '2025-01-01', '2025-01-01');",
                (default_pid,),
            )
            row = conn.execute("SELECT COUNT(*) FROM conversations;").fetchone()
            counts.append(row[0])

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

    assert counts == [1]
