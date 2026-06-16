"""Phase 19: per-chat RAG-source chips render in the sidebar (above the
Settings footer) instead of the chat-panel header. Tests cover:

- Sidebar Sources section rendering across the active / inactive states.
- Toggle endpoint returning the sidebar partial directly.
- The new 'red' state for chips whose backing server fails /health,
  including the precedence rule that disabled chips never show red
  (red is reserved for enabled-but-broken sources).
- Empty-state composer no longer carrying RAG inputs; new chats with
  no ``enabled_rag_servers`` form field still default to all-on.

Re-uses ``make_client`` and the tool-capable model stub from
``tests/test_routes.py`` so each test gets a fresh DB + mocked Ollama
identical to the rest of the suite.
"""

import os
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from app import queries, rag_health
from app import rag_servers as _rag_servers
from app.connection import open_connection
from app.db import initialize_database

from tests.test_routes import (
    ClientFactory,
    _default_project_id,
    _tool_capable_handler,
    make_client,  # noqa: F401 — fixture re-export
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_chat(
    *,
    project_id: int | None = None,
    active_agent: str | None = None,
    model: str = "llama3",
) -> int:
    """Create a chat row directly in the DB; return the chat id.

    Bypasses the create-chat endpoint (which spawns a generation that
    would consume the test's mock). Used by every test that needs an
    existing chat to navigate to.
    """
    db_path = Path(os.environ["DB_PATH"])
    initialize_database(db_path)
    with closing(open_connection(db_path)) as conn:
        if project_id is None:
            project_id = conn.execute(
                "SELECT id FROM projects ORDER BY id LIMIT 1;"
            ).fetchone()[0]
        chat = queries.create_conversation(
            conn, name="test", model=model, project_id=project_id,
            active_agent=active_agent,
        )
    return chat.id


def _seed_server(name: str = "arxiv", url: str = "http://fake/arxiv") -> None:
    db_path = Path(os.environ["DB_PATH"])
    initialize_database(db_path)
    with closing(open_connection(db_path)) as conn:
        _rag_servers.create_server(conn, name, url)


# ---------------------------------------------------------------------------
# Sidebar guard — visibility across active / inactive states
# ---------------------------------------------------------------------------


def test_sidebar_renders_section_with_active_chat_and_server(
    make_client: ClientFactory,
) -> None:
    """Chat-panel page: the Sources section + chip render in the sidebar."""
    _seed_server("arxiv", "http://fake/arxiv")
    chat_id = _seed_chat()
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.get(f"/projects/{pid}/chats/{chat_id}")

    assert response.status_code == 200
    assert 'id="sidebar-rag-section"' in response.text
    assert 'class="sidebar__rag-section"' in response.text
    assert 'Sources' in response.text
    assert '>arxiv<' in response.text  # the chip label


def test_full_page_load_does_not_duplicate_section(
    make_client: ClientFactory,
) -> None:
    """Regression: on a plain (non-HX) GET, the partial must render exactly
    ONCE — in the sidebar — not also at the bottom of the page.

    The bug: _project_page.html unconditionally OOB-included the
    partial; on full-page loads HTMX isn't processing the response, so
    the browser rendered the `hx-swap-oob`-flagged section inline at the
    bottom. The fix: guard the OOB include on request.headers['HX-Request'].
    """
    _seed_server("arxiv", "http://fake/arxiv")
    chat_id = _seed_chat()
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        # No HX-Request header — plain browser load.
        response = client.get(f"/projects/{pid}/chats/{chat_id}")

    assert response.status_code == 200
    assert response.text.count('id="sidebar-rag-section"') == 1


def test_hx_request_includes_oob_section(
    make_client: ClientFactory,
) -> None:
    """The companion to the above: HTMX chat-switches DO need the OOB
    section so the sidebar updates. With HX-Request, the response is the
    project_page fragment plus the OOB section — two occurrences of the
    id."""
    _seed_server("arxiv", "http://fake/arxiv")
    chat_id = _seed_chat()
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.get(
            f"/projects/{pid}/chats/{chat_id}",
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
    assert 'hx-swap-oob="true"' in response.text
    # Note: the fragment-only response contains the OOB section once;
    # the existing #sidebar-rag-section already in DOM is what it
    # OOB-replaces on the client side. Server-side we should see one.
    assert response.text.count('id="sidebar-rag-section"') == 1


def test_sidebar_section_empty_when_no_active_chat(
    make_client: ClientFactory,
) -> None:
    """Projects index: the section wrapper exists (stable OOB target) but
    has no styling class and no chips."""
    _seed_server("arxiv", "http://fake/arxiv")

    with make_client(_tool_capable_handler) as client:
        response = client.get("/projects")

    assert response.status_code == 200
    assert 'id="sidebar-rag-section"' in response.text
    # The empty wrapper has neither the styling class nor a button.
    assert 'class="sidebar__rag-section"' not in response.text
    assert 'sidebar__rag-chips' not in response.text


def test_sidebar_section_empty_when_no_servers_configured(
    make_client: ClientFactory,
) -> None:
    """Chat with zero RAG servers: empty wrapper, no chips."""
    chat_id = _seed_chat()  # no _seed_server call
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.get(f"/projects/{pid}/chats/{chat_id}")

    assert response.status_code == 200
    assert 'id="sidebar-rag-section"' in response.text
    assert 'class="sidebar__rag-section"' not in response.text


def test_sidebar_section_empty_when_model_lacks_tools(
    make_client: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-tool-capable model: section is the empty wrapper."""
    async def _no_tools(_client: object, _name: str) -> bool:
        return False

    monkeypatch.setattr(
        "app.routes.projects.ollama.model_supports_tools", _no_tools
    )
    _seed_server("arxiv", "http://fake/arxiv")
    chat_id = _seed_chat()
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.get(f"/projects/{pid}/chats/{chat_id}")

    assert response.status_code == 200
    assert 'id="sidebar-rag-section"' in response.text
    assert 'class="sidebar__rag-section"' not in response.text


# ---------------------------------------------------------------------------
# Toggle endpoint — returns the new partial
# ---------------------------------------------------------------------------


def test_toggle_rag_server_returns_sidebar_partial(
    make_client: ClientFactory,
) -> None:
    """POST /chats/{id}/rag-servers/{name} returns the sidebar section,
    not the old chat-header tool chips fragment."""
    _seed_server("arxiv", "http://fake/arxiv")
    chat_id = _seed_chat()

    with make_client(_tool_capable_handler) as client:
        response = client.post(
            f"/chats/{chat_id}/rag-servers/arxiv",
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
    assert 'id="sidebar-rag-section"' in response.text
    assert 'class="sidebar__rag-section"' in response.text
    # We toggled from default-enabled to disabled, so the chip is off.
    assert 'tool-chip--off' in response.text


# ---------------------------------------------------------------------------
# Red / unavailable state
# ---------------------------------------------------------------------------


def test_red_state_when_enabled_and_health_false(
    make_client: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled chip + /health probe returns False → tool-chip--unavailable."""
    async def _all_bad(servers, *, force=False):
        return {s.name: False for s in servers}

    monkeypatch.setattr(rag_health, "get_health_map", _all_bad)

    _seed_server("arxiv", "http://fake/arxiv")
    chat_id = _seed_chat()  # default: arxiv is enabled
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.get(f"/projects/{pid}/chats/{chat_id}")

    assert response.status_code == 200
    assert 'tool-chip--unavailable' in response.text


def test_no_red_state_when_disabled_even_if_health_false(
    make_client: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled chip should render as plain off even when probe returns
    False — red is informational for sources the model would actually try
    to query, and a disabled source won't be queried."""
    async def _all_bad(servers, *, force=False):
        return {s.name: False for s in servers}

    monkeypatch.setattr(rag_health, "get_health_map", _all_bad)

    _seed_server("arxiv", "http://fake/arxiv")
    chat_id = _seed_chat()
    # Flip it to disabled first via the toggle endpoint.
    with make_client(_tool_capable_handler) as client:
        client.post(
            f"/chats/{chat_id}/rag-servers/arxiv",
            headers={"HX-Request": "true"},
        )
        # Now load the chat panel; chip should be off, not unavailable.
        pid = _default_project_id()
        response = client.get(f"/projects/{pid}/chats/{chat_id}")

    assert response.status_code == 200
    assert 'tool-chip--off' in response.text
    assert 'tool-chip--unavailable' not in response.text


def test_healthy_probe_renders_on_chip(
    make_client: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled chip + probe True → tool-chip--on (no red, no off)."""
    async def _all_good(servers, *, force=False):
        return {s.name: True for s in servers}

    monkeypatch.setattr(rag_health, "get_health_map", _all_good)

    _seed_server("arxiv", "http://fake/arxiv")
    chat_id = _seed_chat()
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.get(f"/projects/{pid}/chats/{chat_id}")

    assert response.status_code == 200
    assert 'tool-chip--on' in response.text
    assert 'tool-chip--unavailable' not in response.text


# ---------------------------------------------------------------------------
# Composer cleanup + new-chat seeding
# ---------------------------------------------------------------------------


def test_composer_no_longer_has_rag_inputs(
    make_client: ClientFactory,
) -> None:
    """Empty-state composer should not render any RAG-server checkboxes
    (phase 19 dropped them; sidebar handles per-chat toggling)."""
    _seed_server("arxiv", "http://fake/arxiv")
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.get(f"/projects/{pid}/chats")

    assert response.status_code == 200
    # No checkbox / label combinations submitting enabled_rag_servers.
    assert 'name="enabled_rag_servers"' not in response.text
    # Tool chips for non-RAG tools still render in the composer.
    assert 'name="enabled_tools"' in response.text


def test_new_chat_with_no_rag_form_field_seeds_all_on(
    make_client: ClientFactory,
) -> None:
    """Creating a chat without any enabled_rag_servers field (because the
    composer no longer submits them) still seeds the per-chat RAG rows
    as all-on — matches the existing 'omit → default all enabled' branch
    in seed_chat_rag_servers."""
    _seed_server("arxiv", "http://fake/arxiv")
    _seed_server("pubmed", "http://fake/pubmed")
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.post(
            f"/projects/{pid}/chats",
            data={"model": "llama3", "content": "hello"},
        )

    assert response.status_code == 201
    # Pull the created chat id out of the response.
    marker = 'data-chat-id="'
    start = response.text.index(marker) + len(marker)
    chat_id = int(response.text[start: response.text.index('"', start)])

    db_path = Path(os.environ["DB_PATH"])
    with closing(open_connection(db_path)) as conn:
        rows = conn.execute(
            "SELECT server_name, enabled FROM chat_rag_settings"
            " WHERE conversation_id = ? ORDER BY server_name;",
            (chat_id,),
        ).fetchall()
    states = {r["server_name"]: bool(r["enabled"]) for r in rows}
    assert states == {"arxiv": True, "pubmed": True}
