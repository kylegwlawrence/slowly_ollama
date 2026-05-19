"""Phase 12b/12c: tool decorator + registry + RAG tool tests."""

from pathlib import Path

import httpx
import pytest

from app.tools import TOOLS, run_tool, tool, tool_specs_for_ollama


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
    @tool
    def double(n: int) -> int:
        """Double n."""
        return n * 2

    assert await run_tool("double", {"n": 21}) == "42"


@pytest.mark.asyncio
async def test_run_tool_unknown_returns_error_string() -> None:
    result = await run_tool("nonexistent", {})
    assert "not registered" in result


@pytest.mark.asyncio
async def test_run_tool_arg_mismatch_returns_error_string() -> None:
    @tool
    def need_x(x: int) -> int:
        """Need x."""
        return x

    result = await run_tool("need_x", {"wrong_name": 1})
    # Returns explanatory string, doesn't raise.
    assert "rejected arguments" in result


@pytest.mark.asyncio
async def test_current_time_baseline() -> None:
    """The baseline tool runs and returns an ISO 8601 string."""
    from app.tools import builtins  # noqa: F401 — registers current_time
    result = await run_tool("current_time", {"timezone": "UTC"})
    # ISO format starts with YYYY-
    assert result[:4].isdigit()
    assert result[4] == "-"


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
    assert "Unknown RAG source 'missing'" in result


@pytest.mark.asyncio
async def test_query_rag_empty_query_returns_message(
    rag_db: Path,
) -> None:
    """An empty / whitespace-only query is rejected without an HTTP call."""
    from app.tools import rag as _rag  # noqa: F401 — registers query_rag

    result = await run_tool(
        "query_rag", {"source": "arxiv", "query": "   "}
    )
    assert "cannot be empty" in result


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
    # The body of the response made it through _format_chunks.
    assert "[1] Doc (§1)" in result
    assert "answer" in result


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
    assert "unreachable" in result


def test_refresh_query_rag_source_description_injects_names(
    rag_db: Path,
) -> None:
    """After CRUD, the tool's source-arg description lists the current names."""
    from app import rag_servers as _rs
    from app.connection import open_connection
    from app.tools import TOOLS
    from app.tools import rag as _rag  # noqa: F401 — registers query_rag
    from app.tools.rag import refresh_query_rag_source_description

    # Start: no servers → "(none configured)" in the hint.
    refresh_query_rag_source_description()
    desc = TOOLS["query_rag"].parameters_schema["properties"]["source"][
        "description"
    ]
    assert "(none configured)" in desc

    # Add some, refresh, hint reflects them.
    with open_connection() as conn:
        _rs.create_server(conn, "arxiv", "http://x/arxiv")
        _rs.create_server(conn, "wikipedia", "http://x/wiki")
    refresh_query_rag_source_description()
    desc = TOOLS["query_rag"].parameters_schema["properties"]["source"][
        "description"
    ]
    assert "arxiv" in desc
    assert "wikipedia" in desc


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
