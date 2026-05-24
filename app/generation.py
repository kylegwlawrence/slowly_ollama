"""Phase 12g: background-task generation that survives client disconnects.

Until this module existed, the LLM call ran inside the SSE response
generator and was tied 1:1 to the HTTP connection. A page reload
cancelled the task and lost the response — phase 12e.1's safety net
caught the broken-chat case ("(response interrupted)") but couldn't
preserve the actual reply.

This module decouples generation from the HTTP connection:

  * `start_generation(...)` registers a `GenerationState` for the
    conversation and spawns an asyncio.Task running
    `_run_generation`. The task is owned by the registry, not by any
    one request.
  * SSE endpoints become consumers: `consume_generation(state)`
    yields every event the producer appends, replaying from index 0
    for late consumers (reloads, second tabs).
  * `consume_finished(db, conv_id)` is the fallback for the "reload
    landed AFTER the generation finished" race — it yields a single
    done event from the persisted assistant row.

Phase 12e.1's cheap-fix try/finally stays inside `_run_generation`
for catastrophic failures (server shutdown via CancelledError /
GeneratorExit, unhandled exceptions). On those paths a partial
assistant row is still persisted before the exception resumes.
"""

import asyncio
import copy
import html
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal

import httpx

from app import ollama, queries, rag_servers as _rag_module, render
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


# Hard ceiling on how many tool rounds a single assistant turn can run
# before we bail out. 5 matches the spec in PLAN.md / phase12 plans.
_TOOL_ITERATION_CAP = 5


# Phase 15: minimal system prompt injected ONLY on turns where tools are
# actually available (see `_run_generation`). Local Ollama models tend to
# under-call tools without an explicit policy, so document-grounded
# questions get answered from the model's weights instead of retrieval.
# This nudges grounded retrieval while still discouraging speculative
# calls — the same balance the per-tool `current_time` description strikes.
SINGLE_AGENT_SYSTEM_PROMPT = (
    "You have tools available. Call one only when its result would change "
    "your answer — prefer retrieval over memory for questions grounded in "
    "the user's configured knowledge sources. Don't call tools speculatively. "
    "If the user asks for a specific tool, use it."
)


class GenerationInProgress(Exception):
    """Raised by `start_generation` when the conv already has a live task.

    Mapped to HTTP 409 by the route layer. The UI gate (placeholder
    keeps the send button disabled) makes this rare, but the
    exception is the defensive layer if a duplicate POST slips
    through.
    """


@dataclass
class GenerationState:
    """Shared state between the producer task and SSE consumers.

    Attributes:
        conversation_id: The chat the generation belongs to.
        events: Append-only log of (event_name, html_payload) tuples.
            Producer appends; consumers index into the list to
            replay or tail. Order matches what the original 12d/12e
            SSE stream would have yielded.
        done: True once the producer has emitted its final event
            (done OR error). Consumers exit their loop when this is
            True and they've drained all events.
        cond: Signal the producer fires after each append (and after
            setting done). Consumers `await cond.wait()` between
            drains.
        task: The asyncio.Task driving the producer. Held so the
            done-callback has a reference to inspect.
    """

    conversation_id: int
    events: list[tuple[str, str]] = field(default_factory=list)
    done: bool = False
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None


# Single-process registry. Keyed by conversation_id.
#
# A multi-worker uvicorn deployment would lose cross-worker visibility
# of this dict — but the app is built for single-user, single-process
# local use, so that's not a concern. See
# `docs/plans/phase12g-resumable-generation.md` §Known limitations.
live_generations: dict[int, GenerationState] = {}


def _sse(payload: str, event: str | None = None) -> str:
    """Format an HTML payload as a single SSE message.

    Mirrors `app.routes._sse` (kept duplicated to avoid circular
    imports — routes.py imports from this module). Each newline in
    `payload` becomes its own `data:` line per the SSE spec.
    """
    prefix = f"event: {event}\n" if event else ""
    lines = payload.split("\n") if payload else [""]
    data_lines = "".join(f"data: {line}\n" for line in lines)
    return f"{prefix}{data_lines}\n"


async def _emit(state: GenerationState, event: str, payload: str) -> None:
    """Append one SSE event to the state and wake all consumers.

    Holding `state.cond` for the append + notify makes the wake-up
    atomic relative to the consumer's drain-and-wait — there's no
    window where a consumer can see `len(events)` unchanged and then
    miss the notify.
    """
    async with state.cond:
        state.events.append((event, payload))
        state.cond.notify_all()


async def emit_ollama_error(
    state: GenerationState,
    exc: OllamaUnavailable | OllamaProtocolError,
) -> None:
    """Emit an SSE ``error`` event describing an Ollama-layer failure.

    Replaces four near-identical ``except`` blocks across
    ``_run_generation``.
    """
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

    Phase 12e.1 safety net. The producer's outer ``finally`` calls
    this exactly once on every exit path. When ``persisted_or_errored``
    is True (normal completion OR an Ollama error already wrote/emitted)
    this is a no-op. Otherwise we drop a ``(response interrupted)``
    bubble — or the partial token buffer if any tokens streamed —
    so the chat panel has *something* to render after a reload.

    Pulled out for the same reason as ``emit_ollama_error``: keeping the
    safety-net shape in one place so a duplicate finally-body can't drift.
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

    Producer's last act on every exit path. ``notify_all`` is safe
    when there are zero consumers; a future consumer that attaches
    after this point still sees ``state.done`` and exits its drain
    loop without waiting on the condition.
    """
    async with state.cond:
        state.done = True
        state.cond.notify_all()


async def consume_generation(
    state: GenerationState,
) -> AsyncIterator[str]:
    """Yield SSE events from a state, replaying from index 0 then tailing.

    Used by the SSE endpoint to attach a consumer. New consumers
    (e.g., a reloaded page) see every event that's already been
    emitted; an early-attached consumer iterates in lock-step with
    the producer. `cond` provides cross-task signalling so the
    consumer doesn't busy-poll.
    """
    pos = 0
    while True:
        # Drain new events without holding the lock so the producer
        # isn't blocked on us.
        while pos < len(state.events):
            event, payload = state.events[pos]
            yield _sse(payload, event=event)
            pos += 1
        if state.done:
            return
        # Wait for the next event. Take the lock for the recheck —
        # the producer takes the same lock when it notifies, so if
        # we got here with the lock held there's no window in which
        # we'd miss a signal that just fired.
        async with state.cond:
            if state.done or pos < len(state.events):
                continue
            await state.cond.wait()


async def consume_finished(
    db: sqlite3.Connection, conversation_id: int
) -> AsyncIterator[str]:
    """Emit a single done event for a finished/missing generation.

    Used when a reload's GET /stream lands AFTER the generation
    finished and was removed from the registry. The placeholder
    rendered by the chat panel needs a `done` event to close
    cleanly — otherwise the streaming-dots animation hangs forever.

    Reads the last assistant row and yields it as the done event's
    OOB-swap payload — same shape as the live happy-path done event,
    so HTMX swaps the placeholder out to the persisted bubble.
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
    # Defensive: no assistant row exists at all. Emit an empty
    # assistant bubble OOB-swap so the placeholder at least closes —
    # better a blank bubble than a forever-streaming placeholder.
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
    system_prompt_override: str | None = None,
    tool_allowlist: frozenset[str] | None = None,
    think: bool | None = None,
    num_ctx: int | None = None,
) -> GenerationState:
    """Register a GenerationState and spawn the producer task.

    Always runs the single-agent producer ``_run_generation``. When the
    caller is invoking a named agent (phase 16), it passes the agent's
    ``model`` (as ``model``), ``system_prompt_override``, and
    ``tool_allowlist``; otherwise those default to None and the producer
    runs the ordinary per-chat-chips plain-chat path.

    Args:
        system_prompt_override: The invoked agent's system prompt, injected
            on every turn. None for Normal chat (the producer falls back to
            its tool-use nudge, and only when tools are present).
        tool_allowlist: The invoked agent's permitted tool names. None for
            Normal chat (the producer filters by the per-chat tool/RAG
            chips). An empty frozenset means "agent with no tools".
        think: The invoked agent's Ollama ``think`` flag (True/False), or
            None for Normal chat (omit the flag → Ollama default). Passed
            straight through to the chat calls.

    Raises:
        GenerationInProgress: if a generation is already running for
            this conversation. Raised SYNCHRONOUSLY before the first
            ``await``, so callers' ``except GenerationInProgress``
            still catches it before any dispatch work fires. The route
            maps this to HTTP 409.
    """
    # In-flight guard must fire BEFORE the first await — callers'
    # try/except GenerationInProgress depends on it raising
    # synchronously. Anything after `await` is a coroutine
    # suspension point and the exception would arrive in a
    # different control flow shape.
    existing = live_generations.get(conversation_id)
    if existing is not None and not existing.done:
        raise GenerationInProgress(
            f"Conversation {conversation_id} already has a generation in flight"
        )

    state = GenerationState(conversation_id=conversation_id)
    # Register BEFORE create_task so the registry is populated by
    # the time control returns to the caller. A done entry from a
    # previous turn gets evicted here — it's no longer the live
    # state for this conversation. Done entries remain in the
    # registry until replaced (so a fresh GET /stream from a slow
    # reload can still replay the recently-finished events), which
    # is also why we evict on new-gen-start rather than on
    # task-done.
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
            system_prompt_override=system_prompt_override,
            tool_allowlist=tool_allowlist,
            think=think,
            num_ctx=num_ctx,
        )
    )
    state.task.add_done_callback(_make_done_callback(conversation_id))
    return state


def _make_done_callback(conversation_id: int):
    """Build the per-conversation done-callback for a generation task.

    Only responsibility now (phase 12g): surface any unhandled task
    exception via logging — without this, an exception inside a
    fire-and-forget task gets silently swallowed by asyncio.

    The state is NOT removed from `live_generations` on done. It
    stays until the next `start_generation` for this conversation
    evicts it. This lets a slow reload that lands after the gen
    finished still replay the event log via `consume_generation`,
    which would otherwise have to fall through to the lossy
    `consume_finished` path.
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

    return cb


# ---------------------------------------------------------------------------
# Producer (the body that was `_stream_assistant_reply` in phase 12e.1)
# ---------------------------------------------------------------------------


def _build_history_payload(
    history: list, system_prompt: str | None = None
) -> list[dict]:
    """Turn Message dataclasses into the wire format Ollama expects.

    Identical semantics to `app.routes._build_history_payload` in
    phase 12d; moved here because `_run_generation` is the only
    caller after phase 12g.

    Args:
        history: Conversation Message rows to serialize.
        system_prompt: When set, a ``{"role": "system", ...}`` message
            is prepended so the model sees it before any turn. Used by
            the tool-enabled generation path (phase 15) to nudge
            retrieval; left ``None`` for title generation, which has no
            business carrying a tool-use policy. Conversation rows never
            store a ``system`` role, so prepending here can't duplicate
            one already present in ``history``.

    Returns:
        A list of dicts in Ollama's ``/api/chat`` ``messages`` shape.
    """
    out: list[dict] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    # When we drop a corrupt tool_call row, we must ALSO drop the
    # paired tool_result that follows it — otherwise Ollama sees a
    # role="tool" message with no preceding assistant+tool_calls and
    # rejects the whole chat with a 400. `_run_generation` writes
    # call/result rows strictly interleaved (one result immediately
    # after each call), so "skip the next tool_result" is the right
    # pairing rule. A non-result row resets the flag so we never
    # wrongly skip a future unrelated result.
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
            # Decode the JSON envelope so the model sees plain text, not
            # the {"text": ..., "sources": [...]} structure (phase 12h).
            # Legacy pre-12h rows are plain text already; decode_tool_result
            # round-trips them cleanly via its fallback.
            out.append({
                "role": "tool",
                "content": decode_tool_result(m.content).text,
            })
        elif m.role in ("user", "assistant"):
            skip_next_result = False
            out.append({"role": m.role, "content": m.content})
        else:
            # Unknown/legacy role (e.g. research_findings / review_verdict
            # rows left by the removed agentic loop). Drop silently so a
            # chat that used a since-removed feature still serializes into
            # a valid Ollama payload instead of shipping an invalid role.
            skip_next_result = False
    return out


async def _maybe_emit_title(
    state: GenerationState,
    client: httpx.AsyncClient,
    db: sqlite3.Connection,
    conversation_id: int,
) -> None:
    """Fire the auto-titler after the 1st through 3rd assistant reply.

    Phase 11d behavior, ported to the new producer architecture.
    Emits zero or one `title` SSE event via `_emit` (consumers see
    it before the final `done` event because we emit in order).

    Silent skips (no event):
      - The chat has been manually renamed (`name_locked`).
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

    full_history = queries.list_messages(db, conversation_id)
    try:
        title = await ollama.generate_title(
            client,
            conversation.model,
            _build_history_payload(full_history),
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

    # Bare `hx-swap-oob="true"` tells HTMX to match by id and swap in
    # place — the existing `#chat-{id}` row gets replaced with this
    # renamed version when the SSE `title` event lands.
    # Phase 17: pass the owning project so the rendered row's link URL
    # is project-scoped (matches the canonical URL the user is on).
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


def _chat_tool_specs(
    db: sqlite3.Connection, conversation_id: int
) -> list[dict]:
    """Tool specs for a Normal (non-agent) turn — per-chat chips filter them.

    The per-chat tool chips (phase 15) and per-server RAG chips (phase 15b)
    select which registered tools the model sees. ``query_rag`` has no chip of
    its own — its sole gate is whether any RAG server is enabled for the chat;
    when at least one is, its ``source`` description is rebuilt from the
    enabled servers.

    Args:
        db: Open SQLite connection.
        conversation_id: Chat whose chip state filters the registry.

    Returns:
        Ollama-shaped tool specs, possibly empty.
    """
    enabled_names = set(
        queries.get_enabled_tool_names(db, conversation_id, list(TOOLS.keys()))
    )
    all_rag_servers = _rag_module.list_servers(db)
    enabled_rag_names = set(
        queries.get_enabled_rag_server_names(
            db, conversation_id, [s.name for s in all_rag_servers]
        )
    )
    enabled_rag_servers = [
        s for s in all_rag_servers if s.name in enabled_rag_names
    ]

    specs: list[dict] = []
    for spec in tool_specs_for_ollama():
        name = spec["function"]["name"]
        if name == RAG_TOOL_NAME:
            if not enabled_rag_servers:
                continue  # All server chips off → exclude query_rag.
            spec = copy.deepcopy(spec)  # Don't mutate the global registry.
            spec["function"]["parameters"]["properties"]["source"][
                "description"
            ] = build_source_description(enabled_rag_servers)
            specs.append(spec)
            continue
        if name not in enabled_names:
            continue
        specs.append(spec)
    return specs


def _agent_tool_specs(
    db: sqlite3.Connection, allowlist: frozenset[str]
) -> list[dict]:
    """Tool specs for an invoked agent — its allowlist is the only gate.

    The agent's per-chat chips do NOT apply; the allowlist alone decides which
    registered tools are offered. ``query_rag`` is included only when it's in
    the allowlist AND at least one RAG server is configured (using all
    configured servers for the ``source`` description — there are no per-agent
    server chips).

    Args:
        db: Open SQLite connection.
        allowlist: Tool names the agent may call. Empty → no tools.

    Returns:
        Ollama-shaped tool specs, possibly empty.
    """
    if not allowlist:
        return []
    all_rag_servers = _rag_module.list_servers(db)
    specs: list[dict] = []
    for spec in tool_specs_for_ollama():
        name = spec["function"]["name"]
        if name not in allowlist:
            continue
        if name == RAG_TOOL_NAME:
            if not all_rag_servers:
                continue
            spec = copy.deepcopy(spec)  # Don't mutate the global registry.
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
    system_prompt_override: str | None = None,
    tool_allowlist: frozenset[str] | None = None,
    think: bool | None = None,
    num_ctx: int | None = None,
) -> None:
    """Producer body — runs the LLM and writes events to the state.

    Ported from phase 12e.1's `_stream_assistant_reply` in routes.py.
    Differences:
      - No `yield`; this is a plain async function. Every
        `yield _sse(payload, event=ev)` is now
        `await _emit(state, ev, payload)`.
      - Final block sets `state.done = True` and notifies one last
        time so any pending consumers wake up to drain and exit.
      - Phase 12e.1's safety-net try/finally is kept for
        catastrophic failures (CancelledError / GeneratorExit on
        the task during server shutdown, unhandled exceptions).

    See the original docstring at `app/routes.py:878` (commit
    `319dd40`) for loop semantics — they're unchanged.
    """
    working_history = list(history)
    # Build the candidate tool specs, then gate on model capability.
    # `model_supports_tools` returns False on cache/network failure, which
    # collapses to tools_payload = None (omits the key entirely).
    #
    # Two paths:
    #   - Agent turn (phase 16, `tool_allowlist is not None`): the invoked
    #     agent's allowlist governs the specs (an empty frozenset → no tools).
    #   - Normal turn (`tool_allowlist is None`): the per-chat tool/RAG chips
    #     filter the registry, exactly as before.
    if tool_allowlist is not None:
        _enabled_specs = _agent_tool_specs(db, tool_allowlist)
    else:
        _enabled_specs = _chat_tool_specs(db, conversation_id)

    tools_payload = (
        _enabled_specs
        if _enabled_specs and await ollama.model_supports_tools(client, model)
        else None
    )

    # Phase 17: resolve the owning project up-front. Needed for two
    # things: (1) the per-project workspace root for file tools, and
    # (2) the per-project system prompt injected on Normal-chat turns.
    try:
        _project = queries.get_project_for_conversation(db, conversation_id)
    except LookupError:
        # Defensive: post-phase-17, every chat has a project. Degrade
        # to None so the turn still runs without project-scoped extras.
        _project = None

    # System prompt selection:
    #   - Agent turn: always inject the agent's prompt — it's the agent's
    #     identity, and a no-tools agent (Content Generator) still needs it.
    #     The per-project prompt is intentionally IGNORED here; agents have
    #     purposeful prompts of their own.
    #   - Normal turn: combine the project's system prompt (when set) with
    #     the tool-use nudge (when tools are actually sent). With neither,
    #     omit the system message entirely — keeps plain chat byte-identical
    #     to pre-project-prompt behavior for users who haven't set one.
    if tool_allowlist is not None:
        system_prompt = system_prompt_override
    else:
        parts: list[str] = []
        if _project is not None and _project.system_prompt:
            parts.append(_project.system_prompt)
        if tools_payload is not None:
            parts.append(SINGLE_AGENT_SYSTEM_PROMPT)
        system_prompt = "\n\n".join(parts) if parts else None

    turn_id = str(time.monotonic_ns())
    card_id = render.card_id_for(turn_id)
    list_id = f"{card_id}-list"
    summary_id = f"{card_id}-summary"
    call_index = 0
    in_flight: dict[str, dict] = {}

    # Phase 12e.1 safety net: hoist `chunks` and the
    # `persisted_or_errored` flag so the function-level try/finally
    # below catches CancelledError / GeneratorExit at any phase and
    # persists a partial assistant row before re-raising.
    chunks: list[str] = []
    persisted_or_errored = False

    # Phase 17: scope the file tools to this chat's project workspace
    # for the duration of the turn. The ContextVar set/reset wraps the
    # whole producer body — file-tool calls inside this region see the
    # per-project root via `current_workspace_root` (with fallback to
    # FILE_TOOL_ROOT when the project workspace can't be resolved, e.g.
    # FILE_TOOL_ROOT unset or the defensive None-project path above).
    if _project is not None:
        ws_root = project_workspace_root(_project)
        if ws_root is not None:
            # Lazily create so a brand-new project's workspace exists
            # by the time a tool tries to read/write within it.
            ws_root.mkdir(parents=True, exist_ok=True)
    else:
        ws_root = None
    ws_token = current_workspace_root.set(ws_root)

    try:
        for iteration in range(tool_iteration_cap):
            try:
                tool_calls, _content = await ollama.maybe_tool_call(
                    client,
                    model,
                    _build_history_payload(working_history, system_prompt),
                    tools=tools_payload,
                    temperature=temperature,
                    think=think,
                    num_ctx=num_ctx,
                )
            except (OllamaUnavailable, OllamaProtocolError) as e:
                # Set BEFORE the await — await is a cancellation point;
                # the outer finally must see the flag if cancellation
                # lands inside emit_ollama_error.
                persisted_or_errored = True
                await emit_ollama_error(state, e)
                return

            if not tool_calls:
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

            working_history = queries.list_messages(db, conversation_id)
        else:
            # Iteration cap hit: persist apology + emit done with
            # frozen-row OOBs for any unpaired calls.
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
        try:
            async for chunk in ollama.stream_chat(
                client, model,
                _build_history_payload(working_history, system_prompt),
                temperature=temperature,
                think=think,
                num_ctx=num_ctx,
            ):
                if chunk.content:
                    chunks.append(chunk.content)
                    await _emit(state, "token", html.escape(chunk.content))
                if chunk.done:
                    # Final chunk carries Ollama's reported token counts
                    # for this turn (None if Ollama didn't report them,
                    # e.g. on a full prompt-cache hit).
                    prompt_tokens = chunk.prompt_tokens
                    eval_tokens = chunk.eval_tokens
                    break
        except (OllamaUnavailable, OllamaProtocolError) as e:
            # Flag-before-await mirrors the probe-loop branch above.
            persisted_or_errored = True
            await emit_ollama_error(state, e)
            return

        full_text = "".join(chunks)
        if on_complete == "append":
            message = queries.append_message(
                db, conversation_id, "assistant", full_text,
                prompt_tokens=prompt_tokens,
                eval_tokens=eval_tokens,
            )
        else:  # "replace"
            message = queries.replace_last_assistant_message(
                db, conversation_id, full_text,
                prompt_tokens=prompt_tokens,
                eval_tokens=eval_tokens,
            )
        # Persisted — outer finally must not double-write a partial.
        # Set BEFORE any further awaits, since await is a suspension
        # point where cancellation can land.
        persisted_or_errored = True

        # Phase 11d: title fires BEFORE the final done event so the
        # consumer's SSE connection is still attached (the OOB
        # outerHTML in done removes the placeholder which closes the
        # EventSource — anything emitted after that is dropped).
        if on_complete == "append":
            await _maybe_emit_title(state, client, db, conversation_id)

        # Final done event with the persisted message bubble +
        # tool-card past-tense summary OOB.
        done_card_oobs = render.render_done_card_oobs(
            call_index, in_flight, summary_id
        )
        final_html = templates.get_template("_message.html").render(
            message=message,
            swap_target=f"#assistant-stream-{conversation_id}",
        )
        await _emit(state, "done", done_card_oobs + final_html)
    finally:
        # Phase 17: reset the workspace ContextVar BEFORE the safety-net
        # helpers run. Resetting here (not in a nested finally) keeps the
        # producer body's try/finally pyramid shallow; the helpers
        # below don't depend on the per-turn workspace, so the order is
        # safe.
        current_workspace_root.reset(ws_token)
        # Phase 12e.1 safety net. The helpers fire on every exit path —
        # CancelledError, GeneratorExit, unhandled exception, normal
        # completion, OR Ollama errors. `maybe_persist_partial` no-ops
        # when `persisted_or_errored` is True; `signal_done` always
        # wakes pending consumers so they exit their drain loop instead
        # of waiting forever on a task that's gone.
        maybe_persist_partial(
            db, conversation_id, on_complete, chunks, persisted_or_errored
        )
        await signal_done(state)
