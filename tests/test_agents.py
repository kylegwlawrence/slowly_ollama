"""Phase 13: smoke tests for the agentic-loop module."""

from app import agents


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
