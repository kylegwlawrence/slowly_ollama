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
| What's the next phase / open work? | `docs/plans/phase13-agentic-loop.md` (next phase exec spec) |
| What did we learn from prior phases? | `docs/retros/` (per-phase, 6 through 12) |
| Accumulated conventions + gotchas | `docs/CONVENTIONS.md` |
| Recent code reviews + cleanup notes | `docs/code_reviews/` |
| Test strategy + how to run | `tests/README.md` |
| End-user setup | `README.md` |

Treat `PLAN.md` as the build-order spine. Each phase has its own plan in
`docs/plans/` and its retro in `docs/retros/`. Plans and retros are version-
controlled artifacts, not workspace scratch.

## Current state

- **Phases 0–11 done.** v1 PLAN.md scope is complete (`PLAN.md` is frozen at
  Phase 10; phases 11–16 are off-PLAN.md extensions documented in their own
  plan files).
- **Phase 12 (tool-calling + RAG) complete.** Sub-phases 12a → 12h
  shipped: schema migration, `@tool` decorator + registry, `current_time`
  baseline, `query_rag` tool, RAG server CRUD + `/settings` UI, server-
  side tool-calling loop, tool-card UI, resumable assistant generation,
  (12f) composer dropdown filtered to tool-capable models via
  `/api/show` capabilities — with a generation-side belt-and-suspenders
  in `_run_generation` so chats pinned to a now-non-capable model
  degrade to plain chat instead of 400ing — and (12h) expandable
  per-row sources list on the tool card, with `tool_result.content`
  now a JSON envelope `{"text": ..., "sources": [...]}` produced by
  `encode_tool_result` (legacy plain-text rows decode via fallback).
- **Pre-phase-13 cleanup complete.** Extracted `app/templates.py`
  (retired four lazy `from app.routes import templates` imports in
  `generation.py`); fixed the connection leak in `app/tools/rag.py`
  via `contextlib.closing`; added orphan-tool_result-skipping to
  `_build_history_payload`; extracted producer-runtime helpers from
  `_run_generation` (`emit_ollama_error`, `maybe_persist_partial`,
  `signal_done`); moved tool-card OOB rendering into `app/render.py`
  as `render_tool_card_initial` / `render_tool_card_row_append` /
  `render_tool_card_row_freeze` / `render_done_card_oobs`; added
  `encode_tool_call` / `decode_tool_call` to mirror the tool_result
  envelope. Test infra: added `tests/conftest.py` consolidating
  module-state isolation; moved misplaced `_build_history_payload`
  tests into `test_generation.py`; hoisted function-body imports in
  `test_routes.py`. See `docs/code_reviews/2026-05-20-*.md` for the
  reviews that drove the cleanup.
- **309/309 tests passing**; coverage 98% on `app/` + `main.py`.

## Working rules (override Claude defaults where they conflict)

These come from `PLAN.md` and have been reinforced across every retro:

- **Keep it simple first.** Add complexity only when needed; don't pre-build
  for hypothetical features. The single highest-impact change in Phase 11 was
  a "wait, why do we even need that?" simplification (-117 lines).
- **Small commits, always ask before committing.** Never commit without
  explicit user approval, even for trivial changes. The repo *is* a git repo
  now (was not when CLAUDE.md was first written).
- **Python style.** Google-style docstrings (`Args:` / `Returns:` / `Raises:`)
  on functions and classes. Type hints everywhere. Inline comments explaining
  non-obvious code (the *why*, not the *what*).
- **Plans live in `docs/plans/`**, retros in `docs/retros/`. Workspace-only
  plan files vanish; repo plan files are searchable, reviewable, and
  version-controlled.
- **Detailed plans for handoff.** When a plan is meant for another agent (or
  future-you) to execute, include concrete code, exact diffs, and test
  specifications — not prose. See `docs/plans/phase8-frontend-design.md` and
  `phase12-tool-calling-detail.md` for the shape that worked.
- **Plan-mode review pass before implementing.** Catching structural bugs in
  markdown is dramatically cheaper than in code. Phase 11's pre-implementation
  review caught 4 real bugs.
- **Test after each phase.** Write tests for the phase's work and run them
  before moving on. Run `pytest --cov=app --cov=main --cov-report=term-missing`
  to find gaps before writing speculative tests.
- **Smoke-test UI changes in a real browser**, not just curl or pytest. The
  test client doesn't run JS, fire mutation observers, or evaluate CSS
  cascades. Phase 11 shipped 5 post-launch bugs because curl-only smoke tests
  missed browser-only failures (SSE-after-placeholder-removed,
  `hx-push-url` inheritance, dark-mode blank page).
- **Audience.** The user is building their first full-stack app. Frame
  explanations accordingly — name tradeoffs, explain unfamiliar concepts,
  and challenge assumptions rather than silently accepting them. Phase 0 set
  this tone and it's still load-bearing.

## Tech stack (locked)

- **Python 3.13** (`.venv/` at project root, `python3.13` in `.venv/pyvenv.cfg`)
- **FastAPI** + **uvicorn[standard]** — backend framework + ASGI server
- **httpx** — HTTP client for Ollama (and for RAG servers in Phase 12)
- **Jinja2** + **HTMX** + **htmx-ext-sse** — server-rendered HTML fragments + SSE streaming, no JS framework, no build step
- **SQLite** — persistence (single shared connection on `app.state.db`)
- **Pico CSS classless** + hand-written `static/style.css` — visual layer
- **Material Symbols Outlined** — vendored as woff2 under `static/`
- **markdown** — assistant message rendering
- **python-dotenv** — `.env` loading
- **pytest** + **pytest-asyncio** + **pytest-cov** — test suite (mock-only Ollama)

Versions are pinned in `requirements.txt`. Transitive deps are intentionally
unpinned for a simple local app (switch to a lockfile later if reproducibility
bites).

## Repo layout

```
main.py                  # FastAPI app + lifespan (shared DB conn + httpx client)
app/
  config.py              # .env-backed accessors (OLLAMA_HOST, DB_PATH)
  connection.py          # SQLite connection opener (WAL, foreign_keys)
  db.py                  # Schema init + idempotent migrations
  queries.py             # All SQL queries; `Role` literal enforces validity
  dependencies.py        # `DB` / `OllamaClient` Annotated[..., Depends(...)] aliases
  ollama.py              # httpx client + streaming /api/chat + /api/tags + /api/show
  rag_servers.py         # RAG server CRUD queries (phase 12c)
  rag_health.py          # /health probe for newly-added RAG servers (phase 12e)
  templates.py           # Jinja2 instance + markdown filter (shared by routes/generation/render)
  routes.py              # Thin HTTP layer — every route returns HTML or SSE-of-HTML
  generation.py          # Background-task producer for SSE streams (phase 12g) + shared helpers (emit_ollama_error, maybe_persist_partial, signal_done)
  render.py              # Render-shaped views + tool-card OOB HTML helpers
  tools/
    __init__.py          # @tool decorator, ToolSpec, registry, run_tool, tool_specs_for_ollama, encode/decode_tool_call, encode/decode_tool_result
    builtins.py          # current_time tool
    rag.py               # query_rag tool + RAG-server HTTP client
templates/               # Jinja fragments — every endpoint returns one of these
static/                  # Pico, HTMX, htmx-ext-sse, Material Symbols, style.css
tests/
  conftest.py            # Autouse module-state isolation (live_generations, capability cache)
  test_*.py              # Per-module unit tests + one end-to-end integration journey
docs/
  plans/                 # PLAN.md + per-phase plans (phase8 through phase13)
  retros/                # Per-phase retrospectives (phase6 through phase12)
  code_reviews/          # Dated cleanup reviews (e.g., pre-phase-13)
  CONVENTIONS.md         # Distilled lessons — conventions, gotchas, patterns
```

## Environment

- Activate the venv: `source .venv/bin/activate`
- Install deps: `pip install -r requirements.txt`
- Configure: `cp .env.example .env` (defaults work if Ollama is on `:11434`)
- Run the app: `uvicorn main:app --reload`
- Run tests: `pytest` (whole suite ~2s, hermetic, no real Ollama)
- Coverage: `pytest --cov=app --cov=main --cov-report=term-missing`

The SQLite DB lives at `~/Library/Application Support/ollama_slowly/chats.db`
by default; the directory is created on first run. Configurable via `DB_PATH`
in `.env`.

## Architecture in one paragraph

The lifespan in `main.py` opens one SQLite connection and one `httpx.AsyncClient`
and stores both on `app.state`. Routes get them via the `DB` / `OllamaClient`
`Annotated` aliases in `app/dependencies.py`. Every endpoint returns either an
HTML fragment (HTMX swaps it in) or an SSE stream of named events
(`token` / `tool-call` / `tool-result` / `title` / `done` / `error`) carrying
HTML payloads. The chat-send flow is split into POST (save user message,
start a background `asyncio.Task` running `_run_generation` in
`app/generation.py`, return assistant placeholder) + GET (attach as a
consumer via `consume_generation`). The producer task is owned by the
module-level `live_generations` dict, NOT the HTTP request — a page reload
cancels the consumer but the producer keeps running, so reloads attach as
fresh consumers that replay the event log from index 0. Each turn persists
its own message row (`role` is one of `user` / `assistant` / `tool_call` /
`tool_result`); the producer emits events for the placeholder to consume.
Tool execution caps at 5 iterations per assistant turn.

## Key gotchas (one-liners; deep dives in `docs/CONVENTIONS.md`)

- **httpx default 5s timeout is wrong for local LLMs.** Use 120s for chat,
  15s for RAG retrieval. Cold model loads take 10–30s.
- **HTMX attribute inheritance.** `hx-push-url` and most `hx-*` attributes
  cascade to descendants. Prefer server-side `HX-Push-Url` / `HX-Location`
  response headers when the URL update isn't tied to one specific element.
- **SSE event order matters.** Any event sent after `done` is dropped because
  htmx-ext-sse closes the EventSource when its placeholder element is removed.
  Send `title` / `tool-*` events BEFORE `done`.
- **Pico classless fights us systematically** on form elements (background,
  text color, button width, `prefers-color-scheme`). When your rule doesn't
  seem to apply, `curl /static/pico.classless.min.css | tr '}' '\n' | grep <selector>`.
- **`with conn:`** (native sqlite3 context manager) for transactions on
  the SHARED `app.state.db` connection. For PRIVATE one-shot connections
  opened via `open_connection()` (e.g. inside a tool), use
  `with closing(open_connection()) as conn:` — `Connection.__exit__`
  commits/rolls back but does NOT close. Without `closing`, the handle
  leaks until GC. See `docs/CONVENTIONS.md` and `app/tools/rag.py`.
- **Test fixtures must snapshot/restore `dependency_overrides`**, never `.clear()`
  — `.clear()` wipes overrides added by other fixtures.
- **Tests pin contracts (`data-*` attrs, `hx-*` attrs), not implementations**
  (DOM tree shape). Substring assertions are surprisingly robust.
- **Coverage's 99% ceiling is intentional.** `get_ollama_client`'s body is
  structurally unreachable in tests because every test overrides it via
  `app.dependency_overrides`. Don't chase 100%; document the ceiling instead
  (see `tests/README.md`).
- **Phase boundaries are guidance, not walls.** If a later phase reveals a
  gap in an earlier one, backfilling is preferred over hacks at the new layer.

## When making changes

- **Read the relevant retro first** if you're touching an area covered by
  one (`docs/retros/phase<N>-*.md`). Each retro has a "Notes for future
  phases" section that captures lessons that should still apply.
- **Skip ahead with intent.** If asked for work that belongs to a later phase,
  surface that and confirm before proceeding.
- **Materialize a plan in `docs/plans/`** before non-trivial work. Run
  through a plan-mode review pass before writing code.
- **Ask before committing.** Always. Even for trivial diffs.
- **Run `pytest` before declaring a phase done.** All tests should pass green;
  no regressions in coverage.
