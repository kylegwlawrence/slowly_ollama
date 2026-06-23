"""Built-in tools.

- ``current_time``: baseline tool with no external dependencies.
- ``read_file`` / ``write_file`` / ``list_directory`` / ``search_files``:
  workspace file access, confined to ``FILE_TOOL_ROOT``. When that env var
  is unset these are removed from the registry (see
  :func:`refresh_file_tools_registration`) so the model is never offered a
  tool with nowhere to operate.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import file_tool_root
from app.format import format_size_bytes
from app.tools import tool

# Output caps so a huge result can't blow the model's context window.
# Mirror the caps in app/tools/rag.py.
_READ_FILE_CAP = 50_000
# LIST_DIR_CAP is re-used by the Files-tab browser (``app/routes.py``) so the
# render cap matches the model-facing one.
LIST_DIR_CAP = 200
_SEARCH_CAP = 100


@tool
def current_time(timezone: str = "UTC") -> str:
    """Return the current date and time. Only call when the user explicitly asks for the date/time, or a calculation truly depends on "now" — never as a default, warm-up, or speculative call.

    Args:
        timezone: IANA timezone name like "America/Vancouver" or "UTC".
            Defaults to "UTC". Unknown names fall back to UTC and
            include a note in the returned string.
    """
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        # Return the error as a string (not a raise) so the model can retry,
        # and include the actual UTC time so the call isn't wasted.
        return f"Unknown timezone '{timezone}'; defaulted to UTC. Now: {datetime.now(ZoneInfo('UTC')).isoformat()}"
    return datetime.now(tz).isoformat()


class _PathOutsideRoot(Exception):
    """Raised when a requested path escapes the workspace root.

    Carries a model-facing message; the file tools catch it and return the
    message as their tool output rather than letting it propagate into the
    generation loop.
    """


def _active_workspace_root() -> Path | None:
    """Return the workspace root the file tools should resolve against.

    Reads the per-turn ``current_workspace_root`` ContextVar set by
    ``app.generation._run_generation``, falling back to ``FILE_TOOL_ROOT``
    when unset — covering direct calls from tests, and the popped case where
    the tools aren't registered at all.
    """
    # Lazy import to avoid a top-of-module circular import: both this module
    # and ``app.projects`` are pulled in by various startup paths.
    from app.projects import current_workspace_root

    root = current_workspace_root.get()
    if root is None:
        root = file_tool_root()
    return root


def _resolve_within_root(path: str) -> Path:
    """Resolve a model-supplied path against the active workspace root, rejecting escapes.

    The active root is the per-turn ContextVar (see
    :func:`_active_workspace_root`), falling back to ``FILE_TOOL_ROOT``.

    Args:
        path: Path relative to the active workspace root. Leading slashes
            are stripped; absolute-after-normalization paths and ``..``
            escapes are rejected by the containment check.

    Returns:
        The fully-resolved absolute ``Path`` contained by the active root.

    Raises:
        _PathOutsideRoot: No root configured, or the resolved path escapes
            the root — covering ``..`` traversal and symlink escapes
            (``resolve()`` follows symlinks before the check).
    """
    root = _active_workspace_root()
    if root is None:
        # Unreachable in production (tools are popped when the root is
        # unset); defensive for direct callers like tests.
        raise _PathOutsideRoot(
            "File tools are not configured (FILE_TOOL_ROOT is unset)."
        )
    # Strip leading slashes — models often add them despite "relative path",
    # and pathlib would otherwise discard the root during joining.
    normalized = path.lstrip("/")
    candidate = (root / normalized).resolve()
    if not candidate.is_relative_to(root):
        raise _PathOutsideRoot(
            f"Path '{path}' is outside the allowed workspace."
        )
    return candidate


@tool
def read_file(path: str) -> str:
    """Read a text file from the workspace and return its contents. Use this when the user references a file you need to see. Paths are relative to the workspace root; files outside the workspace cannot be read.

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
        # Binary files, permission errors, etc.: surface as text so the
        # tool loop reacts instead of crashing.
        return f"Could not read '{path}': {e}"
    if len(text) > _READ_FILE_CAP:
        # Reserve 3 chars for the ellipsis so visible length stays at the cap.
        text = text[: _READ_FILE_CAP - 3] + "..."
    return text


@tool
def write_file(path: str, content: str) -> str:
    """Save text to a file in the workspace. WARNING: if the path already exists the entire file is replaced — only call when the user asks you to save, write, or update a file, and pass the FULL final contents. Only the workspace directory is writable.

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
    """List files and subdirectories in a workspace directory. Use this to see what files exist before reading them. Pass "." (the default) to list the workspace root. Only the workspace directory is accessible.

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
    truncated = len(entries) > LIST_DIR_CAP
    for entry in entries[:LIST_DIR_CAP]:
        if entry.is_dir():
            lines.append(f"[dir]  {entry.name}/")
        else:
            try:
                size_str = format_size_bytes(entry.stat().st_size)
            except OSError:
                size_str = "?"
            lines.append(f"[file] {entry.name} ({size_str})")

    header = f"{path}/ ({len(entries)} item{'s' if len(entries) != 1 else ''})"
    if truncated:
        header += f" — showing first {LIST_DIR_CAP}"
    return header + "\n\n" + "\n".join(lines)


@tool
def search_files(pattern: str, path: str = ".") -> str:
    """Find files in the workspace by name pattern (e.g. "*.md" or "report_*.txt"), searched recursively. Returns file paths only — call read_file afterwards to see contents.

    Args:
        pattern: Filename pattern to match, e.g. "*.md" or "report_*.txt".
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
    # Same active root as _resolve_within_root, for the relative-path display.
    root = _active_workspace_root()
    lines: list[str] = []
    for m in matches[:_SEARCH_CAP]:
        try:
            size_str = format_size_bytes(m.stat().st_size)
        except OSError:
            size_str = "?"
        rel = m.relative_to(root)
        lines.append(f"[file] {rel} ({size_str})")
    header = f'{total} file{"s" if total != 1 else ""} matching "{pattern}" in \'{path}\''
    if truncated:
        header += f" — showing first {_SEARCH_CAP}"
    return header + "\n\n" + "\n".join(lines)


# Snapshot the file-tool specs built by @tool above so
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

    When ``FILE_TOOL_ROOT`` is unset, the file tools are removed from
    ``app.tools.TOOLS`` so the model is never offered a tool with nowhere to
    operate; when set, they are (re-)added from the decoration-time snapshot.

    Mirrors :func:`app.tools.rag.refresh_query_rag_registration`. Called at
    lifespan startup; the root is static .env config, so there is no
    per-request refresh.
    """
    # Re-import locally so tests that patch app.tools.TOOLS see the right object.
    from app.tools import TOOLS

    if file_tool_root() is None:
        for name in _FILE_TOOL_SPECS:
            TOOLS.pop(name, None)
        return
    for name, spec in _FILE_TOOL_SPECS.items():
        if name not in TOOLS:
            TOOLS[name] = spec
