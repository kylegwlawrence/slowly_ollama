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

    # 3. Sidebar now shows the chat (placeholder name "New chat" —
    # phase 11d will auto-title later).
    chats = client.get("/chats")
    assert "New chat" in chats.text
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
        f'sse-connect="/chats/{chat_id}/regenerate-stream"'
        in regen_placeholder.text
    )

    # 8. Drive the regenerate stream.
    regen_stream = client.get(f"/chats/{chat_id}/regenerate-stream")
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
    assert ">New chat<" not in rename.text
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
