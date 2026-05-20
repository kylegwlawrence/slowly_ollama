# Per-source descriptions for RAG tool calling

## Context

The chat model currently sees a single `query_rag` tool whose `source`
parameter is just an enum of configured server names (e.g.
`arxiv, medical, legal`). With no information about what's *in* each
source, the model can't intelligently pick — it guesses or asks the user.
This makes RAG-backed answers feel worse than they should, and gets
worse as users add more sources.

We're keeping the single-tool design (rejected: one-tool-per-source —
adds dynamic registration, name sanitization, and orphan-history-row
complexity for no real model-side gain). Instead, each `rag_servers`
row gets a user-supplied `description` string, which is folded into
the `source` parameter's description so the model sees a self-describing
picker:

```
Name of the RAG source to query. Available sources:
- arxiv: Papers from arxiv.org on CS/ML/physics
- medical: PubMed abstracts 2020-2024
- legal: U.S. Supreme Court opinions
```

**Sub-decisions locked in (per user):**
- **Soft-required**: `TEXT NOT NULL DEFAULT ''` in the schema (so legacy
  rows validate), `required` attribute on the form field (so new
  submissions must include one).
- **Migration**: existing rows default to `''`, rendered as
  `(no description)` in the picker.
- **Length cap**: 200 chars, silently truncated server-side. Tool
  descriptions ride along with every `/api/chat` request — we don't
  want a verbose user to bloat every chat turn.
- **No edit-in-place** for now. Delete + re-add, matching the existing
  behaviour for `name` and `url`.
- **Tool gating on empty config**: when zero RAG servers are configured,
  `query_rag` is removed from the tool registry entirely — not just
  given a degenerate `(none configured)` hint. A tool that can never
  successfully execute should not be visible to the model. See section
  4 for the implementation.

## Files to change

### 1. Schema + migration
**`app/db.py`**
- Add `description TEXT NOT NULL DEFAULT ''` to `_SCHEMA_SQL`'s
  `rag_servers` CREATE TABLE (lines 66–72).
- Add `_ensure_rag_servers_description_column(conn)` mirroring the
  existing `_ensure_name_locked_column` pattern (lines 86–106) —
  `PRAGMA table_info` check, then `ALTER TABLE rag_servers ADD COLUMN
  description TEXT NOT NULL DEFAULT ''` if missing.
- Call it from `initialize_database` alongside the other migrations
  (after `_migrate_messages_drop_role_check`, line 197).

### 2. CRUD layer
**`app/rag_servers.py`**
- Add `description: str` field to `RagServer` **at the end of the
  dataclass** (after `updated_at`). Appending is the only safe move
  without auditing every direct positional construction site; logical
  grouping costs us nothing because the dataclass is `frozen=True`
  and all production callers go through `_row_to_server` / kwargs.
- Update `_row_to_server` to populate `description=row["description"]`.
- Update `list_servers` SQL to include `description` in the SELECT.
- Update `create_server`:
  - Add `description: str = ""` parameter (default `""` so any test
    that omits it keeps passing without touch-ups).
  - Update INSERT column list, VALUES tuple, and RETURNING clause
    to include `description`.

### 3. Route handler
**`app/routes.py`** (`add_server_endpoint`, lines 174–222)
- Accept `description: Annotated[str, Form()] = ""`. The default keeps
  the route consistent with the "soft-required" decision: the form's
  `required` attribute is the user-facing constraint; programmatic
  POSTs that omit `description` get `""` rather than a 422. (Without
  the default, every test in `test_routes.py` and every curl example
  would have to include the field even when it's not the test's
  focus.)
- Compute `description_clean = description.strip()[:200]` (the 200-char
  silent cap; `maxlength="200"` on the textarea is a client-side hint
  only, so the truncation must live here as belt-and-suspenders).
- Pass `description=description_clean` to `create_server`.

### 4. Tool registration + spec generation
**`app/tools/rag.py`** (`refresh_query_rag_source_description`, lines 223–252)

**Design shift**: a RAG tool with no RAG servers configured isn't a
usable tool — the model would call it and get `(none configured)`
back. Instead of carrying a degenerate `(none configured)` hint, this
function now also gates *registration* of `query_rag`:

- **0 servers** → pop `query_rag` from `TOOLS` so the model never sees
  it.
- **≥1 server** → ensure `query_rag` is in `TOOLS` with a per-source
  description folded into the `source` param.

Concrete steps:

1. Replace `_list_source_names()` (lines 43–59) with `_list_sources()`
   returning `list[RagServer]`. Keep the `closing(open_connection())`
   rationale in the docstring.
2. Capture the decorator-built spec at module-import time so we can
   re-register after a pop:
   ```python
   @tool
   async def query_rag(...): ...

   # Snapshot for re-registration (see refresh_query_rag_registration).
   # @tool put the spec into TOOLS as a side effect; this just grabs
   # the reference. parameters_schema stays shared by design — the
   # refresh function mutates it in place to reflect the current
   # source list.
   _QUERY_RAG_SPEC = TOOLS["query_rag"]
   ```
3. Rename `refresh_query_rag_source_description` →
   `refresh_query_rag_registration` (name now reflects what it does):
   ```python
   def refresh_query_rag_registration() -> None:
       """Sync query_rag's TOOLS entry to the current rag_servers state.

       Removes the tool entirely when no servers are configured, so the
       chat model isn't tempted to call a tool that can't possibly
       succeed. Re-adds and re-describes it when at least one server
       exists.
       """
       from app.tools import TOOLS
       servers = _list_sources()
       if not servers:
           TOOLS.pop("query_rag", None)
           return
       # Re-add after a prior pop. The spec object is the same one
       # the @tool decorator built — keeps name/description/func intact.
       if "query_rag" not in TOOLS:
           TOOLS["query_rag"] = _QUERY_RAG_SPEC
       spec = TOOLS["query_rag"]
       lines = ["Name of the RAG source to query. Available sources:"]
       for s in servers:
           desc = s.description.strip() or "(no description)"
           lines.append(f"- {s.name}: {desc}")
       spec.parameters_schema["properties"]["source"]["description"] = (
           "\n".join(lines)
       )
   ```
4. **Lifespan startup hook already exists** — `main.py:55` already
   calls `refresh_query_rag_source_description()` (added in phase 12d).
   No new call needed; the existing one just gets renamed. Critically:
   the gating behavior we're adding piggybacks on the existing hook —
   on startup, if the DB has 0 servers, the new function pops
   `query_rag` from `TOOLS`, so the model never sees it without a
   route ever firing. **Also update the lifespan comment** (lines
   47–54) to mention the gating behavior, since "primes the source
   schema" no longer covers what the function does.
5. **Update call sites** — three places call the renamed function:
   - `main.py:28` (import) + `main.py:55` (call)
   - `app/routes.py:217` (add_server_endpoint) — also update the
     import at line 72
   - `app/routes.py:239` (delete_server_endpoint)
6. **Grep for stragglers** before finishing:
   `rg refresh_query_rag_source_description` — must return zero hits
   after the rename. Candidates beyond the call sites above: the
   `tests/test_tools.py` test name (already in the test rename list),
   any docstring references in `app/tools/rag.py` itself, and
   in-line comments in any of the touched files.

### 5. Templates
**`templates/_settings.html`** (form, lines 42–67)
- Add a description textarea between the URL label and the submit
  button:
  ```html
  <label class="rag-server-form__description">
    Description
    <textarea name="description" required maxlength="200" rows="2"
              placeholder="What's in this RAG server? e.g. 'PubMed abstracts on cardiology, 2020-2024'"></textarea>
  </label>
  ```
- `required` is the form-level soft-require. `maxlength="200"` is the
  client-side mirror of the route's silent truncation.

**`templates/_rag_server_row.html`**
- Add a row beneath the existing name/url/delete row showing the
  description (so users can see what they typed):
  ```html
  <div class="rag-server__description">
    {{ server.description or "(no description)" }}
  </div>
  ```
- Keep the existing `<li id="rag-server-{id}">` wrapper; the new div
  becomes a child so `hx-swap="delete"` on the delete button still
  removes the whole row including its description.

### 6. CSS
**`static/style.css`**
- `.rag-server` (lines 934–967): currently
  `grid-template-columns: 1fr auto auto;` (name, url, delete). Switch
  to a 2-row layout: row 1 keeps the existing 3 columns; row 2 holds
  `.rag-server__description` spanning columns 1–2 (so the delete
  button stays visually anchored to the first row). Use
  `grid-template-areas` for clarity, or `grid-column: 1 / span 2;
  grid-row: 2;` on `.rag-server__description`.
- `.rag-server-form` (lines 972–1041): currently
  `grid-template-columns: 1fr 2fr auto auto;` (name, url, button, icon).
  Add a second row holding the description textarea label spanning
  all 4 columns (`grid-column: 1 / -1; grid-row: 2;`).
- Add `.rag-server__description` styles: muted color (use the existing
  muted text variable if one is defined; otherwise `color: var(--muted-color)`),
  smaller font size, wraps freely.

### 7. Tests

**`tests/test_rag_servers.py`**
- Update `test_create_returns_populated_row` (line 39) to pass
  `description="…"` and assert it round-trips.
- Add `test_create_server_defaults_description_to_empty_string` —
  call `create_server` without `description`, assert `.description == ""`
  (covers the backward-compat default).

**`tests/test_routes.py`**
- Update every existing `test_settings_add_server_*` test (lines 1632,
  1652, 1680, 1714, 1737, 1768) to include `description` in the form
  data (any non-empty short string).
- Add `test_settings_add_server_persists_description` — POST with
  description, assert the new row has it and that the row template
  renders the text.
- Add `test_settings_add_server_truncates_description_at_200_chars` —
  POST with a 250-char description, assert the stored value is exactly
  200 chars.
- Add `test_settings_add_server_strips_description_whitespace` —
  POST with whitespace-wrapped description, assert stored value is
  stripped.
- Add `test_settings_get_renders_description_field` — GET /settings,
  assert the form contains `<textarea name="description"` and the
  `required` attribute.

**`tests/test_tools.py`**
- Rename `test_refresh_query_rag_source_description_injects_names`
  (line 620) → `test_refresh_query_rag_registration_includes_descriptions`.
  Seed servers with descriptions, assert each appears as
  `- <name>: <description>` in the hint.
- Add `test_refresh_query_rag_registration_uses_no_description_fallback`
  — seed a server with an empty description, assert the line reads
  `- <name>: (no description)`.
- Add `test_refresh_query_rag_registration_removes_tool_when_no_servers`
  — start with `query_rag` in `TOOLS`, call refresh with 0 servers,
  assert `"query_rag" not in TOOLS`.
- Add `test_refresh_query_rag_registration_readds_tool_when_server_added`
  — start with `TOOLS.pop("query_rag")`, seed a server, call refresh,
  assert the tool is back and the source description reflects the new
  server.

**`tests/conftest.py`**
- The existing autouse module-state isolation (per CLAUDE.md, "consolidating
  module-state isolation: live_generations, capability cache") needs a
  third snapshot: `TOOLS["query_rag"]`. After the refresh-registration
  change, tests can pop it from the registry as a side effect; without
  isolation, an unrelated downstream test could see the registry in an
  unexpected state. Snapshot the entry pre-test and restore post-test.

## Functions/utilities to reuse

- **`_ensure_name_locked_column`** (`app/db.py:86`) — exact template
  for the new column migration. `PRAGMA table_info` + conditional
  `ALTER TABLE`.
- **`closing(open_connection())`** pattern (`app/tools/rag.py:58`) —
  reuse for `_list_sources()`. Sqlite's `Connection.__exit__` doesn't
  close the handle.
- **`probe_rag_health`** (`app/rag_health.py`) — unchanged; it only
  uses `name`/`url` strings and never touches the `RagServer` shape.
- **The existing form-error / health-icon flow** in `static/app.js`
  (`.rag-server-form` branch, lines 189–237) — unchanged. `form.reset()`
  handles textareas natively; the JS gating on `event.detail.successful`
  already covers our case.

## Verification

1. **Unit + integration tests**: `pytest` — full suite stays green.
   Target the new + updated tests directly with
   `pytest tests/test_rag_servers.py tests/test_routes.py tests/test_tools.py -v`.
2. **Coverage**: `pytest --cov=app --cov=main --cov-report=term-missing` —
   should hold at 98%+. New branches in the hint builder
   (empty-description, no-servers) need coverage.
3. **Browser smoke test** (per CLAUDE.md "Smoke-test UI changes in a
   real browser"):
   - `source .venv/bin/activate && uvicorn main:app --reload`
   - Open `http://localhost:8000/settings`
   - Add a new RAG server with a description. Verify the row appears
     with the description visible underneath the URL.
   - Attempt to submit with an empty description — browser should
     block (HTML `required`).
   - Paste a 300-char description — browser should clip at 200 chars
     (HTML `maxlength`).
   - Delete and re-add a server (since there's no edit flow).
4. **Tool-spec inspection**: open a Python REPL and:
   - With 0 servers: `from app.tools.rag import
     refresh_query_rag_registration; refresh_query_rag_registration();
     from app.tools import TOOLS; assert "query_rag" not in TOOLS`.
   - After seeding 2–3 servers with descriptions:
     `refresh_query_rag_registration();
     print(TOOLS["query_rag"].parameters_schema["properties"]["source"]["description"])`.
     Confirm the hint contains the `- <name>: <description>` list.
5. **End-to-end with Ollama** (manual): in a real chat, ask a question
   that's clearly relevant to one source vs. another. Confirm the
   model picks the right source. This is the actual feature payoff —
   pytest can't validate it.
6. **Migration smoke**: against an existing DB at
   `~/Library/Application Support/ollama_slowly/chats.db`, restart the
   app, query `PRAGMA table_info(rag_servers);` to confirm the
   `description` column exists with `NOT NULL DEFAULT ''`, and verify
   existing rows have an empty description that renders as
   `(no description)` in the settings UI and `(no description)` in
   the tool hint.
