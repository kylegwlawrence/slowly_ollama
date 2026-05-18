"""End-to-end integration test.

Walks the full single-chat user journey through `TestClient`:

    create → list → load → send → stream → regenerate → rename → delete

Catches gaps where individual routes pass their per-route unit tests
but don't wire together correctly across a real session.

Ollama is mocked via `httpx.MockTransport` per Phase 10's documented
strategy (see `tests/README.md`) — no real Ollama server contacted.
"""

import re
from collections.abc import Callable, Iterator
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

    chat_call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "llama3"}]}
            )
        # /api/chat
        chat_call_count[0] += 1
        if chat_call_count[0] == 1:
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
    """
    client = integration_client

    # 1. Index page renders with the empty-state.
    index = client.get("/")
    assert index.status_code == 200
    assert "empty-state" in index.text
    assert "chats-list" in index.text

    # 2. Create a chat. Response is the sidebar row + OOB swap for
    # #main + an HX-Push-Url header pointing at the new chat.
    created = client.post(
        "/chats", data={"name": "My journey", "model": "llama3"}
    )
    assert created.status_code == 201
    match = re.search(r'data-chat-id="(\d+)"', created.text)
    assert match is not None
    chat_id = int(match.group(1))
    assert 'class="chat-item"' in created.text
    assert 'hx-swap-oob="innerHTML"' in created.text
    assert created.headers.get("HX-Push-Url") == f"/chats/{chat_id}"

    # 3. Sidebar now shows the chat.
    chats = client.get("/chats")
    assert "My journey" in chats.text
    assert f'data-chat-id="{chat_id}"' in chats.text

    # 4. Reload-safe: a direct browser hit on /chats/{id} returns the
    # full index page with the chat preloaded.
    panel = client.get(f"/chats/{chat_id}")
    assert panel.status_code == 200
    assert "My journey" in panel.text
    assert "chat-panel" in panel.text

    # 5. Send a user message. Response is the user bubble + the
    # streaming-assistant placeholder (no streaming yet — that
    # happens on the GET below).
    sent = client.post(
        f"/chats/{chat_id}/messages", data={"content": "hello"}
    )
    assert sent.status_code == 200
    assert 'data-role="user"' in sent.text
    assert "hello" in sent.text
    assert (
        f'sse-connect="/chats/{chat_id}/stream"' in sent.text
    )

    # 6. Drive the SSE stream.
    stream = client.get(f"/chats/{chat_id}/stream")
    assert stream.status_code == 200
    assert "event: token" in stream.text
    assert "data: First " in stream.text
    assert "event: done" in stream.text

    # 7. Conversation now has both messages persisted.
    panel_after = client.get(f"/chats/{chat_id}")
    assert "hello" in panel_after.text
    assert "First reply" in panel_after.text

    # 8. Regenerate the last assistant response.
    regen_placeholder = client.post(f"/chats/{chat_id}/regenerate")
    assert regen_placeholder.status_code == 200
    assert (
        f'sse-connect="/chats/{chat_id}/regenerate-stream"'
        in regen_placeholder.text
    )

    # 9. Drive the regenerate stream.
    regen_stream = client.get(f"/chats/{chat_id}/regenerate-stream")
    assert regen_stream.status_code == 200
    assert "Regenerated " in regen_stream.text

    # 10. The assistant message has been replaced in place — new text
    # present, old text gone (regenerate replaces, doesn't append).
    panel_after_regen = client.get(f"/chats/{chat_id}")
    assert "Regenerated reply" in panel_after_regen.text
    assert "First reply" not in panel_after_regen.text

    # 11. Rename. PATCH returns the updated sidebar row in display
    # mode (no editing class, new name shown).
    rename = client.patch(
        f"/chats/{chat_id}", data={"name": "Renamed"}
    )
    assert rename.status_code == 200
    assert "Renamed" in rename.text
    assert "My journey" not in rename.text
    assert "chat-item--editing" not in rename.text

    # 12. Delete from the sidebar while not currently viewing this
    # chat (Referer = /). No HX-Location header in the response so
    # the user's current view stays intact.
    deleted = client.delete(
        f"/chats/{chat_id}", headers={"Referer": "http://test/"}
    )
    assert deleted.status_code == 200
    assert "HX-Location" not in deleted.headers

    # 13. Sidebar is empty again.
    chats_final = client.get("/chats")
    assert f'data-chat-id="{chat_id}"' not in chats_final.text


def test_delete_while_viewing_emits_hx_location(
    integration_client: TestClient,
) -> None:
    """Mirror of the journey's step 12 but with a Referer that points
    at the chat being deleted — the response carries HX-Location: /
    so HTMX navigates the user away from the 404'd URL.

    Worth a separate test from the full journey because the journey
    deletes from a different view; this is the "delete the chat
    you're looking at" path.
    """
    client = integration_client

    created = client.post(
        "/chats", data={"name": "DeleteMe", "model": "llama3"}
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
