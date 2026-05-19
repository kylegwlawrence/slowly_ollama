# Phase 12g retrospective — resumable assistant generation

## Scope

Decouple LLM generation from the SSE response generator so client
disconnects don't lose the response. Phase 12e.1's safety net
caught the inconsistent-chat case (orphan tool card with no
following bubble), but a reload still lost the actual reply
because the LLM call was tied 1:1 to the HTTP connection — when
the browser disconnected, Starlette cancelled the task, the call
aborted, and only `(response interrupted)` got persisted. The
user said: that's not enough — we want the response itself to
survive.

Phase 12g moves the LLM call into a background `asyncio.Task`
owned by a process-local registry. SSE endpoints become consumers
that replay an event log from index 0 then tail new events. A
reloaded page (or a second tab) attaches as a fresh consumer and
sees the full history plus anything still streaming.

Out of scope: token-level DB persistence (would survive server
restart). The user signed off on losing in-flight state on
restart since the cheap-fix safety net handles that gracefully
("(response interrupted)" on next render).

## What landed

| Commit | Title |
|---|---|
| `fcdedc7` | docs: plan phase 12g — resumable assistant generation |
| `75595b7` | feat: phase 12g — resumable assistant generation via background task |
| `e1626b2` *(partial)* | refactor: code-review pass on phase 12e/12g |

240/240 tests pass at the close. Coverage 96% overall;
`app/generation.py` lands at 94% (the remaining 6% is the
streaming-phase Ollama-error handlers — only reachable when the
probe succeeds but streaming fails).

## Decisions (and why)

- **Producer/consumer split: one `asyncio.Task` per turn,
  multiple SSE consumers attaching/detaching.** The producer
  appends `(event, payload)` tuples to `state.events`;
  `asyncio.Condition` wakes consumers on each append. New
  consumers replay from index 0. This delivers reload-resume,
  multi-tab live mirror, and chat-switch-and-return for free.
- **In-memory registry, no schema change.** The
  `live_generations: dict[int, GenerationState]` module-level
  dict is sufficient for single-process single-user. A DB-backed
  per-token persistence layer would survive server restart but
  costs a schema migration, per-token writes, and a polling-or-
  event-driven SSE tail. Deferred as phase 12g.2 if it ever
  matters.
- **Registry retains DONE states until the next generation for
  the same conv evicts them.** Without this, slow reloads
  landing after the task finished (the test environment with
  synchronous mocks, but also real network slowness) fall
  through to `consume_finished` which can't replay tool events
  or partial tokens. Keeping done states lets `consume_generation`
  still play back the full sequence. Memory cost: one event log
  per active-or-recently-finished conversation; bounded by the
  tool cap + token count per turn.
- **`start_generation` evicts done states, raises on live states.**
  The "in flight" check is `not state.done` — a finished state
  is replaceable by a new turn for the same conv. The 409 on a
  truly in-flight duplicate is defensive (the streaming-class
  CSS disables the send button) but cheap to add.
- **Phase 12e.1's safety net stays inside `_run_generation`.**
  Server-shutdown via `CancelledError` / `GeneratorExit` on the
  task still needs to persist a partial. The safety net's role
  shifted from "main mid-stream-cancel guard" to "catastrophic
  failure backstop."
- **`/regenerate-stream` collapsed into `/stream`.** After phase
  12g, both stream endpoints reduce to "consume the live state,
  else emit a finished done event." The regenerate POST still
  differs (`on_complete="replace"`), but the GET layer is
  identical. Deleting the duplicate route was the smallest part
  of the diff but the most satisfying.
- **`asyncio.Condition` over `Event` for consumer wakeup.** The
  recheck-under-lock pattern (`if state.done or pos < len(events): continue`)
  avoids missed signals between drain and wait. An `Event` would
  need to be reset by exactly one consumer and not others;
  Condition's `notify_all` + per-consumer wait is correct
  out-of-the-box.
- **Two test helpers: `_create_chat_and_get_id` (calls POST,
  spawns gen) and `_create_chat_db_only` (direct DB insert).**
  POST /chats now has the side effect of spawning a generation,
  which consumes mock probes that tool tests want to reserve for
  their POST /messages flow. The split keeps both groups of
  tests deterministic.
- **`_isolate_live_generations` as autouse fixture.** The
  registry is module-level state shared across tests in the same
  process. Phase 12g changed it to retain done entries, which
  bleeds across tests with same-id conversations. Autouse
  fixture clears it around every test; cancellation tests that
  bypass `make_client` (driving `start_generation` directly)
  still get the cleanup for free.

## What worked

- **The plan's bug review (B1–B14) caught the two real
  architectural hazards before code was written.** B1 (double
  tool-card on reload) and B2 (`asyncio.create_task` from sync
  routes) both had concrete mitigations baked into the plan —
  the chat-panel skip-trailing-batch logic and the `async def`
  conversion respectively. Implementation took ~2 hours; without
  the plan it would have been the same time spread over three
  debug-fix cycles.
- **Direct generation-level tests in `tests/test_generation.py`.**
  Ten focused unit tests for the producer/consumer interaction
  (replay, two concurrent consumers, late consumer, done eviction,
  consume_finished fallback). Quick to write because they don't
  go through TestClient.
- **Keeping the phase 12e.1 safety net intact.** Resisted the
  urge to delete it after 12g made resumable-generation the
  happy path. The safety net is exactly the catastrophic-failure
  backstop the new architecture needs — server-shutdown case
  was easier to reason about because the existing tests still
  verified it.
- **Removing dead code in the review pass.** The phase-12g move
  left six stale imports and a dead `_sse()` in routes.py.
  Cleaning them up bumped coverage from 96% to 98% with no
  behavior change. The autouse fixture for `live_generations`
  removed four manual cleanup lines from the cancellation tests.
- **Asking the user about scope before the proper fix.** The
  cheap-fix vs proper-fix question saved an hour of speculative
  work. The user picked cheap first; that shipped quickly; we
  did the proper fix only after the cheap fix confirmed the
  symptom diagnosis was right.

## What was tricky / went less well

- **First refactor of `_stream_assistant_reply` broke indentation.**
  Wrapping the body in `try:` with a sequence of Edits ended up
  under-indenting the inner for-loop body. Recovered with
  `git checkout HEAD -- app/routes.py` and a clean one-shot
  rewrite. Large body-level re-indents shouldn't be done via
  sequential string edits; use a single full-block replacement
  instead.
- **Tests broke en masse after the refactor.** ~25 tests went
  red because (a) the moved helpers had new import paths, (b)
  the monkeypatch targets shifted from `app.routes.ollama.*` to
  `app.generation.ollama.*`, (c) POST /chats now spawned a
  generation that consumed mock probes, and (d) the
  cancellation tests drove `_stream_assistant_reply` directly,
  which no longer exists. Fixed in stages: global sed for the
  monkeypatch paths, helper split for the mock consumption,
  cancellation-test rewrite for the architectural change. Each
  stage cut failures roughly in half.
- **`asyncio.create_task` from sync routes was a real footgun.**
  Three of the four POST handlers were `def` (threadpool); the
  fix was a one-word change per handler, but missing it would
  have produced a `RuntimeError: no running event loop` for
  every message send. Caught by the plan's B2 entry.
- **The done-callback's responsibility shifted late.** First
  cut had it pop the entry from the registry on task done.
  Tests then saw "event: done" only because consume_finished
  fired — `consume_generation` had nothing left to replay
  because the registry was empty. Fixed by keeping done states
  in the registry; the done-callback now only logs unhandled
  exceptions.
- **Test environment timing differs from production.** With
  synchronous mocks, the task completes near-instantly relative
  to the GET that follows it. In a real browser, the task takes
  seconds and the GET attaches first. Two helpers
  (`_create_chat_and_get_id` vs `_create_chat_db_only`) plus
  the keep-done-states design closed the gap without coupling
  tests to wall-clock timing.

## Surprises

- **`live_generations` retention turned a test-environment
  fix into a real-user feature.** I started keeping done
  states to make tests deterministic. It also makes the
  slow-reload-after-completion case render correctly — the
  consumer can replay the recently-finished event log instead
  of falling through to a lossy done-only path. Two birds.
- **Refactoring 570 lines out of routes.py.** The diff is
  comically lopsided: `app/routes.py | 740 ++++-------------`.
  Made me notice that 12d-12e had been packing a lot of
  generation logic into the routes module; moving it out made
  the routes file feel like a routes file again.
- **`{% with %} {% include %} {% endwith %}` for variable
  isolation.** Hadn't used it before; learned it solving the
  duplicate-`hx-swap-oob` bug in 12e. Reused mental model in
  12g for the chat-panel-template's `pending_stream_url`
  conditional rendering. Now a tool in my Jinja toolbox.
- **The bug-review pass changed what I built.** B1 (double
  tool-card) wasn't obvious from the plan's first draft;
  thinking through "what happens on reload to the historic
  card" surfaced it. The implementation got an extra
  `if blocks[-1].kind == "tool_batch": blocks = blocks[:-1]`
  branch — easy to write once spotted, impossible to find
  during browser smoke testing because you'd just see "two
  cards, weird, why?"

## Open issues / follow-ups

- **No DB-backed persistence; server restart loses in-flight
  state.** Phase 12g.2 if needed. The user explicitly said
  they don't need this; flagged as deferred.
- **The 6% coverage gap in `app/generation.py` is in the
  streaming-phase Ollama-error handlers.** Reachable only when
  the probe succeeds and then streaming fails — contrived to
  set up, defensive in spirit. Not worth a contrived test.
- **No UI "Stop" button to cancel a running generation.**
  Implementation would be small: POST `/chats/{id}/stop` looks
  up the state and calls `state.task.cancel()`; the safety net
  writes the partial. Not in scope for 12g; mentioned in the
  plan's "Out of scope" section.
- **No timeout on a runaway-but-not-erroring generation.** A
  model that streams forever without ever sending `done=True`
  would hold the registry entry indefinitely. httpx's 120s read
  timeout caps individual chunk waits but not the total turn.
  Realistic local models don't hit this; flagged for the
  record.
- **The `consume_finished` fallback's empty-bubble branch is
  defensive code that's hard to test.** Reachable only if the
  task crashed mid-tool before the safety net could write
  anything AND the registry was already cleared. Untested in
  the suite; the defensive comment in code documents the
  intent.

## Notes for future phases

- **Background-task architecture as a pattern.** Future work
  that involves long-running operations the user could disconnect
  from (e.g., embedding a large document into RAG, batch
  reprocessing past chats) should follow the same shape:
  module-level registry + asyncio.Task + condition-driven
  consumers. The infrastructure in `app/generation.py` could
  generalize but doesn't need to until there's a second
  consumer.
- **Plan-mode bug review continues to earn its keep.** Both
  HIGH-severity bugs (B1 double-card, B2 sync routes) were
  caught in the plan. Twelve LOWER-severity items were either
  confirmed safe by analysis or got tiny mitigations. The
  pattern is now: write the plan, then bug-review the plan, then
  implement.
- **Test helpers that wrap routes are fragile when routes change
  side effects.** `_create_chat_and_get_id` quietly started
  spawning a generation as a side effect of POST /chats. That
  broke tool tests in ways that took ~5 minutes to diagnose.
  Future test helpers that call routes should be commented with
  "calls X which has side effects Y," or split into route-call
  vs route-bypass variants from the start.
- **Module-level state is fine if there's a documented
  isolation strategy.** `live_generations` is the first
  cross-request in-memory state in the codebase. The autouse
  fixture pattern in `tests/test_routes.py` and
  `tests/test_generation.py` keeps tests deterministic. Worth
  documenting in `CONVENTIONS.md` for any future cross-request
  state (done: §"Process-local non-DB state").

## Wrap-up

Phase 12g delivered resumable assistant generation: a page reload
during a running response now attaches a fresh consumer to the
still-running task and continues watching tokens arrive. Two-tab
live mirroring works. Chat-switch-and-return works. Server
restart still loses in-flight state but the cheap-fix safety net
keeps the chat consistent.

The phase took two narrow rounds: a cheap fix (12e.1) that
preserved chat consistency but lost the response, and the proper
fix (12g) that preserves the response itself. The split was
right — the cheap fix went out fast and confirmed the symptom
diagnosis; the proper fix shipped only after the cheap fix's
limitations matched expectations.

PLAN.md is unchanged; phase 12 stays off-PLAN.md. Next phase (if
any) could be 12f (model-capability filtering, per the original
12 plan) or 12g.2 (token-level DB persistence) or something
unrelated. The current state is a good stopping point.
