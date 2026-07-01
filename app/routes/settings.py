"""Global settings page — RAG server CRUD and default-* settings.

Routes:
    GET    /settings                            — settings page
    POST   /settings/servers                    — add RAG server
    POST   /settings/sync-descriptions          — sync descriptions from /sources
    GET    /settings/servers/{id}               — fetch row (view or edit)
    PATCH  /settings/servers/{id}               — update description
    DELETE /settings/servers/{id}               — remove RAG server
    POST   /settings/agents                     — add reusable agent (persona)
    GET    /settings/agents/{id}                — fetch agent row (view or edit)
    PATCH  /settings/agents/{id}                — update agent
    DELETE /settings/agents/{id}                — remove agent
    PATCH  /settings/default-temperature        — set global default temp
    PATCH  /settings/default-tool-cap           — set global default tool cap
    PATCH  /settings/default-num-ctx            — set global default num_ctx
    PATCH  /settings/default-model              — set global default model
"""

import html
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse

from app import queries
from app import rag_servers as _rag_servers
from app.dependencies import DB
from app.rag_health import probe_rag_health
from app.rag_sources import description_for, fetch_sources, host_root
from app.routes._helpers import _sidebar_reference_context, _sidebar_reference_oob
from app.templates import templates
from app.tools.rag import refresh_query_rag_registration

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_endpoint(request: Request, db: DB) -> Response:
    """Settings page — RAG servers + default temperature/tool-cap/num-ctx/model.

    Direct browser hits return the full index shell with the settings
    fragment preloaded (so reload / bookmarks land on the same view).
    HTMX requests get just the fragment for a cheap swap into ``#main``.
    """
    servers = _rag_servers.list_servers(db)
    default_temperature = queries.get_default_temperature(db)
    default_tool_iteration_cap = queries.get_default_tool_iteration_cap(db)
    default_model = queries.get_default_model(db)
    default_num_ctx = queries.get_default_num_ctx(db)
    settings_ctx = {
        "servers": servers,
        "default_temperature": default_temperature,
        "default_tool_iteration_cap": default_tool_iteration_cap,
        "default_model": default_model,
        "default_num_ctx": default_num_ctx,
        # Phase 29: reusable agents (personas) + their field caps, surfaced to
        # the create form + inline-edit rows so maxlength stays in sync.
        "agents": queries.list_agents(db),
        "agent_name_max_chars": queries.AGENT_NAME_MAX_CHARS,
        "system_prompt_max_chars": queries.SYSTEM_PROMPT_MAX_CHARS,
    }
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_settings.html",
            context=settings_ctx,
        )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            # Settings is its own top-level layout: the unified sidebar
            # (project list + Settings nav) renders alongside the settings
            # fragment in the main slot.
            "layout": "settings",
            "project": None,
            "conversation": None,
            "active_chat_id": None,
            "settings_view": True,
            "projects": queries.list_projects(db),
            "active_project_id": None,
            # Always-visible sidebar reference lists.
            **_sidebar_reference_context(db),
            # Aliased so the index template's `{% set servers = rag_servers %}`
            # adapter resolves it for the included _settings.html fragment.
            "rag_servers": servers,
            **{k: v for k, v in settings_ctx.items() if k != "servers"},
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

    Probes ``/health`` BEFORE inserting so a row only lands in SQLite if
    the named database is reachable; a failed probe returns 502 plain text
    that the form's ``after-request`` JS pipes into the inline error region.

    A UNIQUE-constraint collision on the name surfaces as ``IntegrityError``,
    mapped to 409. HTMX won't swap a non-2xx response, so the list stays
    intact and the form keeps the user's typed values.

    On success, ``refresh_query_rag_registration`` makes the next chat
    turn's tool spec reflect the new source.
    """
    name_clean = name.strip()
    url_clean = url.strip()
    # The textarea's maxlength is a client-side hint only; truncate
    # server-side as belt-and-suspenders.
    description_clean = description.strip()[:400]

    healthy, reason = await probe_rag_health(name_clean, url_clean)
    if not healthy:
        return HTMLResponse(
            reason,
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    try:
        server = _rag_servers.create_server(
            db, name=name_clean, url=url_clean, description=description_clean
        )
    except sqlite3.IntegrityError:
        return HTMLResponse(
            f"Server name '{html.escape(name_clean)}' already in use.",
            status_code=status.HTTP_409_CONFLICT,
        )
    refresh_query_rag_registration()
    # Append an OOB re-render of the sidebar "Sources" list so the new
    # server shows there immediately, not just in the settings list.
    row_html = templates.get_template("_rag_server_row.html").render(
        request=request, server=server
    )
    return HTMLResponse(row_html + _sidebar_reference_oob(db))


@router.post("/settings/sync-descriptions", response_class=HTMLResponse)
async def sync_descriptions_endpoint(request: Request, db: DB) -> Response:
    """Overwrite RAG server descriptions from each host's ``/sources`` endpoint.

    Groups configured servers by host root, fetches ``GET /sources`` once per
    distinct host, matches each server by its ``_rag``-stripped name to a
    source ``id``, and rewrites its description to
    ``"<description> (<timeframe>)"``. The remote is treated as the source of
    truth, so a matched row is overwritten even if it had a manual value;
    rows whose description is already current are left untouched (no
    needless ``updated_at`` bump).

    Re-renders the full servers list (every changed row swaps in at once)
    plus an OOB sidebar refresh and an OOB result banner summarizing how many
    rows were updated / left unmatched / sat on an unreachable host.

    On any change, ``refresh_query_rag_registration`` folds the new
    descriptions into the next chat turn's ``query_rag`` source hint.
    """
    servers = _rag_servers.list_servers(db)

    # One /sources fetch per distinct host root; None marks an unreachable
    # (or malformed) host so its servers count as "host unreachable" below.
    sources_by_root: dict[str | None, dict | None] = {}
    for server in servers:
        root = host_root(server.url)
        if root not in sources_by_root:
            sources_by_root[root] = await fetch_sources(server.url)

    updated = 0
    unmatched = 0
    unreachable = 0
    for server in servers:
        sources = sources_by_root.get(host_root(server.url))
        if sources is None:
            unreachable += 1
            continue
        new_description = description_for(server.name, sources)
        if new_description is None:
            unmatched += 1
            continue
        if new_description != server.description:
            _rag_servers.update_server_description(
                db, server.id, new_description
            )
            updated += 1

    if updated:
        refresh_query_rag_registration()

    # Re-read so the re-rendered rows carry the freshly-written descriptions.
    servers = _rag_servers.list_servers(db)
    rows_html = "".join(
        templates.get_template("_rag_server_row.html").render(
            request=request, server=server, editing=False
        )
        for server in servers
    )
    result_html = templates.get_template("_sync_result.html").render(
        request=request,
        updated=updated,
        unmatched=unmatched,
        unreachable=unreachable,
    )
    return HTMLResponse(rows_html + result_html + _sidebar_reference_oob(db))


@router.delete(
    "/settings/servers/{server_id}",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
def delete_server_endpoint(server_id: int, db: DB) -> Response:
    """Delete a RAG server; return 200 for ``hx-swap="delete"``.

    Idempotent at the query layer (missing ids are silently accepted).
    The registration refresh keeps the tool's schema in sync with the
    now-shrunk set of source names.
    """
    _rag_servers.delete_server(db, server_id)
    refresh_query_rag_registration()
    # hx-swap="delete" removes the row and ignores the body, but the OOB
    # fragment still applies, dropping the server from the sidebar list.
    return HTMLResponse(_sidebar_reference_oob(db))


@router.get("/settings/servers/{server_id}", response_class=HTMLResponse)
def get_server_endpoint(
    server_id: int,
    request: Request,
    db: DB,
    edit: bool = False,
) -> Response:
    """Return one RAG server row, in view or edit mode.

    Backs the inline editor: the edit pencil GETs with ``?edit=1`` to swap
    in a form; Cancel GETs without the param to swap back. Both target the
    row's ``<li>`` with ``hx-swap="outerHTML"``.

    A missing id (e.g. another tab deleted the row) returns 404 so HTMX
    leaves the stale row in place rather than blanking it.
    """
    server = _rag_servers.get_server(db, server_id)
    if server is None:
        return Response(content="", status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request=request,
        name="_rag_server_row.html",
        context={"server": server, "editing": edit},
    )


@router.patch("/settings/servers/{server_id}", response_class=HTMLResponse)
async def update_server_endpoint(
    server_id: int,
    request: Request,
    db: DB,
    description: Annotated[str, Form()] = "",
    name: Annotated[str | None, Form()] = None,
    url: Annotated[str | None, Form()] = None,
) -> Response:
    """Update a server's name, URL, and description; return the view-mode row.

    With both ``name`` and ``url`` (full-edit path), re-probes health before
    writing — same guarantee as the add form. A failed probe returns 502;
    a name collision returns 409. With only ``description``, updates that
    field alone (no re-probe).

    Truncates description to 400 chars server-side. A missing id returns
    404 so a stale row from another tab's delete isn't replaced.
    """
    description_clean = description.strip()[:400]
    name_clean = name.strip() if name else None
    url_clean = url.strip() if url else None

    if name_clean and url_clean:
        healthy, reason = await probe_rag_health(name_clean, url_clean)
        if not healthy:
            return HTMLResponse(reason, status_code=status.HTTP_502_BAD_GATEWAY)
        try:
            server = _rag_servers.update_server(
                db, server_id, name_clean, url_clean, description_clean
            )
        except sqlite3.IntegrityError:
            return HTMLResponse(
                f"Server name '{html.escape(name_clean)}' already in use.",
                status_code=status.HTTP_409_CONFLICT,
            )
    else:
        server = _rag_servers.update_server_description(
            db, server_id, description_clean
        )

    if server is None:
        return Response(content="", status_code=status.HTTP_404_NOT_FOUND)
    refresh_query_rag_registration()
    # OOB-refresh the sidebar: a rename changes the chip label, an edited
    # description changes its hover title.
    row_html = templates.get_template("_rag_server_row.html").render(
        request=request, server=server, editing=False
    )
    return HTMLResponse(row_html + _sidebar_reference_oob(db))


# ---------------------------------------------------------------------------
# Phase 29: reusable agents (personas) CRUD — mirrors the RAG-server endpoints
# above (add / get row / update / delete), folded into the same /settings page.
# ---------------------------------------------------------------------------


def _render_agent_row(request: Request, agent, *, editing: bool = False) -> str:
    """Render one ``_agent_row.html`` with the field-cap context it needs.

    Single place that threads ``AGENT_NAME_MAX_CHARS`` /
    ``SYSTEM_PROMPT_MAX_CHARS`` into the row's ``maxlength`` attrs, so every
    render site (POST / GET / PATCH) stays in sync.
    """
    return templates.get_template("_agent_row.html").render(
        request=request,
        agent=agent,
        editing=editing,
        agent_name_max_chars=queries.AGENT_NAME_MAX_CHARS,
        system_prompt_max_chars=queries.SYSTEM_PROMPT_MAX_CHARS,
    )


@router.post("/settings/agents", response_class=HTMLResponse)
async def add_agent_endpoint(
    request: Request,
    db: DB,
    name: Annotated[str, Form()],
    system_prompt: Annotated[str, Form()] = "",
    default_model: Annotated[str, Form()] = "",
) -> Response:
    """Add a reusable agent; return the new row for ``hx-swap="beforeend"``.

    An empty/whitespace name returns 422 and a duplicate name returns 409;
    both are plain-text bodies the ``.agent-form`` branch in app.js pipes into
    ``#agent-form-error``. HTMX won't swap a non-2xx, so the list stays intact
    and the form keeps the typed values. ``system_prompt`` / ``name`` are
    clamped server-side; an empty ``default_model`` stores NULL.
    """
    name_clean = name.strip()[:queries.AGENT_NAME_MAX_CHARS]
    if not name_clean:
        return HTMLResponse(
            "Agent name is required.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    system_prompt_clean = system_prompt.strip()[:queries.SYSTEM_PROMPT_MAX_CHARS]
    default_model_clean = default_model.strip() or None

    try:
        agent = queries.create_agent(
            db,
            name=name_clean,
            system_prompt=system_prompt_clean,
            default_model=default_model_clean,
        )
    except sqlite3.IntegrityError:
        return HTMLResponse(
            f"Agent name '{html.escape(name_clean)}' already in use.",
            status_code=status.HTTP_409_CONFLICT,
        )
    return HTMLResponse(_render_agent_row(request, agent))


@router.get("/settings/agents/{agent_id}", response_class=HTMLResponse)
def get_agent_endpoint(
    agent_id: int,
    request: Request,
    db: DB,
    edit: bool = False,
) -> Response:
    """Return one agent row, in view or edit mode.

    Backs the inline editor: the edit pencil GETs with ``?edit=1`` to swap in
    a form; Cancel GETs without it to swap back. A missing id returns 404 so
    HTMX leaves the stale row in place rather than blanking it.
    """
    try:
        agent = queries.get_agent(db, agent_id)
    except LookupError:
        return Response(content="", status_code=status.HTTP_404_NOT_FOUND)
    return HTMLResponse(_render_agent_row(request, agent, editing=edit))


@router.patch("/settings/agents/{agent_id}", response_class=HTMLResponse)
async def update_agent_endpoint(
    agent_id: int,
    request: Request,
    db: DB,
    name: Annotated[str, Form()],
    system_prompt: Annotated[str, Form()] = "",
    default_model: Annotated[str, Form()] = "",
) -> Response:
    """Update an agent's name, system prompt, and preferred model.

    Returns the view-mode row on success. An empty name returns 422; a name
    collision with another agent returns 409; a missing id returns 404 (a
    stale row from another tab's delete isn't replaced). An empty
    ``default_model`` clears it (stores NULL).
    """
    name_clean = name.strip()[:queries.AGENT_NAME_MAX_CHARS]
    if not name_clean:
        return HTMLResponse(
            "Agent name is required.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    system_prompt_clean = system_prompt.strip()[:queries.SYSTEM_PROMPT_MAX_CHARS]
    default_model_clean = default_model.strip() or None

    try:
        agent = queries.update_agent(
            db,
            agent_id,
            name=name_clean,
            system_prompt=system_prompt_clean,
            default_model=default_model_clean,
        )
    except LookupError:
        return Response(content="", status_code=status.HTTP_404_NOT_FOUND)
    except sqlite3.IntegrityError:
        return HTMLResponse(
            f"Agent name '{html.escape(name_clean)}' already in use.",
            status_code=status.HTTP_409_CONFLICT,
        )
    return HTMLResponse(_render_agent_row(request, agent))


@router.delete(
    "/settings/agents/{agent_id}",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
def delete_agent_endpoint(agent_id: int, db: DB) -> Response:
    """Delete an agent; return 200 for ``hx-swap="delete"``.

    Idempotent at the query layer (missing ids are silently accepted). Any
    chat pointing at it reverts to Normal via ``ON DELETE SET NULL``.
    """
    queries.delete_agent(db, agent_id)
    # hx-swap="delete" removes the row and ignores the (empty) body.
    return HTMLResponse(content="")


@router.patch(
    "/settings/default-temperature",
    response_class=Response,
)
async def set_default_temperature_endpoint(
    db: DB,
    temperature: Annotated[float, Form()],
) -> Response:
    """Persist the global default sampling temperature for new chats.

    Driven by the ``<input>`` in ``_settings.html`` (``hx-patch`` on
    ``change``). Clamps to [0.0, 2.0] server-side against hand-crafted
    requests. Only affects chats created after the change.

    Returns 204 — the input already shows the typed value, no swap needed.
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

    Driven by the ``<input>`` in ``_settings.html`` (``hx-patch`` on
    ``change``). Clamps to [1, 10] server-side against hand-crafted
    requests. Only affects chats created after the change.

    Returns 204 — the input already shows the typed value, no swap needed.
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

    Driven by the ``<input>`` in ``_settings.html`` (``hx-patch`` on
    ``change``). Clamps to [NUM_CTX_MIN, NUM_CTX_MAX] server-side against
    hand-crafted requests. Takes effect on the next turn of any chat
    without a project-level override.

    Returns 204 — the input already shows the typed value, no swap needed.
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
