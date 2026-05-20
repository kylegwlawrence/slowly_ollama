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


@pytest.fixture(autouse=True)
def _isolate_module_state() -> Iterator[None]:
    """Snapshot/restore process-global state around every test.

    Two globals matter today:

    - ``generation.live_generations`` — dict keyed by conversation_id.
      Retains DONE generations until evicted by a new generation for
      the same conv (so slow-reload replays still work). Between tests
      a leftover entry would interfere with same-id assertions.
    - ``ollama._capability_cache`` — memoized /api/show probe results.
      A test that populates the cache with hand-rolled names would
      leak into a later test that expects to drive its own mock
      transport.

    Snapshot-and-restore (rather than just clear-on-teardown) means a
    test that *intentionally* pre-populates one of these globals can
    do so without the teardown wiping a separate fixture's setup.
    """
    saved_gens = dict(generation.live_generations)
    generation.live_generations.clear()
    ollama.reset_capability_cache()
    yield
    generation.live_generations.clear()
    generation.live_generations.update(saved_gens)
    ollama.reset_capability_cache()
