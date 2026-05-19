# olliellama

A local chat application for Mac that uses a locally-running [Ollama](https://ollama.com) instance as the inference engine. No cloud calls — all inference runs on your machine.

Built with FastAPI + HTMX + SQLite. Conversation history persists across restarts.

---

## Prerequisites

- **Python 3.13** — the virtualenv is pinned to 3.13.13
- **Ollama** running locally (default: `http://localhost:11434`)
- At least one model pulled in Ollama (e.g. `ollama pull llama3.1:8b`)

---

## Installation

### 1. Create and activate the virtualenv

```bash
python3.13 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Configure environment

Copy the example env file and edit it if needed:

```bash
cp .env.example .env
```

The defaults work out of the box if Ollama is running on its default port:

```
OLLAMA_HOST=http://localhost:11434
DB_PATH=~/Library/Application Support/ollama_slowly/chats.db
```

- **`OLLAMA_HOST`** — base URL of the local Ollama HTTP API. Change this if you run Ollama on a non-standard port or host.
- **`DB_PATH`** — path to the SQLite database file. The `~` expands to your home directory. The directory is created automatically on first run.

---

## Starting the app

Make sure Ollama is running, then:

```bash
source .venv/bin/activate
uvicorn main:app --reload
```

Open `http://localhost:8000` in your browser.

`--reload` restarts the server automatically when source files change — useful during development. Drop it in production.

---

## Running tests

```bash
source .venv/bin/activate
pytest
```

For a coverage report:

```bash
pytest --cov=app --cov-report=term-missing
```

Tests use an in-memory SQLite database and mock the Ollama client — no live Ollama instance required.

---

## Project structure

```
main.py              # FastAPI app entry point (lifespan, mounts)
app/
  config.py          # Env var accessors (OLLAMA_HOST, DB_PATH)
  connection.py      # SQLite connection helper
  db.py              # Schema initialization (CREATE TABLE IF NOT EXISTS)
  queries.py         # All SQL queries (chats, messages, settings)
  dependencies.py    # FastAPI dependency functions (db, ollama client)
  ollama.py          # httpx client for the Ollama /api/chat endpoint
  routes.py          # All HTTP routes (chat CRUD, streaming, settings)
  rag_servers.py     # RAG server CRUD queries
  tools/             # Tool-calling system
    builtins.py      # Built-in tools (current_time, etc.)
    rag.py           # RAG query tool
templates/           # Jinja2/HTMX HTML templates
static/              # Vendored CSS + JS (Pico, HTMX, Material Symbols)
tests/               # pytest test suite
docs/plans/          # Design and phase plans
docs/retros/         # Post-phase retrospectives
```

---

## Features

- **Persistent conversations** — chats and messages stored in SQLite, survive restarts
- **Per-chat model selection** — pick any model available in your local Ollama instance
- **Global settings** — temperature and context size configurable from the UI
- **Streaming responses** — assistant replies stream token-by-token via SSE
- **Tool calling** — extensible tool system; built-in `current_time` tool included
- **RAG support** — connect external retrieval servers and query them from chat
- **Fully local** — no telemetry, no cloud API calls, works offline
