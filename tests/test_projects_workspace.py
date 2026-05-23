"""Tests for the phase-17 per-project workspace helpers.

Covers:
- :func:`app.projects.project_workspace_root` resolves to the right path
  (and returns None when FILE_TOOL_ROOT is unset).
- :func:`app.projects.ensure_project_workspace` materializes the dir.
- :func:`app.projects.migrate_legacy_workspace` moves top-level FILE_TOOL_ROOT
  entries into ``default/`` exactly once, sets the gate flag, and skips
  collisions without overwriting.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import queries
from app.connection import open_connection
from app.db import initialize_database
from app.projects import (
    ensure_project_workspace,
    migrate_legacy_workspace,
    project_workspace_root,
)


def _make_project(
    workspace_subdir: str = "default", *, project_id: int = 1
) -> queries.Project:
    """Build a Project dataclass in-memory (no DB needed)."""
    now = datetime.now(timezone.utc)
    return queries.Project(
        id=project_id,
        name="x",
        description="",
        workspace_subdir=workspace_subdir,
        default_model=None,
        default_agent=None,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# project_workspace_root / ensure_project_workspace
# ---------------------------------------------------------------------------


def test_project_workspace_root_returns_subdir_under_root(
    tmp_path: Path, monkeypatch
) -> None:
    """project_workspace_root returns FILE_TOOL_ROOT / workspace_subdir."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    p = _make_project("demo")
    expected = (tmp_path / "demo").resolve()
    assert project_workspace_root(p) == expected


def test_project_workspace_root_returns_none_when_root_unset(
    monkeypatch,
) -> None:
    """With FILE_TOOL_ROOT unset, the helper returns None (file tools off)."""
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    p = _make_project("anything")
    assert project_workspace_root(p) is None


def test_ensure_project_workspace_creates_dir(
    tmp_path: Path, monkeypatch
) -> None:
    """ensure_project_workspace materializes the directory if absent."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    p = _make_project("fresh")
    target = ensure_project_workspace(p)
    assert target is not None
    assert target.is_dir()


def test_ensure_project_workspace_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    """Second call when the dir already exists is a no-op."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    p = _make_project("repeat")
    ensure_project_workspace(p)
    # Second invocation must not raise.
    target = ensure_project_workspace(p)
    assert target is not None and target.is_dir()


def test_ensure_project_workspace_no_root_returns_none(monkeypatch) -> None:
    """With no FILE_TOOL_ROOT, the helper returns None and doesn't create."""
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    p = _make_project("x")
    assert ensure_project_workspace(p) is None


# ---------------------------------------------------------------------------
# migrate_legacy_workspace
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path):
    """Open connection to a freshly-initialized DB.

    The Default project (workspace_subdir = "default") exists from init.
    """
    db_path = tmp_path / "chats.db"
    initialize_database(db_path)
    with open_connection(db_path) as conn:
        yield conn


def test_migrate_moves_top_level_entries(
    tmp_path: Path, monkeypatch, db
) -> None:
    """Files at the FILE_TOOL_ROOT root land inside default/ after migration."""
    root = tmp_path / "workspace"
    root.mkdir()
    # Populate with a mix of files and a subdir.
    (root / "notes.md").write_text("hi")
    (root / "a-dir").mkdir()
    (root / "a-dir" / "inner.txt").write_text("inner")
    monkeypatch.setenv("FILE_TOOL_ROOT", str(root))

    migrate_legacy_workspace(db, queries)

    assert (root / "default").is_dir()
    assert (root / "default" / "notes.md").read_text() == "hi"
    assert (root / "default" / "a-dir" / "inner.txt").read_text() == "inner"
    # Originals should be gone from the top level.
    assert not (root / "notes.md").exists()
    assert not (root / "a-dir").exists()
    # Gate flag is set.
    assert queries.get_setting(db, "workspace_v2_migrated") == "1"


def test_migrate_is_idempotent(
    tmp_path: Path, monkeypatch, db
) -> None:
    """Second migrate call is a no-op (gated by the flag)."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "first.md").write_text("first")
    monkeypatch.setenv("FILE_TOOL_ROOT", str(root))

    migrate_legacy_workspace(db, queries)
    # Now drop a NEW top-level file. A second migrate must NOT touch it,
    # because the flag is already set.
    (root / "after-flag.md").write_text("after")

    migrate_legacy_workspace(db, queries)

    # The post-flag file should still be at the top level.
    assert (root / "after-flag.md").exists()
    # And not duplicated into default/.
    assert not (root / "default" / "after-flag.md").exists()


def test_migrate_skips_collisions_without_overwrite(
    tmp_path: Path, monkeypatch, db
) -> None:
    """If a name already exists in default/, the migration skips and warns."""
    root = tmp_path / "workspace"
    (root / "default").mkdir(parents=True)
    # Existing file in default/.
    (root / "default" / "clash.md").write_text("kept")
    # Conflicting top-level file with the SAME name.
    (root / "clash.md").write_text("would clobber")
    monkeypatch.setenv("FILE_TOOL_ROOT", str(root))

    migrate_legacy_workspace(db, queries)

    # The original default/clash.md content is preserved (not overwritten).
    assert (root / "default" / "clash.md").read_text() == "kept"
    # And the top-level file is still there (skipped, not moved).
    assert (root / "clash.md").read_text() == "would clobber"


def test_migrate_noop_when_root_unset(monkeypatch, db) -> None:
    """With FILE_TOOL_ROOT unset, the migration just sets the flag and returns."""
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    migrate_legacy_workspace(db, queries)
    assert queries.get_setting(db, "workspace_v2_migrated") == "1"


def test_migrate_preserves_other_projects_dirs(
    tmp_path: Path, monkeypatch, db
) -> None:
    """A non-default project's workspace_subdir is never moved into default/."""
    root = tmp_path / "workspace"
    root.mkdir()
    # Make a project that owns a top-level subdir.
    queries.create_project(db, name="Reserved", description="")  # slug=reserved
    (root / "reserved").mkdir()
    (root / "reserved" / "keep.md").write_text("mine")
    (root / "loose.md").write_text("moves")
    monkeypatch.setenv("FILE_TOOL_ROOT", str(root))

    migrate_legacy_workspace(db, queries)

    # reserved/ stays put — its workspace_subdir is registered.
    assert (root / "reserved" / "keep.md").read_text() == "mine"
    # loose.md moves into default/.
    assert (root / "default" / "loose.md").read_text() == "moves"
