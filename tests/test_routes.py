"""Tests for Phase 7 step 1: HTML-fragment routes.

Each test gets a TestClient backed by a tempfile DB (via monkeypatch on
``DB_PATH``) and a mocked Ollama client (via ``app.dependency_overrides``
on ``get_ollama_client``). The Ollama mock is configured per test by
passing a handler to the ``make_client`` factory. Assertions check for
specific HTML substrings — Jinja escapes content and the templates
include stable ``data-*`` attributes precisely so the tests have
something less brittle than full string equality to match against.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_ollama_client


def _ollama_unreachable(request: httpx.Request) -> httpx.Response:
    """Default mock — behave as if Ollama isn't running.

    Tests that don't expect Ollama traffic use this so an accidental
    call surfaces as a clear ConnectError → 503 rather than a confusing
    test failure further down.
    """
    raise httpx.ConnectError("ollama mock: no handler set for this test")


ClientFactory = Callable[
    [Callable[[httpx.Request], httpx.Response]], TestClient
]


@pytest.fixture
def make_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ClientFactory]:
    """Yield a factory that builds TestClients with a fresh DB + mock Ollama."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "chats.db"))
    monkeypatch.setenv("OLLAMA_HOST", "http://test")

    from main import app

    # Snapshot existing overrides so teardown restores exactly what
    # was there before this fixture touched anything.
    saved_overrides = dict(app.dependency_overrides)

    def _make(handler: Callable[[httpx.Request], httpx.Response]) -> TestClient:
        mock_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://test",
        )
        app.dependency_overrides[get_ollama_client] = lambda: mock_client
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved_overrides)


# ---------------------------------------------------------------------------
# /models
# ---------------------------------------------------------------------------


def test_models_returns_option_tags(make_client: ClientFactory) -> None:
    """GET /models renders Ollama's models as <option> tags."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={"models": [{"name": "llama3"}, {"name": "qwen2.5"}]},
        )

    with make_client(handler) as client:
        response = client.get("/models")

    assert response.status_code == 200
    assert 'value="llama3"' in response.text
    assert 'value="qwen2.5"' in response.text
    # Order matters — option tags should appear in Ollama's order.
    assert response.text.index("llama3") < response.text.index("qwen2.5")


def test_models_503_when_ollama_unreachable(
    make_client: ClientFactory,
) -> None:
    """OllamaUnavailable → 503."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/models")
    assert response.status_code == 503


def test_models_502_on_protocol_error(make_client: ClientFactory) -> None:
    """OllamaProtocolError → 502."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with make_client(handler) as client:
        response = client.get("/models")
    assert response.status_code == 502


# ---------------------------------------------------------------------------
# /chats (sidebar list + CRUD)
# ---------------------------------------------------------------------------


def test_list_chats_returns_ul_with_items(
    make_client: ClientFactory,
) -> None:
    """GET /chats returns a <ul> containing one <li> per conversation."""
    with make_client(_ollama_unreachable) as client:
        client.post("/chats", data={"name": "A", "model": "llama3"})
        client.post("/chats", data={"name": "B", "model": "llama3"})

        response = client.get("/chats")

    assert response.status_code == 200
    # Wrapper present.
    assert 'id="chats-list"' in response.text
    # Both items present, in most-recently-updated-first order.
    assert response.text.index(">B<") < response.text.index(">A<")


def test_create_chat_returns_201_with_chat_item(
    make_client: ClientFactory,
) -> None:
    """POST /chats creates the row and returns just that <li>."""
    with make_client(_ollama_unreachable) as client:
        response = client.post(
            "/chats", data={"name": "My chat", "model": "llama3"}
        )

    assert response.status_code == 201
    # The returned HTML is one row, not the whole list.
    assert "<ul" not in response.text
    assert 'class="chat-item"' in response.text
    assert "My chat" in response.text
    # Includes the data-chat-id attribute so HTMX can target later
    # rename/delete operations against this specific row.
    assert "data-chat-id=" in response.text


def _create_chat_and_get_id(client: TestClient, name: str = "Topic") -> int:
    """Create a chat via the route, return its id parsed from data-chat-id.

    Used by tests that need an existing conversation to act on; avoids
    duplicating the marker-parsing dance in every test body.
    """
    response = client.post("/chats", data={"name": name, "model": "llama3"})
    marker = 'data-chat-id="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return int(response.text[start:end])


def test_index_renders_layout_with_empty_main(
    make_client: ClientFactory,
) -> None:
    """GET / returns the full page with sidebar and an empty-state main."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/")

    assert response.status_code == 200
    # Page shell from base.html.
    assert "<!DOCTYPE html>" in response.text
    # Sidebar layout.
    assert 'class="sidebar"' in response.text
    assert 'id="chats-list"' in response.text
    # Empty state in main when no chat is loaded.
    assert "empty-state" in response.text
    assert 'class="chat-panel"' not in response.text


def test_index_includes_new_chat_form(
    make_client: ClientFactory,
) -> None:
    """The index page renders the new-chat form so users can create
    conversations from the UI (not just through curl)."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/")

    # Form posts to /chats and prepends the returned <li> into the
    # existing chats list.
    assert 'class="new-chat-form"' in response.text
    assert 'hx-post="/chats"' in response.text
    assert 'hx-target="#chats-list"' in response.text
    assert 'hx-swap="afterbegin"' in response.text
    # The two fields the POST /chats route expects via Form().
    assert 'name="name"' in response.text
    assert 'name="model"' in response.text


def test_new_chat_form_model_dropdown_auto_loads_from_models(
    make_client: ClientFactory,
) -> None:
    """The model <select> fetches /models on page load and swaps its
    innerHTML with the returned <option> tags. Without these
    attributes the dropdown would be permanently empty."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/")

    assert "<select" in response.text
    assert 'hx-get="/models"' in response.text
    assert 'hx-trigger="load"' in response.text
    # The placeholder is visible until /models responds.
    assert "Loading models" in response.text


def test_index_lists_existing_chats_in_sidebar(
    make_client: ClientFactory,
) -> None:
    """GET / populates the sidebar from the DB."""
    with make_client(_ollama_unreachable) as client:
        client.post("/chats", data={"name": "First", "model": "llama3"})
        client.post("/chats", data={"name": "Second", "model": "llama3"})

        response = client.get("/")

    assert "First" in response.text
    assert "Second" in response.text


def test_chat_url_direct_hit_renders_full_page_with_panel(
    make_client: ClientFactory,
) -> None:
    """A direct browser hit to /chats/{id} (no HX-Request) returns the
    full index page with the chat panel preloaded. This is the reload /
    bookmark / back-button path — the URL alone is enough to restore
    the same view.
    """
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "Topic")

        response = client.get(f"/chats/{chat_id}")

    assert response.status_code == 200
    # It's the full index page, not just a fragment.
    assert "<!DOCTYPE html>" in response.text
    assert 'class="sidebar"' in response.text
    # And the chat panel is preloaded into #main.
    assert 'class="chat-panel"' in response.text
    assert "Topic" in response.text
    # The empty-state placeholder is replaced by the chat panel.
    assert "empty-state" not in response.text


def test_chat_url_htmx_request_returns_fragment_only(
    make_client: ClientFactory,
) -> None:
    """GET /chats/{id} with HX-Request: true returns just the panel.

    HTMX adds this header on every request it fires. The branching
    keeps the fragment small (no <html>, no sidebar redraw) so the
    swap into #main stays cheap.
    """
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "Topic")

        response = client.get(
            f"/chats/{chat_id}", headers={"HX-Request": "true"}
        )

    assert response.status_code == 200
    # Just the fragment — no full-page wrapping.
    assert "<!DOCTYPE html>" not in response.text
    assert 'class="sidebar"' not in response.text
    # But the panel is there with its content.
    assert 'class="chat-panel"' in response.text
    assert "Topic" in response.text


def test_chat_url_direct_hit_404_for_unknown_id(
    make_client: ClientFactory,
) -> None:
    """A direct hit on a missing chat returns 404 regardless of branch."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/chats/999")
    assert response.status_code == 404


def test_chat_url_htmx_request_404_for_unknown_id(
    make_client: ClientFactory,
) -> None:
    """The HTMX branch also returns 404 for a missing id."""
    with make_client(_ollama_unreachable) as client:
        response = client.get(
            "/chats/999", headers={"HX-Request": "true"}
        )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Static assets (vendored Pico + HTMX)
# ---------------------------------------------------------------------------


def test_static_mount_serves_htmx(make_client: ClientFactory) -> None:
    """GET /static/htmx.min.js returns the vendored HTMX bundle.

    Guards the StaticFiles mount in main.py — if the mount path or
    the directory resolution breaks, this test catches it before the
    UI silently fails to load HTMX.
    """
    with make_client(_ollama_unreachable) as client:
        response = client.get("/static/htmx.min.js")

    assert response.status_code == 200
    # The first bytes of the file are distinctive enough to verify
    # we're serving the right asset (not, e.g., an index.html error
    # page from a misconfigured fallback).
    assert response.text.startswith("var htmx=function()")


def test_static_mount_serves_sse_extension(
    make_client: ClientFactory,
) -> None:
    """The htmx-ext-sse extension is served alongside HTMX core."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/static/htmx-ext-sse.js")

    assert response.status_code == 200
    assert "Server Sent Events Extension" in response.text


def test_static_mount_serves_pico_css(make_client: ClientFactory) -> None:
    """Pico CSS is served from the same /static mount."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/static/pico.classless.min.css")

    assert response.status_code == 200
    assert "Pico CSS" in response.text


def test_index_page_references_vendored_assets(
    make_client: ClientFactory,
) -> None:
    """base.html references the vendored URLs (not CDN/commented-out).

    Re-commenting the script tags during a future refactor would
    silently break the UI; this catches the regression at the route
    layer rather than at the browser.
    """
    with make_client(_ollama_unreachable) as client:
        response = client.get("/")

    assert "/static/pico.classless.min.css" in response.text
    assert "/static/htmx.min.js" in response.text
    assert "/static/htmx-ext-sse.js" in response.text


def test_chat_item_link_carries_href_and_hx_push_url(
    make_client: ClientFactory,
) -> None:
    """Sidebar links must work both with and without HTMX.

    The href powers normal browser navigation (and page reload). The
    hx-push-url tells HTMX to sync the URL with the swap, so the two
    paths converge on the same observable URL.
    """
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "X")
        response = client.get("/chats")

    assert f'href="/chats/{chat_id}"' in response.text
    assert 'hx-push-url="true"' in response.text


def test_rename_chat_returns_updated_item(
    make_client: ClientFactory,
) -> None:
    """PATCH /chats/{id} updates the name and returns the row."""
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            "/chats", data={"name": "Old", "model": "llama3"}
        )
        marker = 'data-chat-id="'
        start = created.text.index(marker) + len(marker)
        chat_id = int(created.text[start: created.text.index('"', start)])

        response = client.patch(
            f"/chats/{chat_id}", data={"name": "New"}
        )

    assert response.status_code == 200
    assert "New" in response.text
    assert "Old" not in response.text


def test_rename_chat_404_for_unknown_id(
    make_client: ClientFactory,
) -> None:
    """PATCH on a missing id returns 404."""
    with make_client(_ollama_unreachable) as client:
        response = client.patch("/chats/999", data={"name": "X"})
    assert response.status_code == 404


def test_delete_chat_returns_empty_200(
    make_client: ClientFactory,
) -> None:
    """DELETE /chats/{id} returns an empty body with status 200."""
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            "/chats", data={"name": "X", "model": "llama3"}
        )
        marker = 'data-chat-id="'
        start = created.text.index(marker) + len(marker)
        chat_id = int(created.text[start: created.text.index('"', start)])

        response = client.delete(f"/chats/{chat_id}")
        assert response.status_code == 200
        assert response.text == ""

        # Listing now omits the row.
        listing = client.get("/chats")
        assert f'data-chat-id="{chat_id}"' not in listing.text


# ---------------------------------------------------------------------------
# /chats/{id}/messages and /chats/{id}/stream
# ---------------------------------------------------------------------------


def test_send_message_returns_user_bubble_and_placeholder(
    make_client: ClientFactory,
) -> None:
    """POST /chats/{id}/messages returns the user bubble + SSE placeholder.

    No streaming happens in this response — the placeholder's
    sse-connect attribute triggers the streaming GET when HTMX inserts
    it into the DOM.
    """
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            "/chats", data={"name": "X", "model": "llama3"}
        )
        marker = 'data-chat-id="'
        start = created.text.index(marker) + len(marker)
        chat_id = int(created.text[start: created.text.index('"', start)])

        response = client.post(
            f"/chats/{chat_id}/messages", data={"content": "hello"}
        )

    assert response.status_code == 200
    # User bubble carries the role and content.
    assert 'data-role="user"' in response.text
    assert "hello" in response.text
    # Assistant placeholder with sse-connect to the streaming endpoint.
    assert 'data-role="assistant"' in response.text
    assert f'sse-connect="/chats/{chat_id}/stream"' in response.text


def test_send_message_404_for_unknown_conversation(
    make_client: ClientFactory,
) -> None:
    """POST on a missing chat returns 404 — no orphan user message."""
    with make_client(_ollama_unreachable) as client:
        response = client.post(
            "/chats/999/messages", data={"content": "hi"}
        )
    assert response.status_code == 404


def test_stream_endpoint_emits_token_and_done_events(
    make_client: ClientFactory,
) -> None:
    """GET /chats/{id}/stream emits SSE token events and a done event."""
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
            "/chats", data={"name": "X", "model": "llama3"}
        )
        marker = 'data-chat-id="'
        start = created.text.index(marker) + len(marker)
        chat_id = int(created.text[start: created.text.index('"', start)])

        # Save the user message first so the stream has something to
        # respond to.
        client.post(
            f"/chats/{chat_id}/messages", data={"content": "hi"}
        )

        response = client.get(f"/chats/{chat_id}/stream")

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    text = response.text
    # Each chunk is wrapped in a named "token" event with its content
    # in the data field (HTML-escaped).
    assert "event: token" in text
    assert "data: Hello " in text
    assert "data: world" in text
    # The stream finishes with a "done" event whose data contains the
    # final persisted message bubble.
    assert "event: done" in text
    assert 'data-role="assistant"' in text


def test_stream_endpoint_emits_error_event_when_ollama_unreachable(
    make_client: ClientFactory,
) -> None:
    """A mid-stream Ollama failure surfaces as SSE event: error."""
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            "/chats", data={"name": "X", "model": "llama3"}
        )
        marker = 'data-chat-id="'
        start = created.text.index(marker) + len(marker)
        chat_id = int(created.text[start: created.text.index('"', start)])

        client.post(
            f"/chats/{chat_id}/messages", data={"content": "hi"}
        )

        response = client.get(f"/chats/{chat_id}/stream")

    # HTTP status is still 200 — headers already sent before Ollama
    # was called. The failure is reported inside the stream.
    assert response.status_code == 200
    assert "event: error" in response.text
    assert "Ollama unavailable" in response.text


def test_stream_escapes_html_in_token_content(
    make_client: ClientFactory,
) -> None:
    """A token containing `<` or `&` is HTML-escaped before going on the wire.

    Without escaping, a model that emits `<script>` could break the
    page when the token swaps into the DOM.
    """
    ndjson = (
        b'{"message":{"content":"<b>boom</b>"},"done":false}\n'
        b'{"message":{"content":""},"done":true}\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ndjson)

    with make_client(handler) as client:
        created = client.post(
            "/chats", data={"name": "X", "model": "llama3"}
        )
        marker = 'data-chat-id="'
        start = created.text.index(marker) + len(marker)
        chat_id = int(created.text[start: created.text.index('"', start)])
        client.post(
            f"/chats/{chat_id}/messages", data={"content": "hi"}
        )

        response = client.get(f"/chats/{chat_id}/stream")

    # The raw `<b>` must not appear in the token data — only its
    # escaped form.
    assert "data: &lt;b&gt;boom&lt;/b&gt;" in response.text
    assert "data: <b>boom" not in response.text


# ---------------------------------------------------------------------------
# /chats/{id}/regenerate
# ---------------------------------------------------------------------------


def test_regenerate_returns_placeholder_for_replacement(
    make_client: ClientFactory,
) -> None:
    """POST /chats/{id}/regenerate returns a placeholder that replaces last bubble."""
    ndjson = (
        b'{"message":{"content":"First answer"},"done":false}\n'
        b'{"message":{"content":""},"done":true}\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ndjson)

    with make_client(handler) as client:
        created = client.post(
            "/chats", data={"name": "X", "model": "llama3"}
        )
        marker = 'data-chat-id="'
        start = created.text.index(marker) + len(marker)
        chat_id = int(created.text[start: created.text.index('"', start)])

        # Need an existing assistant message before regenerate is valid.
        client.post(
            f"/chats/{chat_id}/messages", data={"content": "hi"}
        )
        client.get(f"/chats/{chat_id}/stream")

        response = client.post(f"/chats/{chat_id}/regenerate")

    assert response.status_code == 200
    # The placeholder's sse-connect points at the regenerate stream,
    # not the normal one.
    assert (
        f'sse-connect="/chats/{chat_id}/regenerate-stream"' in response.text
    )


def test_regenerate_400_when_no_assistant_message(
    make_client: ClientFactory,
) -> None:
    """Regenerate without an assistant message yet → 400."""
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            "/chats", data={"name": "X", "model": "llama3"}
        )
        marker = 'data-chat-id="'
        start = created.text.index(marker) + len(marker)
        chat_id = int(created.text[start: created.text.index('"', start)])

        response = client.post(f"/chats/{chat_id}/regenerate")

    assert response.status_code == 400


def test_regenerate_stream_replaces_last_assistant_in_place(
    make_client: ClientFactory,
) -> None:
    """The regenerate stream replaces the existing assistant row (same id)."""
    first = (
        b'{"message":{"content":"Original"},"done":false}\n'
        b'{"message":{"content":""},"done":true}\n'
    )
    second = (
        b'{"message":{"content":"Regenerated"},"done":false}\n'
        b'{"message":{"content":""},"done":true}\n'
    )

    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(
            200, content=first if call_count[0] == 1 else second
        )

    with make_client(handler) as client:
        created = client.post(
            "/chats", data={"name": "X", "model": "llama3"}
        )
        marker = 'data-chat-id="'
        start = created.text.index(marker) + len(marker)
        chat_id = int(created.text[start: created.text.index('"', start)])

        client.post(
            f"/chats/{chat_id}/messages", data={"content": "hi"}
        )
        client.get(f"/chats/{chat_id}/stream")

        # Now regenerate: the stream's done event should contain the
        # updated message bubble with the same id but new content.
        response = client.get(f"/chats/{chat_id}/regenerate-stream")

    assert response.status_code == 200
    assert "Regenerated" in response.text
    # The done event's payload contains the persisted message bubble;
    # verifying it has the assistant role + the new content covers
    # the round-trip from stream → DB → render.
    assert 'data-role="assistant"' in response.text
