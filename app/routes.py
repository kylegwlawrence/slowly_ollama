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
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse

from app import generation, ollama, queries, render
from app import rag_servers as _rag_servers_module
from app.agents.prompts import (
    GENERATION_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
)
from app.dependencies import DB, OllamaClient
from app.ollama import OllamaProtocolError, OllamaUnavailable
from app.rag_health import probe_rag_health
from app.templates import templates

# Side-effecting imports: app.tools.builtins registers `current_time`
# and app.tools.rag registers `query_rag` via their @tool decorators.
# Without these imports, the production app would never call those
# modules (the registry would be empty). They live in routes.py rather
# than generation.py because main.py only imports routes; moving them
# to generation.py would still work today (routes imports generation)
# but couples the registration to an internal seam. The noqa silences
# the unused-import warning since the imports are purely for side
# effect.
from app.tools import builtins as _builtins  # noqa: F401
from app.tools import rag as _rag_tool  # noqa: F401
from app.tools.rag import refresh_query_rag_source_description

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
# Settings — RAG servers (phase 12c) + agentic-mode toggle (phase 13e)
# ---------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
def settings_endpoint(request: Request, db: DB) -> Response:
    """Standalone settings page — RAG servers + (phase 13) agentic mode.

    Direct browser hits return the full index shell with the settings
    fragment preloaded in the main slot (so reload / bookmarks land on
    the same view). HTMX requests get just the fragment, sized for a
    cheap swap into ``#main``. Mirrors the branching pattern in
    ``get_chat_panel_endpoint``.
    """
    servers = _rag_servers_module.list_servers(db)
    agentic_mode_on = queries.get_agentic_mode(db)
    agentic_prompts = {
        "research": RESEARCH_SYSTEM_PROMPT,
        "review": REVIEW_SYSTEM_PROMPT,
        "generation": GENERATION_SYSTEM_PROMPT,
    }
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_settings.html",
            context={
                "servers": servers,
                "agentic_mode_on": agentic_mode_on,
                "agentic_prompts": agentic_prompts,
            },
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
            "agentic_mode_on": agentic_mode_on,
            "agentic_prompts": agentic_prompts,
        },
    )


@router.post("/settings/servers", response_class=HTMLResponse)
async def add_server_endpoint(
    request: Request,
    db: DB,
    name: Annotated[str, Form()],
    url: Annotated[str, Form()],
) -> Response:
    """Add a RAG server; return the new row for ``hx-swap="beforeend"``.

    Probes the remote ``/health`` endpoint BEFORE inserting so a row
    only lands in SQLite if the database the user named is actually
    healthy. A failed probe returns a 502 with the reason as a plain-
    text body, which the form's ``after-request`` JS pipes into the
    inline error region.

    A UNIQUE-constraint collision on the server name comes back from
    SQLite as ``IntegrityError`` — we map it to a 409 with a short
    plain-text body. HTMX's default behaviour is to NOT swap a non-2xx
    response, so the existing list stays intact and the form keeps the
    user's typed values (its `after-request` reset is guarded on
    ``event.detail.successful``).

    On success we call ``refresh_query_rag_source_description`` so the
    next chat turn's tool spec reflects the newly-added source name.
    """
    name_clean = name.strip()
    url_clean = url.strip()

    healthy, reason = await probe_rag_health(name_clean, url_clean)
    if not healthy:
        return HTMLResponse(
            reason,
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    try:
        server = _rag_servers_module.create_server(
            db, name=name_clean, url=url_clean
        )
    except sqlite3.IntegrityError:
        return HTMLResponse(
            f"Server name '{html.escape(name_clean)}' already in use.",
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


@router.post("/settings/agentic-mode", response_class=HTMLResponse)
def toggle_agentic_mode_endpoint(
    request: Request,
    db: DB,
    enabled: Annotated[str | None, Form()] = None,
) -> Response:
    """Toggle the global agentic-mode setting (phase 13e).

    The checkbox sends ``enabled=on`` when checked; the field is absent
    entirely when unchecked. This matches the standard HTML form
    convention and lets us write the helper as a presence check rather
    than a string compare.

    Returns the agentic-mode section fragment so HTMX swaps it in place
    (the toggle lives inside ``#settings-agentic-section``). The
    read-only prompt block is included in the fragment so toggling on
    reveals it without a follow-up round trip.
    """
    agentic_mode_on = enabled is not None
    queries.set_agentic_mode(db, agentic_mode_on)
    return templates.TemplateResponse(
        request=request,
        name="_settings_agentic_section.html",
        context={
            "agentic_mode_on": agentic_mode_on,
            "agentic_prompts": {
                "research": RESEARCH_SYSTEM_PROMPT,
                "review": REVIEW_SYSTEM_PROMPT,
                "generation": GENERATION_SYSTEM_PROMPT,
            },
        },
    )


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


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@router.get("/models", response_class=HTMLResponse)
async def list_models_endpoint(
    request: Request, client: OllamaClient
) -> Response:
    """Return ``<option>`` tags for the model dropdown.

    Phase 12f filters this list to models whose ``/api/show`` capability
    list advertises ``"tools"`` — picking a non-tool-capable model from
    the dropdown 400s on the first message because every chat turn ships
    with ``tools=[...]`` in the request. ``list_tool_capable_models``
    caches per process so the per-model ``/api/show`` round trips only
    pay the cost on the first render in a 60-second window.

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
        models = await ollama.list_tool_capable_models(client)
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
async def create_chat_endpoint(
    request: Request,
    db: DB,
    client: OllamaClient,
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
    blocks = render.group_messages_for_render(messages)

    # Phase 12g: spawn the generation task now so it's already
    # running when the browser opens the SSE connection. A
    # brand-new chat can't have a generation in flight, so no
    # GenerationInProgress catch needed here.
    await generation.start_generation(
        client=client,
        db=db,
        conversation_id=chat.id,
        model=chat.model,
        history=messages,
        on_complete="append",
    )

    # Panel includes the just-saved user bubble AND an inline assistant
    # placeholder that opens the SSE stream on insert. Inlining the
    # placeholder (via `pending_stream_url`) avoids an OOB-vs-main
    # swap-ordering race against `#messages`, which doesn't exist in
    # the live DOM until the main swap finishes.
    panel_html = templates.get_template("_chat_panel.html").render(
        conversation=chat,
        blocks=blocks,
        pending_stream_url=f"/chats/{chat.id}/stream",
        active_chat_id=chat.id,
    )

    # New sidebar row, OOB-prepended to `#chats-list`. The OOB attribute
    # lives on a wrapping <ul>, not on the <li>, because HTMX's non-
    # outerHTML OOB modes insert the OOB element's CHILDREN into the
    # target — a top-level <li hx-swap-oob="afterbegin:..."> would be
    # unwrapped, only its inner <a>/<div> would land in #chats-list,
    # and the new row would render unstyled until reload. See
    # docs/CONVENTIONS.md ("Non-outerHTML OOB swaps unwrap their root").
    item_html = templates.get_template("_chat_item.html").render(
        chat=chat,
        active_chat_id=chat.id,
    )
    oob_sidebar_row = (
        f'<ul hx-swap-oob="afterbegin:#chats-list">{item_html}</ul>'
    )

    body = panel_html + oob_sidebar_row
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
    blocks = render.group_messages_for_render(messages)

    # Phase 12g: if a generation is IN PROGRESS for this conv, the
    # trailing ToolBatchBlock (if any) belongs to the in-progress
    # turn — exclude it from the panel render so the SSE replay can
    # rebuild the card via OOB swaps. Setting `pending_stream_url`
    # makes the chat-panel template render a streaming placeholder
    # pointing at /stream, where consume_generation attaches as a
    # fresh consumer.
    #
    # `live_generations` retains DONE entries for replay-on-slow-
    # reload, so the `not done` check matters — we don't want to
    # render a streaming placeholder on top of an already-finished
    # historic conversation.
    pending_stream_url = None
    live = generation.live_generations.get(conversation_id)
    if live is not None and not live.done:
        if blocks and blocks[-1].kind == "tool_batch":
            blocks = blocks[:-1]
        pending_stream_url = f"/chats/{conversation_id}/stream"

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_chat_panel.html",
            context={
                "conversation": conversation,
                "blocks": blocks,
                "pending_stream_url": pending_stream_url,
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "chats": queries.list_conversations(db),
            "conversation": conversation,
            "blocks": blocks,
            "pending_stream_url": pending_stream_url,
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
async def send_message_endpoint(
    request: Request,
    conversation_id: int,
    db: DB,
    client: OllamaClient,
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
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    user_message = queries.append_message(
        db, conversation_id, "user", content
    )

    # Phase 12g: spawn the generation task NOW so the LLM call is
    # already running by the time the browser opens the SSE
    # connection. The task lives beyond this request's lifecycle —
    # it's owned by `generation.live_generations`, not by the
    # response generator. A page reload (client disconnect) won't
    # cancel it; consume_generation just attaches a new consumer.
    history = queries.list_messages(db, conversation_id)
    try:
        await generation.start_generation(
            client=client,
            db=db,
            conversation_id=conversation_id,
            model=conversation.model,
            history=history,
            on_complete="append",
        )
    except generation.GenerationInProgress:
        # UI gate (placeholder keeps the send button disabled) makes
        # this rare; defensive 409 in case a duplicate POST sneaks
        # through.
        return HTMLResponse(
            '<div class="error">A reply is already streaming for this chat.</div>',
            status_code=status.HTTP_409_CONFLICT,
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
    """SSE stream — attach as a consumer to the live generation if one
    exists, else emit a done event from the persisted assistant row.

    Phase 12g: the POST that triggered this stream (either /messages
    or /regenerate) spawned a generation task and registered it in
    `generation.live_generations`. This endpoint is a thin
    dispatcher — `consume_generation` handles all the replay/tail
    logic. The fallback to `consume_finished` covers the race where
    a reload's GET lands AFTER the generation finished and was
    removed from the registry.
    """
    state = generation.live_generations.get(conversation_id)
    if state is not None:
        return StreamingResponse(
            generation.consume_generation(state),
            media_type="text/event-stream",
        )
    try:
        queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return StreamingResponse(
        generation.consume_finished(db, conversation_id),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# Regenerate: replace last assistant message
# ---------------------------------------------------------------------------


@router.post(
    "/chats/{conversation_id}/regenerate",
    response_class=HTMLResponse,
)
async def regenerate_endpoint(
    request: Request, conversation_id: int, db: DB, client: OllamaClient
) -> Response:
    """Spawn a regen generation; return a placeholder that replaces the bubble.

    Phase 12g: identical shape to send_message_endpoint, but
    ``on_complete="replace"`` so the existing assistant row is
    overwritten in place. The placeholder's ``sse-connect`` points
    at ``/chats/{id}/stream`` (same endpoint as new-message flow
    after 12g — the /regenerate-stream endpoint was removed).
    """
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
    try:
        await generation.start_generation(
            client=client,
            db=db,
            conversation_id=conversation_id,
            model=conversation.model,
            history=prompt_history,
            on_complete="replace",
        )
    except generation.GenerationInProgress:
        return HTMLResponse(
            '<div class="error">A reply is already streaming for this chat.</div>',
            status_code=status.HTTP_409_CONFLICT,
        )

    placeholder_html = templates.get_template(
        "_assistant_placeholder.html"
    ).render(
        conversation_id=conversation_id,
        stream_url=f"/chats/{conversation_id}/stream",
    )
    return HTMLResponse(content=placeholder_html)


