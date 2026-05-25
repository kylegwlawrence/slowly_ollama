"""Shared pytest fixtures auto-discovered by every test in this directory.

Pytest applies fixtures defined here BEFORE per-file fixtures, so the
module-state reset below runs around every test in the suite without
each test file having to opt in.

The reset is narrow on purpose: only the global mutable state in
``app/`` that production code reads at module level. Per-area helpers
(``make_client`` in test_routes, ``_setup_chat`` in test_generation,
the ``_FakeClient`` wrapper in test_tools) stay in their respective
files — they're test-area conveniences, not shared infrastructure.

If phase 13 (or any later phase) introduces another module-level
cache or registry that needs per-test isolation, add it here in one
place. The list is short by design: long-lived global state should be
the exception in this codebase, and any addition warrants a deliberate
update here.
"""

from collections.abc import Iterator

import pytest

from app import generation, ollama, rag_health
from app.tools import TOOLS
from app.tools import builtins as _builtins  # noqa: F401 — registers file tools

_FILE_TOOL_NAMES = ("read_file", "write_file")


@pytest.fixture(autouse=True)
def _isolate_module_state() -> Iterator[None]:
    """Snapshot/restore process-global state around every test.

    Three globals matter today:

    - ``generation.live_generations`` — dict keyed by conversation_id.
      Retains DONE generations until evicted by a new generation for
      the same conv (so slow-reload replays still work). Between tests
      a leftover entry would interfere with same-id assertions.
    - ``ollama._capability_cache`` — memoized /api/show probe results.
      A test that populates the cache with hand-rolled names would
      leak into a later test that expects to drive its own mock
      transport.
    - ``TOOLS["query_rag"]`` — ``refresh_query_rag_registration`` can
      pop ``query_rag`` from the registry as a side effect (when 0
      servers are configured). Without isolation, a downstream test
      that expects the tool present would see a missing entry.

    Snapshot-and-restore (rather than just clear-on-teardown) means a
    test that *intentionally* pre-populates one of these globals can
    do so without the teardown wiping a separate fixture's setup.
    """
    saved_gens = dict(generation.live_generations)
    generation.live_generations.clear()
    ollama.reset_capability_cache()
    saved_query_rag = TOOLS.get("query_rag")
    # The file tools register at import (via the @tool decorator) but
    # FILE_TOOL_ROOT is unset under test, so production would have popped
    # them at lifespan startup. Snapshot, then pop, so the default test
    # tool universe is {current_time, query_rag} — matching an
    # unconfigured install. File-tool tests opt back in by setting
    # FILE_TOOL_ROOT and calling refresh_file_tools_registration().
    saved_file_tools = {n: TOOLS.get(n) for n in _FILE_TOOL_NAMES}
    for name in _FILE_TOOL_NAMES:
        TOOLS.pop(name, None)
    # Phase 19: clear the RAG health cache between tests so a probe in
    # one test doesn't leak a stale entry into the next.
    rag_health.clear_cache()
    yield
    generation.live_generations.clear()
    generation.live_generations.update(saved_gens)
    ollama.reset_capability_cache()
    rag_health.clear_cache()
    if saved_query_rag is not None:
        TOOLS["query_rag"] = saved_query_rag
    else:
        TOOLS.pop("query_rag", None)
    for name, spec in saved_file_tools.items():
        if spec is not None:
            TOOLS[name] = spec
        else:
            TOOLS.pop(name, None)


@pytest.fixture(autouse=True)
def _no_rag_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 19: stub ``rag_health.get_health_map`` so chat-panel renders
    don't fire real network probes during tests.

    Returns an empty dict, which the sidebar partial reads as "unknown
    status" for every server — chips render in their plain on/off state,
    no red. Tests that need to exercise unavailable / healthy states
    should override this by re-patching ``get_health_map`` after this
    fixture runs (pytest applies same-priority autouse fixtures in
    definition order; per-test monkeypatching wins on the last setattr).
    """
    async def _noop_map(servers, *, force=False):
        return {}

    monkeypatch.setattr(rag_health, "get_health_map", _noop_map)
