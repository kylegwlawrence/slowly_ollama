# Phase 15b Retrospective — Per-RAG-server chip toggles per chat

## What shipped

Per-RAG-server chip toggles in the tool bar. Instead of a single
`query_rag` chip that gates all sources at once, each configured RAG
server now gets its own chip that can be toggled independently per chat.

Example tool bar with two RAG servers:

```
[✓ current_time]  [✓ arxiv]  [✕ pubmed]
```

Toggling a server off removes it from the `query_rag` source list sent
to Ollama for that chat. Toggling off all servers excludes `query_rag`
from the tool spec entirely — the model won't attempt a tool call it
can't satisfy.

## Changes by file

- **`app/db.py`** — New `chat_rag_settings(conversation_id, server_name,
  enabled)` table. `CREATE TABLE IF NOT EXISTS` in `_SCHEMA_SQL` handles
  both fresh and existing DBs without a separate migration function.

- **`app/queries.py`** — New `ChatRagState` dataclass + four helpers:
  `seed_chat_rag_servers`, `get_chat_rag_states`, `toggle_chat_rag_server`,
  `get_enabled_rag_server_names`. Exact structural parallel to Phase 15's
  `ChatToolState` family. Missing rows default to enabled so existing chats
  see newly-added servers without re-seeding.

- **`app/tools/__init__.py`** — `RAG_TOOL_NAME = "query_rag"` constant.
  Eliminates the raw string literal that was scattered across `routes.py`,
  `generation.py`, and `rag.py`.

- **`app/tools/rag.py`** — `build_source_description(servers)` helper
  extracted from `refresh_query_rag_registration`. Both the global-spec
  refresh and the per-chat generation-time patching now call the same
  function with different server lists.

- **`app/generation.py`** — `_run_generation` reads per-chat enabled
  servers from DB, then deep-copies the `query_rag` spec and patches its
  source description to only mention enabled servers. If all server chips
  are off, `query_rag` is excluded from the tools payload entirely.
  `copy` and `app.tools.rag.build_source_description` added as imports.

- **`app/routes.py`** — New `_default_rag_server_states(db)` and
  `_chip_states(db, conversation_id, *, servers=None)` helpers.
  `new_chat_endpoint` gained `db: DB` dependency (needed for the composer
  chip bar). New endpoint `POST /chats/{id}/rag-servers/{server_name}`.
  `create_chat_endpoint` seeds RAG server rows from `enabled_rag_servers`
  form fields. `_chip_states`'s optional `servers=` kwarg lets callers
  reuse an already-fetched list to avoid a second `list_servers` call.

- **`templates/_tool_chips.html`** — Second loop over `rag_server_states`
  added after the tool chips loop. Composer mode uses
  `name="enabled_rag_servers"` checkboxes; chat mode uses
  `hx-post="/chats/{id}/rag-servers/{name}"` buttons.

- **`templates/_composer.html`** — `default_rag_server_states` wired into
  the chip include. Guard updated from `{% if default_tool_states %}` to
  `{% if default_tool_states or default_rag_server_states %}` so the chip
  bar renders when only RAG servers are configured.

- **`templates/_chat_panel.html`** — Same guard update: `{% if
  supports_tools and (tool_states or rag_server_states) %}`.

## Test additions (449 total, up from 433)

- `tests/test_db.py` — Updated table set assertion for
  `chat_rag_settings`.
- `tests/test_queries.py` — 11 new tests covering all five new query
  functions: seed idempotency, partial-enabled seeding, unseeded-defaults-
  to-enabled, toggle on/off from various states, `ChatRagState` frozen
  check.
- `tests/test_routes.py` — 4 new tests: toggle-returns-chip-bar, 404 on
  unknown conversation, 404 on unknown server, create-chat seeds RAG rows.
- `tests/test_generation.py` — 2 new end-to-end tests: `query_rag`
  excluded when all server chips are disabled; source description filtered
  to only enabled servers when one of two is toggled off.

## Coverage

`app/queries.py` at 100%. Overall 97%, unchanged from Phase 15.

## What worked well

- The `chat_tool_settings` → `chat_rag_settings` parallel was a natural
  shape to follow — the four query helpers wrote themselves once the
  table structure was decided.
- `CREATE TABLE IF NOT EXISTS` in `_SCHEMA_SQL` handled the migration for
  free. No `_ensure_*` migration function was needed because the schema
  already uses `IF NOT EXISTS` everywhere.
- `build_source_description` extraction was caught by the code review
  (not noticed during implementation). The description-building logic was
  genuinely duplicated word-for-word between `rag.py` and `generation.py`.
- The `servers=` optional kwarg on `_chip_states` eliminated two
  double-`list_servers` fetches (`toggle_chat_rag_server_endpoint` and
  `create_chat_endpoint`) without changing any callers that don't hold the
  list already.
- `deep-copy` of the per-spec dict kept the global TOOLS registry clean —
  source-description patching is a per-turn ephemeral operation and must
  not mutate the shared spec.

## What was harder than expected

- The composer needed `db: DB` added to `new_chat_endpoint` to read the
  RAG server list. The endpoint previously took no DB dependency — a
  small but necessary change.
- The chip bar condition (`{% if supports_tools and tool_states %}`) broke
  silently when `query_rag` was filtered out of `tool_states` but RAG
  server chips existed. Updating it to `{% if supports_tools and
  (tool_states or rag_server_states) %}` was trivial but required
  noticing the scenario.

## What was deferred

- The Jinja `{% macro %}` refactor for the chip template: the
  `{% if is_composer %}...{% else %}...{% endif %}` branch is repeated for
  both the tool loop and the RAG server loop. A macro would cut ~25 lines.
  Deferred because the template is already readable at its current length
  and macros add indirection that complicates debugging.
- The agentic loop (`app/agents/loop.py`) still uses an unfiltered
  `tool_specs_for_ollama()` call — no per-chat RAG filtering. Safe for
  now because agentic mode is disabled (`_AGENTIC_AVAILABLE = False`).
  When it's re-enabled, it will need the same `_enabled_rag_servers` logic
  that `_run_generation` now has.

## Notes for future phases

- `chat_rag_settings` rows are keyed by `server_name` (a string), not by
  `server_id`. This means deleting a RAG server from `/settings` leaves
  orphan rows in `chat_rag_settings` that are harmlessly ignored on
  lookup. If a bulk-cleanup sweep is ever wanted, `DELETE FROM
  chat_rag_settings WHERE server_name NOT IN (SELECT name FROM
  rag_servers)` is the query.
- When re-enabling the agentic loop: `_run_agentic_generation` in
  `app/agents/loop.py` will need `_all_rag_servers`, `_enabled_rag_names`,
  and `_enabled_rag_servers` computed the same way `_run_generation` does
  it now, then passed into the `tool_specs = ...` line that currently
  calls `tool_specs_for_ollama()` unfiltered. Note this is the *same*
  unfiltered call that also skips the per-chat **tool** chip filtering
  (`_enabled_names` in `_run_generation`), not just RAG-server filtering —
  port BOTH gates, otherwise the agentic loop ignores every per-chat chip
  toggle (tools and RAG sources alike).
- `_chip_states`'s `servers=` kwarg is a caller-side optimization only —
  it does not change behavior. Any new call site that doesn't already hold
  the list can omit the kwarg and let `_chip_states` fetch it.
- The `RAG_TOOL_NAME` constant in `app/tools/__init__.py` is the single
  source of truth for the string `"query_rag"`. If the function is ever
  renamed, update it there and all references follow.
