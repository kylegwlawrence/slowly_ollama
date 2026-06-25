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


def test_compact_lone_summary_422s(
    make_client: ClientFactory,
) -> None:
    """A chat whose only active row is a summary has nothing new to fold in.

    Re-summarizing a lone summary would only degrade it, so the endpoint
    refuses. This is the floor now that compaction keeps nothing verbatim.
    """
    with make_client(_summarize_handler("ignored")) as client:
        db_path = os.environ["DB_PATH"]
        with open_connection(db_path) as conn:
            chat = queries.create_conversation(conn, name="t", model="llama3")
            queries.append_message(conn, chat.id, "summary", "prior briefing")
        response = client.post(f"/chats/{chat.id}/compact")
    assert response.status_code == 422
    assert "nothing to compact" in response.text.lower()


def test_compact_empty_chat_422s(make_client: ClientFactory) -> None:
    """A chat with no messages at all has nothing to compact."""
    with make_client(_summarize_handler("ignored")) as client:
        db_path = os.environ["DB_PATH"]
        with open_connection(db_path) as conn:
            chat = queries.create_conversation(conn, name="t", model="llama3")
        response = client.post(f"/chats/{chat.id}/compact")
    assert response.status_code == 422
    assert "nothing to compact" in response.text.lower()


def test_compact_single_message_now_summarizes(
    make_client: ClientFactory,
) -> None:
    """With nothing kept verbatim, even a single user message compacts.

    The old keep-recent floor refused chats this short; the new contract
    folds the whole active history regardless of length.
    """
    with make_client(_summarize_handler("the one-message briefing")) as client:
        chat_id = _create_chat_db_only("just one message")
        response = client.post(f"/chats/{chat_id}/compact")
    assert response.status_code == 200

    db_path = os.environ["DB_PATH"]
    with open_connection(db_path) as conn:
        active = queries.list_active_messages(conn, chat_id)
    # Only the summary survives; the original user message is archived.
    assert [m.role for m in active] == ["summary"]
    assert "the one-message briefing" in active[0].content


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

    # Nothing is kept verbatim: the summary is the ONLY active row.
    assert [m.role for m in active] == ["summary"]
    assert "the user wants X" in active[0].content

    # All 10 originals are still in the DB, just archived.
    archived = [m for m in all_rows if m.archived_at is not None]
    assert len(archived) == 10
    # The last user/assistant pair is archived now, not kept.
    assert all(m.archived_at is not None for m in all_rows if "msg-asst-4" in m.content)

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
    # 10 prior rows, all folded in (nothing kept verbatim), so the corpus
    # contains every active row.
    assert len(corpus) == 10


def test_post_compaction_turn_excludes_archived_from_model_payload(
    make_client: ClientFactory,
) -> None:
    """After compaction, the NEXT chat turn must not resend archived rows.

    This is the whole point of Compact: shrink per-turn context. The
    generation layer reads ``list_active_messages`` (archived_at IS NULL), so
    a continued turn should send Ollama only the summary + the kept tail + the
    new user message — never the compacted-away originals. End-to-end guard:
    compact a seeded chat, then drive a real turn and inspect the streaming
    /api/chat payload.
    """
    SUMMARY = "BRIEFING-SUMMARY-TOKEN"
    captured: dict = {"stream": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/chat":
            return httpx.Response(
                404, content=f"unexpected {request.url.path}".encode()
            )
        body = json.loads(request.content or b"{}")
        if body.get("stream"):
            # The actual generation. Capture its payload; return a minimal
            # one-token NDJSON reply the SSE pipeline can consume.
            captured["stream"] = body
            return httpx.Response(
                200,
                content=(
                    b'{"message":{"content":"ok"},"done":false}\n'
                    b'{"message":{"content":""},"done":true}\n'
                ),
            )
        # Non-streaming: either the compaction summarizer or the turn's
        # tool-probe (and the auto-titler). Distinguish by the instruction in
        # the last user turn so the summarizer gets the summary and the probe
        # gets a clean "no tool calls" so the producer proceeds to stream.
        last = body["messages"][-1]["content"] if body["messages"] else ""
        if "compact briefing" in last:
            return httpx.Response(
                200, json={"message": {"content": SUMMARY, "tool_calls": []}}
            )
        return httpx.Response(
            200, json={"message": {"content": "", "tool_calls": []}}
        )

    with make_client(handler) as client:
        # 10 rows; compaction archives ALL of them, keeping nothing verbatim.
        chat_id = _seed_chat_with_history(turns=5, content_prefix="ARCHIVEME")
        assert client.post(f"/chats/{chat_id}/compact").status_code == 200

        # Continue chatting: POST a fresh message, then drive the stream.
        client.post(
            f"/chats/{chat_id}/messages", data={"content": "FRESH-QUESTION"}
        )
        client.get(f"/chats/{chat_id}/stream")

    assert captured["stream"] is not None, "continued turn never streamed"
    blob = "\n".join(
        m.get("content", "") for m in captured["stream"]["messages"]
    )
    # The archived originals (all 5 turns) must NOT reach the model.
    for i in range(5):
        assert f"ARCHIVEME-user-{i}" not in blob
        assert f"ARCHIVEME-asst-{i}" not in blob
    # The summary replacement and the new message DO.
    assert SUMMARY in blob
    assert "FRESH-QUESTION" in blob


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
