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
    archive_messages_before,
    count_archived_messages,
    count_assistant_messages,
    create_conversation,
    delete_conversation,
    get_chat_rag_states,
    get_chat_tool_states,
    get_conversation,
    get_default_model,
    get_default_num_ctx,
    get_default_temperature,
    get_default_tool_iteration_cap,
    get_enabled_rag_server_names,
    get_enabled_tool_names,
    get_setting,
    list_active_messages,
    list_conversations,
    list_messages,
    rename_conversation,
    replace_last_assistant_message,
    seed_chat_rag_servers,
    seed_chat_tools,
    set_active_agent,
    set_conversation_temperature,
    set_conversation_tool_iteration_cap,
    set_default_model,
    set_default_num_ctx,
    set_default_temperature,
    set_default_tool_iteration_cap,
    set_name_auto,
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


def test_create_conversation_defaults_tool_iteration_cap_to_5(
    conn: sqlite3.Connection,
) -> None:
    """A new conversation starts with the default tool-iteration cap of 5."""
    c = create_conversation(conn, name="My chat", model="llama3")
    assert c.tool_iteration_cap == 5


def test_create_conversation_honors_explicit_tool_iteration_cap(
    conn: sqlite3.Connection,
) -> None:
    """create_conversation stores a caller-supplied tool-iteration cap."""
    c = create_conversation(
        conn, name="My chat", model="llama3", tool_iteration_cap=3
    )
    assert c.tool_iteration_cap == 3
    assert get_conversation(conn, c.id).tool_iteration_cap == 3


def test_set_conversation_tool_iteration_cap_round_trips(
    conn: sqlite3.Connection,
) -> None:
    """The setter updates the cap and the change is readable afterwards."""
    c = create_conversation(conn, "X", "llama3")
    updated = set_conversation_tool_iteration_cap(conn, c.id, 7)
    assert updated.tool_iteration_cap == 7
    assert get_conversation(conn, c.id).tool_iteration_cap == 7


def test_set_conversation_tool_iteration_cap_raises_for_unknown_id(
    conn: sqlite3.Connection,
) -> None:
    """Updating a non-existent conversation raises LookupError."""
    with pytest.raises(LookupError):
        set_conversation_tool_iteration_cap(conn, 999999, 5)


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
# Phase 18: archive / list-active / count-archived helpers
# ---------------------------------------------------------------------------


def test_append_message_summary_role_round_trips(
    conn: sqlite3.Connection,
) -> None:
    """The Phase-18 ``summary`` role inserts and reads back cleanly."""
    c = create_conversation(conn, name="t", model="m")
    msg = append_message(conn, c.id, "summary", "the briefing")
    assert msg.role == "summary"
    assert msg.archived_at is None
    rows = list_messages(conn, c.id)
    assert any(r.role == "summary" for r in rows)


def test_list_active_messages_excludes_archived(
    conn: sqlite3.Connection,
) -> None:
    c = create_conversation(conn, name="t", model="m")
    append_message(conn, c.id, "user", "u1")
    append_message(conn, c.id, "assistant", "a1")
    append_message(conn, c.id, "user", "u2")
    summary = append_message(conn, c.id, "summary", "summary text")
    archive_messages_before(conn, c.id, summary.id)

    active = list_active_messages(conn, c.id)
    # Only the summary row should be left active (its id is > all
    # previous ids, so archive_messages_before doesn't touch it).
    assert [m.role for m in active] == ["summary"]
    assert active[0].content == "summary text"


def test_list_active_messages_excludes_archived_summary(
    conn: sqlite3.Connection,
) -> None:
    """A prior archived summary is excluded — the active list shows only
    rows that should go to Ollama on the next turn."""
    c = create_conversation(conn, name="t", model="m")
    summary1 = append_message(conn, c.id, "summary", "first")
    append_message(conn, c.id, "user", "follow-up")
    summary2 = append_message(conn, c.id, "summary", "second")
    archive_messages_before(conn, c.id, summary2.id)

    active = list_active_messages(conn, c.id)
    assert [m.content for m in active] == ["second"]
    # And the first summary is in the full listing but archived.
    all_rows = list_messages(conn, c.id)
    archived = [m for m in all_rows if m.archived_at is not None]
    assert any(m.content == "first" for m in archived)


def test_archive_messages_before_returns_rowcount(
    conn: sqlite3.Connection,
) -> None:
    c = create_conversation(conn, name="t", model="m")
    append_message(conn, c.id, "user", "u1")
    append_message(conn, c.id, "assistant", "a1")
    summary = append_message(conn, c.id, "summary", "s")
    n = archive_messages_before(conn, c.id, summary.id)
    assert n == 2


def test_archive_messages_before_is_idempotent(
    conn: sqlite3.Connection,
) -> None:
    c = create_conversation(conn, name="t", model="m")
    append_message(conn, c.id, "user", "u1")
    summary = append_message(conn, c.id, "summary", "s")
    n1 = archive_messages_before(conn, c.id, summary.id)
    n2 = archive_messages_before(conn, c.id, summary.id)
    assert n1 == 1
    assert n2 == 0


def test_archive_messages_before_bumps_updated_at(
    conn: sqlite3.Connection,
) -> None:
    """Archiving bumps the parent conversation's updated_at so the
    sidebar's sort key reflects the action."""
    c = create_conversation(conn, name="t", model="m")
    append_message(conn, c.id, "user", "u1")
    summary = append_message(conn, c.id, "summary", "s")
    # Snapshot updated_at after the append, then archive.
    before = get_conversation(conn, c.id).updated_at
    # Sleep just enough for the now_iso() string to advance (~ms).
    import time
    time.sleep(0.002)
    archive_messages_before(conn, c.id, summary.id)
    after = get_conversation(conn, c.id).updated_at
    assert after > before


def test_archive_messages_before_no_op_does_not_bump_updated_at(
    conn: sqlite3.Connection,
) -> None:
    """When the WHERE clause matches zero rows, updated_at is not bumped."""
    c = create_conversation(conn, name="t", model="m")
    before = get_conversation(conn, c.id).updated_at
    import time
    time.sleep(0.002)
    n = archive_messages_before(conn, c.id, cutoff_message_id=99999)
    after = get_conversation(conn, c.id).updated_at
    # Empty conversation, nothing to archive: rowcount 0, updated_at intact.
    assert n == 0
    assert after == before


def test_count_archived_messages_counts_only_archived(
    conn: sqlite3.Connection,
) -> None:
    c = create_conversation(conn, name="t", model="m")
    append_message(conn, c.id, "user", "u1")
    append_message(conn, c.id, "assistant", "a1")
    summary = append_message(conn, c.id, "summary", "s")
    archive_messages_before(conn, c.id, summary.id)
    assert count_archived_messages(conn, c.id) == 2


def test_count_archived_messages_zero_for_unknown_id(
    conn: sqlite3.Connection,
) -> None:
    assert count_archived_messages(conn, 999) == 0


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


def test_default_temperature_default_is_0_2(conn: sqlite3.Connection) -> None:
    """No row → 0.2. Production default before the user touches
    /settings."""
    assert get_default_temperature(conn) == 0.2


def test_default_temperature_round_trip(conn: sqlite3.Connection) -> None:
    """A set value reads back unchanged on a subsequent read."""
    set_default_temperature(conn, 1.2)
    assert get_default_temperature(conn) == 1.2


def test_set_default_temperature_clamps_out_of_range(
    conn: sqlite3.Connection,
) -> None:
    """Values outside [0.0, 2.0] are clamped before storage so they
    can never be read back out of range."""
    set_default_temperature(conn, 5.0)
    assert get_default_temperature(conn) == 2.0
    set_default_temperature(conn, -3.0)
    assert get_default_temperature(conn) == 0.0


def test_get_default_temperature_falls_back_on_malformed_row(
    conn: sqlite3.Connection,
) -> None:
    """A non-numeric value (hand-edited or legacy DB) reads as 0.2
    rather than raising, so a corrupt setting can't break chat
    creation."""
    set_setting(conn, "default_temperature", "not-a-number")
    assert get_default_temperature(conn) == 0.2


def test_default_tool_iteration_cap_default_is_5(conn: sqlite3.Connection) -> None:
    """No row → 5. Production default before the user touches /settings."""
    assert get_default_tool_iteration_cap(conn) == 5


def test_default_tool_iteration_cap_round_trip(conn: sqlite3.Connection) -> None:
    """A set value reads back unchanged on a subsequent read."""
    set_default_tool_iteration_cap(conn, 3)
    assert get_default_tool_iteration_cap(conn) == 3


def test_set_default_tool_iteration_cap_clamps_out_of_range(
    conn: sqlite3.Connection,
) -> None:
    """Values outside [1, 10] are clamped before storage."""
    set_default_tool_iteration_cap(conn, 20)
    assert get_default_tool_iteration_cap(conn) == 10
    set_default_tool_iteration_cap(conn, 0)
    assert get_default_tool_iteration_cap(conn) == 1


def test_get_default_tool_iteration_cap_falls_back_on_malformed_row(
    conn: sqlite3.Connection,
) -> None:
    """A non-numeric value reads as 5 rather than raising."""
    set_setting(conn, "default_tool_iteration_cap", "not-a-number")
    assert get_default_tool_iteration_cap(conn) == 5


def test_default_model_default_is_none(conn: sqlite3.Connection) -> None:
    """No row → None. No global default before the user sets one."""
    assert get_default_model(conn) is None


def test_default_model_round_trip(conn: sqlite3.Connection) -> None:
    """A set value reads back unchanged on a subsequent read."""
    set_default_model(conn, "granite4.1:8b")
    assert get_default_model(conn) == "granite4.1:8b"


def test_set_default_model_clears_on_none(conn: sqlite3.Connection) -> None:
    """Passing None removes the setting so it returns None again."""
    set_default_model(conn, "granite4.1:8b")
    set_default_model(conn, None)
    assert get_default_model(conn) is None


def test_set_default_model_clears_on_empty_string(conn: sqlite3.Connection) -> None:
    """Passing an empty string clears the setting (same as None)."""
    set_default_model(conn, "granite4.1:8b")
    set_default_model(conn, "")  # type: ignore[arg-type]
    assert get_default_model(conn) is None


def test_default_num_ctx_default_is_16384(conn: sqlite3.Connection) -> None:
    """No row → 16384. Production default before the user touches /settings."""
    assert get_default_num_ctx(conn) == 16384


def test_default_num_ctx_round_trip(conn: sqlite3.Connection) -> None:
    """A set value reads back unchanged on a subsequent read."""
    set_default_num_ctx(conn, 32768)
    assert get_default_num_ctx(conn) == 32768


def test_set_default_num_ctx_clamps_out_of_range(
    conn: sqlite3.Connection,
) -> None:
    """Values outside [NUM_CTX_MIN, NUM_CTX_MAX] are clamped before storage."""
    from app.queries import NUM_CTX_MAX, NUM_CTX_MIN

    set_default_num_ctx(conn, NUM_CTX_MAX + 1)
    assert get_default_num_ctx(conn) == NUM_CTX_MAX
    set_default_num_ctx(conn, 0)
    assert get_default_num_ctx(conn) == NUM_CTX_MIN


def test_get_default_num_ctx_falls_back_on_malformed_row(
    conn: sqlite3.Connection,
) -> None:
    """A non-numeric value reads as 16384 rather than raising."""
    set_setting(conn, "default_num_ctx", "not-a-number")
    assert get_default_num_ctx(conn) == 16384


def test_resolve_num_ctx_for_project_uses_override(
    conn: sqlite3.Connection,
) -> None:
    """When the project sets num_ctx, that value wins over the global default."""
    from app.queries import resolve_num_ctx_for_project

    set_default_num_ctx(conn, 8000)
    # Project override of 32000 should be returned (and clamped, but it's
    # already in range).
    assert resolve_num_ctx_for_project(conn, 32000) == 32000


def test_resolve_num_ctx_for_project_falls_back_to_global(
    conn: sqlite3.Connection,
) -> None:
    """When the project's num_ctx is None, return the global default."""
    from app.queries import resolve_num_ctx_for_project

    set_default_num_ctx(conn, 8000)
    assert resolve_num_ctx_for_project(conn, None) == 8000


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


# ---------------------------------------------------------------------------
# Phase 16: per-chat active agent
# ---------------------------------------------------------------------------


def test_create_conversation_defaults_active_agent_none(
    conn: sqlite3.Connection,
) -> None:
    """A new chat starts on the Normal agent (active_agent NULL)."""
    chat = create_conversation(conn, name="t", model="m")
    assert chat.active_agent is None
    assert get_conversation(conn, chat.id).active_agent is None


def test_create_conversation_persists_active_agent(
    conn: sqlite3.Connection,
) -> None:
    """Starting a chat with an agent stores its name."""
    chat = create_conversation(
        conn, name="t", model="m", active_agent="research"
    )
    assert chat.active_agent == "research"
    assert get_conversation(conn, chat.id).active_agent == "research"


def test_set_active_agent_sets_and_clears(conn: sqlite3.Connection) -> None:
    """set_active_agent updates the row and round-trips through reads."""
    chat = create_conversation(conn, name="t", model="m")
    updated = set_active_agent(conn, chat.id, "content_generator")
    assert updated.active_agent == "content_generator"
    assert get_conversation(conn, chat.id).active_agent == "content_generator"
    cleared = set_active_agent(conn, chat.id, None)
    assert cleared.active_agent is None
    assert get_conversation(conn, chat.id).active_agent is None


def test_set_active_agent_unknown_conversation_raises(
    conn: sqlite3.Connection,
) -> None:
    """Setting the agent on a missing chat raises LookupError."""
    with pytest.raises(LookupError):
        set_active_agent(conn, 9999, "research")


def test_set_conversation_temperature_round_trip(
    conn: sqlite3.Connection,
) -> None:
    """Per-chat temperature updates persist and round-trip through reads."""
    chat = create_conversation(conn, name="t", model="m")
    updated = set_conversation_temperature(conn, chat.id, 1.4)
    assert updated.temperature == 1.4
    assert get_conversation(conn, chat.id).temperature == 1.4


def test_set_conversation_temperature_unknown_conversation_raises(
    conn: sqlite3.Connection,
) -> None:
    """Updating a missing chat's temperature raises LookupError."""
    with pytest.raises(LookupError):
        set_conversation_temperature(conn, 9999, 1.0)


def test_list_conversations_includes_active_agent(
    conn: sqlite3.Connection,
) -> None:
    """The sidebar listing carries active_agent for each row."""
    chat = create_conversation(
        conn, name="t", model="m", active_agent="research"
    )
    rows = list_conversations(conn)
    match = next(c for c in rows if c.id == chat.id)
    assert match.active_agent == "research"
