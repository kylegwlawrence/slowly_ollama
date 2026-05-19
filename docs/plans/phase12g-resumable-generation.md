# Phase 12g — Resumable assistant generation

## Context

Phase 12e.1 (commit `319dd40`) added a safety net: when the SSE
generator is cancelled mid-stream, persist a partial assistant row
so a page reload doesn't leave the chat with orphan tool rows. That
fix prevents the broken-looking "tool card with nothing after it"
state, but the user observation that drove this phase is more
ambitious: **the actual response should survive a reload, not get
replaced by `(response interrupted)`**.

The cheap fix can't deliver that because the generator's lifecycle
is tied 1:1 to the HTTP response. When the browser disconnects,
Starlette cancels the task, the LLM call aborts, and there's no
more output to persist.

This phase decouples LLM generation from the HTTP connection so
that:

- The model finishes generating even if every client disconnects
  partway through.
- A reloaded page (or a second tab on the same chat) attaches as
  a fresh consumer to the still-running generation and sees the
  full history plus any new tokens as they arrive.
- A reload after generation has fully completed shows the persisted
  final response with no surprises.

## Design

### Architecture

```
┌──────────────────┐                ┌─────────────────────────┐
│ POST /messages   │ start_         │ asyncio.Task            │
│ (user message)   │ generation()   │ _run_generation(state)  │
│                  ├───────────────▶│  - runs LLM probe loop  │
│                  │                │  - runs tools           │
│                  │                │  - streams tokens       │
│                  │                │  - appends each SSE     │
│                  │                │    event to state.events│
│                  │                │  - cond.notify_all()    │
│                  │                │    on each event        │
└──────────────────┘                └─────────────┬───────────┘
                                                  │
                                                  ▼
                                    ┌──────────────────────────┐
                                    │ live_generations[conv_id]│
                                    │ = GenerationState        │
                                    │   .events: list[(ev, hp)]│
                                    │   .done: bool            │
                                    │   .cond: asyncio.Condition│
                                    └─────────────▲────────────┘
                                                  │
                          ┌───────────────────────┼───────────────────────┐
                          │                       │                       │
                ┌─────────┴─────────┐  ┌──────────┴─────────┐  ┌──────────┴─────────┐
                │ GET /stream       │  │ GET /stream        │  │ GET /stream        │
                │ (original tab)    │  │ (reloaded tab)     │  │ (tab #2)           │
                │ consume_generation│  │ consume_generation │  │ consume_generation │
                │ (pos=0 → tail)    │  │ (replays from 0)   │  │ (replays from 0)   │
                └───────────────────┘  └────────────────────┘  └────────────────────┘
```

The background task is the single producer; SSE endpoints are
multiple consumers that read from `state.events` and wait on
`state.cond` for new entries.

### State

```python
# app/generation.py
@dataclass
class GenerationState:
    conversation_id: int
    # Replay log. Each entry is (event_name, html_payload). Consumers
    # iterate this list in order, then wait on `cond` for new entries.
    events: list[tuple[str, str]]
    done: bool
    cond: asyncio.Condition
    task: asyncio.Task | None  # set after asyncio.create_task fires
```

### Producer (background task)

`_run_generation(state, client, db, conversation_id, model, history, on_complete)`
is the existing `_stream_assistant_reply` body, modified so that
every `yield _sse(payload, event=ev)` becomes
`await _emit(state, ev, payload)`, where:

```python
async def _emit(state: GenerationState, event: str, payload: str) -> None:
    async with state.cond:
        state.events.append((event, payload))
        state.cond.notify_all()
```

Note the producer is itself an async function (not a generator). The
`yield`-flow becomes a linear flow with `await _emit(...)` calls.

The cheap-fix safety net (`try/finally` writing
`(response interrupted)`) stays in place inside `_run_generation`:
catastrophic failures (server restart, unhandled exceptions) still
need to leave a consistent DB. Within normal operation the safety
net is dormant — the task runs to completion and persists the full
assistant row.

### Consumer (SSE endpoint)

```python
async def consume_generation(state: GenerationState) -> AsyncIterator[str]:
    """Stream all events from a (possibly already-running) generation.
    
    New consumers replay from index 0; an early reload sees the full
    sequence (tool-card events + any tokens streamed before the reload)
    just as the original consumer would have.
    """
    pos = 0
    while True:
        # Drain anything new without holding the condition's lock.
        while pos < len(state.events):
            event, payload = state.events[pos]
            yield _sse(payload, event=event)
            pos += 1
        if state.done:
            return
        async with state.cond:
            # Recheck under lock to avoid missing a notify that fired
            # between the drain and the wait.
            if state.done or pos < len(state.events):
                continue
            await state.cond.wait()
```

### Registry

```python
# Module-level in app/generation.py — single-process, single-loop.
live_generations: dict[int, GenerationState] = {}
```

`start_generation(conversation_id, ...)`:

1. Refuses to start a second generation for the same conversation
   (raises `GenerationInProgress`; the route maps this to HTTP 409).
2. Creates the `GenerationState`, registers in `live_generations`.
3. `task = asyncio.create_task(_run_generation(state, ...))`.
4. Attaches a done-callback that removes the entry from
   `live_generations` and re-raises any unobserved task exception
   (logging it, since no consumer may be attached at that point).

### Endpoint changes

**POST `/chats/{id}/messages`** (in `app/routes.py`):

- Persist user message (unchanged).
- Call `start_generation(...)` synchronously — it's quick (creates
  the state and schedules the task; the LLM call doesn't run before
  it returns).
- Return user bubble + placeholder pointing at `/chats/{id}/stream`.
  The placeholder is unchanged from today.

**GET `/chats/{id}/stream`**:

```python
state = generation.live_generations.get(conversation_id)
if state is None:
    # Generation finished and was already drained, OR no generation
    # was ever started for this conv. Emit a no-op done event so the
    # placeholder gets cleanly swapped to whatever's already in the
    # DB (or to an empty assistant bubble if nothing was persisted).
    return StreamingResponse(_consume_finished(db, conversation_id), ...)
return StreamingResponse(
    generation.consume_generation(state), media_type="text/event-stream"
)
```

`_consume_finished(db, conv_id)` yields a single `done` event that
OOB-replaces the placeholder with the last assistant row's bubble
(reusing `_message.html` exactly like the current done path).

**GET `/chats/{id}`** (chat panel render):

- Existing behavior: render all persisted messages.
- New: if `live_generations` has an entry for this conv, append an
  assistant streaming placeholder to the rendered `#messages` div,
  pointing at `/chats/{id}/stream`. Mirrors the
  `pending_stream_url` mechanism that POST /chats already uses for
  the new-chat flow (`app/routes.py:451-456`).

### Race conditions

1. **POST races EventSource open.** `start_generation` registers the
   state synchronously before returning. By the time the response
   reaches the browser and the placeholder renders, the state is
   already in the registry. The first SSE consumer attaches with
   `pos=0` and sees every event.

2. **Reload after gen finished.** Done-callback removes from
   registry. New `/stream` request sees no state and uses
   `_consume_finished` which immediately emits the final `done`
   event from the persisted message. The placeholder is swapped
   without flicker.

3. **Reload while gen still finishing.** Race between consumer
   reading `state.events[pos]` and producer appending. Both happen
   on the same event loop — `consume_generation` only reads
   `state.events` from the consumer's coroutine; the producer only
   writes from `_emit` under the cond lock. The list is mutated by a
   single producer; consumers just read indexes. No data race.

4. **Two tabs open on same chat.** Both `/stream` requests attach a
   consumer. Each independently replays from `pos=0` and tails new
   events. `cond.notify_all()` wakes both.

5. **Server restart mid-generation.** All in-memory state lost. The
   task gets `asyncio.CancelledError`; the existing safety net in
   `_run_generation` writes `(response interrupted)` to the DB.
   This is the same behavior the cheap fix delivers today — no
   regression, just no improvement either. (See §Known limitations.)

6. **Duplicate POSTs to same chat.** The UI shouldn't allow this
   (placeholder keeps the send button disabled), but if a request
   slips through, `start_generation` raises and the route returns
   409 with a small HTML error fragment.

## Files

| File | Change |
|---|---|
| `app/generation.py` *(new)* | `GenerationState`, `live_generations`, `start_generation`, `consume_generation`, `_emit`, `_run_generation` (the existing `_stream_assistant_reply` body), `_consume_finished` |
| `app/routes.py` | Remove `_stream_assistant_reply` body (moved to `app/generation.py`); update POST/GET endpoints to use `start_generation` and `consume_generation`; chat-panel route checks `live_generations` for the in-progress placeholder |
| `templates/_chat_panel.html` | Already handles `pending_stream_url`; reuse for resume case |
| `app/dependencies.py` | Optional: expose `live_generations` as a dependency for testability |
| `tests/test_generation.py` *(new)* | Unit tests for the producer/consumer interaction |
| `tests/test_routes.py` | Update mid-stream tests to match new architecture; add reload-resume tests |

## Critical files to read first

- `app/routes.py:878-1204` — current `_stream_assistant_reply` body
  to be extracted
- `app/routes.py:451-472` — `pending_stream_url` mechanism that
  POST /chats uses; resume path follows the same pattern
- `templates/_assistant_placeholder.html` — `sse-connect` target;
  unchanged in this phase
- `app/render.py` — `group_messages_for_render` is unchanged but
  worth reading to confirm in-progress tool rows render correctly
  as historic when the in-flight task hasn't finished writing them
  yet

## Implementation steps

1. **Create `app/generation.py`** with `GenerationState`,
   `live_generations`, `_emit`, `consume_generation`,
   `_consume_finished`, `start_generation`, and a placeholder
   `_run_generation` that just emits a "todo" event. Verify the
   skeleton wires through end-to-end before touching the real
   producer logic.

2. **Move `_stream_assistant_reply` body into `_run_generation`**:
   replace every `yield _sse(payload, event=ev)` with
   `await _emit(state, ev, payload)`. Keep the cheap-fix
   `try/finally` block intact — it's the safety net for catastrophic
   failures inside the task. Remove `yield` and the
   `AsyncIterator[str]` return type; `_run_generation` becomes a
   regular async function. The function still ends by appending the
   final `done` event to state and setting `state.done = True`.

3. **Update POST `/chats/{id}/messages`** to call
   `generation.start_generation(...)` instead of leaving it for the
   GET /stream handler to drive. The placeholder it returns
   continues to point at `/stream`.

4. **Update GET `/chats/{id}/stream`** to dispatch on registry
   lookup: existing state → `consume_generation`; no state →
   `_consume_finished`.

5. **Update GET `/chats/{id}`** to set `pending_stream_url` when an
   in-progress generation exists. The chat-panel template already
   knows how to render the placeholder for this var.

6. **Add 409 handling** for duplicate POSTs. Return a small inline
   error fragment that the composer's form can `hx-swap` into a
   visible error region.

7. **Update the regenerate endpoint** similarly:
   `start_generation(..., on_complete="replace")`.

8. **Tests**: per the §Tests section below.

9. **Browser smoke**: per the §Verification section.

## Tests

Unit tests in `tests/test_generation.py`:

- `start_generation` registers state and spawns task; on completion
  the entry is removed.
- Two `consume_generation` calls attached to the same state both
  see the full event sequence in order.
- A late consumer (attaching after some events were already emitted)
  replays from index 0 and gets every event.
- A consumer attaching after `state.done = True` sees all events
  then exits.
- `start_generation` raises `GenerationInProgress` when an entry
  already exists for the conversation.

End-to-end tests in `tests/test_routes.py`:

- POST /messages spawns a generation; GET /stream attaches and
  receives the full event sequence.
- Reload simulation: POST /messages spawns gen; consume some
  events from one GET /stream; open a second GET /stream; second
  consumer sees all events including the ones the first consumer
  saw.
- POST /messages while a generation for the same conv is in
  progress returns 409.
- GET /chats/{id} includes the placeholder when a generation is in
  progress; doesn't include it once the gen finishes and the
  registry entry is removed.
- All phase 12e.1 cancellation tests still pass (the safety net is
  unchanged inside `_run_generation`).

Reuse the existing `make_client` fixture; add a `make_consumer`
helper that wraps an async generator step-by-step so tests can
inspect the replay log without consuming everything at once.

## Verification

1. `source .venv/bin/activate && pytest` — all green.
2. `uvicorn main:app --reload` and walk:
   - Send a message that triggers `query_rag`. Reload the page
     immediately after the tool card appears. Confirm the assistant
     bubble continues streaming and lands a real response (not
     `(response interrupted)`).
   - Reload while tokens are still streaming. Confirm the bubble
     restarts from the beginning of the (already-streamed) text
     and continues with new tokens.
   - Open the same chat in a second tab while the first tab's
     response is still streaming. Both tabs show the same content
     live.
   - Send a message in chat A, immediately switch to chat B before
     A finishes, then switch back to A. The response in A continued
     while you were away; the switch-back lands on a placeholder
     that replays everything.
   - Stop and restart `uvicorn` mid-stream. Reload. Verify
     `(response interrupted)` is shown (the safety net behavior
     unchanged from phase 12e.1; this confirms no regression).
3. Toggle dark mode mid-response. CSS unchanged so this is just
   smoke.

## Known limitations

- **Server restart loses in-progress generations.** The in-memory
  registry is module-level. A fully durable solution would persist
  tokens to the DB as they stream and resume from there on restart,
  but that requires a status column, per-token UPDATEs, and a
  polling-or-event-based DB tail in `consume_generation`. Deferred
  as phase 12g.2 if it ever matters.
- **Memory grows with chunk count per active generation.** Each
  token allocates a small `(event, payload)` tuple in
  `state.events`. For a 1000-token response that's ~50KB; fine for
  the local-only use case. If a runaway tool loop pushed the count
  much higher, the iteration cap at `_TOOL_ITERATION_CAP = 5`
  bounds the worst case.
- **Single process only.** `live_generations` is a module dict; a
  multi-worker uvicorn would lose cross-worker visibility. Not a
  goal for this app.

## Out of scope

- Persisting tokens to the DB as they stream (phase 12g.2).
- Background-task supervision / restart logic.
- Generation cancellation from the UI (a "Stop" button). Could be
  added as a small follow-up: POST `/chats/{id}/stop` finds the
  state, calls `state.task.cancel()`, the safety net writes the
  partial.

## Notes / follow-ups to flag for the user after implementation

- The chat panel's `pending_stream_url` mechanism becomes the
  standard way to render in-progress turns. Worth documenting in
  `docs/CONVENTIONS.md` once shipped.
- The `live_generations` dict is the first piece of cross-request
  in-memory state in this codebase. Worth a CONVENTIONS bullet on
  "non-DB process-local state lives here" pattern.
- Phase 12e.1's safety-net comment in `_run_generation` should be
  updated to clarify that catastrophic-failure cases are now the
  main use; mid-stream cancellation no longer ditches the response.
