"""FastAPI application entry point for ollama_slowly.

Run with::

    uvicorn main:app

The lifespan opens shared resources once at startup (the SQLite
connection and the httpx ``AsyncClient`` for Ollama) and closes them at
shutdown. Routes pull them off ``app.state`` via the dependency
functions in ``app.dependencies``.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.connection import open_connection
from app.db import initialize_database
from app.ollama import create_client
from app.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open shared resources at startup; close them on shutdown.

    Yields control to FastAPI between the two phases. Anything that
    raises before ``yield`` aborts startup; anything that raises after
    is logged but doesn't keep the process alive.
    """
    # initialize_database is idempotent; safe to call on every boot.
    initialize_database()

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
