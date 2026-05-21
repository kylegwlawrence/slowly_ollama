"""Tests for Phase 4: dataclasses and query functions.

Each test gets a fresh, schema-initialized SQLite DB at `tmp_path/chats.db`
via the `conn` fixture. Tests then exercise the public query functions and
read back results to verify behavior.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from app.connection import open_connection
from app.db import initialize_database
from app.queries import (
    ChatRagState,
    ChatToolState,
    append_message,
    count_assistant_messages,
    create_conversation,
    delete_conversation,
    get_agentic_mode,
    get_chat_rag_states,
    get_chat_tool_states,
    get_conversation,
    get_enabled_rag_server_names,
    get_enabled_tool_names,
    get_generator_enabled,
    get_review_enabled,
    get_setting,
    list_conversations,
    list_messages,
    rename_conversation,
    replace_last_assistant_message,
    seed_chat_rag_servers,
    seed_chat_tools,
    set_agentic_mode,
    set_generator_enabled,
    set_name_auto,
    set_review_enabled,
    set_setting,
    toggle_chat_rag_server,
    toggle_chat_tool,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection to a freshly-initialized DB in tmp_path.

    Yields:
        An open `sqlite3.Connection` with the Phase 2 schema applied and
        the Phase 3 pragmas/row-factory in place. The connection is
        closed (via the `with` block's exit + GC) after the test.
    """
    db_path = tmp_path / "chats.db"
    initialize_database(db_path)
    with open_connection(db_path) as connection:
        yield connection


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


def test_create_conversation_returns_populated_row(
    conn: sqlite3.Connection,
) -> None:
    """create_conversation returns the row with id and UTC timestamps set."""
    c = create_conversation(conn, name="My chat", model="llama3")

    assert c.id > 0
    assert c.name == "My chat"
    assert c.model == "llama3"
    assert isinstance(c.created_at, datetime)
    # The DB stores ISO 8601 UTC; the parsed value must round-trip with a
    # timezone attached (naive datetimes here would mean we lost the UTC
    # marker somewhere in the read path).
    assert c.created_at.tzinfo is not None
    assert c.updated_at == c.created_at


def test_get_conversation_returns_the_row(conn: sqlite3.Connection) -> None:
    """get_conversation returns the Conversation for a known id."""
    c = create_conversation(conn, "X", "llama3")

    fetched = get_conversation(conn, c.id)

    assert fetched.id == c.id
    assert fetched.name == "X"
    assert fetched.model == "llama3"


def test_get_conversation_raises_for_unknown_id(
    conn: sqlite3.Connection,
) -> None:
    """get_conversation raises LookupError when no row matches."""
    with pytest.raises(LookupError):
        get_conversation(conn, 999)


def test_list_conversations_orders_most_recently_updated_first(
    conn: sqlite3.Connection,
) -> None:
    """The sidebar order: most-recently-updated conversation on top."""
    first = create_conversation(conn, "First", "llama3")
    second = create_conversation(conn, "Second", "llama3")

    # `second` was created after `first`, so its updated_at is newer; the
    # tiebreaker `id DESC` also puts the newer row first when timestamps
    # collide at sub-microsecond.
    result = list_conversations(conn)

    assert [c.id for c in result] == [second.id, first.id]


def test_rename_conversation_updates_name_and_bumps_updated_at(
    conn: sqlite3.Connection,
) -> None:
    """Rename writes the new name and advances updated_at."""
    c = create_conversation(conn, "Old", "llama3")

    renamed = rename_conversation(conn, c.id, "New")

    assert renamed.id == c.id
    assert renamed.name == "New"
    assert renamed.updated_at >= c.updated_at


def test_rename_conversation_raises_for_unknown_id(
    conn: sqlite3.Connection,
) -> None:
    """Renaming an id that doesn't exist raises LookupError."""
    with pytest.raises(LookupError):
        rename_conversation(conn, 999, "Nope")


def test_delete_conversation_cascades_to_messages(
    conn: sqlite3.Connection,
) -> None:
    """Deleting a conversation removes its messages via ON DELETE CASCADE."""
    c = create_conversation(conn, "X", "llama3")
    append_message(conn, c.id, "user", "Hello")

    delete_conversation(conn, c.id)

    assert list_conversations(conn) == []
    assert list_messages(conn, c.id) == []


def test_delete_conversation_is_idempotent(conn: sqlite3.Connection) -> None:
    """Deleting an id that doesn't exist is a silent no-op."""
    # Must not raise — the UI button might be clicked on an already-gone
    # conversation in race-y cases.
    delete_conversation(conn, 999)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def test_append_message_returns_populated_row(
    conn: sqlite3.Connection,
) -> None:
    """append_message returns the saved message with id and timestamp."""
    c = create_conversation(conn, "X", "llama3")

    m = append_message(conn, c.id, "user", "Hello")

    assert m.id > 0
    assert m.conversation_id == c.id
    assert m.role == "user"
    assert m.content == "Hello"
    assert m.created_at.tzinfo is not None


def test_append_message_bumps_parent_updated_at(
    conn: sqlite3.Connection,
) -> None:
    """Appending a message advances the parent conversation's updated_at."""
    c = create_conversation(conn, "X", "llama3")
    original_updated = c.updated_at

    append_message(conn, c.id, "user", "Hi")

    refreshed = next(x for x in list_conversations(conn) if x.id == c.id)
    assert refreshed.updated_at >= original_updated


# Note: a previous test (`test_append_message_rejects_invalid_role`)
# verified that the schema CHECK rejected unknown roles. Phase 12a
# dropped that CHECK — validation now lives entirely in the Python
# `Role` literal, which is a type-checker concern, not a runtime one.
# There's nothing left for pytest to assert here, so the test was
# removed.


def test_list_messages_returns_chronological_order(
    conn: sqlite3.Connection,
) -> None:
    """Messages list oldest-first so the chat reads top-to-bottom."""
    c = create_conversation(conn, "X", "llama3")
    first = append_message(conn, c.id, "user", "First")
    second = append_message(conn, c.id, "assistant", "Second")
    third = append_message(conn, c.id, "user", "Third")

    msgs = list_messages(conn, c.id)

    assert [m.id for m in msgs] == [first.id, second.id, third.id]


# ---------------------------------------------------------------------------
# Regenerate (replace last assistant message)
# ---------------------------------------------------------------------------


def test_replace_last_assistant_message_updates_in_place(
    conn: sqlite3.Connection,
) -> None:
    """Replace keeps the same id and created_at; only content changes."""
    c = create_conversation(conn, "X", "llama3")
    append_message(conn, c.id, "user", "Q")
    original = append_message(conn, c.id, "assistant", "Old answer")

    replaced = replace_last_assistant_message(conn, c.id, "New answer")

    assert replaced.id == original.id
    assert replaced.content == "New answer"
    # created_at preserved so the regenerated message stays in position
    # when the conversation is re-listed.
    assert replaced.created_at == original.created_at


def test_replace_last_assistant_message_targets_the_last_one(
    conn: sqlite3.Connection,
) -> None:
    """When multiple assistant messages exist, only the latest is replaced."""
    c = create_conversation(conn, "X", "llama3")
    append_message(conn, c.id, "user", "Q1")
    append_message(conn, c.id, "assistant", "A1")
    append_message(conn, c.id, "user", "Q2")
    append_message(conn, c.id, "assistant", "A2")

    replace_last_assistant_message(conn, c.id, "A2 regenerated")

    msgs = list_messages(conn, c.id)
    # Order: u(Q1), a(A1), u(Q2), a(A2 regenerated)
    assert msgs[1].content == "A1"
    assert msgs[3].content == "A2 regenerated"


def test_replace_last_assistant_message_raises_when_no_assistant_yet(
    conn: sqlite3.Connection,
) -> None:
    """Regenerate without any assistant message in the conversation raises."""
    c = create_conversation(conn, "X", "llama3")
    append_message(conn, c.id, "user", "Q")

    with pytest.raises(LookupError):
        replace_last_assistant_message(conn, c.id, "Anything")


# ---------------------------------------------------------------------------
# Phase 11d: name_locked + auto-title helpers
# ---------------------------------------------------------------------------


def test_create_conversation_starts_unlocked(
    conn: sqlite3.Connection,
) -> None:
    """New chats are unlocked so the auto-titler can refresh the name."""
    c = create_conversation(conn, "New chat", "llama3")
    assert c.name_locked is False


def test_rename_conversation_locks_the_name(
    conn: sqlite3.Connection,
) -> None:
    """Manual rename flips name_locked to True so future auto-runs skip."""
    c = create_conversation(conn, "New chat", "llama3")
    assert c.name_locked is False

    renamed = rename_conversation(conn, c.id, "Renamed")

    assert renamed.name_locked is True
    # And the lock persists across reads — not just the returned row.
    assert get_conversation(conn, c.id).name_locked is True


def test_set_name_auto_updates_unlocked_chat(
    conn: sqlite3.Connection,
) -> None:
    """An unlocked chat takes the auto-generated name."""
    c = create_conversation(conn, "New chat", "llama3")

    updated = set_name_auto(conn, c.id, "Auto Title")

    assert updated is not None
    assert updated.name == "Auto Title"
    assert updated.name_locked is False  # auto-title does NOT lock
    assert get_conversation(conn, c.id).name == "Auto Title"


def test_set_name_auto_respects_manual_lock(
    conn: sqlite3.Connection,
) -> None:
    """After a manual rename, auto-title attempts must no-op."""
    c = create_conversation(conn, "New chat", "llama3")
    rename_conversation(conn, c.id, "I Chose This Name")

    result = set_name_auto(conn, c.id, "Auto wants to overwrite")

    # None signals "nothing changed"; the row's name should be intact.
    assert result is None
    assert get_conversation(conn, c.id).name == "I Chose This Name"


def test_set_name_auto_returns_none_for_unknown_id(
    conn: sqlite3.Connection,
) -> None:
    """A missing id is treated the same as a locked one — no error, no write."""
    assert set_name_auto(conn, 999, "ignored") is None


def test_count_assistant_messages_steps_per_reply(
    conn: sqlite3.Connection,
) -> None:
    """Count rises by 1 per assistant append; user messages don't move it."""
    c = create_conversation(conn, "New chat", "llama3")
    assert count_assistant_messages(conn, c.id) == 0

    append_message(conn, c.id, "user", "Q1")
    assert count_assistant_messages(conn, c.id) == 0

    append_message(conn, c.id, "assistant", "A1")
    assert count_assistant_messages(conn, c.id) == 1

    append_message(conn, c.id, "user", "Q2")
    append_message(conn, c.id, "assistant", "A2")
    assert count_assistant_messages(conn, c.id) == 2


def test_count_assistant_messages_zero_for_unknown_id(
    conn: sqlite3.Connection,
) -> None:
    """An id with no rows returns 0 cleanly — no LookupError."""
    assert count_assistant_messages(conn, 999) == 0


# ---------------------------------------------------------------------------
# Phase 13a: app_settings helpers
# ---------------------------------------------------------------------------


def test_get_setting_returns_default_when_missing(
    conn: sqlite3.Connection,
) -> None:
    """Unset keys come back as the supplied default — or None."""
    assert (
        get_setting(conn, "nonexistent", default="fallback") == "fallback"
    )
    assert get_setting(conn, "nonexistent") is None


def test_set_setting_upserts(conn: sqlite3.Connection) -> None:
    """Repeated set_setting calls overwrite the previous value
    (ON CONFLICT(key) DO UPDATE)."""
    set_setting(conn, "k", "v1")
    assert get_setting(conn, "k") == "v1"
    set_setting(conn, "k", "v2")
    assert get_setting(conn, "k") == "v2"


def test_agentic_mode_default_off(conn: sqlite3.Connection) -> None:
    """No row → agentic mode is off. Production default before the
    user touches /settings."""
    assert get_agentic_mode(conn) is False


def test_agentic_mode_round_trip(conn: sqlite3.Connection) -> None:
    """Toggle on, toggle off, both observable on subsequent reads."""
    set_agentic_mode(conn, True)
    assert get_agentic_mode(conn) is True
    set_agentic_mode(conn, False)
    assert get_agentic_mode(conn) is False


def test_agentic_mode_treats_non_on_values_as_off(
    conn: sqlite3.Connection,
) -> None:
    """Defensive: a row whose value isn't literally "on" reads False.
    Guards against legacy or hand-edited DBs that wrote something other
    than the two values set_agentic_mode produces. Case-sensitive: the
    comparison is against lowercase "on" exactly."""
    set_setting(conn, "agentic_mode", "yes")
    assert get_agentic_mode(conn) is False
    set_setting(conn, "agentic_mode", "")
    assert get_agentic_mode(conn) is False
    set_setting(conn, "agentic_mode", "ON")  # uppercase
    assert get_agentic_mode(conn) is False
    set_setting(conn, "agentic_mode", "On")  # title-case
    assert get_agentic_mode(conn) is False


def test_set_agentic_mode_rejects_non_bool(conn: sqlite3.Connection) -> None:
    """`set_agentic_mode("off")` would silently write "on" (truthy
    non-empty string). Guard against the foot-gun with a TypeError."""
    with pytest.raises(TypeError):
        set_agentic_mode(conn, "off")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        set_agentic_mode(conn, 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        set_agentic_mode(conn, None)  # type: ignore[arg-type]
    # State unchanged — no row was written by any of the failed calls.
    assert get_agentic_mode(conn) is False


# ---------------------------------------------------------------------------
# Phase 14: review_enabled / generator_enabled helpers
# ---------------------------------------------------------------------------


def test_get_review_enabled_default_true(conn: sqlite3.Connection) -> None:
    """Absent row → True. Reviewer is on by default so enabling agentic
    mode for the first time keeps Phase 13's full-loop behavior."""
    assert get_review_enabled(conn) is True


def test_set_review_enabled_roundtrip(conn: sqlite3.Connection) -> None:
    """Toggle off then back on; both reads reflect the new state."""
    set_review_enabled(conn, False)
    assert get_review_enabled(conn) is False
    set_review_enabled(conn, True)
    assert get_review_enabled(conn) is True


def test_set_review_enabled_rejects_non_bool(conn: sqlite3.Connection) -> None:
    """String "off" is truthy and would write "on"; guard with TypeError."""
    with pytest.raises(TypeError):
        set_review_enabled(conn, "off")  # type: ignore[arg-type]
    # State unchanged.
    assert get_review_enabled(conn) is True


def test_get_generator_enabled_default_true(conn: sqlite3.Connection) -> None:
    """Absent row → True. Same first-time-experience rationale as
    review_enabled."""
    assert get_generator_enabled(conn) is True


def test_set_generator_enabled_roundtrip(conn: sqlite3.Connection) -> None:
    """Toggle off then back on."""
    set_generator_enabled(conn, False)
    assert get_generator_enabled(conn) is False
    set_generator_enabled(conn, True)
    assert get_generator_enabled(conn) is True


def test_set_generator_enabled_rejects_non_bool(
    conn: sqlite3.Connection,
) -> None:
    """Guard against the truthy-string foot-gun."""
    with pytest.raises(TypeError):
        set_generator_enabled(conn, "off")  # type: ignore[arg-type]
    assert get_generator_enabled(conn) is True


# ---------------------------------------------------------------------------
# Phase 15: per-chat tool enablement
# ---------------------------------------------------------------------------


def _make_chat(conn: sqlite3.Connection) -> int:
    """Create a minimal conversation and return its id."""
    chat = create_conversation(conn, name="t", model="llama3")
    return chat.id


def test_seed_chat_tools_all_enabled_by_default(
    conn: sqlite3.Connection,
) -> None:
    """seed_chat_tools with enabled_names=None enables every tool."""
    cid = _make_chat(conn)
    seed_chat_tools(conn, cid, ["current_time", "query_rag"])
    states = get_chat_tool_states(conn, cid, ["current_time", "query_rag"])
    assert all(s.enabled for s in states)


def test_seed_chat_tools_partial_enabled(conn: sqlite3.Connection) -> None:
    """seed_chat_tools with enabled_names only enables the named subset."""
    cid = _make_chat(conn)
    seed_chat_tools(
        conn, cid, ["current_time", "query_rag"],
        enabled_names={"current_time"},
    )
    states = {s.tool_name: s.enabled
              for s in get_chat_tool_states(conn, cid, ["current_time", "query_rag"])}
    assert states["current_time"] is True
    assert states["query_rag"] is False


def test_seed_chat_tools_is_idempotent(conn: sqlite3.Connection) -> None:
    """Calling seed_chat_tools twice doesn't flip or duplicate rows."""
    cid = _make_chat(conn)
    seed_chat_tools(conn, cid, ["current_time"], enabled_names={"current_time"})
    seed_chat_tools(conn, cid, ["current_time"], enabled_names=set())
    # Second call uses INSERT OR IGNORE — first write wins.
    states = get_chat_tool_states(conn, cid, ["current_time"])
    assert states[0].enabled is True


def test_get_chat_tool_states_unseeded_defaults_to_enabled(
    conn: sqlite3.Connection,
) -> None:
    """Conversations with no rows are treated as all-tools-on."""
    cid = _make_chat(conn)
    states = get_chat_tool_states(conn, cid, ["current_time", "query_rag"])
    assert all(s.enabled for s in states)
    assert [s.tool_name for s in states] == ["current_time", "query_rag"]


def test_get_chat_tool_states_respects_seeded_rows(
    conn: sqlite3.Connection,
) -> None:
    """Seeded disabled row is reflected correctly."""
    cid = _make_chat(conn)
    seed_chat_tools(conn, cid, ["current_time", "query_rag"],
                    enabled_names=set())
    states = {s.tool_name: s.enabled
              for s in get_chat_tool_states(conn, cid, ["current_time", "query_rag"])}
    assert states["current_time"] is False
    assert states["query_rag"] is False


def test_toggle_chat_tool_off_from_unseeded(conn: sqlite3.Connection) -> None:
    """First toggle on an unseeded tool turns it off (implicit on → off)."""
    cid = _make_chat(conn)
    result = toggle_chat_tool(conn, cid, "current_time")
    assert result is False
    states = get_chat_tool_states(conn, cid, ["current_time"])
    assert states[0].enabled is False


def test_toggle_chat_tool_on_from_off(conn: sqlite3.Connection) -> None:
    """Second toggle flips back to on."""
    cid = _make_chat(conn)
    toggle_chat_tool(conn, cid, "current_time")   # off
    result = toggle_chat_tool(conn, cid, "current_time")  # on
    assert result is True


def test_get_enabled_tool_names_returns_subset(
    conn: sqlite3.Connection,
) -> None:
    """Only enabled tool names are returned."""
    cid = _make_chat(conn)
    seed_chat_tools(conn, cid, ["current_time", "query_rag"],
                    enabled_names={"current_time"})
    enabled = get_enabled_tool_names(conn, cid, ["current_time", "query_rag"])
    assert enabled == ["current_time"]


def test_get_enabled_tool_names_unseeded_returns_all(
    conn: sqlite3.Connection,
) -> None:
    """Unseeded conversation: all names returned (missing row = enabled)."""
    cid = _make_chat(conn)
    enabled = get_enabled_tool_names(conn, cid, ["current_time", "query_rag"])
    assert enabled == ["current_time", "query_rag"]


def test_chat_tool_state_dataclass(conn: sqlite3.Connection) -> None:
    """ChatToolState is frozen and carries the right fields."""
    state = ChatToolState(tool_name="current_time", enabled=True)
    assert state.tool_name == "current_time"
    assert state.enabled is True
    with pytest.raises(Exception):
        state.enabled = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Phase 15b: per-chat RAG server enablement
# ---------------------------------------------------------------------------


def test_seed_chat_rag_servers_all_enabled_by_default(
    conn: sqlite3.Connection,
) -> None:
    """seed_chat_rag_servers with enabled_names=None enables every server."""
    cid = _make_chat(conn)
    seed_chat_rag_servers(conn, cid, ["arxiv", "pubmed"])
    states = get_chat_rag_states(conn, cid, ["arxiv", "pubmed"])
    assert {s.server_name: s.enabled for s in states} == {"arxiv": True, "pubmed": True}


def test_seed_chat_rag_servers_partial_enabled(conn: sqlite3.Connection) -> None:
    """seed_chat_rag_servers with enabled_names only enables the named subset."""
    cid = _make_chat(conn)
    seed_chat_rag_servers(conn, cid, ["arxiv", "pubmed"], enabled_names={"arxiv"})
    states = get_chat_rag_states(conn, cid, ["arxiv", "pubmed"])
    assert {s.server_name: s.enabled for s in states} == {"arxiv": True, "pubmed": False}


def test_seed_chat_rag_servers_is_idempotent(conn: sqlite3.Connection) -> None:
    """Calling seed_chat_rag_servers twice doesn't flip rows already written."""
    cid = _make_chat(conn)
    seed_chat_rag_servers(conn, cid, ["arxiv"], enabled_names={"arxiv"})
    seed_chat_rag_servers(conn, cid, ["arxiv"], enabled_names=set())
    states = get_chat_rag_states(conn, cid, ["arxiv"])
    # First seed wins; second call is a no-op (INSERT OR IGNORE).
    assert states[0].enabled is True


def test_get_chat_rag_states_unseeded_defaults_to_enabled(
    conn: sqlite3.Connection,
) -> None:
    """Unseeded conversation: all servers default to enabled."""
    cid = _make_chat(conn)
    states = get_chat_rag_states(conn, cid, ["arxiv", "pubmed"])
    assert all(s.enabled for s in states)


def test_get_chat_rag_states_respects_seeded_rows(
    conn: sqlite3.Connection,
) -> None:
    """Seeded disabled row is returned as disabled."""
    cid = _make_chat(conn)
    seed_chat_rag_servers(conn, cid, ["arxiv", "pubmed"], enabled_names={"pubmed"})
    states = {s.server_name: s.enabled for s in get_chat_rag_states(conn, cid, ["arxiv", "pubmed"])}
    assert states == {"arxiv": False, "pubmed": True}


def test_toggle_chat_rag_server_off_from_unseeded(conn: sqlite3.Connection) -> None:
    """First toggle on an unseeded row inserts disabled (on → off)."""
    cid = _make_chat(conn)
    result = toggle_chat_rag_server(conn, cid, "arxiv")
    assert result is False
    states = get_chat_rag_states(conn, cid, ["arxiv"])
    assert states[0].enabled is False


def test_toggle_chat_rag_server_on_from_off(conn: sqlite3.Connection) -> None:
    """Second toggle flips back to enabled (off → on)."""
    cid = _make_chat(conn)
    toggle_chat_rag_server(conn, cid, "arxiv")  # off
    result = toggle_chat_rag_server(conn, cid, "arxiv")  # on
    assert result is True


def test_get_enabled_rag_server_names_unseeded(conn: sqlite3.Connection) -> None:
    """Unseeded conversation: all names returned (missing row = enabled)."""
    cid = _make_chat(conn)
    enabled = get_enabled_rag_server_names(conn, cid, ["arxiv", "pubmed"])
    assert enabled == ["arxiv", "pubmed"]


def test_get_enabled_rag_server_names_filtered(conn: sqlite3.Connection) -> None:
    """Only enabled servers are returned when some are toggled off."""
    cid = _make_chat(conn)
    seed_chat_rag_servers(conn, cid, ["arxiv", "pubmed"], enabled_names={"pubmed"})
    enabled = get_enabled_rag_server_names(conn, cid, ["arxiv", "pubmed"])
    assert enabled == ["pubmed"]


def test_chat_rag_state_dataclass(conn: sqlite3.Connection) -> None:
    """ChatRagState is frozen and carries the right fields."""
    state = ChatRagState(server_name="arxiv", enabled=True)
    assert state.server_name == "arxiv"
    assert state.enabled is True
    with pytest.raises(Exception):
        state.enabled = False  # type: ignore[misc]
