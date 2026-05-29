"""Tests for the phase-17 ContextVar-based workspace scoping of the file tools.

When ``current_workspace_root`` is set, ``read_file`` / ``write_file`` /
``list_directory`` / ``search_files`` resolve paths against THAT root —
not against ``FILE_TOOL_ROOT``. The fallback to ``FILE_TOOL_ROOT`` covers
tests + direct-invocation paths that don't bind a project.
"""

from pathlib import Path

import pytest

from app.projects import current_workspace_root
from app.tools import builtins as _builtins
from app.tools.builtins import (
    list_directory,
    read_file,
    refresh_file_tools_registration,
    search_files,
    write_file,
)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    """Create a per-test FILE_TOOL_ROOT with a project subdir inside.

    Returns ``(root, project_dir)`` so tests can write fixture files under
    either and exercise both the ContextVar path and the fallback path.
    """
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    # The file tools may have been popped by the autouse fixture in
    # conftest; re-register them now that FILE_TOOL_ROOT is set.
    refresh_file_tools_registration()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    yield tmp_path, project_dir


def test_read_file_uses_current_workspace_contextvar(workspace) -> None:
    """A file inside the ContextVar-bound root is reachable via read_file."""
    _root, project_dir = workspace
    (project_dir / "hello.txt").write_text("inside")
    token = current_workspace_root.set(project_dir)
    try:
        assert read_file("hello.txt") == "inside"
    finally:
        current_workspace_root.reset(token)


def test_read_file_rejects_path_outside_contextvar_root(workspace) -> None:
    """Even when FILE_TOOL_ROOT contains a parallel file at the same name,
    the ContextVar-bound root is the only one in scope."""
    root, project_dir = workspace
    # A file outside the project's bound root but inside FILE_TOOL_ROOT.
    (root / "stray.txt").write_text("outside")
    token = current_workspace_root.set(project_dir)
    try:
        # No file with this name inside project_dir — the tool says "No file".
        msg = read_file("stray.txt")
        assert "No file" in msg or "outside" in msg.lower()
    finally:
        current_workspace_root.reset(token)


def test_write_file_writes_into_contextvar_root(workspace) -> None:
    """Writes land under the ContextVar-bound root, not under FILE_TOOL_ROOT."""
    root, project_dir = workspace
    token = current_workspace_root.set(project_dir)
    try:
        write_file("note.md", "hi")
    finally:
        current_workspace_root.reset(token)
    # File appears under the project, NOT at the top of FILE_TOOL_ROOT.
    assert (project_dir / "note.md").read_text() == "hi"
    assert not (root / "note.md").exists()


def test_list_directory_under_contextvar_root(workspace) -> None:
    """list_directory only sees files inside the bound root."""
    root, project_dir = workspace
    (project_dir / "a.txt").write_text("a")
    (root / "outside.txt").write_text("hidden")
    token = current_workspace_root.set(project_dir)
    try:
        listing = list_directory(".")
    finally:
        current_workspace_root.reset(token)
    assert "a.txt" in listing
    assert "outside.txt" not in listing


def test_search_files_relative_to_contextvar_root(workspace) -> None:
    """search_files emits paths relative to the bound root."""
    _root, project_dir = workspace
    (project_dir / "match.md").write_text("x")
    token = current_workspace_root.set(project_dir)
    try:
        result = search_files("*.md", ".")
    finally:
        current_workspace_root.reset(token)
    assert "match.md" in result


def test_file_tools_fall_back_to_file_tool_root(workspace) -> None:
    """With no ContextVar set, the tools resolve against FILE_TOOL_ROOT."""
    root, _project_dir = workspace
    (root / "global.txt").write_text("global")
    # Sanity: ContextVar is unset.
    assert current_workspace_root.get() is None
    assert read_file("global.txt") == "global"


def test_resolve_outside_contextvar_root_rejects_traversal(workspace) -> None:
    """``..`` paths that escape the bound root are rejected."""
    _root, project_dir = workspace
    token = current_workspace_root.set(project_dir)
    try:
        # Should NOT raise — the tool catches _PathOutsideRoot and returns
        # an explanatory message.
        msg = read_file("../escape.txt")
    finally:
        current_workspace_root.reset(token)
    assert "outside" in msg.lower()


def test_read_file_strips_leading_slash(workspace) -> None:
    """Leading slashes are automatically stripped so models can use /path.

    Models often add a leading "/" even when the description says "relative
    path" (e.g., "/bc_undergrad_physics/file.md"). The tool normalizes this
    by stripping leading slashes before resolution, so the model can access
    subdirectories without hitting "outside workspace" errors.
    """
    _root, project_dir = workspace
    # Create a nested structure like "bc_undergrad_physics/bc_year4_physics.md"
    subdir = project_dir / "bc_undergrad_physics"
    subdir.mkdir()
    (subdir / "bc_year4_physics.md").write_text("Physics content")

    token = current_workspace_root.set(project_dir)
    try:
        # Model passes "/bc_undergrad_physics/bc_year4_physics.md" (with leading /)
        result = read_file("/bc_undergrad_physics/bc_year4_physics.md")
        # Should succeed — the leading slash is stripped
        assert result == "Physics content"

        # Also verify write_file works with leading slash
        write_file("/new_file.txt", "new content")
        assert (project_dir / "new_file.txt").read_text() == "new content"
    finally:
        current_workspace_root.reset(token)
