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
) -> Message:
    """Append a message to a conversation.

    Bumps the parent conversation's `updated_at` in the same transaction
    so the message count and the sidebar's sort key can never diverge.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the parent conversation.
        role: One of the `Role` literal values (currently "user",
            "assistant", "tool_call", "tool_result"). The type checker
            enforces this — the SQLite CHECK was dropped in phase 12a.
        content: The message text.
        prompt_tokens: Ollama's reported `prompt_eval_count` for the
            turn that produced this message. Only meaningful on
            assistant rows; pass None for user / tool_* rows and for
            assistant rows where Ollama didn't report counts.
        eval_tokens: Ollama's reported `eval_count` (tokens generated)
            for this assistant turn.

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
            "  prompt_tokens, eval_tokens)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " RETURNING id, conversation_id, role, content, created_at,"
            "  prompt_tokens, eval_tokens, archived_at;",
            (conversation_id, role, content, now,
             prompt_tokens, eval_tokens),
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
        "SELECT id, conversation_id, role, content, created_at,"
        "  prompt_tokens, eval_tokens, archived_at"
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

    Used by the generation layer (Phase 18): archived rows are excluded
    from the prompt sent to Ollama so that the user's manual ``Compact``
    action actually shrinks per-turn context. Rendering still uses
    ``list_messages`` (the full list) so the chat panel can show a
    ``▸ N archived messages`` disclosure.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation whose active messages to fetch.

    Returns:
        Messages with ``archived_at IS NULL`` ordered by ``created_at ASC``
        (with ``id ASC`` as a stable tiebreaker). An active ``summary`` row,
        when present, is included — it's the synthetic replacement the
        Compact endpoint produced and is the row Ollama should see.
    """
    rows = conn.execute(
        "SELECT id, conversation_id, role, content, created_at,"
        "  prompt_tokens, eval_tokens, archived_at"
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
    """Mark every active row in ``conversation_id`` with id < cutoff as archived.

    Phase 18: invoked by the manual-compact endpoint after the synthetic
    ``summary`` row has been inserted. The cutoff is the summary row's id,
    which (being the most recently inserted) is greater than every prior
    row, so ``id < cutoff`` selects everything except the summary itself.
    Bumps the conversation's ``updated_at`` so the sidebar's sort key
    reflects the change.

    Args:
        conn: Open SQLite connection.
        conversation_id: Conversation whose rows to archive.
        cutoff_message_id: Archive rows with ``id < cutoff_message_id``.

    Returns:
        Number of rows updated. Zero if nothing matched (idempotent
        re-run, or a chat with no prior history).
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
            "UPDATE messages SET content = ?,"
            "  prompt_tokens = ?, eval_tokens = ?"
            " WHERE id = ?"
            " RETURNING id, conversation_id, role, content, created_at,"
            "  prompt_tokens, eval_tokens, archived_at;",
            (new_content, prompt_tokens, eval_tokens, latest["id"]),
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

    Phase 11d's auto-titler uses this to decide whether to fire: it
    runs only when this count is 1, 2, or 3 (the first three assistant
    responses). After the third reply the title is considered "settled"
    and won't refresh on subsequent turns.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation to count messages for.

    Returns:
        The count of `role = 'assistant'` rows. Returns 0 for unknown
        conversation ids (no error — the caller's "if count not in 1..3"
        check naturally skips).
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM messages"
        " WHERE conversation_id = ? AND role = 'assistant';",
        (conversation_id,),
    ).fetchone()
    return row[0]
