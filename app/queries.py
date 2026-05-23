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
        project_id: The project this chat belongs to (phase 17). NOT NULL on
            the schema side: the migration assigns every legacy chat to the
            Default project before enforcing the FK.
    """

    id: int
    name: str
    model: str
    name_locked: bool
    temperature: float
    tool_iteration_cap: int
    project_id: int
    created_at: datetime
    updated_at: datetime
    active_agent: str | None = None


@dataclass(frozen=True)
class Project:
    """One row of the ``projects`` table (phase 17).

    Attributes:
        id: Auto-assigned primary key.
        name: Human-readable display name, unique across the projects table.
        description: Free-text description (may be empty).
        workspace_subdir: Path segment under ``FILE_TOOL_ROOT`` — the
            project's workspace lives at ``FILE_TOOL_ROOT/<subdir>/``.
            Slugified from ``name`` at create time; never edited.
        default_model: Pre-fill for the model dropdown on new chats in this
            project. ``None`` means no project default (use the global one).
        default_agent: Pre-selection for the agent dropdown on new chats.
            ``None`` means Normal (no agent).
        num_ctx: Per-project override for Ollama's ``num_ctx`` (context
            window in tokens). ``None`` means inherit the global default
            from ``app_settings``. Applied per turn, not seeded onto chats,
            so changing it takes effect on the next message in any chat
            belonging to this project.
        created_at, updated_at: ISO 8601 UTC timestamps.
    """

    id: int
    name: str
    description: str
    workspace_subdir: str
    default_model: str | None
    default_agent: str | None
    num_ctx: int | None
    created_at: datetime
    updated_at: datetime


class _Unset:
    """Sentinel marker for "argument intentionally omitted" in updaters.

    Allows ``update_project`` to distinguish "this kwarg was not passed
    (leave the field alone)" from "this kwarg was passed as ``None`` (set
    the field to SQL NULL)". A plain ``None`` default cannot tell the two
    apart, but clearing ``default_model`` / ``default_agent`` from the UI
    must persist as NULL, not be silently ignored.
    """


_UNSET = _Unset()


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
    # Per-turn token counts reported by Ollama on the final stream chunk
    # of an assistant message. NULL on non-assistant rows, on assistant
    # rows persisted before this column existed, and on responses where
    # Ollama didn't report counts (e.g. full prompt-cache hit). See
    # `app.ollama.ChatChunk` for the source-of-truth interpretation.
    prompt_tokens: int | None = None
    eval_tokens: int | None = None


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
        project_id=int(row["project_id"]),
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
        prompt_tokens=row["prompt_tokens"],
        eval_tokens=row["eval_tokens"],
    )


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


def create_conversation(
    conn: sqlite3.Connection,
    name: str,
    model: str,
    *,
    project_id: int | None = None,
    temperature: float = 0.8,
    tool_iteration_cap: int = 5,
    active_agent: str | None = None,
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
        active_agent: Name of the user-invoked agent to start the chat with
            (a key in `app.agents.AGENTS`), or None for Normal plain chat.

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
            "  active_agent, project_id, created_at, updated_at)"
            " VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?)"
            " RETURNING id, name, model, name_locked, temperature, tool_iteration_cap,"
            "          active_agent, project_id, created_at, updated_at;",
            (
                name, model, temperature, tool_iteration_cap, active_agent,
                project_id, now, now,
            ),
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
        " active_agent, project_id, created_at, updated_at"
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
        " active_agent, project_id, created_at, updated_at"
        " FROM conversations"
        " ORDER BY updated_at DESC, id DESC;"
    ).fetchall()
    return [_row_to_conversation(r) for r in rows]


def list_conversations_in_project(
    conn: sqlite3.Connection, project_id: int
) -> list[Conversation]:
    """Return every conversation in a project, most-recently-updated first.

    Phase 17: powers the per-project sidebar. Same ordering convention as
    ``list_conversations`` (updated_at DESC, id DESC) so the most recently
    touched chat floats to the top.

    Args:
        conn: Open SQLite connection.
        project_id: The project whose conversations to list.

    Returns:
        Conversations in the project, ordered by ``updated_at DESC``. Empty
        list when the project exists but has no chats yet (or doesn't exist).
    """
    rows = conn.execute(
        "SELECT id, name, model, name_locked, temperature, tool_iteration_cap,"
        " active_agent, project_id, created_at, updated_at"
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
            "          active_agent, project_id, created_at, updated_at;",
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
            "          active_agent, project_id, created_at, updated_at;",
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
            "          active_agent, project_id, created_at, updated_at;",
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
            "          active_agent, project_id, created_at, updated_at;",
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
            "          active_agent, project_id, created_at, updated_at;",
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
            "  prompt_tokens, eval_tokens;",
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
        "  prompt_tokens, eval_tokens"
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
            "  prompt_tokens, eval_tokens;",
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


# ---------------------------------------------------------------------------
# Phase 17: projects
# ---------------------------------------------------------------------------


_PROJECT_COLS = (
    "id, name, description, workspace_subdir, default_model, default_agent,"
    " num_ctx, created_at, updated_at"
)


def _row_to_project(row: sqlite3.Row) -> Project:
    """Map a ``projects`` row to the :class:`Project` dataclass."""
    return Project(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        workspace_subdir=row["workspace_subdir"],
        default_model=row["default_model"],
        default_agent=row["default_agent"],
        num_ctx=row["num_ctx"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def slugify_project_name(name: str) -> str:
    """Convert a project name to a filesystem-safe workspace slug.

    Lowercases, replaces runs of non-``[a-z0-9]`` with a single hyphen,
    strips leading/trailing hyphens, caps at 60 chars. Falls back to
    ``"project"`` when the result would be empty (e.g. the name was all
    punctuation).

    The caller (``create_project``) is responsible for ensuring uniqueness
    against existing ``workspace_subdir`` values — on collision it appends
    ``-2``, ``-3``, ... until unique.

    Args:
        name: Human-readable project name (caller supplies; not modified).

    Returns:
        A best-effort slug suitable for use as a single path segment.
    """
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    return slug or "project"


def list_projects(conn: sqlite3.Connection) -> list[Project]:
    """Return every project, alphabetically by name (case-insensitive).

    Args:
        conn: Open SQLite connection.

    Returns:
        All projects ordered by ``name COLLATE NOCASE ASC``. The projects-
        index page surfaces this order; alphabetical is a more stable choice
        than created_at since the user thinks of projects by name.
    """
    rows = conn.execute(
        f"SELECT {_PROJECT_COLS} FROM projects"
        f" ORDER BY name COLLATE NOCASE ASC;"
    ).fetchall()
    return [_row_to_project(r) for r in rows]


def get_project(conn: sqlite3.Connection, project_id: int) -> Project:
    """Look up a project by id.

    Args:
        conn: Open SQLite connection.
        project_id: Id to look up.

    Returns:
        The matching Project.

    Raises:
        LookupError: When no project exists with that id.
    """
    row = conn.execute(
        f"SELECT {_PROJECT_COLS} FROM projects WHERE id = ?;", (project_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"Project {project_id} not found.")
    return _row_to_project(row)


def get_project_for_conversation(
    conn: sqlite3.Connection, conversation_id: int
) -> Project:
    """Return the project that owns ``conversation_id``.

    Used by the generation producer (to scope file tools) and by the
    backcompat ``/chats/{id}`` redirect (to compute the canonical project-
    scoped URL).

    Args:
        conn: Open SQLite connection.
        conversation_id: Id of the chat to resolve.

    Returns:
        The owning Project.

    Raises:
        LookupError: When the conversation does not exist.
    """
    row = conn.execute(
        "SELECT p.id, p.name, p.description, p.workspace_subdir,"
        " p.default_model, p.default_agent, p.num_ctx,"
        " p.created_at, p.updated_at"
        " FROM projects p JOIN conversations c ON c.project_id = p.id"
        " WHERE c.id = ?;",
        (conversation_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_project(row)


def count_projects(conn: sqlite3.Connection) -> int:
    """Return the total number of projects.

    Used by ``delete_project_endpoint`` to refuse deletion when one project
    remains (the app needs a project as the home view).
    """
    return conn.execute("SELECT COUNT(*) FROM projects;").fetchone()[0]


def create_project(
    conn: sqlite3.Connection,
    name: str,
    description: str = "",
    default_model: str | None = None,
    default_agent: str | None = None,
) -> Project:
    """Insert a new project; slugify the workspace subdir from ``name``.

    On slug collision (rare — two names that normalize to the same slug),
    appends ``"-2"``, ``"-3"``, ... until unique. The ``name`` itself must
    also be unique (UNIQUE constraint on the column); a duplicate raises
    ``sqlite3.IntegrityError`` which the route layer catches and maps to 409.

    Args:
        conn: Open SQLite connection.
        name: Display name. Caller is responsible for ``.strip()`` /
            length-validation.
        description: Free-text description; may be empty.
        default_model: Pre-fill for new chats. ``None`` means use the
            global default.
        default_agent: Pre-selection for new chats. ``None`` means Normal.

    Returns:
        The newly created Project.

    Raises:
        sqlite3.IntegrityError: When ``name`` already exists.
    """
    now = _now_iso()
    base = slugify_project_name(name)
    slug = base
    n = 2
    # Find an unused slug. The loop's an upper bound of "how many
    # projects could share a base slug"; we cap nowhere because a real
    # user can't realistically create thousands of similarly-named
    # projects.
    while (
        conn.execute(
            "SELECT 1 FROM projects WHERE workspace_subdir = ?;", (slug,)
        ).fetchone()
        is not None
    ):
        slug = f"{base}-{n}"
        n += 1
    with conn:
        row = conn.execute(
            "INSERT INTO projects"
            " (name, description, workspace_subdir, default_model, default_agent,"
            "  num_ctx, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?, ?)"
            f" RETURNING {_PROJECT_COLS};",
            (name, description, slug, default_model, default_agent, now, now),
        ).fetchone()
    return _row_to_project(row)


def update_project(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    default_model: "str | None | _Unset" = _UNSET,
    default_agent: "str | None | _Unset" = _UNSET,
    num_ctx: "int | None | _Unset" = _UNSET,
) -> Project:
    """Update editable project fields. Each kwarg is optional.

    ``default_model`` / ``default_agent`` / ``num_ctx`` use a sentinel
    (``_UNSET``) to distinguish "not passed" from "set to NULL". A plain
    ``None`` default would silently swallow the "clear this field"
    intent, but the settings form must be able to clear a previously-set
    override.

    Args:
        conn: Open SQLite connection.
        project_id: Id of the project to update.
        name: New display name (``None`` = leave alone).
        description: New description (``None`` = leave alone).
        default_model: New default model, ``None`` to clear, or the
            sentinel ``_UNSET`` (default) to leave alone.
        default_agent: New default agent name, ``None`` to clear, or the
            sentinel ``_UNSET`` (default) to leave alone.
        num_ctx: New per-project Ollama context-window override (in
            tokens), ``None`` to clear (inherit global), or the
            sentinel ``_UNSET`` (default) to leave alone. Values are
            clamped to [NUM_CTX_MIN, NUM_CTX_MAX].

    Returns:
        The updated Project (or unchanged Project when no kwargs were passed).

    Raises:
        LookupError: When the project does not exist.
        sqlite3.IntegrityError: When ``name`` collides with another project.
    """
    sets: list[str] = []
    args: list = []
    if name is not None:
        sets.append("name = ?")
        args.append(name)
    if description is not None:
        sets.append("description = ?")
        args.append(description)
    if not isinstance(default_model, _Unset):
        sets.append("default_model = ?")
        args.append(default_model)
    if not isinstance(default_agent, _Unset):
        sets.append("default_agent = ?")
        args.append(default_agent)
    if not isinstance(num_ctx, _Unset):
        sets.append("num_ctx = ?")
        args.append(None if num_ctx is None else clamp_num_ctx(num_ctx))
    if not sets:
        # No-op update — return the current row rather than performing a
        # bare ``UPDATE ... SET updated_at = ?`` which would falsely bump
        # the timestamp.
        return get_project(conn, project_id)
    sets.append("updated_at = ?")
    args.append(_now_iso())
    args.append(project_id)
    with conn:
        row = conn.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ?"
            f" RETURNING {_PROJECT_COLS};",
            tuple(args),
        ).fetchone()
    if row is None:
        raise LookupError(f"Project {project_id} not found.")
    return _row_to_project(row)


def delete_project(conn: sqlite3.Connection, project_id: int) -> None:
    """Delete a project and (via FK cascade) every conversation it owns.

    Idempotent: deleting a non-existent project is a no-op (mirrors
    ``delete_conversation``). The on-disk workspace under
    ``FILE_TOOL_ROOT/<workspace_subdir>`` is PRESERVED — the user can
    recover files from a deleted project even though the row is gone.

    Args:
        conn: Open SQLite connection.
        project_id: Id of the project to delete.
    """
    with conn:
        conn.execute(
            "DELETE FROM projects WHERE id = ?;", (project_id,)
        )


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
_DEFAULT_TEMPERATURE_FALLBACK = 0.2


def get_default_temperature(conn: sqlite3.Connection) -> float:
    """Return the global default sampling temperature for new chats.

    Default (no row): ``0.2``. The stored value is clamped to the
    [0.0, 2.0] range Ollama accepts; a malformed row (non-numeric,
    written by a hand-crafted request) falls back to ``0.2`` rather
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


_DEFAULT_MODEL_KEY = "default_model"


def get_default_model(conn: sqlite3.Connection) -> str | None:
    """Return the global default model for new chats, or None if unset.

    Args:
        conn: Open SQLite connection.
    """
    return get_setting(conn, _DEFAULT_MODEL_KEY, default=None)


def set_default_model(conn: sqlite3.Connection, model: str | None) -> None:
    """Persist the global default model for new chats.

    Passing ``None`` or an empty string clears the setting so no
    model is pre-selected by the global default.

    Args:
        conn: Open SQLite connection.
        model: Ollama model identifier (e.g. ``"granite4.1:8b"``), or
            ``None`` / empty string to clear.
    """
    if model:
        set_setting(conn, _DEFAULT_MODEL_KEY, model)
    else:
        with conn:
            conn.execute(
                "DELETE FROM app_settings WHERE key = ?;",
                (_DEFAULT_MODEL_KEY,),
            )


_DEFAULT_TOOL_ITERATION_CAP_KEY = "default_tool_iteration_cap"
_DEFAULT_TOOL_ITERATION_CAP_FALLBACK = 5


def get_default_tool_iteration_cap(conn: sqlite3.Connection) -> int:
    """Return the global default per-turn tool-iteration cap for new chats.

    Default (no row): ``5``. The stored value is clamped to the [1, 10]
    range the app enforces; a malformed row falls back to ``5`` rather
    than raising, so a corrupt setting can never break chat creation.

    Args:
        conn: Open SQLite connection.
    """
    raw = get_setting(conn, _DEFAULT_TOOL_ITERATION_CAP_KEY, default=None)
    if raw is None:
        return _DEFAULT_TOOL_ITERATION_CAP_FALLBACK
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TOOL_ITERATION_CAP_FALLBACK
    return max(1, min(10, value))


def set_default_tool_iteration_cap(
    conn: sqlite3.Connection, tool_iteration_cap: int
) -> None:
    """Persist the global default per-turn tool-iteration cap for new chats.

    Clamps to [1, 10] before storing so an out-of-range value can't be
    read back later. Stored as a string (the app_settings value column
    is text).

    Args:
        conn: Open SQLite connection.
        tool_iteration_cap: New default cap (clamped to 1–10).
    """
    clamped = max(1, min(10, int(tool_iteration_cap)))
    set_setting(conn, _DEFAULT_TOOL_ITERATION_CAP_KEY, str(clamped))


# Ollama's own default for `num_ctx` is 2048 — far too small for real
# conversations. 16384 matches what most local 7-13B models comfortably
# fit and what tool-using sessions typically need. NUM_CTX_MIN/MAX bound
# the clamp on read and write: 512 is below any usable chat context, and
# 1_048_576 (1M) is a future-proof ceiling well above any current model.
_DEFAULT_NUM_CTX_KEY = "default_num_ctx"
_DEFAULT_NUM_CTX_FALLBACK = 16384
NUM_CTX_MIN = 512
NUM_CTX_MAX = 1_048_576


def clamp_num_ctx(num_ctx: int) -> int:
    """Clamp a num_ctx value to the [NUM_CTX_MIN, NUM_CTX_MAX] range."""
    return max(NUM_CTX_MIN, min(NUM_CTX_MAX, int(num_ctx)))


def get_default_num_ctx(conn: sqlite3.Connection) -> int:
    """Return the global default Ollama context window for new chats.

    Default (no row): ``16384`` (see ``_DEFAULT_NUM_CTX_FALLBACK``). The
    stored value is clamped to the [NUM_CTX_MIN, NUM_CTX_MAX] range; a
    malformed row falls back to the default rather than raising, so a
    corrupt setting can never break chat creation.

    Args:
        conn: Open SQLite connection.
    """
    raw = get_setting(conn, _DEFAULT_NUM_CTX_KEY, default=None)
    if raw is None:
        return _DEFAULT_NUM_CTX_FALLBACK
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_NUM_CTX_FALLBACK
    return clamp_num_ctx(value)


def set_default_num_ctx(conn: sqlite3.Connection, num_ctx: int) -> None:
    """Persist the global default Ollama context window for new chats.

    Clamps to [NUM_CTX_MIN, NUM_CTX_MAX] before storing so an out-of-
    range value can't be read back later. Stored as a string (the
    app_settings value column is text).

    Args:
        conn: Open SQLite connection.
        num_ctx: New default context window in tokens.
    """
    set_setting(conn, _DEFAULT_NUM_CTX_KEY, str(clamp_num_ctx(num_ctx)))


def resolve_num_ctx_for_project(
    conn: sqlite3.Connection, project_num_ctx: int | None
) -> int:
    """Resolve the effective num_ctx for a turn: project override or global.

    Args:
        conn: Open SQLite connection.
        project_num_ctx: The project's ``num_ctx`` column value, or
            ``None`` when the project inherits the global default.

    Returns:
        A clamped, ready-to-use ``num_ctx`` token count for the Ollama
        request's ``options`` dict.
    """
    if project_num_ctx is not None:
        return clamp_num_ctx(project_num_ctx)
    return get_default_num_ctx(conn)


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
