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
# CHECK was dropped in phase 12a (12a added tool_call/tool_result) and
# SQLite can't ALTER CHECK constraints, so we enforce here in Python
# instead. The Literal alias documents intent and lets a type checker
# catch wrong-role bugs before they hit SQLite.
Role = Literal[
    "user",
    "assistant",
    "tool_call",
    "tool_result",
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
        active_agent: Name of the user-invoked agent currently active for this
            chat (a key in `app.agents.AGENTS`), or None for the default
            "Normal" plain-chat behavior. Persisted so the picker + indicator
            survive reloads.
    """

    id: int
    name: str
    model: str
    name_locked: bool
    temperature: float
    tool_iteration_cap: int
    created_at: datetime
    updated_at: datetime
    active_agent: str | None = None


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
        temperature=float(row["temperature"]),
        tool_iteration_cap=int(row["tool_iteration_cap"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        active_agent=row["active_agent"],
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
    conn: sqlite3.Connection,
    name: str,
    model: str,
    temperature: float = 0.8,
    tool_iteration_cap: int = 5,
    active_agent: str | None = None,
) -> Conversation:
    """Insert a new conversation row.

    Args:
        conn: Open SQLite connection.
        name: Human-readable conversation name.
        model: Ollama model identifier this conversation will use.
        temperature: Sampling temperature passed to Ollama (0.0–2.0).
        tool_iteration_cap: Per-turn cap on single-agent tool-call
            iterations (caller should clamp to 1–10).
        active_agent: Name of the user-invoked agent to start the chat with
            (a key in `app.agents.AGENTS`), or None for Normal plain chat.

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
            " (name, model, name_locked, temperature, tool_iteration_cap, active_agent, created_at, updated_at)"
            " VALUES (?, ?, 0, ?, ?, ?, ?, ?)"
            " RETURNING id, name, model, name_locked, temperature, tool_iteration_cap,"
            "          active_agent, created_at, updated_at;",
            (name, model, temperature, tool_iteration_cap, active_agent, now, now),
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
        "SELECT id, name, model, name_locked, temperature, tool_iteration_cap,"
        " active_agent, created_at, updated_at"
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
        " active_agent, created_at, updated_at"
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
            " RETURNING id, name, model, name_locked, temperature, tool_iteration_cap,"
            "          active_agent, created_at, updated_at;",
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
            "          active_agent, created_at, updated_at;",
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
            "          active_agent, created_at, updated_at;",
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
            "          active_agent, created_at, updated_at;",
            (tool_iteration_cap, conversation_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_conversation(row)


def set_active_agent(
    conn: sqlite3.Connection, conversation_id: int, agent_name: str | None
) -> Conversation:
    """Set (or clear) the user-invoked agent active for a conversation.

    Does NOT bump ``updated_at`` — switching agents isn't a message event and
    shouldn't reorder the sidebar (same convention as the temperature / tool-
    cap setters above).

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation to update.
        agent_name: An agent key from `app.agents.AGENTS`, or None to return
            the chat to Normal plain-chat behavior. Caller validates the name
            (routes resolve it via `app.agents.get_agent`).

    Returns:
        The updated Conversation.

    Raises:
        LookupError: If no conversation exists with that id.
    """
    with conn:
        row = conn.execute(
            "UPDATE conversations"
            " SET active_agent = ?"
            " WHERE id = ?"
            " RETURNING id, name, model, name_locked, temperature, tool_iteration_cap,"
            "          active_agent, created_at, updated_at;",
            (agent_name, conversation_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_conversation(row)


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
        key: Setting key (e.g. ``"default_temperature"``).
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


_DEFAULT_TEMPERATURE_KEY = "default_temperature"
_DEFAULT_TEMPERATURE_FALLBACK = 0.7


def get_default_temperature(conn: sqlite3.Connection) -> float:
    """Return the global default sampling temperature for new chats.

    Default (no row): ``0.7``. The stored value is clamped to the
    [0.0, 2.0] range Ollama accepts; a malformed row (non-numeric,
    written by a hand-crafted request) falls back to ``0.7`` rather
    than raising, so a corrupt setting can never break chat creation.

    Args:
        conn: Open SQLite connection.
    """
    raw = get_setting(conn, _DEFAULT_TEMPERATURE_KEY, default=None)
    if raw is None:
        return _DEFAULT_TEMPERATURE_FALLBACK
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TEMPERATURE_FALLBACK
    return max(0.0, min(2.0, value))


def set_default_temperature(
    conn: sqlite3.Connection, temperature: float
) -> None:
    """Persist the global default sampling temperature for new chats.

    Clamps to [0.0, 2.0] before storing so an out-of-range value can't
    be read back later. Stored as a string (the app_settings value
    column is text).

    Args:
        conn: Open SQLite connection.
        temperature: New default temperature (clamped to 0.0–2.0).
    """
    clamped = max(0.0, min(2.0, float(temperature)))
    set_setting(conn, _DEFAULT_TEMPERATURE_KEY, str(clamped))


# ---------------------------------------------------------------------------
# Phase 15: per-chat tool enablement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatToolState:
    """Enabled/disabled state of one tool for one conversation.

    Attributes:
        tool_name: Registered name of the tool (matches TOOLS key).
        enabled: True when the tool is active for this conversation.
    """

    tool_name: str
    enabled: bool


def seed_chat_tools(
    conn: sqlite3.Connection,
    conversation_id: int,
    tool_names: list[str],
    *,
    enabled_names: set[str] | None = None,
) -> None:
    """Insert default tool rows for a new conversation.

    Uses INSERT OR IGNORE so re-runs are safe (idempotent). Called at
    chat creation time so every new chat starts with explicit rows.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the newly-created conversation.
        tool_names: All currently-registered tool names (from TOOLS.keys()).
        enabled_names: When provided, only these names are seeded as
            enabled=1. All others get enabled=0. None → all tools enabled.
    """
    rows = [
        (conversation_id, name, 1 if (enabled_names is None or name in enabled_names) else 0)
        for name in tool_names
    ]
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO chat_tool_settings"
            " (conversation_id, tool_name, enabled) VALUES (?, ?, ?);",
            rows,
        )


def get_chat_tool_states(
    conn: sqlite3.Connection,
    conversation_id: int,
    all_tool_names: list[str],
) -> list[ChatToolState]:
    """Return enabled/disabled state for every tool in all_tool_names.

    Tools with no row (unseeded conversations) default to enabled=True so
    existing chats behave as if all tools are on without needing a migration.

    Args:
        conn: Open SQLite connection.
        conversation_id: Conversation to look up.
        all_tool_names: Canonical list from TOOLS.keys().

    Returns:
        One ChatToolState per entry in all_tool_names, in the same order.
    """
    rows = conn.execute(
        "SELECT tool_name, enabled FROM chat_tool_settings"
        " WHERE conversation_id = ?;",
        (conversation_id,),
    ).fetchall()
    stored = {row["tool_name"]: bool(row["enabled"]) for row in rows}
    return [
        ChatToolState(tool_name=name, enabled=stored.get(name, True))
        for name in all_tool_names
    ]


def toggle_chat_tool(
    conn: sqlite3.Connection,
    conversation_id: int,
    tool_name: str,
) -> bool:
    """Flip the enabled state of one tool for one conversation.

    Unseeded tools are treated as currently on, so the first toggle
    inserts a disabled row (on → off). Subsequent toggles XOR-flip the
    stored value.

    Args:
        conn: Open SQLite connection.
        conversation_id: Conversation whose tool to toggle.
        tool_name: Name of the tool to toggle.

    Returns:
        True if the tool is now enabled, False if now disabled.
    """
    with conn:
        row = conn.execute(
            "INSERT INTO chat_tool_settings (conversation_id, tool_name, enabled)"
            " VALUES (?, ?, 0)"
            " ON CONFLICT(conversation_id, tool_name)"
            " DO UPDATE SET enabled = 1 - enabled"
            " RETURNING enabled;",
            (conversation_id, tool_name),
        ).fetchone()
    return bool(row["enabled"])


def get_enabled_tool_names(
    conn: sqlite3.Connection,
    conversation_id: int,
    all_tool_names: list[str],
) -> list[str]:
    """Return only the tool names that are enabled for a conversation.

    Used by _run_generation to build the filtered tools payload. Unseeded
    tools are treated as enabled.

    Args:
        conn: Open SQLite connection.
        conversation_id: Conversation to look up.
        all_tool_names: Canonical list from TOOLS.keys().

    Returns:
        Subset of all_tool_names where enabled (including unseeded tools).
    """
    states = get_chat_tool_states(conn, conversation_id, all_tool_names)
    return [s.tool_name for s in states if s.enabled]


# ---------------------------------------------------------------------------
# Phase 15b: per-chat RAG server enablement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatRagState:
    """Enabled/disabled state of one RAG server for one conversation.

    Attributes:
        server_name: Unique name from rag_servers.name (e.g. ``"arxiv"``).
        enabled: True when this server's chip is toggled on for the chat.
    """

    server_name: str
    enabled: bool


def seed_chat_rag_servers(
    conn: sqlite3.Connection,
    conversation_id: int,
    server_names: list[str],
    *,
    enabled_names: set[str] | None = None,
) -> None:
    """Insert default RAG server rows for a new conversation.

    Uses INSERT OR IGNORE so re-runs are safe (idempotent). Called at
    chat creation time alongside ``seed_chat_tools``.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the newly-created conversation.
        server_names: All currently-configured server names.
        enabled_names: When provided, only these names are seeded as
            enabled=1. All others get enabled=0. None → all enabled.
    """
    rows = [
        (
            conversation_id,
            name,
            1 if (enabled_names is None or name in enabled_names) else 0,
        )
        for name in server_names
    ]
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO chat_rag_settings"
            " (conversation_id, server_name, enabled) VALUES (?, ?, ?);",
            rows,
        )


def get_chat_rag_states(
    conn: sqlite3.Connection,
    conversation_id: int,
    all_server_names: list[str],
) -> list[ChatRagState]:
    """Return enabled/disabled state for every server in all_server_names.

    Servers with no row (unseeded conversations or newly-added servers)
    default to enabled=True so existing chats see new sources without
    explicit seeding.

    Args:
        conn: Open SQLite connection.
        conversation_id: Conversation to look up.
        all_server_names: Current snapshot of configured server names.

    Returns:
        One ChatRagState per entry in all_server_names, in the same order.
    """
    rows = conn.execute(
        "SELECT server_name, enabled FROM chat_rag_settings"
        " WHERE conversation_id = ?;",
        (conversation_id,),
    ).fetchall()
    stored = {row["server_name"]: bool(row["enabled"]) for row in rows}
    return [
        ChatRagState(server_name=name, enabled=stored.get(name, True))
        for name in all_server_names
    ]


def toggle_chat_rag_server(
    conn: sqlite3.Connection,
    conversation_id: int,
    server_name: str,
) -> bool:
    """Flip the enabled state of one RAG server for one conversation.

    Unseeded servers are treated as currently on, so the first toggle
    inserts a disabled row (on → off). Subsequent toggles XOR-flip the
    stored value.

    Args:
        conn: Open SQLite connection.
        conversation_id: Conversation whose RAG server to toggle.
        server_name: Name of the RAG server to toggle.

    Returns:
        True if the server is now enabled, False if now disabled.
    """
    with conn:
        row = conn.execute(
            "INSERT INTO chat_rag_settings (conversation_id, server_name, enabled)"
            " VALUES (?, ?, 0)"
            " ON CONFLICT(conversation_id, server_name)"
            " DO UPDATE SET enabled = 1 - enabled"
            " RETURNING enabled;",
            (conversation_id, server_name),
        ).fetchone()
    return bool(row["enabled"])


def get_enabled_rag_server_names(
    conn: sqlite3.Connection,
    conversation_id: int,
    all_server_names: list[str],
) -> list[str]:
    """Return only the RAG server names that are enabled for a conversation.

    Used by _run_generation to filter the query_rag source list. Unseeded
    servers are treated as enabled.

    Args:
        conn: Open SQLite connection.
        conversation_id: Conversation to look up.
        all_server_names: Current snapshot of configured server names.

    Returns:
        Subset of all_server_names where enabled (including unseeded servers).
    """
    states = get_chat_rag_states(conn, conversation_id, all_server_names)
    return [s.server_name for s in states if s.enabled]
