"""HTTP routes that return HTML fragments for HTMX.

Originally a single ``app/routes.py`` module. Split into per-area
sub-modules — each owning an ``APIRouter`` — to keep route bodies
near each other by topic. The package's ``router`` is the union of all
sub-routers, mounted by ``main.py``.

Sub-modules:
    :mod:`app.routes.chats`    — chat panel + sidebar + messages + stream
                                 + per-chat settings (temperature, tool cap,
                                 agent, tool/RAG toggles), plus ``/`` and
                                 ``/models``.
    :mod:`app.routes.projects` — projects index + CRUD + chats/settings tabs.
    :mod:`app.routes.files`    — files tab routes (list, view, download).
    :mod:`app.routes.settings` — global settings page (RAG server CRUD +
                                 default-* settings).

HTTP error mapping is the same as Phase 6:
    ``OllamaUnavailable`` → 503
    ``OllamaProtocolError`` → 502
    ``LookupError`` (unknown id) → 404
Mid-stream failures emit an SSE ``event: error`` (headers already sent).

Re-exports for tests:
    ``ollama`` — the :mod:`app.ollama` module (tests patch
        ``routes.ollama.generate_title`` etc.).
    ``_placeholder_name`` — the user-message → sidebar-name helper.
"""

from fastapi import APIRouter

# Side-effecting imports: app.tools.builtins registers `current_time`
# (and the file tools) and app.tools.rag registers `query_rag` via their
# @tool decorators. Without these imports the production app would never
# call those modules (the registry would be empty).
from app import ollama  # re-exported for tests that do `routes.ollama`
from app.routes._helpers import _placeholder_name  # re-exported for tests
from app.routes.chats import router as _chats_router
from app.routes.degrees import router as _degrees_router
from app.routes.files import router as _files_router
from app.routes.projects import router as _projects_router
from app.routes.settings import router as _settings_router
from app.tools import builtins as _tools_builtins  # noqa: F401
from app.tools import github as _github_tool  # noqa: F401
from app.tools import rag as _rag_tool  # noqa: F401

router = APIRouter()
router.include_router(_chats_router)
router.include_router(_degrees_router)
router.include_router(_files_router)
router.include_router(_projects_router)
router.include_router(_settings_router)

__all__ = ["router", "ollama", "_placeholder_name"]
