"""Tests for the ``fetch_github_file`` tool."""

import httpx
import pytest

from app.tools import github as _github  # noqa: F401 — registers tool
from app.tools.github import _to_raw_url, fetch_github_file


def test_fetch_github_file_registered_in_tools() -> None:
    """fetch_github_file registers into the live TOOLS registry.

    Phase 23 offers a tool-capable model the full registry every turn (no
    allowlist / chip filtering), so registration is all that gates a tool's
    availability. This fails if the @tool decorator stops running.
    """
    from app.tools import TOOLS

    assert "fetch_github_file" in TOOLS


def test_to_raw_url_passes_raw_through() -> None:
    """Raw URLs are already in target form — returned unchanged."""
    url = "https://raw.githubusercontent.com/octocat/Hello/main/README.md"
    assert _to_raw_url(url) == url


def test_to_raw_url_rewrites_blob_to_raw() -> None:
    """Web-UI blob URLs are rewritten to raw.githubusercontent.com."""
    blob = "https://github.com/octocat/Hello/blob/main/README.md"
    assert _to_raw_url(blob) == (
        "https://raw.githubusercontent.com/octocat/Hello/main/README.md"
    )


def test_to_raw_url_rejects_non_file_urls() -> None:
    """Anything not matching the blob or raw shape returns None."""
    assert _to_raw_url("https://github.com/octocat/Hello") is None
    assert _to_raw_url("https://example.com/foo.txt") is None
    assert _to_raw_url("not a url") is None


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> None:
    """Replace ``httpx.AsyncClient`` inside the github tool with a mocked one.

    Mirrors the pattern in test_tools.py: snapshot the real class before
    patching so the fake can still build a real client underneath
    without recursing into its own monkeypatch.
    """
    real_async_client = httpx.AsyncClient

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self._client = real_async_client(
                transport=httpx.MockTransport(handler)
            )

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, *exc):
            await self._client.aclose()

    monkeypatch.setattr(_github.httpx, "AsyncClient", _FakeClient)


@pytest.mark.asyncio
async def test_fetch_returns_file_text_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 response with text body comes back verbatim."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, content=b"hello world")

    _install_mock_transport(monkeypatch, handler)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    out = await fetch_github_file(
        url="https://raw.githubusercontent.com/o/r/main/README.md"
    )
    assert out == "hello world"
    assert captured["url"] == (
        "https://raw.githubusercontent.com/o/r/main/README.md"
    )
    # No token configured → no Authorization header.
    assert captured["auth"] is None


@pytest.mark.asyncio
async def test_fetch_normalizes_blob_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blob URL is rewritten before the HTTP call."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, content=b"ok")

    _install_mock_transport(monkeypatch, handler)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    await fetch_github_file(
        url="https://github.com/o/r/blob/main/file.py"
    )
    assert captured["url"] == (
        "https://raw.githubusercontent.com/o/r/main/file.py"
    )


@pytest.mark.asyncio
async def test_fetch_sends_token_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GITHUB_TOKEN rides along in the Authorization header."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, content=b"ok")

    _install_mock_transport(monkeypatch, handler)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")

    await fetch_github_file(
        url="https://raw.githubusercontent.com/o/r/main/f.txt"
    )
    assert captured["auth"] == "Bearer ghp_secret"


@pytest.mark.asyncio
async def test_fetch_rejects_unrecognized_url() -> None:
    """A non-GitHub URL returns an explanatory error string, no HTTP call."""
    out = await fetch_github_file(url="https://example.com/foo.py")
    assert "Not a recognized GitHub file URL" in out


@pytest.mark.asyncio
async def test_fetch_handles_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 → friendly 'File not found' message including the URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _install_mock_transport(monkeypatch, handler)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    url = "https://raw.githubusercontent.com/o/r/main/missing.md"
    out = await fetch_github_file(url=url)
    assert "not found" in out.lower()
    assert url in out


@pytest.mark.asyncio
async def test_fetch_handles_rate_limit_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 + X-RateLimit-Remaining: 0 + no token → hint to set GITHUB_TOKEN."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, headers={"X-RateLimit-Remaining": "0"}
        )

    _install_mock_transport(monkeypatch, handler)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    out = await fetch_github_file(
        url="https://raw.githubusercontent.com/o/r/main/f.txt"
    )
    assert "rate limit" in out.lower()
    assert "GITHUB_TOKEN" in out


@pytest.mark.asyncio
async def test_fetch_handles_rate_limit_with_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 + rate-limit while authenticated → no hint about setting the token."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, headers={"X-RateLimit-Remaining": "0"}
        )

    _install_mock_transport(monkeypatch, handler)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")

    out = await fetch_github_file(
        url="https://raw.githubusercontent.com/o/r/main/f.txt"
    )
    assert "rate limit" in out.lower()
    # Token already set → don't suggest setting it again.
    assert "Set GITHUB_TOKEN" not in out


@pytest.mark.asyncio
async def test_fetch_handles_generic_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-404/403 4xx surfaces the status code so the model can react."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(451)

    _install_mock_transport(monkeypatch, handler)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    out = await fetch_github_file(
        url="https://raw.githubusercontent.com/o/r/main/f.txt"
    )
    assert "451" in out


@pytest.mark.asyncio
async def test_fetch_truncates_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files larger than _FETCH_CAP are truncated with a trailing notice."""
    big = ("x" * 150_000).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big)

    _install_mock_transport(monkeypatch, handler)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    out = await fetch_github_file(
        url="https://raw.githubusercontent.com/o/r/main/huge.txt"
    )
    assert "[truncated:" in out
    # Body is capped to _FETCH_CAP (100k) plus the trailing notice.
    assert len(out) < 101_000


@pytest.mark.asyncio
async def test_fetch_handles_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx.HTTPError → 'GitHub fetch failed' rather than raising."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _install_mock_transport(monkeypatch, handler)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    out = await fetch_github_file(
        url="https://raw.githubusercontent.com/o/r/main/f.txt"
    )
    assert "fetch failed" in out.lower()
