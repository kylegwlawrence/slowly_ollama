# Phases 12-16 — Non-goal features (high-level roadmap + phase 12 detail)

## Context

PLAN.md listed several features as deliberate v1 non-goals "to prevent
scope creep" but noted that "each can be revisited later." The app is
now stable (122/122 tests, 12 commits through phase 11) and the user
wants to start unlocking them, motivated by **learning** — biased
toward features that teach durable LLM-app concepts rather than just
table-stakes UX. The user runs the app locally and has several
**remote RAG servers** already up on a trusted network.

This document is the **high-level roadmap** for the 5 non-goal
features the user named. Phase 12 (tool-calling + RAG-via-tool) is
detailed enough to seed the next-session detailed implementation
plan; phases 13-16 are sketched briefly so we know what's coming
without over-designing for hypotheticals.

## Lessons carried in from phase 11

Pulled from `docs/retros/phase11-ui-improvements.md` — applied
proactively here, not just hoped for:

- **Smoke-test in a real browser**, not just curl. Tool-call UI is
  another browser-only failure surface (HTMX OOB swaps, SSE
  multi-event flows, collapsible cards).
- **HTMX attribute inheritance is a footgun.** No `hx-push-url`
  drift onto child elements; prefer server-side `HX-Push-Url`
  headers when possible.
- **httpx's default 5s timeout is wrong for LLM apps.** Any new
  client uses `timeout=httpx.Timeout(120.0, connect=5.0)`. RAG
  servers get a separate, shorter timeout (15s — they're
  retrieval, not generation).
- **Listen for "wait, why do we even need that?" simplifications**
  mid-build. The tinyllama-to-chat-model pivot deleted -117 lines.
  Tool-calling has at least one obvious simplification candidate:
  "do we even need separate tool messages, or can we shove the
  JSON into assistant messages?" — answer in the plan: yes, we
  need separate messages so the chat panel can render distinct
  cards.
- **Plan-mode review pass before implementing** caught 4 real bugs
  in the phase 11 plan. The detailed implementation plan for phase
  12 should go through the same review before any code is written.

## Decisions (from the design conversation)

| Decision | Value |
|---|---|
| Motivation | Learning — bias toward architecture that teaches |
| Phase shape | One feature per phase (12-16), no umbrella |
| First feature | Tool-calling (with RAG as the headline tool) |
| Runtime scope | Local app + remote services (RAG servers on trusted network) |
| Tool protocol | Roll our own with Python decorators (no MCP yet) |
| Safety model | Auto-execute read-only tools; confirm for write/exec (forward-looking — phase 12 has only read-only) |
| Tool UI | Collapsed cards between bubbles ("Called query_rag(...) — click to expand") |
| RAG transport | Multiple servers, managed via UI + DB table |
| RAG API shape | Custom (user fills in contract before implementation) |
| RAG auth | None — trusted local network |
| SSE flow | Server-side loop; single SSE stream with new `tool-call` / `tool-result` events |
| Tool persistence | New roles `tool_call` and `tool_result` in messages table |
| Schema migration | Drop the role CHECK; enforce in Python (`Role` Literal) |
| Iteration cap | 5 tool calls per assistant turn |
| Model filtering | Composer dropdown shows only tool-capable models |

## Roadmap (phases 12-16)

Sketched briefly. Each phase is decided in its own planning session
when we get to it — these are placeholders, not commitments.

- **Phase 12 — Tool-calling + RAG-via-tool** (detailed below).
- **Phase 13 — System prompts.** Per-chat textbox in chat header /
  composer; stored as a column on `conversations`; prepended as a
  `system` role message in the Ollama request. Small phase (~3-5
  commits).
- **Phase 14 — Generation parameters.** Temperature, top_p, num_ctx,
  num_predict, repeat_penalty. Per-chat (matches the "model is
  per-chat" pattern). Likely settings popover or expandable
  panel from the chat header. Small phase (~3-5 commits).
- **Phase 15 — More tools.** Now that the tool framework exists,
  add high-value tools. Likely candidates: URL fetch + clean text,
  web search (Tavily/Brave/DuckDuckGo), calculator, current_time
  beyond the baseline. Each is its own commit. Medium phase.
- **Phase 16 — Multi-agent (remote).** Largest, fuzziest. Decided
  later when we have tool-calling shipped and can see the natural
  shape (e.g., "agents are just LLM chats that can call other
  chats as tools"). Deliberately deferred until phase 15 reveals
  what's needed.

The roadmap is a Lego instruction sheet — pick up the next phase
when the prior one's behavior settles.

---

## Phase 12 — Tool-calling + RAG-via-tool

### Goal

Ship a server-side tool-calling loop with a Python-decorator tool
framework. First two tools:

1. `current_time` — trivial baseline, validates the loop without
   any external dependency.
2. `query_rag` — the headline feature. Takes a `source` enum
   (one of the configured RAG servers) and a `query` string;
   returns retrieved chunks. The user's existing remote RAG
   servers become first-class tools the chat model can invoke.

Tool calls + results are persisted as their own message rows.
The chat panel renders them as collapsed cards between user /
assistant bubbles. Only models that advertise tool capability
appear in the composer dropdown.

### Critical files

New:
- `app/tools/__init__.py` — `@tool` decorator + registry.
- `app/tools/builtins.py` — `current_time` tool.
- `app/tools/rag.py` — `query_rag` tool + RAG-server client.
- `app/rag_servers.py` — DB-backed CRUD for the RAG server table.
- `templates/_tool_call.html` — collapsed-card render for a
  `tool_call` message row.
- `templates/_tool_result.html` — collapsed-card render for a
  `tool_result` message row.
- `templates/_settings.html` — RAG server management UI.
- `tests/test_tools.py` — decorator + registry + tools.
- `tests/test_rag_servers.py` — RAG server CRUD queries.

Modified:
- `app/db.py` — drop role CHECK; add `rag_servers` table.
- `app/queries.py` — extend `Role` literal; add `rag_servers`
  CRUD functions.
- `app/ollama.py` — `stream_chat` accepts a `tools` kwarg
  (forwarded as `tools` in the `/api/chat` payload); a new
  `list_tool_capable_models()` helper that filters `/api/tags`
  by capability.
- `app/routes.py` — `_stream_assistant_reply` becomes a loop
  that yields `tool-call` and `tool-result` SSE events between
  the existing `token` events; `/models` route filters by tool
  support; new `/settings` route + the CRUD endpoints for the
  RAG server table.
- `templates/_assistant_placeholder.html` — `sse-swap` listens
  for `tool-call` and `tool-result` events.
- `templates/_chat_panel.html` — render `tool_call` and
  `tool_result` messages with their own templates inside the
  message list.
- `templates/index.html` — sidebar gets a settings link.
- `static/style.css` — new `.tool-card`, `.tool-card--call`,
  `.tool-card--result`, `.tool-card[open]` styles.

### Sub-phases (one commit each)

**12a — Schema + role expansion.**
- Drop the CHECK on `messages.role` via a table-recreate migration.
- Add `rag_servers` table: `id INTEGER PRIMARY KEY, name TEXT
  NOT NULL UNIQUE, url TEXT NOT NULL, request_template TEXT,
  response_jq TEXT, created_at TEXT, updated_at TEXT`.
  (`request_template` and `response_jq` are placeholders for the
  user-supplied API contract — filled in 12c.)
- Extend `Role` to include `tool_call` and `tool_result`.
- Migration guard via `PRAGMA table_info` so the new column adds
  cleanly to existing DBs (same pattern as phase 11d's
  `name_locked`).
- Tests: schema present after init; role literal accepts the new
  values.

**12b — Tool decorator + registry + baseline tool.**
- `@tool` decorator in `app/tools/__init__.py` that:
  - Infers JSON schema from function type hints + docstring.
    Use `typing.get_type_hints` and a small docstring parser
    (first line = description; `Args:` block = arg descriptions).
  - Registers the function in a module-level dict.
  - Annotates the function with a `is_read_only: bool` flag.
- `current_time(timezone: str = "UTC") -> str` baseline tool in
  `app/tools/builtins.py`. Returns ISO-formatted current time.
- `app/tools/__init__.py` exposes:
  - `TOOLS: dict[str, ToolSpec]` — registry.
  - `tool_specs_for_ollama() -> list[dict]` — returns Ollama's
    tools-payload format for inclusion in `/api/chat` body.
  - `async def run_tool(name: str, args: dict) -> str` —
    dispatch + execute + return string result.
- Tests: schema inference correctness, registry lookup,
  read-only flag, `current_time` runs.

**12c — RAG tool + server CRUD + settings UI.**
- The user fills in the **RAG API contract** in this section of
  the plan before implementation starts. We don't write the
  client until the request/response shape is locked.
- `app/rag_servers.py` — CRUD functions backed by the
  `rag_servers` table.
- `app/tools/rag.py` — `query_rag(source: str, query: str)`
  tool. `source` is dynamically constrained to the names of
  configured RAG servers (the decorator's enum support
  reads the server list at registration time, refreshed via
  a `reload_tools()` call after settings changes).
- Settings UI: `GET /settings` returns a fragment listing
  current servers + an add form. `POST /settings/servers`
  adds one. `DELETE /settings/servers/{id}` removes one.
  Sidebar gets a small "Settings" link in the footer area
  (the dark-mode toggle slot that 11c didn't ship — we can
  reuse the same flex `margin-top: auto` pattern from the
  plan file's 11c section).
- 15s timeout on the RAG HTTP client (retrieval is fast;
  don't inherit the 120s chat timeout).
- Tests: server CRUD round-trips; mocked RAG client returns
  expected shape; `query_rag` rejects unknown sources.

**12d — Server-side tool-calling loop in `_stream_assistant_reply`.**
- Refactor `_stream_assistant_reply` so the body is a loop:
  1. Call Ollama with `tools=tool_specs_for_ollama()`.
  2. If the response has `tool_calls`: for each call, persist a
     `tool_call` message row, emit a `tool-call` SSE event
     (rendered `_tool_call.html` fragment, OOB-prepended into
     `#messages`), run the tool, persist a `tool_result` row,
     emit a `tool-result` SSE event. Loop back to step 1 with
     the new history.
  3. If the response is plain text (no `tool_calls`): stream
     tokens as today via `token` events, persist the assistant
     message, emit `done`.
  4. Hard cap: 5 iterations. On exceeded, emit `error` event
     with a clear message and stop.
- The yielding order rule from phase 11 still applies:
  `title` event (if generated) goes BEFORE `done` because
  `done` removes the placeholder.
- Auto-title flow integrates: history fed to `generate_title`
  now includes tool messages too — the title is computed from
  the full conversation including tool side-trips.
- Tests: mocked Ollama returns tool_calls → loop fires once →
  tool ran → final text streams. Cap test: ollama keeps
  asking for tools, loop terminates after 5.

**12e — Tool UI cards + assistant placeholder updates.**
- `_tool_call.html`: a `<details>` element styled as a
  collapsed card with the tool name + args; opens to reveal
  the JSON args.
- `_tool_result.html`: same shape, opens to reveal the tool
  output (truncated to a max length with "show more").
- Assistant placeholder's `sse-swap` extended to include
  `tool-call,tool-result`. OOB swap targeting the placeholder
  with `beforebegin:#assistant-stream-{id}` so cards insert
  immediately above the streaming bubble (the detailed plan
  should confirm this against the existing OOB-swap patterns
  from phase 11d's title-row swap).
- CSS: `.tool-card` minimal — neutral surface, subtle border,
  monospace for args/output, `<summary>` styled as a clickable
  row with a chevron.
- Smoke test in browser: send a message that causes a tool
  call; verify cards appear, expand on click, persist after
  reload.

**12f — Model filtering by tool capability.**
- Ollama's `/api/show` (POST with `model` name) returns model
  capabilities. Filter for `tools` in the capabilities list.
- New `list_tool_capable_models()` helper in `app/ollama.py`.
  Cache results in-process for the duration of a request
  (don't re-query for every model on the dropdown).
- `/models` route uses this for the composer's model picker.
- For EXISTING chats whose model doesn't support tools:
  don't pass `tools=` to Ollama on those requests (Ollama
  returns 400 if you do). Capability check happens before
  each chat request; cached result keeps it cheap.
- Tests: mocked `/api/show` → only tool-capable names appear
  in the `<option>` list; non-tool models still chat fine
  (no `tools` in payload).

### Defer to detailed plan (the next planning session)

These need the user's input before implementation can start; the
detailed plan in the next session will lock them down:

1. **The RAG API contract.** User will fill in the exact request
   body shape, response body shape, and any optional fields.
   Example template the user will edit:
   ```
   REQUEST  POST {server.url}/...
            headers: { "Content-Type": "application/json" }
            body: { "query": "...", "...": "..." }
   RESPONSE 200 OK
            { "results": [ { "text": "...", "source": "..." }, ... ] }
   ```
2. **The decorator's enum-from-runtime-list mechanism.** Two
   options: (a) `source: Literal[*server_names]` rebuilt at each
   `tool_specs_for_ollama()` call, or (b) `source: str` with a
   `description` that lists valid names. (a) is more correct but
   harder; (b) is simpler and the model usually obeys
   description-stated constraints.
3. **`response_jq` field necessity.** If the user's RAG servers
   have varied response shapes, a per-server `jq`-style selector
   normalizes them to `[{text, source}]` before returning to the
   model. If they're all the same shape, drop the column.
4. **Settings UI surface.** Standalone `/settings` page vs.
   sidebar popover vs. modal. Decide in the detailed plan after
   we sketch the markup.
5. **Tool-card OOB-swap target.** The exact HTMX swap-style +
   selector for inserting tool cards above the streaming
   placeholder. Phase 11d used `hx-swap-oob="true"` (replace
   by id) and `afterbegin:#chats-list` (insert into); phase
   12e needs `beforebegin:#assistant-stream-{id}` or a wrapper
   div approach. Confirm before writing the template.
6. **Auto-title input filtering.** Whether to include
   `tool_call`/`tool_result` rows in the history fed to
   `generate_title`, or filter to just `user`/`assistant`
   (small models may get confused by unexpected roles).

### Verification (phase 12 overall)

1. `pytest` — full suite passes. Coverage doesn't regress meaningfully.
2. `uvicorn main:app --reload` and walk the path:
   - Open `/`. Confirm composer's model dropdown shows only
     tool-capable models.
   - Settings link works; can add and remove RAG servers.
   - Send a message that requires a RAG lookup; observe the
     tool-call card and tool-result card appear in the chat
     panel between user and assistant bubbles; assistant streams
     a final answer that cites the retrieved chunks.
   - Reload the chat URL; tool-call / tool-result cards still
     render.
   - Force a 6th tool call (mock RAG to always return "search
     more"); verify the cap kicks in with a clear error.
3. Browser visual confirmation: cards collapse/expand; styling
   reads as cohesive with phase 11's sage palette.

### What's NOT in phase 12

- System prompts (phase 13).
- Generation parameters (phase 14).
- Write/exec tools (no safety-confirm UI to design yet).
- MCP protocol support (decoration of our tools is the
  internal API; MCP can wrap later if needed).
- Image / vision inputs.
- Multi-agent — explicitly phase 16+.
- Settings page beyond RAG-server CRUD. (No theme toggle,
  no global model defaults — those land in later phases.)

---

## Existing reference code

The user has working RAG-client and multi-agent code in a separate
repo. We are **not porting it** — we re-implement here so the code
matches this project's style (Google docstrings, type hints,
inline comments, the existing httpx / SQLite / Jinja patterns).
The reference repo is consulted as a sanity check for the RAG
request/response shape and any subtle "we tried this and it
didn't work" lessons. The detailed plan should reference any
specific files the user wants the implementing agent to read.

## Notes for the next-session detailed implementation plan

When the next plan-mode session drafts the detailed phase 12
plan (the one the executing agent will follow), include:

- Verbatim Python code for the `@tool` decorator.
- Exact JSON schema generation rules for each Python type.
- Exact RAG request/response examples the user fills in here.
- Exact Jinja markup for `_tool_call.html` and
  `_tool_result.html` (`<details>` structure, attributes).
- Exact CSS for `.tool-card` (matching the sage tonal palette).
- Exact migration SQL for dropping the role CHECK (table
  recreate pattern — SQLite doesn't support `ALTER TABLE …
  ALTER CONSTRAINT`).
- Exact test specifications (which mock returns, which
  assertions) for each sub-phase.

The detailed plan goes through the same plan-mode review pass
phase 11's did — catch structural bugs in markdown before
they reach code.
