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
from app.hosts import get_host, get_primary_host, list_hosts, UnknownHostError
from app.dependencies import DB, OllamaClient
from app.projects import ensure_project_workspace
from app.routes._helpers import (
    _host_overrides,
    _composer_host_context,
    _effective_model,
    _placeholder_name,
    _project_context,
    _resolve_active_host,
    _sidebar_reference_context,
)
from app.templates import templates

router = APIRouter()


@router.get("/projects", response_class=HTMLResponse)
def list_projects_endpoint(request: Request, db: DB) -> Response:
    """Render the projects index page (the app home; ``GET /`` 302s here).

    HTMX requests get just the projects-index fragment for a cheap
    main-panel swap; full hits get the layout with sidebar.
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
            # No project is "current" on the index page.
            "active_project_id": None,
            "project": None,
            "conversation": None,
            "active_chat_id": None,
            **_sidebar_reference_context(db),
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

    Name must be unique; on conflict returns 409 (HTMX leaves the form
    intact on non-2xx). Eagerly creates the on-disk workspace so the Files
    tab works before the first tool call lands there.
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
    # Render the main-panel tile AND OOB-prepend a row into the sidebar's
    # #projects-list so the new project appears without a full reload.
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
    """Canonical entry: /projects/{id} → /projects/{id}/chats (default tab).

    Does NOT verify the project exists — the redirect target 404s if not.
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

    The settings form posts ALL fields on submit, so we read FormData
    directly to distinguish "field omitted" (sentinel — leave alone) from
    "field submitted empty" (clear to NULL).

    Raises:
        HTTPException 404: When the project does not exist.
    """
    try:
        queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    form = await request.form()

    # Name / description: passing None leaves them alone (the form always
    # sends them, since the textbox can't be omitted client-side).
    name = form.get("name")
    description = form.get("description")

    def _string_or_clear(key: str):
        """Map a form field to update_project's value.

        Absent → sentinel (don't touch); empty/whitespace → None (clear to
        NULL); else the trimmed string.
        """
        if key not in form:
            return queries._UNSET
        raw = form.get(key, "")
        s = raw.strip() if isinstance(raw, str) else ""
        return s if s else None

    def _int_or_clear(key: str):
        """Mirror of ``_string_or_clear`` for integer fields like num_ctx.

        Absent → sentinel; empty/whitespace → None (inherit global);
        non-numeric → sentinel (untouched, rather than crash the save on a
        stray ``"abc"``); else the parsed int (clamped in update_project).
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

    # Per-project system prompt. Trim + cap server-side (the textarea caps
    # client-side too, but a hand-rolled POST would bypass it).
    # Absent field → None, so update_project leaves the existing value alone.
    raw_prompt = form.get("system_prompt") if "system_prompt" in form else None
    if raw_prompt is None:
        system_prompt_arg: str | None = None
    else:
        s = raw_prompt.strip() if isinstance(raw_prompt, str) else ""
        system_prompt_arg = s[: queries.SYSTEM_PROMPT_MAX_CHARS]

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
    # The project name also lives in the page header and the sidebar, both
    # outside #project-page-body. Append OOB swaps for both so a rename lands
    # everywhere on one response. Idempotent, so always include them.
    settings_html = templates.get_template(
        "_project_settings_body.html"
    ).render(
        project=project,
        saved=True,
        hosts=list_hosts(),
        global_default_num_ctx=queries.get_default_num_ctx(db),
        system_prompt_max_chars=queries.SYSTEM_PROMPT_MAX_CHARS,
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
    """Delete a project (cascading its chats); refuses the last one.

    Returns 409 when this would leave zero projects — the app needs at
    least one as a home view. The on-disk workspace is PRESERVED so the
    user can recover its files.

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
    # Use HX-Redirect, not HX-Location: HX-Location does an ajax GET and
    # swaps the /projects fragment into <body>, wiping the sidebar.
    # HX-Redirect sets window.location, so the browser navigates for real
    # and /projects renders as a full page with the sidebar intact.
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
        extra: Per-tab context merged into the template context (e.g.
            ``conversation``, ``files_ctx``, ``settings_ctx``).
    """
    chats = queries.list_conversations_in_project(db, project.id)
    base = {
        "project": project,
        "active_tab": active_tab,
        "chats": chats,
        # None highlights no sidebar row (empty-state / Files / Settings).
        "active_chat_id": None,
        # Hosts + defaults, so included partials don't re-fetch.
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
    # Full-page renders include the sidebar (projects list + active row id).
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "layout": "project",
            "projects": queries.list_projects(db),
            "active_project_id": project.id,
            **_sidebar_reference_context(db),
            **base,
        },
    )


@router.get("/projects/{project_id}/chats", response_class=HTMLResponse)
def project_chats_endpoint(
    project_id: int, request: Request, db: DB
) -> Response:
    """Render the project page, Chats tab active, no chat open.

    Shows the empty-state composer (POST goes to /projects/{pid}/chats).
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
            # Pre-fill hooks so the project default propagates into the
            # composer's model / host selects.
            "project_default_model": project.default_model,
            "project_default_agent": project.default_agent,
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
    """HTMX-only: the sidebar "+ New chat" link.

    Returns just the composer fragment. Cheaper than a full project-chats
    render, and leaves the chats list's scroll position untouched.

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

    404s if the chat belongs to a different project rather than rendering
    it as if it were under this one.

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

    # So a reload during an in-flight generation attaches as a fresh
    # consumer (mirrors the legacy get_chat_panel_endpoint).
    pending_stream_url = None
    live = generation.live_generations.get(conversation_id)
    if live is not None and not live.done:
        if blocks and blocks[-1].kind == "tool_batch":
            blocks = blocks[:-1]
        pending_stream_url = f"/chats/{conversation_id}/stream"

    # Resolve through the toggle so a disabled remote host renders as the
    # primary (matches the indicator + generation path).
    active_host_spec = _resolve_active_host(conversation, db)

    # Effective model = the chat's per-host model on its selected host, else
    # its pinned model — same rule the indicator uses.
    effective_model = _effective_model(conversation, active_host_spec, db)
    effective_host = (
        active_host_spec.ollama_host if active_host_spec else None
    )

    # Gate the header chips on the effective model + host (what actually
    # runs this turn). A model that only exists on a non-primary host must
    # be probed against THAT host, or the local /api/show 404s and the chip
    # wrongly hides.
    supports_tools = await ollama.model_supports_tools(
        client, effective_model, host=effective_host
    )
    supports_thinking = await ollama.model_supports_thinking(
        client, effective_model, host=effective_host
    )

    model_loaded = await ollama.is_model_loaded(
        client, effective_model, host=effective_host
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
            "supports_thinking": supports_thinking,
            "active_host_spec": active_host_spec,
            "effective_model": effective_model,
            "model_loaded": model_loaded,
            # `agents` (the picker options) comes from _project_context; this
            # resolves the currently-attached agent for the header chip.
            "conversation_agent": queries.get_agent_for_conversation(
                db, conversation.id
            ),
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
    think_mode: Annotated[str | None, Form()] = None,
    host: Annotated[str | None, Form()] = None,
) -> Response:
    """Create a chat inside a project AND save its first message.

    Persists the chat, spawns the generation task, and returns the chat
    panel + OOB sidebar row in one body. ``HX-Push-Url`` syncs the address
    bar to the canonical project-scoped URL.

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
    # Coerce unknown values to 'default' so a stale/hand-crafted think_mode
    # can't resolve to think=true and 400 a non-thinking model.
    if think_mode not in {"default", "off"}:
        think_mode = "default"
    try:
        host_spec = get_host(host)
    except UnknownHostError:
        # Stale composer post (host removed since the page rendered) → primary.
        host_spec = get_primary_host()
    # The `model` field is for the SELECTED host. On the primary host it IS
    # conversations.model; on a non-primary host it lives in chat_host_models
    # and conversations.model (NOT NULL) keeps a primary fallback, so
    # switching to primary mid-chat still has a valid model.
    if host_spec.is_primary:
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
        think_mode=think_mode,
        active_host=None if host_spec.is_primary else host_spec.name,
    )
    # Remember the picked model for the non-primary host. Empty ("" before
    # /models loads) → skip, so the host uses its default_model.
    if not host_spec.is_primary and model:
        queries.set_chat_host_model(db, chat.id, host_spec.name, model)
    queries.append_message(db, chat.id, "user", content)

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
        **_host_overrides(chat, db),
    )

    # Toggle-aware host spec for the header chip (a disabled remote host
    # renders as the primary), matching the generation path's resolution.
    active_spec = _resolve_active_host(chat, db)

    # Gate the header chips on the effective model + host (what actually
    # runs); see project_chat_panel_endpoint for the non-primary-host probe.
    effective_model = _effective_model(chat, active_spec, db)
    effective_host = active_spec.ollama_host if active_spec else None
    supports_tools = await ollama.model_supports_tools(
        client, effective_model, host=effective_host
    )
    supports_thinking = await ollama.model_supports_thinking(
        client, effective_model, host=effective_host
    )

    panel_html = templates.get_template("_chat_panel.html").render(
        conversation=chat,
        blocks=blocks,
        # A brand-new chat has no archived rows, but pass 0 explicitly so
        # the template's `archived_count is defined` check is symmetric with
        # the chat-panel-load path.
        archived_count=0,
        pending_stream_url=f"/chats/{chat.id}/stream",
        active_chat_id=chat.id,
        supports_tools=supports_tools,
        supports_thinking=supports_thinking,
        hosts=list_hosts(),
        active_host_spec=active_spec,
        effective_model=effective_model,
        # start_generation is (re)loading the model right now, so skip the
        # /api/ps round trip and render the chip in its loaded colour.
        model_loaded=True,
        project=project,
        # Agent picker options + the (empty) current selection: a brand-new
        # chat has no agent, so the header chip stays clean.
        agents=queries.list_agents(db),
        conversation_agent=None,
    )

    # OOB-prepended sidebar row, wrapped in <ul>: non-outerHTML OOB modes
    # unwrap the root element, so a bare <li> would lose its styling.
    item_html = templates.get_template("_chat_item.html").render(
        chat=chat,
        active_chat_id=chat.id,
        project=project,
    )
    oob_sidebar_row = (
        f'<ul hx-swap-oob="afterbegin:#chats-list">{item_html}</ul>'
    )

    # The sidebar reference lists are chat-independent, so a new chat needs
    # no sidebar OOB beyond its row.
    body = panel_html + oob_sidebar_row
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
                "hosts": list_hosts(),
                "saved": False,
                "global_default_num_ctx": queries.get_default_num_ctx(db),
                "system_prompt_max_chars": queries.SYSTEM_PROMPT_MAX_CHARS,
            },
        },
    )
