# Physics Lessons Style Guide

Short conventions for outlines in `agent_workspace/physics-lessons/`. The templates (`UNIT_TEMPLATE.md`, `WEEK_TEMPLATE.md`, `CAPSTONE_WEEK_TEMPLATE.md`) live alongside this file and instantiate these rules.

## Audience

One learner — self-study. Not a classroom. Templates and outlines drop all classroom-only apparatus: differentiation tiers, group work, peer review, science-fair extensions, and "show students a video" hooks. Replace with solo prompts, self-assessment, and reflection.

## Heading hierarchy

- `#` (H1) — file title only, exactly once at the top
- `##` (H2) — top-level sections (Overview, Metadata, Session 1, etc.)
- `###` (H3) — subsections within a section (Objective, Concept Study, Practice, etc.)
- Do **not** skip levels.
- Do **not** use `####` (H4) or deeper. If you find you need more depth, restructure into a new H2 section.

The current corpus has a hierarchy break at Week 7: Weeks 1–6 use `#### Day N` for day headers; Weeks 7–onward use `## Day N`. Normalize to H2 + H3 everywhere.

## File naming

- Unit overviews: `unitN_{{topic}}.md` (e.g., `unit3_electromagnetism.md`)
- Week files: `weekN_{{topic}}.md` (e.g., `week13_quantum_measurement.md`)
- Capstone weeks: `weekN_{{phase}}.md` (e.g., `week31_proposal.md`, `week32_execution.md`, `week33_submission.md`)
- All lowercase, underscore-separated. Topic in 1–3 words.

## Week numbering

Global across the entire curriculum: **W1 through W33**. Do not restart numbering per unit.

The current corpus is inconsistent here: Units 1–4 number weeks per-unit (W1–3 in each unit), while Units 5–10 use global numbering (W13–33). When rewriting an existing file, normalize to global numbering.

## Prerequisites

A week's or unit's prerequisites must be a strict subset of content introduced in earlier files in the curriculum. If a needed concept isn't covered upstream:

- Add a `**Prerequisite gap**:` note in the Metadata section
- Link to a suggested external reading (textbook chapter, video, or paper) so you can fill the gap before starting

Examples from the current corpus that need this treatment:
- `week10_wave_particle_duality.md` lists "Probability Basics" — no prior week covers it
- `week12_entanglement_superposition.md` assumes Dirac notation |0⟩, |1⟩ — used in Week 11 but never formally introduced

Do not silently list a concept as a prereq and hope you'll have it.

## Tool URLs

- Link only to URLs you have **personally visited and confirmed work**.
- Mark anything you haven't verified inline: `[unverified] http://example.org/sim`
- Bias toward stable canonical sources: PhET (phet.colorado.edu), IBM Quantum (quantum-computing.ibm.com), arXiv (arxiv.org), Wolfram Alpha (wolframalpha.com), MIT OCW (ocw.mit.edu), NIST.
- If you can't find a real simulator for a topic, leave a placeholder using `{{...}}` syntax, e.g. `{{find a sim for: photoelectric effect}}` — don't fabricate a plausible-sounding domain.

The current corpus contains several suspect domains (e.g., `quantumthermodynamics.org`, `bose-einstein-condensate.com`, `topological-quantum-computing.com`, `quantum-simulator.com`, `feynmanpathintegrals.com/*`, `quantuminterpreters.org`, `quantumdecay.org`, `stringtheory.org`, `cosmology-simulator.com`, `quantumbiology.org`). Treat them all as `[unverified]` until you've checked them yourself.

## Objectives — action verbs only

Replace "Understand X" with checkable verbs:

- Derive, Apply, Compute, Compare, Explain, Predict, Model, Simulate, Critique

Each objective should describe something you can demonstrate you can do — by solving a problem, producing an artifact, or stating an argument.

## Synthesis prompts — vary them

Retire the recycled phrasing **"How does [topic] apply to a career you're interested in?"** It appears in W4, W5, and W6 of the current corpus and adds nothing.

Prefer these patterns:

- **Concrete prediction**: "If you doubled X, what would Y do? Make a prediction, then test it."
- **Comparison**: "In one paragraph, compare the assumptions of interpretation A vs. B."
- **Connection**: "How does this week's concept relate to [specific prior week]?"
- **Failure mode**: "What would happen if the assumption that X is small were violated?"
- **Justification**: "Pick one of the three interpretations and defend it in 2 paragraphs."

## Unit-week content split

- **Unit file**: thin overview only — narrative, learning outcomes, glossary, weekly TOC table, summative challenge. **No daily detail.**
- **Week file**: source of truth for daily content — session-by-session structure.

Do not duplicate content between unit and week files. If you find yourself describing the same activity in both, push the detail down into the week and leave only a one-line summary in the unit.

The current corpus has this problem badly: `unit1_mechanics_3weeks.md` repeats much of what's in `week1_kinematics.md`, `week2_forces.md`, `week3_energy.md`. Templates rewrite this relationship.

## No meta-notes in outlines

Outlines must not contain footers describing their own file structure (e.g., the trailing paragraph in `week1_kinematics.md`: *"This file is saved as Week1_kinematics.md with the same structure as Week2_forces.md…"*). Templates and this style guide carry conventions; outlines stay clean.

## "Files to Save" sections

Don't include them. Filenames are not learner-facing content. If you need to list the week files of a unit, use the **Weekly Sequence** table in the unit template — it has a `File` column and serves as a linked TOC.

## What to drop entirely

These appear in the current corpus and have no home in the new templates:

- **Differentiation** (Struggling Students / Advanced Students) — irrelevant for solo learner
- **Pedagogical Approach** sections — meta-commentary on teaching style; replaced by the templates themselves
- **Materials Needed** sections — irrelevant for simulation-based weeks; mention any physical equipment inline in the Active Engagement step
- **Group activities** (debates, group brainstorming, peer review workshops) — converted to solo equivalents in Going Deeper ("argue both sides" prompts) where the underlying skill is valuable
- **Science Fair extensions** — classroom-context only
- **Classroom Hook framing** ("Show students a video and ask…") — reframe as solo warm-up reading inside Concept Study
- **15-question quizzes** — formal grading apparatus; replaced by self-assessment problems + reflection
