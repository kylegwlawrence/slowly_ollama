# Backend code review — pre-phase-13

**Date:** 2026-05-20
**Scope:** every `.py` under `app/` plus `main.py` (~4250 LOC, 14 modules)
**Reviewer:** Claude (Opus 4.7)
**Triggered by:** request to clean up before starting phase 13 (agentic multi-agent loop)

Calibrated against `docs/CONVENTIONS.md`, the phase 6–12 retros, and the
phase 13 plan — flagging real issues, not differences of taste from
already-validated choices.

Findings are ordered by **impact on phase 13**, not by severity in
isolation. Items 1–4 will compound when the agentic orchestrator lands,
so they're worth addressing first.

---

## Critical — fix before phase 13

### 1. Templates live in `routes.py`, so every other module has to lazy-import across the layer

`app/generation.py` does `from app.routes import templates` **four times** as
a function-body lazy import (lines `174`, `340`, `389`, `453`) explicitly to
dodge a circular dependency. The `templates = Jinja2Templates(...)` instance
and the `_render_markdown` filter setup live at `app/routes.py:74-112`.

Phase 13's `app/agents/loop.py` will need templates for the same reasons
(tool rows, message bubble, OOB card shell). The phase 13 plan already
imports `_emit` and `_maybe_emit_title` from `app.generation`, which means
it picks up this lazy-import problem transitively. We'd be stacking two
layering violations.

**Fix.** Extract a new `app/templates.py` (~40 lines) that owns:
- `templates = Jinja2Templates(...)`
- `_md_converter`, `_LIST_ITEM_RE`, `_ensure_list_spacing`, `_render_markdown`
- the `templates.env.filters["markdown"] = _render_markdown` wiring

Then `routes.py`, `generation.py`, and (in phase 13) `agents/loop.py` all
do `from app.templates import templates` at the top — no lazy imports.

---

### 2. `with open_connection() as conn:` leaks connections in `app/tools/rag.py` (2 sites)

`app/tools/rag.py:54` (`_list_source_names`) and `app/tools/rag.py:132`
(`query_rag`) both do `with open_connection() as conn:`.

`sqlite3.Connection.__exit__` **commits/rolls back but does not close**.
The connection stays open until GC reclaims it. The CONVENTIONS doc's
"never `with closing(conn)`" warning is specifically about the *shared*
connection on `app.state.db` — for a one-shot private handle, `closing()`
is exactly what you want. The "91 unclosed SQLite connection warnings"
the README flags as benign are this leak.

**Fix (minimal).** Wrap with `contextlib.closing`:
```python
from contextlib import closing
with closing(open_connection()) as conn:
    ...
```

**Fix (better, see item 7).** Make tools accept the shared connection so
they don't open private ones at all.

---

### 3. `_run_generation` is 270 lines and is about to fork into a parallel orchestrator

`app/generation.py:427-699` is a single function doing dispatcher work,
tool-call loop, persistence, OOB construction, error handling, post-stream
finalization, title hook, and safety-net `finally`. Phase 13's plan adds
a parallel `_run_agentic_generation` and the draft duplicates ~50 lines
verbatim (the four `OllamaUnavailable / OllamaProtocolError` except blocks,
the `persisted_or_errored` flag, the `finally` partial-persist).

If we go into phase 13 with two ~300-line producers sharing ~50 duplicated
lines, those lines drift on the first one-sided fix.

**Fix.** Extract three helpers in a pre-phase-13 commit:

- `async def emit_ollama_error(state, exc, where: str)` — replaces the
  four near-identical `except` blocks
- `async def persist_partial(state, db, conv_id, on_complete, chunks)` —
  the `finally`-block body
- `def render_tool_card_oobs(...)` — move `_build_done_card_oobs` and
  the per-call live/frozen row construction out of `generation.py` and
  into `render.py`, which already owns `ToolRowView`, `summary_text`,
  `format_elapsed_mm_ss`. `render.py` is the natural home for OOB HTML;
  `generation.py` should orchestrate only.

After this, `_run_generation` is ~150 lines and `_run_agentic_generation`
can reuse the helpers without copy-paste.

---

### 4. `_build_history_payload` can produce an Ollama-invalid history if a `tool_call` row is corrupt

`app/generation.py:288-303`:

```python
if m.role == "tool_call":
    try:
        call = json.loads(m.content)
        out.append({"role": "assistant", "tool_calls": [...]})
    except (json.JSONDecodeError, KeyError, TypeError):
        continue   # ← skips the call but the next tool_result still gets appended
elif m.role == "tool_result":
    out.append({"role": "tool", "content": ...})
```

If a `tool_call` row is corrupt and gets skipped, the next `tool_result`
row becomes an orphan: Ollama sees a `role: "tool"` message with no
preceding `assistant` + `tool_calls`, returns 400, and the whole
conversation becomes unsendable.

Likelihood is low (we control writes), but the failure mode is "this chat
is dead." Render-side `app/render.py:227-235` has the same swallow-on-decode
pattern but renders `name="?"`, so it's safer there.

**Fix.** Track skipped calls and drop their paired result:

```python
skip_next_result = False
for m in history:
    if m.role == "tool_call":
        try:
            call = json.loads(m.content)
            out.append({...})
        except (json.JSONDecodeError, KeyError, TypeError):
            skip_next_result = True
            continue
    elif m.role == "tool_result":
        if skip_next_result:
            skip_next_result = False
            continue
        out.append({...})
    else:
        out.append({...})
```

---

## Architecture — addressing growing pains

### 5. `_emit` / `_maybe_emit_title` will be shared by two producers — the underscores will lie

The phase 13 plan imports `_emit` and `_maybe_emit_title` directly from
`app.generation`. The leading underscore says "private to this module";
once a second module consumes them, that's no longer accurate.

**Fix.** During the item-3 refactor: drop the underscores and either keep
them in `generation.py` as the producer-runtime surface, or move them to
a new `app/producer_runtime.py`. `_maybe_emit_title` is arguably its own
concern — `app/title.py` would be a clean home, and both producers call
into it.

---

### 6. Module-level globals have no test-isolation contract

Three module-level mutable globals:

- `app/generation.py:100` — `live_generations: dict[int, GenerationState]`
- `app/ollama.py:172` — `_capability_cache: dict | None`
- `app/tools/__init__.py:146` — `TOOLS: dict[str, ToolSpec]`

`reset_capability_cache()` is called by tests; `live_generations` is not
cleared between tests; `TOOLS` is registered at import time. Tests pass
today because nothing happens to interfere. A future test that forgets to
clear `live_generations` could pass in isolation and fail when run after
the integration suite.

The phase 13 plan explicitly states "the existing tests do not share via
conftest, and 13 should not introduce one." I'd push back gently — the
cost of *not* having one is rising, and phase 13 introduces a fourth
module-level state surface (agentic-mode setting cache, if we add one).

**Fix.** A `tests/conftest.py` with one autouse fixture:

```python
@pytest.fixture(autouse=True)
def _reset_module_state():
    yield
    generation.live_generations.clear()
    ollama.reset_capability_cache()
```

---

### 7. Tools that need DB access open private connections — a layering inversion

`app/tools/rag.py` calls `open_connection()` to get a fresh SQLite handle,
even though `query_rag` runs inside a route handler that already holds
the shared connection on `request.app.state.db`. The chain
`route → generation → run_tool → tool → open_connection` skips three layers
that already have `db` in scope.

This causes item 2's leak today; it also makes tool tests need either a
real SQLite file or a mock of `open_connection`. Phase 13's `mark_passed` /
`request_more_research` "tools" don't need DB access, but a future tool
that, say, queries chat history will hit the same wall.

**Fix.** Introduce a `RunContext` carried through `run_tool`:

```python
@dataclass(frozen=True)
class RunContext:
    db: sqlite3.Connection
    client: httpx.AsyncClient

async def run_tool(name: str, args: dict, ctx: RunContext) -> ToolResult:
    spec = TOOLS.get(name)
    ...
    sig = inspect.signature(spec.func)
    kwargs = {**args, "ctx": ctx} if "ctx" in sig.parameters else args
    result = spec.func(**kwargs)
    ...
```

Tools opt in by declaring `ctx: RunContext` (the decorator adds `ctx` to
the parameter-skip list alongside `self` so it doesn't appear in the JSON
schema sent to the model). `query_rag(ctx, source, query)` then uses
`ctx.db` directly.

Similar shape to FastAPI's `Depends` pattern. Larger change — flag for
discussion rather than mandate.

---

## Code quality

### 8. tool_call JSON encoding/decoding is duplicated in three places

`app/generation.py:519` writes `json.dumps({"name": name, "arguments": arguments})`.
The same shape is parsed in `app/render.py:228` and `app/generation.py:291`.
There's no `encode_tool_call` / `decode_tool_call` to mirror the
`tool_result` envelope helpers that already exist in
`app/tools/__init__.py:63-116`.

**Fix.** Add the sibling helpers next to the tool_result ones:

```python
def encode_tool_call(name: str, arguments: dict) -> str:
    return json.dumps({"name": name, "arguments": arguments})

def decode_tool_call(content: str) -> tuple[str, dict]:
    try:
        payload = json.loads(content)
        return payload["name"], payload.get("arguments") or {}
    except (json.JSONDecodeError, KeyError, TypeError):
        return "?", {}
```

Three call sites switch to these helpers. Phase 13's `research_findings`
and `review_verdict` rows can follow the same pattern.

---

### 9. `_md_converter` is shared mutable state across FastAPI's threadpool

`app/routes.py:78` — module-level `_md.Markdown()` instance, `.reset()`
called per render at line `108`. `reset()` clears converter state but not
extension state, and FastAPI runs sync endpoints in a threadpool. Two
concurrent markdown renders could race.

Single-user local app, so unlikely to manifest. But it's a latent footgun
and the fix is one-line: build a fresh `Markdown()` per call. Cost: a few
µs per render.

---

### 10. `tool_specs_for_ollama` returns shared mutable references

`app/tools/__init__.py:288-298` returns `spec.parameters_schema` directly
with no copy. `refresh_query_rag_source_description` (line 246) relies on
this exact mutation. But the relationship is implicit — any other caller
could mutate accidentally.

**Fix.** `copy.deepcopy(spec.parameters_schema)` in the comprehension;
have `refresh_query_rag_source_description` continue to mutate the
registry's `spec.parameters_schema` directly. The two concerns become
independent.

---

### 11. `_TYPE_TO_JSON_SCHEMA` silently defaults non-primitive types to "string"

`app/tools/__init__.py:152-157` maps `str/int/float/bool`. Anything else
(e.g., `list[str]`, `dict`) silently becomes `{"type": "string"}` and the
tool crashes when Ollama passes a string. Phase 13's verdict tools
sidestep this with hand-written specs, but a future tool author writing
`def my_tool(items: list[str])` will get a runtime crash on first call.

**Fix.** Raise `TypeError` at decoration time for unknown types. Fast
feedback at app start > debugging "why is `items` a string here?" later.

---

### 12. CONVENTIONS doc has rotted around persistence

The doc (lines 70-73) says: *"Persist assistant text AFTER the stream
completes. If the client disconnects mid-stream, the partial response is
discarded. Documented tradeoff in `app/routes.py`."*

Phase 12g made both claims false: text is buffered then persisted; client
disconnect no longer loses the response (the producer task survives); and
the relevant code moved to `app/generation.py`. Worth an update in the
same commit as the item-1 refactor so the docs catch up.

---

## Future risks — flagged, not blockers

- **`live_generations` grows unboundedly** across the process lifetime.
  One entry per conversation ever generated; never evicted unless a new
  generation starts for the same conv. For a months-running process with
  hundreds of chats, this adds up. A simple LRU cap (last ~50, say) would
  bound memory without sacrificing the slow-reload-replay property.
- **`_capability_cache` 60s TTL** means a freshly-pulled model is
  invisible for up to a minute. Acceptable; flagging for completeness.
- **No retry on transient Ollama failures.** A flaky local connection
  surfaces as user-visible 503. Probably the right call (transparency
  over magic), but worth a deliberate decision rather than an accident.
- **`format_tool_invocation` uses Python's `repr()`** on argument values
  (`app/tools/__init__.py:359`). String args render as `'foo'` with single
  quotes — looks weird in a chat UI. Minor cosmetic.
- **`/models` returns 200-with-disabled-option on Ollama failure** instead
  of the documented 503 mapping (`app/routes.py:333-350`). The reason
  (HTMX won't swap non-2xx into a dropdown) is sound and inline-documented,
  but CONVENTIONS lists the 503 mapping as universal. Worth a one-line
  caveat in the doc.

---

## Things I deliberately did NOT flag

These are deliberate choices justified in retros / the convention doc:

- The two `_now_iso` helpers (`queries.py` and `rag_servers.py`) —
  duplicated to keep modules decoupled, per `rag_servers.py:9-12`.
- The `for/else` apology in `_run_generation` — comment is mildly
  misleading about `in_flight` content (it's always empty when the cap
  fires because we await each `run_tool` synchronously), but behavior is
  correct.
- No `/api` prefix; no JSON anywhere; `check_same_thread=False` on the
  shared SQLite; the 99% coverage ceiling.
- Side-effecting tool registration imports in `app/routes.py` with
  `# noqa: F401`.

---

## Recommended sequencing

If you adopt the critical items, this is the cheapest order:

1. **Item 1** (extract `app/templates.py`) — pure mechanical move, no
   behavior change. Removes 4 lazy imports.
2. **Item 2** (`closing()` wrapper) — 2-line fix, kills the leak warnings.
3. **Item 4** (skip orphan tool_results) — 5-line defensive fix.
4. **Item 3** (refactor `_run_generation`) — biggest diff; do this last
   so items 1 and 2 are out of the way and the refactor only touches
   `generation.py` + `render.py`.
5. **Item 8** (`encode_tool_call` / `decode_tool_call`) — bundle into the
   item-3 commit; the new helpers land in `app/tools/__init__.py` and
   the three call sites switch over.
6. **Item 6** (`conftest.py`) — standalone commit, can land any time.

Items 5, 7, 9, 10, 11, 12 can be deferred or bundled as appropriate.

---

## Open questions for the user

1. Should items 1–4 land as one or several commits? (My take: items 1 and
   2 are atomic single-commit fixes; items 3 and 4 are their own commits.)
2. Item 7 (RunContext) — in scope, or defer until a future tool actually
   needs it?
3. Want this review extended to `templates/` and `tests/` as a follow-up?
