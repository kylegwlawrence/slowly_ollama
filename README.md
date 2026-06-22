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
- **`FILE_TOOL_ROOT`** — (optional) absolute path to a directory the assistant may read, write, and search via the file tools. When unset, those tools (`read_file`, `write_file`, `list_directory`, `search_files`) are removed from the registry. Each project gets its own subdirectory underneath this root.
- **`OLLAMA_EXTRA_HOSTS`** — (optional) JSON array of additional Ollama machines the in-app host picker can route a chat to, e.g. `[{"name":"studio","url":"http://studio:11434","default_model":"llama3.1:70b"}]`. The primary `OLLAMA_HOST` is always the default; see `.env.example` for the full format.

### 4. Enable the file tools (optional)

A tool-capable model can read, write, and search files — but only inside a sandboxed directory you choose. To enable it:

```bash
mkdir -p ~/olliellama_workspace
```

Then add the path to your `.env`:

```
FILE_TOOL_ROOT=~/olliellama_workspace
```

Any path works — use an existing project folder, a notes directory, whatever you want the model to have access to. Each project is scoped to its own subdirectory under this root (browsable from the project's Files tab). Restart the app after changing this setting.

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

## Deploying split across two machines

You can run the **web tier** (FastAPI + uvicorn) on one machine — e.g. a small Linux box named `web-host` — and offload **inference** to a second machine (`<gpu-host>`) over a private network (VPN). The full runbook lives in `docs/plans/phase23-split-deployment.md`; the short version:

- **Ollama is network-native; SQLite is not.** Point `OLLAMA_HOST` at the other machine and inference moves there with no code change. The live SQLite database, by contrast, must stay **local to the web tier** (SQLite is an embedded file DB, not a client/server one). Durability instead comes from the built-in remote mirror (`REMOTE_DB_PATH` / `REMOTE_PATH`), which pushes a consistent copy of the DB + workspaces to the other machine on every change.
- **On `<gpu-host>`:** make Ollama listen beyond localhost (`Environment="OLLAMA_HOST=0.0.0.0:11434"` in its systemd override; default `127.0.0.1` is unreachable over the private network), and allow non-interactive SSH key auth from the web tier (`ssh <gpu-host> true` must succeed with no prompt — the backup uses `ssh -o BatchMode=yes`).
- **On the web tier:** `cp deploy/web-host.env.example .env` (Linux paths + remote Ollama + mirror), install `deploy/olliellama.service` as a systemd unit (binds uvicorn to `127.0.0.1:8000`), then front it with HTTPS reachable only on your private network (e.g. a reverse proxy or your VPN's built-in HTTPS serving). The UI lands at `https://web-host.<your-private-domain>`.

> **No authentication.** Keep the app reachable **only on your private network**, never exposed to the public internet. The private network is the security boundary.

After deploying, smoke-test in a real browser through the proxy — confirm chat tokens **stream** in rather than arriving all at once, and that new-chat titling / redirects work.

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
  config.py          # Env var accessors (OLLAMA_HOST, DB_PATH, FILE_TOOL_ROOT)
  connection.py      # SQLite connection helper
  db.py              # Schema initialization + idempotent migrations
  projects.py        # Per-project workspace helpers + legacy file migration
  rag_servers.py     # RAG server CRUD
  rag_health.py      # TTL-cached /health probe for RAG servers
  ollama.py          # httpx client for Ollama /api/chat, /api/tags, /api/show
  generation.py      # Background-task producer driving the SSE stream
  render.py          # Render-shaped views + tool-card OOB HTML helpers
  templates.py       # Jinja2 instance + markdown filter
  dependencies.py    # FastAPI dependency functions (db, ollama client)
  queries/           # SQL queries, dataclasses, Role literal
  routes/            # HTTP routes split by concern (chats, projects, settings)
  hosts/             # Ollama host registry for the per-chat host picker
  tools/             # Tool-calling system
    builtins.py      # Built-in tools (current_time, read_file, write_file, list_directory, search_files)
    rag.py           # RAG query tool (query_rag)
    github.py        # GitHub file fetching tool (fetch_github_file)
templates/           # Jinja2/HTMX HTML templates
static/              # Vendored CSS + JS (Pico, HTMX, Material Symbols)
tests/               # pytest test suite (+ conftest.py for shared fixtures)
docs/plans/          # Design and phase plans
docs/retros/         # Post-phase retrospectives
docs/code_reviews/   # Dated code reviews
```

---

## Features

- **Persistent conversations** — chats and messages stored in SQLite, survive restarts
- **Projects** — organize chats into named projects; each project has its own workspace directory, a default model and Ollama host, an optional system prompt (≤2000 chars, injected on each turn), and a read-only Files tab to browse workspace files
- **Per-chat model selection** — pick any tool-capable model from your local Ollama instance; click the model chip in the chat header to unload it from Ollama memory
- **Streaming responses** — assistant replies stream token-by-token via SSE
- **Reload-safe generation** — a page reload during a reply attaches a new consumer to the in-flight stream instead of cancelling it
- **Manual chat compaction** — summarize the older portion of a chat to shrink the Ollama prompt; originals are soft-archived and viewable through a disclosure in the summary bubble
- **Tool calling** — extensible tool system; a tool-capable model is offered the full registry every turn. Built-in tools: `current_time`, `fetch_github_file`, `query_rag` (RAG retrieval), and a workspace file suite (`read_file`, `write_file`, `list_directory`, `search_files`) gated on `FILE_TOOL_ROOT`
- **RAG support** — register external retrieval servers from `/settings`; `query_rag` searches every configured server, and the sidebar shows each server's read-only health state (green/grey/red), refreshed in the background on each send
- **Multi-machine Ollama hosts** — pick which Ollama machine runs a chat from the header host picker; configure extra machines via `OLLAMA_EXTRA_HOSTS` (the primary `OLLAMA_HOST` is always the default). Each chat remembers its model per machine
- **Fully local** — no telemetry, no cloud API calls, works offline
