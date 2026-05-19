# Phase 11 retrospective — UI improvements

## Scope

An off-PLAN.md extension: the app shipped at end of phase 10 with the
Google-Chat-styled v1 UI, but it felt clunky and a few features
listed as v1 non-goals (auto-titles, theme switching) had become
real wants. Phase 11 broke into four sub-phases, each its own
commit:

- **11a** — visual refresh: sage pastel palette, airy spacing, larger
  type scale, active-row affordance.
- **11b** — empty-state composer that creates a chat AND sends the
  first message in one round trip; the old `<details>`-driven
  "Compose" disclosure is gone.
- **11c** — manual light/dark toggle. **Deferred** — rendered blank
  in the browser despite passing tests and curl-rendered HTML
  looking correct. Reverted cleanly; plan file kept for a future
  attempt.
- **11d** — auto-generated chat titles after assistant replies 1-3.

After all four sub-phases I did a focused code-review pass that
landed three quality fixes (pill CSS consolidated, title-warning
flash eliminated, dead `import json` removed). Then a string of
small fixes for bugs that only surfaced in the browser: a
hx-push-url attribute leak, two timeout issues, an SSE event
ordering bug, a model-quality simplification, and a final 6-word
cap on auto-titles.

End state: 122 tests passing (was 102 at the start of the phase;
+20 net). 11 commits, one revert (11c). Phase 11 is feature-complete
modulo the deferred 11c.

## What landed

| Commit | Title |
|---|---|
| `4fd7cdb` | docs: phase 11 UI improvements plan |
| `0dacd92` | feat: phase 11a — sage accent + airy visual refresh |
| `4296254` | feat: phase 11b — empty-state composer; remove Compose disclosure |
| `ab5ff08` | feat: phase 11d — auto-generated chat titles via tinyllama |
| `274618c` | refactor: phase 11 review pass — pill share, warning flash, dead import |
| `fb66d69` | fix: cap generate_title HTTP timeout at 10s |
| `09dcd96` | fix: stop hx-push-url from leaking onto /models on page load |
| `9916cfa` | fix: bump default httpx timeout to 120s for chat streams |
| `0efc03a` | fix: deliver auto-title via SSE before done; smart placeholder name |
| `7fec07c` | refactor: reuse chat model for auto-titles, drop tinyllama special case |
| `93cd6f6` | feat: cap auto-titles at 6 words |

11c (dark mode) is not in this list — its draft never landed; the
revert returned the working tree to `4296254` before the next
commit. The sub-phase remains documented in
`docs/plans/phase11-ui-improvements.md` for a future attempt.

## Decisions (and why)

- **Sage pastel green (#9ccaa6) over Google blue.** The user picked
  it from a preview comparison. The palette is calmer and reads
  more "grown-up". Required adding a new `--accent-on` token
  (#1f3a26 deep sage) for text on filled-accent buttons — white on
  pastel sage fails WCAG contrast badly (1.95:1); deep sage on
  pale sage passes at ~5:1.
- **Empty state IS the composer.** No separate "+ New chat" form
  expansion — typing into the empty state creates the chat and
  sends the first message in one `POST /chats`. The response
  carries the rendered chat panel plus an OOB sidebar row plus
  an `HX-Push-Url` header. Removes a click and a UI state.
- **Manual rename locks the chat against auto-titling.** Once you
  pick a name, the auto-titler never overwrites it. The lock lives
  in a DB column (`name_locked`), checked at the query layer's
  UPDATE — so even a multi-tab race can't accidentally clobber a
  fresh rename.
- **Auto-titles use the chat's own model, not a separate one.** The
  first cut hardcoded `tinyllama:1.1b-chat-v1-fp16`. The user
  asked the obvious question after seeing it work: "why not just
  use the chat's model?" That removed ~117 lines (no
  `OllamaModelMissing` exception, no install-this-model banner
  template, no banner CSS, no `title-warning` SSE event, no
  hardcoded constant) and gave noticeably better title quality
  because the chat model is usually larger and follows
  instructions better than 1B-class models. The chat model is
  already warm in Ollama's memory, so reuse costs almost nothing.
- **6-word cap enforced server-side.** The prompt's word-count
  instruction is a hint that smaller models routinely ignore. The
  cap lives in `generate_title`'s post-processing — split on
  whitespace, keep first 6 words.
- **Smart placeholder name (first 40 chars of message).** Before
  the auto-titler runs (or if it fails silently), every chat used
  to show "New chat" — non-distinguishing. Now the placeholder is
  derived from the user's first message so the sidebar is useful
  immediately.
- **SSE order matters: title BEFORE done.** The `done` event uses
  an outerHTML OOB swap that removes the streaming placeholder —
  the element htmx-ext-sse's connection is bound to. The mutation
  observer closes the connection, and any event sent after `done`
  is dropped. So the title event has to fire first. UX cost: the
  placeholder keeps its `message--streaming` class for the title
  roundtrip (~1-2s), which keeps the send button disabled. Token
  text is already visible to the user, so it reads as a brief
  "settling" pause.
- **Plan-mode review pass before implementing.** Caught at least 4
  real bugs in the plan before any code was written (Pico's
  `prefers-color-scheme` leak path, missing `aria-current`
  default guard, wrong OOB swap form for prepending the sidebar
  row, missing `hx-swap-oob` on the title-warning fragment). The
  review changed the plan file rather than the code, which is
  much cheaper.

## What worked

- **Plan-mode review caught structural bugs cheaply.** The four
  bugs found in the plan would each have surfaced as failing
  smoke tests or visible browser bugs. Fixing them in a markdown
  edit is dramatically faster than fixing them mid-implementation
  or post-merge.
- **Smoke-testing each sub-phase against a real Ollama.** Pytest
  passes don't prove SSE behaviour because the test client reads
  the entire response body — it doesn't simulate HTMX's
  element-removal-triggered EventSource close. The browser does.
  Smoke-testing 11d caught the title-after-done ordering bug
  immediately. (It also caught the "Ollama unavailable" timeout
  bug.)
- **The user's simplification on 11d (reuse chat model) was the
  highest-impact change in the whole phase.** A late "wait, why
  do we even need a separate model?" question deleted 117 lines
  of code, removed a whole UX surface (install-this-model banner),
  and improved title quality. The "obvious in hindsight"
  simplification is the one most worth listening for.
- **Defensive post-processing on tinyllama output paid off.** The
  preamble stripper ("Title:", "Conversation:", etc.), quote
  stripper, line picker, and word/char caps all earned their
  keep when tinyllama produced things like "Quickly Summarize:
  2+2 is 4 using 3-6 word summary". The post-processing isn't
  pretty but is necessary for small models. With the move to
  chat-model titles in `7fec07c`, the stripper still helps even
  with larger models (which still sometimes wrap titles in
  quotes).
- **The pill CSS consolidation found by the review pass removed
  ~50 lines** of near-duplicate CSS between the composer and the
  in-chat message form. Single `.input-pill` class, both
  templates use it, easier to maintain.
- **The schema migration for `name_locked` survived live
  databases.** `PRAGMA table_info` check before `ALTER TABLE`
  meant the existing chat database picked up the new column on
  next startup with zero ceremony.

## What was tricky / went less well

- **Dark mode (11c) burned a sub-phase's worth of effort with no
  output.** Tests passed, rendered HTML looked correct under curl
  inspection, but the browser rendered a blank page. Couldn't
  diagnose without browser-level tooling (DevTools, screenshots,
  console output). Reverted the whole sub-phase cleanly. Lesson:
  for CSS/JS changes whose only failure mode is visual, the
  curl-based smoke test is not enough.
- **The chat-stream timeout bug only surfaced when the user tried
  a real chat.** httpx's default 5s read timeout is fine for
  normal web requests but absurdly tight for a local LLM's
  first-token latency on cold load (a 7B model can take 10-30s
  to warm up). Tests with `httpx.MockTransport` respond
  synchronously, so the suite never had a chance to catch this.
  Fixed by bumping the default to 120s in `9916cfa`.
- **The `hx-push-url` attribute inheritance bug was non-obvious.**
  I added `hx-push-url="true"` to the composer form so the URL
  would update after `POST /chats`. HTMX inherits the attribute
  down to descendants, so the inner `<select hx-get="/models"
  hx-trigger="load">` ALSO got it, and the model dropdown's
  load-time GET pushed `/models?model=` into the address bar
  before the user did anything. On reload, the page rendered the
  bare fragment. Removing `hx-push-url` from the form fixed it —
  the server's `HX-Push-Url` response header was already doing
  the URL update. Symptom-to-cause was minutes; would've been
  hours if the user hadn't reported the symptom clearly ("page
  defaults to `/models?model=`").
- **The title-after-done ordering bug was a knowledge gap.** I
  didn't know htmx-ext-sse closed the EventSource on placeholder
  removal. Once I understood the mechanism, the fix (reorder
  yields) was small. Caught only when the user said "the
  auto-gen chat titles do not show up" — at that point the bug
  was obvious to inspect.
- **Tinyllama is genuinely small.** "Quickly Summarize: 2+2 is 4
  using 3-6 word summary" is not a usable title. The chat-model
  pivot mitigates this, but if a user picks tinyllama as their
  chat model, the title will still be mediocre. The 6-word cap
  helps prevent egregious overshoot but can't fix bad word
  choice.
- **Five post-launch bug fixes is a lot.** Each was small and
  the user surfaced them quickly, but they suggest my pre-merge
  testing rituals miss certain failure modes — specifically
  anything that requires a real browser session and any LLM
  interaction that's not extremely fast.

## Surprises

- **A 1.1B chat model parses prompts almost literally.** The
  prompt "Summarize this conversation in 3-6 words as a concise
  title" produces "Quickly Summarize: 2+2 is 4 using 3-6 word
  summary" — the model treats "3-6 words" as a token sequence
  to echo. Switching to verb-first phrasing ("Title this
  conversation in 3 to 6 words") helped tinyllama; switching to
  a 7B chat model fixed it entirely.
- **HTMX inherits more attributes than expected.** I'd assumed
  `hx-post` / `hx-get` were the only attributes that mattered
  per element; in fact most htmx-* attributes inherit down the
  tree. That's powerful when used deliberately, dangerous when
  accidental.
- **The "stop the SSE connection from closing while we wait" need
  has no clean answer in htmx-ext-sse.** The mutation observer
  is hard-coded. The only escape hatches are: send everything
  before the connection-closing event, or build a multiplexed
  parent-element connection (way more infrastructure). I picked
  reordering; the UX cost (1-2s "settling" pause) is acceptable.
- **117 lines of code deleted in one refactor.** The "use the
  chat model for titles" simplification removed a hardcoded
  constant, an exception class, a template, CSS rules, an SSE
  event listener, an HTML mount point, and two tests. Software
  rarely gets that much simpler in a single commit.

## Open issues / follow-ups

- **11c (dark mode) deferred.** The plan in
  `docs/plans/phase11-ui-improvements.md` is unchanged from when
  we started — pick it up by installing a headless browser
  (Playwright) to diagnose the blank-page issue, then re-attempt.
- **No automated visual-regression coverage.** Three of the post-
  launch bugs (push-URL leak, dark-mode blank, SSE title) were
  visual / interactive failures that pytest can't catch. A
  Playwright smoke test that loads the page, sends a message,
  and screenshots the result would catch this class of bug. Not
  worth the cost for a single-user local app, but flagged.
- **Title quality depends entirely on the chat model.** A user
  who picks a small model gets mediocre titles. Could surface a
  per-conversation title-model override in settings if this
  becomes annoying.
- **The `_maybe_generate_title` race window is bounded but
  exists.** A user with two tabs open could send a 4th message
  in tab B during tab A's title generation, and the title would
  fire based on a now-stale 1..3 count. Worst case is one extra
  title fire; `name_locked` plus the UPDATE-with-WHERE-clause
  guarantee no manual rename ever gets overwritten. Acceptable.
- **PLAN.md doesn't mention phase 11 at all.** Phase 11 was an
  off-plan extension. Worth deciding whether to retroactively
  add phase 11 to PLAN.md's phase list (and 12+ for any future
  extensions) or leave PLAN.md as a frozen v1 build plan.

## Notes for future phases (if any)

- **Smoke-test in a real browser, not just via curl.** Curl reads
  the response body and shows you bytes. Browsers run JS, run
  HTMX, fire mutation observers, evaluate CSS cascades. The bugs
  that only surface in browsers are exactly the ones curl can't
  catch. For UI work, opening the page in Chrome is mandatory
  before declaring "ship it" — or, better, a Playwright headless
  test that loads the page and asserts what the user sees.
- **Listen for "wait, why do we even need that?" questions.** The
  tinyllama-to-chat-model pivot was the single highest-impact
  change in the phase. Spotting a simplification mid-build, and
  taking it, is usually cheaper than carrying complexity to
  completion.
- **The plan-mode review pass earns its keep.** Catching
  structural bugs in markdown is dramatically faster than catching
  them in code. The pre-implementation review found four real
  bugs that would have shipped otherwise.
- **HTMX attribute inheritance is a footgun.** Future HTMX
  attributes added to a form or container should be paired with
  an explicit "do descendants need to NOT inherit this?" check.
  When in doubt, prefer server-side mechanisms (response headers)
  over client-side inherited attributes.
- **httpx's default timeout is wrong for local LLM apps.** Any
  new client created in this codebase should set
  `timeout=httpx.Timeout(120.0, connect=5.0)` or similar. The
  default 5s read is calibrated for normal web traffic, not for
  models that take 10-30s to load.
- **Post-launch fixes that are each tiny still feel like a lot.**
  Five fixes after the phase's main commits is a signal that
  the testing rituals don't cover something. Worth periodically
  asking: "what's the class of bugs my tests can't see?" and
  building one bridge each phase.

## Wrap-up

Phase 11 added the visual modernization, composer flow, and
auto-titles the user wanted, plus a handful of post-launch fixes.
122/122 tests pass at the close. The deferred 11c (dark mode) is
the only outstanding item.

The original PLAN.md is unchanged; phase 11 is an addition
documented entirely in `docs/plans/phase11-ui-improvements.md`
(plan) and this file (retro). Future phases — if any — could
extend the plan or branch new ones.
