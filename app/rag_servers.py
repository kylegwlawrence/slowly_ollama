"""CRUD for the rag_servers table and liveness probing.

Mirrors the conversations / messages query helpers in style: each function
takes a ``sqlite3.Connection`` and wraps writes in ``with conn:`` for
atomicity. RAG servers are user-configured at runtime via the /settings
UI; the ``query_rag`` tool reads this table to validate the model's chosen
``source`` argument and to look up the corresponding base URL.

The remote RAG host exposes a ``/health`` endpoint reporting each database's
status as ``{name: status}`` under ``databases``. Before inserting a new row
we probe it and verify the typed name is present and reports ``"ok"``.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse, urlunparse

import httpx

from app._time import now_iso as _now_iso

# Health endpoints are cheap (no FTS/ANN, just a status map), so the timeout
# is tight: two seconds to connect, five total.
_HEALTH_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
_HEALTHY_STATUS = "ok"


@dataclass(frozen=True)
class RagServer:
    """One row of the ``rag_servers`` table.

    Attributes:
        id: Auto-assigned primary key.
        name: Short human/model-facing identifier (e.g. ``"arxiv"``).
            Used as the ``source`` argument value in ``query_rag`` and
            therefore must be UNIQUE — the schema enforces this and the
            route handler converts the UNIQUE violation to a 409.
        url: Full base URL up through the source prefix
            (e.g. ``"http://10.0.0.5:8002/arxiv"``). The ``query_rag``
            tool appends ``"/chunks"`` itself.
        created_at: When the row was first inserted (UTC).
        updated_at: When the row was last touched (UTC). Currently bumped
            only at insert; included for symmetry with the conversations
            table in case a future phase adds in-place edits.
        description: User-supplied text describing the source contents,
            e.g. ``"PubMed abstracts 2020–2024"``. Folded into the
            ``query_rag`` tool's ``source`` parameter hint so the model
            can pick the right source intelligently. Empty string for
            legacy rows; rendered as ``(no description)`` in the UI and
            in the tool spec.
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
    """Map a ``rag_servers`` row to the ``RagServer`` dataclass.

    Parses ISO 8601 timestamps into ``datetime`` so callers don't deal
    with raw strings.
    """
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

    Stable insertion order (``ORDER BY id ASC``) so the settings UI
    doesn't reshuffle rows on each reload — adding a new server makes
    it appear at the bottom of the list, where the form just submitted it.

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
        name: Unique source identifier (e.g. ``"arxiv"``). The model
            uses this string as the ``source`` arg to ``query_rag``.
        url: Full base URL up through the source prefix.
        description: Human-readable summary of the source's contents.
            Defaults to ``""`` so existing callers (tests, REPL) that
            omit it keep working without changes.

    Returns:
        The newly created RagServer, populated with its assigned id
        and timestamps.

    Raises:
        sqlite3.IntegrityError: ``name`` collides with an existing row
            (UNIQUE constraint). The route handler converts this into
            an HTTP 409.
    """
    now = _now_iso()
    with conn:
        # RETURNING (SQLite 3.35+) saves a follow-up SELECT for the
        # auto-assigned id and the timestamps we just wrote.
        row = conn.execute(
            "INSERT INTO rag_servers (name, url, description, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " RETURNING id, name, url, description, created_at, updated_at;",
            (name, url, description, now, now),
        ).fetchone()
    return _row_to_server(row)


def get_server(conn: sqlite3.Connection, server_id: int) -> RagServer | None:
    """Fetch a single server row by id.

    Backs the inline description editor's GET route, which re-renders
    one row in view or edit mode without re-listing the whole table.

    Args:
        conn: Open SQLite connection.
        server_id: Id of the server to fetch.

    Returns:
        The matching RagServer, or ``None`` if no row has that id (e.g.
        another tab deleted it). The route maps ``None`` to a 404.
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
        name: New unique source identifier. Raises ``IntegrityError`` on
            collision with another row; the route converts this to a 409.
        url: New full base URL.
        description: New human-readable summary of the source's contents.

    Returns:
        The updated RagServer, or ``None`` if no row has that id (the
        route maps ``None`` to a 404).

    Raises:
        sqlite3.IntegrityError: ``name`` collides with a different existing
            row (UNIQUE constraint).
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
        description: New human-readable summary of the source's contents.

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

    Missing ids are silently accepted — the UI flow is "user clicks
    delete on a row"; a stale id (e.g. another tab already deleted it)
    shouldn't surface as an exception. Mirrors the same idempotent
    behaviour as ``queries.delete_conversation``.

    Args:
        conn: Open SQLite connection.
        server_id: Id of the server to delete.
    """
    with conn:
        conn.execute("DELETE FROM rag_servers WHERE id = ?;", (server_id,))


# ---------------------------------------------------------------------------
# Health probing
# ---------------------------------------------------------------------------


def _health_url(base_url: str) -> str | None:
    """Derive the ``/health`` URL from a typed RAG server base URL.

    Strips path/query/fragment and appends ``/health``. Returns ``None``
    if the URL is missing scheme or host.

    Args:
        base_url: Full RAG base URL as typed into the form.

    Returns:
        The ``/health`` URL, or ``None`` if ``base_url`` is malformed.
    """
    parsed = urlparse(base_url.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, "/health", "", "", ""))


async def probe_rag_health(name: str, base_url: str) -> tuple[bool, str]:
    """Probe ``/health`` for a named database; return ``(healthy, reason)``.

    On success returns ``(True, "")``. On any failure returns
    ``(False, <user-facing reason>)``. Never raises.

    A non-2xx status alone is NOT treated as failure: the shared /health
    endpoint returns 503 when ANY hosted database is unhealthy, but the
    per-database map still reports each entry correctly. We read the map
    regardless of HTTP status and judge only the specific ``name`` the
    user typed.

    Args:
        name: Database name to look up under the /health ``databases`` map.
        base_url: Full RAG base URL as typed (e.g. ``http://host1:8002/arxiv``).

    Returns:
        Tuple of ``(healthy, reason)``. ``reason`` is empty on success.
    """
    health_url = _health_url(base_url)
    if health_url is None:
        return (
            False,
            "URL must include scheme and host"
            " (e.g. http://host1:8002/arxiv_rag).",
        )

    try:
        async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT) as client:
            response = await client.get(health_url)
    except httpx.HTTPError:
        return (
            False,
            f"Health check failed: server unreachable at {health_url}.",
        )

    try:
        body = response.json()
    except ValueError:
        body = None

    databases = body.get("databases") if isinstance(body, dict) else None
    if not isinstance(databases, dict):
        if response.status_code >= 400:
            return (
                False,
                f"Health check failed: HTTP {response.status_code} from {health_url}.",
            )
        if body is None:
            return (
                False,
                f"Health check failed: non-JSON response from {health_url}.",
            )
        return (
            False,
            f"Health check failed: /health response missing 'databases' map.",
        )

    if name not in databases:
        available = ", ".join(sorted(databases)) or "(none)"
        return (
            False,
            f"'{name}' not found in /health response."
            f" Available databases: {available}.",
        )

    reported = databases[name]
    if reported != _HEALTHY_STATUS:
        return (
            False,
            f"'{name}' is not healthy (status: {reported!r}).",
        )

    return (True, "")
