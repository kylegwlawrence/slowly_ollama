"""Unit tests for ``app.rag_sources`` — /sources fetch + name matching.

The async ``fetch_sources`` is exercised with ``httpx.MockTransport`` (same
hermetic pattern as ``tests/test_rag_health.py``); the pure helpers
(``host_root`` / ``_source_key`` / ``description_for``) are tested directly.
"""

import httpx
import pytest

from app import rag_sources
from app.rag_sources import (
    _source_key,
    _sources_url,
    description_for,
    fetch_sources,
    host_root,
)


# ---------------------------------------------------------------------------
# URL derivation
# ---------------------------------------------------------------------------


def test_host_root_strips_path() -> None:
    assert host_root("http://pop-os:8002/arxiv_rag") == "http://pop-os:8002"


def test_host_root_none_on_missing_scheme() -> None:
    assert host_root("pop-os:8002/arxiv_rag") is None


def test_sources_url_appends_sources() -> None:
    assert (
        _sources_url("http://pop-os:8002/arxiv_rag")
        == "http://pop-os:8002/sources"
    )


def test_sources_url_none_on_malformed() -> None:
    assert _sources_url("not-a-url") is None


# ---------------------------------------------------------------------------
# Name → source-id matching
# ---------------------------------------------------------------------------


def test_source_key_strips_rag_suffix() -> None:
    assert _source_key("arxiv_rag") == "arxiv"


def test_source_key_lowercases() -> None:
    assert _source_key("OpenAlex_RAG") == "openalex"


def test_source_key_keeps_name_without_suffix() -> None:
    assert _source_key("openalex") == "openalex"


# ---------------------------------------------------------------------------
# description_for
# ---------------------------------------------------------------------------


_SOURCES = {
    "arxiv": {
        "id": "arxiv",
        "description": "Cutting-edge research papers.",
        "timeframe": "1991–current",
    },
    "openstax": {
        "id": "openstax",
        "description": "College textbooks.",
        "timeframe": "",
    },
    "blank": {"id": "blank", "description": "", "timeframe": "2020"},
}


def test_description_for_appends_timeframe() -> None:
    assert (
        description_for("arxiv_rag", _SOURCES)
        == "Cutting-edge research papers. (1991–current)"
    )


def test_description_for_omits_blank_timeframe() -> None:
    assert description_for("openstax_rag", _SOURCES) == "College textbooks."


def test_description_for_unmatched_name_returns_none() -> None:
    assert description_for("nonexistent_rag", _SOURCES) is None


def test_description_for_blank_description_returns_none() -> None:
    # An entry with a timeframe but no description text is treated as
    # unmatched rather than writing a bare "(2020)".
    assert description_for("blank_rag", _SOURCES) is None


def test_description_for_truncates_at_cap() -> None:
    long = {"id": "x", "description": "y" * 500, "timeframe": ""}
    result = description_for("x_rag", {"x": long})
    assert result is not None
    assert len(result) == 400


# ---------------------------------------------------------------------------
# fetch_sources — MockTransport, mirroring test_rag_health
# ---------------------------------------------------------------------------


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Patch ``httpx.AsyncClient`` so fetch_sources uses our MockTransport."""
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(rag_sources.httpx, "AsyncClient", _factory)


@pytest.mark.asyncio
async def test_fetch_sources_returns_id_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://pop-os:8002/sources"
        return httpx.Response(
            200,
            json={
                "items": [
                    {"id": "arxiv", "description": "papers"},
                    {"id": "OpenAlex", "description": "catalog"},
                ]
            },
        )

    _install_transport(monkeypatch, handler)

    result = await fetch_sources("http://pop-os:8002/arxiv_rag")
    assert result is not None
    # Keyed by lowercased id.
    assert set(result) == {"arxiv", "openalex"}
    assert result["arxiv"]["description"] == "papers"


@pytest.mark.asyncio
async def test_fetch_sources_none_on_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _install_transport(monkeypatch, handler)
    assert await fetch_sources("http://pop-os:8002/arxiv_rag") is None


@pytest.mark.asyncio
async def test_fetch_sources_none_on_non_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    _install_transport(monkeypatch, handler)
    assert await fetch_sources("http://pop-os:8002/arxiv_rag") is None


@pytest.mark.asyncio
async def test_fetch_sources_none_when_items_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sources": []})

    _install_transport(monkeypatch, handler)
    assert await fetch_sources("http://pop-os:8002/arxiv_rag") is None


@pytest.mark.asyncio
async def test_fetch_sources_none_on_malformed_url() -> None:
    # Short-circuits before any HTTP call (no transport installed).
    assert await fetch_sources("not-a-url") is None
