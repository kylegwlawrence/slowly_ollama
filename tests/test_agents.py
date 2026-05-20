"""Phase 13: smoke tests for the agentic-loop module."""

from app import agents
from app.agents import verdict_tools


def test_prompts_are_non_empty_strings() -> None:
    """Each prompt is a multi-line string with non-trivial content."""
    for prompt in (
        agents.RESEARCH_SYSTEM_PROMPT,
        agents.REVIEW_SYSTEM_PROMPT,
        agents.GENERATION_SYSTEM_PROMPT,
    ):
        assert isinstance(prompt, str)
        assert len(prompt.strip()) > 100  # not a placeholder


def test_review_prompt_names_both_verdict_tools() -> None:
    """The review prompt must mention both tool names verbatim — the
    model needs to know what to call. Catches accidental renames in
    one place but not the other."""
    p = agents.REVIEW_SYSTEM_PROMPT
    assert "mark_passed" in p
    assert "request_more_research" in p


# ---------------------------------------------------------------------------
# verdict_tools — REVIEW_TOOL_SPECS + parse_verdict (phase 13c)
# ---------------------------------------------------------------------------


def test_review_tool_specs_have_correct_names() -> None:
    """Both verdict tools are present with the names the prompt references."""
    names = {
        spec["function"]["name"] for spec in verdict_tools.REVIEW_TOOL_SPECS
    }
    assert names == {"mark_passed", "request_more_research"}


def test_review_tool_specs_have_required_args() -> None:
    """Each spec marks its arg as required so a misbehaving model
    that calls mark_passed() with no reason gets a parse error from
    Ollama rather than producing an empty-message verdict."""
    by_name = {
        spec["function"]["name"]: spec["function"] for spec in verdict_tools.REVIEW_TOOL_SPECS
    }
    assert by_name["mark_passed"]["parameters"]["required"] == ["reason"]
    assert by_name["request_more_research"]["parameters"]["required"] == ["feedback"]


def test_review_tool_specs_not_in_global_registry() -> None:
    """The whole point of the separate registry: the research agent
    must NOT see the verdict tools via `tool_specs_for_ollama()`.
    Pin: if a future refactor accidentally @tool-decorates these,
    this assertion fails loudly."""
    from app.tools import TOOLS

    assert "mark_passed" not in TOOLS
    assert "request_more_research" not in TOOLS


def test_parse_verdict_passed() -> None:
    """mark_passed → VerdictDecision(verdict='passed', message=reason)."""
    calls = [
        {"name": "mark_passed", "arguments": {"reason": "looks good"}}
    ]
    d = verdict_tools.parse_verdict(calls)
    assert d.verdict == "passed"
    assert d.message == "looks good"


def test_parse_verdict_failed() -> None:
    """request_more_research → VerdictDecision(verdict='failed', ...)."""
    calls = [
        {
            "name": "request_more_research",
            "arguments": {"feedback": "missing source citations"},
        }
    ]
    d = verdict_tools.parse_verdict(calls)
    assert d.verdict == "failed"
    assert d.message == "missing source citations"


def test_parse_verdict_passed_wins_over_failed() -> None:
    """If the model calls both, mark_passed takes precedence."""
    calls = [
        {"name": "request_more_research", "arguments": {"feedback": "x"}},
        {"name": "mark_passed", "arguments": {"reason": "y"}},
    ]
    assert verdict_tools.parse_verdict(calls).verdict == "passed"


def test_parse_verdict_no_verdict_tools_falls_through() -> None:
    """No recognized tool call → treat as failed with a default message."""
    d = verdict_tools.parse_verdict([])
    assert d.verdict == "failed"
    assert "did not call" in d.message
    d2 = verdict_tools.parse_verdict(
        [{"name": "random_tool", "arguments": {}}]
    )
    assert d2.verdict == "failed"


def test_parse_verdict_missing_arguments_key_defaults_to_empty_message() -> None:
    """Defensive: the model called the verdict tool but Ollama
    dropped the arguments key (or the model emitted no args). Verdict
    is still recognized; message is the empty string. Avoids a
    KeyError that would surface to the user as a stack trace."""
    d = verdict_tools.parse_verdict([{"name": "mark_passed"}])
    assert d.verdict == "passed"
    assert d.message == ""
    d2 = verdict_tools.parse_verdict([{"name": "request_more_research"}])
    assert d2.verdict == "failed"
    assert d2.message == ""


def test_parse_verdict_non_string_argument_coerced() -> None:
    """If the model emits a non-string argument (rare but possible
    with poorly-tuned models), the message is still stringified rather
    than propagating a wrong-type value into persisted JSON."""
    d = verdict_tools.parse_verdict(
        [{"name": "mark_passed", "arguments": {"reason": 42}}]
    )
    assert d.verdict == "passed"
    assert d.message == "42"


def test_parse_verdict_non_dict_arguments_safe() -> None:
    """A misbehaving model that emits `arguments` as a list (or any
    non-dict) must NOT crash the loop. The verdict is still
    recognized; message defaults to empty. Without this guard the
    inline `.get("reason", "")` would AttributeError on a list."""
    # arguments is a list — the kind of garbage a poorly-tuned model
    # might emit if it interpreted the schema sloppily.
    d = verdict_tools.parse_verdict(
        [{"name": "mark_passed", "arguments": ["reason", "looks good"]}]
    )
    assert d.verdict == "passed"
    assert d.message == ""
    # arguments is a string.
    d2 = verdict_tools.parse_verdict(
        [{"name": "request_more_research", "arguments": "missing source"}]
    )
    assert d2.verdict == "failed"
    assert d2.message == ""
    # arguments is an int.
    d3 = verdict_tools.parse_verdict(
        [{"name": "mark_passed", "arguments": 42}]
    )
    assert d3.verdict == "passed"
    assert d3.message == ""
