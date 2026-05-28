# Unit N: {{Title}}

> **Filling this template?** The "Agent brief" section below is reference material for you. When you `write_file` the filled outline, **start the output at the `## Metadata` heading** and omit everything above it.

## Agent brief — read before filling

You are filling this template for one **self-study learner**. Not a classroom.

**Workflow.** Multi-turn fill — do one section per turn. Suggested order: Metadata → Overview → Learning Outcomes → Key Concepts → Weekly Sequence → Glossary → Tools & Resources → Real-World Connections → Unit Self-Assessment → Going Deeper.

**Audience.** Address the reader as "you". Do **not** write: "students" / "the class", Differentiation, Pedagogical Approach, Materials Needed, Science Fair extensions, or classroom hooks. The unit file is a **thin overview** — daily content lives in the week files, not here. If you find yourself describing a specific session activity, push it down into the week file and leave only a one-line summary in the unit.

**Action verbs (mandatory).** Every Learning Outcome must start with a checkable action verb. Pick from:

**Derive, Apply, Compute, Compare, Explain, Analyze, Interpret, Predict, Model, Simulate, Argue, Critique, Compose.**

Never write "Understand X" — it is not checkable. Pick the verb that fits your subject.

**Sources & URLs.** Use tools (`query_rag`, `fetch_github_file`, `read_file`) to ground every citation. When a tool confirms a source, write `[verified] URL`. When you cannot confirm, write a placeholder — `{{find a sim for: TOPIC}}` or `{{find source: TOPIC}}`. **Never invent a domain, paper title, or textbook chapter number.** Bias to canonical sources for your subject:

- STEM: arXiv, MIT OCW, Wolfram Alpha, NIST, established subject-specific simulation platforms
- humanities & social science: JSTOR, Internet Archive, Project MUSE, primary-source archives, university press publications
- programming: official library / framework documentation, language reference docs
- general: established academic publishers, peer-reviewed journals, official institutional sites

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
- **Prerequisites**: {{prior units (e.g., Unit M); skills required (e.g., calculus, close reading, a specific programming language); concepts assumed}}
- **Difficulty tier**: {{intro | intermediate | advanced | frontier}}

## Overview

{{2–3 paragraphs. Why does this unit exist? What learning arc does it trace, from start to finish? Where does it fit in the broader curriculum, and what questions will you be able to answer after finishing it?}}

## Learning Outcomes

By the end of this unit you should be able to:

- {{Action verb + object + qualifier (e.g., "Compare two models of phenomenon X under condition Y" or "Analyze the central argument of source X")}}
- {{…}}
- {{6 outcomes total. Use a checkable action verb from the Agent brief. Never "Understand X" — it isn't checkable.}}

## Key Concepts

High-level ideas the unit covers. No daily detail (that belongs in week files).

- {{Concept}}
- {{…}}
- {{8 items}}

## Weekly Sequence

**Format example for one row** (replace bracketed prompts with content from your subject):

| Week | File | Focus | Key Skills |
|------|------|-------|------------|
| 1 | [`week1_topic.md`](../week1_topic.md) | [one-line focus — the week's central concept or skill in concrete terms] | [specific skill or technique the learner gains, e.g., "applying formula X"; "annotating a primary source"; "debugging recursion"] |

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

Bias toward canonical sources for your subject — see the Sources & URLs block in the Agent brief for examples by discipline.

- `[verified]` {{Tool / textbook / video / course}} — {{one-line reason it's recommended}}
- `[unverified]` {{URL or resource}} — {{verify before relying on this}}
- {{…}}

## Real-World Connections

Where this unit's content shows up outside the curriculum — in industry, research, contemporary practice, or daily life.

- {{Connection: e.g., "[Unit topic] → [3 concrete contexts where it shows up, separated by commas]"}}
- {{…}}
- {{4 bullets}}

## Unit Self-Assessment

A single open-ended summative challenge at the end of the unit. Pick one:

- **Problem**: {{a substantive multi-step problem or analytical question requiring you to synthesize several weeks}}
- **Project**: {{a build, simulation, extended derivation, composition, or research artifact exercising multiple weeks}}
- **Synthesis**: {{a written essay or argument tying the unit's core ideas together}}

Self-grade against the Learning Outcomes above. No formal quiz.

## Going Deeper

Optional extensions for curiosity-driven follow-up.

- {{Advanced text, paper, video, or topic}}
- {{…}}
