"""Per-turn response duration: the format helper + its render in a bubble.

The duration is captured server-side in the generation producer, persisted on
the assistant `messages` row (``duration_ms``), and rendered under the token
counts as a human-readable string in the same quiet monospaced style. These
tests pin the formatting and the template contract (the ``message__duration``
element + its value), not the surrounding DOM shape.
"""

from datetime import datetime, timezone

import pytest

from app.format import format_duration_ms
from app.queries._models import Message
from app.templates import templates


@pytest.mark.parametrize(
    "duration_ms, expected",
    [
        (0, "0s"),
        (999, "1s"),  # rounds to the nearest second
        (1_000, "1s"),
        (32_000, "32s"),
        (32_400, "32s"),
        (59_000, "59s"),  # just under a minute → no "min" segment
        (59_500, "1min 0s"),  # rounds up to 60s, which rolls into a minute
        (60_000, "1min 0s"),
        (632_000, "10min 32s"),  # the example from the feature request
        (4_500_000, "75min 0s"),  # minutes are the largest unit, no hours
    ],
)
def test_format_duration_ms(duration_ms: int, expected: str) -> None:
    """ms → compact "Mmin Ss" / "Ss" string, rounded to whole seconds."""
    assert format_duration_ms(duration_ms) == expected


def _assistant_message(*, duration_ms: int | None) -> Message:
    """A minimal assistant Message for rendering ``_message.html``."""
    return Message(
        id=1,
        conversation_id=7,
        role="assistant",
        content="Hello",
        created_at=datetime.now(timezone.utc),
        duration_ms=duration_ms,
    )


def test_bubble_renders_duration_when_set() -> None:
    """A duration_ms-bearing assistant bubble shows the formatted duration."""
    html = templates.get_template("_message.html").render(
        message=_assistant_message(duration_ms=632_000), swap_target=None
    )
    assert 'class="message__duration"' in html
    assert "10min 32s" in html


def test_bubble_omits_duration_when_none() -> None:
    """No duration recorded → no duration element at all."""
    html = templates.get_template("_message.html").render(
        message=_assistant_message(duration_ms=None), swap_target=None
    )
    assert "message__duration" not in html
