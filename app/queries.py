"""Phase 4: dataclasses for the two row types and the query functions that
read/write them.

Each function takes a `sqlite3.Connection` (typically the long-lived shared
one from Phase 3) and wraps its work in `with conn:` so the operation is
atomic — partial state never lands in the DB if something raises mid-way.

Timestamps are stored as ISO 8601 TEXT in UTC and converted to/from
`datetime` at the boundary so callers work with proper datetime values
instead of strings.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

# Role values are constrained at the schema level (CHECK constraint in Phase 2)
# AND at the type level here. The Literal alias documents intent and lets a
# type checker catch wrong-role bugs before they hit SQLite.
Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class Conversation:
    """One row of the `conversations` table.

    Attributes:
        id: Auto-assigned primary key.
        name: Human-readable label shown in the sidebar.
        model: Ollama model identifier (e.g. "llama3:latest").
        created_at: When the row was first inserted (UTC).
        updated_at: When the row was last touched — bumped by rename, by
            appending a message, or by replacing the last assistant message.
            Used as the sort key for the sidebar so active chats float up.
    """

    id: int
    name: str
    model: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Message:
    """One row of the `messages` table.

    Attributes:
        id: Auto-assigned primary key.
        conversation_id: Foreign key into `conversations`.
        role: Either "user" or "assistant" (enforced by the schema CHECK).
        content: The message text.
        created_at: When the row was first inserted (UTC). For a regenerated
            assistant message the original timestamp is preserved so message
            order is unchanged.
    """

    id: int
    conversation_id: int
    role: Role
    content: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string for DB storage."""
    return datetime.now(timezone.utc).isoformat()


def _row_to_conversation(row: sqlite3.Row) -> Conversation:
    """Map a `conversations` row to the `Conversation` dataclass.

    Parses the stored ISO 8601 timestamps into `datetime` so the rest of the
    app doesn't deal in raw strings.
    """
    return Conversation(
        id=row["id"],
        name=row["name"],
        model=row["model"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_message(row: sqlite3.Row) -> Message:
    """Map a `messages` row to the `Message` dataclass."""
    return Message(
        id=row["id"],
        conversation_id=row["conversation_id"],
        role=row["role"],
        content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


def create_conversation(
    conn: sqlite3.Connection, name: str, model: str
) -> Conversation:
    """Insert a new conversation row.

    Args:
        conn: Open SQLite connection.
        name: Human-readable conversation name.
        model: Ollama model identifier this conversation will use.

    Returns:
        The newly created Conversation, populated with its assigned id and
        timestamps.
    """
    now = _now_iso()
    with conn:
        # RETURNING (SQLite 3.35+) avoids a follow-up SELECT to pick up the
        # auto-assigned id and the timestamps we just wrote.
        row = conn.execute(
            "INSERT INTO conversations (name, model, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " RETURNING id, name, model, created_at, updated_at;",
            (name, model, now, now),
        ).fetchone()
    return _row_to_conversation(row)


def get_conversation(
    conn: sqlite3.Connection, conversation_id: int
) -> Conversation:
    """Look up a single conversation by id.

    Phase 6's streaming endpoint uses this to read the conversation's
    model before calling Ollama.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id to look up.

    Returns:
        The matching Conversation.

    Raises:
        LookupError: If no conversation exists with that id.
    """
    row = conn.execute(
        "SELECT id, name, model, created_at, updated_at"
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
        "SELECT id, name, model, created_at, updated_at"
        " FROM conversations"
        " ORDER BY updated_at DESC, id DESC;"
    ).fetchall()
    return [_row_to_conversation(r) for r in rows]


def rename_conversation(
    conn: sqlite3.Connection, conversation_id: int, new_name: str
) -> Conversation:
    """Change a conversation's name; bumps its `updated_at`.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation to rename.
        new_name: Replacement name.

    Returns:
        The updated Conversation.

    Raises:
        LookupError: If no conversation exists with that id.
    """
    now = _now_iso()
    with conn:
        row = conn.execute(
            "UPDATE conversations SET name = ?, updated_at = ?"
            " WHERE id = ?"
            " RETURNING id, name, model, created_at, updated_at;",
            (new_name, now, conversation_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_conversation(row)


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


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def append_message(
    conn: sqlite3.Connection,
    conversation_id: int,
    role: Role,
    content: str,
) -> Message:
    """Append a message to a conversation.

    Bumps the parent conversation's `updated_at` in the same transaction
    so the message count and the sidebar's sort key can never diverge.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the parent conversation.
        role: Either "user" or "assistant".
        content: The message text.

    Returns:
        The newly inserted Message.

    Raises:
        sqlite3.IntegrityError: If `conversation_id` doesn't exist (FK), or
            if `role` is not one of the documented values (CHECK).
    """
    now = _now_iso()
    with conn:
        row = conn.execute(
            "INSERT INTO messages"
            " (conversation_id, role, content, created_at)"
            " VALUES (?, ?, ?, ?)"
            " RETURNING id, conversation_id, role, content, created_at;",
            (conversation_id, role, content, now),
        ).fetchone()
        # Bumping updated_at here (rather than via trigger) keeps all
        # mutation in one Python codepath — easier to reason about and to
        # search for "what touches updated_at" in the future.
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?;",
            (now, conversation_id),
        )
    return _row_to_message(row)


def list_messages(
    conn: sqlite3.Connection, conversation_id: int
) -> list[Message]:
    """Return all messages in a conversation, oldest first.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation whose messages to fetch.

    Returns:
        Messages ordered by `created_at ASC` (with `id ASC` as a stable
        tiebreaker for messages stamped within the same microsecond).
    """
    rows = conn.execute(
        "SELECT id, conversation_id, role, content, created_at"
        " FROM messages"
        " WHERE conversation_id = ?"
        " ORDER BY created_at ASC, id ASC;",
        (conversation_id,),
    ).fetchall()
    return [_row_to_message(r) for r in rows]


def replace_last_assistant_message(
    conn: sqlite3.Connection,
    conversation_id: int,
    new_content: str,
) -> Message:
    """Replace the content of the most-recent assistant message in place.

    Used by the regenerate flow. Keeps the original id and `created_at` so
    the message stays in the same position when the conversation is
    relisted. Bumps the conversation's `updated_at` since something
    visible changed.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation whose last assistant
            message should be replaced.
        new_content: Replacement text.

    Returns:
        The updated Message (same id, same created_at, new content).

    Raises:
        LookupError: If the conversation has no assistant message yet.
    """
    with conn:
        # SELECT-then-UPDATE is fine here because the app is single-user
        # and one process; no concurrent writer can sneak a row in between.
        # The ordering mirrors `list_messages` so "last assistant message"
        # always means the same thing across the codebase.
        latest = conn.execute(
            "SELECT id FROM messages"
            " WHERE conversation_id = ? AND role = 'assistant'"
            " ORDER BY created_at DESC, id DESC LIMIT 1;",
            (conversation_id,),
        ).fetchone()
        if latest is None:
            raise LookupError(
                f"Conversation {conversation_id} has no assistant message"
                " to replace."
            )
        row = conn.execute(
            "UPDATE messages SET content = ? WHERE id = ?"
            " RETURNING id, conversation_id, role, content, created_at;",
            (new_content, latest["id"]),
        ).fetchone()
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?;",
            (_now_iso(), conversation_id),
        )
    return _row_to_message(row)
