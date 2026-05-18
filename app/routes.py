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
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app import ollama, queries
from app.dependencies import DB, OllamaClient
from app.ollama import OllamaProtocolError, OllamaUnavailable

# Templates live at the project root. Resolving relative to this file's
# location keeps the directory lookup correct regardless of where
# `uvicorn` is launched from.
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=_TEMPLATE_DIR)

router = APIRouter()


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def index_endpoint(request: Request, db: DB) -> Response:
    """Render the full layout — sidebar list + empty main panel.

    Direct hits to ``/`` (the user opens the app) land here. The
    sidebar is populated from the DB; the main panel shows an empty
    state until the user clicks a chat or creates one.
    """
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "chats": queries.list_conversations(db),
            "conversation": None,
            "messages": [],
        },
    )


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


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
        context={"chats": queries.list_conversations(db)},
    )


@router.post(
    "/chats",
    response_class=HTMLResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_chat_endpoint(
    request: Request,
    db: DB,
    name: Annotated[str, Form()],
    model: Annotated[str, Form()],
) -> Response:
    """Create a new conversation; return one sidebar row to prepend."""
    chat = queries.create_conversation(db, name=name, model=model)
    return templates.TemplateResponse(
        request=request,
        name="_chat_item.html",
        context={"chat": chat},
        status_code=status.HTTP_201_CREATED,
    )


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
def delete_chat_endpoint(conversation_id: int, db: DB) -> Response:
    """Delete a conversation; return empty 200 (HTMX removes the row).

    Returning 200 with no body (rather than 204) keeps things simple
    for HTMX consumers — some HTMX extensions ignore 204 responses,
    and an empty 200 body works uniformly across swap strategies.
    """
    queries.delete_conversation(db, conversation_id)
    return Response(content="", status_code=status.HTTP_200_OK)


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


def _build_history_payload(
    history: list,
) -> list[dict[str, str]]:
    """Turn Message dataclasses into the wire format Ollama expects."""
    return [{"role": m.role, "content": m.content} for m in history]


async def _stream_assistant_reply(
    client,
    db,
    conversation_id: int,
    model: str,
    history: list,
    on_complete: str,
) -> AsyncIterator[str]:
    """Stream Ollama tokens as SSE HTML; persist the full reply at end.

    Each Ollama chunk is HTML-escaped and yielded as a ``token`` SSE
    event so HTMX's ``sse-swap="token"`` appends just that bit of
    text to the assistant bubble. When the stream completes
    successfully, the accumulated text is saved to the DB and a final
    ``done`` event delivers the persisted message bubble as the
    replacement HTML for the streaming placeholder.

    Args:
        client: Shared httpx AsyncClient.
        db: Shared SQLite Connection.
        conversation_id: Parent conversation id.
        model: Ollama model identifier.
        history: Messages dataclasses to send as the prompt.
        on_complete: ``"append"`` for the new-send case (creates a
            new assistant row); ``"replace"`` for regenerate
            (overwrites the existing last assistant row in place).
    """
    chunks: list[str] = []
    try:
        async for chunk in ollama.stream_chat(
            client, model, _build_history_payload(history)
        ):
            if chunk.content:
                chunks.append(chunk.content)
                # html.escape so a token containing `<` or `&`
                # doesn't break the page when swapped into the DOM.
                yield _sse(html.escape(chunk.content), event="token")
            if chunk.done:
                break
    except OllamaUnavailable as e:
        # Wrap the message in a small fragment that HTMX can swap into
        # the placeholder. Keeps the failure visible without needing
        # JS to interpret a status code.
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
