"""Phase 23: the sidebar's Sources section is a READ-ONLY RAG-source health
panel (above the Settings footer). Per-server gating chips (phase 15b/19) were
removed — query_rag always searches every configured server — so the section
just lists each configured server with its cached liveness. Tests cover:

- Section rendering across the active / inactive states.
- Health colours: green (up), red (down), grey (unknown).
- The section is read-only (no toggle buttons / endpoint).
- Empty-state composer carries no chip inputs.

Re-uses ``make_client`` and the tool-capable model stub from
``tests/test_routes.py`` so each test gets a fresh DB + mocked Ollama
identical to the rest of the suite.
"""

import os
from contextlib import closing
from pathlib import Path

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
# Visibility across active / inactive states
# ---------------------------------------------------------------------------


def test_sidebar_renders_section_with_active_chat_and_server(
    make_client: ClientFactory,
) -> None:
    """Chat-panel page: the Sources section + server label render in the
    sidebar."""
    _seed_server("arxiv", "http://fake/arxiv")
    chat_id = _seed_chat()
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.get(f"/projects/{pid}/chats/{chat_id}")

    assert response.status_code == 200
    assert 'id="sidebar-rag-section"' in response.text
    assert 'class="sidebar__rag-section"' in response.text
    assert 'Sources' in response.text
    assert '>arxiv<' in response.text  # the server label


def test_sidebar_is_read_only_no_toggle(
    make_client: ClientFactory,
) -> None:
    """The Sources panel is informational only — no per-server toggle
    button/endpoint is rendered (phase 23 removed gating)."""
    _seed_server("arxiv", "http://fake/arxiv")
    chat_id = _seed_chat()
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.get(f"/projects/{pid}/chats/{chat_id}")

    assert response.status_code == 200
    # The old chip toggled via hx-post to /chats/{id}/rag-servers/{name};
    # that endpoint and markup are gone.
    assert '/rag-servers/' not in response.text


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
    section so the sidebar updates. With HX-Request, the response carries
    the OOB-flagged section."""
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
# Health colours: green / red / grey
# ---------------------------------------------------------------------------


def test_unavailable_state_when_health_false(
    make_client: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server whose /health probe returns False renders as
    tool-chip--unavailable (red)."""
    async def _all_bad(servers, *, force=False):
        return {s.name: False for s in servers}

    monkeypatch.setattr(rag_health, "get_health_map", _all_bad)

    _seed_server("arxiv", "http://fake/arxiv")
    chat_id = _seed_chat()
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.get(f"/projects/{pid}/chats/{chat_id}")

    assert response.status_code == 200
    assert 'tool-chip--unavailable' in response.text


def test_healthy_probe_renders_on_chip(
    make_client: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server whose probe is True renders tool-chip--on (green)."""
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


def test_unknown_health_renders_grey(
    make_client: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown health (None — malformed URL or pre-cache) renders the neutral
    tool-chip--off state, never red."""
    async def _all_unknown(servers, *, force=False):
        return {s.name: None for s in servers}

    monkeypatch.setattr(rag_health, "get_health_map", _all_unknown)

    _seed_server("arxiv", "http://fake/arxiv")
    chat_id = _seed_chat()
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.get(f"/projects/{pid}/chats/{chat_id}")

    assert response.status_code == 200
    assert 'tool-chip--off' in response.text
    assert 'tool-chip--unavailable' not in response.text


# ---------------------------------------------------------------------------
# Composer cleanup
# ---------------------------------------------------------------------------


def test_composer_has_no_chip_inputs(
    make_client: ClientFactory,
) -> None:
    """Empty-state composer renders no tool/RAG chip inputs (phase 23 removed
    per-chat chips entirely)."""
    _seed_server("arxiv", "http://fake/arxiv")
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.get(f"/projects/{pid}/chats")

    assert response.status_code == 200
    assert 'name="enabled_rag_servers"' not in response.text
    assert 'name="enabled_tools"' not in response.text
