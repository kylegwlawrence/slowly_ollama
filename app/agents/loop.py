"""Phase 13d: orchestrator for the agentic multi-agent loop.

Coexists with ``app/generation.py:_run_generation`` — the
single-agent producer. The dispatcher in ``start_generation`` (added
in 13d.3) picks between them based on the ``agentic_mode`` setting
and the chat's model capability.

Same protocol as ``_run_generation``:

- Drives a ``GenerationState``; never yields directly.
- Emits SSE events via ``_emit``.
- Persists every step as message rows
  (``user`` / ``tool_call`` / ``tool_result`` / ``research_findings``
  / ``review_verdict`` / ``assistant``).
- Marks ``state.done = True`` in a ``finally`` via ``signal_done``.
- Re-uses the same producer-runtime helpers
  (``emit_ollama_error``, ``maybe_persist_partial``, ``signal_done``)
  so any bug fix to one path benefits both.

Loop shape:

  empty card shell
  ↓
  for iteration in 1..3:
    iteration-start (header row + summary swap)
    research pass:
      loop up to 5 tool calls
      capture findings (last non-tool text response, or synthesized
        empty-findings string if the model hit the inner cap without
        producing prose)
      persist research_findings row + emit findings row
    review pass:
      single maybe_tool_call with REVIEW_TOOL_SPECS
      parse_verdict → "passed" or "failed"
      persist review_verdict row + emit verdict row
      passed → break; failed → append feedback to intra_turn, continue
  else (for-else: ran full cap without break):
    max_iterations_reached = True; emit max-iterations badge
  ↓
  generation pass: stream_chat from findings → tokens → assistant row
  ↓
  done event (past-tense summary swap + persisted message bubble)
"""

import html
import json
import logging
import sqlite3
import time
from typing import Literal

import httpx

from app import ollama, queries, render
from app.agents import AGENTIC_ITERATION_CAP
from app.agents.prompts import (
    GENERATION_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
)
from app.agents.verdict_tools import REVIEW_TOOL_SPECS, parse_verdict
from app.generation import (
    GenerationState,
    _emit,
    _maybe_emit_title,
    emit_ollama_error,
    maybe_persist_partial,
    signal_done,
)
from app.ollama import OllamaProtocolError, OllamaUnavailable
from app.templates import templates
from app.tools import (
    encode_tool_call,
    encode_tool_result,
    format_tool_invocation,
    run_tool,
    tool_specs_for_ollama,
)

logger = logging.getLogger(__name__)


# Module-private alias for the package-level cap so existing in-file
# references stay terse. The shared value lives in
# ``app/agents/__init__.py`` because phase 13f's historic-render path
# (``app/render.py``) also needs to read it — putting it in loop.py
# would cycle (loop imports render). See the comment over
# ``AGENTIC_ITERATION_CAP`` in ``app/agents/__init__.py``.
#
# Deliberately NOT aliased to single-agent's ``_TOOL_ITERATION_CAP``
# in ``app/generation.py`` — that one is the per-turn cap for the
# single-agent path; this one is the per-pass cap inside the
# agentic loop's research stage. Same number today, different
# concepts. Aliasing would couple future changes: raising
# single-agent to 8 would silently make agentic 8 iterations ×
# 5 tool calls each = 40 tool calls per turn.
_AGENTIC_ITERATION_CAP = AGENTIC_ITERATION_CAP

# Per-iteration cap on research tool calls. Hit means we hand off to
# review with whatever findings the model produced last (or a
# synthesized empty-findings string).
_RESEARCH_TOOL_CAP_PER_PASS = 5


# ---------------------------------------------------------------------------
# Payload builders — kept module-private (underscore prefix) because they
# only make sense inside the orchestrator's three discrete agent calls.
# ---------------------------------------------------------------------------


def _filter_prior_history_for_research(messages: list) -> list[dict]:
    """Extract prior turns' user/assistant rows in Ollama wire format.

    Strips out ``tool_call`` / ``tool_result`` / ``research_findings``
    / ``review_verdict`` rows — they were means-of-production for
    prior answers and don't need to appear in a new turn's research
    context. The most recent user row (the current question) is
    INCLUDED so callers don't have to special-case it.

    Args:
        messages: All rows from ``queries.list_messages`` for this
            conversation, oldest-first.

    Returns:
        A list of dicts in Ollama's ``messages`` shape, containing
        only user + assistant rows.
    """
    out = []
    for m in messages:
        if m.role in ("user", "assistant"):
            out.append({"role": m.role, "content": m.content})
    return out


def _build_research_payload(
    prior_history: list[dict],
    intra_turn: list[dict],
) -> list[dict]:
    """Assemble the research agent's per-iteration Ollama payload.

    Layout:
      system: research system prompt
      <prior turns' user/assistant rows>
      <current turn's intra-iteration history: tool calls/results,
       findings from prior passes, and any review-feedback messages
       the orchestrator pushed onto intra_turn at iteration
       boundaries>

    The ``intra_turn`` list accumulates ACROSS iterations within the
    same turn. Review feedback is appended by the orchestrator as a
    user-role message so it flows into every ``maybe_tool_call``
    invocation within the iteration — not just the first one.
    """
    return [
        {"role": "system", "content": RESEARCH_SYSTEM_PROMPT},
        *prior_history,
        *intra_turn,
    ]


def _build_review_payload(user_message: str, findings: str) -> list[dict]:
    """Assemble the review agent's ephemeral Ollama payload.

    Review sees NO prior chat history and NO intra-turn artifacts
    besides the findings — its job is to judge the findings against
    the question, full stop.
    """
    return [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Original user question:\n{user_message}\n\n"
                f"Research agent's findings:\n{findings}\n\n"
                "Decide if these findings are sufficient. Call"
                " mark_passed or request_more_research."
            ),
        },
    ]


def _build_generation_payload(user_message: str, findings: str) -> list[dict]:
    """Assemble the generation agent's ephemeral Ollama payload.

    Generation sees only the original question + approved findings,
    no tools. It writes the user-visible final answer.
    """
    return [
        {"role": "system", "content": GENERATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Original user question:\n{user_message}\n\n"
                f"Approved research findings:\n{findings}\n\n"
                "Write the answer to the user's question using only"
                " the findings above."
            ),
        },
    ]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def _run_agentic_generation(
    *,
    state: GenerationState,
    client: httpx.AsyncClient,
    db: sqlite3.Connection,
    conversation_id: int,
    model: str,
    history: list,
    on_complete: Literal["append", "replace"],
) -> None:
    """Producer body for the agentic three-agent loop.

    Same shape as ``app/generation.py:_run_generation``:
    no yield, writes events via ``_emit``, marks ``state.done`` in
    finally via ``signal_done``. Drives the research → review →
    generation loop instead of the single tool-calling round trip.

    See ``docs/plans/phase13-agentic-loop.md`` §Architecture for the
    high-level diagram.
    """
    if not history or history[-1].role != "user":
        # Defensive — start_generation is always called right after
        # append_message("user", ...). If we get here, something
        # upstream is broken; bail with a clear error.
        await _emit(
            state, "error",
            '<div class="error">Agentic loop launched without a'
            ' pending user message.</div>',
        )
        await signal_done(state)
        return

    user_message = history[-1].content
    prior_history = _filter_prior_history_for_research(history)
    intra_turn: list[dict] = []
    final_findings: str = ""
    iterations_run = 0

    turn_id = str(time.monotonic_ns())
    card_id = f"tool-card-{turn_id}"
    list_id = f"{card_id}-list"
    summary_id = f"{card_id}-summary"

    chunks: list[str] = []
    persisted_or_errored = False

    try:
        # Emit the empty card shell once. Subsequent OOB rows append
        # into #{list_id}.
        await _emit(
            state,
            "tool-call",
            render.render_agentic_card_shell(
                card_id=card_id,
                list_id=list_id,
                summary_id=summary_id,
                conversation_id=conversation_id,
            ),
        )

        for iteration in range(_AGENTIC_ITERATION_CAP):
            iteration_index = iteration + 1

            await _emit(
                state,
                "iteration-start",
                render.render_iteration_start(
                    iteration_index=iteration_index,
                    list_id=list_id,
                    summary_id=summary_id,
                ),
            )

            # === Research pass ===
            #
            # Snapshot the tool specs once per iteration; the registry
            # is module-level state and shouldn't change mid-pass.
            tool_specs = tool_specs_for_ollama()
            research_call_index = 0
            findings = ""

            for _ in range(_RESEARCH_TOOL_CAP_PER_PASS):
                payload = _build_research_payload(prior_history, intra_turn)
                try:
                    tool_calls, content = await ollama.maybe_tool_call(
                        client, model, payload, tools=tool_specs,
                    )
                except (OllamaUnavailable, OllamaProtocolError) as e:
                    persisted_or_errored = True
                    await emit_ollama_error(state, e)
                    return

                if not tool_calls:
                    findings = content.strip()
                    break

                for call in tool_calls:
                    name = call["name"]
                    arguments = call.get("arguments") or {}

                    queries.append_message(
                        db, conversation_id, "tool_call",
                        content=encode_tool_call(name, arguments),
                    )
                    # Mirror what `app.ollama.maybe_tool_call` would emit
                    # back on the next request — keeps the intra_turn
                    # history wire-format-correct so research sees its
                    # own prior calls.
                    intra_turn.append({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "function": {
                                "name": name,
                                "arguments": arguments,
                            }
                        }],
                    })

                    row_id = (
                        f"{card_id}-iter-{iteration_index}"
                        f"-row-{research_call_index}"
                    )
                    label = format_tool_invocation(name, arguments)
                    start_ms = int(time.time() * 1000)
                    live_row = render.ToolRowView(
                        id=row_id,
                        label=label,
                        elapsed_start_ms=start_ms,
                        elapsed_final_ms=None,
                        elapsed_display="0:00",
                    )
                    await _emit(
                        state,
                        "tool-call",
                        render.render_agentic_tool_row_append(
                            live_row=live_row,
                            list_id=list_id,
                        ),
                    )

                    result = await run_tool(name, arguments)
                    queries.append_message(
                        db, conversation_id, "tool_result",
                        content=encode_tool_result(result),
                    )
                    intra_turn.append({
                        "role": "tool",
                        "content": result.text,
                    })

                    end_ms = int(time.time() * 1000)
                    duration_ms = max(0, end_ms - start_ms)
                    frozen_row = render.ToolRowView(
                        id=row_id,
                        label=label,
                        elapsed_start_ms=None,
                        elapsed_final_ms=duration_ms,
                        elapsed_display=render.format_elapsed_mm_ss(
                            duration_ms
                        ),
                        sources=result.sources,
                    )
                    await _emit(
                        state,
                        "tool-result",
                        render.render_tool_card_row_freeze(frozen_row),
                    )
                    research_call_index += 1
            # for-else NOT used here: the inner-cap fall-through is the
            # normal hand-off-to-review path, not an exceptional bail.

            if not findings:
                # Inner cap hit OR model called zero tools and emitted
                # zero text. Synthesize a minimal findings line so the
                # review pass has something to judge.
                findings = (
                    f"(No findings produced; research agent called tools "
                    f"{research_call_index} times without summarising.)"
                )

            queries.append_message(
                db, conversation_id, "research_findings", findings,
            )
            intra_turn.append({"role": "assistant", "content": findings})
            await _emit(
                state,
                "research-findings",
                render.render_findings_row(
                    findings=findings,
                    iteration_index=iteration_index,
                    list_id=list_id,
                ),
            )

            # === Review pass ===
            review_payload = _build_review_payload(user_message, findings)
            try:
                verdict_calls, _ = await ollama.maybe_tool_call(
                    client, model, review_payload, tools=REVIEW_TOOL_SPECS,
                )
            except (OllamaUnavailable, OllamaProtocolError) as e:
                persisted_or_errored = True
                await emit_ollama_error(state, e)
                return

            decision = parse_verdict(verdict_calls)
            queries.append_message(
                db, conversation_id, "review_verdict",
                content=json.dumps({
                    "verdict": decision.verdict,
                    "message": decision.message,
                }),
            )
            await _emit(
                state,
                "review-verdict",
                render.render_verdict_row(
                    verdict_status=decision.verdict,
                    verdict_message=decision.message,
                    iteration_index=iteration_index,
                    list_id=list_id,
                ),
            )

            final_findings = findings
            iterations_run = iteration_index
            if decision.verdict == "passed":
                break

            # Failed — push feedback into intra_turn as a user
            # message so the NEXT iteration's research payload
            # includes it (and so EVERY maybe_tool_call inside that
            # iteration sees it, since intra_turn is rebuilt into
            # the payload on each call).
            intra_turn.append({
                "role": "user",
                "content": (
                    f"Review feedback on your last findings:\n"
                    f"{decision.message}\n\n"
                    "Continue researching to address the feedback."
                    " Do not repeat queries you already ran."
                ),
            })
        else:
            # for-else: completed all _AGENTIC_ITERATION_CAP without
            # a "passed" break → fall through to generation with the
            # last iteration's findings. Emits the visible
            # "(max reached)" badge into the marker span; the badge
            # survives the done-event's summary swap because the
            # marker is a sibling of the summary span.
            await _emit(
                state,
                "max-iterations",
                render.render_max_iterations_badge(card_id),
            )

        # === Generation pass ===
        generation_payload = _build_generation_payload(
            user_message, final_findings,
        )
        try:
            async for chunk in ollama.stream_chat(
                client, model, generation_payload,
            ):
                if chunk.content:
                    chunks.append(chunk.content)
                    await _emit(state, "token", html.escape(chunk.content))
                if chunk.done:
                    break
        except (OllamaUnavailable, OllamaProtocolError) as e:
            persisted_or_errored = True
            await emit_ollama_error(state, e)
            return

        full_text = "".join(chunks)
        if on_complete == "append":
            message = queries.append_message(
                db, conversation_id, "assistant", full_text,
            )
        else:
            message = queries.replace_last_assistant_message(
                db, conversation_id, full_text,
            )
        # Set BEFORE the next await — await is a cancellation point;
        # the outer finally's maybe_persist_partial must see the flag
        # if cancellation lands between here and the done emit.
        persisted_or_errored = True

        if on_complete == "append":
            await _maybe_emit_title(state, client, db, conversation_id)

        # Final done event: past-tense summary swap + persisted
        # message bubble that OOB-replaces the streaming placeholder.
        # The max-iterations marker (sibling of summary span) survives
        # this swap when present, so the "(max reached)" badge stays
        # visible alongside the past-tense summary.
        done_summary = render.render_agentic_done_summary(
            summary_id=summary_id,
            iterations_run=iterations_run,
        )
        final_html = templates.get_template("_message.html").render(
            message=message,
            swap_target=f"#assistant-stream-{conversation_id}",
        )
        await _emit(state, "done", done_summary + final_html)

    finally:
        # Same safety net as `_run_generation`. Fires on any non-
        # normal exit (CancelledError, GeneratorExit, unhandled
        # exception). maybe_persist_partial no-ops when
        # persisted_or_errored is True; signal_done always wakes
        # pending consumers.
        maybe_persist_partial(
            db, conversation_id, on_complete, chunks, persisted_or_errored,
        )
        await signal_done(state)
