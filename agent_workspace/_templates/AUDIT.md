# Physics Lessons Audit

Per-file audit of all 45 existing outlines in `agent_workspace/physics-lessons/outlines/` against the new templates in this directory.

## How to read this audit

- **Conforms?** uses three states:
  - `✓` — close to template; only minor edits needed
  - `◐` — partial; some sections fit, others need restructuring
  - `✗` — structurally different; full restructuring needed
- **Action** is one of:
  - `keep` — basically conforms; no work
  - `light edit` — fix specific minor issues (drop classroom sections, fix headings, etc.)
  - `restructure` — significant rewrite under new template
  - `consolidate` — duplicate; merge into one
  - `flag` — concrete bug to fix
- This audit lists *what* needs doing per file. The order of *what to do first* is up to you. A suggested triage order is in [Recommended cleanup order](#recommended-cleanup-order) at the bottom.

## Fix log

| Date | Fix | Status |
|------|-----|--------|
| 2026-05-28 | Bug #1 partial: deleted duplicate `week31_research_methodology.md` (flat-list version). Survivor `week31_research.md` still needs restructure under `CAPSTONE_WEEK_TEMPLATE.md`. | ✓ partial |
| 2026-05-28 | Bug #2: restructured `week1_kinematics.md` from 5-day to 3-session under `WEEK_TEMPLATE.md`. | ✓ done |
| 2026-05-28 | Bug #3: self-referential footer removed via Bug #2 rewrite. | ✓ done |
| 2026-05-28 | Bug #14: renamed `week18_quantum_thermodynamics.md` → `week18_quantum_thermo_beyond.md` (H1 was already distinct from W27). | ✓ done |
| 2026-05-28 | Bug #9: verified 15 suspect URLs via WebFetch. All are unusable as simulators (see Bug #9 detail). | ✓ done |
| 2026-05-28 | Step 3: capstone restructured. Wrote `week31_proposal.md`, `week32_execution.md`, `week33_submission.md`, and new `unit10_capstone_project.md` under capstone+unit templates. Deleted superseded `week31_research.md`, `week32_capstone_execution.md`, `week33_capstone_final.md`. Bug #1 fully resolved; bugs #12, #13 resolved (capstone weeks now have rubrics; U10 references U5–9 explicitly). | ✓ done |
| 2026-05-28 | Step 4: restructured all 10 unit files under `UNIT_TEMPLATE.md` (thin overviews). Renamed: `unit1_mechanics_3weeks.md` → `unit1_mechanics.md`, similar for U2–U6. U7–U9 kept their existing names. Each new file has Overview, Learning Outcomes, Key Concepts, Weekly Sequence (linked TOC), Glossary, Tools & Resources (with `[verified]` tagging), Real-World Connections, Unit Self-Assessment, Going Deeper. Pedagogical Approach + Differentiation dropped. Also fixed `week1_kinematics.md` parent-unit link. Note: U7 file previously listed a stray Week 25 that overlapped with U8 — dropped from new U7. Bugs #5, #6, #15 resolved. | ✓ done |
| 2026-05-28 | Step 5: restructured all 18 advanced-tier week files (W13–W30) under `WEEK_TEMPLATE.md`. All converted from 3-day classroom format to 3-session self-study format. Differentiation, Debate, Science Fair extensions, classroom hooks dropped. Key Terms added. Suspect URLs replaced with verified alternatives. Heading hierarchy normalized to H2/H3. W18 already renamed (Bug #14). Bug #4 (heading hierarchy break at W7) now resolved for W13+. | ✓ done |
| 2026-05-28 | Step 6: restructured all 11 intro-tier week files (W2–W12) under `WEEK_TEMPLATE.md`. Same conversion pattern as Step 5. Heading hierarchy break at W7 fully resolved. Recycled "career applications" prompt retired across W4/W5/W6 (Bug #11). Prerequisite gaps explicitly flagged for W10 (probability) and W12 (tensor products) per Bug #8. Vague "understand X" objectives replaced with action-verb outcomes (Bug #10). Self-referential footers removed across W1–W6. Bugs #4, #8, #10, #11 fully resolved. All 45 outline files now conform to the new templates. | ✓ done |

## Known concrete bugs

High-confidence issues called out specifically. Each is referenced by number from the audit tables below.

1. **Duplicate Week 31** — `week31_research_methodology.md` (46 lines, flat 7-section topic list) and `week31_research.md` (114 lines, 3-day retrofit) describe the same week. Consolidate: take the richer file's content, rewrite under `CAPSTONE_WEEK_TEMPLATE.md` as a milestone-based **Proposal** week. Delete the flat-list version. **✓ Partial — duplicate deleted on 2026-05-28; survivor `week31_research.md` still pending restructure under capstone template.**

2. **`week1_kinematics.md` was a 5-day outlier** — every other week in the corpus is 3 days. Either conform to 3 sessions under the new template, or accept as an intentional extended onboarding week (in which case rename to make the duration visible). **✓ Resolved on 2026-05-28 — restructured to 3 sessions under `WEEK_TEMPLATE.md`.**

3. **Self-referential footer in `week1_kinematics.md` line 171** — `"This file is saved as Week1_kinematics.md with the same structure as Week2_forces.md, Week3_energy.md, and Week4_thermodynamics.md."` Agent meta-note that leaked into the output. Delete unconditionally. **✓ Resolved on 2026-05-28 — gone via Bug #2 rewrite.**

4. **Heading hierarchy break at Week 7** — `week1`–`week6` use `#### Day N` for day headers; `week7`–`week30` use `## Day N`. Normalize everywhere to `## Session N` (H2) under the new template.

5. **Unit "Pedagogical Approach" appears only in U6+** — added mid-stream and never backfilled. Dropped entirely under the new UNIT_TEMPLATE.

6. **Unit 7 missing Differentiation** while Units 6, 8, 9 have it — moot under the new template (Differentiation is dropped wholesale for self-study), but evidence of template drift.

7. **Week numbering inconsistency** — Units 1–4 restart W1–3 per unit; Units 5–10 use global W13–33. The Unit 5–10 proposal calls Unit 8's first week "Week 25", which matches the actual file — good — but Unit 1–4's per-unit numbering means there's no canonical "Week 4" outside Unit 2. Normalize to global numbering everywhere.

8. **Prerequisite chain incoherence**:
   - `week10_wave_particle_duality.md` lists "Probability Basics" as a prerequisite. No prior week formally teaches probability. Add a Prerequisite gap note pointing to an external probability primer.
   - `week12_entanglement_superposition.md` assumes Dirac notation `|0⟩`, `|1⟩` from W11, but W11 uses it without teaching it. Either backfill a notation-introduction session in W11 or add a Prerequisite gap note in W12.
   - `week7_electric_forces.md` solves Coulomb problems with `F=ma` but doesn't reference Unit 1 mechanics. List Unit 1 (Mechanics) as a prerequisite.

9. **Suspect tool URLs** — **✓ Verified on 2026-05-28 via WebFetch.** All 15 checked URLs are unusable as simulators; replace with PhET, IBM Quantum, or `{{find a sim for: TOPIC}}` placeholder per STYLE_GUIDE.

   **Fabricated (DNS doesn't resolve, ECONNREFUSED)** — 10 domains:
   - `bose-einstein-condensate.com` (W26, U8)
   - `feynmanpathintegrals.com` and sub-pages (W14, W22, W23, W25; U5, U7, U8)
   - `quantuminterpreters.org` (W13)
   - `quantumdecay.org` (W13, W18)
   - `stringtheory.org` (W28)
   - `topological-quantum-computing.com` (W27, U8)
   - `quantum-simulator.com` (W16)
   - `cosmology-simulator.com` (W29)
   - `plasma-simulator.com` (W24)
   - `quantummetrology.org` (W18)

   **Exists but NOT a simulator** — 3 domains:
   - `quantumthermodynamics.org` (W18, W27, U8) — academic researcher's static page (Gian Paolo Beretta, unibs.it). No interactive sim.
   - `quantumbiology.org` (W30, U9) — Quantum Biology Institute non-profit informational site. No interactive sim.
   - `relativitycalculator.com` (W21) — repurposed as a Korean link-aggregation site. Unrelated to physics.

   **Ambiguous specific paths** — likely fabricated:
   - `desmos.com/calculator/chaos` (W20) and `desmos.com/calculator/` Lagrangian sub-path (W19) — Desmos main is real, but published graphs use random IDs like `/abc123def`, not semantic names. Treat as fabricated.
   - `demos.wolframcloud.com/QuantumBellTest/` (W13) — returns 403; can't confirm. Treat as unverified.

   **Verified-OK references in the corpus** — keep these:
   - PhET (`phet.colorado.edu`)
   - IBM Quantum (`quantum-computing.ibm.com`)
   - arXiv (`arxiv.org`)
   - Wolfram Alpha main (`wolframalpha.com`) — but not specific demo sub-paths
   - Qiskit (`qiskit.org`)
   - GitHub, Google Scholar, ResearchGate (research workflow tools, not simulators)
   - Python libraries (NumPy, Matplotlib, SciPy) — packages, not URLs

10. **Vague "understand X" objectives** — replace with action verbs.
    - `week2_forces.md` Day 1 Objective: "Understand Newton's First Law..."
    - `week10_wave_particle_duality.md` Day 2 Objective: similar phrasing
    - `unit1_mechanics_3weeks.md` Overview Objectives: 2 of 4 begin with "Understand..."

11. **Recycled career-applications homework** — "How does [thermodynamic concept] apply to a career you're interested in?" appears in `week4_intro_thermodynamics.md`, `week5_thermodynamics_laws_1_and_2.md`, and `week6_thermodynamics_second_law.md`. Replace with varied synthesis prompts (see `STYLE_GUIDE.md`).

12. **Capstone units have no rubrics** — assessments are listed by name only ("Lab Reports", "Final project report and GitHub repository") with zero criteria. The new `CAPSTONE_WEEK_TEMPLATE.md` includes an explicit 4-criterion self-assessment rubric.

13. **Capstone has no backward refs to U5–9 content** — capstone weeks don't point at which advanced-unit physics they expect you to build on. Either Unit 10 overview adds a "How this draws on Units 5–9" section, or each capstone week names candidate physics topics.

14. **W18 and W27 filename collision** — both used `*_quantum_thermodynamics.md` suffix. H1 titles were already distinct (W18 "and Beyond", W27 "Thermodynamics in Quantum Systems"). **✓ Resolved on 2026-05-28 — `week18_quantum_thermodynamics.md` renamed to `week18_quantum_thermo_beyond.md` to match its H1.**

15. **Unit 1 lists `Week4_thermodynamics.md` in the W1 footer** but Unit 2 (which holds Week 4) actually has `week4_intro_thermodynamics.md`. Minor — naming drift between what the agent planned and what it created.

## Unit files (10)

| File | Conforms? | Issues | Action |
|------|-----------|--------|--------|
| `unit1_mechanics_3weeks.md` | ✗ | Repeats week content verbatim (each "Week N" section duplicates the matching week file's days). Missing Glossary. "Files to Save" footer. Classroom Assessment/Differentiation sections. "Understand X" objectives (bug #10). Filename embeds duration ("_3weeks") — STYLE_GUIDE wants `unit1_mechanics.md`. | restructure |
| `unit2_thermodynamics_3weeks.md` | ✗ | Same shape issues as U1. Has "Why This Sequence?" rationale which is good — preserve as part of new Overview. | restructure |
| `unit3_electromagnetism_3weeks.md` | ✗ | Same as U1. Missing "Files to Save" (unlike U1/U2) — minor drift. | restructure |
| `unit4_quantum_mechanics_3weeks.md` | ✗ | Same as U1. | restructure |
| `unit5_advanced_quantum_mechanics_6weeks.md` | ◐ | Richer structure than U1–4: Overview, Objectives, Logical Sequence (with weekly Topics/Activities embedded — still duplicating week content), Pedagogical Approach (drop), Assessment, Differentiation (drop), Extensions, Tools table. Add Glossary; replace Logical Sequence's embedded weekly detail with a Weekly Sequence link table. | restructure |
| `unit6_advanced_classical_mechanics_3weeks.md` | ◐ | Same as U5. Filename embeds duration — strip. | restructure |
| `unit7_electromagnetism_advanced.md` | ◐ | Same as U5/U6. Missing Differentiation (bug #6 — moot under new template) and missing Prerequisites — must add. | restructure |
| `unit8_statistical_mechanics.md` | ◐ | Same as U5. Best example of the U5–10 unit pattern; closest to what a thin restructure needs to strip. | restructure |
| `unit9_physics_frontier.md` | ◐ | Same as U5. | restructure |
| `unit10_capstone_project.md` | ✗ | Treats capstone as a regular unit. Per-week sections embed topics/activities (and re-state what duplicate Week 31 files attempt). No rubric (bug #12). No backward references to U5–9 content (bug #13). Restructure under the new UNIT_TEMPLATE but ALSO with capstone-specific Overview noting which prior physics each milestone draws on. | restructure |

## Proposal file (1)

| File | Conforms? | Issues | Action |
|------|-----------|--------|--------|
| `unit5-10_proposal.md` | n/a | Predecessor draft to the individual U5–10 unit files. Useful as a historical record of the curriculum plan. Now superseded by the individual unit files. | keep as historical (or archive into a `_archive/` sub-folder if you want to declutter) |

## Week files: intro tier (W1–W12)

Common issues across this band (cited once here, referenced in rows):

- **(A)** Classroom "Hook" framing in Engagement step ("Show students a video and ask…") — reframe as solo warm-up reading
- **(B)** Differentiation section (Struggling/Advanced) — drop under new template
- **(C)** Extensions includes Debate + Science Fair as group activities — drop Science Fair, convert Debate to solo "argue both sides" in Going Deeper
- **(D)** Missing Key Terms section — add 5–10 week-specific terms
- **(E)** Classroom Assessment framing (formal quiz, lab report grading) — convert to self-assessment problems + reflection
- **(F)** Heading: `#### Day N` (W1–6) or `## Day N` (W7–12). Normalize to `## Session N`.

| File | Conforms? | Issues | Action |
|------|-----------|--------|--------|
| `week1_kinematics.md` | ✓ | **Resolved 2026-05-28** — restructured to 3 sessions under `WEEK_TEMPLATE.md`; footer removed; "Materials Needed" dropped; 5-day outlier eliminated. | done |
| `week2_forces.md` | ◐ | A, B, C, D, E, F (`####`). **"Understand inertia" objective** Day 1 (bug #10). | restructure |
| `week3_energy.md` | ◐ | A, B, C, D, E, F (`####`). Solid concept progression (pendulum → KE/PE → conservation → roller-coaster project) — preserve this in the restructure. | restructure |
| `week4_intro_thermodynamics.md` | ◐ | A, B, C, D, E, F (`####`). **Recycled career-applications homework** (bug #11). | restructure |
| `week5_thermodynamics_laws_1_and_2.md` | ◐ | A, B, C, D, E, F (`####`). Recycled career prompt (bug #11). Math notation density spikes at entropy without scaffolding. | restructure |
| `week6_thermodynamics_second_law.md` | ◐ | A, B, C, D, E, F (`####`). Recycled career prompt (bug #11). 159 lines — longest in intro tier; topic creep into free energy/biology. Tighten on restructure. | restructure |
| `week7_electric_forces.md` | ◐ | A, B, C, D, E. **Heading shifts to `## Day N`** here (bug #4). No reference to Unit 1 mechanics despite F=ma usage (bug #8). | restructure |
| `week8_magnetic_forces.md` | ◐ | A, B, C, D, E. | restructure |
| `week9_electromagnetic_induction.md` | ◐ | A, B, C, D, E. Maxwell's equations introduced with differential notation without prior calculus bridge. | restructure |
| `week10_wave_particle_duality.md` | ◐ | A, B, C, D, E. **Prereq "Probability Basics" not covered upstream** (bug #8). **"Understand X" objective** Day 2 (bug #10). | restructure + flag #8 |
| `week11_quantum_basics.md` | ◐ | A, B, C, D, E. Uses Dirac notation without teaching it. | restructure |
| `week12_entanglement_superposition.md` | ◐ | A, B, C, D, E. **Assumes Dirac notation from W11 which W11 doesn't teach** (bug #8). Jumps to Bell's theorem with no scaffolding. | restructure + flag #8 |

## Week files: advanced tier (W13–W30)

This band is the strongest in the corpus — the agent applied its template consistently. Same recurring issues (A–E above; F doesn't apply because W13+ already use `## Day N` — just rename Day → Session).

| File | Conforms? | Issues | Action |
|------|-----------|--------|--------|
| `week13_quantum_measurement.md` | ✓ | A, B, C, D, E. Suspect URLs: `quantuminterpreters.org`, `quantumdecay.org`, Wolfram Bell Test demo (bug #9). **Closest existing file to the new WEEK_TEMPLATE**; useful as the reference when reshaping the template into actual outlines. | light edit |
| `week14_qft.md` | ✓ | A, B, C, D, E. Suspect URL `feynmanpathintegrals.com` (bug #9). | light edit |
| `week15_relativistic_quantum_mechanics.md` | ✓ | A, B, C, D, E. Dirac equation introduced with heavy notation — consider adding a math-bridge note in Concept Study. | light edit |
| `week16_quantum_entanglement.md` | ✓ | A, B, C, D, E. Suspect URL `quantum-simulator.com` (bug #9). | light edit |
| `week17_quantum_computing.md` | ✓ | A, B, C, D, E. IBM Quantum and Qiskit URLs verified-OK; keep. | light edit |
| `week18_quantum_thermo_beyond.md` | ✓ | A, B, C, D, E. Suspect URL `quantumthermodynamics.org` (bug #9). **Filename collision with W27 resolved 2026-05-28** — renamed from `week18_quantum_thermodynamics.md`. | light edit |
| `week19_lagrangian_hamiltonian_mechanics.md` | ✓ | A, B, C, D, E. Suspect `desmos.com/calculator/` Lagrangian sub-path (bug #9). | light edit |
| `week20_chaos_theory.md` | ✓ | A, B, C, D, E. Suspect `desmos.com/calculator/chaos` sub-path (bug #9). | light edit |
| `week21_relativistic_mechanics.md` | ✓ | A, B, C, D, E. Suspect `relativitycalculator.com` (bug #9). | light edit |
| `week22_relativistic_electromagnetism.md` | ✓ | A, B, C, D, E. Suspect `feynmanpathintegrals.com/maxwells-equations.html` (bug #9). LHC reference is fine. | light edit |
| `week23_qed_precision.md` | ✓ | A, B, C, D, E. Suspect `feynmanpathintegrals.com/qed.html` (bug #9). | light edit |
| `week24_plasmas_high_energy.md` | ✓ | A, B, C, D, E. Suspect `plasma-simulator.com` (bug #9). | light edit |
| `week25_statistical_mechanics_basics.md` | ✓ | A, B, C, D, E. Suspect `feynmanpathintegrals.com/statistical-mechanics.html` (bug #9). | light edit |
| `week26_quantum_statistics.md` | ✓ | A, B, C, D, E. Suspect `bose-einstein-condensate.com` (bug #9). | light edit |
| `week27_quantum_thermodynamics.md` | ✓ | A, B, C, D, E. Title collides with W18 (bug #14). Suspect `quantumthermodynamics.org` (bug #9). | light edit + flag #14 |
| `week28_string_theory.md` | ✓ | A, B, C, D, E. Suspect `stringtheory.org` (bug #9). | light edit |
| `week29_quantum_cosmology.md` | ✓ | A, B, C, D, E. Suspect `cosmology-simulator.com` (bug #9). | light edit |
| `week30_quantum_biology.md` | ✓ | A, B, C, D, E. Suspect `quantumbiology.org` (bug #9). | light edit |

## Week files: capstone tier (W31–W33)

Doesn't match either canonical template — uses the new `CAPSTONE_WEEK_TEMPLATE.md`.

| File | Conforms? | Issues | Action |
|------|-----------|--------|--------|
| ~~`week31_research_methodology.md`~~ | ✓ | **Deleted 2026-05-28** as duplicate of `week31_research.md`. | done |
| `week31_research.md` | ✗ | Duplicate cleanup done (bug #1 partial). Still needs restructure: 114 lines, retrofitted into 3-day classroom format; should be milestone-based under capstone template. No rubric. | restructure under `CAPSTONE_WEEK_TEMPLATE.md` (milestone: Proposal); rename to `week31_proposal.md` per STYLE_GUIDE. |
| `week32_capstone_execution.md` | ✗ | Milestone-based structure (right direction) but no rubric (bug #12). Time vague. Assessment by name only. Drop classroom phrasing. | restructure under `CAPSTONE_WEEK_TEMPLATE.md` (milestone: Execution); rename to `week32_execution.md`. |
| `week33_capstone_final.md` | ✗ | Same as W32 — no rubric, classroom phrasing in places. | restructure under `CAPSTONE_WEEK_TEMPLATE.md` (milestone: Submission); rename to `week33_submission.md`. |

## Workspace framing docs (not outlines)

These live at the workspace root, not in `outlines/`. They're framing material, not lesson plans. Out of scope for the templates plan, but noted here for completeness.

| File | Notes |
|------|-------|
| `physics_description.md` | High-level "what is physics" framing for the curriculum's audience. Keep as a top-level intro. Could optionally feed Unit 1's Overview. |
| `real_world_applications.md` | Cross-curriculum applications doc. Could feed the **Real-World Connections** sections of unit files (especially U1 mechanics, U3 electromagnetism, U4 quantum). Keep as a reference. |

## Recommended cleanup order

If you tackle this incrementally, suggested triage from highest leverage to lowest:

1. **Fix the named bugs first** (bugs #1, #2, #3, #14): duplicate Week 31, Week 1 outlier, self-referential footer, W18/W27 title collision. These are concrete, isolated, and discoverable.
2. **Verify the suspect URL list** (bug #9). Visit each domain; mark verified URLs in a sweep across all advanced-tier weeks. Where a sim doesn't exist, replace with `{{find a sim for: TOPIC}}` placeholder per STYLE_GUIDE.
3. **Restructure the capstone** (W31–W33 + U10): high-leverage because these are most divergent and used at the very end of the curriculum, so getting them right matters for the project capstone arc.
4. **Restructure unit overviews** under `UNIT_TEMPLATE.md` (10 files): biggest content gain because they currently duplicate week content and lack glossaries. Once thinned, the corpus becomes much easier to skim.
5. **Light-edit the advanced-tier weeks** (W13–W30, 18 files): mostly just drop classroom apparatus, add Key Terms, rename Day → Session. Mechanical.
6. **Restructure the intro-tier weeks** (W1–W12, 12 files): same drops + add Key Terms + fix prereq chains (bug #8) + retire recycled career prompt (bug #11). Higher effort because of the heading-hierarchy fix and prereq mismatches.

Steps 1–2 are quick wins. Step 3 unlocks confidence in the capstone target. Steps 4–6 are the bulk content work.
