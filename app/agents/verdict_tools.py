"""Phase 13: review-agent verdict tools.

The review agent expresses its pass/fail verdict by calling one of two
"tools" we advertise to it. They aren't real tools — they have no
body. They're a structured-output mechanism on top of Ollama's
tool-call protocol: the model is already trained to call tools
reliably, so encoding the verdict as a tool call is more robust
than asking for JSON in free-form text.

These specs are deliberately NOT registered in `app.tools.TOOLS` —
that registry is what the research agent reads via
`tool_specs_for_ollama()`. Keeping these specs separate ensures the
research agent never sees them and can't short-circuit the loop by
"marking" its own findings.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class VerdictDecision:
    """Decoded review verdict.

    Attributes:
        verdict: ``"passed"`` when the review agent called
            ``mark_passed``; ``"failed"`` when it called
            ``request_more_research`` (or when neither tool was
            called — see :func:`parse_verdict`).
        message: The reason / feedback string the model passed as
            its argument. May be empty if the model called the tool
            without an argument.
    """

    verdict: Literal["passed", "failed"]
    message: str


# Ollama tool-spec shape; mirrors what `tool_specs_for_ollama()`
# produces for the @tool-decorated tools but built by hand because
# these tools have no Python body to inspect. Deliberately no
# `additionalProperties: false` — it would diverge from the existing
# tool-spec convention and `parse_verdict` already ignores
# unrecognized arguments, so the stricter constraint isn't
# load-bearing.
REVIEW_TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "mark_passed",
            "description": (
                "Call this when the research findings are sufficient"
                " to answer the user's question. Pass a brief reason."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": (
                            "Short explanation of why the findings"
                            " are sufficient."
                        ),
                    },
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_more_research",
            "description": (
                "Call this when the research findings are NOT yet"
                " sufficient. Pass specific, actionable feedback on"
                " what's missing or wrong."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "feedback": {
                        "type": "string",
                        "description": (
                            "Specific feedback for the research"
                            " agent's next pass. Avoid generic"
                            " 'do more research' notes."
                        ),
                    },
                },
                "required": ["feedback"],
            },
        },
    },
]


def _safe_arg(call: dict, key: str) -> str:
    """Extract a string argument from a tool call, tolerating malformed shapes.

    Returns ``""`` when ``arguments`` is missing, ``None``, or not a
    dict (e.g., a misbehaving model emits ``"arguments": ["reason",
    "ok"]``). Otherwise returns ``str(value)`` for the named key
    (defaults to ``""``). Coercion via ``str()`` guards against the
    rare case where the model emits a non-string value for an
    argument the spec declares as string-typed.
    """
    args = call.get("arguments")
    if not isinstance(args, dict):
        return ""
    return str(args.get(key, ""))


def parse_verdict(tool_calls: list[dict]) -> VerdictDecision:
    """Map a list of tool_calls from ``maybe_tool_call`` to a verdict.

    Rules (defensive — the model may misbehave):
    1. If any call is ``mark_passed``, treat as ``"passed"``; use its
       ``reason`` arg as the message.
    2. Else if any call is ``request_more_research``, treat as
       ``"failed"``; use its ``feedback`` arg as the message.
    3. Else (no calls, or unrecognized calls): treat as ``"failed"``
       with a fallback message asking for retry. This keeps the loop
       moving when the model ignores instructions.

    Args:
        tool_calls: Output of ``ollama.maybe_tool_call``'s first
            tuple element — a list of ``{"name", "arguments"}`` dicts.

    Returns:
        A :class:`VerdictDecision`.
    """
    for call in tool_calls:
        if call.get("name") == "mark_passed":
            return VerdictDecision(
                verdict="passed",
                message=_safe_arg(call, "reason"),
            )
    for call in tool_calls:
        if call.get("name") == "request_more_research":
            return VerdictDecision(
                verdict="failed",
                message=_safe_arg(call, "feedback"),
            )
    return VerdictDecision(
        verdict="failed",
        message=(
            "Review agent did not call a verdict tool. Continue"
            " researching."
        ),
    )
