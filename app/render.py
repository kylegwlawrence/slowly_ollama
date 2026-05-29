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
from typing import ClassVar

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
class SummaryBlock:
    """A synthetic ``summary`` row produced by the manual-compact endpoint.

    Phase 18. Distinct from :class:`MessageBlock` so the chat panel can
    style it differently (a "compacted history" badge + a disclosure
    revealing the archived originals) without conditional branches in
    the message template. The archived-row count is bound from the
    render context — kept off the dataclass so
    :func:`group_messages_for_render` stays a pure messages → blocks
    transformer.

    Attributes:
        message: The persisted ``role = 'summary'`` row.
        kind: Template discriminator. Class-level constant.
    """

    message: Message
    kind: ClassVar[str] = "summary"


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


Block = MessageBlock | ToolBatchBlock | SummaryBlock


_TOOL_ROLES = frozenset({"tool_call", "tool_result"})
# Phase 18: `summary` is renderable too — it gets its own SummaryBlock,
# but for the loop's flush/skip logic it behaves the same way as a
# user/assistant row (flushes any pending tool batch ahead of itself).
_RENDERABLE_MESSAGE_ROLES = frozenset({"user", "assistant", "summary"})


def group_messages_for_render(messages: list[Message]) -> list[Block]:
    """Walk messages, folding tool-call/result runs into ToolBatchBlocks.

    Each contiguous run of `tool_call` / `tool_result` rows is flushed as a
    :class:`ToolBatchBlock` — the aggregated card above the assistant turn
    that used the tools. A `user` / `assistant` row flushes the pending run
    ahead of itself and emits a :class:`MessageBlock`. Any other role is
    silently skipped: this defends against legacy rows left by removed
    features so they neither render as stray bubbles nor get folded into a
    tool card.

    Rules:
        - Tool rows accumulate into a pending run; a renderable message row
          triggers a flush + emits a MessageBlock.
        - End-of-list flush handles crashed-mid-turn conversations (no
          closing assistant row).
        - A `tool_result` without a preceding `tool_call` is treated as an
          orphan and skipped — the streaming loop only writes results after
          calls, but historic DB corruption shouldn't break the panel render.

    Args:
        messages: Rows from `queries.list_messages`, oldest-first.

    Returns:
        A list of `MessageBlock` / `ToolBatchBlock` instances in display
        order. Empty input yields empty output.
    """
    blocks: list[Block] = []
    pending_rows: list[Message] = []

    def flush_batch() -> None:
        nonlocal pending_rows
        if not pending_rows:
            return
        block = _build_classic_tool_batch(pending_rows)
        # Returns None when the run held only orphan tool_result rows — see
        # its docstring. Drop instead of appending an empty card.
        if block is not None:
            blocks.append(block)
        pending_rows = []

    for m in messages:
        if m.role in _TOOL_ROLES:
            pending_rows.append(m)
        elif m.role == "summary":
            # Phase 18: synthetic compaction row. Flush any tool batch
            # ahead of it (defensive — in practice no tool rows survive
            # the compact, which archives them) then emit its own block.
            flush_batch()
            blocks.append(SummaryBlock(message=m))
        elif m.role in _RENDERABLE_MESSAGE_ROLES:
            flush_batch()
            blocks.append(MessageBlock(message=m))
        # else: unknown/legacy role — skip without flushing.

    flush_batch()  # end-of-list

    return blocks


def count_archived_blocks(messages: list[Message]) -> int:
    """Count the display blocks the archived-history disclosure will render.

    The summary bubble's ``N archived message(s)`` label must agree with
    what the disclosure body shows when expanded. That body renders the
    archived rows — excluding the synthetic ``summary`` rows it hides —
    through :func:`group_messages_for_render`, which folds each contiguous
    ``tool_call`` / ``tool_result`` run into a single card.

    Counting raw archived rows instead (the original Phase 18 behaviour)
    over-reports: a tool turn is two rows but one card, so a tool-heavy
    chat's label runs to roughly double the items actually displayed, and
    archived ``summary`` rows get counted despite never being shown. This
    helper applies the same filter + grouping the disclosure uses, so the
    label matches the body exactly.

    Args:
        messages: All rows for the conversation, oldest first — the same
            list the chat panel already loaded for rendering.

    Returns:
        The number of blocks the disclosure will display for this chat.
    """
    archived = [
        m for m in messages
        if m.archived_at is not None and m.role != "summary"
    ]
    return len(group_messages_for_render(archived))


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


# ---------------------------------------------------------------------------
# Tool-card OOB rendering — emitted by the producer layer at each phase of
# the tool-calling turn. Kept here (rather than in app/generation.py) so the
# template-render dance lives next to the view dataclasses. The producer
# stays in charge of WHEN to emit; render owns WHAT the OOB fragment looks
# like.
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
