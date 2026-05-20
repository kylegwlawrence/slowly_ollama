"""Phase 13: multi-agent research → review → generation loop.

Three discrete agents, each invoked via Ollama with its own system
prompt and tool scope:

- **research** sees full chat history + the current user message and
  has access to every registered tool. It runs a tool-calling
  inner loop until it stops calling tools; the last text response
  is captured as the iteration's "findings".
- **review** sees only the original user message and the latest
  findings, with two custom tools (mark_passed,
  request_more_research) that encode its verdict.
- **generation** sees only the original user message and the final
  findings, no tools. It writes the final assistant response.

The orchestrator (`loop._run_agentic_generation`) wires them
together with a 3-iteration cap. See
`docs/plans/phase13-agentic-loop.md` for the architecture diagram.
"""

from app.agents.prompts import (
    GENERATION_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
)

# NOTE: `app.agents.verdict_tools` is deliberately NOT re-exported
# from this package's __init__. The commit order in
# docs/plans/phase13-agentic-loop.md lets 13b (this module + prompts)
# ship before 13c (verdict_tools); pulling verdict_tools into the
# package surface would couple the two and force them into a single
# commit. Call sites import directly:
#
#     from app.agents.verdict_tools import REVIEW_TOOL_SPECS, parse_verdict

__all__ = [
    "GENERATION_SYSTEM_PROMPT",
    "RESEARCH_SYSTEM_PROMPT",
    "REVIEW_SYSTEM_PROMPT",
]
