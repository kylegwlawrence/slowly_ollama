"""CRUD for the ``messages`` table."""

import sqlite3
from datetime import datetime

from app._time import now_iso as _now_iso
from app.queries._models import Message, Role


def _row_to_message(row: sqlite3.Row) -> Message:
    """Map a ``messages`` row to the :class:`Message` dataclass."""
    archived_at_raw = row["archived_at"]
    return Message(
        id=row["id"],
        conversation_id=row["conversation_id"],
        role=row["role"],
        content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]),
        prompt_tokens=row["prompt_tokens"],
        eval_tokens=row["eval_tokens"],
        duration_ms=row["duration_ms"],
        thinking=row["thinking"],
        archived_at=(
            datetime.fromisoformat(archived_at_raw)
            if archived_at_raw is not None
            else None
        ),
    )


def append_message(
    conn: sqlite3.Connection,
    conversation_id: int,
    role: Role,
    content: str,
    *,
    prompt_tokens: int | None = None,
    eval_tokens: int | None = None,
    duration_ms: int | None = None,
    thinking: str | None = None,
) -> Message:
    """Append a message to a conversation.

    Bumps the parent's `updated_at` in the same transaction so the message
    count and the sidebar's sort key never diverge.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the parent conversation.
        role: One of the `Role` literal values (validated in Python).
        content: The message text.
        prompt_tokens: Ollama's `prompt_eval_count` for the turn. Only
            meaningful on assistant rows; pass None otherwise and when Ollama
            reported no counts.
        eval_tokens: Ollama's `eval_count` (tokens generated) for this turn.
        duration_ms: Wall-clock generation time for the turn, in milliseconds.
            Only meaningful on assistant rows; pass None otherwise.
        thinking: A thinking model's accumulated reasoning for the turn. Only
            meaningful on assistant rows; pass None otherwise and on
            non-reasoning turns.

    Returns:
        The newly inserted Message.

    Raises:
        sqlite3.IntegrityError: If `conversation_id` doesn't exist (FK).
    """
    now = _now_iso()
    with conn:
        row = conn.execute(
            "INSERT INTO messages"
            " (conversation_id, role, content, created_at,"
            "  prompt_tokens, eval_tokens, duration_ms, thinking)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " RETURNING id, conversation_id, role, content, created_at,"
            "  prompt_tokens, eval_tokens, duration_ms, thinking, archived_at;",
            (conversation_id, role, content, now,
             prompt_tokens, eval_tokens, duration_ms, thinking),
        ).fetchone()
        # Bump updated_at in Python (not a trigger) to keep all mutation in
        # one codepath that's easy to find when asking "what touches it?".
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
        Messages ordered by `created_at ASC`, `id ASC` (stable tiebreaker
        for rows stamped in the same microsecond).
    """
    rows = conn.execute(
        "SELECT id, conversation_id, role, content, created_at,"
        "  prompt_tokens, eval_tokens, duration_ms, thinking, archived_at"
        " FROM messages"
        " WHERE conversation_id = ?"
        " ORDER BY created_at ASC, id ASC;",
        (conversation_id,),
    ).fetchall()
    return [_row_to_message(r) for r in rows]


def list_active_messages(
    conn: sqlite3.Connection, conversation_id: int
) -> list[Message]:
    """Return non-archived messages in a conversation, oldest first.

    Used by the generation layer: archived rows are excluded from the prompt
    so the user's manual ``Compact`` actually shrinks per-turn context.
    Rendering still uses ``list_messages`` (the full list) to show a
    ``▸ N archived messages`` disclosure.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation whose active messages to fetch.

    Returns:
        Messages with ``archived_at IS NULL``, ordered ``created_at ASC``,
        ``id ASC``. An active ``summary`` row, if present, is included — it's
        the synthetic replacement Ollama should see.
    """
    rows = conn.execute(
        "SELECT id, conversation_id, role, content, created_at,"
        "  prompt_tokens, eval_tokens, duration_ms, thinking, archived_at"
        " FROM messages"
        " WHERE conversation_id = ? AND archived_at IS NULL"
        " ORDER BY created_at ASC, id ASC;",
        (conversation_id,),
    ).fetchall()
    return [_row_to_message(r) for r in rows]


def archive_messages_before(
    conn: sqlite3.Connection,
    conversation_id: int,
    cutoff_message_id: int,
) -> int:
    """Archive every active row in ``conversation_id`` with id < cutoff.

    Invoked by the manual-compact endpoint after inserting the synthetic
    ``summary`` row. The cutoff is the summary's id (the newest), so
    ``id < cutoff`` archives everything except the summary itself. Bumps
    ``updated_at`` so the sidebar reflects the change.

    Args:
        conn: Open SQLite connection.
        conversation_id: Conversation whose rows to archive.
        cutoff_message_id: Archive rows with ``id < cutoff_message_id``.

    Returns:
        Number of rows updated. Zero if nothing matched (idempotent re-run,
        or no prior history).
    """
    now = _now_iso()
    with conn:
        cursor = conn.execute(
            "UPDATE messages SET archived_at = ?"
            " WHERE conversation_id = ?"
            "   AND id < ?"
            "   AND archived_at IS NULL;",
            (now, conversation_id, cutoff_message_id),
        )
        if cursor.rowcount > 0:
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?;",
                (now, conversation_id),
            )
        return cursor.rowcount


def replace_last_assistant_message(
    conn: sqlite3.Connection,
    conversation_id: int,
    new_content: str,
    *,
    prompt_tokens: int | None = None,
    eval_tokens: int | None = None,
    duration_ms: int | None = None,
    thinking: str | None = None,
) -> Message:
    """Replace the most-recent assistant message's content in place.

    Used by the regenerate flow. Keeps the original id and `created_at` so
    the message stays in position; bumps `updated_at` since content changed.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation whose last assistant message
            to replace.
        new_content: Replacement text.
        prompt_tokens: Ollama's `prompt_eval_count` for the regenerated turn.
        eval_tokens: Ollama's `eval_count` for the regenerated turn.
        duration_ms: Wall-clock generation time for the regenerated turn, in
            milliseconds.
        thinking: The regenerated turn's accumulated reasoning, or None.

    Returns:
        The updated Message (same id and created_at, new content).

    Raises:
        LookupError: If the conversation has no assistant message yet.
    """
    with conn:
        # SELECT-then-UPDATE is safe: the app is single-process single-user,
        # so no concurrent writer can sneak in. Ordering mirrors
        # `list_messages` so "last assistant message" is consistent.
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
            "UPDATE messages SET content = ?,"
            "  prompt_tokens = ?, eval_tokens = ?, duration_ms = ?,"
            "  thinking = ?"
            " WHERE id = ?"
            " RETURNING id, conversation_id, role, content, created_at,"
            "  prompt_tokens, eval_tokens, duration_ms, thinking, archived_at;",
            (new_content, prompt_tokens, eval_tokens, duration_ms, thinking,
             latest["id"]),
        ).fetchone()
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?;",
            (_now_iso(), conversation_id),
        )
    return _row_to_message(row)


def count_assistant_messages(
    conn: sqlite3.Connection, conversation_id: int
) -> int:
    """Return the number of assistant messages in a conversation.

    The auto-titler uses this to decide whether to fire: only when the count
    is 1, 2, or 3 (the first three replies). After that the title is
    "settled" and won't refresh.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation to count messages for.

    Returns:
        The count of `role = 'assistant'` rows; 0 for unknown ids (the
        caller's "if count not in 1..3" check naturally skips).
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM messages"
        " WHERE conversation_id = ? AND role = 'assistant';",
        (conversation_id,),
    ).fetchone()
    return row[0]
