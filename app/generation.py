"""Background-task generation that survives client disconnects.

The LLM call runs in an asyncio.Task owned by a module-level registry,
not by the HTTP connection — so a page reload can't cancel it or lose
the response. The pieces:

  * `start_generation(...)` registers a `GenerationState` and spawns the
    producer task `_run_generation`.
  * SSE endpoints are consumers: `consume_generation(state)` yields every
    event the producer appends, replaying from index 0 for late consumers
    (reloads, second tabs).
  * `consume_finished(db, conv_id)` is the fallback for when a reload's
    GET /stream lands AFTER the generation finished — it yields a single
    done event from the persisted assistant row.

`_run_generation`'s try/finally persists a partial assistant row on
catastrophic exits (CancelledError / GeneratorExit on shutdown, unhandled
exceptions) before the exception resumes.
"""

import asyncio
import html
import logging
import sqlite3
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Literal

import httpx

from app import backup, ollama, queries, rag_servers as _rag_servers, render
from app._time import today_utc
from app.ollama import OllamaProtocolError, OllamaUnavailable
from app.projects import current_workspace_root, project_workspace_root
from app.templates import templates
from app.tools import (
    RAG_TOOL_NAME,
    TOOLS,
    decode_tool_call,
    decode_tool_result,
    encode_tool_call,
    encode_tool_result,
    format_tool_invocation,
    run_tool,
    tool_specs_for_ollama,
)
from app.tools.rag import build_source_description

logger = logging.getLogger(__name__)


# Hard ceiling on tool rounds per assistant turn before we bail out.
_TOOL_ITERATION_CAP = 5

# Shown when a turn produces no visible answer at all — neither the streaming
# call nor the recovered tool-probe content yielded any text. A thinking model
# can spend a whole turn reasoning (and even "decide" mid-stream to call a tool
# the no-tools streaming call can't run), leaving an otherwise-blank bubble.
# A short note is clearer than an empty bubble; the reasoning is still kept in
# the collapsed thinking card.
_EMPTY_ANSWER_FALLBACK = (
    "_(The model stopped after reasoning without producing an answer. "
    "Try rephrasing or asking again.)_"
)


# Minimal system prompt injected ONLY on turns where tools are available
# (see `_run_generation`). Local models tend to under-call tools without an
# explicit policy, answering grounded questions from weights instead of
# retrieval; this nudges retrieval while discouraging speculative calls.
SINGLE_AGENT_SYSTEM_PROMPT = (
    "You have tools available. Call one only when its result would change "
    "your answer — prefer retrieval over memory for questions grounded in "
    "the user's configured knowledge sources. Don't call tools speculatively. "
    "If the user asks for a specific tool, use it. "
    "The current date is already provided. Call the time tool only for the "
    "precise time of day or a different timezone."
)


class GenerationInProgress(Exception):
    """Raised by `start_generation` when the conv already has a live task.

    Mapped to HTTP 409 by the route layer. The UI gate (the placeholder
    disables the send button) makes this rare; this is the defensive layer
    if a duplicate POST slips through.
    """


@dataclass
class GenerationState:
    """Shared state between the producer task and SSE consumers.

    Attributes:
        conversation_id: The chat the generation belongs to.
        events: Append-only log of (event_name, html_payload) tuples.
            Producer appends; consumers index in to replay or tail.
        done: True once the producer emitted its final event (done OR
            error). Consumers exit once this is True and events are drained.
        cond: Signalled by the producer after each append (and after setting
            done). Consumers `await cond.wait()` between drains.
        task: The producer asyncio.Task, held so the done-callback can
            inspect it.
    """

    conversation_id: int
    events: list[tuple[str, str]] = field(default_factory=list)
    done: bool = False
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None


# Single-process registry, keyed by conversation_id. A multi-worker uvicorn
# deployment would lose cross-worker visibility of this dict, but the app is
# single-user / single-process, so that's not a concern.
live_generations: dict[int, GenerationState] = {}


def _sse(payload: str, event: str | None = None) -> str:
    """Format an HTML payload as a single SSE message.

    Each newline in ``payload`` becomes its own ``data:`` line per the
    SSE spec.
    """
    prefix = f"event: {event}\n" if event else ""
    lines = payload.split("\n") if payload else [""]
    data_lines = "".join(f"data: {line}\n" for line in lines)
    return f"{prefix}{data_lines}\n"


async def _emit(state: GenerationState, event: str, payload: str) -> None:
    """Append one SSE event to the state and wake all consumers.

    Holding `state.cond` across the append + notify makes the wake-up atomic
    relative to the consumer's drain-and-wait: no window where a consumer
    sees `len(events)` unchanged and then misses the notify.
    """
    async with state.cond:
        state.events.append((event, payload))
        state.cond.notify_all()


async def emit_ollama_error(
    state: GenerationState,
    exc: OllamaUnavailable | OllamaProtocolError,
) -> None:
    """Emit an SSE ``error`` event describing an Ollama-layer failure."""
    label = (
        "Ollama unavailable"
        if isinstance(exc, OllamaUnavailable)
        else "Ollama protocol error"
    )
    await _emit(
        state,
        "error",
        f'<div class="error">{label}: {html.escape(str(exc))}</div>',
    )


def maybe_persist_partial(
    db: sqlite3.Connection,
    conversation_id: int,
    on_complete: Literal["append", "replace"],
    chunks: list[str],
    persisted_or_errored: bool,
) -> None:
    """Write a partial assistant row if the normal-path write didn't fire.

    The producer's outer ``finally`` calls this once on every exit path.
    A no-op when ``persisted_or_errored`` is True (normal completion or an
    Ollama error already wrote/emitted); otherwise drops a
    ``(response interrupted)`` bubble — or the partial token buffer if any
    streamed — so the chat panel has something to render after a reload.
    """
    if persisted_or_errored:
        return
    partial = "".join(chunks) if chunks else "(response interrupted)"
    if on_complete == "append":
        queries.append_message(db, conversation_id, "assistant", partial)
    elif chunks:
        queries.replace_last_assistant_message(
            db, conversation_id, partial
        )


async def signal_done(state: GenerationState) -> None:
    """Mark the state done and wake every pending consumer.

    The producer's last act on every exit path. ``notify_all`` is safe with
    zero consumers; a consumer that attaches later still sees ``state.done``
    and exits its drain loop without waiting on the condition.
    """
    async with state.cond:
        state.done = True
        state.cond.notify_all()


async def consume_generation(
    state: GenerationState,
) -> AsyncIterator[str]:
    """Yield SSE events from a state, replaying from index 0 then tailing.

    A new consumer (e.g. a reloaded page) sees every already-emitted event;
    an early-attached one iterates in lock-step with the producer. `cond`
    provides cross-task signalling so the consumer doesn't busy-poll.
    """
    pos = 0
    while True:
        # Drain new events without holding the lock so we don't block the
        # producer.
        while pos < len(state.events):
            event, payload = state.events[pos]
            yield _sse(payload, event=event)
            pos += 1
        if state.done:
            return
        # Recheck under the lock the producer notifies under, so there's no
        # window where we'd miss a signal that just fired.
        async with state.cond:
            if state.done or pos < len(state.events):
                continue
            await state.cond.wait()


async def consume_finished(
    db: sqlite3.Connection, conversation_id: int
) -> AsyncIterator[str]:
    """Emit a single done event for a finished/missing generation.

    Used when a reload's GET /stream lands AFTER the generation finished and
    left the registry. The chat-panel placeholder needs a `done` event to
    close cleanly, else the streaming-dots animation hangs forever.

    Reads the last assistant row and yields it as the done event's OOB-swap
    payload — same shape as the live happy-path done, so HTMX swaps the
    placeholder out for the persisted bubble.
    """
    messages = queries.list_messages(db, conversation_id)
    for m in reversed(messages):
        if m.role == "assistant":
            final_html = templates.get_template("_message.html").render(
                message=m,
                swap_target=f"#assistant-stream-{conversation_id}",
            )
            yield _sse(final_html, event="done")
            return
    # Defensive: no assistant row at all. Emit an empty assistant bubble
    # OOB-swap so the placeholder closes — better blank than forever-streaming.
    yield _sse(
        f'<div class="message message--assistant" '
        f'hx-swap-oob="outerHTML:#assistant-stream-{conversation_id}"></div>',
        event="done",
    )


async def start_generation(
    *,
    client: httpx.AsyncClient,
    db: sqlite3.Connection,
    conversation_id: int,
    model: str,
    temperature: float = 0.8,
    tool_iteration_cap: int = _TOOL_ITERATION_CAP,
    history: list,
    on_complete: Literal["append", "replace"],
    think: bool | None = None,
    num_ctx: int | None = None,
    ollama_host: str | None = None,
) -> GenerationState:
    """Register a GenerationState and spawn the producer ``_run_generation``.

    A tool-capable model is always offered the full tool registry (gated
    only on capability); the project's system prompt, the selected Ollama
    host, and the chat's ``think`` flag are threaded through.

    Args:
        think: The Ollama ``think`` flag (True/False), or None to omit it
            (Ollama default). Passed straight through to the chat calls.
        ollama_host: Optional remote Ollama base URL. When set, the producer
            routes every chat call (probe + stream) there instead of the
            shared client's local base_url. None keeps local behavior.

    Raises:
        GenerationInProgress: when a generation is already running for this
            conversation. Raised SYNCHRONOUSLY before the first ``await`` so
            callers' ``except`` catches it before any dispatch work. The
            route maps this to HTTP 409.
    """
    # The in-flight guard must raise BEFORE the first await — callers'
    # try/except depends on it being synchronous; past an await the
    # exception would surface in a different control-flow shape.
    existing = live_generations.get(conversation_id)
    if existing is not None and not existing.done:
        raise GenerationInProgress(
            f"Conversation {conversation_id} already has a generation in flight"
        )

    state = GenerationState(conversation_id=conversation_id)
    # Register BEFORE create_task so the registry is populated by the time
    # control returns. This evicts any done entry from a previous turn. Done
    # entries linger until replaced (not removed on task-done) so a slow
    # reload's GET /stream can still replay the recently-finished events.
    live_generations[conversation_id] = state
    state.task = asyncio.create_task(
        _run_generation(
            state=state,
            client=client,
            db=db,
            conversation_id=conversation_id,
            model=model,
            temperature=temperature,
            tool_iteration_cap=tool_iteration_cap,
            history=history,
            on_complete=on_complete,
            think=think,
            num_ctx=num_ctx,
            ollama_host=ollama_host,
        )
    )
    state.task.add_done_callback(_make_done_callback(conversation_id))
    return state


def _make_done_callback(conversation_id: int):
    """Build the per-conversation done-callback for a generation task.

    Two jobs: surface any unhandled task exception via logging (asyncio
    silently swallows exceptions in fire-and-forget tasks otherwise), and
    request a remote backup now that the turn's rows are persisted.

    The state is NOT removed from `live_generations` on done — it stays
    until the next `start_generation` evicts it, so a slow reload landing
    after the gen finished can still replay the event log via
    `consume_generation` instead of the lossy `consume_finished` path.
    """
    def cb(task: asyncio.Task) -> None:
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "Generation task for conversation %d failed",
                    conversation_id,
                    exc_info=exc,
                )
        # Turn over, rows persisted — push to the remote mirror. Fires on
        # every completion path; debounced and a no-op when backups are off.
        backup.request_backup("generation-complete")

    return cb


# ---------------------------------------------------------------------------
# Producer
# ---------------------------------------------------------------------------


def _build_history_payload(
    history: list, system_prompt: str | None = None
) -> list[dict]:
    """Turn Message dataclasses into the wire format Ollama expects.

    Args:
        history: Conversation Message rows to serialize.
        system_prompt: When set, prepended as a ``{"role": "system", ...}``
            message so the model sees it before any turn. Used by the
            tool-enabled path to nudge retrieval; ``None`` for title
            generation. Conversation rows never store a ``system`` role, so
            prepending here can't duplicate one in ``history``.

    Returns:
        A list of dicts in Ollama's ``/api/chat`` ``messages`` shape.
    """
    out: list[dict] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    # Dropping a corrupt tool_call row means ALSO dropping its paired
    # tool_result — else Ollama sees a role="tool" message with no preceding
    # assistant+tool_calls and 400s the whole chat. `_run_generation` writes
    # call/result rows strictly interleaved, so "skip the next tool_result"
    # is the right rule; a non-result row resets the flag.
    skip_next_result = False
    for m in history:
        if m.role == "tool_call":
            decoded = decode_tool_call(m.content)
            if decoded is None:
                skip_next_result = True
                continue
            name, arguments = decoded
            out.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": name,
                        "arguments": arguments,
                    },
                }],
            })
            skip_next_result = False
        elif m.role == "tool_result":
            if skip_next_result:
                skip_next_result = False
                continue
            # Decode the JSON envelope so the model sees plain text, not the
            # {"text": ..., "sources": [...]} structure. Legacy plain-text
            # rows round-trip cleanly via decode_tool_result's fallback.
            out.append({
                "role": "tool",
                "content": decode_tool_result(m.content).text,
            })
        elif m.role in ("user", "assistant"):
            skip_next_result = False
            out.append({"role": m.role, "content": m.content})
        elif m.role == "summary":
            # Synthetic row from the manual-compact endpoint. Inject as a
            # `system` message so the model treats it as background context,
            # not a turn to respond to. It falls AFTER any turn-level
            # system_prompt prepended above — the right precedence: project
            # instructions speak for the CURRENT turn, the summary for BEFORE.
            skip_next_result = False
            out.append({
                "role": "system",
                "content": f"Earlier conversation summary:\n\n{m.content}",
            })
        else:
            # Unknown role — drop silently so legacy rows from removed
            # features still serialize into a valid payload.
            skip_next_result = False
    return out


# Public alias so out-of-module callers (the manual-compact route) can use the
# function without reaching into a leading-underscore name. Same object.
build_history_payload = _build_history_payload


# How much of each opening turn the titler sees. A title needs only the gist
# of the request and reply, and `generate_title` forces `think: false`, which
# changes the prompt template versus the streamed reply and so misses Ollama's
# prompt cache — the opening turns are re-prefilled from scratch. Capping their
# length bounds that prefill (the user turn is usually short, but a first reply
# or a pasted document can run thousands of tokens), keeping the title call
# fast and under its timeout regardless of conversation size. ~800 chars ≈ 200
# tokens per turn; plenty to name a chat.
_TITLE_CONTENT_BUDGET = 800


def _title_context(history: list) -> list:
    """Pick the user+assistant turns to title the conversation from.

    Returns every ``user`` message and every non-empty ``assistant`` message,
    in order, each truncated to ``_TITLE_CONTENT_BUDGET`` characters.
    Tool-call/result and summary rows are skipped — they add tokens (and
    tool-interleaving hazards) without sharpening a title. Assistant turns are
    optional: a title can be drawn from the user turns alone if no text reply
    exists yet.

    The caller fires the titler only for the first few assistant replies
    (``_maybe_emit_title``'s ``1 <= count <= 3`` guard), so this list stays a
    handful of turns even on a chat that later grows long. That bound is what
    keeps the title call's prefill cheap — see ``_maybe_emit_title``.

    Args:
        history: Active conversation Message rows, oldest first.

    Returns:
        A list of length-capped Message copies: the user and non-empty
        assistant text turns so far, oldest first.
    """
    return [
        replace(m, content=m.content[:_TITLE_CONTENT_BUDGET])
        for m in history
        if m.role == "user" or (m.role == "assistant" and m.content.strip())
    ]


async def _maybe_emit_title(
    state: GenerationState,
    client: httpx.AsyncClient,
    db: sqlite3.Connection,
    conversation_id: int,
    ollama_host: str | None = None,
) -> None:
    """Fire the auto-titler after the 1st through 3rd assistant reply.

    Emits zero or one `title` SSE event via `_emit` (consumers see it before
    the final `done` because we emit in order).

    Args:
        state: The live generation's state (for emitting the `title` event).
        client: The shared httpx client.
        db: The shared SQLite connection.
        conversation_id: The chat being titled.
        ollama_host: The chat's selected Ollama host — the same machine that
            just streamed the reply, so the model is resident there. Passed
            through to `generate_title`; ``None`` targets the primary host.

    Silent skips (no event):
      - The chat was manually renamed (`name_locked`).
      - The count is outside 1..3 (cap on title-refresh attempts).
      - Any Ollama failure (down, malformed reply, timeout).
      - The model returns empty text after stripping.
    """
    conversation = queries.get_conversation(db, conversation_id)
    if conversation.name_locked:
        return

    count = queries.count_assistant_messages(db, conversation_id)
    if not 1 <= count <= 3:
        return

    # Title generation runs on the ACTIVE (post-compact) history so the
    # title reflects what the conversation is now, not an archived prefix.
    #
    # We feed the titler every user+assistant turn SO FAR (tool/summary rows
    # skipped), so each of the up-to-three title passes refines using the
    # latest turns, not just the opening request. The `1 <= count <= 3` guard
    # above bounds this to a few short turns, which is what keeps it cheap:
    #   1. Speed/reliability. `generate_title` forces `think: False`, but the
    #      reply we just streamed was generated with the chat's own think
    #      setting (thinking ON for reasoning models). The flag change renders
    #      a different prompt template, so Ollama's prompt cache misses and it
    #      re-prefills the turns we send. A handful of bounded turns keeps that
    #      prefill small and under the title timeout — the hazard only bites on
    #      a LONG transcript, which the firing window rules out.
    #   2. Quality. A subject that only emerges over replies 2-3 can sharpen
    #      the title instead of being ignored.
    context = _title_context(queries.list_active_messages(db, conversation_id))
    try:
        title = await ollama.generate_title(
            client,
            conversation.model,
            _build_history_payload(context),
            host=ollama_host,
        )
    except (OllamaUnavailable, OllamaProtocolError) as e:
        logger.warning("Title generation failed for conv %d: %s", conversation_id, e)
        return

    if not title:
        logger.warning("Title generation returned empty for conv %d", conversation_id)
        return

    updated = queries.set_name_auto(db, conversation_id, title)
    if updated is None:
        logger.debug("Title set_name_auto skipped for conv %d (locked?)", conversation_id)
        return

    # Bare `hx-swap-oob="true"` matches by id and swaps in place — the
    # existing `#chat-{id}` row is replaced when the `title` event lands.
    # Pass the owning project so the row's link URL is project-scoped.
    try:
        project = queries.get_project_for_conversation(db, conversation_id)
    except LookupError:
        project = None
    row_html = templates.get_template("_chat_item.html").render(
        chat=updated,
        active_chat_id=updated.id,
        project=project,
        oob_swap="true",
    )
    await _emit(state, "title", row_html)


def _turn_tool_specs(db: sqlite3.Connection) -> list[dict]:
    """All registered tool specs for a turn, gated only by configuration.

    Every registered tool is offered to a tool-capable model. ``query_rag``
    is the one conditional: included only when at least one RAG server is
    configured, with its ``source`` description rebuilt from every server.

    Args:
        db: Open SQLite connection (to read the configured RAG servers).

    Returns:
        Ollama-shaped tool specs, possibly empty.
    """
    all_rag_servers = _rag_servers.list_servers(db)
    specs: list[dict] = []
    # tool_specs_for_ollama() deep-copies each spec's parameters, so patching
    # `source.description` below is local to this turn and never leaks back
    # into the registry.
    for spec in tool_specs_for_ollama():
        name = spec["function"]["name"]
        if name == RAG_TOOL_NAME:
            if not all_rag_servers:
                continue  # No RAG servers configured → omit query_rag.
            spec["function"]["parameters"]["properties"]["source"][
                "description"
            ] = build_source_description(all_rag_servers)
        specs.append(spec)
    return specs


async def _run_generation(
    *,
    state: GenerationState,
    client: httpx.AsyncClient,
    db: sqlite3.Connection,
    conversation_id: int,
    model: str,
    temperature: float = 0.8,
    tool_iteration_cap: int = _TOOL_ITERATION_CAP,
    history: list,
    on_complete: Literal["append", "replace"],
    think: bool | None = None,
    num_ctx: int | None = None,
    ollama_host: str | None = None,
) -> None:
    """Producer body — runs the LLM and writes events to the state.

    A plain async function (no `yield`): instead of yielding SSE it calls
    `await _emit(state, ev, payload)`. The outer try/finally persists a
    partial assistant row and signals done on every exit path, including
    catastrophic ones (CancelledError / GeneratorExit on shutdown, unhandled
    exceptions).
    """
    working_history = list(history)
    # Tools are offered, gated only on model capability.
    # `model_supports_tools` returns False on cache/network failure, which
    # collapses tools_payload to None (omits the key). query_rag is included
    # only when a RAG server is configured (see _turn_tool_specs).
    _enabled_specs = _turn_tool_specs(db)
    tools_payload = (
        _enabled_specs
        if _enabled_specs
        and await ollama.model_supports_tools(client, model, host=ollama_host)
        else None
    )

    # Resolve the owning project up-front, for two things: the per-project
    # workspace root (file tools) and the per-project system prompt (Normal
    # turns).
    try:
        _project = queries.get_project_for_conversation(db, conversation_id)
    except LookupError:
        # Defensive: every chat should have a project. Degrade to None so the
        # turn still runs without project-scoped extras.
        _project = None

    # System prompt = current date + project prompt (when set) + tool-use
    # nudge (only when tools are sent). The date is injected unconditionally,
    # fresh each turn, so the model never answers time-sensitive questions
    # from its frozen training knowledge — it doesn't have to *recognise* a
    # question as date-dependent, the date is simply always in context. This
    # gives up the former "no system message on bare plain chat" property,
    # but the line is factual rather than behavioural, so it's unlikely to
    # shift small-model behaviour.
    parts: list[str] = [f"Current date: {today_utc()} (UTC)."]
    if _project is not None and _project.system_prompt:
        parts.append(_project.system_prompt)
    if tools_payload is not None:
        parts.append(SINGLE_AGENT_SYSTEM_PROMPT)
    system_prompt = "\n\n".join(parts)

    turn_id = str(time.monotonic_ns())
    card_id = render.card_id_for(turn_id)
    list_id = f"{card_id}-list"
    summary_id = f"{card_id}-summary"
    call_index = 0
    in_flight: dict[str, dict] = {}

    # Thinking-card ids share the turn_id so the open/append/collapse OOBs all
    # target the same <details>. See render.render_thinking_* helpers.
    thinking_card_id = render.thinking_card_id_for(turn_id)
    thinking_content_id = f"{thinking_card_id}-content"
    thinking_summary_id = f"{thinking_card_id}-summary"

    # Hoist `chunks` + `persisted_or_errored` so the function-level
    # try/finally can persist a partial assistant row on a CancelledError /
    # GeneratorExit at any phase before re-raising. `thinking_chunks`
    # accumulates the final stream_chat call's reasoning for persistence.
    chunks: list[str] = []
    thinking_chunks: list[str] = []
    persisted_or_errored = False
    # Content the tool-probe returned on the round it decided NOT to call a
    # tool. The probe (`maybe_tool_call`) often carries the actual answer there;
    # the loop normally discards it and re-streams. We keep it as a recovery
    # source for the rare turn whose streaming call comes back empty (see the
    # empty-answer fallback after the stream).
    probe_content = ""

    # Scope file tools to this chat's project workspace for the turn. The
    # ContextVar wraps the whole producer body; tool calls inside see the
    # per-project root via `current_workspace_root` (falling back to
    # FILE_TOOL_ROOT when the workspace can't be resolved).
    if _project is not None:
        ws_root = project_workspace_root(_project)
        if ws_root is not None:
            # Create lazily so a brand-new project's workspace exists by the
            # time a tool reads/writes within it.
            ws_root.mkdir(parents=True, exist_ok=True)
    else:
        ws_root = None
    ws_token = current_workspace_root.set(ws_root)

    try:
        for iteration in range(tool_iteration_cap):
            try:
                tool_calls, probe_content = await ollama.maybe_tool_call(
                    client,
                    model,
                    _build_history_payload(working_history, system_prompt),
                    tools=tools_payload,
                    temperature=temperature,
                    think=think,
                    num_ctx=num_ctx,
                    host=ollama_host,
                )
            except (OllamaUnavailable, OllamaProtocolError) as e:
                # Set BEFORE the await — await is a cancellation point, and
                # the outer finally must see the flag if cancellation lands
                # inside emit_ollama_error.
                persisted_or_errored = True
                await emit_ollama_error(state, e)
                return

            if not tool_calls:
                # `probe_content` now holds whatever this round answered with;
                # the empty-answer fallback below recovers it if the streaming
                # call yields nothing.
                break

            for call in tool_calls:
                name = call["name"]
                arguments = call.get("arguments") or {}

                queries.append_message(
                    db,
                    conversation_id,
                    "tool_call",
                    content=encode_tool_call(name, arguments),
                )

                start_ms = int(time.time() * 1000)
                row_id = f"{card_id}-row-{call_index}"
                label = format_tool_invocation(name, arguments)
                live_row = render.ToolRowView(
                    id=row_id,
                    label=label,
                    elapsed_start_ms=start_ms,
                    elapsed_final_ms=None,
                    elapsed_display="0:00",
                )

                if call_index == 0:
                    payload = render.render_tool_card_initial(
                        card_id=card_id,
                        list_id=list_id,
                        summary_id=summary_id,
                        live_row=live_row,
                        conversation_id=conversation_id,
                    )
                else:
                    payload = render.render_tool_card_row_append(
                        live_row=live_row,
                        list_id=list_id,
                        summary_id=summary_id,
                        call_index=call_index,
                    )

                await _emit(state, "tool-call", payload)

                in_flight[row_id] = {
                    "start_ms": start_ms,
                    "name": name,
                    "arguments": arguments,
                    "label": label,
                }
                call_index += 1

                result = await run_tool(name, arguments)

                # A successful workspace write changes on-disk state mid-turn
                # — push it. `run_tool` never raises, so success is the
                # "Wrote " prefix write_file returns. Debounce coalesces this
                # with the generation-complete push into ~one rsync.
                if name == "write_file" and result.text.startswith("Wrote "):
                    backup.request_backup("write")

                queries.append_message(
                    db,
                    conversation_id,
                    "tool_result",
                    content=encode_tool_result(result),
                )

                end_ms = int(time.time() * 1000)
                duration_ms = max(0, end_ms - start_ms)
                frozen_row = render.ToolRowView(
                    id=row_id,
                    label=label,
                    elapsed_start_ms=None,
                    elapsed_final_ms=duration_ms,
                    elapsed_display=render.format_elapsed_mm_ss(duration_ms),
                    sources=result.sources,
                )
                await _emit(
                    state,
                    "tool-result",
                    render.render_tool_card_row_freeze(frozen_row),
                )

                del in_flight[row_id]

            # Read back only the active rows so an earlier-compacted chat
            # doesn't re-bloat its prompt on the next tool-loop iteration.
            working_history = queries.list_active_messages(db, conversation_id)
        else:
            # Iteration cap hit: persist apology + emit done with frozen-row
            # OOBs for any unpaired calls.
            message = queries.append_message(
                db,
                conversation_id,
                "assistant",
                "(Tool-call limit reached; no final answer produced.)",
            )
            persisted_or_errored = True
            bail_payload = render.render_done_card_oobs(
                call_index, in_flight, summary_id
            )
            final_html = templates.get_template("_message.html").render(
                message=message,
                swap_target=f"#assistant-stream-{conversation_id}",
            )
            await _emit(state, "done", bail_payload + final_html)
            return

        # Streaming phase.
        prompt_tokens: int | None = None
        eval_tokens: int | None = None
        # Thinking lifecycle: the model streams reasoning chunks (content
        # empty) first, then content chunks. The card opens on the first
        # reasoning chunk and collapses the moment the first visible token
        # arrives — mirroring the tool-card OOB lifecycle.
        thinking_started = False
        thinking_collapsed = False
        try:
            async for chunk in ollama.stream_chat(
                client, model,
                _build_history_payload(working_history, system_prompt),
                temperature=temperature,
                think=think,
                num_ctx=num_ctx,
                host=ollama_host,
            ):
                if chunk.thinking:
                    if not thinking_started:
                        thinking_started = True
                        await _emit(
                            state,
                            "think",
                            render.render_thinking_open(
                                card_id=thinking_card_id,
                                content_id=thinking_content_id,
                                summary_id=thinking_summary_id,
                                first_text=chunk.thinking,
                                conversation_id=conversation_id,
                            ),
                        )
                    else:
                        await _emit(
                            state,
                            "think",
                            render.render_thinking_append(
                                content_id=thinking_content_id,
                                text=chunk.thinking,
                            ),
                        )
                    thinking_chunks.append(chunk.thinking)
                if chunk.content:
                    # First visible token: collapse the open card before the
                    # answer starts streaming below it.
                    if thinking_started and not thinking_collapsed:
                        thinking_collapsed = True
                        await _emit(
                            state,
                            "think",
                            render.render_thinking_collapse(
                                card_id=thinking_card_id,
                                content_id=thinking_content_id,
                                summary_id=thinking_summary_id,
                                full_text="".join(thinking_chunks),
                            ),
                        )
                    chunks.append(chunk.content)
                    await _emit(state, "token", html.escape(chunk.content))
                if chunk.done:
                    # Final chunk carries Ollama's token counts for the turn
                    # (None if unreported, e.g. a full prompt-cache hit).
                    prompt_tokens = chunk.prompt_tokens
                    eval_tokens = chunk.eval_tokens
                    break
        except (OllamaUnavailable, OllamaProtocolError) as e:
            # Flag-before-await, as in the probe-loop branch above.
            persisted_or_errored = True
            await emit_ollama_error(state, e)
            return

        # Thinking-only stream (reasoning but no visible answer): collapse the
        # still-open card so it doesn't linger in the "Thinking…" state.
        if thinking_started and not thinking_collapsed:
            await _emit(
                state,
                "think",
                render.render_thinking_collapse(
                    card_id=thinking_card_id,
                    content_id=thinking_content_id,
                    summary_id=thinking_summary_id,
                    full_text="".join(thinking_chunks),
                ),
            )

        full_text = "".join(chunks)
        # Empty-answer recovery. The streaming call (which carries no tools) can
        # finish having emitted only reasoning — e.g. a thinking model that
        # "decides" mid-stream to call a tool it can't reach here. Rather than
        # persist a blank bubble, fall back to the tool-probe's content (often
        # the real answer, otherwise empty), then to a short note. Emit it as a
        # token so the live placeholder fills in before the done swap, matching
        # the normal streaming path. The reasoning is still kept below.
        if not full_text.strip():
            full_text = probe_content if probe_content.strip() else _EMPTY_ANSWER_FALLBACK
            await _emit(state, "token", html.escape(full_text))
        # Persist the reasoning so a reload rebuilds the collapsed card. None
        # (not "") on a non-reasoning turn keeps the column NULL and the
        # historic render free of an empty thinking card.
        thinking_text = "".join(thinking_chunks) or None
        if on_complete == "append":
            message = queries.append_message(
                db, conversation_id, "assistant", full_text,
                prompt_tokens=prompt_tokens,
                eval_tokens=eval_tokens,
                thinking=thinking_text,
            )
        else:  # "replace"
            message = queries.replace_last_assistant_message(
                db, conversation_id, full_text,
                prompt_tokens=prompt_tokens,
                eval_tokens=eval_tokens,
                thinking=thinking_text,
            )
        # Persisted — outer finally must not double-write a partial. Set
        # BEFORE any further awaits, since await is a cancellation point.
        persisted_or_errored = True

        # Title fires BEFORE the done event: the OOB outerHTML in done removes
        # the placeholder, which closes the EventSource, so anything emitted
        # after done is dropped.
        if on_complete == "append":
            await _maybe_emit_title(
                state, client, db, conversation_id, ollama_host=ollama_host
            )

        # Final done event: persisted message bubble + past-tense tool-card
        # summary OOB.
        done_card_oobs = render.render_done_card_oobs(
            call_index, in_flight, summary_id
        )
        final_html = templates.get_template("_message.html").render(
            message=message,
            swap_target=f"#assistant-stream-{conversation_id}",
        )
        await _emit(state, "done", done_card_oobs + final_html)
    finally:
        # Reset the workspace ContextVar first. The safety-net helpers below
        # don't depend on it, so resetting here (not in a nested finally)
        # keeps the try/finally pyramid shallow.
        current_workspace_root.reset(ws_token)
        # Safety net, fires on every exit path (CancelledError,
        # GeneratorExit, unhandled exception, normal completion, Ollama
        # errors). `maybe_persist_partial` no-ops when persisted_or_errored;
        # `signal_done` always wakes pending consumers so they don't wait
        # forever on a task that's gone.
        maybe_persist_partial(
            db, conversation_id, on_complete, chunks, persisted_or_errored
        )
        await signal_done(state)
