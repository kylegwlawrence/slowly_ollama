"""Tests for Phase 6: HTTP routes.

Each test gets a TestClient backed by a tempfile DB (via monkeypatch on
``DB_PATH``) and a mocked Ollama client (via ``app.dependency_overrides``
on ``get_ollama_client``). The Ollama mock is configured per test by
passing a handler to the ``make_client`` factory.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_ollama_client


def _ollama_unreachable(request: httpx.Request) -> httpx.Response:
    """Default mock handler — behaves as if Ollama isn't running.

    Tests that don't expect Ollama traffic use this so that any
    accidental call surfaces as a clear ConnectError -> 503 rather
    than a confusing test failure further down.
    """
    raise httpx.ConnectError("ollama mock: no handler set for this test")


# Type alias for readability in the fixture signature.
ClientFactory = Callable[
    [Callable[[httpx.Request], httpx.Response]], TestClient
]


@pytest.fixture
def make_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ClientFactory]:
    """Factory yielding a TestClient with a fresh DB and mocked Ollama.

    Yields:
        A callable that takes an Ollama mock handler and returns a
        TestClient. Dependency overrides are cleared at fixture
        teardown so tests don't leak overrides to one another.
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "chats.db"))
    # Override OLLAMA_HOST too — the lifespan calls create_client which
    # reads it. The real client is built but never used (we override
    # get_ollama_client below); pointing it at a sentinel host keeps
    # the lifespan from accidentally trying to resolve a real address.
    monkeypatch.setenv("OLLAMA_HOST", "http://test")

    from main import app

    def _make(handler: Callable[[httpx.Request], httpx.Response]) -> TestClient:
        mock_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://test",
        )
        app.dependency_overrides[get_ollama_client] = lambda: mock_client
        return TestClient(app)

    yield _make
    # Clear overrides between tests — the `app` object is a module-level
    # singleton imported once, so override state would otherwise leak.
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /api/models
# ---------------------------------------------------------------------------


def test_list_models_returns_names_from_ollama(
    make_client: ClientFactory,
) -> None:
    """GET /api/models surfaces just the names from Ollama's /api/tags."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={"models": [{"name": "llama3"}, {"name": "qwen2.5"}]},
        )

    with make_client(handler) as client:
        response = client.get("/api/models")

    assert response.status_code == 200
    assert response.json() == ["llama3", "qwen2.5"]


def test_list_models_returns_503_when_ollama_unreachable(
    make_client: ClientFactory,
) -> None:
    """OllamaUnavailable maps to HTTP 503 (Service Unavailable)."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/api/models")

    assert response.status_code == 503


def test_list_models_returns_502_on_ollama_protocol_error(
    make_client: ClientFactory,
) -> None:
    """OllamaProtocolError maps to HTTP 502 (Bad Gateway).

    The path through `app.ollama.list_models` wraps a JSONDecodeError
    as OllamaProtocolError; the route maps that to 502 so the UI can
    tell "Ollama answered garbage" apart from "Ollama isn't running."
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with make_client(handler) as client:
        response = client.get("/api/models")

    assert response.status_code == 502


# ---------------------------------------------------------------------------
# /api/conversations (CRUD)
# ---------------------------------------------------------------------------


def test_create_conversation_returns_201_with_row(
    make_client: ClientFactory,
) -> None:
    """POST /api/conversations creates the row and returns it as 201."""
    with make_client(_ollama_unreachable) as client:
        response = client.post(
            "/api/conversations",
            json={"name": "My chat", "model": "llama3"},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["id"] > 0
    assert body["name"] == "My chat"
    assert body["model"] == "llama3"


def test_list_conversations_orders_most_recent_first(
    make_client: ClientFactory,
) -> None:
    """GET /api/conversations mirrors queries.list_conversations order."""
    with make_client(_ollama_unreachable) as client:
        client.post(
            "/api/conversations", json={"name": "A", "model": "llama3"}
        )
        client.post(
            "/api/conversations", json={"name": "B", "model": "llama3"}
        )

        response = client.get("/api/conversations")

    assert response.status_code == 200
    convs = response.json()
    assert len(convs) == 2
    # B was created after A, so it sorts first by updated_at desc.
    assert convs[0]["name"] == "B"


def test_rename_conversation_returns_updated_row(
    make_client: ClientFactory,
) -> None:
    """PATCH /api/conversations/{id} updates the name."""
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            "/api/conversations", json={"name": "Old", "model": "llama3"}
        ).json()
        response = client.patch(
            f"/api/conversations/{created['id']}", json={"name": "New"}
        )

    assert response.status_code == 200
    assert response.json()["name"] == "New"


def test_rename_conversation_404_for_unknown_id(
    make_client: ClientFactory,
) -> None:
    """PATCH on a nonexistent id returns 404."""
    with make_client(_ollama_unreachable) as client:
        response = client.patch(
            "/api/conversations/999", json={"name": "X"}
        )
    assert response.status_code == 404


def test_delete_conversation_returns_204_and_removes_row(
    make_client: ClientFactory,
) -> None:
    """DELETE /api/conversations/{id} returns 204 and clears the row."""
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            "/api/conversations", json={"name": "X", "model": "llama3"}
        ).json()

        response = client.delete(f"/api/conversations/{created['id']}")
        assert response.status_code == 204

        # The row is gone; list comes back empty.
        listing = client.get("/api/conversations").json()
        assert listing == []


# ---------------------------------------------------------------------------
# /api/conversations/{id}/messages
# ---------------------------------------------------------------------------


def test_list_messages_returns_empty_list_for_new_conversation(
    make_client: ClientFactory,
) -> None:
    """A freshly-created conversation has no messages yet."""
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            "/api/conversations", json={"name": "X", "model": "llama3"}
        ).json()
        response = client.get(
            f"/api/conversations/{created['id']}/messages"
        )

    assert response.status_code == 200
    assert response.json() == []


def test_send_message_streams_assistant_reply_and_persists(
    make_client: ClientFactory,
) -> None:
    """POST /messages saves the user msg, streams the reply, saves it."""
    ndjson = (
        b'{"message":{"content":"Hello "},"done":false}\n'
        b'{"message":{"content":"world"},"done":false}\n'
        b'{"message":{"content":""},"done":true}\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(200, content=ndjson)

    with make_client(handler) as client:
        created = client.post(
            "/api/conversations", json={"name": "X", "model": "llama3"}
        ).json()

        response = client.post(
            f"/api/conversations/{created['id']}/messages",
            json={"content": "hi"},
        )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        # Each chunk arrives as a JSON-in-data SSE event; the stream
        # ends with a named "done" event so the frontend knows when to
        # stop listening.
        text = response.text
        assert '{"content": "Hello "}' in text
        assert '{"content": "world"}' in text
        assert "event: done" in text

        # The persisted state should be: 1 user message, 1 assistant
        # message with the full concatenated reply.
        messages = client.get(
            f"/api/conversations/{created['id']}/messages"
        ).json()
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hi"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "Hello world"


def test_send_message_404_for_unknown_conversation(
    make_client: ClientFactory,
) -> None:
    """POST /messages on a nonexistent conversation returns 404."""
    with make_client(_ollama_unreachable) as client:
        response = client.post(
            "/api/conversations/999/messages", json={"content": "hi"}
        )
    assert response.status_code == 404


def test_send_message_emits_sse_error_when_ollama_unreachable(
    make_client: ClientFactory,
) -> None:
    """Mid-stream Ollama failure → SSE error event, user msg still saved.

    The HTTP status is still 200 because headers were sent before
    Ollama was even called; the failure is reported inside the stream.
    """
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            "/api/conversations", json={"name": "X", "model": "llama3"}
        ).json()

        response = client.post(
            f"/api/conversations/{created['id']}/messages",
            json={"content": "hi"},
        )

        assert response.status_code == 200
        assert "event: error" in response.text

        # User message was saved before streaming started; no
        # assistant message was persisted because the stream errored.
        messages = client.get(
            f"/api/conversations/{created['id']}/messages"
        ).json()
        assert len(messages) == 1
        assert messages[0]["role"] == "user"


# ---------------------------------------------------------------------------
# /api/conversations/{id}/regenerate
# ---------------------------------------------------------------------------


def test_regenerate_replaces_last_assistant_message_in_place(
    make_client: ClientFactory,
) -> None:
    """Regenerate replaces the last assistant row keeping the same id."""
    first = (
        b'{"message":{"content":"Original"},"done":false}\n'
        b'{"message":{"content":""},"done":true}\n'
    )
    second = (
        b'{"message":{"content":"Regenerated"},"done":false}\n'
        b'{"message":{"content":""},"done":true}\n'
    )

    # MockTransport calls the handler once per HTTP request. We track
    # the call count to return different streams for the initial send
    # vs the regenerate.
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(
            200,
            content=first if call_count[0] == 1 else second,
        )

    with make_client(handler) as client:
        created = client.post(
            "/api/conversations", json={"name": "X", "model": "llama3"}
        ).json()

        # First send creates the initial assistant message.
        client.post(
            f"/api/conversations/{created['id']}/messages",
            json={"content": "hi"},
        )
        before = client.get(
            f"/api/conversations/{created['id']}/messages"
        ).json()
        assert before[-1]["content"] == "Original"
        original_id = before[-1]["id"]

        response = client.post(
            f"/api/conversations/{created['id']}/regenerate"
        )
        assert response.status_code == 200

        # Same row count, same assistant id, different content.
        after = client.get(
            f"/api/conversations/{created['id']}/messages"
        ).json()
        assert len(after) == 2
        assert after[-1]["id"] == original_id
        assert after[-1]["content"] == "Regenerated"


def test_regenerate_returns_400_when_no_assistant_message(
    make_client: ClientFactory,
) -> None:
    """Regenerate without an existing assistant message returns 400.

    Surfaces clearer than letting it fall through to the query layer's
    LookupError on an empty conversation.
    """
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            "/api/conversations", json={"name": "X", "model": "llama3"}
        ).json()

        response = client.post(
            f"/api/conversations/{created['id']}/regenerate"
        )

    assert response.status_code == 400
