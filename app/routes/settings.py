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
from app.hosts import list_hosts
from app.config import extra_ollama_hosts
from app.dependencies import DB
from app.rag_health import probe_rag_health
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
    hosts = list_hosts()
    # remote_configured gates the toggle vs the "set env vars first" hint;
    # extra_hosts feeds the read-only per-host labels.
    extra_hosts = extra_ollama_hosts()
    remote_configured = bool(extra_hosts)
    remote_enabled = queries.get_remote_ollama_enabled(db)
    settings_ctx = {
        "servers": servers,
        "default_temperature": default_temperature,
        "default_tool_iteration_cap": default_tool_iteration_cap,
        "default_model": default_model,
        "default_num_ctx": default_num_ctx,
        "hosts": hosts,
        "remote_configured": remote_configured,
        "extra_hosts": extra_hosts,
        "remote_enabled": remote_enabled,
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


@router.post(
    "/settings/remote-ollama-enabled",
    response_class=Response,
)
async def set_remote_ollama_enabled_endpoint(
    db: DB,
    enabled: Annotated[str, Form()] = "0",
) -> Response:
    """Persist the app-wide Remote Ollama enable flag.

    Driven by the checkbox in ``_settings.html`` (``hx-post`` on
    ``change``). The field uses ``hx-vals="js:{enabled: this.checked ?
    '1' : '0'}"`` so both states arrive as an explicit "1"/"0" rather
    than relying on checkbox absence — keeps the endpoint dumb and
    idempotent.

    Returns 204 — the checkbox already shows the choice, no swap needed.
    The next chat panel render reflects the new state automatically.
    """
    queries.set_remote_ollama_enabled(db, enabled == "1")
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
