# Phase 12e retrospective — tool-usage card + cancellation safety net

## Scope

Two related sub-phases shipped back-to-back:

- **12e — aggregated tool-usage card above AI responses.** One
  `<details>` per assistant turn that used tools, with a live
  per-row mm:ss timer and an expandable list of
  `searching <source>: "<query>"` entries. Supersedes the original
  12e design in `docs/plans/phase12-tool-calling-detail.md` (one
  card per individual tool call) — the aggregated form scans
  better and the live counter conveys progress.
- **12e.1 — partial-assistant safety net on mid-stream
  cancellation.** Wraps `_stream_assistant_reply`'s body in a
  top-level try/finally so any non-normal exit (CancelledError,
  GeneratorExit, unhandled exception) at any phase persists a
  partial assistant row before re-raising. Fixed a real bug
  surfaced by the user: a reload after the tool card appeared but
  before the response finished streaming left the chat with
  orphan tool rows and no following assistant bubble.

The cancellation work was a follow-up to 12e because the
aggregated card made the orphan-tool-rows state much more visible
on reload (the historic-render path would show the card with
nothing after it). The card itself worked correctly; the bug was
in the pre-existing generator lifecycle.

## What landed

| Commit | Title |
|---|---|
| `06029d6` | feat: phase 12e — aggregated tool-usage card above AI responses |
| `319dd40` | fix: persist partial assistant on mid-stream cancellation |
| `e1626b2` *(partial)* | refactor: code-review pass on phase 12e/12g |

220/220 tests pass at the close of 12e; 226/226 after 12e.1's
six new cancellation tests.

## Decisions (and why)

- **One aggregated card per assistant turn, not per tool call.**
  The user redesigned the planned 12e mid-implementation. The
  aggregated form gives a live `using N tools…` counter that
  conveys progress, switches to past-tense `used N tool(s)` on
  completion, and keeps the chat panel less cluttered when a
  turn fires several tools.
- **Per-row live timer driven by one inline JS `setInterval`.**
  Tracking time per call (rather than a single turn-level timer)
  matched the user's mental model — "how long did each thing
  take" — and the implementation was small. The
  `window.__toolTimerStarted` guard keeps the panel-switch flow
  from accumulating intervals.
- **Live and historic paths share `_tool_card_shell.html` and the
  same `summary_text(count, done)` helper.** The card has only
  one source of truth for verb/plural/ellipsis so a refactor of
  any of those three can't silently desynchronize the two render
  paths.
- **SSE replay model for the live card.** First `tool-call`
  event emits the full `<details>` shell (OOB
  `beforebegin:#assistant-stream-…`). Subsequent `tool-call`s
  emit just a row-append (OOB `beforeend:#…-list`) and a summary
  outerHTML swap. `tool-result` OOB-replaces the row with a
  frozen variant carrying `data-elapsed-final`. All elements in
  every SSE payload are top-level so HTMX walks them as
  independent OOB swaps.
- **`group_messages_for_render` lives in a new `app/render.py`,
  not `queries.py`.** Render-time grouping is logic, not SQL.
  Putting it next to the SQL helpers would have started a slope
  toward bloated DB modules.
- **`ToolRowView` precomputes everything the row template
  needs.** The template just renders fields — no Jinja-level
  computation, no inline JSON parsing. Three mutually exclusive
  states (live ticking / frozen / historic-unpaired) are encoded
  via attribute presence rather than enum flags so the template
  predicates are direct.
- **Top-level `try/finally` for the cancellation safety net
  (12e.1).** The first attempt only caught `asyncio.CancelledError`
  in the streaming-phase except clause. The user reported "still
  not persisting on reload" because (a) Starlette raises
  `GeneratorExit` via `aclose()`, not `CancelledError`, and
  (b) reloads during tool execution land between the inner
  streaming-phase try and the streaming itself. Top-level
  `try/finally` catches everything at every phase.
- **`(response interrupted)` placeholder for zero-chunk
  cancellations on the append path; preserve original on the
  regen path.** A bare placeholder beats an orphan card with
  nothing after it; but for regenerate, clobbering the user's
  previous answer with a placeholder on accidental reload would
  silently destroy data. The `elif chunks:` guard handles both.

## What worked

- **Detailed plan with concrete code paid off.** The 12e plan
  (workspace + materialized at `docs/plans/phase12e-tool-card.md`)
  was specific enough that implementation barely deviated. The
  pre-implementation bug-review pass caught five potential issues
  before any code landed.
- **The Jinja `{% with %} {% include %} {% endwith %}` block was
  the right tool for the row template.** Without it the shell's
  `swap_oob` variable leaked into the row template (which has
  its own `swap_oob` for OOB-update contexts), producing
  duplicate `hx-swap-oob` attributes. Caught immediately by the
  two-tool test.
- **Direct-generator tests for the cancellation safety net.**
  TestClient buffers the full SSE response and can't simulate
  mid-stream disconnect. Bypassing it and driving the generator
  with `__anext__` + `aclose()` (12e.1) and then with
  `start_generation` + `task.cancel()` (12g) made the
  safety-net behavior verifiable.
- **The user's "still not persisting" feedback at 12e.1.** It
  prompted the diagnosis of `CancelledError` vs `GeneratorExit`
  and the rewrite to a top-level try/finally. The pattern from
  the phase 11 retro held: terse user feedback ("doesn't work")
  was enough to surface the architectural gap.
- **Plan-mode bug-review pass.** Identified pluralization
  (single `verb` swap missed the `tool` → `tools` flip), the
  iteration-cap bail not freezing in-flight rows, and the
  inline-JS multi-init concern BEFORE writing any code. Markdown
  edits are dramatically cheaper than reimplementation.

## What was tricky / went less well

- **First cancellation fix only caught `CancelledError`.** The
  Starlette disconnect path raises `GeneratorExit` via
  `aclose()`. The unit tests I wrote injected `CancelledError`
  directly so they passed, masking the real-disconnect gap. The
  user surfacing it in the browser was the test the test suite
  couldn't run.
- **The second cancellation fix's first cut still only wrapped
  the streaming phase.** A reload during tool execution (before
  streaming starts) bypassed the inner try/finally entirely. The
  user's reproduction (`(response interrupted)` showed but the
  REAL response was lost) made the gap visible. The fix was a
  full-function try/finally with a `persisted_or_errored` flag.
- **Mid-implementation re-indentation churn.** The first
  attempt to wrap the function body in `try:` left the inner
  `for call in tool_calls:` body under-indented. Recovered with
  `git checkout HEAD -- app/routes.py` and a clean rewrite.
  Lesson: a 200-line indent change is fragile via sequential
  Edits; better to rewrite the block in one shot.
- **JS tick driver placement.** First plan put it in
  `static/tool-timer.js`. The pre-implementation review caught
  that `CONVENTIONS.md:104-108` says to keep inline JS inline
  until a second use case appears. Moved into a script tag at
  the end of `_chat_panel.html` with a `__toolTimerStarted`
  guard against re-init on chat-switch.
- **Test for the user's actual scenario didn't exist initially.**
  The first set of cancellation tests covered "during
  streaming" but not "during tool execution." Added
  `test_stream_persists_placeholder_when_aclosed_during_tool_execution`
  AFTER the user reported the bug — that test would have caught
  the gap if I'd written it first.

## Surprises

- **HTMX OOB-swap inheritance via Jinja `{% include %}`.** I
  knew HTMX inherits `hx-*` attributes down the DOM tree but
  hadn't internalized that Jinja's default `{% include %}`
  inherits the parent template's variable scope too. The shell's
  `swap_oob="beforebegin:..."` was leaking into the row
  template's identically-named variable. `{% with swap_oob = none %}`
  fixed it cleanly.
- **`asyncio.CancelledError` vs `GeneratorExit` are different.**
  Both are `BaseException` subclasses (since 3.8) but they're
  raised by different mechanisms. A consumer task cancellation
  raises `CancelledError` at the consumer's `__anext__()` await;
  Starlette's cleanup then calls `aclose()` on the async
  generator, which raises `GeneratorExit` at the suspended
  yield. A `try/finally` catches both; an `except` for one
  silently misses the other.
- **SQLite synchronous writes inside an async `finally`.** I
  worried briefly whether the DB write would land before the
  cancellation resumed. It does — `sqlite3` is synchronous and
  has no await points, so the write completes before the
  exception continues to propagate.
- **`(response interrupted)` is a decent placeholder.** I added
  it as a defensive fallback expecting it'd be visible in 1% of
  reloads. In practice the user hit it on most early reloads
  because their model takes 10-30s of first-token latency.
  That's what motivated phase 12g.

## Open issues / follow-ups

- **The card's visual polish is minimal.** It works but could
  use tighter spacing, better alignment between the icon and
  text, possibly a subtle background when expanded. Deferred —
  the user signed off on the current look.
- **Tool-result content is not surfaced in the card** (per user
  decision). If they ever want to see the raw RAG hit text or
  the `current_time` result, the data is in the DB
  (`role = 'tool_result'`) — surface via a second click on the
  row or an additional `<details>` per row.
- **The `persisted_or_errored` flag pattern is mildly awkward.**
  Five sites set it true before returning. A more functional
  approach (e.g., the producer returns a Result-like value
  whose discriminant the finally inspects) would be cleaner but
  costs more code. Acceptable as-is.
- **`current_time` always renders as the generic fallback** in
  the card (`calling current_time(timezone='UTC')`). Could give
  it a custom display ("checking the current time") via a small
  extension to `format_tool_invocation`. Not worth doing
  speculatively.

## Notes for future phases

- **HTMX template-include variables inherit by default.** When
  including a template inside another, any same-named local in
  the outer scope leaks in. Use `{% with name = ... %}` or
  `{% include ... without context %}` to isolate. Mention this
  in any future template-heavy phase.
- **Test the actual disconnect path, not a proxy.** Manually
  raising `CancelledError` in a fake stream_chat doesn't
  exercise the same exception type as a real client disconnect
  (Starlette raises `GeneratorExit`). For task-based work, drive
  via `start_generation` + `task.cancel()` and let the finally
  run; for generator-based work, drive via `__anext__` +
  `aclose()`.
- **User reproduction beats local tests.** Two of the three
  cancellation gaps in 12e.1 were caught by the user's reload
  smoke test, not by the existing test suite. Whenever a user
  reports "still not working," the next test should reproduce
  that exact scenario before any code change.
- **Plan-mode bug review pass keeps earning its keep.** Phase
  11's retro called it out; phase 12e confirmed it again. Five
  issues caught in markdown that would have been bugs in code.
- **Inline JS until proven otherwise.** `CONVENTIONS.md:104-108`
  was the right call; the tool-timer's `__toolTimerStarted`
  guard works because the script ships with the panel template
  and runs on every panel render.

## Wrap-up

Phase 12e shipped the aggregated tool-usage card with live and
historic render parity; phase 12e.1 fixed a real bug surfaced
during smoke testing by adding a top-level try/finally safety net
to `_stream_assistant_reply`. Both were enabled by detailed plans
with bug-review passes and confirmed in the browser by the user.
The user's smoke-test feedback was the load-bearing test for the
disconnect-path correctness.

The 12e.1 safety net later became the catastrophic-failure
backstop inside `_run_generation` after phase 12g shipped the
proper resumable-generation architecture.
