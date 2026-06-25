"""Chat-centric routes.

Routes:
    GET    /                                 — redirect to /projects
    GET    /models                           — model dropdown options
    GET    /chats                            — sidebar list
    GET    /chats/{id}                       — legacy backcompat redirect
    GET    /chats/{id}/edit                  — sidebar row in edit mode
    GET    /chats/{id}/item                  — sidebar row in display mode
    PATCH  /chats/{id}                       — rename
    DELETE /chats/{id}                       — delete
    POST   /chats/{id}/messages              — user msg + assistant placeholder
    GET    /chats/{id}/stream                — SSE assistant stream
    POST   /chats/{id}/regenerate            — regenerate assistant reply
    POST   /chats/{id}/host                  — set/clear selected Ollama host
    POST   /chats/{id}/compact                — summarize older turns
    GET    /chats/{id}/archived               — archived rows for disclosure
    PATCH  /chats/{id}/temperature           — set per-chat temperature
    PATCH  /chats/{id}/tool-iteration-cap    — set per-chat tool cap
    PATCH  /chats/{id}/think-mode            — set per-chat thinking mode
    GET    /backup/status                    — remote-backup status chip
    POST   /backup/pull                      — restore DB + workspaces from mirror
    POST   /backup/push                      — trigger an immediate mirror push
"""

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app import backup, config, generation, ollama, queries, render
from app.hosts import get_host, get_primary_host, UnknownHostError
from app.connection import open_connection
from app.db import initialize_database
from app.dependencies import DB, OllamaClient
from app.ollama import OllamaProtocolError, OllamaUnavailable
from app.routes._helpers import (
    _host_overrides,
    _effective_model,
    _resolve_active_host,
    _resolve_num_ctx,
)
from app.templates import templates

router = APIRouter()


@router.get("/")
def index_endpoint() -> RedirectResponse:
    """Redirect the home URL to the projects index (the app's home)."""
    return RedirectResponse(
        url="/projects", status_code=status.HTTP_302_FOUND
    )


@router.get("/models", response_class=HTMLResponse)
async def list_models_endpoint(
    request: Request,
    client: OllamaClient,
    prepend_blank: bool = False,
    host: str | None = None,
) -> Response:
    """Return ``<option>`` tags for the model dropdown.

    ``host`` is the selected host's NAME (a key in ``app.hosts.HOSTS``).
    Unset/empty (or an unknown name) lists the primary ``OLLAMA_HOST``'s
    models; a known name lists that host's. Powers re-fetching the dropdown
    when the user switches machines.

    Filtered to models whose ``/api/show`` capabilities advertise ``"tools"``:
    a non-tool-capable model 400s on the first message because every turn
    ships ``tools=[...]``. ``list_tool_capable_models`` caches per process, so
    the ``/api/show`` round trips only cost on the first render per 60s window.

    With ``prepend_blank=1`` the list starts with a "(no default — use global)"
    option; Project Settings uses it so clearing the default posts an empty
    value (persisted as NULL via the _UNSET sentinel).

    On Ollama failure returns 200 with a single disabled ``<option>`` carrying
    the message, NOT a 5xx: HTMX won't swap the dropdown on a non-2xx, leaving
    it stuck at "Loading models…". The disabled option still blocks submission
    (empty value + ``required``) while showing the user what's wrong.
    """
    # Resolve the host NAME to its base URL: known non-primary → its
    # ollama_host, primary → None (local). A stale dropdown may post a
    # since-removed name — treat that as primary rather than 500.
    try:
        spec = get_host(host)
    except UnknownHostError:
        spec = get_primary_host()
    target_host = spec.ollama_host
    try:
        models = sorted(
            await ollama.list_tool_capable_models(client, host=target_host)
        )
        # Which models can think — tags each option so the composer's Think
        # select shows/hides as the model changes.
        thinking_models = set(
            await ollama.list_thinking_capable_models(client, host=target_host)
        )
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
            "thinking_models": thinking_models,
            "error": None,
            "prepend_blank": prepend_blank,
        },
    )


@router.get("/chats", response_class=HTMLResponse)
def list_chats_endpoint(request: Request, db: DB) -> Response:
    """Render the sidebar list of conversations."""
    return templates.TemplateResponse(
        request=request,
        name="_chats_list.html",
        # None here: this refreshes the sidebar standalone, with no
        # conversation context. The page owning the URL sets the highlight.
        context={
            "chats": queries.list_conversations(db),
            "active_chat_id": None,
        },
    )


@router.get("/chats/{conversation_id}")
def chat_redirect_endpoint(conversation_id: int, db: DB) -> RedirectResponse:
    """Backcompat: 302 a legacy ``/chats/{id}`` to its canonical
    project-scoped URL (``/projects/{pid}/chats/{cid}``).

    Keeps old bookmarks + transitional links working.

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
    """Return the sidebar row in edit mode (the rename form).

    The rename button swaps this in (outerHTML on the <li>). Submit PATCHes
    /chats/{id} → display fragment; Cancel triggers GET /chats/{id}/item.
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

    Backs the rename flow's Cancel button: swaps the display fragment back
    over the edit fragment, restoring the row unchanged.
    """
    try:
        chat = queries.get_conversation(db, conversation_id)
        # Pass the owning project so the row's link renders the canonical
        # project-scoped path, not the legacy /chats/{id}.
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
        # Include the project so the row's link is project-scoped (matches
        # the canonical URL the browser is on).
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
    ``hx-swap="delete"`` removes the sidebar row.

    If the user is viewing the chat they just deleted (``Referer`` matches its
    URL), set ``HX-Location`` to navigate to the project's chats tab —
    otherwise they're left on a stale panel whose URL 404s on reload.

    The check is server-side (not client ``window.location``) to dodge a
    timing race: ``hx-swap="delete"`` removes the button's ``<li>`` before
    ``htmx:after-request`` fires, and events to detached elements aren't
    reliably delivered across browsers.
    """
    # Resolve the owning project BEFORE the delete — afterward the join 404s.
    try:
        project = queries.get_project_for_conversation(db, conversation_id)
    except LookupError:
        project = None
    queries.delete_conversation(db, conversation_id)
    response = Response(content="", status_code=status.HTTP_200_OK)
    referer = request.headers.get("Referer", "")
    # Match both the canonical and legacy URLs so a user on a backcompat
    # link still gets redirected.
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

    The placeholder opens an SSE connection to ``/chats/{id}/stream`` on
    insert, which drives the streaming. Splitting save (POST) from stream
    (GET) is the standard HTMX pattern: htmx-ext-sse only opens connections
    via GET-based ``sse-connect``.
    """
    try:
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    user_message = queries.append_message(
        db, conversation_id, "user", content
    )

    # Spawn the generation task NOW so the LLM call is already running by
    # the time the browser opens the SSE connection. The task is owned by
    # `generation.live_generations`, not this request — a reload (client
    # disconnect) won't cancel it; consume_generation attaches a new consumer.
    #
    # Read only the active rows: compact may have archived an earlier prefix
    # into a `summary` row, and re-sending the archived rows would defeat it.
    history = queries.list_active_messages(db, conversation_id)
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
            **_host_overrides(conversation, db),
        )
    except generation.GenerationInProgress:
        # The UI gate (disabled send button) makes this rare; defensive 409
        # for a duplicate POST that slips through.
        return HTMLResponse(
            '<div class="error">A reply is already streaming for this chat.</div>',
            status_code=status.HTTP_409_CONFLICT,
        )
    # User message persisted — push to the remote mirror. Fire-and-forget +
    # debounced; no-ops when unconfigured.
    backup.request_backup("send")

    # Render the user bubble + assistant placeholder as one fragment; the
    # placeholder's `sse-connect` triggers the streaming GET on insert.
    user_html = templates.get_template("_message.html").render(
        message=user_message
    )
    placeholder_html = templates.get_template(
        "_assistant_placeholder.html"
    ).render(
        conversation_id=conversation_id,
        stream_url=f"/chats/{conversation_id}/stream",
    )
    # OOB-swap the header model chip to "loaded": the spawned generation will
    # (re)load the model. No-op if already loaded; flips it back if the user
    # had just clicked unload.
    spec = _resolve_active_host(conversation, db)
    indicator_oob = templates.get_template("_host_indicator.html").render(
        conversation=conversation,
        active_host_spec=spec,
        effective_model=_effective_model(conversation, spec, db),
        model_loaded=True,
        oob=True,
    )
    # OOB-swap the backup chip into its pending state so it starts polling
    # /backup/status (request_backup("send") above already set status to
    # "pending"). Renders empty when backups aren't configured.
    backup_chip_oob = templates.get_template("_backup_chip.html").render(oob=True)
    return HTMLResponse(
        content=user_html + placeholder_html + indicator_oob + backup_chip_oob
    )


@router.get("/chats/{conversation_id}/stream")
async def stream_endpoint(
    conversation_id: int, db: DB, client: OllamaClient
) -> StreamingResponse:
    """SSE stream — attach as a consumer to the live generation if one
    exists, else emit a done event from the persisted assistant row.

    The triggering POST (/messages or /regenerate) spawned the task and
    registered it in `generation.live_generations`; this is a thin dispatcher
    over `consume_generation` (which handles replay/tail). The `consume_finished`
    fallback covers the race where a reload's GET lands after the generation
    finished and left the registry.
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


@router.post(
    "/chats/{conversation_id}/regenerate",
    response_class=HTMLResponse,
)
async def regenerate_endpoint(
    request: Request, conversation_id: int, db: DB, client: OllamaClient
) -> Response:
    """Spawn a regen generation; return a placeholder that replaces the bubble.

    Same shape as send_message_endpoint, but ``on_complete="replace"`` so the
    existing assistant row is overwritten in place. The placeholder's
    ``sse-connect`` points at ``/chats/{id}/stream`` (the shared stream endpoint).
    """
    try:
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    # Operate on active rows only. The assistant row being replaced is by
    # definition active, so the "last active row must be assistant" gate holds.
    history = queries.list_active_messages(db, conversation_id)
    if not history or history[-1].role != "assistant":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No assistant message to regenerate.",
        )

    # Drop the last assistant message so Ollama generates fresh rather than
    # seeing its own previous output.
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
            **_host_overrides(conversation, db),
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
    # Same chip-reset as send_message: regen reloads the model too.
    spec = _resolve_active_host(conversation, db)
    indicator_oob = templates.get_template("_host_indicator.html").render(
        conversation=conversation,
        active_host_spec=spec,
        effective_model=_effective_model(conversation, spec, db),
        model_loaded=True,
        oob=True,
    )
    # The aggregated tool card for this turn is a sibling ABOVE the assistant
    # bubble, so the `closest .message` swap above leaves it in the DOM; the
    # regenerated turn then streams a fresh card under a new id, orphaning the
    # old one until a full reload. Delete it by id. Its `turn_id` derives from
    # the FIRST tool_call row in the contiguous tool run right before the
    # assistant row — the same rule `render._build_classic_tool_batch` uses.
    stale_oob = ""
    first_tool_call_id = None
    for m in reversed(history[:-1]):
        if m.role not in ("tool_call", "tool_result"):
            break
        if m.role == "tool_call":
            first_tool_call_id = m.id
    if first_tool_call_id is not None:
        stale_oob = render.render_oob_delete(
            element_id=render.card_id_for(f"hist-{first_tool_call_id}")
        )
    # The historic thinking card (phase 28) is likewise a sibling above the
    # bubble, untouched by the `closest .message` swap; the new turn streams a
    # fresh card under a different id, stacking two until reload. Delete the
    # stale one by id. Conditional, so a no-thinking regenerate emits nothing.
    regen_row = history[-1]
    if regen_row.thinking:
        stale_oob += render.render_oob_delete(
            element_id=render.thinking_card_id_for(f"hist-{regen_row.id}")
        )
    return HTMLResponse(content=placeholder_html + stale_oob + indicator_oob)


@router.post("/chats/{conversation_id}/host", response_class=HTMLResponse)
async def set_chat_host_endpoint(
    conversation_id: int,
    db: DB,
    client: OllamaClient,
    host: Annotated[str | None, Form()] = None,
) -> Response:
    """Set/clear the selected Ollama host for a chat; return OOB UI updates.

    Called by the in-chat host dropdown on change (``hx-post`` +
    ``hx-swap="none"`` — response is OOB-only). The selection persists across
    reloads and subsequent turns. ``host`` is the host name, or empty/None for
    the primary host; a stale name (since-removed) resolves to primary.

    OOB-swaps ``#host-indicator-{id}``: the header indicator, updated to the
    host label + model.

    Raises:
        HTTPException 404: When the conversation is unknown.
    """
    try:
        queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    try:
        selected = get_host(host)
    except UnknownHostError:
        # Stale dropdown post → primary.
        selected = get_primary_host()
    conversation = queries.set_active_host(
        db, conversation_id, None if selected.is_primary else selected.name
    )
    # Resolve through the toggle so a disabled remote host falls back to
    # primary (matches the indicator + generation path).
    spec = _resolve_active_host(conversation, db)

    # The effective model can change with the host, so re-probe residency ON
    # THAT HOST before re-rendering the chip rather than reusing old state.
    effective_model = _effective_model(conversation, spec, db)
    effective_host = spec.ollama_host
    model_loaded = await ollama.is_model_loaded(
        client, effective_model, host=effective_host
    )

    indicator_html = templates.get_template("_host_indicator.html").render(
        conversation=conversation,
        active_host_spec=spec,
        effective_model=effective_model,
        model_loaded=model_loaded,
        oob=True,
    )

    return HTMLResponse(content=indicator_html)


@router.post(
    "/chats/{conversation_id}/unload-model",
    response_class=HTMLResponse,
)
async def unload_chat_model_endpoint(
    conversation_id: int,
    db: DB,
    client: OllamaClient,
) -> Response:
    """Unload the chat's currently-effective model from Ollama's memory.

    Triggered by clicking the header model chip. The "effective" model is the
    selected host's model on a non-primary host, else the chat's pinned model
    (same rule the indicator displays) — we unload what the user sees.

    Returns the chip with ``data-state="unloaded"``. The next send OOB-swaps
    it back to ``loaded`` since generation implicitly reloads the model.

    Raises:
        HTTPException 404: When the conversation is unknown.
        HTTPException 502: When Ollama is unreachable. The chip keeps its
            previous state (HTMX won't swap on a non-2xx) rather than lying
            that the model was unloaded.
    """
    try:
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    spec = _resolve_active_host(conversation, db)
    effective_model = _effective_model(conversation, spec, db)
    effective_host = spec.ollama_host if spec else None

    try:
        await ollama.unload_model(client, effective_model, host=effective_host)
    except OllamaUnavailable as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Couldn't reach Ollama to unload: {e}",
        )

    indicator_html = templates.get_template("_host_indicator.html").render(
        conversation=conversation,
        active_host_spec=spec,
        effective_model=effective_model,
        model_loaded=False,
    )
    return HTMLResponse(content=indicator_html)


@router.post(
    "/chats/{conversation_id}/compact", response_class=HTMLResponse
)
async def compact_chat_endpoint(
    request: Request,
    conversation_id: int,
    db: DB,
    client: OllamaClient,
) -> Response:
    """Summarize the entire active history of a chat into one ``summary`` row.

    Summarizes every active message — nothing is kept verbatim — then archives
    all of it (including any prior ``summary``) and inserts a fresh ``summary``
    row with the model-generated briefing. Returns the re-rendered messages
    container for HTMX to swap in place.

    Raises:
        HTTPException 404: Unknown conversation.
        HTTPException 409: A generation is in flight for this chat —
            compacting mid-stream would race the producer's history reads.
        HTTPException 422: Nothing to compact yet — the chat has no active
            rows, or its only active row is an existing ``summary`` (no new
            turns to fold in).
        HTTPException 502: Ollama returned a body we couldn't parse, or
            returned an empty summary.
        HTTPException 503: Ollama is unreachable.
    """
    try:
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    # In-flight gate: the producer's `list_active_messages` rebuild inside the
    # tool-call loop would race the archive UPDATE below. Refusing is simpler
    # than coordinating.
    state = generation.live_generations.get(conversation_id)
    if state is not None and not state.done:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cannot compact while a response is generating.",
        )

    active = queries.list_active_messages(db, conversation_id)
    # Compact the entire active history — nothing is kept verbatim. The only
    # "nothing to compact" cases are an empty chat or a lone, already-active
    # summary (no new turns to fold in; re-summarizing it would only degrade
    # it).
    if not active or (len(active) == 1 and active[0].role == "summary"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Nothing to compact yet.",
        )
    to_summarize = active

    # num_ctx for the summarization call: project override or global default —
    # same as a normal turn, so a project pinning a larger window benefits
    # when summarizing a long history.
    num_ctx = _resolve_num_ctx(db, conversation_id)

    # On a non-primary host, compact through that host's model + host so the
    # summarizer reuses the warm KV cache where the conversation just streamed.
    # Route through `_host_overrides` so the remote-Ollama toggle is respected:
    # remote host selected but toggle off → summarize locally on the pinned
    # model rather than reaching the remote.
    overrides = _host_overrides(conversation, db)
    summarize_model = overrides["model"]
    summarize_host = overrides["ollama_host"]
    try:
        summary_text = await ollama.summarize_conversation(
            client,
            summarize_model,
            generation.build_history_payload(to_summarize),
            num_ctx=num_ctx,
            host=summarize_host,
        )
    except OllamaUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    except OllamaProtocolError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))

    if not summary_text:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Compaction model returned empty text.",
        )

    # Insert the summary, then archive the entire prior history. The summary's
    # id is > every prior row's (monotonic rowid), so ``id < summary.id``
    # archives everything except the summary itself.
    summary = queries.append_message(
        db, conversation_id, "summary", summary_text
    )
    queries.archive_messages_before(
        db, conversation_id, summary.id
    )

    # Re-render the whole messages container — simpler than an OOB delta for
    # "head N rows go, summary bubble appears", and it's a rare user action.
    messages = queries.list_messages(db, conversation_id)
    blocks = render.group_messages_for_render(messages)
    archived_count = render.count_archived_blocks(messages)
    return templates.TemplateResponse(
        request=request,
        name="_messages_inner.html",
        context={
            "conversation": conversation,
            "blocks": blocks,
            "archived_count": archived_count,
            "pending_stream_url": None,
        },
    )


@router.get(
    "/chats/{conversation_id}/archived", response_class=HTMLResponse
)
async def archived_messages_endpoint(
    request: Request, conversation_id: int, db: DB,
) -> Response:
    """Render the archived (compacted-away) messages for inline disclosure.

    Backs the ``<details>`` on the summary bubble. The fetch is lazy
    (``hx-trigger="toggle once``) so the panel doesn't render archived rows on
    every mount.

    Archived ``summary`` rows are hidden — they're stale by definition (the
    next compact subsumed them) and would only confuse the viewer.

    Raises:
        HTTPException 404: Unknown conversation.
    """
    try:
        queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    rows = [
        m for m in queries.list_messages(db, conversation_id)
        if m.archived_at is not None and m.role != "summary"
    ]
    blocks = render.group_messages_for_render(rows)
    return templates.TemplateResponse(
        request=request,
        name="_archived_messages.html",
        context={"blocks": blocks},
    )


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

    Called by the temperature ``<input>`` via ``hx-patch`` on ``change``.
    Clamps to [0.0, 2.0] server-side so a hand-crafted request can't push
    Ollama out of range. Returns 204 — the input already shows the value.

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

    Called by the cap ``<input>`` via ``hx-patch`` on ``change``. Clamps to
    [1, 10] server-side so a hand-crafted request can't drive a runaway or
    no-op tool loop. Returns 204 — the input already shows the value.

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
    "/chats/{conversation_id}/think-mode",
    response_class=Response,
)
async def set_chat_think_mode_endpoint(
    conversation_id: int,
    db: DB,
    think_mode: Annotated[str, Form()],
) -> Response:
    """Persist the per-chat thinking mode.

    Called by the thinking ``<select>`` via ``hx-patch`` on ``change``. Values
    outside ``{'default', 'off'}`` are coerced to ``'default'`` so a
    hand-crafted request can't persist one that resolves to ``think=true`` and
    400s a non-thinking model. Returns 204 — the select already shows the value.

    Raises:
        HTTPException 404: When the conversation doesn't exist.
    """
    if think_mode not in {"default", "off"}:
        think_mode = "default"
    try:
        queries.set_conversation_think_mode(db, conversation_id, think_mode)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/backup/status", response_class=HTMLResponse)
def backup_status_endpoint() -> Response:
    """Render the remote-backup status chip from process-local state.

    Polled by the chip (~every 2s) only while a push is in flight; the fragment
    stops re-arming its trigger once the backup settles, so the poll self-stops.
    No I/O — reads in-memory status via the ``backups_enabled`` /
    ``backup_status`` Jinja globals.
    """
    return HTMLResponse(templates.get_template("_backup_chip.html").render())


@router.post("/backup/pull", response_class=HTMLResponse)
async def backup_pull_endpoint(request: Request) -> Response:
    """Restore the chats DB + agent workspaces from the remote mirror.

    The "I switched machines" path: runs ``copy_agent_workspace.py --all`` (via
    :func:`app.backup.pull_all`). Because that overwrites the live ``chats.db``,
    the app closes its shared WAL connection around the pull, reopens it, then
    ``HX-Redirect``s to ``/projects`` to show the pulled state.

    Refused (409) while a generation is streaming — its producer holds the
    shared connection, so closing it mid-stream would break the stream and risk
    the DB. 404 when backups aren't configured.
    """
    if not config.backups_enabled():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "backups not configured"
        )

    # A live producer task holds app.state.db; don't yank it out mid-stream.
    # (Entries are never removed from live_generations, so check `not done`.)
    if any(not state.done for state in generation.live_generations.values()):
        return HTMLResponse(
            templates.get_template("_pull_chip.html").render(
                error="Finish generating first"
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    ok, detail = False, "Pull failed"
    app = request.app
    app.state.db.close()
    try:
        ok, detail = await backup.pull_all()
        if ok:
            # Idempotent migrations on the pulled file, in case the mirror was
            # written by an older app version.
            initialize_database()
    finally:
        # ALWAYS reopen — a failed/timed-out pull must not strand the app with
        # a closed connection (every later request would 500).
        app.state.db = open_connection()

    if ok:
        # HX-Redirect (not a 3xx): the swap is skipped and the browser
        # navigates, reloading sidebar + panel against the pulled DB.
        return HTMLResponse("", headers={"HX-Redirect": "/projects"})
    return HTMLResponse(
        templates.get_template("_pull_chip.html").render(
            error=detail or "Pull failed"
        )
    )


@router.post("/backup/push", response_class=HTMLResponse)
async def backup_push_endpoint() -> Response:
    """Trigger an immediate push of the local DB + workspaces to the mirror.

    For state changed OUTSIDE a chat turn — e.g. a file dropped into the
    workspace by hand — that the automatic triggers (send / generation-complete
    / ``write_file``) wouldn't catch. Fire-and-forget: ``request_backup``
    coalesces and runs the push in the background, so this returns at once.

    Async (not sync) because ``request_backup`` schedules an
    ``asyncio.create_task`` and must run on the event loop, not the threadpool.
    Returns the backup chip OOB in its pending state, re-arming the
    ``/backup/status`` poll. 404 when backups aren't configured.
    """
    if not config.backups_enabled():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "backups not configured"
        )
    backup.request_backup("manual")
    return HTMLResponse(
        templates.get_template("_backup_chip.html").render(oob=True)
    )
