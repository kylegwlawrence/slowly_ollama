"""Phase 12c: CRUD tests for app.rag_servers.

Each test gets a fresh, schema-initialized SQLite DB at
``tmp_path/chats.db`` via the ``conn`` fixture. The fixture mirrors the
one in ``tests/test_queries.py`` so the two test modules share a
familiar shape.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.connection import open_connection
from app.db import initialize_database
from app.rag_servers import (
    RagServer,
    create_server,
    delete_server,
    list_servers,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a connection to a freshly-initialized DB in tmp_path."""
    db_path = tmp_path / "chats.db"
    initialize_database(db_path)
    with open_connection(db_path) as connection:
        yield connection


def test_list_empty(conn: sqlite3.Connection) -> None:
    """An empty rag_servers table lists as []. Covers the no-config case."""
    assert list_servers(conn) == []


def test_create_returns_populated_row(conn: sqlite3.Connection) -> None:
    """create_server returns a RagServer with id, name, url, and UTC timestamps."""
    server = create_server(conn, "arxiv", "http://example/arxiv")

    assert isinstance(server, RagServer)
    assert server.id > 0
    assert server.name == "arxiv"
    assert server.url == "http://example/arxiv"
    # ISO 8601 UTC round-trip — naive datetimes would mean we lost the
    # timezone marker somewhere in the read path.
    assert server.created_at.tzinfo is not None
    assert server.updated_at == server.created_at


def test_create_and_list_in_insertion_order(conn: sqlite3.Connection) -> None:
    """list_servers returns rows in oldest-first insertion order.

    The settings UI relies on this stable order so adding a server
    appends it to the bottom of the rendered list rather than
    reshuffling the existing entries.
    """
    s1 = create_server(conn, "arxiv", "http://x/arxiv")
    s2 = create_server(conn, "factbook", "http://x/factbook")
    assert s1.id < s2.id
    rows = list_servers(conn)
    assert [r.name for r in rows] == ["arxiv", "factbook"]
    assert rows[0].url == "http://x/arxiv"
    assert rows[1].url == "http://x/factbook"


def test_create_rejects_duplicate_name(conn: sqlite3.Connection) -> None:
    """The schema's UNIQUE on `name` surfaces as IntegrityError.

    The route handler converts this to HTTP 409 — see the matching
    route test in tests/test_routes.py.
    """
    create_server(conn, "arxiv", "http://x/arxiv")
    with pytest.raises(sqlite3.IntegrityError):
        create_server(conn, "arxiv", "http://y/arxiv")


def test_create_allows_same_url_with_different_name(
    conn: sqlite3.Connection,
) -> None:
    """Only `name` is unique — two sources at the same URL are legal.

    (Edge case: the user is debugging or running two logical sources
    against one physical RAG box.)
    """
    create_server(conn, "arxiv", "http://shared/")
    create_server(conn, "duplicate-source", "http://shared/")
    assert len(list_servers(conn)) == 2


def test_delete_removes_row(conn: sqlite3.Connection) -> None:
    """delete_server removes the matching row from list_servers output."""
    s = create_server(conn, "arxiv", "http://x/arxiv")

    delete_server(conn, s.id)

    assert list_servers(conn) == []


def test_delete_is_idempotent_for_missing_ids(
    conn: sqlite3.Connection,
) -> None:
    """Deleting a missing id is silent — same behaviour as queries.delete_conversation.

    The UI flow is "user clicks delete on a row they can see"; a stale
    id from another tab shouldn't crash the request.
    """
    s = create_server(conn, "arxiv", "http://x/arxiv")
    delete_server(conn, s.id)
    # Re-delete and a never-existed id — both no-op.
    delete_server(conn, s.id)
    delete_server(conn, 999)
    assert list_servers(conn) == []
