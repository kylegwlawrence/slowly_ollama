# Phase 16 Retrospective — User-invoked agents

## What shipped

Replaced the automatic research → review → generation loop (built in
phases 13–14, disabled since phase 15) with **named agents the user
invokes by hand** from a composer dropdown. The chat defaults to
"Normal" (plain chat); picking an agent runs that turn with the agent's
own model, system prompt, tool allowlist, and Ollama `think` flag.

Shipped roster (code-defined):

- **Research** — model `granite4.1:8b`, tools `{current_time, query_rag}`,
  `think=False`. Gathers info and reports findings (not the final answer).
- **Content Generator** — model `granite4.1:8b`, no tools, `think=False`.
  Synthesizes the conversation so far into a polished piece.

(Both agents initially shipped on `qwen3.5:9b` — Research with `think=True`.
qwen is too slow on a 16GB M1, so both moved to `granite4.1:8b` — fast and
tool-capable, but NOT a thinking model, which forced Research's `think` back
to `False`. Sharing one model is also a perf win: the Research → Content
hand-off needs no model swap/reload. See the `think` follow-up below.)

The key realization: **an invoked agent is just the single-agent
producer `_run_generation` parameterized by four things** — model,
system prompt, tool allowlist, think flag. No new SSE events, message
roles, or render blocks were needed (unlike the old loop, which added
all three). Agents pass information to each other through the shared
conversation history — that's the hand-off channel.

## Changes by file

- **`app/agents/__init__.py`** — Rewritten from the loop's package surface
  into the registry: `AgentSpec` (frozen dataclass: name, label,
  description, model, system_prompt, tools, think) + `AGENTS` dict +
  `list_agents()` / `get_agent()`. `get_agent(None | "" | unknown)` → None
  (Normal), so an unknown/removed name degrades gracefully.
- **`app/agents/prompts.py`** — Replaced the three loop prompts with
  `RESEARCH_AGENT_PROMPT` / `CONTENT_GENERATOR_PROMPT`.
- **`app/agents/loop.py`, `verdict_tools.py`** — **Deleted.**
- **`app/db.py`** — `active_agent TEXT` (nullable) on `conversations` +
  `_ensure_conversations_active_agent_column` migration mirroring the
  temperature/tool-cap backfills. NULL = Normal.
- **`app/queries.py`** — `Conversation.active_agent` (audited every
  row→dataclass mapper + RETURNING clause); `create_conversation(...,
  active_agent=None)`; new `set_active_agent`. Dropped
  `research_findings`/`review_verdict` from `Role` and the agentic settings
  helpers (`get/set_agentic_mode`, `review_enabled`, `generator_enabled`).
- **`app/ollama.py`** — `stream_chat` / `maybe_tool_call` gained a
  `think: bool | None` param; `"think"` is added to the payload **only when
  not None**, so Normal chat is byte-identical to before.
- **`app/generation.py`** — `start_generation` + `_run_generation` gained
  `system_prompt_override`, `tool_allowlist`, `think`. Extracted
  `_chat_tool_specs` (Normal: per-chat chips) and `_agent_tool_specs`
  (agent: allowlist only). Agent path always injects the agent's prompt
  (even with no tools); `think` flows to both Ollama calls.
  `_build_history_payload` now drops any unrecognized role.
- **`app/render.py`** — Removed `AgenticToolBatchBlock` / `AgenticIteration`
  + all agentic OOB-render helpers (≈520 lines). `group_messages_for_render`
  silently skips unknown/legacy roles.
- **`app/routes.py`** — `_agent_overrides(conversation)` → start_generation
  kwargs (effective model + the three overrides). Agent resolution wired
  into create/send/regenerate. New `POST /chats/{id}/agent` returns OOB
  swaps for the header indicator + chip bar. Deleted the three agentic
  settings endpoints, `_compute_agentic_skipped`, and the `httpx` import.
- **`templates/`** — New `_agent_select.html`, `_agent_indicator.html`,
  `_chat_tool_chips.html`. `_chat_panel.html` gets the indicator + an
  in-chat picker form; `_composer.html` gets the picker left of the model
  select. Deleted `_agentic_tool_card.html`, `_findings_row.html`,
  `_verdict_row.html`, `_settings_agentic_section.html`; trimmed the
  agentic SSE events from `_assistant_placeholder.html` and the agentic
  shell bits from `_tool_card_shell.html`.
- **`static/app.js`** — Greys/locks `#composer-model` when a non-Normal
  agent is picked (CSS-disabled, not HTML-`disabled`, so it still submits a
  Normal model). **`static/style.css`** — picker / indicator / agent-tools
  / locked-select styles.

## Tests

Suite went **449 → 425** — the deleted agentic suites (`test_agentic_loop.py`
958 lines, `test_templates_agentic_card.py` 214 lines, plus settings/banner
cases in shared files) outnumber the new agent tests. New coverage:

- `test_agents.py` — rewritten for the registry (fields, contents,
  `get_agent` None/unknown/valid, frozen, think defaults/opt-in,
  old-loop-symbols-gone).
- `test_queries.py` — `active_agent` round-trip + `set_active_agent`
  set/clear/unknown; backfilled `set_conversation_temperature` coverage.
- `test_db.py` — `active_agent` migration on a pre-existing table.
- `test_generation.py` — agent path injects the agent prompt + filters to
  the allowlist; no-tools agent still injects its prompt; `think` reaches
  the payload (and is omitted for Normal); `_agent_tool_specs` /
  `_chat_tool_specs` branch coverage; rewrote the dispatcher tests as
  producer-spawn + override-forwarding tests.
- `test_routes.py` — `POST /chats/{id}/agent` persist + indicator, Normal
  clear, unknown→Normal, 404; create-chat-with-agent persists.
- `test_integration.py` — end-to-end journey: create on Research, drain
  the stream, switch to Content Generator, back to Normal.

Coverage **98%** (up from 97%); `app/agents`, `app/render.py`,
`app/queries.py` all at 100%.

## What worked well

- **"Agent = `_run_generation` + N parameters"** kept the producer change
  tiny. Threading explicit overrides (`system_prompt_override`,
  `tool_allowlist`, `think`) rather than passing an `AgentSpec` kept
  `generation.py` decoupled from the registry — the route does the
  resolving, generation stays agent-agnostic.
- **Deleting the dead loop was almost all the diff** (−4781/+1693). The
  disabled phases 13–14 machinery was pure liability; removing it left one
  clear agent concept.
- **Defensive unknown-role skipping** in both `group_messages_for_render`
  and `_build_history_payload` made any leftover `research_findings` /
  `review_verdict` rows in the dev DB harmless — no destructive
  delete-the-rows migration needed.
- **The plan-mode review pass earned its keep**: it caught the HTMX
  attribute-inheritance trap (below) in markdown, before any code.
- **Code registry over DB-CRUD** matched the repo's hardcoded-prompts
  philosophy. Adding an agent is ~8 lines in `AGENTS` + a prompt constant.

## What was harder than expected / gotchas

- **HTMX attribute inheritance.** The in-chat agent picker had to be its
  *own* `<form>` sibling above `.message-form`, not nested inside it —
  otherwise the message form's `hx-post="/messages"` / `hx-target="#messages"`
  inherit onto the `<select>`. Its class `message-form__agent` also must NOT
  match the `.message-form` afterRequest handler in `app.js` (it doesn't, by
  BEM naming) or it would trigger a stray textarea reset.
- **OOB needs a stable target.** Introduced `_chat_tool_chips.html` so
  `#chat-tool-chips` always exists when the model is tool-capable, switching
  its *content* (chips vs. read-only agent tools). Without it, the agent
  endpoint couldn't reliably OOB-swap the chip bar.
- **A disabled `<select>` submits nothing.** The composer model select is
  CSS-locked (pointer-events/opacity), not HTML-`disabled`, so the chat
  still persists a Normal model when an agent is picked.
- **sed-based test surgery + Edit cache.** Deleting agentic test regions by
  line range was fast, but mixing `sed -i` with the Edit tool invalidated
  the file-state cache twice (had to re-Read before editing). Bulk range
  deletes are best done before, not interleaved with, targeted edits.
- **A shared test helper lived inside the deleted section.**
  `_tool_capable_handler` was defined in the agentic-tests block but used by
  the phase-15 tests below it; deleting the block broke them. Recovered from
  `git show HEAD`. Lesson: shared fixtures/helpers shouldn't live inside a
  feature's test section.

## Code-review follow-up — per-agent `think` flag

After review, the user flagged qwen3.5:9b "thinking too much and not
getting to the response." Probing Ollama settled the design:

- `think: false` → HTTP 200 on **any** model (including non-thinking
  `llama3.1:8b`).
- `think: true` → HTTP **400** on a non-thinking model ("does not support
  thinking").

So `AgentSpec.think` **defaults `False`** (safe anywhere); an agent opts in
only when paired with a thinking-capable model. The response parser already
reads only `message.content`, so a thinking model's reasoning (in the
separate `thinking` field) never reaches the bubble — no parser change was
needed.

This immediately paid off: Research briefly ran on `qwen3.5:9b` with
`think=True`, then moved to `granite4.1:8b` for speed. Because the safe
default is `False` and `think=True` 400s on a non-thinking model, the swap
was a two-line change (model + `think=True` → `think=False`) with no
surprise breakage. Both shipped agents now run `think=False`; the opt-in
path stays exercised by tests but is unused by the current roster.

## Verification

- **425 tests, 98% coverage.**
- **Server-side smoke** (live uvicorn + real Ollama): composer picker
  present, `/settings` free of agentic toggles, create-with-agent shows the
  indicator + agent model, `POST /chats/{id}/agent` returns OOB
  indicator + chip swaps and persists, switch-to-Normal restores the pinned
  model + real chips, reload preserves the selection.
- **The flagged hand-off risk did NOT materialize.** Built the exact
  Content-Generator payload (`system → user → assistant+tool_calls → tool →
  assistant → user`) via `_build_history_payload` and fired it at
  `llama3.1:8b` and `granite4.1:8b` with `tools=None`: both 200, both read
  the prior turn's tool result. No model 400'd on tool-history-without-tools,
  so the planned "strip tool messages for no-tool agents" mitigation was
  **not** needed.
- **Browser-only bits left to manual check**: the JS model-greying, live
  HTMX OOB visuals, and CSS layout (no browser-automation tool available in
  this session).

## What was deferred / out of scope

- Disabling picker options whose model isn't installed (the invocation-time
  error path covers it; a panel-render install check was the cost).
- Per-message agent attribution in history (which agent produced each bubble).
- Per-agent temperature / tool-iteration cap (agents reuse the chat's).
- Automatic agent chaining (invocation is sequential and by hand).
- The composer does **not** hide tool chips when an agent is picked, though
  the in-chat panel does — minor UX inconsistency (the composer chips seed
  the chat's Normal config, so showing them is defensible).
- The read-only agent-tools line shows the raw allowlist (e.g. `query_rag`
  even when no RAG server is configured, so it wouldn't actually be offered).
- Displaying the `thinking` field in the UI — currently dropped on purpose.

## Notes for future phases

- **Adding an agent** = add an `AgentSpec` to `AGENTS` + a prompt constant.
  Set `think=True` **only** for a thinking-capable model (Ollama 400s on
  `think: true` otherwise); the `False` default is safe on any model.
- `active_agent` is a code-registry key with **no DB constraint**.
  `get_agent` maps unknown/removed names → Normal, so dropping an agent from
  `AGENTS` auto-degrades any chat pinned to it — no migration needed.
- **This supersedes phases 13–14 entirely.** The phase 15b retro's "when
  re-enabling the agentic loop, port both per-chat gates" note is now moot —
  there is no loop to re-enable. Per-chat tool/RAG filtering lives only in
  `_run_generation` (Normal path) and `_agent_tool_specs` (agent path).
- **Tool-spec gating differs by path on purpose.** `_chat_tool_specs`
  (Normal) honors the per-chat tool/RAG chips; `_agent_tool_specs` (agent)
  uses the allowlist + **all** configured RAG servers (there are no
  per-agent server chips).
- **Residual risk, parked:** if a future model *does* 400 on
  tool-history-with-`tools=None` (it didn't on llama3.1 / granite4.1), the
  fix is localized — strip `tool` / `tool_calls` messages in
  `_build_history_payload` when `tool_allowlist` is empty. Don't strip
  unconditionally: the Content Generator *uses* that history for its
  hand-off.
