"""Phase 7: HTTP routes that return HTML fragments for HTMX.

Every endpoint here returns either an HTML fragment (for HTMX swaps) or
a Server-Sent Events stream of HTML fragments (for the streaming chat
endpoints). The query layer (``app.queries``) and Ollama client
(``app.ollama``) are unchanged from earlier phases — this module just
swaps their results into Jinja2 templates instead of JSON.

Path layout:

  GET    /                             — full index page (sidebar + empty
                                         main panel)
  GET    /models                       — option tags for the model dropdown
  GET    /chats                        — sidebar list
  POST   /chats                        — create + return one row
  GET    /chats/{id}                   — chat panel: full index page on
                                         direct hit (browser nav / reload),
                                         just the panel fragment when HTMX
                                         requests it (HX-Request header)
  GET    /chats/{id}/edit              — sidebar row in edit mode (form)
  GET    /chats/{id}/item              — sidebar row in display mode
                                         (used by rename's Cancel button)
  PATCH  /chats/{id}                   — rename + return one row
  DELETE /chats/{id}                   — delete + return empty 200
  POST   /chats/{id}/messages          — save user msg, return user bubble
                                         + assistant SSE placeholder
  GET    /chats/{id}/stream            — SSE: tokens of the new reply
  POST   /chats/{id}/regenerate        — return assistant SSE placeholder
                                         that replaces the last assistant
                                         bubble
  GET    /chats/{id}/regenerate-stream — SSE: tokens of the regenerated
                                         reply

HTTP error mapping is the same as Phase 6:
  ``OllamaUnavailable`` → 503
  ``OllamaProtocolError`` → 502
  ``LookupError`` (unknown id) → 404
Mid-stream failures emit an SSE ``event: error`` (headers already sent).
"""

import html
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

import markdown as _md
from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app import ollama, queries
from app import rag_servers as _rag_servers_module
from app.dependencies import DB, OllamaClient
from app.ollama import OllamaProtocolError, OllamaUnavailable

# Phase 12d: importing app.tools.builtins has the side effect of
# registering the `current_time` tool via its @tool decorator. Without
# this import the production app would only ever see `query_rag`
# (registered transitively via the `rag` import below) — tests in
# tests/test_tools.py import builtins themselves, which masked the gap
# until the 12d agent flagged it. noqa because the import is purely a
# side effect; we never reference the module by name here.
from app.tools import builtins as _builtins  # noqa: F401

# Phase 12c: importing app.tools.rag has the side effect of registering
# the `query_rag` tool via its @tool decorator. Aliased to `_rag_tool`
# and silenced with noqa so the import isn't flagged as unused —
# `refresh_query_rag_source_description` is the only name we call
# directly from this module, but the side-effecting import must land
# regardless so `TOOLS["query_rag"]` exists by the time anything
# downstream (settings handlers, the streaming loop in 12d) reads it.
from app.tools import rag as _rag_tool  # noqa: F401
from app.tools import run_tool, tool_specs_for_ollama
from app.tools.rag import refresh_query_rag_source_description

# Templates live at the project root. Resolving relative to this file's
# location keeps the directory lookup correct regardless of where
# `uvicorn` is launched from.
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=_TEMPLATE_DIR)

# fenced_code: ```lang ... ``` blocks; tables: GFM-style tables.
_md_converter = _md.Markdown(extensions=["fenced_code", "tables"])

# Matches any line that starts a list item (ordered or unordered).
_LIST_ITEM_RE = re.compile(r"^[ \t]*(\d+[.)]\s+|[-*+]\s+)")


def _ensure_list_spacing(text: str) -> str:
    """Insert a blank line before list items that directly follow non-list text.

    LLMs often omit the blank line that standard Markdown requires before a
    list when it comes after paragraph text (e.g. "Steps:\n1. First").
    Without the blank line the markdown library renders everything as a single
    paragraph.  This pass inserts the missing blank line so the list is
    recognised correctly.
    """
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        if _LIST_ITEM_RE.match(line) and out and out[-1].strip() and not _LIST_ITEM_RE.match(out[-1]):
            out.append("")
        out.append(line)
    return "\n".join(out)


def _render_markdown(text: str) -> str:
    """Convert markdown text to an HTML string.

    Resets internal state between calls because the Markdown instance is
    reused across requests for efficiency.
    """
    _md_converter.reset()
    return _md_converter.convert(_ensure_list_spacing(text))


templates.env.filters["markdown"] = _render_markdown

router = APIRouter()


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def index_endpoint(request: Request, db: DB) -> Response:
    """Render the full layout — sidebar list + empty-state composer.

    Direct hits to ``/`` (the user opens the app) land here. The
    sidebar is populated from the DB; the main panel shows the
    centered composer (greeting + textarea + model dropdown) until
    the user clicks a chat or sends a first message.
    """
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "chats": queries.list_conversations(db),
            "conversation": None,
            "messages": [],
            # No chat is selected on the empty index — pass None so the
            # sidebar template's `aria-current` check is always defined.
            "active_chat_id": None,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_chat_endpoint(request: Request) -> Response:
    """Return just the empty-state composer fragment.

    Wired to the sidebar "+ New chat" link, which `hx-get`s this URL
    and swaps the response into ``#main``. The fragment-only response
    keeps the swap cheap and avoids re-rendering the sidebar (which
    would briefly lose the current active-row highlight before the
    push-url updates).
    """
    return templates.TemplateResponse(
        request=request,
        name="_composer.html",
        context={},
    )


# ---------------------------------------------------------------------------
# Settings (RAG servers — phase 12c)
# ---------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
def settings_endpoint(request: Request, db: DB) -> Response:
    """Standalone settings page — RAG servers in phase 12c.

    Direct browser hits return the full index shell with the settings
    fragment preloaded in the main slot (so reload / bookmarks land on
    the same view). HTMX requests get just the fragment, sized for a
    cheap swap into ``#main``. Mirrors the branching pattern in
    ``get_chat_panel_endpoint``.
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
            # Passed under `rag_servers` so the index template's
            # `{% set servers = rag_servers %}` adapter resolves it for
            # the included _settings.html fragment.
            "rag_servers": servers,
        },
    )


@router.post("/settings/servers", response_class=HTMLResponse)
def add_server_endpoint(
    request: Request,
    db: DB,
    name: Annotated[str, Form()],
    url: Annotated[str, Form()],
) -> Response:
    """Add a RAG server; return the new row for ``hx-swap="beforeend"``.

    A UNIQUE-constraint collision on the server name comes back from
    SQLite as ``IntegrityError`` — we map it to a 409 with a short
    plain-text body. HTMX's default behaviour is to NOT swap a non-2xx
    response, so the existing list stays intact and the form keeps the
    user's typed values (its `after-request` reset is guarded on
    ``event.detail.successful``).

    On success we call ``refresh_query_rag_source_description`` so the
    next chat turn's tool spec reflects the newly-added source name.
    """
    try:
        server = _rag_servers_module.create_server(
            db, name=name.strip(), url=url.strip()
        )
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


@router.delete(
    "/settings/servers/{server_id}",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
def delete_server_endpoint(server_id: int, db: DB) -> Response:
    """Delete a RAG server; return empty 200 for ``hx-swap="delete"``.

    Mirrors ``delete_chat_endpoint``'s shape: idempotent at the query
    layer (missing ids are silently accepted), empty body so HTMX just
    removes the row. The list-description refresh keeps the tool's
    schema in sync with the (now-shrunk) set of source names.
    """
    _rag_servers_module.delete_server(db, server_id)
    refresh_query_rag_source_description()
    return Response(content="", status_code=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _placeholder_name(content: str) -> str:
    """Derive a sidebar-friendly placeholder name from a user message.

    Used by ``create_chat_endpoint`` to give every new chat an
    immediately-identifiable sidebar entry from the moment it's
    created, instead of a generic "New chat" everyone has. The
    phase 11d auto-titler may replace it later with a cleaner
    model-generated summary.

    Args:
        content: The user's first message. Multi-line content is
            collapsed to the first non-empty line; whitespace
            is trimmed. The 40-char cap fits a 280px-wide sidebar
            without truncation ellipses kicking in.

    Returns:
        Up to 40 chars of the first non-empty line. Falls back to
        ``"New chat"`` if ``content`` is empty or whitespace-only
        (POST /chats requires content via Form() so this is
        mostly a defensive fallback).
    """
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:40]
    return "New chat"


def _sse(payload: str, event: str | None = None) -> str:
    """Format an HTML payload as a single SSE message.

    Each line of ``payload`` becomes its own ``data:`` line — that's
    the SSE spec's rule for multi-line content (e.g. an HTML fragment
    that contains embedded newlines). The browser reassembles them
    with ``\\n`` separators before delivering to the listener.

    Args:
        payload: HTML string (or empty) to put in the data field.
        event: Optional named event. Default events fire the generic
            handler; named events (``token``, ``done``, ``error``)
            let HTMX's ``sse-swap`` dispatch them to specific targets.

    Returns:
        A complete SSE message ending in the event terminator
        ``\\n\\n``.
    """
    prefix = f"event: {event}\n" if event else ""
    # Split-and-rejoin handles newlines inside the HTML fragment.
    # Empty payload still needs a `data:` line to be a valid event.
    lines = payload.split("\n") if payload else [""]
    data_lines = "".join(f"data: {line}\n" for line in lines)
    return f"{prefix}{data_lines}\n"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@router.get("/models", response_class=HTMLResponse)
async def list_models_endpoint(
    request: Request, client: OllamaClient
) -> Response:
    """Return ``<option>`` tags for the model dropdown.

    On Ollama failure this returns 200 with a single disabled
    ``<option>`` carrying an explanatory message. The reason for not
    returning 5xx: HTMX won't swap the dropdown's contents on a
    non-2xx response by default, which would leave the placeholder
    stuck at "Loading models…" with no indication that anything's
    wrong. A 200 with a disabled option still blocks submission
    (empty value + the form's `required` attribute) while giving the
    user a clear message to act on.
    """
    try:
        models = await ollama.list_models(client)
    except OllamaUnavailable:
        return templates.TemplateResponse(
            request=request,
            name="_model_options.html",
            context={
                "models": [],
                "error": "Ollama is unreachable — start it and reload.",
            },
        )
    except OllamaProtocolError:
        return templates.TemplateResponse(
            request=request,
            name="_model_options.html",
            context={
                "models": [],
                "error": "Ollama returned an unexpected response.",
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="_model_options.html",
        context={"models": models, "error": None},
    )


# ---------------------------------------------------------------------------
# Conversations / sidebar
# ---------------------------------------------------------------------------


@router.get("/chats", response_class=HTMLResponse)
def list_chats_endpoint(request: Request, db: DB) -> Response:
    """Render the sidebar list of conversations."""
    return templates.TemplateResponse(
        request=request,
        name="_chats_list.html",
        # `active_chat_id` is None here — GET /chats refreshes the
        # sidebar standalone (no conversation context). The page that
        # owns the URL is responsible for the active highlight.
        context={
            "chats": queries.list_conversations(db),
            "active_chat_id": None,
        },
    )


@router.post(
    "/chats",
    response_class=HTMLResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_chat_endpoint(
    request: Request,
    db: DB,
    model: Annotated[str, Form()],
    content: Annotated[str, Form()],
) -> Response:
    """Create a conversation AND save the first message in one request.

    The empty-state composer is the only caller — it posts ``model``
    and the user's first ``content``. The response is the rendered
    chat panel (with the user's message + an assistant streaming
    placeholder waiting inside ``#messages``), targeted at ``#main``
    by the composer form. A second fragment carries the new sidebar
    row OOB-prepended into ``#chats-list``. ``HX-Push-Url`` syncs the
    address bar to the new chat's URL.

    Why both in one round-trip: the composer's job is to *start a
    conversation*, not just to create an empty shell. Splitting this
    into "POST /chats → empty panel → manually POST first message"
    would double-render the panel and add a perceived delay before
    the streaming placeholder appears.

    The placeholder name is derived from the user's first message
    (first non-empty line, truncated to 40 chars). It's good enough
    to identify the chat in the sidebar from the moment it's created;
    phase 11d's auto-titler may overwrite it with a model-generated
    title after the first assistant response completes.
    """
    chat = queries.create_conversation(
        db, name=_placeholder_name(content), model=model
    )
    queries.append_message(db, chat.id, "user", content)
    messages = queries.list_messages(db, chat.id)

    # Panel includes the just-saved user bubble AND an inline assistant
    # placeholder that opens the SSE stream on insert. Inlining the
    # placeholder (via `pending_stream_url`) avoids an OOB-vs-main
    # swap-ordering race against `#messages`, which doesn't exist in
    # the live DOM until the main swap finishes.
    panel_html = templates.get_template("_chat_panel.html").render(
        conversation=chat,
        messages=messages,
        pending_stream_url=f"/chats/{chat.id}/stream",
        active_chat_id=chat.id,
    )

    # New sidebar row, marked OOB with selector syntax so HTMX prepends
    # it to the existing `#chats-list` <ul>. The bare `hx-swap-oob`
    # values (`true`, `outerHTML`, `innerHTML`) only target an element
    # by matching id; for `afterbegin` we need the explicit selector
    # form `<swap-style>:<selector>` (HTMX docs §"Out of Band Swaps").
    item_html = templates.get_template("_chat_item.html").render(
        chat=chat,
        active_chat_id=chat.id,
        oob_position="afterbegin:#chats-list",
    )

    body = panel_html + item_html
    response = HTMLResponse(content=body, status_code=status.HTTP_201_CREATED)
    response.headers["HX-Push-Url"] = f"/chats/{chat.id}"
    return response


@router.get("/chats/{conversation_id}", response_class=HTMLResponse)
def get_chat_panel_endpoint(
    request: Request, conversation_id: int, db: DB
) -> Response:
    """Return the chat panel — as a fragment for HTMX, or the full page
    on a direct browser hit.

    HTMX sets the ``HX-Request: true`` header on every request it
    fires. We branch on that header:

    - Present: return just ``_chat_panel.html``. The HTMX swap puts
      it inside ``#main`` and ``hx-push-url`` updates the address bar.
    - Absent: render the full ``index.html`` with the panel preloaded
      in the main slot. This is what the browser sees on a direct
      visit, a reload, or the back/forward buttons — so the URL
      ``/chats/{id}`` is bookmarkable and reload-safe.
    """
    try:
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    messages = queries.list_messages(db, conversation_id)

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_chat_panel.html",
            context={"conversation": conversation, "messages": messages},
        )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "chats": queries.list_conversations(db),
            "conversation": conversation,
            "messages": messages,
            # The active row highlight lives in the sidebar; pass the
            # id so `_chat_item.html` can set `aria-current="page"`.
            "active_chat_id": conversation.id,
        },
    )


@router.get("/chats/{conversation_id}/edit", response_class=HTMLResponse)
def get_chat_edit_endpoint(
    request: Request, conversation_id: int, db: DB
) -> Response:
    """Return the sidebar row in edit mode (a form with the name input).

    Wired to the rename button on the display row, which swaps this
    fragment into place (outerHTML on the <li>). On submit the form
    PATCHes /chats/{id}, which returns the display fragment that
    swaps back over the edit fragment. On cancel the edit fragment
    triggers GET /chats/{id}/item below.
    """
    try:
        chat = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return templates.TemplateResponse(
        request=request,
        name="_chat_item_edit.html",
        context={"chat": chat},
    )


@router.get("/chats/{conversation_id}/item", response_class=HTMLResponse)
def get_chat_item_endpoint(
    request: Request, conversation_id: int, db: DB
) -> Response:
    """Return the sidebar row in display mode.

    Exists for the rename flow's Cancel button: clicking it swaps
    this display fragment back over the edit fragment, restoring the
    original row without modifying anything.
    """
    try:
        chat = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return templates.TemplateResponse(
        request=request,
        name="_chat_item.html",
        context={"chat": chat},
    )


@router.patch("/chats/{conversation_id}", response_class=HTMLResponse)
def rename_chat_endpoint(
    request: Request,
    conversation_id: int,
    db: DB,
    name: Annotated[str, Form()],
) -> Response:
    """Rename a conversation; return the updated sidebar row."""
    try:
        chat = queries.rename_conversation(db, conversation_id, name)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return templates.TemplateResponse(
        request=request,
        name="_chat_item.html",
        context={"chat": chat},
    )


@router.delete(
    "/chats/{conversation_id}",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
def delete_chat_endpoint(
    conversation_id: int, request: Request, db: DB
) -> Response:
    """Delete a conversation; return empty 200 so HTMX's
    ``hx-swap="delete"`` removes the row from the sidebar.

    If the user is currently viewing the chat they just deleted
    (``Referer`` ends with ``/chats/{id}``), set ``HX-Location: /``
    on the response so HTMX navigates the page to the index —
    otherwise they'd be left looking at a stale chat panel whose URL
    404s on reload.

    Server-side check (rather than client-side ``window.location``
    comparison) avoids a brittle timing race: the row's
    ``hx-swap="delete"`` removes the button's parent ``<li>`` before
    ``htmx:after-request`` fires, and event delivery to detached
    elements isn't reliable across browsers.
    """
    queries.delete_conversation(db, conversation_id)
    response = Response(content="", status_code=status.HTTP_200_OK)
    referer = request.headers.get("Referer", "")
    if referer.endswith(f"/chats/{conversation_id}"):
        response.headers["HX-Location"] = "/"
    return response


# ---------------------------------------------------------------------------
# Messages: send + stream
# ---------------------------------------------------------------------------


@router.post(
    "/chats/{conversation_id}/messages",
    response_class=HTMLResponse,
)
def send_message_endpoint(
    request: Request,
    conversation_id: int,
    db: DB,
    content: Annotated[str, Form()],
) -> Response:
    """Save the user message; return user-bubble + assistant placeholder.

    The placeholder opens an SSE connection to
    ``/chats/{id}/stream`` on insert — that endpoint drives the
    actual streaming. Splitting "save user message" (POST) from
    "stream assistant reply" (GET) is the standard HTMX pattern for
    POST-triggered streams: htmx-ext-sse only opens connections via
    GET-based ``sse-connect``.
    """
    try:
        # Confirm the conversation exists before saving so we don't
        # leave an orphan FK error for the user to puzzle over.
        queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    user_message = queries.append_message(
        db, conversation_id, "user", content
    )

    # Render the user bubble + assistant placeholder as one fragment.
    # The browser receives them both, swaps them into #messages, and
    # the placeholder's `sse-connect` triggers the streaming GET.
    user_html = templates.get_template("_message.html").render(
        message=user_message
    )
    placeholder_html = templates.get_template(
        "_assistant_placeholder.html"
    ).render(
        conversation_id=conversation_id,
        stream_url=f"/chats/{conversation_id}/stream",
    )
    return HTMLResponse(content=user_html + placeholder_html)


@router.get("/chats/{conversation_id}/stream")
async def stream_endpoint(
    conversation_id: int, db: DB, client: OllamaClient
) -> StreamingResponse:
    """SSE stream of the assistant's reply to the latest user message."""
    try:
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    history = queries.list_messages(db, conversation_id)
    return StreamingResponse(
        _stream_assistant_reply(
            client, db, conversation_id, conversation.model,
            history, on_complete="append",
        ),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# Regenerate: replace last assistant message
# ---------------------------------------------------------------------------


@router.post(
    "/chats/{conversation_id}/regenerate",
    response_class=HTMLResponse,
)
def regenerate_endpoint(
    request: Request, conversation_id: int, db: DB
) -> Response:
    """Return an assistant placeholder that replaces the last bubble.

    The placeholder's ``sse-connect`` opens the regenerate stream;
    HTMX's swap (``outerHTML`` on the existing assistant message)
    replaces the rendered text with the streaming placeholder.
    """
    try:
        queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    history = queries.list_messages(db, conversation_id)
    if not history or history[-1].role != "assistant":
        # Same 400 case as Phase 6 — gives a clearer error than
        # letting the LookupError surface from the query layer
        # mid-stream.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No assistant message to regenerate.",
        )

    placeholder_html = templates.get_template(
        "_assistant_placeholder.html"
    ).render(
        conversation_id=conversation_id,
        stream_url=f"/chats/{conversation_id}/regenerate-stream",
    )
    return HTMLResponse(content=placeholder_html)


@router.get("/chats/{conversation_id}/regenerate-stream")
async def regenerate_stream_endpoint(
    conversation_id: int, db: DB, client: OllamaClient
) -> StreamingResponse:
    """SSE stream that replaces the last assistant message in place."""
    try:
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    history = queries.list_messages(db, conversation_id)
    if not history or history[-1].role != "assistant":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No assistant message to regenerate.",
        )
    # Drop the last assistant message from the prompt so Ollama
    # generates a fresh reply rather than seeing its own previous
    # output in the history.
    prompt_history = history[:-1]
    return StreamingResponse(
        _stream_assistant_reply(
            client, db, conversation_id, conversation.model,
            prompt_history, on_complete="replace",
        ),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# Shared streaming generator (used by both stream endpoints)
# ---------------------------------------------------------------------------


# Hard ceiling on how many tool rounds a single assistant turn can run
# before we bail out. 5 matches the spec in PLAN.md / phase12 plans.
# Without this, a chatty model that keeps requesting the same tool
# would spin forever and choke the SSE stream.
_TOOL_ITERATION_CAP = 5


def _build_history_payload(
    history: list,
) -> list[dict]:
    """Turn Message dataclasses into the wire format Ollama expects.

    Phase 12d roles map:
        user / assistant → passed through as ``{"role", "content"}``.
        tool_call → assistant message carrying a ``tool_calls`` list,
            unparsed from the JSON we stored in ``content``.
        tool_result → ``{"role": "tool", "content": <result text>}``.

    Malformed ``tool_call`` rows (corrupt JSON, missing keys) are
    skipped silently rather than crashing the request — the rest of
    the conversation is still usable for Ollama to respond to. The
    skip is intentional: a hard failure here would block every
    subsequent turn in a chat that hit one bad row historically.

    Returns:
        A list of dicts in Ollama's ``/api/chat`` ``messages`` shape.
    """
    out: list[dict] = []
    for m in history:
        if m.role == "tool_call":
            # tool_call rows stash the call as JSON in `content`. Unparse
            # back to dicts so the wire format carries structured
            # arguments rather than a stringified blob.
            try:
                call = json.loads(m.content)
                out.append({
                    "role": "assistant",
                    # Ollama wants `content` even on tool-call turns;
                    # empty string is the conventional placeholder.
                    "content": "",
                    "tool_calls": [{
                        "function": {
                            "name": call["name"],
                            "arguments": call.get("arguments", {}),
                        },
                    }],
                })
            except (json.JSONDecodeError, KeyError, TypeError):
                # Skip rather than fail — see docstring rationale.
                continue
        elif m.role == "tool_result":
            # The model expects role="tool" so it can attribute the
            # content back to its earlier tool_calls request.
            out.append({"role": "tool", "content": m.content})
        else:
            out.append({"role": m.role, "content": m.content})
    return out


async def _stream_assistant_reply(
    client,
    db,
    conversation_id: int,
    model: str,
    history: list,
    on_complete: str,
) -> AsyncIterator[str]:
    """Run the tool-calling loop, then stream the final assistant reply.

    Phase 12d wraps the streaming codepath in an iteration loop so the
    model can call tools mid-turn. Each iteration:

    1. Asks Ollama (one non-streaming /api/chat call) whether it wants
       to call a tool given the current history.
    2. If yes: persist each tool_call, emit a placeholder ``tool-call``
       SSE event, run the tool, persist a tool_result row, emit a
       placeholder ``tool-result`` event. Refresh history from the DB
       and loop.
    3. If no: break out of the loop and switch to ``stream_chat`` for
       the visible response.

    Hard cap at ``_TOOL_ITERATION_CAP`` rounds — if Ollama is still
    asking for tools at that point, persist an apology assistant
    message and stop.

    After the streaming response completes (the non-tool branch), the
    phase 11d auto-titler is invoked BEFORE the ``done`` event — the
    done event removes the streaming placeholder which closes the SSE
    connection, so any later events are dropped on the floor.

    Capability gating: phase 12d always passes ``tools=`` regardless of
    whether the chat's model supports tool-calling. The
    ``model_supports_tools`` helper is a 12f deliverable; until it
    ships, chats whose model lacks tool capability will surface an
    Ollama 400 as an SSE ``error`` event. Acceptable since 12f arrives
    shortly and the failure is loud.

    Args:
        client: Shared httpx AsyncClient.
        db: Shared SQLite Connection.
        conversation_id: Parent conversation id.
        model: Ollama model identifier.
        history: Initial Message dataclasses for the prompt. After
            each tool round the history is re-read from the DB so the
            next call to Ollama sees the freshly persisted rows.
        on_complete: ``"append"`` for the new-send case (creates a new
            assistant row); ``"replace"`` for regenerate (overwrites
            the existing last assistant row in place).
    """
    # `working_history` is the live view used to build each Ollama
    # payload. We seed from the caller's list, then refresh from the DB
    # after every tool round so newly-persisted tool_call/tool_result
    # rows are visible to the next iteration.
    working_history = list(history)
    # 12d always advertises every registered tool. Filtering by model
    # capability is 12f — this means non-tool-capable models will see
    # an Ollama 400 here until 12f lands. Documented in the function
    # docstring.
    tools_payload = tool_specs_for_ollama()

    # `for` loop with an `else`: the `else` branch fires only if the
    # loop runs to completion without hitting `break`, i.e. we hit the
    # cap and the model is still requesting tools. Inside the loop we
    # `break` as soon as the model returns no tool calls (signal that
    # it's ready to stream the final answer).
    for iteration in range(_TOOL_ITERATION_CAP):
        try:
            tool_calls, _content = await ollama.maybe_tool_call(
                client,
                model,
                _build_history_payload(working_history),
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
            # Model is done with tools; fall through to streaming.
            break

        # Persist + emit + run each tool call. The events here use
        # placeholder HTML — the real card templates land in 12e. The
        # placeholders still fire the right SSE event names so 12e's
        # `sse-swap` extension is exercised end-to-end during dev.
        for call in tool_calls:
            name = call["name"]
            arguments = call.get("arguments") or {}

            queries.append_message(
                db,
                conversation_id,
                "tool_call",
                # JSON in `content` so _build_history_payload can
                # round-trip it back to Ollama's wire shape next iter.
                content=json.dumps(
                    {"name": name, "arguments": arguments}
                ),
            )
            # 12d placeholder: empty data div with the tool name on a
            # data-attribute. 12e replaces this with the real
            # <details> card via the OOB-swap target
            # `beforebegin:#assistant-stream-{id}`.
            yield _sse(
                f'<div data-tool-call="{html.escape(name)}"></div>',
                event="tool-call",
            )

            result = await run_tool(name, arguments)

            queries.append_message(
                db,
                conversation_id,
                "tool_result",
                content=result,
            )
            yield _sse(
                f'<div data-tool-result="{html.escape(name)}"></div>',
                event="tool-result",
            )

        # Re-read history so the next iteration's payload includes the
        # rows we just persisted. Cheap (one SELECT) and keeps the
        # loop's mental model simple — `working_history` is always the
        # source of truth.
        working_history = queries.list_messages(db, conversation_id)
    else:
        # Loop ran to completion without breaking: the model kept
        # requesting tools past the cap. Persist an apology assistant
        # message so the chat panel has something to show after reload,
        # and emit `done` so the streaming placeholder gets swapped
        # away. Skipping titling here on purpose — a runaway loop isn't
        # representative content for the auto-titler.
        message = queries.append_message(
            db,
            conversation_id,
            "assistant",
            "(Tool-call limit reached; no final answer produced.)",
        )
        final_html = templates.get_template("_message.html").render(
            message=message,
            swap_target=f"#assistant-stream-{conversation_id}",
        )
        yield _sse(final_html, event="done")
        return

    # Final round: stream the model's text response. Identical to the
    # pre-12d streaming codepath, with `working_history` (rather than
    # the initial `history`) as the prompt so any tool rounds we ran
    # are visible to the model.
    chunks: list[str] = []
    try:
        async for chunk in ollama.stream_chat(
            client, model, _build_history_payload(working_history)
        ):
            if chunk.content:
                chunks.append(chunk.content)
                # html.escape so a token containing `<` or `&`
                # doesn't break the page when swapped into the DOM.
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
            db, conversation_id, "assistant", full_text
        )
    else:  # "replace"
        message = queries.replace_last_assistant_message(
            db, conversation_id, full_text
        )

    # Phase 11d: auto-generated title fires BEFORE the done event.
    #
    # The done event uses outerHTML OOB to remove the streaming
    # placeholder (replacing it with the persisted bubble). Once the
    # placeholder is gone, htmx-ext-sse's mutation observer detects
    # the removal and closes the EventSource — so any SSE event sent
    # AFTER done is dropped on the floor.
    #
    # Cost of this ordering: the placeholder keeps its
    # message--streaming class for the duration of title generation
    # (~1-2s with tinyllama), which keeps the send button disabled
    # and the regenerate button hidden. The accumulated text is
    # already visible to the user, so this reads as a brief
    # "settling" pause. Acceptable tradeoff; the alternative
    # (multiplexed SSE on a stable parent element) is much heavier.
    #
    # Skipped entirely on the regenerate path: replace doesn't add
    # an assistant row, so the count-based gate would lie.
    if on_complete == "append":
        async for sse_event in _maybe_generate_title(
            client, db, conversation_id
        ):
            yield sse_event

    # On completion, hand back the final persisted message bubble
    # carrying `hx-swap-oob` so HTMX replaces the streaming
    # placeholder element with this real row (rather than nesting it
    # inside, which would leave the placeholder and its `streaming`
    # class around forever).
    final_html = templates.get_template("_message.html").render(
        message=message,
        swap_target=f"#assistant-stream-{conversation_id}",
    )
    yield _sse(final_html, event="done")


async def _maybe_generate_title(
    client,
    db,
    conversation_id: int,
) -> AsyncIterator[str]:
    """Fire the auto-titler after the 1st, 2nd, or 3rd assistant reply.

    Runs INSIDE `_stream_assistant_reply` between persisting the
    assistant message and yielding the `done` event. The done event
    removes the streaming placeholder, which closes the SSE
    connection — so the title MUST go out first or HTMX never sees
    it. See the call-site comment in _stream_assistant_reply for
    the UX tradeoff (placeholder lingers in its streaming state
    for the title-gen roundtrip).

    Yields zero or one ``title`` SSE event: an OOB sidebar-row swap
    with the new name. The chat's own model is reused for the title
    request (already warm in Ollama from the assistant reply), so
    there's no separate model to install or load.

    Silent skips (no event yielded):
    - The chat has been manually renamed (`name_locked`).
    - The count is outside 1..3 (cap on how many times we refresh).
    - Any Ollama failure (down, malformed reply, timeout). The user
      didn't ask for a title; we don't owe them an error.
    - The model returns empty text after stripping.
    """
    conversation = queries.get_conversation(db, conversation_id)
    if conversation.name_locked:
        return

    count = queries.count_assistant_messages(db, conversation_id)
    if not 1 <= count <= 3:
        return

    full_history = queries.list_messages(db, conversation_id)
    try:
        title = await ollama.generate_title(
            client,
            conversation.model,
            _build_history_payload(full_history),
        )
    except (OllamaUnavailable, OllamaProtocolError):
        # Silent — the chat keeps its current name (placeholder or
        # last successful auto-title). User can rename manually.
        return

    if not title:
        return

    updated = queries.set_name_auto(db, conversation_id, title)
    if updated is None:
        # User renamed between get_conversation above and this UPDATE.
        # The name_locked check inside set_name_auto kept us from
        # overwriting their choice.
        return

    # Render the sidebar row WITH hx-swap-oob="true" baked into the
    # root <li>. The id="chat-{id}" already matches the live row, so
    # HTMX swaps in place via the OOB pass.
    row_html = templates.get_template("_chat_item.html").render(
        chat=updated,
        active_chat_id=updated.id,
        oob_swap=True,
    )
    yield _sse(row_html, event="title")
