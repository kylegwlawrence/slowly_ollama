"""Phase 12e: tests for app/render.py — block grouping + view helpers."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.queries import Message
from app.agents import AGENTIC_ITERATION_CAP
from app.render import (
    AgenticIteration,
    AgenticToolBatchBlock,
    DedupedSource,
    MessageBlock,
    ToolBatchBlock,
    ToolRowView,
    agentic_summary_text,
    card_id_for,
    dedup_sources,
    format_elapsed_mm_ss,
    group_messages_for_render,
    render_agentic_card_shell,
    render_agentic_done_summary,
    render_agentic_tool_row_append,
    render_done_card_oobs,
    render_findings_row,
    render_iteration_start,
    render_max_iterations_badge,
    render_tool_card_initial,
    render_tool_card_row_append,
    render_tool_card_row_freeze,
    render_verdict_row,
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
# Phase 13f: AgenticToolBatchBlock grouping
# ---------------------------------------------------------------------------


def _verdict_json(status: str, message: str = "") -> str:
    """Helper: build a review_verdict row's persisted JSON payload."""
    return json.dumps({"verdict": status, "message": message})


def test_group_messages_single_iteration_agentic_block() -> None:
    """One iteration that passed: tool_call + tool_result +
    research_findings + review_verdict(passed) collapses into a single
    AgenticToolBatchBlock with one AgenticIteration."""
    msgs = [
        _msg(id=1, role="user", content="research X"),
        _msg(
            id=2,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        _msg(id=3, role="tool_result", content="..."),
        _msg(id=4, role="research_findings", content="some notes"),
        _msg(id=5, role="review_verdict", content=_verdict_json("passed", "lgtm")),
        _msg(id=6, role="assistant", content="the answer"),
    ]
    blocks = group_messages_for_render(msgs)

    # user, agentic batch, assistant.
    assert len(blocks) == 3
    assert blocks[0].kind == "message" and blocks[0].message.id == 1
    assert blocks[1].kind == "agentic_tool_batch"
    assert blocks[2].kind == "message" and blocks[2].message.id == 6

    batch = blocks[1]
    assert isinstance(batch, AgenticToolBatchBlock)
    assert len(batch.iterations) == 1
    it = batch.iterations[0]
    assert it.index == 1
    assert len(it.tool_calls) == 1
    call, result = it.tool_calls[0]
    assert call.id == 2 and result is not None and result.id == 3
    assert it.findings is not None and it.findings.id == 4
    assert it.verdict is not None and it.verdict.id == 5
    assert it.verdict_status == "passed"
    assert it.verdict_message == "lgtm"
    # turn_id derives from the FIRST row in the run (the tool_call,
    # id=2), matching the classic block's convention.
    assert batch.turn_id == "hist-2"
    assert batch.card_id == "tool-card-hist-2"


def test_group_messages_multi_iteration_agentic_block() -> None:
    """Two failed iterations followed by a passing one collapse into
    a single AgenticToolBatchBlock with three iterations carrying
    the right verdict statuses."""
    msgs = [
        _msg(id=1, role="user", content="hard question"),
        # Iteration 1: tool call → findings → failed
        _msg(
            id=2,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        _msg(id=3, role="tool_result", content="r1"),
        _msg(id=4, role="research_findings", content="round 1"),
        _msg(id=5, role="review_verdict", content=_verdict_json("failed", "needs more")),
        # Iteration 2: same shape, also failed
        _msg(
            id=6,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        _msg(id=7, role="tool_result", content="r2"),
        _msg(id=8, role="research_findings", content="round 2"),
        _msg(id=9, role="review_verdict", content=_verdict_json("failed", "still")),
        # Iteration 3: passes
        _msg(id=10, role="research_findings", content="round 3"),
        _msg(id=11, role="review_verdict", content=_verdict_json("passed", "ok")),
        _msg(id=12, role="assistant", content="the answer"),
    ]
    blocks = group_messages_for_render(msgs)

    assert len(blocks) == 3
    batch = blocks[1]
    assert isinstance(batch, AgenticToolBatchBlock)
    assert [it.index for it in batch.iterations] == [1, 2, 3]
    assert [it.verdict_status for it in batch.iterations] == [
        "failed",
        "failed",
        "passed",
    ]
    # Iteration 3 has findings but no tool calls — that branch must
    # not produce an empty iteration.
    assert batch.iterations[2].tool_calls == []
    assert batch.iterations[2].findings is not None
    # Last verdict was passed → max_iterations_reached is False even
    # though we ran the full cap.
    assert batch.max_iterations_reached is False


def test_group_messages_max_iterations_reached_block() -> None:
    """All three iterations failed → max_iterations_reached flags True.

    The live producer renders a "(max reached)" badge in this state;
    historic replay surfaces the same signal via this flag.
    """
    assert AGENTIC_ITERATION_CAP == 3  # guard: tweak this test if cap moves
    msgs: list[Message] = []
    next_id = 1
    for round_num in range(3):
        msgs.append(_msg(id=next_id, role="research_findings", content=f"r{round_num}"))
        msgs.append(
            _msg(
                id=next_id + 1,
                role="review_verdict",
                content=_verdict_json("failed", f"round {round_num} feedback"),
            )
        )
        next_id += 2
    msgs.append(_msg(id=next_id, role="assistant", content="forced answer"))

    blocks = group_messages_for_render(msgs)

    # agentic batch + assistant.
    assert len(blocks) == 2
    batch = blocks[0]
    assert isinstance(batch, AgenticToolBatchBlock)
    assert len(batch.iterations) == 3
    assert all(it.verdict_status == "failed" for it in batch.iterations)
    assert batch.max_iterations_reached is True
    # Iterations have no tool calls (this test exercises the
    # findings-only path) — they must still show up.
    assert all(it.tool_calls == [] for it in batch.iterations)


def test_group_messages_classic_batch_unchanged_by_phase13f() -> None:
    """Regression guard: a turn with ONLY tool_call/tool_result rows
    still emits a ToolBatchBlock, not an AgenticToolBatchBlock. Phase
    13f only changes grouping when findings/verdict rows are present.
    """
    msgs = [
        _msg(
            id=1,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        _msg(id=2, role="tool_result", content="ok"),
        _msg(id=3, role="assistant", content="answered"),
    ]
    blocks = group_messages_for_render(msgs)

    assert len(blocks) == 2
    assert isinstance(blocks[0], ToolBatchBlock)
    assert blocks[0].kind == "tool_batch"


def test_group_messages_trailing_findings_without_verdict_still_commits() -> None:
    """Defensive: if a process crashed between persisting findings
    and persisting the verdict, the iteration still surfaces (with
    verdict=None) instead of getting silently dropped."""
    msgs = [
        _msg(id=1, role="research_findings", content="partial"),
        # No verdict row — process died here.
    ]
    blocks = group_messages_for_render(msgs)

    assert len(blocks) == 1
    batch = blocks[0]
    assert isinstance(batch, AgenticToolBatchBlock)
    assert len(batch.iterations) == 1
    it = batch.iterations[0]
    assert it.findings is not None and it.findings.id == 1
    assert it.verdict is None
    assert it.verdict_status == "unknown"
    assert it.verdict_message == ""


def test_agentic_iteration_pairs_calls_with_results_within_iteration() -> None:
    """Two tool calls inside one iteration → both pair with their
    respective results inside the same AgenticIteration.tool_calls."""
    msgs = [
        _msg(
            id=1,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        _msg(id=2, role="tool_result", content="r1"),
        _msg(
            id=3,
            role="tool_call",
            content=json.dumps(
                {"name": "query_rag", "arguments": {"source": "arxiv", "query": "x"}}
            ),
        ),
        _msg(id=4, role="tool_result", content="r2"),
        _msg(id=5, role="research_findings", content="combined"),
        _msg(id=6, role="review_verdict", content=_verdict_json("passed")),
    ]
    blocks = group_messages_for_render(msgs)

    batch = blocks[0]
    assert isinstance(batch, AgenticToolBatchBlock)
    assert len(batch.iterations) == 1
    it = batch.iterations[0]
    assert len(it.tool_calls) == 2
    # First call paired with first result; second with second.
    assert it.tool_calls[0][0].id == 1 and it.tool_calls[0][1].id == 2
    assert it.tool_calls[1][0].id == 3 and it.tool_calls[1][1].id == 4


def test_agentic_iteration_row_views_use_iteration_scoped_ids() -> None:
    """Row ids embed both the iteration index and the within-iteration
    call index, matching the live SSE path's `{card_id}-iter-N-row-M`
    format. Same-id parity matters for mid-turn reloads."""
    msgs = [
        # Iteration 1: two tool calls
        _msg(
            id=1,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        _msg(id=2, role="tool_result", content="ok"),
        _msg(
            id=3,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        _msg(id=4, role="tool_result", content="ok"),
        _msg(id=5, role="research_findings", content="notes"),
        _msg(id=6, role="review_verdict", content=_verdict_json("failed")),
        # Iteration 2: one tool call
        _msg(
            id=7,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        _msg(id=8, role="tool_result", content="ok"),
        _msg(id=9, role="research_findings", content="notes 2"),
        _msg(id=10, role="review_verdict", content=_verdict_json("passed")),
    ]
    blocks = group_messages_for_render(msgs)
    batch = blocks[0]
    assert isinstance(batch, AgenticToolBatchBlock)

    iter1_rows = batch.iterations[0].row_views(batch.card_id)
    iter2_rows = batch.iterations[1].row_views(batch.card_id)
    assert [r.id for r in iter1_rows] == [
        f"{batch.card_id}-iter-1-row-0",
        f"{batch.card_id}-iter-1-row-1",
    ]
    assert [r.id for r in iter2_rows] == [
        f"{batch.card_id}-iter-2-row-0",
    ]


def test_agentic_block_summary_is_past_tense_iterations_phrase() -> None:
    """The summary span text reads `ran N iterations` (or singular at
    N==1), regardless of whether the cap was hit. The "(max reached)"
    badge lives in the sibling max-marker span, not the summary."""
    batch = AgenticToolBatchBlock(
        iterations=[
            AgenticIteration(index=1, tool_calls=[], findings=None, verdict=None)
        ],
        turn_id="hist-1",
    )
    assert batch.summary == "ran 1 iteration"

    batch3 = AgenticToolBatchBlock(
        iterations=[
            AgenticIteration(index=i, tool_calls=[], findings=None, verdict=None)
            for i in (1, 2, 3)
        ],
        turn_id="hist-1",
    )
    assert batch3.summary == "ran 3 iterations"


def test_agentic_iteration_verdict_status_handles_malformed_json() -> None:
    """A `review_verdict` row with corrupt JSON should fall back to
    `unknown` rather than crashing the whole panel render."""
    bad_verdict = _msg(id=9, role="review_verdict", content="{not json")
    it = AgenticIteration(
        index=1, tool_calls=[], findings=None, verdict=bad_verdict
    )
    assert it.verdict_status == "unknown"
    assert it.verdict_message == ""


def test_agentic_iteration_verdict_unrecognized_status_falls_back() -> None:
    """An unexpected `verdict` value (e.g. `"maybe"`) maps to `unknown`
    so the CSS selector for verdict colouring doesn't blow up on
    drift between the model and our schema."""
    weird = _msg(
        id=9,
        role="review_verdict",
        content=_verdict_json("maybe", "huh"),
    )
    it = AgenticIteration(
        index=1, tool_calls=[], findings=None, verdict=weird
    )
    assert it.verdict_status == "unknown"
    # The message field is still surfaced — only the status falls back.
    assert it.verdict_message == "huh"


def test_agentic_iteration_verdict_message_from_non_dict_payload() -> None:
    """A `review_verdict` row whose JSON parses to a non-dict (e.g.,
    a bare list) falls back to an empty message — same fail-soft
    behaviour as malformed JSON."""
    bare_list = _msg(id=9, role="review_verdict", content="[1, 2, 3]")
    it = AgenticIteration(
        index=1, tool_calls=[], findings=None, verdict=bare_list
    )
    assert it.verdict_status == "unknown"
    assert it.verdict_message == ""


def test_agentic_block_max_iterations_false_when_below_cap() -> None:
    """A passing iteration short of the cap never trips the
    max-iterations flag — the badge must NOT show for those turns."""
    batch = AgenticToolBatchBlock(
        iterations=[
            AgenticIteration(
                index=1,
                tool_calls=[],
                findings=None,
                verdict=_msg(
                    id=1,
                    role="review_verdict",
                    content=_verdict_json("passed"),
                ),
            ),
        ],
        turn_id="hist-1",
    )
    assert batch.max_iterations_reached is False


def test_agentic_block_list_and_summary_ids_match_card_id() -> None:
    """Live SSE and historic templates target rows by `{card_id}-list`
    and `{card_id}-summary`. The block accessors stay consistent with
    that convention so a mid-stream reload doesn't break OOB swaps."""
    batch = AgenticToolBatchBlock(iterations=[], turn_id="hist-7")
    assert batch.card_id == "tool-card-hist-7"
    assert batch.list_id == "tool-card-hist-7-list"
    assert batch.summary_id == "tool-card-hist-7-summary"


def test_agentic_block_iteration_with_unpaired_tool_call() -> None:
    """An iteration that ends with a tool_call but no tool_result
    (process died mid-call) still commits — the dangling call lands
    in `tool_calls` with `result=None` so historic rendering shows
    `?` for its elapsed time."""
    msgs = [
        _msg(
            id=1,
            role="tool_call",
            content=json.dumps({"name": "current_time", "arguments": {}}),
        ),
        # No tool_result — research moved straight to findings.
        _msg(id=2, role="research_findings", content="partial"),
        _msg(id=3, role="review_verdict", content=_verdict_json("passed")),
    ]
    blocks = group_messages_for_render(msgs)

    batch = blocks[0]
    assert isinstance(batch, AgenticToolBatchBlock)
    assert len(batch.iterations) == 1
    it = batch.iterations[0]
    assert len(it.tool_calls) == 1
    assert it.tool_calls[0][0].id == 1
    assert it.tool_calls[0][1] is None


def test_agentic_block_back_to_back_tool_calls_within_iteration() -> None:
    """Two consecutive `tool_call` rows with no intervening result
    (defensive — shouldn't happen with today's loop) both land in
    the iteration: the first as unpaired, the second paired with
    the following result."""
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
        _msg(id=3, role="tool_result", content="ok"),
        _msg(id=4, role="research_findings", content="combined"),
        _msg(id=5, role="review_verdict", content=_verdict_json("passed")),
    ]
    blocks = group_messages_for_render(msgs)

    batch = blocks[0]
    assert isinstance(batch, AgenticToolBatchBlock)
    it = batch.iterations[0]
    assert len(it.tool_calls) == 2
    assert it.tool_calls[0][1] is None  # first call orphaned
    assert it.tool_calls[1][1] is not None
    assert it.tool_calls[1][1].id == 3


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


# ---------------------------------------------------------------------------
# Agentic-mode render helpers (phase 13d)
# ---------------------------------------------------------------------------


def test_agentic_summary_text_initial_state() -> None:
    """iterations_run=0 + not-done → just "researching…", no iteration
    number. Used by the empty card shell on first emission."""
    assert agentic_summary_text(0, done=False) == "researching…"


def test_agentic_summary_text_mid_iteration() -> None:
    """iterations_run>0 + not-done → "researching (iteration N)…"."""
    assert (
        agentic_summary_text(1, done=False) == "researching (iteration 1)…"
    )
    assert (
        agentic_summary_text(3, done=False) == "researching (iteration 3)…"
    )


def test_agentic_summary_text_done_singular_and_plural() -> None:
    """Past tense: singular at 1, plural elsewhere — same convention
    as summary_text() for tool counts."""
    assert agentic_summary_text(1, done=True) == "ran 1 iteration"
    assert agentic_summary_text(2, done=True) == "ran 2 iterations"
    assert agentic_summary_text(3, done=True) == "ran 3 iterations"


def test_agentic_summary_text_done_does_not_carry_max_reached_signal() -> None:
    """The summary text is identical whether max-iterations fired or
    not — the "(max reached)" badge lives in the sibling marker span
    (see render_max_iterations_badge). The summary swap on done only
    cares about the iteration count."""
    # No way to ask the summary about max-iterations any more — the
    # function signature dropped that parameter. The phrasing is the
    # same regardless of how the loop terminated.
    assert agentic_summary_text(3, done=True) == "ran 3 iterations"


def test_render_agentic_card_shell_has_agentic_modifier() -> None:
    """The shell carries the `tool-card--agentic` modifier class so
    CSS can target iteration headers / verdicts / findings rows
    without affecting single-agent cards."""
    html_out = render_agentic_card_shell(
        card_id="tool-card-T",
        list_id="tool-card-T-list",
        summary_id="tool-card-T-summary",
        conversation_id=42,
    )
    assert 'class="tool-card tool-card--agentic"' in html_out
    # OOB swap targets the streaming placeholder, same as the
    # single-agent first-call path.
    assert 'hx-swap-oob="beforebegin:#assistant-stream-42"' in html_out
    # Summary reads the initial "researching…" — no iteration count yet.
    assert "researching…" in html_out
    # The empty <ul> is present and addressable.
    assert 'id="tool-card-T-list"' in html_out


def test_render_agentic_card_shell_plants_max_marker_span() -> None:
    """The sentinel `<span id="…-max-marker">` is rendered empty into
    the summary. The orchestrator's max-iterations branch fills it
    via an outerHTML OOB swap — avoiding a full <details> re-render
    that would clobber rows already in the DOM."""
    html_out = render_agentic_card_shell(
        card_id="tool-card-T",
        list_id="tool-card-T-list",
        summary_id="tool-card-T-summary",
        conversation_id=42,
    )
    assert 'id="tool-card-T-max-marker"' in html_out


def test_render_iteration_start_appends_header_and_swaps_summary() -> None:
    """iteration-start carries TWO OOB fragments: a header <li>
    appended to the card's <ul>, and a summary span outerHTML swap
    reading "researching (iteration N)…"."""
    html_out = render_iteration_start(
        iteration_index=2,
        list_id="tool-card-T-list",
        summary_id="tool-card-T-summary",
    )
    # Header row: append + iteration-data attribute.
    assert 'hx-swap-oob="beforeend:#tool-card-T-list"' in html_out
    assert 'class="tool-card__iteration-header"' in html_out
    assert 'data-iteration="2"' in html_out
    assert "Iteration 2" in html_out
    # Summary span swap: outerHTML on the right id.
    assert 'id="tool-card-T-summary"' in html_out
    assert 'hx-swap-oob="outerHTML"' in html_out
    assert "researching (iteration 2)" in html_out


def test_render_agentic_tool_row_append_emits_row_only_no_summary_bump() -> None:
    """Agentic row append is identical to the single-agent variant
    for the row HTML itself, but MUST NOT emit a summary span swap —
    the iteration-start event already set "researching (iteration
    N)…" and downstream rows don't change it."""
    row = ToolRowView(
        id="tool-card-T-iter-1-row-0",
        label='searching arxiv: "x"',
        elapsed_start_ms=1_000,
        elapsed_final_ms=None,
        elapsed_display="0:00",
    )
    html_out = render_agentic_tool_row_append(
        live_row=row, list_id="tool-card-T-list",
    )
    # Row append into the existing list.
    assert 'hx-swap-oob="beforeend:#tool-card-T-list"' in html_out
    assert 'id="tool-card-T-iter-1-row-0"' in html_out
    # No summary swap — the agentic shell's summary stays put.
    assert 'id="tool-card-T-summary"' not in html_out
    assert "using" not in html_out  # no "using N tools…" text


def test_render_findings_row_renders_markdown_inside_details() -> None:
    """Findings text passes through the markdown filter so the
    model's natural prose renders formatted (bullets, bold, code).
    The outer <li> is the swap unit; the <details> just rides along."""
    html_out = render_findings_row(
        findings="**Key finding**: ozone enhances gas transfer by 12%.",
        iteration_index=1,
        list_id="tool-card-T-list",
    )
    assert 'class="tool-card__findings"' in html_out
    assert 'data-iteration="1"' in html_out
    assert 'hx-swap-oob="beforeend:#tool-card-T-list"' in html_out
    assert "<details" in html_out
    # Markdown's **bold** rendered as <strong>.
    assert "<strong>Key finding</strong>" in html_out
    assert "ozone enhances gas transfer by 12%" in html_out


def test_render_findings_row_swap_oob_on_outer_li() -> None:
    """hx-swap-oob sits on the outer <li> (the swap unit), not on
    the inner <details>. Mirrors the same OOB-unit contract as
    _tool_row.html."""
    html_out = render_findings_row(
        findings="x", iteration_index=1, list_id="L",
    )
    li_prefix, _, details_part = html_out.partition("<details")
    assert "hx-swap-oob" in li_prefix
    assert "hx-swap-oob" not in details_part


def test_render_verdict_row_passed_uses_check_glyph() -> None:
    """Passed verdict gets the check_circle Material Symbols glyph,
    `--passed` class modifier, and the "Passed:" label."""
    html_out = render_verdict_row(
        verdict_status="passed",
        verdict_message="findings cover the question",
        iteration_index=1,
        list_id="tool-card-T-list",
    )
    assert "tool-card__verdict--passed" in html_out
    assert "check_circle" in html_out
    assert "Passed:" in html_out
    assert "findings cover the question" in html_out
    assert 'hx-swap-oob="beforeend:#tool-card-T-list"' in html_out


def test_render_verdict_row_failed_uses_cancel_glyph() -> None:
    """Failed verdict gets the cancel Material Symbols glyph,
    `--failed` class modifier, and the "Failed:" label."""
    html_out = render_verdict_row(
        verdict_status="failed",
        verdict_message="missing source citations",
        iteration_index=2,
        list_id="tool-card-T-list",
    )
    assert "tool-card__verdict--failed" in html_out
    assert "cancel" in html_out
    assert "Failed:" in html_out
    assert "missing source citations" in html_out


def test_render_verdict_row_escapes_message_html() -> None:
    """Verdict message comes from the model; Jinja autoescape must
    keep model-injected HTML from rendering as live markup."""
    html_out = render_verdict_row(
        verdict_status="failed",
        verdict_message="<script>alert(1)</script>",
        iteration_index=1,
        list_id="L",
    )
    # The literal <script> tag is escaped to entities, not rendered.
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_render_max_iterations_badge_fills_marker_with_visible_text() -> None:
    """The badge is a tiny outerHTML swap on the sentinel marker span
    that render_agentic_card_shell planted in the summary. Avoids
    re-rendering the whole <details>. Crucially the swap inserts
    VISIBLE badge text so the user sees the cap-hit signal between
    the iteration-3 failure and the final done event — not just a
    data attribute that's invisible until 13f's CSS kicks in."""
    html_out = render_max_iterations_badge("tool-card-T")
    assert 'id="tool-card-T-max-marker"' in html_out
    assert 'hx-swap-oob="outerHTML"' in html_out
    assert 'data-max-iterations="true"' in html_out
    # Visible content: the user sees this even with no CSS applied.
    assert "(max reached)" in html_out


def test_render_agentic_done_summary_phrasing() -> None:
    """Final summary swap on done — past tense, plural at N>1. The
    summary phrasing is identical regardless of whether the loop
    terminated via 'passed' or max-iterations; the cap-hit signal
    rides in the sibling marker span (see render_max_iterations_badge),
    not in the summary text."""
    html_out = render_agentic_done_summary(
        summary_id="tool-card-T-summary",
        iterations_run=2,
    )
    assert 'id="tool-card-T-summary"' in html_out
    assert 'hx-swap-oob="outerHTML"' in html_out
    assert "ran 2 iterations" in html_out
    # Summary never carries the max-reached suffix — that's the
    # marker's job, and it's a sibling not a child of #summary, so
    # this outerHTML swap leaves it intact.
    assert "max reached" not in html_out


def test_render_agentic_done_summary_singular_at_one_iteration() -> None:
    """Pluralization matches summary_text() conventions for the
    single-agent tool-card."""
    html_out = render_agentic_done_summary(
        summary_id="tool-card-T-summary",
        iterations_run=1,
    )
    assert "ran 1 iteration" in html_out
    assert "iterations" not in html_out


def test_marker_and_done_summary_are_independent_swap_targets() -> None:
    """The done-event's outerHTML on #{summary_id} replaces only the
    summary span. The marker span is a sibling (see the shell
    template) so it survives the swap — the "(max reached)" badge
    stays visible alongside the past-tense summary at done time.

    Pins the structural contract: marker id and summary id are
    distinct so HTMX targets them independently."""
    badge = render_max_iterations_badge("tool-card-T")
    summary = render_agentic_done_summary(
        summary_id="tool-card-T-summary", iterations_run=3,
    )
    # Two different swap targets; one swap doesn't touch the other.
    assert 'id="tool-card-T-max-marker"' in badge
    assert 'id="tool-card-T-summary"' in summary
    assert "max-marker" not in summary
    assert "-summary" not in badge.replace("tool-card-T-max-marker", "")
