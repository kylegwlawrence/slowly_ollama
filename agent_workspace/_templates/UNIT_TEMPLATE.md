# Unit N: {{Title}}

> **Filling this template?** The "Agent brief" section below is reference material for you. When you `write_file` the filled outline, **start the output at the `## Metadata` heading** and omit everything above it.

## Agent brief — read before filling

You are filling this template for one **self-study learner**. Not a classroom.

**Workflow.** Multi-turn fill — do one section per turn. Suggested order: Metadata → Overview → Learning Outcomes → Key Concepts → Weekly Sequence → Glossary → Tools & Resources → Real-World Connections → Unit Self-Assessment → Going Deeper.

**Audience.** Address the reader as "you". Do **not** write: "students" / "the class", Differentiation, Pedagogical Approach, Materials Needed, Science Fair extensions, or classroom hooks. The unit file is a **thin overview** — daily content lives in the week files, not here. If you find yourself describing a specific session activity, push it down into the week file and leave only a one-line summary in the unit.

**Action verbs (mandatory).** Every Learning Outcome must start with one of:

**Derive, Apply, Compute, Compare, Explain, Predict, Model, Simulate, Critique.**

Never write "Understand X" — it is not checkable.

**Sources & URLs.** Use tools (`query_rag`, `fetch_github_file`, `read_file`) to ground every citation. When a tool confirms a source, write `[verified] URL`. When you cannot confirm, write a placeholder — `{{find a sim for: TOPIC}}` or `{{find source: TOPIC}}`. **Never invent a domain, paper title, or textbook chapter number.** Bias to canonical sources: PhET (phet.colorado.edu), IBM Quantum, arXiv (arxiv.org), Wolfram Alpha, MIT OCW (ocw.mit.edu), NIST, Qiskit (qiskit.org), Python library docs.

**Weekly Sequence.** Use `read_file` on each child week file before filling its row — the Focus and Key Skills cells should match the week file's actual H1 title and Learning Outcomes, not your guess.

**Format.** H1 once (the file title), H2 for sections, H3 for subsections. No H4 or deeper. The Weekly Sequence is the only table — everything else is bullets.

**Targets — write exactly these counts:**

- 6 Learning Outcomes
- 8 Key Concepts
- 12 Glossary terms
- 4 Real-World Connections
- 3 Going Deeper items

---

## Metadata

- **Duration**: N weeks
- **Estimated effort**: ~N hours/week
- **Prerequisites**: {{prior units (e.g., Unit M); skills required (e.g., calculus, linear algebra); concepts assumed}}
- **Difficulty tier**: {{intro | intermediate | advanced | frontier}}

## Overview

{{2–3 paragraphs. Why does this unit exist? What learning arc does it trace, from start to finish? Where does it fit in the broader 33-week curriculum, and what questions will you be able to answer after finishing it?}}

## Learning Outcomes

By the end of this unit you should be able to:

- {{Action verb + object + qualifier (e.g., "Derive the kinematic equations for constant acceleration from first principles")}}
- {{…}}
- {{6 outcomes total. Use Derive, Apply, Compute, Compare, Explain, Predict, Model, Simulate, Critique. Never "Understand X" — it isn't checkable.}}

## Key Concepts

High-level ideas the unit covers. No daily detail (that belongs in week files).

- {{Concept}}
- {{…}}
- {{8 items}}

## Weekly Sequence

**Format example for one row** (your unit's data will differ):

| Week | File | Focus | Key Skills |
|------|------|-------|------------|
| 1 | [`week1_kinematics.md`](../week1_kinematics.md) | 1D and 2D motion under constant acceleration | reading position–time graphs; applying v² = u² + 2as |

**Your unit's Weekly Sequence (fill in):**

| Week | File | Focus | Key Skills |
|------|------|-------|------------|
| N | [`weekN_topic.md`](../weekN_topic.md) | {{one-line focus}} | {{skill or technique introduced}} |
| N+1 | [`week(N+1)_topic.md`](…) | … | … |

## Glossary

Centralized at the unit level so week files don't redefine these terms.

- **Term**: {{one-line definition}}
- **Term**: {{one-line definition}}
- {{…}}
- {{12 terms}}

## Tools & Resources

Use tools (`query_rag`, `fetch_github_file`) to confirm each entry exists before listing it. Mark each URL `[verified]` (tool confirmed) or `[unverified]` (still needs checking). If you cannot confirm something exists, write a placeholder — e.g., `{{find a sim for: TOPIC}}` — rather than inventing a domain.

Bias toward canonical sources: PhET (phet.colorado.edu), IBM Quantum, arXiv (arxiv.org), Wolfram Alpha, MIT OCW (ocw.mit.edu), NIST, Qiskit (qiskit.org), Python library docs.

- `[verified]` {{Tool / textbook / video / course}} — {{one-line reason it's recommended}}
- `[unverified]` {{URL or resource}} — {{verify before relying on this}}
- {{…}}

## Real-World Connections

Where this unit's content shows up in industry, research, or daily life.

- {{Connection: e.g., "Mechanics → vehicle crash safety, sports biomechanics, robotic motion planning"}}
- {{…}}
- {{4 bullets}}

## Unit Self-Assessment

A single open-ended summative challenge at the end of the unit. Pick one:

- **Problem**: {{a substantive multi-step problem that requires synthesizing several weeks}}
- **Project**: {{a build, simulation, or extended derivation exercising multiple weeks}}
- **Synthesis**: {{a written essay or argument tying the unit's core ideas together}}

Self-grade against the Learning Outcomes above. No formal quiz.

## Going Deeper

Optional extensions for curiosity-driven follow-up.

- {{Advanced text, paper, video, or topic}}
- {{…}}
