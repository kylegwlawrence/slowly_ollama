# RAG source descriptions retrospective

## Scope

The `query_rag` tool's `source` parameter was a plain enum of server names
with no context about what each source contains. With multiple sources
configured, the model guessed or asked the user rather than picking
intelligently.

This phase added a `description` column to `rag_servers` (user-supplied,
200-char cap), folded per-source descriptions into the tool spec so the
model sees a self-describing picker, and gated `query_rag` registration on
whether any servers exist at all — zero servers means the tool is removed
from the registry entirely rather than left visible with a degenerate hint.

Locked design decisions going in:
- **Soft-required**: `TEXT NOT NULL DEFAULT ''` in the schema; `required` on
  the form field; `description: str = ""` default in `create_server` so
  existing callers keep working.
- **200-char cap** silently truncated server-side; `maxlength="200"` on the
  textarea is a client-side hint only.
- **No edit-in-place**: delete + re-add, matching existing name/url behaviour.
- **Tool gating**: 0 servers → pop `query_rag` from `TOOLS`; ≥1 server →
  re-add/update with descriptions.

## What landed

| Area | Change |
|---|---|
| `app/db.py` | `description TEXT NOT NULL DEFAULT ''` in schema; `_ensure_rag_servers_description_column` migration |
| `app/rag_servers.py` | `description: str` on `RagServer`; `_row_to_server`, `list_servers`, `create_server` updated |
| `app/routes.py` | `description` form param; strip + 200-char cap; calls `refresh_query_rag_registration` (renamed) |
| `app/tools/rag.py` | `_list_source_names` → `_list_sources` (returns `list[RagServer]`); `_QUERY_RAG_SPEC` snapshot; `refresh_query_rag_source_description` → `refresh_query_rag_registration` with gating |
| `main.py` | import + call renamed; lifespan comment updated to describe gating |
| `templates/_settings.html` | description textarea between URL label and submit button |
| `templates/_rag_server_row.html` | description div on row 2, renders `(no description)` for empty |
| `static/style.css` | `.rag-server` 2-row grid; `.rag-server__description` spanning cols 1–2; form textarea styles; `.rag-server-form__description` spanning all columns row 2 |
| Tests | 10 new tests, 7 updated; conftest adds `TOOLS["query_rag"]` isolation |

Tests at phase close: **401/401 passing**, coverage **98%** on `app/` +
`main.py` (was 401 → 401; net +10 new tests). Two existing tests needed
fixes (see below).

## Decisions (and why)

- **`_QUERY_RAG_SPEC` snapshot at module level after the decorator.** Re-
  registration after a pop requires the original `ToolSpec` — name,
  description, and function reference intact. Snapshotting once at import
  time is cheaper than rebuilding from scratch and keeps the re-added spec
  object-identical to the decorator-built one. The snapshot line imports
  `TOOLS` non-idiomatically (not at the top of the file) because it must
  come after the `@tool` decorator runs; a `# noqa: E402` comment covers it.
- **Tool gating via pop/re-add, not a flag on the spec.** Removing the tool
  from the registry means `tool_specs_for_ollama()` and every other consumer
  never see it — no special-casing required anywhere downstream. The
  alternative (leave it in but suppress it somehow) would require every
  consumer to understand the "don't advertise this" state.
- **`description: str = ""` default on `create_server`.** The form's
  `required` attribute is the user-facing constraint. The route handler
  always passes `description` explicitly. The default on `create_server`
  is purely for test ergonomics — tests that aren't about description don't
  have to include it, matching the existing `url`-only test style.
- **Description truncation lives in the route, not the CRUD layer.** The
  route is the system boundary for user input; the 200-char cap is a
  user-input constraint, not a business rule. `create_server` accepts
  whatever string it's given — simpler to test and easier to change the cap
  without touching the model layer.
- **`conftest.py` TOOLS isolation snapshot-and-restore.** Same pattern as
  `live_generations` and `_capability_cache`: snapshot pre-test, restore
  post-test. Tests that pop `query_rag` as a side effect don't bleed into
  downstream tests that expect the tool present.

## What worked

- **The plan was self-contained enough that execution was clean.** Zero
  rework loops: every file changed, every test written matched the plan's
  spec. The two unexpected test fixes (column-set assertion and tools-list
  assertion) were trivial and predictable — the plan just didn't enumerate
  them because they were pre-existing tests that needed updating for the new
  behavior.
- **The conftest comment pre-anticipated the TOOLS isolation need.** The
  existing conftest had: "If phase 13 (or any later phase) introduces
  another module-level cache or registry that needs per-test isolation, add
  it here." The TOOLS isolation dropped in exactly there with no discussion.
  Good documentation of intent pays forward.
- **Tool gating as a design felt right immediately.** A tool with 0
  configured sources isn't a usable tool — the model calling it would always
  get an error. Removing it from the registry is strictly more correct than
  leaving it with a "(none configured)" hint, and the pop/re-add mechanism
  is clean enough that the implementation complexity is low.
- **CSS grid auto-placement handled the button/icon row correctly without
  explicit column assignments.** Forcing `.rag-server-form__description` to
  `grid-row: 2` left the button and health icon auto-placed at columns 3
  and 4, row 1 — exactly where they were before. No explicit column
  placement needed on the button or icon.

## What was tricky

- **Two pre-existing tests needed updating for the new behavior.**
  `test_rag_servers_table_exists_after_init` hard-coded the column set
  without `description`; `test_stream_passes_tools_payload_to_ollama`
  asserted `"query_rag" in names` — now correctly absent when no servers
  are configured. Both were one-line fixes, but they represent a real
  behavior change that the plan didn't flag as "update this test".
- **The module-level `_QUERY_RAG_SPEC` import is syntactically awkward.**
  The snapshot must happen after the `@tool` decorator runs, which means
  after the function definition, which means not at the top of the file.
  It reads slightly like an accident. A comment explaining why it's here
  (not at the top) makes it defensible, but a future reader skimming imports
  could still be confused.

## Surprises

- **The rename from `refresh_query_rag_source_description` to
  `refresh_query_rag_registration` touched more places than expected.**
  Three call sites (main.py, two in routes.py), the import in routes.py,
  the import in main.py, the existing test, plus every docstring/comment
  that referenced the old name inside rag.py. Verified clean via grep
  before closing. The lesson: when renaming a function that's called by
  name in strings (docstrings, comments), grep is mandatory — the compiler
  won't catch it.
- **Coverage on `app/rag_servers.py` hit 100%** after the changes. The
  new `description` field added branches (round-trip, default, row-mapping)
  that were all covered by the new tests.

## Notes for future phases

- **The pop/re-add pattern for conditional tools is reusable.** Any tool
  that should only appear when its backing config is non-empty can use the
  same `_SPEC_SNAPSHOT` + `TOOLS.pop / TOOLS[name] = _SPEC` pattern. The
  conftest TOOLS isolation already handles the test-isolation side.
- **Route-level truncation for user-supplied strings.** The 200-char
  description cap lives in the route (`description.strip()[:200]`). If
  other user-supplied strings need caps in the future, follow the same
  pattern: truncate at the route boundary, document the cap on the form
  field with `maxlength`, and belt-and-suspenders server-side.
- **`RagServer.description` is now available everywhere `list_servers`
  is called.** The `query_rag` tool's inline `by_name` lookup already
  re-fetches from the DB on each call — description is available there if
  a future feature needs it (e.g., embedding the description in the error
  message when the model picks a wrong source).
- **The plan's "no edit-in-place" decision is increasingly visible.**
  Delete + re-add means updating a description requires losing the server's
  id and re-running the health probe. If user feedback surfaces this as
  friction, an edit-in-place flow (textarea in the row itself, PATCH
  endpoint) is the natural follow-up. The schema and CRUD layer are ready
  for it — only the route and template need adding.
