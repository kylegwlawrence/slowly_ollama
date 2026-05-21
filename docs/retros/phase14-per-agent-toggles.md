# Phase 14 Retrospective — Per-agent toggles for the agentic loop

## What shipped

Two new sub-toggles nested under the existing `agentic_mode` master
toggle: **Reviewer agent** and **Generator agent**. The composition
of the three-agent loop is now user-controllable at runtime without
code changes.

Four storage/behavior configurations are live:

| Master | Reviewer | Generator | Behavior |
|---|---|---|---|
| off | * | * | Single-agent path — unchanged |
| on | on | on | Phase 13 full loop |
| on | on | off | Research↔Review loop; findings → assistant bubble |
| on | off | on | Research one-shot → generator synthesizes |
| on | off | off | Research one-shot → findings → assistant bubble |

## Changes by file

- **`app/queries.py`** — Four new helpers: `get_review_enabled`,
  `set_review_enabled`, `get_generator_enabled`, `set_generator_enabled`.
  Default-truthy (opposite of `get_agentic_mode`'s default-falsy) so
  enabling the master toggle for the first time keeps Phase 13 behavior.

- **`app/agents/loop.py`** — Two new kwargs on `_run_agentic_generation`
  (`review_enabled=True`, `generator_enabled=True`). Review pass wrapped
  in `if review_enabled:` with a break-on-first-pass `else:` branch.
  Generation pass wrapped in `if generator_enabled:` with a single-token
  emit of the findings text when off.

- **`app/generation.py`** — `start_generation` reads both new flags from
  DB when routing to the agentic producer and passes them as `**kwargs`.
  Single-agent path untouched.

- **`app/routes.py`** — `settings_endpoint` and
  `toggle_agentic_mode_endpoint` updated to thread `review_enabled` /
  `generator_enabled` into template context. Two new endpoints:
  `POST /settings/agentic-review` and `POST /settings/agentic-generator`.

- **`templates/_settings_agentic_section.html`** — Sub-toggles block
  inserted inside `{% if agentic_mode_on %}`, before the prompts
  `<details>`.

- **`static/style.css`** — `.agentic-sub-toggles*` and
  `.agentic-sub-toggle*` rules; left-border indent communicates
  cascade relationship.

## Test additions (419 total, up from 393)

- `tests/test_queries.py` — 6 new tests for the four helpers.
- `tests/test_agentic_loop.py` — 5 new tests: 4 configuration
  tests (reviewer off, generator off, both off, default-kwargs
  compat) + 1 error-during-review test that also filled a
  pre-existing coverage gap.
- `tests/test_generation.py` — 1 dispatcher test verifying
  `review_enabled` / `generator_enabled` are read and passed through.
- `tests/test_routes.py` — 6 new tests: toggle-on/toggle-off for
  each endpoint + sub-toggle visibility gated on master.

## Coverage

`app/queries.py` at 100%. `app/agents/loop.py` at 88% (19 misses,
all in `_strip_json_tool_calls`'s inner brace-scanner — a pre-existing
gap unrelated to Phase 14). All other files unchanged or improved.

## What worked well

- The plan's locked-decisions section made implementation mechanical.
  No architectural surprises.
- Default-truthy sub-toggles (absence → True) meant zero behavior
  change for anyone who had the master toggle on before Phase 14 shipped.
- The single-token emit for generator-off kept the wire protocol
  uniform (placeholder always fed before `done` lands).
- Adding the review-error test alongside the Phase 14 tests filled a
  real gap for free.

## What was deferred (open questions from the plan)

- `maybe_tool_call_streaming` variant for generator-off (findings
  would stream token-by-token instead of arriving as one chunk).
- `RESEARCH_AS_ANSWER_SYSTEM_PROMPT` variant for when generator is
  off (findings-tone answer might read oddly in some queries; bench
  in real use first).
- Per-chat snapshot of sub-toggle state (global runtime scope only
  for now, same as the master toggle).

## Notes for future phases

- The default-truthy/default-falsy asymmetry between
  `get_agentic_mode` and `get_review_enabled`/`get_generator_enabled`
  is deliberate. Master is opt-in; sub-toggles default on once master
  is on. Document this clearly if adding more sub-settings.
- The `if review_enabled: ... else: break` pattern inside the
  `for iteration in range(CAP):` loop is subtle — the for-else
  (max-iterations badge) is NOT reached when reviewer is off, because
  `break` fires on iteration 1. The plan called this out explicitly;
  keep it in mind if the loop structure changes.
- The HTMX swap target (`#settings-agentic-section` → `innerHTML`)
  means every sub-toggle POST re-renders the whole section. This is
  intentional — cheap and keeps state coherent — but means a toggle
  that fails (e.g. DB write error) would re-render the old state.
  Currently there's no error path for settings writes; if one is
  added, keep this in mind.
