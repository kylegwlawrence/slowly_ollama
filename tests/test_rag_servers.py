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
    get_server,
    list_servers,
    update_server,
    update_server_description,
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
    """create_server returns a RagServer with id, name, url, description, and UTC timestamps."""
    server = create_server(conn, "arxiv", "http://example/arxiv", description="Papers on CS/ML")

    assert isinstance(server, RagServer)
    assert server.id > 0
    assert server.name == "arxiv"
    assert server.url == "http://example/arxiv"
    assert server.description == "Papers on CS/ML"
    # ISO 8601 UTC round-trip — naive datetimes would mean we lost the
    # timezone marker somewhere in the read path.
    assert server.created_at.tzinfo is not None
    assert server.updated_at == server.created_at


def test_create_server_defaults_description_to_empty_string(
    conn: sqlite3.Connection,
) -> None:
    """create_server omitting description stores an empty string.

    The default keeps existing callers (tests, REPL) working without
    changes; the route handler always passes description explicitly.
    """
    server = create_server(conn, "arxiv", "http://example/arxiv")
    assert server.description == ""


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


def test_get_server_returns_matching_row(conn: sqlite3.Connection) -> None:
    """get_server fetches the row whose id matches."""
    s = create_server(conn, "arxiv", "http://x/arxiv", description="Papers")
    fetched = get_server(conn, s.id)
    assert fetched is not None
    assert fetched.id == s.id
    assert fetched.name == "arxiv"
    assert fetched.description == "Papers"


def test_get_server_returns_none_for_missing_id(
    conn: sqlite3.Connection,
) -> None:
    """get_server returns None when no row has that id (route -> 404)."""
    assert get_server(conn, 999) is None


def test_update_description_changes_text_and_bumps_updated_at(
    conn: sqlite3.Connection,
) -> None:
    """update_server_description rewrites description and advances updated_at.

    name/url/created_at are left untouched — only the description is
    editable in place.
    """
    s = create_server(conn, "arxiv", "http://x/arxiv", description="old")

    updated = update_server_description(conn, s.id, "new and improved")

    assert updated is not None
    assert updated.id == s.id
    assert updated.name == "arxiv"
    assert updated.url == "http://x/arxiv"
    assert updated.description == "new and improved"
    assert updated.created_at == s.created_at
    assert updated.updated_at >= s.updated_at
    # The change is durable, not just reflected in the return value.
    assert get_server(conn, s.id).description == "new and improved"


def test_update_description_to_empty_string(conn: sqlite3.Connection) -> None:
    """A description can be cleared back to empty string."""
    s = create_server(conn, "arxiv", "http://x/arxiv", description="something")
    updated = update_server_description(conn, s.id, "")
    assert updated is not None
    assert updated.description == ""


def test_update_description_missing_id_returns_none(
    conn: sqlite3.Connection,
) -> None:
    """Updating a missing id returns None rather than raising (route -> 404)."""
    assert update_server_description(conn, 999, "ignored") is None


def test_update_server_changes_all_fields(conn: sqlite3.Connection) -> None:
    """update_server rewrites name, url, and description and advances updated_at."""
    s = create_server(conn, "arxiv", "http://host:8002/arxiv", description="old")

    updated = update_server(conn, s.id, "pubmed", "http://host:8002/pubmed", "new desc")

    assert updated is not None
    assert updated.id == s.id
    assert updated.name == "pubmed"
    assert updated.url == "http://host:8002/pubmed"
    assert updated.description == "new desc"
    assert updated.created_at == s.created_at
    assert updated.updated_at >= s.updated_at
    # Durable.
    fetched = get_server(conn, s.id)
    assert fetched.name == "pubmed"
    assert fetched.url == "http://host:8002/pubmed"


def test_update_server_name_collision_raises(conn: sqlite3.Connection) -> None:
    """update_server raises IntegrityError when the new name collides with another row."""
    create_server(conn, "taken", "http://host:8002/taken")
    s = create_server(conn, "arxiv", "http://host:8002/arxiv")

    import pytest as _pytest
    with _pytest.raises(sqlite3.IntegrityError):
        update_server(conn, s.id, "taken", "http://host:8002/arxiv", "")


def test_update_server_missing_id_returns_none(conn: sqlite3.Connection) -> None:
    """update_server returns None when no row has that id (route -> 404)."""
    assert update_server(conn, 999, "x", "http://x/x", "") is None


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
