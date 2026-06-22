"""Phase 24: the sidebar carries two always-visible reference lists below the
projects list — every configured RAG server ("Sources") and every registered
tool ("Tools"). This replaced the chat-gated Sources health panel, so the lists:

- render on every full page (projects index, project/chat page, settings),
- are chat-independent (no active-chat / tool-capable-model gating),
- are health-free (no green/red/grey dots, no toggle endpoint),
- still render when no RAG servers are configured (tools always exist).

Re-uses ``make_client`` and the tool-capable model stub from
``tests/test_routes.py`` so each test gets a fresh DB + mocked Ollama identical
to the rest of the suite.
"""

import os
from contextlib import closing
from pathlib import Path

from app import queries
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


def _seed_chat(*, model: str = "llama3") -> int:
    """Create a chat row directly in the DB; return the chat id.

    Bypasses the create-chat endpoint (which spawns a generation that would
    consume the test's mock). Used by tests that navigate to an open chat.
    """
    db_path = Path(os.environ["DB_PATH"])
    initialize_database(db_path)
    with closing(open_connection(db_path)) as conn:
        project_id = conn.execute(
            "SELECT id FROM projects ORDER BY id LIMIT 1;"
        ).fetchone()[0]
        chat = queries.create_conversation(
            conn, name="test", model=model, project_id=project_id
        )
    return chat.id


def _seed_server(name: str = "arxiv", url: str = "http://fake/arxiv") -> None:
    db_path = Path(os.environ["DB_PATH"])
    initialize_database(db_path)
    with closing(open_connection(db_path)) as conn:
        _rag_servers.create_server(conn, name, url)


# ---------------------------------------------------------------------------
# Always-visible across pages
# ---------------------------------------------------------------------------


def test_reference_section_on_projects_index(
    make_client: ClientFactory,
) -> None:
    """Projects index (no active chat): both lists render with their headings."""
    _seed_server("arxiv", "http://fake/arxiv")

    with make_client(_tool_capable_handler) as client:
        response = client.get("/projects")

    assert response.status_code == 200
    assert 'id="sidebar-reference"' in response.text
    assert ">Sources<" in response.text
    assert ">Tools<" in response.text
    # Server label (Sources) + an always-registered tool (Tools).
    assert ">arxiv<" in response.text
    assert ">current_time<" in response.text


def test_reference_section_on_settings_page(
    make_client: ClientFactory,
) -> None:
    """Settings is its own layout but still renders the sidebar reference."""
    _seed_server("arxiv", "http://fake/arxiv")

    with make_client(_tool_capable_handler) as client:
        response = client.get("/settings")

    assert response.status_code == 200
    assert 'id="sidebar-reference"' in response.text
    assert ">arxiv<" in response.text
    assert ">current_time<" in response.text


def test_reference_section_on_chat_page(
    make_client: ClientFactory,
) -> None:
    """An open chat page renders the same always-visible reference lists."""
    _seed_server("arxiv", "http://fake/arxiv")
    chat_id = _seed_chat()
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.get(f"/projects/{pid}/chats/{chat_id}")

    assert response.status_code == 200
    assert 'id="sidebar-reference"' in response.text
    assert ">arxiv<" in response.text
    assert ">current_time<" in response.text


def test_reference_section_renders_once_per_full_page(
    make_client: ClientFactory,
) -> None:
    """Regression guard: the section renders exactly once on a plain load —
    the projects-index sidebar must not duplicate it."""
    _seed_server("arxiv", "http://fake/arxiv")

    with make_client(_tool_capable_handler) as client:
        response = client.get("/projects")

    assert response.status_code == 200
    assert response.text.count('id="sidebar-reference"') == 1


# ---------------------------------------------------------------------------
# Server label formatting + empty state
# ---------------------------------------------------------------------------


def test_server_label_strips_rag_suffix(
    make_client: ClientFactory,
) -> None:
    """A ``foo_rag`` server is labelled ``foo`` (matches the old panel)."""
    _seed_server("pydocs_rag", "http://fake/pydocs_rag")

    with make_client(_tool_capable_handler) as client:
        response = client.get("/projects")

    assert response.status_code == 200
    # The chip label is the stripped name; the full ``_rag`` form only
    # survives in the title attribute (the server URL), never as the label.
    assert ">pydocs<" in response.text
    assert ">pydocs_rag<" not in response.text


def test_sources_empty_state_when_no_servers(
    make_client: ClientFactory,
) -> None:
    """Zero RAG servers: Sources shows the empty hint, Tools still lists tools."""
    with make_client(_tool_capable_handler) as client:
        response = client.get("/projects")

    assert response.status_code == 200
    assert 'id="sidebar-reference"' in response.text
    assert "None configured" in response.text
    # Tools never empties — current_time is always registered.
    assert ">current_time<" in response.text


# ---------------------------------------------------------------------------
# No health / no toggles (the panel this replaced is gone)
# ---------------------------------------------------------------------------


def test_reference_section_has_no_health_or_toggle_markup(
    make_client: ClientFactory,
) -> None:
    """The lists are informational labels — no red health chip, no per-server
    toggle endpoint."""
    _seed_server("arxiv", "http://fake/arxiv")
    chat_id = _seed_chat()
    pid = _default_project_id()

    with make_client(_tool_capable_handler) as client:
        response = client.get(f"/projects/{pid}/chats/{chat_id}")

    assert response.status_code == 200
    assert "tool-chip--unavailable" not in response.text
    assert "/rag-servers/" not in response.text
    assert 'id="sidebar-rag-section"' not in response.text
