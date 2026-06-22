"""Phase 12c: the ``query_rag`` tool and its HTTP client.

Hits a configured RAG server's ``/chunks`` endpoint and returns retrieved
passages formatted as a readable citation block the chat model can quote
back at the user.

The list of valid ``source`` names — with per-source descriptions — is
discovered at runtime from the ``rag_servers`` table (see
``app.rag_servers``); the settings route handlers call
``refresh_query_rag_registration()`` after each CRUD write so the model
sees an up-to-date list on the next chat turn — no restart required.
When zero servers are configured, ``query_rag`` is removed from the
registry entirely so the model is never tempted to call a tool that
cannot possibly succeed.

This module is imported (via ``app.routes``) at app startup so the
``@tool`` decorator registers ``query_rag`` in ``app.tools.TOOLS`` before
any code reads from the registry.
"""

from contextlib import closing

import httpx

from app import rag_servers as _rag_servers_module
from app.connection import open_connection
from app.rag_servers import RagServer
from app.tools import RAG_TOOL_NAME, Source, ToolResult, tool

# ---------------------------------------------------------------------------
# Hardcoded caps — keep RAG output from blowing the model's context window.
# A pathological retrieval could otherwise return 5 chunks each containing
# tens of kilobytes of text; trimming here costs the model some recall but
# protects the whole conversation from getting choked off.
# ---------------------------------------------------------------------------
_TOP_K = 5
_PER_CHUNK_TEXT_CAP = 800
_TOTAL_OUTPUT_CAP = 4000

# Retrieval should be fast (sparse FTS5 + dense ANN over local SQLite on
# the RAG server side). 30s total / 5s connect leaves headroom for slow
# private-network routes between the chat app and the RAG box, while still
# failing fast on a truly down server.
_RAG_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


def _list_sources() -> list[RagServer]:
    """Walk the rag_servers table for the current set of configured sources.

    Opens a private connection rather than reusing the app's shared one
    because this helper is called from
    ``refresh_query_rag_registration()``, which runs synchronously
    inside a route handler — it doesn't have the FastAPI request scope
    handy, and the work is small (one SELECT). ``contextlib.closing``
    wraps ``open_connection()`` because ``sqlite3.Connection.__exit__``
    only commits/rolls back — it does NOT close. Without ``closing``
    the handle would leak until GC.

    Returns:
        RagServer rows in stable insertion order.
    """
    with closing(open_connection()) as conn:
        return _rag_servers_module.list_servers(conn)


def _format_chunks(items: list[dict], used_dense: bool) -> str:
    """Render the RAG response as a readable, length-capped citation block.

    Each chunk becomes::

        [N] <title> (§<section>)
            <text>...

    Sections that are ``None`` are omitted from the header. Per-chunk
    text is truncated to ``_PER_CHUNK_TEXT_CAP`` characters with an
    ellipsis; the final concatenated string is hard-capped to
    ``_TOTAL_OUTPUT_CAP``. When ``used_dense`` is False, prepend a note
    so the model knows recall may be degraded (the RAG server fell back
    to keyword-only retrieval because its embedding service is down).
    The note is worded to keep the model *using* the passages below it —
    earlier phrasing ("embedding service unreachable") read like a hard
    failure and made the model refuse to quote perfectly valid sparse
    hits, claiming "service limitations" (see Phase 19 follow-up).

    Args:
        items: Raw ``items`` list from the RAG server's JSON response.
            Each entry is expected to have ``title``, ``section``, and
            ``text`` keys; missing keys degrade gracefully.
        used_dense: Whether the retrieval used dense embeddings. False
            means sparse-only fallback was used.

    Returns:
        A formatted citation block, or ``"(no matching chunks)"`` if
        ``items`` is empty and there's no sparse-only note to show.
    """
    parts: list[str] = []
    if not used_dense:
        # Surface degraded-retrieval state so the model can hedge its
        # answer — but word it so the model still USES the passages below.
        # The blunt "embedding service unreachable" wording made models
        # treat valid sparse hits as a failure and refuse to quote them.
        parts.append(
            "(Note: semantic ranking is temporarily unavailable, so these"
            " are keyword-search results. The passages below are real and"
            " complete — use and quote them normally; only recall may be"
            " lower than usual.)\n"
        )
    for idx, item in enumerate(items, 1):
        title = item.get("title") or "(untitled)"
        section = item.get("section")
        header = f"[{idx}] {title}"
        if section:
            header += f" (§{section})"
        text = (item.get("text") or "").strip()
        if len(text) > _PER_CHUNK_TEXT_CAP:
            # Reserve 3 chars for the ellipsis so the visible length
            # stays at _PER_CHUNK_TEXT_CAP exactly.
            text = text[: _PER_CHUNK_TEXT_CAP - 3] + "..."
        parts.append(f"{header}\n    {text}\n")
    out = "\n".join(parts).strip()
    if len(out) > _TOTAL_OUTPUT_CAP:
        out = out[: _TOTAL_OUTPUT_CAP - 3] + "..."
    # "(no matching chunks)" is what falls out when items=[] AND
    # used_dense=True (no sparse-only note to fill the void).
    return out or "(no matching chunks)"


@tool
async def query_rag(source: str, query: str) -> ToolResult:
    """Search the user's configured knowledge bases for passages relevant to a question, returning short excerpts you can quote to ground your answer. Only call when the user's question is likely covered by one of the sources listed under the source argument — never speculatively.

    Args:
        source: Name of the knowledge base to search. Valid values
            are listed at runtime from the user's settings; pick the
            one whose description best matches the question.
        query: Natural-language search query.
    """
    # The Args:source description above is the static fallback; the live
    # source list (with descriptions) is injected by
    # refresh_query_rag_registration() so the model sees an up-to-date
    # "Available sources: ..." list on every chat turn.

    if not query.strip():
        # Defensive: an empty query is rejected here rather than sent on
        # to the RAG server, where it'd produce a 400 anyway.
        return ToolResult(text="Tool query_rag: 'query' cannot be empty.")

    # Look up the source name → URL mapping fresh on each call so a
    # newly-added server is usable immediately (no caching to stale).
    # ``closing`` because sqlite3.Connection's context manager only
    # commits/rolls back — not closes. See ``_list_sources``.
    with closing(open_connection()) as conn:
        servers = _rag_servers_module.list_servers(conn)
    by_name = {s.name: s for s in servers}
    server = by_name.get(source)
    if server is None:
        # Pass the configured names back so the model can self-correct
        # on the next tool call instead of guessing blindly.
        names = ", ".join(by_name.keys()) or "(none configured)"
        return ToolResult(
            text=(
                f"Unknown RAG source '{source}'."
                f" Configured sources: {names}"
            )
        )

    # The stored URL is the source-prefixed base (e.g.
    # ".../arxiv"); we tack on /chunks ourselves so the rag_servers
    # row stays usable for other endpoints a future tool might add.
    url = f"{server.url.rstrip('/')}/chunks"
    try:
        async with httpx.AsyncClient(timeout=_RAG_TIMEOUT) as client:
            response = await client.get(
                url, params={"q": query, "top_k": _TOP_K}
            )
    except httpx.HTTPError:
        # Network-level failure (DNS, connect, timeout, read error).
        # Include the configured source list so the model can self-correct
        # on its next tool call rather than guessing a source name.
        names = ", ".join(by_name.keys()) or "(none configured)"
        return ToolResult(
            text=(
                f"RAG source '{source}' unreachable."
                f" Configured sources: {names}"
            )
        )

    # Status code branches: 503 is the documented "indexes not built"
    # signal; >=500 is server-side trouble; >=400 covers any client-side
    # rejection (we already validated `query` non-empty, so 400 here
    # would be a contract bug on the RAG side rather than ours).
    if response.status_code == 503:
        return ToolResult(
            text=(
                f"RAG source '{source}' unavailable"
                f" (server reports indexes not built)."
            )
        )
    if response.status_code >= 500:
        return ToolResult(
            text=f"RAG source '{source}' failed (HTTP {response.status_code})."
        )
    if response.status_code >= 400:
        return ToolResult(
            text=(
                f"RAG source '{source}' rejected the query"
                f" (HTTP {response.status_code})."
            )
        )

    try:
        body = response.json()
        items = body.get("items") or []
        # `used_dense` defaults to True so a server that doesn't
        # surface this flag (older RAG impls) is treated as full-recall.
        used_dense = bool(body.get("used_dense", True))
    except ValueError:
        # response.json() raises ValueError on a non-JSON body —
        # surface it without dumping the raw body into the chat.
        return ToolResult(
            text=f"RAG source '{source}' returned non-JSON response."
        )

    sources = [
        Source(
            title=item.get("title") or "(untitled)",
            section=item.get("section"),
        )
        for item in items
    ]
    return ToolResult(
        text=_format_chunks(items, used_dense),
        sources=sources,
    )


def build_source_description(servers: list[RagServer]) -> str:
    """Build the ``source`` parameter description for the query_rag tool spec.

    Used by ``refresh_query_rag_registration`` (global spec) and by
    ``app.generation._run_generation`` (per-chat filtered spec) so the
    bullet format stays in one place.

    Args:
        servers: The servers to include. Callers filter to the relevant
            subset before calling (all configured, or chat-enabled only).

    Returns:
        A multi-line string listing each server with its description.
    """
    lines = ["Name of the RAG source to query. Available sources:"]
    for s in servers:
        desc = s.description.strip() or "(no description)"
        lines.append(f"- {s.name}: {desc}")
    return "\n".join(lines)


# Snapshot the ToolSpec the @tool decorator built above so we can
# re-register query_rag after a pop (see refresh_query_rag_registration).
# parameters_schema stays shared by design — the refresh function mutates
# it in place to reflect the current source list.
from app.tools import TOOLS as _TOOLS  # noqa: E402
_QUERY_RAG_SPEC = _TOOLS[RAG_TOOL_NAME]


def refresh_query_rag_registration() -> None:
    """Sync ``query_rag``'s TOOLS entry to the current rag_servers state.

    Removes the tool entirely when no servers are configured, so the
    chat model isn't tempted to call a tool that can't possibly succeed.
    Re-adds and re-describes it when at least one server exists, folding
    each server's description into the ``source`` parameter hint so the
    model can pick intelligently.

    Called by the settings route handlers after CRUD operations and by
    the lifespan startup hook so the registry stays in sync without
    requiring a restart.
    """
    # Imported lazily at module level above (_TOOLS), but we re-import
    # locally so tests that patch app.tools.TOOLS see the right object.
    from app.tools import TOOLS

    servers = _list_sources()
    if not servers:
        # No sources configured → remove the tool so the model never
        # sees a tool it cannot successfully invoke.
        TOOLS.pop(RAG_TOOL_NAME, None)
        return

    # Re-add after a prior pop (or on first call). The spec object is
    # the same one the @tool decorator built — name/description/func stay intact.
    if RAG_TOOL_NAME not in TOOLS:
        TOOLS[RAG_TOOL_NAME] = _QUERY_RAG_SPEC

    spec = TOOLS[RAG_TOOL_NAME]
    spec.parameters_schema["properties"]["source"]["description"] = (
        build_source_description(servers)
    )
