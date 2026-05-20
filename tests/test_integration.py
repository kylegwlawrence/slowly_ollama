"""End-to-end integration test.

Walks the full single-chat user journey through `TestClient`:

    create → list → load → send → stream → regenerate → rename → delete

Catches gaps where individual routes pass their per-route unit tests
but don't wire together correctly across a real session.

Ollama is mocked via `httpx.MockTransport` per Phase 10's documented
strategy (see `tests/README.md`) — no real Ollama server contacted.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_ollama_client


def _ndjson_chat(chunks: list[str]) -> bytes:
    """Build an NDJSON `/api/chat` stream body from text chunks.

    Each chunk becomes a `done=false` line; a final empty-content
    `done=true` line terminates the stream as Ollama does.
    """
    lines = [
        f'{{"message":{{"content":"{c}"}},"done":false}}' for c in chunks
    ]
    lines.append('{"message":{"content":""},"done":true}')
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture
def integration_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """TestClient with a fresh tempfile DB and a scripted Ollama mock.

    The mock counts calls to `/api/chat` and returns a different
    streamed payload each time, so the journey can verify that
    regenerate actually replaces the assistant response (not just
    re-emits the same text).
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "chats.db"))
    monkeypatch.setenv("OLLAMA_HOST", "http://test")

    # Phase 12d: a single assistant turn now triggers TWO /api/chat
    # POSTs — first a non-streaming probe for tool intent, then the
    # streaming reply. We only vary the streaming reply across the
    # journey's "first" vs. "regenerate" turn; every probe gets the
    # no-tool-calls JSON object.
    import json as _json
    stream_call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "llama3"}]}
            )
        # Phase 12f: list_tool_capable_models fans out one /api/show
        # per model behind the dropdown. The journey relies on `llama3`
        # surfacing in the dropdown AND on the generation-side guard
        # passing tools= through, so advertise the "tools" capability.
        if request.url.path == "/api/show":
            return httpx.Response(
                200, json={"capabilities": ["completion", "tools"]}
            )
        # /api/chat — branch on whether it's the probe or the stream.
        body = _json.loads(request.content or b"{}")
        if not body.get("stream"):
            return httpx.Response(
                200,
                json={"message": {"content": "", "tool_calls": []}},
            )
        stream_call_count[0] += 1
        if stream_call_count[0] == 1:
            return httpx.Response(
                200, content=_ndjson_chat(["First ", "reply"])
            )
        return httpx.Response(
            200, content=_ndjson_chat(["Regenerated ", "reply"])
        )

    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )

    from main import app

    saved_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_ollama_client] = lambda: mock_client

    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved_overrides)


def test_full_user_journey(integration_client: TestClient) -> None:
    """One chat, lifecycle from create through delete.

    Asserts state at every step so a regression in any single
    request's contract surfaces as a clear failure rather than
    cascading silently through subsequent steps.

    The post-11b flow: the composer (empty state) posts the model +
    first message together; the response carries the rendered chat
    panel (with the user bubble and an inline SSE placeholder) and
    an OOB sidebar row in the same body.
    """
    client = integration_client

    # 1. Index page renders the composer (empty-state was removed
    # in phase 11b).
    index = client.get("/")
    assert index.status_code == 200
    assert 'class="composer"' in index.text
    assert "chats-list" in index.text

    # 2. Create a chat AND send the first message in one POST.
    # Response carries the rendered chat panel + an OOB sidebar row
    # marked for afterbegin into #chats-list + the HX-Push-Url
    # header pointing at the new chat.
    created = client.post(
        "/chats", data={"model": "llama3", "content": "hello"}
    )
    assert created.status_code == 201
    match = re.search(r'data-chat-id="(\d+)"', created.text)
    assert match is not None
    chat_id = int(match.group(1))
    assert 'class="chat-panel"' in created.text
    assert 'class="chat-item"' in created.text
    assert 'hx-swap-oob="afterbegin:#chats-list"' in created.text
    # The first user message is already in the panel and an
    # assistant placeholder is waiting on SSE.
    assert 'data-role="user"' in created.text
    assert "hello" in created.text
    assert f'sse-connect="/chats/{chat_id}/stream"' in created.text
    assert created.headers.get("HX-Push-Url") == f"/chats/{chat_id}"

    # 3. Sidebar now shows the chat. The placeholder name is derived
    # from the first user message ("hello") — phase 11d may auto-rename
    # to a tinyllama-generated title after the assistant replies.
    chats = client.get("/chats")
    assert ">hello<" in chats.text
    assert f'data-chat-id="{chat_id}"' in chats.text

    # 4. Reload-safe: a direct browser hit on /chats/{id} returns the
    # full index page with the chat preloaded.
    panel = client.get(f"/chats/{chat_id}")
    assert panel.status_code == 200
    assert "hello" in panel.text  # first user message persisted
    assert "chat-panel" in panel.text

    # 5. Drive the SSE stream for the first assistant reply.
    stream = client.get(f"/chats/{chat_id}/stream")
    assert stream.status_code == 200
    assert "event: token" in stream.text
    assert "data: First " in stream.text
    assert "event: done" in stream.text

    # 6. Conversation now has both messages persisted.
    panel_after = client.get(f"/chats/{chat_id}")
    assert "hello" in panel_after.text
    assert "First reply" in panel_after.text

    # 7. Regenerate the last assistant response.
    regen_placeholder = client.post(f"/chats/{chat_id}/regenerate")
    assert regen_placeholder.status_code == 200
    assert (
        f'sse-connect="/chats/{chat_id}/stream"'
        in regen_placeholder.text
    )

    # 8. Drive the regenerate stream.
    regen_stream = client.get(f"/chats/{chat_id}/stream")
    assert regen_stream.status_code == 200
    assert "Regenerated " in regen_stream.text

    # 9. The assistant message has been replaced in place — new text
    # present, old text gone (regenerate replaces, doesn't append).
    panel_after_regen = client.get(f"/chats/{chat_id}")
    assert "Regenerated reply" in panel_after_regen.text
    assert "First reply" not in panel_after_regen.text

    # 10. Rename. PATCH returns the updated sidebar row in display
    # mode (no editing class, new name shown, placeholder gone).
    rename = client.patch(
        f"/chats/{chat_id}", data={"name": "Renamed Journey"}
    )
    assert rename.status_code == 200
    assert ">Renamed Journey<" in rename.text
    # The placeholder was derived from the first message ("hello").
    assert ">hello<" not in rename.text
    assert "chat-item--editing" not in rename.text

    # 11. Delete from the sidebar while not currently viewing this
    # chat (Referer = /). No HX-Location header in the response so
    # the user's current view stays intact.
    deleted = client.delete(
        f"/chats/{chat_id}", headers={"Referer": "http://test/"}
    )
    assert deleted.status_code == 200
    assert "HX-Location" not in deleted.headers

    # 12. Sidebar is empty again.
    chats_final = client.get("/chats")
    assert f'data-chat-id="{chat_id}"' not in chats_final.text


def test_delete_while_viewing_emits_hx_location(
    integration_client: TestClient,
) -> None:
    """Mirror of the journey's step 11 but with a Referer that points
    at the chat being deleted — the response carries HX-Location: /
    so HTMX navigates the user away from the 404'd URL.

    Worth a separate test from the full journey because the journey
    deletes from a different view; this is the "delete the chat
    you're looking at" path.
    """
    client = integration_client

    created = client.post(
        "/chats", data={"model": "llama3", "content": "first msg"}
    )
    chat_id = int(
        re.search(r'data-chat-id="(\d+)"', created.text).group(1)
    )

    response = client.delete(
        f"/chats/{chat_id}",
        headers={"Referer": f"http://test/chats/{chat_id}"},
    )
    assert response.status_code == 200
    assert response.headers.get("HX-Location") == "/"


# ---------------------------------------------------------------------------
# Phase 13g: agentic-mode end-to-end happy path
# ---------------------------------------------------------------------------


@pytest.fixture
def agentic_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """TestClient wired for an agentic-mode happy-path journey.

    Scripts an Ollama mock that:
    - Reports `llama3` as tool-capable (so the dispatcher routes to
      `_run_agentic_generation` instead of the single-agent fallback).
    - Returns a `current_time` tool call on research's first probe,
      then findings text on its second probe (no more tools).
    - Returns a `mark_passed` verdict tool call on review's probe.
    - Streams the generation agent's final answer.

    Probe vs. stream is branched on `body["stream"]`; probe-call
    counting picks the right scripted response based on which agent's
    turn it is.
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "chats.db"))
    monkeypatch.setenv("OLLAMA_HOST", "http://test")

    import json as _json
    probe_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "llama3"}]}
            )
        if request.url.path == "/api/show":
            return httpx.Response(
                200, json={"capabilities": ["completion", "tools"]}
            )
        # Must be /api/chat — branch by stream flag.
        body = _json.loads(request.content or b"{}")
        if body.get("stream"):
            # Generation agent's final answer.
            return httpx.Response(
                200, content=_ndjson_chat(["The ", "answer ", "is 42."])
            )
        # Non-streaming probe. The orchestrator calls maybe_tool_call
        # three times per happy-path turn:
        #   1. Research: emit one current_time tool call.
        #   2. Research: emit findings text, no more tools.
        #   3. Review: emit mark_passed verdict tool call.
        # Plus title-gen at the end via another stream — but title
        # generation flows through stream_chat, not the probe.
        probe_count[0] += 1
        if probe_count[0] == 1:
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
        if probe_count[0] == 2:
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": "Found a timestamp via the tool.",
                        "tool_calls": [],
                    }
                },
            )
        if probe_count[0] == 3:
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "mark_passed",
                                    "arguments": {"reason": "looks good"},
                                }
                            }
                        ],
                    }
                },
            )
        # Any further probes are unexpected (e.g. an accidental
        # second research iteration). Surface as a clear failure.
        return httpx.Response(
            500,
            content=b"unexpected probe past the happy-path count",
        )

    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )

    from main import app

    saved_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_ollama_client] = lambda: mock_client

    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved_overrides)


def test_agentic_mode_full_journey(agentic_client: TestClient) -> None:
    """Enable agentic mode → send a message → loop runs → answer
    arrives → reload reconstructs the agentic card from persisted rows.

    Covers the seam between every 13-phase piece:
    - 13a/13e: agentic_mode flag persisted via POST /settings/agentic-mode
    - 13d.3 dispatcher: routes to `_run_agentic_generation` because
      both the toggle is on AND llama3 advertises `tools`
    - 13d.2 orchestrator: emits iteration-start / tool-call /
      tool-result / research-findings / review-verdict / token / done
    - 13f historic render: GET /chats/{id} after completion shows the
      AgenticToolBatchBlock-rendered card
    """
    client = agentic_client

    # 1. Toggle agentic mode on.
    toggle_response = client.post(
        "/settings/agentic-mode",
        data={"enabled": "on"},
        headers={"HX-Request": "true"},
    )
    assert toggle_response.status_code == 200

    # 2. Create a chat with the user's first message. The composer
    # POST kicks off the agentic generation as a side effect.
    created = client.post(
        "/chats", data={"model": "llama3", "content": "what time is it?"}
    )
    assert created.status_code == 201
    chat_id = int(
        re.search(r'data-chat-id="(\d+)"', created.text).group(1)
    )

    # 3. Drive the SSE stream. The orchestrator's full event sequence
    # for a one-iteration happy path is:
    #   iteration-start → tool-call → tool-result →
    #   research-findings → review-verdict → token* → done
    stream = client.get(f"/chats/{chat_id}/stream")
    assert stream.status_code == 200

    # Every named event the orchestrator emits must appear.
    for event_name in (
        "iteration-start",
        "tool-call",
        "tool-result",
        "research-findings",
        "review-verdict",
        "token",
        "done",
    ):
        assert f"event: {event_name}" in stream.text, (
            f"missing SSE event '{event_name}' in stream payload"
        )

    # Order pins the orchestrator's contract. Note: `tool-call`
    # appears TWICE in the stream — first carrying the empty agentic
    # card shell (before iteration-start, as the OOB swap target),
    # then again for each actual tool invocation (after
    # iteration-start). We don't pin `tool-call`'s position because
    # the shell-emit precedes iteration-start by design. The
    # invariants that matter:
    #   - The iteration header lands before its findings (otherwise
    #     the findings row has no card to insert into).
    #   - Findings precede the review verdict for that iteration.
    #   - The verdict closes the loop before generation tokens stream.
    #   - `done` is last.
    indices = {
        name: stream.text.index(f"event: {name}")
        for name in (
            "iteration-start",
            "research-findings",
            "review-verdict",
            "token",
            "done",
        )
    }
    assert indices["iteration-start"] < indices["research-findings"]
    assert indices["research-findings"] < indices["review-verdict"]
    assert indices["review-verdict"] < indices["token"]
    assert indices["token"] < indices["done"]
    # Final assistant text appears in the done event's payload.
    assert "The answer is 42." in stream.text

    # 4. Persisted message sequence matches the agentic-mode shape.
    panel = client.get(f"/chats/{chat_id}")
    assert panel.status_code == 200
    # Historic render uses the agentic card template — the
    # `tool-card--agentic` modifier class is its distinguishing
    # marker vs. the single-agent shell.
    assert "tool-card--agentic" in panel.text
    # Iteration header surfaces as a <li> with the data-attribute.
    assert 'data-iteration="1"' in panel.text
    # Findings + passed-verdict markers are present.
    assert "tool-card__findings" in panel.text
    assert "tool-card__verdict--passed" in panel.text
    # Assistant bubble carries the final answer.
    assert "The answer is 42." in panel.text


def test_agentic_mode_dispatcher_falls_back_when_model_lacks_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with agentic mode globally on, a chat pinned to a
    non-tool-capable model uses the single-agent producer. The chat
    panel surfaces a banner explaining why.

    Companion to `test_agentic_mode_full_journey`'s happy path —
    pins the silent-fallback contract that's documented as a locked
    decision in `docs/plans/phase13-agentic-loop.md`.
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "chats.db"))
    monkeypatch.setenv("OLLAMA_HOST", "http://test")

    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "llama3"}]}
            )
        if request.url.path == "/api/show":
            # The crucial bit: no `tools` capability.
            return httpx.Response(
                200, json={"capabilities": ["completion"]}
            )
        body = _json.loads(request.content or b"{}")
        if body.get("stream"):
            return httpx.Response(
                200, content=_ndjson_chat(["plain ", "reply"])
            )
        return httpx.Response(
            200, json={"message": {"content": "", "tool_calls": []}}
        )

    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )

    from main import app

    saved_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_ollama_client] = lambda: mock_client

    try:
        with TestClient(app) as client:
            # Toggle agentic mode on globally.
            client.post(
                "/settings/agentic-mode",
                data={"enabled": "on"},
                headers={"HX-Request": "true"},
            )

            # Create a chat pinned to llama3 (which now lacks tools).
            created = client.post(
                "/chats",
                data={"model": "llama3", "content": "hello"},
            )
            assert created.status_code == 201
            chat_id = int(
                re.search(r'data-chat-id="(\d+)"', created.text).group(1)
            )

            # Drive the SSE stream — single-agent flow, plain tokens.
            stream = client.get(f"/chats/{chat_id}/stream")
            assert stream.status_code == 200
            # No agentic-only events should appear; the orchestrator
            # was never invoked.
            assert "event: iteration-start" not in stream.text
            assert "event: research-findings" not in stream.text
            assert "event: review-verdict" not in stream.text
            # The plain stream answer landed.
            assert "plain reply" in stream.text

            # Reload — the chat panel renders the agentic-skipped
            # banner naming the model.
            panel = client.get(f"/chats/{chat_id}")
            assert "chat-panel__agentic-skipped" in panel.text
            assert "llama3" in panel.text
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved_overrides)
