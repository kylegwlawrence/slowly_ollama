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

# Phase 13f: the iteration cap is shared by the live orchestrator
# (`app.agents.loop`) and the historic-render path (`app.render`).
# Both modules need to agree on "did this turn hit the cap?" — the
# orchestrator decides when to render the max-iterations badge live,
# and historic render derives the same flag from the persisted
# iteration count.
#
# Lives in this package's __init__ rather than in `loop.py` because
# `loop.py` already imports `app.render`; if render imported from
# loop the import graph would cycle. The __init__ is dependency-free
# (only pulls in `prompts`, which is pure constants) and is the
# natural shared surface for the agents package.
AGENTIC_ITERATION_CAP = 3

# NOTE: `app.agents.verdict_tools` is deliberately NOT re-exported
# from this package's __init__. The commit order in
# docs/plans/phase13-agentic-loop.md lets 13b (this module + prompts)
# ship before 13c (verdict_tools); pulling verdict_tools into the
# package surface would couple the two and force them into a single
# commit. Call sites import directly:
#
#     from app.agents.verdict_tools import REVIEW_TOOL_SPECS, parse_verdict

__all__ = [
    "AGENTIC_ITERATION_CAP",
    "GENERATION_SYSTEM_PROMPT",
    "RESEARCH_SYSTEM_PROMPT",
    "REVIEW_SYSTEM_PROMPT",
]
