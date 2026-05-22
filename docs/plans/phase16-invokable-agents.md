# Phase 16 — User-invoked agents

## Context

Today the app has an **automatic** multi-agent loop (research → review →
generation) behind a global toggle. It's disabled (`start_generation` always
runs single-agent — `app/generation.py:335`). It gives the user no control: the
loop decides everything, and you can't say "do research now, then turn it into
content."

We're replacing that concept with **user-invoked named agents**. The user picks
an agent from a dropdown in the chat composer; that agent runs with its own
system prompt, its own assigned model, and its own allowlist of tools. The chat
defaults to a "Normal" agent (plain chat, today's behavior). Agents are invoked
sequentially and by hand — pick Research, send a query, read the findings, then
pick Content Generator and send "write this up." Because every agent sees the
whole conversation, each one can build on the previous one's output. That shared
history *is* the hand-off channel.

**Key implementation insight:** an invoked agent is just the existing
single-agent producer (`_run_generation`) parameterized by three things — a
**system prompt**, a **model**, and a **tool allowlist**. The producer already
filters tools and gates on model capability (`app/generation.py:560-614`). We
swap "per-chat chips → agent allowlist", "chat model → agent model", and
"default prompt → agent prompt". No new SSE events, message roles, or render
blocks are needed (unlike the old loop).

## Decisions (locked with the user)

- **Agent definitions:** a **code registry** (Python `AgentSpec` list in
  `app/agents/`). Adding an agent = a few lines + restart. Mirrors the `@tool`
  registry and the existing hardcoded prompts. No runtime CRUD UI.
- **Old auto-loop:** **removed** as part of this work (it's unreachable dead
  code). One clear agent concept.
- **UI:** in-chat composer gets the **agent picker only**; the header shows the
  model read-only and an "Agent: …" indicator. The new-chat composer keeps an
  Agent + Model row, model greying out when an agent is picked.
- **Starting roster + models:**
  - **Normal** — implicit (no `AgentSpec`); today's behavior.
  - **Research** — model `qwen3.5:9b`, tools `{current_time, query_rag}`.
  - **Content Generator** — model `qwen3.5:9b`, no tools.
  (`qwen3.5:9b` is tool-capable and has a "thinking" capability — verified via
  `/api/show`.)

## AI-agent best practices applied

- **Single responsibility** per agent (research vs. synthesis), not one
  do-everything prompt.
- **Least privilege on tools** — each agent gets only the tools it needs (the
  allowlist); Content Generator gets none.
- **Right model per task** — per-agent model assignment.
- **Shared-context hand-off** — agents read the full conversation, so a later
  agent builds on an earlier one's output; prompts say so explicitly.
- **Clear output contract** — each prompt states what to produce and to avoid
  fabrication / meta-commentary.

---

## Part A — Remove the old auto-loop

**Delete entirely:**
- `app/agents/loop.py`, `app/agents/verdict_tools.py`
- `templates/_agentic_tool_card.html`, `templates/_findings_row.html`,
  `templates/_verdict_row.html`, `templates/_settings_agentic_section.html`
- `tests/test_agentic_loop.py`, `tests/test_templates_agentic_card.py`

**Edit (strip agentic bits, keep the rest):**
- `app/agents/__init__.py` — rewrite as the new registry surface (Part B). Drop
  `AGENTIC_ITERATION_CAP` and the three loop-prompt re-exports.
- `app/agents/prompts.py` — replace the three loop prompts with the new agent
  prompts (Part B).
- `app/queries.py` — remove `research_findings` / `review_verdict` from `Role`;
  delete `get/set_agentic_mode`, `get/set_review_enabled`,
  `get/set_generator_enabled` and their key constants. (Add `active_agent`
  helpers in Part C.)
- `app/generation.py` — drop the dead `research_findings`/`review_verdict`
  branch in `_build_history_payload` (line 462); change the final `else` to emit
  only `user`/`assistant` rows and silently drop any unrecognized role (defends
  against any legacy agentic rows in the dev DB). Clean the dispatcher comments
  (303-316, 335-337).
- `app/render.py` — remove `from app.agents import AGENTIC_ITERATION_CAP` (l.22),
  the `AgenticToolBatchBlock` / `AgenticIteration` dataclasses, the
  `agentic_tool_batch` branch in `group_messages_for_render`, and the
  max-iterations helper. Keep `MessageBlock` / `ToolBatchBlock`. Make
  `group_messages_for_render` **silently skip any unrecognized role** (anything
  not `user`/`assistant`/`tool_call`/`tool_result`) — symmetric with the
  `_build_history_payload` change, so any legacy `research_findings` /
  `review_verdict` rows in the existing dev DB don't render as stray bubbles or
  break the payload. (Non-destructive: the rows stay in the DB, just unrendered.)
- `app/routes.py` — remove `from app.agents.prompts import (...)` (l.51); delete
  the `/settings/agentic-mode`, `/settings/agentic-review`,
  `/settings/agentic-generator` endpoints and `_compute_agentic_skipped`; drop
  every `agentic_skipped=` kwarg and the agentic-prompt data passed to
  `/settings`.
- `templates/_chat_panel.html` — remove the agentic-skipped banner (52-67) and
  the `agentic_tool_batch` block branch (90-95).
- `templates/_assistant_placeholder.html` — trim `sse-swap` (l.48) to
  `token,done,error,title,tool-call,tool-result`; update the comment.
- `templates/_settings.html` — remove the `_settings_agentic_section.html`
  include + its wrapper (104-106).
- `templates/_tool_card_shell.html` — comment-only cleanup; leave functional
  single-agent markup intact.
- `tests/` (`test_routes.py`, `test_render.py`, `test_generation.py`,
  `test_queries.py`, `test_integration.py`, `test_db.py`) — delete the
  agentic-specific cases (settings toggles, agentic dispatch/roles/render,
  skipped banner); keep all single-agent cases.

No DB change is needed to drop the two roles — the `messages.role` CHECK was
already removed (`app/db.py:52`); the `Role` literal is the only gate.

---

## Part B — The agent registry (`app/agents/`)

**`app/agents/prompts.py`** — two hardcoded prompts (full text below in
"Prompts").

**`app/agents/__init__.py`** — registry, mirroring `app/tools/__init__.py`:

```python
from dataclasses import dataclass, field
from app.agents.prompts import RESEARCH_AGENT_PROMPT, CONTENT_GENERATOR_PROMPT

@dataclass(frozen=True)
class AgentSpec:
    name: str            # stable id, stored in DB + used as form value
    label: str           # UI display text
    description: str     # dropdown help / tooltip
    model: str           # assigned Ollama model id
    system_prompt: str
    tools: frozenset[str] = field(default_factory=frozenset)  # tool-name allowlist

AGENTS: dict[str, AgentSpec] = {
    "research": AgentSpec(
        name="research", label="Research",
        description="Gathers information with tools and reports findings.",
        model="qwen3.5:9b", system_prompt=RESEARCH_AGENT_PROMPT,
        tools=frozenset({"current_time", "query_rag"}),
    ),
    "content_generator": AgentSpec(
        name="content_generator", label="Content Generator",
        description="Writes a polished piece from the conversation so far.",
        model="qwen3.5:9b", system_prompt=CONTENT_GENERATOR_PROMPT,
        tools=frozenset(),
    ),
}

def list_agents() -> list[AgentSpec]:
    return list(AGENTS.values())

def get_agent(name: str | None) -> AgentSpec | None:
    """Return the AgentSpec for `name`, or None for Normal/unknown."""
    if not name:
        return None
    return AGENTS.get(name)
```

"Normal" is the absence of an agent (`active_agent` NULL → `get_agent` returns
`None` → today's behavior). Unknown names also resolve to `None` (defensive).

---

## Part C — Persist the active agent on the conversation

- **Schema** (`app/db.py`): add `active_agent TEXT` (nullable) to the
  `conversations` table in `_SCHEMA_SQL`, and a
  `_ensure_conversations_active_agent_column(conn)` migration mirroring
  `_ensure_conversations_temperature_column` (l.138); call it from
  `initialize_database`.
- **`app/queries.py`**: add `active_agent: str | None` to the `Conversation`
  dataclass. **Audit every row→`Conversation` construction** (the shared
  `_row_to_conversation`-style mapper and any `SELECT` that hydrates a
  conversation — `get_conversation`, `list_conversations`, etc.) so the new
  column is mapped everywhere, not just in `get_conversation`.
  `create_conversation(..., active_agent=None)` persists it; add
  `set_active_agent(conn, conversation_id, name: str | None)`
  (`UPDATE conversations SET active_agent = ?`).

NULL / `""` mean Normal.

---

## Part D — Generation integration

Thread two explicit overrides so `app/generation.py` stays decoupled from the
registry (the route does the resolving):

- **`start_generation` + `_run_generation`** gain
  `system_prompt_override: str | None = None` and
  `tool_allowlist: frozenset[str] | None = None`. `start_generation` forwards
  them.
- Inside `_run_generation`, the tool-spec / system-prompt block becomes:
  - **Agent path** (`tool_allowlist is not None` — note: an empty `frozenset()`
    still takes this path, which is how the no-tools Content Generator is
    distinguished from Normal=`None`): build `_enabled_specs` by filtering
    `tool_specs_for_ollama()` to names in `tool_allowlist`. For `query_rag`:
    include it only if it's in the allowlist **and** at least one RAG server is
    configured (`_rag_module.list_servers(db)`), using all configured servers
    for the source description. `tools_payload = specs if specs and await
    ollama.model_supports_tools(client, model) else None`. `system_prompt =
    system_prompt_override` **always** (so the no-tools Content Generator still
    gets its prompt — verified the streaming call passes `system_prompt`,
    `app/generation.py:751`).
  - **Normal path** (`tool_allowlist is None`): unchanged — per-chat chip
    filtering + `SINGLE_AGENT_SYSTEM_PROMPT`-only-when-tools.
- **No special-casing for no-tools agents.** When `tools_payload` is `None`,
  the existing producer already makes one non-streaming "detect" call
  (`maybe_tool_call` with the tools key omitted), gets no tool calls, breaks,
  and streams — identical to today's plain-chat turn (`maybe_tool_call`
  docstring, `app/ollama.py:357`). The Content Generator reuses that shape
  unchanged; it just carries the agent's system prompt.
- `model` arriving at `_run_generation` is already the **effective** model (the
  route computes `agent.model if agent else conversation.model`).
- History is unchanged: `_build_history_payload` transcodes the whole
  conversation, so a later agent sees earlier agents' assistant text and tool
  rows — the hand-off.

**Routes** (`app/routes.py`), resolving the agent and passing overrides:
- `create_chat_endpoint`: add `agent: Annotated[str | None, Form()] = None`.
  `spec = get_agent(agent)`; persist `active_agent=spec.name if spec else None`;
  spawn with `model=spec.model if spec else chat.model`,
  `system_prompt_override=spec.system_prompt if spec else None`,
  `tool_allowlist=spec.tools if spec else None`.
- `send_message_endpoint` and the regenerate endpoint: `spec =
  get_agent(conversation.active_agent)` and pass the same overrides +
  effective model.
- **New** `POST /chats/{id}/agent` (form `agent`): validate via `get_agent`,
  `set_active_agent`, return a small fragment with two OOB swaps —
  - the header indicator (`#agent-indicator-{id}`);
  - the chip bar (`#chat-tool-chips`): when switching **to Normal**, recompute
    `_chip_states` and re-render `_tool_chips.html` so the real chips return;
    when an agent is active, replace its content with the agent's read-only
    tool list (or empty). (Targeting a missing `#chat-tool-chips` — non-tool
    model — is a harmless HTMX no-op.)
- The `GET /settings` route's template context loses the agentic toggle flags
  (`agentic_mode`, `review_enabled`, `generator_enabled`) and the agentic-prompt
  strings, alongside deleting the three toggle endpoints.

---

## Part E — UI

- **`templates/_agent_select.html`** (new, shared partial): a `<select
  name="agent">` with a "Normal" option (value `""`) + one option per
  `list_agents()`. v1 renders all options enabled (no per-render Ollama call);
  an unavailable model is handled at invocation by the existing error path (see
  Edge cases).
- **`templates/_agent_indicator.html`** (new): header element
  `id="agent-indicator-{id}"`. Normal → `model: {{ conversation.model }}` (as
  today). Agent → `Agent: {{ label }} ({{ agent.model }})`.
- **`templates/_chat_panel.html`**: replace the static `chat-panel__model` span
  (l.8) with the indicator partial. Add the agent picker as **its own `<form>`
  sibling immediately above `.message-form`** (NOT nested inside it), carrying
  `hx-post="/chats/{id}/agent" hx-trigger="change" hx-swap="none"` — mirroring
  the header temperature chip-form. Nesting it inside `.message-form` would let
  that form's `hx-post="/messages"` / `hx-target="#messages"` inherit onto the
  `<select>` (the HTMX attribute-inheritance gotcha in CLAUDE.md). Selected
  option = `conversation.active_agent`. When an agent is active, the
  `#chat-tool-chips` content is hidden (agent allowlist governs instead).
- **`templates/_composer.html`** (new-chat): add `_agent_select.html` to the
  left of the model select in `.composer__toolbar`.
- **`static/app.js`**: on `#composer-agent` change, lock/grey the model select
  when value ≠ `""` (CSS-disabled — keep it submittable so the chat still
  persists a Normal model; a real `disabled` select submits nothing). No JS for
  the in-chat picker (HTMX handles it).
- **`static/style.css`**: `.message-form__toolbar` layout; greyed/locked model
  select style.

Temperature and tool-cap still apply to agent turns (reuse the chat's values).

---

## Prompts (`app/agents/prompts.py`)

**RESEARCH_AGENT_PROMPT**
> You are the Research agent. Your job is to gather accurate information and
> report it clearly — not to produce the user's final polished deliverable.
>
> You have tools: a clock and a retrieval tool over the user's configured
> knowledge sources. Use them when they materially help — when a question
> depends on those sources, call the retrieval tool to ground your findings
> rather than relying on memory. Prefer several targeted queries over one broad
> one, and cite the specific source (title/section) for each fact you pull. Do
> not call tools speculatively or for things you already know.
>
> When you have enough material, stop calling tools and write a clear,
> well-organized findings summary: the key facts with their sources, plus any
> gaps, uncertainties, or contradictions. Keep it factual and skimmable — the
> user may next invoke the Content Generator to turn your findings into a
> finished piece, so make them easy to build on.

**CONTENT_GENERATOR_PROMPT**
> You are the Content Generator agent. Your job is to turn the conversation so
> far into a polished, well-structured piece of writing for the user.
>
> You have no tools — work entirely from the conversation, which may include
> research findings produced earlier by the Research agent. Synthesize the
> relevant material into a clear, coherent deliverable. Follow the user's
> instructions on format, length, audience, and tone; if unspecified, choose
> sensible defaults and clean markdown structure. Ground everything in the
> conversation — do not invent facts; if something important is missing, say so
> plainly. Produce final-quality output: no meta-commentary about being an
> agent, no filler.

---

## Edge cases

- **Agent model not installed** → the Ollama call fails and the existing
  `error` SSE path surfaces it as an error bubble. (v1 does not pre-disable the
  picker option — see Out of scope.)
- **Agent has tools but its model isn't tool-capable** → `model_supports_tools`
  gate drops tools (`tools_payload = None`); the agent still runs with its
  prompt (degrade, not crash). Not a concern for the shipped roster
  (`qwen3.5:9b` is tool-capable).
- **`active_agent` references a removed agent** → `get_agent` → `None` → Normal.

## Out of scope (v1) — noted for later

- Disabling/greying picker options whose model isn't installed (needs an
  installed-model lookup at panel-render time; the error path covers it for now).
- Per-message agent attribution in history (which agent produced each bubble).
- Per-agent temperature / tool-iteration cap (agents reuse the chat's values).
- Automatic agent chaining (the user invokes agents by hand, sequentially).

---

## Tests

- `tests/test_agents.py` (repurposed): `AgentSpec` fields; `AGENTS` contents
  (research has `query_rag`, content_generator empty); `get_agent`
  None/unknown/valid; `list_agents` order.
- `tests/test_queries.py`: `active_agent` defaults None; `set_active_agent`
  set/clear; round-trips through `get_conversation`.
- `tests/test_db.py`: migration adds `active_agent` to a pre-existing DB.
- `tests/test_generation.py`: agent path injects the agent prompt + filters
  tools to the allowlist; empty allowlist → no tools but prompt still injected;
  Normal path byte-identical to today.
- `tests/test_routes.py`: `POST /chats/{id}/agent` sets `active_agent` + returns
  the indicator; invalid agent → Normal; create-chat-with-agent persists it;
  send/regenerate use the active agent's model/prompt/tools.
- `tests/test_render.py` / template tests: indicator renders Normal vs agent
  variant; both composers include the picker; in-chat picker sits in its own
  form (not inside `.message-form`).
- `tests/test_integration.py`: end-to-end hand-off — invoke Research (mock
  Ollama returns a tool call then findings), switch to Content Generator, send,
  assert no tools offered and that prior findings are in the payload.

Target: keep coverage ~97%+ on `app/` + `main.py` (no regression).

---

## Verification

1. `pytest --cov=app --cov=main --cov-report=term-missing` — green, no coverage
   regression.
2. **Browser smoke test** (`uvicorn main:app --reload`, per CLAUDE.md — required
   for UI):
   - New chat: picker shows Normal / Research / Content Generator. Pick Research
     → model select greys. Send a query → streams from `qwen3.5:9b` with tool
     access; header shows "Agent: Research (qwen3.5:9b)".
   - Switch to Content Generator mid-chat → header updates, chips hide; send
     "write this up" → it synthesizes the research findings, calls no tools.
     (Confirm Ollama accepts the prior tool-role history with no tools offered;
     if it rejects, strip tool rows for no-tool agents — not pre-built.)
   - Switch back to Normal → pinned model resumes, chips reappear.
   - Reload mid-chat → picker + indicator reflect the persisted `active_agent`.
   - `/settings` no longer shows agentic toggles; RAG-server manager intact.

---

## Critical files

| File | Change |
|---|---|
| `app/agents/__init__.py` | Rewrite → `AgentSpec`, `AGENTS`, `get_agent`, `list_agents` |
| `app/agents/prompts.py` | Replace loop prompts with the two agent prompts |
| `app/agents/loop.py`, `verdict_tools.py` | **Delete** |
| `app/generation.py` | `system_prompt_override` + `tool_allowlist` in `start_generation`/`_run_generation`; strip agentic branch from `_build_history_payload` |
| `app/routes.py` | Resolve agent in create/send/regenerate; new `POST /chats/{id}/agent`; delete agentic settings endpoints + `_compute_agentic_skipped` |
| `app/queries.py` | `Conversation.active_agent`; `set_active_agent`; drop agentic settings + roles |
| `app/db.py` | `active_agent` column + migration |
| `app/render.py` | Drop agentic blocks + cap helper |
| `templates/_agent_select.html`, `_agent_indicator.html` | **New** |
| `templates/_chat_panel.html`, `_composer.html`, `_assistant_placeholder.html`, `_settings.html` | Picker/indicator wiring; remove agentic UI |
| `static/app.js`, `static/style.css` | New-chat model greying; toolbar styles |
