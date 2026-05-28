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

**Action verbs (mandatory).** Every Learning Outcome and Session Objective must start with one of:

**Derive, Apply, Compute, Compare, Explain, Predict, Model, Simulate, Critique.**

Never write "Understand X" — it is not checkable.

**Sources & URLs.** Use tools to ground every citation:

- `query_rag` for retrieval over configured knowledge sources
- `fetch_github_file` for a github.com blob URL or raw.githubusercontent.com URL
- `read_file` for prior week and unit files in this workspace

When a tool confirms a source, write `[verified] URL`. When you cannot confirm, write a placeholder — `{{find a sim for: TOPIC}}`, `{{find source: TOPIC}}`, or `{{find paper: TOPIC}}`. **Never invent a domain, paper title, or textbook chapter number.** Bias to canonical sources: PhET (phet.colorado.edu), IBM Quantum, arXiv, Wolfram Alpha, MIT OCW, NIST, Qiskit, Python library docs.

**Prerequisites.** Use `read_file` on prior week files before listing prereqs. Fill the `**Prerequisite gap**` bullet only when you have inspected upstream files and confirmed the concept is missing. Otherwise delete that bullet.

**Format.** H1 once (the file title), H2 for sections, H3 for subsections. No H4 or deeper. Bullets only — this template uses no tables.

**Targets — write exactly these counts:**

- 4 Learning Outcomes
- 8 Key Terms
- 3 Practice problems per session
- 5 End-of-Week problems
- 1–2 Real-World examples
- 3 Going Deeper items

## Example of a filled Session 1 (reference only — your topic will differ)

### Objective

Apply Newton's second law to compute net force on a mass subject to multiple horizontal forces.

### Concept Study (~45–60 min)

- PhET: Forces and Motion: Basics — `[verified] https://phet.colorado.edu/sims/html/forces-and-motion-basics/latest/forces-and-motion-basics_en.html` — vary applied force, friction, and mass; observe acceleration.
- Read: OpenStax *College Physics* §4.3 ("Newton's Second Law of Motion") — derive F = ma from the time derivative of momentum.

### Active Engagement (~30–45 min)

- **Derivation.** Starting from p = mv (constant m), differentiate to get dp/dt = m(dv/dt) = ma. Write the derivation in 4–6 lines. Flag where the constant-mass assumption enters and what changes if m varies.

Concrete deliverable expected: a half-page derivation with the constant-mass assumption explicitly flagged.

### Practice (~30–45 min)

1. A 2.0 kg block on a frictionless surface is pulled by a 6.0 N horizontal force. Compute its acceleration.
2. Same block, with kinetic friction μ_k = 0.20. Compute net force and resulting acceleration. (g = 9.8 m/s².)
3. Two blocks (1.0 kg and 3.0 kg) are linked by a massless rope on a frictionless surface. A 12 N force pulls the 3.0 kg block. Compute the tension in the rope.

### Reflection (~5–10 min)

- Which step in the derivation felt least intuitive? Why?
- How does this session's content connect to last week's kinematics work?

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
- {{4 outcomes total. Use Derive, Apply, Compute, Compare, Explain, Predict, Model, Simulate, Critique. Never "Understand X" — it isn't checkable.}}

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

- {{Specific source: textbook chapter X.Y / video URL / paper / your own derivation prompt}}
- {{…}}

### Active Engagement (~30–45 min)

A solo activity to convert passive concept-study into active understanding. Choose one or combine:

- **Simulation**: {{Run sim X; observe behavior Y; vary parameter Z}}
- **Derivation**: {{Derive equation X starting from assumption Y}}
- **Thought experiment**: {{Predict outcome of scenario X; reason about why}}

Concrete deliverable expected: {{one sentence, e.g., "a plotted graph", "a half-page derivation", "a written prediction with justification"}}.

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

Where this week's content shows up in industry, research, or daily life. Be specific — name a real system, technology, or phenomenon.

- {{1–2 grounded examples}}

## Going Deeper

Optional extensions if curious. Includes contrarian or open-question prompts framed for solo reflection.

- {{Paper, video, advanced textbook chapter, or open question}}
- {{"Argue both sides" prompt: a question with two defensible answers; reason through both}}
- {{…}}
