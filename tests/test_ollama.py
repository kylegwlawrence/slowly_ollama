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

from app import ollama as _ollama_module
from app.ollama import (
    ChatChunk,
    OllamaProtocolError,
    OllamaUnavailable,
    create_client,
    generate_title,
    is_model_loaded,
    list_loaded_models,
    list_models,
    list_tool_capable_models,
    maybe_tool_call,
    model_supports_tools,
    stream_chat,
    summarize_conversation,
    unload_model,
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
async def test_list_models_targets_host_override() -> None:
    """A host override builds an absolute /api/tags URL against that host.

    This is what lets the composer list a second host's ("host2") models
    while the client's base_url still points at the primary host.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"models": [{"name": "mac:1"}]})

    async with _client_with(handler) as client:
        models = await list_models(client, host="http://host1:11434")

    assert models == ["mac:1"]
    # base_url was http://test, but the override sent the request to host1.
    assert seen == ["http://host1:11434/api/tags"]


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
async def test_stream_chat_passes_num_ctx_in_options() -> None:
    """When num_ctx is provided it lands in the Ollama options dict."""
    body = b'{"message":{"content":""},"done":true}\n'
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, content=body)

    async with _client_with(handler) as client:
        async for _ in stream_chat(
            client,
            model="llama3",
            messages=[{"role": "user", "content": "Hi"}],
            num_ctx=32768,
        ):
            pass

    assert seen["payload"]["options"]["num_ctx"] == 32768
    # Temperature still rides alongside, default 0.8.
    assert seen["payload"]["options"]["temperature"] == 0.8


@pytest.mark.asyncio
async def test_stream_chat_omits_num_ctx_when_none() -> None:
    """When num_ctx is None the options dict has no num_ctx key (Ollama default)."""
    body = b'{"message":{"content":""},"done":true}\n'
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, content=body)

    async with _client_with(handler) as client:
        async for _ in stream_chat(
            client,
            model="llama3",
            messages=[{"role": "user", "content": "Hi"}],
        ):
            pass

    assert "num_ctx" not in seen["payload"]["options"]


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
async def test_generate_title_uses_passed_model_and_appends_title_request() -> None:
    """generate_title forwards the model arg and appends a user turn
    asking for a title.

    The handler captures the outgoing payload so we can assert the
    history was forwarded, the model is the one the caller passed,
    and a third (title-request) turn was tacked onto the end.
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
            "llama3:8b",
            [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello!"},
            ],
        )

    assert title == "A Concise Title"
    assert captured["path"] == "/api/chat"
    # The model arg is forwarded — no hardcoded default in the client.
    assert captured["body"]["model"] == "llama3:8b"
    # Non-streaming — title responses are tiny, no need for SSE.
    assert captured["body"]["stream"] is False
    # History forwarded; a third (title-request) turn appended.
    msgs = captured["body"]["messages"]
    assert len(msgs) == 3
    assert msgs[0] == {"role": "user", "content": "Hi"}
    assert msgs[1] == {"role": "assistant", "content": "Hello!"}
    assert msgs[2]["role"] == "user"
    assert "title" in msgs[2]["content"].lower()


@pytest.mark.asyncio
async def test_generate_title_raises_unavailable_on_5xx() -> None:
    """5xx → OllamaUnavailable (no special-case for 404 anymore — we
    reuse the chat's own model, which is guaranteed installed)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _client_with(handler) as client:
        with pytest.raises(OllamaUnavailable):
            await generate_title(client, "llama3", [])


@pytest.mark.asyncio
async def test_generate_title_strips_quotes_and_preambles() -> None:
    """Post-processing scrubs the small-model decorations we sometimes see."""
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
            assert await generate_title(client, "llama3", []) == expected, raw


@pytest.mark.asyncio
async def test_generate_title_caps_at_four_words() -> None:
    """Titles get capped at 4 words — smaller models routinely
    overshoot the prompt's word-count instruction."""
    overshoot = "one two three four five six seven eight nine ten"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"message": {"content": overshoot}}
        )

    async with _client_with(handler) as client:
        title = await generate_title(client, "llama3", [])

    assert title == "one two three four"
    assert len(title.split()) == 4


@pytest.mark.asyncio
async def test_generate_title_short_titles_pass_through() -> None:
    """Titles already at or under the 4-word cap are returned unchanged."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"message": {"content": "Three Word Title"}}
        )

    async with _client_with(handler) as client:
        assert await generate_title(client, "llama3", []) == "Three Word Title"


@pytest.mark.asyncio
async def test_generate_title_char_cap_is_final_safety_net() -> None:
    """When the 4 words are themselves absurdly long, the 30-char cap
    truncates the result so the sidebar row can't explode."""
    huge_word = "X" * 100
    payload = " ".join([huge_word] * 4)  # 403 chars, 4 words

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": payload}})

    async with _client_with(handler) as client:
        title = await generate_title(client, "llama3", [])

    assert len(title) == 30


# ---------------------------------------------------------------------------
# maybe_tool_call (Phase 12d)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_tool_call_unwraps_ollama_tool_call_shape() -> None:
    """Ollama wraps each tool call as `{"function": {"name", "arguments"}}`;
    maybe_tool_call flattens that to `{"name", "arguments"}` so the
    rest of the codebase doesn't need to know the wire shape."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Sanity: it's a non-streaming POST with a tools key.
        body = json.loads(request.content)
        assert request.url.path == "/api/chat"
        assert body.get("stream") is False
        assert "tools" in body
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "current_time",
                                "arguments": {"timezone": "UTC"},
                            }
                        }
                    ],
                }
            },
        )

    async with _client_with(handler) as client:
        tool_calls, content = await maybe_tool_call(
            client, "llama3", messages=[], tools=[{"any": "spec"}]
        )

    assert content == ""
    assert tool_calls == [
        {"name": "current_time", "arguments": {"timezone": "UTC"}}
    ]


@pytest.mark.asyncio
async def test_maybe_tool_call_returns_empty_list_when_no_tools() -> None:
    """When the model returns plain text (no tool_calls key, or empty),
    the result is `([], content)` — caller takes that as "switch to
    streaming for the visible response"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"message": {"content": "just text"}},
        )

    async with _client_with(handler) as client:
        tool_calls, content = await maybe_tool_call(
            client, "llama3", messages=[], tools=None
        )

    assert tool_calls == []
    assert content == "just text"


@pytest.mark.asyncio
async def test_maybe_tool_call_omits_tools_key_when_none() -> None:
    """Passing `tools=None` keeps the key out of the outgoing payload —
    some models 400 on `tools=[]` so the loop opts out cleanly via
    None when there's nothing to advertise."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"message": {"content": "ok"}},
        )

    async with _client_with(handler) as client:
        await maybe_tool_call(
            client, "llama3", messages=[], tools=None
        )

    assert "tools" not in captured["body"]


@pytest.mark.asyncio
async def test_maybe_tool_call_raises_unavailable_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    async with _client_with(handler) as client:
        with pytest.raises(OllamaUnavailable):
            await maybe_tool_call(
                client, "llama3", messages=[], tools=None
            )


@pytest.mark.asyncio
async def test_maybe_tool_call_raises_protocol_error_on_bad_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    async with _client_with(handler) as client:
        with pytest.raises(OllamaProtocolError):
            await maybe_tool_call(
                client, "llama3", messages=[], tools=None
            )


# ---------------------------------------------------------------------------
# list_tool_capable_models / model_supports_tools (phase 12f)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tool_capable_models_filters_to_tool_capable() -> None:
    """Only models whose /api/show capabilities include 'tools' are returned.

    Also pins ordering: the filtered list matches the /api/tags order
    (asyncio.gather preserves input order in its result, so the filter
    inherits the same stability).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [
                    {"name": "llama3.1:8b"},
                    {"name": "nomic-embed-text:latest"},
                    {"name": "qwen2.5:7b"},
                ]},
            )
        if request.url.path == "/api/show":
            body = json.loads(request.content)
            tool_capable = body["model"] in {"llama3.1:8b", "qwen2.5:7b"}
            caps = ["completion", "tools"] if tool_capable else ["embedding"]
            return httpx.Response(200, json={"capabilities": caps})
        return httpx.Response(404)

    async with _client_with(handler) as client:
        names = await list_tool_capable_models(client)

    assert names == ["llama3.1:8b", "qwen2.5:7b"]


@pytest.mark.asyncio
async def test_list_tool_capable_models_rejects_tools_without_completion() -> None:
    """A model reporting ``["embedding", "tools"]`` is still filtered out.

    Ollama sometimes inherits the ``tools`` capability flag on rerankers
    and embedder-derived models that share a chat base — Ollama itself
    runs the user's
    ``pdurugyan/qwen3-reranker-0.6b-q8_0`` with ``capabilities = ["embedding", "tools"]``,
    but it's not a usable chat model: /api/chat against it returns
    garbage or 400s. Requiring BOTH "completion" and "tools" is the
    cheapest reliable signal that the model is a chat model that can
    actually run a tool round.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [
                    {"name": "real-chat"},
                    {"name": "reranker"},
                ]},
            )
        if request.url.path == "/api/show":
            body = json.loads(request.content)
            caps = (
                ["completion", "tools"]
                if body["model"] == "real-chat"
                else ["embedding", "tools"]
            )
            return httpx.Response(200, json={"capabilities": caps})
        return httpx.Response(404)

    async with _client_with(handler) as client:
        names = await list_tool_capable_models(client)

    assert names == ["real-chat"]


@pytest.mark.asyncio
async def test_list_tool_capable_models_drops_models_where_show_errors() -> None:
    """A 5xx (or any HTTPError) on one model's /api/show isn't fatal.

    The misbehaving model is silently dropped; the rest of the dropdown
    stays usable. A single Ollama hiccup on a single model shouldn't
    leave the user staring at an empty dropdown.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [
                    {"name": "good-model"},
                    {"name": "broken-model"},
                ]},
            )
        if request.url.path == "/api/show":
            body = json.loads(request.content)
            if body["model"] == "broken-model":
                return httpx.Response(500)
            return httpx.Response(
                200, json={"capabilities": ["completion", "tools"]}
            )
        return httpx.Response(404)

    async with _client_with(handler) as client:
        names = await list_tool_capable_models(client)

    assert names == ["good-model"]


@pytest.mark.asyncio
async def test_list_tool_capable_models_caches_results_within_ttl() -> None:
    """Two calls in quick succession only hit /api/show once per model.

    The cache is the whole point — a fresh dropdown render should never
    re-probe Ollama for capabilities it just learned a moment ago.
    """
    show_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal show_calls
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "llama3"}, {"name": "qwen"}]}
            )
        if request.url.path == "/api/show":
            show_calls += 1
            return httpx.Response(
                200, json={"capabilities": ["completion", "tools"]}
            )
        return httpx.Response(404)

    async with _client_with(handler) as client:
        names1 = await list_tool_capable_models(client)
        names2 = await list_tool_capable_models(client)

    assert names1 == ["llama3", "qwen"]
    assert names2 == ["llama3", "qwen"]
    # Two models × one fetch each — the second list_tool_capable_models
    # call returned from the cache without hitting /api/show again.
    assert show_calls == 2


@pytest.mark.asyncio
async def test_list_tool_capable_models_refreshes_after_cache_expiry() -> None:
    """Once the TTL has elapsed the next call re-probes /api/show."""
    show_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal show_calls
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "llama3"}]}
            )
        if request.url.path == "/api/show":
            show_calls += 1
            return httpx.Response(
                200, json={"capabilities": ["completion", "tools"]}
            )
        return httpx.Response(404)

    async with _client_with(handler) as client:
        await list_tool_capable_models(client)
        assert show_calls == 1
        # Force the cache to look expired without messing with the
        # global clock — `expires_at` is the only TTL signal we care
        # about, so pushing it into the past is a faithful simulation.
        assert _ollama_module._capability_cache is not None
        _ollama_module._capability_cache["expires_at"] = -1
        await list_tool_capable_models(client)

    assert show_calls == 2


@pytest.mark.asyncio
async def test_model_supports_tools_reflects_capability_membership() -> None:
    """True when /api/show lists 'tools' for the model, False otherwise."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [
                    {"name": "chatty"}, {"name": "embed-only"}
                ]},
            )
        if request.url.path == "/api/show":
            body = json.loads(request.content)
            caps = (
                ["completion", "tools"]
                if body["model"] == "chatty"
                else ["embedding"]
            )
            return httpx.Response(200, json={"capabilities": caps})
        return httpx.Response(404)

    async with _client_with(handler) as client:
        assert await model_supports_tools(client, "chatty") is True
        assert await model_supports_tools(client, "embed-only") is False
        # A name Ollama doesn't even know about behaves like "not capable"
        # — same outcome as embed-only above.
        assert await model_supports_tools(client, "no-such-model") is False


@pytest.mark.asyncio
async def test_model_supports_tools_returns_false_when_ollama_unavailable() -> None:
    """A /api/tags failure resolves to False, not an exception.

    The generation path uses this helper synchronously inside the
    streaming generator; raising would break the SSE flow. False
    collapses to ``tools_payload = None`` and the chat falls back to
    plain streaming — far safer than 400ing Ollama.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    async with _client_with(handler) as client:
        assert await model_supports_tools(client, "llama3") is False


# ---------------------------------------------------------------------------
# Phase 18: summarize_conversation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_appends_instruction_and_returns_text() -> None:
    """The helper appends a user-turn instruction and returns stripped text."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message": {"content": "  the briefing  "}}
        )

    async with _client_with(handler) as client:
        text = await summarize_conversation(
            client,
            "llama3:8b",
            [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello!"},
            ],
        )

    assert text == "the briefing"
    assert captured["path"] == "/api/chat"
    body = captured["body"]
    assert body["model"] == "llama3:8b"
    assert body["stream"] is False
    # Low-creativity temperature is hardcoded — not the chat's own setting.
    assert body["options"]["temperature"] == 0.2
    # History is forwarded; one extra user turn is appended.
    msgs = body["messages"]
    assert len(msgs) == 3
    assert msgs[0] == {"role": "user", "content": "Hi"}
    assert msgs[1] == {"role": "assistant", "content": "Hello!"}
    assert msgs[2]["role"] == "user"
    assert "summarize" in msgs[2]["content"].lower()


@pytest.mark.asyncio
async def test_summarize_passes_num_ctx_when_provided() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message": {"content": "x"}}
        )

    async with _client_with(handler) as client:
        await summarize_conversation(
            client, "llama3", [], num_ctx=16384
        )
    assert captured["body"]["options"]["num_ctx"] == 16384


@pytest.mark.asyncio
async def test_summarize_omits_num_ctx_when_none() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message": {"content": "x"}}
        )

    async with _client_with(handler) as client:
        await summarize_conversation(client, "llama3", [])
    assert "num_ctx" not in captured["body"]["options"]


@pytest.mark.asyncio
async def test_summarize_raises_unavailable_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with _client_with(handler) as client:
        with pytest.raises(OllamaUnavailable):
            await summarize_conversation(client, "llama3", [])


@pytest.mark.asyncio
async def test_summarize_raises_unavailable_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _client_with(handler) as client:
        with pytest.raises(OllamaUnavailable):
            await summarize_conversation(client, "llama3", [])


@pytest.mark.asyncio
async def test_summarize_raises_protocol_error_on_bad_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Valid JSON, wrong shape (no `message.content`).
        return httpx.Response(200, json={"unexpected": "shape"})

    async with _client_with(handler) as client:
        with pytest.raises(OllamaProtocolError):
            await summarize_conversation(client, "llama3", [])


@pytest.mark.asyncio
async def test_summarize_raises_protocol_error_on_non_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    async with _client_with(handler) as client:
        with pytest.raises(OllamaProtocolError):
            await summarize_conversation(client, "llama3", [])


@pytest.mark.asyncio
async def test_summarize_returns_empty_string_on_empty_content() -> None:
    """A blank model reply round-trips as the empty string — the caller
    decides what to do (the route 502s)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "   "}})

    async with _client_with(handler) as client:
        text = await summarize_conversation(client, "llama3", [])
    assert text == ""


# ---------------------------------------------------------------------------
# list_loaded_models / is_model_loaded / unload_model (header chip)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_loaded_models_returns_names_from_ps() -> None:
    """list_loaded_models extracts the ``name`` field from /api/ps."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ps"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "llama3:latest", "size_vram": 4_000_000_000},
                    {"name": "qwen2.5:7b", "size_vram": 3_500_000_000},
                ]
            },
        )

    async with _client_with(handler) as client:
        names = await list_loaded_models(client)

    assert names == ["llama3:latest", "qwen2.5:7b"]


@pytest.mark.asyncio
async def test_list_loaded_models_returns_empty_when_nothing_resident() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": []})

    async with _client_with(handler) as client:
        assert await list_loaded_models(client) == []


@pytest.mark.asyncio
async def test_list_loaded_models_unavailable_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _client_with(handler) as client:
        with pytest.raises(OllamaUnavailable):
            await list_loaded_models(client)


@pytest.mark.asyncio
async def test_list_loaded_models_protocol_error_on_wrong_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"wrong_key": []})

    async with _client_with(handler) as client:
        with pytest.raises(OllamaProtocolError):
            await list_loaded_models(client)


@pytest.mark.asyncio
async def test_is_model_loaded_true_when_listed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "llama3"}]})

    async with _client_with(handler) as client:
        assert await is_model_loaded(client, "llama3") is True


@pytest.mark.asyncio
async def test_is_model_loaded_false_when_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "qwen2.5"}]})

    async with _client_with(handler) as client:
        assert await is_model_loaded(client, "llama3") is False


@pytest.mark.asyncio
async def test_is_model_loaded_defaults_true_on_ollama_failure() -> None:
    """A /api/ps failure must NOT flip the chip to "unloaded" — we'd
    rather show the chip in its normal colour than lie about residency."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with _client_with(handler) as client:
        assert await is_model_loaded(client, "llama3") is True


@pytest.mark.asyncio
async def test_is_model_loaded_defaults_true_on_protocol_error() -> None:
    """A garbled /api/ps body is also swallowed — same rationale as above."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    async with _client_with(handler) as client:
        assert await is_model_loaded(client, "llama3") is True


@pytest.mark.asyncio
async def test_unload_model_posts_keep_alive_zero() -> None:
    """unload_model POSTs /api/generate with keep_alive=0 (Ollama's
    documented unload protocol)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/generate"
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "llama3", "done": True})

    async with _client_with(handler) as client:
        await unload_model(client, "llama3")

    assert seen["body"] == {"model": "llama3", "keep_alive": 0}


@pytest.mark.asyncio
async def test_unload_model_raises_unavailable_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _client_with(handler) as client:
        with pytest.raises(OllamaUnavailable):
            await unload_model(client, "llama3")


@pytest.mark.asyncio
async def test_unload_model_raises_unavailable_on_connect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    async with _client_with(handler) as client:
        with pytest.raises(OllamaUnavailable):
            await unload_model(client, "llama3")


# ---------------------------------------------------------------------------
# host= override (remote-agent routing)
# ---------------------------------------------------------------------------
# When a function is called with host="http://host1:11434", the outgoing
# request must hit that host instead of the client's base_url. The shared
# client still has base_url=http://test from _client_with(); the host=
# kwarg builds an absolute URL that overrides it on a per-call basis.


@pytest.mark.asyncio
async def test_stream_chat_host_override_targets_remote_url() -> None:
    """`host=` makes the streaming POST land on the remote URL."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            content=(
                b'{"message":{"content":"hi"},"done":false}\n'
                b'{"message":{"content":""},"done":true}\n'
            ),
        )

    async with _client_with(handler) as client:
        async for _ in stream_chat(
            client, "m", [{"role": "user", "content": "hi"}],
            host="http://host1:11434",
        ):
            pass

    assert seen == ["http://host1:11434/api/chat"]


@pytest.mark.asyncio
async def test_stream_chat_without_host_uses_client_base_url() -> None:
    """Default behavior (host=None) still routes through the client's base_url."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            content=(
                b'{"message":{"content":""},"done":true}\n'
            ),
        )

    async with _client_with(handler) as client:
        async for _ in stream_chat(
            client, "m", [{"role": "user", "content": "hi"}],
        ):
            pass

    assert seen == ["http://test/api/chat"]


@pytest.mark.asyncio
async def test_maybe_tool_call_host_override_targets_remote_url() -> None:
    """`host=` makes the non-streaming probe land on the remote URL."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200, json={"message": {"content": "", "tool_calls": []}}
        )

    async with _client_with(handler) as client:
        await maybe_tool_call(
            client, "m", [{"role": "user", "content": "hi"}],
            tools=None, host="http://host1:11434",
        )

    assert seen == ["http://host1:11434/api/chat"]


@pytest.mark.asyncio
async def test_summarize_conversation_host_override_targets_remote_url() -> None:
    """`host=` makes compaction's POST land on the remote URL."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200, json={"message": {"content": "summary"}}
        )

    async with _client_with(handler) as client:
        out = await summarize_conversation(
            client, "m", [{"role": "user", "content": "x"}],
            host="http://host1:11434",
        )

    assert seen == ["http://host1:11434/api/chat"]
    assert out == "summary"


@pytest.mark.asyncio
async def test_model_supports_tools_host_does_one_show_probe() -> None:
    """`host=` triggers a single /api/show against the remote (no cache)."""
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append((str(request.url), body))
        return httpx.Response(
            200,
            json={"capabilities": ["completion", "tools"]},
        )

    async with _client_with(handler) as client:
        ok = await model_supports_tools(
            client, "llama3.1:70b", host="http://host1:11434"
        )

    assert ok is True
    assert seen == [
        ("http://host1:11434/api/show", {"model": "llama3.1:70b"})
    ]


@pytest.mark.asyncio
async def test_model_supports_tools_host_returns_false_on_failure() -> None:
    """A failing /api/show probe against the remote degrades to False."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    async with _client_with(handler) as client:
        ok = await model_supports_tools(
            client, "m", host="http://host1:11434"
        )
    assert ok is False


@pytest.mark.asyncio
async def test_model_supports_tools_host_false_when_no_completion_cap() -> None:
    """Even when /api/show advertises 'tools', missing 'completion' → False
    (mirrors the local cache filter — embed-only models 400 on /api/chat)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"capabilities": ["embedding", "tools"]}
        )

    async with _client_with(handler) as client:
        ok = await model_supports_tools(
            client, "m", host="http://host1:11434"
        )
    assert ok is False


