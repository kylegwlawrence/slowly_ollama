# Unit 1: Foundations of Mechanics

> **Worked example of [`UNIT_TEMPLATE.md`](UNIT_TEMPLATE.md).** Subject: undergraduate physics. Use it as a target shape when filling a unit outline. Counts match the template (6 outcomes, 8 concepts, 12 glossary terms, 5 resources, 4 connections, 3 going-deeper items). See [`STYLE_GUIDE.md`](STYLE_GUIDE.md) for conventions.

## Metadata

- **Parent course**: [Physics: Mechanics to the Quantum Frontier](COURSE_EXAMPLE.md)
- **Duration**: 3 weeks
- **Estimated effort**: ~6–9 hours/week (3 sessions × 2–3 hours)
- **Prerequisites**: Basic algebra (solving for an unknown, manipulating equations); reading and sketching linear and parabolic graphs.
- **Difficulty tier**: intro

## Overview

Mechanics is the entry point to physics — the study of motion and the forces that cause it. This unit builds, in order: how to describe motion (kinematics), why motion changes (forces and Newton's laws), and how the bookkeeping of energy gives a second, often simpler way to predict outcomes (work and energy conservation).

Every later unit assumes you can move fluently between these three viewpoints. The thermodynamics unit's "energy" relies on this unit's definition; electromagnetism's force law mirrors `F = ma`; quantum mechanics still uses kinetic and potential energy as its accounting categories. By the end you should be able to look at an everyday motion problem and pick the right framework — kinematics, forces, or energy — instead of mechanically reaching for whichever you learned first.

## Learning Outcomes

By the end of this unit you should be able to:

- Apply the constant-acceleration kinematic equations to one- and two-dimensional motion, including projectile and free-fall problems
- Draw correct free-body diagrams and apply Newton's three laws to predict an object's acceleration
- Compute kinetic energy, potential energy, work, and power for a system of point masses
- Predict the outcome of a multi-stage motion (ramp + flat + curve) using conservation of energy, and identify when conservation fails
- Compare a force-based solution and an energy-based solution for the same problem and argue which is more efficient
- Model a concrete real-world system (a vehicle, a thrown ball, a structure under load) and identify which idealizations the model rests on

## Key Concepts

High-level ideas the unit covers. No daily detail (that belongs in week files).

- Displacement, velocity, and acceleration as vectors
- Kinematic equations for constant acceleration
- Newton's three laws of motion
- Free-body diagrams; friction, normal force, tension
- Work, kinetic energy, gravitational potential energy
- Power as the rate of energy transfer
- Conservation of energy in conservative systems
- Idealization vs. modeling — when "frictionless" or "massless" is fair game and when it lies

## Weekly Sequence

| Week | File | Focus | Key Skills |
|------|------|-------|------------|
| W1 | [`week1_kinematics.md`](../week1_kinematics.md) | One- and two-dimensional motion under constant acceleration | Kinematic equations; free-fall; projectile motion; reading position-time and velocity-time graphs |
| W2 | [`week2_forces.md`](../week2_forces.md) | Newton's laws and force analysis | Free-body diagrams; F = ma; friction; action-reaction pairs |
| W3 | [`week3_energy.md`](../week3_energy.md) | Work, energy, and conservation | Computing W, KE, PE; conservation as a shortcut; distinguishing conservative vs. non-conservative forces |

## Glossary

Centralized at the unit level so week files don't redefine these terms.

- **Displacement (Δx)**: change in position; a vector (m)
- **Velocity (v)**: rate of change of position; a vector (m/s)
- **Acceleration (a)**: rate of change of velocity; a vector (m/s²)
- **Force (F)**: an interaction that causes a mass to accelerate (N = kg·m/s²)
- **Inertia**: an object's resistance to change in its motion; quantified by mass
- **Friction**: a force opposing relative motion between surfaces in contact
- **Free-body diagram (FBD)**: a sketch showing all forces acting on a single object
- **Work (W)**: force applied through a displacement; W = F·d for constant force along the displacement direction (J)
- **Kinetic energy (KE)**: energy of motion; KE = ½mv² (J)
- **Potential energy (PE)**: energy of position; e.g., PE_gravity = mgh (J)
- **Power (P)**: rate of doing work; P = W/t (W)
- **Conservation of energy**: in an isolated system, total mechanical energy is constant if only conservative forces act

## Tools & Resources

- `[verified]` [PhET — Forces and Motion: Basics](https://phet.colorado.edu/en/simulations/forces-and-motion-basics) — free, browser-based; covers Newton's-law intuition without setup
- `[verified]` [PhET — Energy Skate Park](https://phet.colorado.edu/en/simulations/energy-skate-park) — visualizes KE↔PE exchange on arbitrary tracks
- `[verified]` [MIT OCW 8.01: Classical Mechanics (Lewin)](https://ocw.mit.edu/courses/8-01sc-classical-mechanics-fall-2016/) — the gold-standard intro lectures; first ~6 cover this unit
- `[verified]` [Khan Academy: One-dimensional motion](https://www.khanacademy.org/science/physics/one-dimensional-motion) — gentle first-pass intro before MIT
- `[unverified]` *Resnick, Halliday & Krane, Physics Vol. 1* — standard reference text; verify the edition you have covers chapters 2–8

## Real-World Connections

- **Vehicle safety**: stopping distances assume uniform deceleration; highway brake-distance signage is computed from the kinematic equations under specific friction assumptions.
- **Sports biomechanics**: a basketball free throw is a projectile-motion problem; the optimal release angle is set by court geometry and hoop height, not by the theoretical 45° range maximum.
- **Structural engineering**: bridges and buildings are designed by treating forces as Newton's-law balance problems with safety factors layered on top.
- **Aerospace**: rocket trajectories are 2D projectile motion plus an additional thrust force; the kinematic framework underpins early-stage trajectory planning.

## Unit Self-Assessment

A single open-ended summative challenge at the end of the unit. Pick one:

- **Problem**: a 6-problem mixed set — 2 kinematics, 2 forces / FBDs, 2 energy conservation. Write a rubric for yourself before starting so the grade isn't post-hoc.
- **Project**: design a paper roller coaster. Specify the heights at each peak and valley, compute the speed at each point using energy conservation, sketch the velocity-time graph for one full run, and identify where friction would break your idealized analysis.
- **Synthesis**: write a 1–2 page comparison of solving "a block sliding down a ramp with friction" via forces + Newton's laws vs. via energy conservation. Which approach is easier here? Where does each fail? What does that tell you about which framework to reach for first in unfamiliar problems?

Self-grade against the Learning Outcomes above. No formal quiz.

## Going Deeper

- **Calculus connection**: velocity is dx/dt; acceleration is dv/dt. The constant-acceleration kinematic equations are special cases; general motion needs integration. Work through chapter 2 of *Resnick, Halliday & Krane* with this lens.
- **Beyond Newton**: Lagrangian and Hamiltonian formulations (covered in advanced classical mechanics) are alternative starting points that scale to more complex systems. Once F = ma is solid, those become the natural next layer.
- **Rotational mechanics**: this unit covers translation only. Angular velocity, torque, and angular momentum are the rotational analogues — worth a self-study weekend with a standard text once translation is fluent.
