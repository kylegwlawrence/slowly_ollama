"""CRUD for the ``conversations`` table."""

import sqlite3
from datetime import datetime

from app._time import now_iso as _now_iso
from app.queries._models import Conversation


def _row_to_conversation(row: sqlite3.Row) -> Conversation:
    """Map a ``conversations`` row to the :class:`Conversation` dataclass.

    Parses the stored ISO 8601 timestamps into ``datetime`` so the rest of
    the app doesn't deal in raw strings. ``name_locked`` is stored as
    INTEGER in SQLite (0 or 1); ``bool()`` widens it to the Python type the
    dataclass declares.
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
    )


def create_conversation(
    conn: sqlite3.Connection,
    name: str,
    model: str,
    *,
    project_id: int | None = None,
    temperature: float = 0.8,
    tool_iteration_cap: int = 5,
    active_host: str | None = None,
) -> Conversation:
    """Insert a new conversation row.

    Args:
        conn: Open SQLite connection.
        name: Human-readable conversation name.
        model: Ollama model identifier this conversation will use.
        project_id: The project this chat lives in (phase 17 — every chat
            belongs to exactly one project). When omitted, the chat is
            assigned to the lowest-id project (the "Default" the migration
            creates). The FK enforces existence; pass an explicit value when
            you care which project owns the chat.
        temperature: Sampling temperature passed to Ollama (0.0–2.0).
        tool_iteration_cap: Per-turn cap on single-agent tool-call
            iterations (caller should clamp to 1–10).
        active_host: Name of the Ollama host to start the chat on (a key in
            `app.hosts.HOSTS`, e.g. "host2"), or None for the primary host.
            A non-primary host's per-chat model is stored separately via
            ``set_chat_host_model`` (the ``chat_host_models`` table).

    Returns:
        The newly created Conversation, populated with its assigned id and
        timestamps.

    Raises:
        LookupError: When ``project_id`` is omitted AND no projects exist
            (which should never happen in production — initialize_database
            guarantees the Default project).
    """
    if project_id is None:
        # Fallback: assume the Default project. Keeps the function ergonomic
        # for tests + tools that don't care which project a chat lands in.
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
        # RETURNING (SQLite 3.35+) avoids a follow-up SELECT to pick up the
        # auto-assigned id and the timestamps we just wrote. New rows always
        # start unlocked (name_locked = 0) — phase 11d's auto-titler is
        # free to refresh the placeholder until the user manually renames.
        row = conn.execute(
            "INSERT INTO conversations"
            " (name, model, name_locked, temperature, tool_iteration_cap,"
            "  active_host, project_id, created_at, updated_at)"
            " VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?)"
            " RETURNING id, name, model, name_locked, temperature, tool_iteration_cap,"
            "          active_host, project_id, created_at, updated_at, think_mode;",
            (
                name, model, temperature, tool_iteration_cap, active_host,
                project_id, now, now,
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
        " active_host, project_id, created_at, updated_at, think_mode"
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
        Conversations ordered by `updated_at DESC`. The sidebar surfaces
        this order so the chat the user just touched is on top.
    """
    rows = conn.execute(
        "SELECT id, name, model, name_locked, temperature, tool_iteration_cap,"
        " active_host, project_id, created_at, updated_at, think_mode"
        " FROM conversations"
        " ORDER BY updated_at DESC, id DESC;"
    ).fetchall()
    return [_row_to_conversation(r) for r in rows]


def list_conversations_in_project(
    conn: sqlite3.Connection, project_id: int
) -> list[Conversation]:
    """Return every conversation in a project, most-recently-updated first.

    Same ordering convention as :func:`list_conversations` (updated_at DESC,
    id DESC) so the most recently touched chat floats to the top.

    Args:
        conn: Open SQLite connection.
        project_id: The project whose conversations to list.

    Returns:
        Conversations in the project, ordered by ``updated_at DESC``. Empty
        list when the project exists but has no chats yet (or doesn't exist).
    """
    rows = conn.execute(
        "SELECT id, name, model, name_locked, temperature, tool_iteration_cap,"
        " active_host, project_id, created_at, updated_at, think_mode"
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

    Bumps `updated_at` and flips `name_locked` to 1 in the same write so
    the auto-titler's subsequent runs see the lock and skip. The
    business rule: a deliberate human action always wins over the next
    automated refresh — even if the model was about to produce a great
    title.

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
            "          active_host, project_id, created_at, updated_at, think_mode;",
            (new_name, now, conversation_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_conversation(row)


def set_name_auto(
    conn: sqlite3.Connection, conversation_id: int, new_name: str
) -> Conversation | None:
    """Auto-set the name iff it hasn't been manually renamed yet.

    Used by phase 11d's title-generation flow. The `WHERE name_locked = 0`
    clause is the race-condition guard: if the user clicks Rename between
    the title request firing and this UPDATE running, the row's
    `name_locked` is already 1 and the UPDATE matches zero rows. Returning
    None lets the caller skip the OOB sidebar swap entirely so the
    just-set manual name stays put.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation whose name to refresh.
        new_name: Model-generated title.

    Returns:
        The updated Conversation if the write landed; None if the row
        was locked or the id didn't exist (both treated the same — the
        caller has nothing to do in either case).
    """
    now = _now_iso()
    with conn:
        row = conn.execute(
            "UPDATE conversations"
            " SET name = ?, updated_at = ?"
            " WHERE id = ? AND name_locked = 0"
            " RETURNING id, name, model, name_locked, temperature, tool_iteration_cap,"
            "          active_host, project_id, created_at, updated_at, think_mode;",
            (new_name, now, conversation_id),
        ).fetchone()
    return _row_to_conversation(row) if row is not None else None


def delete_conversation(
    conn: sqlite3.Connection, conversation_id: int
) -> None:
    """Delete a conversation and (via FK cascade) all its messages.

    Idempotent: no error if the conversation is already gone. The UI flow
    is "user clicks delete"; a stale id shouldn't surface as an exception.

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
            "          active_host, project_id, created_at, updated_at, think_mode;",
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
            "          active_host, project_id, created_at, updated_at, think_mode;",
            (tool_iteration_cap, conversation_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_conversation(row)


def set_active_host(
    conn: sqlite3.Connection, conversation_id: int, host_name: str | None
) -> Conversation:
    """Set (or clear) the selected Ollama host for a conversation.

    Does NOT bump ``updated_at`` — switching hosts isn't a message event and
    shouldn't reorder the sidebar (same convention as the temperature / tool-
    cap setters above).

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation to update.
        host_name: A host key from `app.hosts.HOSTS`, or None to return the
            chat to the primary host. Caller validates the name (routes resolve
            it via `app.hosts.get_host`).

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
            "          active_host, project_id, created_at, updated_at, think_mode;",
            (host_name, conversation_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_conversation(row)


def set_conversation_think_mode(
    conn: sqlite3.Connection, conversation_id: int, think_mode: str
) -> Conversation:
    """Update the per-chat thinking mode (phase 25).

    Does NOT bump ``updated_at`` — toggling thinking isn't a message event
    and shouldn't reorder the sidebar (same convention as the temperature /
    tool-cap / active-host setters above).

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation to update.
        think_mode: One of ``'default'`` or ``'off'``. The caller (route)
            validates and coerces unknown values to ``'default'`` so a
            hand-crafted request can't persist a value that would later
            resolve to ``think=true`` and 400 a non-thinking model.

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
            "          active_host, project_id, created_at, updated_at, think_mode;",
            (think_mode, conversation_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_conversation(row)
