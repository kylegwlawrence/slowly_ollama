# How to write a phase retrospective

Retros in this repo are reference material — CLAUDE.md tells future agents
to read the relevant retro before touching a covered area. Write them so a
future you (or another agent) can pick up cold.

## 1. Read these references first

Match their shape. They cover the range you'll likely fall into:

- [`phase7-frontend.md`](phase7-frontend.md) — feature build with a
  prior-phase decision reversal. Good shape reference for "we shipped a
  new layer and changed our minds about X."
- [`phase16-invokable-agents.md`](phase16-invokable-agents.md) — replacement
  phase that deleted code from earlier phases. Good shape reference for
  "we removed/replaced something."
- [`phase12g-resumable-generation.md`](phase12g-resumable-generation.md) —
  narrow architectural sub-phase. Good shape reference for "small surgical
  change with one key insight."

Two of the three is usually enough. Skim, don't deep-read.

## 2. Required sections, in this order

| Section | One-line constraint |
|---|---|
| `## Scope` | Narrative paragraph. Name the *constraint* that shaped the work. End with test count + commit count. |
| `## What landed` (or `## Changes by file`) | Table: `File \| Role`. One phrase per row. Include deletions. Skip trivial edits. |
| `## Decisions (and why)` | Bulleted prose, 2–5 sentences each. **Bold the decision.** If a prior decision was reversed, say so explicitly and why. |
| `## What worked` | Concrete: name the file, library, or tool. Vague entries ("good communication") teach nothing. |
| `## What was tricky / less well` | Same rule. Name the library/browser/symptom so the next agent recognizes the trap. |
| `## Open issues / follow-ups` | Actionable with enough context to action later. Not bare TODOs. |
| `## Notes for future phases` | The section CLAUDE.md tells agents to read first. One bullet per durable lesson. |

## 3. Before closing the phase

- **Promote durable lessons** to [`../CONVENTIONS.md`](../CONVENTIONS.md).
  If a gotcha applies beyond this phase, it belongs there. Retros are
  history; conventions are reference.
- **Update [`PLAN.md`](../plans/PLAN.md)** if scope shifted, or add a
  per-phase plan in `docs/plans/phase<N>-*.md`.
- **Cross-link related retros inline** — "see Phase 7 §Decisions for the
  JSON reversal."

## 4. Anti-patterns

- Decisions as a table — cells can't hold the *why* + trade-off.
- Test counts framed as KPIs ("91 tests passing"). Use deltas in narrative:
  "grew from 63 → 91."
- Invented examples. Pull examples from the actual phase, not hypotheticals.
- Lead / Date / Next Steps boilerplate — no real retro here uses it.
- Generic phrases ("worked well", "had challenges"). If you can't name a
  file or symptom, the entry doesn't belong.

## 5. Where to save

`docs/retros/phase<N>-<short-slug>.md`. Lowercase, hyphenated slug.
