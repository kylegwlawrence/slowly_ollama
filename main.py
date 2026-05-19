"""FastAPI application entry point for ollama_slowly.

Run with::

    uvicorn main:app

The lifespan opens shared resources once at startup (the SQLite
connection and the httpx ``AsyncClient`` for Ollama) and closes them at
shutdown. Routes pull them off ``app.state`` via the dependency
functions in ``app.dependencies``.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.connection import open_connection
from app.db import initialize_database
from app.ollama import create_client
from app.routes import router
# Importing app.routes (above) transitively imports app.tools.rag, which
# registers the `query_rag` tool. We re-import the refresh helper here
# explicitly so the startup hook below is obvious in the lifespan — the
# alternative (digging through `routes.refresh_query_rag_source_description`)
# would hide a side effect inside an HTTP module.
from app.tools.rag import refresh_query_rag_source_description

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

    # Phase 12d: prime the `query_rag` tool's `source` schema with the
    # currently-configured RAG server names. Without this hook the
    # description text stays at the static fallback baked into the
    # decorator at import time until the user POSTs/DELETEs a server —
    # so chat turns on a freshly-booted app with pre-existing servers
    # would advertise an empty source list to the model. Runs AFTER
    # initialize_database so the rag_servers table is guaranteed to
    # exist before we SELECT from it.
    refresh_query_rag_source_description()

    db = open_connection()
    ollama_client = create_client()
    app.state.db = db
    app.state.ollama_client = ollama_client

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
