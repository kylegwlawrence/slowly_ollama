"""Phase 21: the remote-backup status chip route + fragment.

Covers the GET /backup/status endpoint and the _backup_chip.html fragment:

- State → visual contract (tool-chip--* class, Material symbol, title).
- The self-polling trigger is present ONLY while a push is in flight
  (pending/pushing) and absent once settled (ok/offline/failed/idle), so the
  chip's poll self-stops.
- The whole chip is omitted when backups aren't configured.

Contract assertions (substrings of data-/hx-/class attributes), per the repo's
"pin contracts, not DOM shape" rule. Re-uses ``make_client`` from
``tests/test_routes.py`` for a fresh DB + mocked Ollama.
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from app import backup, generation
from app.generation import GenerationState

from tests.test_routes import (
    ClientFactory,
    _default_project_id,
    _ollama_unreachable,
    make_client,  # noqa: F401 — fixture re-export
)


def _trivial_handler(request: httpx.Request) -> httpx.Response:
    """Minimal Ollama stub — the status route makes no Ollama calls."""
    return httpx.Response(200, json={})


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure both remotes so backups_enabled() is True."""
    monkeypatch.setenv("REMOTE_PATH", "host1:/ws")
    monkeypatch.setenv("REMOTE_DB_PATH", "host1:/db")


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the remotes so backups_enabled() is False."""
    monkeypatch.delenv("REMOTE_PATH", raising=False)
    monkeypatch.delenv("REMOTE_DB_PATH", raising=False)


@pytest.fixture(autouse=True)
def _reset_status():
    """Reset the process-local backup status around each test."""
    backup._status = "idle"
    backup._status_at = None
    yield
    backup._status = "idle"
    backup._status_at = None


# ---------------------------------------------------------------------------
# Visual contract per state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state, css_class, symbol",
    [
        ("idle", "tool-chip--off", "cloud"),
        ("pending", "tool-chip--off", "progress_activity"),
        ("pushing", "tool-chip--off", "progress_activity"),
        ("ok", "tool-chip--on", "cloud_done"),
        ("offline", "tool-chip--off", "cloud_off"),
        ("failed", "tool-chip--unavailable", "cloud_off"),
    ],
)
def test_chip_renders_expected_class_and_symbol(
    make_client: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    css_class: str,
    symbol: str,
) -> None:
    """Each state maps to its tool-chip class + Material symbol."""
    _enable(monkeypatch)
    backup._status = state
    client = make_client(_trivial_handler)

    html = client.get("/backup/status").text

    assert 'id="backup-chip"' in html
    assert css_class in html
    assert f">{symbol}</span>" in html


# ---------------------------------------------------------------------------
# Self-stopping poll: trigger present only while busy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["pending", "pushing"])
def test_busy_states_arm_the_poll(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """While a push is in flight the chip re-requests itself every ~2s."""
    _enable(monkeypatch)
    backup._status = state
    client = make_client(_trivial_handler)

    html = client.get("/backup/status").text

    assert 'hx-get="/backup/status"' in html
    assert 'hx-trigger="load delay:2s"' in html
    assert "backup-chip__icon--spin" in html


@pytest.mark.parametrize("state", ["idle", "ok", "offline", "failed"])
def test_settled_states_stop_the_poll(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """A settled chip omits the trigger, so the poll halts."""
    _enable(monkeypatch)
    backup._status = state
    client = make_client(_trivial_handler)

    html = client.get("/backup/status").text

    assert "hx-trigger" not in html
    assert "backup-chip__icon--spin" not in html


# ---------------------------------------------------------------------------
# Gating: hidden when backups aren't configured
# ---------------------------------------------------------------------------


def test_chip_hidden_when_backups_disabled(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the remotes unset the fragment is empty (no chip element)."""
    _disable(monkeypatch)
    backup._status = "ok"  # even a non-idle status stays hidden
    client = make_client(_trivial_handler)

    html = client.get("/backup/status").text

    assert "backup-chip" not in html


# ---------------------------------------------------------------------------
# Send wiring: the message POST kicks the chip into its polling state
# ---------------------------------------------------------------------------


def _make_chat(client) -> int:
    """Create a chat via the project endpoint; return its id."""
    created = client.post(
        f"/projects/{_default_project_id()}/chats",
        data={"model": "llama3", "content": "hi"},
    )
    marker = 'data-chat-id="'
    start = created.text.index(marker) + len(marker)
    return int(created.text[start : created.text.index('"', start)])


def test_send_response_oob_swaps_polling_chip_when_enabled(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /messages returns an OOB backup chip armed to poll.

    ``request_backup`` is stubbed to set pending status synchronously without
    spawning the real ssh/rsync task — we only assert the response wiring.
    """
    _enable(monkeypatch)
    monkeypatch.setattr(backup, "request_backup", lambda reason: backup._set_status("pending"))

    with make_client(_ollama_unreachable) as client:
        chat_id = _make_chat(client)
        response = client.post(
            f"/chats/{chat_id}/messages", data={"content": "hello"}
        )

    text = response.text
    assert 'id="backup-chip"' in text
    assert 'hx-swap-oob="true"' in text
    # Pending → the chip carries the self-polling trigger.
    assert 'hx-get="/backup/status"' in text


def test_send_response_has_no_chip_when_backups_disabled(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With backups unconfigured the send response contains no backup chip."""
    _disable(monkeypatch)

    with make_client(_ollama_unreachable) as client:
        chat_id = _make_chat(client)
        response = client.post(
            f"/chats/{chat_id}/messages", data={"content": "hello"}
        )

    assert "backup-chip" not in response.text


# ---------------------------------------------------------------------------
# Chat-panel header include (globals available during a full panel render)
# ---------------------------------------------------------------------------


def test_chat_panel_header_includes_chip_when_enabled(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat-panel render embeds the chip via {% include %} + Jinja globals."""
    _enable(monkeypatch)
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            f"/projects/{_default_project_id()}/chats",
            data={"model": "llama3", "content": "hi"},
        )
    assert 'id="backup-chip"' in created.text


def test_chat_panel_header_omits_chip_when_disabled(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No chip in the chat-panel header when backups aren't configured."""
    _disable(monkeypatch)
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            f"/projects/{_default_project_id()}/chats",
            data={"model": "llama3", "content": "hi"},
        )
    assert "backup-chip" not in created.text


# ---------------------------------------------------------------------------
# Phase 22: the "Pull from mirror" button + POST /backup/pull
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_live_generations():
    """Keep the module-global generation registry empty around each test."""
    generation.live_generations.clear()
    yield
    generation.live_generations.clear()


def test_pull_button_in_header_when_enabled(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat-panel header carries the pull button (with its contract attrs)."""
    _enable(monkeypatch)
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            f"/projects/{_default_project_id()}/chats",
            data={"model": "llama3", "content": "hi"},
        )
    text = created.text
    assert 'id="pull-chip"' in text
    assert 'hx-post="/backup/pull"' in text
    assert "hx-confirm=" in text
    assert ">download</span>" in text


def test_pull_button_hidden_when_disabled(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No pull button — and POST /backup/pull 404s — when backups are off."""
    _disable(monkeypatch)
    with make_client(_ollama_unreachable) as client:
        created = client.post(
            f"/projects/{_default_project_id()}/chats",
            data={"model": "llama3", "content": "hi"},
        )
        assert "pull-chip" not in created.text
        assert client.post("/backup/pull").status_code == 404


def test_pull_success_redirects_and_reopens_db(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean pull → HX-Redirect to /projects and a usable (reopened) DB."""
    _enable(monkeypatch)
    monkeypatch.setattr(backup, "pull_all", AsyncMock(return_value=(True, None)))

    with make_client(_ollama_unreachable) as client:
        response = client.post("/backup/pull")
        assert response.headers.get("HX-Redirect") == "/projects"
        # The shared connection was closed for the pull, then reopened — a
        # query against it must succeed (else later requests would 500).
        assert client.app.state.db.execute("SELECT 1").fetchone()[0] == 1


def test_pull_refused_while_generating(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live producer → 409, the DB is left untouched, pull_all never runs."""
    _enable(monkeypatch)
    pull = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(backup, "pull_all", pull)
    generation.live_generations[999_999] = GenerationState(conversation_id=999_999)

    with make_client(_ollama_unreachable) as client:
        response = client.post("/backup/pull")

    assert response.status_code == 409
    assert 'id="pull-chip"' in response.text
    assert "Finish generating" in response.text
    pull.assert_not_awaited()


def test_pull_failure_renders_error_chip_without_redirect(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable mirror → red chip with the reason, and no redirect."""
    _enable(monkeypatch)
    monkeypatch.setattr(
        backup, "pull_all", AsyncMock(return_value=(False, "Mirror unreachable"))
    )

    with make_client(_ollama_unreachable) as client:
        response = client.post("/backup/pull")

    assert response.status_code == 200
    assert "HX-Redirect" not in response.headers
    assert "tool-chip--unavailable" in response.text
    assert "Mirror unreachable" in response.text
