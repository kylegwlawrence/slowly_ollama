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
_md_converter = _md.Markdown(extensions=["fenced_code", "tables"])

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


# Phase 20b: agent_host_label is imported lazily inside the wrapper so this
# module stays importable from app.generation without dragging in the agent
# registry at module load (and the cycle that would create with app.config /
# app.queries). The first template render warms the import.
def _agent_host_label(spec):
    """Jinja filter: hostname for an agent's ollama_host (or None)."""
    from app.agents import agent_host_label
    return agent_host_label(spec)


templates.env.filters["agent_host_label"] = _agent_host_label
