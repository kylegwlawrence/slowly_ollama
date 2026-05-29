# Physics: Mechanics to the Quantum Frontier

> **Worked example of [`COURSE_TEMPLATE.md`](COURSE_TEMPLATE.md).** Subject: undergraduate physics. Use it as a target shape when filling a course outline. Counts match the template (6 outcomes, 4 themes, 10-unit sequence, 4 connections, 3 after-course items). See [`STYLE_GUIDE.md`](STYLE_GUIDE.md) for conventions.

## Metadata

- **Total units**: 10
- **Total weeks**: ~33 weeks (Units 1–4: 3 weeks each; Unit 5: 6 weeks; Units 6–9: 3 weeks each; Unit 10: 3 weeks)
- **Estimated effort**: ~6–9 hours/week (3 sessions × 2–3 hours)
- **Prerequisites**: Single-variable calculus (derivatives, integrals, chain rule); algebra; basic vector notation (dot product, cross product, magnitude). No prior physics assumed.
- **Difficulty tier**: intro (Units 1–4) through frontier (Units 5–10)

## Overview

Physics is the study of how the universe behaves at its most fundamental level — from the motion of billiard balls to the decay of particles that existed for a fraction of a nanosecond after the Big Bang. This course traces the full arc of classical and modern physics as a self-study curriculum: it begins with the Newtonian framework that held for two centuries, pushes through thermodynamics and electromagnetism, and then breaks that framework apart with quantum mechanics. The second half builds on the wreckage — advanced quantum theory, Lagrangian and Hamiltonian mechanics, relativistic electrodynamics, statistical mechanics, and the current research frontier.

By the end, you will have encountered every major conceptual revolution in physics from Newton to the present. You will not be a specialist in any one area, but you will have the working vocabulary, mathematical tools, and physical intuition to read primary literature in most subfields and to identify which tools apply to a new problem. The course ends with a capstone research project in which you choose an open question at the frontier and conduct an original investigation.

The difficulty ramps sharply at Unit 4 (quantum mechanics) and again at Unit 5 (advanced quantum). The intro tier — Units 1–4 — is accessible to anyone comfortable with calculus and vectors. The advanced tier — Units 5–10 — assumes fluency with the entire intro tier and moves quickly. Expect the advanced units to require more re-reading and more problem-solving outside the session structure.

## Learning Outcomes

By the end of this course you should be able to:

- Derive the equations of motion for a classical system using Newtonian, Lagrangian, and Hamiltonian formulations, and compare the three approaches to select the most tractable for a given problem
- Apply conservation laws (energy, momentum, angular momentum, charge) across mechanics, thermodynamics, and electromagnetism to predict system behavior without integrating equations of motion
- Analyze the classical-to-quantum boundary: identify the conditions under which classical mechanics fails and explain how the quantum formalism recovers classical predictions in the appropriate limit
- Model a real physical system, specify its load-bearing idealizations, and predict how the model's output changes when each assumption is relaxed
- Interpret quantum phenomena (superposition, entanglement, decoherence, measurement) using the standard formalism, and argue for or against a named interpretation given the experimental evidence
- Compose a self-contained investigation of a frontier physics question, synthesizing theoretical tools, simulation results, and primary literature from at least three units of the course

## Core Themes

The 4 recurring ideas or tensions that run across all units. A theme is a recurring tension, question, or challenge — not a topic name. It should appear in multiple units in different forms.

- **Conservation as the primary problem-solving move**: every unit introduces a new arena (mechanics, heat, fields, quantum states) but the same bookkeeping principle recurs — find the conserved quantity, track it across the system boundary, and the answer follows; the frameworks change but the move does not
- **Idealization and its failure modes**: every model in this course rests on assumptions (frictionless surfaces, infinite heat reservoirs, point particles, classical paths) that eventually break down; recognizing where an idealization fails is as important as applying it correctly, and identifying the failure mode is often how physics advances
- **The classical limit as a constraint on quantum theory**: quantum mechanics must reproduce Newtonian results at macroscopic scales; this correspondence principle threads through every advanced unit and forces a recurring question — what does "large enough" actually mean, and why does it depend on the observable?
- **Symmetry generates physical law**: conservation laws are consequences of symmetry (Noether's theorem), Maxwell's equations follow from gauge symmetry, and selection rules in quantum mechanics trace back to symmetry of the Hamiltonian; this connection appears in Units 1, 3, 6, 7, and 5 and deepens each time

## Unit Sequence

**Format example for one row** (replace with content from your subject):

| Unit | File | Focus | Weeks | Key Capability Added |
|------|------|-------|-------|----------------------|
| 3 | [`unit3_thermodynamics.md`](unit3_thermodynamics/unit3_thermodynamics.md) | Energy, entropy, and the limits of heat engines | W7–W9 | Applying the first and second laws to predict the efficiency and failure modes of real heat engines |

Note: **Focus** is the unit's central concept or question in one line. **Key Capability Added** is what the learner can do after the unit that they could not do before — one clause, specific enough to test.

**This course's Unit Sequence:**

| Unit | File | Focus | Weeks | Key Capability Added |
|------|------|-------|-------|----------------------|
| 1 | [`unit1_mechanics.md`](unit1_mechanics/unit1_mechanics.md) | Kinematics, forces, and energy conservation | W1–W3 | Solving one- and two-dimensional motion problems using both force-based and energy-based approaches |
| 2 | [`unit2_thermodynamics.md`](unit2_thermodynamics/unit2_thermodynamics.md) | Heat, entropy, and the laws of thermodynamics | W4–W6 | Predicting the direction and limits of heat flow and mechanical work using the first and second laws |
| 3 | [`unit3_electromagnetism.md`](unit3_electromagnetism/unit3_electromagnetism.md) | Electric and magnetic fields, induction, and Maxwell's equations | W7–W9 | Computing field strengths and predicting induction and wave propagation using Maxwell's unified framework |
| 4 | [`unit4_quantum_mechanics.md`](unit4_quantum_mechanics/unit4_quantum_mechanics.md) | Wave-particle duality, the Schrödinger equation, and quantum measurement | W10–W12 | Setting up and solving the Schrödinger equation for simple systems and interpreting the probabilistic output |
| 5 | [`unit5_advanced_quantum.md`](unit5_advanced_quantum/unit5_advanced_quantum.md) | Quantum field theory, relativistic quantum mechanics, and entanglement | W13–W18 | Applying quantum field theoretic tools to particle interactions and analyzing entanglement as a physical resource |
| 6 | [`unit6_advanced_classical.md`](unit6_advanced_classical/unit6_advanced_classical.md) | Lagrangian and Hamiltonian mechanics, chaos theory, and special relativity | W19–W21 | Reformulating mechanics in the Lagrangian and Hamiltonian frameworks and identifying the onset of chaotic behavior |
| 7 | [`unit7_advanced_em.md`](unit7_advanced_em/unit7_advanced_em.md) | Relativistic electrodynamics and gauge symmetry | W22–W24 | Deriving Maxwell's equations from the requirements of special relativity and identifying gauge symmetry as the source of the electromagnetic force |
| 8 | [`unit8_statistical_mechanics.md`](unit8_statistical_mechanics/unit8_statistical_mechanics.md) | Statistical mechanics, partition functions, and quantum statistics | W25–W27 | Connecting microscopic particle behavior to macroscopic thermodynamic quantities via statistical ensembles |
| 9 | [`unit9_frontier.md`](unit9_frontier/unit9_frontier.md) | String theory, quantum cosmology, and quantum biology — the current frontier | W28–W30 | Evaluating speculative theories at the research frontier against the standard of established experimental evidence |
| 10 | [`unit10_capstone.md`](unit10_capstone/unit10_capstone.md) | Independent research project: scoping and executing a physics investigation | W31–W33 | Scoping, researching, and presenting an original investigation of an open physics question using tools from Units 5–9 |

## Course Arc

Ten units is the appropriate count because physics divides naturally into classical (Units 1–4) and modern (Units 5–10) domains, and within each domain there are conceptual jumps that cannot be merged without stranding the learner at a critical transition. Fewer units would force mechanics and thermodynamics into a single arc — each is deep enough to carry its own summative challenge and glossary. More units would fragment electromagnetism or statistical mechanics into pieces too small to support genuine integration.

The sequence follows a conceptual dependency chain. Unit 1 (mechanics) is the entry point because every later unit borrows its language: force, energy, the idea of a conserved quantity, and the habit of isolating a system and summing what crosses its boundary. Unit 2 (thermodynamics) extends energy to systems too large to track microscopically — it builds on Unit 1's conservation framework and introduces entropy as the new bookkeeping variable. Unit 3 (electromagnetism) introduces field theory, the mathematical language that Units 5, 7, and all quantum units rely on; it also delivers Maxwell's equations, which are the first hint that classical mechanics needs revision. Unit 4 is the pivot point of the entire course: quantum mechanics breaks the classical assumptions built in Units 1–3 and opens every unit that follows. Without Unit 4 landing, nothing in Units 5–9 is accessible — the formalism (wavefunctions, operators, probability amplitudes) is assumed throughout.

Units 5–9 then push into the advanced tier in a deliberate order: Unit 5 deepens quantum theory before Units 6 and 7 revisit classical mechanics and electromagnetism in modern dress (so the learner can see what the Lagrangian and gauge-invariance frameworks were quietly pointing at all along). Unit 8 (statistical mechanics) bridges Units 2 and 5 — it requires both thermodynamic intuition and quantum statistics. Unit 9 (the frontier) comes last because it requires the learner to hold all the advanced tools simultaneously and assess which apply to speculative theories. Unit 10 (capstone) cannot be placed anywhere else; it requires the full toolkit.

## Resources

Major resources that span multiple units. Per-unit and per-week resources belong in the unit and week files.

- `[verified]` [MIT OpenCourseWare — Physics](https://ocw.mit.edu/courses/#physics) — lecture notes, problem sets, and exams for 8.01 (mechanics), 8.02 (electromagnetism), and 8.04–8.06 (quantum); covers Units 1–5 in depth
- `[verified]` [PhET Interactive Simulations](https://phet.colorado.edu) — browser-based simulations for mechanics, thermodynamics, electromagnetism, and quantum units; no setup required
- `[unverified]` *The Feynman Lectures on Physics* (feynmanlectures.caltech.edu) — three-volume text covering all classical and quantum content in Units 1–8; widely considered the most readable unified treatment of undergraduate physics
- `[unverified]` *Resnick, Halliday & Krane, Physics Vol. 1 & 2* — standard reference for Units 1–4; verify the edition covers the chapters needed for thermodynamics and electromagnetism before relying on it

## Real-World Connections

Where the full course — not a single unit — shows up in practice, research, or daily life.

- **Semiconductor design**: integrated circuits, transistors, and solid-state devices are designed using quantum mechanics (Unit 4), statistical mechanics (Unit 8), and electromagnetic field theory (Unit 3); no single unit provides the full picture — device physics requires all three simultaneously
- **Medical imaging**: MRI uses nuclear magnetic resonance (Units 4–5), PET scans rely on particle-antiparticle annihilation (Unit 5), and X-ray CT uses classical electromagnetism (Unit 3); a medical physicist interpreting scanner output needs the tools from at least four units of this course
- **GPS accuracy**: satellite clocks run fast by ~38 microseconds per day due to special-relativistic (Unit 6) and general-relativistic effects (beyond this course); without the correction, positional error accumulates to ~10 km/day — the most widely-used navigation system in the world depends on a relativistic correction derived from Units 1 and 6
- **Climate and energy systems**: thermodynamic efficiency limits (Unit 2), electromagnetic energy transmission (Unit 3), and statistical-mechanical models of molecular and atmospheric behavior (Unit 8) all intersect in the physics of renewable energy conversion, grid design, and climate modeling

## After This Course

Where to go once the Unit 10 capstone is complete. Three opinionated destinations.

- **Next course**: Graduate Classical Mechanics using Goldstein's *Classical Mechanics* or Graduate Quantum Mechanics using Sakurai's *Modern Quantum Mechanics*, depending on whether your frontier interest runs toward field theory or condensed matter — both assume exactly the toolkit this course builds
- **Key text or project**: implement a quantum circuit simulator in Python using Qiskit (qiskit.org), applying the formalism from Units 4 and 5 to a real quantum computing problem; the Qiskit textbook provides worked examples that assume Unit 4-level quantum mechanics and scale up from there
- **Applied context**: condensed matter physics research groups, quantum computing laboratories (IBM Quantum, Google Quantum AI, national labs), or experimental particle physics collaborations — all require the ability to move between the classical, thermodynamic, and quantum frameworks that this course builds across its ten units
