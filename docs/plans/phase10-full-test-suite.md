# Phase 10 — Full test suite

## Context

Tests have grown organically over Phases 1–9: 94 tests across seven
files covering dependencies, schema, connection, config, queries,
Ollama client, and routes. Phase 10 rounds out coverage with a few
targeted additions and pins the Ollama mocking strategy as an
explicit decision rather than emergent practice.

## Decisions

- **Add `pytest-cov`.** Coverage reports tell us which lines /
  branches aren't exercised. One-line dependency add; one flag at
  the test invocation.
- **Mock-only Ollama strategy.** Keep `httpx.MockTransport` for
  every test. No real-Ollama integration tests yet. Fast, hermetic,
  no external state. Documented in a small note in `tests/` so
  future contributors know it's deliberate, not accidental.

## Plan

1. **Commit 0 — materialize plan** in `docs/plans/`.
2. **Commit 1 — `pytest-cov`** added to `requirements.txt` and the
   importability check in `tests/test_dependencies.py`. Install in
   the venv.
3. **Coverage baseline.** Run
   `pytest --cov=app --cov-report=term-missing` once to see actual
   gaps. Pick the highest-value missing lines/branches.
4. **Commit 2 — gap fixes.** Add tests for whatever coverage shows
   is meaningfully untested. Likely candidates: `main.py` lifespan
   (startup/shutdown of shared resources), branches in routes that
   only fire on specific error paths.
5. **Commit 3 — one end-to-end integration test** in a new
   `tests/test_integration.py`. Walks the full user journey via
   `TestClient`: create chat → load panel → send message → drive
   the SSE stream → regenerate → delete. Catches integration gaps
   that per-route unit tests can't see.
6. **Commit 4 — document the Ollama mocking decision.** A short
   `tests/README.md` (or a top-of-file note in `tests/test_ollama.py`)
   explaining the mock-only choice and the future option of adding
   `@pytest.mark.integration` if real-Ollama testing becomes useful.

## Critical files

- `requirements.txt`, `tests/test_dependencies.py` — `pytest-cov`
- `tests/test_integration.py` (new) — end-to-end journey
- `tests/test_lifespan.py` (new, if main.py lifespan is uncovered)
- `tests/README.md` (new) — Ollama mocking note
- `docs/plans/PLAN.md` — mark Phase 10 done (or leave for the retro)

## Verification

```bash
source .venv/bin/activate
pytest --cov=app --cov-report=term-missing
# Expect: 100+ tests passing; coverage report shows the new
# tests catching previously uncovered lines.
```

## What's NOT in this phase

- No real-Ollama integration tests (deferred; documented).
- No new app features.
- No CI/lint setup (out of scope; could be a Phase 11 if wanted).
- No coverage thresholds (we report, not enforce, for now).
