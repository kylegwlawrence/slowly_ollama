# Phase 10 retrospective — Full test suite

## Scope

PLAN.md's brief for this phase was a single sentence: "round out
tests across all layers" and "decide on Ollama mocking strategy."
Both turned out small in practice — coverage was already at 96%
entering the phase, the decision was easy ("keep mock-only,
document it"), and the highest-value addition was a single
end-to-end integration test that exercises the full user journey
through `TestClient`.

End state: 102 tests passing (was 95 at the start of the phase;
+7 net), 99% coverage on `app/` + `main.py`. Five commits.

This is the last phase listed in PLAN.md.

## What landed

| File | Role |
|---|---|
| `docs/plans/phase10-full-test-suite.md` | The plan, materialized into the repo |
| `requirements.txt`, `tests/test_dependencies.py` | `pytest-cov` added as a direct dep + importability check |
| `tests/test_routes.py` | 5 new tests filling the previously-uncovered branches: stream 404, regenerate 404, regenerate-stream 404, regenerate-stream 400 (no assistant), and the `OllamaProtocolError` arm of the streaming generator |
| `tests/test_integration.py` (new) | One end-to-end user-journey test (`create → list → load → send → stream → regenerate → rename → delete`) plus one focused test for the delete-while-viewing → `HX-Location` branch |
| `tests/README.md` (new) | Documents the mock-only Ollama strategy (explicit decision, not accident), the layout of test files by layer, how to run coverage, and the future path to integration tests if needed |

## Decisions (and why)

- **Add `pytest-cov` as a direct dep.** Coverage reports surface
  what unit tests are missing in objective terms. Cheap (~1MB
  dependency) and saves the time of eyeballing branches.
- **Mock-only Ollama strategy.** Every test uses
  `httpx.MockTransport`; no real Ollama is contacted. Documented
  in `tests/README.md` so a future contributor knows it's
  deliberate. The Ollama API surface we use is small (`GET /api/tags`,
  streaming `POST /api/chat`) and stable, so the mock surface
  covers the contract adequately.
- **Document the migration path to integration tests** rather
  than build them now. The README explains exactly how to add a
  `pytest -m integration` marker if/when real-Ollama testing
  becomes useful. Keeps the door open without paying the cost
  today.
- **Don't chase 100% coverage.** `app/dependencies.get_ollama_client`'s
  body is structurally unreachable because every test overrides
  it via `app.dependency_overrides`. Forcing the line to 100%
  would either defeat the mock strategy or add a `# pragma`
  comment for one line. Neither felt worth it. Document the 99%
  ceiling instead.
- **One end-to-end test, not many.** A single comprehensive
  journey through every major route catches "the wiring is
  broken" regressions; multiple narrow integration tests
  duplicate what the per-route unit tests already cover.

## What worked

- **Coverage report immediately found real gaps.** Without
  `pytest-cov`, I would have written tests for things I *thought*
  were missing. The report instead named exact lines: five
  branches in `routes.py` (the 404 paths on stream/regenerate
  endpoints, the 400 path on regenerate-stream when there's no
  assistant, and the `OllamaProtocolError` arm of
  `_stream_assistant_reply`). Writing tests for those exact
  branches took about ten minutes.
- **Scripted-by-call-count Ollama mock for the integration test.**
  The fixture's mock handler returns different NDJSON streams for
  the first and second `/api/chat` calls. That's how the journey
  can verify regenerate actually *replaces* the assistant text
  with new content (not just re-emits the same text). A
  static-response mock wouldn't catch the regenerate semantics.
- **The integration test as a contract pin for the whole flow.**
  The earlier per-route tests verify "this endpoint behaves
  correctly in isolation." The integration test verifies "all the
  endpoints wire together correctly across a session" — a
  qualitatively different guarantee.
- **`tests/README.md` makes the test layout discoverable.** A new
  contributor (or future-me) can find the right file to add a
  test in 30 seconds. The Ollama mocking decision is recorded
  with rationale, so the question "should I mock or use real
  Ollama here?" doesn't have to be re-litigated.
- **Speed.** 102 tests in 1.5–2 seconds. Hermetic, fast, no real
  Ollama or network. The "no real Ollama" decision pays for
  itself every time the suite runs.

## What was tricky / went less well

- **`ResourceWarning: unclosed database` spam from one test.** The
  `test_base_css_hides_regenerate_except_on_last_assistant` test
  triggers ~91 warnings about unclosed SQLite connections from
  somewhere in the test infrastructure. Tests still pass; the
  warnings don't fail anything. Probably stems from the shared
  Phase 6 connection lifecycle interacting with the test
  fixture's tempfile DB. Not load-bearing enough to fix in this
  phase — flagged for if someone wants to chase it later.
- **The 99% ceiling is awkward to explain.** Every coverage tool
  presents missing lines as "untested" with no semantic context.
  `get_ollama_client`'s body is intentionally unreachable in
  tests; explaining that requires the README. A more
  sophisticated alternative would be to put `# pragma: no cover`
  on the line, but that has its own downsides (someone removes
  the override pattern and the pragma silently swallows real
  coverage loss).
- **One small fixture duplication.** `tests/test_integration.py`'s
  `integration_client` fixture is structurally similar to
  `tests/test_routes.py`'s `make_client` fixture (both build a
  `TestClient` with tempfile DB + mocked Ollama). Could be DRY'd
  to a shared `conftest.py`. Deferred — the integration fixture
  has different needs (call-counting mock) and refactoring for
  one shared fixture isn't load-bearing.

## Open issues / follow-ups

- **No CI.** Tests run locally only. If this project ever grows
  past one developer, a GitHub Actions workflow that runs `pytest
  --cov` on push would be a 20-line addition. Defer until needed.
- **No coverage threshold enforcement.** We *report* coverage but
  don't *enforce* a minimum. A `--cov-fail-under=95` would fail
  the suite on regressions. Worth considering if the test
  discipline ever slips.
- **The dependency-override pattern's blind spot.** Every test
  overrides `get_ollama_client` via `app.dependency_overrides`.
  This is correct for hermetic testing but means the body of
  `get_ollama_client` is never exercised in the test suite, and
  any bug in *how* the override interacts with the rest of the
  app would surface only at runtime. Not a hypothetical concern,
  just a known limitation of the pattern.
- **The integration test mocks streaming as one buffered
  response.** Real-world streaming has different timing
  (server sends chunks over seconds, browser receives them
  progressively). The mock's "whole stream available immediately"
  shape doesn't catch timing-sensitive bugs. The trade-off was
  flagged in earlier retros and accepted; the mock surface still
  catches contract / shape bugs.

## Notes for future phases (if any)

- **Run coverage before "rounding out tests."** Spending an hour
  writing tests for things you assume are missing is much less
  efficient than five minutes of `pytest --cov --cov-report=term-missing`.
  This phase wrote five tests for five specific named lines; that
  efficiency is hard to match by reading code.
- **An end-to-end integration test is the highest-leverage test
  to write last.** Per-route unit tests catch many bugs; one
  integration test catches the "two correct routes don't compose
  correctly" class of bugs that's invisible to unit tests.
- **Document strategy decisions in the test directory itself,
  not in PLAN.md or a retro.** `tests/README.md` is discoverable
  from where contributors naturally look (the test directory).
  PLAN.md describes phases; retros describe history; the README
  describes the current strategy. Three different audiences,
  three different documents.
- **Phase 10 was small.** Sometimes the right answer is "the
  preceding phases left us in better shape than the plan assumed."
  Coverage was already at 96% entering the phase — there wasn't
  much to round out. Worth noting that organic test discipline
  across earlier phases (testing each phase's work as it landed)
  pays off here.

## Wrap-up — all PLAN.md phases complete

| Phase | Title | Retro |
|---|---|---|
| 0 | Discussion / clarification | (no retro — pre-build) |
| 1 | Package requirements | (no retro — small) |
| 2 | Database | (no retro) |
| 3 | Database connection | (no retro) |
| 4 | Models and queries | (no retro) |
| 5 | Ollama client | (no retro) |
| 6 | FastAPI routers | `docs/retros/phase6-fastapi-routers.md` |
| 7 | Frontend (HTMX + Jinja) | `docs/retros/phase7-frontend.md` |
| 8 | Frontend polish | `docs/retros/phase8-frontend-polish.md` |
| 9 | Frontend bug fixes | `docs/retros/phase9-frontend-fixes.md` |
| 10 | Full test suite | this file |

App is a usable local-only Ollama chat. Possible future phases (none
in PLAN.md):

- Polish: dark mode toggle, model switcher mid-conversation,
  auto-generated chat titles, scroll-to-bottom UX refinements,
  keyboard shortcuts beyond Enter-to-send.
- Infrastructure: GitHub Actions CI, coverage threshold,
  pre-commit hooks, dependency update automation.
- Features: search across past chats, export, RAG, image input,
  tool calling — all explicitly listed as v1 non-goals in
  PLAN.md.

Whatever comes next, the layered architecture (config / db /
connection / queries / ollama / routes / templates) and the test
discipline (per-layer unit tests + one integration journey) hold
up as a clean base to build on.
