"""Phase 12e: tests for app/render.py — block grouping + view helpers."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.queries import Message
from app.render import (
    DedupedSource,
    MessageBlock,
    ToolBatchBlock,
    ToolRowView,
    card_id_for,
    dedup_sources,
    format_elapsed_mm_ss,
    group_messages_for_render,
    render_done_card_oobs,
    render_tool_card_initial,
    render_tool_card_row_append,
    render_tool_card_row_freeze,
    summary_text,
)
from app.tools import Source, ToolResult, encode_tool_result


def _msg(
    *,
    id: int,
    role: str,
    content: str,
    created_at: datetime | None = None,
) -> Message:
    """Tiny factory so tests don't repeat the dataclass spelling."""
    return Message(
        id=id,
        conversation_id=1,
        role=role,
        content=content,
        created_at=created_at
        or datetime(2026, 5, 19, 12, 0, id, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# format_elapsed_mm_ss
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ms,expected",
    [
        (0, "0:00"),
        (999, "0:00"),  # sub-second rounds down
        (1000, "0:01"),
        (8000, "0:08"),
        (59000, "0:59"),
        (60000, "1:00"),
        (65500, "1:05"),
        (3725000, "62:05"),  # > 60min still readable
    ],
)
def test_format_elapsed_mm_ss(ms: int, expected: str) -> None:
    """The helper matches stopwatch convention and never zero-pads minutes."""
    assert format_elapsed_mm_ss(ms) == expected


# ---------------------------------------------------------------------------
# summary_text
# ---------------------------------------------------------------------------


def test_summary_text_using_present_tense_with_ellipsis() -> None:
    """While streaming, the verb is `using` and the phrase ends in `…`."""
    assert summary_text(1, done=False) == "using 1 tool…"
    assert summary_text(2, done=False) == "using 2 tools…"
    assert summary_text(5, done=False) == "using 5 tools…"


def test_summary_text_used_past_tense_no_ellipsis() -> None:
    """Once `done`, the verb flips to `used` and the ellipsis drops."""
    assert summary_text(1, done=True) == "used 1 tool"
    assert summary_text(2, done=True) == "used 2 tools"


def test_summary_text_pluralization() -> None:
    """Singular at count=1, plural everywhere else (including 0)."""
    assert "tool…" in summary_text(1, done=False)
    assert "tools…" in summary_text(2, done=False)
    assert "tools…" in summary_text(0, done=False)


# ---------------------------------------------------------------------------
# card_id_for
# ---------------------------------------------------------------------------


def test_card_id_for_uses_stable_prefix() -> None:
    """Same prefix lets the live SSE path and historic replay produce
    matching DOM ids."""
    assert card_id_for("123") == "tool-card-123"
    assert card_id_for("hist-42") == "tool-card-hist-42"


# ---------------------------------------------------------------------------
# group_messages_for_render
# ---------------------------------------------------------------------------


def test_group_messages_empty_input() -> None:
    """Empty list in, empty list out — no edge-case crash."""
    assert group_messages_for_render([]) == []


def test_group_messages_no_tool_rows() -> None:
    """A conversation with only user/assistant rows yields only MessageBlocks."""
    msgs = [
        _msg(id=1, role="user", content="hi"),
        _msg(id=2, role="assistant", content="hello"),
        _msg(id=3, role="user", content="thanks"),
    ]
    blocks = group_messages_for_render(msgs)

    assert len(blocks) == 3
    assert all(b.kind == "message" for b in blocks)
    assert all(isinstance(b, MessageBlock) for b in blocks)
    # Preserves order.
    assert [b.message.id for b in blocks] == [1, 2, 3]


def test_group_messages_folds_paired_tool_call_and_result() -> None:
    """One tool_call + one tool_result before an assistant message → one
    ToolBatchBlock with one (call, result) pair."""
    msgs = [
        _msg(id=1, role="user", content="search arxiv"),
        _msg(
            id=2,
            role="tool_call",
            content=json.dumps(
                {"name": "query_rag", "arguments": {"source": "arxiv", "query": "x"}}
            ),
        ),
        _msg(id=3, role="tool_result", content="...result..."),
        _msg(id=4, role="assistant", content="here's what I found"),
    ]
    blocks = group_messages_for_render(msgs)

    # user, batch, assistant.
    assert len(blocks) == 3
    assert blocks[0].kind == "message" and blocks[0].message.id == 1
    assert blocks[1].kind == "tool_batch"
    assert blocks[2].kind == "message" and blocks[2].message.id == 4

    batch = blocks[1]
    assert isinstance(batch, ToolBatchBlock)
    assert len(batch.calls) == 1
    call, result = batch.calls[0]
    assert call.id == 2
    assert result is not None and result.id == 3
    # turn_id derived from first call → stable across reloads.
    assert batch.turn_id == "hist-2"


def test_group_messages_folds_multiple_pairs_into_one_batch() -> None:
    """Two consecutive (call, result) pairs go into the same batch — the
    aggregated card lists both rows."""
    msgs = [
        _msg(
            id=1,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        _msg(id=2, role="tool_result", content="2026-05-19T12:00:00Z"),
        _msg(
            id=3,
            role="tool_call",
            content=json.dumps(
                {"name": "query_rag", "arguments": {"source": "arxiv", "query": "x"}}
            ),
        ),
        _msg(id=4, role="tool_result", content="...result..."),
        _msg(id=5, role="assistant", content="combined answer"),
    ]
    blocks = group_messages_for_render(msgs)

    assert len(blocks) == 2  # batch + assistant
    batch = blocks[0]
    assert isinstance(batch, ToolBatchBlock)
    assert len(batch.calls) == 2


def test_group_messages_unpaired_trailing_call_lands_as_none() -> None:
    """When the iteration cap bails, the last tool_call has no
    matching tool_result. The batch still emits with `result=None` in
    that slot so historic rendering shows `?`."""
    msgs = [
        _msg(
            id=1,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        _msg(id=2, role="tool_result", content="2026-05-19T12:00:00Z"),
        _msg(
            id=3,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        # No tool_result for call #3 — loop bailed.
        _msg(id=4, role="assistant", content="(Tool-call limit reached…)"),
    ]
    blocks = group_messages_for_render(msgs)

    batch = blocks[0]
    assert isinstance(batch, ToolBatchBlock)
    assert len(batch.calls) == 2
    assert batch.calls[1][1] is None


def test_group_messages_end_of_list_flush() -> None:
    """A tool batch that ends the message list (no following
    user/assistant row) still flushes — otherwise crashed-mid-turn
    conversations would render with an invisible card."""
    msgs = [
        _msg(
            id=1,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        _msg(id=2, role="tool_result", content="2026-05-19T12:00:00Z"),
    ]
    blocks = group_messages_for_render(msgs)

    assert len(blocks) == 1
    assert blocks[0].kind == "tool_batch"


def test_group_messages_two_consecutive_unpaired_calls_both_recorded() -> None:
    """Defensive: if two tool_call rows appear back-to-back with no
    intervening tool_result (shouldn't happen with the current loop,
    but the helper is permissive), both land in the batch — the first
    as unpaired, the second paired with the following result."""
    msgs = [
        _msg(
            id=1,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        _msg(
            id=2,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        _msg(id=3, role="tool_result", content="2026-05-19T12:00:00Z"),
        _msg(id=4, role="assistant", content="done"),
    ]
    blocks = group_messages_for_render(msgs)

    batch = blocks[0]
    assert isinstance(batch, ToolBatchBlock)
    assert len(batch.calls) == 2
    assert batch.calls[0][1] is None  # first call orphaned
    assert batch.calls[1][1] is not None
    assert batch.calls[1][1].id == 3


def test_group_messages_orphan_result_skipped() -> None:
    """A tool_result with no preceding tool_call is dropped silently —
    the server doesn't write these, but a corrupt DB row shouldn't
    break the whole panel."""
    msgs = [
        _msg(id=1, role="tool_result", content="huh?"),
        _msg(id=2, role="assistant", content="hi"),
    ]
    blocks = group_messages_for_render(msgs)

    # Only the assistant row survives.
    assert len(blocks) == 1
    assert blocks[0].kind == "message"


# ---------------------------------------------------------------------------
# ToolBatchBlock.rows / view materialization
# ---------------------------------------------------------------------------


def test_batch_rows_format_query_rag_invocation() -> None:
    """The `query_rag` branch of format_tool_invocation produces the
    `searching <source>: "<query>"` label users see."""
    call = _msg(
        id=10,
        role="tool_call",
        content=json.dumps(
            {
                "name": "query_rag",
                "arguments": {
                    "source": "arxiv",
                    "query": "enhanced gas transfer",
                },
            }
        ),
    )
    result = _msg(
        id=11,
        role="tool_result",
        content="...",
        created_at=call.created_at + timedelta(seconds=8),
    )
    batch = ToolBatchBlock(calls=[(call, result)], turn_id="hist-10")

    rows = batch.rows
    assert len(rows) == 1
    assert isinstance(rows[0], ToolRowView)
    assert rows[0].label == 'searching arxiv: "enhanced gas transfer"'
    # 8s diff → "0:08".
    assert rows[0].elapsed_display == "0:08"
    assert rows[0].elapsed_final_ms == 8000
    assert rows[0].elapsed_start_ms is None


def test_batch_rows_generic_fallback_for_non_search_tool() -> None:
    """`current_time` and any future non-query_rag tool use the generic
    `calling tool(args)` fallback."""
    call = _msg(
        id=20,
        role="tool_call",
        content=json.dumps(
            {"name": "current_time", "arguments": {"timezone": "UTC"}}
        ),
    )
    result = _msg(
        id=21,
        role="tool_result",
        content="2026-05-19T12:00:00Z",
        created_at=call.created_at + timedelta(milliseconds=50),
    )
    batch = ToolBatchBlock(calls=[(call, result)], turn_id="hist-20")

    label = batch.rows[0].label
    assert label.startswith("calling current_time(")
    assert "timezone=" in label


def test_batch_rows_unpaired_call_renders_with_question_mark() -> None:
    """Historic unpaired call (loop bailed) → row with `?` elapsed,
    neither `data-elapsed-start` nor `data-elapsed-final` set so the
    JS tick driver ignores it."""
    call = _msg(
        id=30,
        role="tool_call",
        content=json.dumps({"name": "current_time", "arguments": {}}),
    )
    batch = ToolBatchBlock(calls=[(call, None)], turn_id="hist-30")

    row = batch.rows[0]
    assert row.elapsed_display == "?"
    assert row.elapsed_start_ms is None
    assert row.elapsed_final_ms is None


def test_batch_rows_corrupt_call_json_does_not_crash() -> None:
    """Defensive: a tool_call row with malformed JSON content still
    renders a row (label `calling ?()`) rather than crashing the
    whole panel render."""
    call = _msg(id=40, role="tool_call", content="{not valid json")
    result = _msg(
        id=41,
        role="tool_result",
        content="ok",
        created_at=call.created_at + timedelta(seconds=1),
    )
    batch = ToolBatchBlock(calls=[(call, result)], turn_id="hist-40")

    # Doesn't raise.
    row = batch.rows[0]
    # Fallback name is "?" so the label reads "calling ?()".
    assert "calling ?(" in row.label


def test_batch_summary_and_ids_are_consistent() -> None:
    """The card/list/summary id helpers all derive from the same
    `turn_id`, so live OOB swaps and historic renders never disagree
    on element ids."""
    batch = ToolBatchBlock(calls=[], turn_id="hist-99")
    assert batch.card_id == "tool-card-hist-99"
    assert batch.list_id == "tool-card-hist-99-list"
    assert batch.summary_id == "tool-card-hist-99-summary"


def test_batch_summary_text_is_past_tense() -> None:
    """Historic rendering is always `done` — the summary phrase reads
    `used N tool(s)`."""
    call = _msg(id=1, role="tool_call", content=json.dumps({"name": "x", "arguments": {}}))
    result = _msg(
        id=2,
        role="tool_result",
        content="ok",
        created_at=call.created_at + timedelta(seconds=1),
    )
    batch = ToolBatchBlock(calls=[(call, result)], turn_id="hist-1")
    assert batch.summary == "used 1 tool"

    # Two calls → plural.
    call2 = _msg(
        id=3,
        role="tool_call",
        content=json.dumps({"name": "x", "arguments": {}}),
    )
    result2 = _msg(
        id=4,
        role="tool_result",
        content="ok",
        created_at=call2.created_at + timedelta(seconds=1),
    )
    batch2 = ToolBatchBlock(
        calls=[(call, result), (call2, result2)], turn_id="hist-1"
    )
    assert batch2.summary == "used 2 tools"


# ---------------------------------------------------------------------------
# dedup_sources (phase 12h)
# ---------------------------------------------------------------------------


def test_dedup_sources_empty_input() -> None:
    """No sources → empty deduped list. No edge-case crash on empty input."""
    assert dedup_sources([]) == []


def test_dedup_sources_single_chunk_with_section() -> None:
    """count == 1 with section → `(§Section)` meta suffix."""
    out = dedup_sources([Source(title="Paper", section="Intro")])
    assert out == [DedupedSource(title="Paper", meta="(§Intro)")]


def test_dedup_sources_single_chunk_no_section() -> None:
    """count == 1 with no section → empty meta (just the title)."""
    out = dedup_sources([Source(title="Paper", section=None)])
    assert out == [DedupedSource(title="Paper", meta="")]


def test_dedup_sources_multi_chunk_same_title_drops_section() -> None:
    """count > 1 → `(N chunks)` even when chunks share a section.

    The chosen dedup-by-title trades section granularity for a
    cleaner list; multi-chunk meta always reads "(N chunks)".
    """
    out = dedup_sources([
        Source(title="Paper", section="Intro"),
        Source(title="Paper", section="Results"),
    ])
    assert out == [DedupedSource(title="Paper", meta="(2 chunks)")]


def test_dedup_sources_multi_chunk_same_title_same_section_still_chunks() -> None:
    """Even when every chunk has the same section, multi-chunk reads
    `(N chunks)` not `(§Section)`. The format is uniform once N > 1."""
    out = dedup_sources([
        Source(title="Paper", section="Intro"),
        Source(title="Paper", section="Intro"),
        Source(title="Paper", section="Intro"),
    ])
    assert out == [DedupedSource(title="Paper", meta="(3 chunks)")]


def test_dedup_sources_preserves_first_seen_order() -> None:
    """Order matches the first-seen title in the input; later chunks
    of an earlier-seen title stay grouped with their first."""
    out = dedup_sources([
        Source(title="A", section="1"),
        Source(title="B", section=None),
        Source(title="A", section="2"),
    ])
    assert out == [
        DedupedSource(title="A", meta="(2 chunks)"),
        DedupedSource(title="B", meta=""),
    ]


def test_dedup_sources_three_unique_titles_no_collapse() -> None:
    """Unique titles each get their own line — no collapsing."""
    out = dedup_sources([
        Source(title="A", section="1"),
        Source(title="B", section=None),
        Source(title="C", section="X"),
    ])
    assert out == [
        DedupedSource(title="A", meta="(§1)"),
        DedupedSource(title="B", meta=""),
        DedupedSource(title="C", meta="(§X)"),
    ]


# ---------------------------------------------------------------------------
# ToolRowView.sources + deduped_sources property (phase 12h)
# ---------------------------------------------------------------------------


def test_tool_row_view_default_sources_is_empty_list() -> None:
    """Sources defaults to [] so pre-12h call sites and pending rows
    (no sources yet) keep constructing the view without spelling it out."""
    row = ToolRowView(
        id="r",
        label="x",
        elapsed_start_ms=None,
        elapsed_final_ms=1000,
        elapsed_display="0:01",
    )
    assert row.sources == []
    assert row.deduped_sources == []


def test_tool_row_view_deduped_sources_property_routes_through_dedup() -> None:
    """The property is just sugar over dedup_sources(self.sources)."""
    row = ToolRowView(
        id="r",
        label="x",
        elapsed_start_ms=None,
        elapsed_final_ms=1000,
        elapsed_display="0:01",
        sources=[
            Source(title="A", section="1"),
            Source(title="A", section="2"),
            Source(title="B", section=None),
        ],
    )
    assert row.deduped_sources == [
        DedupedSource(title="A", meta="(2 chunks)"),
        DedupedSource(title="B", meta=""),
    ]


# ---------------------------------------------------------------------------
# Historic render path picks up sources from the JSON envelope (phase 12h)
# ---------------------------------------------------------------------------


def test_historic_row_view_extracts_sources_from_json_envelope() -> None:
    """A `tool_result` row whose content is the JSON envelope produces
    a ToolRowView with the decoded sources attached."""
    call = _msg(
        id=10,
        role="tool_call",
        content=json.dumps(
            {
                "name": "query_rag",
                "arguments": {"source": "arxiv", "query": "x"},
            }
        ),
    )
    envelope = encode_tool_result(
        ToolResult(
            text="[1] Foo (§Intro)\n    body",
            sources=[
                Source(title="Foo", section="Intro"),
                Source(title="Bar", section=None),
            ],
        )
    )
    result = _msg(
        id=11,
        role="tool_result",
        content=envelope,
        created_at=call.created_at + timedelta(seconds=1),
    )
    batch = ToolBatchBlock(calls=[(call, result)], turn_id="hist-10")

    row = batch.rows[0]
    assert row.sources == [
        Source(title="Foo", section="Intro"),
        Source(title="Bar", section=None),
    ]


def test_historic_row_view_plain_text_content_has_empty_sources() -> None:
    """Pre-12h rows (plain text content) decode to ToolResult with
    sources=[], so historic rendering of old conversations shows a
    plain row — no chevron, no expand affordance."""
    call = _msg(
        id=20,
        role="tool_call",
        content=json.dumps(
            {
                "name": "query_rag",
                "arguments": {"source": "arxiv", "query": "x"},
            }
        ),
    )
    result = _msg(
        id=21,
        role="tool_result",
        content="[1] Foo\n    just plain pre-12h text",
        created_at=call.created_at + timedelta(seconds=1),
    )
    batch = ToolBatchBlock(calls=[(call, result)], turn_id="hist-20")

    row = batch.rows[0]
    assert row.sources == []
    assert row.deduped_sources == []


def test_historic_row_view_unpaired_call_has_empty_sources() -> None:
    """A loop-bailed call has no result to decode → sources stays [].
    Doesn't crash on the missing result row."""
    call = _msg(
        id=30,
        role="tool_call",
        content=json.dumps({"name": "current_time", "arguments": {}}),
    )
    batch = ToolBatchBlock(calls=[(call, None)], turn_id="hist-30")
    assert batch.rows[0].sources == []


# ---------------------------------------------------------------------------
# Tool-card OOB renders (moved from generation.py)
# ---------------------------------------------------------------------------


def _live_row(call_index: int = 0, label: str = 'searching arxiv: "x"') -> ToolRowView:
    """Build a live (ticking) row view for OOB-render tests."""
    return ToolRowView(
        id=f"tool-card-T-row-{call_index}",
        label=label,
        elapsed_start_ms=1_000,
        elapsed_final_ms=None,
        elapsed_display="0:00",
    )


def test_render_tool_card_initial_emits_full_card_with_beforebegin_swap() -> None:
    """First call in a turn: full <details> card OOB-inserted as the
    streaming placeholder's preceding sibling. The summary reads
    present-tense / singular because only one row exists yet."""
    html_out = render_tool_card_initial(
        card_id="tool-card-T",
        list_id="tool-card-T-list",
        summary_id="tool-card-T-summary",
        live_row=_live_row(),
        conversation_id=42,
    )
    # Card shell + the row are both present.
    assert 'id="tool-card-T"' in html_out
    assert 'id="tool-card-T-list"' in html_out
    assert 'id="tool-card-T-summary"' in html_out
    assert 'id="tool-card-T-row-0"' in html_out
    # OOB swap is the beforebegin selector with the conversation_id.
    assert 'hx-swap-oob="beforebegin:#assistant-stream-42"' in html_out
    # Summary text is present tense, singular.
    assert "using 1 tool" in html_out


def test_render_tool_card_row_append_emits_row_plus_summary_bump() -> None:
    """Subsequent call: row gets appended into the card's list and the
    summary span swaps to reflect the new count (pluralized at N >= 2)."""
    html_out = render_tool_card_row_append(
        live_row=_live_row(call_index=1),
        list_id="tool-card-T-list",
        summary_id="tool-card-T-summary",
        call_index=1,
    )
    # Row append into the existing list.
    assert 'hx-swap-oob="beforeend:#tool-card-T-list"' in html_out
    assert 'id="tool-card-T-row-1"' in html_out
    # Summary swap to "using 2 tools…" (pluralized).
    assert 'id="tool-card-T-summary"' in html_out
    assert "using 2 tools" in html_out
    # No standalone card shell — only the row + span.
    assert "tool-card-T-list" in html_out  # appears as the swap target
    # The shell <details id="tool-card-T"> must NOT be re-emitted —
    # otherwise HTMX would replace the in-DOM card and clobber prior
    # rows. The card id appears only as part of the row/summary ids.
    assert '<details id="tool-card-T"' not in html_out


def test_render_tool_card_row_freeze_swaps_row_in_place() -> None:
    """Result arrival: outerHTML swap on the row by id, replacing the
    live ticking variant with the frozen one (has data-elapsed-final,
    no data-elapsed-start — JS tick driver skips frozen rows)."""
    frozen = ToolRowView(
        id="tool-card-T-row-0",
        label='searching arxiv: "x"',
        elapsed_start_ms=None,
        elapsed_final_ms=8000,
        elapsed_display="0:08",
    )
    html_out = render_tool_card_row_freeze(frozen)
    assert 'hx-swap-oob="outerHTML"' in html_out
    assert 'id="tool-card-T-row-0"' in html_out
    assert 'data-elapsed-final="8000"' in html_out
    assert "data-elapsed-start" not in html_out


def test_render_done_card_oobs_zero_calls_returns_empty() -> None:
    """A turn with no tool calls produces no card-related OOB fragments.
    Keeps the done event's payload compact for tool-free assistant turns."""
    assert render_done_card_oobs(0, {}, "tool-card-T-summary") == ""


def test_render_done_card_oobs_empty_in_flight_emits_summary_only() -> None:
    """Happy path: every call was paired in the loop, so the only
    OOB fragment is the past-tense summary swap."""
    html_out = render_done_card_oobs(
        call_count=2, in_flight={}, summary_id="tool-card-T-summary"
    )
    assert 'id="tool-card-T-summary"' in html_out
    assert 'hx-swap-oob="outerHTML"' in html_out
    assert "used 2 tools" in html_out
    # No row OOBs in the empty-in_flight branch.
    assert "tool-row" not in html_out


def test_render_done_card_oobs_freezes_in_flight_rows() -> None:
    """Defensive: any row still in_flight at done time gets frozen so
    the JS tick driver stops incrementing it after SSE close. Today's
    _run_generation always drains in_flight, so this branch is exercised
    only here — keeps the safety-net coverage live."""
    in_flight = {
        "tool-card-T-row-0": {
            "start_ms": 1_000_000_000,
            "name": "current_time",
            "arguments": {"timezone": "UTC"},
            "label": "calling current_time(timezone='UTC')",
        },
    }
    html_out = render_done_card_oobs(
        call_count=1, in_flight=in_flight, summary_id="tool-card-T-summary"
    )
    assert "used 1 tool" in html_out
    assert 'id="tool-card-T-row-0"' in html_out
    assert "data-elapsed-final=" in html_out
    assert "calling current_time" in html_out
