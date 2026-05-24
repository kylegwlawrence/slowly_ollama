"""Per-chat tool + RAG-server enablement state.

Phase 15 / 15b: the chat panel exposes a chip per registered tool and
per configured RAG server; clicking flips the row in ``chat_tool_settings``
or ``chat_rag_settings``. Unseeded chats default to "all on" so existing
conversations don't need a backfill when a new tool / server is added.
"""

import sqlite3

from app.queries._models import ChatToolState, ChatRagState


# ---------------------------------------------------------------------------
# Tool chips (chat_tool_settings)
# ---------------------------------------------------------------------------


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
# RAG server chips (chat_rag_settings)
# ---------------------------------------------------------------------------


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
