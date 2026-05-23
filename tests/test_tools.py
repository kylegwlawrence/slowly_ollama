"""Phase 12b/12c: tool decorator + registry + RAG tool tests."""

from pathlib import Path

import httpx
import pytest

from app.tools import (
    TOOLS,
    Source,
    ToolResult,
    decode_tool_call,
    decode_tool_result,
    encode_tool_call,
    encode_tool_result,
    run_tool,
    tool,
    tool_specs_for_ollama,
)


def test_decorator_registers_with_inferred_schema() -> None:
    """@tool extracts name, description, and arg schema from the function."""
    @tool
    def add(x: int, y: int = 1) -> int:
        """Add two integers.

        Args:
            x: The first integer.
            y: The second integer (defaults to 1).
        """
        return x + y

    spec = TOOLS["add"]
    assert spec.name == "add"
    assert spec.description == "Add two integers."
    props = spec.parameters_schema["properties"]
    assert props["x"]["type"] == "integer"
    assert props["x"]["description"] == "The first integer."
    assert props["y"]["type"] == "integer"
    # Only x is required; y has a default.
    assert spec.parameters_schema["required"] == ["x"]
    # Read-only by default.
    assert spec.is_read_only is True


def test_decorator_handles_no_docstring() -> None:
    """A tool with no docstring still registers (description = name)."""
    @tool
    def noop() -> str:
        return ""

    assert TOOLS["noop"].description == "noop"
    assert TOOLS["noop"].parameters_schema["required"] == []


def test_tool_specs_for_ollama_shape() -> None:
    """The shape matches Ollama's /api/chat tools parameter format."""
    @tool
    def greet(name: str) -> str:
        """Say hi."""
        return f"hi {name}"

    specs = tool_specs_for_ollama()
    # Other tests register their own tools; pick out greet by name rather
    # than asserting on the full list (the registry is module-level).
    greet_spec = next(s for s in specs if s["function"]["name"] == "greet")
    assert greet_spec == {
        "type": "function",
        "function": {
            "name": "greet",
            "description": "Say hi.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    }


@pytest.mark.asyncio
async def test_run_tool_dispatches_sync_function() -> None:
    """A sync tool returning a plain value is wrapped in ToolResult."""
    @tool
    def double(n: int) -> int:
        """Double n."""
        return n * 2

    result = await run_tool("double", {"n": 21})
    assert isinstance(result, ToolResult)
    assert result.text == "42"
    assert result.sources == []


@pytest.mark.asyncio
async def test_run_tool_passes_through_tool_result_returns() -> None:
    """A tool that already returns a ToolResult comes through verbatim,
    sources intact."""
    @tool
    def with_sources() -> ToolResult:
        """Returns a structured result."""
        return ToolResult(
            text="formatted",
            sources=[Source(title="T", section="S")],
        )

    result = await run_tool("with_sources", {})
    assert result.text == "formatted"
    assert result.sources == [Source(title="T", section="S")]


@pytest.mark.asyncio
async def test_run_tool_unknown_returns_error_string() -> None:
    result = await run_tool("nonexistent", {})
    assert isinstance(result, ToolResult)
    assert "not registered" in result.text
    assert result.sources == []


@pytest.mark.asyncio
async def test_run_tool_arg_mismatch_returns_error_string() -> None:
    @tool
    def need_x(x: int) -> int:
        """Need x."""
        return x

    result = await run_tool("need_x", {"wrong_name": 1})
    # Returns explanatory ToolResult, doesn't raise.
    assert "rejected arguments" in result.text
    assert result.sources == []


@pytest.mark.asyncio
async def test_run_tool_internal_exception_returns_tool_result() -> None:
    """A tool that raises mid-run produces a ToolResult with an
    error explanation rather than letting the exception escape into
    the SSE stream."""
    @tool
    def boom() -> str:
        """Always raises."""
        raise RuntimeError("kaboom")

    result = await run_tool("boom", {})
    assert isinstance(result, ToolResult)
    assert "failed" in result.text
    assert "kaboom" in result.text
    assert result.sources == []


@pytest.mark.asyncio
async def test_current_time_baseline() -> None:
    """The baseline tool runs and returns an ISO 8601 string."""
    from app.tools import builtins  # noqa: F401 — registers current_time
    result = await run_tool("current_time", {"timezone": "UTC"})
    # ISO format starts with YYYY-
    assert result.text[:4].isdigit()
    assert result.text[4] == "-"
    # current_time has no sources.
    assert result.sources == []


# ---------------------------------------------------------------------------
# ToolResult round-trip encoding (phase 12h)
# ---------------------------------------------------------------------------


def test_encode_decode_tool_result_round_trip_empty_sources() -> None:
    """A ToolResult with no sources round-trips through the JSON envelope."""
    original = ToolResult(text="hello", sources=[])
    decoded = decode_tool_result(encode_tool_result(original))
    assert decoded.text == "hello"
    assert decoded.sources == []


def test_encode_decode_tool_result_round_trip_populated_sources() -> None:
    """Title + section round-trip through encode/decode unchanged."""
    original = ToolResult(
        text="[1] Foo (§Intro)",
        sources=[
            Source(title="Foo", section="Intro"),
            Source(title="Bar", section=None),
        ],
    )
    decoded = decode_tool_result(encode_tool_result(original))
    assert decoded == original


def test_decode_tool_result_plain_text_backwards_compat() -> None:
    """Pre-12h DB rows store plain text — decoding falls back to
    ToolResult(text=content, sources=[]) so old conversations render."""
    decoded = decode_tool_result("just some text the model wrote")
    assert decoded.text == "just some text the model wrote"
    assert decoded.sources == []


def test_decode_tool_result_malformed_json_falls_back() -> None:
    """Non-JSON content (e.g. partial brace) decodes as plain text."""
    decoded = decode_tool_result("{not valid json")
    assert decoded.text == "{not valid json"
    assert decoded.sources == []


def test_decode_tool_result_json_without_envelope_keys_falls_back() -> None:
    """Valid JSON that isn't our envelope still decodes to plain text —
    handles pathological legacy rows like '[1, 2, 3]'."""
    decoded = decode_tool_result('{"unrelated": "object"}')
    # Body is preserved verbatim as text; sources defaults to empty.
    assert decoded.text == '{"unrelated": "object"}'
    assert decoded.sources == []


def test_decode_tool_result_partial_source_entries_skipped() -> None:
    """Defensive: malformed source entries (e.g. non-dict) are skipped
    rather than crashing the decode. Keeps render robust against
    historical data drift."""
    raw = '{"text": "x", "sources": [{"title": "ok", "section": null}, "garbage", null]}'
    decoded = decode_tool_result(raw)
    assert decoded.text == "x"
    # Only the dict entry survives.
    assert decoded.sources == [Source(title="ok", section=None)]


def test_decode_tool_result_missing_title_falls_back_to_untitled() -> None:
    """If a source dict has no title key, decode substitutes the
    same "(untitled)" placeholder the tool would have used."""
    raw = '{"text": "x", "sources": [{"section": "S"}]}'
    decoded = decode_tool_result(raw)
    assert decoded.sources == [Source(title="(untitled)", section="S")]


# ---------------------------------------------------------------------------
# encode_tool_call / decode_tool_call round-trip
# ---------------------------------------------------------------------------


def test_encode_decode_tool_call_round_trip() -> None:
    """Well-formed encode → decode returns the same name + arguments."""
    encoded = encode_tool_call("query_rag", {"source": "arxiv", "query": "x"})
    decoded = decode_tool_call(encoded)
    assert decoded == ("query_rag", {"source": "arxiv", "query": "x"})


def test_encode_decode_tool_call_round_trip_empty_arguments() -> None:
    """Tools with no arguments round-trip with an empty dict."""
    encoded = encode_tool_call("current_time", {})
    decoded = decode_tool_call(encoded)
    assert decoded == ("current_time", {})


def test_decode_tool_call_returns_none_on_malformed_json() -> None:
    """Non-JSON content signals corruption via None, not via a fallback
    string. Callers (generation.py) need the explicit None to know to
    drop the row plus its paired tool_result."""
    assert decode_tool_call("{not valid json") is None
    assert decode_tool_call("") is None


def test_decode_tool_call_returns_none_when_name_missing() -> None:
    """Valid JSON without a `name` key is treated as corrupt — same
    posture as a truly malformed row."""
    assert decode_tool_call('{"arguments": {"x": 1}}') is None


def test_decode_tool_call_returns_none_when_not_a_dict() -> None:
    """Valid JSON that isn't a dict (e.g., a stray list) signals
    corruption rather than crashing the caller."""
    assert decode_tool_call("[1, 2, 3]") is None
    assert decode_tool_call('"just a string"') is None


def test_decode_tool_call_coerces_non_dict_arguments_to_empty() -> None:
    """A model that emits arguments as something other than a dict
    (e.g., a stray string) is treated as "no arguments" rather than
    propagating a malformed value into the wire payload."""
    assert decode_tool_call('{"name": "t", "arguments": "weird"}') == ("t", {})
    assert decode_tool_call('{"name": "t", "arguments": null}') == ("t", {})


def test_decode_tool_call_missing_arguments_key_defaults_to_empty() -> None:
    """Some models omit the arguments key entirely when the tool takes
    no args; treat as an empty dict, same as if they'd sent `{}`."""
    assert decode_tool_call('{"name": "ping"}') == ("ping", {})


# ---------------------------------------------------------------------------
# Phase 12c: query_rag tool
# ---------------------------------------------------------------------------


@pytest.fixture
def rag_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Initialize a fresh SQLite DB at tmp_path and point DB_PATH at it.

    The RAG tool opens its own short-lived connection via
    ``app.connection.open_connection()`` — it doesn't take the app's
    shared connection — so it'll resolve DB_PATH from the env. Setting
    the env here lets the tool actually open the test DB instead of
    hitting whatever path the developer's local .env points at.
    """
    from app.db import initialize_database

    db_path = tmp_path / "chats.db"
    initialize_database(db_path)
    monkeypatch.setenv("DB_PATH", str(db_path))
    return db_path


@pytest.mark.asyncio
async def test_query_rag_unknown_source_returns_message(
    rag_db: Path,
) -> None:
    """When the model picks a source that's not configured, the tool
    returns an explanatory message — doesn't raise.

    The empty source list comes from rag_db being freshly-initialized
    (no rag_servers rows). This also implicitly verifies the tool's
    @tool registration: ``run_tool("query_rag", ...)`` would fail with
    "not registered" if app.tools.rag's decorator hadn't run.
    """
    from app.tools import rag as _rag  # noqa: F401 — registers query_rag

    result = await run_tool(
        "query_rag", {"source": "missing", "query": "hi"}
    )
    assert "Unknown RAG source 'missing'" in result.text
    assert result.sources == []


@pytest.mark.asyncio
async def test_query_rag_empty_query_returns_message(
    rag_db: Path,
) -> None:
    """An empty / whitespace-only query is rejected without an HTTP call."""
    from app.tools import rag as _rag  # noqa: F401 — registers query_rag

    result = await run_tool(
        "query_rag", {"source": "arxiv", "query": "   "}
    )
    assert "cannot be empty" in result.text
    assert result.sources == []


def test_format_chunks_renders_citation_block() -> None:
    """Successful retrieval renders as a numbered, section-aware block.

    Multiple chunks are numbered [1], [2], ...; a None section is
    silently omitted from the header (rather than rendered as
    "(§None)" which would be ugly).
    """
    from app.tools.rag import _format_chunks

    items = [
        {"title": "Foo", "section": "Intro", "text": "hello world"},
        {"title": "Bar", "section": None, "text": "another chunk"},
    ]
    out = _format_chunks(items, used_dense=True)
    assert "[1] Foo (§Intro)" in out
    assert "hello world" in out
    assert "[2] Bar" in out
    # No "§None" garbage when the section is missing.
    assert "§None" not in out
    assert "another chunk" in out
    # No sparse-only note when used_dense=True.
    assert "sparse-only" not in out


def test_format_chunks_sparse_only_note() -> None:
    """When used_dense=False, the output is prefixed with a warning."""
    from app.tools.rag import _format_chunks

    out = _format_chunks(
        [{"title": "T", "section": None, "text": "x"}],
        used_dense=False,
    )
    assert "sparse-only retrieval" in out


def test_format_chunks_truncates_long_text() -> None:
    """Per-chunk text over _PER_CHUNK_TEXT_CAP gets ellipsis-truncated."""
    from app.tools.rag import _PER_CHUNK_TEXT_CAP, _format_chunks

    long_text = "x" * (_PER_CHUNK_TEXT_CAP + 500)
    out = _format_chunks(
        [{"title": "T", "section": None, "text": long_text}],
        used_dense=True,
    )
    assert "..." in out
    # The visible chunk text portion is the per-chunk cap exactly
    # (count is the run of x's that's at most _PER_CHUNK_TEXT_CAP - 3).
    x_run = out.count("x")
    assert x_run <= _PER_CHUNK_TEXT_CAP


def test_format_chunks_empty_items_returns_no_match_message() -> None:
    """Zero items with full-recall retrieval shows a clean fallback string."""
    from app.tools.rag import _format_chunks

    out = _format_chunks([], used_dense=True)
    assert out == "(no matching chunks)"


def test_format_chunks_missing_title_falls_back() -> None:
    """A chunk with no title still renders without crashing."""
    from app.tools.rag import _format_chunks

    out = _format_chunks(
        [{"section": None, "text": "body"}], used_dense=True
    )
    assert "(untitled)" in out
    assert "body" in out


@pytest.mark.asyncio
async def test_query_rag_returns_formatted_chunks_on_success(
    rag_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: configured server + mocked HTTP response → formatted output.

    Patches ``httpx.AsyncClient`` inside app.tools.rag so we don't
    actually hit the network. Asserts the tool walks the DB, fires
    a GET, and formats the response via _format_chunks.
    """
    from app import rag_servers as _rs
    from app.connection import open_connection
    from app.tools import rag as _rag  # noqa: F401 — registers query_rag
    from app.tools.rag import query_rag

    # Seed a server so the source lookup succeeds.
    with open_connection() as conn:
        _rs.create_server(conn, "arxiv", "http://fake/arxiv")

    # Capture the URL hit so we can verify the tool builds it right.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "items": [
                    {"title": "Doc", "section": "1", "text": "answer"},
                ],
                "used_dense": True,
            },
        )

    # Snapshot the real AsyncClient BEFORE monkeypatching so the
    # _FakeClient wrapper can still build a real one underneath. If we
    # called `httpx.AsyncClient` inside the wrapper, our own monkeypatch
    # would point that name at the wrapper itself → unbounded recursion.
    _real_async_client = httpx.AsyncClient

    class _FakeClient:
        """Stand-in for httpx.AsyncClient that routes through a MockTransport.

        The tool uses `async with httpx.AsyncClient(timeout=...) as client`,
        so we only need to honor the async-context-manager protocol; the
        timeout kwarg is consumed and discarded — the MockTransport doesn't
        time out.
        """

        def __init__(self, *args, **kwargs):
            self._client = _real_async_client(
                transport=httpx.MockTransport(handler)
            )

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, *exc):
            await self._client.aclose()

    monkeypatch.setattr(_rag.httpx, "AsyncClient", _FakeClient)

    result = await query_rag(source="arxiv", query="what is x")
    # Built the right URL (base + /chunks, with query params).
    assert captured["url"].startswith("http://fake/arxiv/chunks")
    assert "q=what+is+x" in captured["url"]
    assert "top_k=5" in captured["url"]
    # The body of the response made it through _format_chunks (model-facing).
    assert "[1] Doc (§1)" in result.text
    assert "answer" in result.text
    # Phase 12h: structured sources also surface alongside the text.
    assert result.sources == [Source(title="Doc", section="1")]


@pytest.mark.parametrize(
    "status_code,expected_substring",
    [
        (503, "unavailable"),
        (500, "failed (HTTP 500)"),
        (502, "failed (HTTP 502)"),
        (400, "rejected the query (HTTP 400)"),
        (404, "rejected the query (HTTP 404)"),
    ],
)
@pytest.mark.asyncio
async def test_query_rag_http_error_status_returns_tool_result(
    rag_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_substring: str,
) -> None:
    """Each HTTP error branch (503 / 5xx / 4xx) returns a ToolResult
    with an explanatory text and no sources — pins the refactor from
    plain-string returns to structured ToolResult."""
    from app import rag_servers as _rs
    from app.connection import open_connection
    from app.tools import rag as _rag  # noqa: F401 — registers query_rag
    from app.tools.rag import query_rag

    with open_connection() as conn:
        _rs.create_server(conn, "arxiv", "http://fake/arxiv")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

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

    monkeypatch.setattr(_rag.httpx, "AsyncClient", _FakeClient)

    result = await query_rag(source="arxiv", query="x")
    assert isinstance(result, ToolResult)
    assert expected_substring in result.text
    assert result.sources == []


@pytest.mark.asyncio
async def test_query_rag_non_json_response_returns_tool_result(
    rag_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 body that isn't JSON (e.g. an HTML error page mistakenly
    served as 200) decodes to a ToolResult — the raw body is NOT
    surfaced into the chat."""
    from app import rag_servers as _rs
    from app.connection import open_connection
    from app.tools import rag as _rag  # noqa: F401 — registers query_rag
    from app.tools.rag import query_rag

    with open_connection() as conn:
        _rs.create_server(conn, "arxiv", "http://fake/arxiv")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

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

    monkeypatch.setattr(_rag.httpx, "AsyncClient", _FakeClient)

    result = await query_rag(source="arxiv", query="x")
    assert isinstance(result, ToolResult)
    assert "non-JSON response" in result.text
    # The raw HTML body must not leak into the chat.
    assert "<html>" not in result.text
    assert result.sources == []


@pytest.mark.asyncio
async def test_query_rag_handles_unreachable_server(
    rag_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network-level failure produces a plain string, not a raise."""
    from app import rag_servers as _rs
    from app.connection import open_connection
    from app.tools import rag as _rag  # noqa: F401 — registers query_rag
    from app.tools.rag import query_rag

    with open_connection() as conn:
        _rs.create_server(conn, "arxiv", "http://fake/arxiv")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

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

    monkeypatch.setattr(_rag.httpx, "AsyncClient", _FakeClient)

    result = await query_rag(source="arxiv", query="x")
    assert "unreachable" in result.text
    assert result.sources == []


def test_refresh_query_rag_registration_includes_descriptions(
    rag_db: Path,
) -> None:
    """After adding servers, the source-arg hint contains name + description."""
    from app import rag_servers as _rs
    from app.connection import open_connection
    from app.tools import TOOLS
    from app.tools import rag as _rag  # noqa: F401 — registers query_rag
    from app.tools.rag import refresh_query_rag_registration

    with open_connection() as conn:
        _rs.create_server(conn, "arxiv", "http://x/arxiv", description="Papers on CS/ML")
        _rs.create_server(conn, "wikipedia", "http://x/wiki", description="Wikipedia summaries")
    refresh_query_rag_registration()
    desc = TOOLS["query_rag"].parameters_schema["properties"]["source"][
        "description"
    ]
    assert "arxiv" in desc
    assert "Papers on CS/ML" in desc
    assert "wikipedia" in desc
    assert "Wikipedia summaries" in desc


def test_refresh_query_rag_registration_uses_no_description_fallback(
    rag_db: Path,
) -> None:
    """A server with empty description renders as '(no description)' in the hint."""
    from app import rag_servers as _rs
    from app.connection import open_connection
    from app.tools import TOOLS
    from app.tools import rag as _rag  # noqa: F401 — registers query_rag
    from app.tools.rag import refresh_query_rag_registration

    with open_connection() as conn:
        _rs.create_server(conn, "arxiv", "http://x/arxiv")  # description=""
    refresh_query_rag_registration()
    desc = TOOLS["query_rag"].parameters_schema["properties"]["source"][
        "description"
    ]
    assert "arxiv" in desc
    assert "(no description)" in desc


def test_refresh_query_rag_registration_removes_tool_when_no_servers(
    rag_db: Path,
) -> None:
    """With 0 servers, query_rag is removed from TOOLS entirely."""
    from app.tools import TOOLS
    from app.tools import rag as _rag  # noqa: F401 — registers query_rag
    from app.tools.rag import refresh_query_rag_registration

    assert "query_rag" in TOOLS
    refresh_query_rag_registration()
    assert "query_rag" not in TOOLS


def test_refresh_query_rag_registration_readds_tool_when_server_added(
    rag_db: Path,
) -> None:
    """After a pop, adding a server and refreshing restores the tool."""
    from app import rag_servers as _rs
    from app.connection import open_connection
    from app.tools import TOOLS
    from app.tools import rag as _rag  # noqa: F401 — registers query_rag
    from app.tools.rag import refresh_query_rag_registration

    # Simulate 0-server state (tool was previously popped).
    TOOLS.pop("query_rag", None)
    assert "query_rag" not in TOOLS

    with open_connection() as conn:
        _rs.create_server(conn, "arxiv", "http://x/arxiv", description="CS papers")
    refresh_query_rag_registration()

    assert "query_rag" in TOOLS
    desc = TOOLS["query_rag"].parameters_schema["properties"]["source"][
        "description"
    ]
    assert "arxiv" in desc
    assert "CS papers" in desc


# ---------------------------------------------------------------------------
# format_tool_invocation (phase 12e — tool-card row labels)
# ---------------------------------------------------------------------------


def test_format_tool_invocation_query_rag_uses_search_shape() -> None:
    """query_rag is the dominant search-shaped tool, so it gets a
    purpose-built label that reads naturally next to a stopwatch."""
    from app.tools import format_tool_invocation

    label = format_tool_invocation(
        "query_rag",
        {"source": "arxiv", "query": "enhanced gas transfer"},
    )
    assert label == 'searching arxiv: "enhanced gas transfer"'


def test_format_tool_invocation_query_rag_missing_args_uses_placeholders() -> None:
    """Defensive: if the model emits a partial query_rag call, the row
    still renders rather than KeyError-ing the whole stream."""
    from app.tools import format_tool_invocation

    label = format_tool_invocation("query_rag", {})
    # Source falls back to "?"; empty query renders as empty quotes.
    assert label == 'searching ?: ""'


def test_format_tool_invocation_generic_fallback_shows_args() -> None:
    """Non-query_rag tools (current_time + any future ones) use the
    generic `calling name(args)` shape."""
    from app.tools import format_tool_invocation

    label = format_tool_invocation("current_time", {"timezone": "UTC"})
    assert label.startswith("calling current_time(")
    assert "timezone=" in label
    # repr-style quoting keeps strings visually delimited from keys.
    assert "'UTC'" in label


def test_format_tool_invocation_generic_no_args() -> None:
    """A no-arg tool produces `calling name()` — no trailing comma."""
    from app.tools import format_tool_invocation

    assert format_tool_invocation("ping", {}) == "calling ping()"


def test_format_tool_invocation_search_files_uses_pattern_and_path() -> None:
    """search_files gets a purpose-built label showing path and pattern."""
    from app.tools import format_tool_invocation

    label = format_tool_invocation("search_files", {"pattern": "*.md", "path": "docs"})
    assert 'docs' in label
    assert '"*.md"' in label


def test_format_tool_invocation_search_files_missing_args_uses_defaults() -> None:
    """search_files with no args falls back to sensible placeholders."""
    from app.tools import format_tool_invocation

    label = format_tool_invocation("search_files", {})
    assert '"*"' in label
    assert "." in label


# ---------------------------------------------------------------------------
# File tools: read_file / write_file (sandboxed to FILE_TOOL_ROOT)
# ---------------------------------------------------------------------------

from app.tools import builtins as _file_builtins  # noqa: E402


def test_read_file_reads_within_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read_file returns the contents of a file inside the workspace root."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "note.txt").write_text("hello world", encoding="utf-8")
    assert _file_builtins.read_file("note.txt") == "hello world"


def test_read_file_missing_returns_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-existent path returns an explanatory message, not an exception."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    assert "No file" in _file_builtins.read_file("nope.txt")


def test_read_file_rejects_parent_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `..` path that escapes the root is rejected without leaking content."""
    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    monkeypatch.setenv("FILE_TOOL_ROOT", str(root))
    out = _file_builtins.read_file("../secret.txt")
    assert "outside the allowed workspace" in out
    assert "top secret" not in out


def test_read_file_rejects_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absolute path escapes the root and is rejected."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    assert "outside the allowed workspace" in _file_builtins.read_file("/etc/hosts")


def test_read_file_truncates_at_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Output is hard-capped to _READ_FILE_CAP chars with an ellipsis."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    big = "x" * (_file_builtins._READ_FILE_CAP + 100)
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")
    out = _file_builtins.read_file("big.txt")
    assert len(out) == _file_builtins._READ_FILE_CAP
    assert out.endswith("...")


def test_read_file_no_root_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no root configured the tool reports the misconfiguration."""
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    assert "not configured" in _file_builtins.read_file("note.txt")


def test_read_file_non_utf8_returns_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-UTF-8 (e.g. binary) file surfaces a read error, not an exception."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00\x01")
    assert "Could not read" in _file_builtins.read_file("bin.dat")


def test_write_file_os_error_returns_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unwritable target (parent path is a file) surfaces a write error."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "blocker").write_text("i am a file", encoding="utf-8")
    # mkdir(parents=True) under a path whose parent is a regular file
    # raises NotADirectoryError (an OSError subclass).
    assert "Could not write" in _file_builtins.write_file("blocker/child.txt", "x")


def test_write_file_creates_dirs_and_reads_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write_file creates missing parent dirs and writes the content."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    out = _file_builtins.write_file("sub/dir/out.txt", "payload")
    assert "Wrote 7 characters" in out
    assert (tmp_path / "sub/dir/out.txt").read_text(encoding="utf-8") == "payload"


def test_write_file_overwrites_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing file is replaced (overwrite-only semantics)."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "f.txt").write_text("old", encoding="utf-8")
    _file_builtins.write_file("f.txt", "new")
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "new"


def test_write_file_rejects_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `..` path that escapes the root is rejected and writes nothing."""
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("FILE_TOOL_ROOT", str(root))
    out = _file_builtins.write_file("../escape.txt", "x")
    assert "outside the allowed workspace" in out
    assert not (tmp_path / "escape.txt").exists()


def test_write_file_marked_not_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write_file is the only mutating tool; read_file stays read-only."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    _file_builtins.refresh_file_tools_registration()
    assert TOOLS["write_file"].is_read_only is False
    assert TOOLS["read_file"].is_read_only is True


def test_refresh_registers_file_tools_when_root_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gating adds both file tools to the registry when a root is configured."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    _file_builtins.refresh_file_tools_registration()
    assert "read_file" in TOOLS
    assert "write_file" in TOOLS


def test_refresh_pops_file_tools_when_root_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gating removes both file tools when the root is unset."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    _file_builtins.refresh_file_tools_registration()
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    _file_builtins.refresh_file_tools_registration()
    assert "read_file" not in TOOLS
    assert "write_file" not in TOOLS


@pytest.mark.asyncio
async def test_run_tool_dispatches_write_then_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry dispatches both tools end-to-end via run_tool."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    _file_builtins.refresh_file_tools_registration()
    written = await run_tool("write_file", {"path": "a.txt", "content": "data"})
    assert "Wrote" in written.text
    read_back = await run_tool("read_file", {"path": "a.txt"})
    assert read_back.text == "data"


# ---------------------------------------------------------------------------
# list_directory (workspace directory browser)
# ---------------------------------------------------------------------------


def test_list_directory_lists_files_and_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directories appear before files; both are labeled and sorted."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "notes").mkdir()
    (tmp_path / "readme.txt").write_text("hi", encoding="utf-8")
    out = _file_builtins.list_directory(".")
    assert "[dir]  notes/" in out
    assert "[file] readme.txt" in out
    # Dirs come before files in the output.
    assert out.index("[dir]") < out.index("[file]")


def test_list_directory_default_path_is_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling with no argument lists the workspace root."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "hello.txt").write_text("x", encoding="utf-8")
    out = _file_builtins.list_directory()
    assert "[file] hello.txt" in out


def test_list_directory_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subdirectory path lists only that directory's contents."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    sub = tmp_path / "docs"
    sub.mkdir()
    (sub / "plan.md").write_text("y", encoding="utf-8")
    out = _file_builtins.list_directory("docs")
    assert "[file] plan.md" in out
    # Nothing from the root leaks in.
    assert "docs/" not in out.split("\n", 1)[1]  # header may say "docs/"


def test_list_directory_empty_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty directory returns a specific message rather than a blank listing."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "empty").mkdir()
    out = _file_builtins.list_directory("empty")
    assert "empty" in out


def test_list_directory_nonexistent_returns_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that doesn't exist returns an explanatory message, not an exception."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    out = _file_builtins.list_directory("nope")
    assert "No directory" in out


def test_list_directory_file_path_returns_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing at a file returns a helpful redirect, not a crash."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "f.txt").write_text("data", encoding="utf-8")
    out = _file_builtins.list_directory("f.txt")
    assert "is a file" in out
    assert "read_file" in out


def test_list_directory_rejects_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `..` path escaping the root is rejected without listing anything."""
    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "secret").mkdir()
    monkeypatch.setenv("FILE_TOOL_ROOT", str(root))
    out = _file_builtins.list_directory("../secret")
    assert "outside the allowed workspace" in out


def test_list_directory_rejects_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absolute path escapes the root and is rejected."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    out = _file_builtins.list_directory("/tmp")
    assert "outside the allowed workspace" in out


def test_list_directory_no_root_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no root configured the tool reports the misconfiguration."""
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    out = _file_builtins.list_directory(".")
    assert "not configured" in out


def test_list_directory_shows_file_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File entries include a human-readable size."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "small.txt").write_text("ab", encoding="utf-8")
    out = _file_builtins.list_directory(".")
    # Size appears in parentheses after the filename.
    assert "(" in out and "B)" in out


def test_list_directory_truncates_at_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directories with more than _LIST_DIR_CAP entries are truncated."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    for i in range(_file_builtins._LIST_DIR_CAP + 10):
        (tmp_path / f"file{i:04d}.txt").write_text("x", encoding="utf-8")
    out = _file_builtins.list_directory(".")
    assert f"showing first {_file_builtins._LIST_DIR_CAP}" in out
    assert out.count("[file]") == _file_builtins._LIST_DIR_CAP


def test_list_directory_header_item_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The header reports the true total count even when truncated."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    total = _file_builtins._LIST_DIR_CAP + 5
    for i in range(total):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    out = _file_builtins.list_directory(".")
    assert f"({total} items)" in out


def test_refresh_includes_list_directory_when_root_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gating adds list_directory to the registry when a root is configured."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    _file_builtins.refresh_file_tools_registration()
    assert "list_directory" in TOOLS


def test_refresh_pops_list_directory_when_root_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gating removes list_directory when the root is unset."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    _file_builtins.refresh_file_tools_registration()
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    _file_builtins.refresh_file_tools_registration()
    assert "list_directory" not in TOOLS


def test_list_directory_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """list_directory is read-only (no confirmation needed)."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    _file_builtins.refresh_file_tools_registration()
    assert TOOLS["list_directory"].is_read_only is True


@pytest.mark.asyncio
async def test_run_tool_dispatches_list_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry dispatches list_directory end-to-end via run_tool."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    _file_builtins.refresh_file_tools_registration()
    (tmp_path / "doc.txt").write_text("hello", encoding="utf-8")
    result = await run_tool("list_directory", {"path": "."})
    assert "[file] doc.txt" in result.text


# search_files (recursive glob search within workspace)
# ---------------------------------------------------------------------------


def test_search_files_finds_matching_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """search_files returns matching files with their paths and sizes."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "notes.md").write_text("hi", encoding="utf-8")
    (tmp_path / "readme.txt").write_text("bye", encoding="utf-8")
    out = _file_builtins.search_files("*.md")
    assert "notes.md" in out
    assert "readme.txt" not in out


def test_search_files_is_recursive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """search_files descends into subdirectories."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    sub = tmp_path / "docs"
    sub.mkdir()
    (sub / "plan.md").write_text("x", encoding="utf-8")
    out = _file_builtins.search_files("*.md")
    assert "docs/plan.md" in out or "docs" in out


def test_search_files_no_match_returns_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pattern with no matches returns an explanatory message."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "readme.txt").write_text("hi", encoding="utf-8")
    out = _file_builtins.search_files("*.md")
    assert "No files matching" in out


def test_search_files_excludes_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directories matching the pattern are not listed."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "notes").mkdir()  # a dir named "notes" — NOT a file
    (tmp_path / "notes.txt").write_text("file", encoding="utf-8")
    out = _file_builtins.search_files("notes*")
    assert "[file] notes.txt" in out
    assert "[dir]" not in out


def test_search_files_shows_file_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each result includes a human-readable size."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "a.md").write_text("hello", encoding="utf-8")
    out = _file_builtins.search_files("*.md")
    assert "(" in out and "B)" in out


def test_search_files_nonexistent_path_returns_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A starting path that doesn't exist returns an explanatory message."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    out = _file_builtins.search_files("*.md", path="nope")
    assert "No directory" in out


def test_search_files_file_path_returns_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing the starting path at a file returns a redirect message."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "f.txt").write_text("data", encoding="utf-8")
    out = _file_builtins.search_files("*.md", path="f.txt")
    assert "is a file" in out


def test_search_files_rejects_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `..` path escaping the root is rejected."""
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("FILE_TOOL_ROOT", str(root))
    out = _file_builtins.search_files("*.md", path="../escape")
    assert "outside the allowed workspace" in out


def test_search_files_rejects_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absolute starting path is rejected."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    out = _file_builtins.search_files("*.md", path="/tmp")
    assert "outside the allowed workspace" in out


def test_search_files_no_root_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no FILE_TOOL_ROOT the tool reports the misconfiguration."""
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    out = _file_builtins.search_files("*.md")
    assert "not configured" in out


def test_search_files_truncates_at_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Results are capped at _SEARCH_CAP; the header reports the true total."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    total = _file_builtins._SEARCH_CAP + 5
    for i in range(total):
        (tmp_path / f"file{i:04d}.md").write_text("x", encoding="utf-8")
    out = _file_builtins.search_files("*.md")
    assert f"showing first {_file_builtins._SEARCH_CAP}" in out
    assert out.count("[file]") == _file_builtins._SEARCH_CAP
    assert f"{total} file" in out


def test_search_files_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """search_files is read-only (no confirmation needed)."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    _file_builtins.refresh_file_tools_registration()
    assert TOOLS["search_files"].is_read_only is True


def test_refresh_includes_search_files_when_root_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gating adds search_files to the registry when a root is configured."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    _file_builtins.refresh_file_tools_registration()
    assert "search_files" in TOOLS


def test_refresh_pops_search_files_when_root_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gating removes search_files when the root is unset."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    _file_builtins.refresh_file_tools_registration()
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    _file_builtins.refresh_file_tools_registration()
    assert "search_files" not in TOOLS


@pytest.mark.asyncio
async def test_run_tool_dispatches_search_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry dispatches search_files end-to-end via run_tool."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    _file_builtins.refresh_file_tools_registration()
    (tmp_path / "doc.md").write_text("hello", encoding="utf-8")
    result = await run_tool("search_files", {"pattern": "*.md"})
    assert "doc.md" in result.text
