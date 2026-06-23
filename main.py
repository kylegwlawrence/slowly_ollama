"""FastAPI application entry point for ollama_slowly.

Run with::

    uvicorn main:app

The lifespan opens shared resources once at startup (the SQLite
connection and the httpx ``AsyncClient`` for Ollama) and closes them at
shutdown. Routes pull them off ``app.state`` via the dependency
functions in ``app.dependencies``.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import queries
from app.connection import open_connection
from app.db import initialize_database
from app.ollama import create_client
from app.projects import migrate_legacy_workspace
from app.routes import router
# Importing app.routes (above) transitively imports app.tools.rag, which
# registers the `query_rag` tool. We re-import the refresh helper here
# explicitly so the startup hook below is obvious in the lifespan — the
# alternative (digging through `routes.refresh_query_rag_registration`)
# would hide a side effect inside an HTTP module.
from app.tools.builtins import refresh_file_tools_registration
from app.tools.rag import refresh_query_rag_registration

# Static assets live alongside `main.py` at the project root. Resolving
# the path relative to this file (rather than CWD) keeps the mount
# working regardless of where uvicorn is launched from.
_STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open shared resources at startup; close them on shutdown.

    Yields control to FastAPI between the two phases. Anything that
    raises before ``yield`` aborts startup; anything that raises after
    is logged but doesn't keep the process alive.
    """
    # initialize_database is idempotent; safe to call on every boot.
    initialize_database()

    # Sync query_rag's registry entry to the current rag_servers state.
    # With 0 servers the tool is removed entirely (model shouldn't call
    # a tool that can't succeed); with ≥1 server it's re-added/updated
    # with per-source descriptions so the model can pick intelligently.
    # Runs AFTER initialize_database so the rag_servers table is
    # guaranteed to exist before we SELECT from it.
    refresh_query_rag_registration()

    # Sync the file tools (read_file / write_file) to FILE_TOOL_ROOT:
    # present when a workspace dir is configured, removed otherwise so
    # the model never sees a tool with nowhere to operate.
    refresh_file_tools_registration()

    db = open_connection()
    ollama_client = create_client()
    app.state.db = db
    app.state.ollama_client = ollama_client

    # Phase 17: one-shot move of pre-projects FILE_TOOL_ROOT contents into
    # FILE_TOOL_ROOT/default/. Gated by an app_settings flag so re-runs are
    # no-ops. Runs after open_connection() so we have the shared DB to
    # read/write the flag through.
    migrate_legacy_workspace(db, queries)

    # Phase 26: reconcile stored host selections against the current registry.
    # A host removed from OLLAMA_EXTRA_HOSTS leaves stale active_host names;
    # clear them to NULL (primary) so host resolution's "unknown name is a bug"
    # invariant holds. Runs every boot — config can change between restarts.
    from app.hosts import HOSTS

    cleared = queries.clear_unknown_active_hosts(db, set(HOSTS))
    if cleared:
        logging.getLogger("uvicorn.error").info(
            "Reconciled %d stale active_host selection(s) to the primary host.",
            cleared,
        )

    try:
        yield
    finally:
        # Close in reverse order of opening. aclose() is async; the
        # SQLite Connection.close() is sync.
        await ollama_client.aclose()
        db.close()


app = FastAPI(lifespan=lifespan)
app.include_router(router)
# Serve the vendored Pico CSS + HTMX bundle under /static. Local-only,
# no CDN — fits the "no cloud calls" rule and means the app works
# offline.
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
