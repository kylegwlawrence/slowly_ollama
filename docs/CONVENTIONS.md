# Conventions and lessons

Distilled from `docs/retros/phase6-fastapi-routers.md` through
`docs/retros/phase11-ui-improvements.md`, plus the phase 12 plans. Organized
by layer. Each entry has a one-line rule + the *why* behind it. When a rule
has been violated and we learned the cost, the cost is named.

---

## Database, schemas, queries

- **Use `with conn:` for transactions** (native sqlite3 context manager)
  on the SHARED connection from `app.state.db`. Never
  `with closing(shared_conn)` — the shared connection is opened once at
  startup in `main.py`'s lifespan; `closing()` would close it after one
  query and break every subsequent call.
- **Use `with closing(open_connection()) as conn:` for private one-shot
  connections.** `sqlite3.Connection.__exit__` only commits/rolls back;
  it does NOT close. Without `closing`, the handle leaks until GC —
  that's the source of the "unclosed SQLite connection" warnings.
  See `app/tools/rag.py` for the call sites; tools open private
  connections today because they can't see the shared one (a future
  `RunContext` could change that — see
  `docs/code_reviews/2026-05-20-backend-pre-phase-13.md` item 7).
- **Schema CHECKs in SQL are a one-way street.** SQLite has no
  `ALTER TABLE ... DROP CONSTRAINT`. If we add a CHECK and later need to
  relax it (as in 12a, expanding `role` from `('user','assistant')` to
  include `'tool_call' / 'tool_result'`), the migration is a full table
  recreate. Enforce validity in Python (`typing.Literal`) when the set of
  legal values is likely to grow.
- **Idempotent migrations only.** Every migration in `app/db.py` runs on
  every boot. `PRAGMA table_info(...)` to detect "already migrated", or
  guard on `sqlite_master.sql LIKE '%CHECK%'`. Re-running must no-op
  cleanly — fresh DBs and old DBs both pass through `initialize_database()`.
- **Multi-query atomicity isn't there yet.** Each `queries.py` helper wraps
  its own `with conn:`. If a route ever needs an atomic
  "create-conversation-and-append-first-message" operation, the inner
  context managers commit too early. Refactor to caller-managed
  transactions at that point — not before.
- **UPDATE-with-WHERE for race-safety on per-row locks.** `name_locked = 0`
  in the WHERE clause of an auto-rename UPDATE guarantees a manual rename
  in another tab can't be clobbered. Cheaper than application-level locks
  and survives the single-shared-connection model.
- **Cascade deletes are tested in Phase 2.** Deleting a conversation drops
  its messages via `ON DELETE CASCADE`. If you add a new table that
  references `conversations` or `messages`, add the cascade and add a test
  that the cascade actually fires (the schema doesn't enforce
  `PRAGMA foreign_keys = ON` per-connection automatically).

---

## FastAPI routes

- **Every route returns HTML or SSE-of-HTML.** No JSON. The Phase 6 → 7
  pivot to HTML-only was cheap because the storage and Ollama-client
  layers were disciplined about boundaries. Don't reintroduce JSON
  responses unless a non-HTMX consumer arrives.
- **No `/api` prefix.** Every consumer is HTMX; there's no separate JSON
  API to keep namespaced.
- **Dependency injection via `Annotated` aliases.** `DB` and
  `OllamaClient` in `app/dependencies.py` are
  `Annotated[..., Depends(get_db)]`. Route signatures stay short; the
  plumbing is concentrated in one file.
- **HTTP error mapping (consistent across all routes):**
  - `OllamaUnavailable` → 503
  - `OllamaProtocolError` → 502
  - `LookupError` (unknown id) → 404
  - "no assistant message to regenerate" → 400
  - Mid-stream failures emit `event: error` (headers already sent)
- **Branch on `HX-Request` for reload-safe URLs.** `GET /chats/{id}`
  returns the chat-panel fragment for HTMX, the full index for a direct
  browser hit (reload / bookmark / back-forward). `hx-push-url="true"`
  on sidebar links keeps URL + DOM in sync.
- **Server-side `HX-Location` / `HX-Push-Url` headers over client-side
  `window.location`.** Less code, no detached-handler timing issues, and
  the server has Referer + DB context to make the right decision (e.g.,
  "redirect to `/` only when the user is viewing the chat they just
  deleted").
- **Persist assistant text AFTER the stream completes.** If the client
  disconnects mid-stream, the partial response is discarded. Documented
  tradeoff in `app/routes.py`. Don't try to persist incrementally without
  also designing a "draft" UX.
- **The tool-calling loop has a 5-iteration cap.** Defense against a
  model that keeps asking for tools indefinitely. Emit `event: error`
  with a clear message when the cap is hit.

---

## HTMX — patterns and gotchas

- **HTMX attribute inheritance is a footgun.** Most `hx-*` attributes
  cascade to descendants. Phase 11 shipped a bug where `hx-push-url="true"`
  on a form leaked onto a child `<select hx-get="/models" hx-trigger="load">`,
  pushing `/models?model=` into the address bar on page load. When in
  doubt, prefer server-side response headers (`HX-Push-Url`, `HX-Location`)
  over inherited client-side attributes.
- **OOB swap + `HX-Push-Url` for "create then navigate to" flows.** One
  response, two DOM regions: the response body is the new chat panel for
  `#main`, plus an OOB-swapped sidebar row, plus an `HX-Push-Url` header
  to update the URL. No follow-up GET needed.
- **Non-outerHTML OOB swaps unwrap their root.** Anything other than
  `hx-swap-oob="true"` / `"outerHTML"` / `"outerHTML:#id"` (so:
  `afterbegin:…`, `beforeend:…`, `beforebegin:…`, `afterend:…`,
  `innerHTML:…`, `delete`) inserts the OOB element's CHILDREN into the
  target — the OOB element itself is discarded. Put the `hx-swap-oob`
  attribute on a throwaway wrapper, not on the styled element you
  want preserved. Bug surfaced in the new-chat sidebar row: a top-
  level `<li class="chat-item" hx-swap-oob="afterbegin:#chats-list">`
  silently lost its `<li>` wrapper, leaving the row unstyled until
  reload. Fix: `<ul hx-swap-oob="…"><li class="chat-item">…</li></ul>`.
- **Two-step POST-then-GET for SSE streaming.** `htmx-ext-sse` opens
  EventSource connections only via GET. So POST `/chats/{id}/messages`
  persists the user message and returns an SSE placeholder element; the
  placeholder's `sse-connect` triggers GET `/chats/{id}/stream` which
  drives the actual SSE. The seam between the two is conversation state
  in the DB — the stream reads the latest user message rather than
  taking it as a parameter.
- **`hx-on:keydown` for one-line keyboard handlers.** Phase 9's
  Enter-to-send / Shift+Enter-newline handler is one line on the
  textarea. `requestSubmit()` triggers the HTMX-intercepted submit
  path identically to a click. Guard with `!event.isComposing` to
  avoid IME picker conflicts (Japanese / Chinese / Korean).
- **Inline JS is acceptable when HTMX can't express it.** Auto-scroll on
  panel load is a tiny inline `<script>` in `_chat_panel.html` — HTMX's
  swap events don't fire when the panel itself IS the swap target.
  Document at the point of use; don't graduate to a separate `.js`
  file until there's a second use case.
- **Test what's rendered, not what the framework does.** Route tests
  assert on the HTML coming back through `TestClient` — not on the
  template name or the dependency-injection wiring. That's the layer
  where bugs actually manifest.

---

## SSE — server-sent events

- **Named events on the wire.** `token`, `tool-call`, `tool-result`,
  `title`, `done`, `error`. Each event carries an HTML fragment as its
  payload (the choice from Phase 7 — HTMX consumes HTML natively, not
  JSON). Newlines inside fragments are escaped per the SSE spec.
- **Event order MATTERS.** `htmx-ext-sse` installs a mutation observer
  on the placeholder; when the placeholder is removed (e.g., by the
  OOB swap in the `done` event), the EventSource closes. Anything sent
  AFTER `done` is dropped. Phase 11d shipped this bug — auto-titles
  fired after `done`, never reached the client. Fix: yield title /
  tool-* events BEFORE the closing event.
- **The placeholder retains `message--streaming` until `done` fires.**
  Costs ~1–2s of "settling" pause while the title event lands, but
  prevents a second send from interleaving with tool calls or title
  generation.
- **SSE testing has a blind spot.** `TestClient` reads the entire
  response body — it doesn't simulate element-removal-triggered
  EventSource close. Pytest passes don't prove SSE ordering. Smoke-test
  SSE flows in a real browser (or with Playwright headless) before
  declaring them done.

---

## CSS, Pico classless, Material Symbols

- **Pico classless fights us systematically** on form elements:
  - `button[type=submit] { width: 100% }` — fights inline edit-form
    layouts; override with `flex: 0 0 auto; width: auto` on the form
    children.
  - `input { background, color }` flips under `prefers-color-scheme:
    dark`; explicit `background: var(--bg); color: var(--text-primary)`
    pins it.
  - Form-element font and line-height ride on Pico's tokens; bumping the
    project's `--font-size-base` may not propagate.
- **Diagnostic shortcut: curl + grep into vendored Pico.** When your CSS
  looks right and the visible behavior is wrong, the answer is usually
  Pico winning on a property you didn't override. Run
  `curl /static/pico.classless.min.css | tr '}' '\n' | grep <selector>`
  to see every Pico rule for a selector in seconds.
- **`display: inline-block` swallows flex properties.** `align-items`
  and `gap` only apply to `flex` / `grid` containers. If you set both
  in different rule blocks (one for "show this", one for "lay it
  out"), the inline-block wins on display and the flex props no-op.
  Tests pin selectors, not display values — this bug rides through
  green tests. Caught by code review only.
- **`field-sizing: content` is Chrome-only** (auto-grow textareas).
  Implement the JS fallback in Safari / Firefox; the plan flagged it
  in Phase 8 and skipping it cost a follow-up commit.
- **`:has()`, `:focus-within`, `:empty::before` are powerful.** Three
  interactive features (kebab popup, typing dots, bubble grouping)
  shipped without JS. Worth trying these primitives before reaching
  for a script. Modern-browser support is fine for a local app; the
  user controls their browser.
- **Material Symbols variable woff2 is 318KB**, larger than typical
  for an icon set. Acceptable for a vendored local app; subset to
  the ~8 glyphs we use (`edit`, `delete`, `refresh`, `send`,
  `more_vert`, `check`, `close`, `chat`) if edge bytes ever matter.
- **Two inline CSS rules stay in `base.html`** — the streaming-disable
  on the send button (`pointer-events: none`) and the regenerate-button
  visibility. Tests substring-match those rules; moving them to
  `style.css` breaks tests.
- **Tests should pin contracts, not implementations.** `data-chat-id`,
  `hx-delete`, `aria-current` are contracts. DOM tree shape (e.g.
  "first child of `<li>`") is implementation. Phase 8's kebab refactor
  moved buttons three levels deeper in the DOM without touching one
  test, because the tests pinned attributes, not positions.

---

## Tests

- **Mock-only Ollama** via `httpx.MockTransport`. Every test fully
  scripts the responses it wants. No real Ollama is contacted. This
  is a deliberate choice, documented in `tests/README.md`, not an
  accident. Trade-off: an Ollama API change wouldn't be caught by
  the suite alone — only by running the app for real.
- **Per-layer unit tests + one integration journey.** Phase 10's
  `tests/test_integration.py` walks the full path
  (`create → list → load → send → stream → regenerate → rename → delete`)
  through `TestClient`. Per-route tests catch "this endpoint is
  broken"; the journey catches "two correct endpoints don't compose."
- **Snapshot/restore `dependency_overrides`** in test fixtures, never
  `.clear()`. `.clear()` wipes overrides added by other fixtures
  (caught in Phase 6 follow-up commit).
- **HTML substring assertions are surprisingly robust.** Tests use
  `'data-chat-id="42"' in response.text` and similar. The templates
  carry stable `data-*` attributes precisely so tests have something
  to match. Resist the urge to use a real HTML parser — substrings
  are faster, simpler, and good enough for HTMX wiring.
- **Run coverage before "rounding out tests."** Spending an hour
  writing speculative tests is much less efficient than five minutes
  of `pytest --cov --cov-report=term-missing`. Phase 10 added five
  tests for five named missing lines in ten minutes.
- **Coverage ceiling is 99%, not 100%.** `get_ollama_client`'s body
  is structurally unreachable in tests (every test overrides it via
  `app.dependency_overrides`). Don't add `# pragma: no cover` — a
  future refactor that removes the override pattern would silently
  swallow real coverage loss. Document the ceiling instead.
- **Scripted-by-call-count mocks for stateful flows.** The
  integration test's `regenerate` step needs the mock to return
  different content on the 2nd `/api/chat` call than the 1st. A
  static-response mock can't catch the "regenerate actually replaces"
  contract; a call-counting mock can.
- **Round-trip tests as regression catchers even when the bug
  lives elsewhere.** Phase 9 added a rename-round-trip test
  (`GET /edit → PATCH`) after fixing a CSS bug. The test doesn't
  catch the CSS, but it pins the HTTP contract that the CSS bug
  was visible against. Worth having.
- **The DB warning spam from one test is benign.** Phase 10's
  `test_base_css_hides_regenerate_except_on_last_assistant` emits
  ~91 "unclosed SQLite connection" warnings. Tests still pass.
  Not load-bearing enough to chase.

---

## Tool calling (Phase 12)

- **Tool framework lives at `app/tools/`.** `__init__.py` owns the
  `@tool` decorator, `ToolSpec` dataclass, the `TOOLS` registry,
  `tool_specs_for_ollama()` (formats for `/api/chat` payload), and
  `run_tool()` (dispatch + execute + return a `ToolResult`).
- **`run_tool` always returns a `ToolResult`** (phase 12h). A tool
  may return either a plain string or a `ToolResult(text, sources)`;
  strings get wrapped at the boundary so the generation loop only
  handles one shape. The model itself only ever sees `.text` —
  sources are a UI-only concern, surfaced in the expandable tool
  row in the chat panel.
- **`tool_result.content` is a JSON envelope** (phase 12h):
  `{"text": "...", "sources": [{"title": "...", "section": "..."}]}`.
  Produced by `encode_tool_result(result)` at write time and read
  back via `decode_tool_result(content)` — both live in `app/tools/`.
  Legacy pre-12h rows are plain text; `decode_tool_result` round-trips
  them cleanly via its fallback so old conversations still render
  (as plain non-expandable rows). The generation loop's
  `_build_history_payload` decodes the envelope before sending to
  Ollama so the model never sees the JSON wrapper.
- **Side-effecting registration imports.** `app/routes.py` and
  `main.py` import `app.tools.builtins` and `app.tools.rag` for the
  side effect of `@tool`-decorating their functions. Without these
  imports, `TOOLS["current_time"]` and `TOOLS["query_rag"]` don't
  exist at runtime even though tests pass (tests import the
  modules themselves). The imports are aliased and `# noqa: F401`-d
  with comments explaining the side effect.
- **`Role` literal expansion is the source of truth.** SQLite's CHECK
  was dropped in 12a; `typing.Literal["user","assistant","tool_call","tool_result"]`
  in `app/queries.py` is the validator. Add new roles there first.
- **Tool calls and results persist as their own rows** in the
  `messages` table. At render time `app.render.group_messages_for_render`
  folds each contiguous run of `tool_call`/`tool_result` rows into one
  `ToolBatchBlock`, which the chat panel renders as a single
  aggregated `<details>` card above the assistant message that used
  the tools — collapsed by default, expand for the per-row
  `searching <source>: "<query>"` list and live mm:ss timer.
  Reload-safe: live and historic paths share `_tool_card_shell.html`
  and the same `summary_text(count, done)` helper, so verb/plural/
  ellipsis stay coordinated across both.
- **Generation runs in a background asyncio.Task** (phase 12g). The
  LLM call lives in `app/generation.py`'s `_run_generation`, not in
  the SSE response handler. POST `/chats`, POST `/messages`, POST
  `/regenerate` all call `generation.start_generation(...)` which
  registers a `GenerationState` in the module-level
  `live_generations` dict and spawns the task. GET `/stream`
  becomes a thin consumer that calls `consume_generation(state)`
  (replay events + tail) when a state is present, else
  `consume_finished(db, conv_id)` (single done event from the
  persisted assistant row). A client disconnect cancels the
  consumer; the producer task keeps running and persists the
  final reply. A reloaded page (or a second tab) attaches as a
  fresh consumer that replays the event log from index 0 then
  tails new events.
- **`live_generations` retains DONE states** until the next
  generation for the same conv evicts them. This is what makes the
  slow-reload-after-completion case render correctly — the
  consumer can still replay the recently-finished event log
  instead of falling through to the lossy `consume_finished`
  path. Memory cost: one event log per conversation that has ever
  generated, bounded by the tool-iteration cap and max-token
  count per turn. Acceptable for the local single-user app.
- **Process-local non-DB state lives in `app/generation.py`'s
  `live_generations` dict.** First piece of cross-request
  in-memory state in this codebase. If any other phase needs
  similar state, it should follow this pattern (module-level
  dict, no `app.state` coupling, autouse fixture in tests to
  clear between cases).
- **`is_read_only` flag on every tool.** Phase 12 tools are all
  read-only (auto-execute). The flag is forward-looking — when
  write/exec tools land, the streaming loop will surface a
  confirmation card instead of auto-running.
- **RAG description is dynamic.** The `query_rag` tool's `source`
  parameter description gets refreshed at app startup and after
  every settings POST/DELETE so the model sees the current set of
  configured server names. The startup hook is in `main.py`'s
  lifespan, AFTER `initialize_database` runs.
- **RAG client errors are tool results, not user errors.** A
  network failure or 5xx from a RAG server returns a string like
  `"RAG source <name> unreachable"` as the tool's result. The model
  sees the error, can choose to try a different source or proceed
  without retrieval. Don't bubble RAG errors to the user.
- **30s timeout for RAG, 120s for chat.** Separate `httpx.Timeout`
  per client. RAG is retrieval (FTS5 + ANN over local SQLite) — fast.
  Chat can be cold-load slow (10–30s on first request to a 7B model).
- **The chat-stream timeout was tuned in Phase 11.** Any new
  long-lived httpx client created in this codebase should set
  `timeout=httpx.Timeout(120.0, connect=5.0)` (or the appropriate
  per-context value). The library default 5s read is calibrated
  for normal web traffic.

---

## Plans, retros, and process

- **Plans live in `docs/plans/`, retros in `docs/retros/`.** Workspace
  plan files vanish; repo files are searchable, reviewable, and
  version-controlled. Plan mode's default workspace path is overridden
  by making "materialize the plan in `docs/plans/`" the first
  execution step.
- **Detailed plans for handoff include concrete code, not prose.**
  `docs/plans/phase8-frontend-design.md` (1250 lines) and
  `phase12-tool-calling-detail.md` (2033 lines) are the shape that
  works. Implementation barely diverges when the plan has exact
  diffs, exact test specs, exact CSS.
- **Plan-mode review pass catches structural bugs cheaply.** Phase 11's
  pre-implementation review of its own plan file found 4 real bugs —
  fixing them as markdown edits is dramatically faster than catching
  them mid-implementation.
- **Question rounds front-load consequential decisions.** Phase 8 ran
  four rounds of `AskUserQuestion` (16 questions total) before
  writing any CSS. Zero mid-implementation reversals followed. SSE
  format (Phase 6/7), accent color (11a), and RAG response shape
  (12c) are the canonical "ask before you write" calls.
- **Listen for "wait, why do we even need that?" simplifications
  mid-build.** Phase 11d's tinyllama → chat-model pivot deleted 117
  lines of code, removed a whole UX surface, and improved title
  quality. The "obvious in hindsight" simplification is the one
  most worth catching.
- **Post-phase code review catches real bugs the tests can't.** Five
  phases in a row found CSS bugs, OOB-swap order bugs, and
  inline-script bugs in the review pass after the implementation
  was "done." The review is part of the phase, not optional.
- **One clarifying question can be worth ten minutes of static
  analysis.** Vague bug reports get visual-symptom clarifications
  first ("very narrow and left aligned" → flex layout failure → fix
  in three lines of CSS).
- **Phase boundaries are guidance, not walls.** Phase 6's
  `get_conversation` crossed into Phase 4's module; Phase 11d's
  `_ensure_name_locked_column` added a column to the Phase 2 schema.
  Backfilling into earlier phases is preferred over hacks at the
  new layer.

---

## Debugging shortcuts

- **CSS rule not applying?** `curl /static/pico.classless.min.css | tr '}' '\n' | grep <selector>` shows Pico's rule for any selector in seconds.
- **HTMX request looks wrong?** The browser DevTools Network tab shows the actual `hx-*` headers + form bodies sent. The pre-request inspector inside Chrome's HTMX extension also helps.
- **Tests pass but the browser is blank?** That's a JS/CSS-only failure. Open in Chrome, check console, watch the Network tab for non-200 responses, look for `<script>` errors. `TestClient` can't see any of this.
- **SSE seems to drop events?** Check the order of `yield`s in the route handler. Anything after the placeholder-removing event (`done` for the assistant stream) is silently dropped.
- **"unused import" — but the import has side effects?** Alias the import and add `# noqa: F401` with a comment explaining the side effect (see the tool-registration imports in `app/routes.py` and `main.py`).
- **Coverage report shows a line uncovered but you can't reach it from a test?** That's often the dependency-override pattern — confirm in `tests/README.md` that the line is expected to be unreachable, or refactor to make it testable.
