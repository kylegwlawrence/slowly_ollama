"""Per-chat model for each non-primary Ollama host.

A chat can be routed to any configured host (see ``app.hosts``). The primary
host's model lives in ``conversations.model``; a non-primary host's per-chat
model lives here in ``chat_host_models``, keyed by host name. A missing row
means "use that host's default model" (the host's ``default_model`` from
config). Recording the host name lets a chat remember a distinct model per
machine, so switching hosts mid-chat recalls the right one.
"""

import sqlite3


def set_chat_host_model(
    conn: sqlite3.Connection,
    conversation_id: int,
    host_name: str,
    model: str,
) -> None:
    """Persist (upsert) the model a chat uses on one non-primary host.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation.
        host_name: The host's name (a key in ``app.hosts.HOSTS``, e.g.
            "host2"). The primary host is NOT stored here — its model lives
            in ``conversations.model``.
        model: The Ollama model tag to run on that host for this chat.

    The ``ON CONFLICT`` clause upserts on the ``(conversation_id, host_name)``
    primary key so re-selecting a model overwrites the prior choice.
    """
    with conn:
        conn.execute(
            "INSERT INTO chat_host_models (conversation_id, host_name, model)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT (conversation_id, host_name)"
            " DO UPDATE SET model = excluded.model;",
            (conversation_id, host_name, model),
        )


def get_chat_host_model(
    conn: sqlite3.Connection,
    conversation_id: int,
    host_name: str,
) -> str | None:
    """Return the model a chat uses on one non-primary host, or None.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation.
        host_name: The host's name (a key in ``app.hosts.HOSTS``).

    Returns:
        The stored model tag, or ``None`` when the chat has no remembered
        model for that host (the caller falls back to the host's
        ``default_model``).
    """
    row = conn.execute(
        "SELECT model FROM chat_host_models"
        " WHERE conversation_id = ? AND host_name = ?;",
        (conversation_id, host_name),
    ).fetchone()
    if row is None:
        return None
    # Index by position to work with both Row and tuple row factories.
    return row[0]
