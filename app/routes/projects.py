"""Project-level routes: project CRUD + tabs (chats, settings).

Routes:
    GET    /projects                                   — project index
    POST   /projects                                   — create project
    GET    /projects/{id}                              — redirect to chats tab
    PATCH  /projects/{id}                              — update project
    DELETE /projects/{id}                              — delete project
    GET    /projects/{id}/chats                        — chats tab (empty state)
    GET    /projects/{id}/chats/new                    — composer fragment
    GET    /projects/{id}/chats/{cid}                  — chats tab (open chat)
    POST   /projects/{id}/chats                        — create chat
    GET    /projects/{id}/settings                     — settings tab

Files-tab routes live in :mod:`app.routes.files`.
"""

import html
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app import generation, ollama, queries, render
from app import rag_servers as _rag_servers
from app.agents import get_agent, list_agents
from app.dependencies import DB, OllamaClient
from app.projects import ensure_project_workspace
from app.routes._helpers import (
    _agent_overrides,
    _chip_states,
    _composer_host_context,
    _effective_model,
    _placeholder_name,
    _project_context,
    _resolve_active_spec,
    _sidebar_rag_context,
    _spawn_health_refresh,
)
from app.tools import TOOLS
from app.templates import templates

router = APIRouter()


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
    # in the sidebar without a full reload.
    tile_html = templates.get_template("_project_item.html").render(
        project=project
    )
    sidebar_row_html = templates.get_template(
        "_project_sidebar_row.html"
    ).render(project=project, oob=True)
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

    # Per-project system prompt. The form always submits the field, even
    # when blank. Trim + cap at 200 chars (the textarea enforces the cap
    # client-side too, but a hand-rolled POST would bypass that). When
    # the field is absent entirely (e.g. legacy form), pass None so
    # update_project leaves the existing value alone.
    raw_prompt = form.get("system_prompt") if "system_prompt" in form else None
    if raw_prompt is None:
        system_prompt_arg: str | None = None
    else:
        s = raw_prompt.strip() if isinstance(raw_prompt, str) else ""
        system_prompt_arg = s[:200]

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
        system_prompt=system_prompt_arg,
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
    header_oob = templates.get_template(
        "_project_header_oob.html"
    ).render(project=project)
    sidebar_link_oob = templates.get_template(
        "_project_sidebar_link_oob.html"
    ).render(project=project)
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
        # Phase 19: sidebar Sources section defaults — overridden via
        # `extra` on chat-panel routes that compute the real values.
        # Always present so templates can read them without |default
        # gymnastics, and so the partial renders an empty (invisible)
        # wrapper as a stable OOB target on chat-less pages.
        "active_conversation": None,
        "active_chat_supports_tools": False,
        "active_chat_agent_active": False,
        "rag_server_states": [],
        "rag_health": {},
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
            # Initial host + model-default for the single model dropdown.
            **_composer_host_context(db, project),
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
            **_composer_host_context(db, project),
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
    archived_count = render.count_archived_blocks(messages)

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

    # Resolve through the toggle so a disabled remote host renders as the
    # primary (matches the indicator + generation path).
    active_agent_spec = _resolve_active_spec(conversation, db)

    # Reflect Ollama's actual memory state in the header chip. The "effective"
    # model is the chat's per-host model on its selected host, else the chat's
    # pinned model — same rule the indicator uses to decide what to render.
    effective_model = _effective_model(conversation, active_agent_spec, db)
    effective_host = (
        active_agent_spec.ollama_host if active_agent_spec else None
    )
    model_loaded = await ollama.is_model_loaded(
        client, effective_model, host=effective_host
    )

    sidebar_ctx = await _sidebar_rag_context(
        db, conversation, supports_tools=supports_tools
    )

    return _render_project_page(
        request,
        db=db,
        project=project,
        active_tab="chats",
        extra={
            "conversation": conversation,
            "active_chat_id": conversation.id,
            "blocks": blocks,
            "archived_count": archived_count,
            "pending_stream_url": pending_stream_url,
            "supports_tools": supports_tools,
            "tool_states": tool_states,
            "rag_server_states": rag_server_states,
            "active_agent_spec": active_agent_spec,
            "effective_model": effective_model,
            "model_loaded": model_loaded,
            **sidebar_ctx,
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
    # The single `model` field is the model for the SELECTED host. On the
    # primary host it IS conversations.model. On a non-primary host it belongs
    # in chat_host_models; conversations.model (NOT NULL) keeps a sensible
    # primary fallback so switching to primary mid-chat still has a valid model.
    if agent_spec is None:
        primary_model = model
    else:
        primary_model = (
            project.default_model or queries.get_default_model(db) or model
        )
    chat = queries.create_conversation(
        db,
        name=_placeholder_name(content),
        model=primary_model,
        project_id=project_id,
        temperature=temperature,
        tool_iteration_cap=tool_iteration_cap,
        active_agent=agent_spec.name if agent_spec else None,
    )
    # Remember the picked model for the non-primary host. Empty ("" before
    # /models loads) → skip, so the host falls back to its default_model.
    if agent_spec is not None and model:
        queries.set_chat_host_model(db, chat.id, agent_spec.name, model)
    queries.append_message(db, chat.id, "user", content)

    form_data = await request.form()
    enabled_tools_raw = form_data.getlist("enabled_tools")
    enabled_names: set[str] | None = (
        set(enabled_tools_raw) if enabled_tools_raw else None
    )
    queries.seed_chat_tools(
        db, chat.id, list(TOOLS), enabled_names=enabled_names
    )

    enabled_rag_raw = form_data.getlist("enabled_rag_servers")
    enabled_rag: set[str] | None = (
        set(enabled_rag_raw) if enabled_rag_raw else None
    )
    rag_servers_list = _rag_servers.list_servers(db)
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
        **_agent_overrides(chat, db),
    )
    # Phase 19: warm the RAG health cache so a freshly-created chat's
    # sidebar Sources section reflects current server health.
    _spawn_health_refresh(db)

    supports_tools = await ollama.model_supports_tools(client, chat.model)
    if supports_tools:
        tool_states, rag_server_states = _chip_states(
            db, chat.id, servers=rag_servers_list
        )
    else:
        tool_states, rag_server_states = [], []

    # Toggle-aware host spec for the header chip (a disabled remote host
    # renders as the primary), matching the generation path's resolution.
    active_spec = _resolve_active_spec(chat, db)

    panel_html = templates.get_template("_chat_panel.html").render(
        conversation=chat,
        blocks=blocks,
        # Phase 18: a brand-new chat can't have archived rows yet (no
        # compaction is possible), but pass 0 explicitly so the template's
        # `archived_count is defined` check is symmetric with the chat-
        # panel-load path.
        archived_count=0,
        pending_stream_url=f"/chats/{chat.id}/stream",
        active_chat_id=chat.id,
        supports_tools=supports_tools,
        tool_states=tool_states,
        rag_server_states=rag_server_states,
        agents=list_agents(),
        active_agent_spec=active_spec,
        effective_model=_effective_model(chat, active_spec, db),
        # Brand-new chat: we just kicked off start_generation, which is
        # (re)loading the effective model right now. Skip the /api/ps
        # round trip and render the chip in its loaded colour.
        model_loaded=True,
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

    # Phase 19: OOB-swap the sidebar Sources section so it reflects the new
    # active chat. The pre-existing sidebar in DOM has an empty
    # #sidebar-rag-section wrapper (always-rendered in _sidebar.html);
    # OOB outerHTML-swap with `oob=true` replaces it with the populated
    # section for the new chat.
    sidebar_ctx = await _sidebar_rag_context(
        db, chat, supports_tools=supports_tools, servers=rag_servers_list
    )
    oob_sidebar_rag = templates.get_template(
        "_sidebar_rag_section.html"
    ).render(
        conversation=chat,
        oob=True,
        **sidebar_ctx,
    )

    body = panel_html + oob_sidebar_row + oob_sidebar_rag
    response = HTMLResponse(
        content=body, status_code=status.HTTP_201_CREATED
    )
    response.headers["HX-Push-Url"] = (
        f"/projects/{project_id}/chats/{chat.id}"
    )
    return response


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
