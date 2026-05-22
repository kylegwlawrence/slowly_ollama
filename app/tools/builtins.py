"""Built-in tools shipped with phase 12 (and later).

- ``current_time``: the baseline that validates the tool-calling loop
  without depending on any external service.
- ``read_file`` / ``write_file``: workspace file access, confined to the
  directory named by ``FILE_TOOL_ROOT``. When that env var is unset the
  pair is removed from the registry (see
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


def _resolve_within_root(path: str) -> Path:
    """Resolve a model-supplied path against the workspace root, rejecting escapes.

    Args:
        path: Path as the model supplied it, interpreted relative to the
            configured root. An absolute path escapes the root (joining
            an absolute path onto the root discards the root), so it is
            rejected by the containment check below.

    Returns:
        The fully-resolved absolute ``Path`` contained by the root.

    Raises:
        _PathOutsideRoot: When no root is configured, or the resolved
            path is not contained by the root — covering ``..``
            traversal, absolute paths, and symlink escapes (``resolve()``
            follows symlinks before the check).
    """
    root = file_tool_root()
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


@tool(is_read_only=False)
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


# Snapshot the file-tool specs the @tool decorator built above so
# refresh_file_tools_registration() can re-add them after a pop without
# losing the introspected schema. Mirrors app/tools/rag.py.
from app.tools import TOOLS as _TOOLS  # noqa: E402

_FILE_TOOL_SPECS = {
    "read_file": _TOOLS["read_file"],
    "write_file": _TOOLS["write_file"],
}


def refresh_file_tools_registration() -> None:
    """Sync the file tools' registry presence to whether a root is configured.

    When ``FILE_TOOL_ROOT`` is unset, ``read_file`` / ``write_file`` are
    removed from ``app.tools.TOOLS`` so the chat model is never offered a
    tool with nowhere to operate. When it is set, both are (re-)added
    from the specs snapshotted at decoration time.

    Mirrors :func:`app.tools.rag.refresh_query_rag_registration`. Called
    at lifespan startup so the initial registry matches config; the root
    is static .env config, so there is no per-request refresh.
    """
    # Re-import locally so tests that patch app.tools.TOOLS see the right
    # object (same reasoning as refresh_query_rag_registration).
    from app.tools import TOOLS

    if file_tool_root() is None:
        TOOLS.pop("read_file", None)
        TOOLS.pop("write_file", None)
        return
    for name, spec in _FILE_TOOL_SPECS.items():
        if name not in TOOLS:
            TOOLS[name] = spec
