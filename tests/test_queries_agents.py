"""Tests for Phase 29 reusable-agent (persona) CRUD in :mod:`app.queries`.

The ``conn`` fixture mirrors ``test_queries_projects.py`` / ``test_rag_servers.py``:
a temp-dir-backed DB with the full schema applied and FK enforcement on (via
``open_connection``), which the ``ON DELETE SET NULL`` test relies on.
"""

import sqlite3
from pathlib import Path

import pytest

from app.connection import open_connection
from app.db import initialize_database
from app.queries import (
    _UNSET,
    SYSTEM_PROMPT_MAX_CHARS,
    Agent,
    create_agent,
    create_conversation,
    delete_agent,
    get_agent,
    get_agent_for_conversation,
    get_conversation,
    list_agents,
    set_conversation_agent,
    update_agent,
)


@pytest.fixture
def conn(tmp_path: Path):
    """Yield an open connection to a freshly-initialized DB."""
    db_path = tmp_path / "chats.db"
    initialize_database(db_path)
    with open_connection(db_path) as c:
        yield c


# ---------------------------------------------------------------------------
# create / get / list
# ---------------------------------------------------------------------------


def test_create_returns_populated_row(conn: sqlite3.Connection) -> None:
    """create_agent returns an Agent with id, fields, and UTC timestamps."""
    agent = create_agent(
        conn, "Researcher", system_prompt="Cite sources.", default_model="qwen3"
    )
    assert isinstance(agent, Agent)
    assert agent.id > 0
    assert agent.name == "Researcher"
    assert agent.system_prompt == "Cite sources."
    assert agent.default_model == "qwen3"
    assert agent.created_at.tzinfo is not None
    assert agent.updated_at == agent.created_at


def test_create_defaults(conn: sqlite3.Connection) -> None:
    """Omitting system_prompt / default_model stores '' / NULL."""
    agent = create_agent(conn, "Bare")
    assert agent.system_prompt == ""
    assert agent.default_model is None


def test_create_clamps_system_prompt(conn: sqlite3.Connection) -> None:
    """system_prompt is clamped to SYSTEM_PROMPT_MAX_CHARS defensively."""
    agent = create_agent(conn, "Long", system_prompt="x" * (SYSTEM_PROMPT_MAX_CHARS + 50))
    assert len(agent.system_prompt) == SYSTEM_PROMPT_MAX_CHARS


def test_create_rejects_duplicate_name(conn: sqlite3.Connection) -> None:
    """The UNIQUE on name surfaces as IntegrityError (route -> 409)."""
    create_agent(conn, "Dup")
    with pytest.raises(sqlite3.IntegrityError):
        create_agent(conn, "Dup")


def test_list_is_name_sorted_case_insensitive(conn: sqlite3.Connection) -> None:
    """list_agents returns rows by name, case-insensitively."""
    create_agent(conn, "beta")
    create_agent(conn, "Alpha")
    create_agent(conn, "gamma")
    assert [a.name for a in list_agents(conn)] == ["Alpha", "beta", "gamma"]


def test_get_agent_raises_when_absent(conn: sqlite3.Connection) -> None:
    """get_agent raises LookupError for an unknown id."""
    with pytest.raises(LookupError):
        get_agent(conn, 999)


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_update_partial_leaves_untouched_fields(conn: sqlite3.Connection) -> None:
    """A partial update changes only the passed fields, bumps updated_at."""
    a = create_agent(conn, "A", system_prompt="orig", default_model="m1")
    updated = update_agent(conn, a.id, system_prompt="new")
    assert updated.system_prompt == "new"
    assert updated.name == "A"            # untouched
    assert updated.default_model == "m1"  # untouched
    assert updated.updated_at >= a.updated_at
    # Durable.
    assert get_agent(conn, a.id).system_prompt == "new"


def test_update_default_model_none_clears(conn: sqlite3.Connection) -> None:
    """Passing default_model=None clears it (distinct from _UNSET = leave)."""
    a = create_agent(conn, "A", default_model="m1")
    cleared = update_agent(conn, a.id, default_model=None)
    assert cleared.default_model is None


def test_update_default_model_unset_leaves(conn: sqlite3.Connection) -> None:
    """Omitting default_model (the _UNSET default) leaves it alone."""
    a = create_agent(conn, "A", default_model="m1")
    same = update_agent(conn, a.id, name="A2")
    assert same.name == "A2"
    assert same.default_model == "m1"
    assert _UNSET is not None  # sentinel exists / imported


def test_update_no_kwargs_is_noop(conn: sqlite3.Connection) -> None:
    """update_agent with no fields returns the current row without bumping."""
    a = create_agent(conn, "A")
    same = update_agent(conn, a.id)
    assert same == a


def test_update_missing_id_raises(conn: sqlite3.Connection) -> None:
    """update_agent raises LookupError for an unknown id."""
    with pytest.raises(LookupError):
        update_agent(conn, 999, name="x")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_is_idempotent(conn: sqlite3.Connection) -> None:
    """delete_agent removes the row; re-deleting / unknown id is a no-op."""
    a = create_agent(conn, "A")
    delete_agent(conn, a.id)
    assert list_agents(conn) == []
    delete_agent(conn, a.id)
    delete_agent(conn, 999)


# ---------------------------------------------------------------------------
# get_agent_for_conversation + attach/detach + ON DELETE SET NULL
# ---------------------------------------------------------------------------


def test_get_agent_for_conversation_none_when_unattached(
    conn: sqlite3.Connection,
) -> None:
    """A chat with no agent returns None (not an error)."""
    chat = create_conversation(conn, "c", "llama3")
    assert chat.agent_id is None
    assert get_agent_for_conversation(conn, chat.id) is None


def test_get_agent_for_conversation_unknown_chat_is_none(
    conn: sqlite3.Connection,
) -> None:
    """An unknown conversation degrades to None rather than raising."""
    assert get_agent_for_conversation(conn, 999) is None


def test_set_conversation_agent_attaches_then_clears(
    conn: sqlite3.Connection,
) -> None:
    """set_conversation_agent attaches an agent, then clears back to Normal."""
    agent = create_agent(conn, "Persona", system_prompt="be terse")
    chat = create_conversation(conn, "c", "llama3")

    attached = set_conversation_agent(conn, chat.id, agent.id)
    assert attached.agent_id == agent.id
    fetched = get_agent_for_conversation(conn, chat.id)
    assert fetched is not None and fetched.id == agent.id

    cleared = set_conversation_agent(conn, chat.id, None)
    assert cleared.agent_id is None
    assert get_agent_for_conversation(conn, chat.id) is None


def test_set_conversation_agent_unknown_chat_raises(
    conn: sqlite3.Connection,
) -> None:
    """set_conversation_agent raises LookupError for an unknown chat."""
    with pytest.raises(LookupError):
        set_conversation_agent(conn, 999, None)


def test_delete_agent_sets_attached_chats_to_null(
    conn: sqlite3.Connection,
) -> None:
    """ON DELETE SET NULL: deleting an agent reverts its chats to Normal.

    Relies on PRAGMA foreign_keys=ON (open_connection sets it).
    """
    agent = create_agent(conn, "Doomed")
    chat = create_conversation(conn, "c", "llama3")
    set_conversation_agent(conn, chat.id, agent.id)

    delete_agent(conn, agent.id)

    assert get_conversation(conn, chat.id).agent_id is None
    assert get_agent_for_conversation(conn, chat.id) is None
