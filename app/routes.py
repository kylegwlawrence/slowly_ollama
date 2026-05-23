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
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)

from app import generation, ollama, queries, render
from app import rag_servers as _rag_servers_module
from app.agents import get_agent, list_agents
from app.dependencies import DB, OllamaClient
from app.ollama import OllamaProtocolError, OllamaUnavailable
from app.projects import ensure_project_workspace, project_workspace_root
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
from app.tools import RAG_TOOL_NAME, TOOLS
from app.tools import builtins as _builtins  # noqa: F401
from app.tools import rag as _rag_tool  # noqa: F401
from app.tools.rag import refresh_query_rag_registration

router = APIRouter()

# All currently-registered tool names, in registration order.
# Computed once at import time. Used for seeding and generation filtering.
_ALL_TOOL_NAMES: list[str] = list(TOOLS.keys())


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------


def _default_tool_states() -> list[queries.ChatToolState]:
    """Return ChatToolState list with all non-RAG tools enabled.

    query_rag is excluded — RAG servers get their own per-server chips.
    """
    return [
        queries.ChatToolState(tool_name=name, enabled=True)
        for name in _ALL_TOOL_NAMES
        # `name in TOOLS` excludes tools the lifespan gating popped (e.g.
        # the file tools when FILE_TOOL_ROOT is unset) so a misconfigured
        # install doesn't seed chips for a tool the model can't see.
        if name != RAG_TOOL_NAME and name in TOOLS
    ]


def _default_rag_server_states(
    db: sqlite3.Connection,
) -> list[queries.ChatRagState]:
    """Return ChatRagState list with all configured RAG servers enabled.

    Used by the empty-state composer so per-server chips default to on
    before a chat is created.
    """
    servers = _rag_servers_module.list_servers(db)
    return [
        queries.ChatRagState(server_name=s.name, enabled=True) for s in servers
    ]


def _chip_states(
    db: sqlite3.Connection,
    conversation_id: int,
    *,
    servers: list | None = None,
) -> tuple[list[queries.ChatToolState], list[queries.ChatRagState]]:
    """Return (tool_states, rag_server_states) for the chip bar.

    tool_states excludes query_rag; RAG servers get their own chips.
    Both lists respect the per-chat settings stored in DB.

    Pass ``servers`` when you already hold the list from a prior
    ``_rag_servers_module.list_servers`` call to avoid a redundant fetch.
    """
    tool_states = [
        s
        for s in queries.get_chat_tool_states(
            db,
            conversation_id,
            # Live-filter so gating-popped tools (e.g. file tools when
            # FILE_TOOL_ROOT is unset) don't surface as chips.
            [n for n in _ALL_TOOL_NAMES if n in TOOLS],
        )
        if s.tool_name != RAG_TOOL_NAME
    ]
    if servers is None:
        servers = _rag_servers_module.list_servers(db)
    rag_server_states = queries.get_chat_rag_states(
        db, conversation_id, [s.name for s in servers]
    )
    return tool_states, rag_server_states


@router.get("/")
def index_endpoint() -> RedirectResponse:
    """Redirect the home URL to the projects index (phase 17).

    All "where am I" navigation enters via /projects after phase 17 — the
    projects index is the new home of the app. Direct hits to ``/`` (the
    user opens the app from a fresh tab) 302 to ``/projects``.
    """
    return RedirectResponse(
        url="/projects", status_code=status.HTTP_302_FOUND
    )


# ---------------------------------------------------------------------------
# Settings — RAG servers (phase 12c)
# ---------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
def settings_endpoint(request: Request, db: DB) -> Response:
    """Standalone settings page — RAG servers + default temperature + default tool cap.

    Direct browser hits return the full index shell with the settings
    fragment preloaded in the main slot (so reload / bookmarks land on
    the same view). HTMX requests get just the fragment, sized for a
    cheap swap into ``#main``. Mirrors the branching pattern in
    ``get_chat_panel_endpoint``.
    """
    servers = _rag_servers_module.list_servers(db)
    default_temperature = queries.get_default_temperature(db)
    default_tool_iteration_cap = queries.get_default_tool_iteration_cap(db)
    default_model = queries.get_default_model(db)
    default_num_ctx = queries.get_default_num_ctx(db)
    agents = list_agents()
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_settings.html",
            context={
                "servers": servers,
                "default_temperature": default_temperature,
                "default_tool_iteration_cap": default_tool_iteration_cap,
                "default_model": default_model,
                "default_num_ctx": default_num_ctx,
                "agents": agents,
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            # Phase 17: settings is its own top-level layout. The
            # unified sidebar (project list + Settings nav) renders
            # alongside the settings fragment in the main slot.
            "layout": "settings",
            "project": None,
            "conversation": None,
            "active_chat_id": None,
            "settings_view": True,
            # Phase 17b: unified sidebar needs the projects list.
            "projects": queries.list_projects(db),
            "active_project_id": None,
            # Passed under `rag_servers` so the index template's
            # `{% set servers = rag_servers %}` adapter resolves it for
            # the included _settings.html fragment.
            "rag_servers": servers,
            "default_temperature": default_temperature,
            "default_tool_iteration_cap": default_tool_iteration_cap,
            "default_model": default_model,
            "default_num_ctx": default_num_ctx,
            "agents": agents,
        },
    )


@router.post("/settings/servers", response_class=HTMLResponse)
async def add_server_endpoint(
    request: Request,
    db: DB,
    name: Annotated[str, Form()],
    url: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
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

    On success we call ``refresh_query_rag_registration`` so the next
    chat turn's tool spec reflects the newly-added source.
    """
    name_clean = name.strip()
    url_clean = url.strip()
    # 200-char cap: maxlength="200" on the textarea is a client-side
    # hint only; silently truncate here as belt-and-suspenders.
    description_clean = description.strip()[:200]

    healthy, reason = await probe_rag_health(name_clean, url_clean)
    if not healthy:
        return HTMLResponse(
            reason,
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    try:
        server = _rag_servers_module.create_server(
            db, name=name_clean, url=url_clean, description=description_clean
        )
    except sqlite3.IntegrityError:
        return HTMLResponse(
            f"Server name '{html.escape(name_clean)}' already in use.",
            status_code=status.HTTP_409_CONFLICT,
        )
    refresh_query_rag_registration()
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
    refresh_query_rag_registration()
    return Response(content="", status_code=status.HTTP_200_OK)


@router.get("/settings/servers/{server_id}", response_class=HTMLResponse)
def get_server_endpoint(
    server_id: int,
    request: Request,
    db: DB,
    edit: bool = False,
) -> Response:
    """Return one RAG server row, in view or edit mode.

    Backs the inline description editor: the row's edit pencil GETs with
    ``?edit=1`` to swap the row into a textarea form; the form's Cancel
    button GETs without the param to swap back to view mode. Both target
    the row's own ``<li>`` with ``hx-swap="outerHTML"``.

    A missing id (e.g. another tab deleted the row) returns 404 so HTMX
    leaves the stale row in place rather than blanking it.
    """
    server = _rag_servers_module.get_server(db, server_id)
    if server is None:
        return Response(content="", status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request=request,
        name="_rag_server_row.html",
        context={"server": server, "editing": edit},
    )


@router.patch("/settings/servers/{server_id}", response_class=HTMLResponse)
def update_server_endpoint(
    server_id: int,
    request: Request,
    db: DB,
    description: Annotated[str, Form()] = "",
) -> Response:
    """Update a server's description in place; return the view-mode row.

    Only the description is editable inline — name/URL edits would need a
    health re-probe and a tool-registry rename, so those still go through
    delete + re-add. Truncates to 200 chars to match the add-server form's
    cap (the textarea's ``maxlength`` is a client-side hint only), then
    refreshes the query_rag registration so the tool's ``source`` hint
    reflects the edited description.

    A missing id returns 404 so a stale row left over from another tab's
    delete isn't replaced with anything.
    """
    description_clean = description.strip()[:200]
    server = _rag_servers_module.update_server_description(
        db, server_id, description_clean
    )
    if server is None:
        return Response(content="", status_code=status.HTTP_404_NOT_FOUND)
    refresh_query_rag_registration()
    return templates.TemplateResponse(
        request=request,
        name="_rag_server_row.html",
        context={"server": server, "editing": False},
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
    request: Request,
    client: OllamaClient,
    prepend_blank: bool = False,
) -> Response:
    """Return ``<option>`` tags for the model dropdown.

    Phase 12f filters this list to models whose ``/api/show`` capability
    list advertises ``"tools"`` — picking a non-tool-capable model from
    the dropdown 400s on the first message because every chat turn ships
    with ``tools=[...]`` in the request. ``list_tool_capable_models``
    caches per process so the per-model ``/api/show`` round trips only
    pay the cost on the first render in a 60-second window.

    Phase 17b: when ``prepend_blank=1`` is passed, the rendered list
    starts with a "(no default — use global)" option. Project Settings
    uses this so clearing the default selects an empty value (which the
    PATCH route persists as NULL via the _UNSET sentinel).

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
                "prepend_blank": prepend_blank,
            },
        )
    except OllamaProtocolError:
        return templates.TemplateResponse(
            request=request,
            name="_model_options.html",
            context={
                "models": [],
                "error": "Ollama returned an unexpected response.",
                "prepend_blank": prepend_blank,
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="_model_options.html",
        context={
            "models": models,
            "error": None,
            "prepend_blank": prepend_blank,
        },
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


def _resolve_num_ctx(
    db: sqlite3.Connection, conversation_id: int
) -> int:
    """Effective Ollama ``num_ctx`` for a turn: project override → global.

    Looks up the project that owns ``conversation_id`` and returns
    ``project.num_ctx`` when set, otherwise the global default
    (``queries.get_default_num_ctx``). Falls back to the global default
    when the project can't be resolved (defensive — every chat should
    have a project post-phase-17).
    """
    try:
        project = queries.get_project_for_conversation(db, conversation_id)
    except LookupError:
        return queries.get_default_num_ctx(db)
    return queries.resolve_num_ctx_for_project(db, project.num_ctx)


def _agent_overrides(conversation: queries.Conversation) -> dict:
    """Resolve a conversation's active agent into ``start_generation`` kwargs.

    Returns the effective ``model`` plus ``system_prompt_override`` /
    ``tool_allowlist`` / ``think``. For Normal chat (no agent, or an
    unknown/removed agent name) this is the chat's pinned model with no
    overrides and ``think=None`` (omit the flag) — i.e. today's plain-chat
    behavior.
    """
    spec = get_agent(conversation.active_agent)
    if spec is None:
        return {
            "model": conversation.model,
            "system_prompt_override": None,
            "tool_allowlist": None,
            "think": None,
        }
    return {
        "model": spec.model,
        "system_prompt_override": spec.system_prompt,
        "tool_allowlist": spec.tools,
        "think": spec.think,
    }


@router.get("/chats/{conversation_id}")
def chat_redirect_endpoint(conversation_id: int, db: DB) -> RedirectResponse:
    """Phase 17 backcompat: resolve the project for a chat and 302 to the canonical URL.

    Pre-17 chats were addressable at ``/chats/{id}``; post-17 the canonical
    URL is project-scoped (``/projects/{pid}/chats/{cid}``). External
    bookmarks + transitional links keep working via this redirect.

    Raises:
        HTTPException 404: When the conversation does not exist.
    """
    try:
        project = queries.get_project_for_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return RedirectResponse(
        url=f"/projects/{project.id}/chats/{conversation_id}",
        status_code=status.HTTP_302_FOUND,
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
        project = queries.get_project_for_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return templates.TemplateResponse(
        request=request,
        name="_chat_item_edit.html",
        context={"chat": chat, "project": project},
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
        # Phase 17: pass the owning project so the row's link URL renders
        # as the canonical project-scoped path, not the legacy /chats/{id}.
        project = queries.get_project_for_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return templates.TemplateResponse(
        request=request,
        name="_chat_item.html",
        context={"chat": chat, "project": project},
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
        # Phase 17: include the project so the rendered row's link URL is
        # project-scoped (matches the canonical URL the browser is on).
        project = queries.get_project_for_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return templates.TemplateResponse(
        request=request,
        name="_chat_item.html",
        context={"chat": chat, "project": project},
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

    If the user is currently viewing the chat they just deleted (``Referer``
    ends with the project-scoped or legacy chat URL), set ``HX-Location`` on
    the response so HTMX navigates the page back to the owning project's
    chats tab — otherwise they'd be left looking at a stale chat panel whose
    URL 404s on reload.

    Server-side check (rather than client-side ``window.location``
    comparison) avoids a brittle timing race: the row's
    ``hx-swap="delete"`` removes the button's parent ``<li>`` before
    ``htmx:after-request`` fires, and event delivery to detached
    elements isn't reliable across browsers.
    """
    # Resolve the owning project BEFORE the delete — post-delete the join
    # would 404. Cache the project so we can build the redirect URL below.
    try:
        project = queries.get_project_for_conversation(db, conversation_id)
    except LookupError:
        project = None
    queries.delete_conversation(db, conversation_id)
    response = Response(content="", status_code=status.HTTP_200_OK)
    referer = request.headers.get("Referer", "")
    # Match BOTH the project-scoped canonical URL and the legacy redirect
    # URL — a user on a backcompat link should still be redirected after
    # deleting the chat they're viewing.
    is_viewing_deleted = (
        project is not None
        and (
            referer.endswith(
                f"/projects/{project.id}/chats/{conversation_id}"
            )
            or referer.endswith(f"/chats/{conversation_id}")
        )
    )
    if is_viewing_deleted:
        response.headers["HX-Location"] = f"/projects/{project.id}/chats"
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
            temperature=conversation.temperature,
            tool_iteration_cap=conversation.tool_iteration_cap,
            num_ctx=_resolve_num_ctx(db, conversation_id),
            history=history,
            on_complete="append",
            **_agent_overrides(conversation),
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
            temperature=conversation.temperature,
            tool_iteration_cap=conversation.tool_iteration_cap,
            num_ctx=_resolve_num_ctx(db, conversation_id),
            history=prompt_history,
            on_complete="replace",
            **_agent_overrides(conversation),
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


@router.post("/chats/{conversation_id}/agent", response_class=HTMLResponse)
async def set_chat_agent_endpoint(
    conversation_id: int,
    db: DB,
    client: OllamaClient,
    agent: Annotated[str | None, Form()] = None,
) -> Response:
    """Set/clear the user-invoked agent for a chat; return OOB UI updates.

    Called by the in-chat agent dropdown on change (``hx-post`` with
    ``hx-swap="none"`` — the response is OOB-only). The selection is
    persisted so it survives reloads and so subsequent turns resolve the
    same agent. ``agent`` is the agent name, or empty/None for Normal.
    An unknown name resolves to Normal (defensive).

    OOB swaps returned:
      - ``#agent-indicator-{id}``: the header indicator, updated to the
        agent label + model (or the chat's pinned model for Normal).
      - ``#chat-tool-chips``: refreshed so the per-chat chips are hidden
        while an agent is active (its allowlist governs its tools) and
        restored when switching back to Normal. Only emitted when the
        chat's pinned model supports tools — otherwise there is no chip
        bar in the DOM and the swap would be a no-op anyway.

    Raises:
        HTTPException 404: When the conversation is unknown.
    """
    try:
        queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    spec = get_agent(agent)
    conversation = queries.set_active_agent(
        db, conversation_id, spec.name if spec else None
    )

    indicator_html = templates.get_template("_agent_indicator.html").render(
        conversation=conversation,
        active_agent_spec=spec,
        oob=True,
    )

    chips_oob = ""
    if await ollama.model_supports_tools(client, conversation.model):
        tool_states, rag_server_states = _chip_states(db, conversation_id)
        chips_oob = templates.get_template("_chat_tool_chips.html").render(
            conversation=conversation,
            active_agent_spec=spec,
            supports_tools=True,
            tool_states=tool_states,
            rag_server_states=rag_server_states,
            oob=True,
        )

    return HTMLResponse(content=indicator_html + chips_oob)


# ---------------------------------------------------------------------------
# Phase 15: per-chat tool toggles
# ---------------------------------------------------------------------------


@router.post(
    "/chats/{conversation_id}/tools/{tool_name}",
    response_class=HTMLResponse,
)
async def toggle_chat_tool_endpoint(
    request: Request,
    conversation_id: int,
    tool_name: str,
    db: DB,
) -> Response:
    """Toggle one tool on/off for a conversation; return the updated chip bar.

    Called by an HTMX hx-post on each tool chip. Returns the full chip
    bar fragment for innerHTML swap into ``#chat-tool-chips`` so chip
    ordering stays stable and all chip states are in sync.

    Chips are only visible when the model supports tools, so supports_tools
    is always True here — no capability re-check needed.

    Raises:
        HTTPException 404: When the conversation or tool name is unknown.
    """
    try:
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    if tool_name not in TOOLS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown tool: {tool_name}")
    queries.toggle_chat_tool(db, conversation_id, tool_name)
    tool_states, rag_server_states = _chip_states(db, conversation_id)
    return templates.TemplateResponse(
        request=request,
        name="_tool_chips.html",
        context={
            "conversation": conversation,
            "tool_states": tool_states,
            "rag_server_states": rag_server_states,
            "supports_tools": True,
            "is_composer": False,
        },
    )


@router.post(
    "/chats/{conversation_id}/rag-servers/{server_name}",
    response_class=HTMLResponse,
)
async def toggle_chat_rag_server_endpoint(
    request: Request,
    conversation_id: int,
    server_name: str,
    db: DB,
) -> Response:
    """Toggle one RAG server on/off for a conversation; return the chip bar.

    Called by an HTMX hx-post on each per-server chip. Returns the full
    chip bar for innerHTML swap into ``#chat-tool-chips``.

    404s when the conversation is unknown or the server name is not in the
    currently-configured set — prevents toggling phantom servers.

    Raises:
        HTTPException 404: When the conversation or server name is unknown.
    """
    try:
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    servers = _rag_servers_module.list_servers(db)
    if server_name not in {s.name for s in servers}:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Unknown RAG server: {server_name}"
        )
    queries.toggle_chat_rag_server(db, conversation_id, server_name)
    tool_states, rag_server_states = _chip_states(db, conversation_id, servers=servers)
    return templates.TemplateResponse(
        request=request,
        name="_tool_chips.html",
        context={
            "conversation": conversation,
            "tool_states": tool_states,
            "rag_server_states": rag_server_states,
            "supports_tools": True,
            "is_composer": False,
        },
    )


# ---------------------------------------------------------------------------
# Per-chat temperature
# ---------------------------------------------------------------------------


@router.patch(
    "/chats/{conversation_id}/temperature",
    response_class=Response,
)
async def set_chat_temperature_endpoint(
    conversation_id: int,
    db: DB,
    temperature: Annotated[float, Form()],
) -> Response:
    """Persist the sampling temperature for a conversation.

    Called by the temperature ``<input>`` in ``_chat_panel.html`` via
    ``hx-patch`` on the ``change`` event. Clamps to [0.0, 2.0] server-side
    so a hand-crafted request can't push Ollama out of range.

    Returns 204 No Content — the browser input already shows the typed
    value, so no swap is needed.

    Raises:
        HTTPException 404: When the conversation doesn't exist.
    """
    temperature = max(0.0, min(2.0, temperature))
    try:
        queries.set_conversation_temperature(db, conversation_id, temperature)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/chats/{conversation_id}/tool-iteration-cap",
    response_class=Response,
)
async def set_chat_tool_iteration_cap_endpoint(
    conversation_id: int,
    db: DB,
    tool_iteration_cap: Annotated[int, Form()],
) -> Response:
    """Persist the single-agent tool-iteration cap for a conversation.

    Called by the cap ``<input>`` in ``_chat_panel.html`` via
    ``hx-patch`` on the ``change`` event. Clamps to [1, 10] server-side
    so a hand-crafted request can't drive a runaway or no-op tool loop.

    Returns 204 No Content — the browser input already shows the typed
    value, so no swap is needed.

    Raises:
        HTTPException 404: When the conversation doesn't exist.
    """
    tool_iteration_cap = max(1, min(10, tool_iteration_cap))
    try:
        queries.set_conversation_tool_iteration_cap(
            db, conversation_id, tool_iteration_cap
        )
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/settings/default-temperature",
    response_class=Response,
)
async def set_default_temperature_endpoint(
    db: DB,
    temperature: Annotated[float, Form()],
) -> Response:
    """Persist the global default sampling temperature for new chats.

    Called by the default-temperature ``<input>`` in ``_settings.html``
    via ``hx-patch`` on the ``change`` event. Clamps to [0.0, 2.0]
    server-side so a hand-crafted request can't store an out-of-range
    value. Only affects chats created after the change; existing chats
    keep their own per-chat temperature.

    Returns 204 No Content — the browser input already shows the typed
    value, so no swap is needed.
    """
    queries.set_default_temperature(db, temperature)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/settings/default-tool-cap",
    response_class=Response,
)
async def set_default_tool_iteration_cap_endpoint(
    db: DB,
    tool_iteration_cap: Annotated[int, Form()],
) -> Response:
    """Persist the global default per-turn tool-iteration cap for new chats.

    Called by the default-tool-cap ``<input>`` in ``_settings.html``
    via ``hx-patch`` on the ``change`` event. Clamps to [1, 10]
    server-side so a hand-crafted request can't store an out-of-range
    value. Only affects chats created after the change; existing chats
    keep their own per-chat cap.

    Returns 204 No Content — the browser input already shows the typed
    value, so no swap is needed.
    """
    queries.set_default_tool_iteration_cap(db, tool_iteration_cap)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/settings/default-num-ctx",
    response_class=Response,
)
async def set_default_num_ctx_endpoint(
    db: DB,
    num_ctx: Annotated[int, Form()],
) -> Response:
    """Persist the global default Ollama context window for new chats.

    Called by the default-num-ctx ``<input>`` in ``_settings.html`` via
    ``hx-patch`` on the ``change`` event. Clamps to
    [NUM_CTX_MIN, NUM_CTX_MAX] server-side so a hand-crafted request
    can't store an out-of-range value. Takes effect on the next turn
    of any chat that doesn't have a project-level override.

    Returns 204 No Content — the browser input already shows the typed
    value, so no swap is needed.
    """
    queries.set_default_num_ctx(db, num_ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/settings/default-model",
    response_class=Response,
)
async def set_default_model_endpoint(
    db: DB,
    model: Annotated[str | None, Form()] = None,
) -> Response:
    """Persist the global default model for new chats.

    Called by the default-model ``<select>`` in ``_settings.html`` via
    ``hx-patch`` on the ``change`` event. An empty string or missing
    field clears the setting so the composer falls back to whichever
    model Ollama lists first. Only affects chats created after the
    change; existing chats keep their own per-chat model.

    Returns 204 No Content — the select already shows the chosen option.
    """
    queries.set_default_model(db, model or None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Phase 17: projects
# ---------------------------------------------------------------------------


@dataclass
class _WorkspaceEntry:
    """One row in the Files-tab directory listing.

    Attributes:
        name: Display name of the file or directory.
        is_dir: True for directories (which get a browse link, no
            size, no download).
        size_display: Pretty-printed byte count for files; empty
            string for dirs.
        href_browse: URL to descend into the directory (None for files).
        href_view: URL to render the file in the Files tab (None for dirs).
        href_download: URL to download the file as an attachment (None
            for dirs).
    """

    name: str
    is_dir: bool
    size_display: str
    href_browse: str | None
    href_view: str | None
    href_download: str | None


@dataclass
class _WorkspaceListing:
    """Result shape for ``_browse_workspace``.

    Attributes:
        available: False when FILE_TOOL_ROOT is unset (file tools off);
            the template renders an "unavailable" message.
        path: The workspace-relative directory being shown.
        breadcrumbs: ``[(label, href), ...]`` from workspace root down to
            the current directory.
        entries: The listed children (dirs first, then files), capped.
        error: A user-facing reason when the listing failed (e.g. the
            path is outside the workspace, or doesn't exist). Mutually
            exclusive with ``entries`` carrying useful data.
    """

    available: bool
    path: str
    breadcrumbs: list[tuple[str, str]]
    entries: list[_WorkspaceEntry]
    error: str | None


@dataclass
class _WorkspaceFileView:
    """Result shape for ``_read_workspace_file``.

    Attributes:
        available: False when FILE_TOOL_ROOT is unset.
        path: The workspace-relative file path.
        breadcrumbs: Crumbs from root down to (and including) the file.
        text: UTF-8 contents (truncated at the cap), or None when the
            file isn't displayable as text.
        is_markdown: True for ``.md`` / ``.markdown`` extensions.
        rendered_html: Pre-rendered HTML for markdown views (None for
            plain text).
        size_display: Pretty-printed file size.
        error: User-facing reason when the file can't be displayed (not
            found, binary, etc.).
        download_href: URL to download the original file as an attachment.
    """

    available: bool
    path: str
    breadcrumbs: list[tuple[str, str]]
    text: str | None
    is_markdown: bool
    rendered_html: str | None
    size_display: str
    error: str | None
    download_href: str


# Mirrors ``_LIST_DIR_CAP`` in app/tools/builtins.py — keep the cap
# consistent between the model-facing list_directory tool and the user-
# facing Files tab so neither view can swamp the renderer.
_FILES_BROWSE_CAP = 200

# UTF-8 text-view ceiling. Larger files are rendered truncated with a
# "use Download for full file" hint.
_FILE_VIEW_CAP = 100_000


def _format_size_bytes(n: int) -> str:
    """Pretty-print a byte count (matches ``app.tools.builtins._format_size``)."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _project_workspace_or_none(project: queries.Project) -> Path | None:
    """Return the project's workspace dir (creating it), or None when off.

    Wrapper that combines :func:`app.projects.project_workspace_root` and
    :func:`app.projects.ensure_project_workspace` so the Files-tab helpers
    can pre-create the dir on first visit (so the listing isn't an
    immediate "directory not found" the first time a user clicks Files
    after creating a project).
    """
    root = project_workspace_root(project)
    if root is None:
        return None
    ensure_project_workspace(project)
    return root


def _build_breadcrumbs(
    project_id: int, rel_path: str, tab: str
) -> list[tuple[str, str]]:
    """Build ``[(label, href), ...]`` for the workspace breadcrumb bar.

    Args:
        project_id: The owning project's id (interpolated into URLs).
        rel_path: Workspace-relative path being shown (``"."`` for root).
        tab: ``"browse"`` (directory listing) or ``"view"`` (single file).
            The browse tab's last crumb points at the directory itself;
            the view tab's last crumb points at the file viewer for that
            file.

    Returns:
        Ordered crumbs starting with ``("workspace", root URL)``.
    """
    # `Path.parts` includes a leading "." for "." or "" — filter it out
    # so the crumb list doesn't start with a vestigial entry.
    parts = [p for p in Path(rel_path).parts if p not in (".", "")]
    crumbs: list[tuple[str, str]] = [
        ("workspace", f"/projects/{project_id}/files")
    ]
    accum = Path(".")
    # For a directory view, every part is a clickable subdirectory link.
    # For a file view, the last part is the file (rendered by view tab),
    # so only the leading parts are directory links.
    nav_parts = parts if tab == "browse" else parts[:-1]
    for part in nav_parts:
        accum = accum / part
        crumbs.append(
            (part, f"/projects/{project_id}/files?path={accum}")
        )
    if tab == "view" and parts:
        crumbs.append(
            (
                parts[-1],
                f"/projects/{project_id}/files/view?path={rel_path}",
            )
        )
    return crumbs


def _browse_workspace(
    project: queries.Project, path: str
) -> _WorkspaceListing:
    """Build a directory listing for the Files tab.

    Args:
        project: The owning project.
        path: Workspace-relative directory path (``"."`` = workspace root).

    Returns:
        A populated _WorkspaceListing. ``available`` is False when
        FILE_TOOL_ROOT is unset; ``error`` is populated for path-outside-
        workspace or directory-not-found cases.
    """
    root = _project_workspace_or_none(project)
    if root is None:
        return _WorkspaceListing(
            available=False,
            path=path,
            breadcrumbs=[],
            entries=[],
            error="File tools are not configured (FILE_TOOL_ROOT is unset).",
        )
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        return _WorkspaceListing(
            available=True,
            path=path,
            breadcrumbs=_build_breadcrumbs(project.id, ".", "browse"),
            entries=[],
            error="Path is outside the workspace.",
        )
    if not target.exists() or not target.is_dir():
        return _WorkspaceListing(
            available=True,
            path=path,
            breadcrumbs=_build_breadcrumbs(project.id, path, "browse"),
            entries=[],
            error="Directory not found.",
        )
    rel_target = "" if target == root else str(target.relative_to(root))
    entries: list[_WorkspaceEntry] = []
    # Sort: dirs first, then files; each group alphabetical (case-
    # insensitive). Matches `list_directory`'s ordering.
    children = sorted(
        target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
    )[:_FILES_BROWSE_CAP]
    for child in children:
        child_rel = str(child.relative_to(root))
        if child.is_dir():
            entries.append(
                _WorkspaceEntry(
                    name=child.name,
                    is_dir=True,
                    size_display="",
                    href_browse=(
                        f"/projects/{project.id}/files?path={child_rel}"
                    ),
                    href_view=None,
                    href_download=None,
                )
            )
        else:
            try:
                size = _format_size_bytes(child.stat().st_size)
            except OSError:
                size = "?"
            entries.append(
                _WorkspaceEntry(
                    name=child.name,
                    is_dir=False,
                    size_display=size,
                    href_browse=None,
                    href_view=(
                        f"/projects/{project.id}/files/view?path={child_rel}"
                    ),
                    href_download=(
                        f"/projects/{project.id}/files/download?path={child_rel}"
                    ),
                )
            )
    return _WorkspaceListing(
        available=True,
        path=rel_target or ".",
        breadcrumbs=_build_breadcrumbs(
            project.id, rel_target or ".", "browse"
        ),
        entries=entries,
        error=None,
    )


def _read_workspace_file(
    project: queries.Project, path: str
) -> _WorkspaceFileView:
    """Build a file-view payload for the Files tab.

    Args:
        project: The owning project.
        path: Workspace-relative file path.

    Returns:
        A populated _WorkspaceFileView. ``error`` is populated for
        path-outside-workspace, file-not-found, or binary-file cases.
    """
    root = _project_workspace_or_none(project)
    download_href = (
        f"/projects/{project.id}/files/download?path={path}"
    )
    if root is None:
        return _WorkspaceFileView(
            available=False,
            path=path,
            breadcrumbs=[],
            text=None,
            is_markdown=False,
            rendered_html=None,
            size_display="",
            download_href=download_href,
            error="File tools are not configured.",
        )
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return _WorkspaceFileView(
            available=True,
            path=path,
            breadcrumbs=_build_breadcrumbs(project.id, path, "view"),
            text=None,
            is_markdown=False,
            rendered_html=None,
            size_display="",
            download_href=download_href,
            error="File not found.",
        )
    size = _format_size_bytes(target.stat().st_size)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _WorkspaceFileView(
            available=True,
            path=path,
            breadcrumbs=_build_breadcrumbs(project.id, path, "view"),
            text=None,
            is_markdown=False,
            rendered_html=None,
            size_display=size,
            download_href=download_href,
            error="Binary file — use Download.",
        )
    if len(text) > _FILE_VIEW_CAP:
        text = (
            text[:_FILE_VIEW_CAP]
            + "\n\n… (truncated; use Download for full file)"
        )
    is_md = target.suffix.lower() in (".md", ".markdown")
    rendered = None
    if is_md:
        import markdown as _md

        rendered = _md.markdown(text, extensions=["fenced_code", "tables"])
    return _WorkspaceFileView(
        available=True,
        path=path,
        breadcrumbs=_build_breadcrumbs(project.id, path, "view"),
        text=text,
        is_markdown=is_md,
        rendered_html=rendered,
        size_display=size,
        download_href=download_href,
        error=None,
    )


# ---------------------------------------------------------------------------
# /projects routes
# ---------------------------------------------------------------------------


def _project_context(
    db: sqlite3.Connection,
    *,
    composer: bool = False,
) -> dict:
    """Build the shared context for project page renders.

    Pulls together global defaults the composer / chat panel need —
    default temperature, default tool cap, agent registry, current
    RAG-server states. Centralized here so every project endpoint
    renders with the same shape.

    Args:
        db: Open SQLite connection.
        composer: When True, also includes the per-composer default
            tool / RAG chip states (only meaningful on Chats-tab empty
            state where the composer renders).
    """
    ctx = {
        "default_temperature": queries.get_default_temperature(db),
        "default_tool_iteration_cap": queries.get_default_tool_iteration_cap(db),
        "global_default_model": queries.get_default_model(db),
        "agents": list_agents(),
    }
    if composer:
        ctx["default_tool_states"] = _default_tool_states()
        ctx["default_rag_server_states"] = _default_rag_server_states(db)
    return ctx


@router.get("/projects", response_class=HTMLResponse)
def list_projects_endpoint(request: Request, db: DB) -> Response:
    """Render the projects index page.

    Full layout with the projects sidebar on the left and a
    list-and-create panel on the right. Direct hits to /projects land
    here; the page is the new home of the app (``GET /`` 302s here).
    HTMX requests get just the projects-index fragment for a cheap
    main-panel swap.
    """
    projects = queries.list_projects(db)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_projects_index.html",
            context={"projects": projects},
        )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "layout": "projects",
            "projects": projects,
            # Phase 17b: sidebar highlights the current project; no
            # project is "current" on the index page.
            "active_project_id": None,
            "project": None,
            "conversation": None,
            "active_chat_id": None,
        },
    )


@router.post(
    "/projects",
    response_class=HTMLResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_project_endpoint(
    request: Request,
    db: DB,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
) -> Response:
    """Create a project; return the new row + push the URL to its chats tab.

    Name must be unique (UNIQUE constraint). On conflict returns 409 with a
    plain-text reason; HTMX leaves the form intact on a non-2xx response.

    Eagerly creates the on-disk workspace so the Files tab works
    immediately, even before the first agent tool call lands there.
    """
    name_clean = name.strip()
    if not name_clean:
        return HTMLResponse(
            "Name is required.", status_code=status.HTTP_400_BAD_REQUEST
        )
    try:
        project = queries.create_project(
            db, name=name_clean, description=description.strip()
        )
    except sqlite3.IntegrityError:
        return HTMLResponse(
            f"Project name '{html.escape(name_clean)}' already in use.",
            status_code=status.HTTP_409_CONFLICT,
        )
    ensure_project_workspace(project)
    # Phase 17b: render the main-panel tile AND OOB-prepend a row into
    # the unified sidebar's #projects-list so the new project appears
    # in the sidebar without a full reload. Wrapping <ul> matches the
    # OOB pattern used by chat creation (afterbegin unwraps the root,
    # so a top-level <li> would lose its parent context).
    tile_html = templates.get_template("_project_item.html").render(
        project=project
    )
    escaped_name = html.escape(project.name)
    sidebar_row_html = (
        f'<ul hx-swap-oob="afterbegin:#projects-list">'
        f'<li class="chat-item project-item" '
        f'data-project-id="{project.id}">'
        f'<a id="project-sidebar-link-{project.id}" '
        f'href="/projects/{project.id}/chats" '
        f'hx-get="/projects/{project.id}/chats" '
        f'hx-target="#main" hx-swap="innerHTML" '
        f'hx-push-url="true">{escaped_name}</a>'
        f"</li></ul>"
    )
    response = HTMLResponse(
        content=tile_html + sidebar_row_html,
        status_code=status.HTTP_201_CREATED,
    )
    response.headers["HX-Push-Url"] = f"/projects/{project.id}/chats"
    return response


@router.get("/projects/{project_id}")
def project_redirect_endpoint(project_id: int) -> RedirectResponse:
    """Canonical entry: /projects/{id} → /projects/{id}/chats.

    Saves a tab in the URL for "open the project" links; the Chats tab is
    the default. Does NOT verify the project exists — the redirect target
    will 404 if not.
    """
    return RedirectResponse(
        url=f"/projects/{project_id}/chats",
        status_code=status.HTTP_302_FOUND,
    )


@router.patch("/projects/{project_id}", response_class=HTMLResponse)
async def update_project_endpoint(
    project_id: int,
    request: Request,
    db: DB,
) -> Response:
    """Update a project's editable fields; return the refreshed settings tab.

    The settings form posts ALL of its fields on submit (even when they
    haven't changed), so the route reads the FormData directly to
    distinguish "field omitted entirely" (sentinel — leave alone) from
    "field submitted empty" (clear). Empty strings for
    ``default_model`` / ``default_agent`` persist as NULL.

    Raises:
        HTTPException 404: When the project does not exist.
    """
    try:
        queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    form = await request.form()

    # Name / description are simple — passing None leaves them alone, but
    # the form always sends them (the textbox can't be omitted client-side).
    name = form.get("name")
    description = form.get("description")

    def _string_or_clear(key: str):
        """Return the new value (or None to clear, or sentinel to leave alone).

        If the form key is absent → sentinel (don't touch).
        If present but empty/whitespace → None (clear to SQL NULL).
        Else → the trimmed string.
        """
        if key not in form:
            return queries._UNSET
        raw = form.get(key, "")
        s = raw.strip() if isinstance(raw, str) else ""
        return s if s else None

    def _int_or_clear(key: str):
        """Mirror of ``_string_or_clear`` for integer fields like num_ctx.

        Absent → sentinel; empty/whitespace → None (inherit global);
        non-numeric → sentinel (treat as untouched — better than
        crashing the whole save for a stray ``"abc"``); otherwise the
        parsed int (clamping happens in ``update_project``).
        """
        if key not in form:
            return queries._UNSET
        raw = form.get(key, "")
        s = raw.strip() if isinstance(raw, str) else ""
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return queries._UNSET

    project = queries.update_project(
        db,
        project_id,
        name=(name.strip() if isinstance(name, str) and name.strip() else None),
        description=(
            description.strip() if isinstance(description, str) else None
        ),
        default_model=_string_or_clear("default_model"),
        default_agent=_string_or_clear("default_agent"),
        num_ctx=_int_or_clear("num_ctx"),
    )
    # Phase 17b: the settings body is swapped into #project-page-body, but
    # the project name also lives in the page header (above the body) and
    # in the unified sidebar. Append OOB swaps for both so a rename lands
    # everywhere on the same response — no separate refresh needed. Idempotent
    # when the name didn't change, so we always include them.
    settings_html = templates.get_template(
        "_project_settings_body.html"
    ).render(
        project=project,
        saved=True,
        agents=list_agents(),
        global_default_num_ctx=queries.get_default_num_ctx(db),
    )
    escaped_name = html.escape(project.name)
    header_oob = (
        f'<h2 id="project-page-name" class="project-page__name" '
        f'hx-swap-oob="true">{escaped_name}</h2>'
    )
    sidebar_link_oob = (
        f'<a id="project-sidebar-link-{project.id}" '
        f'href="/projects/{project.id}/chats" '
        f'hx-get="/projects/{project.id}/chats" '
        f'hx-target="#main" hx-swap="innerHTML" '
        f'hx-push-url="true" hx-swap-oob="true">{escaped_name}</a>'
    )
    return HTMLResponse(settings_html + header_oob + sidebar_link_oob)


@router.delete(
    "/projects/{project_id}",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
def delete_project_endpoint(project_id: int, db: DB) -> Response:
    """Delete a project (and cascade its chats). Refuses the last project.

    Refuses with 409 when this would leave zero projects — the app needs
    at least one as a home view. The on-disk workspace under
    ``FILE_TOOL_ROOT/<workspace_subdir>`` is PRESERVED (not deleted) so the
    user can recover files from a deleted project.

    Raises:
        HTTPException 404: When the project does not exist.
    """
    try:
        queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    if queries.count_projects(db) <= 1:
        return HTMLResponse(
            "Cannot delete the last project.",
            status_code=status.HTTP_409_CONFLICT,
        )
    queries.delete_project(db, project_id)
    # Phase 17b: use HX-Redirect, not HX-Location. HX-Location issues an
    # ajax GET (carrying HX-Request) and swaps the response into <body> —
    # which here means the /projects fragment replaces the entire body,
    # wiping the sidebar. HX-Redirect sets window.location, so the
    # browser does a real navigation, /projects renders as a full page
    # with the sidebar intact, and history is clean.
    response = Response(content="", status_code=status.HTTP_200_OK)
    response.headers["HX-Redirect"] = "/projects"
    return response


def _render_project_page(
    request: Request,
    *,
    db: sqlite3.Connection,
    project: queries.Project,
    active_tab: str,
    extra: dict | None = None,
) -> Response:
    """Render the project page (full or HTMX fragment).

    Args:
        request: Inbound request (used to detect HX-Request).
        db: Open SQLite connection.
        project: The project being viewed.
        active_tab: ``"chats"`` / ``"files"`` / ``"settings"``.
        extra: Per-tab context — merged into the template context. Pass
            ``conversation`` for an open chat, ``files_ctx`` for the
            Files tab, ``settings_ctx`` for the Settings tab, etc.
    """
    chats = queries.list_conversations_in_project(db, project.id)
    base = {
        "project": project,
        "active_tab": active_tab,
        "chats": chats,
        # Active chat id (when one is open) makes the sidebar row
        # highlight correctly. Default None for the empty-state /
        # Files / Settings tabs.
        "active_chat_id": None,
        # Most tabs need agents + defaults available; include them so
        # included partials don't have to re-fetch.
        **_project_context(db, composer=active_tab == "chats"),
    }
    if extra:
        base.update(extra)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_project_page.html",
            context=base,
        )
    # Phase 17b: full-page renders include the unified sidebar, which
    # needs the projects list + active row id.
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "layout": "project",
            "projects": queries.list_projects(db),
            "active_project_id": project.id,
            **base,
        },
    )


@router.get("/projects/{project_id}/chats", response_class=HTMLResponse)
def project_chats_endpoint(
    project_id: int, request: Request, db: DB
) -> Response:
    """Render the project page with the Chats tab active, no chat open.

    Shows the empty-state composer (the only "create new chat"
    affordance — POST goes to /projects/{pid}/chats).
    """
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return _render_project_page(
        request,
        db=db,
        project=project,
        active_tab="chats",
        extra={
            "conversation": None,
            # Pre-fill hooks for the composer so a project default
            # propagates into the model / agent selects.
            "project_default_model": project.default_model,
            "project_default_agent": project.default_agent,
        },
    )


@router.get(
    "/projects/{project_id}/chats/new",
    response_class=HTMLResponse,
)
def project_new_chat_endpoint(
    project_id: int, request: Request, db: DB
) -> Response:
    """HTMX-only entry point for the sidebar "+ New chat" link.

    Returns the empty-state composer fragment scoped to the project.
    Cheaper than a full project-chats render because the sidebar
    doesn't re-render (preserving the chats list's scroll position).

    Raises:
        HTTPException 404: When the project does not exist.
    """
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return templates.TemplateResponse(
        request=request,
        name="_composer.html",
        context={
            **_project_context(db, composer=True),
            "project": project,
            "project_default_model": project.default_model,
            "project_default_agent": project.default_agent,
        },
    )


@router.get(
    "/projects/{project_id}/chats/{conversation_id}",
    response_class=HTMLResponse,
)
async def project_chat_panel_endpoint(
    project_id: int,
    conversation_id: int,
    request: Request,
    db: DB,
    client: OllamaClient,
) -> Response:
    """Render the project page with a specific chat open.

    Validates that the chat belongs to the project — a chat-id that
    points at a different project's chat 404s rather than rendering as
    if it were under this project. The backcompat /chats/{id} redirect
    resolves the real project_id and lands the user on the canonical URL.

    Raises:
        HTTPException 404: When the project, the chat, or the
            project-chat pairing doesn't exist.
    """
    try:
        project = queries.get_project(db, project_id)
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    if conversation.project_id != project_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Chat {conversation_id} does not belong to project {project_id}.",
        )
    messages = queries.list_messages(db, conversation_id)
    blocks = render.group_messages_for_render(messages)

    # Phase 12g: identical pending-stream logic to the legacy
    # get_chat_panel_endpoint — preserved verbatim so a reload during an
    # in-flight generation attaches as a fresh consumer.
    pending_stream_url = None
    live = generation.live_generations.get(conversation_id)
    if live is not None and not live.done:
        if blocks and blocks[-1].kind == "tool_batch":
            blocks = blocks[:-1]
        pending_stream_url = f"/chats/{conversation_id}/stream"

    supports_tools = await ollama.model_supports_tools(
        client, conversation.model
    )
    if supports_tools:
        tool_states, rag_server_states = _chip_states(db, conversation_id)
    else:
        tool_states, rag_server_states = [], []

    active_agent_spec = get_agent(conversation.active_agent)

    return _render_project_page(
        request,
        db=db,
        project=project,
        active_tab="chats",
        extra={
            "conversation": conversation,
            "active_chat_id": conversation.id,
            "blocks": blocks,
            "pending_stream_url": pending_stream_url,
            "supports_tools": supports_tools,
            "tool_states": tool_states,
            "rag_server_states": rag_server_states,
            "active_agent_spec": active_agent_spec,
        },
    )


@router.post(
    "/projects/{project_id}/chats",
    response_class=HTMLResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_chat_endpoint(
    project_id: int,
    request: Request,
    db: DB,
    client: OllamaClient,
    model: Annotated[str, Form()],
    content: Annotated[str, Form()],
    temperature: Annotated[float | None, Form()] = None,
    tool_iteration_cap: Annotated[int | None, Form()] = None,
    agent: Annotated[str | None, Form()] = None,
) -> Response:
    """Create a chat inside a project AND save its first message.

    Same shape as the pre-phase-17 ``POST /chats``: persists the chat,
    seeds per-chat tool / RAG rows from the composer checkboxes, spawns
    the generation task, and returns the rendered chat panel + the OOB
    sidebar row in one body. ``HX-Push-Url`` syncs the address bar to
    the canonical project-scoped URL.

    Raises:
        HTTPException 404: When the project does not exist.
    """
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    if temperature is None:
        temperature = queries.get_default_temperature(db)
    temperature = max(0.0, min(2.0, temperature))
    if tool_iteration_cap is None:
        tool_iteration_cap = queries.get_default_tool_iteration_cap(db)
    tool_iteration_cap = max(1, min(10, tool_iteration_cap))
    agent_spec = get_agent(agent)
    chat = queries.create_conversation(
        db,
        name=_placeholder_name(content),
        model=model,
        project_id=project_id,
        temperature=temperature,
        tool_iteration_cap=tool_iteration_cap,
        active_agent=agent_spec.name if agent_spec else None,
    )
    queries.append_message(db, chat.id, "user", content)

    form_data = await request.form()
    enabled_tools_raw = form_data.getlist("enabled_tools")
    enabled_names: set[str] | None = (
        set(enabled_tools_raw) if enabled_tools_raw else None
    )
    queries.seed_chat_tools(
        db, chat.id, _ALL_TOOL_NAMES, enabled_names=enabled_names
    )

    enabled_rag_raw = form_data.getlist("enabled_rag_servers")
    enabled_rag: set[str] | None = (
        set(enabled_rag_raw) if enabled_rag_raw else None
    )
    rag_servers_list = _rag_servers_module.list_servers(db)
    queries.seed_chat_rag_servers(
        db,
        chat.id,
        [s.name for s in rag_servers_list],
        enabled_names=enabled_rag,
    )

    messages = queries.list_messages(db, chat.id)
    blocks = render.group_messages_for_render(messages)

    await generation.start_generation(
        client=client,
        db=db,
        conversation_id=chat.id,
        temperature=chat.temperature,
        tool_iteration_cap=chat.tool_iteration_cap,
        num_ctx=queries.resolve_num_ctx_for_project(db, project.num_ctx),
        history=messages,
        on_complete="append",
        **_agent_overrides(chat),
    )

    supports_tools = await ollama.model_supports_tools(client, chat.model)
    if supports_tools:
        tool_states, rag_server_states = _chip_states(
            db, chat.id, servers=rag_servers_list
        )
    else:
        tool_states, rag_server_states = [], []

    panel_html = templates.get_template("_chat_panel.html").render(
        conversation=chat,
        blocks=blocks,
        pending_stream_url=f"/chats/{chat.id}/stream",
        active_chat_id=chat.id,
        supports_tools=supports_tools,
        tool_states=tool_states,
        rag_server_states=rag_server_states,
        agents=list_agents(),
        active_agent_spec=agent_spec,
        project=project,
    )

    # OOB-prepended sidebar row — wrapping <ul> for the same reason as
    # the legacy create-chat path: non-outerHTML OOB modes unwrap the
    # root element, so a top-level <li> would lose its styling.
    item_html = templates.get_template("_chat_item.html").render(
        chat=chat,
        active_chat_id=chat.id,
        project=project,
    )
    oob_sidebar_row = (
        f'<ul hx-swap-oob="afterbegin:#chats-list">{item_html}</ul>'
    )

    body = panel_html + oob_sidebar_row
    response = HTMLResponse(
        content=body, status_code=status.HTTP_201_CREATED
    )
    response.headers["HX-Push-Url"] = (
        f"/projects/{project_id}/chats/{chat.id}"
    )
    return response


# ---------------------------------------------------------------------------
# Files tab
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_id}/files", response_class=HTMLResponse
)
def project_files_endpoint(
    project_id: int,
    request: Request,
    db: DB,
    path: str = ".",
) -> Response:
    """Render the project page with the Files tab active.

    ``path`` is a workspace-relative directory; default ``"."`` lists the
    workspace root. Containment + missing-directory cases are handled by
    :func:`_browse_workspace` and surface as in-page error text.

    Raises:
        HTTPException 404: When the project does not exist.
    """
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    listing = _browse_workspace(project, path)
    return _render_project_page(
        request,
        db=db,
        project=project,
        active_tab="files",
        extra={
            "files_ctx": {"project": project, "listing": listing},
        },
    )


@router.get(
    "/projects/{project_id}/files/view", response_class=HTMLResponse
)
def project_file_view_endpoint(
    project_id: int,
    request: Request,
    db: DB,
    path: str,
) -> Response:
    """Render a single workspace file in the Files tab.

    Markdown files get pre-rendered via the ``markdown`` library; all
    other text files render as a ``<pre>`` block. Binary files surface a
    "use Download" message rather than corrupted byte output.

    Raises:
        HTTPException 404: When the project does not exist.
    """
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    view = _read_workspace_file(project, path)
    return _render_project_page(
        request,
        db=db,
        project=project,
        active_tab="files",
        extra={
            "files_ctx": {"project": project, "view": view},
        },
    )


@router.get("/projects/{project_id}/files/download")
def project_file_download_endpoint(
    project_id: int, db: DB, path: str
) -> Response:
    """Stream a workspace file to the browser as an attachment.

    Validates containment (``..`` traversal / absolute paths are
    rejected) and existence; uses ``Content-Disposition: attachment``
    so browsers save instead of inlining.

    Raises:
        HTTPException 400: When file tools are not configured.
        HTTPException 404: When the project does not exist, the path
            escapes the workspace, or the file doesn't exist.
    """
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    root = project_workspace_root(project)
    if root is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "File tools not configured."
        )
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    return FileResponse(
        target,
        filename=target.name,
        media_type="application/octet-stream",
    )


# ---------------------------------------------------------------------------
# Project Settings tab
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_id}/settings", response_class=HTMLResponse
)
def project_settings_endpoint(
    project_id: int,
    request: Request,
    db: DB,
) -> Response:
    """Render the project page with the Settings tab active.

    Raises:
        HTTPException 404: When the project does not exist.
    """
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return _render_project_page(
        request,
        db=db,
        project=project,
        active_tab="settings",
        extra={
            "settings_ctx": {
                "project": project,
                "agents": list_agents(),
                "saved": False,
                "global_default_num_ctx": queries.get_default_num_ctx(db),
            },
        },
    )
