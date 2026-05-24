# CLAUDE.md

Guidance for Claude Code when working in this repo.

## Project

**olliellama** — a local-only chat app for Mac (M3) that talks to a locally-running
Ollama instance. No cloud calls; everything runs on-device. FastAPI + HTMX + Jinja
+ SQLite, served via uvicorn at `http://localhost:8000`. See `README.md` for
end-user setup and `docs/plans/PLAN.md` for the build-time roadmap.

## Where the source of truth lives

| Question | Read this |
|---|---|
| What are we building and why? | `docs/plans/PLAN.md` |
| What's the latest shipped phase? | `docs/plans/phase16-invokable-agents.md` + retro |
| What did we learn from prior phases? | `docs/retros/` (per-phase, 6 through 16) |
| How to write a phase retro? | `docs/retros/RETRO_INSTRUCTIONS.md` |
| Accumulated conventions + gotchas | `docs/CONVENTIONS.md` |
| Recent code reviews + cleanup notes | `docs/code_reviews/` |
| Test strategy + how to run | `tests/README.md` |
| End-user setup | `README.md` |

Treat `PLAN.md` as the build-order spine. Each phase has its own plan in
`docs/plans/` and its retro in `docs/retros/`. Plans and retros are version-
controlled artifacts, not workspace scratch.

## Current state

`PLAN.md` is frozen at Phase 10; phases 11+ are off-PLAN extensions, each
under its own `docs/plans/phase<N>-*.md` (+ retro). Phases 0–11 shipped v1.
Highlights since:

- **Phase 12 (tool-calling + RAG).** `@tool` decorator + registry, server-side
  tool-calling loop, tool-card UI, resumable generation, capability-filtered
  model dropdown with a generation-side fallback so chats on a now-non-capable
  model degrade to plain chat instead of 400ing.
- **Phases 13–14 REMOVED in Phase 16.** The automatic research → review →
  generation loop and its toggles/verdict tools/agentic SSE events/extra
  roles were all deleted. Retros remain as history; the code does not.
- **Phase 15 / 15b (per-chat chips).** `chat_tool_settings` /
  `chat_rag_settings` gate which tools + RAG servers a chat may use.
  `query_rag` is gated solely by per-server chips (≥1 enabled), not a tool chip.
- **Phase 16 (user-invoked agents).** Named agents (`app/agents/`) the user
  picks from the composer; "Normal" is default. Agent = model + prompt + tool
  allowlist + `think`, reusing `_run_generation` via `_agent_overrides`.
  Roster: **Research** (`granite4.1:8b`, `current_time` + `query_rag`) and
  **Content Generator** (file tools); both `think=False`. Per-chat
  `active_agent` persisted; agents hand off through shared history.
- **Per-project system prompt (post-16).** Optional ≤200-char prompt
  (`projects.system_prompt`) injected as `system` on Normal-chat turns in that
  project, combined with `SINGLE_AGENT_SYSTEM_PROMPT` when tools are sent.
  Agent turns intentionally ignore it — the agent's own prompt wins.

**605/605 tests passing**; coverage 97% on `app/` + `main.py`.

## Working rules (override Claude defaults where they conflict)

- **Keep it simple first.** Add complexity only when needed; don't pre-build
  for hypothetical features.
- **Small commits, always ask before committing** — even for trivial diffs.
- **Python style.** Google-style docstrings (`Args:` / `Returns:` / `Raises:`)
  on functions and classes. Type hints everywhere. Inline comments explain the
  *why*, not the *what*.
- **Plans live in `docs/plans/`**, retros in `docs/retros/` — searchable,
  reviewable, version-controlled. For handoff plans, include concrete code,
  exact diffs, and test specs (see `phase8-frontend-design.md`,
  `phase12-tool-calling-detail.md`). Plan-mode review pass before code.
- **Test after each phase.** Use `pytest --cov` to find gaps before writing
  speculative tests.
- **Smoke-test UI changes in a real browser**, not just curl or pytest. The
  test client doesn't run JS, fire mutation observers, or evaluate CSS
  cascades — past misses include SSE-after-placeholder-removed,
  `hx-push-url` cascading to descendants, and a dark-mode blank page.
- **Audience.** The user is building their first full-stack app. Name
  tradeoffs, explain unfamiliar concepts, challenge assumptions rather than
  silently accepting them.

## Tech stack (locked)

- **Python 3.13** (`.venv/` at project root)
- **FastAPI** + **uvicorn[standard]** — backend + ASGI server
- **httpx** — HTTP client for Ollama + RAG servers
- **Jinja2** + **HTMX** + **htmx-ext-sse** — server-rendered fragments + SSE
  streaming, no JS framework, no build step
- **SQLite** — persistence (single shared connection on `app.state.db`)
- **Pico CSS classless** + hand-written `static/style.css`
- **Material Symbols Outlined** — vendored woff2 under `static/`
- **markdown** — assistant message rendering
- **python-dotenv** — `.env` loading
- **pytest** + **pytest-asyncio** + **pytest-cov** — mock-only Ollama

Versions pinned in `requirements.txt`; transitive deps intentionally unpinned.

## Repo layout

```
main.py                # FastAPI app + lifespan (shared DB conn + httpx client)
app/
  config.py            # .env-backed accessors
  connection.py        # SQLite opener (WAL, foreign_keys)
  db.py                # Schema init + idempotent migrations
  queries.py           # All SQL; `Role` literal; chips + app_settings helpers
  dependencies.py      # `DB` / `OllamaClient` Annotated aliases
  ollama.py            # /api/chat (stream) + /api/tags + /api/show
  rag_servers.py       # RAG server CRUD
  rag_health.py        # /health probe for newly-added RAG servers
  templates.py         # Jinja2 instance + markdown filter
  routes.py            # Thin HTTP layer — HTML or SSE-of-HTML
  generation.py        # SSE producer; per-agent overrides
                       #   (model/prompt/tools/think)
  render.py            # Render-shaped views + tool-card OOB helpers
  agents/              # AgentSpec + AGENTS registry + prompts
  tools/               # @tool decorator + registry; builtins.py + rag.py
templates/             # Jinja fragments
static/                # Pico, HTMX, htmx-ext-sse, Material Symbols, style.css
tests/                 # Per-module unit tests + integration journeys
docs/
  plans/               # PLAN.md + per-phase plans
  retros/              # Per-phase retrospectives
  code_reviews/        # Dated cleanup reviews
  CONVENTIONS.md       # Distilled lessons — conventions, gotchas, patterns
```

## Environment

`source .venv/bin/activate` → `pip install -r requirements.txt` →
`cp .env.example .env` (defaults work if Ollama is on `:11434`) →
`uvicorn main:app --reload`. Tests: `pytest` (~2s, hermetic, no real Ollama).
Coverage: `pytest --cov=app --cov=main --cov-report=term-missing`. DB lives at
`~/Library/Application Support/ollama_slowly/chats.db` by default (created on
first run); configurable via `DB_PATH` in `.env`.

## Architecture in one paragraph

`main.py` lifespan opens one SQLite connection + one `httpx.AsyncClient` on
`app.state`; routes get them via the `DB` / `OllamaClient` aliases. Every
endpoint returns an HTML fragment (HTMX swaps it in) or an SSE stream of named
events (`token` / `tool-call` / `tool-result` / `title` / `done` / `error`)
carrying HTML payloads. Chat-send is split into POST (save user message, spawn
`asyncio.Task` via `start_generation`, return assistant placeholder) + GET
(attach as consumer via `consume_generation`). `start_generation` always
spawns the single-agent producer `_run_generation`; when a named agent is
active, the route passes the agent's model/prompt/tool-allowlist/`think` as
overrides via `_agent_overrides`. The producer task is owned by the module-
level `live_generations` dict, NOT the HTTP request — a reload cancels the
consumer but the producer keeps running, so the next consumer replays the
event log from index 0. Each turn persists its own message row
(`role` ∈ `user` / `assistant` / `tool_call` / `tool_result`); tool execution
caps at 5 iterations per turn.

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
- **Agent `think=True` 400s on a non-thinking model.** Ollama rejects
  `think: true` without the capability; `think: false` is safe anywhere.
  Set True only when the agent's model is thinking-capable.

## When making changes

- **Read the relevant retro first** if touching a covered area — each has a
  "Notes for future phases" section.
- **Skip ahead with intent.** If asked for work that belongs to a later phase,
  surface it and confirm before proceeding.
- **Materialize a plan in `docs/plans/`** before non-trivial work; plan-mode
  review pass before code.
- **Ask before committing** — always, even for trivial diffs.
- **Run `pytest` before declaring a phase done.** All green; no coverage regressions.
