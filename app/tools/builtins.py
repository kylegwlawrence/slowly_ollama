"""Built-in tools shipped with phase 12 (and later).

- ``current_time``: the baseline that validates the tool-calling loop
  without depending on any external service.
- ``read_file`` / ``write_file`` / ``list_directory``: workspace file
  access, confined to the directory named by ``FILE_TOOL_ROOT``. When
  that env var is unset the trio is removed from the registry (see
  :func:`refresh_file_tools_registration`) so the model is never offered
  a tool with nowhere to operate.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import file_tool_root
from app.tools import tool

# Hard cap on read_file output so a huge file can't blow the model's
# context window. Mirrors the output caps in app/tools/rag.py.
_READ_FILE_CAP = 50_000

# Hard cap on list_directory entries so a huge directory can't blow the
# model's context window.
_LIST_DIR_CAP = 200

# Hard cap on search_files results so a broad pattern can't blow the
# model's context window.
_SEARCH_CAP = 100


def _format_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable string.

    Args:
        size_bytes: File size in bytes.

    Returns:
        A compact string like "4 B", "1.2 KB", or "3.4 MB".
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


@tool
def current_time(timezone: str = "UTC") -> str:
    """Get the current wall-clock time as ISO 8601. Only call this when the user explicitly asks for the date/time or a calculation genuinely depends on "now"; never as a default, warm-up, or speculative call.

    Args:
        timezone: IANA timezone name like "America/Vancouver" or "UTC".
            Defaults to "UTC". Unknown names fall back to UTC and
            include a note in the returned string.
    """
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        # Don't raise — the model just sees the error string and can
        # retry with a valid timezone. Always include the actual time
        # so the call isn't a total loss.
        return f"Unknown timezone '{timezone}'; defaulted to UTC. Now: {datetime.now(ZoneInfo('UTC')).isoformat()}"
    return datetime.now(tz).isoformat()


class _PathOutsideRoot(Exception):
    """Raised internally when a requested path escapes the workspace root.

    Carries a model-facing message; the file tools catch it and return
    the message as their (string) tool output rather than letting it
    propagate into the generation loop.
    """


def _active_workspace_root() -> Path | None:
    """Return the workspace root the file tools should resolve against.

    Phase 17: reads the per-turn ``current_workspace_root`` ContextVar set
    by ``app.generation._run_generation``. Falls back to ``FILE_TOOL_ROOT``
    when the var is unset — covering test code that calls the tools
    directly without binding a project, and the
    ``refresh_file_tools_registration``-popped case where the tools aren't
    registered at all (so this branch is unreachable in production but
    safe to keep for defensive direct invocations).
    """
    # Imported lazily to avoid a top-of-module circular import — both
    # this module and ``app.projects`` are pulled in by various startup
    # paths, and the local import keeps the dependency edge one-way.
    from app.projects import current_workspace_root

    root = current_workspace_root.get()
    if root is None:
        root = file_tool_root()
    return root


def _resolve_within_root(path: str) -> Path:
    """Resolve a model-supplied path against the active workspace root, rejecting escapes.

    Phase 17: the active root comes from the per-turn ContextVar set by
    the generation producer (see :func:`_active_workspace_root`). When no
    project-scoped root is in effect, falls back to ``FILE_TOOL_ROOT``.

    Args:
        path: Path as the model supplied it, interpreted relative to the
            active workspace root. An absolute path escapes the root
            (joining an absolute path onto the root discards the root),
            so it is rejected by the containment check below.

    Returns:
        The fully-resolved absolute ``Path`` contained by the active root.

    Raises:
        _PathOutsideRoot: When no root is configured, or the resolved
            path is not contained by the root — covering ``..``
            traversal, absolute paths, and symlink escapes (``resolve()``
            follows symlinks before the check).
    """
    root = _active_workspace_root()
    if root is None:
        # Unreachable in production: the tools are popped from the
        # registry when the root is unset. Defensive in case a caller
        # invokes the function directly (e.g. a test).
        raise _PathOutsideRoot(
            "File tools are not configured (FILE_TOOL_ROOT is unset)."
        )
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise _PathOutsideRoot(
            f"Path '{path}' is outside the allowed workspace."
        )
    return candidate


@tool
def read_file(path: str) -> str:
    """Read a UTF-8 text file from the workspace and return its contents. Only files inside the configured workspace directory are accessible.

    Args:
        path: Path to the file, relative to the workspace root (e.g.
            "notes/todo.md"). Paths that escape the workspace, or that
            point at a non-existent or non-text file, return an
            explanatory message instead of raising.
    """
    try:
        target = _resolve_within_root(path)
    except _PathOutsideRoot as e:
        return str(e)
    if not target.is_file():
        return f"No file at '{path}'."
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        # Binary files, permission errors, etc. Surface as text so the
        # model can react instead of the tool loop crashing.
        return f"Could not read '{path}': {e}"
    if len(text) > _READ_FILE_CAP:
        # Reserve 3 chars for the ellipsis so the visible length stays at
        # _READ_FILE_CAP exactly (mirrors the RAG truncation convention).
        text = text[: _READ_FILE_CAP - 3] + "..."
    return text


@tool
def write_file(path: str, content: str) -> str:
    """Create or overwrite a UTF-8 text file in the workspace with the given content. Only the configured workspace directory is writable; an existing file at the path is replaced.

    Args:
        path: Path to the file, relative to the workspace root. Missing
            parent directories are created. Paths that escape the
            workspace return an explanatory message instead of writing.
        content: Full text to write. Any existing file is overwritten.
    """
    try:
        target = _resolve_within_root(path)
    except _PathOutsideRoot as e:
        return str(e)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"Could not write '{path}': {e}"
    return f"Wrote {len(content)} characters to '{path}'."


@tool
def list_directory(path: str = ".") -> str:
    """List the files and subdirectories inside a workspace directory. Directories appear before files, both sorted alphabetically. Only paths inside the configured workspace are accessible. Use "." (the default) to list the workspace root.

    Args:
        path: Path to the directory, relative to the workspace root (e.g.
            "notes" or "."). Paths that escape the workspace, or that
            point at a non-existent or non-directory path, return an
            explanatory message instead of raising.
    """
    try:
        target = _resolve_within_root(path)
    except _PathOutsideRoot as e:
        return str(e)
    if not target.exists():
        return f"No directory at '{path}'."
    if not target.is_dir():
        return f"'{path}' is a file, not a directory. Use read_file to read it."
    try:
        # Dirs first, then files; each group sorted case-insensitively.
        entries = sorted(
            target.iterdir(),
            key=lambda p: (p.is_file(), p.name.lower()),
        )
    except OSError as e:
        return f"Could not list '{path}': {e}"

    if not entries:
        return f"'{path}' is empty."

    lines: list[str] = []
    truncated = len(entries) > _LIST_DIR_CAP
    for entry in entries[:_LIST_DIR_CAP]:
        if entry.is_dir():
            lines.append(f"[dir]  {entry.name}/")
        else:
            try:
                size_str = _format_size(entry.stat().st_size)
            except OSError:
                size_str = "?"
            lines.append(f"[file] {entry.name} ({size_str})")

    header = f"{path}/ ({len(entries)} item{'s' if len(entries) != 1 else ''})"
    if truncated:
        header += f" — showing first {_LIST_DIR_CAP}"
    return header + "\n\n" + "\n".join(lines)


@tool
def search_files(pattern: str, path: str = ".") -> str:
    """Find files matching a glob pattern anywhere inside a workspace directory.

    Args:
        pattern: Filename glob to match, e.g. "*.md" or "report_*.txt".
            Applied recursively under the starting path. Only files are
            returned — directories are excluded.
        path: Starting directory, relative to the workspace root. Defaults to
            "." (the workspace root). Must stay inside the workspace.
    """
    try:
        target = _resolve_within_root(path)
    except _PathOutsideRoot as e:
        return str(e)
    if not target.exists():
        return f"No directory at '{path}'."
    if not target.is_dir():
        return f"'{path}' is a file, not a directory. Use read_file to read it."
    try:
        matches = sorted(
            (m for m in target.rglob(pattern) if m.is_file()),
            key=lambda p: str(p).lower(),
        )
    except (OSError, ValueError) as e:
        return f"Could not search '{path}': {e}"
    if not matches:
        return f'No files matching "{pattern}" in \'{path}\'.'
    total = len(matches)
    truncated = total > _SEARCH_CAP
    # Phase 17: same resolution rule as _resolve_within_root — the active
    # workspace is the per-turn ContextVar set by the producer, falling
    # back to FILE_TOOL_ROOT.
    root = _active_workspace_root()
    lines: list[str] = []
    for m in matches[:_SEARCH_CAP]:
        try:
            size_str = _format_size(m.stat().st_size)
        except OSError:
            size_str = "?"
        rel = m.relative_to(root)
        lines.append(f"[file] {rel} ({size_str})")
    header = f'{total} file{"s" if total != 1 else ""} matching "{pattern}" in \'{path}\''
    if truncated:
        header += f" — showing first {_SEARCH_CAP}"
    return header + "\n\n" + "\n".join(lines)


# Snapshot the file-tool specs the @tool decorator built above so
# refresh_file_tools_registration() can re-add them after a pop without
# losing the introspected schema. Mirrors app/tools/rag.py.
from app.tools import TOOLS as _TOOLS  # noqa: E402

_FILE_TOOL_SPECS = {
    "read_file": _TOOLS["read_file"],
    "write_file": _TOOLS["write_file"],
    "list_directory": _TOOLS["list_directory"],
    "search_files": _TOOLS["search_files"],
}


def refresh_file_tools_registration() -> None:
    """Sync the file tools' registry presence to whether a root is configured.

    When ``FILE_TOOL_ROOT`` is unset, every file tool (``read_file`` /
    ``write_file`` / ``list_directory`` / ``search_files``) is removed
    from ``app.tools.TOOLS`` so the chat model is never offered a tool
    with nowhere to operate. When it is set, all of them are (re-)added
    from the specs snapshotted at decoration time.

    Mirrors :func:`app.tools.rag.refresh_query_rag_registration`. Called
    at lifespan startup so the initial registry matches config; the root
    is static .env config, so there is no per-request refresh.
    """
    # Re-import locally so tests that patch app.tools.TOOLS see the right
    # object (same reasoning as refresh_query_rag_registration).
    from app.tools import TOOLS

    if file_tool_root() is None:
        for name in _FILE_TOOL_SPECS:
            TOOLS.pop(name, None)
        return
    for name, spec in _FILE_TOOL_SPECS.items():
        if name not in TOOLS:
            TOOLS[name] = spec
