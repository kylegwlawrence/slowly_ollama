# Phase 17 — Projects

## Context

Today the app has a flat list of chats sharing one global workspace
(`FILE_TOOL_ROOT`). We're introducing **Projects** as the organizational
container above chats. A project bundles:

1. A set of **chats** (the "Chats" tab — the only place new chats can be
   created).
2. A **workspace** — a per-project subdirectory of `FILE_TOOL_ROOT` that
   the file tools (`read_file` / `write_file` / `list_directory` /
   `search_files`) are scoped to during turns belonging to that project's
   chats.
3. **Settings** — name, description, default model + default agent for
   new chats in this project.

A "Files" tab browses the project's workspace read-only with download.

This phase is purely additive in concept but touches the spine of the
app (every chat URL, every file-tool resolution, the page layout). The
goal is a clean, testable refactor with no functional regression for the
existing single-project use case.

## Decisions (locked with the user)

- **Workspace scoping:** per-project subdir under `FILE_TOOL_ROOT`. Each
  project's `workspace_subdir` is a stable slug stored on the row. File
  tools resolve every path against `FILE_TOOL_ROOT/<subdir>/`.
- **Chat membership:** every chat belongs to exactly one project. A
  migration creates a `"Default"` project and assigns every existing
  chat to it.
- **Project settings (v1):** name, description, **default_model** (pre-
  fills the model select on new chats in the project), **default_agent**
  (pre-selects the agent on new chats). NOT applied retroactively to
  existing chats. No prompt-prefix, no project-level tool/RAG chip
  defaults — keep the surface small.
- **Navigation:** **nested routes** + dedicated `/projects` index. The
  sidebar inside a project shows only that project's chats; a "← All
  projects" link at the top of the sidebar returns to `/projects`.
- **Files tab:** read-only browser + download. No upload / edit /
  delete in v1.
- **RAG servers:** stay **global** (`/settings`); per-chat chips
  unchanged. No project-level RAG scoping.

### Decisions deferred (NOT in v1)

- Editable `workspace_subdir`. v1 derives it from name slug at create
  time; rename does NOT move the on-disk folder.
- Files-tab uploads / deletes / renames / in-browser editing.
- Per-project default tool/RAG chips.
- Per-project custom system-prompt prefix.

---

## Workspace migration (the one risky bit)

`FILE_TOOL_ROOT` currently holds the user's workspace files at the root.
We're introducing `FILE_TOOL_ROOT/default/` and want existing files
there so the Default project is the natural home for them.

**Plan:** at lifespan startup, after `initialize_database()`:

1. Read `get_setting(db, "workspace_v2_migrated")`. If `"1"`, skip.
2. If `file_tool_root()` is None (file tools disabled), set the flag
   to `"1"` and skip — no workspace to migrate.
3. Otherwise, ensure `FILE_TOOL_ROOT/default/` exists.
4. Walk `FILE_TOOL_ROOT`'s top-level entries. For each entry whose name
   is NOT `"default"` and NOT in the set of any existing project's
   `workspace_subdir`, move it into `FILE_TOOL_ROOT/default/<name>` using
   `shutil.move`. Skip and warn (logger.warning) on collision rather
   than overwrite.
5. Log a single info line summarizing what moved.
6. Set `workspace_v2_migrated = "1"`.

This is one-shot and idempotent on subsequent boots. The implementer
MUST log the planned moves before executing so the dev terminal output
makes it obvious what happened. (`logger.info("Workspace v2 migration:
moving %d entries into %s", n, default_dir)`.)

If `FILE_TOOL_ROOT` contains nothing the migration is a no-op except for
creating the `default/` directory and setting the flag.

**Testing:** unit test the migration helper directly (tmp dir, populated
with a mix of files and dirs; assert post-migration layout + flag set;
re-run is no-op). See "Tests" below.

---

## URL plan (concrete)

| Method | URL | Purpose |
|---|---|---|
| GET | `/` | 302 → `/projects` |
| GET | `/projects` | Projects index page (list + create form). Full layout, sidebar shows projects list. |
| POST | `/projects` | Create project. Response = OOB-prepended row + `HX-Push-Url: /projects/{id}/chats`. |
| GET | `/projects/{pid}` | 302 → `/projects/{pid}/chats` |
| GET | `/projects/{pid}/chats` | Project page, Chats tab. No active chat → empty composer. |
| GET | `/projects/{pid}/chats/new` | Empty-state composer fragment (for HTMX sidebar `+ New chat`). |
| GET | `/projects/{pid}/chats/{cid}` | Project page, Chats tab, chat panel for cid. |
| POST | `/projects/{pid}/chats` | Create chat in project + send first message. Returns `_chat_panel.html` + OOB sidebar row + `HX-Push-Url: /projects/{pid}/chats/{cid}`. |
| GET | `/projects/{pid}/files` | Project page, Files tab, lists workspace root. |
| GET | `/projects/{pid}/files/browse?path=...` | Browse a subdir. |
| GET | `/projects/{pid}/files/view?path=...` | Render a single file. |
| GET | `/projects/{pid}/files/download?path=...` | Download a single file (`Content-Disposition: attachment`). |
| GET | `/projects/{pid}/settings` | Project page, Settings tab. |
| PATCH | `/projects/{pid}` | Update name / description / default_model / default_agent. |
| DELETE | `/projects/{pid}` | Delete project + cascade chats. 409 if it's the last project. |
| GET | `/settings` | Global settings (unchanged). |
| GET | `/chats/{cid}` | **Backcompat** 302 → `/projects/{pid}/chats/{cid}` (resolve pid from row). |

**Chat write endpoints stay where they are** — they don't need
project-scoped URLs since chat IDs are globally unique:

- `POST /chats/{cid}/messages` — unchanged
- `PATCH /chats/{cid}` (rename) — unchanged
- `DELETE /chats/{cid}` — unchanged, but `HX-Location` redirects to
  `/projects/{pid}/chats` when the viewer is on the deleted chat
- `POST /chats/{cid}/regenerate` — unchanged
- `POST /chats/{cid}/agent` — unchanged
- `POST /chats/{cid}/tools/{name}` — unchanged
- `POST /chats/{cid}/rag-servers/{name}` — unchanged
- `PATCH /chats/{cid}/temperature` — unchanged
- `PATCH /chats/{cid}/tool-iteration-cap` — unchanged
- `GET /chats/{cid}/stream` — unchanged
- `GET /chats/{cid}/edit` — unchanged
- `GET /chats/{cid}/item` — unchanged

These endpoints look up the chat, derive its project, and use that for
any project-aware response (e.g. delete's `HX-Location`, OOB sidebar
row context).

The old `POST /chats` and `GET /new` endpoints are **removed**; their
nested replacements take over. The old `GET /chats/{cid}` becomes a
backcompat redirect so external bookmarks still work.

---

## Part A — Schema (`app/db.py`, `app/queries.py`)

### A.1 `_SCHEMA_SQL` additions

Add (idempotently, via `CREATE TABLE IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS projects (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    description       TEXT NOT NULL DEFAULT '',
    -- Path segment under FILE_TOOL_ROOT (a slug). UNIQUE so projects
    -- can't share a workspace. Set at create time; never edited.
    workspace_subdir  TEXT NOT NULL UNIQUE,
    -- NULL = no project default; new chats use the global default.
    default_model     TEXT,
    -- NULL = Normal (no agent) is the default; otherwise an agent name.
    default_agent     TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
```

In the `conversations` table block, add (within the `CREATE TABLE`):

```sql
project_id INTEGER NOT NULL
    REFERENCES projects(id) ON DELETE CASCADE,
```

> Note: `CREATE TABLE IF NOT EXISTS` does NOT alter an existing table.
> The column is added via the migration helper below.

### A.2 Migration helpers

Add these to `app/db.py` and call them from `initialize_database`
**after** `executescript(_SCHEMA_SQL)` and **before**
`_ensure_conversations_active_agent_column`:

```python
def _ensure_default_project(conn: sqlite3.Connection) -> int:
    """Ensure at least one project exists; return the Default project's id.

    Called as part of the projects migration. Idempotent: if any
    project already exists, returns the id of the lexicographically-
    first one (deterministic for tests); otherwise inserts "Default"
    with workspace_subdir "default" and returns its id.

    Args:
        conn: Open SQLite connection.

    Returns:
        The id of the Default (or first existing) project.
    """
    row = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1;").fetchone()
    if row is not None:
        return row["id"] if isinstance(row, sqlite3.Row) else row[0]
    now = _now_iso_db()  # local helper, see below
    cursor = conn.execute(
        "INSERT INTO projects"
        " (name, description, workspace_subdir, default_model, default_agent,"
        "  created_at, updated_at)"
        " VALUES ('Default', '', 'default', NULL, NULL, ?, ?);",
        (now, now),
    )
    return cursor.lastrowid


def _ensure_conversations_project_id_column(
    conn: sqlite3.Connection, default_project_id: int
) -> None:
    """Add conversations.project_id and backfill it.

    SQLite cannot ALTER COLUMN to add NOT NULL on an existing column,
    so we use the table-rewrite pattern. Idempotent: detects the new
    column's presence and exits early.

    Args:
        conn: Open SQLite connection.
        default_project_id: Project id to assign to every existing
            conversation row that has no project_id.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations);")}
    if "project_id" in columns:
        return
    # Phase 1: add the column as NULLable so we can backfill.
    conn.execute("ALTER TABLE conversations ADD COLUMN project_id INTEGER;")
    conn.execute(
        "UPDATE conversations SET project_id = ? WHERE project_id IS NULL;",
        (default_project_id,),
    )
    # Phase 2: table-rewrite to enforce NOT NULL + FK ON DELETE CASCADE.
    conn.executescript(f"""
        BEGIN;
        CREATE TABLE conversations_new (
            id           INTEGER PRIMARY KEY,
            name         TEXT NOT NULL,
            model        TEXT NOT NULL,
            name_locked  INTEGER NOT NULL DEFAULT 0,
            temperature  REAL NOT NULL DEFAULT 0.8,
            tool_iteration_cap INTEGER NOT NULL DEFAULT 5,
            active_agent TEXT,
            project_id   INTEGER NOT NULL
                REFERENCES projects(id) ON DELETE CASCADE,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        INSERT INTO conversations_new
            (id, name, model, name_locked, temperature, tool_iteration_cap,
             active_agent, project_id, created_at, updated_at)
        SELECT id, name, model, name_locked, temperature, tool_iteration_cap,
               active_agent, project_id, created_at, updated_at FROM conversations;
        DROP TABLE conversations;
        ALTER TABLE conversations_new RENAME TO conversations;
        COMMIT;
    """)


def _now_iso_db() -> str:
    """ISO 8601 UTC string for DB writes from db.py (mirror of queries._now_iso)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
```

Wire into `initialize_database`:

```python
with sqlite3.connect(target) as conn:
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row  # so _ensure_default_project's row dict-access works
    conn.executescript(_SCHEMA_SQL)
    _ensure_name_locked_column(conn)
    _migrate_messages_drop_role_check(conn)
    _ensure_rag_servers_description_column(conn)
    _ensure_conversations_temperature_column(conn)
    _ensure_conversations_tool_iteration_cap_column(conn)
    _ensure_conversations_active_agent_column(conn)
    # Phase 17: projects table + per-chat project_id column.
    default_project_id = _ensure_default_project(conn)
    _ensure_conversations_project_id_column(conn, default_project_id)
```

`_SCHEMA_SQL` itself is updated to include `project_id INTEGER NOT NULL
REFERENCES projects(id) ON DELETE CASCADE` in the conversations block —
that path applies on a FRESH DB only (the migration applies on existing
DBs). On a fresh DB, `_ensure_default_project` still runs and inserts
the Default row before any conversation can be created (because
conversations now require a non-null FK).

### A.3 `app/queries.py` — Project dataclass + CRUD

Add a new section after the existing settings code:

```python
# ---------------------------------------------------------------------------
# Phase 17: projects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Project:
    """One row of the `projects` table.

    Attributes:
        id: Auto-assigned primary key.
        name: Human-readable display name, unique.
        description: Free-text description (may be empty).
        workspace_subdir: Path segment under FILE_TOOL_ROOT — the
            project's workspace lives at FILE_TOOL_ROOT/<subdir>/.
            Slugified from `name` at create time; never edited.
        default_model: Pre-fill for the model dropdown on new chats in
            this project. None when no project default is set.
        default_agent: Pre-selection for the agent dropdown on new
            chats. None means Normal (no agent).
        created_at, updated_at: ISO 8601 UTC.
    """

    id: int
    name: str
    description: str
    workspace_subdir: str
    default_model: str | None
    default_agent: str | None
    created_at: datetime
    updated_at: datetime


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        workspace_subdir=row["workspace_subdir"],
        default_model=row["default_model"],
        default_agent=row["default_agent"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


_PROJECT_COLS = (
    "id, name, description, workspace_subdir, default_model, default_agent,"
    " created_at, updated_at"
)


def slugify_project_name(name: str) -> str:
    """Convert a project name to a filesystem-safe workspace slug.

    Lowercases, replaces non-[a-z0-9] runs with a single hyphen, strips
    leading/trailing hyphens, caps at 60 chars. Falls back to "project"
    when the result would be empty (e.g. the name was all punctuation).

    The caller (create_project) is responsible for ensuring uniqueness
    against `workspace_subdir` — if a collision occurs, append `-2`,
    `-3`, ... until unique.
    """
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    return slug or "project"


def list_projects(conn: sqlite3.Connection) -> list[Project]:
    """Return every project, alphabetically by name."""
    rows = conn.execute(
        f"SELECT {_PROJECT_COLS} FROM projects ORDER BY name COLLATE NOCASE ASC;"
    ).fetchall()
    return [_row_to_project(r) for r in rows]


def get_project(conn: sqlite3.Connection, project_id: int) -> Project:
    """Look up a project by id.

    Raises:
        LookupError: When the project does not exist.
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
    """Return the project that owns `conversation_id`.

    Raises:
        LookupError: When the conversation does not exist.
    """
    row = conn.execute(
        f"SELECT p.id, p.name, p.description, p.workspace_subdir,"
        f" p.default_model, p.default_agent, p.created_at, p.updated_at"
        f" FROM projects p JOIN conversations c ON c.project_id = p.id"
        f" WHERE c.id = ?;",
        (conversation_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"Conversation {conversation_id} not found.")
    return _row_to_project(row)


def count_projects(conn: sqlite3.Connection) -> int:
    """Return the total number of projects (used to gate last-project deletion)."""
    return conn.execute("SELECT COUNT(*) FROM projects;").fetchone()[0]


def create_project(
    conn: sqlite3.Connection,
    name: str,
    description: str = "",
    default_model: str | None = None,
    default_agent: str | None = None,
) -> Project:
    """Insert a new project. Slugifies the workspace_subdir.

    On slug collision (rare — same name normalizes to same slug), append
    "-2", "-3", ... until unique. The name itself must be unique via
    the UNIQUE constraint — caller catches IntegrityError.

    Raises:
        sqlite3.IntegrityError: When the name is already taken.
    """
    now = _now_iso()
    base = slugify_project_name(name)
    slug = base
    n = 2
    while conn.execute(
        "SELECT 1 FROM projects WHERE workspace_subdir = ?;", (slug,)
    ).fetchone() is not None:
        slug = f"{base}-{n}"
        n += 1
    with conn:
        row = conn.execute(
            "INSERT INTO projects"
            " (name, description, workspace_subdir, default_model, default_agent,"
            "  created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
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
    default_model: str | None | _Unset = _UNSET,
    default_agent: str | None | _Unset = _UNSET,
) -> Project:
    """Update editable project fields. Each kwarg is optional.

    Note `default_model` / `default_agent` use a sentinel to distinguish
    "not passed" from "set to NULL" — explicitly clearing them via the
    settings form must persist as NULL, not be silently ignored.

    Raises:
        LookupError: When the project does not exist.
        sqlite3.IntegrityError: When `name` collides with another project.
    """
    sets: list[str] = []
    args: list = []
    if name is not None:
        sets.append("name = ?")
        args.append(name)
    if description is not None:
        sets.append("description = ?")
        args.append(description)
    if default_model is not _UNSET:
        sets.append("default_model = ?")
        args.append(default_model)
    if default_agent is not _UNSET:
        sets.append("default_agent = ?")
        args.append(default_agent)
    if not sets:
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
    """Delete a project and (via FK cascade) all its conversations.

    Idempotent: deleting a non-existent project is a no-op (mirrors
    `delete_conversation`).
    """
    with conn:
        conn.execute("DELETE FROM projects WHERE id = ?;", (project_id,))
```

Add a small sentinel module-private to `queries.py`:

```python
class _Unset:
    pass

_UNSET = _Unset()
```

### A.4 Conversation changes

- Add `project_id: int` to the `Conversation` dataclass (NOT NULL on
  the schema side, so non-optional in Python).
- Update `_row_to_conversation` to read `project_id`.
- Update the `SELECT` lists in `get_conversation`, `list_conversations`,
  `rename_conversation`, `set_name_auto`, `set_conversation_temperature`,
  `set_conversation_tool_iteration_cap`, `set_active_agent` to include
  `project_id`.
- Update `create_conversation` to require `project_id` as a required
  positional/keyword arg; insert it. Audit every call site (`routes.py:565`,
  any tests) to supply it.
- Add `list_conversations_in_project(conn, project_id)` mirroring
  `list_conversations` with a `WHERE project_id = ?` clause:

```python
def list_conversations_in_project(
    conn: sqlite3.Connection, project_id: int
) -> list[Conversation]:
    """Return every conversation in a project, most-recently-updated first."""
    rows = conn.execute(
        f"SELECT id, name, model, name_locked, temperature, tool_iteration_cap,"
        f" active_agent, project_id, created_at, updated_at"
        f" FROM conversations WHERE project_id = ?"
        f" ORDER BY updated_at DESC, id DESC;",
        (project_id,),
    ).fetchall()
    return [_row_to_conversation(r) for r in rows]
```

**Whether to keep `list_conversations` at all:** keep it; it's used by
`get_chat_panel_endpoint` for context that doesn't currently filter by
project. We'll switch those call sites to `list_conversations_in_project`
where appropriate (sidebar render); `list_conversations` itself is
retained for tests / future admin use.

---

## Part B — Per-project workspace plumbing

### B.1 `app/projects.py` (new) — workspace helpers

Pure functions: no DB, no I/O at import time.

```python
"""Phase 17: per-project workspace path resolution.

The file tools (`app/tools/builtins.py`) confine reads/writes to a
single directory. Pre-17 this was always `FILE_TOOL_ROOT` (from .env).
With projects, the directory is per-turn: `FILE_TOOL_ROOT/<project.workspace_subdir>`.

The producer (`app/generation._run_generation`) sets the active
workspace via the `current_workspace_root` ContextVar before each tool
call. The file tools read that var (with a fallback to FILE_TOOL_ROOT
for tests / direct invocation).
"""

import logging
import shutil
from contextvars import ContextVar
from pathlib import Path

from app.config import file_tool_root
from app.queries import Project

logger = logging.getLogger(__name__)

# Set by `app.generation._run_generation` before each turn's tool calls.
# `None` means "fall back to FILE_TOOL_ROOT" (used by tests that call file
# tools directly without setting a project).
current_workspace_root: ContextVar[Path | None] = ContextVar(
    "current_workspace_root", default=None
)


def project_workspace_root(project: Project) -> Path | None:
    """Compute the on-disk workspace root for a project.

    Returns:
        `FILE_TOOL_ROOT / project.workspace_subdir`, fully resolved.
        Returns `None` when FILE_TOOL_ROOT is unset.

    Does NOT create the directory — that is `ensure_project_workspace`'s
    job, called explicitly by the create-project route + lifespan.
    """
    root = file_tool_root()
    if root is None:
        return None
    return (root / project.workspace_subdir).resolve()


def ensure_project_workspace(project: Project) -> Path | None:
    """Create the project's workspace directory on disk if needed.

    Returns the resolved path (or None when FILE_TOOL_ROOT is unset).
    Safe to call repeatedly — `mkdir(parents=True, exist_ok=True)`.
    """
    target = project_workspace_root(project)
    if target is None:
        return None
    target.mkdir(parents=True, exist_ok=True)
    return target


def migrate_legacy_workspace(
    db,  # sqlite3.Connection
    queries_mod,  # app.queries (imported lazily to avoid cycles)
) -> None:
    """One-shot move of pre-projects FILE_TOOL_ROOT contents into default/.

    See docs/plans/phase17-projects.md §"Workspace migration" for the
    full spec. No-op when FILE_TOOL_ROOT is unset OR the flag is set.
    Logs what it moves.
    """
    if queries_mod.get_setting(db, "workspace_v2_migrated") == "1":
        return
    root = file_tool_root()
    if root is None:
        queries_mod.set_setting(db, "workspace_v2_migrated", "1")
        return

    reserved = {
        p.workspace_subdir for p in queries_mod.list_projects(db)
    }
    reserved.add("default")

    default_dir = root / "default"
    default_dir.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []
    skipped: list[str] = []
    for entry in root.iterdir():
        if entry.name in reserved:
            continue
        target = default_dir / entry.name
        if target.exists():
            logger.warning(
                "Workspace v2 migration: %s already exists in default/, skipping move",
                entry.name,
            )
            skipped.append(entry.name)
            continue
        shutil.move(str(entry), str(target))
        moved.append(entry.name)

    if moved or skipped:
        logger.info(
            "Workspace v2 migration: moved %d entries into %s (skipped %d collisions)",
            len(moved), default_dir, len(skipped),
        )
    queries_mod.set_setting(db, "workspace_v2_migrated", "1")
```

### B.2 `app/config.py` — leave `file_tool_root` as is

`file_tool_root()` keeps returning the env-var root; it's the gate for
whether file tools are configured at all. The per-project subdir lives
inside the new `app/projects.py` helper.

### B.3 `app/tools/builtins.py` — read from ContextVar

Replace the body of `_resolve_within_root` to consult the ContextVar
first:

```python
def _resolve_within_root(path: str) -> Path:
    """Resolve a model-supplied path against the active workspace root.

    Phase 17: reads the per-turn `current_workspace_root` ContextVar set
    by `app.generation._run_generation`. Falls back to FILE_TOOL_ROOT
    when the var is unset (covers test code that calls tools directly
    without a project context, and the FILE_TOOL_ROOT-unset case which
    `refresh_file_tools_registration` already pops from the registry).
    """
    from app.projects import current_workspace_root  # avoid import cycle

    root = current_workspace_root.get()
    if root is None:
        root = file_tool_root()
    if root is None:
        raise _PathOutsideRoot(
            "File tools are not configured (FILE_TOOL_ROOT is unset)."
        )
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise _PathOutsideRoot(
            f"Path '{path}' is outside the allowed workspace."
        )
    return candidate
```

`search_files`'s `_format_size`-and-rel-path code uses `file_tool_root()`
directly for the `relative_to(root)` call. Change to consult the same
resolution:

```python
# in search_files
root = current_workspace_root.get() or file_tool_root()
```

(import the var at the top of the file).

### B.4 `app/generation.py` — set the ContextVar

In `_run_generation`, before the tool-iteration loop, resolve the
project workspace and set the var:

```python
from app.projects import (
    current_workspace_root,
    ensure_project_workspace,
    project_workspace_root,
)
from app.queries import get_project_for_conversation

# ... inside _run_generation, before `for iteration in range(...)`:
project = get_project_for_conversation(db, conversation_id)
ws_root = project_workspace_root(project)
if ws_root is not None:
    # Lazily create so a brand-new project's workspace exists by the
    # time a tool tries to read/write within it.
    ws_root.mkdir(parents=True, exist_ok=True)
token = current_workspace_root.set(ws_root)
try:
    # ... existing iteration loop body ...
    # ... existing streaming phase ...
finally:
    current_workspace_root.reset(token)
    # existing finally body (maybe_persist_partial + signal_done)
```

`maybe_persist_partial` + `signal_done` must continue to fire on every
exit path, so the existing outer `try:` / `finally:` shape stays; the
ContextVar set/reset wraps inside it. Suggested structure:

```python
try:
    project = get_project_for_conversation(db, conversation_id)
    ws_root = project_workspace_root(project)
    if ws_root is not None:
        ws_root.mkdir(parents=True, exist_ok=True)
    ctx_token = current_workspace_root.set(ws_root)
    try:
        for iteration in range(tool_iteration_cap):
            # ... existing body unchanged ...
        # ... existing streaming phase ...
    finally:
        current_workspace_root.reset(ctx_token)
finally:
    maybe_persist_partial(...)
    await signal_done(state)
```

---

## Part C — Routes (`app/routes.py`)

This is the biggest single file delta. Organize the new routes into a
clearly-marked section. Order in the file: projects routes first, then
the existing /chats and /settings routes (which themselves get a few
edits).

### C.1 Replace `index_endpoint` with a redirect

```python
from fastapi.responses import RedirectResponse

@router.get("/")
def index_endpoint() -> RedirectResponse:
    """Redirect the home URL to the projects index.

    All "where am I" navigation enters via /projects after phase 17.
    """
    return RedirectResponse(url="/projects", status_code=status.HTTP_302_FOUND)
```

### C.2 New projects endpoints

```python
# ---------------------------------------------------------------------------
# Phase 17: projects
# ---------------------------------------------------------------------------


@router.get("/projects", response_class=HTMLResponse)
def list_projects_endpoint(request: Request, db: DB) -> Response:
    """Render the projects index page.

    Full layout with a project list on the left (sidebar replacement)
    and a "Create project" affordance on the right. Direct hits to
    /projects always land here; the page is the home of the app.
    """
    projects = queries.list_projects(db)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_projects_index.html",
            context={"projects": projects},
        )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "layout": "projects",
            "projects": projects,
            # No project / chat / settings context on this view.
            "project": None,
            "conversation": None,
        },
    )


@router.post(
    "/projects",
    response_class=HTMLResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_project_endpoint(
    request: Request,
    db: DB,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
) -> Response:
    """Create a project; return its row + push the URL to its chats tab.

    Name must be unique (UNIQUE constraint). On conflict returns 409
    with a plain-text reason; the form keeps user input (HTMX does not
    swap on non-2xx by default).
    """
    name_clean = name.strip()
    if not name_clean:
        return HTMLResponse("Name is required.", status_code=status.HTTP_400_BAD_REQUEST)
    try:
        project = queries.create_project(
            db, name=name_clean, description=description.strip()
        )
    except sqlite3.IntegrityError:
        return HTMLResponse(
            f"Project name '{html.escape(name_clean)}' already in use.",
            status_code=status.HTTP_409_CONFLICT,
        )
    # Create the workspace directory eagerly so the Files tab works
    # immediately (even before the first tool call).
    from app.projects import ensure_project_workspace
    ensure_project_workspace(project)
    response = templates.TemplateResponse(
        request=request,
        name="_project_item.html",
        context={"project": project},
        status_code=status.HTTP_201_CREATED,
    )
    response.headers["HX-Push-Url"] = f"/projects/{project.id}/chats"
    return response


@router.get("/projects/{project_id}")
def project_redirect_endpoint(project_id: int) -> RedirectResponse:
    """Canonical entry: /projects/{id} → /projects/{id}/chats."""
    return RedirectResponse(
        url=f"/projects/{project_id}/chats",
        status_code=status.HTTP_302_FOUND,
    )


@router.patch("/projects/{project_id}", response_class=HTMLResponse)
def update_project_endpoint(
    project_id: int,
    request: Request,
    db: DB,
    name: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    default_model: Annotated[str | None, Form()] = None,
    default_agent: Annotated[str | None, Form()] = None,
) -> Response:
    """Update a project's editable fields; return the settings tab content.

    Empty strings for default_model / default_agent map to NULL (clear).
    For non-passed fields the form serializer just won't include the
    key — FastAPI binds those to None, which `update_project` treats
    as "leave as-is" via the sentinel.
    """
    try:
        queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    # FastAPI binds missing form fields to the default None — but the
    # browser does send empty strings for cleared selects, which we want
    # to persist as NULL. So: empty-string ⇒ NULL; missing ⇒ leave alone.
    # The route hand-picks which form keys were actually present:
    form_data = await request.form() if False else None  # see note below
    # Actually FastAPI's Form() always passes through; routes can detect
    # "not submitted" by relying on the default None vs empty string ""
    # convention the templates enforce (the form ALWAYS submits all
    # editable fields, so None here means "not present in the form" =
    # don't touch; "" means "submitted and cleared" = NULL).
    project = queries.update_project(
        db,
        project_id,
        name=(name.strip() if isinstance(name, str) else None) or None,
        description=description.strip() if isinstance(description, str) else None,
        default_model=(
            (default_model.strip() or None)
            if isinstance(default_model, str)
            else queries._UNSET
        ),
        default_agent=(
            (default_agent.strip() or None)
            if isinstance(default_agent, str)
            else queries._UNSET
        ),
    )
    return templates.TemplateResponse(
        request=request,
        name="_project_settings_body.html",
        context={"project": project, "saved": True, "agents": list_agents()},
    )


@router.delete(
    "/projects/{project_id}",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
def delete_project_endpoint(project_id: int, db: DB) -> Response:
    """Delete a project (and cascade its chats). Refuses last project.

    Refuses with 409 when this would leave zero projects — the app
    requires at least one to be a valid home. The Files tab's on-disk
    workspace is PRESERVED (not deleted) so the user can recover.
    """
    try:
        queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    if queries.count_projects(db) <= 1:
        return HTMLResponse(
            "Cannot delete the last project.",
            status_code=status.HTTP_409_CONFLICT,
        )
    queries.delete_project(db, project_id)
    response = Response(content="", status_code=status.HTTP_200_OK)
    response.headers["HX-Location"] = "/projects"
    return response


@router.get("/projects/{project_id}/chats", response_class=HTMLResponse)
def project_chats_endpoint(
    project_id: int, request: Request, db: DB
) -> Response:
    """Render the project page with the Chats tab active, no chat open."""
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    chats = queries.list_conversations_in_project(db, project_id)

    composer_ctx = {
        "default_tool_states": _default_tool_states(),
        "default_rag_server_states": _default_rag_server_states(db),
        "default_temperature": queries.get_default_temperature(db),
        "default_tool_iteration_cap": queries.get_default_tool_iteration_cap(db),
        "agents": list_agents(),
        "project": project,
    }
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_project_page.html",
            context={
                "project": project,
                "active_tab": "chats",
                "chats": chats,
                "conversation": None,
                "composer_ctx": composer_ctx,
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "layout": "project",
            "project": project,
            "active_tab": "chats",
            "chats": chats,
            "active_chat_id": None,
            "conversation": None,
            "composer_ctx": composer_ctx,
        },
    )


@router.get("/projects/{project_id}/chats/new", response_class=HTMLResponse)
def project_new_chat_endpoint(
    project_id: int, request: Request, db: DB
) -> Response:
    """Empty-state composer fragment for the project. HTMX-only entry point."""
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return templates.TemplateResponse(
        request=request,
        name="_composer.html",
        context={
            "default_tool_states": _default_tool_states(),
            "default_rag_server_states": _default_rag_server_states(db),
            "default_temperature": queries.get_default_temperature(db),
            "default_tool_iteration_cap": queries.get_default_tool_iteration_cap(db),
            "agents": list_agents(),
            "project": project,
            # Pre-fills used by _composer.html if present:
            "project_default_model": project.default_model,
            "project_default_agent": project.default_agent,
        },
    )


@router.get("/projects/{project_id}/chats/{conversation_id}", response_class=HTMLResponse)
async def project_chat_panel_endpoint(
    project_id: int,
    conversation_id: int,
    request: Request,
    db: DB,
    client: OllamaClient,
) -> Response:
    """Render the project page with a specific chat open.

    Validates that the chat belongs to the project (404 otherwise — the
    backcompat redirect at /chats/{id} resolves the real project_id).
    """
    try:
        conversation = queries.get_conversation(db, conversation_id)
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    if conversation.project_id != project_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Chat {conversation_id} does not belong to project {project_id}.",
        )
    chats = queries.list_conversations_in_project(db, project_id)
    messages = queries.list_messages(db, conversation_id)
    blocks = render.group_messages_for_render(messages)

    pending_stream_url = None
    live = generation.live_generations.get(conversation_id)
    if live is not None and not live.done:
        if blocks and blocks[-1].kind == "tool_batch":
            blocks = blocks[:-1]
        pending_stream_url = f"/chats/{conversation_id}/stream"

    supports_tools = await ollama.model_supports_tools(client, conversation.model)
    if supports_tools:
        tool_states, rag_server_states = _chip_states(db, conversation_id)
    else:
        tool_states, rag_server_states = [], []

    agents = list_agents()
    active_agent_spec = get_agent(conversation.active_agent)

    panel_ctx = {
        "conversation": conversation,
        "blocks": blocks,
        "pending_stream_url": pending_stream_url,
        "supports_tools": supports_tools,
        "tool_states": tool_states,
        "rag_server_states": rag_server_states,
        "agents": agents,
        "active_agent_spec": active_agent_spec,
        "project": project,
    }
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_project_page.html",
            context={
                "project": project,
                "active_tab": "chats",
                "chats": chats,
                "conversation": conversation,
                "panel_ctx": panel_ctx,
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "layout": "project",
            "project": project,
            "active_tab": "chats",
            "chats": chats,
            "active_chat_id": conversation.id,
            "conversation": conversation,
            "panel_ctx": panel_ctx,
        },
    )


@router.post(
    "/projects/{project_id}/chats",
    response_class=HTMLResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_chat_endpoint(
    project_id: int,
    request: Request,
    db: DB,
    client: OllamaClient,
    model: Annotated[str, Form()],
    content: Annotated[str, Form()],
    temperature: Annotated[float | None, Form()] = None,
    tool_iteration_cap: Annotated[int | None, Form()] = None,
    agent: Annotated[str | None, Form()] = None,
) -> Response:
    """Create a chat in a project + save first message. See create_chat_endpoint."""
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    if temperature is None:
        temperature = queries.get_default_temperature(db)
    temperature = max(0.0, min(2.0, temperature))
    if tool_iteration_cap is None:
        tool_iteration_cap = queries.get_default_tool_iteration_cap(db)
    tool_iteration_cap = max(1, min(10, tool_iteration_cap))
    agent_spec = get_agent(agent)
    chat = queries.create_conversation(
        db,
        name=_placeholder_name(content),
        model=model,
        project_id=project_id,
        temperature=temperature,
        tool_iteration_cap=tool_iteration_cap,
        active_agent=agent_spec.name if agent_spec else None,
    )
    queries.append_message(db, chat.id, "user", content)

    form_data = await request.form()
    enabled_tools_raw = form_data.getlist("enabled_tools")
    enabled_names: set[str] | None = (
        set(enabled_tools_raw) if enabled_tools_raw else None
    )
    queries.seed_chat_tools(db, chat.id, _ALL_TOOL_NAMES, enabled_names=enabled_names)

    enabled_rag_raw = form_data.getlist("enabled_rag_servers")
    enabled_rag: set[str] | None = (
        set(enabled_rag_raw) if enabled_rag_raw else None
    )
    rag_servers_list = _rag_servers_module.list_servers(db)
    queries.seed_chat_rag_servers(
        db, chat.id, [s.name for s in rag_servers_list],
        enabled_names=enabled_rag,
    )

    messages = queries.list_messages(db, chat.id)
    blocks = render.group_messages_for_render(messages)

    await generation.start_generation(
        client=client,
        db=db,
        conversation_id=chat.id,
        temperature=chat.temperature,
        tool_iteration_cap=chat.tool_iteration_cap,
        history=messages,
        on_complete="append",
        **_agent_overrides(chat),
    )

    supports_tools = await ollama.model_supports_tools(client, chat.model)
    if supports_tools:
        tool_states, rag_server_states = _chip_states(db, chat.id, servers=rag_servers_list)
    else:
        tool_states, rag_server_states = [], []

    panel_html = templates.get_template("_chat_panel.html").render(
        conversation=chat,
        blocks=blocks,
        pending_stream_url=f"/chats/{chat.id}/stream",
        active_chat_id=chat.id,
        supports_tools=supports_tools,
        tool_states=tool_states,
        rag_server_states=rag_server_states,
        agents=list_agents(),
        active_agent_spec=agent_spec,
        project=project,
    )

    item_html = templates.get_template("_chat_item.html").render(
        chat=chat,
        active_chat_id=chat.id,
        project=project,
    )
    oob_sidebar_row = (
        f'<ul hx-swap-oob="afterbegin:#chats-list">{item_html}</ul>'
    )

    body = panel_html + oob_sidebar_row
    response = HTMLResponse(content=body, status_code=status.HTTP_201_CREATED)
    response.headers["HX-Push-Url"] = f"/projects/{project_id}/chats/{chat.id}"
    return response


# ---------------------------------------------------------------------------
# Files tab
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/files", response_class=HTMLResponse)
def project_files_endpoint(
    project_id: int,
    request: Request,
    db: DB,
    path: str = ".",
) -> Response:
    """Render the project page with the Files tab active.

    `path` is a workspace-relative directory (default = workspace root).
    """
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    chats = queries.list_conversations_in_project(db, project_id)
    listing = _browse_workspace(project, path)
    files_ctx = {"project": project, "listing": listing}
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_project_page.html",
            context={
                "project": project,
                "active_tab": "files",
                "chats": chats,
                "files_ctx": files_ctx,
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "layout": "project",
            "project": project,
            "active_tab": "files",
            "chats": chats,
            "active_chat_id": None,
            "files_ctx": files_ctx,
        },
    )


@router.get("/projects/{project_id}/files/view", response_class=HTMLResponse)
def project_file_view_endpoint(
    project_id: int,
    request: Request,
    db: DB,
    path: str,
) -> Response:
    """Render a single file's contents in the project page."""
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    chats = queries.list_conversations_in_project(db, project_id)
    contents = _read_workspace_file(project, path)
    files_ctx = {"project": project, "view": contents}
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_project_page.html",
            context={
                "project": project,
                "active_tab": "files",
                "chats": chats,
                "files_ctx": files_ctx,
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "layout": "project",
            "project": project,
            "active_tab": "files",
            "chats": chats,
            "active_chat_id": None,
            "files_ctx": files_ctx,
        },
    )


@router.get("/projects/{project_id}/files/download")
def project_file_download_endpoint(
    project_id: int, db: DB, path: str
) -> Response:
    """Stream a workspace file to the browser as an attachment."""
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    from app.projects import project_workspace_root
    root = project_workspace_root(project)
    if root is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File tools not configured.")
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    from fastapi.responses import FileResponse
    return FileResponse(
        target,
        filename=target.name,
        media_type="application/octet-stream",
    )


@router.get("/projects/{project_id}/settings", response_class=HTMLResponse)
def project_settings_endpoint(
    project_id: int,
    request: Request,
    db: DB,
) -> Response:
    """Render the project page with the Settings tab active."""
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    chats = queries.list_conversations_in_project(db, project_id)
    settings_ctx = {
        "project": project,
        "agents": list_agents(),
        "saved": False,
    }
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_project_page.html",
            context={
                "project": project,
                "active_tab": "settings",
                "chats": chats,
                "settings_ctx": settings_ctx,
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "layout": "project",
            "project": project,
            "active_tab": "settings",
            "chats": chats,
            "active_chat_id": None,
            "settings_ctx": settings_ctx,
        },
    )
```

### C.3 `_browse_workspace` + `_read_workspace_file` helpers

Add to `app/routes.py` (near other helpers):

```python
@dataclass
class _WorkspaceEntry:
    name: str
    is_dir: bool
    size_display: str  # "" for dirs
    href_browse: str | None  # for dirs
    href_view: str | None    # for files
    href_download: str | None


@dataclass
class _WorkspaceListing:
    available: bool  # False when FILE_TOOL_ROOT unset
    path: str
    breadcrumbs: list[tuple[str, str]]  # [(label, href)], including root
    entries: list[_WorkspaceEntry]
    error: str | None  # e.g. "directory not found"


@dataclass
class _WorkspaceFileView:
    available: bool
    path: str
    breadcrumbs: list[tuple[str, str]]
    text: str | None
    is_markdown: bool
    rendered_html: str | None  # populated when is_markdown
    size_display: str
    error: str | None
    download_href: str
```

`_browse_workspace(project, path)`: resolves `project_workspace_root /
path`, validates containment (404 on escape), returns a `_WorkspaceListing`
with dirs-first-then-files sorted entries. Caps at 200 (mirror
`_LIST_DIR_CAP`).

`_read_workspace_file(project, path)`: resolves + validates the same
way, reads UTF-8 (cap at 100KB; binary files show "Binary file — use
Download"), populates `_WorkspaceFileView`.

Implementation skeletons:

```python
def _project_workspace_or_none(project: queries.Project) -> Path | None:
    from app.projects import project_workspace_root, ensure_project_workspace
    root = project_workspace_root(project)
    if root is None:
        return None
    ensure_project_workspace(project)
    return root


def _build_breadcrumbs(
    project_id: int, rel_path: str, tab: str
) -> list[tuple[str, str]]:
    parts = [p for p in Path(rel_path).parts if p not in (".", "")]
    crumbs = [("workspace", f"/projects/{project_id}/files")]
    accum = Path(".")
    for part in parts[:-1] if tab == "view" else parts:
        accum = accum / part
        crumbs.append((part, f"/projects/{project_id}/files?path={accum}"))
    if tab == "view" and parts:
        crumbs.append((parts[-1], f"/projects/{project_id}/files/view?path={rel_path}"))
    return crumbs


def _browse_workspace(project: queries.Project, path: str) -> _WorkspaceListing:
    root = _project_workspace_or_none(project)
    if root is None:
        return _WorkspaceListing(
            available=False, path=path, breadcrumbs=[], entries=[],
            error="File tools are not configured (FILE_TOOL_ROOT is unset).",
        )
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        return _WorkspaceListing(
            available=True, path=path,
            breadcrumbs=_build_breadcrumbs(project.id, ".", "browse"),
            entries=[], error="Path is outside the workspace.",
        )
    if not target.exists() or not target.is_dir():
        return _WorkspaceListing(
            available=True, path=path,
            breadcrumbs=_build_breadcrumbs(project.id, path, "browse"),
            entries=[], error="Directory not found.",
        )
    rel_target = "" if target == root else str(target.relative_to(root))
    entries: list[_WorkspaceEntry] = []
    for child in sorted(
        target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
    )[:200]:
        child_rel = str(child.relative_to(root))
        if child.is_dir():
            entries.append(_WorkspaceEntry(
                name=child.name, is_dir=True, size_display="",
                href_browse=f"/projects/{project.id}/files?path={child_rel}",
                href_view=None, href_download=None,
            ))
        else:
            try:
                size = _format_size_bytes(child.stat().st_size)
            except OSError:
                size = "?"
            entries.append(_WorkspaceEntry(
                name=child.name, is_dir=False, size_display=size,
                href_browse=None,
                href_view=f"/projects/{project.id}/files/view?path={child_rel}",
                href_download=f"/projects/{project.id}/files/download?path={child_rel}",
            ))
    return _WorkspaceListing(
        available=True, path=rel_target or ".",
        breadcrumbs=_build_breadcrumbs(project.id, rel_target or ".", "browse"),
        entries=entries, error=None,
    )


_FILE_VIEW_CAP = 100_000


def _read_workspace_file(
    project: queries.Project, path: str
) -> _WorkspaceFileView:
    root = _project_workspace_or_none(project)
    download_href = f"/projects/{project.id}/files/download?path={path}"
    if root is None:
        return _WorkspaceFileView(
            available=False, path=path, breadcrumbs=[],
            text=None, is_markdown=False, rendered_html=None,
            size_display="", download_href=download_href,
            error="File tools are not configured.",
        )
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return _WorkspaceFileView(
            available=True, path=path,
            breadcrumbs=_build_breadcrumbs(project.id, path, "view"),
            text=None, is_markdown=False, rendered_html=None,
            size_display="", download_href=download_href,
            error="File not found.",
        )
    size = _format_size_bytes(target.stat().st_size)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _WorkspaceFileView(
            available=True, path=path,
            breadcrumbs=_build_breadcrumbs(project.id, path, "view"),
            text=None, is_markdown=False, rendered_html=None,
            size_display=size, download_href=download_href,
            error="Binary file — use Download.",
        )
    if len(text) > _FILE_VIEW_CAP:
        text = text[:_FILE_VIEW_CAP] + "\n\n… (truncated; use Download for full file)"
    is_md = target.suffix.lower() in (".md", ".markdown")
    rendered = None
    if is_md:
        import markdown as _md
        rendered = _md.markdown(text, extensions=["fenced_code", "tables"])
    return _WorkspaceFileView(
        available=True, path=path,
        breadcrumbs=_build_breadcrumbs(project.id, path, "view"),
        text=text, is_markdown=is_md, rendered_html=rendered,
        size_display=size, download_href=download_href, error=None,
    )


def _format_size_bytes(n: int) -> str:
    """Same display format as app/tools/builtins.py's _format_size."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
```

### C.4 Existing chat routes — adjustments

- **`create_chat_endpoint`** (`POST /chats`): **delete**. Replaced by
  `POST /projects/{pid}/chats`.
- **`new_chat_endpoint`** (`GET /new`): **delete**. Replaced by
  `GET /projects/{pid}/chats/new`.
- **`get_chat_panel_endpoint`** (`GET /chats/{cid}`): replace body with
  a redirect to the project-scoped URL:

  ```python
  @router.get("/chats/{conversation_id}")
  def chat_redirect_endpoint(conversation_id: int, db: DB) -> RedirectResponse:
      """Backcompat — resolve project and 302 to the canonical URL."""
      try:
          project = queries.get_project_for_conversation(db, conversation_id)
      except LookupError as e:
          raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
      return RedirectResponse(
          url=f"/projects/{project.id}/chats/{conversation_id}",
          status_code=status.HTTP_302_FOUND,
      )
  ```
- **`delete_chat_endpoint`**: change the `HX-Location` target from `/`
  to `/projects/{pid}/chats` (look up project via
  `get_project_for_conversation` before the delete, since post-delete
  it's gone):

  ```python
  try:
      project = queries.get_project_for_conversation(db, conversation_id)
  except LookupError:
      project = None
  queries.delete_conversation(db, conversation_id)
  response = Response(content="", status_code=status.HTTP_200_OK)
  referer = request.headers.get("Referer", "")
  if project and (
      referer.endswith(f"/projects/{project.id}/chats/{conversation_id}")
      or referer.endswith(f"/chats/{conversation_id}")
  ):
      response.headers["HX-Location"] = f"/projects/{project.id}/chats"
  return response
  ```
- **`rename_chat_endpoint`**: unchanged behavior; the template it
  renders (`_chat_item.html`) gets a `project` context to update the
  link URL — pass `project=queries.get_project_for_conversation(db, conversation_id)`.
- **`get_chat_edit_endpoint`**, **`get_chat_item_endpoint`**: same —
  pass `project` to the templates so the rendered link is project-scoped.
- **`set_chat_agent_endpoint`**: unchanged.
- **`stream_endpoint`**, **`send_message_endpoint`**, **`regenerate_endpoint`**:
  URLs unchanged; bodies unchanged.
- **`settings_endpoint`** (`GET /settings`): unchanged behavior; the
  template now lives under a slightly different layout (see Part D), so
  pass `layout="settings"` instead of `settings_view=True`.

### C.5 Backcompat for sidebar OOB swap during create-chat

Inside `create_project_chat_endpoint` the OOB-prepended sidebar row now
includes the project context. The `_chat_item.html` template needs an
update (Part D.4) to render links scoped to the project. Pass `project`
to the render call.

---

## Part D — Templates

### D.1 `templates/index.html` — three-layout shell

Replace the single-branch body with a layout dispatcher:

```jinja
{% extends "base.html" %}
{% block title %}
  {% if layout == "projects" %}Projects — {% endif %}
  {% if project %}{{ project.name }} — {% endif %}
  {% if conversation %}{{ conversation.name }} — {% endif %}
  olliellama
{% endblock %}
{% block content %}
<div class="layout">
  {% if layout == "projects" %}
    {% include "_projects_sidebar.html" %}
    <main id="main">{% include "_projects_index.html" %}</main>
  {% elif layout == "project" %}
    {% include "_project_sidebar.html" %}
    <main id="main">{% include "_project_page.html" %}</main>
  {% elif layout == "settings" %}
    {% include "_projects_sidebar.html" %}
    <main id="main">
      {% set servers = rag_servers %}
      {% include "_settings.html" %}
    </main>
  {% else %}
    {# Fallback — render the project page if a project is in context. #}
    {% include "_project_sidebar.html" %}
    <main id="main">{% include "_project_page.html" %}</main>
  {% endif %}
</div>
{% endblock %}
```

### D.2 New `templates/_projects_sidebar.html`

A minimal sidebar for the projects-index and global-settings pages:

```jinja
<aside class="sidebar">
  <div class="sidebar__header">
    <h1 class="sidebar__logo">olliellama</h1>
  </div>
  <nav class="sidebar__projects-nav">
    <a class="sidebar__home" href="/projects"
       hx-get="/projects" hx-target="#main"
       hx-swap="innerHTML" hx-push-url="/projects">
      <span class="material-symbols-outlined">folder</span>
      Projects
    </a>
  </nav>
  <div class="sidebar__footer">
    <a class="sidebar__settings" href="/settings"
       hx-get="/settings" hx-target="#main"
       hx-swap="innerHTML" hx-push-url="/settings">
      <span class="material-symbols-outlined">settings</span>
      Settings
    </a>
    <button class="theme-toggle" aria-label="Toggle dark mode" type="button">
      <span class="material-symbols-outlined theme-toggle__icon">dark_mode</span>
    </button>
  </div>
</aside>
```

### D.3 New `templates/_project_sidebar.html`

Sidebar for inside-a-project views: shows project name, "← All
projects" link, this project's chats, settings/theme footer.

```jinja
<aside class="sidebar">
  <div class="sidebar__header">
    <a class="sidebar__back" href="/projects"
       hx-get="/projects" hx-target="#main"
       hx-swap="innerHTML" hx-push-url="/projects"
       aria-label="All projects">
      <span class="material-symbols-outlined">arrow_back</span>
      All projects
    </a>
  </div>
  <div class="sidebar__project-name">{{ project.name }}</div>
  <a class="sidebar__new-chat"
     href="/projects/{{ project.id }}/chats"
     hx-get="/projects/{{ project.id }}/chats/new"
     hx-target="#main" hx-swap="innerHTML"
     hx-push-url="/projects/{{ project.id }}/chats"
     aria-label="New chat">
    <span class="material-symbols-outlined">add</span>
    New chat
  </a>
  {% include "_chats_list.html" %}
  <div class="sidebar__footer">
    <a class="sidebar__settings" href="/settings"
       hx-get="/settings" hx-target="#main"
       hx-swap="innerHTML" hx-push-url="/settings">
      <span class="material-symbols-outlined">settings</span>
      Settings
    </a>
    <button class="theme-toggle" aria-label="Toggle dark mode" type="button">
      <span class="material-symbols-outlined theme-toggle__icon">dark_mode</span>
    </button>
  </div>
</aside>
```

### D.4 Update `templates/_chat_item.html`

Take a `project` context variable and rewrite the link URLs:

```jinja
<li id="chat-{{ chat.id }}" class="chat-item" data-chat-id="{{ chat.id }}"
    {%- if chat.id == active_chat_id|default(none) %} aria-current="page"{% endif %}
    {%- if oob_swap is defined and oob_swap %} hx-swap-oob="{{ oob_swap }}"{% endif %}>
  <a href="/projects/{{ project.id }}/chats/{{ chat.id }}"
     hx-get="/projects/{{ project.id }}/chats/{{ chat.id }}"
     hx-target="#main" hx-swap="innerHTML"
     hx-push-url="true">{{ chat.name }}</a>
  ...
```

Rename + delete kebab buttons keep their `/chats/{id}/edit` and
`/chats/{id}` URLs.

`rename_chat_endpoint`, `get_chat_edit_endpoint`, `get_chat_item_endpoint`,
`set_name_auto` (in `_maybe_emit_title`) all render `_chat_item.html`,
and must pass `project=...` (looked up via
`get_project_for_conversation`) to it.

### D.5 New `templates/_projects_index.html`

Main-panel content for `/projects`:

```jinja
<section class="projects-index">
  <header class="projects-index__header">
    <h2>Projects</h2>
  </header>
  <form class="projects-index__create"
        hx-post="/projects" hx-target="#projects-list"
        hx-swap="afterbegin">
    <label>
      Name
      <input type="text" name="name" required maxlength="80"
             placeholder="My new project">
    </label>
    <label>
      Description
      <input type="text" name="description" maxlength="200"
             placeholder="(optional)">
    </label>
    <button type="submit">Create project</button>
  </form>
  <ul id="projects-list" class="projects-list">
    {% for p in projects %}
      {% include "_project_item.html" %}
    {% endfor %}
  </ul>
</section>
```

### D.6 New `templates/_project_item.html`

```jinja
<li class="project-item" data-project-id="{{ project.id }}">
  <a class="project-item__link"
     href="/projects/{{ project.id }}/chats"
     hx-get="/projects/{{ project.id }}/chats"
     hx-target="#main" hx-swap="innerHTML"
     hx-push-url="true">
    <div class="project-item__name">{{ project.name }}</div>
    {% if project.description %}
      <div class="project-item__desc">{{ project.description }}</div>
    {% endif %}
  </a>
</li>
```

### D.7 New `templates/_project_page.html`

The three-tab wrapper. Tab content varies by `active_tab`.

```jinja
<section class="project-page" data-project-id="{{ project.id }}">
  <header class="project-page__header">
    <h2 class="project-page__name">{{ project.name }}</h2>
    {% include "_project_tabs.html" %}
  </header>
  <div id="project-page-body" class="project-page__body">
    {% if active_tab == "chats" %}
      {% if conversation %}
        {% set ctx = panel_ctx %}
        {% include "_chat_panel.html" %}
      {% else %}
        {% set ctx = composer_ctx %}
        {% include "_composer.html" %}
      {% endif %}
    {% elif active_tab == "files" %}
      {% include "_project_files.html" %}
    {% elif active_tab == "settings" %}
      {% include "_project_settings_body.html" %}
    {% endif %}
  </div>
</section>
```

Note: the `chat_panel` and `composer` includes currently read context
vars directly (`conversation`, `default_tool_states`, etc.) rather than
from a `ctx` dict. Easier change: bubble those vars up through the
route's template context (the routes already do this). The `ctx`
indirection above can be dropped — just pass the right vars.

Simplified body:

```jinja
{% if active_tab == "chats" %}
  {% if conversation %}
    {% include "_chat_panel.html" %}
  {% else %}
    {% set default_tool_states = composer_ctx.default_tool_states %}
    {% set default_rag_server_states = composer_ctx.default_rag_server_states %}
    {% set default_temperature = composer_ctx.default_temperature %}
    {% set default_tool_iteration_cap = composer_ctx.default_tool_iteration_cap %}
    {% set agents = composer_ctx.agents %}
    {% set project_default_model = project.default_model %}
    {% set project_default_agent = project.default_agent %}
    {% include "_composer.html" %}
  {% endif %}
{% elif active_tab == "files" %}
  {% include "_project_files.html" %}
{% elif active_tab == "settings" %}
  {% include "_project_settings_body.html" %}
{% endif %}
```

### D.8 New `templates/_project_tabs.html`

```jinja
<nav class="project-tabs" aria-label="Project sections">
  <a class="project-tabs__tab{% if active_tab == 'chats' %} project-tabs__tab--active{% endif %}"
     href="/projects/{{ project.id }}/chats"
     hx-get="/projects/{{ project.id }}/chats"
     hx-target="#main" hx-swap="innerHTML"
     hx-push-url="true">Chats</a>
  <a class="project-tabs__tab{% if active_tab == 'files' %} project-tabs__tab--active{% endif %}"
     href="/projects/{{ project.id }}/files"
     hx-get="/projects/{{ project.id }}/files"
     hx-target="#main" hx-swap="innerHTML"
     hx-push-url="true">Files</a>
  <a class="project-tabs__tab{% if active_tab == 'settings' %} project-tabs__tab--active{% endif %}"
     href="/projects/{{ project.id }}/settings"
     hx-get="/projects/{{ project.id }}/settings"
     hx-target="#main" hx-swap="innerHTML"
     hx-push-url="true">Settings</a>
</nav>
```

### D.9 New `templates/_project_files.html`

```jinja
{% set listing = files_ctx.listing|default(none) %}
{% set view = files_ctx.view|default(none) %}
<section class="project-files">
  {% if listing %}
    <nav class="project-files__crumbs">
      {% for label, href in listing.breadcrumbs %}
        <a href="{{ href }}"
           hx-get="{{ href }}" hx-target="#main"
           hx-swap="innerHTML" hx-push-url="true">{{ label }}</a>
        {% if not loop.last %} / {% endif %}
      {% endfor %}
    </nav>
    {% if not listing.available %}
      <p class="project-files__empty">{{ listing.error }}</p>
    {% elif listing.error %}
      <p class="project-files__empty">{{ listing.error }}</p>
    {% elif not listing.entries %}
      <p class="project-files__empty">No files yet. Agents that use the file tools will write here.</p>
    {% else %}
      <ul class="project-files__list">
        {% for entry in listing.entries %}
          <li class="project-files__item">
            {% if entry.is_dir %}
              <a class="project-files__dir"
                 href="{{ entry.href_browse }}"
                 hx-get="{{ entry.href_browse }}" hx-target="#main"
                 hx-swap="innerHTML" hx-push-url="true">
                <span class="material-symbols-outlined">folder</span>
                {{ entry.name }}/
              </a>
            {% else %}
              <a class="project-files__file"
                 href="{{ entry.href_view }}"
                 hx-get="{{ entry.href_view }}" hx-target="#main"
                 hx-swap="innerHTML" hx-push-url="true">
                <span class="material-symbols-outlined">description</span>
                {{ entry.name }}
              </a>
              <span class="project-files__size">{{ entry.size_display }}</span>
              <a class="project-files__download"
                 href="{{ entry.href_download }}" download>
                <span class="material-symbols-outlined">download</span>
              </a>
            {% endif %}
          </li>
        {% endfor %}
      </ul>
    {% endif %}
  {% elif view %}
    <nav class="project-files__crumbs">
      {% for label, href in view.breadcrumbs %}
        <a href="{{ href }}"
           hx-get="{{ href }}" hx-target="#main"
           hx-swap="innerHTML" hx-push-url="true">{{ label }}</a>
        {% if not loop.last %} / {% endif %}
      {% endfor %}
    </nav>
    <div class="project-files__view-header">
      <span class="project-files__size">{{ view.size_display }}</span>
      <a class="project-files__download" href="{{ view.download_href }}" download>Download</a>
    </div>
    {% if view.error %}
      <p class="project-files__empty">{{ view.error }}</p>
    {% elif view.is_markdown %}
      <article class="project-files__markdown">{{ view.rendered_html|safe }}</article>
    {% else %}
      <pre class="project-files__pre">{{ view.text }}</pre>
    {% endif %}
  {% endif %}
</section>
```

### D.10 New `templates/_project_settings_body.html`

```jinja
<section class="project-settings">
  <form class="project-settings__form"
        hx-patch="/projects/{{ project.id }}"
        hx-target="#project-page-body" hx-swap="innerHTML">
    <label>
      Name
      <input type="text" name="name" required maxlength="80"
             value="{{ project.name }}">
    </label>
    <label>
      Description
      <textarea name="description" maxlength="400"
                rows="3">{{ project.description }}</textarea>
    </label>
    <label>
      Default model (for new chats)
      <input type="text" name="default_model"
             value="{{ project.default_model or '' }}"
             placeholder="(global default)">
    </label>
    <label>
      Default agent (for new chats)
      <select name="default_agent">
        <option value=""{% if not project.default_agent %} selected{% endif %}>Normal</option>
        {% for a in agents %}
          <option value="{{ a.name }}"{% if project.default_agent == a.name %} selected{% endif %}>{{ a.label }}</option>
        {% endfor %}
      </select>
    </label>
    <div class="project-settings__buttons">
      <button type="submit">Save</button>
      {% if saved %}<span class="project-settings__saved">Saved.</span>{% endif %}
    </div>
  </form>
  <hr>
  <form class="project-settings__delete"
        hx-delete="/projects/{{ project.id }}"
        hx-confirm="Delete project '{{ project.name }}'? Chats will be deleted; workspace files are preserved on disk.">
    <button type="submit" class="danger">Delete project</button>
  </form>
</section>
```

### D.11 Update `templates/_composer.html`

The form action becomes project-scoped. Add `project` context and use:

```jinja
<form class="composer__form"
      hx-post="/projects/{{ project.id }}/chats"
      hx-target="#main"
      hx-swap="innerHTML">
```

Pre-fill the agent select with `project_default_agent`:

```jinja
{% set selected_agent = project_default_agent or "" %}
```

Pre-fill the model select dropdown via JS hook — emit a `data-default`
attribute on the `<select>` so app.js can set it once `/models` finishes
loading:

```jinja
<select id="composer-model" name="model" required
        data-default="{{ project_default_model or '' }}"
        hx-get="/models" hx-trigger="load" hx-target="this" hx-swap="innerHTML">
  <option value="">Loading models…</option>
</select>
```

Then in `static/app.js`, after the `/models` swap returns, read
`data-default` and set `select.value = data-default` if it matches an
option. (Single small JS hook, no framework.)

### D.12 Update `templates/_chats_list.html`

No change — it iterates `chats` and includes `_chat_item.html`. The
project context propagates from the surrounding template.

### D.13 Update `templates/_chat_panel.html`

Two URL updates:

- `hx-post="/chats/{{ conversation.id }}/messages"` — unchanged
- The `_chat_item.html` it OOB-renames or the like — unchanged (those
  templates own their own URLs)

Header should optionally show project breadcrumb:

```jinja
<header class="chat-panel__header">
  <h2 class="chat-panel__name">{{ conversation.name }}</h2>
  ...
```

Optionally above the name (small grey crumb):

```jinja
<div class="chat-panel__crumb">
  <a href="/projects/{{ project.id }}/chats"
     hx-get="/projects/{{ project.id }}/chats" hx-target="#main"
     hx-swap="innerHTML" hx-push-url="true">{{ project.name }}</a> /
</div>
```

### D.14 Static (`static/style.css`, `static/app.js`)

CSS additions:
- `.projects-index`, `.projects-list`, `.project-item`, `.project-item__name`, `.project-item__desc`
- `.project-page`, `.project-page__header`, `.project-page__body`
- `.project-tabs`, `.project-tabs__tab`, `.project-tabs__tab--active`
- `.project-files`, `.project-files__crumbs`, `.project-files__list`, `.project-files__item`, `.project-files__pre`, `.project-files__markdown`, `.project-files__view-header`, `.project-files__size`, `.project-files__download`, `.project-files__empty`
- `.project-settings`, `.project-settings__form`, `.project-settings__buttons`, `.project-settings__saved`, `.project-settings__delete`
- `.sidebar__back`, `.sidebar__project-name`, `.sidebar__projects-nav`, `.sidebar__home`
- `.chat-panel__crumb`

Match the existing visual language (Pico classless + the
`.tool-chip`/`.sidebar` pattern). Tab nav: a row of `<a>` links with
underline-on-active.

JS hook (`static/app.js`):

```js
// Phase 17: project default-model prefill.
// _composer.html's #composer-model loads its <option>s via HTMX from
// /models. After the swap, if the select has a data-default attribute
// matching one of the loaded options, select it.
document.body.addEventListener("htmx:afterSwap", (evt) => {
  const target = evt.target;
  if (target && target.id === "composer-model" && target.dataset.default) {
    const want = target.dataset.default;
    for (const opt of target.options) {
      if (opt.value === want) {
        target.value = want;
        break;
      }
    }
  }
});
```

---

## Part E — `main.py` lifespan

Wire the workspace migration into startup, AFTER db init + AFTER the
existing tool-registry refresh helpers:

```python
from app.connection import open_connection
from app.db import initialize_database
from app.ollama import create_client
from app.projects import migrate_legacy_workspace
from app.routes import router
from app import queries
from app.tools.builtins import refresh_file_tools_registration
from app.tools.rag import refresh_query_rag_registration

# ... inside lifespan, after initialize_database():
refresh_query_rag_registration()
refresh_file_tools_registration()

db = open_connection()
ollama_client = create_client()
app.state.db = db
app.state.ollama_client = ollama_client

# Phase 17: one-shot legacy workspace migration. Runs after open_connection
# so it can read/write the app_settings table.
migrate_legacy_workspace(db, queries)
```

---

## Part F — Tests

### F.1 `tests/test_db.py`

- `test_projects_table_created_on_fresh_db` — a fresh DB has a `projects`
  table with the expected columns.
- `test_default_project_inserted_on_fresh_db` — after init, exactly one
  project named `"Default"` with `workspace_subdir="default"` exists.
- `test_conversations_get_project_id_column` — initialize an old-schema
  DB (manually create tables without `project_id`), insert a chat,
  re-run `initialize_database`, assert the column exists and the chat
  is assigned to the Default project.
- `test_existing_default_project_preserved` — when a project row already
  exists, the migration does NOT create another Default.

### F.2 `tests/test_queries_projects.py` (new)

- `test_create_project_inserts_row`, `..._slugifies_subdir`, `..._handles_collision_via_n_suffix`.
- `test_create_project_name_uniqueness_violation` — second create with
  same name → `IntegrityError`.
- `test_list_projects_alpha_order`.
- `test_get_project_lookup_error_on_missing`.
- `test_get_project_for_conversation`.
- `test_update_project_name_description_defaults` — including the
  sentinel behavior: passing `default_model=None` clears it; not passing
  it leaves it alone.
- `test_delete_project_cascades_to_conversations`.
- `test_count_projects`.
- `test_slugify_project_name` — edge cases (empty, all-punct, long).

### F.3 `tests/test_projects_workspace.py` (new)

- `test_project_workspace_root_returns_subdir_under_root`.
- `test_project_workspace_root_returns_none_when_root_unset`.
- `test_ensure_project_workspace_creates_dir`.
- `test_migrate_legacy_workspace_moves_top_level_entries` —
  set FILE_TOOL_ROOT to a tmp dir with files+dirs, no `default/`;
  run; assert files moved under `default/`, flag set.
- `test_migrate_legacy_workspace_is_idempotent`.
- `test_migrate_legacy_workspace_skips_collision_without_overwrite`.
- `test_migrate_legacy_workspace_noop_when_root_unset`.

### F.4 `tests/test_tools_workspace.py` (new)

- `test_read_file_uses_current_workspace_contextvar` — set
  `current_workspace_root` to a tmp dir, write a file there, call
  `read_file("foo.txt")`, assert content.
- `test_read_file_rejects_path_outside_contextvar_root` — even when
  FILE_TOOL_ROOT is set to a parent dir, the ContextVar wins.
- `test_write_file_writes_into_contextvar_root`.
- `test_search_files_relative_to_contextvar_root`.

### F.5 `tests/test_routes_projects.py` (new)

- `test_get_projects_renders_index`.
- `test_post_projects_creates_and_redirects` — `HX-Push-Url` header.
- `test_post_projects_name_collision_returns_409`.
- `test_get_project_id_redirects_to_chats`.
- `test_get_project_chats_no_active_chat_renders_composer`.
- `test_get_project_chats_with_chat_renders_panel`.
- `test_get_project_chats_404_when_chat_not_in_project`.
- `test_post_project_chats_creates_in_project` — chat's `project_id`
  matches; sidebar OOB row uses the project URL.
- `test_get_project_files_lists_workspace`.
- `test_get_project_files_view_renders_text_file`.
- `test_get_project_files_view_renders_markdown`.
- `test_get_project_files_download_streams_attachment`.
- `test_get_project_files_path_traversal_rejected`.
- `test_get_project_settings_renders_form`.
- `test_patch_project_updates_fields_and_clears_defaults`.
- `test_delete_project_cascades_and_redirects`.
- `test_delete_last_project_returns_409`.
- `test_get_chats_id_legacy_redirects_to_project_url`.
- `test_get_root_redirects_to_projects`.

### F.6 `tests/test_generation.py` updates

- `test_run_generation_sets_workspace_contextvar` — patch the file
  tool's `current_workspace_root` reads and assert the producer sets it
  to the project's workspace root for the duration of the turn.
- Existing tests: add `project_id` to any direct `create_conversation`
  calls in fixtures.

### F.7 `tests/test_routes.py` (existing) updates

Update fixtures so:
- Conversations are created with `project_id` of the Default project.
- Anywhere that asserted against `/chats/{id}` URLs, accept either the
  legacy redirect (302 → project URL) or the new URL directly.
- Tests that hit `GET /` now expect a 302 to `/projects`.

### F.8 `tests/test_integration.py` updates

End-to-end: open `/projects`, create a project, create a chat in it,
send a message, switch tabs, view a file the agent wrote, download it.

### F.9 Coverage target

Keep `pytest --cov=app --cov=main --cov-report=term-missing` at **≥97%**
on `app/` + `main.py`. New modules: `app/projects.py` must reach 100%
on its happy paths. Migration helpers must be hit by `test_db.py`.

---

## Part G — Implementation order

Suggested execution order (sub-phases) for the implementer:

1. **G.1 Schema + queries.** Update `_SCHEMA_SQL`, add migrations, add
   the `Project` dataclass and CRUD in `queries.py`, add `project_id`
   to `Conversation`. Add `list_conversations_in_project`. Run
   `tests/test_db.py` + `test_queries_projects.py` → green.
2. **G.2 Workspace plumbing.** Add `app/projects.py`. Update
   `app/tools/builtins.py` to read from ContextVar. Wire
   `migrate_legacy_workspace` into lifespan. Run
   `test_projects_workspace.py` + `test_tools_workspace.py` → green.
3. **G.3 Generation integration.** Update `_run_generation` to set the
   ContextVar. Add `get_project_for_conversation` call. Update existing
   `test_generation.py` to supply `project_id` in fixtures and assert
   ContextVar behavior. → green.
4. **G.4 Routes — projects.** Add all `/projects` endpoints + helpers.
   Add the `/` redirect. Replace `index_endpoint`. Run
   `test_routes_projects.py` → green.
5. **G.5 Routes — chat URL adjustments.** Replace the old `POST /chats`
   + `GET /new` with redirects / removals. Update `delete_chat_endpoint`
   to redirect to project URL. Pass `project=` context through to
   `_chat_item.html` renders in `rename_chat_endpoint`,
   `get_chat_edit_endpoint`, `get_chat_item_endpoint`, the auto-titler
   in `app/generation._maybe_emit_title`. Update the existing
   `test_routes.py` fixtures + asserts. → green.
6. **G.6 Templates.** Create all the new templates listed in Part D.
   Update `index.html`, `_chat_item.html`, `_chats_list.html`,
   `_chat_panel.html`, `_composer.html`. Add CSS + the small JS hook. →
   `pytest` green, full coverage gate.
7. **G.7 Browser smoke test.** Per CLAUDE.md: `uvicorn main:app
   --reload`, exercise the whole flow described in §Verification.
   Capture screenshots of any non-trivial UI bug found and fix.

Do NOT batch-commit. Per the user's standing rule (`CLAUDE.md` + the
auto-memory): always ask the user before committing — even for trivial
diffs. Run tests after each sub-phase G.1–G.7; if a sub-phase is going
to leave the repo in a non-green state for >5 minutes, stop and surface
the issue before continuing.

---

## Verification (manual browser smoke)

After running `pytest --cov=app --cov=main --cov-report=term-missing`
(must be green, ≥97% coverage):

1. **Migration on existing DB:** snapshot
   `~/Library/Application Support/ollama_slowly/chats.db` to a side
   path, run the app; assert (via `sqlite3` CLI) that:
   - `projects` table exists with exactly one "Default" row.
   - `conversations.project_id` exists and all rows point at Default's id.
   - `app_settings` has `workspace_v2_migrated = "1"`.
   - The workspace's pre-existing files are now under
     `FILE_TOOL_ROOT/default/`.

2. **Projects index:** open `http://localhost:8000` → redirects to
   `/projects`. Create a project named "Test Project"; row appears;
   address bar updates to `/projects/<id>/chats`.

3. **Chats tab:** picker shows Normal / Research / Content Generator;
   model dropdown loads. Create a chat; it streams; sidebar shows it.
   Reload — sidebar still shows only that project's chats.

4. **Files tab:** click Files → workspace lists files; create a chat
   that calls `write_file("notes.md", ...)` via the Content Generator;
   refresh Files tab → file appears at the project's workspace, not
   under another project's.

5. **Cross-project isolation:** create a second project. From its
   Content Generator, `list_directory(".")` → shows only that project's
   workspace contents, not the first project's.

6. **Settings tab:** change `default_model` to a specific id; reload
   the project; create new chat → composer pre-selects that model.

7. **Delete a project:** confirm → redirects to `/projects`; the
   project's chats are gone; the project's on-disk workspace remains.

8. **Backcompat:** open `/chats/<id>` directly → 302 to
   `/projects/<pid>/chats/<id>`.

9. **Last-project guard:** delete projects until one remains; attempt to
   delete it → 409 surfaced inline.

---

## Critical files

| File | Change |
|---|---|
| `app/db.py` | `projects` table in `_SCHEMA_SQL`; `_ensure_default_project`; `_ensure_conversations_project_id_column` (table-rewrite); call in `initialize_database` |
| `app/queries.py` | `Project` dataclass + CRUD; `slugify_project_name`; `_UNSET` sentinel; `Conversation.project_id`; `list_conversations_in_project`; audit row mappers |
| `app/projects.py` | **New** — `current_workspace_root` ContextVar, `project_workspace_root`, `ensure_project_workspace`, `migrate_legacy_workspace` |
| `app/tools/builtins.py` | `_resolve_within_root` + `search_files` read ContextVar with FILE_TOOL_ROOT fallback |
| `app/generation.py` | `_run_generation` resolves project + sets ContextVar around tool loop |
| `app/routes.py` | All `/projects/...` endpoints; `_browse_workspace` / `_read_workspace_file` helpers; replace `index_endpoint` with redirect; delete `POST /chats` + `GET /new`; legacy `GET /chats/{id}` → redirect; `delete_chat_endpoint` redirects to project URL; pass `project=` to `_chat_item.html` renders |
| `main.py` | Call `migrate_legacy_workspace` after `open_connection()` |
| `templates/index.html` | Layout dispatcher (`projects` / `project` / `settings`) |
| `templates/_projects_sidebar.html`, `_project_sidebar.html` | **New** sidebars |
| `templates/_projects_index.html`, `_project_item.html` | **New** projects-index UI |
| `templates/_project_page.html`, `_project_tabs.html` | **New** project page + tabs |
| `templates/_project_files.html`, `_project_settings_body.html` | **New** Files + Settings tab bodies |
| `templates/_chat_item.html` | Project-scoped link URLs |
| `templates/_composer.html` | `hx-post="/projects/{id}/chats"`; project defaults pre-fill |
| `templates/_chat_panel.html` | Optional project crumb |
| `static/style.css` | New sections for projects-index, tabs, files browser, settings form |
| `static/app.js` | Apply `data-default` on `#composer-model` after `/models` swap |
| `tests/test_db.py` | Migration tests |
| `tests/test_queries_projects.py` | **New** |
| `tests/test_projects_workspace.py` | **New** |
| `tests/test_tools_workspace.py` | **New** |
| `tests/test_routes_projects.py` | **New** |
| `tests/test_routes.py`, `test_generation.py`, `test_integration.py` | Fixture updates for `project_id` + URL changes |

---

## Out-of-scope follow-ups (note for the implementer to surface, not build)

- Editable `workspace_subdir` (rename → folder rename).
- Move-chat-between-projects.
- Upload / delete / edit / rename in Files tab.
- Per-project tool/RAG defaults.
- Per-project custom system-prompt prefix.
- A "recently visited project" landing instead of `/projects` index on `/`.

---

## Handoff notes for the implementer

- **Do NOT commit** any code. Surface a summary of what was changed, what
  tests pass, and any unresolved questions; let the user review and
  commit themselves. (See user feedback memory: ask before committing.)
- **Ask, don't guess.** If a decision in this plan turns out to be wrong
  for the codebase (e.g. an `_run_generation` ContextVar set-point
  doesn't interact well with the existing try/finally), stop and
  surface the conflict.
- **Browser smoke test is mandatory** (CLAUDE.md). pytest doesn't
  exercise JS or CSS — the bugs phase 11 shipped came from skipping it.
- **Keep coverage ≥97%** — run `pytest --cov=app --cov=main
  --cov-report=term-missing` after each sub-phase.
- **Migration is one-shot.** Test that re-running lifespan on an
  already-migrated DB is a no-op; in particular, do not run the
  workspace move twice.
