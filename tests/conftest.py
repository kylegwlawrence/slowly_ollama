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

from app import generation, ollama
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
    # web_search registers at import like the file tools, but is gated on
    # SEARXNG_URL (unset under test), so production would have popped it at
    # startup. Snapshot, then pop, keeping the default test tool universe
    # unconfigured; web_search tests opt back in by setting SEARXNG_URL and
    # calling refresh_web_search_registration().
    saved_web_search = TOOLS.get("web_search")
    TOOLS.pop("web_search", None)
    yield
    generation.live_generations.clear()
    generation.live_generations.update(saved_gens)
    ollama.reset_capability_cache()
    if saved_query_rag is not None:
        TOOLS["query_rag"] = saved_query_rag
    else:
        TOOLS.pop("query_rag", None)
    for name, spec in saved_file_tools.items():
        if spec is not None:
            TOOLS[name] = spec
        else:
            TOOLS.pop(name, None)
    if saved_web_search is not None:
        TOOLS["web_search"] = saved_web_search
    else:
        TOOLS.pop("web_search", None)
