"""Phase 12g: tests for the background-task generation module."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app import generation, ollama, queries
from app.connection import open_connection
from app.db import initialize_database
from app.generation import _build_history_payload
from app.queries import Message


def _setup_chat(db_path: Path, name: str = "test") -> int:
    """Create a chat + one user message in a fresh DB. Returns the conv id."""
    initialize_database(db_path)
    with open_connection(db_path) as conn:
        chat = queries.create_conversation(conn, name, "llama3")
        queries.append_message(conn, chat.id, "user", "hi")
    return chat.id


def _no_tools_handler() -> "callable":
    """Build an httpx handler that returns 'no tools wanted' on every probe."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if body.get("stream"):
            return httpx.Response(
                200,
                content=(
                    b'{"message":{"content":"hi"},"done":false}\n'
                    b'{"message":{"content":""},"done":true}\n'
                ),
            )
        return httpx.Response(
            200, json={"message": {"content": "", "tool_calls": []}}
        )
    return handler


# ---------------------------------------------------------------------------
# start_generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_generation_registers_state_and_spawns_task(tmp_path):
    """The state lands in live_generations immediately; the task
    runs in the background."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_no_tools_handler()),
        base_url="http://test",
    )

    with open_connection(db_path) as db:
        state = generation.start_generation(
            client=client,
            db=db,
            conversation_id=conv_id,
            model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )

        assert conv_id in generation.live_generations
        assert generation.live_generations[conv_id] is state
        assert state.task is not None

        await state.task
        # State stays in the registry on done so slow reloads can
        # still replay (the phase 12g design choice).
        assert conv_id in generation.live_generations
        assert state.done


@pytest.mark.asyncio
async def test_start_generation_rejects_in_flight_duplicate(tmp_path):
    """Second start while a non-done generation exists for the same
    conv raises GenerationInProgress."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)

    # Sentinel state that isn't yet done — simulate an in-flight gen
    # without actually running an async task (cheap).
    state = generation.GenerationState(conversation_id=conv_id)
    generation.live_generations[conv_id] = state

    with pytest.raises(generation.GenerationInProgress):
        generation.start_generation(
            client=None,
            db=None,
            conversation_id=conv_id,
            model="llama3",
            history=[],
            on_complete="append",
        )


@pytest.mark.asyncio
async def test_start_generation_evicts_done_state(tmp_path):
    """A done state is allowed to be replaced by a fresh
    generation for the same conv."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_no_tools_handler()),
        base_url="http://test",
    )

    # Plant a done state from a "previous" turn.
    prev = generation.GenerationState(conversation_id=conv_id)
    prev.done = True
    prev.events.append(("token", "stale"))
    generation.live_generations[conv_id] = prev

    with open_connection(db_path) as db:
        new_state = generation.start_generation(
            client=client,
            db=db,
            conversation_id=conv_id,
            model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )
        assert generation.live_generations[conv_id] is new_state
        assert new_state is not prev
        await new_state.task


# ---------------------------------------------------------------------------
# consume_generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_generation_drains_then_exits(tmp_path):
    """A fresh consumer attached to a live gen sees every event in
    order, then exits when the producer sets done."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_no_tools_handler()),
        base_url="http://test",
    )

    with open_connection(db_path) as db:
        state = generation.start_generation(
            client=client,
            db=db,
            conversation_id=conv_id,
            model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )
        seen = []
        async for sse in generation.consume_generation(state):
            seen.append(sse)
        # At minimum: one token event ("hi"), one done event.
        assert any("event: token" in s for s in seen)
        assert any("event: done" in s for s in seen)
        # Last event is done — consumer exited at the right moment.
        assert "event: done" in seen[-1]


@pytest.mark.asyncio
async def test_late_consumer_replays_full_history(tmp_path):
    """A consumer attached AFTER the producer has finished still
    sees every event from index 0 (this is the slow-reload path)."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_no_tools_handler()),
        base_url="http://test",
    )

    with open_connection(db_path) as db:
        state = generation.start_generation(
            client=client,
            db=db,
            conversation_id=conv_id,
            model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )
        await state.task  # producer fully finished
        assert state.done

        seen = []
        async for sse in generation.consume_generation(state):
            seen.append(sse)
        assert any("event: token" in s for s in seen)
        assert any("event: done" in s for s in seen)


@pytest.mark.asyncio
async def test_two_consumers_see_same_events(tmp_path):
    """Concurrent consumers each replay from index 0 and converge
    on identical event sequences."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_no_tools_handler()),
        base_url="http://test",
    )

    with open_connection(db_path) as db:
        state = generation.start_generation(
            client=client,
            db=db,
            conversation_id=conv_id,
            model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )

        seen_a, seen_b = [], []

        async def drain(out: list) -> None:
            async for sse in generation.consume_generation(state):
                out.append(sse)

        await asyncio.gather(drain(seen_a), drain(seen_b))
    assert seen_a == seen_b
    assert any("event: done" in s for s in seen_a)


# ---------------------------------------------------------------------------
# consume_finished
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_finished_emits_done_from_persisted_assistant(
    tmp_path, monkeypatch
):
    """When no live state exists, consume_finished yields a single
    done event built from the most recent persisted assistant row."""
    db_path = tmp_path / "chats.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("OLLAMA_HOST", "http://test")

    initialize_database(db_path)
    with open_connection(db_path) as db:
        chat = queries.create_conversation(db, "finished", "llama3")
        queries.append_message(db, chat.id, "user", "hi")
        queries.append_message(db, chat.id, "assistant", "the response")

        seen = []
        async for sse in generation.consume_finished(db, chat.id):
            seen.append(sse)
    assert len(seen) == 1
    assert "event: done" in seen[0]
    assert f'outerHTML:#assistant-stream-{chat.id}' in seen[0]
    assert "the response" in seen[0]


@pytest.mark.asyncio
async def test_consume_finished_emits_empty_bubble_when_no_assistant(
    tmp_path, monkeypatch
):
    """Defensive: a conversation with no assistant row at all still
    gets a done event (empty bubble) so the placeholder closes."""
    db_path = tmp_path / "chats.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("OLLAMA_HOST", "http://test")

    initialize_database(db_path)
    with open_connection(db_path) as db:
        chat = queries.create_conversation(db, "no-assistant", "llama3")
        queries.append_message(db, chat.id, "user", "hi")

        seen = []
        async for sse in generation.consume_finished(db, chat.id):
            seen.append(sse)
    assert len(seen) == 1
    assert "event: done" in seen[0]
    # Empty assistant bubble OOB-swap so the placeholder resolves.
    assert "message message--assistant" in seen[0]


# ---------------------------------------------------------------------------
# Helper functions (moved from routes.py to generation.py in phase 12g)
# ---------------------------------------------------------------------------


def test_summary_text_helper_imported_from_render() -> None:
    """summary_text + format_elapsed_mm_ss are still in app.render —
    this just confirms the import surface from generation.py is
    intact (sanity check after the routes.py → generation.py move)."""
    from app.render import format_elapsed_mm_ss, summary_text

    assert summary_text(1, done=False) == "using 1 tool…"
    assert format_elapsed_mm_ss(8000) == "0:08"


def test_build_history_payload_lives_in_generation() -> None:
    """The helper that was at app.routes._build_history_payload in
    phase 12e.1 is now at app.generation._build_history_payload."""
    from app.generation import _build_history_payload
    from app.queries import Message
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    history = [
        Message(
            id=1, conversation_id=1, role="user",
            content="hi", created_at=now,
        ),
        Message(
            id=2, conversation_id=1, role="tool_call",
            content='{"name": "current_time", "arguments": {}}',
            created_at=now,
        ),
        Message(
            id=3, conversation_id=1, role="tool_result",
            content="2026-05-19T12:00:00Z", created_at=now,
        ),
    ]
    out = _build_history_payload(history)
    # user → straight through
    assert out[0] == {"role": "user", "content": "hi"}
    # tool_call → assistant + tool_calls
    assert out[1]["role"] == "assistant"
    assert out[1]["tool_calls"][0]["function"]["name"] == "current_time"
    # tool_result → role=tool
    assert out[2] == {"role": "tool", "content": "2026-05-19T12:00:00Z"}


# ---------------------------------------------------------------------------
# tools= gating (phase 12f)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generation_omits_tools_when_model_not_tool_capable(
    tmp_path, monkeypatch
):
    """When the model isn't tool-capable, /api/chat carries no ``tools`` key.

    Phase 12f's belt-and-suspenders for the 400-on-non-tool-capable
    case. The dropdown filter prevents fresh chats from being created
    with a non-tool-capable model, but a chat row pins its model at
    creation time — a model that later loses tool support (re-pull,
    Ollama upgrade) would 400 every follow-up without this guard.
    """
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)

    captured_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        captured_bodies.append(body)
        if body.get("stream"):
            return httpx.Response(
                200,
                content=(
                    b'{"message":{"content":"hi"},"done":false}\n'
                    b'{"message":{"content":""},"done":true}\n'
                ),
            )
        return httpx.Response(
            200, json={"message": {"content": "", "tool_calls": []}}
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )

    # Stub the capability check to "not tool-capable" so we exercise
    # the `tools_payload = None` branch in _run_generation without
    # having to wire a full /api/tags + /api/show fixture chain.
    async def _not_capable(_client, _model):
        return False

    monkeypatch.setattr(ollama, "model_supports_tools", _not_capable)

    with open_connection(db_path) as db:
        state = generation.start_generation(
            client=client,
            db=db,
            conversation_id=conv_id,
            model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )
        await state.task

    # At least one probe (non-stream) plus the streaming reply both
    # hit /api/chat. None of those bodies should include ``tools`` —
    # ``maybe_tool_call`` omits the key when its tools arg is None,
    # and ``stream_chat`` never sends it.
    assert captured_bodies, "expected at least one /api/chat call"
    assert any(not b.get("stream") for b in captured_bodies), (
        "expected a non-stream probe so we can verify it lacked tools"
    )
    for body in captured_bodies:
        assert "tools" not in body, (
            f"non-tool-capable model still received tools=: {body}"
        )


# ---------------------------------------------------------------------------
# tool_result JSON envelope (phase 12h)
# ---------------------------------------------------------------------------


def _tool_handler(
    *,
    items: list[dict],
    used_dense: bool = True,
) -> "callable":
    """Build a handler that asks for one query_rag call, then streams a reply.

    Round 1 (non-stream probe): emits a single ``tool_calls`` entry the
    generation loop picks up and runs through ``run_tool``.
    Round 2 (non-stream probe): no tool calls, so the loop breaks out.
    Stream: a short final assistant token then done.

    The RAG response body for the in-loop ``query_rag`` is delivered by
    a separate fake httpx client patched into ``app.tools.rag`` at test
    time (see ``test_tool_result_persisted_as_json_envelope`` below).
    """
    state = {"chat_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if body.get("stream"):
            return httpx.Response(
                200,
                content=(
                    b'{"message":{"content":"answer"},"done":false}\n'
                    b'{"message":{"content":""},"done":true}\n'
                ),
            )
        state["chat_calls"] += 1
        if state["chat_calls"] == 1:
            # First non-stream probe: tell the model to call query_rag.
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "query_rag",
                                    "arguments": {
                                        "source": "arxiv",
                                        "query": "test",
                                    },
                                }
                            }
                        ],
                    }
                },
            )
        # Subsequent non-stream probes: no further calls, break loop.
        return httpx.Response(
            200, json={"message": {"content": "", "tool_calls": []}}
        )

    return handler


def _install_rag_server(db_path: Path, monkeypatch) -> None:
    """Seed an arxiv RAG server + point DB_PATH so query_rag sees it."""
    from app import rag_servers as _rs

    monkeypatch.setenv("DB_PATH", str(db_path))
    with open_connection(db_path) as conn:
        _rs.create_server(conn, "arxiv", "http://fake/arxiv")


def _patch_rag_http(monkeypatch, items: list[dict], used_dense: bool = True):
    """Patch ``httpx.AsyncClient`` inside app.tools.rag to a MockTransport.

    Mirrors the pattern in test_tools.py — see the comment there for
    why we snapshot the real AsyncClient before patching.
    """
    from app.tools import rag as _rag

    def rag_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"items": items, "used_dense": used_dense},
        )

    real_client = httpx.AsyncClient

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self._client = real_client(
                transport=httpx.MockTransport(rag_handler)
            )

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, *exc):
            await self._client.aclose()

    monkeypatch.setattr(_rag.httpx, "AsyncClient", _FakeClient)


@pytest.mark.asyncio
async def test_tool_result_persisted_as_json_envelope_with_sources(
    tmp_path, monkeypatch
):
    """End-to-end: a query_rag tool call lands as a JSON envelope on
    the tool_result row, with title+section preserved for historic
    render."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)
    _install_rag_server(db_path, monkeypatch)
    # Build the test's Ollama chat client BEFORE patching httpx —
    # _patch_rag_http monkeypatches httpx.AsyncClient at module level
    # (since `_rag.httpx is httpx`), so any AsyncClient(...) call AFTER
    # the patch would resolve to the fake. Capture the real one first.
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_tool_handler(items=[])),
        base_url="http://test",
    )
    _patch_rag_http(monkeypatch, items=[
        {"title": "Doc A", "section": "1", "text": "first"},
        {"title": "Doc B", "section": None, "text": "second"},
    ])

    async def _capable(*args, **kwargs):
        return True

    monkeypatch.setattr(ollama, "model_supports_tools", _capable)

    with open_connection(db_path) as db:
        state = generation.start_generation(
            client=client,
            db=db,
            conversation_id=conv_id,
            model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )
        await state.task

        rows = queries.list_messages(db, conv_id)
    tool_results = [r for r in rows if r.role == "tool_result"]
    assert len(tool_results) == 1

    envelope = json.loads(tool_results[0].content)
    assert "text" in envelope
    assert "[1] Doc A (§1)" in envelope["text"]
    assert envelope["sources"] == [
        {"title": "Doc A", "section": "1"},
        {"title": "Doc B", "section": None},
    ]


@pytest.mark.asyncio
async def test_tool_result_persisted_as_json_envelope_for_text_only_tool(
    tmp_path, monkeypatch
):
    """Even for tools with no sources (e.g. current_time), the envelope
    shape is uniform — sources is just an empty list. Simplifies the
    decode path: every row is JSON; no per-row shape detection."""
    from app.tools import builtins  # noqa: F401 — registers current_time

    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)
    monkeypatch.setenv("DB_PATH", str(db_path))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if body.get("stream"):
            return httpx.Response(
                200,
                content=(
                    b'{"message":{"content":"ok"},"done":false}\n'
                    b'{"message":{"content":""},"done":true}\n'
                ),
            )
        # First non-stream probe asks for current_time; subsequent ones
        # return no tool_calls so the loop exits.
        if not getattr(handler, "_called", False):
            handler._called = True
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "current_time",
                                    "arguments": {"timezone": "UTC"},
                                }
                            }
                        ],
                    }
                },
            )
        return httpx.Response(
            200, json={"message": {"content": "", "tool_calls": []}}
        )

    async def _capable(*args, **kwargs):
        return True

    monkeypatch.setattr(ollama, "model_supports_tools", _capable)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )

    with open_connection(db_path) as db:
        state = generation.start_generation(
            client=client,
            db=db,
            conversation_id=conv_id,
            model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )
        await state.task

        rows = queries.list_messages(db, conv_id)
    tool_results = [r for r in rows if r.role == "tool_result"]
    assert len(tool_results) == 1
    envelope = json.loads(tool_results[0].content)
    # current_time has no sources — uniform envelope with empty list.
    assert envelope["sources"] == []
    assert envelope["text"].startswith("20")  # ISO timestamp


def test_build_history_payload_decodes_json_envelope_tool_result() -> None:
    """A tool_result row whose content is the JSON envelope is mapped
    to {"role": "tool", "content": <text only>} for Ollama — the
    model never sees the JSON envelope wrapper."""
    now = datetime.now(timezone.utc)
    envelope = json.dumps({
        "text": "[1] Doc (§Intro)\n    body",
        "sources": [{"title": "Doc", "section": "Intro"}],
    })
    history = [
        Message(
            id=1, conversation_id=1, role="tool_result",
            content=envelope, created_at=now,
        ),
    ]
    out = _build_history_payload(history)
    assert out == [
        {"role": "tool", "content": "[1] Doc (§Intro)\n    body"},
    ]


def test_build_history_payload_plain_text_tool_result_backwards_compat() -> None:
    """Pre-12h plain-text content passes through unchanged via the
    decode fallback. Pin: old conversations keep replaying."""
    now = datetime.now(timezone.utc)
    history = [
        Message(
            id=1, conversation_id=1, role="tool_result",
            content="2026-05-19T12:00:00Z", created_at=now,
        ),
    ]
    out = _build_history_payload(history)
    assert out == [
        {"role": "tool", "content": "2026-05-19T12:00:00Z"},
    ]


def test_build_history_payload_handles_tool_roles() -> None:
    """`_build_history_payload` maps each role to Ollama's wire format:
    user/assistant pass through, tool_call becomes assistant+tool_calls,
    tool_result becomes role=tool."""
    now = datetime.now(timezone.utc)
    history = [
        Message(
            id=1, conversation_id=1, role="user",
            content="hi", created_at=now,
        ),
        Message(
            id=2, conversation_id=1, role="tool_call",
            content=json.dumps(
                {"name": "current_time", "arguments": {"timezone": "UTC"}}
            ),
            created_at=now,
        ),
        Message(
            id=3, conversation_id=1, role="tool_result",
            content="2024-01-01T00:00:00+00:00", created_at=now,
        ),
        Message(
            id=4, conversation_id=1, role="assistant",
            content="the time is...", created_at=now,
        ),
    ]
    out = _build_history_payload(history)
    # 4 rows in → 4 messages out (no skips on well-formed input).
    assert len(out) == 4
    # user: passes through.
    assert out[0] == {"role": "user", "content": "hi"}
    # tool_call: assistant + tool_calls list with the function dict.
    assert out[1]["role"] == "assistant"
    assert out[1]["content"] == ""
    assert out[1]["tool_calls"] == [
        {
            "function": {
                "name": "current_time",
                "arguments": {"timezone": "UTC"},
            }
        }
    ]
    # tool_result: role becomes "tool"; content stays as the raw string.
    assert out[2] == {
        "role": "tool",
        "content": "2024-01-01T00:00:00+00:00",
    }
    # assistant: passes through.
    assert out[3] == {"role": "assistant", "content": "the time is..."}


def test_build_history_payload_skips_malformed_tool_call_rows() -> None:
    """A tool_call row with invalid JSON in `content` is silently
    skipped — better than crashing every subsequent chat turn for
    that conversation."""
    now = datetime.now(timezone.utc)
    history = [
        Message(
            id=1, conversation_id=1, role="user",
            content="hi", created_at=now,
        ),
        # Garbage JSON in a tool_call row.
        Message(
            id=2, conversation_id=1, role="tool_call",
            content="not json", created_at=now,
        ),
        # Missing required `name` key.
        Message(
            id=3, conversation_id=1, role="tool_call",
            content='{"arguments": {}}', created_at=now,
        ),
        Message(
            id=4, conversation_id=1, role="assistant",
            content="ok", created_at=now,
        ),
    ]
    out = _build_history_payload(history)
    # The two malformed tool_call rows are dropped; user + assistant remain.
    assert len(out) == 2
    assert out[0]["role"] == "user"
    assert out[1]["role"] == "assistant"


def test_build_history_payload_skips_orphan_result_after_corrupt_call() -> None:
    """A corrupt tool_call also drops its paired tool_result. Otherwise
    the result would land as role='tool' with no preceding assistant
    +tool_calls — Ollama rejects that shape with a 400 and the whole
    chat becomes unusable. Pins the pairing rule that's documented in
    _build_history_payload's skip_next_result logic."""
    now = datetime.now(timezone.utc)
    history = [
        Message(
            id=1, conversation_id=1, role="user",
            content="hi", created_at=now,
        ),
        # Corrupt: must be dropped.
        Message(
            id=2, conversation_id=1, role="tool_call",
            content="not json", created_at=now,
        ),
        # Paired result: must ALSO be dropped (orphan otherwise).
        Message(
            id=3, conversation_id=1, role="tool_result",
            content="2024-01-01T00:00:00+00:00", created_at=now,
        ),
        Message(
            id=4, conversation_id=1, role="assistant",
            content="ok", created_at=now,
        ),
    ]
    out = _build_history_payload(history)
    # Only user + assistant survive; the corrupt call AND its paired
    # result are both gone.
    assert len(out) == 2
    assert out[0]["role"] == "user"
    assert out[1]["role"] == "assistant"
    # Defensive: no role="tool" anywhere in the output.
    assert all(m["role"] != "tool" for m in out)


def test_build_history_payload_skip_flag_does_not_leak_past_unrelated_rows() -> None:
    """A corrupt tool_call followed by a NON-result row (e.g., the
    model emitted a stray assistant message) resets the skip flag, so
    a later valid call/result pair still renders into Ollama's wire
    format. Without the reset, the next legitimate tool_result anywhere
    in the conversation would silently vanish."""
    now = datetime.now(timezone.utc)
    history = [
        Message(
            id=1, conversation_id=1, role="user",
            content="hi", created_at=now,
        ),
        # Corrupt call sets the skip flag.
        Message(
            id=2, conversation_id=1, role="tool_call",
            content="not json", created_at=now,
        ),
        # Assistant row resets the flag — the corrupt call's paired
        # result never appeared (real-world: writer crashed mid-turn).
        Message(
            id=3, conversation_id=1, role="assistant",
            content="interim text", created_at=now,
        ),
        # Fresh, well-formed call/result pair must pass through.
        Message(
            id=4, conversation_id=1, role="tool_call",
            content=json.dumps(
                {"name": "current_time", "arguments": {"timezone": "UTC"}}
            ),
            created_at=now,
        ),
        Message(
            id=5, conversation_id=1, role="tool_result",
            content="2024-01-01T00:00:00+00:00", created_at=now,
        ),
        Message(
            id=6, conversation_id=1, role="assistant",
            content="done", created_at=now,
        ),
    ]
    out = _build_history_payload(history)
    # Corrupt call dropped; everything else through. user + interim
    # assistant + (valid call as assistant+tool_calls) + tool_result +
    # final assistant = 5.
    assert len(out) == 5
    assert out[0]["role"] == "user"
    assert out[1] == {"role": "assistant", "content": "interim text"}
    # The valid call survives the flag-reset and produces its
    # assistant+tool_calls pair.
    assert out[2]["role"] == "assistant"
    assert out[2]["tool_calls"][0]["function"]["name"] == "current_time"
    # And its paired result lands as role=tool.
    assert out[3] == {
        "role": "tool",
        "content": "2024-01-01T00:00:00+00:00",
    }
    assert out[4] == {"role": "assistant", "content": "done"}


def test_build_history_payload_drops_phase13_agentic_rows() -> None:
    """`research_findings` and `review_verdict` rows are agentic-loop
    internal artifacts. They MUST NOT appear in the wire-format payload
    we ship to Ollama for unrelated calls — `_maybe_emit_title`
    rebuilds the same history via `_build_history_payload`, and Ollama
    would reject the unfamiliar role names. The orchestrator builds
    its own per-agent payloads separately."""
    now = datetime.now(timezone.utc)
    history = [
        Message(
            id=1, conversation_id=1, role="user",
            content="hi", created_at=now,
        ),
        Message(
            id=2, conversation_id=1, role="research_findings",
            content="research notes the model produced", created_at=now,
        ),
        Message(
            id=3, conversation_id=1, role="review_verdict",
            content='{"verdict": "passed", "message": "looks good"}',
            created_at=now,
        ),
        Message(
            id=4, conversation_id=1, role="assistant",
            content="the answer", created_at=now,
        ),
    ]
    out = _build_history_payload(history)
    # Only user + assistant survive.
    assert len(out) == 2
    assert out[0] == {"role": "user", "content": "hi"}
    assert out[1] == {"role": "assistant", "content": "the answer"}
    # Defensive: neither agentic role surfaces under any name.
    roles = {m["role"] for m in out}
    assert "research_findings" not in roles
    assert "review_verdict" not in roles


def test_build_history_payload_agentic_row_does_not_swallow_following_tool_result() -> None:
    """A `research_findings` row is NOT a tool_call — it must not
    arm the skip-next-tool_result flag. Otherwise a later legitimate
    tool_result anywhere in the conversation would silently vanish.
    Mirrors the skip-flag-reset rule for assistant rows."""
    now = datetime.now(timezone.utc)
    history = [
        Message(
            id=1, conversation_id=1, role="research_findings",
            content="prior turn's findings", created_at=now,
        ),
        Message(
            id=2, conversation_id=1, role="tool_call",
            content=json.dumps(
                {"name": "current_time", "arguments": {"timezone": "UTC"}}
            ),
            created_at=now,
        ),
        Message(
            id=3, conversation_id=1, role="tool_result",
            content="2024-01-01T00:00:00+00:00", created_at=now,
        ),
    ]
    out = _build_history_payload(history)
    # Findings dropped; valid call + result both survive.
    assert len(out) == 2
    assert out[0]["role"] == "assistant"
    assert out[0]["tool_calls"][0]["function"]["name"] == "current_time"
    assert out[1] == {
        "role": "tool",
        "content": "2024-01-01T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_frozen_row_after_tool_result_carries_sources_in_oob_payload(
    tmp_path, monkeypatch
):
    """The tool-result SSE event's HTML payload contains the
    expandable-row markers (tool-row--expandable + <details>) when
    the tool returns sources. Pins the live-stream contract end-to-end."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)
    _install_rag_server(db_path, monkeypatch)
    # Build the chat client BEFORE patching httpx (see the
    # backwards-comment in the sibling test for the rationale).
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_tool_handler(items=[])),
        base_url="http://test",
    )
    _patch_rag_http(monkeypatch, items=[
        {"title": "Doc Z", "section": "Body", "text": "x"},
    ])

    async def _capable(*args, **kwargs):
        return True

    monkeypatch.setattr(ollama, "model_supports_tools", _capable)

    with open_connection(db_path) as db:
        state = generation.start_generation(
            client=client,
            db=db,
            conversation_id=conv_id,
            model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )
        await state.task

    # Find the tool-result event payload in the event log.
    tool_result_events = [
        payload for (ev, payload) in state.events if ev == "tool-result"
    ]
    assert tool_result_events, "expected a tool-result SSE event"
    payload = tool_result_events[0]
    assert "tool-row--expandable" in payload
    assert "<details" in payload
    assert "Doc Z" in payload
    assert "(§Body)" in payload
    # The OOB swap unit is the outer <li>, not the inner <details>.
    li_prefix, _, details_part = payload.partition("<details")
    assert 'hx-swap-oob="outerHTML"' in li_prefix
    assert "hx-swap-oob" not in details_part
