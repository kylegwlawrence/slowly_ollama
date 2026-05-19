"""Phase 12c: the ``query_rag`` tool and its HTTP client.

Hits a configured RAG server's ``/chunks`` endpoint and returns retrieved
passages formatted as a readable citation block the chat model can quote
back at the user.

The list of valid ``source`` names is discovered at runtime from the
``rag_servers`` table (see ``app.rag_servers``); the settings route
handlers call ``refresh_query_rag_source_description()`` after each CRUD
write so the model sees an up-to-date list on the next chat turn — no
restart required.

This module is imported (via ``app.routes``) at app startup so the
``@tool`` decorator registers ``query_rag`` in ``app.tools.TOOLS`` before
any code reads from the registry.
"""

import httpx

from app import rag_servers as _rag_servers_module
from app.connection import open_connection
from app.tools import tool

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
# the RAG server side). 15s total / 5s connect leaves headroom for slow
# Tailscale routes between the chat app and the RAG box, while still
# failing fast on a truly down server.
_RAG_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def _list_source_names() -> list[str]:
    """Walk the rag_servers table for the current set of source names.

    Opens a private connection rather than reusing the app's shared one
    because this helper is called from
    ``refresh_query_rag_source_description()``, which runs synchronously
    inside a route handler — it doesn't have the FastAPI request scope
    handy, and the work is small (one SELECT). The connection is closed
    by the ``with`` block on exit.

    Returns:
        Source names in stable insertion order.
    """
    with open_connection() as conn:
        return [s.name for s in _rag_servers_module.list_servers(conn)]


def _format_chunks(items: list[dict], used_dense: bool) -> str:
    """Render the RAG response as a readable, length-capped citation block.

    Each chunk becomes::

        [N] <title> (§<section>)
            <text>...

    Sections that are ``None`` are omitted from the header. Per-chunk
    text is truncated to ``_PER_CHUNK_TEXT_CAP`` characters with an
    ellipsis; the final concatenated string is hard-capped to
    ``_TOTAL_OUTPUT_CAP``. When ``used_dense`` is False, prepend a note
    so the model knows retrieval recall is degraded (the RAG server is
    falling back to sparse-only because its embedding service is down).

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
        # Surface degraded-retrieval state to the model so it can hedge
        # its answer rather than confidently quoting a thin set of hits.
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
async def query_rag(source: str, query: str) -> str:
    """Retrieve passages from a configured RAG source.

    Args:
        source: Name of the configured RAG server to query. Valid
            values are discovered at runtime from the user's settings
            (see /settings).
        query: Natural-language query string.
    """
    # The Args:source description above is the static fallback; the
    # live source list is injected by refresh_query_rag_source_description()
    # so the model sees an up-to-date "Valid values are: ..." list.

    if not query.strip():
        # Defensive: an empty query is rejected here rather than sent on
        # to the RAG server, where it'd produce a 400 anyway. Returning
        # a plain string keeps the run_tool contract (never raise).
        return "Tool query_rag: 'query' cannot be empty."

    # Look up the source name → URL mapping fresh on each call so a
    # newly-added server is usable immediately (no caching to stale).
    with open_connection() as conn:
        servers = _rag_servers_module.list_servers(conn)
    by_name = {s.name: s for s in servers}
    server = by_name.get(source)
    if server is None:
        # Pass the configured names back so the model can self-correct
        # on the next tool call instead of guessing blindly.
        names = ", ".join(by_name.keys()) or "(none configured)"
        return (
            f"Unknown RAG source '{source}'."
            f" Configured sources: {names}"
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
        return (
            f"RAG source '{source}' unreachable."
            f" Configured sources: {names}"
        )

    # Status code branches: 503 is the documented "indexes not built"
    # signal; >=500 is server-side trouble; >=400 covers any client-side
    # rejection (we already validated `query` non-empty, so 400 here
    # would be a contract bug on the RAG side rather than ours).
    if response.status_code == 503:
        return (
            f"RAG source '{source}' unavailable"
            f" (server reports indexes not built)."
        )
    if response.status_code >= 500:
        return f"RAG source '{source}' failed (HTTP {response.status_code})."
    if response.status_code >= 400:
        return (
            f"RAG source '{source}' rejected the query"
            f" (HTTP {response.status_code})."
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
        return f"RAG source '{source}' returned non-JSON response."

    return _format_chunks(items, used_dense)


def refresh_query_rag_source_description() -> None:
    """Re-inject the current source list into ``query_rag``'s schema.

    Called by the settings route handlers after CRUD operations so the
    next ``tool_specs_for_ollama()`` call reflects the updated list. We
    can't update the docstring after-the-fact — the description is
    cached in the ToolSpec at registration time — so we mutate the
    schema dict in place. ``ToolSpec`` itself is ``frozen=True`` but
    ``parameters_schema`` is a plain ``dict`` field, so this mutation
    works and the cached spec picks up the new value on next read.

    No-op if the tool isn't registered yet (e.g. unit tests that
    import ``app.rag_servers`` without importing the tool module).
    """
    # Imported lazily so this module can be imported (for unit tests
    # that just want _format_chunks etc.) without forcing the registry
    # to exist already. The decorator above already registered the
    # tool when this module was imported normally; this lookup just
    # finds it.
    from app.tools import TOOLS

    spec = TOOLS.get("query_rag")
    if spec is None:
        return
    names = _list_source_names()
    sources_hint = (
        "Name of the configured RAG server to query."
        f" Valid values are: {', '.join(names) if names else '(none configured)'}"
    )
    spec.parameters_schema["properties"]["source"]["description"] = sources_hint
