"""Tests for Phase 17 project CRUD in :mod:`app.queries`.

The Default project is created at ``initialize_database`` time, so every
test here starts with at least one project already present. The
``initialized_db`` fixture re-uses the same shape as ``test_db.py``: a
temp-dir-backed DB whose schema is fully applied.
"""

import sqlite3
from pathlib import Path

import pytest

from app.connection import open_connection
from app.db import initialize_database
from app.queries import (
    _UNSET,
    Project,
    count_projects,
    create_conversation,
    create_project,
    delete_project,
    get_project,
    get_project_for_conversation,
    list_projects,
    slugify_project_name,
    update_project,
)


@pytest.fixture
def conn(tmp_path: Path):
    """Yield an open connection to a freshly-initialized DB."""
    db_path = tmp_path / "chats.db"
    initialize_database(db_path)
    with open_connection(db_path) as c:
        yield c


# ---------------------------------------------------------------------------
# slugify_project_name
# ---------------------------------------------------------------------------


def test_slugify_basic() -> None:
    """Simple ASCII names slugify to a lowercased, hyphen-joined slug."""
    assert slugify_project_name("My Project") == "my-project"


def test_slugify_strips_punctuation() -> None:
    """Runs of non-alphanumerics collapse to a single hyphen, edges stripped."""
    assert slugify_project_name("  Hello !! World  ") == "hello-world"


def test_slugify_empty_falls_back_to_project() -> None:
    """An empty/all-punct name slugs to the literal ``"project"`` fallback."""
    assert slugify_project_name("") == "project"
    assert slugify_project_name("!!!") == "project"


def test_slugify_caps_at_60_chars() -> None:
    """Slugs are capped at 60 characters to keep path segments short."""
    long_name = "x" * 200
    assert len(slugify_project_name(long_name)) == 60


# ---------------------------------------------------------------------------
# create_project + list / get
# ---------------------------------------------------------------------------


def test_create_project_inserts_row(conn) -> None:
    """``create_project`` returns a populated Project and persists the row."""
    p = create_project(conn, name="Demo", description="hi")
    assert isinstance(p, Project)
    assert p.name == "Demo"
    assert p.description == "hi"
    assert p.workspace_subdir == "demo"
    # Confirm it round-trips through list_projects.
    names = {row.name for row in list_projects(conn)}
    assert "Demo" in names


def test_create_project_slugifies_subdir(conn) -> None:
    """The subdir is derived from the name via slugify_project_name."""
    p = create_project(conn, name="Cool Stuff!")
    assert p.workspace_subdir == "cool-stuff"


def test_create_project_handles_subdir_collision(conn) -> None:
    """Two projects whose names normalize to the same slug get -2, -3 suffixes."""
    # The Default project already owns "default" — pick a name that would
    # collide with it after slugification.
    p1 = create_project(conn, name="my proj")
    p2 = create_project(conn, name="My Proj")
    # Names must differ (UNIQUE constraint); workspace_subdir differs via
    # the -N suffix.
    assert p1.workspace_subdir == "my-proj"
    assert p2.workspace_subdir == "my-proj-2"


def test_create_project_name_uniqueness_violation(conn) -> None:
    """A second create with the same name raises IntegrityError (UNIQUE)."""
    create_project(conn, name="Dup")
    with pytest.raises(sqlite3.IntegrityError):
        create_project(conn, name="Dup")


def test_list_projects_alpha_order(conn) -> None:
    """list_projects returns projects ordered by name (case-insensitive)."""
    create_project(conn, name="charlie")
    create_project(conn, name="Alpha")
    create_project(conn, name="bravo")
    names = [p.name for p in list_projects(conn)]
    # Default + the three new ones, alphabetized.
    assert names == ["Alpha", "bravo", "charlie", "Default"]


def test_get_project_returns_row(conn) -> None:
    """get_project finds a project by id."""
    p = create_project(conn, name="Findable")
    looked_up = get_project(conn, p.id)
    assert looked_up.name == "Findable"


def test_get_project_raises_on_missing(conn) -> None:
    """get_project raises LookupError for an unknown id."""
    with pytest.raises(LookupError):
        get_project(conn, 99_999)


def test_count_projects(conn) -> None:
    """count_projects reflects the current row count (Default + created)."""
    assert count_projects(conn) == 1
    create_project(conn, name="X")
    assert count_projects(conn) == 2


def test_get_project_for_conversation_returns_owning_project(conn) -> None:
    """get_project_for_conversation joins conversations to projects."""
    p = create_project(conn, name="Owner")
    chat = create_conversation(conn, name="c", model="m", project_id=p.id)
    looked_up = get_project_for_conversation(conn, chat.id)
    assert looked_up.id == p.id


def test_get_project_for_conversation_missing_chat_raises(conn) -> None:
    """get_project_for_conversation raises LookupError on an unknown chat."""
    with pytest.raises(LookupError):
        get_project_for_conversation(conn, 99_999)


# ---------------------------------------------------------------------------
# update_project + sentinel semantics
# ---------------------------------------------------------------------------


def test_update_project_name_and_description(conn) -> None:
    """Plain name + description updates land on the row."""
    p = create_project(conn, name="Old")
    updated = update_project(conn, p.id, name="New", description="d")
    assert updated.name == "New"
    assert updated.description == "d"


def test_update_project_clears_default_model_with_none(conn) -> None:
    """Passing default_model=None CLEARS the field (the sentinel default leaves alone)."""
    p = create_project(conn, name="P", default_model="llama3")
    cleared = update_project(conn, p.id, default_model=None)
    assert cleared.default_model is None


def test_update_project_leaves_default_model_alone_when_unpassed(conn) -> None:
    """Omitting default_model preserves the existing value (sentinel path)."""
    p = create_project(conn, name="P", default_model="llama3")
    # Only updating the name should leave default_model untouched.
    updated = update_project(conn, p.id, name="Renamed")
    assert updated.default_model == "llama3"


def test_update_project_clears_default_agent_with_none(conn) -> None:
    """Passing default_agent=None CLEARS the field."""
    p = create_project(conn, name="P", default_agent="research")
    cleared = update_project(conn, p.id, default_agent=None)
    assert cleared.default_agent is None


def test_create_project_seeds_num_ctx_as_none(conn) -> None:
    """A newly-created project's num_ctx is NULL (inherit global)."""
    p = create_project(conn, name="Pfresh")
    assert p.num_ctx is None


def test_update_project_sets_num_ctx(conn) -> None:
    """Passing num_ctx sets the per-project override and clamps it."""
    p = create_project(conn, name="Pnum")
    updated = update_project(conn, p.id, num_ctx=32768)
    assert updated.num_ctx == 32768


def test_update_project_clears_num_ctx_with_none(conn) -> None:
    """Passing num_ctx=None CLEARS the override (inherit global again)."""
    p = create_project(conn, name="Pclear", default_model="llama3")
    update_project(conn, p.id, num_ctx=16384)
    cleared = update_project(conn, p.id, num_ctx=None)
    assert cleared.num_ctx is None


def test_update_project_clamps_num_ctx(conn) -> None:
    """An out-of-range num_ctx is clamped before storage."""
    from app.queries import NUM_CTX_MAX

    p = create_project(conn, name="Pclamp")
    updated = update_project(conn, p.id, num_ctx=10_000_000)
    assert updated.num_ctx == NUM_CTX_MAX


def test_update_project_leaves_num_ctx_alone_when_unpassed(conn) -> None:
    """Omitting num_ctx preserves the existing value (sentinel path)."""
    p = create_project(conn, name="Punset")
    update_project(conn, p.id, num_ctx=24000)
    renamed = update_project(conn, p.id, name="Renamed2")
    assert renamed.num_ctx == 24000


def test_update_project_no_kwargs_is_noop(conn) -> None:
    """Calling update_project with nothing returns the unchanged Project.

    Importantly, this must NOT bump ``updated_at`` (no-op updates shouldn't
    falsely advertise a change).
    """
    p = create_project(conn, name="Stable")
    same = update_project(conn, p.id)
    assert same.id == p.id
    assert same.updated_at == p.updated_at


def test_update_project_missing_raises(conn) -> None:
    """Updating a non-existent project raises LookupError."""
    with pytest.raises(LookupError):
        update_project(conn, 99_999, name="x")


# ---------------------------------------------------------------------------
# delete_project + cascade
# ---------------------------------------------------------------------------


def test_delete_project_cascades_to_conversations(conn) -> None:
    """Deleting a project removes its chats via FK ON DELETE CASCADE."""
    p = create_project(conn, name="Doomed")
    chat = create_conversation(conn, name="c", model="m", project_id=p.id)
    delete_project(conn, p.id)
    # Chat should be gone too.
    row = conn.execute(
        "SELECT id FROM conversations WHERE id = ?;", (chat.id,)
    ).fetchone()
    assert row is None


def test_delete_project_missing_is_noop(conn) -> None:
    """Deleting a non-existent project does NOT raise (mirrors delete_conversation)."""
    delete_project(conn, 99_999)  # Should not raise.


# ---------------------------------------------------------------------------
# _UNSET sentinel
# ---------------------------------------------------------------------------


def test_unset_sentinel_is_distinguishable_from_none() -> None:
    """_UNSET is a sentinel singleton, not equal to None."""
    assert _UNSET is not None
    assert _UNSET is _UNSET
