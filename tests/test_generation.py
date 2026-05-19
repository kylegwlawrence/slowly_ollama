"""Phase 12g: tests for the background-task generation module."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import generation, ollama, queries
from app.connection import open_connection
from app.db import initialize_database


@pytest.fixture(autouse=True)
def _clear_live_generations():
    """Reset the module-level registry between tests so a residual
    state from a previous test can't influence this one."""
    saved = dict(generation.live_generations)
    generation.live_generations.clear()
    yield
    generation.live_generations.clear()
    generation.live_generations.update(saved)


@pytest.fixture(autouse=True)
def _reset_capability_cache():
    """Drop phase 12f's process-global tool-capability cache.

    Same kind of module-level state as ``live_generations`` above —
    if another file's test populates it first, our ``_run_generation``
    calls would consult stale names instead of going through the
    monkeypatched ``model_supports_tools`` stub.
    """
    ollama.reset_capability_cache()
    yield
    ollama.reset_capability_cache()


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
