"""Phase 17: per-project workspace path resolution + one-shot migration.

The file tools (``app/tools/builtins.py``) confine reads / writes to a
single directory. Pre-17 that was always ``FILE_TOOL_ROOT`` (set by .env).
With projects, the active directory is per-turn:
``FILE_TOOL_ROOT/<project.workspace_subdir>``.

The generation producer (``app/generation._run_generation``) sets the
active workspace via the :data:`current_workspace_root` ContextVar before
each tool call. The file tools read that var (with a fallback to
``FILE_TOOL_ROOT`` for tests / direct invocation that aren't bound to a
project).

A one-shot ``migrate_legacy_workspace`` helper moves the pre-projects
contents of ``FILE_TOOL_ROOT`` into ``FILE_TOOL_ROOT/default/`` so the
new "Default" project naturally owns whatever the user had before phase
17. The migration is gated by an ``app_settings`` flag so re-runs are
no-ops.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from contextvars import ContextVar
from pathlib import Path

from app.config import file_tool_root
from app.queries import Project

logger = logging.getLogger(__name__)


# Set by ``app.generation._run_generation`` before each turn's tool
# calls; reset in the matching finally block. ``None`` means "fall back
# to FILE_TOOL_ROOT" — used by direct test invocations of the file tools
# that don't bind a project.
current_workspace_root: ContextVar[Path | None] = ContextVar(
    "current_workspace_root", default=None
)


def project_workspace_root(project: Project) -> Path | None:
    """Compute the on-disk workspace root for a project.

    Does NOT create the directory — see :func:`ensure_project_workspace`
    for the creating variant.

    Args:
        project: The project whose workspace path to resolve.

    Returns:
        ``FILE_TOOL_ROOT / project.workspace_subdir``, fully resolved.
        Returns ``None`` when ``FILE_TOOL_ROOT`` is unset (file tools
        disabled at the config layer).
    """
    root = file_tool_root()
    if root is None:
        return None
    return (root / project.workspace_subdir).resolve()


def ensure_project_workspace(project: Project) -> Path | None:
    """Create the project's workspace directory on disk if it doesn't exist.

    Safe to call repeatedly — uses ``mkdir(parents=True, exist_ok=True)``.

    Args:
        project: The project whose workspace to materialize.

    Returns:
        The resolved workspace path, or ``None`` when ``FILE_TOOL_ROOT`` is
        unset (in which case there's no on-disk workspace to create).
    """
    target = project_workspace_root(project)
    if target is None:
        return None
    target.mkdir(parents=True, exist_ok=True)
    return target


def migrate_legacy_workspace(
    db: sqlite3.Connection,
    queries_mod,
) -> None:
    """One-shot move of pre-projects ``FILE_TOOL_ROOT`` contents into ``default/``.

    Before phase 17 the user's workspace files lived at the top of
    ``FILE_TOOL_ROOT``; under projects the Default project owns
    ``FILE_TOOL_ROOT/default/``. To keep existing files reachable from the
    Default project, this helper walks the top-level entries of
    ``FILE_TOOL_ROOT`` and moves anything that doesn't already match an
    existing project's ``workspace_subdir`` (and isn't ``"default"``
    itself) into the new ``default/`` directory.

    Gated by ``app_settings("workspace_v2_migrated")`` so subsequent boots
    are no-ops. Logs everything it moves; warns on collisions and skips
    rather than overwriting (a user-visible filename clash should never
    silently lose data).

    Args:
        db: Open SQLite connection (the lifespan-shared one).
        queries_mod: The ``app.queries`` module, passed in to avoid a
            circular import (``queries`` doesn't import from this module
            either, but the indirection keeps the helper testable).
    """
    if queries_mod.get_setting(db, "workspace_v2_migrated") == "1":
        return

    root = file_tool_root()
    if root is None:
        # No workspace configured — nothing to migrate. Still set the flag
        # so we don't re-check on every boot.
        queries_mod.set_setting(db, "workspace_v2_migrated", "1")
        return

    # Names that should NOT be moved into ``default/``: the literal
    # "default" dir itself, plus any other project's workspace_subdir
    # (in case the user pre-created project rows by hand). Touching one
    # of those would clobber an unrelated project's workspace.
    reserved = {p.workspace_subdir for p in queries_mod.list_projects(db)}
    reserved.add("default")

    default_dir = root / "default"
    default_dir.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []
    skipped: list[str] = []
    if root.exists():
        for entry in root.iterdir():
            if entry.name in reserved:
                continue
            target = default_dir / entry.name
            if target.exists():
                logger.warning(
                    "Workspace v2 migration: %s already exists in default/, "
                    "skipping move",
                    entry.name,
                )
                skipped.append(entry.name)
                continue
            shutil.move(str(entry), str(target))
            moved.append(entry.name)

    if moved or skipped:
        logger.info(
            "Workspace v2 migration: moved %d entries into %s "
            "(skipped %d collisions)",
            len(moved),
            default_dir,
            len(skipped),
        )

    queries_mod.set_setting(db, "workspace_v2_migrated", "1")
