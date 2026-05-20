"""Render-time grouping of message rows into renderable blocks.

The chat panel walks a flat list of `Message` rows but needs to render a
single aggregated `<details>` tool-card above each assistant turn that used
tools. Doing the grouping in Jinja would require stateful loop variables and
two passes over the messages; doing it here in Python keeps the template
simple (`{% for block in blocks %}` + a kind switch) and lets it be tested
in isolation.

This module is the home for *render-shaped* views of persisted rows. Pure
SQL helpers stay in `app/queries.py`; tool registry / execution stays in
`app/tools/`. The seam: queries return `Message` dataclasses, this module
re-groups them into blocks, templates consume blocks.
"""

import html
import time
from dataclasses import dataclass, field
from typing import ClassVar, Union

from app.queries import Message
from app.templates import templates
from app.tools import (
    Source,
    decode_tool_call,
    decode_tool_result,
    format_tool_invocation,
)


def card_id_for(turn_id: str) -> str:
    """Build the DOM id for a tool-card with the given turn id.

    Centralized so the live SSE-emitting path in `routes.py` and the
    historic-replay path in this module produce the same id format.
    Row ids extend this with `-row-{N}`.

    Args:
        turn_id: Live turns use `str(time.monotonic_ns())`; historic
            turns use `f"hist-{first_call_message_id}"`.

    Returns:
        A DOM id like `tool-card-12345678901234567` or
        `tool-card-hist-42`.
    """
    return f"tool-card-{turn_id}"


def summary_text(count: int, done: bool) -> str:
    """Render the card's summary phrase as one string.

    Single source of truth so verb / plural / ellipsis stay
    coordinated. Without this helper a refactor that touches one of
    the three pieces (e.g., dropping the ellipsis) leaves the other
    two in inconsistent states across the live and historic paths.

    Args:
        count: Number of tool invocations in this card.
        done: False while the model is still streaming (live ticking
            state); True once the assistant turn has finished.

    Returns:
        Strings like `"using 1 tool…"`, `"using 2 tools…"`,
        `"used 1 tool"`, `"used 2 tools"`.
    """
    verb = "used" if done else "using"
    noun = "tool" if count == 1 else "tools"
    suffix = "" if done else "…"
    return f"{verb} {count} {noun}{suffix}"


def format_elapsed_mm_ss(ms: int) -> str:
    """Format an elapsed-milliseconds duration as `m:ss`.

    Used by the tool-card row template for the initial server-rendered
    elapsed value. The browser's tick driver replaces this text on each
    second-boundary; the server-rendered value is the brief initial
    state before the first tick fires.

    For `ms < 60_000` the minutes digit stays single-digit (`0:00`,
    `0:42`). Multi-minute durations show as `1:05`, `62:05`, etc. The
    format mirrors stopwatch convention and matches what JS produces in
    the tick driver, so swapping a row from live → frozen doesn't shift
    the digit alignment.

    Args:
        ms: Non-negative elapsed milliseconds.

    Returns:
        A string like `"0:08"` or `"62:05"`.
    """
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


@dataclass(frozen=True)
class DedupedSource:
    """One source entry as the tool-row template renders it.

    Produced by :func:`dedup_sources` from the raw :class:`Source` list
    on a :class:`ToolRowView`. Kept in this module (not in
    ``app/tools``) because the dedup choice + display-suffix shape
    are render concerns — the tool itself doesn't know how the UI
    chooses to collapse chunks.

    Attributes:
        title: Document title, unmodified from the underlying ``Source``.
        meta: Parenthesized suffix to display after the title:

            - ``"(§Section)"`` when count == 1 and the chunk has a section
            - ``"(N chunks)"`` when count > 1 (section dropped on
              purpose; chunks from the same title may span sections)
            - ``""`` when count == 1 and there is no section
    """

    title: str
    meta: str


def dedup_sources(sources: list[Source]) -> list[DedupedSource]:
    """Collapse :class:`Source` entries by title, first-seen order.

    A RAG retrieval often returns several chunks from the same paper.
    Phase 12h's UI design (chosen by the user) is "one line per
    document"; the line shows the section when only one chunk exists,
    or a chunk count when multiple do. Chapter / page metadata are not
    rendered — see the phase 12h plan for rationale.

    Args:
        sources: Raw entries in retrieval (relevance) order.

    Returns:
        One :class:`DedupedSource` per unique title, preserving the
        relevance order of the first chunk seen for each title.
    """
    groups: dict[str, list[Source]] = {}
    order: list[str] = []
    for s in sources:
        if s.title not in groups:
            groups[s.title] = []
            order.append(s.title)
        groups[s.title].append(s)
    out: list[DedupedSource] = []
    for title in order:
        items = groups[title]
        if len(items) > 1:
            meta = f"({len(items)} chunks)"
        elif items[0].section:
            meta = f"(§{items[0].section})"
        else:
            meta = ""
        out.append(DedupedSource(title=title, meta=meta))
    return out


@dataclass(frozen=True)
class ToolRowView:
    """Precomputed view of one tool invocation row, ready for template render.

    Three states, mutually exclusive:

    - **live ticking** (`elapsed_start_ms` set, `elapsed_final_ms` is None):
      JS driver updates the elapsed text every second. `elapsed_display`
      is the initial `0:00` shown before the first tick.
    - **frozen** (`elapsed_final_ms` set, `elapsed_start_ms` is None):
      Final `m:ss` value; JS skips this row. Used for both historic
      replay and live-after-result-arrives.
    - **historic-unpaired** (both None): the call never resolved (loop
      bailed). `elapsed_display` is `"?"`; JS skips the row because
      `data-elapsed-start` is missing.

    Attributes:
        id: Stable DOM id (`tool-card-…-row-N` live; `tool-card-hist-…`
            historic). Used both as `<li id=…>` and as the
            `hx-swap-oob` selector when this view is sent as an OOB
            replacement.
        label: Human-readable invocation string from
            `format_tool_invocation`. Jinja autoescape handles HTML.
        elapsed_start_ms: Epoch milliseconds when the call was
            persisted, or None for non-ticking rows.
        elapsed_final_ms: Final duration in milliseconds, or None for
            still-running and historic-unpaired rows.
        elapsed_display: Initial text shown in the elapsed span. The
            JS driver overwrites it on each tick for live-ticking rows.
        sources: Phase 12h. Retrieved-source metadata for the tool
            row. Empty for tools without sources (e.g. ``current_time``)
            and for live (pending) rows; populated when the
            ``tool-result`` SSE event freezes the row, and when historic
            replay decodes a stored JSON envelope.
    """

    id: str
    label: str
    elapsed_start_ms: int | None
    elapsed_final_ms: int | None
    elapsed_display: str
    sources: list[Source] = field(default_factory=list)

    @property
    def deduped_sources(self) -> list[DedupedSource]:
        """Template-facing collapsed source list.

        Computed on demand rather than at construction time so test
        fixtures can build ``ToolRowView`` instances without paying
        the dedup cost, and so the underlying ``sources`` list
        remains the canonical record.
        """
        return dedup_sources(self.sources)


def _row_view_from_pair(
    call: Message,
    result: Message | None,
    row_id: str,
) -> ToolRowView:
    """Build a frozen / historic-unpaired row view from persisted rows.

    For historic replay only. Live rows are built inline at the call
    site in `routes.py` where the call's name + arguments are still in
    scope (no need to round-trip through JSON parsing).

    Args:
        call: A `tool_call` row whose `content` is the JSON-serialized
            `{"name": ..., "arguments": ...}` written in
            `_stream_assistant_reply`.
        result: The paired `tool_result` row, or None if the loop
            bailed before pairing.
        row_id: DOM id to attach to the row.

    Returns:
        A `ToolRowView` in either the *frozen* or *historic-unpaired*
        state.
    """
    # Forgiving fallback: a corrupt row still renders, with the label
    # showing "calling ?()". `_build_history_payload` in app.generation
    # treats the same None signal as "drop the row" since orphan
    # results would 400 Ollama — different recovery, same primitive.
    decoded_call = decode_tool_call(call.content)
    name, arguments = decoded_call if decoded_call is not None else ("?", {})
    label = format_tool_invocation(name, arguments)
    if result is None:
        return ToolRowView(
            id=row_id,
            label=label,
            elapsed_start_ms=None,
            elapsed_final_ms=None,
            elapsed_display="?",
        )
    decoded = decode_tool_result(result.content)
    duration_ms = int(
        (result.created_at - call.created_at).total_seconds() * 1000
    )
    return ToolRowView(
        id=row_id,
        label=label,
        elapsed_start_ms=None,
        elapsed_final_ms=duration_ms,
        elapsed_display=format_elapsed_mm_ss(duration_ms),
        sources=decoded.sources,
    )


@dataclass(frozen=True)
class MessageBlock:
    """A standalone user or assistant message, rendered with _message.html.

    Attributes:
        message: The persisted row to render.
        kind: Discriminator string consumed by the template's
            `{% if block.kind == ... %}` branch. Not a dataclass field so it
            doesn't show up in `__init__` or `__repr__` noise.
    """

    message: Message
    kind: ClassVar[str] = "message"


@dataclass(frozen=True)
class ToolBatchBlock:
    """One assistant turn's tool invocations, grouped for the aggregated card.

    Each element of `calls` is a `(tool_call_row, tool_result_row_or_None)`
    pair. The `None` case occurs when the streaming loop bailed at
    `_TOOL_ITERATION_CAP` (`app/routes.py:951-968`) before the final call
    could resolve — historic rendering shows `?` for elapsed in that row.

    Attributes:
        calls: The (call, result) pairs in invocation order. At least one.
        turn_id: A stable id derived from the first call's row id, used as
            the DOM id suffix so re-rendering the same conversation
            produces stable element ids (useful for test assertions and
            for browser focus persistence across reloads).
        kind: Template discriminator. Class-level constant.
    """

    calls: list[tuple[Message, Message | None]] = field(default_factory=list)
    turn_id: str = ""
    kind: ClassVar[str] = "tool_batch"

    @property
    def card_id(self) -> str:
        """DOM id of the <details> card element."""
        return card_id_for(self.turn_id)

    @property
    def list_id(self) -> str:
        """DOM id of the inner <ul> the rows live in."""
        return f"{self.card_id}-list"

    @property
    def summary_id(self) -> str:
        """DOM id of the <span> holding the summary phrase."""
        return f"{self.card_id}-summary"

    @property
    def rows(self) -> list[ToolRowView]:
        """Materialize ToolRowViews for each (call, result) pair.

        Used by the historic-render path; live OOB updates build views
        inline in `routes.py` where the call's name + arguments don't
        need to round-trip through JSON.
        """
        prefix = f"{self.card_id}-row"
        return [
            _row_view_from_pair(call, result, f"{prefix}-{i}")
            for i, (call, result) in enumerate(self.calls)
        ]

    @property
    def summary(self) -> str:
        """Past-tense summary phrase (historic rendering is always done)."""
        return summary_text(len(self.calls), done=True)


Block = Union[MessageBlock, ToolBatchBlock]


def group_messages_for_render(messages: list[Message]) -> list[Block]:
    """Walk messages, folding tool_call/tool_result runs into ToolBatchBlocks.

    Rules:
        - `tool_call` rows accumulate; each is paired with the *next*
          `tool_result` row that follows. A trailing unpaired call (the
          loop bailed) is appended with `result=None`.
        - The first non-tool row (`user` / `assistant`) flushes the batch
          ahead of itself.
        - End-of-list flush: if the message list ends mid-batch (no
          following assistant row — e.g., a crash mid-turn), the batch is
          still emitted rather than dropped.
        - A `tool_result` arriving with no pending call is treated as an
          orphan and skipped. The streaming loop only persists results
          after a call, so this shouldn't happen in practice; the
          permissive behavior keeps replay robust against any historic
          corruption.

    Args:
        messages: Rows from `queries.list_messages`, oldest-first.

    Returns:
        A list of `MessageBlock` / `ToolBatchBlock` instances in display
        order. Empty input yields empty output.
    """
    blocks: list[Block] = []
    pending_calls: list[tuple[Message, Message | None]] = []
    pending_unpaired: Message | None = None  # most recent unmatched call

    def flush_batch() -> None:
        nonlocal pending_calls, pending_unpaired
        if pending_unpaired is not None:
            pending_calls.append((pending_unpaired, None))
            pending_unpaired = None
        if pending_calls:
            turn_id = f"hist-{pending_calls[0][0].id}"
            blocks.append(
                ToolBatchBlock(calls=list(pending_calls), turn_id=turn_id)
            )
            pending_calls = []

    for m in messages:
        if m.role == "tool_call":
            # Defensive: two consecutive tool_calls without an intervening
            # result would mean the server batched calls without
            # persisting paired results. In practice the loop pairs
            # 1:1, but if we ever see this just push the previous as
            # unpaired and continue.
            if pending_unpaired is not None:
                pending_calls.append((pending_unpaired, None))
            pending_unpaired = m
        elif m.role == "tool_result":
            if pending_unpaired is not None:
                pending_calls.append((pending_unpaired, m))
                pending_unpaired = None
            # else: orphan result — silently skip (see docstring rationale)
        elif m.role in ("research_findings", "review_verdict"):
            # Phase 13a: these rows are persisted by the agentic
            # orchestrator (lands in 13d) but the proper rendering —
            # iteration headers, findings rows, verdict rows inside an
            # AgenticToolBatchBlock — lands in 13f. Until 13f teaches
            # this grouper to fold them into the new block type, skip
            # them here so they don't render as standalone MessageBlocks
            # (which would dump raw verdict JSON or unformatted findings
            # text into the chat panel).
            continue
        else:
            flush_batch()
            blocks.append(MessageBlock(message=m))

    flush_batch()  # end-of-list

    return blocks


# ---------------------------------------------------------------------------
# Tool-card OOB rendering — emitted by the producer layer at each phase of
# the tool-calling turn. Kept here (rather than in app/generation.py) so the
# template-render dance lives next to the view dataclasses, and so phase 13's
# agentic-loop producer can call the same helpers instead of duplicating the
# string-building logic. The producer stays in charge of WHEN to emit; render
# owns WHAT the OOB fragment looks like.
# ---------------------------------------------------------------------------


def render_tool_card_initial(
    *,
    card_id: str,
    list_id: str,
    summary_id: str,
    live_row: ToolRowView,
    conversation_id: int,
) -> str:
    """Render the first tool-call OOB: full <details> card + one row.

    The first call in a turn inserts the entire card as the streaming
    placeholder's preceding sibling via ``beforebegin:#assistant-stream-
    {conversation_id}``. Subsequent calls hit
    :func:`render_tool_card_row_append` instead.
    """
    return templates.get_template("_tool_card_shell.html").render(
        card_id=card_id,
        list_id=list_id,
        summary_id=summary_id,
        summary_text=summary_text(1, done=False),
        rows=[live_row],
        swap_oob=f"beforebegin:#assistant-stream-{conversation_id}",
    )


def render_tool_card_row_append(
    *,
    live_row: ToolRowView,
    list_id: str,
    summary_id: str,
    call_index: int,
) -> str:
    """Render the Nth (N>=2) tool-call OOB: row append + summary bump.

    Subsequent calls in the same turn append into the card's <ul> via
    ``beforeend:#{list_id}`` and OOB-swap the summary span to reflect
    the new count. The summary span swap uses ``outerHTML`` so the
    span element itself is replaced — not just its text.

    ``call_index`` is the zero-based count BEFORE this call landed, so
    the display count is ``call_index + 1`` (the new total).
    """
    row_html = templates.get_template("_tool_row.html").render(
        row=live_row,
        swap_oob=f"beforeend:#{list_id}",
    )
    summary_html = (
        f'<span id="{summary_id}" hx-swap-oob="outerHTML">'
        f"{html.escape(summary_text(call_index + 1, done=False))}"
        f"</span>"
    )
    return row_html + summary_html


def render_tool_card_row_freeze(frozen_row: ToolRowView) -> str:
    """Render the OOB that replaces a live ticking row with its frozen form.

    Emitted on each ``tool-result`` SSE event. ``swap_oob="outerHTML"``
    targets the row by its id, replacing the live variant (with
    ``data-elapsed-start``) with the frozen one (with
    ``data-elapsed-final``). The JS tick driver stops ticking the row
    on this swap because frozen rows have no ``data-elapsed-start``.
    """
    return templates.get_template("_tool_row.html").render(
        row=frozen_row,
        swap_oob="outerHTML",
    )


def render_done_card_oobs(
    call_count: int,
    in_flight: dict[str, dict],
    summary_id: str,
) -> str:
    """Build the tool-card OOB fragments that ride along with the ``done`` event.

    Two things must happen when the assistant turn finishes:

    1. The card summary span swaps from present-tense / ellipsis
       ("using N tool(s)…") to past-tense ("used N tool(s)").
    2. Any rows still missing a paired ``tool_result`` get OOB-replaced
       with a frozen variant carrying ``data-elapsed-final``. The JS
       tick driver only ticks rows that have ``data-elapsed-start``
       AND no ``data-elapsed-final``; once SSE closes (on ``done``)
       we never get another chance to freeze, so leftover live rows
       would tick forever.

    The ``in_flight`` branch is defensive — today's
    ``_run_generation`` awaits each ``run_tool`` synchronously and
    deletes from ``in_flight`` before the loop continues, so the dict
    is always empty when this runs. The branch exists for future
    failure modes where a row might be left live.

    Args:
        call_count: Number of tool invocations in the turn. ``0`` means
            no tools were called and this helper returns ``""``.
        in_flight: Map of row_id → info dict for any unfrozen rows.
            See above — empty in normal flows.
        summary_id: DOM id of the card's summary span.

    Returns:
        Concatenated OOB HTML fragments, ready to prepend to the
        ``done`` event's primary payload (the persisted message
        bubble's OOB swap).
    """
    if call_count == 0:
        return ""

    summary_html = (
        f'<span id="{summary_id}" hx-swap-oob="outerHTML">'
        f"{html.escape(summary_text(call_count, done=True))}"
        f"</span>"
    )

    if not in_flight:
        return summary_html

    now_ms = int(time.time() * 1000)
    frozen_rows_html = ""
    for row_id, info in in_flight.items():
        duration_ms = max(0, now_ms - info["start_ms"])
        frozen_row = ToolRowView(
            id=row_id,
            label=info["label"],
            elapsed_start_ms=None,
            elapsed_final_ms=duration_ms,
            elapsed_display=format_elapsed_mm_ss(duration_ms),
        )
        frozen_rows_html += render_tool_card_row_freeze(frozen_row)
    return summary_html + frozen_rows_html


# ---------------------------------------------------------------------------
# Agentic-mode render helpers (phase 13d)
#
# Companion to the single-agent tool-card helpers above. The agentic
# orchestrator emits one card per assistant turn with the same outer
# <details> shell but with iteration headers, findings rows, and
# verdict rows inside instead of a flat tool-row list. The historic
# replay path lands in 13f (AgenticToolBatchBlock + its template);
# these helpers cover the live SSE path.
# ---------------------------------------------------------------------------


def agentic_summary_text(iterations_run: int, *, done: bool) -> str:
    """Render the agentic-card summary phrase.

    Two states:

    - **live** (done=False): ``"researching…"`` for the empty shell
      (iterations_run==0); ``"researching (iteration N)…"`` once an
      iteration starts.
    - **done**: ``"ran N iteration(s)"`` — plural beyond 1.

    The max-iterations-reached signal is NOT part of the summary text;
    it lives in the sibling ``<span id="…-max-marker">`` so the
    visible "(max reached)" badge appears at cap-hit time (before
    generation streams) and survives the done-event's outerHTML swap
    on the summary span (the marker is a sibling, not a child).

    Args:
        iterations_run: Count for the past-tense phrasing. Pass 0 for
            the initial shell render (yields ``"researching…"``).
        done: False during the live loop; True for the final summary
            swap that rides along with the ``done`` SSE event.

    Returns:
        Plain text for the ``<span id="…-summary">`` element. Callers
        wrap it in the outerHTML OOB swap; HTML-escape at the boundary.
    """
    if not done:
        if iterations_run == 0:
            return "researching…"
        return f"researching (iteration {iterations_run})…"
    plural = "iteration" if iterations_run == 1 else "iterations"
    return f"ran {iterations_run} {plural}"


def render_agentic_card_shell(
    *,
    card_id: str,
    list_id: str,
    summary_id: str,
    conversation_id: int,
) -> str:
    """Render the empty agentic card shell — emitted once per turn.

    Subsequent iteration-start / tool-call / findings / verdict
    events OOB-append into ``#{list_id}`` and OOB-swap the summary
    span and the max-marker span. The shell is the swap unit only on
    first emission (``beforebegin:#assistant-stream-…``); after that
    the card lives in the DOM and downstream events target its
    children.
    """
    return templates.get_template("_tool_card_shell.html").render(
        card_id=card_id,
        list_id=list_id,
        summary_id=summary_id,
        summary_text=agentic_summary_text(0, done=False),
        rows=[],
        agentic=True,
        swap_oob=f"beforebegin:#assistant-stream-{conversation_id}",
    )


def render_iteration_start(
    *,
    iteration_index: int,
    list_id: str,
    summary_id: str,
) -> str:
    """Render the iteration-start OOB: header row + summary update.

    Two OOB fragments concatenated:

    - ``<li class="tool-card__iteration-header">`` appended to the
      card's <ul> via ``beforeend:#{list_id}``. The header is purely
      decorative; CSS hides the bullet via ``list-style: none``.
    - ``<span id="{summary_id}">`` replaces the current summary span
      with one reading ``"researching (iteration N)…"``.

    Built inline (no template) because each fragment is one element
    and the OOB attributes are the whole payload — a template would
    be more noise than signal.
    """
    header_html = (
        f'<li hx-swap-oob="beforeend:#{list_id}"'
        f' class="tool-card__iteration-header"'
        f' data-iteration="{iteration_index}">'
        f'Iteration {iteration_index}'
        f'</li>'
    )
    summary_html = (
        f'<span id="{summary_id}" hx-swap-oob="outerHTML">'
        f'{html.escape(agentic_summary_text(iteration_index, done=False))}'
        f'</span>'
    )
    return header_html + summary_html


def render_agentic_tool_row_append(
    *,
    live_row: ToolRowView,
    list_id: str,
) -> str:
    """Append a live tool row inside an agentic iteration.

    Mirrors :func:`render_tool_card_row_append` but emits ONLY the
    row HTML — no summary span swap. The iteration-start event
    already swapped the summary to ``"researching (iteration N)…"``
    and downstream tool rows within the same iteration don't change
    that phrasing. Same row template + swap target as the single-
    agent variant, so the JS tick driver picks the live row up the
    same way.
    """
    return templates.get_template("_tool_row.html").render(
        row=live_row,
        swap_oob=f"beforeend:#{list_id}",
    )


def render_findings_row(
    *,
    findings: str,
    iteration_index: int,
    list_id: str,
) -> str:
    """Render the research-findings OOB for one iteration.

    OOB-appends a ``<li class="tool-card__findings">`` to the card's
    <ul>. The text is markdown-rendered via the Jinja filter so the
    model's natural prose comes out formatted; the historic-replay
    template (sub-phase 13f) renders the same shape from the
    persisted ``research_findings`` row.
    """
    return templates.get_template("_findings_row.html").render(
        findings=findings,
        iteration_index=iteration_index,
        swap_oob=f"beforeend:#{list_id}",
    )


def render_verdict_row(
    *,
    verdict_status: str,
    verdict_message: str,
    iteration_index: int,
    list_id: str,
) -> str:
    """Render the review-verdict OOB for one iteration.

    OOB-appends a ``<li class="tool-card__verdict tool-card__verdict--{status}">``
    to the card's <ul>. ``verdict_status`` is ``"passed"`` or
    ``"failed"`` — the class modifier drives the border + background
    colour in CSS (added in 13f).
    """
    return templates.get_template("_verdict_row.html").render(
        verdict_status=verdict_status,
        verdict_message=verdict_message,
        iteration_index=iteration_index,
        swap_oob=f"beforeend:#{list_id}",
    )


def render_max_iterations_badge(card_id: str) -> str:
    """Fill the max-iterations sentinel span with visible badge text.

    Emitted only when ``_run_agentic_generation`` exhausted its
    iteration cap without a "passed" verdict. Targets the
    ``<span id="{card_id}-max-marker">`` placeholder
    ``render_agentic_card_shell`` planted inside the summary —
    avoiding a full ``<details>`` re-render that would clobber the
    rows already in the DOM.

    The marker is a SIBLING of ``#{card_id}-summary``, not a child,
    so the done-event's outerHTML swap on the summary span leaves
    the badge intact. That's how the "(max reached)" text stays
    visible from cap-hit through generation streaming into the
    final done state — without any double-rendering and without the
    summary text itself needing to know about max-iterations.

    ``data-max-iterations="true"`` doubles as a CSS hook for
    colouring the badge (added in 13f).
    """
    return (
        f'<span id="{card_id}-max-marker"'
        f' hx-swap-oob="outerHTML"'
        f' data-max-iterations="true">'
        f' (max reached)'
        f'</span>'
    )


def render_agentic_done_summary(
    *,
    summary_id: str,
    iterations_run: int,
) -> str:
    """OuterHTML swap on the summary span for the agentic ``done`` event.

    Flips ``"researching (iteration N)…"`` to past tense. Mirrors
    ``render_done_card_oobs`` for the single-agent flow but the
    summary phrasing is different (iterations vs. tool count) and
    there are no in-flight rows to freeze (research_findings and
    review_verdict rows are emitted with their final shape and
    don't need a freeze pass).

    The max-iterations-reached signal lives in the sibling
    ``#{card_id}-max-marker`` span, populated by
    :func:`render_max_iterations_badge` at cap-hit time. This swap
    targets the summary span only, so the marker (if present) stays
    visible alongside the past-tense summary.
    """
    return (
        f'<span id="{summary_id}" hx-swap-oob="outerHTML">'
        f'{html.escape(agentic_summary_text(iterations_run, done=True))}'
        f'</span>'
    )
