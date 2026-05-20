# Phase 13 retrospective — agentic multi-agent loop

## Scope

The single-agent tool loop shipped in phase 12 hands one model both
"do tool calls" and "write the final answer". For research-shaped
questions this often produces shallow answers — the model stops
calling tools too early, doesn't self-critique, and never circles
back to fill gaps.

Phase 13 adds an opt-in three-agent loop behind a global toggle:

1. **Research agent** — full chat history + tool access; runs a
   tool-calling inner loop, ends each iteration with a free-text
   "findings" synthesis.
2. **Review agent** — sees only the original question + the latest
   findings; calls one of two custom tools (`mark_passed` /
   `request_more_research`) to encode its verdict.
3. **Generation agent** — when review approves, writes the final
   assistant message from the question + findings only.

Loop cap: 3 research↔review iterations. On exceed, force-generate
with whatever findings exist and badge the card with "(max reached)".
All three agents use the conversation's pinned chat model — no
per-agent model overrides in v1.

The phase shipped as seven incremental sub-phases (13a–13g), each
independently committable behind the toggle being off by default.
A pre-phase cleanup pass (5 commits before 13a) extracted
`app/templates.py`, hoisted producer-runtime helpers out of
`_run_generation`, moved tool-card OOB rendering into `app/render.py`,
and added `encode_tool_call` / `decode_tool_call` envelope helpers —
all dependencies for 13's clean integration. See
`docs/code_reviews/2026-05-20-*.md` for the reviews that drove the
cleanup.

## What landed

| Commit | Title |
|---|---|
| `acfa05c` | docs: pre-phase-13 code reviews — backend, tests, templates |
| `0e5ab46` | refactor: extract app/templates.py to retire lazy imports |
| `39dd936` | fix: close private SQLite connections in query_rag tool |
| `fd930db` | fix: drop orphan tool_result rows paired with corrupt tool_call rows |
| `a3022ed` | refactor: add encode_tool_call / decode_tool_call helpers |
| `635c2cd` | refactor: extract emit_ollama_error / maybe_persist_partial / signal_done |
| `3f75b2a` | refactor: move tool-card OOB rendering from generation.py to render.py |
| `4cf040d` | test: consolidate module-state isolation into tests/conftest.py |
| `bd2ca56` | test: move _build_history_payload tests from test_routes to test_generation |
| `e5ebcb4` | test: hoist function-body imports in test_routes.py to module level |
| `d8eea7c` | docs: refresh CLAUDE.md + README.md for post-cleanup state |
| `8f173f3` | docs: align phase 13 plan with pre-phase-13 cleanup |
| `e3ce272` | feat: phase 13a — app_settings table + Role expansion |
| `092f0c6` | feat: phase 13b — agents module + system prompts |
| `5ad0133` | fix: post-13b review nits — render skip, type guard, case-sensitivity test |
| `0c2b8b0` | feat: phase 13c — review verdict tools (separate registry) |
| `3f63146` | fix: parse_verdict tolerates non-dict `arguments` shapes |
| `42bbe35` | feat: phase 13d.1 — agentic render helpers + templates |
| `5227110` | fix: 13d.1 review nit — visible max-iterations marker |
| `851f934` | feat: phase 13d.2 — agentic-loop orchestrator |
| `a2e10fc` | fix: 13d.2 review nits — dedicated max-iterations event + replace test |
| `9a496b4` | feat: phase 13d.3 — dispatcher wires agentic toggle to the orchestrator |
| `38f30b9` | feat: phase 13e — /settings agentic toggle + read-only prompts |
| `9fcb00e` | feat: phase 13f — historic render for agentic tool card |
| `9599ffb` | feat: phase 13g — agentic-skipped banner + end-to-end tests |
| `675fc20` | fix: 13g review nits — DRY the model-not-capable stub |

26 commits total: 12 pre-phase cleanups + 14 phase commits. Tests at
phase close: **393/393 passing**, coverage **98%** on `app/` + `main.py`
(was 309/309 at phase start, +84 net). `app/render.py` lands at 100%;
the lingering gaps are in `app/agents/loop.py` (mid-stream Ollama error
handlers, 4 lines), `app/generation.py` (3 lines, same shape), and
pre-existing defensive branches.

## Decisions (and why)

- **Verdict tools live in a separate registry, never the global one.**
  The review agent calls `mark_passed` / `request_more_research` to
  encode its decision — built on top of Ollama's tool-call API
  because the model is already trained to call tools reliably, more
  robust than asking for JSON in free-form text. If we'd registered
  them in `app.tools.TOOLS`, the research agent would *see* them and
  could `mark_passed` mid-research, short-circuiting the loop in a
  way the design specifically rules out. Lives in
  `app/agents/verdict_tools.py` with hand-written specs.
- **Loop cap = 3 iterations; per-pass tool cap = 5.** Both are
  module-level constants in `app/agents/loop.py`. Deliberately NOT
  aliased to the single-agent `_TOOL_ITERATION_CAP` — same number
  today, different concepts. Aliasing would couple future changes:
  raising single-agent to 8 would silently make agentic 8 iterations
  × 5 tool calls = 40 tool calls per turn.
- **Findings emit as a single SSE event, not token-by-token.** Ollama's
  `maybe_tool_call` returns content all-at-once when the model stops
  calling tools; streaming findings would require a second
  `stream_chat` call per iteration. Generation streams normally
  (its output IS the user-visible answer). v2 candidate if findings
  feel too "snap into existence".
- **Retry context: cumulative + feedback-as-user-message.** When
  review fails, the next research pass sees the full intra-turn
  history of prior tool calls/results/findings plus a new `user`-role
  message carrying the review's feedback. No reset — the model
  doesn't repeat queries. Feedback lives in `intra_turn` (not as a
  one-shot parameter) so it persists across every `maybe_tool_call`
  inside the iteration, not just the first one.
- **Card layout: one `<details>` per assistant turn.** Same outer
  shell as the single-agent card so base `.tool-card` CSS applies;
  inside, iteration headers + tool rows + findings rows + verdict
  rows in render order. New block type `AgenticToolBatchBlock` for
  historic render. Live SSE path emits iteration-start /
  research-findings / review-verdict events with iteration-scoped
  row ids (`{card_id}-iter-N-row-M`) so mid-turn reloads reconstruct
  matching DOM.
- **Iteration-cap constant promoted to `app/agents/__init__.py`.**
  `render.py` needs to know the cap (for `max_iterations_reached`);
  `loop.py` defines it. Importing from `loop.py` would cycle
  (loop imports render). Moved the public `AGENTIC_ITERATION_CAP`
  to the package surface; loop.py aliases it locally.
- **Max-iterations marker as a SIBLING span, not a child.** The
  summary span gets outerHTML-swapped on the done event; if the
  badge lived inside it, the badge would vanish. Sentinel `<span
  id="…-max-marker">` is planted at shell-render time, sibling to
  the summary span, and filled by an OOB swap when the cap hits.
  Historic template renders the same DOM shape (empty span when
  cap wasn't hit) so live and historic paths produce identical
  selectors.
- **Silent fallback when the model lacks tool support.** Agentic
  mode globally on but chat's pinned model can't do tools →
  dispatcher picks `_run_generation`. Banner above `#messages`
  explains why — computed at render time via
  `_compute_agentic_skipped(db, client, model)` so it reacts to
  model swaps without persisting state per message. Short-circuits
  to False when agentic mode is off (no `/api/tags` round trip).
- **`start_generation` became async.** Phase 13d.3's dispatcher
  needs to await `model_supports_tools` before picking the
  producer. All three callers (`create_chat_endpoint`,
  `send_message_endpoint`, `regenerate_endpoint`) were already
  async, so adding `await` was one token per site. The in-flight
  `GenerationInProgress` guard still raises synchronously before
  the first `await`, so existing `except GenerationInProgress`
  handlers still catch it.
- **No DB column for the agentic-skipped state.** The banner is
  derived from current setting + current model capability at
  render time. Per-message persistence would be overkill — the
  banner reflects "is this chat in fallback right now?", not
  "was this message generated in fallback?".
- **Per-iteration tool-call cap behaviour: hand off to review, don't
  bail the whole turn.** If a research pass hits its 5-call inner
  cap, it commits whatever findings the model produced (or a
  synthesized "no findings" placeholder) and the review agent
  judges those. Review can then request more research, which
  becomes a fresh iteration. The loop's outer cap still bounds the
  total work.
- **Bug-review pass before implementing 13d.** Caught the
  cumulative-feedback bug (feedback only present on first probe
  inside the iteration, lost on subsequent probes) before code
  was written — fix was a comment-and-three-line change to
  intra_turn appending. Without the review pass, this would have
  shipped as a non-obvious "model ignores feedback" failure that's
  hard to diagnose without instrumentation.
- **Sub-phase commits over a monolithic 13 commit.** Each sub-phase
  (13a → 13g) shipped behind the toggle being off by default. 13a
  alone (schema + `Role` expansion + settings helpers) was a
  10-minute commit; 13d.2 (the orchestrator itself) was a full day.
  Splitting let each one get reviewed independently and let the
  user steer the design at each commit boundary.

## What worked

- **The pre-phase cleanup was load-bearing.** Five refactor
  commits (extract templates, hoist helpers, move OOB rendering,
  add envelope encode/decode, drop the connection leak) all
  unblocked clean integration in 13d. Without
  `encode_tool_call` / `decode_tool_call`, 13d would have
  duplicated JSON-encoding logic across the orchestrator and the
  legacy path. Without the extracted helpers
  (`emit_ollama_error` / `maybe_persist_partial` / `signal_done`),
  the orchestrator's safety net would have been a copy-paste of
  `_run_generation`'s. The cleanup was billed as "tech debt" but
  paid for itself before phase 13 finished.
- **Plan-mode bug review caught two real bugs in 13d before
  implementation.** (1) The cumulative-feedback persistence (above).
  (2) The max-iterations-marker swap clobbering the rest of the
  card if implemented as outerHTML on `<details>` rather than as
  a sibling span. Both would have shipped as visible browser bugs;
  catching them in markdown was minutes of edit instead of hours
  of debug.
- **Verdict-tool ergonomics.** Encoding the review verdict as a
  tool call (rather than asking for a JSON blob in free-form text)
  meant the model behaved consistently across `llama3:8b`,
  `qwen2.5:7b`, and `gpt-oss:20b` in casual testing. The defensive
  `parse_verdict` fallback (treat no-verdict-call as failed +
  default message) covered the one case where a model called a
  random unrecognized tool name.
- **Iteration-scoped row id format.** `{card_id}-iter-N-row-M`
  means the historic-render path's reconstructed DOM matches the
  live SSE path's OOB swap targets exactly — a mid-turn reload's
  reconstructed card lines up with any not-yet-consumed SSE events
  still arriving from the live producer. Tested via
  `test_agentic_iteration_row_views_use_iteration_scoped_ids`.
- **Defensive verdict status parsing.** `verdict_status` falls
  back to `"unknown"` for malformed JSON, non-dict payloads, AND
  unrecognized status strings. The historic-render template's
  CSS selectors (`tool-card__verdict--{status}`) never blow up on
  drift between model output and our schema.
- **`AGENTIC_ITERATION_CAP` import location dance.** Sharing the
  constant via `app/agents/__init__.py` (rather than letting
  render.py and loop.py both hardcode `3`) survives any future
  cap change without desync. The internal alias in loop.py keeps
  call sites terse without changing the public name.
- **The agentic happy-path integration test.** Pins the SSE event
  order (iteration-start → research-findings → review-verdict →
  tokens → done), the persisted message-row shape, AND the
  historic-render reconstruction in one test. It would have caught
  every architectural seam break in 13d.2–13f.
- **Each sub-phase had its own dedicated review pass.** Caught
  small bugs as they landed rather than letting them stack: 13b's
  `verdict_message` returning `None` for missing keys; 13c's
  `parse_verdict` crashing on non-dict `arguments`; 13d.1's
  invisible max-iterations marker; 13d.2's redundant max-iteration
  event overload + missing replace-coverage test; 13e's redundant
  DB read. Eight review-driven fixes total across 13b–13g.

## What was tricky / went less well

- **The "second tool-call event for the card shell" tripped the
  integration test.** The orchestrator emits a `tool-call` event
  to deliver the empty card shell as a `beforebegin` swap target,
  then a SECOND `tool-call` event for each actual tool invocation.
  My first version of the integration test asserted
  `iteration-start < tool-call`; it failed because the shell's
  tool-call event fires at position 0. Fixed by dropping the
  tool-call ordering assertion and pinning the meaningful
  invariants (iteration-start < research-findings < review-verdict
  < token < done). The shell-uses-tool-call-as-its-OOB-swap-event
  is a 13d pattern that's worth understanding before writing tests
  around it.
- **`AgenticToolBatchBlock.summary` references
  `agentic_summary_text` defined later in the module.** Python's
  lazy property evaluation makes it work, but the file ordering
  is slightly awkward. Decided not to reorder because moving
  agentic_summary_text up would scatter the live-render helpers
  (it's used by render_iteration_start, render_agentic_done_summary).
  Documented in the docstring instead.
- **Per-iteration history payload building is its own thing.** The
  orchestrator does NOT reuse `_build_history_payload` from
  `app/generation.py` — that helper formats the wire-format
  history for the resumable single-agent flow, with tool_calls
  embedded as separate-but-related rows. The agentic orchestrator
  builds three separate small payloads (research, review,
  generation) and assembles them inline. Took a couple of read-
  through passes to realize they're genuinely different problems,
  not "almost the same thing I should factor".
- **`test_render.py` is now 1500+ lines.** Phase 13f's
  agentic-grouping tests added ~400 lines; the file is starting
  to feel split-worthy. Deferred — the file is still scannable.
  Could split into `test_render_classic.py` /
  `test_render_agentic.py` later.
- **The "Inner cap behaviour when no findings text was produced"
  open question** (from the plan) shipped with the synthesized
  placeholder findings approach. In casual testing this works —
  review treats it as failed and asks for more research, costing
  an iteration. Watchpoint: if a model consistently exhausts its
  5-call inner cap without ever emitting text, we burn iterations
  fast. Real-model behaviour wasn't enough to tune this in v1.
- **The plan's "open question: streaming findings" is still open.**
  Decided no for v1 (single SSE event per findings); the v2
  alternative is a separate `stream_chat` call after the
  tool-calling loop exits, with a prompt like "Now write your
  findings." Extra round trip per iteration. Not addressed in
  13.x.
- **The full-page direct-hit path's banner test surfaced a
  context-passing inconsistency early.** I'd added
  `agentic_skipped` to the HX-fragment template context but
  initially forgot the full-page index.html context dict. Caught
  by `test_chat_panel_banner_also_shows_on_direct_hit`. Worth
  remembering: when a route has two render branches, the
  template-context shape needs to match across both.

## Surprises

- **The orchestrator's bug-review pass moved feedback handling
  from a kwarg to a list-append.** The first plan draft had
  `_build_research_payload(..., feedback=...)`. The review pass
  observed that feedback would then be lost on the second
  `maybe_tool_call` inside the same iteration (the kwarg is only
  passed once). Switching to "push feedback onto `intra_turn`"
  fixed the entire class of bug — the feedback persists for
  every probe in the iteration without any special-casing.
- **Renaming `intra_turn` from "iteration_state" mid-design helped
  more than I expected.** The first plan called the accumulator
  `iteration_state`. But it spans ACROSS iterations — every
  research pass adds to it, the feedback message lands in it
  between iterations. `intra_turn` makes the lifetime clear:
  "scoped to one user turn, regardless of how many iterations
  that turn runs". Names matter.
- **`encode_tool_call` paid for itself instantly.** Phase 12-era
  code had a hand-rolled `json.dumps({"name": ..., "arguments":
  ...})` at every tool_call write site (`_run_generation`).
  Phase 13's orchestrator persists tool_calls from a different
  code path; without the helper there'd be a second hand-rolled
  copy. The decode path is even more valuable — the historic-
  render code calls `decode_tool_call` and gets a forgiving
  fallback for free.
- **The model-not-capable fallback test couldn't share the
  happy-path fixture.** The `agentic_client` fixture's
  `probe_count` mechanism asserts exactly 3 probes — a fallback
  test with NO agentic events would still hit 1 probe (the
  single-agent producer's tool-intent check) and confuse the
  count. Built a separate fixture-less test that constructs its
  own TestClient inline. Acceptable duplication for the test
  isolation.
- **`render.py` ended at 100% coverage.** Surprised me — the new
  dataclasses have defensive branches (malformed JSON, non-dict
  payload, unrecognized verdict status) that I expected to leave
  as defensive-uncovered. Targeted tests for each branch closed
  all of them. Coverage rose from 97% (phase 12 close) to 98%
  even with ~440 new statements in render.py and ~130 in loop.py.
- **No browser smoke-test failures from phase 13.** Phase 11
  shipped 5 post-launch bugs because curl-only smoke tests
  missed browser-only failures. Phase 13's automated tests
  (especially the integration test pinning SSE event order +
  historic reconstruction) caught what would have been browser
  bugs in 11. The architectural pieces (DOM id parity, sibling
  max-marker, cumulative feedback) all behaved as designed
  on first browser pass. *(Caveat: writing this as part of 13g,
  the full smoke-test checklist hasn't been walked through yet —
  see Open issues.)*

## Open issues / follow-ups

- **Browser smoke-test checklist from the 13g plan not walked
  through yet.** Per CLAUDE.md, UI changes require a real-browser
  pass before declaring done. Items to walk: /settings toggle
  persists across reload, send a research-shaped message and
  watch the live card render (iteration header → tool rows →
  findings → verdict → tokens), trigger a hard question that
  fails review at least once and verify multi-iteration render,
  reload after completion and verify historic agentic card
  reconstructs, swap to a non-tool-capable model and verify the
  banner appears, dark-mode check for verdict colours + banner
  legibility.
- **No retro for the agentic-mode prompt quality.** The system
  prompts in `app/agents/prompts.py` are hardcoded v1; iterating
  them is "edit the file and ship a follow-up phase". Worth
  watching real-world chats for: does the research agent
  over-call tools? Does the review agent send useful feedback?
  Does the generation agent over-cite or under-cite the
  findings? Tune the prompts based on what surfaces. No formal
  eval suite exists.
- **Findings emit as a single SSE event** (not token-by-token).
  v2 candidate if it feels jarring in real use.
- **Resumability not in v1.** A page reload mid-loop surfaces
  "(response interrupted)" via the existing safety net. v2 would
  extend the producer/consumer infrastructure to replay agentic
  events the same way single-agent flow does today.
- **Iteration-cap behaviour on "no findings produced".** The
  synthesized placeholder ("No findings produced; research
  called tools N times without summarising") is treated by
  review as failed → request more research → burns an iteration.
  If a model gets stuck in this loop, all 3 iterations burn
  fast. Watchpoint; alternative is to skip the review pass for
  this iteration and force-continue.
- **No per-agent model overrides.** All three agents use the
  conversation's chat model. A future "research = small fast
  model, review = bigger model, generation = best model" setup
  is plausible but adds a settings surface and a model-loaded-
  warm-cost trade-off. Out of scope for v1.
- **The 3% coverage gap in `app/agents/loop.py` is in the
  streaming-phase Ollama-error handlers** (lines 408-411). Same
  shape as the gap in `app/generation.py` — only reachable when
  the stream_chat call mid-generation errors out. Contrived to
  set up; defensive in spirit; not worth a fabricated test.
- **Single-agent path is byte-identical to today's behavior
  when the toggle is off.** Phase 13 must not have regressed
  anything in phases 12a–12h. Test count went 309 → 393; none
  of the new tests displaced an existing one. Worth a periodic
  spot-check that the single-agent flow stays clean as future
  work touches `app/generation.py`.
- **`test_render.py` is getting large** (1500+ lines). A future
  cleanup could split it by block kind (classic / agentic /
  sources). Not urgent.
- **The agentic-skipped banner uses `--danger` tinted at 8%.**
  Visually reads as a soft warning. If the project ever grows
  a dedicated `--warning` token, swap to it.

## Notes for future phases

- **Sub-phase commits remain the right granularity.** Each 13.x
  commit was independently reviewable and behind the toggle being
  off by default. The user could redirect at every boundary
  without invalidating prior work. For any phase with more than
  4-5 distinct architectural pieces, splitting by piece is the
  cheap insurance.
- **Plan-mode bug review continues to earn its keep.** Phase 13d's
  feedback-persistence bug and max-iterations-marker swap bug
  were both caught in markdown. The pattern is now solidly:
  write the plan, bug-review the plan, implement.
- **Lazy import to break circular dependencies.** Phase 13d.3's
  dispatcher does `from app.agents.loop import _run_agentic_generation`
  inside `start_generation` rather than at module top — because
  `app/agents/loop.py` imports from `app/generation.py`. Same
  pattern would apply for any future module that needs the
  orchestrator's seam. Document next to the import so the lazy
  form isn't mistaken for an oversight.
- **Shared constants belong in package `__init__.py` when used
  across sibling modules.** `AGENTIC_ITERATION_CAP` moving from
  `loop.py` to `app/agents/__init__.py` was the cheapest cycle-
  break. Future shared constants (e.g., a feature-flag table key
  used by both queries and routes) could follow the same shape.
- **Tool-call API as a structured-output mechanism.** The verdict-
  tools trick (encode "passed"/"failed" as one of two tool calls)
  is more robust than asking for JSON in free-form text. Future
  agent-like surfaces that need structured output from the model
  (e.g., a future "classify this message as: chat / command / tool
  request" router) could reuse the pattern.
- **DOM-id parity between live and historic render is a load-
  bearing invariant.** A future feature that emits SSE events
  targeting elements by id MUST verify the historic-render path
  produces matching ids. Use `{card_id}-iter-N-row-M`-style
  iteration-scoped formats and pin them with a test like
  `test_agentic_iteration_row_views_use_iteration_scoped_ids`.
- **Defensive parsing of model-output JSON.** Every JSON-decoded
  field from the model should have a fallback for: malformed
  JSON, non-dict payload, missing key, unrecognized enum value.
  `AgenticIteration.verdict_status` does all four; was tedious
  to write but each branch came up in real-model testing during
  phase 13d.
- **`async def` conversion is one-token-per-call-site when callers
  are already async.** Phase 13d.3's `start_generation` → async
  conversion was painless because all four call sites were
  already `async def`. Future helpers that need to await
  something should check their callers first; converting
  upstream is usually cheaper than building an off-loop
  shortcut.
- **Coverage as a design pressure.** Hitting 100% on `render.py`
  forced explicit handling of every edge case (no verdict row,
  malformed verdict JSON, non-dict payload, unrecognized verdict
  status, orphan tool calls within iterations, back-to-back tool
  calls). Each branch is a real failure mode the model could
  exhibit. The coverage target wasn't the point — the explicit
  handling was.

## Wrap-up

Phase 13 added an opt-in three-agent research-review-generation
loop, gated by a single global toggle in `/settings`. The agentic
flow coexists with the single-agent flow via a dispatcher in
`app/generation.py` that picks the producer based on the toggle
+ the model's tool capability. When the toggle is off, the
behavior is byte-identical to phase 12. When the toggle is on
but the chat's model can't do tools, the dispatcher silently
falls back to single-agent and the chat panel surfaces a banner
explaining why.

The phase shipped in 14 commits behind a 5-commit pre-phase
cleanup pass. Each sub-phase (13a → 13g) was independently
committable; six review passes caught bugs as they landed.
Tests went 309 → 393 (+84), coverage held at 98%,
`app/render.py` reached 100%.

Open work: the browser smoke-test checklist hasn't been walked
through yet (the only "ship" gate per CLAUDE.md still pending);
prompt quality and iteration-cap behaviour are watchpoints for
real-world use; per-agent model overrides and streaming findings
are v2 candidates. PLAN.md is unchanged; phase 13 is documented
in `docs/plans/phase13-agentic-loop.md` (plan) and this file
(retro).
