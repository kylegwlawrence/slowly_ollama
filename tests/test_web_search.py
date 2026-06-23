"""Phase 27: web_search tool tests (mock-only — never hits a real SearXNG).

Mirrors the query_rag tests in test_tools.py: patch ``httpx.AsyncClient``
inside ``app.tools.web_search`` with a ``MockTransport``-backed stand-in so
no network traffic occurs. The autouse ``_isolate_module_state`` fixture in
conftest.py snapshots/restores the ``web_search`` registry entry, so the
registration-gating tests here don't leak state into other tests.
"""

from collections.abc import Callable

import httpx
import pytest

from app.tools import TOOLS, Source, ToolResult
from app.tools.web_search import (
    _TOP_K,
    _TOTAL_OUTPUT_CAP,
    refresh_web_search_registration,
    web_search,
)


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Patch ``app.tools.web_search.httpx.AsyncClient`` to route via a mock.

    The tool uses ``async with httpx.AsyncClient(timeout=...) as client``, so
    the stand-in only needs the async-context-manager protocol. The real
    AsyncClient is snapshotted BEFORE patching so the wrapper can build one
    underneath without recursing into the patched name.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        handler: A MockTransport handler mapping a request to a response.
    """
    from app.tools import web_search as _ws

    _real_async_client = httpx.AsyncClient

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self._client = _real_async_client(
                transport=httpx.MockTransport(handler)
            )

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, *exc):
            await self._client.aclose()

    monkeypatch.setattr(_ws.httpx, "AsyncClient", _FakeClient)


@pytest.mark.asyncio
async def test_web_search_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured URL + mocked JSON → titles/URLs in text, sources populated."""
    monkeypatch.setenv("SEARXNG_URL", "http://fake:8888")

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Python 3.13", "url": "http://a", "content": "released"},
                    {"title": "Release notes", "url": "http://b", "content": "changelog"},
                    {"title": "Download", "url": "http://c", "content": "binaries"},
                ]
            },
        )

    _install_fake_client(monkeypatch, handler)

    result = await web_search(query="latest python")
    assert isinstance(result, ToolResult)
    # Built the right request: SearXNG /search with format=json.
    assert captured["url"].startswith("http://fake:8888/search")
    assert "format=json" in captured["url"]
    # Titles and URLs reach the model-facing text.
    assert "Python 3.13" in result.text
    assert "http://a" in result.text
    assert "released" in result.text
    # Structured sources surface each title for the tool card.
    assert result.sources == [
        Source(title="Python 3.13", section=None),
        Source(title="Release notes", section=None),
        Source(title="Download", section=None),
    ]


@pytest.mark.asyncio
async def test_web_search_empty_query_makes_no_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty query returns the rejection message and fires no request."""
    monkeypatch.setenv("SEARXNG_URL", "http://fake:8888")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("web_search must not hit the network on empty query")

    _install_fake_client(monkeypatch, handler)

    result = await web_search(query="   ")
    assert "'query' cannot be empty" in result.text
    assert result.sources == []


@pytest.mark.asyncio
async def test_web_search_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty results array returns the '(no results)' block, no sources."""
    monkeypatch.setenv("SEARXNG_URL", "http://fake:8888")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    _install_fake_client(monkeypatch, handler)

    result = await web_search(query="zxcvqwer no hits")
    assert result.text == "(no results)"
    assert result.sources == []


@pytest.mark.asyncio
async def test_web_search_caps_results_at_top_k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More than _TOP_K results in → only _TOP_K out, in text and sources."""
    monkeypatch.setenv("SEARXNG_URL", "http://fake:8888")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": f"Result {i}", "url": f"http://{i}", "content": "x"}
                    for i in range(20)
                ]
            },
        )

    _install_fake_client(monkeypatch, handler)

    result = await web_search(query="anything")
    assert len(result.sources) == _TOP_K
    # The (_TOP_K)th item is numbered [_TOP_K]; the next would be [_TOP_K + 1].
    assert f"[{_TOP_K}]" in result.text
    assert f"[{_TOP_K + 1}]" not in result.text


@pytest.mark.asyncio
async def test_web_search_truncates_long_snippet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An over-long snippet is truncated with an ellipsis."""
    monkeypatch.setenv("SEARXNG_URL", "http://fake:8888")
    long_snippet = "z" * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"title": "T", "url": "http://a", "content": long_snippet}]},
        )

    _install_fake_client(monkeypatch, handler)

    result = await web_search(query="q")
    assert "..." in result.text
    # The full untruncated snippet must not survive.
    assert long_snippet not in result.text


@pytest.mark.asyncio
async def test_web_search_total_cap_preserves_framing_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When output exceeds the total cap, the prepended framing note survives.

    Pins the §3 bug fix: the note is PREPENDED, not appended, so the
    tail-truncating total cap can't chop it off. Long titles (uncapped)
    push total output past _TOTAL_OUTPUT_CAP.
    """
    monkeypatch.setenv("SEARXNG_URL", "http://fake:8888")
    long_title = "T" * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": long_title, "url": f"http://{i}", "content": "c"}
                    for i in range(_TOP_K)
                ]
            },
        )

    _install_fake_client(monkeypatch, handler)

    result = await web_search(query="q")
    assert len(result.text) <= _TOTAL_OUTPUT_CAP
    # The framing note is at the very front despite tail truncation.
    assert result.text.startswith("(These are search-result snippets")


@pytest.mark.asyncio
async def test_web_search_403_points_at_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 returns the enable-json / relax-limiter guidance."""
    monkeypatch.setenv("SEARXNG_URL", "http://fake:8888")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    _install_fake_client(monkeypatch, handler)

    result = await web_search(query="q")
    assert "HTTP 403" in result.text
    assert "json" in result.text
    assert "limiter" in result.text
    assert result.sources == []


@pytest.mark.asyncio
async def test_web_search_other_4xx_returns_generic_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-403 error status returns the generic failed-HTTP message."""
    monkeypatch.setenv("SEARXNG_URL", "http://fake:8888")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _install_fake_client(monkeypatch, handler)

    result = await web_search(query="q")
    assert "failed (HTTP 500)" in result.text
    assert result.sources == []


@pytest.mark.asyncio
async def test_web_search_non_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 body that isn't JSON returns the enable-'json' message, no leak."""
    monkeypatch.setenv("SEARXNG_URL", "http://fake:8888")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    _install_fake_client(monkeypatch, handler)

    result = await web_search(query="q")
    assert "non-JSON" in result.text
    assert "json" in result.text
    assert "<html>" not in result.text
    assert result.sources == []


@pytest.mark.asyncio
async def test_web_search_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network-level failure returns the unreachable message, not a raise."""
    monkeypatch.setenv("SEARXNG_URL", "http://fake:8888")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    _install_fake_client(monkeypatch, handler)

    result = await web_search(query="q")
    assert "unreachable" in result.text
    assert result.sources == []


@pytest.mark.asyncio
async def test_web_search_unset_url_is_defensive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct call with SEARXNG_URL unset explains itself and makes no call."""
    monkeypatch.delenv("SEARXNG_URL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("web_search must not hit the network when unconfigured")

    _install_fake_client(monkeypatch, handler)

    result = await web_search(query="q")
    assert "not configured" in result.text
    assert "SEARXNG_URL" in result.text
    assert result.sources == []


def test_refresh_registration_removes_tool_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With SEARXNG_URL unset, web_search is removed from TOOLS."""
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    # Simulate a previously-registered state.
    TOOLS["web_search"] = _spec()
    refresh_web_search_registration()
    assert "web_search" not in TOOLS


def test_refresh_registration_readds_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With SEARXNG_URL set, refresh re-adds web_search and is idempotent."""
    monkeypatch.setenv("SEARXNG_URL", "http://fake:8888")
    TOOLS.pop("web_search", None)

    refresh_web_search_registration()
    assert "web_search" in TOOLS
    first = TOOLS["web_search"]

    # A second call is a no-op: same spec object, still present.
    refresh_web_search_registration()
    assert TOOLS["web_search"] is first


def _spec():
    """Return the snapshotted web_search ToolSpec for registry manipulation."""
    from app.tools.web_search import _WEB_SEARCH_SPEC

    return _WEB_SEARCH_SPEC
