# Tests

## Running

```bash
source .venv/bin/activate
pytest                       # all tests
pytest -v                    # verbose
pytest --cov=app --cov=main --cov-report=term-missing   # coverage
pytest tests/test_routes.py  # one file
pytest -k rename             # tests matching a name
```

## Layout

| File | Layer |
|---|---|
| `test_dependencies.py` | Pinned third-party packages are importable |
| `test_db.py` | SQLite schema + `initialize_database()` |
| `test_connection.py` | `open_connection()` pragmas + thread-safety |
| `test_config.py` | `.env`-backed accessors |
| `test_queries.py` | CRUD + cascade + regenerate semantics |
| `test_ollama.py` | The Ollama HTTP client (success / unavailable / protocol error) |
| `test_routes.py` | Every FastAPI route + HTMX wiring |
| `test_integration.py` | One end-to-end user journey through `TestClient` |

## Ollama mocking strategy

**Mock-only via `httpx.MockTransport`.** Every test that touches the
Ollama client passes an `httpx.AsyncClient(transport=httpx.MockTransport(handler))`
so each test fully scripts the responses it wants. No test spawns a
real Ollama server.

This is a deliberate choice, not an accident:

- **Fast.** Whole suite (102 tests) runs in under 2 seconds.
- **Hermetic.** No external state, no flake from a busy local
  Ollama, no need to pull models before running tests.
- **Sufficient for the trust boundary.** The Ollama client is small
  (one `GET /api/tags`, one streaming `POST /api/chat`). Its
  contract with the real server is narrow enough that the mock
  transport exercises the same code paths as a real server would.

The trade-off: if Ollama ever changes its API (renames `/api/chat`,
changes the NDJSON shape, adds a required header) we wouldn't catch
it from the test suite alone — only when running the app for real.
The risk is mitigated by the fact that Ollama's API has been stable
for a long time and the surface we use is tiny.

### Adding integration tests later (if needed)

If real-Ollama testing becomes valuable, the lowest-overhead path:

1. Add `pytest-asyncio` markers: `@pytest.mark.integration`.
2. Configure `pyproject.toml` / `pytest.ini`:

   ```ini
   [pytest]
   markers =
       integration: tests that hit a real Ollama (requires Ollama running on localhost:11434)
   addopts = -m "not integration"
   ```
3. Write integration tests in `tests/test_ollama_integration.py` that
   skip if Ollama isn't reachable.
4. Run with `pytest -m integration` (or `pytest -m ""` to bypass the
   default deselection).

Not done now because the mock surface is already covering the
contract, and "Ollama must be installed and running" is a
nontrivial setup burden for CI / new contributors.

## Coverage

Phase 10 added `pytest-cov`. Current coverage is **99%** across
`app/` and `main.py` (the only miss is `get_ollama_client`'s body,
which is unreachable in tests because every test overrides it via
`app.dependency_overrides`). Re-run with `--cov-report=term-missing`
to see the picture; any drop below ~95% deserves a look.
