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
    POST   /chats/{id}/agent                 — set/clear active agent
    POST   /chats/{id}/compact                — summarize older turns (phase 18)
    GET    /chats/{id}/archived               — archived rows for disclosure
    POST   /chats/{id}/tools/{name}          — toggle per-chat tool
    POST   /chats/{id}/rag-servers/{name}    — toggle per-chat RAG server
    PATCH  /chats/{id}/temperature           — set per-chat temperature
    PATCH  /chats/{id}/tool-iteration-cap    — set per-chat tool cap
"""

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app import generation, ollama, queries, render
from app import rag_servers as _rag_servers
from app.agents import get_agent
from app.dependencies import DB, OllamaClient
from app.ollama import OllamaProtocolError, OllamaUnavailable
from app.routes._helpers import (
    _agent_overrides,
    _chip_states,
    _resolve_num_ctx,
)
from app.templates import templates
from app.tools import TOOLS

router = APIRouter()


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
    #
    # Phase 18: read only the active rows. The compact endpoint may
    # have archived an earlier prefix into a `summary` row; sending
    # the archived rows back would defeat the whole feature.
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

    # Phase 18: regenerate operates on active rows only. The persisted
    # assistant row being replaced is by definition active, so the
    # "last active row must be assistant" gate still works.
    history = queries.list_active_messages(db, conversation_id)
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


# Phase 18: number of trailing (user/assistant/summary) rows to keep active
# when the user clicks Compact. Captures the current working thread without
# being so generous that the prompt stays bloated. Hardcoded for v1; a
# per-chat knob is the natural "out of scope" thread once we have usage
# data.
_KEEP_RECENT_ON_COMPACT = 4


def _split_for_compact(
    active: list[queries.Message], keep_recent: int
) -> tuple[list[queries.Message], list[queries.Message]]:
    """Split active rows into ``(to-summarize, to-keep)`` halves.

    ``keep_recent`` is counted in renderable rows (``user`` / ``assistant`` /
    a prior ``summary``); attached ``tool_call`` / ``tool_result`` rows
    travel with the kept assistant turn they belong to. Leaving an orphan
    ``tool_result`` at the head of the kept window would 400 Ollama on
    the next turn (the wire format requires a preceding ``assistant`` row
    with ``tool_calls`` for every ``role: "tool"`` message).

    A prior ``summary`` row counts as renderable for the split because
    re-compacting deliberately *subsumes* it — the prior summary becomes
    part of the new compaction corpus, and the new summary replaces it.

    Args:
        active: Rows from :func:`queries.list_active_messages`, oldest first.
        keep_recent: How many renderable rows to keep unarchived. Must be
            >= 1; callers pass ``_KEEP_RECENT_ON_COMPACT``.

    Returns:
        ``(to_summarize, to_keep)``. Either list may be empty: an empty
        ``to_summarize`` means there's nothing older than the kept window
        (the caller should 422), and an empty ``to_keep`` cannot occur in
        practice because the user message that triggered the call sits
        at the tail.
    """
    # Walk from the end; once we've seen `keep_recent` renderable rows,
    # the boundary is at that index. If the chat has FEWER than
    # `keep_recent` renderable rows total, the loop exits without
    # breaking — in that case there's nothing to compact, so we keep
    # everything and return an empty `to_summarize`. The route then
    # 422s with "Nothing to compact yet."
    keep_idx: int | None = None
    renderables_seen = 0
    for i in range(len(active) - 1, -1, -1):
        if active[i].role in ("user", "assistant", "summary"):
            renderables_seen += 1
            if renderables_seen >= keep_recent:
                keep_idx = i
                break
    if keep_idx is None:
        return [], list(active)
    # Slide forward past any leading tool_* rows on the kept side so the
    # kept window doesn't start with an orphan tool_result. (A leading
    # tool_call without its assistant context is also illegal in the
    # wire format; the slide-forward handles both.)
    while keep_idx < len(active) and active[keep_idx].role in (
        "tool_call", "tool_result",
    ):
        keep_idx += 1
    return active[:keep_idx], active[keep_idx:]


@router.post(
    "/chats/{conversation_id}/compact", response_class=HTMLResponse
)
async def compact_chat_endpoint(
    request: Request,
    conversation_id: int,
    db: DB,
    client: OllamaClient,
) -> Response:
    """Summarize the older portion of a chat into a single ``summary`` row.

    Phase 18. Keeps the most-recent ``_KEEP_RECENT_ON_COMPACT`` renderable
    messages active; archives everything older (including any prior
    ``summary`` row); inserts a fresh ``summary`` row carrying the
    model-generated briefing. Returns the re-rendered messages container
    so HTMX can swap it in place.

    Raises:
        HTTPException 404: Unknown conversation.
        HTTPException 409: A generation is in flight for this chat —
            compacting mid-stream would race the producer's history reads.
        HTTPException 422: Nothing to compact yet (the chat has fewer than
            ``_KEEP_RECENT_ON_COMPACT`` + 1 active renderable rows).
        HTTPException 502: Ollama returned a body we couldn't parse, or
            returned an empty summary.
        HTTPException 503: Ollama is unreachable.
    """
    try:
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    # In-flight gate. The producer's `working_history = list_active_messages`
    # rebuild inside the tool-call loop would race the archive UPDATE
    # below; refusing here is simpler and safer than coordinating.
    state = generation.live_generations.get(conversation_id)
    if state is not None and not state.done:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cannot compact while a response is generating.",
        )

    active = queries.list_active_messages(db, conversation_id)
    to_summarize, to_keep = _split_for_compact(
        active, _KEEP_RECENT_ON_COMPACT
    )
    if not to_summarize:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Nothing to compact yet.",
        )
    # The cutoff is the FIRST kept row's id, not the summary row's id.
    # Every row in `to_summarize` has id < to_keep[0].id (rows are
    # ordered by created_at, id). Using the summary's id would archive
    # the kept rows too — they were all appended BEFORE the summary, so
    # all of their ids are also less than the summary's id.
    archive_cutoff_id = to_keep[0].id

    # Effective num_ctx for the summarization call: project override or
    # global default. Matches what `_run_generation` would use for a
    # normal turn, so a project that pinned a larger context window
    # still gets the benefit when summarizing a long history.
    num_ctx = _resolve_num_ctx(db, conversation_id)

    try:
        summary_text = await ollama.summarize_conversation(
            client,
            conversation.model,
            generation.build_history_payload(to_summarize),
            num_ctx=num_ctx,
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

    # Insert the summary, then archive everything older than the kept
    # window. The summary row's id is > every prior row's id (SQLite's
    # rowid is monotonic), so it's NOT included in the archived range.
    queries.append_message(
        db, conversation_id, "summary", summary_text
    )
    queries.archive_messages_before(
        db, conversation_id, archive_cutoff_id
    )

    # Re-render the whole messages container. Cheaper than crafting an
    # OOB delta for "the head N rows go away + a summary bubble appears"
    # — one user action per page load, simpler wins.
    messages = queries.list_messages(db, conversation_id)
    blocks = render.group_messages_for_render(messages)
    archived_count = queries.count_archived_messages(db, conversation_id)
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

    Phase 18: powered by the ``<details>`` element on the summary bubble.
    The fetch is lazy (``hx-trigger="toggle once``) so the chat panel
    doesn't pay to render archived rows on every panel mount.

    Archived ``summary`` rows are intentionally hidden — they're stale
    by definition (the next compact subsumed them) and surfacing them
    would only confuse the viewer.

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
    servers = _rag_servers.list_servers(db)
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
