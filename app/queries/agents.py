"""CRUD for the ``agents`` table — reusable personas (named system prompts).

Mirrors :mod:`app.queries.projects`. An agent bundles a ``name`` +
``system_prompt`` (+ an optional preferred ``default_model``); any chat can
attach one via ``conversations.agent_id``. The agent prompt stacks BEFORE the
project prompt each turn (see :mod:`app.generation`). Global, not
project-scoped, so a persona is reusable across projects. Introduced in
Phase 29.
"""

import sqlite3
from datetime import datetime

from app._time import now_iso as _now_iso
from app.queries._models import Agent, _Unset, _UNSET
# Reuse the project prompt cap so agent + project prompts share one limit.
from app.queries.projects import SYSTEM_PROMPT_MAX_CHARS


_AGENT_COLS = "id, name, system_prompt, default_model, created_at, updated_at"

# Max length of an agent's display name. Enforced server-side here and in the
# route layer, and surfaced to the input's ``maxlength`` via route context.
AGENT_NAME_MAX_CHARS = 80


def _row_to_agent(row: sqlite3.Row) -> Agent:
    """Map an ``agents`` row to the :class:`Agent` dataclass."""
    return Agent(
        id=row["id"],
        name=row["name"],
        system_prompt=row["system_prompt"],
        default_model=row["default_model"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def list_agents(conn: sqlite3.Connection) -> list[Agent]:
    """Return every agent, alphabetically by name (case-insensitive).

    Args:
        conn: Open SQLite connection.

    Returns:
        All agents ordered by ``name COLLATE NOCASE ASC`` — the picker + the
        settings list share this order; alphabetical is more stable than
        created_at since the user thinks of a persona by name.
    """
    rows = conn.execute(
        f"SELECT {_AGENT_COLS} FROM agents"
        f" ORDER BY name COLLATE NOCASE ASC;"
    ).fetchall()
    return [_row_to_agent(r) for r in rows]


def get_agent(conn: sqlite3.Connection, agent_id: int) -> Agent:
    """Look up an agent by id.

    Args:
        conn: Open SQLite connection.
        agent_id: Id to look up.

    Returns:
        The matching Agent.

    Raises:
        LookupError: When no agent exists with that id.
    """
    row = conn.execute(
        f"SELECT {_AGENT_COLS} FROM agents WHERE id = ?;", (agent_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"Agent {agent_id} not found.")
    return _row_to_agent(row)


def get_agent_for_conversation(
    conn: sqlite3.Connection, conversation_id: int
) -> Agent | None:
    """Return the agent attached to ``conversation_id``, or None if none/unknown.

    Unlike :func:`app.queries.projects.get_project_for_conversation` this never
    raises — "no agent" is the common case, and a missing conversation degrades
    to None so a turn still runs.

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the chat to resolve.

    Returns:
        The attached Agent, or None when the chat has no agent (or doesn't
        exist).
    """
    row = conn.execute(
        "SELECT a.id, a.name, a.system_prompt, a.default_model,"
        " a.created_at, a.updated_at"
        " FROM agents a JOIN conversations c ON c.agent_id = a.id"
        " WHERE c.id = ?;",
        (conversation_id,),
    ).fetchone()
    return _row_to_agent(row) if row is not None else None


def create_agent(
    conn: sqlite3.Connection,
    name: str,
    system_prompt: str = "",
    default_model: str | None = None,
) -> Agent:
    """Insert a new agent row.

    Args:
        conn: Open SQLite connection.
        name: Display name. Caller handles ``.strip()`` / length validation;
            must be unique (UNIQUE constraint).
        system_prompt: The persona's system prompt. Clamped defensively to
            ``SYSTEM_PROMPT_MAX_CHARS`` (the route enforces it too).
        default_model: Preferred model, or None. Informational in Phase 29.

    Returns:
        The newly created Agent, with its assigned id and timestamps.

    Raises:
        sqlite3.IntegrityError: When ``name`` already exists (UNIQUE); the
            route maps this to 409.
    """
    now = _now_iso()
    with conn:
        row = conn.execute(
            "INSERT INTO agents"
            " (name, system_prompt, default_model, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            f" RETURNING {_AGENT_COLS};",
            (name, system_prompt[:SYSTEM_PROMPT_MAX_CHARS], default_model, now, now),
        ).fetchone()
    return _row_to_agent(row)


def update_agent(
    conn: sqlite3.Connection,
    agent_id: int,
    *,
    name: str | None = None,
    system_prompt: str | None = None,
    default_model: "str | None | _Unset" = _UNSET,
) -> Agent:
    """Update editable agent fields. Each kwarg is optional.

    ``default_model`` uses the ``_UNSET`` sentinel to distinguish "not passed"
    from "set to NULL" — the editor must be able to clear a previously-set
    preference, which a plain ``None`` default couldn't express (mirrors
    ``update_project``).

    Args:
        conn: Open SQLite connection.
        agent_id: Id of the agent to update.
        name: New display name (``None`` = leave alone).
        system_prompt: New prompt (``""`` to clear), or ``None`` (default) to
            leave alone. Clamped to ``SYSTEM_PROMPT_MAX_CHARS``.
        default_model: New model, ``None`` to clear, or ``_UNSET`` (default) to
            leave alone.

    Returns:
        The updated Agent (unchanged when no kwargs were passed).

    Raises:
        LookupError: When the agent does not exist.
        sqlite3.IntegrityError: When ``name`` collides with another agent.
    """
    sets: list[str] = []
    args: list = []
    if name is not None:
        sets.append("name = ?")
        args.append(name)
    if system_prompt is not None:
        sets.append("system_prompt = ?")
        args.append(system_prompt[:SYSTEM_PROMPT_MAX_CHARS])
    if not isinstance(default_model, _Unset):
        sets.append("default_model = ?")
        args.append(default_model)
    if not sets:
        # No-op: return the current row rather than bump updated_at for nothing.
        return get_agent(conn, agent_id)
    sets.append("updated_at = ?")
    args.append(_now_iso())
    args.append(agent_id)
    with conn:
        row = conn.execute(
            f"UPDATE agents SET {', '.join(sets)} WHERE id = ?"
            f" RETURNING {_AGENT_COLS};",
            tuple(args),
        ).fetchone()
    if row is None:
        raise LookupError(f"Agent {agent_id} not found.")
    return _row_to_agent(row)


def delete_agent(conn: sqlite3.Connection, agent_id: int) -> None:
    """Delete an agent by id; idempotent.

    Any chat pointing at it reverts to Normal via ``ON DELETE SET NULL`` (FK
    enforcement is per-connection; ``open_connection`` sets it). A missing id
    is silently accepted rather than raised.

    Args:
        conn: Open SQLite connection.
        agent_id: Id of the agent to delete.
    """
    with conn:
        conn.execute("DELETE FROM agents WHERE id = ?;", (agent_id,))
