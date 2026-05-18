# Phase 9 retrospective — Frontend bug fixes

## Scope

Five user-reported issues that browser testing turned up after Phase 8
shipped. Each one was small in isolation; the fifth (rename flow) was
the only real diagnostic challenge. No new features, no scope
expansion — pure bug fixes on top of the Phase 7/8 frontend.

End state: 94 tests passing (was 91 entering the phase; +3 net from
the HX-Location split + the rename round-trip test). Nine commits.

The previous Phase 9 (full test suite) becomes Phase 10.

## The bugs

| # | Bug | Root cause | Fix |
|---|---|---|---|
| 1 | New chat row appeared in sidebar but didn't open in the main panel | POST /chats only returned the sidebar `<li>`; nothing updated `#main` | OOB swap of `#main` + `HX-Push-Url: /chats/{id}` |
| 2 | Enter didn't send; Shift+Enter didn't newline | No keydown handler on the chat textarea; both keys hit the default textarea behavior (both insert a newline) | One-line `hx-on:keydown` handler |
| 3 | Typed text in chat input was too light | No explicit `color` on `.message-form textarea`; Pico's default for form elements bled through | Explicit `color: var(--text-primary)` |
| 4 | Deleting the currently-viewed chat didn't clear the panel | Brittle client-side `hx-on::after-request` on a button that was being deleted in the same swap | Server-side `HX-Location: /` keyed off the `Referer` header |
| 5a | Rename: edit form opened but input was 0px wide | Pico's `button[type=submit] { width: 100% }` made the Save button consume the entire flex row | `flex: 0 0 auto; width: auto` on the edit-form buttons + `min-width: 0` on the input |
| 5b | Rename: input had a dark background | Pico's form-element background flips under `prefers-color-scheme: dark` | Explicit `background: var(--bg); color: var(--text-primary)` |

## What landed

| File | Role |
|---|---|
| `docs/plans/phase9-frontend-fixes.md` | The detailed plan, materialized into the repo |
| `docs/plans/PLAN.md` | Phase 9 renumbered to Phase 10; new Phase 9 inserted |
| `app/routes.py` | `create_chat_endpoint` rewritten to emit two fragments + HX-Push-Url (Bug 1); `delete_chat_endpoint` rewritten to emit HX-Location conditional on Referer (Bug 4) |
| `templates/_chat_panel.html` | `hx-on:keydown` added to the message textarea (Bug 2) |
| `static/style.css` | `color` on the chat textarea (Bug 3); `flex: 0 0 auto; width: auto` on edit-form buttons + `min-width: 0` + explicit background/color on the rename input (Bug 5) |
| `templates/_chat_item.html` | Removed the brittle inline JS from the delete button (Bug 4) |
| `tests/test_routes.py` | `test_chat_item_has_delete_button` tightened (one assertion removed); two new tests for the Referer-based HX-Location; one new round-trip test for the rename flow |

## Decisions (and why)

- **Server-side HX-Location for the delete-clears-panel fix.** The
  prior client-side `hx-on::after-request="...window.location='/'"`
  fired on a button whose parent `<li>` was being deleted in the same
  swap — event delivery to detached elements isn't reliable across
  browsers, and the symptom matched. Moving the navigate decision to
  the server (read `Referer`, emit `HX-Location: /` only when the
  user is viewing the deleted chat) sidesteps the timing entirely.
- **OOB swap + `HX-Push-Url` for new-chat auto-open.** The textbook
  HTMX way to update two unrelated DOM regions from one response.
  No JS, no follow-up request.
- **`requestSubmit()` for the Enter-to-send handler.** Triggers the
  form's submit pipeline the same way clicking the Send button does,
  so HTMX intercepts identically. `!event.isComposing` skips IME
  composition (Japanese / Chinese / Korean) so an Enter that closes
  the IME picker doesn't double as a send.
- **`min-width: 0` on the flex input.** A flex item's default
  minimum width is its intrinsic content width; an input has an
  intrinsic content width (~150px). Without `min-width: 0` the input
  refuses to shrink, and combined with Pico's `button[type=submit]
  { width: 100% }` (which crushed the rest of the row) the result
  was the visible bug.
- **Curl + grep into Pico's CSS as the diagnostic.** I'd looked at
  my own CSS three times for the rename bug and seen nothing wrong
  — because the bug was in the interaction between my rules and
  Pico's, not in mine alone. Curling `pico.classless.min.css` and
  grepping for `input` / `button[type=submit]` surfaced the
  `width: 100%` rule in seconds.

## What worked

- **Plan-first execution (continuing from Phase 8).** Wrote the
  plan into `docs/plans/phase9-frontend-fixes.md` with concrete code
  diffs, then executed commit-by-commit. Of the five bugs, four were
  fixed exactly as the plan described. Only Bug 5 required live
  diagnosis — and the plan acknowledged that up front
  ("Cause not determinable from static analysis. Live-debug…").
- **HTTP-layer curl tests narrowed Bug 5 fast.** Before fixing
  anything, I curled GET `/chats/{id}/edit` and PATCH `/chats/{id}`
  — both returned exactly the right HTML. That immediately told me
  the bug was browser-side / CSS-side, not server-side. Saved time
  that would otherwise have gone into reading route code.
- **The round-trip test as a regression catcher.** The new
  `test_rename_round_trip_via_edit_and_patch` doesn't catch the
  CSS bug we just fixed (it's at the HTTP layer), but it would
  catch any future route change that broke the GET-edit → PATCH
  contract. Worth having as a contract pin even when the
  immediate bug lives elsewhere.
- **Tests preserved across structural changes again.** Bug 4
  required removing an inline JS string that an existing test
  pinned. The fix was to update one test (drop one substring) and
  add two new tests targeting the new server-side behavior — clean
  split, no fragility.
- **Material Symbols icons came in handy for the empty-state and
  the rename buttons.** Already vendored from Phase 8, just needed
  to reference glyph names (`check`, `close`, `refresh`, etc.).

## What was tricky / went less well

- **Bug 5 misdiagnosed initially.** I assumed it was an HTMX or
  browser interaction issue (kebab focus-loss race, autofocus
  unreliability, HTMX PATCH body encoding). All wrong. The user's
  follow-up — "the text box opens but is very very narrow and left
  aligned" — was the decisive clue. "Left aligned" + "very narrow"
  immediately suggested flex layout failing, not interaction
  failing. Lesson: when a bug report is vague ("doesn't work"), ask
  one clarifying question about visual symptoms before guessing.
- **Three Pico-related fixes in one phase.** Pico's classless
  variant has opinions about form elements (background, text color,
  height, margin-bottom, width on submit buttons) that fight with
  our custom CSS unless we override every relevant property. Bug 3
  (textarea color), Bug 5a (button width), and Bug 5b (input
  background) were all the same shape: Pico sets property X that
  we didn't anticipate, our rule sets properties Y/Z but not X.
- **The kebab popup's `:focus-within` mechanism still has a
  theoretical click-loses-focus race**, but the user didn't hit it.
  Confidence in `:focus-within` for production-quality UIs is
  thinner than my Phase 8 retro implied — under specific browser
  behaviors (Safari macOS) it might still misfire. No fix needed
  yet; flagged for if/when it surfaces.
- **One mid-phase plan deviation.** Bug 5's plan said "investigate
  in browser via DevTools." I couldn't open DevTools, but I could
  curl Pico's CSS. The actual workflow was different from what the
  plan described, but reached the right answer.

## Open issues / follow-ups

- **Pico is fighting us systematically.** Three property collisions
  in one phase. The "right" fix is one of:
  - Replace Pico with hand-written CSS (Phase 8 retro called this
    out as an option; deferred).
  - Systematically override every Pico form-element property in
    `style.css` up front (defensive but verbose).
  - Continue fighting fire-by-fire as bugs surface (current
    posture; works but reactive).
- **Kebab popup focus race.** Not yet reproducible, but the
  `:focus-within` mechanism is technically vulnerable.
- **Phase 10 (full test suite) is the only PLAN.md item remaining.**
  Worth bumping the new round-trip test pattern into Phase 10 as a
  general principle: tests should exercise full request-response
  round-trips at the HTTP layer, not just isolated routes.

## Notes for future phases

- **Curl + grep into vendored CSS is a fast diagnostic for "my
  rule doesn't seem to be applied."** When my CSS looks right and
  the visible behavior is wrong, the answer is often that a
  framework rule (Pico, in this case) wins on a property I didn't
  override. The shortcut: `curl /static/pico.classless.min.css | tr
  '}' '\n' | grep <selector>` shows you every Pico rule that
  matters in seconds.
- **One clarifying question can be worth ten minutes of static
  analysis.** "What do you see?" or "What's the visible symptom?"
  on a vague bug report cuts through a lot of guessing. I asked
  none on the initial Bug 5 attempt; got specifics on the second
  pass; fixed it in three lines of CSS.
- **The OOB-swap + HX-Push-Url pattern for "create then navigate
  to" flows is a clean HTMX pattern.** Same pattern would work for
  "create message + scroll into view" or any other "one HTTP
  response, two DOM regions" need.
- **Server-side `HX-Location` over client-side `window.location`**
  for the "this action should navigate the page" case. Less code,
  no detached-handler timing issues, the server has all the info
  it needs (Referer).
