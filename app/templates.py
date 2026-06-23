"""Jinja2 templates + custom filters shared across the app.

Lives in its own module (rather than inside ``app/routes.py``) so the
producer layer in ``app/generation.py`` — and any other module that
needs to render a fragment — can import the ``templates`` instance
without reaching across into the HTTP-routing layer. The pre-extraction shape (templates owned by
routes.py) forced ``generation.py`` into four function-body
``from app.routes import templates`` lazy imports to dodge a circular
dependency; pulling the template setup down a layer removes the cycle
and lets producers import normally at module load.

Importing this module:

- Builds the ``Jinja2Templates`` instance pointed at the project-root
  ``templates/`` directory.
- Wires the ``markdown`` Jinja filter onto that instance so
  ``{{ message.content | markdown | safe }}`` works in any template.

Side-effecting imports — callers that need the filter wired don't have
to invoke anything; just importing this module is enough.
"""

import re
from pathlib import Path

import markdown as _md
from fastapi.templating import Jinja2Templates

# Templates live at the project root (one level up from this file's
# package dir). Resolving relative to ``__file__`` keeps the directory
# lookup correct regardless of where ``uvicorn`` is launched from.
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=_TEMPLATE_DIR)

# fenced_code: ```lang ... ``` blocks; tables: GFM-style tables.
# arithmatex: protect LaTeX math from the markdown parser (otherwise `$x_i$`
# becomes `$x<em>i</em>$`) and normalise every delimiter style — $...$,
# $$...$$, \(...\), \[...\] — to \(...\) / \[...\] wrapped in .arithmatex
# spans/divs. `generic=True` selects the renderer-agnostic span output (vs the
# MathJax-script output); static/app.js typesets those spans with KaTeX in the
# browser. Code fences and bare currency ("$5") are left untouched.
_md_converter = _md.Markdown(
    extensions=["fenced_code", "tables", "pymdownx.arithmatex"],
    extension_configs={"pymdownx.arithmatex": {"generic": True}},
)

# Matches any line that starts a list item (ordered or unordered).
_LIST_ITEM_RE = re.compile(r"^[ \t]*(\d+[.)]\s+|[-*+]\s+)")


def _ensure_list_spacing(text: str) -> str:
    """Insert a blank line before list items that directly follow non-list text.

    LLMs often omit the blank line that standard Markdown requires before a
    list when it comes after paragraph text (e.g. "Steps:\n1. First").
    Without the blank line the markdown library renders everything as a single
    paragraph.  This pass inserts the missing blank line so the list is
    recognised correctly.
    """
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        if _LIST_ITEM_RE.match(line) and out and out[-1].strip() and not _LIST_ITEM_RE.match(out[-1]):
            out.append("")
        out.append(line)
    return "\n".join(out)


# --- LaTeX math protection -------------------------------------------------
# arithmatex only shields math it *recognises*. Its block processor fires only
# for a `\[` / `$$` that starts its own block; one indented inside a list item
# or glued to preceding prose is skipped, and the markdown parser then mangles
# it — `\[` is backslash-unescaped to a bare `[`, `_` becomes emphasis. We
# pre-extract the three delimiter styles static/app.js auto-renders, swap each
# for an opaque placeholder the parser leaves alone, then restore them after the
# pass as the same `.arithmatex` HTML arithmatex emits. Single `$...$` is left
# to arithmatex (its smart_dollar guard keeps a stray "$5" from being read as
# math), so its currency/code exemptions are unaffected.

# Sentinel wrapping a span's index. All-caps ASCII so the markdown parser treats
# it as an ordinary word and passes it through untouched; the *trailing*
# sentinel brackets the index so placeholder 1 is never a substring of 11.
_MATH_STASH = "ARITHMATEXSTASH"

# Fenced (``` / ~~~) and inline (`...`) code. Math inside code is literal, so we
# mask these regions out before protecting math — matching arithmatex's own code
# exemption and renderMathInElement's <code>/<pre> skip.
_CODE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`")

# The three delimiter styles static/app.js typesets. Captured verbatim (content
# in a named group) and replaced before markdown runs. Display first so `$$`
# wins over the inline `\(` alternative at a shared position.
_MATH_RE = re.compile(
    r"\\\[(?P<bracket>[\s\S]+?)\\\]"
    r"|\$\$(?P<dollar>[\s\S]+?)\$\$"
    r"|\\\((?P<paren>[\s\S]+?)\\\)"
)

# A paragraph wrapping only display-math div(s): markdown wraps our inline
# placeholder in a <p>, but a block <div> inside a <p> is invalid HTML and the
# browser auto-closes the <p>. Unwrap so standalone display math is a clean
# block (a <p> that also holds prose — e.g. display math in a list item — is
# left alone; it still typesets in place).
_SOLO_DISPLAY_P_RE = re.compile(
    r'<p>\s*((?:<div class="arithmatex">[\s\S]*?</div>\s*)+)</p>'
)


def _protect_math(text: str) -> tuple[str, list[str]]:
    """Swap LaTeX math spans for placeholders before the markdown pass.

    Extracts every `\\(...\\)`, `\\[...\\]`, and `$$...$$` span outside fenced or
    inline code, replacing each with an opaque placeholder token, and records
    the `.arithmatex` HTML to restore in its place. Content is captured verbatim
    so the markdown parser never sees — and so never mangles — the delimiters or
    the math itself, regardless of where the span sits (indented in a list, glued
    to prose) where arithmatex's block processor would have skipped it.

    Args:
        text: Raw assistant markdown, possibly containing LaTeX math.

    Returns:
        A ``(masked_text, spans)`` pair: ``masked_text`` with each math span
        replaced by a ``_MATH_STASH``-bracketed placeholder, and ``spans`` the
        restoration HTML indexed by the number inside each placeholder.
    """
    spans: list[str] = []

    def _stash(inner: str, *, display: bool) -> str:
        # Normalise to the backslash delimiters arithmatex emits so the client's
        # renderMathInElement (which scans for \(...\)/\[...\]) typesets it and
        # the persisted HTML matches the streaming render.
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

    Math spans are protected before the markdown pass and restored after, so
    arithmatex's block-processor blind spots (display math indented in a list or
    glued to prose) no longer leak raw LaTeX onto the page. Resets the Markdown
    instance's internal state because it's reused across requests for efficiency.
    """
    _md_converter.reset()
    protected, spans = _protect_math(text)
    html = _md_converter.convert(_ensure_list_spacing(protected))
    return _restore_math(html, spans)


templates.env.filters["markdown"] = _render_markdown


# Phase 21: expose the backup feature-gate + status to templates as globals so
# the backup-status chip can self-gate and self-seed without every chat-panel
# render site threading two more context vars. `app.backup` imports only
# config / connection / queries (never app.templates), so this is cycle-safe.
from app import backup as _backup  # noqa: E402 — after the instance is built
from app import config as _config  # noqa: E402

templates.env.globals["backups_enabled"] = _config.backups_enabled
templates.env.globals["backup_status"] = _backup.backup_status
