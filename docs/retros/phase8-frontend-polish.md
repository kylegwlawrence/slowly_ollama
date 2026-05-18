# Phase 8 retrospective — Frontend polish (Google Chat aesthetic)

## Scope

Phase 8 reshaped the Phase 7 UI from "Pico defaults with unicode glyphs" to
a simple, modern Google Chat aesthetic: 256px sidebar, big filled
"Compose" disclosure, kebab popover for per-row actions, pill-shaped
message bubbles with consecutive-bubble corner grouping, Material Symbols
icons, pill input with inline send icon, animated typing dots while
streaming, and a soft empty-state. Light-only, system font, blue accent
reserved for the send button + focus rings.

Mid-project, the phase plan itself was the lead artifact: rather than
sketch a high-level outline and iterate, we wrote a detailed
handoff-ready plan to `docs/plans/phase8-frontend-design.md` and then
executed it. Two memories landed during the discussion as a result: one
about handoff-detailed plans, one about putting plans in `docs/plans/`.

End state: 91 tests passing throughout (same count as entering the
phase — no test changes needed, every substring assertion was
preserved). Eight commits.

## What landed

| File | Role |
|---|---|
| `docs/plans/phase8-frontend-design.md` | The detailed execution plan (new) |
| `docs/plans/PLAN.md` | Renumbered: prior Phase 8 (tests) → Phase 9; new Phase 8 inserted |
| `static/material-symbols.woff2` | Google's Outlined variable font, 318KB vendored |
| `static/material-symbols.css` | `@font-face` + `.material-symbols-outlined` class wiring |
| `static/style.css` | Hand-written visual layer (~430 lines): tokens, layout, sidebar, chat panel, bubbles + grouping, typing dots, pill input, kebab popover |
| `templates/base.html` | `<link>` to new stylesheets; two test-pinned inline CSS rules retained |
| `templates/_new_chat_form.html` | Wrapped in `<details class="compose">` with the form inside; collapses on success |
| `templates/_chat_item.html` | Rename + delete moved into a `:focus-within` kebab popover; preserved every `hx-*` attribute |
| `templates/_chat_item_edit.html` | Material Symbols icons for save/cancel; structural class names unchanged |
| `templates/_chat_panel.html` | Textarea + submit wrapped in `.message-form__pill`; send button is now a Material Symbols glyph in a circle; auto-grow handler added for non-Chrome browsers |
| `templates/_message.html` | Regenerate button uses a `refresh` glyph |
| `templates/_assistant_placeholder.html` | Collapsed to a single line so the div is genuinely `:empty` until the first token swaps in (typing dots via `:empty::before`) |
| `templates/index.html` | Empty state gets a soft Material Symbols `chat` icon |

## Decisions (and why)

- **Plan-first execution.** Instead of writing-and-iterating, we wrote
  the full plan (with concrete CSS, exact HTML diffs, test-substring
  preservation table) into `docs/plans/phase8-frontend-design.md` and
  executed it commit-by-commit. The plan was 1250 lines; the
  implementation barely diverged from it. Saved a memory:
  **detailed plans for handoff** — when a plan is meant for another
  agent to execute, include concrete code snippets, not prose.
- **Kebab popover via `:focus-within`.** Pure CSS show/hide — no JS
  for the toggle, no click-outside-to-close handler. Clicking the
  kebab focuses the button; CSS reveals the popup. Clicking a popup
  item fires HTMX which swaps the row, naturally closing the popup.
- **Compose disclosure via `<details>`.** Native HTML with no JS for
  the toggle. The form's `hx-on::after-request` sets
  `details.open = false` on successful submit.
- **Typing dots via `:empty::before`.** Collapse the placeholder
  template to a single line (no whitespace inside) so `:empty` matches;
  pseudo-element renders the three-dot animation; the first streamed
  token appends as a text node, the div is no longer `:empty`, and the
  pseudo-element disappears automatically. Zero JS, zero state.
- **Bubble grouping via `+` and `:has(+)` selectors.** No
  "is_first_of_group" template flag; pure CSS detects consecutive
  same-sender bubbles and shares the adjacent corner.
- **Inline CSS rules stayed in `base.html`** for the two cases tests
  substring-match: `pointer-events: none` on the streaming-disabled
  send button, and the `.message__regenerate` visibility rules.
  Moving them to `style.css` would have broken
  `test_base_disables_message_button_while_streaming` and
  `test_base_css_hides_regenerate_except_on_last_assistant`.
- **`:has()` + `:focus-within` over JS-driven solutions.** Both are
  well-supported in modern browsers (Chrome 105+, Safari 15.4+,
  Firefox 121+). For a local app where the user controls their
  browser, this is fine.
- **Plans live in `docs/plans/`.** First execution step of the phase
  was to materialize the workspace plan file into the repo. Saved a
  memory: **plans in docs/plans/** — project plans are repo files,
  not workspace files.

## What worked

- **Test substring assertions made the redesign safe.** Every pinned
  attribute (`hx-delete=`, `hx-confirm=`, `class="chat-item"`,
  `hx-push-url="true"`, the `pointer-events: none` rule, etc.) was
  preserved across the structural rearrangement. 91 tests held green
  through every single commit, including the kebab-menu rewrite which
  moved buttons three levels deeper in the DOM.
- **End-to-end smoke via uvicorn.** Curling `GET /` confirmed all five
  stylesheets/scripts were referenced and all six static assets served
  with the expected byte counts. This caught nothing in this phase but
  raises confidence that the static mount + template links + render
  pipeline are intact.
- **Post-phase review caught real bugs the tests couldn't.** The
  `display: inline-block` vs `inline-flex` issue on the regenerate
  button (flex properties had no effect under inline-block), the
  missing `field-sizing` fallback for Safari/Firefox, and the
  inline `style=""` in `index.html` — all surfaced in code review,
  none blocked tests. Same pattern that worked in Phase 6 and 7.
- **Plan mode + detailed plan document.** Forced precision up front;
  decisions were captured as a document rather than tribal knowledge.
  When execution started, there were no design choices left to make.
- **Question rounds caught architectural choices early.** Four rounds
  of `AskUserQuestion` (16 questions total) front-loaded all the
  design calls before any CSS was written. No mid-execution "wait,
  should the sidebar collapse?" reversals.

## What was tricky / went less well

- **Google Fonts user-agent sniffing for woff2 vs TTF.** The default
  `curl` user-agent got a TTF — Google delivers woff2 only when the
  request looks like a modern browser. Needed to spoof Chrome's UA
  to get the smaller, more-widely-supported woff2 file. Easy to miss
  if you only check the response file extension after the fact.
- **Material Symbols variable font is 318KB**, not the ~80KB the plan
  estimated. The variable font ships all glyph variations (FILL,
  weight, grade, optical size) in one file. Acceptable for a local
  app; would matter if we ever cared about edge bytes. Worth knowing
  the next time we estimate.
- **`field-sizing: content` is Chrome-only.** The plan called for a
  JS fallback (`hx-on:input` recalculating height). I skipped it in
  the initial implementation; code review caught it. The Safari /
  Firefox experience would have been a fixed-height textarea that
  scrolls internally — usable but ugly. Lesson: when a plan explicitly
  flags a fallback, implement it the first time even if it feels
  redundant.
- **`display: inline-block` blocked flex properties** on the
  regenerate button. The "show" rule in `base.html` set
  `display: inline-block`, but the visual rules in `style.css` used
  `align-items` and `gap` (which only apply to flex/grid). Result:
  icon and label sat next to each other without controlled spacing.
  Tests pin the selector, not the display value, so the bug rode
  through 91 green tests. Caught in review.
- **Plan-mode workspace file vs `docs/plans/`.** Plan mode mandates a
  specific workspace file path (`.claude/plans/<slug>.md`). The user
  wanted the plan in `docs/plans/` instead. Solved by making
  "materialize the plan in `docs/plans/`" the literal Commit 0 of
  the phase. Saved a memory so future plan-mode sessions for this
  project start there.
- **Mid-work commit collision** from earlier phases didn't recur here,
  but a related coordination thing: the plan-mode workflow makes it
  hard to interleave clarifying questions with implementation. After
  the initial 16 questions, the plan is locked and any execution-time
  surprises (e.g., woff2 vs TTF) need an exit-and-re-plan or a
  judgment call.

## Open issues / follow-ups

- **Visual verification is still on the user.** Unit tests can't see
  pixel layouts, animation feel, or how `:has()` resolves in your
  specific browser. The verification checklist in
  `docs/plans/phase8-frontend-design.md` lists the 12 things to walk
  through.
- **Older browser graceful degradation is partial.** `:has()` and
  `:focus-within` and `field-sizing` all have widely-supported
  modern browsers but no specific fallback. If someone opens this on
  an old browser the kebab popover wouldn't be reachable. Not
  blocking for a single-user local app.
- **Material Symbols woff2 is 318KB**, larger than typical for a
  vendored icon set. If size matters later, we can subset the font to
  only the glyphs we actually use (edit, delete, refresh, send,
  more_vert, check, close, chat) — that'd drop it under 10KB.
- **Pico CSS overrides might still surface visual oddities** under
  inspection (Pico has opinionated defaults for `<button>`,
  `<select>`, `<details>`). Browser QA is the only way to find them.

## Notes for future phases

- **Detailed handoff plans pay off.** Spending an extra round writing
  out concrete code in the plan (not just descriptions) meant zero
  re-design during execution. Worth doing again when the work is
  substantial.
- **Materialize plans in `docs/plans/`.** Workspace-only plan files
  vanish; repo plan files are searchable, version-controlled, and
  reviewable in a PR.
- **Tests should pin contracts, not implementations.** Phase 7's tests
  pinned `data-chat-id` and `hx-delete` substrings — those are
  contracts. They didn't pin DOM tree shape, which let Phase 8
  rearrange the DOM freely. If the tests had pinned shape (e.g.
  "first child of `<li>` is the rename button"), the kebab refactor
  would have required test rewrites.
- **`:has()`, `:focus-within`, `:empty::before` are powerful.** Three
  interactive features (kebab popup, typing dots, bubble grouping)
  shipped without writing a single line of JS. Worth checking these
  primitives first before reaching for a script.
- **Pico-on-the-bottom worked.** Loading Pico classless first and
  overriding via a higher-specificity `style.css` meant we kept
  sensible browser-default form styling for free and only had to
  write CSS for the bits we cared about.
- **Code review catches CSS bugs tests can't.** A `display:
  inline-block` swallowing flex properties is not the kind of thing
  unit tests can see. The post-phase review pattern from Phases 6
  and 7 continues to be the safety net.
