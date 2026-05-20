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

# Role values are constrained at the type level here. The schema-level
# CHECK was dropped in phase 12a — the role set grows over time (12a
# adds tool_call/tool_result; 13a adds research_findings/review_verdict
# for the agentic loop's internal artifacts) and SQLite can't ALTER
# CHECK constraints, so we enforce here in Python instead. The Literal
# alias documents intent and lets a type checker catch wrong-role bugs
# before they hit SQLite.
Role = Literal[
    "user",
    "assistant",
    "tool_call",
    "tool_result",
    "research_findings",  # phase 13: research agent's per-iteration synthesis
    "review_verdict",     # phase 13: review agent's pass/fail verdict (JSON)
]


@dataclass(frozen=True)
class Conversation:
    """One row of the `conversations` table.

    Attributes:
        id: Auto-assigned primary key.
        name: Human-readable label shown in the sidebar.
        model: Ollama model identifier (e.g. "llama3:latest").
        name_locked: When True, the auto-titler must leave `name` alone.
            Flipped to True by `rename_conversation` so a manual rename
            always beats a later automated title refresh.
        created_at: When the row was first inserted (UTC).
        updated_at: When the row was last touched — bumped by rename, by
            appending a message, or by replacing the last assistant message.
            Used as the sort key for the sidebar so active chats float up.
    """

    id: int
    name: str
    model: str
    name_locked: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Message:
    """One row of the `messages` table.

    Attributes:
        id: Auto-assigned primary key.
        conversation_id: Foreign key into `conversations`.
        role: One of the values in the `Role` literal alias above.
            Phase 12a widened this to include "tool_call" and
            "tool_result"; validation now lives in Python (the schema
            CHECK was dropped in the same phase).
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
    app doesn't deal in raw strings. `name_locked` is stored as INTEGER in
    SQLite (0 or 1); `bool()` widens it to the Python type the dataclass
    declares.
    """
    return Conversation(
        id=row["id"],
        name=row["name"],
        model=row["model"],
        name_locked=bool(row["name_locked"]),
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
        # auto-assigned id and the timestamps we just wrote. New rows always
        # start unlocked (name_locked = 0) — phase 11d's auto-titler is
        # free to refresh the placeholder until the user manually renames.
        row = conn.execute(
            "INSERT INTO conversations"
            " (name, model, name_locked, created_at, updated_at)"
            " VALUES (?, ?, 0, ?, ?)"
            " RETURNING id, name, model, name_locked,"
            "          created_at, updated_at;",
            (name, model, now, now),
        ).fetchone()
    return _row_to_conversation(row)


def get_conversation(
    conn: sqlite3.Connection, conversation_id: int
) -> Conversation:
    """Look up a single conversation by id.

    Phase 6's streaming endpoint uses this to read the conversation's
    model before calling Ollama; phase 11d's auto-titler also reads
    `name_locked` from the returned dataclass.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id to look up.

    Returns:
        The matching Conversation.

    Raises:
        LookupError: If no conversation exists with that id.
    """
    row = conn.execute(
        "SELECT id, name, model, name_locked, created_at, updated_at"
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
        "SELECT id, name, model, name_locked, created_at, updated_at"
        " FROM conversations"
        " ORDER BY updated_at DESC, id DESC;"
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
            " RETURNING id, name, model, name_locked,"
            "          created_at, updated_at;",
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
            " RETURNING id, name, model, name_locked,"
            "          created_at, updated_at;",
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
        role: One of the `Role` literal values (currently "user",
            "assistant", "tool_call", "tool_result"). The type checker
            enforces this — the SQLite CHECK was dropped in phase 12a.
        content: The message text.

    Returns:
        The newly inserted Message.

    Raises:
        sqlite3.IntegrityError: If `conversation_id` doesn't exist (FK).
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


# ---------------------------------------------------------------------------
# Settings (phase 13)
# ---------------------------------------------------------------------------


def get_setting(
    conn: sqlite3.Connection, key: str, default: str | None = None
) -> str | None:
    """Read a single app_settings row by key.

    Args:
        conn: Open SQLite connection.
        key: Setting key (e.g. ``"agentic_mode"``).
        default: Returned when no row exists for the key.

    Returns:
        The stored value as a string, or ``default`` when the key
        hasn't been set.
    """
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?;", (key,)
    ).fetchone()
    return row["value"] if row is not None else default


def set_setting(
    conn: sqlite3.Connection, key: str, value: str
) -> None:
    """Upsert one app_settings row.

    Wraps the write in ``with conn:`` so the upsert lands atomically.

    Args:
        conn: Open SQLite connection.
        key: Setting key.
        value: Setting value as a string.
    """
    with conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
            (key, value),
        )


_AGENTIC_MODE_KEY = "agentic_mode"


def get_agentic_mode(conn: sqlite3.Connection) -> bool:
    """Return True when the multi-agent loop is enabled globally.

    Default (no row): False. Any value other than the literal string
    ``"on"`` also returns False, defensively.

    Args:
        conn: Open SQLite connection.
    """
    return get_setting(conn, _AGENTIC_MODE_KEY, default="off") == "on"


def set_agentic_mode(conn: sqlite3.Connection, enabled: bool) -> None:
    """Toggle the global agentic-mode setting.

    Args:
        conn: Open SQLite connection.
        enabled: True for ``"on"``, False for ``"off"``. Must be a real
            bool — strings like ``"off"`` would be truthy and write
            ``"on"``, silently flipping the setting the wrong way.

    Raises:
        TypeError: When ``enabled`` is not a bool. Cheap guard against
            the foot-gun above.
    """
    if not isinstance(enabled, bool):
        raise TypeError(
            f"set_agentic_mode requires a bool; got {type(enabled).__name__}"
        )
    set_setting(conn, _AGENTIC_MODE_KEY, "on" if enabled else "off")
