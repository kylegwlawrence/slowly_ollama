# Week N: {{Title}}

> **Filling this template?** The "Agent brief" and "Example" sections below are reference material for you. When you `write_file` the filled outline, **start the output at the `## Metadata` heading** and omit everything above it.

## Agent brief — read before filling

You are filling this template for one **self-study learner**. Not a classroom.

**Workflow.** Multi-turn fill — do one section per turn. Suggested order:
Metadata → Overview → Learning Outcomes → Key Terms → Session 1 → Session 2 → Session 3 → End-of-Week Self-Assessment → Real-World Application → Going Deeper.

**Audience.** Address the reader as "you". Do **not** write:

- "students", "the class", "in groups", "peer review", "debate teams"
- Differentiation / Struggling Students / Advanced Students sections
- Pedagogical Approach, Materials Needed, Science Fair extensions
- "Show students a video and ask…" — reframe as a solo warm-up reading inside Concept Study

**Action verbs (mandatory).** Every Learning Outcome and Session Objective must start with a checkable action verb. Pick from:

**Derive, Apply, Compute, Compare, Explain, Analyze, Interpret, Predict, Model, Simulate, Argue, Critique, Compose.**

Never write "Understand X" — it is not checkable. Pick the verb that fits your subject (STEM problems lean Derive / Apply / Compute / Model / Simulate; humanities and social science lean Analyze / Interpret / Argue / Critique; arts and writing lean Compose / Critique).

**Sources & URLs.** Use tools to ground every citation:

- `query_rag` for retrieval over configured knowledge sources
- `fetch_github_file` for a github.com blob URL or raw.githubusercontent.com URL
- `read_file` for prior week and unit files in this workspace

When a tool confirms a source, write `[verified] URL`. When you cannot confirm, write a placeholder — `{{find a sim for: TOPIC}}`, `{{find source: TOPIC}}`, or `{{find paper: TOPIC}}`. **Never invent a domain, paper title, or textbook chapter number.** Bias to canonical sources for your subject:

- STEM: arXiv, MIT OCW, Wolfram Alpha, NIST, established subject-specific simulation platforms
- humanities & social science: JSTOR, Internet Archive, Project MUSE, primary-source archives, university press publications
- programming: official library / framework documentation, language reference docs
- general: established academic publishers, peer-reviewed journals, official institutional sites

**Prerequisites.** Use `read_file` on prior week files before listing prereqs. Fill the `**Prerequisite gap**` bullet only when you have inspected upstream files and confirmed the concept is missing. Otherwise delete that bullet.

**Format.** H1 once (the file title), H2 for sections, H3 for subsections. No H4 or deeper. Bullets only — this template uses no tables.

**Targets — write exactly these counts:**

- 4 Learning Outcomes
- 8 Key Terms
- 3 Practice problems per session
- 5 End-of-Week problems
- 1–2 Real-World examples
- 3 Going Deeper items

## Example shape of a filled Session 1 (reference only — replace bracketed prompts with content from your subject)

### Objective

[Action verb] + [specific concept or skill] + [scope qualifier]. One sentence.

### Concept Study (~45–60 min)

- [Specific source — simulation, textbook chapter, paper, video, primary document]: `[verified] URL` — [what to do with it, e.g., vary parameter X; annotate the central thesis; trace the derivation].
- [Second specific source]: `[verified] URL` — [what to do with it].

### Active Engagement (~30–45 min)

- **[One of: Derivation / Simulation / Implementation / Close reading / Thought experiment / Composition]**. [Concrete prompt with one assumption to flag explicitly.]

Concrete deliverable expected: [one sentence describing the artifact, e.g., a half-page derivation, an annotated text with margin notes, a labeled diagram, a debuggable code snippet, a short composition].

### Practice (~30–45 min)

1. [Problem with concrete specifics — numbers, names, dates, code, or excerpts. Mix conceptual and applied.]
2. […]
3. […]

### Reflection (~5–10 min)

- [Question: where did your understanding slow down most? Why?]
- [Question: how does this session relate to prior content — earlier this week, a prior week, or a prior unit?]

---

## Metadata

- **Parent unit**: [Unit M: {{Unit Title}}](../unitM_topic/unitM_topic.md)
- **Duration**: ~3 sessions × 2–3 hours
- **Prerequisites**:
  - {{Specific prior week or upstream concept}}
  - {{…}}
  - **Prerequisite gap** (fill only after `read_file` on prior weeks confirms the concept is missing — otherwise delete this bullet): {{concept needed but not covered upstream; link to a suggested external reading to fill the gap before starting}}

## Overview

{{2 sentences. What does this week unlock? Where does it sit in the unit's arc?}}

## Learning Outcomes

By the end of this week you should be able to:

- {{Action verb + object + qualifier}}
- {{…}}
- {{4 outcomes total. Use a checkable action verb from the Agent brief (Derive, Apply, Compute, Compare, Explain, Analyze, Interpret, Predict, Model, Simulate, Argue, Critique, Compose). Never "Understand X" — it isn't checkable.}}

## Key Terms

Week-specific vocabulary. Broader unit-level terms belong in the parent unit's Glossary.

- **Term**: {{one-line definition}}
- {{…}}
- {{8 terms}}

---

## Session 1: {{Subtopic}}

### Objective

{{One sentence stating what this session accomplishes.}}

### Concept Study (~45–60 min)

What to read, watch, or derive to absorb the concept.

- {{Specific source: textbook chapter X.Y / video URL / paper / primary document / your own derivation or analysis prompt}}
- {{…}}

### Active Engagement (~30–45 min)

A solo activity to convert passive concept-study into active understanding. Choose one or combine:

- **Derivation**: {{Derive equation or result X starting from assumption Y}}
- **Simulation**: {{Run sim X; observe behavior Y; vary parameter Z}}
- **Implementation**: {{Build or extend a small artifact — code snippet, calculation, diagram}}
- **Close reading**: {{Annotate source X; identify the central claim and the strongest supporting evidence}}
- **Thought experiment**: {{Predict the outcome of scenario X; reason about why}}
- **Composition**: {{Write or compose a short piece in form X with constraint Y}}

Concrete deliverable expected: {{one sentence — e.g., "a plotted graph", "a half-page derivation", "an annotated text", "a debuggable code snippet", "a written prediction with justification"}}.

### Practice (~30–45 min)

1. {{Problem with one-line setup. Mix conceptual + computational.}}
2. {{…}}
3. {{…}}

{{3 problems.}}

### Reflection (~5–10 min)

- What from this session is still unclear?
- One connection: how does this session's content relate to prior content (earlier this week, a prior week, or a prior unit)?

---

## Session 2: {{Subtopic}}

### Objective

{{One sentence.}}

### Concept Study (~45–60 min)

- {{…}}

### Active Engagement (~30–45 min)

- {{…}}

### Practice (~30–45 min)

1. {{…}}
2. {{…}}
3. {{…}}

### Reflection (~5–10 min)

- {{…}}

---

## Session 3: {{Subtopic}}

### Objective

{{One sentence.}}

### Concept Study (~45–60 min)

- {{…}}

### Active Engagement (~30–45 min)

- {{…}}

### Practice (~30–45 min)

1. {{…}}
2. {{…}}
3. {{…}}

### Reflection (~5–10 min)

- {{…}}

---

## End-of-Week Self-Assessment

- **Problems** (5 total, mixing conceptual and computational):
  1. {{Problem}}
  2. {{…}}
  3. {{…}}
  4. {{…}}
  5. {{…}}
- **Synthesis prompt** (1–2 paragraphs to write): {{a specific prompt that ties this week's sessions together. Avoid generic "how does X apply to a career" phrasings — see STYLE_GUIDE.md for prompt variety.}}

## Real-World Application

Where this week's content shows up outside the curriculum — in industry, research, contemporary practice, or daily life. Be specific — name a real example, work, system, technology, event, or phenomenon.

- {{1–2 grounded examples}}

## Going Deeper

Optional extensions if curious. Includes contrarian or open-question prompts framed for solo reflection.

- {{Paper, video, advanced textbook chapter, or open question}}
- {{"Argue both sides" prompt: a question with two defensible answers; reason through both}}
- {{…}}
