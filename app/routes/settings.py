"""Global settings page — RAG server CRUD and default-* settings.

Routes:
    GET    /settings                            — settings page
    POST   /settings/servers                    — add RAG server
    GET    /settings/servers/{id}               — fetch row (view or edit)
    PATCH  /settings/servers/{id}               — update description
    DELETE /settings/servers/{id}               — remove RAG server
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
from app.agents import list_agents
from app.dependencies import DB
from app.rag_health import probe_rag_health
from app.templates import templates
from app.tools.rag import refresh_query_rag_registration

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_endpoint(request: Request, db: DB) -> Response:
    """Standalone settings page — RAG servers + default temperature + default tool cap.

    Direct browser hits return the full index shell with the settings
    fragment preloaded in the main slot (so reload / bookmarks land on
    the same view). HTMX requests get just the fragment, sized for a
    cheap swap into ``#main``.
    """
    servers = _rag_servers.list_servers(db)
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
        server = _rag_servers.create_server(
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
    _rag_servers.delete_server(db, server_id)
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
    server = _rag_servers.get_server(db, server_id)
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
    server = _rag_servers.update_server_description(
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
