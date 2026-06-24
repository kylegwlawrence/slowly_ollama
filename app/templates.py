"""Jinja2 templates + custom filters shared across the app.

Its own module (not inside ``app/routes.py``) so the producer layer in
``app/generation.py`` — and anything else rendering a fragment — can import
the ``templates`` instance without depending on the HTTP-routing layer,
which would be a circular dependency.

Importing this module builds the ``Jinja2Templates`` instance pointed at the
project-root ``templates/`` directory and wires the ``markdown`` filter onto
it, so ``{{ message.content | markdown | safe }}`` works in any template.
The wiring is a side effect of import — callers just import; nothing to call.
"""

import re
from pathlib import Path

import markdown as _md
from fastapi.templating import Jinja2Templates

# Templates live at the project root (one level up from this package).
# Resolving relative to ``__file__`` keeps the lookup correct regardless
# of where ``uvicorn`` is launched from.
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=_TEMPLATE_DIR)

# fenced_code: ```lang``` blocks. tables: GFM tables. arithmatex: shields
# LaTeX math from the parser (else `$x_i$` → `$x<em>i</em>$`) and normalises
# every delimiter style to \(...\) / \[...\] in .arithmatex spans/divs.
# `generic=True` emits renderer-agnostic spans (not MathJax-script); static/
# app.js typesets them with KaTeX. Code fences and bare currency ("$5") are
# left untouched.
_md_converter = _md.Markdown(
    extensions=["fenced_code", "tables", "pymdownx.arithmatex"],
    extension_configs={"pymdownx.arithmatex": {"generic": True}},
)

# Matches any line that starts a list item (ordered or unordered).
_LIST_ITEM_RE = re.compile(r"^[ \t]*(\d+[.)]\s+|[-*+]\s+)")


def _ensure_list_spacing(text: str) -> str:
    """Insert a blank line before list items that follow non-list text.

    LLMs often omit the blank line standard Markdown requires before a list
    that follows paragraph text (e.g. "Steps:\n1. First"); without it the
    whole thing renders as one paragraph. This pass inserts the missing line
    so the list is recognised.
    """
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        if _LIST_ITEM_RE.match(line) and out and out[-1].strip() and not _LIST_ITEM_RE.match(out[-1]):
            out.append("")
        out.append(line)
    return "\n".join(out)


def _is_table_delimiter(line: str) -> bool:
    """Return True if ``line`` is a GFM table delimiter row (e.g. ``|---|---|``).

    A delimiter row holds only pipes, dashes, alignment colons, and spaces,
    and must contain at least one of each of a pipe and a dash. Requiring a
    pipe keeps a bare ``---`` thematic break from reading as a table.
    """
    stripped = line.strip()
    return (
        "|" in stripped
        and "-" in stripped
        and set(stripped) <= set("|:- \t")
    )


def _ensure_table_spacing(text: str) -> str:
    """Insert a blank line before a table header that follows non-table text.

    The ``tables`` extension only recognises a table when a blank line precedes
    its header row. LLMs routinely glue the header straight onto a lead-in line
    (e.g. "Here's the data:\n| Type | Example |\n|---|---|"), so the whole block
    renders as one paragraph of raw pipes. This pass spots a header (a pipe line
    whose next line is a delimiter row) sitting on prose and inserts the missing
    blank line so the table is parsed.
    """
    lines = text.split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        is_header = (
            "|" in line
            and i + 1 < len(lines)
            and _is_table_delimiter(lines[i + 1])
        )
        # Only split when the preceding emitted line is prose — a non-blank line
        # that isn't itself a table row (no pipe) — so existing tables and the
        # delimiter row's own lookahead are left untouched.
        if is_header and out and out[-1].strip() and "|" not in out[-1]:
            out.append("")
        out.append(line)
    return "\n".join(out)


# --- LaTeX math protection -------------------------------------------------
# arithmatex only shields math its block processor *recognises* — a `\[` / `$$`
# that starts its own block. One indented in a list item or glued to prose is
# skipped, and the parser then mangles it (`\[` → bare `[`, `_` → emphasis). So
# we pre-extract the three delimiter styles static/app.js renders, swap each for
# an opaque placeholder the parser ignores, then restore them afterward as the
# `.arithmatex` HTML arithmatex emits. Single `$...$` is left to arithmatex (its
# smart_dollar guard keeps "$5" from reading as math).

# Sentinel wrapping a span's index. All-caps ASCII so the parser treats it as an
# ordinary word; the *trailing* sentinel brackets the index so placeholder 1 is
# never a substring of 11.
_MATH_STASH = "ARITHMATEXSTASH"

# Fenced (``` / ~~~) and inline (`...`) code. Math inside code is literal, so
# mask these regions before protecting math — matching arithmatex's code
# exemption and renderMathInElement's <code>/<pre> skip.
_CODE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`")

# The three delimiter styles static/app.js typesets, captured verbatim.
# Display first so `$$` wins over the inline `\(` alternative at a shared spot.
_MATH_RE = re.compile(
    r"\\\[(?P<bracket>[\s\S]+?)\\\]"
    r"|\$\$(?P<dollar>[\s\S]+?)\$\$"
    r"|\\\((?P<paren>[\s\S]+?)\\\)"
)

# A <p> wrapping only display-math div(s): markdown wraps our placeholder in a
# <p>, but a block <div> inside a <p> is invalid HTML and the browser
# auto-closes the <p>. Unwrap so standalone display math is a clean block; a
# <p> that also holds prose is left alone (it still typesets in place).
_SOLO_DISPLAY_P_RE = re.compile(
    r'<p>\s*((?:<div class="arithmatex">[\s\S]*?</div>\s*)+)</p>'
)


def _protect_math(text: str) -> tuple[str, list[str]]:
    """Swap LaTeX math spans for placeholders before the markdown pass.

    Extracts every `\\(...\\)`, `\\[...\\]`, and `$$...$$` span outside code,
    replacing each with an opaque placeholder and recording the `.arithmatex`
    HTML to restore. Content is captured verbatim so the parser never mangles
    the delimiters or math — even where arithmatex's block processor would
    have skipped it (indented in a list, glued to prose).

    Args:
        text: Raw assistant markdown, possibly containing LaTeX math.

    Returns:
        A ``(masked_text, spans)`` pair: ``masked_text`` with each span
        replaced by a ``_MATH_STASH``-bracketed placeholder, and ``spans``
        the restoration HTML indexed by the number in each placeholder.
    """
    spans: list[str] = []

    def _stash(inner: str, *, display: bool) -> str:
        # Normalise to the backslash delimiters arithmatex emits so the
        # client's renderMathInElement (scanning for \(...\)/\[...\]) typesets
        # it and the persisted HTML matches the streaming render.
        left, right, tag = (r"\[", r"\]", "div") if display else (r"\(", r"\)", "span")
        spans.append(f'<{tag} class="arithmatex">{left}{inner.strip()}{right}</{tag}>')
        return f"{_MATH_STASH}{len(spans) - 1}{_MATH_STASH}"

    def _sub(m: re.Match[str]) -> str:
        if (inner := m.group("bracket")) is not None:
            return _stash(inner, display=True)
        if (inner := m.group("dollar")) is not None:
            return _stash(inner, display=True)
        return _stash(m.group("paren"), display=False)

    # Walk the text, leaving code regions verbatim and protecting math between.
    out: list[str] = []
    pos = 0
    for code in _CODE_RE.finditer(text):
        out.append(_MATH_RE.sub(_sub, text[pos : code.start()]))
        out.append(code.group(0))
        pos = code.end()
    out.append(_MATH_RE.sub(_sub, text[pos:]))
    return "".join(out), spans


def _restore_math(html: str, spans: list[str]) -> str:
    """Replace math placeholders with their restored `.arithmatex` HTML.

    Args:
        html: Markdown-converted HTML still holding ``_protect_math`` placeholders.
        spans: Restoration HTML indexed by placeholder number, from ``_protect_math``.

    Returns:
        The HTML with every placeholder swapped back for its math span and any
        ``<p>`` that wraps only display math unwrapped to a clean block.
    """
    for i, span in enumerate(spans):
        html = html.replace(f"{_MATH_STASH}{i}{_MATH_STASH}", span)
    return _SOLO_DISPLAY_P_RE.sub(r"\1", html)


def _render_markdown(text: str) -> str:
    """Convert markdown text to an HTML string.

    Math spans are protected before the pass and restored after, so
    arithmatex's block-processor blind spots (math indented in a list or
    glued to prose) don't leak raw LaTeX. Resets the Markdown instance's
    state since it's reused across requests.
    """
    _md_converter.reset()
    protected, spans = _protect_math(text)
    html = _md_converter.convert(_ensure_table_spacing(_ensure_list_spacing(protected)))
    return _restore_math(html, spans)


templates.env.filters["markdown"] = _render_markdown


# Expose the backup feature-gate + status as template globals so the
# backup-status chip can self-gate and self-seed without every chat-panel
# render site threading two more context vars. `app.backup` never imports
# app.templates, so this is cycle-safe.
from app import backup as _backup  # noqa: E402 — after the instance is built
from app import config as _config  # noqa: E402

templates.env.globals["backups_enabled"] = _config.backups_enabled
templates.env.globals["backup_status"] = _backup.backup_status
