"""CRUD for the ``conversations`` table."""

import sqlite3
from datetime import datetime

from app._time import now_iso as _now_iso
from app.queries._models import Conversation


def _row_to_conversation(row: sqlite3.Row) -> Conversation:
    """Map a ``conversations`` row to the :class:`Conversation` dataclass.

    Parses ISO 8601 timestamps into ``datetime`` and widens the INTEGER
    ``name_locked`` (0/1) to ``bool``.
    """
    return Conversation(
        id=row["id"],
        name=row["name"],
        model=row["model"],
        name_locked=bool(row["name_locked"]),
        temperature=float(row["temperature"]),
        tool_iteration_cap=int(row["tool_iteration_cap"]),
        project_id=int(row["project_id"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        active_host=row["active_host"],
        think_mode=row["think_mode"],
        agent_id=row["agent_id"],
    )


def create_conversation(
    conn: sqlite3.Connection,
    name: str,
    model: str,
    *,
    project_id: int | None = None,
    temperature: float = 0.8,
    tool_iteration_cap: int = 5,
    think_mode: str = "default",
    active_host: str | None = None,
) -> Conversation:
    """Insert a new conversation row.

    Args:
        conn: Open SQLite connection.
        name: Human-readable conversation name.
        model: Ollama model identifier this conversation will use.
        project_id: Owning project. When omitted, assigned to the lowest-id
            project (the migration's "Default"). Pass an explicit value when
            you care which project owns the chat.
        temperature: Sampling temperature passed to Ollama (0.0–2.0).
        tool_iteration_cap: Per-turn tool-call iteration cap (caller clamps
            to 1–10).
        think_mode: Thinking lever. 'default' omits Ollama's ``think`` key;
            'off' suppresses the reasoning phase. Caller coerces unknown
            values to 'default'.
        active_host: Host key in `app.hosts.HOSTS` (e.g. "host2") to start
            on, or None for the primary host. A non-primary host's model is
            stored separately via ``set_chat_host_model``.

    Returns:
        The newly created Conversation, with its assigned id and timestamps.

    Raises:
        LookupError: When ``project_id`` is omitted and no projects exist
            (should never happen — initialize_database guarantees Default).
    """
    if project_id is None:
        # Fallback to the Default project; keeps the function ergonomic for
        # tests + tools that don't care which project a chat lands in.
        row = conn.execute(
            "SELECT id FROM projects ORDER BY id LIMIT 1;"
        ).fetchone()
        if row is None:
            raise LookupError(
                "Cannot create a conversation: no projects exist."
            )
        project_id = row[0] if not isinstance(row, sqlite3.Row) else row["id"]
    now = _now_iso()
    with conn:
        # RETURNING (SQLite 3.35+) avoids a follow-up SELECT for the
        # auto-assigned id + timestamps. New rows start unlocked
        # (name_locked = 0) so the auto-titler can refresh the placeholder
        # until a manual rename.
        row = conn.execute(
            "INSERT INTO conversations"
            " (name, model, name_locked, temperature, tool_iteration_cap,"
            "  think_mode, active_host, project_id, created_at, updated_at)"
            " VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?)"
            " RETURNING id, name, model, name_locked, temperature, tool_iteration_cap,"
            "          active_host, project_id, created_at, updated_at, think_mode, agent_id;",
            (
                name, model, temperature, tool_iteration_cap, think_mode,
                active_host, project_id, now, now,
            ),
        ).fetchone()
    return _row_to_conversation(row)


def get_conversation(
    conn: sqlite3.Connection, conversation_id: int
) -> Conversation:
    """Look up a single conversation by id.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id to look up.

    Returns:
        The matching Conversation.

    Raises:
        LookupError: If no conversation exists with that id.
    """
    row = conn.execute(
        "SELECT id, name, model, name_locked, temperature, tool_iteration_cap,"
        " active_host, project_id, created_at, updated_at, think_mode, agent_id"
        " FROM conversations WHERE id = ?;",
        (conversation_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_conversation(row)


def list_conversations(conn: sqlite3.Connection) -> list[Conversation]:
    """Return every conversation, most-recently-updated first.

    Args:
        conn: Open SQLite connection.

    Returns:
        Conversations ordered by `updated_at DESC` — the sidebar's order,
        so the chat the user just touched is on top.
    """
    rows = conn.execute(
        "SELECT id, name, model, name_locked, temperature, tool_iteration_cap,"
        " active_host, project_id, created_at, updated_at, think_mode, agent_id"
        " FROM conversations"
        " ORDER BY updated_at DESC, id DESC;"
    ).fetchall()
    return [_row_to_conversation(r) for r in rows]


def list_conversations_in_project(
    conn: sqlite3.Connection, project_id: int
) -> list[Conversation]:
    """Return every conversation in a project, most-recently-updated first.

    Same ordering as :func:`list_conversations`.

    Args:
        conn: Open SQLite connection.
        project_id: The project whose conversations to list.

    Returns:
        Conversations in the project, ``updated_at DESC``. Empty list when
        the project has no chats (or doesn't exist).
    """
    rows = conn.execute(
        "SELECT id, name, model, name_locked, temperature, tool_iteration_cap,"
        " active_host, project_id, created_at, updated_at, think_mode, agent_id"
        " FROM conversations"
        " WHERE project_id = ?"
        " ORDER BY updated_at DESC, id DESC;",
        (project_id,),
    ).fetchall()
    return [_row_to_conversation(r) for r in rows]


def rename_conversation(
    conn: sqlite3.Connection, conversation_id: int, new_name: str
) -> Conversation:
    """Change a conversation's name; locks it against future auto-rename.

    Bumps `updated_at` and flips `name_locked` to 1 in the same write, so
    later auto-titler runs see the lock and skip: a deliberate rename always
    wins over the next automated refresh.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation to rename.
        new_name: Replacement name.

    Returns:
        The updated Conversation, with `name_locked=True`.

    Raises:
        LookupError: If no conversation exists with that id.
    """
    now = _now_iso()
    with conn:
        row = conn.execute(
            "UPDATE conversations"
            " SET name = ?, name_locked = 1, updated_at = ?"
            " WHERE id = ?"
            " RETURNING id, name, model, name_locked, temperature, tool_iteration_cap,"
            "          active_host, project_id, created_at, updated_at, think_mode, agent_id;",
            (new_name, now, conversation_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_conversation(row)


def set_name_auto(
    conn: sqlite3.Connection, conversation_id: int, new_name: str
) -> Conversation | None:
    """Auto-set the name iff it hasn't been manually renamed yet.

    Used by the title-generation flow. The `WHERE name_locked = 0` clause
    guards the race where the user clicks Rename between the title request
    firing and this UPDATE: the row is already locked, so the UPDATE matches
    zero rows and the caller skips the OOB sidebar swap, leaving the manual
    name put.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation whose name to refresh.
        new_name: Model-generated title.

    Returns:
        The updated Conversation if the write landed; None if the row was
        locked or the id didn't exist (both no-ops for the caller).
    """
    now = _now_iso()
    with conn:
        row = conn.execute(
            "UPDATE conversations"
            " SET name = ?, updated_at = ?"
            " WHERE id = ? AND name_locked = 0"
            " RETURNING id, name, model, name_locked, temperature, tool_iteration_cap,"
            "          active_host, project_id, created_at, updated_at, think_mode, agent_id;",
            (new_name, now, conversation_id),
        ).fetchone()
    return _row_to_conversation(row) if row is not None else None


def delete_conversation(
    conn: sqlite3.Connection, conversation_id: int
) -> None:
    """Delete a conversation and (via FK cascade) all its messages.

    Idempotent: a stale id is a no-op, not an error.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation to delete.
    """
    with conn:
        conn.execute(
            "DELETE FROM conversations WHERE id = ?;", (conversation_id,)
        )


def set_conversation_temperature(
    conn: sqlite3.Connection, conversation_id: int, temperature: float
) -> Conversation:
    """Update the sampling temperature for a conversation.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation to update.
        temperature: New temperature value (caller should clamp to 0.0–2.0).

    Returns:
        The updated Conversation.

    Raises:
        LookupError: If no conversation exists with that id.
    """
    with conn:
        row = conn.execute(
            "UPDATE conversations"
            " SET temperature = ?"
            " WHERE id = ?"
            " RETURNING id, name, model, name_locked, temperature, tool_iteration_cap,"
            "          active_host, project_id, created_at, updated_at, think_mode, agent_id;",
            (temperature, conversation_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_conversation(row)


def set_conversation_tool_iteration_cap(
    conn: sqlite3.Connection, conversation_id: int, tool_iteration_cap: int
) -> Conversation:
    """Update the single-agent tool-iteration cap for a conversation.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation to update.
        tool_iteration_cap: New cap (caller should clamp to 1–10).

    Returns:
        The updated Conversation.

    Raises:
        LookupError: If no conversation exists with that id.
    """
    with conn:
        row = conn.execute(
            "UPDATE conversations"
            " SET tool_iteration_cap = ?"
            " WHERE id = ?"
            " RETURNING id, name, model, name_locked, temperature, tool_iteration_cap,"
            "          active_host, project_id, created_at, updated_at, think_mode, agent_id;",
            (tool_iteration_cap, conversation_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_conversation(row)


def set_active_host(
    conn: sqlite3.Connection, conversation_id: int, host_name: str | None
) -> Conversation:
    """Set (or clear) the selected Ollama host for a conversation.

    Does NOT bump ``updated_at`` — switching hosts isn't a message event, so
    it shouldn't reorder the sidebar (same as the temperature / tool-cap
    setters).

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation to update.
        host_name: A host key from `app.hosts.HOSTS`, or None for the primary
            host. Caller validates the name (routes use `app.hosts.get_host`).

    Returns:
        The updated Conversation.

    Raises:
        LookupError: If no conversation exists with that id.
    """
    with conn:
        row = conn.execute(
            "UPDATE conversations"
            " SET active_host = ?"
            " WHERE id = ?"
            " RETURNING id, name, model, name_locked, temperature, tool_iteration_cap,"
            "          active_host, project_id, created_at, updated_at, think_mode, agent_id;",
            (host_name, conversation_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_conversation(row)


def clear_unknown_active_hosts(
    conn: sqlite3.Connection, valid_names: set[str]
) -> int:
    """Clear any ``active_host`` not in ``valid_names`` back to NULL (primary).

    Run at startup to reconcile stored selections against the host registry:
    a host removed from ``OLLAMA_EXTRA_HOSTS`` leaves stale names, and
    ``app.hosts.get_host`` raises on an unknown name. Clearing keeps the
    invariant that a stored ``active_host`` is always NULL or a registered
    host.

    Does NOT bump ``updated_at`` (same as ``set_active_host``).

    Args:
        conn: Open SQLite connection.
        valid_names: The registered non-primary host names (``app.hosts.HOSTS``
            keys). The primary host is NULL, never a candidate.

    Returns:
        Count of distinct stale host names cleared (0 when already consistent).
    """
    rows = conn.execute(
        "SELECT DISTINCT active_host FROM conversations"
        " WHERE active_host IS NOT NULL;"
    ).fetchall()
    unknown = [r["active_host"] for r in rows if r["active_host"] not in valid_names]
    if unknown:
        with conn:
            conn.executemany(
                "UPDATE conversations SET active_host = NULL WHERE active_host = ?;",
                [(name,) for name in unknown],
            )
    return len(unknown)


def set_conversation_think_mode(
    conn: sqlite3.Connection, conversation_id: int, think_mode: str
) -> Conversation:
    """Update the per-chat thinking mode.

    Does NOT bump ``updated_at`` (same as the other setters here).

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation to update.
        think_mode: ``'default'`` or ``'off'``. The caller coerces unknown
            values to ``'default'`` so a hand-crafted request can't persist a
            value that would resolve to ``think=true`` and 400 a non-thinking
            model.

    Returns:
        The updated Conversation.

    Raises:
        LookupError: If no conversation exists with that id.
    """
    with conn:
        row = conn.execute(
            "UPDATE conversations"
            " SET think_mode = ?"
            " WHERE id = ?"
            " RETURNING id, name, model, name_locked, temperature, tool_iteration_cap,"
            "          active_host, project_id, created_at, updated_at, think_mode, agent_id;",
            (think_mode, conversation_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_conversation(row)


def set_conversation_agent(
    conn: sqlite3.Connection, conversation_id: int, agent_id: int | None
) -> Conversation:
    """Attach ``agent_id`` to a chat, or clear it (None → Normal).

    Does NOT bump ``updated_at`` (same as the other setters here) — switching
    agents isn't a message event, so it shouldn't reorder the sidebar.

    Args:
        conn: Open SQLite connection.
        conversation_id: Chat to update.
        agent_id: Agent id to attach, or None to detach. Caller validates the
            agent exists (an unknown id is coerced to None at the route).

    Returns:
        The updated Conversation.

    Raises:
        LookupError: If no conversation exists with that id.
    """
    with conn:
        row = conn.execute(
            "UPDATE conversations SET agent_id = ?"
            " WHERE id = ?"
            " RETURNING id, name, model, name_locked, temperature, tool_iteration_cap,"
            "          active_host, project_id, created_at, updated_at, think_mode, agent_id;",
            (agent_id, conversation_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_conversation(row)
