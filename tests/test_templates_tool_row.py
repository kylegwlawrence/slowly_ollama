"""Phase 12h: template-level tests for the two-branch _tool_row.html.

Asserts on contract substrings (class names, hx-* attributes, data-*
attributes) rather than full DOM-tree shape so the tests survive
non-meaningful template tweaks (whitespace, attribute reordering).
"""

import pytest

from app.render import ToolRowView
from app.templates import templates
from app.tools import Source


def _render(row: ToolRowView, swap_oob: str | None = None) -> str:
    """Render the row template the same way the app does."""
    return templates.get_template("_tool_row.html").render(
        row=row, swap_oob=swap_oob
    )


def _frozen_row(
    *, sources: list[Source] | None = None, row_id: str = "r0"
) -> ToolRowView:
    """Helper: a frozen-state row with the elapsed_final_ms set."""
    return ToolRowView(
        id=row_id,
        label='searching arxiv: "x"',
        elapsed_start_ms=None,
        elapsed_final_ms=8000,
        elapsed_display="0:08",
        sources=sources or [],
    )


# ---------------------------------------------------------------------------
# Plain branch (no sources)
# ---------------------------------------------------------------------------


def test_tool_row_renders_plain_li_without_sources() -> None:
    """Empty sources → original plain <li> form. No expandable modifier
    class, no <details>, no chevron."""
    html = _render(_frozen_row(sources=[]))
    assert 'class="tool-row"' in html
    assert "tool-row--expandable" not in html
    assert "<details" not in html
    assert "tool-row__chevron" not in html
    # The label + elapsed still render (pinned contract for the plain row).
    assert 'class="tool-row__label"' in html
    assert 'class="tool-row__elapsed"' in html


def test_tool_row_plain_branch_preserves_elapsed_attrs() -> None:
    """Plain row keeps data-elapsed-* attributes on the outer <li> so
    the JS tick driver finds it via the same selector as before."""
    row = ToolRowView(
        id="live-row-0",
        label="searching arxiv",
        elapsed_start_ms=1234,
        elapsed_final_ms=None,
        elapsed_display="0:00",
    )
    html = _render(row)
    assert 'data-elapsed-start="1234"' in html
    assert "data-elapsed-final" not in html


# ---------------------------------------------------------------------------
# Expandable branch (sources present)
# ---------------------------------------------------------------------------


def test_tool_row_renders_details_when_sources_present() -> None:
    """Non-empty sources → expandable form with <details>, chevron,
    and an inner <ul class="tool-row__sources">."""
    html = _render(_frozen_row(sources=[Source(title="Foo", section="Intro")]))
    assert "tool-row--expandable" in html
    assert '<details class="tool-row__details">' in html
    assert 'class="tool-row__chevron material-symbols-outlined"' in html
    assert "expand_more" in html
    assert 'class="tool-row__sources"' in html
    assert 'class="tool-row__source"' in html


def test_tool_row_single_source_with_section_renders_section_marker() -> None:
    """Single chunk with a section → title + `(§Section)` suffix.

    Pins the chosen format (§Section). Changing it would require
    updating this assertion intentionally.
    """
    html = _render(_frozen_row(sources=[Source(title="Foo", section="Intro")]))
    assert "Foo" in html
    assert "(§Intro)" in html


def test_tool_row_single_source_no_section_renders_title_only() -> None:
    """Single chunk with no section → title only; no `(§…)` suffix."""
    html = _render(_frozen_row(sources=[Source(title="Foo", section=None)]))
    assert "Foo" in html
    assert "(§" not in html


def test_tool_row_multi_chunk_shows_chunk_count_drops_section() -> None:
    """Two chunks of the same title → `(N chunks)` and no section."""
    html = _render(_frozen_row(sources=[
        Source(title="Foo", section="A"),
        Source(title="Foo", section="B"),
    ]))
    assert "Foo" in html
    assert "(2 chunks)" in html
    # The per-chunk sections are intentionally dropped post-dedup.
    assert "(§A)" not in html
    assert "(§B)" not in html


def test_tool_row_multiple_unique_titles_render_separate_lines() -> None:
    """Two different titles → two <li class="tool-row__source"> entries."""
    html = _render(_frozen_row(sources=[
        Source(title="First", section=None),
        Source(title="Second", section="Body"),
    ]))
    # Two source entries → two opening tags for the inner li.
    assert html.count('class="tool-row__source"') == 2
    assert "First" in html
    assert "Second" in html
    assert "(§Body)" in html


def test_tool_row_expandable_preserves_elapsed_attrs_on_outer_li() -> None:
    """The outer <li> (the swap unit) still carries data-elapsed-*
    attributes — they're NOT pushed inside <details>. This keeps the
    JS tick driver's selector (`.tool-row[data-elapsed-start]…`)
    working unchanged in the expandable case."""
    row = _frozen_row(sources=[Source(title="T", section="S")])
    html = _render(row)
    # The data attribute appears in the <li …data-elapsed-final="8000">
    # prefix, before the <details> child opens.
    li_prefix, _, details_part = html.partition("<details")
    assert 'data-elapsed-final="8000"' in li_prefix
    assert "data-elapsed-" not in details_part


def test_tool_row_expandable_swap_oob_lives_on_outer_li() -> None:
    """When rendered as an OOB swap payload, hx-swap-oob sits on the
    outer <li> — the <details> child is just inner content, not the
    swap unit. Pins the HTMX contract that the SSE tool-result event
    replaces the row by id, with the nested <details> riding along."""
    row = _frozen_row(sources=[Source(title="T", section="S")])
    html = _render(row, swap_oob="outerHTML")
    li_prefix, _, details_part = html.partition("<details")
    assert 'hx-swap-oob="outerHTML"' in li_prefix
    assert "hx-swap-oob" not in details_part


def test_tool_row_plain_swap_oob_lives_on_outer_li() -> None:
    """Same OOB-unit contract holds for the non-expandable branch."""
    html = _render(_frozen_row(sources=[]), swap_oob="outerHTML")
    assert 'hx-swap-oob="outerHTML"' in html


@pytest.mark.parametrize(
    "swap_oob",
    ["outerHTML", "beforeend:#tool-card-1-list", "true"],
)
def test_tool_row_swap_oob_renders_verbatim(swap_oob: str) -> None:
    """The template doesn't transform swap_oob — every value passes
    through unchanged so the SSE / template-include call sites stay
    in control of the swap target."""
    html = _render(_frozen_row(sources=[]), swap_oob=swap_oob)
    assert f'hx-swap-oob="{swap_oob}"' in html


def test_tool_row_no_swap_oob_when_unset() -> None:
    """Default render (e.g. historic replay inside the card shell)
    emits no hx-swap-oob attribute. The shell is the swap unit there;
    rows ride inside it."""
    html = _render(_frozen_row(sources=[Source(title="T", section=None)]))
    assert "hx-swap-oob" not in html
