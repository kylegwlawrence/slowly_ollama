"""CRUD for the rag_servers table.

Each function takes a ``sqlite3.Connection`` and wraps writes in
``with conn:`` for atomicity. RAG servers are configured at runtime via
/settings; the ``query_rag`` tool reads this table to validate the model's
``source`` argument and look up its base URL.

Liveness probing lives in :mod:`app.rag_health`.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app._time import now_iso as _now_iso


@dataclass(frozen=True)
class RagServer:
    """One row of the ``rag_servers`` table.

    Attributes:
        id: Auto-assigned primary key.
        name: Short identifier (e.g. ``"arxiv"``) used as the ``source``
            arg in ``query_rag``. UNIQUE — the schema enforces it and the
            route converts the violation to a 409.
        url: Full base URL through the source prefix (e.g.
            ``"http://10.0.0.5:8002/arxiv"``); ``query_rag`` appends
            ``"/chunks"``.
        created_at: First-insert time (UTC).
        updated_at: Last-touched time (UTC).
        description: Source summary (e.g. ``"PubMed abstracts 2020–2024"``),
            folded into the ``query_rag`` ``source`` hint so the model can
            pick the right source. '' for legacy rows; shown as
            ``(no description)``.
    """

    id: int
    name: str
    url: str
    created_at: datetime
    updated_at: datetime
    description: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_server(row: sqlite3.Row) -> RagServer:
    """Map a ``rag_servers`` row to ``RagServer``, parsing ISO timestamps."""
    return RagServer(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        description=row["description"],
    )


# ---------------------------------------------------------------------------
# Public CRUD
# ---------------------------------------------------------------------------


def list_servers(conn: sqlite3.Connection) -> list[RagServer]:
    """Return every configured RAG server, oldest first.

    ``ORDER BY id ASC`` keeps the settings UI stable across reloads — a new
    server appears at the bottom, where the form just submitted it.

    Args:
        conn: Open SQLite connection.

    Returns:
        RagServer rows, oldest insert first.
    """
    rows = conn.execute(
        "SELECT id, name, url, description, created_at, updated_at FROM rag_servers"
        " ORDER BY id ASC;"
    ).fetchall()
    return [_row_to_server(r) for r in rows]


def create_server(
    conn: sqlite3.Connection, name: str, url: str, description: str = ""
) -> RagServer:
    """Insert a new RAG server row.

    Args:
        conn: Open SQLite connection.
        name: Unique source identifier (e.g. ``"arxiv"``), used as the
            ``source`` arg to ``query_rag``.
        url: Full base URL through the source prefix.
        description: Source summary. Defaults to ``""`` so callers that
            omit it keep working.

    Returns:
        The newly created RagServer with its assigned id and timestamps.

    Raises:
        sqlite3.IntegrityError: ``name`` collides with an existing row
            (UNIQUE); the route converts this to a 409.
    """
    now = _now_iso()
    with conn:
        # RETURNING saves a follow-up SELECT for the id + timestamps.
        row = conn.execute(
            "INSERT INTO rag_servers (name, url, description, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " RETURNING id, name, url, description, created_at, updated_at;",
            (name, url, description, now, now),
        ).fetchone()
    return _row_to_server(row)


def get_server(conn: sqlite3.Connection, server_id: int) -> RagServer | None:
    """Fetch a single server row by id.

    Backs the inline description editor's GET route, which re-renders one
    row in view or edit mode without re-listing the table.

    Args:
        conn: Open SQLite connection.
        server_id: Id of the server to fetch.

    Returns:
        The matching RagServer, or ``None`` if no row has that id (the
        route maps ``None`` to a 404).
    """
    row = conn.execute(
        "SELECT id, name, url, description, created_at, updated_at"
        " FROM rag_servers WHERE id = ?;",
        (server_id,),
    ).fetchone()
    return _row_to_server(row) if row else None


def update_server(
    conn: sqlite3.Connection,
    server_id: int,
    name: str,
    url: str,
    description: str,
) -> RagServer | None:
    """Update a server's name, URL, and description in place.

    Args:
        conn: Open SQLite connection.
        server_id: Id of the server to update.
        name: New unique source identifier.
        url: New full base URL.
        description: New source summary.

    Returns:
        The updated RagServer, or ``None`` if no row has that id (the
        route maps ``None`` to a 404).

    Raises:
        sqlite3.IntegrityError: ``name`` collides with another row
            (UNIQUE); the route converts this to a 409.
    """
    now = _now_iso()
    with conn:
        row = conn.execute(
            "UPDATE rag_servers SET name = ?, url = ?, description = ?, updated_at = ?"
            " WHERE id = ?"
            " RETURNING id, name, url, description, created_at, updated_at;",
            (name, url, description, now, server_id),
        ).fetchone()
    return _row_to_server(row) if row else None


def update_server_description(
    conn: sqlite3.Connection, server_id: int, description: str
) -> RagServer | None:
    """Update a server's description in place and bump ``updated_at``.

    Args:
        conn: Open SQLite connection.
        server_id: Id of the server to update.
        description: New source summary.

    Returns:
        The updated RagServer, or ``None`` if no row has that id (the
        route maps ``None`` to a 404).
    """
    now = _now_iso()
    with conn:
        row = conn.execute(
            "UPDATE rag_servers SET description = ?, updated_at = ?"
            " WHERE id = ?"
            " RETURNING id, name, url, description, created_at, updated_at;",
            (description, now, server_id),
        ).fetchone()
    return _row_to_server(row) if row else None


def delete_server(conn: sqlite3.Connection, server_id: int) -> None:
    """Delete a server row by id; idempotent.

    A missing id (e.g. another tab already deleted it) is silently
    accepted rather than raised.

    Args:
        conn: Open SQLite connection.
        server_id: Id of the server to delete.
    """
    with conn:
        conn.execute("DELETE FROM rag_servers WHERE id = ?;", (server_id,))
