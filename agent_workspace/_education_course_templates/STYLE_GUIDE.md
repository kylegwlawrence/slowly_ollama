# Curriculum Style Guide

Short conventions for outlines built from the templates in this folder (`UNIT_TEMPLATE.md`, `WEEK_TEMPLATE.md`, `CAPSTONE_WEEK_TEMPLATE.md`). Subject-agnostic — these rules apply to any self-study curriculum the templates are used for.

## Audience

One learner — self-study. Not a classroom. Templates and outlines drop all classroom-only apparatus: differentiation tiers, group work, peer review, classroom-fair extensions, and "show students a video" hooks. Replace with solo prompts, self-assessment, and reflection.

## Heading hierarchy

- `#` (H1) — file title only, exactly once at the top
- `##` (H2) — top-level sections (Overview, Metadata, Session 1, etc.)
- `###` (H3) — subsections within a section (Objective, Concept Study, Practice, etc.)
- Do **not** skip levels.
- Do **not** use `####` (H4) or deeper. If you find you need more depth, restructure into a new H2 section.

## File naming

- Course overview: `COURSE.md` — one per workspace folder (all caps; there is only one per course)
- Unit overviews: `unitN_{{topic}}.md` (e.g., `unit3_topic.md`)
- Week files: `weekN_{{topic}}.md` (e.g., `week13_topic.md`)
- Capstone weeks: `weekN_{{phase}}.md` (e.g., `weekN_proposal.md`, `weekN_execution.md`, `weekN_submission.md`)
- All lowercase, underscore-separated. Topic in 1–3 words.

## Week numbering

Global across the entire curriculum: **W1 through W_total** (where W_total is the curriculum's full week count). Do not restart numbering per unit.

## Prerequisites

A week's or unit's prerequisites must be a strict subset of content introduced in earlier files in the curriculum. If a needed concept isn't covered upstream:

- Add a `**Prerequisite gap**:` note in the Metadata section
- Link to a suggested external reading (textbook chapter, video, paper, primary source) so you can fill the gap before starting

Do not silently list a concept as a prereq and hope you'll have it.

## Tool URLs

- Link only to URLs you have **personally visited and confirmed work**, or that a tool (`query_rag`, `fetch_github_file`) has confirmed in this session.
- Mark anything unconfirmed inline: `[unverified] http://example.org/sim`
- Bias toward canonical sources for your subject:
  - STEM: arXiv, MIT OCW, Wolfram Alpha, NIST, established subject-specific simulation platforms
  - humanities & social science: JSTOR, Internet Archive, Project MUSE, primary-source archives, university press publications
  - programming: official library / framework documentation, language reference docs
  - general: established academic publishers, peer-reviewed journals, official institutional sites
- If you can't find a real source for a topic, leave a placeholder using `{{...}}` syntax, e.g. `{{find a sim for: photoelectric effect}}` or `{{find primary source: TOPIC}}` — don't fabricate a plausible-sounding domain.

## Objectives — action verbs only

Replace "Understand X" with checkable verbs:

- Derive, Apply, Compute, Compare, Explain, Analyze, Interpret, Predict, Model, Simulate, Argue, Critique, Compose

Pick the verb that fits your subject. STEM problems lean Derive / Apply / Compute / Model / Simulate; humanities and social science lean Analyze / Interpret / Argue / Critique; arts and writing lean Compose / Critique.

Each objective should describe something you can demonstrate you can do — by solving a problem, producing an artifact, or stating an argument.

## Synthesis prompts — vary them

Avoid recycling the same generic phrasing across weeks (e.g., "How does [topic] apply to a career you're interested in?" repeated in three consecutive weeks adds nothing).

Prefer these patterns:

- **Concrete prediction**: "If you doubled X, what would Y do? Make a prediction, then test it." (Or for non-quantitative subjects: "If the author had taken position X instead of Y, how would the argument change?")
- **Comparison**: "In one paragraph, compare the assumptions of interpretation A vs. B."
- **Connection**: "How does this week's concept relate to [specific prior week]?"
- **Failure mode**: "What would happen if the assumption that X holds were violated?"
- **Justification**: "Pick one of the three interpretations and defend it in 2 paragraphs."

## Unit count

Choose the number of units based on the subject's scope and natural structure. A unit is a coherent learning arc with its own outcomes, glossary, and summative challenge — not a topic list.

- **3–4 units**: short, focused course with a single central thread
- **5–7 units**: standard course with distinct phases, frameworks, or periods — most courses fall here
- **8–10 units**: comprehensive survey of a wide field
- **More than 10 units**: this is a curriculum, not a course — split into multiple courses

If two proposed units feel like one logical block, merge them. If a unit can't be summarized as a single capability the learner gains, it needs to be split or scoped more tightly.

## Core Themes

A theme is a recurring tension, question, or challenge that reappears across multiple units in different forms — not a topic name. Topics belong in the Unit Sequence; themes belong in Core Themes.

- **Wrong**: "Thermodynamics" — a topic.
- **Right**: "Order vs. disorder: every system in this course tends toward states that maximize the number of ways things can be arranged" — a recurring tension that reappears in units on gases, chemistry, and information theory.

Each theme should be stated as a claim or question, not a label. Use the formula: *[Tension or recurring challenge]: [one sentence explaining how it shows up across units]*.

## Course-unit content split

- **Course file (`COURSE.md`)**: thin planning overview only — narrative, learning outcomes, core themes, Unit Sequence table, course arc rationale, course-level resources. **No unit detail.**
- **Unit file**: source of truth for unit content — outcomes, glossary, weekly TOC, summative challenge.

Course-level learning outcomes must require content from multiple units to achieve. If an outcome could be fully met within a single unit, push it down into that unit file and raise the course outcome higher.

Do not duplicate content between course and unit files. If you find yourself describing a unit's content in `COURSE.md`, push the detail into the unit file and leave only a one-line summary in the Unit Sequence table.

## Unit-week content split

- **Unit file**: thin overview only — narrative, learning outcomes, glossary, weekly TOC table, summative challenge. **No daily detail.**
- **Week file**: source of truth for daily content — session-by-session structure.

Do not duplicate content between unit and week files. If you find yourself describing the same activity in both, push the detail down into the week and leave only a one-line summary in the unit.

## No meta-notes in outlines

Outlines must not contain footers describing their own file structure (e.g., trailing paragraphs like *"This file is saved as weekN_topic.md with the same structure as week(N+1)_topic.md…"*). Templates and this style guide carry conventions; outlines stay clean.

## "Files to Save" sections

Don't include them. Filenames are not learner-facing content. If you need to list the week files of a unit, use the **Weekly Sequence** table in the unit template — it has a `File` column and serves as a linked TOC.

## What to drop entirely

These appear in many existing classroom-style outlines and have no home in self-study templates:

- **Differentiation** (Struggling Students / Advanced Students) — irrelevant for solo learner
- **Pedagogical Approach** sections — meta-commentary on teaching style; replaced by the templates themselves
- **Materials Needed** sections — usually irrelevant for digital-resource-based weeks; mention any physical equipment inline in the Active Engagement step
- **Group activities** (debates, group brainstorming, peer review workshops) — converted to solo equivalents in Going Deeper ("argue both sides" prompts) where the underlying skill is valuable
- **Classroom-fair / science-fair extensions** — classroom-context only
- **Classroom Hook framing** ("Show students a video and ask…") — reframe as solo warm-up reading inside Concept Study
- **Multiple-choice quizzes** — formal grading apparatus; replaced by self-assessment problems + reflection
