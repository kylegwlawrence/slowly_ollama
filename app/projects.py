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
from dataclasses import dataclass
from pathlib import Path

from app.config import file_tool_root
from app.format import format_size_bytes
from app.queries import Project
from app.tools.builtins import LIST_DIR_CAP

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


# ---------------------------------------------------------------------------
# Files-tab browsing — view-shaped helpers consumed by app.routes
# ---------------------------------------------------------------------------


# UTF-8 text-view ceiling. Larger files are rendered truncated with a
# "use Download for full file" hint.
_FILE_VIEW_CAP = 100_000


@dataclass
class WorkspaceEntry:
    """One row in the Files-tab directory listing.

    Attributes:
        name: Display name of the file or directory.
        is_dir: True for directories (which get a browse link, no
            size, no download).
        size_display: Pretty-printed byte count for files; empty
            string for dirs.
        href_browse: URL to descend into the directory (None for files).
        href_view: URL to render the file in the Files tab (None for dirs).
        href_download: URL to download the file as an attachment (None
            for dirs).
    """

    name: str
    is_dir: bool
    size_display: str
    href_browse: str | None
    href_view: str | None
    href_download: str | None


@dataclass
class WorkspaceListing:
    """Result shape for :func:`browse_workspace`.

    Attributes:
        available: False when FILE_TOOL_ROOT is unset (file tools off);
            the template renders an "unavailable" message.
        path: The workspace-relative directory being shown.
        breadcrumbs: ``[(label, href), ...]`` from workspace root down to
            the current directory.
        entries: The listed children (dirs first, then files), capped.
        error: A user-facing reason when the listing failed (e.g. the
            path is outside the workspace, or doesn't exist). Mutually
            exclusive with ``entries`` carrying useful data.
    """

    available: bool
    path: str
    breadcrumbs: list[tuple[str, str]]
    entries: list[WorkspaceEntry]
    error: str | None


@dataclass
class WorkspaceFileView:
    """Result shape for :func:`read_workspace_file`.

    Attributes:
        available: False when FILE_TOOL_ROOT is unset.
        path: The workspace-relative file path.
        breadcrumbs: Crumbs from root down to (and including) the file.
        text: UTF-8 contents (truncated at the cap), or None when the
            file isn't displayable as text.
        is_markdown: True for ``.md`` / ``.markdown`` extensions.
        rendered_html: Pre-rendered HTML for markdown views (None for
            plain text).
        size_display: Pretty-printed file size.
        error: User-facing reason when the file can't be displayed (not
            found, binary, etc.).
        download_href: URL to download the original file as an attachment.
    """

    available: bool
    path: str
    breadcrumbs: list[tuple[str, str]]
    text: str | None
    is_markdown: bool
    rendered_html: str | None
    size_display: str
    error: str | None
    download_href: str


def _project_workspace_or_none(project: Project) -> Path | None:
    """Return the project's workspace dir (creating it), or None when off.

    Wrapper that combines :func:`project_workspace_root` and
    :func:`ensure_project_workspace` so the Files-tab helpers can
    pre-create the dir on first visit — the listing isn't an immediate
    "directory not found" the first time a user clicks Files after
    creating a project.
    """
    root = project_workspace_root(project)
    if root is None:
        return None
    ensure_project_workspace(project)
    return root


def _build_breadcrumbs(
    project_id: int, rel_path: str, tab: str
) -> list[tuple[str, str]]:
    """Build ``[(label, href), ...]`` for the workspace breadcrumb bar.

    Args:
        project_id: The owning project's id (interpolated into URLs).
        rel_path: Workspace-relative path being shown (``"."`` for root).
        tab: ``"browse"`` (directory listing) or ``"view"`` (single file).
            The browse tab's last crumb points at the directory itself;
            the view tab's last crumb points at the file viewer for that
            file.

    Returns:
        Ordered crumbs starting with ``("workspace", root URL)``.
    """
    # ``Path.parts`` includes a leading "." for "." or "" — filter it out
    # so the crumb list doesn't start with a vestigial entry.
    parts = [p for p in Path(rel_path).parts if p not in (".", "")]
    crumbs: list[tuple[str, str]] = [
        ("workspace", f"/projects/{project_id}/files")
    ]
    accum = Path(".")
    # For a directory view, every part is a clickable subdirectory link.
    # For a file view, the last part is the file (rendered by view tab),
    # so only the leading parts are directory links.
    nav_parts = parts if tab == "browse" else parts[:-1]
    for part in nav_parts:
        accum = accum / part
        crumbs.append(
            (part, f"/projects/{project_id}/files?path={accum}")
        )
    if tab == "view" and parts:
        crumbs.append(
            (
                parts[-1],
                f"/projects/{project_id}/files/view?path={rel_path}",
            )
        )
    return crumbs


def browse_workspace(project: Project, path: str) -> WorkspaceListing:
    """Build a directory listing for the Files tab.

    Args:
        project: The owning project.
        path: Workspace-relative directory path (``"."`` = workspace root).

    Returns:
        A populated :class:`WorkspaceListing`. ``available`` is False when
        FILE_TOOL_ROOT is unset; ``error`` is populated for path-outside-
        workspace or directory-not-found cases.
    """
    root = _project_workspace_or_none(project)
    if root is None:
        return WorkspaceListing(
            available=False,
            path=path,
            breadcrumbs=[],
            entries=[],
            error="File tools are not configured (FILE_TOOL_ROOT is unset).",
        )
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        return WorkspaceListing(
            available=True,
            path=path,
            breadcrumbs=_build_breadcrumbs(project.id, ".", "browse"),
            entries=[],
            error="Path is outside the workspace.",
        )
    if not target.exists() or not target.is_dir():
        return WorkspaceListing(
            available=True,
            path=path,
            breadcrumbs=_build_breadcrumbs(project.id, path, "browse"),
            entries=[],
            error="Directory not found.",
        )
    rel_target = "" if target == root else str(target.relative_to(root))
    entries: list[WorkspaceEntry] = []
    # Sort: dirs first, then files; each group alphabetical (case-
    # insensitive). Matches ``list_directory``'s ordering.
    children = sorted(
        target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
    )[:LIST_DIR_CAP]
    for child in children:
        child_rel = str(child.relative_to(root))
        if child.is_dir():
            entries.append(
                WorkspaceEntry(
                    name=child.name,
                    is_dir=True,
                    size_display="",
                    href_browse=(
                        f"/projects/{project.id}/files?path={child_rel}"
                    ),
                    href_view=None,
                    href_download=None,
                )
            )
        else:
            try:
                size = format_size_bytes(child.stat().st_size)
            except OSError:
                size = "?"
            entries.append(
                WorkspaceEntry(
                    name=child.name,
                    is_dir=False,
                    size_display=size,
                    href_browse=None,
                    href_view=(
                        f"/projects/{project.id}/files/view?path={child_rel}"
                    ),
                    href_download=(
                        f"/projects/{project.id}/files/download?path={child_rel}"
                    ),
                )
            )
    return WorkspaceListing(
        available=True,
        path=rel_target or ".",
        breadcrumbs=_build_breadcrumbs(
            project.id, rel_target or ".", "browse"
        ),
        entries=entries,
        error=None,
    )


def read_workspace_file(project: Project, path: str) -> WorkspaceFileView:
    """Build a file-view payload for the Files tab.

    Args:
        project: The owning project.
        path: Workspace-relative file path.

    Returns:
        A populated :class:`WorkspaceFileView`. ``error`` is populated for
        path-outside-workspace, file-not-found, or binary-file cases.
    """
    root = _project_workspace_or_none(project)
    download_href = (
        f"/projects/{project.id}/files/download?path={path}"
    )
    if root is None:
        return WorkspaceFileView(
            available=False,
            path=path,
            breadcrumbs=[],
            text=None,
            is_markdown=False,
            rendered_html=None,
            size_display="",
            download_href=download_href,
            error="File tools are not configured.",
        )
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return WorkspaceFileView(
            available=True,
            path=path,
            breadcrumbs=_build_breadcrumbs(project.id, path, "view"),
            text=None,
            is_markdown=False,
            rendered_html=None,
            size_display="",
            download_href=download_href,
            error="File not found.",
        )
    size = format_size_bytes(target.stat().st_size)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return WorkspaceFileView(
            available=True,
            path=path,
            breadcrumbs=_build_breadcrumbs(project.id, path, "view"),
            text=None,
            is_markdown=False,
            rendered_html=None,
            size_display=size,
            download_href=download_href,
            error="Binary file — use Download.",
        )
    if len(text) > _FILE_VIEW_CAP:
        text = (
            text[:_FILE_VIEW_CAP]
            + "\n\n… (truncated; use Download for full file)"
        )
    is_md = target.suffix.lower() in (".md", ".markdown")
    rendered = None
    if is_md:
        import markdown as _md

        rendered = _md.markdown(text, extensions=["fenced_code", "tables"])
    return WorkspaceFileView(
        available=True,
        path=path,
        breadcrumbs=_build_breadcrumbs(project.id, path, "view"),
        text=text,
        is_markdown=is_md,
        rendered_html=rendered,
        size_display=size,
        download_href=download_href,
        error=None,
    )
