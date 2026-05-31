# Self-Study Physics Degree: Classical Foundations to Quantum Theory

> **Worked example of [`DEGREE_TEMPLATE.md`](DEGREE_TEMPLATE.md).** Subject: an undergraduate-level physics program decomposed into 5 self-study courses. Use it as a target shape when filling a degree outline. Counts match the template (6 outcomes, 4 themes, 5-course sequence, 4 connections, 3 after-degree items). See [`STYLE_GUIDE.md`](STYLE_GUIDE.md) for conventions. Note: this is a self-study program, not an accredited credential.

## Metadata

- **Total courses**: 5
- **Total weeks**: ~80 weeks (Course 1: 15w; Course 2: 24w; Course 3: 15w; Course 4: 18w; Course 5 capstone: 8w)
- **Estimated effort**: ~6–9 hours/week (3 sessions × 2–3 hours per active course)
- **Prerequisites for entering the program**: Single-variable calculus (derivatives, integrals, chain rule); high-school algebra and trigonometry; the ability to commit ~6–9 hours/week for roughly two years. No prior physics assumed.
- **Difficulty tier reached**: frontier (Courses 4 and 5)

## Overview

This program teaches you to think like a physicist. Not by surveying a long list of topics, but by working through the four mathematical and conceptual frameworks — classical mechanics, electromagnetism, statistical mechanics, and quantum mechanics — that together describe almost every phenomenon physicists study. It exists as a coherent degree, rather than a single course, because each framework is deep enough to require its own sustained arc, and because the capstone needs you to hold all four in mind simultaneously to do anything original.

You begin with mathematical foundations, learning to translate physical questions into equations and solve them. You then build classical intuition in mechanics and electromagnetism, extend that intuition to thermodynamic and statistical systems, and finally break it apart in quantum mechanics. The program ends with an independent thesis: you pick an open question at the boundary of two or more frameworks and conduct an original investigation. By the end you cannot claim a specialist's depth in any one area, but you can read primary literature across most of physics, identify which framework applies to a new problem, and produce a self-contained piece of research.

Difficulty ramps gently through Courses 1, 2, and 3, then jumps sharply at Course 4. Quantum mechanics is the wall: it requires you to abandon assumptions about position, trajectory, and determinism that the first three courses spent ~54 weeks reinforcing. Expect Course 4 to take more re-reading, more problem-solving outside the session structure, and more time per concept than any earlier course.

## Program Outcomes

By the end of this degree you should be able to:

- Translate a physical problem stated in plain language into the appropriate mathematical formalism — vector calculus, linear algebra, or partial differential equations — and solve it using techniques from at least two courses in the program
- Apply conservation laws (energy, momentum, angular momentum, charge) across classical, statistical, and quantum domains to predict the behavior of a coupled system without integrating its full equations of motion
- Analyze the classical-to-quantum boundary: identify the conditions under which classical mechanics fails for a given system and explain how the quantum formalism recovers classical predictions in the appropriate limit
- Connect microscopic particle behavior to macroscopic thermodynamic observables using statistical ensembles informed by quantum statistics
- Critique a published physics paper by separately identifying the mathematical assumptions, the physical idealizations, and the quantum corrections it relies on, and assess whether each is load-bearing for its conclusions
- Compose a research-grade investigation of an open physics question, integrating mathematical methods, theoretical frameworks, and primary literature from at least three prior courses

## Core Themes

The 4 recurring ideas or tensions that run across all courses. A theme is a recurring tension, question, or challenge — not a topic name. It should appear in at least three of the courses in different forms.

- **Mathematics as the language of physics**: every course recasts physical intuition into a new mathematical formalism — vector calculus and ODEs in Course 1, Lagrangian mechanics and Maxwell's equations in Course 2, partition functions in Course 3, Hilbert spaces and operators in Course 4 — and progress in physics often arrives precisely when the right formalism shows up
- **Conservation as the primary problem-solving move**: the same bookkeeping principle (energy, momentum, charge, probability amplitude) recurs across classical, thermal, and quantum domains; the frameworks change but the move does not — find the conserved quantity, track it across the system boundary, and the answer follows
- **Idealization and its failure modes**: every model in this program rests on assumptions — frictionless surfaces, infinite heat reservoirs, point particles, classical paths — that eventually break down; recognizing where an idealization fails is as important as applying it correctly, and identifying the failure mode is often how physics advances
- **The classical limit constrains every modern theory**: quantum mechanics must reproduce Newtonian results at macroscopic scales (Courses 2 and 4), and statistical mechanics must reduce to ordinary thermodynamics in the large-N limit (Course 3); this correspondence principle threads through the program and forces a recurring question — what does "large enough" actually mean, and why does it depend on the observable?

## Course Sequence

**Format example for one row** (replace with content from your subject):

| Course | File | Focus | Weeks | Key Capability Added |
|--------|------|-------|-------|----------------------|
| 2 | [`course2_quantum/COURSE.md`](course2_quantum/COURSE.md) | Quantum mechanics from wavefunctions to entanglement | W55–W72 | Setting up and interpreting the Schrödinger equation for non-trivial systems and analyzing quantum measurement |

Note: **Focus** is the course's central concept or question in one line. **Key Capability Added** is what the learner can do after the course that they could not before — one clause, specific enough to test. The **final row is always the capstone course**.

**This degree's Course Sequence:**

| Course | File | Focus | Weeks | Key Capability Added |
|--------|------|-------|-------|----------------------|
| 1 | [`course1_math_foundations/COURSE.md`](course1_math_foundations/COURSE.md) | Calculus, linear algebra, and differential equations applied to physical problems | W1–W15 | Translating physical problems into mathematical equations and solving them with the appropriate analytical or numerical method |
| 2 | [`course2_classical_em/COURSE.md`](course2_classical_em/COURSE.md) | Classical mechanics (Newton, Lagrange, Hamilton) and Maxwell's electromagnetism | W16–W39 | Deriving equations of motion in three formulations, computing electric and magnetic fields, and identifying which formulation a given problem suits |
| 3 | [`course3_thermal_stat/COURSE.md`](course3_thermal_stat/COURSE.md) | Thermodynamics, entropy, and statistical mechanics | W40–W54 | Predicting heat flow, work, and equilibrium using both the macroscopic laws and the microscopic ensemble formalism |
| 4 | [`course4_quantum/COURSE.md`](course4_quantum/COURSE.md) | Quantum mechanics from wavefunctions to entanglement and decoherence | W55–W72 | Setting up the Schrödinger equation for non-trivial systems and analyzing quantum measurement, superposition, and entanglement |
| 5 | [`course5_capstone/COURSE.md`](course5_capstone/COURSE.md) | Capstone: independent research thesis on an open physics question | W73–W80 | Producing an original investigation integrating mathematical methods, a classical or thermal framework, and quantum theory |

## Program Arc

Five courses is the right count because physics decomposes naturally into four mathematical and conceptual domains — math methods, classical physics (mechanics + electromagnetism), statistical physics, and quantum physics — plus a capstone that integrates them. Fewer than five would force two of those domains into a single course and starve each of room to land properly: combining mechanics with quantum, for instance, skips the classical intuition the quantum framework is defined to extend. More than five would split mechanics from electromagnetism (which share the Lagrangian and field-theoretic language and benefit from being learned together) or split introductory from advanced quantum (which deepens through a single sustained 18-week arc and would lose coherence if interrupted).

The sequence follows a strict dependency chain. Course 1 (Math Foundations) is the entry point because every later course assumes fluency with calculus, linear algebra, and differential equations applied to physical problems; without it, the Lagrangian formalism in Course 2 and the operator algebra in Course 4 are inaccessible. Course 2 (Classical Mechanics & E&M) is the pivot point of the entire degree: it introduces force, energy, the conserved-quantity framework, and field theory — the language that Courses 3 and 4 both extend. If you removed Course 2, neither Course 3's energy bookkeeping nor Course 4's Hamiltonian operator would land. Course 3 (Thermal & Stat Mech) builds on Course 2's energy framework and introduces entropy as a new bookkeeping variable; it is placed before Course 4 to give you a chance to consolidate classical intuition before encountering the quantum break.

Course 5 (Capstone) cannot be placed anywhere else: it requires the full toolkit. The capstone draws explicitly on Courses 2, 3, and 4 — a research thesis on a physics question that touches only one framework would not exercise the breadth this degree builds, so the capstone unit's evaluation rubric requires you to name at least three prior courses your thesis draws on.

## Resources

Major resources that span multiple courses. Per-course resources belong in each course's `COURSE.md`, not here.

- `[verified]` [MIT OpenCourseWare — Physics](https://ocw.mit.edu/courses/#physics) — lecture notes, problem sets, and exams for 18.01–18.03 (Course 1), 8.01 (Course 2 mechanics), 8.02 (Course 2 E&M), 8.044 (Course 3), and 8.04–8.06 (Course 4); covers Courses 1–4 in depth
- `[verified]` [PhET Interactive Simulations](https://phet.colorado.edu) — browser-based simulations for mechanics, electromagnetism, thermodynamics, and quantum systems; used across Courses 2, 3, and 4 for solo Active Engagement weeks
- `[unverified]` *The Feynman Lectures on Physics* (feynmanlectures.caltech.edu) — three-volume narrative text covering all classical and quantum content; read alongside Courses 2, 3, and 4 as a unifying voice
- `[verified]` [arXiv physics archive](https://arxiv.org) — primary literature for the Course 5 capstone, also used in advanced Course 4 units; the single most important source for original research at the frontier
- `[verified]` [Wolfram Alpha](https://www.wolframalpha.com) — symbolic computation for Course 1 calculus and ODEs and for Course 2 mechanics derivations; useful as a check on hand-derived results

## Real-World Connections

Where the full program — not a single course — shows up in practice, research, or daily life.

- **Semiconductor and chip design**: integrated circuits, transistors, and solid-state memory are designed using quantum mechanics (Course 4), statistical mechanics (Course 3), and electromagnetic field theory (Course 2) simultaneously; no single course gives the whole picture — device physics requires all three at once, plus the math methods of Course 1 to model carrier transport
- **Medical imaging**: MRI uses quantum nuclear magnetic resonance (Course 4), PET scans rely on particle-antiparticle annihilation (Course 4), CT uses classical electromagnetism for X-ray attenuation (Course 2), and dose calculations use statistical models (Course 3); a medical physicist interpreting scanner output needs the tools from at least four courses of this program
- **GPS, satellites, and space mission planning**: satellite orbits are computed with classical mechanics and small relativistic corrections (Course 2), atmospheric drag and thermal balance use thermodynamic and statistical models (Course 3), and onboard atomic clocks rely on quantum transitions in cesium or rubidium (Course 4); the most widely-used navigation system in the world is built on the intersection of three of this program's courses
- **Renewable energy systems and climate modeling**: thermodynamic efficiency limits (Course 3), electromagnetic energy transmission and photovoltaics (Course 2 plus quantum corrections from Course 4), and statistical-mechanical models of molecular and atmospheric behavior (Course 3) intersect in solar conversion, grid design, and climate prediction — modeling any of these end-to-end requires the breadth of the full program

## After This Degree

Where to go once the Course 5 capstone is complete. Three opinionated destinations.

- **Next program of study**: a graduate-level physics curriculum in a chosen specialization (condensed matter, particle physics, astrophysics, quantum information) — all assume exactly the toolkit this degree builds, and the capstone thesis can serve as the first chunk of a graduate research portfolio or PhD application sample
- **Significant text or project**: read and reproduce the calculations in a foundational graduate text — Goldstein's *Classical Mechanics*, Sakurai's *Modern Quantum Mechanics*, or Pathria's *Statistical Mechanics* — choosing which one based on your capstone's direction; or implement a quantum circuit simulator in Python using Qiskit and run a short original experiment, applying Courses 4's formalism to a real quantum computing problem
- **Applied context**: condensed-matter research groups, national lab physics positions, quantum computing engineering roles at IBM Quantum / Google Quantum AI / startup labs, or science-adjacent professional fields (quantitative finance, machine learning research, scientific software engineering) where the ability to move fluently between mathematical, classical, statistical, and quantum frameworks is the load-bearing skill
