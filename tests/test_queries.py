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
    append_message,
    count_assistant_messages,
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    list_messages,
    rename_conversation,
    replace_last_assistant_message,
    set_name_auto,
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
