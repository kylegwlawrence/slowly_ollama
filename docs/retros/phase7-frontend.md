# Phase 7 retrospective — HTMX + Jinja frontend

## Scope

Phase 7 put a usable browser UI on top of the Phase 6 backend: sidebar with
conversations, new-chat form with a model dropdown, chat panel that streams
the assistant's reply token-by-token, plus rename / delete / regenerate
controls. The big constraint was "no JS framework, no build step" — HTMX +
Jinja templates do all the work, with two tiny inline scripts for things
HTMX couldn't express cleanly.

End state: 91 tests passing (was 63 entering the phase). Twelve commits.

The phase shipped in four broad waves: backend refactor + persistence,
static-asset vendoring, a sidebar new-chat form, and three UI controls
(delete / rename / regenerate). A post-phase review caught five bugs, which
each got their own small commit.

## What landed

| File | Role |
|---|---|
| `app/routes.py` | Rewritten — 13 endpoints, every response is HTML or SSE-of-HTML, no JSON |
| `main.py` | Same lifespan as Phase 6 plus `StaticFiles` mount at `/static` |
| `templates/base.html` | Page shell with vendored Pico + HTMX, plus two CSS rules (streaming disable + regenerate visibility) |
| `templates/index.html` | Sidebar + main panel layout; either embeds the chat panel or shows an empty state |
| `templates/_chat_item.html` | One sidebar row with rename + delete buttons |
| `templates/_chat_item_edit.html` | Sidebar row in edit mode (rename form) |
| `templates/_chats_list.html` | Wraps the per-item rows in `<ul id="chats-list">` |
| `templates/_chat_panel.html` | Right-hand panel: messages + send form + auto-scroll script |
| `templates/_message.html` | One bubble; conditionally includes the regenerate button on assistant messages |
| `templates/_assistant_placeholder.html` | Empty streaming bubble that opens an SSE connection on insert |
| `templates/_model_options.html` | `<option>` tags for the model dropdown; includes a disabled-error option when Ollama is down |
| `templates/_new_chat_form.html` | Sidebar form to create a conversation; model dropdown auto-loads from `/models` |
| `static/pico.classless.min.css`, `static/htmx.min.js`, `static/htmx-ext-sse.js` | Vendored assets, ~130KB total |
| `tests/test_routes.py` | Grew from 14 to 39 endpoint tests; HTML-substring assertions throughout |

## Decisions (and why)

- **HTML fragments for every route, not JSON.** Initial Phase 6 instinct was
  to ship JSON and adapt for HTMX in Phase 7. That turned out to be the
  wrong call — HTMX doesn't consume JSON natively and a JS adapter layer
  would have defeated the "no build step" rule. Reopening that decision
  cost ~one commit (`aa1cde4`) and was cheap because the backend logic
  (queries, ollama client) didn't change at all; only `routes.py` and
  `test_routes.py` got rewritten.
- **No `/api` prefix.** Every consumer is HTMX, there's no separate JSON
  API to keep namespaced.
- **Vendored static assets, not CDN.** Matches the project's "local-only,
  no cloud calls" rule. The app stays usable offline and the first paint
  doesn't depend on an external host. ~130KB committed; acceptable.
- **Pico CSS classless.** Semantic-HTML defaults without a class-name
  vocabulary to learn. Two custom CSS rules in `base.html` handle the
  things Pico can't (streaming-disable and regenerate visibility).
- **JSON-in-SSE-data → HTML fragments (mid-phase reversal).** Phase 6 chose
  JSON-in-data; Phase 7 reopened the call because the natural HTMX consumer
  for SSE is `sse-swap` which expects HTML. Three named events on the wire:
  `token`, `done`, `error`. The done event's payload carries `hx-swap-oob`
  so it replaces the streaming placeholder by id, while token events keep
  appending text inside it.
- **Two-step POST-then-GET for streaming.** `htmx-ext-sse` only opens
  connections via GET-based `sse-connect`. So `POST /chats/{id}/messages`
  saves the user message and returns the placeholder; the placeholder's
  `sse-connect` triggers `GET /chats/{id}/stream` which drives the actual
  SSE. The seam between the two endpoints is the conversation state —
  the stream reads the latest user message from the DB rather than taking
  it as a parameter.
- **Branch `GET /chats/{id}` on `HX-Request` header.** HTMX requests get
  the bare `_chat_panel.html` fragment; direct browser hits (reload,
  bookmark, back/forward) get the full `index.html` with the chat
  preloaded. `hx-push-url="true"` on sidebar links keeps the URL in sync
  with HTMX-driven swaps. Net result: `/chats/{id}` is a real
  reload-safe URL with progressive enhancement.
- **Regenerate button on every assistant message + CSS to show only the
  last.** Server-side "is this the last assistant message?" flag would
  have required passing the flag through every template that renders
  `_message.html` (chat-panel iteration, SSE done event). A CSS
  `:last-child:not(.message--streaming)` rule does the job without state
  in the templates. Same payload is correct in every context.
- **`/models` returns 200 with a disabled error option, not 5xx.** HTMX
  won't swap on a non-2xx response by default, so a 503 would leave the
  dropdown stuck on "Loading models…". A 200 with a disabled `<option>`
  swaps in cleanly and the form's `required` attribute still blocks
  submission. Loses strict 5xx semantics but `/models` has no non-HTMX
  consumer to care.
- **Snapshot/restore `dependency_overrides` in the test fixture.**
  Carried forward from the Phase 6 review-fix; `.clear()` would wipe any
  override added by a future conftest.py-level fixture.

## What worked

- **The Phase 6 decision was reversible cheaply.** Rewriting `routes.py`
  for HTML output didn't ripple into the storage or Ollama-client
  layers — that boundary held. Worth keeping in mind: layer-by-layer
  output formats are easier to change than layer-by-layer interfaces.
- **AskUserQuestion before consequential decisions.** The SSE-format
  call ("JSON in data" vs "HTML fragments" vs "HTMX-specific HTML") was
  surfaced upfront; getting it slightly wrong the first time still let
  the second pass land in one commit. Same for the "all routes return
  HTML" follow-up — explicit consent before the rewrite avoided
  having to undo a half-built thing.
- **Post-phase code review catches real bugs.** The chat-panel form's
  unconditional `this.reset()`, the SSE done-event-appending-instead-of-
  replacing bug, the `closing()` slip on `dependency_overrides` — all
  caught in summary-flagged "worth flagging" lists and fixed before
  they shipped to a browser session.
- **HTML-substring assertions scale further than expected.** Tests use
  `'data-chat-id="42"' in response.text` and similar — fragile in
  theory, fine in practice because the templates include stable
  `data-*` attributes precisely so tests have something to match.
- **`httpx.MockTransport` continues to pay off.** The Phase 5
  infrastructure for mocked Ollama responses adapted to Phase 6 and
  Phase 7 without changes; each test just defines a handler.
- **End-to-end smoke via uvicorn**. The Phase 7 step 2 verification
  curled the index page through a real uvicorn process — much
  stronger signal than TestClient alone for the static-mount and
  template-resolution paths.

## What was tricky / went less well

- **`htmx-ext-sse` + OOB swap semantics are under-documented.** I had to
  reason through the order-of-operations (sse-swap routes data to a
  target, HTMX processes OOB elements out-of-band as part of the same
  swap, the placeholder gets replaced after the main swap effectively
  no-ops) and confirm via tests on the response body — the actual
  browser behavior is still partly trust-based. The codebase carries
  detailed comments on this for a future reader.
- **Splitting send into POST + GET added cognitive weight.** Two
  endpoints for one user action (send a message). The reasons are good
  (htmx-ext-sse + POST not supported, conversation-state seam works for
  single-user) but it took a chunk of design time and the comments
  needed to explain *why* take more space than the code.
- **CSS-only solutions for "soft disable while streaming" have
  caveats.** `pointer-events: none` blocks mouse but not keyboard. Good
  enough for v1; the alternative (JS handler tied to the placeholder's
  lifecycle) is more complex and the lifecycle of `htmx:sse-close`
  vs OOB-swap-removing-the-placeholder isn't pinned down anywhere
  precise.
- **Auto-scroll required two mechanisms.** `hx-on::after-swap` handles
  the streaming + new-message cases (events bubble inside `#messages`).
  But initial chat-panel load doesn't fire a swap event on `#messages`
  — the panel itself is the swap target. A tiny inline script at the
  end of `_chat_panel.html` covers that case. Two mechanisms for one
  conceptual feature feels off, but each is correct in its window and
  the alternative ("re-fire swap events after the panel mounts") would
  be more code and more magic.
- **`autofocus` on the rename input is unreliable through HTMX swaps.**
  Works on initial page load, inconsistent for dynamically-inserted
  content. Acceptable for v1 — flagged in the review.
- **The visible UI behaviors aren't testable from Claude's side.**
  Unit tests verify the rendered HTML has the right attributes; whether
  the OOB swap actually replaces the placeholder in a browser, or
  whether `:has()` resolves on the user's specific Chrome version,
  has to be confirmed by the user. The review surfaced these caveats
  explicitly so the user knew where to look.
- **One "oops" commit (`ec8e631`).** The rename work landed in a commit
  with a placeholder title because the user ran `git commit -am ...`
  in parallel with my staging. Contents were correct, message wasn't.
  Worth noting as a coordination thing — when working interactively
  with a human committer, my "I'll stage these N files and commit" can
  collide with theirs.

## Open issues / follow-ups for Phase 8 or later

- **No auto-navigate to the new chat after creating one.** User has to
  click the new sidebar row to open it. The alternative (OOB swap to
  also set the chat panel + push URL) is more involved.
- **No auto-focus on the rename input through HTMX swaps.** Add
  `hx-on::htmx:load="..."` if it becomes annoying.
- **Keyboard activation can still fire a send while streaming.** The
  CSS soft-disable only blocks mouse. Real fix is a JS disable tied to
  the placeholder lifecycle; defer until the keyboard path becomes a
  real problem.
- **Two simultaneous SSE streams** if a regenerate button is somehow
  clicked twice in quick succession before the first OOB-swap fires.
  CSS hides the button during streaming so this is unreachable through
  the UI, but the server doesn't enforce single-stream-per-conversation.
- **Phase 8 is the only remaining PLAN.md item.** Full test suite +
  Ollama mocking strategy. The mocking work is already done in spirit
  (MockTransport across Phase 5–7 tests); Phase 8 is mostly about
  rounding out coverage that wasn't natural to land per-phase.

## Notes for future phases

- **Reopen prior-phase decisions when the consumer surfaces the wrong
  shape.** Phase 6 picked JSON in good faith; Phase 7 made it obvious
  that HTML was the right call. The rewrite was localized because the
  prior phase had been disciplined about layer boundaries (queries
  didn't leak through routes).
- **For HTMX-heavy templates, document the *why* in the template
  comment.** Six months from now nobody will remember why the
  placeholder has `sse-swap="token,done,error"` with `hx-swap="beforeend"`
  alongside an OOB attribute on the done payload. The comments are
  longer than the code in some places — worth it.
- **Inline JS is OK when HTMX can't express the thing.** Two tiny
  inline scripts in this phase (initial-scroll, post-delete navigate).
  Neither needed a separate `.js` file; both are documented at the
  point of use.
- **Static-asset vendoring is small enough to do.** Three files,
  ~130KB, fully offline-capable. Worth defaulting to over CDN even
  when the project isn't strictly "local-only."
- **Test what's rendered, not what the framework does.** The route
  tests assert on the actual HTML coming back through `TestClient` —
  not on the template name passed to `TemplateResponse`. That's the
  layer where bugs actually manifest.
