"""FastAPI dependencies — getters for the shared resources on app.state.

The ``main.py`` lifespan opens the SQLite connection and the httpx client once
at startup and stows them on ``app.state``. Routes pull them out per request
via ``Depends`` or the ``DB`` / ``OllamaClient`` aliases below.
"""

import sqlite3
from typing import Annotated

import httpx
from fastapi import Depends, Request


def get_db(request: Request) -> sqlite3.Connection:
    """Return the shared SQLite connection from ``app.state``.

    Opened once by the lifespan and shared by all routes. Safe across
    FastAPI's threadpool because ``open_connection`` sets
    ``check_same_thread=False``.
    """
    return request.app.state.db


def get_ollama_client(request: Request) -> httpx.AsyncClient:
    """Return the shared httpx client for the Ollama server."""
    return request.app.state.ollama_client


# Annotated aliases keep route signatures short:
#   def endpoint(db: DB, client: OllamaClient): ...
DB = Annotated[sqlite3.Connection, Depends(get_db)]
OllamaClient = Annotated[httpx.AsyncClient, Depends(get_ollama_client)]
