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
    """Create a chat via the route, return its id parsed from data-chat-id.

    The composer always posts (model, content). Chats land in the DB
    with the placeholder name "New chat" (phase 11d will auto-rename).
    The data-chat-id attribute lives in the OOB sidebar row of the
    response, so the existing marker-parsing still works.
    """
    response = client.post(
        "/chats", data={"model": "llama3", "content": content}
    )
    marker = 'data-chat-id="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return int(response.text[start:end])


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
    assert 'hx-push-url="true"' in response.text
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

    The placeholder name "New chat" is used for every conversation
    until phase 11d's auto-titler runs, so all rows render with the
    same name until then — that's expected and we just count them.
    """
    with make_client(_ollama_unreachable) as client:
        _create_chat_and_get_id(client, "first")
        _create_chat_and_get_id(client, "second")

        response = client.get("/")

    # Two rows expected; both carry the placeholder name.
    assert response.text.count('class="chat-item"') == 2
    assert response.text.count(">New chat<") == 2


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

    Two mechanisms must be in the rendered panel:
    - `hx-on::after-swap` on `#messages` so streaming tokens and
      newly-sent messages keep the bottom in view.
    - An inline script that scrolls on initial render (chat-panel
      load) since no swap event fires at that point.
    """
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "X")
        response = client.get(f"/chats/{chat_id}")

    # After-swap handler on the messages container.
    assert "scrollTop = this.scrollHeight" in response.text
    # Initial-render scroll script.
    assert "scrollTop = m.scrollHeight" in response.text


def test_chat_panel_form_only_resets_on_successful_response(
    make_client: ClientFactory,
) -> None:
    """Resetting the textarea must be gated on a successful response.

    Without the `event.detail.successful` guard, a failed POST (e.g.
    the conversation was deleted in another tab → 404) would wipe the
    user's typed message and leave them with no indication of what
    happened.
    """
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "X")
        response = client.get(f"/chats/{chat_id}")

    # The conditional must be visible in the rendered template.
    assert "event.detail.successful" in response.text
    assert "this.reset()" in response.text


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
    # Has the rename form with the current placeholder name pre-filled.
    # (Phase 11d's auto-titler will replace "New chat" with a model-
    # generated title; until that's wired the placeholder sticks.)
    assert f'hx-patch="/chats/{chat_id}"' in response.text
    assert 'name="name"' in response.text
    assert 'value="New chat"' in response.text


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
    # Placeholder name from the composer-driven create path.
    assert "New chat" in response.text


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
        # The composer always assigns the placeholder name "New chat";
        # the edit form pre-fills it for the user to overwrite.
        assert 'value="New chat"' in edit_response.text
        assert f'hx-patch="/chats/{chat_id}"' in edit_response.text

        patch_response = client.patch(
            f"/chats/{chat_id}", data={"name": "Renamed Topic"}
        )
        assert patch_response.status_code == 200
        # Use bracket-anchored substrings so "Renamed" doesn't also
        # match part of "Renamed Topic" — we want the post-rename
        # name visible as link text and the placeholder gone.
        assert ">Renamed Topic<" in patch_response.text
        assert ">New chat<" not in patch_response.text
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
    # The placeholder name set by POST /chats should be gone after the
    # rename. Anchor on >…< so we don't accidentally match other text.
    assert ">New chat<" not in response.text


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

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(200, content=ndjson)

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

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ndjson)

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

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

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

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ndjson)

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

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ndjson)

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
        f'sse-connect="/chats/{chat_id}/regenerate-stream"' in response.text
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
    """GET /chats/999/regenerate-stream on a missing chat returns 404."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/chats/999/regenerate-stream")
    assert response.status_code == 404


def test_regenerate_stream_400_when_no_assistant_message(
    make_client: ClientFactory,
) -> None:
    """GET /chats/{id}/regenerate-stream returns 400 if the conversation
    has no assistant message to regenerate. Mirrors the same check on
    the POST /regenerate route; without both checks, a hand-crafted
    GET would bypass the validation."""
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "X")
        response = client.get(f"/chats/{chat_id}/regenerate-stream")
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
        response = client.get(f"/chats/{chat_id}/regenerate-stream")

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

    async def fake_generate_title(client, history):
        return "Sandwiches in Space"

    monkeypatch.setattr(routes.ollama, "generate_title", fake_generate_title)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_stream_ndjson_once())

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


def test_stream_skips_title_when_chat_is_locked(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual rename locks the name; no title event fires after that."""
    import app.routes as routes

    called = {"n": 0}

    async def fake_generate_title(client, history):
        called["n"] += 1
        return "Should Not Be Used"

    monkeypatch.setattr(routes.ollama, "generate_title", fake_generate_title)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_stream_ndjson_once())

    with make_client(handler) as client:
        chat_id = _create_chat_and_get_id(client)
        # User renames immediately, locking the chat.
        client.patch(f"/chats/{chat_id}", data={"name": "I Chose This"})

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

    async def fake_generate_title(client, history):
        calls.append(len(history))
        return f"Title {len(calls)}"

    monkeypatch.setattr(routes.ollama, "generate_title", fake_generate_title)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_stream_ndjson_once())

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


def test_stream_emits_title_warning_when_model_missing(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the title model isn't installed, the stream emits a one-time
    `title-warning` event carrying the install-this-model banner."""
    import app.routes as routes
    from app.ollama import OllamaModelMissing

    async def fake_generate_title(client, history):
        raise OllamaModelMissing("tinyllama:1.1b-chat-v1-fp16")

    monkeypatch.setattr(routes.ollama, "generate_title", fake_generate_title)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_stream_ndjson_once())

    with make_client(handler) as client:
        chat_id = _create_chat_and_get_id(client)
        response = client.get(f"/chats/{chat_id}/stream")

    text = response.text
    assert "event: title-warning" in text
    # The banner fragment carries hx-swap-oob so HTMX targets
    # #title-warning in the sidebar instead of dumping into the
    # assistant placeholder.
    assert 'id="title-warning"' in text
    assert 'hx-swap-oob="true"' in text
    assert "tinyllama:1.1b-chat-v1-fp16" in text


def test_regenerate_stream_does_not_emit_title(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regenerate replaces an assistant message — it must NOT trigger
    a title refresh (the count would be misleading, and the user
    expects regeneration to leave metadata alone)."""
    import app.routes as routes

    called = {"n": 0}

    async def fake_generate_title(client, history):
        called["n"] += 1
        return "Should not be set by regenerate"

    monkeypatch.setattr(routes.ollama, "generate_title", fake_generate_title)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_stream_ndjson_once())

    with make_client(handler) as client:
        chat_id = _create_chat_and_get_id(client)
        # Drive one stream so an assistant message exists (title WILL
        # fire here — that's the new-message path).
        client.get(f"/chats/{chat_id}/stream")
        baseline = called["n"]

        # Now regenerate. The replace path must NOT call generate_title.
        client.post(f"/chats/{chat_id}/regenerate")
        regen_response = client.get(
            f"/chats/{chat_id}/regenerate-stream"
        )

    assert regen_response.status_code == 200
    assert "event: title" not in regen_response.text
    assert called["n"] == baseline, "generate_title fired on regenerate"
