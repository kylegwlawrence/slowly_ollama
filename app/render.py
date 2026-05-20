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
import json
import time
from dataclasses import dataclass, field
from typing import ClassVar, Union

from app.agents import AGENTIC_ITERATION_CAP
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


@dataclass(frozen=True)
class AgenticIteration:
    """One research → review iteration in a historic agentic turn.

    Mirrors the live-SSE iteration grouping: research runs tool calls,
    produces findings, review verdicts the findings. Each iteration's
    pieces are persisted as separate `messages` rows; this dataclass
    re-collects them for the historic-render template.

    Attributes:
        index: 1-based iteration number. Drives the
            `data-iteration` attribute on the rendered header.
        tool_calls: (call, result) pairs from research's tool-calling
            inner loop. Same shape as :attr:`ToolBatchBlock.calls`.
        findings: The `research_findings` row for this iteration, or
            None when the iteration never produced one (defensive —
            shouldn't happen for completed turns).
        verdict: The `review_verdict` row, or None for the same reason
            (e.g. process crashed between findings persist and verdict
            persist).
    """

    index: int
    tool_calls: list[tuple[Message, Message | None]]
    findings: Message | None
    verdict: Message | None

    @property
    def verdict_status(self) -> str:
        """`"passed"` / `"failed"` / `"unknown"` (no verdict row)."""
        if self.verdict is None:
            return "unknown"
        try:
            payload = json.loads(self.verdict.content)
        except (json.JSONDecodeError, TypeError):
            return "unknown"
        status = payload.get("verdict") if isinstance(payload, dict) else None
        if status in ("passed", "failed"):
            return status
        return "unknown"

    @property
    def verdict_message(self) -> str:
        """Human-readable verdict text. Empty when no verdict row or
        when the persisted JSON is malformed."""
        if self.verdict is None:
            return ""
        try:
            payload = json.loads(self.verdict.content)
        except (json.JSONDecodeError, TypeError):
            return ""
        if isinstance(payload, dict):
            message = payload.get("message", "")
            return str(message) if message is not None else ""
        return ""

    def row_views(self, card_id: str) -> list["ToolRowView"]:
        """Materialize ToolRowViews for this iteration's (call, result) pairs.

        Row ids embed the iteration index AND the call-within-iteration
        index, matching the format the live SSE path uses
        (`{card_id}-iter-{N}-row-{M}`). Same-id parity matters for a
        mid-turn reload: the reconstructed historic DOM lines up with
        any not-yet-consumed SSE events still arriving from the live
        producer.

        Args:
            card_id: The owning AgenticToolBatchBlock's card_id, so
                the row ids are scoped to the same DOM subtree.

        Returns:
            One ToolRowView per (call, result) pair, in invocation
            order.
        """
        return [
            _row_view_from_pair(
                call, result,
                f"{card_id}-iter-{self.index}-row-{i}",
            )
            for i, (call, result) in enumerate(self.tool_calls)
        ]


@dataclass(frozen=True)
class AgenticToolBatchBlock:
    """One assistant turn's full agentic loop, grouped for historic replay.

    The companion to :class:`ToolBatchBlock` for the multi-agent flow.
    The template (`_agentic_tool_card.html`) renders the same outer
    `<details>` shell as the single-agent card but interleaves
    iteration headers, findings rows, and verdict rows with the
    tool-row list.

    Attributes:
        iterations: AgenticIteration entries in chronological order.
            At least one entry; max :data:`AGENTIC_ITERATION_CAP` (the
            orchestrator's hard cap).
        turn_id: Stable id for DOM ids; `f"hist-{first_row_id}"` so
            historic and live paths can produce matching card ids when
            the same turn is re-rendered.
        kind: Template discriminator. Class-level constant.
    """

    iterations: list[AgenticIteration] = field(default_factory=list)
    turn_id: str = ""
    kind: ClassVar[str] = "agentic_tool_batch"

    @property
    def max_iterations_reached(self) -> bool:
        """True when the loop exhausted the cap without a passed verdict.

        The live producer renders a "(max reached)" badge in this case
        via :func:`render_max_iterations_badge`; the historic template
        looks at this flag to render the same DOM shape from
        persisted rows. The condition mirrors the orchestrator's
        for-else branch — `AGENTIC_ITERATION_CAP` iterations elapsed
        and the last one's verdict was not `"passed"`.
        """
        if len(self.iterations) < AGENTIC_ITERATION_CAP:
            return False
        return self.iterations[-1].verdict_status != "passed"

    @property
    def card_id(self) -> str:
        return card_id_for(self.turn_id)

    @property
    def list_id(self) -> str:
        return f"{self.card_id}-list"

    @property
    def summary_id(self) -> str:
        return f"{self.card_id}-summary"

    @property
    def summary(self) -> str:
        """Past-tense summary phrase. The "(max reached)" tag is NOT
        included here — it lives in the sibling max-marker span on
        the live path; historic replay surfaces it the same way via
        the template, not by appending to this string."""
        return agentic_summary_text(
            len(self.iterations), done=True
        )


Block = Union[MessageBlock, ToolBatchBlock, AgenticToolBatchBlock]


_AGENTIC_ROLES = frozenset(
    {"tool_call", "tool_result", "research_findings", "review_verdict"}
)


def group_messages_for_render(messages: list[Message]) -> list[Block]:
    """Walk messages, folding tool-related runs into the right block type.

    Each contiguous run of tool-related rows (`tool_call` / `tool_result`
    plus phase 13's `research_findings` / `review_verdict`) is flushed
    as either a :class:`ToolBatchBlock` (the run has only tool calls
    and results — single-agent turn) or an :class:`AgenticToolBatchBlock`
    (the run contains at least one findings or verdict row — agentic
    turn). The first non-tool row (`user` / `assistant`) flushes the
    pending run ahead of itself.

    Rules:
        - Tool-related rows accumulate into a pending run; a non-tool
          row triggers a flush + emits a MessageBlock.
        - End-of-list flush handles crashed-mid-turn conversations
          (no closing assistant row).
        - A `tool_result` without a preceding `tool_call` is treated
          as an orphan and skipped — the streaming loop only writes
          results after calls, but historic DB corruption shouldn't
          break the panel render.

    Args:
        messages: Rows from `queries.list_messages`, oldest-first.

    Returns:
        A list of `MessageBlock` / `ToolBatchBlock` /
        `AgenticToolBatchBlock` instances in display order. Empty
        input yields empty output.
    """
    blocks: list[Block] = []
    pending_rows: list[Message] = []

    def flush_batch() -> None:
        nonlocal pending_rows
        if not pending_rows:
            return
        has_agentic = any(
            r.role in ("research_findings", "review_verdict")
            for r in pending_rows
        )
        if has_agentic:
            blocks.append(_build_agentic_block(pending_rows))
        else:
            block = _build_classic_tool_batch(pending_rows)
            # _build_classic_tool_batch returns None when the run
            # contained only orphan tool_result rows — see its
            # docstring. Drop instead of appending an empty card.
            if block is not None:
                blocks.append(block)
        pending_rows = []

    for m in messages:
        if m.role in _AGENTIC_ROLES:
            pending_rows.append(m)
        else:
            flush_batch()
            blocks.append(MessageBlock(message=m))

    flush_batch()  # end-of-list

    return blocks


def _build_classic_tool_batch(
    rows: list[Message],
) -> ToolBatchBlock | None:
    """Pair tool_call rows with the next tool_result; emit a ToolBatchBlock.

    Identical pairing semantics to the pre-13f grouping rules — just
    factored out so :func:`group_messages_for_render` can pick the
    block type cleanly. Returns ``None`` (rather than an empty card)
    when the run contained only orphan tool_result rows, since the
    pre-13f behaviour skipped those silently and we preserve it here.

    Args:
        rows: Tool-related rows in chronological order. Caller has
            already verified there are no findings/verdict rows in
            this run.
    """
    calls: list[tuple[Message, Message | None]] = []
    pending_call: Message | None = None
    for m in rows:
        if m.role == "tool_call":
            if pending_call is not None:
                # Defensive: back-to-back calls without an intervening
                # result mean a batched-calls codepath we don't have
                # today. Push the previous as unpaired.
                calls.append((pending_call, None))
            pending_call = m
        elif m.role == "tool_result":
            if pending_call is not None:
                calls.append((pending_call, m))
                pending_call = None
            # else: orphan result — silently skip.
    if pending_call is not None:
        calls.append((pending_call, None))
    if not calls:
        return None
    turn_id = f"hist-{calls[0][0].id}"
    return ToolBatchBlock(calls=calls, turn_id=turn_id)


def _build_agentic_block(rows: list[Message]) -> AgenticToolBatchBlock:
    """Slice rows into AgenticIteration entries.

    Iteration boundaries are `review_verdict` rows: each verdict
    closes an iteration. Tool calls / results between the previous
    verdict (or start) and the next `research_findings` row belong to
    that iteration's `tool_calls` list; the `research_findings` row
    immediately following the calls becomes the iteration's
    `findings`; the `review_verdict` row that follows becomes its
    `verdict`.

    Defensive behaviour for partial rows: if the run ends with
    pending tool calls, findings, or both — but no closing verdict —
    we still emit a final iteration with whatever pieces we have
    (verdict=None). This shouldn't happen on completed turns but
    keeps the panel rendering robust if a process died mid-loop.

    Args:
        rows: Tool-related rows in chronological order. Caller has
            verified that at least one findings or verdict row is
            present (otherwise this would be a classic batch).
    """
    iterations: list[AgenticIteration] = []
    pending_calls: list[tuple[Message, Message | None]] = []
    pending_call: Message | None = None
    current_findings: Message | None = None

    def commit_iteration(verdict: Message | None) -> None:
        nonlocal pending_calls, pending_call, current_findings
        if pending_call is not None:
            pending_calls.append((pending_call, None))
            pending_call = None
        iterations.append(AgenticIteration(
            index=len(iterations) + 1,
            tool_calls=list(pending_calls),
            findings=current_findings,
            verdict=verdict,
        ))
        pending_calls = []
        current_findings = None

    for m in rows:
        if m.role == "tool_call":
            if pending_call is not None:
                pending_calls.append((pending_call, None))
            pending_call = m
        elif m.role == "tool_result":
            if pending_call is not None:
                pending_calls.append((pending_call, m))
                pending_call = None
            # else: orphan result — drop silently.
        elif m.role == "research_findings":
            current_findings = m
        elif m.role == "review_verdict":
            commit_iteration(m)

    # End-of-rows: if any iteration material is still pending without
    # a closing verdict, commit it with verdict=None so the panel
    # surfaces what work the model did before the crash.
    if (
        pending_call is not None
        or pending_calls
        or current_findings is not None
    ):
        commit_iteration(verdict=None)

    turn_id = f"hist-{rows[0].id}"
    return AgenticToolBatchBlock(iterations=iterations, turn_id=turn_id)


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
