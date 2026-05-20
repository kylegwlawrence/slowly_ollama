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


# Phase 12d: the streaming codepath now makes TWO different Ollama
# requests per assistant turn — first a non-streaming `maybe_tool_call`
# probe to detect tool intent, then (when no tool is requested) a
# streaming `stream_chat`. Tests that only mock the streaming response
# would 500 on the probe; this helper builds a handler that routes the
# probe to a "no tool calls" JSON reply and the stream to whatever the
# test was already returning.
def _stream_handler(stream_body: bytes) -> Callable[
    [httpx.Request], httpx.Response
]:
    """Build a handler that branches on `stream` in the request body.

    Args:
        stream_body: The NDJSON bytes to return for the streaming call.

    Returns:
        An httpx MockTransport handler that returns an empty-tool_calls
        JSON object when ``stream=false`` and ``stream_body`` when
        ``stream=true``. Any other path (e.g. /api/tags) gets a 404 so
        misrouted traffic is obvious in the failure output.
    """
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/chat":
            # Surfaces as a clear assertion failure rather than a
            # mysterious 404 inside a generator.
            return httpx.Response(
                404, content=f"unexpected request to {request.url.path}".encode()
            )
        body = _json.loads(request.content or b"{}")
        if body.get("stream"):
            return httpx.Response(200, content=stream_body)
        # Non-streaming probe: no tool calls, empty content. This is
        # the "model decided no tool needed" branch maybe_tool_call
        # returns to its caller as (tool_calls=[], content="").
        return httpx.Response(
            200,
            json={"message": {"content": "", "tool_calls": []}},
        )

    return handler


ClientFactory = Callable[
    [Callable[[httpx.Request], httpx.Response]], TestClient
]


@pytest.fixture
def make_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ClientFactory]:
    """Yield a factory that builds TestClients with a fresh DB + mock Ollama.

    Also installs a default "healthy" stub for ``app.routes.probe_rag_health``
    so existing /settings/servers POST tests don't accidentally hit the real
    network. Tests that need to assert the health-check failure paths
    re-patch the same attribute in their own body.
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "chats.db"))
    monkeypatch.setenv("OLLAMA_HOST", "http://test")

    async def _default_healthy_probe(name: str, base_url: str) -> tuple[bool, str]:
        return (True, "")

    monkeypatch.setattr(
        "app.routes.probe_rag_health", _default_healthy_probe
    )

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


@pytest.fixture(autouse=True)
def _isolate_live_generations() -> Iterator[None]:
    """Snapshot + clear `generation.live_generations` around every test.

    Phase 12g retains done states in the registry so slow-reload
    replays still work, which means a finished gen from a previous
    test would otherwise sit in the dict and confuse a same-id
    conversation in the next test. Autouse so cancellation tests
    that bypass `make_client` (driving start_generation directly)
    also benefit — no per-test manual `.pop()` needed.
    """
    from app import generation as _generation
    saved = dict(_generation.live_generations)
    _generation.live_generations.clear()
    yield
    _generation.live_generations.clear()
    _generation.live_generations.update(saved)


@pytest.fixture(autouse=True)
def _isolate_tool_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Reset the capability cache and default ``model_supports_tools`` to True.

    Phase 12f gates ``tools=`` on Ollama's reported capability for the
    chat's model. Route tests that exercise the streaming or
    tool-calling paths assume the chat's model IS tool-capable;
    without this default-True stub every handler in this file would
    have to mock ``/api/show`` in addition to its ``/api/tags`` and
    ``/api/chat`` responses. Two carve-outs:

    - Tests that assert the DROPDOWN filter go through
      ``list_tool_capable_models`` directly (it's what ``/models``
      now calls), which bypasses this stub. Those tests mock
      ``/api/show`` in their own handlers and exercise the real
      filter.
    - Tests that want to verify the generation-side fallback
      (``tools_payload = None`` when the model isn't capable)
      re-patch ``model_supports_tools`` themselves with their own
      monkeypatch.
    """
    from app import ollama as _ollama
    _ollama.reset_capability_cache()

    async def _capable(_client: object, _name: str) -> bool:
        return True

    monkeypatch.setattr(_ollama, "model_supports_tools", _capable)
    yield
    _ollama.reset_capability_cache()


# ---------------------------------------------------------------------------
# /models
# ---------------------------------------------------------------------------


def test_models_returns_option_tags(make_client: ClientFactory) -> None:
    """GET /models renders Ollama's tool-capable models as <option> tags.

    Phase 12f added the /api/show capability filter, so the route now
    fans out one POST per /api/tags entry. Both models in this test
    advertise 'tools' so the rendered fragment matches the pre-12f
    behaviour — order preserved from /api/tags.
    """
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "llama3"}, {"name": "qwen2.5"}]},
            )
        assert request.url.path == "/api/show", request.url.path
        body = _json.loads(request.content)
        assert body["model"] in {"llama3", "qwen2.5"}
        return httpx.Response(
            200, json={"capabilities": ["completion", "tools"]}
        )

    with make_client(handler) as client:
        response = client.get("/models")

    assert response.status_code == 200
    assert 'value="llama3"' in response.text
    assert 'value="qwen2.5"' in response.text
    # Order matters — option tags should appear in Ollama's order.
    assert response.text.index("llama3") < response.text.index("qwen2.5")


def test_models_excludes_non_tool_capable_models(
    make_client: ClientFactory,
) -> None:
    """Models whose /api/show capabilities lack 'tools' don't appear.

    The dropdown is the user's only path to picking a chat model; if
    we listed non-tool-capable ones (embedding/reranker/older chat
    models), submitting would 400 against Ollama because every chat
    request ships with tools=[...]. 12f filters the list upstream so
    that footgun goes away.
    """
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [
                    {"name": "llama3.1:8b"},
                    {"name": "nomic-embed-text:latest"},
                    {"name": "qwen2.5:7b"},
                ]},
            )
        assert request.url.path == "/api/show", request.url.path
        body = _json.loads(request.content)
        # The third model gets ["embedding", "tools"] — Ollama actually
        # reports this for the user's `qwen3-reranker` install, and
        # the filter must NOT trust the `tools` flag on a model that's
        # missing `completion`.
        if body["model"] == "llama3.1:8b":
            caps = ["completion", "tools"]
        elif body["model"] == "qwen2.5:7b":
            caps = ["completion", "tools"]
        else:  # nomic-embed-text:latest — emulate a real embedder
            caps = ["embedding"]
        return httpx.Response(200, json={"capabilities": caps})

    with make_client(handler) as client:
        response = client.get("/models")

    assert response.status_code == 200
    assert 'value="llama3.1:8b"' in response.text
    assert 'value="qwen2.5:7b"' in response.text
    # The embedding model and its placeholder name MUST NOT appear in
    # any rendered <option> — neither as a value nor as visible text.
    assert "nomic-embed-text" not in response.text


def test_models_returns_disabled_option_when_ollama_unreachable(
    make_client: ClientFactory,
) -> None:
    """When Ollama is down, /models returns 200 with a disabled option.

    Returning 5xx would leave the dropdown stuck at "Loading…"
    because HTMX won't swap on a non-2xx response. A 200 with a
    disabled option lets HTMX swap normally and shows the user a
    clear message; the empty value + the form's `required` still
    block submission.
    """
    with make_client(_ollama_unreachable) as client:
        response = client.get("/models")

    assert response.status_code == 200
    assert '<option value="" disabled>' in response.text
    assert "unreachable" in response.text.lower()


def test_models_returns_disabled_option_on_protocol_error(
    make_client: ClientFactory,
) -> None:
    """A protocol-level Ollama failure also surfaces as a disabled
    option (200 with text), not a 502 — same rationale as the
    unreachable case."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with make_client(handler) as client:
        response = client.get("/models")

    assert response.status_code == 200
    assert '<option value="" disabled>' in response.text
    assert "unexpected" in response.text.lower()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_placeholder_name_takes_first_non_empty_line_capped_at_40() -> None:
    """`_placeholder_name` derives a sidebar-friendly label from message
    content: first non-empty line, trimmed, max 40 chars. Empty input
    falls back to "New chat" so the sidebar never shows an empty row."""
    from app.routes import _placeholder_name

    # Single short line — passes through unchanged (just trimmed).
    assert _placeholder_name("Hello there") == "Hello there"
    assert _placeholder_name("  surrounded by spaces  ") == "surrounded by spaces"

    # Multi-line: leading blanks skipped, first content line wins.
    assert _placeholder_name("\n\nFirst content\nSecond line") == "First content"

    # Long content truncates at 40 chars.
    long = "x" * 100
    assert len(_placeholder_name(long)) == 40

    # Empty / whitespace-only falls back to the generic name.
    assert _placeholder_name("") == "New chat"
    assert _placeholder_name("   \n  \t  \n") == "New chat"


# ---------------------------------------------------------------------------
# /chats (sidebar list + CRUD)
# ---------------------------------------------------------------------------


def test_list_chats_returns_ul_with_items(
    make_client: ClientFactory,
) -> None:
    """GET /chats returns a <ul> containing one <li> per conversation.

    With the composer flow, chats are created with the placeholder
    name "New chat" so we can't distinguish A vs. B by name; instead
    we count rows and verify the most-recently-created one appears
    first (the route sorts by updated_at DESC).
    """
    with make_client(_ollama_unreachable) as client:
        first_id = _create_chat_and_get_id(client, "first")
        second_id = _create_chat_and_get_id(client, "second")

        response = client.get("/chats")

    assert response.status_code == 200
    # Wrapper present.
    assert 'id="chats-list"' in response.text
    # Both items present, second-created first (most recent → top).
    assert (
        response.text.index(f'data-chat-id="{second_id}"')
        < response.text.index(f'data-chat-id="{first_id}"')
    )


def test_create_chat_returns_201_with_panel_and_oob_row(
    make_client: ClientFactory,
) -> None:
    """POST /chats creates a chat AND saves the first user message.

    The response is composed of two fragments:
    - The rendered chat panel (replaces #main via the composer's
      hx-target="#main").
    - The new sidebar row marked hx-swap-oob="afterbegin:#chats-list"
      so HTMX prepends it to the existing list.

    Both pieces must land in a single response — the composer's
    job is to start a conversation, not to create an empty shell.
    """
    with make_client(_ollama_unreachable) as client:
        response = client.post(
            "/chats", data={"model": "llama3", "content": "hello there"}
        )

    assert response.status_code == 201
    # Main-target fragment: the chat panel with the user's first
    # message AND an inline assistant placeholder waiting on SSE.
    assert 'class="chat-panel"' in response.text
    assert "hello there" in response.text
    assert 'data-role="user"' in response.text
    assert 'data-role="assistant"' in response.text
    assert "sse-connect=" in response.text
    # OOB sidebar row: marked for the chats-list with the selector
    # form. The bare hx-swap-oob="true" wouldn't work because we
    # need afterbegin against a parent <ul>.
    assert 'hx-swap-oob="afterbegin:#chats-list"' in response.text
    assert 'data-chat-id=' in response.text
    # URL push so reload restores the new chat's view.
    assert response.headers["HX-Push-Url"].startswith("/chats/")


def _create_chat_and_get_id(
    client: TestClient, content: str = "first message"
) -> int:
    """Create a chat via POST /chats and return its id.

    Phase 12g: POST /chats spawns a generation task as a side
    effect, so by the time this helper returns, the test's mocked
    Ollama has been consumed by one generation cycle (one probe +
    one stream). Tests that just want to observe the generation's
    SSE output can then GET /stream and `consume_generation` will
    replay the events.

    For tests that need to control the mock's first probe directly
    (typically tool tests that want the probe to return a tool_call
    on call #1), use `_create_chat_db_only` instead — that bypasses
    the generation spawn and lets the test's first probe arrive via
    POST /messages.
    """
    response = client.post(
        "/chats", data={"model": "llama3", "content": content}
    )
    marker = 'data-chat-id="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return int(response.text[start:end])


def _create_chat_db_only(content: str = "first message") -> int:
    """Create a chat row directly in the DB without spawning a generation.

    Phase 12g: POST /chats spawns a generation as a side effect,
    which consumes the test's mock probes. Tests that want the
    mock to be consumed only by their explicit POST /messages call
    (e.g., tool tests where the first probe should return a
    tool_call) use this helper instead of `_create_chat_and_get_id`.
    """
    import os

    from app import queries
    from app.connection import open_connection

    db_path = os.environ["DB_PATH"]
    with open_connection(db_path) as conn:
        chat = queries.create_conversation(
            conn, name=content[:40] or "New chat", model="llama3"
        )
        queries.append_message(conn, chat.id, "user", content)
    return chat.id


def test_index_renders_layout_with_composer(
    make_client: ClientFactory,
) -> None:
    """GET / returns the full page: sidebar + empty-state composer."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/")

    assert response.status_code == 200
    # Page shell from base.html.
    assert "<!DOCTYPE html>" in response.text
    # Sidebar layout.
    assert 'class="sidebar"' in response.text
    assert 'id="chats-list"' in response.text
    # Composer takes the main area when no chat is loaded.
    assert 'class="composer"' in response.text
    assert 'class="chat-panel"' not in response.text
    # Sidebar "+ New chat" affordance for returning to the composer
    # from inside an existing chat.
    assert 'class="sidebar__new-chat"' in response.text


def test_index_includes_composer_form(
    make_client: ClientFactory,
) -> None:
    """The composer posts to /chats with model + content (no name).

    Phase 11b removed the standalone "Compose" disclosure + named
    new-chat form in favour of a Claude-style empty-state composer
    that starts a conversation in one round trip.
    """
    with make_client(_ollama_unreachable) as client:
        response = client.get("/")

    assert 'class="composer__form"' in response.text
    assert 'hx-post="/chats"' in response.text
    # The composer replaces #main with the new chat panel; the OOB
    # sidebar row is delivered separately by the server.
    assert 'hx-target="#main"' in response.text
    # The composer's <form> MUST NOT carry hx-push-url. HTMX inherits
    # that attribute onto the descendant <select hx-get="/models">,
    # which would push `/models?model=` into the address bar on
    # initial load. URL syncing for chat creation is driven by the
    # server's HX-Push-Url header in POST /chats responses instead.
    composer_start = response.text.index('class="composer__form"')
    composer_end = response.text.index("</form>", composer_start)
    composer_form = response.text[composer_start:composer_end]
    assert "hx-push-url" not in composer_form
    # The two fields POST /chats now requires via Form().
    assert 'name="content"' in response.text
    assert 'name="model"' in response.text
    # The old Compose disclosure is gone.
    assert 'class="compose__button"' not in response.text
    assert 'class="new-chat-form"' not in response.text


def test_composer_model_dropdown_auto_loads_from_models(
    make_client: ClientFactory,
) -> None:
    """The composer's model <select> fetches /models on page load and
    swaps its innerHTML with the returned <option> tags. Without
    these attributes the dropdown would be permanently empty."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/")

    assert "<select" in response.text
    assert 'hx-get="/models"' in response.text
    assert 'hx-trigger="load"' in response.text
    # The placeholder is visible until /models responds.
    assert "Loading models" in response.text


def test_new_route_returns_composer_fragment(
    make_client: ClientFactory,
) -> None:
    """GET /new returns just the composer fragment (no <html> shell).

    Wired to the sidebar "+ New chat" link via hx-get so it can swap
    into #main without re-rendering the sidebar.
    """
    with make_client(_ollama_unreachable) as client:
        response = client.get("/new")

    assert response.status_code == 200
    assert "<!DOCTYPE html>" not in response.text
    assert 'class="sidebar"' not in response.text
    assert 'class="composer"' in response.text
    assert 'hx-post="/chats"' in response.text


def test_index_lists_existing_chats_in_sidebar(
    make_client: ClientFactory,
) -> None:
    """GET / populates the sidebar from the DB.

    The placeholder name is derived from each chat's first user
    message (first non-empty line, capped at 40 chars), so two chats
    created with different content render with different names.
    """
    with make_client(_ollama_unreachable) as client:
        _create_chat_and_get_id(client, "first")
        _create_chat_and_get_id(client, "second")

        response = client.get("/")

    assert response.text.count('class="chat-item"') == 2
    # Each row's link text is the message-derived placeholder.
    assert ">first<" in response.text
    assert ">second<" in response.text


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
    # The helper passes "Topic" as the first message content, so it
    # should render as a user bubble inside the panel.
    assert "Topic" in response.text
    # When viewing a chat, the empty-state composer must NOT render —
    # otherwise both would appear stacked. (Confirms the index
    # template's {% if conversation %} branching.)
    assert 'class="composer"' not in response.text


def test_base_disables_message_button_while_streaming(
    make_client: ClientFactory,
) -> None:
    """The CSS rule that soft-disables the send button is on every page.

    The rule uses :has() to match when a `.message--streaming`
    placeholder is in the DOM. Removing it would re-introduce the
    double-submit bug; this test catches that.
    """
    with make_client(_ollama_unreachable) as client:
        response = client.get("/")

    assert ".chat-panel:has(.message--streaming) .message-form button" in (
        response.text
    )
    assert "pointer-events: none" in response.text


def test_chat_panel_auto_scrolls_to_bottom(
    make_client: ClientFactory,
) -> None:
    """Long conversations open at the latest message, not the top.

    Two contracts must hold together:
    - The chat panel mounts `#messages` (the scroll target).
    - app.js delegates an htmx:afterSwap listener that scrolls the
      bottom into view on every swap into `#main` (chat-panel mount,
      no swap fires inside #messages) or `#messages` (streaming tokens,
      newly-sent messages).
    """
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "X")
        response = client.get(f"/chats/{chat_id}")
        js = client.get("/static/app.js").text

    # Markup contract: the scroll target exists.
    assert 'id="messages"' in response.text
    # Behaviour contract: app.js holds the scroll logic and is wired
    # to the right swap targets.
    assert "scrollMessagesToBottom" in js
    assert "m.scrollTop = m.scrollHeight" in js
    assert "htmx:afterSwap" in js


def test_chat_panel_form_only_resets_on_successful_response(
    make_client: ClientFactory,
) -> None:
    """Resetting the textarea must be gated on a successful response.

    Without the `event.detail.successful` guard, a failed POST (e.g.
    the conversation was deleted in another tab → 404) would wipe the
    user's typed message and leave them with no indication of what
    happened. The reset lives in app.js's `.message-form` branch of
    the htmx:afterRequest handler.
    """
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "X")
        response = client.get(f"/chats/{chat_id}")
        js = client.get("/static/app.js").text

    # The form app.js targets is in the rendered panel.
    assert 'class="message-form"' in response.text
    # And app.js still gates the reset on success — `if (!e.detail.successful) return;`
    # before the form.reset() call.
    assert ".message-form" in js
    assert "e.detail.successful" in js
    assert "form.reset()" in js


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
    assert "/static/app.js" in response.text


def test_chat_item_has_delete_button(
    make_client: ClientFactory,
) -> None:
    """Each sidebar row has a delete button wired to DELETE /chats/{id}.

    Must include hx-confirm (browser prompt) and hx-swap="delete"
    (remove the row from the DOM). The "navigate away when viewing
    the deleted chat" behavior used to live in inline JS on this
    button; it's now server-side (see
    `test_delete_chat_emits_hx_location_when_viewing_deleted_chat`).
    """
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "Topic")
        response = client.get("/chats")

    assert f'hx-delete="/chats/{chat_id}"' in response.text
    assert 'hx-swap="delete"' in response.text
    assert "hx-confirm=" in response.text


def test_delete_chat_emits_hx_location_when_viewing_deleted_chat(
    make_client: ClientFactory,
) -> None:
    """When Referer points at the chat being deleted, the response
    carries HX-Location: / so HTMX navigates the page away from the
    now-404'd URL."""
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "Topic")
        response = client.delete(
            f"/chats/{chat_id}",
            headers={"Referer": f"http://test/chats/{chat_id}"},
        )

    assert response.status_code == 200
    assert response.headers.get("HX-Location") == "/"


def test_delete_chat_omits_hx_location_when_viewing_different_chat(
    make_client: ClientFactory,
) -> None:
    """No HX-Location when the user is on a different chat (or no
    chat). Avoids redirecting them away from a chat they're still
    using."""
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "Topic")
        response = client.delete(
            f"/chats/{chat_id}",
            headers={"Referer": "http://test/"},
        )

    assert response.status_code == 200
    assert "HX-Location" not in response.headers


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


def test_chat_item_has_rename_button(
    make_client: ClientFactory,
) -> None:
    """Each sidebar row has a rename button that fetches the edit
    fragment via GET /chats/{id}/edit and swaps the row into edit mode."""
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "Topic")
        response = client.get("/chats")

    assert "chat-item__rename" in response.text
    assert f'hx-get="/chats/{chat_id}/edit"' in response.text


def test_get_chat_edit_returns_edit_fragment(
    make_client: ClientFactory,
) -> None:
    """GET /chats/{id}/edit returns the row in edit mode (form + input)."""
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client)

        response = client.get(f"/chats/{chat_id}/edit")

    assert response.status_code == 200
    # The edit fragment carries the same id (it's an outerHTML swap
    # target on the existing row) but a distinguishing class.
    assert f'id="chat-{chat_id}"' in response.text
    assert "chat-item--editing" in response.text
    # The edit form pre-fills the current name. With no arg, the
    # helper sends content="first message", so the placeholder name
    # derived from that message is what the form starts with.
    assert f'hx-patch="/chats/{chat_id}"' in response.text
    assert 'name="name"' in response.text
    assert 'value="first message"' in response.text


def test_get_chat_edit_404_for_unknown_id(
    make_client: ClientFactory,
) -> None:
    """Editing a non-existent chat returns 404."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/chats/999/edit")
    assert response.status_code == 404


def test_get_chat_item_returns_display_fragment(
    make_client: ClientFactory,
) -> None:
    """GET /chats/{id}/item returns the row in display mode.

    Used by the Cancel button in the edit fragment to swap back
    without saving.
    """
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client)

        response = client.get(f"/chats/{chat_id}/item")

    assert response.status_code == 200
    assert "chat-item" in response.text
    # No edit form in the display fragment.
    assert "chat-item--editing" not in response.text
    # Placeholder name derived from the first message ("first message"
    # is the helper's default content).
    assert "first message" in response.text


def test_get_chat_item_404_for_unknown_id(
    make_client: ClientFactory,
) -> None:
    """Display fragment for a non-existent chat returns 404."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/chats/999/item")
    assert response.status_code == 404


def test_rename_round_trip_via_edit_and_patch(
    make_client: ClientFactory,
) -> None:
    """End-to-end rename round-trip at the HTTP layer.

    Mirrors what the browser-side kebab→Rename→type→submit flow
    triggers: GET /chats/{id}/edit returns the edit fragment with
    the current name pre-filled; PATCH /chats/{id} with a new name
    returns the display fragment showing the new name.

    A user-reported "renaming doesn't work" bug in Phase 9 surfaced
    only in the browser — the HTTP layer was correct. This test
    catches future regressions that *would* affect the HTTP layer
    (e.g. a route change that breaks the body parsing of the PATCH
    form data).
    """
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client)

        edit_response = client.get(f"/chats/{chat_id}/edit")
        assert edit_response.status_code == 200
        assert "chat-item--editing" in edit_response.text
        # Placeholder name comes from the first message ("first message"
        # is the helper's default content); the edit form pre-fills it.
        assert 'value="first message"' in edit_response.text
        assert f'hx-patch="/chats/{chat_id}"' in edit_response.text

        patch_response = client.patch(
            f"/chats/{chat_id}", data={"name": "Renamed Topic"}
        )
        assert patch_response.status_code == 200
        # Use bracket-anchored substrings so "Renamed" doesn't also
        # match part of "Renamed Topic" — we want the post-rename
        # name visible as link text and the original placeholder gone.
        assert ">Renamed Topic<" in patch_response.text
        assert ">first message<" not in patch_response.text
        # Came back as display fragment, not edit fragment.
        assert "chat-item--editing" not in patch_response.text


def test_rename_chat_returns_updated_item(
    make_client: ClientFactory,
) -> None:
    """PATCH /chats/{id} updates the name and returns the row."""
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client)

        response = client.patch(
            f"/chats/{chat_id}", data={"name": "Renamed"}
        )

    assert response.status_code == 200
    assert ">Renamed<" in response.text
    # The placeholder name (derived from the first message — "first
    # message" with the helper's default content) should be gone after
    # the rename.
    assert ">first message<" not in response.text


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
            "/chats", data={"model": "llama3", "content": "hi"}
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
            "/chats", data={"model": "llama3", "content": "hi"}
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
    # Phase 12d: the stream codepath calls Ollama twice — once
    # non-streaming (tool-call probe) and once streaming (final reply).
    # _stream_handler routes the probe to a no-tool-calls JSON object.
    handler = _stream_handler(ndjson)

    with make_client(handler) as client:
        created = client.post(
            "/chats", data={"model": "llama3", "content": "hi"}
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
    # final persisted message bubble carrying hx-swap-oob — that's
    # what tells HTMX to replace the streaming placeholder rather
    # than nest the final bubble inside it.
    assert "event: done" in text
    assert 'data-role="assistant"' in text
    assert (
        f'hx-swap-oob="outerHTML:#assistant-stream-{chat_id}"' in text
    )


def test_stream_endpoint_emits_error_event_when_ollama_unreachable(
    make_client: ClientFactory,
) -> None:
    """A mid-stream Ollama failure surfaces as SSE event: error."""
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            "/chats", data={"model": "llama3", "content": "hi"}
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
    handler = _stream_handler(ndjson)

    with make_client(handler) as client:
        created = client.post(
            "/chats", data={"model": "llama3", "content": "hi"}
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


def test_stream_endpoint_404_for_unknown_conversation(
    make_client: ClientFactory,
) -> None:
    """GET /chats/999/stream on a missing chat returns 404 before
    opening the SSE connection. Without the 404, an EventSource that
    bound to a stale chat id would error in the browser confusingly."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/chats/999/stream")
    assert response.status_code == 404


def test_stream_endpoint_emits_protocol_error_for_malformed_ollama(
    make_client: ClientFactory,
) -> None:
    """Ollama returning garbage NDJSON mid-stream surfaces as an SSE
    `error` event whose payload mentions a protocol error. Without
    this branch the OllamaProtocolError would crash the generator
    silently from the browser's perspective."""
    body = (
        b'{"message":{"content":"OK"},"done":false}\n'
        b'this is not valid json\n'
    )
    # 12d: the probe (stream=false) succeeds with no tool_calls so the
    # loop falls through to streaming; the garbage NDJSON then trips
    # OllamaProtocolError as before.
    handler = _stream_handler(body)

    with make_client(handler) as client:
        chat_id = _create_chat_and_get_id(client, "X")
        client.post(
            f"/chats/{chat_id}/messages", data={"content": "hi"}
        )

        response = client.get(f"/chats/{chat_id}/stream")

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "protocol error" in response.text.lower()


# ---------------------------------------------------------------------------
# /chats/{id}/regenerate
# ---------------------------------------------------------------------------


def test_assistant_message_bubble_has_regenerate_button(
    make_client: ClientFactory,
) -> None:
    """Every assistant bubble carries a regenerate button.

    CSS in base.html hides all but the last one; the button itself
    is always rendered so the SSE done event's payload contains it
    automatically (the regenerated message has to also be
    re-regeneratable).
    """
    ndjson = (
        b'{"message":{"content":"Hi"},"done":false}\n'
        b'{"message":{"content":""},"done":true}\n'
    )
    handler = _stream_handler(ndjson)

    with make_client(handler) as client:
        chat_id = _create_chat_and_get_id(client, "X")
        client.post(
            f"/chats/{chat_id}/messages", data={"content": "hi"}
        )
        # Drive the stream so the assistant message is persisted.
        client.get(f"/chats/{chat_id}/stream")

        response = client.get(f"/chats/{chat_id}")

    # The button targets the existing message bubble for replacement
    # (outerHTML swap) and POSTs to the regenerate endpoint.
    assert "message__regenerate" in response.text
    assert f'hx-post="/chats/{chat_id}/regenerate"' in response.text
    assert 'hx-target="closest .message"' in response.text


def test_user_message_bubble_has_no_regenerate_button(
    make_client: ClientFactory,
) -> None:
    """Only assistant bubbles get the regenerate button.

    Regenerating a user message makes no semantic sense — it'd be
    asking the model to "redo" the user's own input.
    """
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "X")
        # Save just a user message (no Ollama call) by going through
        # POST /chats/{id}/messages — it returns the user bubble plus
        # an SSE placeholder; we only check the user bubble.
        response = client.post(
            f"/chats/{chat_id}/messages", data={"content": "hi"}
        )

    # The user bubble should NOT contain the regenerate button.
    # (The response also contains the streaming placeholder, but
    # that's a separate <div> and has no regen button either.)
    user_section = response.text.split('data-role="assistant"')[0]
    assert "message__regenerate" not in user_section


def test_base_css_hides_regenerate_except_on_last_assistant(
    make_client: ClientFactory,
) -> None:
    """The CSS rule that conditionally shows the regenerate button is
    on every page — removing it would make the button show on every
    assistant bubble in a conversation, which would be confusing."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/")

    # Default-hidden rule.
    assert ".message__regenerate { display: none; }" in response.text
    # Conditional override targeting only the last non-streaming
    # assistant bubble.
    assert ":last-child.message--assistant:not(.message--streaming)" in (
        response.text
    )


def test_regenerate_returns_placeholder_for_replacement(
    make_client: ClientFactory,
) -> None:
    """POST /chats/{id}/regenerate returns a placeholder that replaces last bubble."""
    ndjson = (
        b'{"message":{"content":"First answer"},"done":false}\n'
        b'{"message":{"content":""},"done":true}\n'
    )
    handler = _stream_handler(ndjson)

    with make_client(handler) as client:
        created = client.post(
            "/chats", data={"model": "llama3", "content": "hi"}
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
        f'sse-connect="/chats/{chat_id}/stream"' in response.text
    )


def test_regenerate_400_when_no_assistant_message(
    make_client: ClientFactory,
) -> None:
    """Regenerate without an assistant message yet → 400."""
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            "/chats", data={"model": "llama3", "content": "hi"}
        )
        marker = 'data-chat-id="'
        start = created.text.index(marker) + len(marker)
        chat_id = int(created.text[start: created.text.index('"', start)])

        response = client.post(f"/chats/{chat_id}/regenerate")

    assert response.status_code == 400


def test_regenerate_404_for_unknown_conversation(
    make_client: ClientFactory,
) -> None:
    """POST /chats/999/regenerate on a missing chat returns 404."""
    with make_client(_ollama_unreachable) as client:
        response = client.post("/chats/999/regenerate")
    assert response.status_code == 404


def test_regenerate_stream_404_for_unknown_conversation(
    make_client: ClientFactory,
) -> None:
    """GET /chats/999/stream on a missing chat returns 404."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/chats/999/stream")
    assert response.status_code == 404


# Phase 12g: the previous regenerate-stream 400 defense was removed when
# /chats/{id}/regenerate-stream was collapsed into /chats/{id}/stream.
# The 400 still fires from POST /regenerate (see the dedicated test for
# that route below); the GET side just dispatches on the registry and
# has no notion of "regenerate vs new-message" intent. The collapsed
# design + the POST-side check is sufficient.


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

    # 12d: each assistant turn now triggers two requests — the
    # tool-call probe (stream=false) and the streaming reply. We only
    # vary the streaming body between the original and the regenerate
    # turn; the probe always returns no tool_calls.
    stream_count = [0]
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content or b"{}")
        if not body.get("stream"):
            return httpx.Response(
                200,
                json={"message": {"content": "", "tool_calls": []}},
            )
        stream_count[0] += 1
        return httpx.Response(
            200, content=first if stream_count[0] == 1 else second
        )

    with make_client(handler) as client:
        created = client.post(
            "/chats", data={"model": "llama3", "content": "hi"}
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
        response = client.get(f"/chats/{chat_id}/stream")

    assert response.status_code == 200
    assert "Regenerated" in response.text
    # The done event's payload contains the persisted message bubble;
    # verifying it has the assistant role + the new content covers
    # the round-trip from stream → DB → render.
    assert 'data-role="assistant"' in response.text


# ---------------------------------------------------------------------------
# Phase 11d: auto-title generation
# ---------------------------------------------------------------------------


def _stream_ndjson_once() -> bytes:
    """Build a minimal NDJSON `/api/chat` body the SSE pipeline can consume.

    A single one-token reply plus the trailing done marker — that's the
    smallest valid Ollama response, and it lets the title flow fire
    after the assistant message gets persisted.
    """
    return (
        b'{"message":{"content":"reply"},"done":false}\n'
        b'{"message":{"content":""},"done":true}\n'
    )


def test_stream_emits_title_event_after_assistant_reply(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the first assistant reply the SSE stream emits a `title`
    event carrying the OOB-swap sidebar row with the new name."""
    import app.routes as routes

    async def fake_generate_title(client, model, history):
        return "Sandwiches in Space"

    monkeypatch.setattr(routes.ollama, "generate_title", fake_generate_title)

    # 12d: route the tool-call probe (stream=false) to no-tool-calls,
    # the streaming reply to the NDJSON body.
    handler = _stream_handler(_stream_ndjson_once())

    with make_client(handler) as client:
        chat_id = _create_chat_and_get_id(client)

        response = client.get(f"/chats/{chat_id}/stream")

    assert response.status_code == 200
    text = response.text
    assert "event: title" in text
    # The title event payload is the rendered sidebar row with
    # hx-swap-oob="true" so HTMX replaces #chat-{id} in place.
    assert 'id="chat-' in text
    assert 'hx-swap-oob="true"' in text
    assert "Sandwiches in Space" in text


def test_stream_passes_conversation_model_to_title_generator(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto-titler reuses the chat's own model — no separate one
    to install or load."""
    import app.routes as routes

    captured: dict = {}

    async def fake_generate_title(client, model, history):
        captured["model"] = model
        return "Anything"

    monkeypatch.setattr(routes.ollama, "generate_title", fake_generate_title)

    # 12d: route the tool-call probe (stream=false) to no-tool-calls,
    # the streaming reply to the NDJSON body.
    handler = _stream_handler(_stream_ndjson_once())

    with make_client(handler) as client:
        # The helper POSTs `model=llama3`, so that's what we expect
        # forwarded into the title request.
        chat_id = _create_chat_and_get_id(client)
        client.get(f"/chats/{chat_id}/stream")

    assert captured["model"] == "llama3"


def test_stream_skips_title_when_chat_is_locked(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual rename locks the name; no title event fires after that."""
    import app.routes as routes

    called = {"n": 0}

    async def fake_generate_title(client, model, history):
        called["n"] += 1
        return "Should Not Be Used"

    monkeypatch.setattr(routes.ollama, "generate_title", fake_generate_title)

    # 12d: route the tool-call probe (stream=false) to no-tool-calls,
    # the streaming reply to the NDJSON body.
    handler = _stream_handler(_stream_ndjson_once())

    with make_client(handler) as client:
        # Phase 12g: PATCH the lock BEFORE the generation starts so
        # the name_locked check inside _maybe_emit_title sees the
        # lock applied. With POST /chats spawning a generation as a
        # side effect (and sync mocks completing it nearly instantly
        # in tests), a PATCH after that point would race the title
        # emit. Using `_create_chat_db_only` skips the gen and lets
        # us drive timing explicitly via POST /messages.
        chat_id = _create_chat_db_only("X")
        client.patch(f"/chats/{chat_id}", data={"name": "I Chose This"})
        client.post(
            f"/chats/{chat_id}/messages", data={"content": "second message"}
        )
        response = client.get(f"/chats/{chat_id}/stream")

    text = response.text
    assert "event: title" not in text
    # The lock check short-circuits BEFORE we call generate_title.
    assert called["n"] == 0


def test_stream_stops_title_after_third_assistant_reply(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto-titler refreshes through the 3rd reply, then stops.

    Drive four full message/stream rounds and count title calls. The
    first three must each trigger generate_title; the fourth must not.
    """
    import app.routes as routes

    calls = []

    async def fake_generate_title(client, model, history):
        calls.append(len(history))
        return f"Title {len(calls)}"

    monkeypatch.setattr(routes.ollama, "generate_title", fake_generate_title)

    # 12d: route the tool-call probe (stream=false) to no-tool-calls,
    # the streaming reply to the NDJSON body.
    handler = _stream_handler(_stream_ndjson_once())

    with make_client(handler) as client:
        chat_id = _create_chat_and_get_id(client)
        # First round: the create flow saved the user message already.
        # Stream the assistant reply (count → 1, title fires).
        client.get(f"/chats/{chat_id}/stream")

        # Rounds 2 and 3: send + stream. Title fires each time
        # (count → 2 then 3).
        for _ in range(2):
            client.post(
                f"/chats/{chat_id}/messages", data={"content": "ping"}
            )
            client.get(f"/chats/{chat_id}/stream")

        assert len(calls) == 3, (
            f"expected title called 3 times after replies 1-3,"
            f" got {len(calls)}"
        )

        # Round 4: count → 4, title MUST NOT fire.
        client.post(
            f"/chats/{chat_id}/messages", data={"content": "ping4"}
        )
        last = client.get(f"/chats/{chat_id}/stream")

    assert len(calls) == 3
    assert "event: title" not in last.text


def test_regenerate_stream_does_not_emit_title(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regenerate replaces an assistant message — it must NOT trigger
    a title refresh (the count would be misleading, and the user
    expects regeneration to leave metadata alone)."""
    import app.routes as routes

    called = {"n": 0}

    async def fake_generate_title(client, model, history):
        called["n"] += 1
        return "Should not be set by regenerate"

    monkeypatch.setattr(routes.ollama, "generate_title", fake_generate_title)

    # 12d: route the tool-call probe (stream=false) to no-tool-calls,
    # the streaming reply to the NDJSON body.
    handler = _stream_handler(_stream_ndjson_once())

    with make_client(handler) as client:
        chat_id = _create_chat_and_get_id(client)
        # Drive one stream so an assistant message exists (title WILL
        # fire here — that's the new-message path).
        client.get(f"/chats/{chat_id}/stream")
        baseline = called["n"]

        # Now regenerate. The replace path must NOT call generate_title.
        client.post(f"/chats/{chat_id}/regenerate")
        regen_response = client.get(
            f"/chats/{chat_id}/stream"
        )

    assert regen_response.status_code == 200
    assert "event: title" not in regen_response.text
    assert called["n"] == baseline, "generate_title fired on regenerate"


# ---------------------------------------------------------------------------
# Phase 12c: /settings + /settings/servers (RAG server CRUD)
# ---------------------------------------------------------------------------


def test_settings_get_renders_full_page_on_direct_hit(
    make_client: ClientFactory,
) -> None:
    """Direct GET /settings returns the full index shell with the settings
    fragment preloaded — same pattern as /chats/{id} on a direct hit.

    This is the bookmark / reload path: the URL alone must be enough
    to restore the same view.
    """
    with make_client(_ollama_unreachable) as client:
        response = client.get("/settings")

    assert response.status_code == 200
    # Full page (base.html shell), not just a fragment.
    assert "<!DOCTYPE html>" in response.text
    assert 'class="sidebar"' in response.text
    # And the settings fragment is preloaded into #main.
    assert 'class="settings"' in response.text
    # Empty state: no rows in the list yet, but the add-server form
    # is still rendered.
    assert 'class="rag-server-form"' in response.text


def test_settings_get_returns_fragment_for_htmx(
    make_client: ClientFactory,
) -> None:
    """An HX-Request to /settings returns just the settings fragment.

    HTMX swaps it into #main (the sidebar Settings link's hx-target)
    without re-rendering the rest of the page.
    """
    with make_client(_ollama_unreachable) as client:
        response = client.get("/settings", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<!DOCTYPE html>" not in response.text
    assert 'class="sidebar"' not in response.text
    assert 'class="settings"' in response.text


def test_settings_add_server_returns_row(
    make_client: ClientFactory,
) -> None:
    """POST /settings/servers returns the new <li> for beforeend swap."""
    with make_client(_ollama_unreachable) as client:
        response = client.post(
            "/settings/servers",
            data={"name": "arxiv", "url": "http://x/arxiv"},
        )

    assert response.status_code == 200
    # The row template renders both the name and the URL inside an
    # <li> whose id encodes the new server's id.
    assert "arxiv" in response.text
    assert "http://x/arxiv" in response.text
    assert 'id="rag-server-' in response.text
    # Delete affordance is right there on the row.
    assert 'hx-delete="/settings/servers/' in response.text


def test_settings_add_server_duplicate_name_returns_409(
    make_client: ClientFactory,
) -> None:
    """A second POST with the same name surfaces as HTTP 409.

    HTMX's default is to NOT swap a non-2xx response, so the existing
    list stays intact and the form's after-request reset (gated on
    `event.detail.successful`) keeps the typed values. The error region
    in the settings page (see test below) is what the user actually sees.
    """
    with make_client(_ollama_unreachable) as client:
        first = client.post(
            "/settings/servers",
            data={"name": "x", "url": "http://x/"},
        )
        assert first.status_code == 200

        response = client.post(
            "/settings/servers",
            data={"name": "x", "url": "http://y/"},
        )

    assert response.status_code == 409
    # Body is a short plain-text message naming the offending value;
    # the form's hx-on copies it verbatim into #rag-server-form-error.
    assert "already in use" in response.text


def test_settings_add_server_health_fail_returns_502(
    make_client: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing health probe blocks the insert and returns 502 + reason.

    The body is the plain-text reason produced by ``probe_rag_health``;
    the form's ``hx-on::after-request`` pipes it into the inline error
    region verbatim. No row is created on failure.
    """
    async def _unhealthy(name: str, base_url: str) -> tuple[bool, str]:
        return (False, "'arxiv_rag' is not healthy (status: 'degraded').")

    monkeypatch.setattr("app.routes.probe_rag_health", _unhealthy)

    with make_client(_ollama_unreachable) as client:
        response = client.post(
            "/settings/servers",
            data={"name": "arxiv_rag", "url": "http://pop-os:8002/arxiv_rag"},
        )
        # No row inserted — the rendered list has no `rag-server-<id>`
        # entries. Use the row-id marker rather than a name substring
        # because "arxiv_rag" now appears in the form's placeholder too.
        listing = client.get("/settings")

    assert response.status_code == 502
    assert response.text == "'arxiv_rag' is not healthy (status: 'degraded')."
    # The list element exists but contains no <li> rows. Each row is
    # `<li id="rag-server-N" class="rag-server">`; the form's error
    # div uses id="rag-server-form-error" so we can't substring-match
    # the id prefix alone.
    assert 'class="rag-server"' not in listing.text


def test_settings_add_server_health_unreachable_returns_502(
    make_client: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network-level probe failures bubble up as 502 with the unreachable msg."""
    async def _unreachable(name: str, base_url: str) -> tuple[bool, str]:
        return (
            False,
            "Health check failed: server unreachable at http://pop-os:8002/health.",
        )

    monkeypatch.setattr("app.routes.probe_rag_health", _unreachable)

    with make_client(_ollama_unreachable) as client:
        response = client.post(
            "/settings/servers",
            data={"name": "arxiv_rag", "url": "http://pop-os:8002/arxiv_rag"},
        )

    assert response.status_code == 502
    assert "unreachable" in response.text


def test_settings_add_server_health_unknown_name_returns_502(
    make_client: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name not present in /health's databases map is rejected.

    The stub mirrors the real probe's wording: not-found errors list
    the live _rag databases (filtered from the /health response), so
    the message stays accurate as new sources are added.
    """
    async def _unknown(name: str, base_url: str) -> tuple[bool, str]:
        return (
            False,
            f"'{name}' not found in /health response."
            " Available RAG databases: arxiv_rag, factbook_rag.",
        )

    monkeypatch.setattr("app.routes.probe_rag_health", _unknown)

    with make_client(_ollama_unreachable) as client:
        response = client.post(
            "/settings/servers",
            data={"name": "bogus_rag", "url": "http://pop-os:8002/bogus_rag"},
        )

    assert response.status_code == 502
    assert "'bogus_rag' not found" in response.text
    # The reason lists the live _rag databases so the user can self-correct.
    assert "Available RAG databases" in response.text


def test_settings_add_server_health_probe_invoked_with_typed_values(
    make_client: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route passes the stripped name + URL to the probe.

    Whitespace must be trimmed BEFORE the probe — otherwise a user
    typing a trailing space would see ``'arxiv_rag '`` (with the
    space) reported as not found. Mirrors how the success path
    later inserts the trimmed values.
    """
    seen: dict[str, str] = {}

    async def _capture(name: str, base_url: str) -> tuple[bool, str]:
        seen["name"] = name
        seen["url"] = base_url
        return (True, "")

    monkeypatch.setattr("app.routes.probe_rag_health", _capture)

    with make_client(_ollama_unreachable) as client:
        response = client.post(
            "/settings/servers",
            data={
                "name": "  arxiv_rag  ",
                "url": "  http://pop-os:8002/arxiv_rag  ",
            },
        )

    assert response.status_code == 200
    assert seen == {
        "name": "arxiv_rag",
        "url": "http://pop-os:8002/arxiv_rag",
    }


def test_settings_renders_form_error_region(
    make_client: ClientFactory,
) -> None:
    """The settings page renders the inline error region for the add-server
    form.

    Contract: a `#rag-server-form-error` element with `role="alert"` and
    the `hidden` attribute must be present so the form's
    `hx-on::after-request` has a stable target to write 4xx messages
    into. Hidden by default — only the JS toggles it visible.
    """
    with make_client(_ollama_unreachable) as client:
        response = client.get("/settings", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert 'id="rag-server-form-error"' in response.text
    assert 'class="form-error"' in response.text
    assert 'role="alert"' in response.text
    # `hidden` is a boolean attribute — present means hidden.
    assert "hidden></div>" in response.text or 'hidden=""' in response.text


def test_settings_renders_health_check_icon_slot(
    make_client: ClientFactory,
) -> None:
    """The settings page renders an empty #health-check-icon slot.

    Contract: the form's ``hx-on::after-request`` writes the ✓/✗ glyph
    into ``#health-check-icon``, so that element must exist on initial
    render. It starts empty — the JS fills it on submit.
    """
    with make_client(_ollama_unreachable) as client:
        response = client.get("/settings", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert 'id="health-check-icon"' in response.text
    assert 'class="health-icon"' in response.text
    # `aria-live="polite"` lets AT announce the result when the glyph
    # swaps in without interrupting the user mid-keystroke.
    assert 'aria-live="polite"' in response.text


def test_settings_delete_server_empty_200(
    make_client: ClientFactory,
) -> None:
    """DELETE /settings/servers/{id} returns empty 200 for hx-swap="delete"."""
    with make_client(_ollama_unreachable) as client:
        add_response = client.post(
            "/settings/servers",
            data={"name": "y", "url": "http://y/"},
        )
        # Pull the new server's id out of the returned row's id attribute.
        marker = 'id="rag-server-'
        start = add_response.text.index(marker) + len(marker)
        end = add_response.text.index('"', start)
        server_id = int(add_response.text[start:end])

        response = client.delete(f"/settings/servers/{server_id}")

    assert response.status_code == 200
    assert response.text == ""


def test_settings_delete_server_idempotent(
    make_client: ClientFactory,
) -> None:
    """Deleting a missing id is a 200 no-op (matches the query layer)."""
    with make_client(_ollama_unreachable) as client:
        response = client.delete("/settings/servers/9999")

    assert response.status_code == 200


def test_settings_get_lists_existing_servers(
    make_client: ClientFactory,
) -> None:
    """The settings page renders previously-added servers in order."""
    with make_client(_ollama_unreachable) as client:
        client.post(
            "/settings/servers",
            data={"name": "first", "url": "http://x/first"},
        )
        client.post(
            "/settings/servers",
            data={"name": "second", "url": "http://x/second"},
        )
        response = client.get("/settings")

    assert response.status_code == 200
    # Both rows present in insertion order.
    assert "first" in response.text
    assert "second" in response.text
    assert response.text.index("first") < response.text.index("second")


def test_sidebar_includes_settings_link(
    make_client: ClientFactory,
) -> None:
    """The sidebar footer carries a Settings link that hx-gets /settings."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/")

    assert 'class="sidebar__footer"' in response.text
    assert 'class="sidebar__settings"' in response.text
    assert 'hx-get="/settings"' in response.text


# ---------------------------------------------------------------------------
# Phase 12d: server-side tool-calling loop
# ---------------------------------------------------------------------------


def test_stream_runs_tool_then_streams_final(
    make_client: ClientFactory,
) -> None:
    """End-to-end: Ollama's first probe returns a tool_call, second probe
    returns no tool_calls, then the streaming reply completes. Verify
    the tool ran, the rows persisted, and the SSE events fire in
    order: tool-call → tool-result → token → done."""
    import json as _json

    # The probe-vs-stream handler needs custom logic here because the
    # FIRST probe must return a tool call (triggering one tool round),
    # the SECOND probe must return no tool calls (releasing the loop
    # into streaming).
    probe_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content or b"{}")
        if body.get("stream"):
            return httpx.Response(
                200,
                content=(
                    b'{"message":{"content":"All "},"done":false}\n'
                    b'{"message":{"content":"done"},"done":false}\n'
                    b'{"message":{"content":""},"done":true}\n'
                ),
            )
        probe_count[0] += 1
        if probe_count[0] == 1:
            # First probe: model wants to call current_time.
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
        # Second probe (after the tool round): no more tools.
        return httpx.Response(
            200,
            json={"message": {"content": "", "tool_calls": []}},
        )

    with make_client(handler) as client:
        chat_id = _create_chat_db_only("tool test")
        client.post(
            f"/chats/{chat_id}/messages", data={"content": "what time is it?"}
        )
        response = client.get(f"/chats/{chat_id}/stream")

    assert response.status_code == 200
    text = response.text
    # Ordering: tool events fire before tokens, which fire before done.
    pos_call = text.find("event: tool-call")
    pos_result = text.find("event: tool-result")
    pos_token = text.find("event: token")
    pos_done = text.find("event: done")
    assert pos_call != -1, f"missing tool-call event in:\n{text}"
    assert pos_result != -1, f"missing tool-result event in:\n{text}"
    assert pos_token != -1, f"missing token event in:\n{text}"
    assert pos_done != -1, f"missing done event in:\n{text}"
    assert pos_call < pos_result < pos_token < pos_done

    # Phase 12e card payload: first tool-call event carries the full
    # <details> shell OOB-swapped beforebegin of the streaming
    # placeholder; the matching tool-result OOB-replaces the row with
    # a frozen variant (data-elapsed-final set).
    assert 'class="tool-card"' in text
    assert 'hx-swap-oob="beforebegin:#assistant-stream-' in text
    assert 'using 1 tool' in text
    # current_time isn't query_rag, so it gets the generic fallback.
    assert "calling current_time" in text
    # The tool-result event freezes the row.
    assert "data-elapsed-final=" in text
    # Past-tense flip lands in the done payload.
    assert "used 1 tool" in text

    # Verify rows persisted by reading them back through the public
    # route — the chat panel renders all message rows.
    with make_client(handler) as client_for_read:
        panel = client_for_read.get(f"/chats/{chat_id}")
    # Both tool roles should appear in the rendered messages list.
    # _chat_panel.html prints data-role for every message; the
    # placeholder template doesn't exist for tool_call/tool_result
    # yet (12e deliverable), but the message dataclasses are visible
    # via the DB directly. Verify via the raw DB instead so we don't
    # depend on the 12e template existing.
    import os
    import sqlite3 as _sqlite3
    db_path = os.environ["DB_PATH"]
    with _sqlite3.connect(db_path) as conn:
        roles = [
            r[0] for r in conn.execute(
                "SELECT role FROM messages WHERE conversation_id = ?"
                " ORDER BY id ASC",
                (chat_id,),
            ).fetchall()
        ]
    # Expect: user (the /messages POST), tool_call, tool_result,
    # assistant (the streamed final). _create_chat_and_get_id ALSO
    # inserts a user row (the first message), so the full order is:
    # user, user, tool_call, tool_result, assistant.
    assert "tool_call" in roles
    assert "tool_result" in roles
    assert roles[-1] == "assistant"


def test_stream_caps_at_five_iterations(
    make_client: ClientFactory,
) -> None:
    """If Ollama keeps requesting tool_calls, the loop terminates after 5
    rounds and persists a "limit reached" assistant message."""
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content or b"{}")
        if body.get("stream"):
            # Should never be reached — the cap fires before streaming.
            raise AssertionError("stream_chat reached despite cap")
        # Every probe says: call current_time again.
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

    with make_client(handler) as client:
        chat_id = _create_chat_db_only("cap test")
        client.post(
            f"/chats/{chat_id}/messages", data={"content": "ping"}
        )
        response = client.get(f"/chats/{chat_id}/stream")

    assert response.status_code == 200
    text = response.text
    # The loop hit its ceiling and emitted done with an apology.
    assert "event: done" in text
    assert "Tool-call limit reached" in text

    # DB has exactly 5 tool_call rows (and 5 tool_result rows).
    import os
    import sqlite3 as _sqlite3
    db_path = os.environ["DB_PATH"]
    with _sqlite3.connect(db_path) as conn:
        tool_call_count = conn.execute(
            "SELECT COUNT(*) FROM messages"
            " WHERE conversation_id = ? AND role = 'tool_call'",
            (chat_id,),
        ).fetchone()[0]
        tool_result_count = conn.execute(
            "SELECT COUNT(*) FROM messages"
            " WHERE conversation_id = ? AND role = 'tool_result'",
            (chat_id,),
        ).fetchone()[0]
        final_text = conn.execute(
            "SELECT content FROM messages"
            " WHERE conversation_id = ? AND role = 'assistant'"
            " ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()[0]
    assert tool_call_count == 5
    assert tool_result_count == 5
    assert "Tool-call limit reached" in final_text


@pytest.mark.asyncio
async def test_stream_persists_partial_assistant_on_aclose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The realistic disconnect path: phase 12g spawns the
    generation as an asyncio.Task; a server shutdown (or test
    cancellation) cancels the task, which raises CancelledError
    inside `_run_generation`. The safety-net try/finally must
    persist whatever tokens already streamed before the
    CancelledError resumes propagating.

    Exercised by driving start_generation directly so we control
    the cancellation timing. TestClient would buffer the full SSE
    response and can't simulate disconnect-during-streaming."""
    import asyncio as _asyncio

    from app import generation, ollama as _ollama, queries
    from app.connection import open_connection
    from app.db import initialize_database

    db_path = tmp_path / "chats.db"
    initialize_database(db_path)

    # Fake stream_chat that yields two chunks then waits forever —
    # gives us a stable point to cancel while the gen is suspended.
    async def fake_stream_chat(client_, model_, messages_):
        yield _ollama.ChatChunk(content="partial ", done=False)
        yield _ollama.ChatChunk(content="answer", done=False)
        await _asyncio.sleep(60)

    async def fake_maybe_tool_call(client_, model_, messages_, tools=None):
        return ([], "")

    monkeypatch.setattr("app.generation.ollama.stream_chat", fake_stream_chat)
    monkeypatch.setattr(
        "app.generation.ollama.maybe_tool_call", fake_maybe_tool_call
    )

    with open_connection(db_path) as db:
        chat = queries.create_conversation(db, "aclose test", "llama3")
        queries.append_message(db, chat.id, "user", "hi")

        state = generation.start_generation(
            client=None,
            db=db,
            conversation_id=chat.id,
            model=chat.model,
            history=queries.list_messages(db, chat.id),
            on_complete="append",
        )
        # Wait until both tokens have landed in state.events.
        for _ in range(50):
            await _asyncio.sleep(0)
            tokens = [
                ev for ev, _payload in state.events if ev == "token"
            ]
            if len(tokens) >= 2:
                break
        assert len([ev for ev, _ in state.events if ev == "token"]) == 2

        # Simulate disconnect by cancelling the producer task.
        state.task.cancel()
        try:
            await state.task
        except _asyncio.CancelledError:
            pass

        rows = queries.list_messages(db, chat.id)

    assistant_rows = [r for r in rows if r.role == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0].content == "partial answer"
    # Registry retained the (now-done) state so a slow-reload
    # consume_generation would still see the events. Clean up so
    # the next test starts fresh.


@pytest.mark.asyncio
async def test_stream_persists_placeholder_when_aclosed_during_tool_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user's reported scenario: a tool_call row has been
    persisted (the tool card is on screen) but the user reloads
    BEFORE the streaming phase starts — i.e., cancellation lands
    inside `await run_tool(...)` between the tool-call event and
    its result. The safety-net try/finally writes the placeholder
    so the chat panel stays consistent on reload."""
    import asyncio as _asyncio

    from app import generation, queries
    from app.connection import open_connection
    from app.db import initialize_database

    db_path = tmp_path / "chats.db"
    initialize_database(db_path)

    async def slow_run_tool(name, args):
        await _asyncio.sleep(60)
        return "never returned"

    async def fake_maybe_tool_call(client_, model_, messages_, tools=None):
        return (
            [
                {
                    "name": "current_time",
                    "arguments": {"timezone": "UTC"},
                }
            ],
            "",
        )

    monkeypatch.setattr("app.generation.run_tool", slow_run_tool)
    monkeypatch.setattr(
        "app.generation.ollama.maybe_tool_call", fake_maybe_tool_call
    )

    with open_connection(db_path) as db:
        chat = queries.create_conversation(db, "tool-cancel test", "llama3")
        queries.append_message(db, chat.id, "user", "what time?")

        state = generation.start_generation(
            client=None,
            db=db,
            conversation_id=chat.id,
            model=chat.model,
            history=queries.list_messages(db, chat.id),
            on_complete="append",
        )
        # Wait until the tool-call event has been emitted (after
        # which the producer is awaiting on slow_run_tool — exactly
        # where the user's reload would land).
        for _ in range(50):
            await _asyncio.sleep(0)
            if any(ev == "tool-call" for ev, _ in state.events):
                break
        assert any(ev == "tool-call" for ev, _ in state.events)

        # Simulate disconnect.
        state.task.cancel()
        try:
            await state.task
        except _asyncio.CancelledError:
            pass

        rows = queries.list_messages(db, chat.id)

    roles = [r.role for r in rows]
    # user + tool_call (persisted before run_tool ran) + assistant
    # placeholder. No tool_result because run_tool never returned.
    assert "tool_call" in roles
    assert "tool_result" not in roles
    assistant_rows = [r for r in rows if r.role == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0].content == "(response interrupted)"


def test_stream_persists_partial_assistant_on_cancellation(
    make_client: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the SSE generator is cancelled mid-stream (e.g., user
    reloads the page), the partial content already streamed must be
    persisted as the assistant message — otherwise the chat panel
    shows an orphan tool-card with nothing after it on reload."""
    import asyncio as _asyncio

    from app import ollama as _ollama

    async def fake_stream_chat(client_, model_, messages_):
        # Yield two chunks, then simulate client disconnect by
        # raising CancelledError mid-stream.
        yield _ollama.ChatChunk(content="partial ", done=False)
        yield _ollama.ChatChunk(content="answer", done=False)
        raise _asyncio.CancelledError

    monkeypatch.setattr("app.generation.ollama.stream_chat", fake_stream_chat)

    def handler(request: httpx.Request) -> httpx.Response:
        # Probe returns no tool calls — release straight into streaming.
        return httpx.Response(
            200, json={"message": {"content": "", "tool_calls": []}}
        )

    with make_client(handler) as client:
        chat_id = _create_chat_and_get_id(client, "cancel test")
        client.post(
            f"/chats/{chat_id}/messages", data={"content": "ping"}
        )
        try:
            client.get(f"/chats/{chat_id}/stream")
        except _asyncio.CancelledError:
            # The re-raise from the routes.py handler propagates up
            # through Starlette and out of TestClient.
            pass

    import os
    import sqlite3 as _sqlite3
    db_path = os.environ["DB_PATH"]
    with _sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT content FROM messages"
            " WHERE conversation_id = ? AND role = 'assistant'"
            " ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    assert row is not None, "no assistant row persisted on cancellation"
    assert row[0] == "partial answer"


def test_stream_persists_placeholder_when_cancelled_with_zero_chunks(
    make_client: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation BEFORE any token streamed (e.g., reload while
    Ollama is still warming up) still writes an assistant row — the
    `(response interrupted)` placeholder — so the chat panel has a
    bubble to show after reload."""
    import asyncio as _asyncio

    async def fake_stream_chat(client_, model_, messages_):
        # No yields — cancellation before the first chunk arrives.
        raise _asyncio.CancelledError
        yield  # unreachable, makes this an async generator

    monkeypatch.setattr("app.generation.ollama.stream_chat", fake_stream_chat)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"message": {"content": "", "tool_calls": []}}
        )

    with make_client(handler) as client:
        chat_id = _create_chat_and_get_id(client, "early cancel")
        client.post(
            f"/chats/{chat_id}/messages", data={"content": "ping"}
        )
        try:
            client.get(f"/chats/{chat_id}/stream")
        except _asyncio.CancelledError:
            pass

    import os
    import sqlite3 as _sqlite3
    db_path = os.environ["DB_PATH"]
    with _sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT content FROM messages"
            " WHERE conversation_id = ? AND role = 'assistant'"
            " ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    assert row is not None, "no assistant row persisted on early cancel"
    assert row[0] == "(response interrupted)"


@pytest.mark.asyncio
async def test_regenerate_cancellation_preserves_original_when_no_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regen + cancellation BEFORE any token streamed must NOT
    clobber the existing assistant message with the
    `(response interrupted)` placeholder — that would silently
    destroy the user's previous response on an accidental reload."""
    import asyncio as _asyncio

    from app import generation, ollama as _ollama, queries
    from app.connection import open_connection
    from app.db import initialize_database

    db_path = tmp_path / "chats.db"
    initialize_database(db_path)

    async def fake_maybe_tool_call(client_, model_, messages_, tools=None):
        return ([], "")

    monkeypatch.setattr(
        "app.generation.ollama.maybe_tool_call", fake_maybe_tool_call
    )

    with open_connection(db_path) as db:
        chat = queries.create_conversation(db, "regen cancel", "llama3")
        queries.append_message(db, chat.id, "user", "first")
        queries.append_message(db, chat.id, "assistant", "original")

        # Regen path: stream_chat raises CancelledError immediately,
        # meaning the producer never collected any chunks.
        async def cancel_immediately(client_, model_, messages_):
            raise _asyncio.CancelledError
            yield  # unreachable, makes this an async generator

        monkeypatch.setattr(
            "app.generation.ollama.stream_chat", cancel_immediately
        )

        # Mimic POST /regenerate: drop the last assistant from prompt
        # history, spawn with on_complete="replace".
        history = queries.list_messages(db, chat.id)
        state = generation.start_generation(
            client=None,
            db=db,
            conversation_id=chat.id,
            model=chat.model,
            history=history[:-1],
            on_complete="replace",
        )
        try:
            await state.task
        except _asyncio.CancelledError:
            pass

        # Exactly one assistant row, still carrying the original.
        # The safety net's `elif chunks:` branch skipped the replace
        # because no chunks ever arrived.
        rows = [
            r for r in queries.list_messages(db, chat.id)
            if r.role == "assistant"
        ]
    assert len(rows) == 1
    assert rows[0].content == "original"


@pytest.mark.asyncio
async def test_regenerate_cancellation_writes_partial_when_tokens_arrived(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regen + cancellation AFTER some tokens streamed: the
    `elif chunks` branch DOES run replace_last_assistant_message,
    overwriting the original with the partial. The user opted into
    regen and the partial is more informative than the stale
    original."""
    import asyncio as _asyncio

    from app import generation, ollama as _ollama, queries
    from app.connection import open_connection
    from app.db import initialize_database

    db_path = tmp_path / "chats.db"
    initialize_database(db_path)

    async def fake_maybe_tool_call(client_, model_, messages_, tools=None):
        return ([], "")

    async def partial_then_block(client_, model_, messages_):
        yield _ollama.ChatChunk(content="new partial", done=False)
        await _asyncio.sleep(60)

    monkeypatch.setattr(
        "app.generation.ollama.maybe_tool_call", fake_maybe_tool_call
    )
    monkeypatch.setattr(
        "app.generation.ollama.stream_chat", partial_then_block
    )

    with open_connection(db_path) as db:
        chat = queries.create_conversation(db, "regen partial", "llama3")
        queries.append_message(db, chat.id, "user", "first")
        queries.append_message(db, chat.id, "assistant", "original")

        history = queries.list_messages(db, chat.id)
        state = generation.start_generation(
            client=None,
            db=db,
            conversation_id=chat.id,
            model=chat.model,
            history=history[:-1],
            on_complete="replace",
        )
        # Wait until the token event lands (after which the
        # producer is awaiting on the sleep).
        for _ in range(50):
            await _asyncio.sleep(0)
            if any(ev == "token" for ev, _ in state.events):
                break
        state.task.cancel()
        try:
            await state.task
        except _asyncio.CancelledError:
            pass

        rows = [
            r for r in queries.list_messages(db, chat.id)
            if r.role == "assistant"
        ]
    # Still exactly one assistant row (replace, not append), but
    # the content is now the partial — original was overwritten.
    assert len(rows) == 1
    assert rows[0].content == "new partial"


def test_build_done_card_oobs_empty_in_flight_only_emits_summary() -> None:
    """Happy path: every tool_call was paired with a tool_result before
    the loop released into streaming. The done payload's card
    contribution is just the past-tense summary swap; no frozen-row
    OOBs needed."""
    from app.generation import _build_done_card_oobs

    result = _build_done_card_oobs(
        call_count=2, in_flight={}, summary_id="tool-card-T-summary"
    )
    assert 'id="tool-card-T-summary"' in result
    assert 'hx-swap-oob="outerHTML"' in result
    assert "used 2 tools" in result
    # No row OOBs.
    assert "tool-row" not in result


def test_build_done_card_oobs_zero_calls_returns_empty() -> None:
    """A turn with no tool calls has no card to update — the helper
    returns an empty string so the done payload stays compact."""
    from app.generation import _build_done_card_oobs

    assert _build_done_card_oobs(0, {}, "tool-card-T-summary") == ""


def test_build_done_card_oobs_freezes_in_flight_rows() -> None:
    """Defensive branch: if any row never got its paired tool_result
    (e.g., a future codepath raises mid-await), the done payload
    OOB-replaces the row with a frozen variant so the JS tick driver
    stops incrementing it after SSE close. Dead code in the current
    control flow — exercised here directly so the safety net stays
    covered."""
    from app.generation import _build_done_card_oobs

    in_flight = {
        "tool-card-T-row-0": {
            "start_ms": 1_000_000_000,  # far in the past — duration is huge
            "name": "current_time",
            "arguments": {"timezone": "UTC"},
            "label": "calling current_time(timezone='UTC')",
        },
    }
    result = _build_done_card_oobs(
        call_count=1, in_flight=in_flight, summary_id="tool-card-T-summary"
    )
    # Summary swap is still there.
    assert "used 1 tool" in result
    # Plus a frozen-row OOB carrying the original label and a
    # data-elapsed-final (the JS skip-key).
    assert 'id="tool-card-T-row-0"' in result
    assert "data-elapsed-final=" in result
    assert "calling current_time" in result


def test_stream_two_tool_calls_emit_row_append_and_summary_bump(
    make_client: ClientFactory,
) -> None:
    """Phase 12e: the second tool-call in a turn must emit a ROW append
    OOB + a SUMMARY swap OOB — not another full <details> card. Verifies
    the count bumps to 2, the noun pluralizes (`tool` → `tools`), and
    the placement OOB target is `beforeend:#…-list` rather than
    `beforebegin:#assistant-stream-…`."""
    import json as _json

    probe_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content or b"{}")
        if body.get("stream"):
            return httpx.Response(
                200,
                content=(
                    b'{"message":{"content":"done"},"done":false}\n'
                    b'{"message":{"content":""},"done":true}\n'
                ),
            )
        probe_count[0] += 1
        if probe_count[0] <= 2:
            # First two probes each ask for one current_time call.
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
        # Third probe: no more tools, release into streaming.
        return httpx.Response(
            200, json={"message": {"content": "", "tool_calls": []}}
        )

    with make_client(handler) as client:
        chat_id = _create_chat_db_only("two-tool test")
        client.post(
            f"/chats/{chat_id}/messages",
            data={"content": "do two things"},
        )
        response = client.get(f"/chats/{chat_id}/stream")

    text = response.text
    # First tool-call event must carry the full card.
    first_card_idx = text.find('hx-swap-oob="beforebegin:#assistant-stream-')
    assert first_card_idx != -1, "missing initial card OOB swap"
    # Second tool-call: row appended into the list, NOT another card.
    assert "beforeend:#tool-card-" in text
    # Summary swap from 1 → 2 with plural noun.
    assert "using 2 tools" in text
    # No second beforebegin (we only insert the card once per turn).
    assert text.count('hx-swap-oob="beforebegin:#assistant-stream-') == 1
    # Past-tense flip lands in the done payload.
    assert "used 2 tools" in text


def test_stream_iteration_cap_freezes_unpaired_row_in_done(
    make_client: ClientFactory,
) -> None:
    """Phase 12e: on iteration-cap bail, the unpaired final tool-call's
    row must be OOB-replaced with a frozen variant in the `done`
    payload — otherwise the JS tick driver would keep incrementing it
    forever after SSE close."""
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content or b"{}")
        if body.get("stream"):
            raise AssertionError("stream_chat reached despite cap")
        # Every probe asks for another tool call.
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

    with make_client(handler) as client:
        chat_id = _create_chat_db_only("bail freeze test")
        client.post(
            f"/chats/{chat_id}/messages", data={"content": "loop"}
        )
        response = client.get(f"/chats/{chat_id}/stream")

    text = response.text
    # Bail emits done.
    assert "event: done" in text
    # Past-tense summary swap with count=5 (the cap). This is the
    # bail-branch-specific contribution to the done payload — the
    # normal streaming branch emits the same swap, but it can't run
    # here because we never reach `not tool_calls`.
    assert "used 5 tools" in text
    # Every tool-result event freezes its own row, so we expect 5
    # frozen-row swaps in the stream regardless of bail behavior.
    # The defensive bail-freeze for genuinely in_flight rows is dead
    # code in this control flow (run_tool always returns a string,
    # never raising) and lives as a safety net for future failure
    # modes — exercised via the no-crash assertion at line 1873.
    assert text.count("data-elapsed-final=") == 5


def test_stream_passes_tools_payload_to_ollama(
    make_client: ClientFactory,
) -> None:
    """The probe call advertises the registered tools so Ollama can
    decide whether to invoke one. Phase 12d always sends `tools=` —
    capability filtering lands in 12f.

    Stands in for the "skips tools for non-tool model" test in the
    plan: until 12f's model_supports_tools helper exists, the loop
    unconditionally advertises tools and a non-tool model would 400.
    Documented in the implementation summary.
    """
    import json as _json

    captured_tools: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content or b"{}")
        if not body.get("stream"):
            # Probe — capture the tools key for inspection.
            captured_tools.append(body.get("tools"))
            return httpx.Response(
                200,
                json={"message": {"content": "", "tool_calls": []}},
            )
        # Stream: simple one-token reply.
        return httpx.Response(
            200,
            content=(
                b'{"message":{"content":"ok"},"done":false}\n'
                b'{"message":{"content":""},"done":true}\n'
            ),
        )

    with make_client(handler) as client:
        chat_id = _create_chat_db_only("tools probe")
        client.post(
            f"/chats/{chat_id}/messages", data={"content": "hello"}
        )
        client.get(f"/chats/{chat_id}/stream")

    # At least one probe ran and the tools list was populated. We
    # don't assert exact contents — the registry changes as tools are
    # added — but the basic shape (a non-empty list of function specs)
    # is verifiable.
    assert captured_tools, "no probe captured"
    advertised = captured_tools[0]
    assert isinstance(advertised, list)
    assert advertised, "tools list was empty"
    names = {t["function"]["name"] for t in advertised}
    # current_time is registered by app.tools.builtins (the import 12d
    # added to routes); query_rag is registered by app.tools.rag (12c).
    assert "current_time" in names
    assert "query_rag" in names


# ---------------------------------------------------------------------------
# Phase 12g: resume flow tests
# ---------------------------------------------------------------------------


def test_post_messages_409_when_generation_in_flight(
    make_client: ClientFactory,
) -> None:
    """POST /messages returns 409 if another generation is still
    in flight for the same conversation. The UI gate (streaming
    placeholder disables the send button) makes this rare, but the
    defensive 409 catches duplicate POSTs that slip through."""
    from app import generation

    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_db_only("409 test")
        # Plant a sentinel non-done state so start_generation raises.
        generation.live_generations[chat_id] = generation.GenerationState(
            conversation_id=chat_id
        )
        response = client.post(
            f"/chats/{chat_id}/messages", data={"content": "another"}
        )
    assert response.status_code == 409
    assert "already streaming" in response.text.lower()


def test_chat_panel_renders_placeholder_for_in_progress_gen(
    make_client: ClientFactory,
) -> None:
    """GET /chats/{id} while a generation is live renders a streaming
    placeholder with sse-connect pointing at /stream. The chat-panel
    template's existing pending_stream_url path is reused for resume."""
    from app import generation

    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_db_only("resume placeholder")
        generation.live_generations[chat_id] = generation.GenerationState(
            conversation_id=chat_id
        )
        response = client.get(f"/chats/{chat_id}")
    assert response.status_code == 200
    assert f'sse-connect="/chats/{chat_id}/stream"' in response.text
    assert "message--streaming" in response.text


def test_chat_panel_skips_placeholder_when_gen_is_done(
    make_client: ClientFactory,
) -> None:
    """A DONE state lingers in the registry for slow-reload replay,
    but the chat panel must NOT render a streaming placeholder for
    it — the conversation is already complete and the historic
    render handles its state.

    Asserts on the placeholder div's id rather than the CSS class
    name (which also appears in base.html's CSS rule selectors,
    making it a false-positive match)."""
    from app import generation

    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_db_only("done placeholder")
        state = generation.GenerationState(conversation_id=chat_id)
        state.done = True
        generation.live_generations[chat_id] = state
        response = client.get(f"/chats/{chat_id}")
    assert response.status_code == 200
    # No streaming placeholder element rendered.
    assert f'id="assistant-stream-{chat_id}"' not in response.text


def test_chat_panel_skips_trailing_tool_batch_during_gen(
    make_client: ClientFactory,
) -> None:
    """When a live (non-done) generation exists AND the conv has
    trailing tool_call/tool_result rows (no following assistant),
    those rows are NOT rendered as a historic ToolBatchBlock — the
    SSE replay will rebuild the card via OOB swaps."""
    import json as _json

    from app import generation, queries
    from app.connection import open_connection

    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_db_only("trailing tool batch")
        # Insert trailing tool rows directly.
        import os
        db_path = os.environ["DB_PATH"]
        with open_connection(db_path) as conn:
            queries.append_message(
                conn, chat_id, "tool_call",
                _json.dumps({"name": "current_time", "arguments": {}}),
            )
            queries.append_message(
                conn, chat_id, "tool_result", "2026-05-19T12:00:00Z",
            )
        generation.live_generations[chat_id] = generation.GenerationState(
            conversation_id=chat_id
        )
        response = client.get(f"/chats/{chat_id}")
    text = response.text
    # The trailing tool batch's id would appear as `tool-card-hist-N`
    # if we rendered it. With the live gen, we skip it.
    assert "tool-card-hist-" not in text
    # Placeholder IS present (live gen → pending_stream_url).
    assert "message--streaming" in text


def test_stream_endpoint_falls_back_to_consume_finished(
    make_client: ClientFactory,
) -> None:
    """GET /stream with no entry in live_generations yields a done
    event built from the persisted assistant row — the slow-reload-
    AFTER-completion case where the registry was cleared."""
    from app import queries
    from app.connection import open_connection
    import os

    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_db_only("finished fallback")
        db_path = os.environ["DB_PATH"]
        with open_connection(db_path) as conn:
            queries.append_message(
                conn, chat_id, "assistant", "the completed reply"
            )
        response = client.get(f"/chats/{chat_id}/stream")
    text = response.text
    assert response.status_code == 200
    assert "event: done" in text
    assert "the completed reply" in text
    assert f'outerHTML:#assistant-stream-{chat_id}' in text


def test_build_history_payload_handles_tool_roles() -> None:
    """`_build_history_payload` maps each role to Ollama's wire format:
    user/assistant pass through, tool_call becomes assistant+tool_calls,
    tool_result becomes role=tool."""
    import json as _json
    from datetime import datetime, timezone

    from app.queries import Message
    from app.generation import _build_history_payload

    now = datetime.now(timezone.utc)
    history = [
        Message(
            id=1, conversation_id=1, role="user",
            content="hi", created_at=now,
        ),
        Message(
            id=2, conversation_id=1, role="tool_call",
            content=_json.dumps(
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
    from datetime import datetime, timezone

    from app.queries import Message
    from app.generation import _build_history_payload

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


# ---------------------------------------------------------------------------
# Phase 12h: historic chat panel renders expandable rows for stored RAG calls
# ---------------------------------------------------------------------------


def test_chat_panel_renders_expandable_row_for_persisted_rag_call(
    make_client: ClientFactory,
) -> None:
    """Seed a conversation with a tool_call + JSON-envelope tool_result;
    GET /chats/{id} renders the row in the expandable form with the
    source title visible in the HTML.

    Pins the historic-render contract: source metadata survives the
    full storage round-trip and the resulting fragment carries the
    `tool-row--expandable` class and the source title substring.
    """
    import json as _json
    import os

    from app import queries
    from app.connection import open_connection
    from app.tools import Source, ToolResult, encode_tool_result

    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_db_only("ask about Transformers")

        db_path = os.environ["DB_PATH"]
        with open_connection(db_path) as conn:
            queries.append_message(
                conn, chat_id, "tool_call",
                _json.dumps({
                    "name": "query_rag",
                    "arguments": {
                        "source": "arxiv",
                        "query": "attention is all you need",
                    },
                }),
            )
            queries.append_message(
                conn, chat_id, "tool_result",
                encode_tool_result(ToolResult(
                    text="[1] Attention Is All You Need (§Introduction)\n    body",
                    sources=[
                        Source(title="Attention Is All You Need",
                               section="Introduction"),
                    ],
                )),
            )
            queries.append_message(
                conn, chat_id, "assistant",
                "It's a transformer paper from 2017.",
            )

        response = client.get(f"/chats/{chat_id}")

    text = response.text
    # Aggregated card is present (phase 12e baseline) …
    assert 'class="tool-card"' in text
    # … and the row is in the expandable form for the RAG call.
    assert "tool-row--expandable" in text
    # The decoded source title makes it into the rendered HTML.
    assert "Attention Is All You Need" in text
    # Single chunk with section → `(§Introduction)` meta suffix.
    assert "(§Introduction)" in text


def test_chat_panel_renders_plain_row_for_legacy_plain_text_tool_result(
    make_client: ClientFactory,
) -> None:
    """Backwards compat: a pre-12h conversation (plain-text content
    on the tool_result row) renders with the plain non-expandable
    row form. No chevron, no <details>. Old conversations stay
    readable."""
    import json as _json
    import os

    from app import queries
    from app.connection import open_connection

    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_db_only("legacy chat")

        db_path = os.environ["DB_PATH"]
        with open_connection(db_path) as conn:
            queries.append_message(
                conn, chat_id, "tool_call",
                _json.dumps({
                    "name": "query_rag",
                    "arguments": {"source": "arxiv", "query": "x"},
                }),
            )
            # The pre-12h shape: plain formatted citation text, no envelope.
            queries.append_message(
                conn, chat_id, "tool_result",
                "[1] Old Paper (§Intro)\n    legacy body text",
            )
            queries.append_message(
                conn, chat_id, "assistant", "ok",
            )

        response = client.get(f"/chats/{chat_id}")

    text = response.text
    # Card + row still render.
    assert 'class="tool-card"' in text
    # But NOT the expandable form — no sources were stored.
    assert "tool-row--expandable" not in text
    assert "tool-row__chevron" not in text
