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
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    list_messages,
    rename_conversation,
    replace_last_assistant_message,
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


def test_append_message_rejects_invalid_role(
    conn: sqlite3.Connection,
) -> None:
    """The schema CHECK constraint still applies through the query layer."""
    c = create_conversation(conn, "X", "llama3")

    with pytest.raises(sqlite3.IntegrityError):
        # Bypass the Literal type hint by casting — we're testing the DB,
        # not the type system.
        append_message(conn, c.id, "system", "should fail")  # type: ignore[arg-type]


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
