"""CRUD for the ``projects`` table."""

import re
import sqlite3
from datetime import datetime

from app._time import now_iso as _now_iso
from app.queries._models import Project, _Unset, _UNSET
from app.queries.settings import clamp_num_ctx


_PROJECT_COLS = (
    "id, name, description, workspace_subdir, default_model, default_agent,"
    " num_ctx, system_prompt, created_at, updated_at"
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
        system_prompt=row["system_prompt"],
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
        " p.default_model, p.default_agent, p.num_ctx, p.system_prompt,"
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
            "  num_ctx, system_prompt, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, NULL, '', ?, ?)"
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
    system_prompt: str | None = None,
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
        system_prompt: New per-project system prompt (``""`` to clear),
            or ``None`` (default) to leave alone. Clamped to 200 chars.

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
    if system_prompt is not None:
        # Clamp at 200 chars defensively — the route also enforces this,
        # but a direct programmatic caller should not be able to insert
        # an unbounded prompt and surprise the model with a giant system.
        sets.append("system_prompt = ?")
        args.append(system_prompt[:200])
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
