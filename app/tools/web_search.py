"""The ``web_search`` tool: query a self-hosted SearXNG instance.

Hits SearXNG's ``/search?format=json`` endpoint and returns ranked web
results as a citation block the chat model can read and quote. The app only
ever talks to the locally-run SearXNG, which does the actual talking-to-the-
internet — keeping the app itself free of direct cloud calls.

Gated on ``SEARXNG_URL``: with it unset the tool is removed from the registry
(see :func:`refresh_web_search_registration`) so the model can't call a tool
that cannot succeed. Mirrors the file-tools and ``query_rag`` gating.

Imported (via ``app.routes``) at startup so the ``@tool`` decorator registers
``web_search`` before any code reads the registry.
"""

import httpx

from app.config import searxng_url
from app.tools import (
    TOOL_HTTP_TIMEOUT,
    Source,
    ToolResult,
    tool,
    truncate_with_ellipsis,
)

# Keep output from blowing the context window. SearXNG can return dozens of
# results; the top handful is what a model needs to pick a direction.
_TOP_K = 6
_PER_RESULT_SNIPPET_CAP = 1000
_TOTAL_OUTPUT_CAP = 8000


def _format_results(items: list[dict]) -> str:
    """Render SearXNG results as a length-capped citation block.

    Each result becomes::

        [N] <title>
            <url>
            <snippet>...

    Per-snippet text is truncated to ``_PER_RESULT_SNIPPET_CAP``; the joined
    string is hard-capped to ``_TOTAL_OUTPUT_CAP``. A leading note tells the
    model these are snippets, not full pages, so it doesn't over-claim.

    Args:
        items: The ``results`` list from SearXNG's JSON response. Entries
            should have ``title``/``url``/``content`` keys; missing keys
            degrade gracefully.

    Returns:
        A formatted block, or ``"(no results)"`` when ``items`` is empty.
    """
    if not items:
        return "(no results)"
    # PREPEND the framing note (don't append): the total cap truncates the
    # TAIL, so an appended note gets chopped off exactly when output is long.
    # Mirrors how rag._format_chunks prepends its degraded-retrieval note.
    parts: list[str] = [
        "(These are search-result snippets, not full pages. Quote them only"
        " as far as they go; say so if the answer needs the full source.)\n"
    ]
    for idx, item in enumerate(items, 1):
        title = (item.get("title") or "(untitled)").strip()
        url = item.get("url") or ""
        snippet = (item.get("content") or "").strip()
        snippet = truncate_with_ellipsis(snippet, _PER_RESULT_SNIPPET_CAP)
        parts.append(f"[{idx}] {title}\n    {url}\n    {snippet}\n")
    out = "\n".join(parts).strip()
    return truncate_with_ellipsis(out, _TOTAL_OUTPUT_CAP)


@tool
async def web_search(query: str) -> ToolResult:
    """Search the public web for current information. Use for recent events, or facts not in the conversation, the project, or the configured knowledge bases. Returns ranked result snippets with URLs.

    Args:
        query: Natural-language web search query.
    """
    if not query.strip():
        return ToolResult(text="Tool web_search: 'query' cannot be empty.")

    base = searxng_url()
    if base is None:
        # Defensive: the tool is normally dropped from the registry when
        # SEARXNG_URL is unset, but a direct call should still explain itself.
        return ToolResult(
            text="Web search is not configured (SEARXNG_URL is unset)."
        )

    url = f"{base.rstrip('/')}/search"
    try:
        async with httpx.AsyncClient(timeout=TOOL_HTTP_TIMEOUT) as client:
            response = await client.get(
                url, params={"q": query, "format": "json"}
            )
    except httpx.HTTPError:
        return ToolResult(
            text="Web search unreachable (SearXNG not responding)."
        )

    if response.status_code == 403:
        # Most common misconfig: JSON format not enabled, or the bot limiter
        # is blocking server-side requests. Point at the fix, not a retry.
        return ToolResult(
            text=(
                "Web search rejected (HTTP 403). Enable 'json' in SearXNG's"
                " search.formats and relax server.limiter for the app host."
            )
        )
    if response.status_code >= 400:
        return ToolResult(
            text=f"Web search failed (HTTP {response.status_code})."
        )

    try:
        items = response.json().get("results") or []
    except ValueError:
        # A non-JSON body almost always means format=json isn't enabled.
        return ToolResult(
            text=(
                "Web search returned non-JSON (enable 'json' in SearXNG's"
                " search.formats)."
            )
        )

    items = items[:_TOP_K]
    sources = [
        Source(
            title=item.get("title") or item.get("url") or "(untitled)",
            section=None,
        )
        for item in items
    ]
    return ToolResult(text=_format_results(items), sources=sources)


# Snapshot the spec the @tool decorator built so we can re-add after a pop.
from app.tools import TOOLS as _TOOLS  # noqa: E402

_WEB_SEARCH_SPEC = _TOOLS["web_search"]


def refresh_web_search_registration() -> None:
    """Sync ``web_search``'s TOOLS entry to whether SEARXNG_URL is set.

    Removes the tool when SearXNG isn't configured so the model is never
    offered a tool that cannot succeed; re-adds it otherwise. Mirrors
    :func:`app.tools.rag.refresh_query_rag_registration` and
    :func:`app.tools.builtins.refresh_file_tools_registration`. Called at
    lifespan startup.
    """
    # Re-import locally (despite module-level _TOOLS) so tests that patch
    # app.tools.TOOLS see the right object.
    from app.tools import TOOLS

    if searxng_url() is None:
        TOOLS.pop("web_search", None)
        return
    if "web_search" not in TOOLS:
        TOOLS["web_search"] = _WEB_SEARCH_SPEC
