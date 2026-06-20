"""Tests for the mobile-friendly responsive UI pass.

The off-canvas sidebar drawer, the chat-header gear-collapse, and the
viewport breakpoint are CSS/JS behaviours the test client can't execute
(no JS, no CSS cascade). So these tests pin the *contracts* the JS + CSS
depend on — the markup hooks (hamburger toggle, scrim, controls wrapper +
gear) and the presence of the responsive ``@media`` block in the
stylesheet — rather than the rendered layout itself. The real layout is
verified in a browser per CLAUDE.md.
"""

from pathlib import Path

# Re-use the shared fixtures + helpers from test_routes (same pattern as
# test_routes_compact / test_routes_backup_status). ``_default_tool_capable``
# is autouse, so importing it stubs ``model_supports_tools`` for this module
# too — the chat-panel GET would otherwise probe the (unreachable) mock.
from tests.test_routes import (
    ClientFactory,
    _create_chat_and_get_id,
    _ollama_unreachable,
    make_client,  # noqa: F401 — fixture re-export
    _default_tool_capable,  # noqa: F401 — fixture re-export
)

_STYLE_CSS = Path(__file__).resolve().parent.parent / "static" / "style.css"


def test_layout_includes_drawer_affordances(make_client: ClientFactory) -> None:
    """Every page renders the mobile hamburger toggle + scrim inside .layout.

    They live in index.html as siblings of #main so they persist across
    HTMX innerHTML swaps; app.js's delegated handlers key off these class
    hooks, and the toggle's aria-controls points at the sidebar's id.
    """
    with make_client(_ollama_unreachable) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "<!DOCTYPE html>" in response.text
    assert 'class="layout__menu-toggle"' in response.text
    assert 'class="layout__scrim"' in response.text
    assert 'aria-controls="sidebar"' in response.text
    assert 'id="sidebar"' in response.text


def test_chat_panel_collapses_controls_behind_gear(
    make_client: ClientFactory,
) -> None:
    """The chat header wraps its controls in a togglable panel + gear button.

    The chat name and model badge stay OUTSIDE the wrapper (always visible
    on mobile); everything else moves inside #chat-controls-{id}, which the
    gear toggles open on narrow viewports.
    """
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "Topic")
        response = client.get(f"/chats/{chat_id}")

    assert response.status_code == 200
    assert 'class="chat-panel__controls-toggle"' in response.text
    assert f'id="chat-controls-{chat_id}"' in response.text
    # The wrapper div (closing quote distinguishes it from the -toggle button).
    assert 'class="chat-panel__controls"' in response.text
    # Name + model badge remain outside the collapsible wrapper.
    assert 'class="chat-panel__name"' in response.text
    assert f'id="agent-indicator-{chat_id}"' in response.text


def test_stylesheet_ships_mobile_breakpoint() -> None:
    """style.css carries the responsive layer.

    CSS isn't otherwise unit-tested, so this pins two contracts: the
    breakpoint exists, and the desktop-neutral flattening that keeps the
    header byte-for-byte unchanged on desktop is in place.
    """
    css = _STYLE_CSS.read_text(encoding="utf-8")
    assert "@media (max-width: 768px)" in css
    assert ".chat-panel__controls { display: contents; }" in css
