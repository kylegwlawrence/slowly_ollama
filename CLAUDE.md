# CLAUDE.md

Guidance for Claude Code when working in this repo.

## Project

**slollillama** — a local-only chat app that talks to a locally-running
Ollama instance. No cloud calls; everything runs on-device. FastAPI + HTMX + Jinja
+ SQLite, served via uvicorn (`http://localhost:8000` for local dev; the
deployed service runs on `:8070`). See `README.md` for end-user setup.

## Where the source of truth lives

| Question | Read this |
|---|---|
| Accumulated conventions + gotchas | `docs/CONVENTIONS.md` |
| Detailed design notes + history | `docs/plans/` + `docs/retros/` |
| Recent code reviews + cleanup notes | `docs/code_reviews/` |
| Test strategy + how to run | `tests/README.md` |
| End-user setup | `README.md` |

Note: `docs/` is gitignored, so these are local reference artifacts, not
committed to version control.

## Current state

A single-user local chat app over Ollama. **925 tests passing**, 0 failing.
Current feature set:

- **Chat over multiple Ollama hosts.** A multi-host registry (`app/hosts/`,
  `chat_hosts` table) lets a chat target any configured Ollama host. The model
  dropdown is capability-filtered, with a generation-side fallback.
- **Streaming generation.** Server-side SSE of HTML fragments. Generation is
  resumable: the producer task outlives the HTTP request, so a reload replays
  the event log rather than dropping the turn.
- **Tool calling + RAG.** `@tool` decorator + registry; a server-side tool loop
  (capped per chat, default 5 iterations). Tool-capable models get the full
  registry every turn. Built-in tools: `current_time`, workspace file tools,
  `query_rag` (searches all configured RAG servers), `fetch_github_file`, and
  `web_search` (queries a self-hosted SearXNG; gated on `SEARXNG_URL`, so the
  app makes no *direct* cloud calls — SearXNG does the internet-facing work).
  Tool calls render as tool-card UI.
- **Projects → chats.** Projects sit above chats, each with a per-project
  workspace subdir under `FILE_TOOL_ROOT`, a default model, and a
  ≤`SYSTEM_PROMPT_MAX_CHARS` (8000) system prompt injected on Normal turns
  (the same cap reusable agents share). URL spine: `/projects/{id}/chats/{id}`.
- **Reusable agents (personas).** An `agents` row = `name` + `system_prompt` +
  optional `default_model`. Global (not project-scoped), CRUD'd on `/settings`
  (mirrors RAG-server CRUD). A chat attaches one via `conversations.agent_id`
  (nullable FK, `ON DELETE SET NULL`): pick it in the **composer** before
  starting the chat (an `agent_id` select beside the model picker, defaulting
  to none — `create_project_chat_endpoint` attaches it before the first turn),
  or change it later via the per-chat **Agent** picker + OOB-swappable header
  chip (`PATCH /chats/{id}/agent`). On a turn the agent
  prompt **stacks before** the project prompt — order is `date → agent →
  project → tool-nudge` (identity, then situation, then tools); it doesn't
  replace the project prompt. `default_model` is informational in v1 (shown,
  not auto-applied on attach).
- **Manual compaction.** A Compact button summarizes old turns into a `summary`
  row; originals are soft-archived (`messages.archived_at`). Generation reads
  `list_active_messages`; `KEEP_RECENT = 2`.
- **Sidebar RAG reference.** A read-only "Sources" panel lists configured RAG
  servers with TTL-cached health state (green/grey/red, 60 s, `app/rag_health.py`),
  refreshed fire-and-forget on send. RAG server name/URL are editable inline.
- **Per-chat thinking toggle.** `conversations.think_mode` (`'default'`/`'off'`)
  → Ollama's `think` flag via `_resolve_think` (`off`→`False`, else `None`;
  never `True`, so it can't 400 a non-thinking model). A header **Think** chip
  and a composer Think select appear only for reasoning-capable models (probed
  via `/api/show`). `PATCH /chats/{id}/think-mode`.
- **Per-chat controls.** Temperature and tool-iteration cap are adjustable per
  chat (`PATCH /chats/{id}/...`). Clicking the model chip unloads the model from
  Ollama.
- **Remote backup/sync.** `app/backup.py` pushes the DB + workspaces to a remote
  mirror on send / generation-complete / successful `write_file`. Single-flight,
  debounced, fire-and-forget, offline-safe. WAL-consistent DB copy via the
  SQLite backup API (never the live `-wal`/`-shm`); daily server-side snapshot.
  Push-only. Gated on `REMOTE_DB_PATH` + `REMOTE_PATH` both set.
  - A chat-header **backup status chip** surfaces the push (spinner →
    green/grey/red = `ok`/`offline`/`failed`), via a self-stopping
    `/backup/status` poll; hidden when backups are off.
  - **Pull** (`POST /backup/pull`) restores DB + workspaces from the mirror
    (`copy_agent_workspace.py --all`): closes the DB, pulls, reopens, redirects
    to `/projects`. Confirm-gated; refused (409) mid-generation.
  - **Push** (`POST /backup/push`) fires a manual backup for state changed
    outside a chat turn (e.g. a hand-added workspace file); no confirm.

## Working rules (override Claude defaults where they conflict)

- **Keep it simple first.** Add complexity only when needed; don't pre-build
  for hypothetical features.
- **Small commits, always ask before committing** — even for trivial diffs.
- **Python style.** Google-style docstrings (`Args:` / `Returns:` / `Raises:`)
  on functions and classes. Type hints everywhere. Inline comments explain the
  *why*, not the *what*.
- **Plan before non-trivial work.** Materialize a plan in `docs/plans/`; do a
  plan-mode review pass before writing code. For handoff plans, include concrete
  code, exact diffs, and test specs.
- **Test your changes.** Use `pytest --cov` to find gaps before writing
  speculative tests. Run `pytest` (all green, no coverage regressions) before
  declaring work done.
- **Smoke-test UI changes in a real browser**, not just curl or pytest. The
  test client doesn't run JS, fire mutation observers, or evaluate CSS
  cascades — past misses include SSE-after-placeholder-removed,
  `hx-push-url` cascading to descendants, and a dark-mode blank page.
- **Audience.** The user is building their first full-stack app. Name
  tradeoffs, explain unfamiliar concepts, challenge assumptions rather than
  silently accepting them.

## Tech stack (locked)

- **Python 3.12** (`.venv/` at project root)
- **FastAPI** + **uvicorn[standard]** — backend + ASGI server
- **httpx** — HTTP client for Ollama + RAG servers
- **Jinja2** + **HTMX** + **htmx-ext-sse** — server-rendered fragments + SSE
  streaming, no JS framework, no build step
- **SQLite** — persistence (single shared connection on `app.state.db`)
- **Pico CSS classless** + hand-written `static/style.css`
- **Material Symbols Outlined** — vendored woff2 under `static/`
- **pytest** + **pytest-asyncio** + **pytest-cov** + **pytest-xdist** — mock-only
  Ollama; suite runs in parallel by default (`-n auto`, see `pyproject.toml`)

Versions pinned in `requirements.txt`; transitive deps intentionally unpinned.

## Repo layout

```
main.py                # FastAPI app + lifespan (shared DB conn + httpx client)
app/
  config.py            # .env-backed accessors
  connection.py        # SQLite opener (WAL, foreign_keys)
  db.py                # Schema init + idempotent migrations
  _time.py             # Time helpers
  format.py            # Formatting helpers
  hosts/               # Multi-host Ollama registry
  queries/             # All SQL; Role literal; dataclasses; helpers
    _models.py         # Message, Conversation, Project, Agent dataclasses + Role
    conversations.py   # Conversation CRUD (incl. set_conversation_agent)
    messages.py        # Message CRUD; list_active_messages; archive helpers
    projects.py        # Project CRUD + slugify_project_name
    agents.py          # Reusable-agent (persona) CRUD + get_agent_for_conversation
    chat_hosts.py      # Per-chat host/model selection queries
    settings.py        # app_settings key/value store
  dependencies.py      # `DB` / `OllamaClient` Annotated aliases
  ollama.py            # /api/chat (stream) + /api/tags + /api/show
                       #   + summarize_conversation (compaction)
  projects.py          # Per-project workspace helpers + legacy migration
  rag_servers.py       # RAG server CRUD
  rag_health.py        # TTL-cached /health probe; get_health_map
  templates.py         # Jinja2 instance + markdown filter
  routes/              # Thin HTTP layer — HTML or SSE-of-HTML
    _helpers.py        # Shared helpers: _host_overrides, _resolve_think, sidebar ctx
    chats.py           # Chat CRUD, send, stream, regenerate, compact, backup
    projects.py        # /projects/* and /projects/{id}/files routes
    settings.py        # /settings route
    files.py           # Workspace-browse helpers used by routes/projects.py
  generation.py        # SSE producer; host/think overrides
  render.py            # Render-shaped views + tool-card OOB helpers
  backup.py            # Remote backup/sync (push) + status
  copy_agent_workspace.py  # Standalone pull script (DB + workspaces)
  tools/               # @tool decorator + registry
    builtins.py        # current_time + workspace file tools
    rag.py             # query_rag tool
    github.py          # fetch_github_file tool
templates/             # Jinja fragments
static/                # Pico, HTMX, htmx-ext-sse, Material Symbols, style.css
tests/                 # Per-module unit tests + integration journeys
docs/
  plans/               # Design notes + per-phase plans
  retros/              # Per-phase retrospectives
  code_reviews/        # Dated cleanup reviews
  CONVENTIONS.md       # Distilled lessons — conventions, gotchas, patterns
```

## Environment

`source .venv/bin/activate` → `pip install -r requirements.txt` →
`cp .env.example .env` (defaults work if Ollama is on `:11434`) →
`uvicorn main:app --reload` for local dev. Tests: `pytest` (hermetic, no
real Ollama; runs parallel via xdist — add `-n0` for a single-test run where
worker spin-up costs more than it saves, and temp files go to `/dev/shm` when
available, see `tests/conftest.py`). Coverage: `pytest --cov=app --cov=main
--cov-report=term-missing` (combines correctly across xdist workers).
DB lives at `~/Library/Application Support/ollama_slowly/chats.db` by default
(created on first run); configurable via `DB_PATH` in `.env`.

**Deployment.** This box runs the app as a systemd **system** service,
`slollillama.service` (`/etc/systemd/system/slollillama.service`, enabled), via
`.venv/bin/uvicorn main:app --host <host-ip> --port 8070`. Manage it with
`sudo systemctl {restart,stop,status} slollillama.service` and read logs with
`journalctl -u slollillama.service`. **`.env` changes require a service restart**
— `config.load_dotenv()` runs once at process start and does not override
already-set env vars, and `--host`-style reloads don't apply to the service.

## Architecture in one paragraph

`main.py` lifespan opens one SQLite connection + one `httpx.AsyncClient` on
`app.state`; routes get them via the `DB` / `OllamaClient` aliases. Every
endpoint returns an HTML fragment (HTMX swaps it in) or an SSE stream of named
events (`token` / `tool-call` / `tool-result` / `title` / `done` / `error`)
carrying HTML payloads. Chat-send is split into POST (save user message, spawn
`asyncio.Task` via `start_generation`, return assistant placeholder) + GET
(attach as consumer via `consume_generation`). `start_generation` spawns the
producer `_run_generation`; the route passes the chat's selected host/model,
system prompt, and `think` flag as overrides via `_host_overrides` /
`_resolve_think`. The producer task is owned by the module-level
`live_generations` dict, NOT the HTTP request — a reload cancels the consumer
but the producer keeps running, so the next consumer replays the event log from
index 0. Each turn persists its own message row (`role` ∈ `user` / `assistant`
/ `tool_call` / `tool_result` / `summary`); tool execution caps per chat
(default 5 iterations).

## Key gotchas (one-liners; deep dives in `docs/CONVENTIONS.md`)

- **httpx default 5s timeout is wrong for local LLMs.** Use 300s for chat,
  30s for RAG retrieval. Cold model loads take 10–30s.
- **HTMX attribute inheritance.** `hx-push-url` and most `hx-*` attrs cascade
  to descendants. Prefer server-side `HX-Push-Url` / `HX-Location` response
  headers when the URL update isn't tied to one element.
- **SSE event order matters.** Anything sent after `done` is dropped because
  htmx-ext-sse closes the EventSource when its placeholder is removed. Send
  `title` / `tool-*` events BEFORE `done`.
- **Pico classless fights us systematically** on form elements. When your rule
  doesn't apply, grep the vendored CSS for the selector.
- **`with conn:`** for transactions on the SHARED `app.state.db` connection.
  For PRIVATE one-shot connections via `open_connection()` (e.g. inside a
  tool), use `with closing(open_connection()) as conn:` — `__exit__`
  commits/rolls back but does NOT close. Without `closing`, the handle leaks.
- **Tests pin contracts (`data-*` / `hx-*` attrs), not implementations** (DOM
  tree shape). Substring assertions are surprisingly robust.
- **`think: true` 400s on a non-thinking model.** Ollama rejects `think: true`
  without the capability; `think: false` is safe anywhere. `_resolve_think`
  never sends `True` — it sends `False` or omits the flag.
- **`projects.default_agent` is a MISNOMER — it stores a host, not an agent.**
  Left over from the pre–Phase-23 "agent registry" → "host registry" rename;
  `_helpers.py` reads `project.default_agent` as the composer's initial host.
  The Phase-29 reusable-agent feature deliberately does NOT touch this column
  (it added its own `agents` table + `conversations.agent_id`). Rename it to
  `default_host` (copy `_ensure_conversations_active_host_column`'s in-place
  `RENAME COLUMN` pattern) before wiring a project-default *agent*.
- **Agent-prompt stacking order is load-bearing.** `_run_generation` builds the
  system prompt as `date → agent → project → tool-nudge`; a no-agent chat is
  byte-identical to before Phase 29. Tests assert the ordering
  (`.index(agent) < .index(project)`), so don't reorder the `parts` list.
