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
| What's the next phase / open work? | `docs/plans/phase12-tool-calling.md` (roadmap), `docs/plans/phase12-tool-calling-detail.md` (current phase exec spec) |
| What did we learn from prior phases? | `docs/retros/` (one file per phase, 6–11) |
| Accumulated conventions + gotchas | `docs/CONVENTIONS.md` |
| Test strategy + how to run | `tests/README.md` |
| End-user setup | `README.md` |

Treat `PLAN.md` as the build-order spine. Each phase has its own plan in
`docs/plans/` and its retro in `docs/retros/`. Plans and retros are version-
controlled artifacts, not workspace scratch.

## Current state

- **Phases 0–11 done.** v1 PLAN.md scope is complete (`PLAN.md` is frozen at
  Phase 10; phases 11–16 are off-PLAN.md extensions documented in their own
  plan files).
- **Phase 12 (tool-calling + RAG) in flight.** Sub-phases 12a → 12d shipped:
  schema migration, `@tool` decorator + registry, `current_time` baseline,
  `query_rag` tool, RAG server CRUD + `/settings` UI, server-side tool-calling
  loop in `_stream_assistant_reply`. Remaining: 12e (tool UI cards polish),
  12f (filter composer to tool-capable models).
- **122/122 tests passing** as of end of Phase 11; coverage 99% on `app/` + `main.py`.

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
  ollama.py              # httpx client + streaming /api/chat + /api/tags
  rag_servers.py         # RAG server CRUD queries (phase 12c)
  routes.py              # Every route returns HTML or SSE-of-HTML; no JSON
  tools/
    __init__.py          # @tool decorator, ToolSpec, registry, run_tool, tool_specs_for_ollama
    builtins.py          # current_time tool
    rag.py               # query_rag tool + RAG-server HTTP client
templates/               # Jinja fragments — every endpoint returns one of these
static/                  # Pico, HTMX, htmx-ext-sse, Material Symbols, style.css
tests/                   # pytest layered per source module + one integration journey
docs/
  plans/                 # PLAN.md + per-phase plans (phase8…phase12-detail)
  retros/                # Per-phase retrospectives (phase6 through phase11)
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
return assistant placeholder) + GET (open the SSE stream); the GET handler runs
a server-side loop that calls Ollama, executes any tool calls, persists each
turn as its own message row (`role` is one of `user` / `assistant` /
`tool_call` / `tool_result`), and emits events for the placeholder to consume.
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
- **`with conn:`** (native sqlite3 context manager) for transactions, never
  `with closing(conn)`. Existing queries.py helpers already follow this.
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
