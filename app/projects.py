"""Per-project workspace path resolution + Files-tab browsing helpers.

The file tools confine reads/writes to one directory. With projects, that
directory is per-turn: ``FILE_TOOL_ROOT/<project.workspace_subdir>``. The
generation producer sets it via the :data:`current_workspace_root`
ContextVar before each tool call; the file tools read that var, falling
back to ``FILE_TOOL_ROOT`` when no project is bound (tests, direct calls).

:func:`migrate_legacy_workspace` is a one-shot that moves pre-projects
``FILE_TOOL_ROOT`` contents into ``default/`` so the Default project owns
them. The rest of this module builds the view-shaped payloads the Files
tab renders.
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


# Set by the generation producer before each turn's tool calls; reset in the
# matching finally block. ``None`` = fall back to FILE_TOOL_ROOT (direct test
# invocations that don't bind a project).
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

    Walks the top-level entries of ``FILE_TOOL_ROOT`` and moves anything
    that isn't ``"default"`` or another project's ``workspace_subdir`` into
    ``default/``, so the Default project owns the user's pre-projects files.

    Gated by ``app_settings("workspace_v2_migrated")`` so later boots no-op.
    On a name collision it skips rather than overwrites — never silently
    lose a user's file.

    Args:
        db: Open SQLite connection (the lifespan-shared one).
        queries_mod: The ``app.queries`` module, passed in to avoid a
            circular import and keep the helper testable.
    """
    if queries_mod.get_setting(db, "workspace_v2_migrated") == "1":
        return

    root = file_tool_root()
    if root is None:
        # Nothing to migrate, but still set the flag so we don't re-check.
        queries_mod.set_setting(db, "workspace_v2_migrated", "1")
        return

    # Names to leave alone: "default" plus every existing project's
    # workspace_subdir — moving one would clobber that project's workspace.
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

    Dirs get ``href_browse``; files get ``href_view`` / ``href_download``
    and a ``size_display``. The unused hrefs are None.

    Attributes:
        name: Display name.
        is_dir: True for directories.
        size_display: Pretty-printed byte count for files; '' for dirs.
        href_browse: URL to descend into a directory.
        href_view: URL to render a file in the Files tab.
        href_download: URL to download a file as an attachment.
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
        available: False when FILE_TOOL_ROOT is unset (file tools off).
        path: The workspace-relative directory being shown.
        breadcrumbs: ``[(label, href), ...]`` from root to current dir.
        entries: Listed children (dirs first, then files), capped.
        error: User-facing reason the listing failed (path outside
            workspace, not found); set instead of ``entries``.
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
        breadcrumbs: Crumbs from root down to and including the file.
        text: UTF-8 contents (truncated at the cap), or None when not
            displayable as text.
        is_markdown: True for ``.md`` / ``.markdown``.
        rendered_html: Pre-rendered HTML for markdown (None for plain text).
        size_display: Pretty-printed file size.
        error: User-facing reason the file can't be displayed (not found,
            binary, ...).
        download_href: URL to download the original file.
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

    Pre-creates the dir so the Files tab doesn't show "directory not found"
    the first time a user opens it on a new project.
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
            The view tab's last crumb links to the file viewer rather than
            a directory.

    Returns:
        Ordered crumbs starting with ``("workspace", root URL)``.
    """
    # Filter the leading "." that Path.parts yields for "." or "".
    parts = [p for p in Path(rel_path).parts if p not in (".", "")]
    crumbs: list[tuple[str, str]] = [
        ("workspace", f"/projects/{project_id}/files")
    ]
    accum = Path(".")
    # On a file view the last part is the file (handled below), so only the
    # leading parts are directory links.
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
    # Dirs first, then files; each group case-insensitive alphabetical.
    # Matches ``list_directory``'s ordering.
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
        # Reuse the chat renderer so workspace .md files typeset LaTeX like
        # assistant messages — it emits .arithmatex spans (KaTeX-typeset by
        # static/app.js) and shields math from the parser. A bare
        # markdown.markdown() leaves raw \(...\) on the page.
        from app.templates import _render_markdown

        rendered = _render_markdown(text)
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
