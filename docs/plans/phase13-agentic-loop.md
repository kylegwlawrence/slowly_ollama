# Phase 13 — Agentic multi-agent workflow

Executable implementation plan. Style matches `phase12-tool-calling-detail.md`:
code snippets are verbatim where it matters, exact file paths called out,
test specs included. Another agent (or future-you) should be able to ship
from this without re-deriving design decisions.

If this file disagrees with anything earlier in `docs/plans/`, **this file
wins** — it's the most recent and reflects the decision pass that
produced it.

---

## Why this exists

The current single-agent tool loop (`_run_generation` in `app/generation.py`)
hands one model both "do tool calls" and "write the final answer". For
genuinely research-shaped questions this often produces shallow or
incomplete answers — the model stops calling tools too early, doesn't
self-critique, and never circles back to fill gaps.

Phase 13 introduces an *opt-in* three-agent loop:

1. **Research agent** — accepts the user message, runs tool calls,
   produces *findings* (a free-text synthesis of what it learned).
2. **Review agent** — judges the findings against the original
   question and either approves them (calls `mark_passed`) or sends
   them back with specific feedback (calls `request_more_research`).
3. **Generation agent** — when review approves, writes the final
   answer for the chat from the findings + the user's question.

Loop cap: **3 iterations** of research↔review. On the 4th attempt the
loop force-generates from whatever findings exist (badged in the UI).

All three agents use the same model (the conversation's pinned model).
The loop is gated by a single global toggle in `/settings`; off by
default, so the existing single-agent path is the baseline.

---

## Locked decisions

Confirmed via the planning conversation that produced this file.

- **Activation:** one global toggle in `/settings` (`agentic_mode = on|off`).
  Off by default. When on, every assistant turn runs the loop.
- **Models:** single chat-pinned model for all three agents (no
  per-agent overrides in v1).
- **Verdict mechanism:** review agent calls one of two tools —
  `mark_passed(reason)` or `request_more_research(feedback)`. Tools
  are registered in a *separate* registry that's only advertised to
  the review agent. Research never sees them.
- **Generation input:** synthesized findings from the last research
  pass + the original user message. Generation does NOT see the
  inter-agent chatter, raw tool results, or prior chat history.
- **Loop cap:** 3 research↔review iterations. On exceed, force the
  generation pass anyway with whatever findings exist; UI badges the
  card as "max iterations reached".
- **Per-pass tool cap:** research can make up to 5 tool calls per
  pass (matches existing single-agent ceiling).
- **Tool access:** Research = full registry; Review = verdict
  registry only; Generation = no tools.
- **Prompts:** hardcoded constants in `app/agents/prompts.py`.
  Iterated via code edits; surface them read-only in `/settings`.
- **Streaming:** tool-call/tool-result rows stream live (as today).
  Findings + verdicts emit as single events (Ollama tool-mode
  outputs are non-streaming; full token-by-token streaming for those
  is a v2). Generation streams token-by-token (unchanged).
- **No-tools fallback:** if the chat's pinned model has no `tools`
  capability, the agentic toggle silently degrades to single-agent
  mode AND the final assistant message renders with a small badge
  "agentic mode skipped (model has no tools)".
- **Retry context:** when review fails, the next research pass sees
  the full cumulative history of prior tool calls/results + a new
  `user`-role message carrying the review's feedback. No reset.
- **Card layout:** one `<details>` tool-card per assistant turn (same
  as today). Inside: iteration headers + nested rows for tool
  calls, findings, and verdicts. New block type
  `AgenticToolBatchBlock` for historic render.
- **Resumability:** *not resumable in v1.* A page reload mid-loop
  surfaces "(response interrupted)" via the existing safety net;
  the user re-sends. v2 if needed.
- **Title generation:** unchanged — `_maybe_emit_title` fires after
  the generation agent's final assistant row lands.
- **Settings UI:** toggle + read-only `<pre>` blocks of the three
  system prompts, expanded when toggle is on.
- **Agent context (per agent):**
  - Research: full prior chat history (user + assistant rows only;
    no tool-call detritus from prior turns) + current user message
    + intra-turn iteration state.
  - Review: system prompt + a single user message bundling the
    original question and the latest findings. No history.
  - Generation: system prompt + a single user message bundling the
    original question and the final findings. No history.

---

## Architecture overview

```
User message
    │
    ▼
┌───────────────────────────────────────────────────────────┐
│ _run_agentic_generation (NEW; called when toggle on AND   │
│ model has 'tools' capability)                             │
│                                                           │
│  for iter in 1..3:                                        │
│    ┌─────────────────────────────────────────────┐        │
│    │ RESEARCH PASS                                │        │
│    │ - history: full chat history + current user │        │
│    │   + (if iter > 1) feedback as user message  │        │
│    │ - tools: full registry                       │        │
│    │ - inner loop: maybe_tool_call up to 5 times  │        │
│    │   * persist tool_call / tool_result rows     │        │
│    │   * SSE tool-call / tool-result events       │        │
│    │ - on text-only response: capture as findings │        │
│    │   * persist research_findings row            │        │
│    │   * SSE research-findings event              │        │
│    └─────────────────────────────────────────────┘        │
│                  │                                        │
│                  ▼                                        │
│    ┌─────────────────────────────────────────────┐        │
│    │ REVIEW PASS                                  │        │
│    │ - history: ephemeral [system + 1 user msg]   │        │
│    │ - tools: verdict registry only               │        │
│    │ - maybe_tool_call once                       │        │
│    │   * expect mark_passed OR                    │        │
│    │     request_more_research tool call          │        │
│    │ - persist review_verdict row (JSON)          │        │
│    │ - SSE review-verdict event                   │        │
│    └─────────────────────────────────────────────┘        │
│                  │                                        │
│         passed? ─┴── yes ──▶ break                        │
│                  │                                        │
│                  no                                       │
│                  │                                        │
│         continue loop                                     │
│                                                           │
│  ┌─────────────────────────────────────────────┐          │
│  │ GENERATION PASS (after break or 3-cap)      │          │
│  │ - history: ephemeral [system + 1 user msg]  │          │
│  │   bundling user question + final findings   │          │
│  │ - tools: none                                │          │
│  │ - stream_chat → SSE token events             │          │
│  │ - persist assistant row                      │          │
│  └─────────────────────────────────────────────┘          │
│                  │                                        │
│  _maybe_emit_title (unchanged)                            │
│                  │                                        │
│  SSE done event                                           │
└───────────────────────────────────────────────────────────┘
```

Existing `_run_generation` is **untouched** — it's still the single-agent
path. The branch happens in a new dispatcher (`start_generation` learns to
pick the right producer based on the toggle + model capability).

---

## Schema changes

### New table: `app_settings`

Simple key-value store for global app settings. Sized for ~10 settings
ever; no plans for it to grow beyond hundreds.

```sql
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

Phase 13's only key is `agentic_mode` with values `"on"` or `"off"`.
Default (when row absent): `"off"`. Future settings (cap overrides,
prompt overrides) reuse the table.

Migration: idempotent `CREATE TABLE IF NOT EXISTS`. No data backfill
needed — absence means default.

### New message roles

Two new values for the `Role` literal in `app/queries.py`:

- `research_findings` — content is plain text (the research agent's
  free-form synthesis after its tool-calling pass ends).
- `review_verdict` — content is JSON:
  `{"verdict": "passed"|"failed", "message": "..."}`.

The DB schema has no role CHECK (dropped in 12a), so no SQL migration
required. Python-side: extend `Role` and update render-layer grouping
to recognize them.

---

## New module: `app/agents/`

```
app/agents/
├── __init__.py        # re-exports
├── prompts.py         # RESEARCH_SYSTEM_PROMPT, REVIEW_SYSTEM_PROMPT, GENERATION_SYSTEM_PROMPT
├── verdict_tools.py   # mark_passed, request_more_research + REVIEW_TOOL_SPECS
├── loop.py            # _run_agentic_generation orchestrator
```

Why a new top-level module instead of folding into `app/tools/`:
the verdict "tools" are not real tools (they never execute work —
they're a verdict-encoding hack on top of the tool-call API). Keeping
them separate from `app/tools/` avoids polluting the global registry
that research can see.

---

## Sub-phase 13a — Schema + settings storage

### Changes

- `app/db.py`: add `app_settings` table to `_SCHEMA_SQL`.
- `app/queries.py`: extend `Role` literal; add `get_setting`,
  `set_setting`, `get_agentic_mode`, `set_agentic_mode` helpers.
- `app/generation.py`: teach `_build_history_payload` to skip
  `research_findings` and `review_verdict` rows. The Role expansion
  in `queries.py` makes those rows legal to insert; this update
  keeps the title-generation path (which calls
  `_build_history_payload` via `_maybe_emit_title`) from shipping
  them to Ollama under unrecognized role names.
- `tests/test_db.py`: confirm `app_settings` table exists after init.
- `tests/test_queries.py`: round-trip the helpers; verify default
  when key missing.
- `tests/test_generation.py`: extend the existing
  `test_build_history_payload_*` cases with one that confirms
  `research_findings` and `review_verdict` rows are dropped from
  the wire-format output.

### Code: `app/db.py`

Append to `_SCHEMA_SQL`:

```python
-- Phase 13: global key/value app settings. One row per setting.
-- Currently the only key is `agentic_mode` ("on" or "off"); future
-- settings reuse the table. No schema migration needed when adding
-- new keys — they appear/disappear via INSERT/DELETE.
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

No migration helper needed — `CREATE TABLE IF NOT EXISTS` covers
existing DBs (the table is purely additive).

### Code: `app/queries.py`

Extend the Role literal:

```python
Role = Literal[
    "user",
    "assistant",
    "tool_call",
    "tool_result",
    "research_findings",  # phase 13
    "review_verdict",     # phase 13
]
```

Add settings helpers at the bottom of the file:

```python
# ---------------------------------------------------------------------------
# Settings (phase 13)
# ---------------------------------------------------------------------------


def get_setting(
    conn: sqlite3.Connection, key: str, default: str | None = None
) -> str | None:
    """Read a single app_settings row by key.

    Args:
        conn: Open SQLite connection.
        key: Setting key (e.g. ``"agentic_mode"``).
        default: Returned when no row exists for the key.

    Returns:
        The stored value as a string, or ``default`` when the key
        hasn't been set.
    """
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?;", (key,)
    ).fetchone()
    return row["value"] if row is not None else default


def set_setting(
    conn: sqlite3.Connection, key: str, value: str
) -> None:
    """Upsert one app_settings row.

    Wraps the write in ``with conn:`` so the upsert lands atomically.

    Args:
        conn: Open SQLite connection.
        key: Setting key.
        value: Setting value as a string.
    """
    with conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
            (key, value),
        )


_AGENTIC_MODE_KEY = "agentic_mode"


def get_agentic_mode(conn: sqlite3.Connection) -> bool:
    """Return True when the multi-agent loop is enabled globally.

    Default (no row): False. Any value other than the literal string
    ``"on"`` also returns False, defensively.

    Args:
        conn: Open SQLite connection.
    """
    return get_setting(conn, _AGENTIC_MODE_KEY, default="off") == "on"


def set_agentic_mode(conn: sqlite3.Connection, enabled: bool) -> None:
    """Toggle the global agentic-mode setting.

    Args:
        conn: Open SQLite connection.
        enabled: True for ``"on"``, False for ``"off"``.
    """
    set_setting(conn, _AGENTIC_MODE_KEY, "on" if enabled else "off")
```

### Code: `app/generation.py`

Insert into `_build_history_payload`, between the existing
`tool_result` branch and the `else` fallback:

```python
        elif m.role in ("research_findings", "review_verdict"):
            # Phase 13 internal artifacts of the agentic loop. They
            # belong to the tool-card UI and to the orchestrator's
            # per-iteration history (built separately in
            # app/agents/loop.py), NOT to the wire-format history we
            # ship to Ollama for unrelated calls like title
            # generation. Drop them silently so a chat that used
            # agentic mode still title-generates cleanly.
            continue
```

The orchestrator builds its own per-agent payloads (see sub-phase
13d) and never goes through `_build_history_payload`, so dropping
these rows here is safe.

### Tests: `tests/test_db.py`

Add one case to confirm the table exists after init:

```python
def test_initialize_database_creates_app_settings_table(tmp_path: Path) -> None:
    """Phase 13: app_settings table is present after first init."""
    db_file = tmp_path / "chats.db"
    initialize_database(db_file)
    with sqlite3.connect(db_file) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name='app_settings';"
        ).fetchall()
    assert len(rows) == 1
```

### Tests: `tests/test_queries.py`

Round-trip the new helpers:

```python
def test_get_setting_returns_default_when_missing(db) -> None:
    """Unset keys come back as the supplied default."""
    assert queries.get_setting(db, "nonexistent", default="fallback") == "fallback"
    assert queries.get_setting(db, "nonexistent") is None


def test_set_setting_upserts(db) -> None:
    """Repeated set_setting calls overwrite the previous value."""
    queries.set_setting(db, "k", "v1")
    assert queries.get_setting(db, "k") == "v1"
    queries.set_setting(db, "k", "v2")
    assert queries.get_setting(db, "k") == "v2"


def test_agentic_mode_default_off(db) -> None:
    """No row → agentic mode is off."""
    assert queries.get_agentic_mode(db) is False


def test_agentic_mode_round_trip(db) -> None:
    """Toggle on, toggle off, both observable."""
    queries.set_agentic_mode(db, True)
    assert queries.get_agentic_mode(db) is True
    queries.set_agentic_mode(db, False)
    assert queries.get_agentic_mode(db) is False
```

---

## Sub-phase 13b — Agent module scaffold + prompts

### Changes

- Create `app/agents/__init__.py`, `app/agents/prompts.py`.
- `tests/test_agents.py`: smoke-test that the three prompts are
  non-empty strings and that the module imports cleanly.

### Code: `app/agents/__init__.py`

```python
"""Phase 13: multi-agent research → review → generation loop.

Three discrete agents, each invoked via Ollama with its own system
prompt and tool scope:

- **research** sees full chat history + the current user message and
  has access to every registered tool. It runs a tool-calling
  inner loop until it stops calling tools; the last text response
  is captured as the iteration's "findings".
- **review** sees only the original user message and the latest
  findings, with two custom tools (mark_passed,
  request_more_research) that encode its verdict.
- **generation** sees only the original user message and the final
  findings, no tools. It writes the final assistant response.

The orchestrator (`loop._run_agentic_generation`) wires them
together with a 3-iteration cap. See
`docs/plans/phase13-agentic-loop.md` for the architecture diagram.
"""

from app.agents.prompts import (
    GENERATION_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
)

# NOTE: `app.agents.verdict_tools` is deliberately NOT re-exported
# from this package's __init__. The commit order in
# docs/plans/phase13-agentic-loop.md lets 13b (this module + prompts)
# ship before 13c (verdict_tools); pulling verdict_tools into the
# package surface would couple the two and force them into a single
# commit. Call sites import directly:
#
#     from app.agents.verdict_tools import REVIEW_TOOL_SPECS, parse_verdict

__all__ = [
    "GENERATION_SYSTEM_PROMPT",
    "RESEARCH_SYSTEM_PROMPT",
    "REVIEW_SYSTEM_PROMPT",
]
```

### Code: `app/agents/prompts.py`

Prompts are deliberately conservative for v1 — short, direct, no
chain-of-thought cargo cult. Refine via code edits + retros.

```python
"""Phase 13: system prompts for the three agentic-loop agents.

Hardcoded constants. Iterated via code edits — there is no UI to
override them (read-only display only). When prompt quality is
limiting answer quality, edit here and ship a follow-up phase.

Per the locked decisions in docs/plans/phase13-agentic-loop.md:
- Research sees full chat history + current user message.
- Review sees only the original user message + latest findings.
- Generation sees only the original user message + final findings.

Each prompt is a single string; we pass it as the `system`-role
message at the start of the agent's Ollama call.
"""


RESEARCH_SYSTEM_PROMPT = """You are the research agent in a three-agent system designed to answer the user's question carefully and accurately.

Your job:
1. Read the user's question and the conversation history for context.
2. Use the available tools to gather information needed to answer the question. Prefer multiple targeted queries over one broad one. Cite specific sources when you find them.
3. When you have enough material, stop calling tools and write a concise "findings" summary in plain prose. The findings should:
   - State the key facts you gathered, with their sources where applicable.
   - Note any gaps, uncertainties, or contradictions you ran into.
   - NOT attempt to answer the user directly — your downstream review and generation agents handle that. You are producing raw research, not the final answer.

If a review agent later sends feedback that your findings were insufficient, you will receive that feedback as a follow-up user message in the same conversation. Use it to direct further tool calls — do not repeat queries you already ran.

You have up to 5 tool calls per research pass. Use them well."""


REVIEW_SYSTEM_PROMPT = """You are the review agent in a three-agent system. You do not answer the user — you judge research quality.

You will receive the user's original question and the research agent's "findings". Your job:
1. Decide if the findings are sufficient to write a complete, accurate answer to the user's question.
2. Call EXACTLY ONE of these tools:
   - mark_passed(reason): findings are sufficient. Briefly state what makes them sufficient.
   - request_more_research(feedback): findings are insufficient. Give the research agent SPECIFIC, ACTIONABLE feedback on what's missing or wrong. Generic notes like "do more research" are not useful.

Be honest but not picky. The goal is a good answer, not a perfect one. If the findings cover the question reasonably well and the user would be satisfied, call mark_passed. If a key fact is missing or wrong, call request_more_research.

Do not write any prose — only the tool call. Do not call any tool other than mark_passed or request_more_research."""


GENERATION_SYSTEM_PROMPT = """You are the generation agent in a three-agent system. The research and review agents have already done their work — your job is to write the final answer to the user.

You will receive the user's original question and a set of "findings" from the research agent that have been approved by the review agent. Use them to write a direct, clear answer:
- Address the user's question head-on.
- Cite specific sources from the findings where relevant.
- Do not mention that you are part of a multi-agent system or that someone did "research" — just answer.
- Do not invent facts. If the findings don't cover something, say so plainly rather than guessing.
- Keep the tone helpful and matter-of-fact. No filler ("Great question!") and no hedging beyond what the findings warrant.

Write in well-structured prose or markdown as appropriate to the question."""
```

### Tests: `tests/test_agents.py`

```python
"""Phase 13: smoke tests for the agentic-loop module."""

from app import agents


def test_prompts_are_non_empty_strings() -> None:
    """Each prompt is a multi-line string with non-trivial content."""
    for prompt in (
        agents.RESEARCH_SYSTEM_PROMPT,
        agents.REVIEW_SYSTEM_PROMPT,
        agents.GENERATION_SYSTEM_PROMPT,
    ):
        assert isinstance(prompt, str)
        assert len(prompt.strip()) > 100  # not a placeholder


def test_review_prompt_names_both_verdict_tools() -> None:
    """The review prompt must mention both tool names verbatim — the
    model needs to know what to call. Catches accidental renames in
    one place but not the other."""
    p = agents.REVIEW_SYSTEM_PROMPT
    assert "mark_passed" in p
    assert "request_more_research" in p
```

---

## Sub-phase 13c — Verdict tools (separate registry)

### Changes

- Create `app/agents/verdict_tools.py`.
- The verdict tools are NOT registered in the global `TOOLS` dict.
  We build their Ollama spec manually so they're invisible to the
  research agent and to any future tool consumers.
- `tests/test_agents.py`: validate the spec shape; test `parse_verdict`.

### Why not the global registry

The decorator-based `@tool` in `app/tools/__init__.py` puts every
registered tool into `TOOLS` — a module-level dict the research
agent reads via `tool_specs_for_ollama()`. If we registered the
verdict tools there, the research agent would see them and could
*call them itself* (mid-research the model decides "I'm done" and
calls `mark_passed`). That would short-circuit the loop in a way
the design specifically rules out.

The verdict tools are also not "tools" in the executable sense —
they have no body to run. They're a structured-output trick on top
of Ollama's tool-call API. Treating them as a separate concept,
not registered alongside `current_time` / `query_rag`, reflects
their actual role.

### Code: `app/agents/verdict_tools.py`

```python
"""Phase 13: review-agent verdict tools.

The review agent expresses its pass/fail verdict by calling one of two
"tools" we advertise to it. They aren't real tools — they have no
body. They're a structured-output mechanism on top of Ollama's
tool-call protocol: the model is already trained to call tools
reliably, so encoding the verdict as a tool call is more robust
than asking for JSON in free-form text.

These specs are deliberately NOT registered in `app.tools.TOOLS` —
that registry is what the research agent reads via
`tool_specs_for_ollama()`. Keeping these specs separate ensures the
research agent never sees them and can't short-circuit the loop by
"marking" its own findings.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class VerdictDecision:
    """Decoded review verdict.

    Attributes:
        verdict: ``"passed"`` when the review agent called
            ``mark_passed``; ``"failed"`` when it called
            ``request_more_research`` (or when neither tool was
            called — see :func:`parse_verdict`).
        message: The reason / feedback string the model passed as
            its argument. May be empty if the model called the tool
            without an argument.
    """

    verdict: Literal["passed", "failed"]
    message: str


# Ollama tool-spec shape; mirrors what `tool_specs_for_ollama` produces
# but built by hand because these tools have no Python body to inspect.
# Keep `additionalProperties: false` so the model can't smuggle in
# extra args we'd silently ignore.
# Shape matches `tool_specs_for_ollama()` output for the @tool-decorated
# tools: no `additionalProperties: false`. Adding stricter schema flags
# here would diverge from the existing tool-spec convention and hasn't
# been tested against the model fleet — `parse_verdict` already ignores
# unrecognized arguments, so the stricter constraint isn't load-bearing.
REVIEW_TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "mark_passed",
            "description": (
                "Call this when the research findings are sufficient"
                " to answer the user's question. Pass a brief reason."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": (
                            "Short explanation of why the findings"
                            " are sufficient."
                        ),
                    },
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_more_research",
            "description": (
                "Call this when the research findings are NOT yet"
                " sufficient. Pass specific, actionable feedback on"
                " what's missing or wrong."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "feedback": {
                        "type": "string",
                        "description": (
                            "Specific feedback for the research"
                            " agent's next pass. Avoid generic"
                            " 'do more research' notes."
                        ),
                    },
                },
                "required": ["feedback"],
            },
        },
    },
]


def parse_verdict(tool_calls: list[dict]) -> VerdictDecision:
    """Map a list of tool_calls from `maybe_tool_call` to a verdict.

    Rules (defensive — the model may misbehave):
    1. If any call is ``mark_passed``, treat as ``"passed"``; use its
       ``reason`` arg as the message.
    2. Else if any call is ``request_more_research``, treat as
       ``"failed"``; use its ``feedback`` arg as the message.
    3. Else (no calls, or unrecognized calls): treat as ``"failed"``
       with a fallback message asking for retry. This keeps the loop
       moving when the model ignores instructions.

    Args:
        tool_calls: Output of ``ollama.maybe_tool_call``'s first
            tuple element — a list of ``{"name", "arguments"}`` dicts.

    Returns:
        A :class:`VerdictDecision`.
    """
    for call in tool_calls:
        if call.get("name") == "mark_passed":
            reason = (call.get("arguments") or {}).get("reason", "")
            return VerdictDecision(verdict="passed", message=str(reason))
    for call in tool_calls:
        if call.get("name") == "request_more_research":
            feedback = (call.get("arguments") or {}).get("feedback", "")
            return VerdictDecision(verdict="failed", message=str(feedback))
    return VerdictDecision(
        verdict="failed",
        message=(
            "Review agent did not call a verdict tool. Continue"
            " researching."
        ),
    )
```

### Tests: extend `tests/test_agents.py`

Import verdict_tools directly (not via the `app.agents` package
re-exports) — see the note in 13b's `__init__.py` about why
verdict_tools isn't re-exported.

```python
from app.agents import verdict_tools


def test_review_tool_specs_have_correct_names() -> None:
    """Both verdict tools are present with the names the prompt references."""
    names = {
        spec["function"]["name"] for spec in verdict_tools.REVIEW_TOOL_SPECS
    }
    assert names == {"mark_passed", "request_more_research"}


def test_parse_verdict_passed() -> None:
    """mark_passed → VerdictDecision(verdict='passed', message=reason)."""
    calls = [
        {"name": "mark_passed", "arguments": {"reason": "looks good"}}
    ]
    d = verdict_tools.parse_verdict(calls)
    assert d.verdict == "passed"
    assert d.message == "looks good"


def test_parse_verdict_failed() -> None:
    """request_more_research → VerdictDecision(verdict='failed', ...)."""
    calls = [
        {
            "name": "request_more_research",
            "arguments": {"feedback": "missing source citations"},
        }
    ]
    d = verdict_tools.parse_verdict(calls)
    assert d.verdict == "failed"
    assert d.message == "missing source citations"


def test_parse_verdict_passed_wins_over_failed() -> None:
    """If the model calls both, mark_passed takes precedence."""
    calls = [
        {"name": "request_more_research", "arguments": {"feedback": "x"}},
        {"name": "mark_passed", "arguments": {"reason": "y"}},
    ]
    assert verdict_tools.parse_verdict(calls).verdict == "passed"


def test_parse_verdict_no_verdict_tools_falls_through() -> None:
    """No recognized tool call → treat as failed with a default message."""
    d = verdict_tools.parse_verdict([])
    assert d.verdict == "failed"
    assert "did not call" in d.message
    d2 = verdict_tools.parse_verdict(
        [{"name": "random_tool", "arguments": {}}]
    )
    assert d2.verdict == "failed"
```

---

## Sub-phase 13d — Orchestrator (`_run_agentic_generation`)

This is the central piece. It lives alongside `_run_generation` in a
new file `app/agents/loop.py`, and `app/generation.py`'s
`start_generation` learns to dispatch between the two.

### Design notes

- **History building for each agent is per-agent and per-iteration.**
  We do NOT reuse `_build_history_payload` from `app/generation.py`
  for the agentic calls — it folds tool calls and assistant rows
  into the Ollama wire format intended for resumable single-agent
  flow. Instead, the orchestrator builds three small payload
  helpers:
  - `_build_research_payload(conv_id, db, current_iteration_messages, feedback)`:
    system + filtered prior turns (user+assistant only) + the
    current turn's intra-iteration messages + optional feedback
    user message.
  - `_build_review_payload(user_message, findings)`:
    system + one user message bundling both.
  - `_build_generation_payload(user_message, findings)`:
    system + one user message bundling both.
- **Findings come from `maybe_tool_call`'s `content` return.** When
  research's tool-calling inner loop sees `tool_calls=[]`, the
  paired `content` string is the findings. We don't do a separate
  `stream_chat` call for findings in v1 — that would cost an extra
  round trip and the findings aren't user-visible prose anyway.
  Findings emit as a single SSE event.
- **Card uses a new turn-id format.** Existing `turn_id =
  str(time.monotonic_ns())` stays; the agentic card uses the same
  scheme. Iteration boundaries are encoded in row ids:
  `tool-card-{turn_id}-iter-{N}-row-{M}`. The `-iter-{N}` segment
  is also a CSS hook for the iteration grouping.
- **Force-generate fallback at iteration 4.** After three failed
  review passes we fall through to generation with whatever findings
  the last research pass produced. The card gets a
  `data-max-iterations` attribute the CSS targets to show a
  "(max iterations reached)" badge.
- **Inner tool-call cap is a fresh constant.** Define
  `_RESEARCH_TOOL_CAP_PER_PASS = 5` in `app/agents/loop.py`. Do NOT
  reuse `app/generation.py:_TOOL_ITERATION_CAP` — that one is the
  single-agent per-turn cap (all calls in one assistant reply); the
  agentic per-pass cap is a different concept that just happens to
  share the same number today. Aliasing them couples future
  changes; e.g., raising single-agent to 8 would silently raise
  agentic to 8 per pass × 3 iterations = 24 tool calls per turn.
- **Per-pass inner-cap behaviour.** If a single research pass hits
  its inner cap, treat the pass as "done research" with whatever
  findings the model has produced so far (or a synthesized empty-
  findings string if it never produced text) and hand off to
  review. Don't bail the whole agentic turn — review can request
  more research.
- **Iteration cap constant lives in `app/agents/loop.py`** as
  `_AGENTIC_ITERATION_CAP = 3` and is imported by `app/render.py`
  (sub-phase 13f) so historic render and the orchestrator share
  one definition. Don't hardcode `3` in render.
- **Summary state machine.** The tool-card summary span (id
  `{card_id}-summary`) cycles through three states, each driven
  by a small OOB swap on that span (NOT on the whole card):
    - Initial: `"researching…"` — emitted with the empty card shell.
    - Per-iteration: `"researching (iteration N)…"` — emitted at
      the top of each iteration alongside the iteration-header
      row.
    - Final: `"ran N iterations"` or `"ran N iterations (max
      reached)"` — emitted in the `done` event's OOB bundle.
  The `data-max-iterations="true"` attribute on the outer
  `<details>` is set via a tiny OOB swap on a sentinel `<span
  id="…-max-marker">` placed inside the summary at shell-render
  time. See sub-phase 13d's notes on the swap mechanics — do NOT
  emit a full `<details>` outerHTML replacement; that would clobber
  rows mid-stream.

### Code: `app/agents/loop.py`

> **Note for the implementer:** the snippet below is the structural
> spine, not a finished implementation. It exists to anchor the
> exact event names, SSE payload shape, persistence calls, and
> control flow. Fill in template renders, SSE shells, and the inner
> tool-call body by mirroring the equivalent sections of
> `app/generation.py:_run_generation`. Do **not** invent new SSE
> event names — reuse `tool-call`, `tool-result`, `token`,
> `title`, `done`, `error`, plus three NEW events introduced here:
> `research-findings`, `review-verdict`, `iteration-start`.

```python
"""Phase 13: orchestrator for the agentic multi-agent loop.

Coexists with `app/generation.py:_run_generation` — the
single-agent producer. The dispatcher in `start_generation` picks
between them based on the `agentic_mode` setting and the chat's
model capability.

Same protocol as `_run_generation`:
- Drives a `GenerationState`; never yields directly.
- Emits SSE events via `_emit`.
- Persists every step as message rows.
- Marks `state.done = True` in a `finally`.
"""

import asyncio
import html
import json
import logging
import sqlite3
import time
from typing import Literal

import httpx

from app import ollama, queries
from app.agents.prompts import (
    GENERATION_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
)
from app.agents.verdict_tools import (
    REVIEW_TOOL_SPECS,
    VerdictDecision,
    parse_verdict,
)
from app import render
from app.generation import (
    GenerationState,
    _emit,
    _maybe_emit_title,
    emit_ollama_error,
    maybe_persist_partial,
    signal_done,
)
from app.ollama import OllamaProtocolError, OllamaUnavailable
from app.templates import templates
from app.tools import (
    encode_tool_call,
    encode_tool_result,
    format_tool_invocation,
    run_tool,
    tool_specs_for_ollama,
)

logger = logging.getLogger(__name__)


# Hard caps. Deliberately NOT aliased to single-agent's
# `_TOOL_ITERATION_CAP` in `app/generation.py` — that one is the
# per-turn cap for the single-agent path; this one is the per-pass
# cap inside the agentic loop's research stage. Same number today,
# different concepts. See design-notes "Inner tool-call cap is a
# fresh constant" for the rationale.
_AGENTIC_ITERATION_CAP = 3
_RESEARCH_TOOL_CAP_PER_PASS = 5


def _filter_prior_history_for_research(messages: list) -> list[dict]:
    """Extract prior turns' user/assistant rows in Ollama wire format.

    Strips out tool_call / tool_result / research_findings /
    review_verdict rows — those were means-of-production for prior
    answers and don't need to appear in a new turn's research
    context. The most recent user row (the current question) is
    INCLUDED so callers don't have to special-case it.

    Args:
        messages: All rows from `queries.list_messages` for this
            conversation, oldest-first.

    Returns:
        A list of dicts in Ollama's `messages` shape, containing
        only user + assistant rows.
    """
    out = []
    for m in messages:
        if m.role in ("user", "assistant"):
            out.append({"role": m.role, "content": m.content})
    return out


def _build_research_payload(
    system_prompt: str,
    prior_history: list[dict],
    intra_turn: list[dict],
) -> list[dict]:
    """Assemble the research agent's per-iteration Ollama payload.

    Layout:
      system: research system prompt
      <prior turns' user/assistant rows>
      <current turn's intra-iteration history: tool calls/results,
       findings from prior passes, and any review-feedback messages
       the orchestrator appended at iteration boundaries>

    The `intra_turn` list accumulates ACROSS iterations within the
    same turn: tool calls, tool results, prior-pass findings, AND
    any review-feedback messages the orchestrator pushed onto it at
    the top of iteration 2+. This is the "cumulative" retry context
    locked in the planning conversation.

    Review feedback is injected into `intra_turn` by the
    orchestrator (not by this helper) — that way it persists for
    every `maybe_tool_call` invocation within the iteration, not
    just the first one. If we only added it to one specific call's
    payload, the model would lose feedback context as soon as it
    made its first tool call inside the iteration.

    Args:
        system_prompt: The research system prompt.
        prior_history: Output of `_filter_prior_history_for_research`.
        intra_turn: Wire-format messages built up over prior iterations
            of the same turn.

    Returns:
        A new list — callers should not mutate intra_turn from this
        function's perspective; we make a fresh list to make the
        boundary explicit.
    """
    return [
        {"role": "system", "content": system_prompt},
        *prior_history,
        *intra_turn,
    ]


def _build_review_payload(user_message: str, findings: str) -> list[dict]:
    """Assemble the review agent's ephemeral Ollama payload.

    Review sees NO prior chat history and NO intra-turn artifacts
    besides the findings — its job is to judge the findings against
    the question, full stop.
    """
    return [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Original user question:\n"
                f"{user_message}\n\n"
                "Research agent's findings:\n"
                f"{findings}\n\n"
                "Decide if these findings are sufficient. Call"
                " mark_passed or request_more_research."
            ),
        },
    ]


def _build_generation_payload(user_message: str, findings: str) -> list[dict]:
    """Assemble the generation agent's ephemeral Ollama payload."""
    return [
        {"role": "system", "content": GENERATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Original user question:\n"
                f"{user_message}\n\n"
                "Approved research findings:\n"
                f"{findings}\n\n"
                "Write the answer to the user's question using only"
                " the findings above."
            ),
        },
    ]


async def _run_agentic_generation(
    *,
    state: GenerationState,
    client: httpx.AsyncClient,
    db: sqlite3.Connection,
    conversation_id: int,
    model: str,
    history: list,
    on_complete: Literal["append", "replace"],
) -> None:
    """Producer body for the agentic three-agent loop.

    Same shape as `app/generation.py:_run_generation` (no yield,
    writes via `_emit`, marks `state.done` in finally) but drives
    the research → review → generation loop instead of the single
    tool-calling round trip.

    See `docs/plans/phase13-agentic-loop.md` §Architecture for the
    high-level diagram.
    """
    # The most recent row in `history` is the user message we're
    # answering. The orchestrator persists everything after this.
    if not history or history[-1].role != "user":
        # Defensive — start_generation is always called right after
        # append_message("user", ...).
        await _emit(
            state, "error",
            '<div class="error">Agentic loop launched without a'
            ' pending user message.</div>',
        )
        return

    user_message = history[-1].content
    prior_history = _filter_prior_history_for_research(history)
    intra_turn: list[dict] = []  # accumulated across iterations
    final_findings: str = ""
    max_iterations_reached = False

    turn_id = str(time.monotonic_ns())
    card_id = f"tool-card-{turn_id}"
    list_id = f"{card_id}-list"
    summary_id = f"{card_id}-summary"

    chunks: list[str] = []
    persisted_or_errored = False

    # Emit the empty card shell once (no rows yet). Subsequent events
    # OOB-append rows into #{list_id}. The shell is a
    # `tool-card--agentic` variant so CSS can target the iteration
    # headers / verdicts.
    #
    # PROMPT FOR IMPLEMENTER: extend `app/render.py` with a sibling
    # of `render_tool_card_initial` — call it
    # `render_agentic_card_shell(card_id, list_id, summary_id,
    # conversation_id)` — that renders `_tool_card_shell.html` with
    # `rows=[]`, `summary_text="researching…"`, the agentic modifier
    # class, and an empty `<span id="…-max-marker">` placeholder
    # inside the summary (the for-else max-iterations branch below
    # OOB-fills that span). Call it here and `await _emit(...)`.
    # The existing render_tool_card_initial stays single-agent-only.

    try:

        for iteration in range(_AGENTIC_ITERATION_CAP):
            iteration_index = iteration + 1

            # Emit an iteration-start event that OOB-appends an
            # iteration header row to #{list_id}. The header is a
            # decorative <li> with class `tool-card__iteration-header`
            # and text like "Iteration 2 of 3". Also OOB-swap the
            # summary span to "researching (iteration N)…" per the
            # summary state machine in the design notes.
            # PROMPT FOR IMPLEMENTER: add `render_iteration_start(
            # iteration_index, list_id, summary_id)` to
            # `app/render.py`. It returns ONE concatenated payload:
            # the `<li class="tool-card__iteration-header">` row with
            # `hx-swap-oob="beforeend:#{list_id}"` AND a summary-span
            # outerHTML swap (`<span id="{summary_id}" hx-swap-oob=
            # "outerHTML">researching (iteration N)…</span>`). The
            # inline-string approach below is the prior sketch; replace
            # it with the helper call once the helper exists.
            await _emit(
                state, "iteration-start",
                f'<li hx-swap-oob="beforeend:#{list_id}"'
                f' class="tool-card__iteration-header"'
                f' data-iteration="{iteration_index}">'
                f'Iteration {iteration_index}'
                f'</li>',
            )

            # === Research pass ===

            # If this is iteration 2+, the prior pass failed and the
            # orchestrator already appended the review feedback as a
            # `user`-role message onto `intra_turn` (see the bottom
            # of the verdict-handling branch below). That message
            # now flows into EVERY maybe_tool_call inside this
            # iteration, not just the first one — which is the bug
            # we'd hit if we instead passed feedback as a one-shot
            # parameter to `_build_research_payload`.

            # Inner tool-calling loop. Mirrors the body of
            # app/generation.py:_run_generation's for-loop. Each call
            # persists tool_call + tool_result rows and emits the
            # corresponding SSE events with row ids
            # f"{card_id}-iter-{iteration_index}-row-{call_index}".
            #
            # IMPORTANT: append wire-format messages onto `intra_turn`
            # as we go so the next iteration's research_payload
            # includes them.

            research_call_index = 0
            findings = ""
            for _ in range(_RESEARCH_TOOL_CAP_PER_PASS):
                payload = _build_research_payload(
                    RESEARCH_SYSTEM_PROMPT,
                    prior_history,
                    intra_turn,
                )
                try:
                    tool_calls, content = await ollama.maybe_tool_call(
                        client, model, payload,
                        tools=tool_specs_for_ollama(),
                    )
                except (OllamaUnavailable, OllamaProtocolError) as e:
                    # Flag-before-await: await is a cancellation point,
                    # so set persisted_or_errored FIRST so the outer
                    # finally's maybe_persist_partial sees it.
                    persisted_or_errored = True
                    await emit_ollama_error(state, e)
                    return

                if not tool_calls:
                    findings = content.strip()
                    break

                # For each call: persist tool_call row, build the live
                # ToolRowView, emit the tool-call SSE via
                # `render.render_tool_card_row_append` (since the
                # agentic card already exists — the shell was emitted
                # before the iteration loop), run the tool, persist
                # tool_result row, emit tool-result SSE via
                # `render.render_tool_card_row_freeze`.
                #
                # Row id format: `f"{card_id}-iter-{iteration_index}-row-{research_call_index}"`.
                # Live row construction matches `_run_generation` —
                # mirror the existing `live_row = render.ToolRowView(...)`
                # pattern in app/generation.py.
                #
                # Also append the call+result to intra_turn so the
                # next maybe_tool_call request sees them.
                for call in tool_calls:
                    name = call["name"]
                    arguments = call.get("arguments") or {}
                    queries.append_message(
                        db, conversation_id, "tool_call",
                        content=encode_tool_call(name, arguments),
                    )
                    intra_turn.append({
                        "role": "assistant", "content": "",
                        "tool_calls": [{
                            "function": {"name": name, "arguments": arguments}
                        }],
                    })
                    # PROMPT FOR IMPLEMENTER:
                    #   row_id = f"{card_id}-iter-{iteration_index}-row-{research_call_index}"
                    #   live_row = render.ToolRowView(id=row_id, label=..., elapsed_start_ms=..., ...)
                    #   await _emit(state, "tool-call",
                    #       render.render_tool_card_row_append(
                    #           live_row=live_row, list_id=list_id,
                    #           summary_id=summary_id,
                    #           call_index=research_call_index,
                    #       ))
                    # NOTE: render_tool_card_row_append's existing
                    # summary text reads "using N tools…" — agentic
                    # mode wants "researching (iteration N)…" instead.
                    # Either (a) add a new
                    # `render_agentic_row_append(...)` helper that
                    # emits the same row HTML but skips the summary
                    # bump (the iteration-start event already swapped
                    # the summary), or (b) extend
                    # render_tool_card_row_append with a
                    # `summary_text_override: str | None = None`
                    # parameter. The first is cleaner; go with that.
                    result = await run_tool(name, arguments)
                    queries.append_message(
                        db, conversation_id, "tool_result",
                        content=encode_tool_result(result),
                    )
                    intra_turn.append({
                        "role": "tool", "content": result.text,
                    })
                    # PROMPT FOR IMPLEMENTER:
                    #   frozen_row = render.ToolRowView(id=row_id, label=..., elapsed_final_ms=..., sources=result.sources, ...)
                    #   await _emit(state, "tool-result",
                    #       render.render_tool_card_row_freeze(frozen_row))
                    # The freeze helper is already source-aware
                    # (phase 12h) so RAG sources surface in the
                    # agentic card the same as in single-agent rows.
                    research_call_index += 1
            else:
                # Inner cap hit. `findings` stays whatever the model
                # emitted on its last successful maybe_tool_call (or
                # "" if it called tools every time).
                pass

            if not findings:
                # Defensive: the model called tools to the cap without
                # ever emitting text. Synthesize a minimal findings
                # message so review has something to evaluate.
                findings = (
                    f"(No findings produced; research agent called tools "
                    f"{research_call_index} times without summarising.)"
                )

            # Persist findings + emit row. Findings row is a single
            # <li> appended to the card list.
            queries.append_message(
                db, conversation_id, "research_findings", findings
            )
            intra_turn.append({"role": "assistant", "content": findings})
            # PROMPT FOR IMPLEMENTER: add a sibling render helper
            # `render_findings_row(findings: str, iteration_index: int,
            # list_id: str) -> str` to `app/render.py`. It returns
            # the OOB-append HTML for a new template
            # `_findings_row.html` — a <li class="tool-card__findings"
            # data-iteration="N" hx-swap-oob="beforeend:#{list_id}">
            # wrapping a <details><summary>Research findings</summary>
            # <p>{findings|markdown|safe}</p></details>. Markdown
            # rendering reuses the `templates.env.filters["markdown"]`
            # filter already wired in `app/templates.py`.
            await _emit(state, "research-findings",
                render.render_findings_row(
                    findings=findings,
                    iteration_index=iteration_index,
                    list_id=list_id,
                ),
            )

            # === Review pass ===

            review_payload = _build_review_payload(user_message, findings)
            try:
                verdict_calls, _ = await ollama.maybe_tool_call(
                    client, model, review_payload, tools=REVIEW_TOOL_SPECS,
                )
            except (OllamaUnavailable, OllamaProtocolError) as e:
                persisted_or_errored = True
                await emit_ollama_error(state, e)
                return

            decision = parse_verdict(verdict_calls)
            queries.append_message(
                db, conversation_id, "review_verdict",
                content=json.dumps({
                    "verdict": decision.verdict,
                    "message": decision.message,
                }),
            )
            # PROMPT FOR IMPLEMENTER: add `render_verdict_row(decision:
            # VerdictDecision, iteration_index: int, list_id: str) ->
            # str` to `app/render.py`. Renders a new template
            # `_verdict_row.html` — a <li class="tool-card__verdict
            # tool-card__verdict--{passed|failed}" data-iteration="N"
            # hx-swap-oob="beforeend:#{list_id}"> containing the
            # check_circle/cancel icon, the verb ("Passed:" /
            # "Failed:"), and the verdict message. The historic-replay
            # template (`_agentic_tool_card.html`, see sub-phase 13f)
            # already builds the same DOM — extract the shared bit so
            # live + historic agree on classes and icons.
            await _emit(state, "review-verdict",
                render.render_verdict_row(
                    decision=decision,
                    iteration_index=iteration_index,
                    list_id=list_id,
                ),
            )

            final_findings = findings
            if decision.verdict == "passed":
                break
            # Push the feedback into intra_turn as a user message so
            # the NEXT iteration's research payload includes it
            # automatically — and keeps including it through every
            # tool call inside that iteration. See "Review pass"
            # design note at the top of this iteration.
            intra_turn.append({
                "role": "user",
                "content": (
                    "Review feedback on your last findings:\n"
                    f"{decision.message}\n\n"
                    "Continue researching to address the feedback."
                    " Do not repeat queries you already ran."
                ),
            })
        else:
            # for-else: ran 3 iterations without break → max reached.
            max_iterations_reached = True
            # PROMPT FOR IMPLEMENTER: emit a tiny outerHTML OOB on the
            # sentinel `<span id="{card_id}-max-marker">` that
            # `render_agentic_card_shell` (see top of try block)
            # placed inside the summary. Filling that span with the
            # badge text avoids re-rendering the whole <details> —
            # which would clobber every row already in the DOM.
            # Add `render_max_iterations_badge(card_id: str) -> str`
            # to `app/render.py` that returns the marker swap, and
            # call it here:
            #
            #   await _emit(state, "iteration-start",
            #       render.render_max_iterations_badge(card_id))
            #
            # Use a dedicated event name like "max-iterations" if
            # you'd rather not overload "iteration-start" — update
            # the placeholder's `sse-swap=` attribute too. The
            # current sketch reuses "iteration-start" for compactness.
            await _emit(state, "iteration-start",
                render.render_max_iterations_badge(card_id),
            )

        # === Generation pass ===

        generation_payload = _build_generation_payload(user_message, final_findings)
        try:
            async for chunk in ollama.stream_chat(
                client, model, generation_payload
            ):
                if chunk.content:
                    chunks.append(chunk.content)
                    await _emit(state, "token", html.escape(chunk.content))
                if chunk.done:
                    break
        except (OllamaUnavailable, OllamaProtocolError) as e:
            persisted_or_errored = True
            await emit_ollama_error(state, e)
            return

        full_text = "".join(chunks)
        if on_complete == "append":
            message = queries.append_message(
                db, conversation_id, "assistant", full_text
            )
        else:
            message = queries.replace_last_assistant_message(
                db, conversation_id, full_text
            )
        persisted_or_errored = True

        if on_complete == "append":
            await _maybe_emit_title(state, client, db, conversation_id)

        # PROMPT FOR IMPLEMENTER: extend `app/render.py` with
        # `render_agentic_done_summary(summary_id, iterations_run,
        # max_iterations_reached) -> str` mirroring
        # `render_done_card_oobs` but with the agentic summary text
        # ("ran N iterations" / "ran N iterations (max reached)").
        # Then build the done payload as:
        #
        #   final_html = templates.get_template("_message.html").render(
        #       message=message,
        #       swap_target=f"#assistant-stream-{conversation_id}",
        #   )
        #   done_summary = render.render_agentic_done_summary(
        #       summary_id, len(iterations_run), max_iterations_reached
        #   )
        #   await _emit(state, "done", done_summary + final_html)
        final_html = templates.get_template("_message.html").render(
            message=message,
            swap_target=f"#assistant-stream-{conversation_id}",
        )
        await _emit(state, "done", final_html)

    finally:
        # Same safety net as `_run_generation` — phase 13 reuses the
        # extracted helpers so any bug fix to one path benefits both.
        maybe_persist_partial(
            db, conversation_id, on_complete, chunks, persisted_or_errored
        )
        await signal_done(state)
```

### Code: dispatcher in `app/generation.py`

Modify `start_generation` to pick between the two producers based
on `(agentic mode toggle on) AND (model has tool capability)`. The
single-agent path stays unchanged; the agentic path delegates to
`app.agents.loop._run_agentic_generation`.

`start_generation` becomes `async` so it can `await
model_supports_tools(client, model)` instead of peeking at the
private cache. The three call sites
(`create_chat_endpoint`, `send_message_endpoint`,
`regenerate_endpoint`) are already `async def` and already call
`start_generation` from inside async context — adding `await`
is a one-token diff at each. The async dispatcher avoids the
cache-staleness window where a freshly-restarted process would
silently take the wrong branch on the first turn.

```python
# Add to the imports at the top of app/generation.py
from app.agents import loop as agents_loop  # phase 13


async def start_generation(  # was: def
    *,
    client: httpx.AsyncClient,
    db: sqlite3.Connection,
    conversation_id: int,
    model: str,
    history: list,
    on_complete: Literal["append", "replace"],
) -> GenerationState:
    # ... existing in-flight guard, unchanged ...
    state = GenerationState(conversation_id=conversation_id)
    live_generations[conversation_id] = state

    # Phase 13 dispatch. The silent fallback for non-tool-capable
    # models is what the locked decisions specify; the route layer
    # is responsible for the "agentic mode skipped" badge (see
    # sub-phase 13e §No-tools fallback badge).
    use_agentic = (
        queries.get_agentic_mode(db)
        and await ollama.model_supports_tools(client, model)
    )
    producer = (
        agents_loop._run_agentic_generation
        if use_agentic
        else _run_generation
    )

    state.task = asyncio.create_task(
        producer(
            state=state,
            client=client,
            db=db,
            conversation_id=conversation_id,
            model=model,
            history=history,
            on_complete=on_complete,
        )
    )
    state.task.add_done_callback(_make_done_callback(conversation_id))
    return state
```

### Call-site updates for the async conversion

All three are already async; each one adds a single `await`. The
fourth caller (route layer's regenerate flow) follows the same
shape. No try/except restructure needed — `start_generation` still
raises `GenerationInProgress` synchronously before its first await,
so the existing `except GenerationInProgress` handlers still work
unchanged.

```python
# app/routes.py — create_chat_endpoint
generation.start_generation(...)   # before
await generation.start_generation(...)  # after

# app/routes.py — send_message_endpoint
generation.start_generation(...)   # before
await generation.start_generation(...)  # after

# app/routes.py — regenerate_endpoint
generation.start_generation(...)   # before
await generation.start_generation(...)  # after
```

### Tests: `tests/test_agentic_loop.py`

Mock-only, mirroring `tests/test_generation.py`'s style: no
`conftest.py` in this repo, no shared `db` / `chat` /
`ollama_client` fixtures — each test sets up its own SQLite tempfile
via the existing `_setup_chat(db_path, ...)` helper pattern, and
stubs Ollama via `monkeypatch.setattr(ollama, "maybe_tool_call",
...)` / `monkeypatch.setattr(ollama, "stream_chat", ...)`. Reuse
the per-file helpers from `tests/test_generation.py` where useful
(`_setup_chat`, `_tool_handler`, `_install_rag_server`, etc.) by
factoring them up to either a new `tests/_helpers.py` or by
duplicating the small ones — the existing tests do not share via
conftest, and 13 should not introduce one.

The sketch below is **illustrative**; the implementer should
flesh it out to match `tests/test_generation.py`'s actual idioms
(local `_setup_chat` helper, `monkeypatch.setattr` for Ollama,
`@pytest.fixture(autouse=True)` for clearing `live_generations`
and the capability cache).

```python
"""Phase 13: integration-style tests for the agentic-loop orchestrator.

Mocks Ollama. Asserts on persisted messages and on the SSE event log
captured in `GenerationState.events`.

Test style mirrors `tests/test_generation.py` — no shared
conftest.py fixtures; each test sets up its own DB tempfile and
monkeypatches `app.ollama` directly.
"""

import json

import pytest

from app import generation, ollama, queries
from app.agents.loop import _run_agentic_generation


def _setup_chat(db_path, name="test"):
    """Local helper — duplicate of test_generation.py's, intentionally.

    Per-area helpers stay co-located with the tests that use them.
    Module-state isolation (`generation.live_generations` clear,
    capability-cache reset) is handled by the autouse fixture in
    `tests/conftest.py`, so this file doesn't need its own copies.
    """
    # ... insert conversation, return its id ...


@pytest.mark.asyncio
async def test_loop_passes_on_first_iteration(tmp_path, monkeypatch) -> None:
    """Research → mark_passed → generation. No retry."""
    # Stage:
    #   monkeypatch.setattr(ollama, "maybe_tool_call", scripted_sequence)
    #   monkeypatch.setattr(ollama, "stream_chat", scripted_chunks)
    # Run _run_agentic_generation against a fresh GenerationState.
    # Assert the persisted Message roles in order:
    #   ["user", "tool_call", "tool_result",
    #    "research_findings", "review_verdict", "assistant"]


@pytest.mark.asyncio
async def test_loop_retries_then_passes(tmp_path, monkeypatch) -> None:
    """Iteration 1: fail. Iteration 2: pass. Final assistant row persisted.

    Asserts two research_findings rows + two review_verdict rows
    + one assistant row.
    """


@pytest.mark.asyncio
async def test_loop_max_iterations_force_generates(
    tmp_path, monkeypatch
) -> None:
    """Three failed iterations → force generation with last findings.

    Stage three request_more_research returns. Assert 3
    research_findings rows, 3 review_verdict rows (all failed), 1
    assistant row, and that the `done` SSE event payload contains
    the max-iterations marker.
    """


@pytest.mark.asyncio
async def test_loop_review_no_verdict_tool_treated_as_fail(
    tmp_path, monkeypatch
) -> None:
    """Defensive: if review emits no recognized tool call,
    parse_verdict's fallback kicks in (failed + default message)
    and the loop continues to the next iteration."""


@pytest.mark.asyncio
async def test_loop_research_inner_cap_then_review_runs(
    tmp_path, monkeypatch
) -> None:
    """Research hits its 5-call inner cap → review still runs on
    whatever findings exist (or the synthesized empty-findings
    fallback message)."""


@pytest.mark.asyncio
async def test_feedback_persists_across_intra_iteration_tool_calls(
    tmp_path, monkeypatch
) -> None:
    """Regression test for the bug fixed in the plan-review pass.

    Iteration 1 fails. Iteration 2's research agent makes TWO tool
    calls before producing findings. The review feedback must be
    visible in the payload of BOTH `maybe_tool_call` invocations in
    iteration 2, not just the first.

    Inspect the recorded `maybe_tool_call` payload arguments to
    assert the feedback user-message appears in both.
    """


# Dispatcher branch test — lives in test_generation.py since it
# tests start_generation, not the orchestrator. Two cases:
#   - toggle on + tool-capable model → producer is agentic loop
#   - toggle on + non-tool-capable model → producer is single-agent
#     (silent fallback)
```

---

## Sub-phase 13e — Settings UI (toggle + read-only prompts)

### Changes

- `app/routes.py`:
  - `GET /settings`: read `get_agentic_mode(db)`; pass it + the three
    prompts to the template.
  - `POST /settings/agentic-mode`: HTMX form handler that toggles the
    setting. Returns the updated section fragment so HTMX swaps it
    in place.
- `templates/_settings.html`: new `.settings__section` for "Agentic
  mode" with the toggle + collapsible read-only prompts.
- `static/style.css`: minor styles for the prompt `<pre>` blocks
  (monospace, max-height + scroll, syntax-neutral).

### Code: route additions in `app/routes.py`

```python
# Add to the imports near the top
from app.agents.prompts import (
    GENERATION_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
)


# Replace the existing settings_endpoint with this expanded version
@router.get("/settings", response_class=HTMLResponse)
def settings_endpoint(request: Request, db: DB) -> Response:
    """Standalone settings page — RAG servers + (phase 13) agentic mode.

    Direct browser hits return the full index shell; HTMX swaps get
    just the fragment. Both paths receive the same context dict so
    the included `_settings.html` template renders identically.
    """
    servers = _rag_servers_module.list_servers(db)
    context = {
        "servers": servers,
        "agentic_mode_on": queries.get_agentic_mode(db),
        "agentic_prompts": {
            "research": RESEARCH_SYSTEM_PROMPT,
            "review": REVIEW_SYSTEM_PROMPT,
            "generation": GENERATION_SYSTEM_PROMPT,
        },
    }
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_settings.html",
            context=context,
        )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "chats": queries.list_conversations(db),
            "conversation": None,
            "messages": [],
            "active_chat_id": None,
            "settings_view": True,
            "rag_servers": servers,
            "agentic_mode_on": context["agentic_mode_on"],
            "agentic_prompts": context["agentic_prompts"],
        },
    )


@router.post("/settings/agentic-mode", response_class=HTMLResponse)
def toggle_agentic_mode_endpoint(
    request: Request,
    db: DB,
    enabled: Annotated[str | None, Form()] = None,
) -> Response:
    """Toggle the global agentic-mode setting.

    The checkbox sends `enabled=on` when checked; the field is absent
    entirely when unchecked. This matches the standard HTML form
    convention and lets us write the helper as a presence check
    rather than a string compare.

    Returns the agentic-mode section fragment so HTMX swaps it in
    place (the toggle stays inside #settings-agentic-section). The
    read-only prompt block is included in the fragment so toggling
    On reveals it without a follow-up round trip.
    """
    queries.set_agentic_mode(db, enabled is not None)
    return templates.TemplateResponse(
        request=request,
        name="_settings_agentic_section.html",
        context={
            "agentic_mode_on": queries.get_agentic_mode(db),
            "agentic_prompts": {
                "research": RESEARCH_SYSTEM_PROMPT,
                "review": REVIEW_SYSTEM_PROMPT,
                "generation": GENERATION_SYSTEM_PROMPT,
            },
        },
    )
```

### Code: `templates/_settings.html` — add a new section

Append after the existing RAG-servers section:

```jinja
  {# Phase 13: agentic-mode toggle + read-only prompts. The whole
     section is one #settings-agentic-section <section> so the POST
     handler can return it as an outerHTML swap target. #}
  <section id="settings-agentic-section" class="settings__section">
    {% include "_settings_agentic_section.html" %}
  </section>
```

### Code: `templates/_settings_agentic_section.html` (NEW)

```jinja
{# Phase 13: agentic-mode toggle + read-only prompts.

   Included by _settings.html on initial render AND returned standalone
   from POST /settings/agentic-mode. Renders identically in both
   contexts so the swap is seamless.

   Context vars:
     agentic_mode_on: bool — current setting
     agentic_prompts: dict[str, str] — keys "research", "review", "generation"
#}
<h2 class="settings__section-title">Agentic mode</h2>
<p class="settings__section-help">
  When on, every assistant turn runs a research → review → generation
  loop instead of a single tool-calling pass. Slower (3+ Ollama
  round-trips per answer) but produces more thorough, self-critiqued
  responses. Requires a tool-capable model.
</p>

{# HTMX attributes live directly on the <input> so there's no inline
   JS. `hx-trigger="change"` fires on the checkbox toggle (the
   default for `<input>`, but spelt out for readability). The
   `enabled` form field is sent only when the checkbox is checked,
   matching the standard HTML convention the POST handler relies on
   for its presence-check. #}
<label class="agentic-mode-toggle">
  <input type="checkbox" name="enabled"
         {% if agentic_mode_on %}checked{% endif %}
         hx-post="/settings/agentic-mode"
         hx-trigger="change"
         hx-target="#settings-agentic-section"
         hx-swap="innerHTML">
  Enable agentic mode
</label>

{% if agentic_mode_on %}
<details class="agentic-prompts">
  <summary>View system prompts (read-only)</summary>
  <div class="agentic-prompts__group">
    <h3>Research agent</h3>
    <pre class="agentic-prompts__text">{{ agentic_prompts.research }}</pre>
  </div>
  <div class="agentic-prompts__group">
    <h3>Review agent</h3>
    <pre class="agentic-prompts__text">{{ agentic_prompts.review }}</pre>
  </div>
  <div class="agentic-prompts__group">
    <h3>Generation agent</h3>
    <pre class="agentic-prompts__text">{{ agentic_prompts.generation }}</pre>
  </div>
</details>
{% endif %}
```

### Code: minimal style additions in `static/style.css`

```css
/* Phase 13: agentic mode settings */
.agentic-mode-toggle {
  margin: var(--space-3) 0;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  cursor: pointer;
}
.agentic-prompts {
  margin-top: var(--space-3);
}
.agentic-prompts__group {
  margin: var(--space-3) 0;
}
.agentic-prompts__text {
  background: var(--card-background-color);
  border: 1px solid var(--muted-border-color);
  border-radius: var(--border-radius);
  padding: var(--space-3);
  max-height: 16rem;
  overflow: auto;
  font-family: var(--font-family-monospace);
  font-size: 0.875rem;
  line-height: 1.4;
  white-space: pre-wrap;
}
```

### Tests: extend `tests/test_routes.py`

```python
def test_get_settings_shows_agentic_toggle_off_by_default(client) -> None:
    """Fresh DB: agentic mode toggle renders unchecked, prompts hidden."""
    resp = client.get("/settings", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert 'name="enabled"' in resp.text
    assert 'checked' not in resp.text.split('name="enabled"')[0][-200:]
    # Prompts <details> not rendered when off.
    assert "agentic-prompts" not in resp.text


def test_toggle_agentic_mode_on(client, db) -> None:
    """POST with enabled=on flips the setting and re-renders the section."""
    resp = client.post(
        "/settings/agentic-mode",
        data={"enabled": "on"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert queries.get_agentic_mode(db) is True
    # Prompts now visible.
    assert "Research agent" in resp.text
    assert "mark_passed" in resp.text  # review prompt body


def test_toggle_agentic_mode_off(client, db) -> None:
    """POST without enabled field flips the setting off."""
    queries.set_agentic_mode(db, True)
    resp = client.post(
        "/settings/agentic-mode",
        data={},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert queries.get_agentic_mode(db) is False
    assert "agentic-prompts" not in resp.text
```

---

## Sub-phase 13f — Tool card extension (iteration grouping)

The live SSE path appends rows directly with the iteration class /
data-attribute hooks from sub-phase 13d. The *historic-render* path
(when a chat is reloaded) needs a parallel block type so the same
visual structure is reconstructed from persisted messages.

### Changes

- `app/render.py`: add `AgenticIteration` and `AgenticToolBatchBlock`
  dataclasses; teach `group_messages_for_render` to detect agentic
  turns and emit the new block type.
- `templates/_chat_panel.html`: branch on `block.kind`. Existing
  `tool_batch` branch unchanged; new `agentic_tool_batch` branch
  uses a new template `_agentic_tool_card.html`.
- New template `_agentic_tool_card.html`: same shell as
  `_tool_card_shell.html` but with iteration headers, findings
  rows, and verdict rows in render order.
- `tests/test_render.py`: cases for the new grouping rules.

### Code: `app/render.py` additions

```python
# Phase 13: share the iteration cap with the live orchestrator.
# Hardcoding `3` in two files would mean a future change to the cap
# silently desyncs historic-render's max_iterations_reached check
# from the actual orchestrator behaviour.
from app.agents.loop import _AGENTIC_ITERATION_CAP


@dataclass(frozen=True)
class AgenticIteration:
    """One research → review iteration in an agentic turn.

    Attributes:
        index: 1-based iteration number. Drives the
            data-iteration attribute on the rendered header.
        tool_calls: (call, result) pairs from research's tool-calling
            inner loop. Same shape as ToolBatchBlock.calls.
        findings: The research_findings row for this iteration, or
            None when an iteration's findings never landed (defensive
            — shouldn't happen for completed turns).
        verdict: The review_verdict row, or None for the same reason.
    """

    index: int
    tool_calls: list[tuple[Message, Message | None]]
    findings: Message | None
    verdict: Message | None

    @property
    def verdict_status(self) -> str:
        """`"passed"` / `"failed"` / `"unknown"` (no verdict)."""
        if self.verdict is None:
            return "unknown"
        try:
            payload = json.loads(self.verdict.content)
            return payload.get("verdict", "unknown")
        except (json.JSONDecodeError, TypeError):
            return "unknown"

    @property
    def verdict_message(self) -> str:
        """Human-readable verdict text. Empty when no verdict row."""
        if self.verdict is None:
            return ""
        try:
            payload = json.loads(self.verdict.content)
            return payload.get("message", "")
        except (json.JSONDecodeError, TypeError):
            return ""


@dataclass(frozen=True)
class AgenticToolBatchBlock:
    """One assistant turn's full agentic loop, grouped for the card.

    Attributes:
        iterations: AgenticIteration entries in chronological order.
            At least one entry; max 3 (the loop cap).
        max_iterations_reached: True when the loop hit
            _AGENTIC_ITERATION_CAP without a "passed" verdict.
            Derived from the final iteration's verdict_status.
        turn_id: Stable id for DOM ids; `f"hist-{first_call_or_finding_id}"`.
        kind: Template discriminator. Class-level constant.
    """

    iterations: list[AgenticIteration] = field(default_factory=list)
    turn_id: str = ""
    kind: ClassVar[str] = "agentic_tool_batch"

    @property
    def max_iterations_reached(self) -> bool:
        if len(self.iterations) < _AGENTIC_ITERATION_CAP:
            return False
        return self.iterations[-1].verdict_status != "passed"

    @property
    def card_id(self) -> str:
        return card_id_for(self.turn_id)

    @property
    def list_id(self) -> str:
        return f"{self.card_id}-list"

    @property
    def summary_id(self) -> str:
        return f"{self.card_id}-summary"

    @property
    def summary(self) -> str:
        n = len(self.iterations)
        plural = "iteration" if n == 1 else "iterations"
        if self.max_iterations_reached:
            return f"ran {n} {plural} (max reached)"
        return f"ran {n} {plural}"
```

Then extend `group_messages_for_render` to detect agentic batches.
Replace the grouping logic with:

```python
def group_messages_for_render(messages: list[Message]) -> list[Block]:
    """Walk messages, folding tool-related runs into the right block type.

    Recognises three run shapes inside a single assistant turn's
    pre-answer section:
      A) zero or more `tool_call`/`tool_result` pairs → ToolBatchBlock
         (existing single-agent behavior).
      B) any run that contains at least one `research_findings` or
         `review_verdict` row → AgenticToolBatchBlock (phase 13).

    Detection: collect the whole pre-answer run as a flat list of
    rows, then inspect for findings/verdict presence to pick the
    block type.

    See module docstring on `ToolBatchBlock` for the prior shape;
    `AgenticToolBatchBlock`'s docstring covers the new shape.
    """
    blocks: list[Block] = []
    pending_rows: list[Message] = []

    def flush() -> None:
        nonlocal pending_rows
        if not pending_rows:
            return
        has_agentic = any(
            r.role in ("research_findings", "review_verdict")
            for r in pending_rows
        )
        if has_agentic:
            blocks.append(_build_agentic_block(pending_rows))
        else:
            blocks.append(_build_classic_tool_batch(pending_rows))
        pending_rows = []

    for m in messages:
        if m.role in (
            "tool_call",
            "tool_result",
            "research_findings",
            "review_verdict",
        ):
            pending_rows.append(m)
        else:
            flush()
            blocks.append(MessageBlock(message=m))

    flush()
    return blocks


def _build_classic_tool_batch(rows: list[Message]) -> ToolBatchBlock:
    """Pair tool_call rows with the next tool_result; emit a ToolBatchBlock.

    Identical to the pre-13 grouping rules — just factored out so
    `group_messages_for_render` can pick the block type cleanly.
    """
    calls: list[tuple[Message, Message | None]] = []
    pending_call: Message | None = None
    for m in rows:
        if m.role == "tool_call":
            if pending_call is not None:
                calls.append((pending_call, None))
            pending_call = m
        elif m.role == "tool_result":
            if pending_call is not None:
                calls.append((pending_call, m))
                pending_call = None
    if pending_call is not None:
        calls.append((pending_call, None))
    turn_id = f"hist-{rows[0].id}"
    return ToolBatchBlock(calls=calls, turn_id=turn_id)


def _build_agentic_block(rows: list[Message]) -> AgenticToolBatchBlock:
    """Slice rows into AgenticIteration entries.

    Iteration boundaries are research_findings rows. Tool calls /
    results between the previous boundary (or start) and the next
    research_findings belong to that iteration. The review_verdict
    row immediately following research_findings closes the
    iteration.
    """
    iterations: list[AgenticIteration] = []
    pending_calls: list[tuple[Message, Message | None]] = []
    pending_call: Message | None = None
    current_findings: Message | None = None
    iteration_index = 0

    def commit_iteration(verdict: Message | None) -> None:
        nonlocal pending_calls, pending_call, current_findings, iteration_index
        iteration_index += 1
        if pending_call is not None:
            pending_calls.append((pending_call, None))
            pending_call = None
        iterations.append(AgenticIteration(
            index=iteration_index,
            tool_calls=list(pending_calls),
            findings=current_findings,
            verdict=verdict,
        ))
        pending_calls = []
        current_findings = None

    for m in rows:
        if m.role == "tool_call":
            if pending_call is not None:
                pending_calls.append((pending_call, None))
            pending_call = m
        elif m.role == "tool_result":
            if pending_call is not None:
                pending_calls.append((pending_call, m))
                pending_call = None
        elif m.role == "research_findings":
            current_findings = m
        elif m.role == "review_verdict":
            commit_iteration(m)

    # End-of-rows: if a findings row was seen without a closing
    # verdict (defensive — shouldn't happen for completed turns),
    # commit the iteration anyway with verdict=None.
    if current_findings is not None or pending_call is not None or pending_calls:
        commit_iteration(verdict=None)

    turn_id = f"hist-{rows[0].id}"
    return AgenticToolBatchBlock(iterations=iterations, turn_id=turn_id)
```

### Template: `templates/_agentic_tool_card.html` (NEW)

```jinja
{# Phase 13: agentic-mode tool card.

   Same outer <details> shape as `_tool_card_shell.html` so existing
   CSS for `tool-card` applies. Adds:
   - `tool-card--agentic` modifier class on the outer <details>
   - `data-max-iterations="true"` attribute when the loop hit the cap
   - iteration headers as <li.tool-card__iteration-header>
   - findings rows as <li.tool-card__findings> wrapping a <details>
   - verdict rows as <li.tool-card__verdict>

   Live SSE path appends these rows incrementally; this template is
   the historic-replay equivalent.

   Context vars:
     block: AgenticToolBatchBlock
#}
<details id="{{ block.card_id }}"
         class="tool-card tool-card--agentic"
         {%- if block.max_iterations_reached %} data-max-iterations="true"{% endif %}>
  <summary class="tool-card__summary">
    <span class="material-symbols-outlined">build</span>
    <span id="{{ block.summary_id }}">{{ block.summary }}</span>
    <span class="tool-card__chevron material-symbols-outlined">expand_more</span>
  </summary>
  <ul id="{{ block.list_id }}" class="tool-card__list">
    {% for iteration in block.iterations %}
      <li class="tool-card__iteration-header"
          data-iteration="{{ iteration.index }}">
        Iteration {{ iteration.index }}
      </li>
      {% for call, result in iteration.tool_calls %}
        {# Reuse _tool_row.html with the iteration-scoped row id.
           `swap_oob = none` shadows any parent-context swap target
           for the duration of the include — historic replay never
           wants OOB swaps on the rows. Mirrors the same
           `{% with swap_oob = none %}` wrapper in
           `_tool_card_shell.html`'s row loop. #}
        {% with row = block.tool_row_view(iteration.index, loop.index0, call, result), swap_oob = none %}
          {% include "_tool_row.html" %}
        {% endwith %}
      {% endfor %}
      {% if iteration.findings %}
        <li class="tool-card__findings"
            data-iteration="{{ iteration.index }}">
          <details>
            <summary>Research findings</summary>
            <p>{{ iteration.findings.content }}</p>
          </details>
        </li>
      {% endif %}
      {% if iteration.verdict %}
        <li class="tool-card__verdict tool-card__verdict--{{ iteration.verdict_status }}"
            data-iteration="{{ iteration.index }}">
          <span class="material-symbols-outlined">
            {%- if iteration.verdict_status == 'passed' -%}
              check_circle
            {%- elif iteration.verdict_status == 'failed' -%}
              cancel
            {%- else -%}
              help
            {%- endif -%}
          </span>
          <strong>{{ iteration.verdict_status|capitalize }}:</strong>
          {{ iteration.verdict_message }}
        </li>
      {% endif %}
    {% endfor %}
  </ul>
</details>
```

> **Note for the implementer:** the snippet uses
> `block.tool_row_view(iteration.index, loop.index0, call, result)` —
> add this helper to `AgenticToolBatchBlock` (mirrors
> `ToolBatchBlock.rows` but returns one row at a time with the
> iteration-scoped row id format). The exact signature is your
> call; the key constraint is that the row id matches the live
> SSE event id format (`tool-card-{turn_id}-iter-{N}-row-{M}`) so a
> mid-turn reload reconstructs the same DOM ids.

### Templates: extend `templates/_chat_panel.html`

Find the existing block dispatch in the chat panel:

```jinja
{% for block in blocks %}
  {% if block.kind == "message" %}
    {% include "_message.html" %}
  {% elif block.kind == "tool_batch" %}
    {% include "_tool_card_shell.html" %}
  {% endif %}
{% endfor %}
```

Add a third branch:

```jinja
  {% elif block.kind == "agentic_tool_batch" %}
    {% include "_agentic_tool_card.html" %}
```

### CSS: extend `static/style.css`

```css
/* Phase 13: agentic-mode tool card extensions. The base tool-card
   styles apply; these add iteration grouping + verdict colouring. */
.tool-card--agentic[data-max-iterations="true"] .tool-card__summary::after {
  content: " (max iterations reached)";
  color: var(--color-warning, #c97e00);
  font-size: 0.875em;
}
.tool-card__iteration-header {
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted-color);
  padding: var(--space-2) 0 var(--space-1) 0;
  border-bottom: 1px solid var(--muted-border-color);
  margin-top: var(--space-2);
  list-style: none;
}
.tool-card__iteration-header[data-iteration="1"] {
  margin-top: 0;
}
.tool-card__findings {
  margin: var(--space-2) 0;
}
.tool-card__findings > details > summary {
  cursor: pointer;
  color: var(--muted-color);
}
.tool-card__verdict {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-2);
  border-left: 3px solid;
  margin: var(--space-2) 0;
  list-style: none;
}
.tool-card__verdict--passed {
  border-left-color: var(--color-success, #18794e);
  background: color-mix(in srgb, var(--color-success, #18794e) 6%, transparent);
}
.tool-card__verdict--failed {
  border-left-color: var(--color-warning, #c97e00);
  background: color-mix(in srgb, var(--color-warning, #c97e00) 6%, transparent);
}
.tool-card__verdict--unknown {
  border-left-color: var(--muted-border-color);
}
.tool-card__verdict .material-symbols-outlined {
  flex-shrink: 0;
}
```

### Tests: extend `tests/test_render.py`

```python
def test_group_messages_classic_tool_batch_unchanged(...) -> None:
    """Pre-phase-13 message sequences still produce ToolBatchBlock."""
    # tool_call + tool_result + assistant → ToolBatchBlock + MessageBlock


def test_group_messages_single_iteration_agentic_block(...) -> None:
    """One iteration with passed verdict produces AgenticToolBatchBlock."""
    # tool_call, tool_result, research_findings, review_verdict(passed), assistant
    # → AgenticToolBatchBlock with 1 iteration + MessageBlock


def test_group_messages_multi_iteration_agentic_block(...) -> None:
    """Three iterations produce a single block with 3 iterations."""
    # ... assert len(block.iterations) == 3 and statuses are
    # ["failed", "failed", "passed"].


def test_group_messages_max_iterations_reached(...) -> None:
    """Three iterations all failed → max_iterations_reached is True."""
    # ... three failed verdicts → block.max_iterations_reached is True.


def test_agentic_block_iteration_pairs_calls_with_results(...) -> None:
    """tool_call/tool_result rows are paired within their iteration."""
    # ... assert iteration.tool_calls is a list of (call, result) tuples.
```

---

## Sub-phase 13g — Tests + browser smoke test

### Add to `tests/test_integration.py`

One end-to-end agentic-mode journey:

```python
@pytest.mark.asyncio
async def test_agentic_mode_full_journey(client, db, mock_ollama) -> None:
    """User enables agentic mode → sends message → loop runs → answer arrives.

    Stages mock Ollama responses for:
    - One research tool call + findings text
    - mark_passed verdict
    - Streamed generation answer

    Asserts:
    - The persisted message sequence matches the expected agentic shape.
    - The SSE stream emits iteration-start, tool-call, tool-result,
      research-findings, review-verdict, token, done events in order.
    - The chat panel rendered on reload reconstructs the AgenticToolBatchBlock.
    """
    # ... full happy-path walkthrough.
```

### Browser smoke-test checklist

Per `CLAUDE.md`: **"Smoke-test UI changes in a real browser, not just
curl or pytest."** Run all of these manually before declaring 13
done:

- [ ] Open the app at `localhost:8000`. Visit `/settings`. Confirm
      the agentic-mode toggle is present and OFF by default. The
      read-only prompts <details> is NOT rendered.
- [ ] Click the toggle. Confirm the page updates (no reload) and the
      read-only prompts <details> appears. Expand it; confirm all
      three prompts are present in monospace.
- [ ] Click the toggle again to turn off. Confirm prompts disappear.
- [ ] Toggle back ON. Reload `/settings`. Confirm the toggle stays
      ON (DB persistence verified).
- [ ] Pick a tool-capable model in the composer. Send a message that
      should trigger research (e.g., "What's the latest paper on
      retrieval-augmented generation in my RAG sources?").
- [ ] Confirm the tool card renders with `Iteration 1` header,
      tool-call rows tick live, then `Research findings` row
      appears, then a `Passed:` or `Failed:` row appears. Generation
      streams tokens into the final bubble.
- [ ] Send a follow-up. Confirm research sees the prior turn's
      context (no need to re-ask for context already given).
- [ ] Send a hard question. Confirm multiple iterations render, each
      with their own header, and the verdict colours differ between
      failed and passed.
- [ ] Reload the page mid-stream (during generation). Confirm the
      assistant message comes back as
      `(response interrupted)` — agentic mode is not resumable in
      v1 per the locked decisions.
- [ ] Reload the page after completion. Confirm the chat panel
      reconstructs the iteration grouping correctly from persisted
      rows (no shape difference vs. the live render).
- [ ] Pick a non-tool-capable model (if available) with agentic mode
      ON. Send a message. Confirm the answer arrives via the
      single-agent path (no iteration headers) AND a small badge
      reads "agentic mode skipped (model has no tools)" near the
      assistant message. (See §No-tools fallback below for the
      badge implementation hook.)
- [ ] Toggle dark mode (via OS preference). Confirm verdict colours
      and the max-iterations badge remain legible.

### No-tools fallback badge — implementation note

The dispatcher in `app/generation.py` already silently picks
`_run_generation` when the model lacks tools, even with the toggle
on. To surface the badge:

1. In `_run_generation`, when the call site KNOWS we wanted agentic
   but fell back (pass a new kwarg `agentic_attempted: bool` to
   `_run_generation`), include the badge in the final done event's
   payload alongside the assistant bubble. The badge is a small
   `<span class="agentic-skip-badge">agentic mode skipped (model
   has no tools)</span>` rendered just below the assistant message.
2. Persist the badge state by adding a new column to `messages`?
   **No** — overkill for v1. Re-render on reload by inspecting
   `queries.get_agentic_mode(db)` + the chat's model capability at
   panel-render time. If `agentic_mode_on AND not
   model_supports_tools(model)`, render the badge below every
   assistant bubble in the chat.

Alternative if (2) is too noisy: only show the badge inline once,
attached to the most recent assistant message. Iterate during impl.

---

## Open questions for the implementer

Decisions deferred because they're better made with code in front of
you. None block 13a–13c (foundation); resolve before 13d–13f.

1. **Sanity-check the `start_generation` async conversion.** Plan
   body commits to async (see sub-phase 13d's dispatcher snippet
   + call-site updates). The conversion is purely additive: the
   three call sites already run inside `async def` handlers, and
   `start_generation` raises `GenerationInProgress` synchronously
   before its first `await`, so existing `except GenerationInProgress`
   blocks still catch it. If something about that contract turns
   out to be more subtle in practice (e.g., a test fixture that
   called it sync), fall back to the cache-peek variant that lived
   in the prior revision of this plan — saved in git history.

2. **Streaming findings.** The plan emits findings as a single SSE
   event because `maybe_tool_call` is non-streaming. If you decide
   to stream findings token-by-token (for UX consistency with
   generation), the workaround is to do a SECOND `stream_chat` call
   to the research model after its tool-calling loop exits, with a
   prompt like "Now write your findings." Extra round-trip per
   iteration. v2 candidate.

3. **`tool_specs_for_ollama()` and verdict tools coexisting.** The
   global `TOOLS` registry should never include the verdict tools.
   Confirm during implementation that no codepath unintentionally
   passes `REVIEW_TOOL_SPECS` to research or vice versa. A simple
   sanity assertion at the top of `_run_agentic_generation` is
   probably worth keeping.

4. **Inner cap behaviour when no findings text was produced.** The
   sketch synthesizes a placeholder findings message ("(No findings
   produced; …)"). Review may treat this as failed and ask for
   more research, costing an iteration. Alternative: skip the
   review pass for this iteration and force-continue. Implementer's
   call — try the synthesized-findings path first and watch
   real-model behaviour.

5. **Live vs historic row id parity.** Live SSE row ids use the
   monotonic-ns turn id; historic uses `hist-{first_row_id}`. The
   iteration-scoped suffix (`-iter-N-row-M`) needs to match on both
   paths so a mid-turn reload's reconstructed DOM aligns with any
   not-yet-consumed SSE events. Confirm during 13f implementation
   that the helper that builds row views uses identical formatting
   in both contexts.

6. **Card-shell OOB for "max iterations reached".** The plan sketch
   uses an `outerHTML` swap of the whole `<details>` to add the
   `data-max-iterations` attribute. That replaces the card's
   already-rendered contents — risky. Better: emit a tiny OOB swap
   on a `<span id="…-max-badge">` placed inside the summary at
   shell-render time, initially empty, filled in on cap-reach.

7. **Single-agent path vs. agentic path code reuse.** The current
   plan duplicates the inner tool-call loop body across the two
   producers. After the agentic path is working, consider
   factoring the tool-call inner loop into a shared helper
   (`_run_tool_calling_phase`) used by both producers. v2 cleanup,
   not blocking.

---

## What ships after Phase 13

- `agentic_mode = on` produces visibly better answers on
  research-shaped questions. Measured via the user's own
  trial-and-error in real chats (no formal eval suite in v1).
- The single-agent path (toggle off) is byte-identical to today's
  behavior. Phase 13 must not regress anything in phases 12a–12h.
- Test suite still green (`pytest`) and coverage holds at ~98% on
  `app/` + `main.py`. New code paths add their own tests; existing
  paths get no new tests beyond what the new code path requires.
- One retro file: `docs/retros/phase13-agentic-loop.md`. Following
  the established format, include a "Notes for future phases"
  section calling out anything that surprised the implementer.

---

## Suggested commit order

A reviewer should be able to land 13a→13c on their own before
touching 13d. Keeps the diff legible.

1. `feat: phase 13a — app_settings table + Role expansion`
2. `feat: phase 13b — agents module + system prompts`
3. `feat: phase 13c — review verdict tools (separate registry)`
4. `feat: phase 13d — agentic loop orchestrator`
5. `feat: phase 13e — /settings agentic toggle + read-only prompts`
6. `feat: phase 13f — historic render for agentic tool card`
7. `chore: phase 13 — integration tests + smoke-test pass`

Each commit's tests must pass before moving to the next.
