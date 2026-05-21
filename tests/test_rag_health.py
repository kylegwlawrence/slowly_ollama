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
    """Name that isn't in /health → error lists every available database."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "databases": {
                    "arxiv": "ok",
                    "arxiv_rag": "ok",
                    "factbook": "ok",
                },
            },
        )

    _install_transport(monkeypatch, handler)

    healthy, reason = await probe_rag_health(
        "bogus", "http://pop-os:8002/bogus"
    )
    assert healthy is False
    assert "'bogus' not found" in reason
    # Reason lists every database the live response reports, sorted, so
    # the user can correct a typo against the real set.
    assert "Available databases: arxiv, arxiv_rag, factbook" in reason


@pytest.mark.asyncio
async def test_probe_accepts_plain_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain (non-_rag) name passes when /health reports it healthy.

    The remote's queryable ``/chunks`` endpoints live at the plain
    database names (e.g. ``arxiv``, ``pydocs``); the ``_rag`` siblings
    404 on ``/chunks``. The probe must therefore accept plain names
    rather than demand a ``_rag`` suffix.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://pop-os:8002/health"
        return httpx.Response(
            200,
            json={"ok": True, "databases": {"pydocs": "ok", "pydocs_rag": "ok"}},
        )

    _install_transport(monkeypatch, handler)

    healthy, reason = await probe_rag_health(
        "pydocs", "http://pop-os:8002/pydocs"
    )
    assert healthy is True
    assert reason == ""


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
    """A 5xx with no usable databases map fails with the HTTP status."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _install_transport(monkeypatch, handler)

    healthy, reason = await probe_rag_health(
        "arxiv", "http://pop-os:8002/arxiv"
    )
    assert healthy is False
    assert "HTTP 500" in reason


@pytest.mark.asyncio
async def test_probe_passes_on_503_when_target_db_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 caused by a broken SIBLING db must not block a healthy one.

    The shared /health endpoint returns ``"ok": false`` + HTTP 503 when
    any hosted database is unhealthy. The map still reports the rest
    correctly, so a healthy target (``pydocs``) should pass even though
    a sibling (``wikihow_rag``) is erroring.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "ok": False,
                "databases": {
                    "pydocs": "ok",
                    "wikihow_rag": "error: 503: db not available",
                },
            },
        )

    _install_transport(monkeypatch, handler)

    healthy, reason = await probe_rag_health(
        "pydocs", "http://pop-os:8002/pydocs"
    )
    assert healthy is True
    assert reason == ""


@pytest.mark.asyncio
async def test_probe_fails_on_503_when_target_db_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 where the TARGET db is the broken one still fails — with its
    per-database status, not the bare HTTP code."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "ok": False,
                "databases": {
                    "pydocs": "ok",
                    "wikihow_rag": "error: 503: db not available",
                },
            },
        )

    _install_transport(monkeypatch, handler)

    healthy, reason = await probe_rag_health(
        "wikihow_rag", "http://pop-os:8002/wikihow_rag"
    )
    assert healthy is False
    assert "not healthy" in reason
    assert "db not available" in reason


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
