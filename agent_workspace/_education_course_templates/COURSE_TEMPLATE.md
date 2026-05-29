# {{Course Title}}

> **Filling this template?** The "Agent brief" section below is reference material for you. When you `write_file` the filled outline, **start the output at the `## Metadata` heading** and omit everything above it.

## Agent brief — read before filling

You are filling this template for one **self-study learner**. Not a classroom.

**Workflow.** Multi-turn fill — do one or two sections per turn. Suggested order: Metadata → Overview → Learning Outcomes → Core Themes → Unit Sequence → Course Arc → Resources → Real-World Connections → After This Course.

**Audience.** Address the reader as "you". Do **not** write: "students" / "the class", Differentiation, Pedagogical Approach, or classroom hooks.

**How many units to choose.** Base the unit count on the scope and natural structure of the subject. A unit is a coherent learning arc with its own outcomes, glossary, and summative challenge — not a topic list. If two proposed units feel like one logical block, merge them.

- **3–4 units**: a short, focused course with a single central thread (e.g., one mathematical method, a constrained historical period, one programming paradigm)
- **5–7 units**: a standard course covering distinct phases, frameworks, or periods — most self-study courses fall here
- **8–10 units**: a comprehensive survey spanning a wide field; if coverage feels shallow at this count, narrow the scope instead of adding units
- **More than 10 units**: this is a curriculum, not a single course — reconsider the scope or split into multiple courses

If the course ends with a capstone project, include it as the final unit in the Unit Sequence, note its weeks, and fill those weeks using `CAPSTONE_WEEK_TEMPLATE.md` rather than the standard `WEEK_TEMPLATE.md`.

**Week numbering.** Weeks are numbered globally across the entire course (W1 through W_total). Do not restart per unit. Estimate 3–5 weeks per unit as a baseline for self-study pacing; adjust based on the unit's depth.

**Action verbs (mandatory).** Every Learning Outcome must start with a checkable action verb. Pick from:

**Derive, Apply, Compute, Compare, Explain, Analyze, Interpret, Predict, Model, Simulate, Argue, Critique, Compose.**

Never write "Understand X" — it is not checkable. Pick the verb that fits your subject (STEM: Derive / Apply / Compute / Model / Simulate; humanities and social science: Analyze / Interpret / Argue / Critique; arts and writing: Compose / Critique).

**Sources & URLs.** At the course level, list only major resources that span multiple units — textbooks, canonical online courses, simulation platforms, primary-source archives. Per-unit and per-week resources belong in the unit and week files, not here. Use tools (`query_rag`, `fetch_github_file`) to verify each entry. Mark each `[verified]` or `[unverified]`. Leave a placeholder (`{{find course resource: TOPIC}}`) rather than fabricating a URL.

**Relationship between course and unit outcomes.** Course-level outcomes require integrating content from multiple units to achieve. They should not duplicate individual unit outcomes — if a course outcome could be fully met by a single unit, push it down into that unit's `UNIT_TEMPLATE.md` and raise the course outcome higher.

  - **Wrong**: "Apply Newton's laws to projectile problems" — Unit 1 achieves this alone.
  - **Right**: "Compare Newtonian and Lagrangian approaches to predict the motion of a coupled system" — requires Units 1 and 6.

**Format.** H1 once (the course title), H2 for sections, H3 for subsections. No H4 or deeper. The Unit Sequence is the only table — everything else is bullets.

**Targets — write exactly these counts:**

- 6 Learning Outcomes
- 4 Core Themes
- N rows in the Unit Sequence (where N is the unit count you chose above)
- 3–5 Resources
- 4 Real-World Connections
- 3 After This Course items

---

## Metadata

- **Total units**: N
- **Total weeks**: ~N weeks (N units × 3–5 weeks each, adjusted for depth)
- **Estimated effort**: ~N–N hours/week (3 sessions × 2–3 hours)
- **Prerequisites**: {{Prior knowledge, skills, tools, or concepts required before starting Unit 1. Be specific — name the skills, not just the subject area.}}
- **Difficulty tier**: {{intro | intermediate | advanced | frontier}}

## Overview

{{3 paragraphs. What is this course about, and why does it exist as a course? What arc does it trace from Unit 1 to the final unit — where does the learner start and where do they end up? What questions will you be able to answer, problems will you be able to solve, or positions will you be able to argue by the end that you could not have at the start?}}

## Learning Outcomes

By the end of this course you should be able to:

- {{Action verb + object + qualifier — course-level outcomes require content from multiple units. Do not list outcomes achievable within a single unit.}}
- {{…}}
- {{6 outcomes total. Use a checkable action verb from the Agent brief. Never "Understand X" — it isn't checkable.}}

## Core Themes

The 4 recurring ideas or tensions that run across all units. A theme is a recurring tension, question, or challenge — not a topic name. It should appear in multiple units in different forms.

**Formula**: *[A tension or recurring challenge]: [one sentence explaining how it shows up across units]*

Examples of the right shape:
- **Idealization vs. reality**: every model makes assumptions that eventually break down — recognizing when they break is as important as using them
- **Local rules vs. global behavior**: small-scale interactions (particles, individuals, genes) repeatedly produce large-scale structure that can't be predicted from a single instance

Write your 4 themes in this format:

- **{{Tension or recurring challenge — a claim or question, not a topic name}}**: {{one sentence explaining how it recurs across units}}
- {{…}}
- {{4 themes}}

## Unit Sequence

**Format example for one row** (replace with content from your subject):

| Unit | File | Focus | Weeks | Key Capability Added |
|------|------|-------|-------|----------------------|
| 3 | [`unit3_thermodynamics.md`](unit3_thermodynamics/unit3_thermodynamics.md) | Energy, entropy, and the limits of heat engines | W7–W9 | Applying the first and second laws to predict the efficiency and failure modes of real heat engines |

Note: **Focus** is the unit's central concept or question in one line. **Key Capability Added** is what the learner can do after the unit that they could not do before — one clause, specific enough to test.

**Your course's Unit Sequence (fill in):**

| Unit | File | Focus | Weeks | Key Capability Added |
|------|------|-------|-------|----------------------|
| 1 | [`unit1_topic.md`](unit1_topic/unit1_topic.md) | {{one-line central concept or question}} | W1–WN | {{specific capability gained}} |
| 2 | [`unit2_topic.md`](unit2_topic/unit2_topic.md) | … | WN+1–WM | … |

## Course Arc

{{2–3 paragraphs. Start by stating how many units the course has and why that number fits the subject — too few would merge distinct phases; too many would fragment what belongs together. Then explain why the units appear in this order: name what Unit 1 makes possible for Unit 2, and carry that chain forward. Identify the one unit that is the conceptual pivot point — the unit that, if removed, would make the units after it impossible. A reader who finishes this section should understand why no unit could come first and why the final unit comes last.}}

## Resources

Major resources that span multiple units. Per-unit and per-week resources belong in the unit and week files, not here. Use tools to verify each entry before listing. Mark each `[verified]` or `[unverified]`. Leave a placeholder (`{{find course resource: TOPIC}}`) rather than fabricating a domain.

- `[verified]` {{Resource — textbook, canonical course, platform, archive}} — {{one-line reason it covers multiple units of this course}}
- {{…}}

## Real-World Connections

Where the full course — not a single unit — shows up in practice, research, or daily life. These connections should require the breadth of the whole course, not just one unit's content.

- {{Connection: "[Course subject] → [3 concrete contexts where the full span of the course's capabilities matters]"}}
- {{…}}
- {{4 bullets}}

## After This Course

Where to go once the final unit's summative challenge is complete. Three opinionated destinations — not an exhaustive list.

- **Next course**: {{Advanced course, specialization, or field that builds directly on this course's final capabilities}}
- **Key text or project**: {{A significant text, open problem, or hands-on project that becomes tractable after mastering this material}}
- **Applied context**: {{A community, practice domain, or real-world field where this course's full range of outcomes is exercised}}
