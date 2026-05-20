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

import json
from dataclasses import dataclass, field
from typing import ClassVar, Union

from app.queries import Message
from app.tools import Source, decode_tool_result, format_tool_invocation


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
    try:
        payload = json.loads(call.content)
        name = payload.get("name", "?")
        arguments = payload.get("arguments", {}) or {}
    except (json.JSONDecodeError, TypeError):
        # Defensive — a corrupt row shouldn't crash a whole conversation
        # render. Same posture as _build_history_payload in routes.py.
        name = "?"
        arguments = {}
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
        else:
            flush_batch()
            blocks.append(MessageBlock(message=m))

    flush_batch()  # end-of-list

    return blocks
