# Tests + templates code review — pre-phase-13

**Date:** 2026-05-20
**Scope:** all 15 templates under `templates/` (~660 LOC) and all 14 test
files under `tests/` (~7400 LOC, plus `tests/README.md`)
**Reviewer:** Claude (Opus 4.7)
**Triggered by:** follow-up to backend review (`2026-05-20-backend-pre-phase-13.md`)

Most findings are about the **test layer** — templates are tight and
disciplined; only a couple of small items there. Test layer has real
architectural rot worth addressing before phase 13 adds another ~50
route tests + an `app/agents/` test module.

Same severity rubric: critical → architecture → quality → flagged.

---

## Critical — fix before phase 13

### T1. Tests for `_build_history_payload` and `_build_done_card_oobs` live in the wrong file

`tests/test_routes.py:2514-2566` and `tests/test_routes.py:2879-2970` test
functions that live in `app/generation.py`, not in `app/routes.py`:

- `test_build_done_card_oobs_*` (3 tests)
- `test_build_history_payload_handles_tool_roles`
- `test_build_history_payload_skips_malformed_tool_call_rows`

These got stranded when `_stream_assistant_reply` moved from `routes.py` to
`generation.py` in phase 12g. `tests/test_generation.py` already exists
and is the right home.

Phase 13 worsens this: the agentic loop's payload helpers
(`_build_research_payload`, `_build_review_payload`,
`_build_generation_payload`) will need their own tests. If the convention
"history-payload tests live in test_routes" persists by accident, those
will land in the wrong file too.

**Fix.** Move the 5 tests to `tests/test_generation.py` in a single
commit, no behavior changes. Also remove the function-body
`from app.generation import _build_history_payload` imports in favor of
top-level imports while you're there.

---

### T2. No `tests/conftest.py` means three near-duplicate autouse fixtures

The same module-level-state isolation logic appears in three files:

- `tests/test_routes.py:121-137` — `_isolate_live_generations`
- `tests/test_routes.py:140-171` — `_isolate_tool_capability`
- `tests/test_generation.py:15-23` — `_clear_live_generations`
- `tests/test_generation.py:26-37` — `_reset_capability_cache`
- `tests/test_ollama.py:31-43` — `_reset_capability_cache`
- `tests/test_integration.py:25-37` — `_reset_capability_cache`

Six near-identical fixtures across four files. Their bodies are 2–8 lines
each so the duplication is small in LOC but high in maintenance surface:
fix a subtle isolation bug (e.g., a new global) and you have to touch
four files in lockstep.

The phase 13 plan adds `agentic_mode` settings caching, prompt registry,
and possibly an agentic-state dict. That's a fourth piece of module-level
state, which means a fourth piece of duplicated fixture logic if we keep
this pattern.

**Fix.** Create `tests/conftest.py` with one autouse fixture:

```python
# tests/conftest.py
import pytest
from app import generation, ollama


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """Reset every module-level cache or registry around each test.

    Add new entries here when phases introduce new module-level state.
    Centralizing the list catches the "I added a global and forgot to
    isolate it in tests" failure mode.
    """
    saved_gens = dict(generation.live_generations)
    generation.live_generations.clear()
    ollama.reset_capability_cache()
    yield
    generation.live_generations.clear()
    generation.live_generations.update(saved_gens)
    ollama.reset_capability_cache()
```

Then delete the four duplicate fixtures. The convention note in the
phase 13 plan ("no shared conftest") is the wrong call IMO; the cost of
not having one is now visible.

---

### T3. `_FakeClient` boilerplate copy-pasted ~5 times

The same `_FakeClient(httpx.AsyncClient)` wrapper appears at:

- `tests/test_tools.py:398-417` (test_query_rag_returns_formatted_chunks_on_success)
- `tests/test_tools.py:463-476`
- `tests/test_tools.py:503-516`
- `tests/test_tools.py:543-556`
- `tests/test_generation.py:520-532` (`_patch_rag_http`)

Every copy is ~10 lines. Five copies, all with the same documented
gotcha ("snapshot the real AsyncClient BEFORE monkeypatching or you'll
get unbounded recursion"). When phase 12-ish-but-actually-13 adds a tool
that needs HTTP mocking, that's a sixth copy.

**Fix.** Move to `tests/conftest.py` (or `tests/_helpers.py` if you
prefer a non-fixture module):

```python
@pytest.fixture
def patch_async_client(monkeypatch):
    """Return a function that patches `<module>.httpx.AsyncClient`
    with a MockTransport-backed wrapper.

    Usage:
        patch_async_client(app.tools.rag, handler)
    """
    def _patch(target_module, handler):
        real = httpx.AsyncClient
        class _Fake:
            def __init__(self, *_a, **_k):
                self._c = real(transport=httpx.MockTransport(handler))
            async def __aenter__(self): return self._c
            async def __aexit__(self, *_): await self._c.aclose()
        monkeypatch.setattr(target_module.httpx, "AsyncClient", _Fake)
    return _patch
```

Five test bodies collapse from ~15 lines to one fixture call each.

---

## Architecture — addressing growing pains

### T4. `tests/test_routes.py` is 3080 lines — needs splitting

Already past the point where "navigate with grep" is the only viable
read mode. Phase 13 will add another ~50 tests covering the agentic
loop's SSE event flow, settings toggle, model-capability fallback badge,
etc. A single 3500-line test file is hard to review and hard to bisect
for "which area broke."

Natural splits along route concern:

- `test_routes_models.py` (~5 tests) — `/models` filtering
- `test_routes_chats.py` (~20 tests) — chat CRUD, sidebar, rename, delete
- `test_routes_messages.py` (~10 tests) — POST `/messages` + GET `/stream`
- `test_routes_settings.py` (~15 tests) — `/settings` RAG server CRUD
- `test_routes_tool_streaming.py` (~15 tests) — tool-call SSE
- `test_routes_cancellation.py` (~6 tests) — the cancellation suite at 2122+

The `make_client` fixture and helpers (`_create_chat_db_only`,
`_stream_handler`, etc.) move into `tests/conftest.py` alongside T2.

This isn't critical for correctness — flagging as architecture because
the file is past its useful size and phase 13 will compound it.

---

### T5. Cancellation tests at the route layer don't actually test what they claim

`tests/test_routes.py:2279` (`test_stream_persists_partial_assistant_on_cancellation`)
and `tests/test_routes.py:2332`
(`test_stream_persists_placeholder_when_cancelled_with_zero_chunks`)
simulate cancellation by `raise asyncio.CancelledError` from inside
the mock `stream_chat` generator. That isn't what happens on a real
client disconnect — a real disconnect surfaces as
`httpx.RemoteProtocolError` or similar, raised at a different code
point.

The phase-12g-style cancellation tests at
`tests/test_routes.py:2122-2376` and the parallel
`test_regenerate_cancellation_*` are more realistic: they drive
`start_generation` directly and `state.task.cancel()` the producer
task. That matches what happens when the FastAPI request handler is
cancelled.

The 2279/2332 tests are testing *some* path through the `finally`
block, just not the path that fires in production. They pass today
because the safety-net `finally` block doesn't distinguish between
cancellation flavors.

**Fix.** Either delete the two synthetic tests (the realistic ones at
2122+ cover the same `finally`-block invariants) or rewrite them to
drive cancellation via `state.task.cancel()`. I'd vote delete — keeping
two tests of the same invariant via different mechanisms makes future
maintenance harder, not safer.

---

### T6. Test isolation depends on environment variables that leak across the lifespan

`test_routes.py:92-93` and similar:
```python
monkeypatch.setenv("DB_PATH", str(tmp_path / "chats.db"))
monkeypatch.setenv("OLLAMA_HOST", "http://test")
```

This sets `DB_PATH` *before* `TestClient(app)` triggers the lifespan,
which opens a connection at the env-pointed path. Works today, but the
chain `env var → lifespan → app.state.db` is implicit. A test that
needs a *different* DB partway through (rare but possible) can't switch
— the connection is already open.

The integration test fixture (`tests/test_integration.py:53-119`) reveals
a related smell: the lifespan opens a real `httpx.AsyncClient` via
`create_client()` and stores it on `app.state.ollama_client`. Then the
test overrides `get_ollama_client` to return a *different* mock client.
The real client is opened, never used, closed at teardown. Cheap waste,
but reveals the gap.

**Fix.** Not urgent, but: a `db_path` fixture that opens the connection
directly and overrides `get_db` (mirroring the `get_ollama_client`
override pattern) would make the seam explicit and remove the env-var
dance. Phase 13 will likely want this anyway when it adds the
`agentic_mode` setting fixture.

---

## Code quality

### T7. 37 function-body `from app import ...` statements in `test_routes.py`

There's only ONE top-level `from app` import in the entire 3080-line
file (`from app.dependencies import get_ollama_client`). Every other
`app` import is inlined inside a test body. Many are unnecessary and
just add noise:

```python
def test_thing(...):
    import json as _json
    from datetime import datetime, timezone
    from app.queries import Message
    from app.generation import _build_history_payload
    ...
```

Most of those imports have no side-effect concern — they could safely
move to the top of the file. The handful that DO need to be inlined
(tools registration side effects, e.g., `from app.tools import rag as
_rag  # noqa: F401`) should stay inlined with a comment.

**Fix.** Hoist all non-side-effect imports to the top of each test file
in a single sweep. ~30 lines saved across the test suite, much improved
readability.

### T8. `import json as _json` — alias is defensive against a non-existent shadow

Appears throughout `test_routes.py`, `test_integration.py`,
`test_generation.py`. There's no test-level `json` variable being
shadowed; the alias is pure ceremony. Plain `import json` is fine.

### T9. Some tests reach into private (underscore-prefixed) functions

`_build_history_payload`, `_build_done_card_oobs`, `_format_chunks`,
`_health_url`, `_placeholder_name`, `_now_iso` — all underscore-prefixed
to signal "module-internal," all directly imported by tests. The
underscore lies because the testing surface IS the public API in
practice.

Either drop the underscores (signal "this is part of the testable
surface") OR move them to an `_internal.py` module per package
(signal "internal-by-convention, tests opt in by importing the
internal module explicitly"). Right now the underscores are noise
in both places.

**Fix.** Drop the underscores on functions that have direct tests; keep
them on functions that genuinely have no tests and are only used inside
their own module.

### T10. `test_build_done_card_oobs_freezes_in_flight_rows` tests dead code

`tests/test_routes.py:2539-2565`. The test's own docstring says: *"Dead
code in the current control flow — exercised here directly so the safety
net stays covered."*

Two options:

1. Delete the dead branch in `_build_done_card_oobs` and this test.
   The branch fires only if a future codepath introduces an `await
   run_tool(...)` that raises with `in_flight` still populated; today's
   code awaits synchronously, deletes after, so `in_flight` is always
   empty at the `else:` arm.
2. Keep the safety net + test, but rename the test (or add a comment)
   to clarify it's defensive coverage for a hypothetical future bug.

Either is fine; the current "we tested dead code on purpose" comment
suggests option 1 was rejected once. Worth a deliberate revisit.

### T11. README is stale on test count and on the marker recipe

`tests/README.md:36` says *"Whole suite (102 tests) runs in under 2
seconds."* The actual count per CLAUDE.md is **297/297**. Phase 12
roughly tripled the suite.

`tests/README.md:53-66` documents how to add `@pytest.mark.integration`
markers and configure pytest deselection, but the recipe isn't actually
applied — no `pyproject.toml` / `pytest.ini` marker config exists. The
recipe is forward-looking, not current state.

**Fix.** Refresh the count (one-line edit) and either implement the
marker recipe or label the section "Future: integration tests" to
signal it's aspirational.

---

## Templates

Templates are well-disciplined — most of what I'd flag the conventions
doc already articulates (the "non-outerHTML OOB unwraps its root"
gotcha, the inline-CSS-tested-by-substring contract, the markdown
filter chain). Three observations:

### TM1. `_message.html:19` — `| markdown | safe` is XSS-shaped on untrusted models

```jinja
{{ message.content | markdown | safe }}
```

Python's `markdown` library doesn't sanitize HTML inside markdown
input by default. If a model emits `<script>alert(1)</script>`, the
filter passes it through and `| safe` renders it.

For a local Ollama with user-trusted models, this is fine — the
threat model is "I trust the model I pulled." If the project ever
adds a "load model from a URL" feature or supports community model
sharing, this becomes an XSS vector. Flagging now so it's a known
property, not a discovery.

**Fix (if scope ever changes).** Use `markdown` with `bleach` for HTML
sanitization, OR switch to a markdown library with built-in safe mode
(`mistune` supports this). Not urgent for current scope.

### TM2. `_assistant_placeholder.html:27` — one 530-char line

The placeholder is a single `<div>` with eight attributes
crammed onto one line. Splitting attributes onto separate lines would
match the formatting in the other templates and make diffs cleaner
when phase 13 adds the agentic-mode badge attribute.

**Fix.** Break at attribute boundaries:
```html
<div id="assistant-stream-{{ conversation_id }}"
     class="message message--assistant message--streaming"
     data-role="assistant"
     hx-ext="sse"
     sse-connect="{{ stream_url }}"
     sse-swap="token,done,error,title,tool-call,tool-result"
     hx-swap="beforeend"></div>
```

Phase 13's new SSE events (`research-findings`, `review-verdict`,
`iteration-start`) will need to be added to `sse-swap`; a per-attribute
layout makes that diff readable.

### TM3. `index.html:64-72` — view dispatch will get unwieldy

The `if settings_view / elif conversation / else composer` cascade
works for three view shapes. Phase 13 doesn't add a fourth view
(agentic-mode toggle lives inside `_settings.html`), but if a future
phase adds, say, a "memory" view or a "history search" view, this
cascade is the wrong abstraction.

Not urgent. Flag for if a fourth top-level view appears.

---

## Recommended sequencing

If you adopt these:

1. **T1** (move misplaced tests) — pure file move, ~5 minutes.
2. **T11** (README refresh) — 5-minute doc edit.
3. **T2** (`tests/conftest.py`) — single commit, deletes ~50 lines of
   duplicate fixtures.
4. **T3** (`patch_async_client` fixture) — depends on T2; collapses
   ~75 lines of `_FakeClient` boilerplate.
5. **T4** (split `test_routes.py`) — bigger lift but the right time is
   before phase 13 adds another ~50 tests to the file.
6. **T7** (hoist function-body imports) — bundle into T4 since both
   touch every test in `test_routes.py`.
7. **T5** (delete synthetic cancellation tests) — depends on T4.
8. **T6** (`db_path` fixture) — defer; not urgent.
9. **T8 / T9 / T10** — small cleanup, do any time.

**Backend review's items still take priority.** This file is the
nice-to-haves; the backend review's items 1–4 are the must-haves
before phase 13.

---

## Open questions

1. **`tests/conftest.py` — yes/no?** The phase 13 plan explicitly
   rejects it. I'm arguing the cost of *not* having one is now visible
   (T2). Want to confirm the call before I (or the agent running
   phase 13) introduces one.
2. **Split `test_routes.py` now or after phase 13?** Splitting after
   means another ~600 lines piled onto an already-large file. Splitting
   now means more diff churn in the phase 13 PR. I lean "split now."
3. **Hoist imports in a separate commit, or bundle into T4?** Bundling
   means one commit per file when splitting. Standalone means a
   reviewable mechanical pass first.
