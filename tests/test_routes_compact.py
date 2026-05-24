"""Tests for Phase 18: the manual-compact endpoint and the archived viewer.

Each test gets a fresh DB + mocked Ollama via the shared ``make_client``
fixture from ``tests/test_routes.py``. The chat is seeded directly through
``queries`` so the test controls exactly how many rows exist before the
POST — the create-chat-and-message path would spawn a generation and
consume our mock.
"""

import json
import os
from typing import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from app import generation, queries
from app.connection import open_connection
from app.routes.chats import _split_for_compact

# Re-use the shared fixtures from test_routes.
from tests.test_routes import (
    ClientFactory,
    _create_chat_db_only,
    _default_project_id,
    make_client,  # noqa: F401 — fixture re-export
    _default_tool_capable,  # noqa: F401 — fixture re-export
)


# ---------------------------------------------------------------------------
# Mock builders
# ---------------------------------------------------------------------------


def _summarize_handler(summary: str) -> Callable[[httpx.Request], httpx.Response]:
    """Handler that returns ``summary`` from the non-streaming /api/chat call.

    The compact endpoint only ever hits /api/chat non-streaming, so the
    handler is simpler than the streaming variants in ``test_routes.py``.
    Any other path 404s so misrouted traffic surfaces clearly.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/chat":
            return httpx.Response(
                404, content=f"unexpected {request.url.path}".encode()
            )
        body = json.loads(request.content or b"{}")
        assert body.get("stream") is False, (
            "Compaction must be non-streaming"
        )
        return httpx.Response(
            200,
            json={"message": {"content": summary, "tool_calls": []}},
        )

    return handler


def _seed_chat_with_history(
    *, turns: int, content_prefix: str = "msg"
) -> int:
    """Create a chat and append ``turns`` paired user/assistant rows.

    Returns the chat id. Used by tests that need a chat with enough
    history that the compact endpoint actually has something to summarize.
    """
    db_path = os.environ["DB_PATH"]
    with open_connection(db_path) as conn:
        chat = queries.create_conversation(
            conn, name="t", model="llama3"
        )
        for i in range(turns):
            queries.append_message(
                conn, chat.id, "user", f"{content_prefix}-user-{i}"
            )
            queries.append_message(
                conn, chat.id, "assistant", f"{content_prefix}-asst-{i}"
            )
    return chat.id


# ---------------------------------------------------------------------------
# Endpoint: POST /chats/{id}/compact
# ---------------------------------------------------------------------------


def test_compact_unknown_chat_404s(make_client: ClientFactory) -> None:
    with make_client(_summarize_handler("summary")) as client:
        response = client.post("/chats/99999/compact")
    assert response.status_code == 404


def test_compact_with_too_little_history_422s(
    make_client: ClientFactory,
) -> None:
    """A chat with just one user message has nothing to summarize."""
    with make_client(_summarize_handler("ignored")) as client:
        chat_id = _create_chat_db_only("just one message")
        response = client.post(f"/chats/{chat_id}/compact")
    assert response.status_code == 422
    assert "nothing to compact" in response.text.lower()


def test_compact_archives_old_turns_and_inserts_summary(
    make_client: ClientFactory,
) -> None:
    """Happy path: old turns become archived, a summary row appears."""
    with make_client(
        _summarize_handler("the user wants X; we found Y.")
    ) as client:
        chat_id = _seed_chat_with_history(turns=5)  # 10 rows total
        response = client.post(f"/chats/{chat_id}/compact")
    assert response.status_code == 200

    db_path = os.environ["DB_PATH"]
    with open_connection(db_path) as conn:
        all_rows = queries.list_messages(conn, chat_id)
        active = queries.list_active_messages(conn, chat_id)

    # Exactly one active summary row, plus the most-recent KEEP_RECENT
    # rows still active. KEEP_RECENT is 4 (see _KEEP_RECENT_ON_COMPACT
    # in app.routes.chats); 10 rows - 4 kept = 6 archived; +1 new summary.
    summaries = [m for m in active if m.role == "summary"]
    assert len(summaries) == 1
    assert "the user wants X" in summaries[0].content

    # Originals are still in the DB, just archived.
    archived = [m for m in all_rows if m.archived_at is not None]
    assert len(archived) > 0
    # The kept tail should still hold the last user/assistant pair.
    assert any(
        m.role == "assistant" and "msg-asst-4" in m.content for m in active
    )

    # The HTML response is the re-rendered #messages container.
    assert 'id="messages"' in response.text
    assert "the user wants X" in response.text


def test_compact_blocks_while_generation_in_flight(
    make_client: ClientFactory,
) -> None:
    """A live generation makes the endpoint 409."""
    with make_client(_summarize_handler("ignored")) as client:
        chat_id = _seed_chat_with_history(turns=5)
        # Plant a not-done state directly in the registry. The endpoint
        # reads `live_generations.get(...)` and refuses when `state.done
        # is False`.
        state = generation.GenerationState(conversation_id=chat_id)
        state.done = False
        generation.live_generations[chat_id] = state
        try:
            response = client.post(f"/chats/{chat_id}/compact")
        finally:
            generation.live_generations.pop(chat_id, None)
    assert response.status_code == 409
    assert "generating" in response.text.lower()


def test_compact_ollama_unreachable_503s(
    make_client: ClientFactory,
) -> None:
    """Ollama transport failure surfaces as 503 (matches the rest of the app)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with make_client(handler) as client:
        chat_id = _seed_chat_with_history(turns=5)
        response = client.post(f"/chats/{chat_id}/compact")
    assert response.status_code == 503


def test_compact_ollama_bad_shape_502s(
    make_client: ClientFactory,
) -> None:
    """An Ollama protocol-level failure surfaces as 502."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with make_client(handler) as client:
        chat_id = _seed_chat_with_history(turns=5)
        response = client.post(f"/chats/{chat_id}/compact")
    assert response.status_code == 502


def test_compact_empty_summary_502s(make_client: ClientFactory) -> None:
    """A blank summary from the model is treated as a protocol failure."""
    with make_client(_summarize_handler("")) as client:
        chat_id = _seed_chat_with_history(turns=5)
        response = client.post(f"/chats/{chat_id}/compact")
    assert response.status_code == 502


def test_recompact_subsumes_prior_summary(
    make_client: ClientFactory,
) -> None:
    """Re-compacting a chat that already has a summary archives the old one
    and inserts a fresh one.

    Verifies the "at most one active summary" invariant.
    """
    chat_id_holder = {}
    with make_client(_summarize_handler("first summary")) as client:
        chat_id = _seed_chat_with_history(turns=5)
        chat_id_holder["id"] = chat_id
        first = client.post(f"/chats/{chat_id}/compact")
        assert first.status_code == 200

        # Add a few more turns so the second compact has something new
        # to fold in.
        db_path = os.environ["DB_PATH"]
        with open_connection(db_path) as conn:
            for i in range(3):
                queries.append_message(conn, chat_id, "user", f"second-u-{i}")
                queries.append_message(
                    conn, chat_id, "assistant", f"second-a-{i}"
                )

    with make_client(_summarize_handler("second summary")) as client2:
        second = client2.post(f"/chats/{chat_id_holder['id']}/compact")
    assert second.status_code == 200

    with open_connection(db_path) as conn:
        active = queries.list_active_messages(conn, chat_id_holder["id"])
        all_rows = queries.list_messages(conn, chat_id_holder["id"])

    active_summaries = [m for m in active if m.role == "summary"]
    assert len(active_summaries) == 1
    assert active_summaries[0].content == "second summary"

    # The first summary must now be among archived rows.
    archived_summaries = [
        m for m in all_rows
        if m.role == "summary" and m.archived_at is not None
    ]
    assert len(archived_summaries) == 1
    assert archived_summaries[0].content == "first summary"


def test_compact_payload_excludes_archived_from_summarizer_input(
    make_client: ClientFactory,
) -> None:
    """The corpus passed to the summarizer is the active-but-old prefix,
    not the full DB list. Otherwise re-compaction would balloon."""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/chat":
            return httpx.Response(404)
        captured.append(json.loads(request.content or b"{}"))
        return httpx.Response(
            200, json={"message": {"content": "ok", "tool_calls": []}}
        )

    with make_client(handler) as client:
        chat_id = _seed_chat_with_history(turns=5)
        response = client.post(f"/chats/{chat_id}/compact")
    assert response.status_code == 200
    assert len(captured) == 1
    # The summarizer instruction is the last `user` row in the payload —
    # everything before it is the corpus.
    msgs = captured[0]["messages"]
    assert msgs[-1]["role"] == "user"
    corpus = msgs[:-1]
    # 10 prior rows; KEEP_RECENT=4 keeps the trailing 4 in the chat (NOT
    # in the corpus) so the corpus contains the 6 oldest rows.
    assert len(corpus) == 6


# ---------------------------------------------------------------------------
# GET /chats/{id}/archived
# ---------------------------------------------------------------------------


def test_archived_endpoint_404s_on_unknown_chat(
    make_client: ClientFactory,
) -> None:
    with make_client(_summarize_handler("ignored")) as client:
        response = client.get("/chats/99999/archived")
    assert response.status_code == 404


def test_archived_endpoint_returns_archived_originals(
    make_client: ClientFactory,
) -> None:
    """After a compact, the archived endpoint returns the original turns
    (minus any archived summary row).
    """
    with make_client(_summarize_handler("the briefing")) as client:
        chat_id = _seed_chat_with_history(turns=5)
        client.post(f"/chats/{chat_id}/compact")
        response = client.get(f"/chats/{chat_id}/archived")
    assert response.status_code == 200
    # Archived originals appear; the summary itself does NOT (it's the
    # ACTIVE summary row, not archived).
    assert "msg-user-0" in response.text
    assert "the briefing" not in response.text


def test_archived_endpoint_empty_for_uncompacted_chat(
    make_client: ClientFactory,
) -> None:
    """A chat that's never been compacted has no archived rows to show."""
    with make_client(_summarize_handler("ignored")) as client:
        chat_id = _seed_chat_with_history(turns=2)
        response = client.get(f"/chats/{chat_id}/archived")
    assert response.status_code == 200
    assert "No archived messages" in response.text


# ---------------------------------------------------------------------------
# _split_for_compact unit tests — the helper is interesting enough to pin
# directly rather than only through the endpoint behavior.
# ---------------------------------------------------------------------------


def _msg(role: str, *, mid: int = 0) -> queries.Message:
    from datetime import UTC, datetime
    return queries.Message(
        id=mid, conversation_id=1, role=role, content="x",
        created_at=datetime.now(UTC),
    )


def test_split_keeps_last_n_renderables() -> None:
    """A flat list of user/assistant rows splits at the boundary."""
    rows = [
        _msg("user", mid=1), _msg("assistant", mid=2),
        _msg("user", mid=3), _msg("assistant", mid=4),
        _msg("user", mid=5), _msg("assistant", mid=6),
        _msg("user", mid=7), _msg("assistant", mid=8),
    ]
    to_summarize, to_keep = _split_for_compact(rows, 4)
    assert [m.id for m in to_summarize] == [1, 2, 3, 4]
    assert [m.id for m in to_keep] == [5, 6, 7, 8]


def test_split_slides_past_orphan_tool_rows() -> None:
    """A kept window whose head would be a tool_result slides forward
    until it points at a renderable row.

    Construct a sequence where the natural boundary at the Nth-from-last
    renderable lands ON a tool_call (the user/assistant turn it belonged
    to is already in the kept tail).
    """
    # u(1), a(2), tc(3), tr(4), a(5), u(6), a(7) — 4 renderables.
    # keep_recent=3 → scan finds a(7)=1, u(6)=2, a(5)=3 → break at
    # index 4 (assistant id=5). The slide is a no-op since active[4]
    # is renderable. To exercise the slide we need the boundary to
    # land ON a tool row — happens when keep_recent is exactly the
    # count of trailing renderables AFTER the last tool batch.
    rows = [
        _msg("user", mid=1),
        _msg("tool_call", mid=2),
        _msg("tool_result", mid=3),
        _msg("assistant", mid=4),
        _msg("user", mid=5),
        _msg("assistant", mid=6),
    ]
    # keep_recent=3: scan finds a(6)=1, u(5)=2, a(4)=3 → break at
    # index 3 (a4). Then slide: active[3]=a (renderable, no slide).
    # Result: to_summarize=[u1,tc2,tr3], to_keep=[a4,u5,a6].
    to_summarize, to_keep = _split_for_compact(rows, 3)
    assert [m.id for m in to_keep] == [4, 5, 6]
    # The leading kept row is renderable, never a tool_*.
    assert to_keep[0].role not in ("tool_call", "tool_result")


def test_split_with_too_little_history_returns_empty_summarize() -> None:
    rows = [_msg("user", mid=1), _msg("assistant", mid=2)]
    to_summarize, to_keep = _split_for_compact(rows, 4)
    assert to_summarize == []
    assert len(to_keep) == 2


def test_split_treats_prior_summary_as_renderable() -> None:
    """A prior summary row counts toward the kept window — re-compaction
    will subsume it into the new summary."""
    rows = [
        _msg("summary", mid=1),
        _msg("user", mid=2),
        _msg("assistant", mid=3),
        _msg("user", mid=4),
        _msg("assistant", mid=5),
    ]
    to_summarize, to_keep = _split_for_compact(rows, 4)
    # The summary plus the last 3 rows = 4 renderables kept.
    assert [m.id for m in to_keep] == [2, 3, 4, 5]
    assert [m.id for m in to_summarize] == [1]
