"""Per-chat model for each non-primary Ollama host.

A chat can target any configured host (see ``app.hosts``). The primary host's
model lives in ``conversations.model``; a non-primary host's model lives in
``chat_host_models``, keyed by host name. A missing row means "use that host's
``default_model``". Keying by host lets a chat remember a distinct model per
machine and recall it when switching hosts mid-chat.
"""

import sqlite3


def set_chat_host_model(
    conn: sqlite3.Connection,
    conversation_id: int,
    host_name: str,
    model: str,
) -> None:
    """Upsert the model a chat uses on one non-primary host.

    Re-selecting a model overwrites the prior choice (upsert on the
    ``(conversation_id, host_name)`` primary key).

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the conversation.
        host_name: Host key in ``app.hosts.HOSTS`` (e.g. "host2"). The
            primary host is NOT stored here — its model lives in
            ``conversations.model``.
        model: Ollama model tag to run on that host for this chat.
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
        host_name: Host key in ``app.hosts.HOSTS``.

    Returns:
        The stored model tag, or ``None`` if none is remembered (the caller
        falls back to the host's ``default_model``).
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
