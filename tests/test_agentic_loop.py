"""Phase 13d.2: integration-style tests for _run_agentic_generation.

Mocks Ollama via monkeypatch on `app.ollama.maybe_tool_call` /
`app.ollama.stream_chat`. Each test scripts a sequence of responses,
drives the orchestrator against a fresh GenerationState + tempfile
DB, and asserts on (a) the persisted message-row sequence and
(b) the SSE event log captured in `state.events`.

Test style mirrors `tests/test_generation.py`: no shared conftest
fixtures beyond the autouse module-state reset in `tests/conftest.py`;
each test sets up its own DB tempfile and stubs Ollama.
"""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from app import generation, ollama, queries
from app.agents import loop as agentic_loop
from app.connection import open_connection
from app.db import initialize_database


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _setup_chat(db_path: Path, name: str = "agentic test") -> int:
    """Create a chat + one user message in a fresh DB. Returns conv id."""
    initialize_database(db_path)
    with open_connection(db_path) as conn:
        chat = queries.create_conversation(conn, name, "llama3")
        queries.append_message(
            conn, chat.id, "user", "what's the latest on X?",
        )
    return chat.id


def _make_state(conv_id: int) -> generation.GenerationState:
    """Fresh GenerationState for a conversation."""
    return generation.GenerationState(conversation_id=conv_id)


def _scripted_maybe_tool_call(
    responses: list[tuple[list[dict], str]],
) -> tuple[Callable, list[list[dict]]]:
    """Build a fake `maybe_tool_call` that returns each response in order.

    Returns the stub plus a list that records each call's `messages`
    argument so tests can assert on what got sent to Ollama.
    """
    state = {"i": 0}
    sent_payloads: list[list[dict]] = []

    async def fake(client_, model_, messages_, tools=None):
        sent_payloads.append(messages_)
        i = state["i"]
        state["i"] += 1
        if i >= len(responses):
            raise AssertionError(
                f"unexpected maybe_tool_call #{i}; scripted only "
                f"{len(responses)} responses"
            )
        return responses[i]

    return fake, sent_payloads


def _scripted_stream_chat(
    chunks: list[str],
) -> Callable[..., Awaitable[None]]:
    """Build a fake `stream_chat` that yields the given text chunks."""
    async def fake(client_, model_, messages_):
        for c in chunks:
            yield ollama.ChatChunk(content=c, done=False)
        yield ollama.ChatChunk(content="", done=True)

    return fake


def _stub_title_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub `ollama.generate_title` so `_maybe_emit_title` no-ops.

    The orchestrator calls _maybe_emit_title on `on_complete="append"`
    (same posture as single-agent). Without a working client OR this
    stub, the title call AttributeError's on `client.post`. Returning
    "" makes `_maybe_emit_title` treat it as "skip rename" and exit
    cleanly — keeping the tests focused on orchestrator behavior
    rather than title-generation side effects.
    """
    async def _no_title(*args, **kwargs):
        return ""

    monkeypatch.setattr(ollama, "generate_title", _no_title)


def _persisted_roles(db_path: Path, conv_id: int) -> list[str]:
    """Sequence of `role` values for the conversation's messages."""
    with open_connection(db_path) as conn:
        return [m.role for m in queries.list_messages(conn, conv_id)]


def _event_names(state: generation.GenerationState) -> list[str]:
    """Sequence of SSE event names recorded on the state."""
    return [ev for (ev, _payload) in state.events]


def _emitted_payloads(
    state: generation.GenerationState, event_name: str
) -> list[str]:
    """All payloads emitted under a given event name."""
    return [p for (ev, p) in state.events if ev == event_name]


# ---------------------------------------------------------------------------
# Defensive guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_requires_pending_user_message(tmp_path):
    """If the caller drives the orchestrator without a trailing user
    row, emit a clear error and signal done. Pinning the defensive
    guard at the top of `_run_agentic_generation`."""
    db_path = tmp_path / "chats.db"
    initialize_database(db_path)
    with open_connection(db_path) as conn:
        chat = queries.create_conversation(conn, "no-user", "llama3")
        # No user message appended.

        state = _make_state(chat.id)
        await agentic_loop._run_agentic_generation(
            state=state,
            client=None,  # no Ollama call should happen
            db=conn,
            conversation_id=chat.id,
            model="llama3",
            history=[],  # empty
            on_complete="append",
        )

    assert "error" in _event_names(state)
    error_payload = _emitted_payloads(state, "error")[0]
    assert "without a pending user message" in error_payload
    assert state.done is True


# ---------------------------------------------------------------------------
# Happy path — pass on iteration 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_passes_on_first_iteration(tmp_path, monkeypatch):
    """Research calls one tool → emits findings → review marks_passed
    → generation streams a final answer. The persisted role sequence
    pins the agentic shape end-to-end."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)

    fake_call, _ = _scripted_maybe_tool_call([
        # Research iter 1: ask for one current_time call.
        ([{"name": "current_time", "arguments": {}}], ""),
        # Research iter 1: no more tools, here are the findings.
        ([], "Found that the current time is noon UTC."),
        # Review iter 1: mark_passed.
        (
            [{"name": "mark_passed", "arguments": {"reason": "clear answer"}}],
            "",
        ),
    ])
    monkeypatch.setattr(ollama, "maybe_tool_call", fake_call)
    _stub_title_noop(monkeypatch)
    monkeypatch.setattr(
        ollama, "stream_chat",
        _scripted_stream_chat(["The current ", "time is noon."]),
    )

    with open_connection(db_path) as db:
        state = _make_state(conv_id)
        history = queries.list_messages(db, conv_id)
        await agentic_loop._run_agentic_generation(
            state=state, client=None, db=db,
            conversation_id=conv_id, model="llama3",
            history=history, on_complete="append",
        )

    # Persisted role sequence: user (seeded) + tool_call + tool_result
    # + research_findings + review_verdict + assistant.
    assert _persisted_roles(db_path, conv_id) == [
        "user",
        "tool_call",
        "tool_result",
        "research_findings",
        "review_verdict",
        "assistant",
    ]
    # SSE order: card shell → iteration-start → tool-call → tool-result
    # → research-findings → review-verdict → tokens → done.
    events = _event_names(state)
    assert events[0] == "tool-call"  # the empty card shell
    assert "iteration-start" in events
    assert "research-findings" in events
    assert "review-verdict" in events
    assert events.count("token") >= 1
    assert events[-1] == "done"

    # The done payload includes both the past-tense summary swap and
    # the final assistant bubble.
    done_payload = _emitted_payloads(state, "done")[0]
    assert "ran 1 iteration" in done_payload
    assert "The current time is noon." in done_payload


# ---------------------------------------------------------------------------
# Retry — iteration 1 fails, iteration 2 passes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_retries_then_passes(tmp_path, monkeypatch):
    """Iteration 1 review fails; iteration 2 research re-runs (sees
    the feedback in its payload), produces new findings, review
    passes. Two research_findings + two review_verdict rows persist."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)

    fake_call, sent_payloads = _scripted_maybe_tool_call([
        # Iter 1 research: no tools, weak findings.
        ([], "weak first attempt"),
        # Iter 1 review: request_more_research with feedback.
        (
            [{
                "name": "request_more_research",
                "arguments": {"feedback": "cite the source paper"},
            }],
            "",
        ),
        # Iter 2 research: no tools, better findings.
        ([], "stronger second attempt with citation [1]"),
        # Iter 2 review: mark_passed.
        (
            [{"name": "mark_passed", "arguments": {"reason": "now cited"}}],
            "",
        ),
    ])
    monkeypatch.setattr(ollama, "maybe_tool_call", fake_call)
    _stub_title_noop(monkeypatch)
    monkeypatch.setattr(
        ollama, "stream_chat",
        _scripted_stream_chat(["Final answer."]),
    )

    with open_connection(db_path) as db:
        state = _make_state(conv_id)
        await agentic_loop._run_agentic_generation(
            state=state, client=None, db=db,
            conversation_id=conv_id, model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )

    roles = _persisted_roles(db_path, conv_id)
    assert roles == [
        "user",
        "research_findings",   # iter 1
        "review_verdict",      # iter 1 (failed)
        "research_findings",   # iter 2
        "review_verdict",      # iter 2 (passed)
        "assistant",
    ]
    # Iter 2's research payload sees the iter-1 feedback as a
    # user-role message (pushed onto intra_turn).
    iter_2_research_payload = sent_payloads[2]
    feedback_messages = [
        m for m in iter_2_research_payload
        if m.get("role") == "user" and "Review feedback" in m.get("content", "")
    ]
    assert len(feedback_messages) == 1
    assert "cite the source paper" in feedback_messages[0]["content"]

    # Done summary reads "ran 2 iterations".
    done_payload = _emitted_payloads(state, "done")[0]
    assert "ran 2 iterations" in done_payload


# ---------------------------------------------------------------------------
# Max iterations — force generation after 3 failed reviews
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_max_iterations_force_generates(tmp_path, monkeypatch):
    """Three failed reviews → for-else fires → max-iterations badge
    emitted → generation runs anyway with the last findings."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)

    fail = (
        [{"name": "request_more_research", "arguments": {"feedback": "more"}}],
        "",
    )
    fake_call, _ = _scripted_maybe_tool_call([
        ([], "findings v1"),
        fail,
        ([], "findings v2"),
        fail,
        ([], "findings v3"),
        fail,
    ])
    monkeypatch.setattr(ollama, "maybe_tool_call", fake_call)
    _stub_title_noop(monkeypatch)
    monkeypatch.setattr(
        ollama, "stream_chat",
        _scripted_stream_chat(["forced final"]),
    )

    with open_connection(db_path) as db:
        state = _make_state(conv_id)
        await agentic_loop._run_agentic_generation(
            state=state, client=None, db=db,
            conversation_id=conv_id, model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )

    roles = _persisted_roles(db_path, conv_id)
    # user + (research_findings + review_verdict) x 3 + assistant.
    assert roles == [
        "user",
        "research_findings", "review_verdict",
        "research_findings", "review_verdict",
        "research_findings", "review_verdict",
        "assistant",
    ]
    # Max-iterations badge was emitted (visible "(max reached)" text).
    badge_payloads = [
        p for (ev, p) in state.events
        if ev == "iteration-start" and "max-marker" in p
    ]
    assert len(badge_payloads) == 1
    assert "(max reached)" in badge_payloads[0]
    # Generation streamed and the done summary reads 3 iterations
    # (no max-reached suffix in the summary itself — the marker
    # carries that signal).
    done_payload = _emitted_payloads(state, "done")[0]
    assert "ran 3 iterations" in done_payload
    assert "max reached" not in done_payload


# ---------------------------------------------------------------------------
# Defensive verdict — model fails to call a verdict tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_review_no_verdict_falls_through(tmp_path, monkeypatch):
    """Review agent emits no recognized tool call → parse_verdict's
    fallback fires → treated as failed → loop continues."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)

    fake_call, _ = _scripted_maybe_tool_call([
        # Iter 1 research: findings.
        ([], "findings"),
        # Iter 1 review: model called nothing.
        ([], ""),
        # Iter 2 research: findings.
        ([], "better findings"),
        # Iter 2 review: passes properly.
        ([{"name": "mark_passed", "arguments": {"reason": "ok"}}], ""),
    ])
    monkeypatch.setattr(ollama, "maybe_tool_call", fake_call)
    _stub_title_noop(monkeypatch)
    monkeypatch.setattr(
        ollama, "stream_chat", _scripted_stream_chat(["done"]),
    )

    with open_connection(db_path) as db:
        state = _make_state(conv_id)
        await agentic_loop._run_agentic_generation(
            state=state, client=None, db=db,
            conversation_id=conv_id, model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )

    # The iter-1 verdict_row should report failed with the default
    # "did not call" message.
    verdict_payloads = _emitted_payloads(state, "review-verdict")
    assert len(verdict_payloads) == 2
    assert "Failed:" in verdict_payloads[0]
    assert "did not call" in verdict_payloads[0]
    # Iter 2 verdict is the proper passed.
    assert "Passed:" in verdict_payloads[1]
    # The persisted iter-1 review_verdict row encodes the fallback.
    with open_connection(db_path) as conn:
        rows = queries.list_messages(conn, conv_id)
    verdicts = [
        json.loads(m.content) for m in rows if m.role == "review_verdict"
    ]
    assert verdicts[0] == {
        "verdict": "failed",
        "message": (
            "Review agent did not call a verdict tool. Continue"
            " researching."
        ),
    }
    assert verdicts[1]["verdict"] == "passed"


# ---------------------------------------------------------------------------
# Inner cap — research hits 5 tool calls, hands off to review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_research_inner_cap_handoff_to_review(tmp_path, monkeypatch):
    """Research calls tools 5 times without ever producing findings
    text → synthesized empty-findings message → review still runs."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)

    tool_call = ([{"name": "current_time", "arguments": {}}], "")
    fake_call, _ = _scripted_maybe_tool_call([
        # 5 research calls, all asking for current_time again.
        tool_call, tool_call, tool_call, tool_call, tool_call,
        # Review: pass with the synthesized findings.
        ([{"name": "mark_passed", "arguments": {"reason": "ok"}}], ""),
    ])
    monkeypatch.setattr(ollama, "maybe_tool_call", fake_call)
    _stub_title_noop(monkeypatch)
    monkeypatch.setattr(
        ollama, "stream_chat", _scripted_stream_chat(["done"]),
    )

    with open_connection(db_path) as db:
        state = _make_state(conv_id)
        await agentic_loop._run_agentic_generation(
            state=state, client=None, db=db,
            conversation_id=conv_id, model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )

    # 5 tool_call rows + 5 tool_result rows persisted within iter 1.
    roles = _persisted_roles(db_path, conv_id)
    assert roles.count("tool_call") == 5
    assert roles.count("tool_result") == 5
    # Exactly one research_findings row — the synthesized one.
    findings_rows = [
        r for r in roles if r == "research_findings"
    ]
    assert len(findings_rows) == 1
    # The findings text mentions "No findings produced".
    with open_connection(db_path) as conn:
        rows = queries.list_messages(conn, conv_id)
    findings_msg = next(m for m in rows if m.role == "research_findings")
    assert "No findings produced" in findings_msg.content


# ---------------------------------------------------------------------------
# Feedback persistence — review feedback flows into every intra-iteration call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feedback_persists_across_intra_iteration_tool_calls(
    tmp_path, monkeypatch,
):
    """After iter 1 fails, iter 2's research makes multiple tool
    calls. The review feedback must be visible in the payload of
    EVERY maybe_tool_call inside iter 2, not just the first.

    Regression test for the bug fixed in the plan-review pass — if
    feedback was passed as a one-shot parameter to
    `_build_research_payload` it would only appear in the first
    call's payload, not subsequent ones."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)

    fake_call, sent_payloads = _scripted_maybe_tool_call([
        # Iter 1 research: findings immediately.
        ([], "weak"),
        # Iter 1 review: fail with specific feedback.
        (
            [{
                "name": "request_more_research",
                "arguments": {"feedback": "ADD CITATIONS"},
            }],
            "",
        ),
        # Iter 2 research call #1: ask for a tool.
        ([{"name": "current_time", "arguments": {}}], ""),
        # Iter 2 research call #2: ask for another tool.
        ([{"name": "current_time", "arguments": {}}], ""),
        # Iter 2 research call #3: produce findings.
        ([], "stronger with citations [1] [2]"),
        # Iter 2 review: pass.
        ([{"name": "mark_passed", "arguments": {"reason": "ok"}}], ""),
    ])
    monkeypatch.setattr(ollama, "maybe_tool_call", fake_call)
    _stub_title_noop(monkeypatch)
    monkeypatch.setattr(
        ollama, "stream_chat", _scripted_stream_chat(["final"]),
    )

    with open_connection(db_path) as db:
        state = _make_state(conv_id)
        await agentic_loop._run_agentic_generation(
            state=state, client=None, db=db,
            conversation_id=conv_id, model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )

    # Payload indices: [0]=iter1 research, [1]=iter1 review,
    # [2]=iter2 research#1, [3]=iter2 research#2, [4]=iter2 research#3,
    # [5]=iter2 review.
    iter_2_payloads = sent_payloads[2:5]
    assert len(iter_2_payloads) == 3

    def _has_feedback(payload: list[dict]) -> bool:
        return any(
            m.get("role") == "user" and "ADD CITATIONS" in m.get("content", "")
            for m in payload
        )

    # Every iter-2 research call carries the feedback.
    assert all(_has_feedback(p) for p in iter_2_payloads), (
        "Review feedback must be visible in every intra-iteration call"
    )


# ---------------------------------------------------------------------------
# Ollama errors at each stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_error_during_research_emits_error_event(
    tmp_path, monkeypatch,
):
    """OllamaUnavailable during research → SSE error event +
    persisted_or_errored gate prevents the safety-net `finally` from
    double-writing a partial."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)

    async def fake(client_, model_, messages_, tools=None):
        raise ollama.OllamaUnavailable("Ollama down")

    monkeypatch.setattr(ollama, "maybe_tool_call", fake)

    with open_connection(db_path) as db:
        state = _make_state(conv_id)
        await agentic_loop._run_agentic_generation(
            state=state, client=None, db=db,
            conversation_id=conv_id, model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )

    assert "error" in _event_names(state)
    assert "Ollama unavailable" in _emitted_payloads(state, "error")[0]
    # No assistant row persisted — error path returned before the
    # generation phase, and persisted_or_errored gates the
    # safety-net partial-write.
    assert "assistant" not in _persisted_roles(db_path, conv_id)


@pytest.mark.asyncio
async def test_ollama_error_during_generation_emits_error_event(
    tmp_path, monkeypatch,
):
    """OllamaUnavailable during the streaming generation pass → SSE
    error event. Research + review already persisted; assistant row
    is NOT persisted because the error fires before the persist."""
    db_path = tmp_path / "chats.db"
    conv_id = _setup_chat(db_path)

    fake_call, _ = _scripted_maybe_tool_call([
        ([], "findings"),
        ([{"name": "mark_passed", "arguments": {"reason": "ok"}}], ""),
    ])
    monkeypatch.setattr(ollama, "maybe_tool_call", fake_call)
    _stub_title_noop(monkeypatch)

    async def fake_stream(*args, **kwargs):
        raise ollama.OllamaUnavailable("Ollama died mid-stream")
        yield  # unreachable

    monkeypatch.setattr(ollama, "stream_chat", fake_stream)

    with open_connection(db_path) as db:
        state = _make_state(conv_id)
        await agentic_loop._run_agentic_generation(
            state=state, client=None, db=db,
            conversation_id=conv_id, model="llama3",
            history=queries.list_messages(db, conv_id),
            on_complete="append",
        )

    assert "error" in _event_names(state)
    # Research + review rows persisted; assistant didn't.
    roles = _persisted_roles(db_path, conv_id)
    assert "research_findings" in roles
    assert "review_verdict" in roles
    assert "assistant" not in roles
