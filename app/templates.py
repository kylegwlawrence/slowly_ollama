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


def _render_markdown(text: str) -> str:
    """Convert markdown text to an HTML string.

    Resets internal state between calls because the Markdown instance is
    reused across requests for efficiency.
    """
    _md_converter.reset()
    return _md_converter.convert(_ensure_list_spacing(text))


templates.env.filters["markdown"] = _render_markdown


# Phase 21: expose the backup feature-gate + status to templates as globals so
# the backup-status chip can self-gate and self-seed without every chat-panel
# render site threading two more context vars. `app.backup` imports only
# config / connection / queries (never app.templates), so this is cycle-safe.
from app import backup as _backup  # noqa: E402 — after the instance is built
from app import config as _config  # noqa: E402

templates.env.globals["backups_enabled"] = _config.backups_enabled
templates.env.globals["backup_status"] = _backup.backup_status


# Host picker: expose the primary OLLAMA_HOST's hostname so the picker's
# leading option/label ("host1") derives from config rather than being
# hardcoded. A callable global (not a value) so it reads the env at render
# time; app.config imports nothing from app.templates, so this is cycle-safe.
def _primary_host_label() -> str:
    """Jinja global: hostname of the primary OLLAMA_HOST (host-picker label)."""
    from urllib.parse import urlparse

    try:
        raw = _config.ollama_host()
    except KeyError:
        # OLLAMA_HOST unset (shouldn't happen in a configured app) — render a
        # neutral label rather than 500 the whole page.
        return "default"
    return urlparse(raw).hostname or raw


templates.env.globals["primary_host_label"] = _primary_host_label
