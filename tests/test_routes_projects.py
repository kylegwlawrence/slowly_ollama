"""Tests for phase-17 ``/projects`` HTTP endpoints.

Mirrors the shape of ``test_routes.py``: a TestClient backed by a
tempfile DB and a mocked Ollama (set tool-capable by default so the
chat-panel render doesn't require an extra ``/api/show`` stub).
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import ollama, queries
from app.connection import open_connection
from app.dependencies import get_ollama_client
from main import app


def _ollama_unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("ollama mock: no handler set for this test")


ClientFactory = Callable[
    [Callable[[httpx.Request], httpx.Response]], TestClient
]


@pytest.fixture
def make_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ClientFactory]:
    """TestClient factory with a fresh per-test DB and Ollama mock."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "chats.db"))
    monkeypatch.setenv("OLLAMA_HOST", "http://test")
    saved = dict(app.dependency_overrides)

    def _make(handler):
        mock_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://test"
        )
        app.dependency_overrides[get_ollama_client] = lambda: mock_client
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)


@pytest.fixture(autouse=True)
def _default_tool_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ``model_supports_tools`` → True for project route tests.

    Matches the convention in ``test_routes.py``: chat-panel renders
    expect the chat's model to be tool-capable; tests can re-patch as
    needed.
    """

    async def _capable(_client: object, _name: str) -> bool:
        return True

    monkeypatch.setattr(ollama, "model_supports_tools", _capable)


def _default_project_id() -> int:
    """Return the Default project's id from the test DB."""
    with open_connection(os.environ["DB_PATH"]) as conn:
        return conn.execute(
            "SELECT id FROM projects ORDER BY id LIMIT 1;"
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# Index + redirect
# ---------------------------------------------------------------------------


def test_get_root_redirects_to_projects(make_client: ClientFactory) -> None:
    """GET / 302s to /projects (phase 17's new home)."""
    with make_client(_ollama_unreachable) as client:
        # follow_redirects=False so we can assert the 302 itself.
        response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/projects"


def test_get_projects_renders_index(make_client: ClientFactory) -> None:
    """GET /projects renders the projects index page with the Default row."""
    with make_client(_ollama_unreachable) as client:
        response = client.get("/projects")
    assert response.status_code == 200
    # The page shell wraps the projects index.
    assert "<!DOCTYPE html>" in response.text
    assert 'class="projects-index"' in response.text
    # The migration-created Default project surfaces in the list.
    assert ">Default<" in response.text


def test_get_projects_htmx_returns_fragment(
    make_client: ClientFactory,
) -> None:
    """HTMX requests get just the projects-index fragment (no <html> shell)."""
    with make_client(_ollama_unreachable) as client:
        response = client.get(
            "/projects", headers={"HX-Request": "true"}
        )
    assert response.status_code == 200
    assert "<!DOCTYPE html>" not in response.text
    assert 'class="projects-index"' in response.text


def test_get_project_id_redirects_to_chats(
    make_client: ClientFactory,
) -> None:
    """GET /projects/{id} 302s to the Chats tab (canonical entry URL)."""
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        response = client.get(
            f"/projects/{pid}", follow_redirects=False
        )
    assert response.status_code == 302
    assert response.headers["location"] == f"/projects/{pid}/chats"


# ---------------------------------------------------------------------------
# Create / update / delete
# ---------------------------------------------------------------------------


def test_post_projects_creates_and_redirects(
    make_client: ClientFactory,
) -> None:
    """POST /projects creates a row and pushes /projects/{id}/chats via HX-Push-Url."""
    with make_client(_ollama_unreachable) as client:
        response = client.post(
            "/projects",
            data={"name": "Created", "description": "from the test"},
        )
    assert response.status_code == 201
    assert 'class="project-item"' in response.text
    assert ">Created<" in response.text
    # HX-Push-Url moves the address bar to the new project's chats tab.
    assert response.headers["HX-Push-Url"].startswith("/projects/")
    assert response.headers["HX-Push-Url"].endswith("/chats")


def test_post_projects_blank_name_400(make_client: ClientFactory) -> None:
    """An empty (or whitespace-only) name returns 400 with a plain reason."""
    with make_client(_ollama_unreachable) as client:
        response = client.post(
            "/projects", data={"name": "   ", "description": ""}
        )
    assert response.status_code == 400


def test_post_projects_name_collision_returns_409(
    make_client: ClientFactory,
) -> None:
    """A duplicate name returns 409 (UNIQUE constraint via IntegrityError)."""
    with make_client(_ollama_unreachable) as client:
        # "Default" already exists from the migration.
        response = client.post(
            "/projects", data={"name": "Default", "description": ""}
        )
    assert response.status_code == 409


def test_patch_project_updates_fields_and_clears_defaults(
    make_client: ClientFactory,
) -> None:
    """PATCH /projects/{id} updates editable fields and clears optional defaults."""
    with make_client(_ollama_unreachable) as client:
        create = client.post(
            "/projects", data={"name": "Editable"}
        )
        assert create.status_code == 201
        # Extract id from the rendered row.
        marker = 'data-project-id="'
        start = create.text.index(marker) + len(marker)
        end = create.text.index('"', start)
        pid = int(create.text[start:end])
        # First: set a default_model.
        resp = client.patch(
            f"/projects/{pid}",
            data={
                "name": "Editable",
                "description": "set",
                "default_model": "llama3",
                "default_agent": "",
            },
        )
        assert resp.status_code == 200
        assert 'value="llama3"' in resp.text
        # Then: clear default_model by submitting an empty string.
        resp2 = client.patch(
            f"/projects/{pid}",
            data={
                "name": "Editable",
                "description": "set",
                "default_model": "",
                "default_agent": "",
            },
        )
        assert resp2.status_code == 200
        # default_model now empty in the rendered form.
        assert 'name="default_model"' in resp2.text


def test_patch_project_missing_404(make_client: ClientFactory) -> None:
    """PATCHing a non-existent project returns 404."""
    with make_client(_ollama_unreachable) as client:
        response = client.patch(
            "/projects/99999", data={"name": "x"}
        )
    assert response.status_code == 404


def test_delete_project_cascades_and_redirects(
    make_client: ClientFactory,
) -> None:
    """DELETE /projects/{id} removes the project + cascades chats, sets HX-Location."""
    with make_client(_ollama_unreachable) as client:
        create = client.post("/projects", data={"name": "Doomed"})
        marker = 'data-project-id="'
        start = create.text.index(marker) + len(marker)
        end = create.text.index('"', start)
        pid = int(create.text[start:end])
        response = client.delete(f"/projects/{pid}")
    assert response.status_code == 200
    assert response.headers.get("HX-Location") == "/projects"


def test_delete_last_project_returns_409(
    make_client: ClientFactory,
) -> None:
    """Refusing to delete the last project: response is 409 with a reason."""
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        response = client.delete(f"/projects/{pid}")
    assert response.status_code == 409


def test_delete_project_missing_404(make_client: ClientFactory) -> None:
    """DELETing a non-existent project returns 404."""
    with make_client(_ollama_unreachable) as client:
        response = client.delete("/projects/99999")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


def test_get_project_chats_no_active_chat_renders_composer(
    make_client: ClientFactory,
) -> None:
    """The Chats tab with no chat open shows the empty-state composer."""
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        response = client.get(f"/projects/{pid}/chats")
    assert response.status_code == 200
    assert 'class="composer"' in response.text
    assert f'hx-post="/projects/{pid}/chats"' in response.text


def test_get_project_chats_with_chat_renders_panel(
    make_client: ClientFactory,
) -> None:
    """A chat-id under the Chats tab renders the chat panel."""
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        # Pre-populate one chat row directly so we don't drive a stream.
        with open_connection(os.environ["DB_PATH"]) as conn:
            chat = queries.create_conversation(
                conn, name="open me", model="llama3", project_id=pid
            )
        response = client.get(f"/projects/{pid}/chats/{chat.id}")
    assert response.status_code == 200
    assert 'class="chat-panel"' in response.text


def test_get_project_chats_404_when_chat_not_in_project(
    make_client: ClientFactory,
) -> None:
    """Asking for a chat under a project that doesn't own it 404s."""
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        with open_connection(os.environ["DB_PATH"]) as conn:
            other = queries.create_project(conn, name="Other")
            chat = queries.create_conversation(
                conn, name="elsewhere", model="llama3",
                project_id=other.id,
            )
        response = client.get(f"/projects/{pid}/chats/{chat.id}")
    assert response.status_code == 404


def test_get_chats_id_legacy_redirects_to_project_url(
    make_client: ClientFactory,
) -> None:
    """Legacy /chats/{id} 302s to the project-scoped canonical URL."""
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        with open_connection(os.environ["DB_PATH"]) as conn:
            chat = queries.create_conversation(
                conn, name="legacy", model="llama3", project_id=pid
            )
        response = client.get(
            f"/chats/{chat.id}", follow_redirects=False
        )
    assert response.status_code == 302
    assert response.headers["location"] == (
        f"/projects/{pid}/chats/{chat.id}"
    )


def test_post_project_chats_creates_chat_in_project(
    make_client: ClientFactory,
) -> None:
    """POST /projects/{pid}/chats persists project_id on the new chat."""
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        response = client.post(
            f"/projects/{pid}/chats",
            data={"model": "llama3", "content": "hi"},
        )
    assert response.status_code == 201
    assert response.headers["HX-Push-Url"].startswith(
        f"/projects/{pid}/chats/"
    )
    # The chat's project_id row is set.
    with open_connection(os.environ["DB_PATH"]) as conn:
        row = conn.execute(
            "SELECT project_id FROM conversations ORDER BY id DESC LIMIT 1;"
        ).fetchone()
    assert row[0] == pid


# ---------------------------------------------------------------------------
# Files tab
# ---------------------------------------------------------------------------


def test_get_project_files_lists_workspace(
    make_client: ClientFactory, monkeypatch, tmp_path
) -> None:
    """Files tab lists the project's workspace contents.

    With FILE_TOOL_ROOT set, the project's workspace is available and a
    file written into it surfaces in the listing.
    """
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    monkeypatch.setenv("FILE_TOOL_ROOT", str(fs_root))
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        # The project's workspace is default/ — create + populate it.
        # The migrate_legacy_workspace lifespan hook may have already
        # created default/, so use exist_ok=True.
        default_ws = fs_root / "default"
        default_ws.mkdir(exist_ok=True)
        (default_ws / "note.md").write_text("hello")
        response = client.get(f"/projects/{pid}/files")
    assert response.status_code == 200
    assert "note.md" in response.text


def test_get_project_files_view_renders_text_file(
    make_client: ClientFactory, monkeypatch, tmp_path
) -> None:
    """The view endpoint renders a UTF-8 file's contents inside <pre>."""
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    monkeypatch.setenv("FILE_TOOL_ROOT", str(fs_root))
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        (fs_root / "default").mkdir(exist_ok=True)
        (fs_root / "default" / "demo.txt").write_text("text body")
        response = client.get(
            f"/projects/{pid}/files/view?path=demo.txt"
        )
    assert response.status_code == 200
    assert "text body" in response.text


def test_get_project_files_view_renders_markdown(
    make_client: ClientFactory, monkeypatch, tmp_path
) -> None:
    """A .md file renders to HTML via the markdown library."""
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    monkeypatch.setenv("FILE_TOOL_ROOT", str(fs_root))
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        (fs_root / "default").mkdir(exist_ok=True)
        (fs_root / "default" / "demo.md").write_text("# Title")
        response = client.get(
            f"/projects/{pid}/files/view?path=demo.md"
        )
    assert response.status_code == 200
    # Rendered <h1> proves the markdown library ran.
    assert "<h1>Title</h1>" in response.text


def test_get_project_files_download_streams_attachment(
    make_client: ClientFactory, monkeypatch, tmp_path
) -> None:
    """Download endpoint returns the file as an attachment."""
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    monkeypatch.setenv("FILE_TOOL_ROOT", str(fs_root))
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        (fs_root / "default").mkdir(exist_ok=True)
        (fs_root / "default" / "x.bin").write_bytes(b"\x00\x01\x02")
        response = client.get(
            f"/projects/{pid}/files/download?path=x.bin"
        )
    assert response.status_code == 200
    # FileResponse sets Content-Disposition: attachment by default
    # when filename= is passed.
    assert "attachment" in response.headers.get("content-disposition", "")


def test_get_project_files_path_traversal_rejected(
    make_client: ClientFactory, monkeypatch, tmp_path
) -> None:
    """A path that escapes the workspace returns an in-page error."""
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    monkeypatch.setenv("FILE_TOOL_ROOT", str(fs_root))
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        response = client.get(
            f"/projects/{pid}/files?path=../escape"
        )
    assert response.status_code == 200
    assert "outside the workspace" in response.text


# ---------------------------------------------------------------------------
# Settings tab
# ---------------------------------------------------------------------------


def test_get_project_settings_renders_form(
    make_client: ClientFactory,
) -> None:
    """The settings tab renders the editable form."""
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        response = client.get(f"/projects/{pid}/settings")
    assert response.status_code == 200
    assert 'class="project-settings"' in response.text
    assert 'name="default_model"' in response.text


def test_404_endpoints_for_unknown_project(
    make_client: ClientFactory,
) -> None:
    """Every project endpoint returns 404 for an unknown id."""
    with make_client(_ollama_unreachable) as client:
        assert client.get("/projects/99999/chats").status_code == 404
        assert client.get("/projects/99999/files").status_code == 404
        assert client.get("/projects/99999/settings").status_code == 404
        assert (
            client.get("/projects/99999/chats/new").status_code == 404
        )
        # File download / view for unknown project (404) and for unknown
        # path inside a known project (404).
        assert (
            client.get(
                "/projects/99999/files/download?path=x"
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/projects/99999/files/view?path=x"
            ).status_code
            == 404
        )
        # Unknown chat under a real project — also 404.
        pid = _default_project_id()
        assert (
            client.get(f"/projects/{pid}/chats/99999").status_code == 404
        )
        # POST to an unknown project also 404s.
        assert (
            client.post(
                "/projects/99999/chats",
                data={"model": "llama3", "content": "x"},
            ).status_code
            == 404
        )


def test_patch_project_leaves_unsubmitted_fields_alone(
    make_client: ClientFactory,
) -> None:
    """Updating with a partial form leaves omitted defaults untouched.

    Exercises the ``_UNSET`` sentinel branch — when ``default_model`` is
    absent from the form, the route must NOT clear an existing value.
    """
    with make_client(_ollama_unreachable) as client:
        # Set up: create a project with a default_model populated.
        with open_connection(os.environ["DB_PATH"]) as conn:
            p = queries.create_project(
                conn, name="Partial", default_model="llama3"
            )
        # PATCH with only `name` in the body — default_model not present.
        response = client.patch(
            f"/projects/{p.id}", data={"name": "Renamed Partial"}
        )
    assert response.status_code == 200
    # The pre-existing default_model value is preserved.
    assert 'value="llama3"' in response.text


def test_download_endpoint_400_when_file_tool_root_unset(
    make_client: ClientFactory, monkeypatch
) -> None:
    """The download endpoint returns 400 when FILE_TOOL_ROOT is unset."""
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        response = client.get(
            f"/projects/{pid}/files/download?path=anything"
        )
    assert response.status_code == 400


def test_download_endpoint_404_for_missing_path(
    make_client: ClientFactory, monkeypatch, tmp_path
) -> None:
    """The download endpoint returns 404 for a path inside the workspace
    that doesn't actually exist."""
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    monkeypatch.setenv("FILE_TOOL_ROOT", str(fs_root))
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        response = client.get(
            f"/projects/{pid}/files/download?path=missing.txt"
        )
    assert response.status_code == 404


def test_files_unavailable_when_file_tool_root_unset(
    make_client: ClientFactory, monkeypatch
) -> None:
    """The Files tab surfaces a 'not configured' message when FILE_TOOL_ROOT is unset."""
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        response = client.get(f"/projects/{pid}/files")
    assert response.status_code == 200
    assert "not configured" in response.text


def test_file_view_unavailable_when_file_tool_root_unset(
    make_client: ClientFactory, monkeypatch
) -> None:
    """File view endpoint surfaces 'not configured' when FILE_TOOL_ROOT is unset."""
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        response = client.get(
            f"/projects/{pid}/files/view?path=any.txt"
        )
    assert response.status_code == 200
    assert "not configured" in response.text


def test_files_directory_not_found(
    make_client: ClientFactory, monkeypatch, tmp_path
) -> None:
    """An existing workspace + missing subdirectory surfaces 'Directory not found'."""
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    monkeypatch.setenv("FILE_TOOL_ROOT", str(fs_root))
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        response = client.get(
            f"/projects/{pid}/files?path=missing-subdir"
        )
    assert response.status_code == 200
    assert "Directory not found" in response.text


def test_file_view_404_for_missing_path(
    make_client: ClientFactory, monkeypatch, tmp_path
) -> None:
    """File view of a missing path surfaces 'File not found' (200 + message)."""
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    monkeypatch.setenv("FILE_TOOL_ROOT", str(fs_root))
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        response = client.get(
            f"/projects/{pid}/files/view?path=does-not-exist.txt"
        )
    assert response.status_code == 200
    assert "File not found" in response.text


def test_file_view_binary_file_shows_use_download(
    make_client: ClientFactory, monkeypatch, tmp_path
) -> None:
    """Binary files surface a 'use Download' message, not corrupted text."""
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    monkeypatch.setenv("FILE_TOOL_ROOT", str(fs_root))
    with make_client(_ollama_unreachable) as client:
        pid = _default_project_id()
        (fs_root / "default").mkdir(exist_ok=True)
        (fs_root / "default" / "blob.bin").write_bytes(
            b"\xff\xfe\xfd\xfc"
        )
        response = client.get(
            f"/projects/{pid}/files/view?path=blob.bin"
        )
    assert response.status_code == 200
    assert "Binary file" in response.text
