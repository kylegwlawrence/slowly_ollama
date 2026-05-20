"""Phase 13f: template-level smoke tests for _agentic_tool_card.html.

The render-layer tests in test_render.py cover the AgenticToolBatchBlock
dataclasses. This file pins the historic-replay template's *output*
contract — class hooks, DOM ids, iteration ordering — so a future
template tweak (whitespace, attribute reordering) doesn't quietly
break the live-vs-historic DOM parity.

Assertions are substring-based, mirroring the test_templates_tool_row
style: durable to non-meaningful template churn.
"""

import json
from datetime import datetime, timedelta, timezone

from app.queries import Message
from app.render import (
    AgenticIteration,
    AgenticToolBatchBlock,
    group_messages_for_render,
)
from app.templates import templates


def _msg(
    *,
    id: int,
    role: str,
    content: str,
    created_at: datetime | None = None,
) -> Message:
    return Message(
        id=id,
        conversation_id=1,
        role=role,
        content=content,
        created_at=created_at
        or datetime(2026, 5, 19, 12, 0, id, tzinfo=timezone.utc),
    )


def _verdict_json(status: str, message: str = "") -> str:
    return json.dumps({"verdict": status, "message": message})


def _render(block: AgenticToolBatchBlock) -> str:
    """Render the historic template against a block, as _chat_panel.html does."""
    return templates.get_template("_agentic_tool_card.html").render(
        block=block
    )


def test_agentic_card_renders_outer_details_with_agentic_modifier() -> None:
    """The outer <details> carries both `tool-card` (so base CSS
    applies) and `tool-card--agentic` (so iteration-specific styles
    can target only this variant)."""
    block = AgenticToolBatchBlock(
        iterations=[
            AgenticIteration(
                index=1,
                tool_calls=[],
                findings=None,
                verdict=_msg(
                    id=1,
                    role="review_verdict",
                    content=_verdict_json("passed", "ok"),
                ),
            ),
        ],
        turn_id="hist-1",
    )
    html = _render(block)

    # Both class names present; we don't pin attribute order.
    assert "tool-card" in html
    assert "tool-card--agentic" in html
    # Card id + list id + summary id all consistent with the block.
    assert 'id="tool-card-hist-1"' in html
    assert 'id="tool-card-hist-1-list"' in html
    assert 'id="tool-card-hist-1-summary"' in html


def test_agentic_card_renders_max_marker_when_cap_hit() -> None:
    """When the cap was reached and the last verdict wasn't `passed`,
    the historic template plants the same max-marker span the live
    `render_max_iterations_badge` produces, with
    `data-max-iterations="true"` and the visible badge text."""
    msgs: list[Message] = []
    next_id = 1
    for _ in range(3):
        msgs.append(_msg(id=next_id, role="research_findings", content="r"))
        msgs.append(
            _msg(
                id=next_id + 1,
                role="review_verdict",
                content=_verdict_json("failed", "needs more"),
            )
        )
        next_id += 2
    block = group_messages_for_render(msgs)[0]
    assert isinstance(block, AgenticToolBatchBlock)
    html = _render(block)

    assert 'data-max-iterations="true"' in html
    assert "(max reached)" in html
    # The marker sits as a sibling of the summary span, NOT inside it.
    # Substring order check: the marker span appears AFTER the summary
    # span's closing tag.
    summary_close = html.index("</span>", html.index('id="tool-card-hist-1-summary"'))
    marker_open = html.index('id="tool-card-hist-1-max-marker"')
    assert marker_open > summary_close


def test_agentic_card_skips_max_marker_text_when_below_cap() -> None:
    """A passing iteration short of the cap renders the max-marker
    span empty — the historic DOM still has the placeholder (so live
    replays after a partial reload can target it), but no badge text
    appears."""
    block = AgenticToolBatchBlock(
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
    html = _render(block)
    assert "data-max-iterations" not in html
    assert "(max reached)" not in html
    # Empty marker span is still there as a placeholder.
    assert 'id="tool-card-hist-1-max-marker"' in html


def test_agentic_card_renders_iteration_headers_and_verdicts_in_order() -> None:
    """Iterations render in chronological order with their headers,
    findings, and verdicts grouped together."""
    msgs = [
        _msg(id=1, role="research_findings", content="round 1"),
        _msg(id=2, role="review_verdict", content=_verdict_json("failed", "fb1")),
        _msg(id=3, role="research_findings", content="round 2"),
        _msg(id=4, role="review_verdict", content=_verdict_json("passed", "ok")),
    ]
    block = group_messages_for_render(msgs)[0]
    assert isinstance(block, AgenticToolBatchBlock)
    html = _render(block)

    # Both iteration headers present.
    assert 'data-iteration="1"' in html
    assert 'data-iteration="2"' in html
    # Iteration 1 markers appear before iteration 2 in source order.
    iter1 = html.index('Iteration 1')
    iter2 = html.index('Iteration 2')
    assert iter1 < iter2
    # The failed-then-passed verdict pair renders both class modifiers.
    assert "tool-card__verdict--failed" in html
    assert "tool-card__verdict--passed" in html
    # Their feedback messages are present and HTML-escaped (Jinja
    # default autoescape).
    assert "fb1" in html
    assert "ok" in html


def test_agentic_card_renders_tool_rows_with_iteration_scoped_ids() -> None:
    """Tool rows inside an iteration get the `{card_id}-iter-N-row-M`
    id format, matching the live SSE path so mid-turn reloads line up."""
    call = _msg(
        id=1,
        role="tool_call",
        content=json.dumps({"name": "current_time", "arguments": {}}),
    )
    result = _msg(
        id=2,
        role="tool_result",
        content="ok",
        created_at=call.created_at + timedelta(seconds=2),
    )
    msgs = [
        call,
        result,
        _msg(id=3, role="research_findings", content="notes"),
        _msg(id=4, role="review_verdict", content=_verdict_json("passed")),
    ]
    block = group_messages_for_render(msgs)[0]
    assert isinstance(block, AgenticToolBatchBlock)
    html = _render(block)

    assert f'id="{block.card_id}-iter-1-row-0"' in html


def test_agentic_card_renders_findings_inside_collapsible_details() -> None:
    """Findings rows wrap their text in a nested <details> so the
    panel stays compact by default — same DOM shape the live path
    builds via `_findings_row.html`."""
    msgs = [
        _msg(id=1, role="research_findings", content="some notes about X"),
        _msg(id=2, role="review_verdict", content=_verdict_json("passed")),
    ]
    block = group_messages_for_render(msgs)[0]
    assert isinstance(block, AgenticToolBatchBlock)
    html = _render(block)

    assert "tool-card__findings" in html
    assert "Research findings" in html
    # The findings text body is HTML-escaped by Jinja's markdown
    # filter (which emits <p>...</p>). Either the wrapped paragraph
    # or the raw text body must be present.
    assert "some notes about X" in html
