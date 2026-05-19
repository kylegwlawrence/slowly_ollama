"""Unit tests for ``app.rag_health.probe_rag_health``.

Uses ``httpx.MockTransport`` to canned-response a fake /health
endpoint so the tests are hermetic — no real network involved.
"""

import httpx
import pytest

from app import rag_health
from app.rag_health import _health_url, probe_rag_health


# ---------------------------------------------------------------------------
# URL derivation
# ---------------------------------------------------------------------------


def test_health_url_strips_path_and_appends_health() -> None:
    assert (
        _health_url("http://pop-os:8002/arxiv_rag")
        == "http://pop-os:8002/health"
    )


def test_health_url_preserves_scheme_and_port() -> None:
    assert (
        _health_url("https://10.0.0.5:9000/whatever/sub/path")
        == "https://10.0.0.5:9000/health"
    )


def test_health_url_trims_whitespace() -> None:
    assert (
        _health_url("  http://pop-os:8002/arxiv_rag  ")
        == "http://pop-os:8002/health"
    )


def test_health_url_returns_none_on_missing_scheme() -> None:
    assert _health_url("pop-os:8002/arxiv_rag") is None


def test_health_url_returns_none_on_missing_host() -> None:
    assert _health_url("http:///path") is None


# ---------------------------------------------------------------------------
# probe_rag_health — uses MockTransport to fake the /health endpoint
# ---------------------------------------------------------------------------


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> None:
    """Patch ``httpx.AsyncClient`` so the probe uses our MockTransport.

    The probe constructs its own client inline; we wrap the constructor
    so it always picks up the mock transport without changing the call
    site under test.
    """
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(rag_health.httpx, "AsyncClient", _factory)


@pytest.mark.asyncio
async def test_probe_returns_true_on_healthy_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path: name is present and reports 'ok'."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://pop-os:8002/health"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "databases": {
                    "arxiv_rag": "ok",
                    "factbook_rag": "ok",
                },
            },
        )

    _install_transport(monkeypatch, handler)

    healthy, reason = await probe_rag_health(
        "arxiv_rag", "http://pop-os:8002/arxiv_rag"
    )
    assert healthy is True
    assert reason == ""


@pytest.mark.asyncio
async def test_probe_fails_when_name_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Name that ends in _rag but isn't in /health → lists available _rag dbs."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "databases": {
                    "arxiv": "ok",      # non-rag sibling — should NOT appear
                    "arxiv_rag": "ok",
                    "factbook_rag": "ok",
                },
            },
        )

    _install_transport(monkeypatch, handler)

    healthy, reason = await probe_rag_health(
        "bogus_rag", "http://pop-os:8002/bogus_rag"
    )
    assert healthy is False
    assert "'bogus_rag' not found" in reason
    # Reason lists the available _rag databases from the live response.
    assert "arxiv_rag" in reason
    assert "factbook_rag" in reason
    # Non-RAG siblings (e.g. plain "arxiv") are filtered out of the
    # suggestion list — they're not valid targets for query_rag.
    assert "Available RAG databases: arxiv_rag, factbook_rag" in reason


@pytest.mark.asyncio
async def test_probe_rejects_non_rag_name_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name lacking the _rag suffix fails BEFORE the /health round-trip.

    The remote reports both ``arxiv`` and ``arxiv_rag`` as healthy, but
    only the latter has the /chunks API ``query_rag`` knows how to call.
    We reject plain ``arxiv`` even though /health would call it healthy
    — and we do so without hitting the network.
    """
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"databases": {"arxiv": "ok"}})

    _install_transport(monkeypatch, handler)

    healthy, reason = await probe_rag_health(
        "arxiv", "http://pop-os:8002/arxiv"
    )
    assert healthy is False
    assert "must end in '_rag'" in reason
    assert called is False, "suffix check must fail-fast without network call"


@pytest.mark.asyncio
async def test_probe_fails_when_status_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "databases": {"arxiv_rag": "degraded"}},
        )

    _install_transport(monkeypatch, handler)

    healthy, reason = await probe_rag_health(
        "arxiv_rag", "http://pop-os:8002/arxiv_rag"
    )
    assert healthy is False
    assert "not healthy" in reason
    assert "degraded" in reason


@pytest.mark.asyncio
async def test_probe_fails_on_non_2xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _install_transport(monkeypatch, handler)

    healthy, reason = await probe_rag_health(
        "arxiv_rag", "http://pop-os:8002/arxiv_rag"
    )
    assert healthy is False
    assert "HTTP 500" in reason


@pytest.mark.asyncio
async def test_probe_fails_on_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_transport(monkeypatch, handler)

    healthy, reason = await probe_rag_health(
        "arxiv_rag", "http://pop-os:8002/arxiv_rag"
    )
    assert healthy is False
    assert "unreachable" in reason
    assert "http://pop-os:8002/health" in reason


@pytest.mark.asyncio
async def test_probe_fails_on_non_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    _install_transport(monkeypatch, handler)

    healthy, reason = await probe_rag_health(
        "arxiv_rag", "http://pop-os:8002/arxiv_rag"
    )
    assert healthy is False
    assert "non-JSON" in reason


@pytest.mark.asyncio
async def test_probe_fails_when_databases_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    _install_transport(monkeypatch, handler)

    healthy, reason = await probe_rag_health(
        "arxiv_rag", "http://pop-os:8002/arxiv_rag"
    )
    assert healthy is False
    assert "missing 'databases' map" in reason


@pytest.mark.asyncio
async def test_probe_rejects_malformed_url() -> None:
    """A URL without scheme/host short-circuits before any HTTP call."""
    healthy, reason = await probe_rag_health(
        "arxiv_rag", "not-a-real-url"
    )
    assert healthy is False
    assert "scheme and host" in reason
