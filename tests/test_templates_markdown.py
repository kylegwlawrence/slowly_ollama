"""Tests for the server-side markdown filter's LaTeX math handling.

The `markdown` Jinja filter (``app.templates._render_markdown``) renders the
*persisted* assistant bubbles (full page load + the `done` OOB swap). Math
support there is `pymdownx.arithmatex` in ``generic`` mode: it shields LaTeX
from the markdown parser and normalises every delimiter style to
``\\(...\\)`` / ``\\[...\\]`` inside ``.arithmatex`` elements, which
``static/app.js`` typesets with KaTeX in the browser.

These tests pin that contract (the wrapper class + normalised delimiters +
the exemptions for code and currency). The actual KaTeX typesetting is JS and
is covered by the manual browser smoke test, not here.
"""

from app.templates import _render_markdown


def test_inline_dollar_math_wraps_in_arithmatex_span() -> None:
    """`$...$` becomes a `.arithmatex` span with `\\(...\\)` delimiters."""
    html = _render_markdown(r"The value $x_i^2$ matters.")
    assert '<span class="arithmatex">' in html
    assert r"\(x_i^2\)" in html


def test_inline_dollar_math_is_not_mangled_by_emphasis() -> None:
    """The bug we're fixing: markdown must not read `_` inside math as emphasis.

    Without arithmatex, `$x_i$` renders as `$x<em>i</em>$`.
    """
    html = _render_markdown(r"$x_i$")
    assert "<em>" not in html


def test_block_dollar_math_wraps_in_arithmatex_div() -> None:
    """`$$...$$` becomes a block `.arithmatex` div with `\\[...\\]`."""
    html = _render_markdown("Equation:\n\n$$\n\\frac{a}{b} = c\n$$")
    assert '<div class="arithmatex">' in html
    assert r"\[" in html and r"\]" in html
    assert r"\frac{a}{b} = c" in html


def test_backslash_paren_math_normalises_to_arithmatex_span() -> None:
    """`\\(...\\)` (common LLM output) lands in the same span shape as `$...$`."""
    html = _render_markdown(r"Euler: \(e^{i\pi}+1=0\).")
    assert '<span class="arithmatex">' in html
    assert r"\(e^{i\pi}+1=0\)" in html


def test_backslash_bracket_math_normalises_to_arithmatex_div() -> None:
    """`\\[...\\]` display math lands in the same div shape as `$$...$$`."""
    html = _render_markdown(r"\[ \int_0^1 x^2 \, dx = \frac{1}{3} \]")
    assert '<div class="arithmatex">' in html
    assert r"\int_0^1 x^2 \, dx = \frac{1}{3}" in html


def test_math_inside_code_fence_stays_literal() -> None:
    """A `$...$` inside a fenced code block is not treated as math."""
    html = _render_markdown("```\n$x_i$ is literal here\n```")
    assert "arithmatex" not in html
    assert "$x_i$ is literal here" in html


def test_bare_currency_is_not_treated_as_math() -> None:
    """Two bare dollar amounts in prose must not be parsed as one math span."""
    html = _render_markdown("It costs $5 and $10 total.")
    assert "arithmatex" not in html
    assert "$5 and $10" in html


def test_ordinary_markdown_still_renders_alongside_math() -> None:
    """Adding arithmatex doesn't regress the existing fenced_code/list passes."""
    html = _render_markdown("Steps:\n1. First\n2. Second\n\nDone with $a+b$.")
    assert "<ol>" in html  # list-spacing pre-pass + standard markdown
    assert '<span class="arithmatex">' in html
