"""Phase 12e: tests for app/render.py — block grouping + view helpers."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.queries import Message
from app.render import (
    MessageBlock,
    ToolBatchBlock,
    ToolRowView,
    card_id_for,
    format_elapsed_mm_ss,
    group_messages_for_render,
    summary_text,
)


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
