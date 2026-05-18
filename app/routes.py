"""Phase 6: HTTP routes for the HTMX frontend.

All routes live under ``/api``. Phase 7's HTML routes will live at the
root (``/``, ``/chats/{id}``, etc.) so the two namespaces never collide.

Errors map to HTTP status as follows:
  - ``OllamaUnavailable`` (transport problem) → 503
  - ``OllamaProtocolError`` (Ollama answered garbage) → 502
  - ``LookupError`` (rename/regenerate on unknown id) → 404
  - Streaming endpoints can't change the status code mid-response
    because headers are already sent — they emit an SSE ``error``
    event instead.
"""

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import ollama, queries
from app.dependencies import DB, OllamaClient
from app.ollama import OllamaProtocolError, OllamaUnavailable
from app.queries import Conversation, Message

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Request bodies — pydantic models for input validation.
# Output uses the queries.py dataclasses directly; FastAPI serializes them
# via pydantic so we don't duplicate the row shape.
# ---------------------------------------------------------------------------


class ConversationCreate(BaseModel):
    """Body for POST /api/conversations."""

    name: str
    model: str


class ConversationRename(BaseModel):
    """Body for PATCH /api/conversations/{id}."""

    name: str


class MessageCreate(BaseModel):
    """Body for POST /api/conversations/{id}/messages."""

    content: str


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse(data: dict, event: str | None = None) -> str:
    """Format ``data`` as a single SSE message.

    The data field is JSON-encoded so newlines inside tokens don't
    break the SSE line protocol (any internal newlines become ``\\n``
    inside the JSON string).

    Args:
        data: The JSON-serializable payload for the ``data:`` field.
        event: Optional event name. If omitted, the consumer's default
            handler fires; named events (``done``, ``error``) let the
            frontend wire different reactions to control messages.

    Returns:
        A bytes-ready string ending in the SSE event terminator
        ``\\n\\n``.
    """
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@router.get("/models")
async def list_models_endpoint(client: OllamaClient) -> list[str]:
    """Return the names of every model installed in the local Ollama."""
    try:
        return await ollama.list_models(client)
    except OllamaUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    except OllamaProtocolError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


@router.get("/conversations")
def list_conversations_endpoint(db: DB) -> list[Conversation]:
    """Return every conversation, most-recently-updated first."""
    return queries.list_conversations(db)


@router.post(
    "/conversations", status_code=status.HTTP_201_CREATED
)
def create_conversation_endpoint(
    payload: ConversationCreate, db: DB
) -> Conversation:
    """Create a new conversation row."""
    return queries.create_conversation(db, name=payload.name, model=payload.model)


@router.patch("/conversations/{conversation_id}")
def rename_conversation_endpoint(
    conversation_id: int, payload: ConversationRename, db: DB
) -> Conversation:
    """Rename a conversation (bumps its updated_at)."""
    try:
        return queries.rename_conversation(db, conversation_id, payload.name)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_conversation_endpoint(conversation_id: int, db: DB) -> Response:
    """Delete a conversation and (via FK cascade) its messages."""
    queries.delete_conversation(db, conversation_id)
    # 204 means "success, no body" — Response with no content satisfies
    # FastAPI's type expectations cleanly.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@router.get("/conversations/{conversation_id}/messages")
def list_messages_endpoint(conversation_id: int, db: DB) -> list[Message]:
    """Return all messages in a conversation, oldest first."""
    return queries.list_messages(db, conversation_id)


def _build_history_payload(
    history: list[Message],
) -> list[dict[str, str]]:
    """Turn Message dataclasses into the dicts Ollama wants over the wire."""
    return [{"role": m.role, "content": m.content} for m in history]


async def _stream_assistant_reply(
    client,
    db,
    conversation_id: int,
    model: str,
    history: list[Message],
    on_complete: str,
) -> AsyncIterator[str]:
    """Stream Ollama chunks as SSE events; persist the result at the end.

    Yields SSE-formatted strings: one JSON ``data:`` per chunk, a
    ``done`` event when complete, an ``error`` event if Ollama fails
    mid-stream. The accumulated assistant text is persisted via either
    ``append_message`` (new send) or ``replace_last_assistant_message``
    (regenerate), selected by ``on_complete``.

    Args:
        client: Shared httpx AsyncClient.
        db: Shared sqlite3 Connection.
        conversation_id: Parent conversation id.
        model: Ollama model identifier.
        history: Messages to send as the prompt context.
        on_complete: ``"append"`` for new sends (saves a new assistant
            row); ``"replace"`` for regenerate (overwrites the existing
            last assistant row in place).
    """
    chunks: list[str] = []
    try:
        async for chunk in ollama.stream_chat(
            client, model, _build_history_payload(history)
        ):
            if chunk.content:
                chunks.append(chunk.content)
                yield _sse({"content": chunk.content})
            if chunk.done:
                break
    except OllamaUnavailable as e:
        yield _sse({"message": str(e)}, event="error")
        return
    except OllamaProtocolError as e:
        yield _sse({"message": str(e)}, event="error")
        return

    # Persist the full assistant reply. We do this AFTER the stream
    # completes so a half-generated response (e.g. user disconnects)
    # doesn't pollute the conversation. Caveat: if the client
    # disconnects mid-stream, the partial text is discarded entirely;
    # that's the documented tradeoff per PLAN's regenerate semantics
    # ("replaces in place; no variant history kept").
    full = "".join(chunks)
    if on_complete == "append":
        queries.append_message(db, conversation_id, "assistant", full)
    else:  # "replace"
        queries.replace_last_assistant_message(db, conversation_id, full)

    yield _sse({}, event="done")


@router.post("/conversations/{conversation_id}/messages")
async def send_message_endpoint(
    conversation_id: int,
    payload: MessageCreate,
    db: DB,
    client: OllamaClient,
) -> StreamingResponse:
    """Save the user message, then stream the assistant's response."""
    try:
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    # Save the user message BEFORE we start streaming so the prompt we
    # send to Ollama includes it (and so a streaming failure still
    # leaves the user's input in the conversation history).
    queries.append_message(db, conversation_id, "user", payload.content)
    history = queries.list_messages(db, conversation_id)

    return StreamingResponse(
        _stream_assistant_reply(
            client, db, conversation_id,
            conversation.model, history, on_complete="append",
        ),
        media_type="text/event-stream",
    )


@router.post("/conversations/{conversation_id}/regenerate")
async def regenerate_endpoint(
    conversation_id: int, db: DB, client: OllamaClient
) -> StreamingResponse:
    """Regenerate the last assistant response; replace it in place."""
    try:
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    history = queries.list_messages(db, conversation_id)
    if not history or history[-1].role != "assistant":
        # Regenerate makes no sense without an existing assistant
        # message to replace. The query layer would raise LookupError
        # later; rejecting here gives a clearer 400 (not a 404).
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No assistant message to regenerate.",
        )

    # Drop the last assistant message from the prompt so Ollama
    # generates a fresh response to the same user turn.
    prompt_history = history[:-1]

    return StreamingResponse(
        _stream_assistant_reply(
            client, db, conversation_id,
            conversation.model, prompt_history, on_complete="replace",
        ),
        media_type="text/event-stream",
    )
