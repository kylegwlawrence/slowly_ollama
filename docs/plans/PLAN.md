# Local Ollama chat app for Mac M3

## Goal
Build a local-only chat application for Mac (M3) that uses a locally-running Ollama instance as the inference engine. No cloud calls — all inference happens on-device.

## Working rules
- Keep things as simple as possible; add complexity only when actually needed
- Write clean, readable code
- Keep commits small; **always** ask before committing
- Python: Google-style docstrings, type hints everywhere
- Write inline comments explaining non-obvious code (skip comments that just restate well-named code)
- Plans live in `docs/plans/`
- Write tests for each phase and run them before moving on
- Audience: this is the user's first full-stack app — frame explanations accordingly, name tradeoffs, challenge assumptions rather than silently accepting them

## Scope (locked in after Phase 0 discussion)

### Functional requirements
- Chat window that **streams** responses from Ollama's `/api/chat` endpoint via Server-Sent Events
- Conversation history persists across app restarts (SQLite)
- Per-chat setting (the only one): **model selection**, populated dynamically from Ollama's `/api/tags`
- Per-conversation actions: **rename**, **delete**
- Per-message action: **regenerate the last assistant response** (replaces in place; no variant history kept)

### Non-goals (deliberately out of scope for v1)
Stating these up front to prevent scope creep. Each can be revisited later.

- System prompt
- Temperature, context size, or any other generation parameters
- Auto-generated chat titles
- Editing user messages after sending
- RAG / document upload
- Image input (vision models)
- Tool / function calling
- Search across past chats
- Exporting chats to file
- Multi-user / authentication
- Sync between machines

### App lifecycle
- Launch manually: run `uvicorn main:app`, then open `http://localhost:8000` in a browser
- Ollama must be started manually before launching the app — the app will **not** attempt to start it
- If Ollama is unreachable, surface a clear error in the UI

## Tech stack
- **Python 3.13** (`.venv/` already provisioned)
- **FastAPI** — backend framework
- **SQLite** — persistence
- **HTMX + Jinja templates** — frontend (served from FastAPI; no JS framework, no build step)
- **pydantic-settings** — typed config loaded from `.env`
- **Ollama** — local inference server (default `http://localhost:11434`, configurable via `.env`)

## Storage and config
- Database file: `~/Library/Application Support/ollama_slowly/chats.db`
  - The directory will be created on first run if it doesn't exist
- Config file: `.env` at the project root
  - `OLLAMA_HOST` (default `http://localhost:11434`)
  - Add other values here as needed

## Phases

### Phase 0 — discussion ✓ done
Requirements and tech stack clarified above. Scope reduced from the original draft: dropped global temperature / context size settings; deferred system prompt, message editing, and auto-titles to "non-goals for v1."

### Phase 1 — package requirements
- Determine and pin required libraries
  - Likely: `fastapi`, `uvicorn[standard]`, `httpx`, `jinja2`, `pydantic-settings`, `pytest`, `pytest-asyncio`
- Decide: `requirements.txt` vs `pyproject.toml` (discuss before choosing)

### Phase 2 — database
- Design SQLite schemas:
  - `conversations` (id, name, model, created_at, updated_at)
  - `messages` (id, conversation_id, role, content, created_at)
- Create the DB file at the Application Support path on first run

### Phase 3 — database connection
- Single shared connection pattern, suitable for use as a FastAPI dependency
- Consider enabling WAL mode for safer concurrent reads during streaming

### Phase 4 — models and queries
- Dataclasses for `Conversation` and `Message`
- Query functions built on the shared connection:
  - Create / list / rename / delete conversations
  - Append / list messages for a conversation
  - Replace the last assistant message (for regenerate)

### Phase 5 — Ollama client
- HTTP client (`httpx`) wrapping:
  - `GET /api/tags` — list installed models
  - `POST /api/chat` with `stream=true` — streaming chat completions
- Graceful handling when Ollama is unreachable

### Phase 6 — FastAPI routers
- HTTP endpoints the HTMX frontend will call
- Streaming endpoint (SSE) for chat responses
- Endpoints for conversation CRUD and model listing

### Phase 7 — frontend
- HTMX + Jinja layout: sidebar with conversation list, main chat panel
- Streaming responses appended to the chat panel via SSE (`htmx-ext-sse`)
- Model dropdown, rename/delete controls, regenerate button

### Phase 8 — full test suite
- Round out tests across all layers
- Decide on Ollama mocking strategy for tests (likely: mock httpx for unit tests; optional integration tests against a real Ollama instance)
