"""The ``query_rag`` tool and its HTTP client.

Hits a configured RAG server's ``/chunks`` endpoint and returns retrieved
passages as a citation block the chat model can quote back to the user.

Valid ``source`` names (with descriptions) are discovered at runtime from
the ``rag_servers`` table; settings route handlers call
``refresh_query_rag_registration()`` after each CRUD write so the model
sees an up-to-date list next turn — no restart required. With zero servers
configured, ``query_rag`` is removed from the registry so the model can't
call a tool that cannot succeed.

Imported (via ``app.routes``) at startup so the ``@tool`` decorator
registers ``query_rag`` before any code reads the registry.
"""

from contextlib import closing

import httpx

from app import rag_servers as _rag_servers_module
from app.connection import open_connection
from app.rag_servers import RagServer
from app.tools import (
    RAG_TOOL_NAME,
    TOOL_HTTP_TIMEOUT,
    Source,
    ToolResult,
    tool,
    truncate_with_ellipsis,
)

# Caps that keep RAG output from blowing the model's context window: a
# pathological retrieval could return 5 multi-kilobyte chunks. Trimming
# costs some recall but protects the conversation from getting choked off.
_TOP_K = 5
_PER_CHUNK_TEXT_CAP = 800
_TOTAL_OUTPUT_CAP = 4000


def _list_sources() -> list[RagServer]:
    """Read the rag_servers table for the configured sources.

    Uses a private connection, not the app's shared one: the caller
    (``refresh_query_rag_registration()``) runs synchronously inside a
    route without the FastAPI request scope handy, and the work is one
    SELECT. ``closing`` wraps ``open_connection()`` because
    ``sqlite3.Connection.__exit__`` commits/rolls back but does NOT close,
    so the handle would otherwise leak until GC.

    Returns:
        RagServer rows in stable insertion order.
    """
    with closing(open_connection()) as conn:
        return _rag_servers_module.list_servers(conn)


def _format_chunks(items: list[dict], used_dense: bool) -> str:
    """Render the RAG response as a length-capped citation block.

    Each chunk becomes::

        [N] <title> (§<section>)
            <text>...

    ``None`` sections are omitted from the header. Per-chunk text is
    truncated to ``_PER_CHUNK_TEXT_CAP``; the joined string is hard-capped
    to ``_TOTAL_OUTPUT_CAP``. When ``used_dense`` is False (the RAG server
    fell back to keyword-only retrieval), prepend a note so the model knows
    recall may be degraded — worded to keep the model *using* the passages,
    since blunter phrasing made models refuse to quote valid sparse hits.

    Args:
        items: Raw ``items`` from the RAG server's JSON response. Entries
            should have ``title``/``section``/``text`` keys; missing keys
            degrade gracefully.
        used_dense: Whether retrieval used dense embeddings. False means
            sparse-only fallback.

    Returns:
        A formatted citation block, or ``"(no matching chunks)"`` when
        ``items`` is empty and there's no sparse-only note to show.
    """
    parts: list[str] = []
    if not used_dense:
        # Surface degraded retrieval so the model can hedge, but word it so
        # the model still USES the passages — blunter phrasing made models
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
        text = truncate_with_ellipsis(text, _PER_CHUNK_TEXT_CAP)
        parts.append(f"{header}\n    {text}\n")
    out = "\n".join(parts).strip()
    out = truncate_with_ellipsis(out, _TOTAL_OUTPUT_CAP)
    # Falls out empty when items=[] and used_dense=True (no note to show).
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
    # refresh_query_rag_registration() each chat turn.

    if not query.strip():
        # Reject an empty query here rather than send a guaranteed 400.
        return ToolResult(text="Tool query_rag: 'query' cannot be empty.")

    # Resolve source name → URL fresh each call so a newly-added server is
    # usable immediately. ``closing`` because sqlite3's context manager
    # commits/rolls back but doesn't close — see ``_list_sources``.
    with closing(open_connection()) as conn:
        servers = _rag_servers_module.list_servers(conn)
    by_name = {s.name: s for s in servers}
    server = by_name.get(source)
    if server is None:
        # Pass the configured names back so the model self-corrects on its
        # next call instead of guessing.
        names = ", ".join(by_name.keys()) or "(none configured)"
        return ToolResult(
            text=(
                f"Unknown RAG source '{source}'."
                f" Configured sources: {names}"
            )
        )

    # The stored URL is the source-prefixed base (e.g. ".../arxiv"); append
    # /chunks here so the row stays usable for other future endpoints.
    url = f"{server.url.rstrip('/')}/chunks"
    try:
        async with httpx.AsyncClient(timeout=TOOL_HTTP_TIMEOUT) as client:
            response = await client.get(
                url, params={"q": query, "top_k": _TOP_K}
            )
    except httpx.HTTPError:
        # Network-level failure (DNS, connect, timeout, read). Include the
        # source list so the model self-corrects rather than guessing.
        names = ", ".join(by_name.keys()) or "(none configured)"
        return ToolResult(
            text=(
                f"RAG source '{source}' unreachable."
                f" Configured sources: {names}"
            )
        )

    # 503 is the documented "indexes not built" signal; >=500 is
    # server-side trouble; >=400 is a client-side rejection (we validated
    # `query`, so a 400 here is a RAG-side contract bug, not ours).
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
        # Default True so an older server that omits this flag is treated
        # as full-recall.
        used_dense = bool(body.get("used_dense", True))
    except ValueError:
        # response.json() raises ValueError on a non-JSON body — surface it
        # without dumping the raw body into the chat.
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

    Used by ``refresh_query_rag_registration`` (global spec) and
    ``app.generation._run_generation`` (per-chat filtered spec) so the
    bullet format lives in one place.

    Args:
        servers: Servers to include; callers filter to the relevant subset
            first (all configured, or chat-enabled only).

    Returns:
        A multi-line string listing each server with its description.
    """
    lines = ["Name of the RAG source to query. Available sources:"]
    for s in servers:
        desc = s.description.strip() or "(no description)"
        lines.append(f"- {s.name}: {desc}")
    return "\n".join(lines)


# Snapshot the ToolSpec the @tool decorator built so we can re-register
# query_rag after a pop (see refresh_query_rag_registration).
# parameters_schema stays shared by design — refresh mutates it in place.
from app.tools import TOOLS as _TOOLS  # noqa: E402
_QUERY_RAG_SPEC = _TOOLS[RAG_TOOL_NAME]


def refresh_query_rag_registration() -> None:
    """Sync ``query_rag``'s TOOLS entry to the current rag_servers state.

    Removes the tool when no servers are configured, so the model isn't
    tempted to call something that can't succeed. Otherwise re-adds it and
    folds each server's description into the ``source`` parameter hint so
    the model can pick intelligently.

    Called by the settings route handlers after CRUD and by the lifespan
    startup hook, keeping the registry in sync without a restart.
    """
    # Re-import locally (despite module-level _TOOLS) so tests that patch
    # app.tools.TOOLS see the right object.
    from app.tools import TOOLS

    servers = _list_sources()
    if not servers:
        # No sources → drop the tool so the model can't invoke it.
        TOOLS.pop(RAG_TOOL_NAME, None)
        return

    # Re-add after a prior pop (or on first call); the spec is the same
    # one @tool built, so name/description/func stay intact.
    if RAG_TOOL_NAME not in TOOLS:
        TOOLS[RAG_TOOL_NAME] = _QUERY_RAG_SPEC

    spec = TOOLS[RAG_TOOL_NAME]
    spec.parameters_schema["properties"]["source"]["description"] = (
        build_source_description(servers)
    )
