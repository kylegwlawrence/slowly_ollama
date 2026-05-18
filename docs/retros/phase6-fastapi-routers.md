# Phase 6 retrospective — FastAPI routers + SSE streaming

## Scope

Phase 6 wired the storage and Ollama-client layers from Phases 2–5 into HTTP
endpoints the HTMX frontend will consume. Eight routes under `/api`, two of
them streaming. The lifespan owns the shared SQLite connection and the
shared `httpx.AsyncClient`; routes pull them from `app.state` through small
`Depends()` getters.

End state: 63 tests passing (was 47 entering the phase).

## What landed

| File | Role |
|---|---|
| `main.py` | FastAPI app + lifespan (open/close shared resources) |
| `app/dependencies.py` | `get_db`, `get_ollama_client`, plus `DB` / `OllamaClient` `Annotated` aliases |
| `app/routes.py` | Single `APIRouter` under `/api`, 8 endpoints, 3 pydantic input models, `_sse(...)` formatter, shared `_stream_assistant_reply` helper |
| `app/queries.py` | Added `get_conversation(conn, id)` — needed by the streaming endpoints to look up the conversation's model |
| `tests/test_routes.py` | 14 endpoint tests using `TestClient` + `httpx.MockTransport` mocked Ollama |
| `tests/test_queries.py` | +2 tests for `get_conversation` |

## Decisions (and why)

- **One `routes.py`, not one router per resource.** Eight endpoints with no
  shared logic between groups didn't justify three files. Revisit if the
  count doubles or auth/middleware gets introduced.
- **Pydantic for inputs, dataclasses for outputs.** Pydantic is already
  transitively in the project via FastAPI; using `BaseModel` for the three
  small input shapes (`ConversationCreate`, `ConversationRename`,
  `MessageCreate`) gives free validation. Outputs use the existing Phase 4
  dataclasses — FastAPI serializes them without duplication.
- **SSE format: JSON in the `data:` field, named `done` / `error` events.**
  Surfaced upfront via an `AskUserQuestion` block so the choice was made
  before any code shipped. Newline-safe (any `\n` inside a token becomes
  `\\n` in the JSON string), frontend-agnostic.
- **HTTP error mapping:** `OllamaUnavailable` → 503, `OllamaProtocolError`
  → 502, `LookupError` → 404, "no assistant message to regenerate" → 400.
  Mid-stream failures emit `event: error` because headers are already on
  the wire by then.
- **Shared `_stream_assistant_reply(...)` helper.** Send-message and
  regenerate differ only in (a) whether the last assistant message is
  included in the prompt history and (b) `append_message` vs
  `replace_last_assistant_message` at the end. Parameterizing the last
  step (`on_complete: "append" | "replace"`) avoided two near-identical
  generators.
- **Persist the assistant text *after* the stream completes.** If the
  client disconnects mid-stream, the partial response is discarded. That's
  the documented tradeoff in `app/routes.py`.

## What worked

- **Reusing the Phase 5 mock-transport pattern.** `httpx.MockTransport` was
  already proven in `test_ollama.py`; the route tests reused it through a
  `make_client(handler)` factory. Streaming tests in particular came
  together quickly because the Phase 5 NDJSON-stream fixture body was
  already a tested shape.
- **`Annotated[Conn, Depends(get_db)]` aliases (`DB`, `OllamaClient`).**
  Route signatures stayed short and readable; the dep injection plumbing
  is concentrated in `app/dependencies.py`.
- **Surfacing review concerns in the post-phase summary.** The "5 things
  worth flagging" list at the end of the phase made the
  `dependency_overrides.clear()` issue visible to the user, who flagged
  it for follow-up. The fix went out as a small targeted commit
  (`5f9cd95`) on top of the phase commit (`faaa130`). Worth repeating in
  future phases.

## What was tricky / went less well

- **`app.dependency_overrides.clear()` in the test fixture was too
  aggressive.** It wiped any overrides on the app, not just the one this
  fixture added. Caught only in the post-phase review (not during
  implementation), and fixed as a follow-up by snapshotting the dict at
  fixture entry and restoring at teardown. Pattern to repeat: prefer
  snapshot/restore (or targeted `pop`) over `.clear()` whenever sharing a
  module-level singleton across tests.
- **`get_conversation` crossed the Phase 4 boundary.** It was added during
  Phase 6 because the streaming endpoint needs the conversation's model.
  Small and defensible as a follow-up, but worth flagging that the
  phase boundary in `PLAN.md` is a *plan*, not a hard wall — if a later
  phase reveals a gap in an earlier one, it's fine to backfill.
- **Sync DB calls inside async streaming handlers.** `_stream_assistant_reply`
  does `queries.append_message(...)` on the event loop thread after the
  stream completes. For a single-user local app this is fine; if the app
  ever serves concurrent users we'd wrap DB calls in `asyncio.to_thread`.
- **The lifespan creates a real `httpx.AsyncClient` even in tests.** The
  dependency override means the real client is never used, but it does
  get created and `aclose()`'d cleanly. Wasted work, harmless. Could be
  avoided by overriding the lifespan in tests, but the complexity isn't
  worth it for a few microseconds of startup.

## Open issues / follow-ups for later phases

- **Multi-query atomicity.** Each query in `app/queries.py` wraps its work
  in `with conn:`. If Phase 6 endpoints ever need a multi-query atomic
  operation (e.g., "create conversation + append the first message in one
  shot"), the inner `with conn:` blocks would commit too early. Refactor
  to caller-managed transactions at that point — not before.
- **Partial-response discard on client disconnect.** Not addressed here;
  a future iteration could persist incrementally and surface a "draft"
  flag, but PLAN.md doesn't require it.
- **Phase 7 will validate the SSE format choice in practice.** The
  `data: {"content": "..."}` shape is what we chose; HTMX's SSE extension
  will need either a small JS adapter or a frontend that knows to parse
  JSON from the data field. If that turns out to be a fight, the
  fallback is to switch to HTML-fragment-in-data (locks the backend to
  HTMX but simplifies the frontend).

## Notes for future phases

- **Ask the consequential design question up front.** SSE format was the
  biggest "wrong choice ripples through the frontend" call in the phase;
  asking before writing routes saved a likely refactor.
- **Trust the layered test infrastructure.** Phase 2 schema tests caught
  the cascade-delete contract; Phase 3 connection tests caught the
  threading contract; Phase 4 query tests caught the row-mapping
  contract. By Phase 6 we were testing routes against a real (tempfile)
  DB through the real query layer — that's faster to write and more
  trustworthy than mocking the DB at the route boundary.
- **Phase boundaries are guidance, not walls.** `get_conversation` proved
  the point: a small backfill into an earlier phase's module is
  preferable to a hack at the new phase's layer.
