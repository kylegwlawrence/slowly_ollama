# {{Degree Title}}

> **Filling this template?** The "Agent brief" section below is reference material for you. When you `write_file` the filled outline, **start the output at the `## Metadata` heading** and omit everything above it.

## Agent brief — read before filling

You are filling this template for one **self-study learner**. Not a classroom, not a university registrar.

A "degree" here is a coherent self-study program of 4–6 courses. It is **not an accredited credential**. Do not write about credit hours, GPA, transcripts, departmental requirements, prerequisites for admission to a real university, or any registrar/institutional apparatus.

**Workflow — strict one section per turn.** Small models lose coherence when filling multiple sections at once. Fill one `##` section, stop, wait for the next turn. Suggested order:

Metadata → Overview → Program Outcomes → Core Themes → Course Sequence → Program Arc → Resources → Real-World Connections → After This Degree.

**Audience.** Address the reader as "you". Do **not** write: "students" / "the class" / "the cohort" / "the program of study committee" / "the registrar". No differentiation tiers, no group work, no peer review, no classroom-fair extensions.

**How many courses to choose.** Pick **exactly one** of: 4, 5, or 6. No other count is valid.

- **4 courses**: a tightly focused program with one central thread (e.g., one method family, one historical era, one programming paradigm)
- **5 courses**: a standard program — most degrees fit here
- **6 courses**: a broad program spanning two related subfields

Fewer than 4 should just be a single course (use `COURSE_TEMPLATE.md`). More than 6 should be split into two degrees.

**The final course is the capstone.** The final row of your Course Sequence is always a degree-level capstone course: an independent thesis, portfolio, or comprehensive integration project drawing on at least three earlier courses. Fill it as a normal course using `COURSE_TEMPLATE.md`. Its final unit is the capstone unit, and that unit's weeks are filled using `CAPSTONE_WEEK_TEMPLATE.md` rather than the standard `WEEK_TEMPLATE.md`.

**Week numbering.** Weeks are numbered **globally across the entire degree** (W1 through W_total). Course 1 starts at W1; Course 2 starts at the week after Course 1 ends. Do not restart per course.

**Action verbs (mandatory).** Every Program Outcome must start with a checkable action verb. Pick from this closed list:

**Derive, Apply, Compute, Compare, Explain, Analyze, Interpret, Predict, Model, Simulate, Argue, Critique, Compose.**

Never write "Understand X" — it is not checkable. Pick the verb that fits your subject (STEM: Derive / Apply / Compute / Model / Simulate; humanities and social science: Analyze / Interpret / Argue / Critique; arts and writing: Compose / Critique).

**Sources & URLs.** At the degree level, list only major resources that span multiple courses — flagship textbooks, canonical online platforms, primary-source archives. Per-course resources belong in each course's `COURSE.md`, not here. Use tools (`query_rag`, `fetch_github_file`) to verify each entry. Mark each `[verified]` or `[unverified]`. Leave a placeholder (`{{find degree-spanning resource: TOPIC}}`) rather than fabricating a URL.

**Relationship between degree and course outcomes.** Program Outcomes must require integrating content from **multiple courses** to achieve. If an outcome could be fully met by a single course, push it down into that course's `COURSE_TEMPLATE.md` and raise the degree outcome higher.

  - **Wrong**: "Derive the equations of motion for a classical system" — a single course on classical mechanics achieves this alone.
  - **Right**: "Apply both classical and quantum frameworks to predict the behavior of a coupled physical system" — requires two distinct courses.

**File layout.** Each course lives in its own subfolder. Reference each course from this file using its relative path. Do not flatten the structure.

```
DEGREE.md                                      <- this file
course1_{{topic}}/COURSE.md                    <- one subfolder per course
course1_{{topic}}/unit1_{{topic}}/unit1_{{topic}}.md
course2_{{topic}}/COURSE.md
...
courseN_capstone/COURSE.md                     <- final course is the capstone
courseN_capstone/unitN_capstone/...            <- capstone weeks use CAPSTONE_WEEK_TEMPLATE.md
```

Each course folder is internally identical to a standalone course folder — the existing `COURSE_TEMPLATE.md`, `UNIT_TEMPLATE.md`, `WEEK_TEMPLATE.md`, and `CAPSTONE_WEEK_TEMPLATE.md` do not change.

**Format.** H1 once (the degree title), H2 for sections, H3 for subsections. No H4 or deeper. The Course Sequence is the only table — everything else is bullets.

**Targets — write exactly these counts:**

- 6 Program Outcomes
- 4 Core Themes
- N rows in the Course Sequence (where N is the 4, 5, or 6 you chose above)
- 3–5 Resources
- 4 Real-World Connections
- 3 After This Degree items

---

## Metadata

- **Total courses**: N
- **Total weeks**: ~N weeks (sum of weeks across all courses; weeks are global across the degree)
- **Estimated effort**: ~N–N hours/week (3 sessions × 2–3 hours per active course)
- **Prerequisites for entering the program**: {{Prior knowledge, skills, tools, or concepts required before starting Course 1. Be specific — name the skills, not just the subject area.}}
- **Difficulty tier reached**: {{intro | intermediate | advanced | frontier}} (the tier the program reaches by the final course)

## Overview

{{3 short paragraphs.}}

{{Paragraph 1: What does this degree teach, and why does it exist as a coherent program rather than a single course?}}

{{Paragraph 2: What arc does it trace from Course 1 to the capstone — where does the learner start, and what can they do at the end that they could not at the start?}}

{{Paragraph 3: How does difficulty ramp through the program, and at which course does the ramp get steep? Name the course.}}

## Program Outcomes

By the end of this degree you should be able to:

- {{Action verb + object + qualifier — degree-level outcomes require content from multiple courses. Do not list outcomes achievable within a single course.}}
- {{…}}
- {{6 outcomes total. Use a checkable action verb from the Agent brief. Never "Understand X" — it isn't checkable.}}

## Core Themes

The 4 recurring ideas or tensions that run across **all courses** in the program. A theme is a recurring tension, question, or challenge — not a topic name. It should appear in at least three of the courses in different forms.

**Formula**: *[A tension or recurring challenge]: [one sentence explaining how it shows up across courses]*

**Right shape (degree-level theme):**
- **Theory and instrumentation evolve together**: every framework in this program is only as useful as the apparatus that can test it — recognizing the experimental constraints is as important as deriving the theory

**Wrong shape (these are topic names, not themes):**
- ~~Quantum mechanics~~ — a topic, not a tension
- ~~Forces and fields~~ — a topic, not a tension

Write your 4 themes in this format:

- **{{Tension or recurring challenge — a claim or question, not a topic name}}**: {{one sentence explaining how it recurs across courses}}
- {{…}}
- {{4 themes}}

## Course Sequence

**Format example for one row** (replace with content from your subject):

| Course | File | Focus | Weeks | Key Capability Added |
|--------|------|-------|-------|----------------------|
| 2 | [`course2_quantum/COURSE.md`](course2_quantum/COURSE.md) | Quantum mechanics from wavefunctions to entanglement | W55–W72 | Setting up and interpreting the Schrödinger equation for non-trivial systems and analyzing quantum measurement |

Note: **Focus** is the course's central concept or question in one line. **Key Capability Added** is what the learner can do after the course that they could not before — one clause, specific enough to test. The **final row is always the capstone course**.

**Your degree's Course Sequence (fill in):**

| Course | File | Focus | Weeks | Key Capability Added |
|--------|------|-------|-------|----------------------|
| 1 | [`course1_topic/COURSE.md`](course1_topic/COURSE.md) | {{one-line central concept or question}} | W1–WN | {{specific capability gained}} |
| 2 | [`course2_topic/COURSE.md`](course2_topic/COURSE.md) | … | WN+1–WM | … |
| {{final}} | [`courseN_capstone/COURSE.md`](courseN_capstone/COURSE.md) | Capstone: independent thesis or comprehensive integration project | … | Producing an original investigation that integrates frameworks from at least three earlier courses |

## Program Arc

{{2–3 short paragraphs.}}

{{Paragraph 1: State how many courses the program has and why that number fits the subject — too few would merge distinct subfields, too many would split a single subfield into pieces too thin to support coherent capstone work.}}

{{Paragraph 2: Explain why the courses appear in this order — name what Course 1 makes possible for Course 2, and carry the chain forward. Identify the one course that is the pivot point — the course that, if removed, would make every course after it impossible.}}

{{Paragraph 3: Explain why the capstone course must come last and name the earlier courses it explicitly draws on.}}

## Resources

Major resources that span multiple courses. Per-course resources belong in each course's `COURSE.md`, not here. Use tools to verify each entry before listing. Mark each `[verified]` or `[unverified]`. Leave a placeholder (`{{find degree-spanning resource: TOPIC}}`) rather than fabricating a domain.

- `[verified]` {{Resource — flagship textbook, canonical course platform, primary-source archive}} — {{one-line reason it covers multiple courses of this program}}
- {{…}}
- {{3–5 items}}

## Real-World Connections

Where the full program — not a single course — shows up in practice, research, or daily life. These connections should require the breadth of the whole degree, not just one course's content.

- {{Connection: "[Degree subject] → [3 concrete contexts where the full span of the program matters]"}}
- {{…}}
- {{4 bullets}}

## After This Degree

Where to go once the capstone course is complete. Three opinionated destinations — not an exhaustive list.

- **Next program of study**: {{Graduate program, advanced curriculum, specialization track, or peer field that builds directly on this degree's final capabilities}}
- **Significant text or project**: {{A canonical text, open research problem, or hands-on project that becomes tractable only after completing all courses}}
- **Applied context**: {{A professional community, research domain, or real-world field where the full breadth of this program's outcomes is exercised}}
