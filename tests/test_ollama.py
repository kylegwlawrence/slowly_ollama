"""Tests for Phase 5: the async Ollama HTTP client.

Tests use ``httpx.MockTransport`` (built into httpx) to stand in for a real
Ollama server. The transport hands each request to a handler function that
the test owns, so we can assert on the outgoing request and shape the
response — no network, no subprocess, no fixture infrastructure.
"""

import json
from collections.abc import Callable

import httpx
import pytest

from app.ollama import (
    ChatChunk,
    OllamaModelMissing,
    OllamaProtocolError,
    OllamaUnavailable,
    TITLE_MODEL,
    create_client,
    generate_title,
    list_models,
    stream_chat,
)


def _client_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    """Build an ``AsyncClient`` backed by an in-process mock transport.

    Args:
        handler: A function that receives an ``httpx.Request`` and
            returns the ``httpx.Response`` we want Ollama to "send".
            The handler may also raise an ``httpx`` exception to
            simulate network-layer failures.

    Returns:
        A configured ``AsyncClient`` ready to use inside an
        ``async with``.
    """
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_models_returns_names_in_server_order() -> None:
    """list_models extracts the 'name' field and preserves server order."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "llama3:latest", "size": 1},
                    {"name": "qwen2.5:7b", "size": 2},
                ]
            },
        )

    async with _client_with(handler) as client:
        models = await list_models(client)

    assert models == ["llama3:latest", "qwen2.5:7b"]


@pytest.mark.asyncio
async def test_list_models_raises_when_ollama_unreachable() -> None:
    """ConnectError is wrapped as OllamaUnavailable."""

    def handler(request: httpx.Request) -> httpx.Response:
        # MockTransport lets the handler raise httpx exceptions to
        # simulate transport-layer failures like "connection refused."
        raise httpx.ConnectError("Connection refused")

    async with _client_with(handler) as client:
        with pytest.raises(OllamaUnavailable):
            await list_models(client)


@pytest.mark.asyncio
async def test_list_models_raises_on_5xx_response() -> None:
    """A non-2xx status from Ollama also surfaces as OllamaUnavailable."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _client_with(handler) as client:
        with pytest.raises(OllamaUnavailable):
            await list_models(client)


@pytest.mark.asyncio
async def test_list_models_preserves_underlying_exception_as_cause() -> None:
    """The wrapped error is reachable via __cause__ for logging."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    async with _client_with(handler) as client:
        with pytest.raises(OllamaUnavailable) as excinfo:
            await list_models(client)

    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


@pytest.mark.asyncio
async def test_list_models_raises_protocol_error_on_non_json_body() -> None:
    """A non-JSON response body (e.g. an HTML error page) raises
    OllamaProtocolError — distinct from OllamaUnavailable so the UI
    can present "Ollama spoke garbage" vs "Ollama isn't running"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    async with _client_with(handler) as client:
        with pytest.raises(OllamaProtocolError):
            await list_models(client)


@pytest.mark.asyncio
async def test_list_models_raises_protocol_error_on_unexpected_shape() -> None:
    """Valid JSON that's missing the 'models' field raises
    OllamaProtocolError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"wrong_key": []})

    async with _client_with(handler) as client:
        with pytest.raises(OllamaProtocolError):
            await list_models(client)


# ---------------------------------------------------------------------------
# stream_chat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_chat_yields_one_chunk_per_ndjson_line() -> None:
    """stream_chat parses NDJSON, yielding a ChatChunk per line."""
    body = (
        b'{"message":{"role":"assistant","content":"Hi"},"done":false}\n'
        b'{"message":{"role":"assistant","content":" there"},"done":false}\n'
        b'{"message":{"role":"assistant","content":""},"done":true}\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        # The outgoing payload should ask for streaming and include the
        # user's messages — verify the wire format here so we know
        # Phase 6 will pass something Ollama actually understands.
        payload = json.loads(request.content)
        assert payload["model"] == "llama3"
        assert payload["stream"] is True
        assert payload["messages"] == [
            {"role": "user", "content": "Hello"}
        ]
        return httpx.Response(200, content=body)

    async with _client_with(handler) as client:
        chunks = [
            chunk
            async for chunk in stream_chat(
                client,
                model="llama3",
                messages=[{"role": "user", "content": "Hello"}],
            )
        ]

    assert chunks == [
        ChatChunk(content="Hi", done=False),
        ChatChunk(content=" there", done=False),
        ChatChunk(content="", done=True),
    ]


@pytest.mark.asyncio
async def test_stream_chat_skips_blank_lines() -> None:
    """Empty lines between JSON objects are tolerated, not parsed."""
    body = (
        b'{"message":{"content":"A"},"done":false}\n'
        b'\n'  # stray blank line
        b'{"message":{"content":""},"done":true}\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with _client_with(handler) as client:
        chunks = [
            chunk
            async for chunk in stream_chat(
                client,
                model="llama3",
                messages=[{"role": "user", "content": "Hi"}],
            )
        ]

    # Two chunks, not three — the blank line was skipped.
    assert len(chunks) == 2
    assert chunks[-1].done is True


@pytest.mark.asyncio
async def test_stream_chat_raises_protocol_error_on_malformed_ndjson() -> None:
    """A non-JSON line in the stream raises OllamaProtocolError.

    The earlier valid line still yields normally; the error fires when
    the bad line is reached, so partial streams are surfaced before the
    error.
    """
    body = (
        b'{"message":{"content":"OK"},"done":false}\n'
        b'this is not valid json\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with _client_with(handler) as client:
        seen: list[ChatChunk] = []
        with pytest.raises(OllamaProtocolError):
            async for chunk in stream_chat(
                client,
                model="llama3",
                messages=[{"role": "user", "content": "Hi"}],
            ):
                seen.append(chunk)
        # The well-formed chunk before the bad line was yielded
        # successfully, then the parse error fired on the next line.
        assert seen == [ChatChunk(content="OK", done=False)]


@pytest.mark.asyncio
async def test_stream_chat_raises_when_ollama_unreachable() -> None:
    """Connection failures while streaming surface as OllamaUnavailable."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    async with _client_with(handler) as client:
        with pytest.raises(OllamaUnavailable):
            async for _ in stream_chat(
                client,
                model="llama3",
                messages=[{"role": "user", "content": "Hi"}],
            ):
                pass


# ---------------------------------------------------------------------------
# create_client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_client_raises_when_ollama_host_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No OLLAMA_HOST anywhere → KeyError at create_client call.

    There is no in-code fallback by design; the setup ritual is
    `cp .env.example .env` before first run.
    """
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    with pytest.raises(KeyError):
        create_client()


@pytest.mark.asyncio
async def test_create_client_uses_ollama_host_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OLLAMA_HOST env var sets the client's base URL.

    `monkeypatch.setenv` wins over the value loaded from .env at import
    time (python-dotenv doesn't override existing env vars), so the
    accessor in app.config sees the patched value.
    """
    monkeypatch.setenv("OLLAMA_HOST", "http://example.com:9999")

    async with create_client() as client:
        # httpx.URL components — comparing host/port avoids fragility
        # around trailing-slash normalization in the URL string.
        assert client.base_url.host == "example.com"
        assert client.base_url.port == 9999


# ---------------------------------------------------------------------------
# Phase 11d: generate_title
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_title_sends_history_and_title_request() -> None:
    """generate_title appends a user turn asking for a title and posts it.

    The handler captures the outgoing payload so we can assert the
    history was forwarded and a title-request turn was tacked onto
    the end.
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message": {"content": "A Concise Title"}}
        )

    async with _client_with(handler) as client:
        title = await generate_title(
            client,
            [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello!"},
            ],
        )

    assert title == "A Concise Title"
    assert captured["path"] == "/api/chat"
    # The configured TITLE_MODEL is used (not whatever the chat used).
    assert captured["body"]["model"] == TITLE_MODEL
    # Non-streaming — title responses are tiny, no need for SSE.
    assert captured["body"]["stream"] is False
    # History forwarded; a third (title-request) turn appended.
    msgs = captured["body"]["messages"]
    assert len(msgs) == 3
    assert msgs[0] == {"role": "user", "content": "Hi"}
    assert msgs[1] == {"role": "assistant", "content": "Hello!"}
    assert msgs[2]["role"] == "user"
    assert "Title" in msgs[2]["content"]


@pytest.mark.asyncio
async def test_generate_title_raises_model_missing_on_404() -> None:
    """A 404 from Ollama maps to OllamaModelMissing (not Unavailable).

    The UI uses this to surface the "install this model" banner
    instead of a generic "Ollama down" error.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    async with _client_with(handler) as client:
        with pytest.raises(OllamaModelMissing) as info:
            await generate_title(client, [])

    # The exception preserves the model name so the banner can name it.
    assert TITLE_MODEL in str(info.value.args[0])


@pytest.mark.asyncio
async def test_generate_title_raises_unavailable_on_5xx() -> None:
    """5xx → OllamaUnavailable (not protocol error)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _client_with(handler) as client:
        with pytest.raises(OllamaUnavailable):
            await generate_title(client, [])


@pytest.mark.asyncio
async def test_generate_title_strips_quotes_and_preambles() -> None:
    """Post-processing scrubs the tinyllama-style decorations we see."""
    samples = [
        ('  "Hello World"  ', "Hello World"),
        ('Title: My Chat Title', "My Chat Title"),
        ('Conversation: Wine Tasting Tonight', "Wine Tasting Tonight"),
        ("Here is the title: The Thing", "The Thing"),
        # Multi-line: take the first non-empty line.
        ("\n\nThe Picked Line\n\nIgnored", "The Picked Line"),
        # Curly quotes get stripped too.
        ('“Smart Quotes Are Fun”', "Smart Quotes Are Fun"),
    ]

    for raw, expected in samples:
        def make_handler(payload: str):
            def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200, json={"message": {"content": payload}}
                )
            return handler

        async with _client_with(make_handler(raw)) as client:
            assert await generate_title(client, []) == expected, raw


@pytest.mark.asyncio
async def test_generate_title_caps_at_80_chars() -> None:
    """A runaway title is truncated so the sidebar row stays sane."""
    long = "A " * 200  # 400 chars

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": long}})

    async with _client_with(handler) as client:
        title = await generate_title(client, [])

    assert len(title) == 80
