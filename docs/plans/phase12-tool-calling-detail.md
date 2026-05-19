# Phase 12 — Detailed implementation plan

Executable companion to `docs/plans/phase12-tool-calling.md`. The
high-level plan covered decisions and roadmap; this file is what an
implementing agent (Claude or otherwise) reads and ships from. Code
snippets are verbatim where it matters — copy-paste-able.

If a section here disagrees with the high-level plan, **this file
wins** — it's the more recent version and has the locked RAG
contract.

## Locked decisions (one-line each)

- One feature per phase. Phase 12 ships tool-calling with two
  built-in tools: `current_time` (baseline) and `query_rag`
  (headline).
- Roll our own Python-decorator tool framework. No MCP yet.
- Auto-execute read-only tools. (Both phase 12 tools are read-only.)
- Tool calls and results persist as their own message rows;
  roles `tool_call` and `tool_result`.
- SQLite role CHECK is dropped — Python `Role` literal enforces.
- Server-side tool-calling loop; single SSE response carries
  `token`, `tool-call`, `tool-result`, `title`, `done`, `error`
  events.
- 5-iteration cap per assistant turn.
- Composer dropdown filters to tool-capable models.
- Multiple RAG servers, managed via standalone `/settings` page.
- RAG `source` argument uses a soft-hint description ("valid
  values are: arxiv, factbook, ...") — no JSON-schema enum.
- Per chunk text capped at 800 chars; `top_k=5` hardcoded; total
  result string capped at ~4 KB.

## RAG API contract (locked)

Inferred verbatim from a separate reference RAG-server repo at
`api/_chunks.py`, `api/models.py`, `api/routers/{arxiv,factbook,
openalex,gutenberg}.py`, and its `CLAUDE.md` (commit-as-of: main
branch when read during planning).

### Request

```
GET <server.url>/chunks?q=<query>&top_k=5&candidate_k=50
```

- `server.url` is the value of `rag_servers.url`. The user
  stores the **full base URL up through the source prefix**, e.g.
  `http://10.0.0.5:8002/arxiv`.
- We always hit the trailing `/chunks` path.
- Query params we send: `q` (required, the model's query string),
  `top_k=5` (hardcoded), `candidate_k` (omit; server defaults to 50).
- No request body.
- No auth headers (trusted local network per the ref repo's
  Tailscale ACL model).

### Response (200 OK)

```json
{
  "items": [
    {
      "chunk_id": 12345,
      "doc_id": "1812.00345",
      "title": "Attention Is All You Need",
      "section": null,
      "chunk_index": 0,
      "text": "We propose a new simple network architecture, the Transformer...",
      "text_length": 1234,
      "score": 0.95
    },
    ...
  ],
  "used_dense": true,
  "top_k": 5,
  "candidate_k": 50
}
```

Fields the tool uses for the model-facing rendering: `title`,
`section`, `text`. The rest are debug fields, discarded.
`used_dense` IS surfaced — if `false`, the rendered output
prepends a one-line note: `"(sparse-only retrieval; embedding
service unreachable)"` so the model knows the recall is
degraded.

### Error responses

| Status | Meaning | Our response |
|---|---|---|
| 400 | Empty `q` | Never sent — we validate client-side before the GET |
| 503 | RAG DB / indexes missing | Silent skip; return `"RAG source <name> unavailable (server reports indexes not built)"` to the model |
| `httpx.HTTPError` | Network / timeout | Silent skip; return `"RAG source <name> unreachable"` to the model |
| Other 5xx | Server error | Silent skip; return `"RAG source <name> failed (HTTP <status>)"` |

The model SEES these error strings as the tool result and can
choose to try a different source, refine the query, or proceed
without retrieval. We don't bubble HTTP errors as user-facing
chat errors — they're tool-call diagnostics.

### Timeout

`httpx.Timeout(15.0, connect=5.0)` on the RAG client. Retrieval
is fast (sparse FTS5 + dense ANN over local SQLite) — 15s read
is generous. Separate from the 120s chat timeout because we
don't want RAG to hold the SSE connection long.

---

## Sub-phase 12a — Schema + role expansion

### Changes

- `app/db.py`: remove the role CHECK from `messages` (recreate
  table for existing DBs); add `rag_servers` table.
- `app/queries.py`: extend `Role` literal; no existing query
  signatures change yet (CRUD for `rag_servers` lands in 12c).
- New `tests/test_db.py` cases: migration is idempotent;
  `rag_servers` table is present after init.

### Code: `app/db.py`

Replace `_SCHEMA_SQL`:

```python
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    model        TEXT NOT NULL,
    name_locked  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- The role CHECK has been removed (phase 12a). Validation now lives
-- in app.queries.Role (a typing.Literal). SQLite can't ALTER an
-- existing CHECK, so this only takes effect for fresh DBs; existing
-- DBs are migrated by _migrate_messages_drop_role_check below.
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
ON messages (conversation_id, created_at);

-- Phase 12a: configured RAG endpoints. Each row is one source the
-- chat model can query via the query_rag tool. `url` is the FULL
-- base URL up through the source prefix (e.g.
-- "http://10.0.0.5:8002/arxiv"); the tool appends "/chunks" itself.
CREATE TABLE IF NOT EXISTS rag_servers (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    url        TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
```

Add the migration helper and call it from `initialize_database`:

```python
def _migrate_messages_drop_role_check(conn: sqlite3.Connection) -> None:
    """Drop the role CHECK from an existing messages table.

    The original schema (phases 2–11) had `CHECK (role IN ('user',
    'assistant'))` on `messages.role`. Phase 12a expands the allowed
    roles to include `tool_call` and `tool_result`; the cleanest
    approach is to drop the CHECK entirely and let the Python `Role`
    literal enforce validity at the app layer.

    SQLite has no `ALTER TABLE ... DROP CONSTRAINT`. The portable
    workaround is to recreate the table without the CHECK and copy
    rows over. Idempotent: re-running detects the absence of the
    CHECK in `sqlite_master` and exits early.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages';"
    ).fetchone()
    if row is None or "CHECK" not in (row[0] or ""):
        # Table doesn't exist (fresh DB — the CREATE TABLE above
        # handled it) OR the CHECK has already been dropped.
        return
    # Recreate. Done in a single executescript so the rename is atomic
    # within one transaction.
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE messages_new (
            id              INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL
                REFERENCES conversations(id) ON DELETE CASCADE,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            created_at      TEXT NOT NULL
        );
        INSERT INTO messages_new (id, conversation_id, role, content, created_at)
            SELECT id, conversation_id, role, content, created_at FROM messages;
        DROP TABLE messages;
        ALTER TABLE messages_new RENAME TO messages;
        CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
            ON messages (conversation_id, created_at);
        COMMIT;
        """
    )
```

Update `initialize_database` body to call it after `executescript`:

```python
def initialize_database(path: Path | None = None) -> Path:
    target = path if path is not None else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(_SCHEMA_SQL)
        _ensure_name_locked_column(conn)
        _migrate_messages_drop_role_check(conn)
    return target
```

### Code: `app/queries.py`

Extend the `Role` type alias:

```python
# At the top, replace the existing Role alias:
Role = Literal["user", "assistant", "tool_call", "tool_result"]
```

No other signatures change in 12a. New `append_message` callers
in 12d will pass `"tool_call"` and `"tool_result"`; the existing
type signature accepts them via the widened Literal.

### Tests for 12a

`tests/test_db.py` additions:

```python
def test_migration_is_idempotent_on_fresh_db(tmp_path: Path) -> None:
    """A brand-new DB doesn't have the legacy CHECK; the migration
    must no-op without errors."""
    initialize_database(tmp_path / "chats.db")
    # Second run should also be fine.
    initialize_database(tmp_path / "chats.db")


def test_migration_drops_legacy_role_check(tmp_path: Path) -> None:
    """A pre-phase-12 DB has CHECK (role IN ('user','assistant'));
    after init, the CHECK is gone and tool_call rows insert cleanly."""
    db = tmp_path / "chats.db"
    # Simulate the legacy schema.
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, model TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

    initialize_database(db)

    conn = sqlite3.connect(db)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='messages';"
    ).fetchone()[0]
    assert "CHECK" not in sql
    # Confirm the new roles INSERT successfully.
    conn.execute("INSERT INTO conversations (name, model, name_locked, created_at, updated_at) VALUES ('x', 'm', 0, 'now', 'now');")
    conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) VALUES (1, 'tool_call', '{}', 'now');")
    conn.commit()
    conn.close()


def test_rag_servers_table_exists_after_init(tmp_path: Path) -> None:
    initialize_database(tmp_path / "chats.db")
    import sqlite3
    conn = sqlite3.connect(tmp_path / "chats.db")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(rag_servers);")}
    assert cols == {"id", "name", "url", "created_at", "updated_at"}
```

---

## Sub-phase 12b — Tool decorator + registry + baseline tool

### Layout

```
app/
  tools/
    __init__.py       # @tool decorator, ToolSpec, registry, helpers
    builtins.py       # current_time
```

### Code: `app/tools/__init__.py`

```python
"""Phase 12: tool definitions and registry for tool-calling.

A `@tool` decorator turns a Python function into a tool the chat
model can call:
- The function's name becomes the tool's name.
- The first line of its docstring becomes the tool description.
- Its type hints become the tool's argument JSON schema.
- An `is_read_only` flag (default True) determines whether tool
  execution requires user confirmation (phase 12 has no
  confirm-needed tools yet; the flag is forward-looking).

The registry (`TOOLS`) is a module-level dict the routes layer
queries via `tool_specs_for_ollama()` (formats for Ollama's
/api/chat tools=[...] parameter) and `run_tool()` (dispatches a
named tool call to its function).
"""

import inspect
import re
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool's metadata + the callable that runs it.

    Attributes:
        name: Tool name as the model sees it (the function's Python name).
        description: First line of the function's docstring.
        parameters_schema: JSON schema for the function's arguments
            (Ollama "tools" parameter format).
        is_read_only: When True, the route layer auto-executes the
            tool. When False, the route should require user
            confirmation (no such tools exist in phase 12 yet).
        func: The actual callable. May be sync or async; `run_tool`
            awaits async functions and calls sync ones directly.
    """
    name: str
    description: str
    parameters_schema: dict
    is_read_only: bool
    func: Callable[..., object] | Callable[..., Awaitable[object]]


TOOLS: dict[str, ToolSpec] = {}


# Maps Python types to JSON schema types. Anything not in this map
# defaults to "string" — the model will pass strings and the
# function's type coercion handles the rest.
_TYPE_TO_JSON_SCHEMA = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
}


def _parse_arg_descriptions(docstring: str) -> dict[str, str]:
    """Pull `Args:` block lines from a Google-style docstring.

    Returns a {arg_name: description} dict. Tolerates missing or
    malformed Args blocks (returns {}). Doesn't try to handle
    multi-line arg descriptions — first line wins.
    """
    if not docstring:
        return {}
    lines = docstring.splitlines()
    in_args = False
    out: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Args:"):
            in_args = True
            continue
        if in_args:
            # Args section ends at the next blank line or section header.
            if not stripped or stripped.endswith(":"):
                in_args = False
                continue
            # Expected form: "arg_name: description text"
            m = re.match(r"^(\w+):\s*(.+)$", stripped)
            if m:
                out[m.group(1)] = m.group(2)
    return out


def tool(
    func: Callable | None = None,
    *,
    is_read_only: bool = True,
) -> Callable:
    """Decorate a function to register it as a callable tool.

    Usage::

        @tool
        def current_time(timezone: str = "UTC") -> str:
            \"\"\"Get the current time.

            Args:
                timezone: IANA timezone name like "America/Vancouver".
            \"\"\"
            ...

    Args:
        func: The function being decorated (when used without arguments).
        is_read_only: When True, the route layer auto-executes. When
            False, user confirmation is required (phase 12 has no such
            tools yet).

    Returns:
        The original function, unmodified. Registration is a side
        effect — call sites use the function normally; the model
        sees it via TOOLS / tool_specs_for_ollama().
    """
    def _decorate(fn: Callable) -> Callable:
        name = fn.__name__
        doc = inspect.getdoc(fn) or ""
        description = doc.split("\n", 1)[0].strip() or name
        arg_descriptions = _parse_arg_descriptions(doc)

        hints = typing.get_type_hints(fn)
        sig = inspect.signature(fn)

        properties: dict[str, dict] = {}
        required: list[str] = []
        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            py_type = hints.get(param_name, str)
            # Strip Optional[X] / X | None down to X for schema purposes.
            origin = typing.get_origin(py_type)
            if origin is typing.Union or (origin is not None and type(None) in typing.get_args(py_type)):
                non_none = [a for a in typing.get_args(py_type) if a is not type(None)]
                py_type = non_none[0] if non_none else str
            schema = dict(_TYPE_TO_JSON_SCHEMA.get(py_type, {"type": "string"}))
            if param_name in arg_descriptions:
                schema["description"] = arg_descriptions[param_name]
            properties[param_name] = schema
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        parameters_schema = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

        spec = ToolSpec(
            name=name,
            description=description,
            parameters_schema=parameters_schema,
            is_read_only=is_read_only,
            func=fn,
        )
        TOOLS[name] = spec
        return fn

    # Allow both `@tool` and `@tool(is_read_only=False)` forms.
    if func is None:
        return _decorate
    return _decorate(func)


def tool_specs_for_ollama() -> list[dict]:
    """Format every registered tool as an entry in Ollama's
    /api/chat `tools` parameter.

    Returns a list of dicts shaped like::

        {"type": "function",
         "function": {"name": "...", "description": "...",
                      "parameters": {... JSON schema ...}}}
    """
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters_schema,
            },
        }
        for spec in TOOLS.values()
    ]


async def run_tool(name: str, args: dict) -> str:
    """Look up a tool by name and call it with the given args.

    Args:
        name: The tool's registered name (from `@tool`).
        args: The argument dict the model sent in its tool_call.

    Returns:
        Whatever the tool returns, stringified. Returns an
        explanatory error string for unknown tools or argument
        mismatches — never raises (the caller stores the result
        verbatim as a tool_result message; raising would break
        the SSE stream).
    """
    spec = TOOLS.get(name)
    if spec is None:
        return f"Tool '{name}' is not registered."
    try:
        result = spec.func(**args)
        if inspect.isawaitable(result):
            result = await result
        return str(result)
    except TypeError as e:
        # Argument mismatch — the model passed wrong kwargs.
        return f"Tool '{name}' rejected arguments: {e}"
    except Exception as e:
        # Tool itself raised; surface to the model but don't crash.
        return f"Tool '{name}' failed: {e}"
```

### Code: `app/tools/builtins.py`

```python
"""Built-in tools shipped with phase 12.

Currently only one: `current_time`, the baseline that validates the
tool-calling loop without depending on any external service.
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.tools import tool


@tool
def current_time(timezone: str = "UTC") -> str:
    """Get the current time as an ISO 8601 string.

    Args:
        timezone: IANA timezone name like "America/Vancouver" or "UTC".
            Defaults to "UTC". Unknown names fall back to UTC and
            include a note in the returned string.
    """
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        return f"Unknown timezone '{timezone}'; defaulted to UTC. Now: {datetime.now(ZoneInfo('UTC')).isoformat()}"
    return datetime.now(tz).isoformat()
```

### Tests for 12b

`tests/test_tools.py`:

```python
"""Phase 12b: tool decorator + registry tests."""

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
```

---

## Sub-phase 12c — RAG tool + server CRUD + settings UI

### Layout

```
app/
  rag_servers.py    # DB-backed CRUD for rag_servers table
  tools/
    rag.py          # query_rag tool + HTTP client
templates/
  _settings.html    # standalone settings page (sidebar + main content)
  _rag_servers_list.html  # the list of server rows + add form
```

### Code: `app/rag_servers.py`

```python
"""Phase 12c: CRUD for the rag_servers table.

Mirrors the conversations / messages query helpers in style:
each function takes a sqlite3.Connection and wraps writes in
`with conn:` for atomicity. RAG servers are user-configured at
runtime via the /settings UI.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class RagServer:
    """One row of the `rag_servers` table.

    Attributes:
        id: Auto-assigned primary key.
        name: Short human/model-facing identifier (e.g. "arxiv").
            Used as the `source` argument value in query_rag.
        url: Full base URL up through the source prefix
            (e.g. "http://10.0.0.5:8002/arxiv"). The query_rag
            tool appends "/chunks" itself.
        created_at: UTC datetime.
        updated_at: UTC datetime.
    """
    id: int
    name: str
    url: str
    created_at: datetime
    updated_at: datetime


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_server(row: sqlite3.Row) -> RagServer:
    return RagServer(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def list_servers(conn: sqlite3.Connection) -> list[RagServer]:
    """Return every configured RAG server, oldest first.

    Stable order so the settings UI doesn't reshuffle on every load.
    """
    rows = conn.execute(
        "SELECT id, name, url, created_at, updated_at FROM rag_servers"
        " ORDER BY id ASC;"
    ).fetchall()
    return [_row_to_server(r) for r in rows]


def create_server(
    conn: sqlite3.Connection, name: str, url: str
) -> RagServer:
    """Insert a new RAG server row.

    Raises:
        sqlite3.IntegrityError: name is already used (UNIQUE
            constraint). The route handler converts this to a 409.
    """
    now = _now_iso()
    with conn:
        row = conn.execute(
            "INSERT INTO rag_servers (name, url, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " RETURNING id, name, url, created_at, updated_at;",
            (name, url, now, now),
        ).fetchone()
    return _row_to_server(row)


def delete_server(conn: sqlite3.Connection, server_id: int) -> None:
    """Delete a server row by id. Idempotent — missing id is fine."""
    with conn:
        conn.execute("DELETE FROM rag_servers WHERE id = ?;", (server_id,))
```

### Code: `app/tools/rag.py`

```python
"""Phase 12c: the query_rag tool.

Hits a configured RAG server's /chunks endpoint and returns
retrieved chunks formatted as a readable citation block for the
chat model.

The list of valid `source` names is rebuilt every time
`tool_specs_for_ollama()` is called (it walks the DB), so adding
or removing a server in /settings is reflected in the next
chat-message round-trip without a restart.
"""

import httpx

from app import rag_servers as _rag_servers_module
from app.connection import open_connection
from app.tools import tool

# Hard caps to keep RAG output from blowing the model's context window.
_TOP_K = 5
_PER_CHUNK_TEXT_CAP = 800
_TOTAL_OUTPUT_CAP = 4000

# Retrieval should be fast — sparse FTS5 + dense ANN over local SQLite.
# 15s read is generous; bumped to 5s connect for slow Tailscale routes.
_RAG_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def _list_source_names() -> list[str]:
    """Walk the rag_servers table for the current set of source names.

    Used to refresh the query_rag tool's description on every
    tool_specs_for_ollama() call so the model sees an up-to-date
    list of valid `source` values.
    """
    with open_connection() as conn:
        return [s.name for s in _rag_servers_module.list_servers(conn)]


def _format_chunks(items: list[dict], used_dense: bool) -> str:
    """Render the RAG response as a readable citation block.

    Each chunk becomes::

        [N] <title> (§<section>)
            <text>...

    Sections that are None are omitted. Texts are truncated to
    _PER_CHUNK_TEXT_CAP characters with an ellipsis. The final
    string is hard-capped to _TOTAL_OUTPUT_CAP characters.

    A "(sparse-only retrieval; embedding service unreachable)"
    note prepends when used_dense=False, so the model knows recall
    is degraded.
    """
    parts: list[str] = []
    if not used_dense:
        parts.append(
            "(sparse-only retrieval; embedding service unreachable)\n"
        )
    for idx, item in enumerate(items, 1):
        title = item.get("title") or "(untitled)"
        section = item.get("section")
        header = f"[{idx}] {title}"
        if section:
            header += f" (§{section})"
        text = (item.get("text") or "").strip()
        if len(text) > _PER_CHUNK_TEXT_CAP:
            text = text[: _PER_CHUNK_TEXT_CAP - 3] + "..."
        parts.append(f"{header}\n    {text}\n")
    out = "\n".join(parts).strip()
    if len(out) > _TOTAL_OUTPUT_CAP:
        out = out[: _TOTAL_OUTPUT_CAP - 3] + "..."
    return out or "(no matching chunks)"


@tool
async def query_rag(source: str, query: str) -> str:
    """Retrieve passages from a configured RAG source.

    Args:
        source: Name of the configured RAG server to query.
            Valid values are discovered at runtime from the
            user's settings (see /settings).
        query: Natural-language query string.
    """
    # Description is rewritten each call to inject the live source
    # list — see _refresh_query_rag_description below.
    if not query.strip():
        return "Tool query_rag: 'query' cannot be empty."

    with open_connection() as conn:
        servers = _rag_servers_module.list_servers(conn)
    by_name = {s.name: s for s in servers}
    server = by_name.get(source)
    if server is None:
        names = ", ".join(by_name.keys()) or "(none configured)"
        return f"Unknown RAG source '{source}'. Configured sources: {names}"

    url = f"{server.url.rstrip('/')}/chunks"
    try:
        async with httpx.AsyncClient(timeout=_RAG_TIMEOUT) as client:
            response = await client.get(
                url, params={"q": query, "top_k": _TOP_K}
            )
    except httpx.HTTPError:
        return f"RAG source '{source}' unreachable."

    if response.status_code == 503:
        return (
            f"RAG source '{source}' unavailable (server reports "
            f"indexes not built)."
        )
    if response.status_code >= 500:
        return f"RAG source '{source}' failed (HTTP {response.status_code})."
    if response.status_code >= 400:
        # Includes 400 on empty q (shouldn't happen, we validated).
        return f"RAG source '{source}' rejected the query (HTTP {response.status_code})."

    try:
        body = response.json()
        items = body.get("items") or []
        used_dense = bool(body.get("used_dense", True))
    except ValueError:
        return f"RAG source '{source}' returned non-JSON response."

    return _format_chunks(items, used_dense)


def refresh_query_rag_source_description() -> None:
    """Re-inject the current source list into query_rag's description.

    Called by the settings route handlers after CRUD operations so
    the next tool_specs_for_ollama() call reflects the updated
    list. (We can't just modify the docstring — the description is
    cached in the ToolSpec at registration time. We mutate the
    cached spec.parameters_schema for `source` and re-cache.)
    """
    from app.tools import TOOLS
    spec = TOOLS.get("query_rag")
    if spec is None:
        return
    names = _list_source_names()
    sources_hint = (
        f"Name of the configured RAG server to query."
        f" Valid values are: {', '.join(names) if names else '(none configured)'}"
    )
    spec.parameters_schema["properties"]["source"]["description"] = sources_hint
```

(Implementation note: since `ToolSpec` is `frozen=True`, mutating
`spec.parameters_schema` works because that's a `dict` field —
the dict itself isn't frozen. The implementing agent should
verify this is the desired pattern; an alternative is to make
`ToolSpec` mutable or to register `query_rag` lazily on each call.)

### Code: `app/routes.py` settings additions

```python
from app import rag_servers as _rag_servers_module
from app.tools.rag import refresh_query_rag_source_description


@router.get("/settings", response_class=HTMLResponse)
def settings_endpoint(request: Request, db: DB) -> Response:
    """Standalone settings page — RAG servers in phase 12; more
    settings join in later phases.

    Direct hits return the full index shell with the settings
    fragment in the main area. HTMX requests return just the
    fragment for swap into #main.
    """
    servers = _rag_servers_module.list_servers(db)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_settings.html",
            context={"servers": servers},
        )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "chats": queries.list_conversations(db),
            "conversation": None,
            "messages": [],
            "active_chat_id": None,
            "settings_view": True,
            "rag_servers": servers,
        },
    )


@router.post("/settings/servers", response_class=HTMLResponse)
def add_server_endpoint(
    request: Request, db: DB,
    name: Annotated[str, Form()],
    url: Annotated[str, Form()],
) -> Response:
    """Add a RAG server. Returns the new <li> for OOB swap into
    the list, plus a 409 on UNIQUE violation."""
    try:
        server = _rag_servers_module.create_server(db, name=name.strip(), url=url.strip())
    except sqlite3.IntegrityError:
        return HTMLResponse(
            f"Server name '{html.escape(name)}' already in use.",
            status_code=status.HTTP_409_CONFLICT,
        )
    refresh_query_rag_source_description()
    return templates.TemplateResponse(
        request=request,
        name="_rag_server_row.html",
        context={"server": server},
    )


@router.delete("/settings/servers/{server_id}", response_class=HTMLResponse)
def delete_server_endpoint(server_id: int, db: DB) -> Response:
    """Delete a RAG server. Returns empty 200 for hx-swap="delete"."""
    _rag_servers_module.delete_server(db, server_id)
    refresh_query_rag_source_description()
    return Response(content="", status_code=status.HTTP_200_OK)
```

### Templates

`templates/_settings.html` (rendered as the main pane when
`settings_view=True` in index.html, or returned standalone for
HTMX swaps):

```html
{# Settings page — RAG server management in phase 12. #}
<section class="settings">
  <header class="settings__header">
    <h1 class="settings__title">Settings</h1>
    <p class="settings__subtitle">Configure RAG servers the chat model can query as a tool.</p>
  </header>

  <section class="settings__section">
    <h2 class="settings__section-title">RAG servers</h2>

    <ul id="rag-servers-list" class="rag-servers">
      {% for server in servers %}
        {% include "_rag_server_row.html" %}
      {% endfor %}
    </ul>

    <form class="rag-server-form"
          hx-post="/settings/servers"
          hx-target="#rag-servers-list"
          hx-swap="beforeend"
          hx-on::after-request="if (event.detail.successful) this.reset()">
      <label>
        Name
        <input type="text" name="name" required placeholder="arxiv">
      </label>
      <label>
        URL
        <input type="url" name="url" required
               placeholder="http://10.0.0.5:8002/arxiv">
      </label>
      <button type="submit">Add server</button>
    </form>
  </section>
</section>
```

`templates/_rag_server_row.html`:

```html
{# One row in the RAG servers list. #}
<li id="rag-server-{{ server.id }}" class="rag-server">
  <div class="rag-server__name">{{ server.name }}</div>
  <code class="rag-server__url">{{ server.url }}</code>
  <button type="button" class="rag-server__delete"
          hx-delete="/settings/servers/{{ server.id }}"
          hx-target="#rag-server-{{ server.id }}"
          hx-swap="delete"
          hx-confirm="Remove RAG server '{{ server.name }}'?">
    <span class="material-symbols-outlined">delete</span>
  </button>
</li>
```

Update `templates/index.html` to handle `settings_view`:

```html
<main id="main">
  {% if settings_view %}
    {% include "_settings.html" %}
  {% elif conversation %}
    {% include "_chat_panel.html" %}
  {% else %}
    {% include "_composer.html" %}
  {% endif %}
</main>
```

Add a sidebar footer link (mirrors the slot 11c didn't ship):

```html
<aside class="sidebar">
  <div class="sidebar__header">
    ...
  </div>
  {% include "_chats_list.html" %}
  <div class="sidebar__footer">
    <a class="sidebar__settings" href="/settings"
       hx-get="/settings" hx-target="#main" hx-swap="innerHTML"
       hx-push-url="/settings">
      <span class="material-symbols-outlined">settings</span>
      Settings
    </a>
  </div>
</aside>
```

### CSS additions (paste into `static/style.css`)

```css
/* ===== Sidebar footer (settings link) ============================
   margin-top: auto pushes the footer to the bottom of the flex
   column. Same pattern the deferred 11c plan called for.
*/

.sidebar__footer {
  margin-top: auto;
  padding-top: var(--space-md);
  border-top: 1px solid var(--border);
}

.sidebar__settings {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  color: var(--text-secondary);
  text-decoration: none;
  padding: var(--space-xs) var(--space-sm);
  border-radius: var(--radius-md);
  font-size: 13px;
}

.sidebar__settings:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}

/* ===== Settings page ===================================================== */

.settings {
  max-width: 720px;
  margin: 0 auto;
  padding: var(--space-xl);
  display: flex;
  flex-direction: column;
  gap: var(--space-xl);
}

.settings__title {
  font-size: 24px;
  font-weight: 500;
  margin: 0;
}

.settings__subtitle {
  color: var(--text-secondary);
  margin: var(--space-xs) 0 0;
}

.settings__section-title {
  font-size: 16px;
  font-weight: 500;
  margin: 0 0 var(--space-md);
}

.rag-servers {
  list-style: none;
  margin: 0 0 var(--space-md);
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
}

.rag-server {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: var(--space-md);
  padding: var(--space-sm) var(--space-md);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.rag-server__name { font-weight: 500; }

.rag-server__url {
  font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 12px;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.rag-server__delete {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--text-secondary);
  padding: var(--space-xs);
  border-radius: var(--radius-sm);
  width: auto;
}

.rag-server__delete:hover { background: var(--surface-hover); color: var(--danger); }

.rag-server-form {
  display: grid;
  grid-template-columns: 1fr 2fr auto;
  gap: var(--space-sm);
  align-items: end;
  padding: var(--space-md);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.rag-server-form label {
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
  font-size: 12px;
  color: var(--text-secondary);
  margin: 0;
}

.rag-server-form input {
  font-size: 14px;
  padding: var(--space-xs) var(--space-sm);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text-primary);
  height: auto;
  margin: 0;
}

.rag-server-form button {
  background: var(--accent);
  color: var(--accent-on);
  border: none;
  padding: var(--space-xs) var(--space-md);
  border-radius: var(--radius-md);
  cursor: pointer;
  font-weight: 500;
}
```

### Tests for 12c

`tests/test_rag_servers.py`:

```python
"""Phase 12c: rag_servers CRUD."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.connection import open_connection
from app.db import initialize_database
from app.rag_servers import create_server, delete_server, list_servers


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "chats.db"
    initialize_database(db_path)
    with open_connection(db_path) as c:
        yield c


def test_list_empty(conn: sqlite3.Connection) -> None:
    assert list_servers(conn) == []


def test_create_and_list(conn: sqlite3.Connection) -> None:
    s1 = create_server(conn, "arxiv", "http://x/arxiv")
    s2 = create_server(conn, "factbook", "http://x/factbook")
    assert s1.id < s2.id
    rows = list_servers(conn)
    assert [r.name for r in rows] == ["arxiv", "factbook"]
    assert rows[0].url == "http://x/arxiv"


def test_unique_name(conn: sqlite3.Connection) -> None:
    create_server(conn, "arxiv", "http://x/arxiv")
    with pytest.raises(sqlite3.IntegrityError):
        create_server(conn, "arxiv", "http://y/arxiv")


def test_delete_idempotent(conn: sqlite3.Connection) -> None:
    s = create_server(conn, "arxiv", "http://x/arxiv")
    delete_server(conn, s.id)
    delete_server(conn, s.id)  # no error
    delete_server(conn, 999)  # no error
    assert list_servers(conn) == []
```

`tests/test_tools.py` extensions (RAG tool with mocked httpx):

```python
@pytest.mark.asyncio
async def test_query_rag_unknown_source_returns_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the model picks a source that's not configured, the tool
    returns an explanatory message — doesn't raise."""
    # Patch list_servers to return an empty list.
    from app import rag_servers
    monkeypatch.setattr(rag_servers, "list_servers", lambda _conn: [])

    from app.tools import builtins  # noqa: F401
    from app.tools.rag import query_rag  # registers; noqa: F401
    from app.tools import run_tool

    result = await run_tool("query_rag", {"source": "missing", "query": "hi"})
    assert "Unknown RAG source 'missing'" in result


@pytest.mark.asyncio
async def test_query_rag_formats_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful retrieval renders as a numbered citation block."""
    from app.tools.rag import query_rag, _format_chunks
    items = [
        {"title": "Foo", "section": "Intro", "text": "hello world"},
        {"title": "Bar", "section": None, "text": "another chunk"},
    ]
    out = _format_chunks(items, used_dense=True)
    assert "[1] Foo (§Intro)" in out
    assert "hello world" in out
    assert "[2] Bar" in out
    assert "another chunk" in out
    # No sparse-only note when used_dense=True.
    assert "sparse-only" not in out


def test_format_chunks_sparse_only_note() -> None:
    from app.tools.rag import _format_chunks
    out = _format_chunks(
        [{"title": "T", "section": None, "text": "x"}],
        used_dense=False,
    )
    assert "sparse-only retrieval" in out


def test_format_chunks_truncates_long_text() -> None:
    from app.tools.rag import _format_chunks, _PER_CHUNK_TEXT_CAP
    long_text = "x" * (_PER_CHUNK_TEXT_CAP + 500)
    out = _format_chunks([{"title": "T", "section": None, "text": long_text}], used_dense=True)
    # The chunk text portion is capped (the prefix counts towards _PER_CHUNK_TEXT_CAP).
    assert "..." in out
```

`tests/test_routes.py` additions for /settings:

```python
def test_settings_get_renders_full_page_on_direct_hit(make_client):
    with make_client(_ollama_unreachable) as client:
        response = client.get("/settings")
    assert response.status_code == 200
    assert "<!DOCTYPE html>" in response.text
    assert 'class="settings"' in response.text


def test_settings_get_returns_fragment_for_htmx(make_client):
    with make_client(_ollama_unreachable) as client:
        response = client.get("/settings", headers={"HX-Request": "true"})
    assert "<!DOCTYPE html>" not in response.text
    assert 'class="settings"' in response.text


def test_settings_add_server_returns_row(make_client):
    with make_client(_ollama_unreachable) as client:
        response = client.post(
            "/settings/servers",
            data={"name": "arxiv", "url": "http://x/arxiv"},
        )
    assert response.status_code == 200
    assert "arxiv" in response.text
    assert 'id="rag-server-' in response.text


def test_settings_add_server_duplicate_name_409(make_client):
    with make_client(_ollama_unreachable) as client:
        client.post("/settings/servers", data={"name": "x", "url": "http://x/"})
        response = client.post("/settings/servers", data={"name": "x", "url": "http://y/"})
    assert response.status_code == 409


def test_settings_delete_server_empty_200(make_client):
    with make_client(_ollama_unreachable) as client:
        client.post("/settings/servers", data={"name": "x", "url": "http://x/"})
        # Servers are listed at IDs starting from 1.
        response = client.delete("/settings/servers/1")
    assert response.status_code == 200
    assert response.text == ""
```

---

## Sub-phase 12d — Server-side tool-calling loop

### Refactored `_stream_assistant_reply`

Replace the existing function in `app/routes.py` with the
iteration-based version. Pseudocode followed by the actual code.

**Loop structure:**

1. Build messages array from DB history (including any tool_call /
   tool_result rows from previous iterations in this turn).
2. Call Ollama with `tools=tool_specs_for_ollama()`.
3. If Ollama returns a non-streaming response with `tool_calls`:
   for each call, persist `tool_call` row, emit `tool-call` event,
   run the tool, persist `tool_result` row, emit `tool-result`
   event. Loop back to step 1.
4. If Ollama starts streaming text: yield `token` events as
   before. On end, persist assistant message, emit `title` (if
   conditions met), then emit `done`.
5. Hard cap at 5 iterations. On exceeded, persist a final
   assistant message ("(Tool-call limit reached.)"), emit `done`.

```python
_TOOL_ITERATION_CAP = 5


async def _stream_assistant_reply(
    client,
    db,
    conversation_id: int,
    model: str,
    history: list,
    on_complete: str,
) -> AsyncIterator[str]:
    """Stream Ollama tokens as SSE, with phase-12 tool-calling loop.

    The loop alternates between Ollama and tools until Ollama
    produces plain text (no tool_calls), which is then streamed to
    the user. Each iteration's tool calls + results persist as
    their own message rows and emit OOB-swap SSE events so the
    chat UI shows them as cards above the streaming bubble.

    Args:
        client: Shared httpx AsyncClient.
        db: Shared SQLite connection.
        conversation_id: Parent conversation id.
        model: Ollama model identifier.
        history: Initial Message dataclasses for the prompt. Tool
            messages added mid-turn are read back from the DB
            after each persist.
        on_complete: "append" (new send) or "replace" (regenerate).
    """
    # Build the live working history once; we re-read after each
    # tool round to pick up freshly-persisted tool messages.
    working_history = list(history)
    tools_payload = tool_specs_for_ollama() if model_supports_tools(model) else None

    for iteration in range(_TOOL_ITERATION_CAP):
        # Non-streaming first attempt to detect whether Ollama wants
        # a tool. If yes, handle the calls and loop. If no, switch
        # to streaming for the final text response.
        try:
            tool_calls, _ = await ollama.maybe_tool_call(
                client, model, _build_history_payload(working_history),
                tools=tools_payload,
            )
        except OllamaUnavailable as e:
            yield _sse(
                f'<div class="error">Ollama unavailable: {html.escape(str(e))}</div>',
                event="error",
            )
            return
        except OllamaProtocolError as e:
            yield _sse(
                f'<div class="error">Ollama protocol error: {html.escape(str(e))}</div>',
                event="error",
            )
            return

        if not tool_calls:
            # No tool requested; stream the actual response.
            break

        # Persist & run each tool call.
        for call in tool_calls:
            call_row = queries.append_message(
                db, conversation_id, "tool_call",
                content=json.dumps({"name": call["name"], "arguments": call["arguments"]}),
            )
            yield _sse(
                templates.get_template("_tool_call.html").render(
                    name=call["name"],
                    arguments=call["arguments"],
                    swap_target=f"#assistant-stream-{conversation_id}",
                ),
                event="tool-call",
            )

            result = await run_tool(call["name"], call["arguments"])

            result_row = queries.append_message(
                db, conversation_id, "tool_result",
                content=result,
            )
            yield _sse(
                templates.get_template("_tool_result.html").render(
                    name=call["name"],
                    result=result,
                    swap_target=f"#assistant-stream-{conversation_id}",
                ),
                event="tool-result",
            )

        # Refresh history from DB to include the newly-persisted rows.
        working_history = queries.list_messages(db, conversation_id)
    else:
        # `for` loop exhausted without breaking — hit the cap.
        message = queries.append_message(
            db, conversation_id, "assistant",
            content="(Tool-call limit reached; no final answer produced.)",
        )
        final_html = templates.get_template("_message.html").render(
            message=message,
            swap_target=f"#assistant-stream-{conversation_id}",
        )
        yield _sse(final_html, event="done")
        return

    # Final round: stream the model's text response.
    chunks: list[str] = []
    try:
        async for chunk in ollama.stream_chat(
            client, model, _build_history_payload(working_history),
        ):
            if chunk.content:
                chunks.append(chunk.content)
                yield _sse(html.escape(chunk.content), event="token")
            if chunk.done:
                break
    except OllamaUnavailable as e:
        yield _sse(
            f'<div class="error">Ollama unavailable: {html.escape(str(e))}</div>',
            event="error",
        )
        return
    except OllamaProtocolError as e:
        yield _sse(
            f'<div class="error">Ollama protocol error: {html.escape(str(e))}</div>',
            event="error",
        )
        return

    full_text = "".join(chunks)
    if on_complete == "append":
        message = queries.append_message(
            db, conversation_id, "assistant", full_text,
        )
    else:
        message = queries.replace_last_assistant_message(
            db, conversation_id, full_text,
        )

    # Phase 11d title fires BEFORE done (placeholder removal closes
    # the SSE connection).
    if on_complete == "append":
        async for sse_event in _maybe_generate_title(client, db, conversation_id):
            yield sse_event

    final_html = templates.get_template("_message.html").render(
        message=message,
        swap_target=f"#assistant-stream-{conversation_id}",
    )
    yield _sse(final_html, event="done")
```

### New `ollama.maybe_tool_call`

`app/ollama.py` gains a helper that does ONE non-streaming
`/api/chat` call and returns either tool_calls or empty:

```python
async def maybe_tool_call(
    client: httpx.AsyncClient,
    model: str,
    messages: list[dict[str, str]],
    tools: list[dict] | None,
) -> tuple[list[dict], str]:
    """Single non-streaming /api/chat to detect tool calls.

    Returns:
        (tool_calls, content). tool_calls is a list of
        {"name": ..., "arguments": {...}} dicts (Ollama format,
        unwrapped from the wire shape). content is the assistant
        text the model emitted alongside (usually empty when
        tool_calls is non-empty; some models add a brief
        explanation).

        If the model decides NOT to call a tool, tool_calls is []
        and content holds whatever text it emitted. The caller
        discards this content and re-calls Ollama in streaming
        mode for the final visible response — yes, this is two
        Ollama calls per "final answer" turn. Acceptable cost
        for a local app; revisit if latency becomes annoying.
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    try:
        response = await client.post("/api/chat", json=payload)
        response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise OllamaUnavailable(f"Ollama request failed: {e}") from e

    try:
        body = response.json()
        message = body.get("message", {})
        raw_calls = message.get("tool_calls") or []
        content = message.get("content") or ""
        # Ollama wraps tool calls as {"function": {"name": ..., "arguments": {...}}}
        unwrapped = [
            {
                "name": tc["function"]["name"],
                "arguments": tc["function"].get("arguments", {}),
            }
            for tc in raw_calls
        ]
        return unwrapped, content
    except (KeyError, TypeError, ValueError) as e:
        raise OllamaProtocolError(
            f"Ollama returned an unexpected /api/chat shape: {e}"
        ) from e
```

### Wire-format conversion

The DB stores `tool_call` content as JSON (`{"name", "arguments"}`)
and `tool_result` content as raw string. When sending history back
to Ollama for the next iteration, these need to convert to
Ollama's wire format. Update `_build_history_payload`:

```python
def _build_history_payload(history: list) -> list[dict]:
    """Turn Message dataclasses into the wire format Ollama expects.

    Roles map:
        user / assistant → passed through.
        tool_call → assistant message with tool_calls=[...].
        tool_result → tool role message with content=<result>.
    """
    out: list[dict] = []
    for m in history:
        if m.role == "tool_call":
            try:
                payload = json.loads(m.content)
                out.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "function": {
                            "name": payload["name"],
                            "arguments": payload["arguments"],
                        },
                    }],
                })
            except (json.JSONDecodeError, KeyError):
                # Skip malformed rows rather than failing the request.
                continue
        elif m.role == "tool_result":
            out.append({"role": "tool", "content": m.content})
        else:
            out.append({"role": m.role, "content": m.content})
    return out
```

(Note: `import json` returns to `app/routes.py`. The phase-11
review removed it as dead code; phase 12d needs it again for
serializing tool-call args.)

### Tests for 12d

`tests/test_routes.py` additions (illustrative — implementer
fills in based on the existing make_client fixture):

```python
def test_stream_runs_tool_then_streams_final(
    make_client, monkeypatch
):
    """Mocked Ollama returns one tool_call on the first /api/chat
    POST, then streams "final" on the second. Verify tool runs,
    rows persist, SSE events fire in order."""
    # Set up: ollama returns tool_call once, then streams.
    # Mock query_rag (or use current_time) to return a known string.
    # Drive the stream and assert:
    # - tool-call event present
    # - tool-result event present
    # - token events present after tool events
    # - done event present LAST
    # - messages table has tool_call and tool_result rows


def test_stream_caps_at_five_iterations(make_client, monkeypatch):
    """If Ollama keeps requesting tool_calls, the loop terminates
    after 5 with an assistant message about the cap."""
    # Mocked Ollama returns tool_call on every /api/chat POST.
    # After driving the stream, assert:
    # - 5 tool_call rows in the DB
    # - final assistant message text is "(Tool-call limit reached...)"
    # - done event present


def test_stream_skips_tools_for_non_tool_model(make_client, monkeypatch):
    """When the chat model doesn't support tools, _stream_assistant_reply
    doesn't pass tools=[] to Ollama (sends None instead)."""
    # Capture the outgoing /api/chat body; assert "tools" key absent.
```

---

## Sub-phase 12e — Tool UI cards

### Templates

`templates/_tool_call.html`:

```html
{# Rendered as the SSE `tool-call` event payload. OOB swap targets
   the streaming placeholder with beforebegin so the card lands
   just above the streaming bubble. The placeholder stays put. #}
<details id="tool-call-{{ now_ms }}" class="tool-card tool-card--call"
         hx-swap-oob="beforebegin:{{ swap_target }}">
  <summary class="tool-card__summary">
    <span class="material-symbols-outlined">arrow_forward</span>
    <span class="tool-card__label">Called {{ name }}</span>
    <span class="material-symbols-outlined tool-card__chevron">expand_more</span>
  </summary>
  <pre class="tool-card__body">{{ arguments | tojson(indent=2) }}</pre>
</details>
```

`templates/_tool_result.html`:

```html
<details id="tool-result-{{ now_ms }}" class="tool-card tool-card--result"
         hx-swap-oob="beforebegin:{{ swap_target }}">
  <summary class="tool-card__summary">
    <span class="material-symbols-outlined">arrow_back</span>
    <span class="tool-card__label">{{ name }} returned</span>
    <span class="material-symbols-outlined tool-card__chevron">expand_more</span>
  </summary>
  <pre class="tool-card__body">{{ result }}</pre>
</details>
```

(`now_ms` is a Jinja-passed timestamp from the route. The
implementing agent can use Python's `time.time_ns() // 1_000_000`
or a uuid4 hex prefix — anything unique per render.)

### CSS additions

```css
/* ===== Tool cards (phase 12e) ==========================================
   Collapsed by default; click <summary> to expand and see args /
   output. Positioned above the streaming assistant bubble during
   tool iterations, then persisted as part of the chat history.
*/

.tool-card {
  align-self: stretch;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  font-size: 13px;
  margin: var(--space-xs) 0;
}

.tool-card__summary {
  list-style: none;
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-md);
  cursor: pointer;
  color: var(--text-secondary);
  user-select: none;
}

.tool-card__summary::-webkit-details-marker { display: none; }

.tool-card__summary .material-symbols-outlined { font-size: 16px; }

.tool-card__label { flex: 1; font-weight: 500; color: var(--text-primary); }

.tool-card__chevron { transition: transform 0.15s ease; }

.tool-card[open] .tool-card__chevron { transform: rotate(180deg); }

.tool-card__body {
  margin: 0;
  padding: var(--space-md);
  background: var(--bg);
  border-top: 1px solid var(--border);
  border-bottom-left-radius: var(--radius-md);
  border-bottom-right-radius: var(--radius-md);
  font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 12px;
  white-space: pre-wrap;
  word-wrap: break-word;
  max-height: 320px;
  overflow-y: auto;
}

.tool-card--call .tool-card__summary { background: var(--accent-tonal); color: var(--accent-tonal-text); }
.tool-card--result .tool-card__summary { background: var(--surface); }
```

### Placeholder template update

`templates/_assistant_placeholder.html`:

```html
<div id="assistant-stream-{{ conversation_id }}"
     class="message message--assistant message--streaming"
     data-role="assistant"
     hx-ext="sse"
     sse-connect="{{ stream_url }}"
     sse-swap="token,done,error,title,tool-call,tool-result"
     hx-swap="beforeend"></div>
```

### Chat-panel rendering for persisted tool rows

`templates/_chat_panel.html` — the `{% for message in messages %}`
loop branches by role:

```jinja
{% for message in messages %}
  {% if message.role == "tool_call" %}
    {% set parsed = message.content | from_json %}
    {% include "_tool_call.html" with context %}
    {# Pass `name`, `arguments`, `now_ms` if the template expects them.
       The implementing agent may prefer to write a small Python helper
       that returns the renderable dict, called from the route, to keep
       the template simple. #}
  {% elif message.role == "tool_result" %}
    {% include "_tool_result.html" with context %}
  {% else %}
    {% include "_message.html" %}
  {% endif %}
{% endfor %}
```

(The `from_json` filter would need to be registered in
`app/routes.py` next to the existing `markdown` filter from the
markdown-rendering work. It's `json.loads`.)

---

## Sub-phase 12f — Model filtering by tool capability

### Helper: `app/ollama.py`

```python
async def list_tool_capable_models(client: httpx.AsyncClient) -> list[str]:
    """Return only the installed Ollama models that advertise tool support.

    Uses /api/show per model (POST {"model": name}) and filters to
    those with "tools" in their `capabilities` list. The capability
    list is the only Ollama-provided signal; we don't fall back to
    hardcoded model-name patterns.

    Caches per-process for 60 seconds — the dropdown refreshes
    when the user creates a new chat, so the cache pays for
    itself on the next composer render.
    """
    # Pseudocode here; implementer wires the actual cache (e.g.
    # functools.lru_cache on a sync helper, or a simple
    # {timestamp, names} module-level dict).
    all_models = await list_models(client)
    tool_capable: list[str] = []
    for name in all_models:
        try:
            resp = await client.post("/api/show", json={"model": name})
            resp.raise_for_status()
            caps = (resp.json().get("capabilities") or [])
            if "tools" in caps:
                tool_capable.append(name)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            # Skip models /api/show fails for; safer than including
            # them and erroring on tool_specs payload.
            continue
    return tool_capable


def model_supports_tools(model_name: str) -> bool:
    """Synchronous tool-support lookup against the cache.

    Used by routes.py inside _stream_assistant_reply to decide
    whether to pass tools= to Ollama. Falls back to True when the
    cache is empty (we'd rather try than skip).
    """
    # Implementer wires this against whatever cache shape
    # list_tool_capable_models uses.
    ...
```

### Route update: `/models`

`list_models_endpoint` calls `list_tool_capable_models` instead of
`list_models`:

```python
@router.get("/models", response_class=HTMLResponse)
async def list_models_endpoint(request: Request, client: OllamaClient) -> Response:
    try:
        names = await ollama.list_tool_capable_models(client)
    except OllamaUnavailable:
        return templates.TemplateResponse(
            request=request,
            name="_model_options.html",
            context={"models": [], "error": "Ollama is unreachable — start it and reload."},
        )
    except OllamaProtocolError:
        return templates.TemplateResponse(
            request=request,
            name="_model_options.html",
            context={"models": [], "error": "Ollama returned an unexpected response."},
        )
    return templates.TemplateResponse(
        request=request,
        name="_model_options.html",
        context={"models": names, "error": None},
    )
```

### Tests for 12f

```python
def test_models_endpoint_filters_tool_capable(make_client):
    """Mock /api/tags to return 3 models; mock /api/show so 2 have
    'tools' in capabilities. Assert only those 2 appear in
    <option> tags."""
    # Implementer wires the mock transport with two endpoints.
```

---

## Verification (phase 12 overall)

After all six sub-phases land:

1. `source .venv/bin/activate && pytest -q` — full suite passes. Target
   ~150 tests (was 122). Coverage doesn't regress meaningfully.
2. `uvicorn main:app --reload` and walk the full path in a real
   browser (not curl — see phase 11 retro):
   - Open `/`. Confirm the composer's model dropdown shows only
     models that support tools.
   - Click "Settings" in the sidebar footer. Add three RAG
     servers (e.g. arxiv / factbook / openalex pointing at your
     reference rags server's `/{source}` prefixes).
   - Start a new chat. Ask the model something that benefits from
     retrieval ("What does ArXiv say about transformer attention?").
   - Observe: a `tool-call` card appears above the streaming
     bubble showing `query_rag` with the model's chosen source
     and query; a `tool-result` card appears below it; the
     assistant then streams a final answer citing the retrieved
     chunks.
   - Reload `/chats/{id}`. Tool cards still render in their
     original positions.
   - Open Settings again and delete one of the servers. Send a
     new message asking for that source. Verify the model gets
     an "Unknown RAG source" string back and adjusts.
3. Edge cases worth manual exercise:
   - Force a 6th iteration by configuring a RAG server that
     always returns empty results — model may keep retrying.
     Verify the cap kicks in with the "(Tool-call limit reached.)"
     message.
   - Send a chat with a non-tool-capable model (if any exist on
     the user's machine — e.g. an embedding model isn't tool-
     capable). Verify it still works as plain chat with no
     tool calls.

## Risks / things-that-could-go-wrong

- **Two Ollama calls per turn** (one for tool-call detection, one
  for streaming the final response) doubles round-trip cost.
  Acceptable for local; revisit if perceived latency suffers.
- **Tool-card OOB targeting via `beforebegin:#assistant-stream-{id}`.**
  This depends on htmx-ext-sse correctly applying OOB swaps from
  the SSE data — phase 11d's `title` OOB swap proved this works
  for `hx-swap-oob="true"` (id-match); `beforebegin:<selector>`
  is a different code path. Verify in browser before treating
  the UI as done.
- **Tool calls inside the `done`-then-title-then-done flow.**
  Phase 11d's ordering (title before done) was tight. Phase 12d
  preserves it but now `_maybe_generate_title` runs AFTER an
  iteration loop. The title-gen call hits Ollama with the tool
  messages included (per our existing `_build_history_payload`
  including tool roles). Smaller models may produce weird
  titles. Acceptable; the user can rename.
- **The decorator's runtime description refresh.** Mutating
  `spec.parameters_schema` works only because it's a `dict` field
  in a frozen dataclass. Alternative cleaner approaches (lazy
  registration; mutable ToolSpec) might be worth picking instead
  — the implementer should choose and document.
- **`now_ms` IDs on tool cards.** Collisions are vanishingly
  unlikely but possible. Implementer should consider `uuid.uuid4().hex[:8]`
  if collision robustness matters.

## What's NOT in phase 12

- Write/exec tools and the confirmation UI for them (phase 15+).
- System prompts (phase 13).
- Generation parameters (phase 14).
- Settings UI for anything besides RAG servers.
- MCP protocol.
- Multi-agent (phase 16).

## Implementation order summary

Ship in this order, one commit per sub-phase, ask before each
commit (per the project working rules):

1. 12a — schema + role expansion
2. 12b — tool decorator + registry + current_time
3. 12c — RAG tool + server CRUD + settings UI
4. 12d — tool-calling loop in _stream_assistant_reply
5. 12e — tool UI cards + persisted-row rendering
6. 12f — model filtering by tool capability

After 12f, run the full browser verification (above) before
calling phase 12 done. Then write the phase 12 retro at
`docs/retros/phase12-tool-calling.md` and update phase 10's
wrap-up table to link it.
