# Week 1: Kinematics (Motion in One Dimension)

> **Worked example of [`WEEK_TEMPLATE.md`](WEEK_TEMPLATE.md).** Subject: undergraduate physics. Use it as a target shape when filling a week outline. Counts match the template (4 outcomes, 8 terms, 3 practice problems/session, 5 end-of-week problems, 2 real-world examples, 3 going-deeper items). See [`STYLE_GUIDE.md`](STYLE_GUIDE.md) for conventions.

## Metadata

- **Parent unit**: [Unit 1: Foundations of Mechanics](UNIT_EXAMPLE.md)
- **Duration**: ~3 sessions × 2–3 hours
- **Prerequisites**:
  - Basic algebra (solving for an unknown; manipulating equations)
  - Reading and sketching linear and parabolic graphs

## Overview

This week builds the vocabulary and equations needed to describe motion along a line and extends them into two dimensions for projectile motion. Everything later in mechanics (forces, energy, momentum) assumes you can already say *how* an object is moving before asking *why*.

## Learning Outcomes

By the end of this week you should be able to:

- Distinguish displacement (vector) from distance (scalar) and velocity (vector) from speed (scalar) in worked problems
- Apply the constant-acceleration kinematic equations to solve for one unknown given any three of {v₀, v, a, Δx, t}
- Predict free-fall outcomes using g = 9.8 m/s² and verify the prediction against a measurement or simulation
- Interpret position-time and velocity-time graphs (slope = velocity / acceleration; area under v-t = displacement)

## Key Terms

Week-specific vocabulary. Broader unit-level terms belong in the parent unit's Glossary.

- **Scalar**: a quantity with magnitude only (e.g., distance, speed, time)
- **Vector**: a quantity with magnitude and direction (e.g., displacement, velocity, acceleration)
- **Distance**: total path length traversed; a scalar (always positive)
- **Speed**: magnitude of velocity; a scalar
- **Free-fall**: motion under gravity alone, ignoring air resistance; a = g ≈ 9.8 m/s² downward near Earth's surface
- **Projectile motion**: 2D motion with constant horizontal velocity and constant vertical acceleration (g downward)
- **Trajectory**: the path traced by a moving object in space
- **Range**: horizontal distance from launch point to landing point for a projectile on level ground

---

## Session 1: Displacement, Velocity, and Acceleration

### Objective

Compute average velocity and average acceleration from position-time data and explain the scalar–vector distinction in worked cases.

### Concept Study (~45–60 min)

- Read [Khan Academy: Distance and displacement](https://www.khanacademy.org/science/physics/one-dimensional-motion/displacement-velocity-time/a/what-are-velocity-vs-time-graphs) — pin down the scalar/vector distinction with the runner-on-a-track example (5 laps of a 400 m track: 2000 m distance, 0 m displacement).
- Read or watch [MIT OCW 8.01 Lecture 1](https://ocw.mit.edu/courses/8-01sc-classical-mechanics-fall-2016/) for an intuition-first treatment of units, dimensions, and rate of change.
- Define acceleration in your own words. Units check: m/s² as "meters per second, per second" — verify by reading off a velocity-time graph.

### Active Engagement (~30–45 min)

- **Implementation — Motion Detective**. Roll a ball (or push a toy car) along a level surface. Mark its position every 1 second using a stopwatch and meter stick. Tabulate position vs. time and compute the average velocity between consecutive readings. Then push it up a slight ramp so it decelerates; tabulate again and estimate the average acceleration. *Assumption to flag explicitly:* the surface friction is roughly uniform over the run.

Concrete deliverable expected: a position-vs-time table with average velocities per segment and, for the ramp case, an average-acceleration estimate.

### Practice (~30–45 min)

1. A runner completes 5 laps of a 400 m track in 1200 s. Compute total distance, total displacement, average speed, and average velocity. (Watch the sign of each.)
2. A car moves 100 m east in 8 s, then 50 m west in 4 s. Compute the average velocity for the whole trip.
3. A bike accelerates uniformly from rest to 15 m/s in 6 s. Compute the average acceleration and sketch the velocity-time graph.

### Reflection (~5–10 min)

- Where did the scalar/vector distinction trip you up most? Why?
- Connection: when you say "I drove 50 miles to work and back," what's your distance? Your displacement? Why might this distinction matter for navigation vs. for measuring engine wear?

---

## Session 2: Kinematic Equations and Free-Fall

### Objective

Derive at least one constant-acceleration kinematic equation from first principles and apply all three to one-dimensional problems including free-fall.

### Concept Study (~45–60 min)

- Read the three constant-acceleration equations and trace where each comes from:
  - **v = v₀ + at** — definition of average acceleration, rearranged
  - **x = x₀ + v₀t + ½at²** — integrate velocity in time
  - **v² = v₀² + 2a(x − x₀)** — eliminate t from the first two
- Derive equation 3 yourself on paper. It's the most useful one when time isn't given.
- Read about free-fall in any standard text: a = g ≈ 9.8 m/s² downward, independent of mass when air resistance is negligible. This remains one of the most counterintuitive results in intro mechanics — test it before you trust it.

### Active Engagement (~30–45 min)

- **Simulation — height-vs-time²**. Use [PhET — Projectile Motion](https://phet.colorado.edu/en/simulations/projectile-motion) (or a real drop test if you can time it) to drop the same object from heights 0.5 m, 1 m, and 1.5 m. Time each fall several times and average. Plot height vs. time². For free-fall from rest, x = ½gt², so the plot should be linear with slope ≈ 4.9 m/s². *Assumption to flag explicitly:* air resistance is negligible at these speeds and shapes.

Concrete deliverable expected: a height-vs-time² plot with the measured slope and a written comparison to ½g.

### Practice (~30–45 min)

1. A ball is thrown straight up at 15 m/s. How high does it rise? How long until it returns to its launch height?
2. A stone is dropped from a 45 m cliff. How long until it hits the ground, and what's its final speed?
3. A driver brakes uniformly from 25 m/s to a stop in 4 s. Compute the deceleration and the stopping distance.

### Reflection (~5–10 min)

- Which of the three kinematic equations felt least natural? Why?
- Connection: the equations apply only when acceleration is constant. Where in everyday motion does that assumption break down? (A car in traffic, a raindrop reaching terminal velocity — pick one and articulate the failure.)

---

## Session 3: Graphs and Two-Dimensional Motion

### Objective

Interpret position-time and velocity-time graphs and extend one-dimensional kinematics to projectile motion using the independence of horizontal and vertical components.

### Concept Study (~45–60 min)

- Read how graphs encode motion: position-time slope = velocity, velocity-time slope = acceleration, velocity-time area = displacement. Practice reading each off a sample graph.
- Read the 2D extension: horizontal and vertical motion are independent. A projectile has constant horizontal velocity (ignoring air resistance) and constant vertical acceleration (g downward). The two components are solved with separate 1D kinematic equations and combined at the end.
- Note the projectile-range result on level ground: R = v₀² sin(2θ) / g, with maximum at θ = 45°. This is the *theoretical* optimum — court geometry often makes the *practical* optimum higher.

### Active Engagement (~30–45 min)

- **Simulation + thought experiment — Graph-sketch & predict**. First, sketch position-time AND velocity-time graphs (on the same time axis) for four scenarios: (1) object at rest, (2) constant-velocity motion, (3) object dropped from rest, (4) object thrown straight up and falling back. Then, use [PhET — Projectile Motion](https://phet.colorado.edu/en/simulations/projectile-motion) at fixed launch speed 15 m/s. Predict the range for angles 30°, 45°, and 60° using the formula, then verify in the sim. *Assumption to flag explicitly:* sim defaults to no air resistance — confirm it's off.

Concrete deliverable expected: four sketched graph pairs plus a table of predicted-vs-simulated ranges for the three angles.

### Practice (~30–45 min)

1. A velocity-time graph is a straight line starting at v = 20 m/s with slope –2 m/s². Compute the position at t = 5 s and t = 12 s (assume x = 0 at t = 0).
2. A ball is kicked at 30° above the horizontal at 10 m/s on level ground. Where does it land relative to the launch point?
3. A ball is thrown horizontally at 5 m/s from a 1.25 m tabletop. How far from the table edge does it land?

### Reflection (~5–10 min)

- What still feels uncertain about reading a v-t graph? Specifically: slope vs. area.
- Connection: projectile-motion ideas show up directly in sports. Pick one sport you know and identify where the 45° optimum applies, where it doesn't, and why (hint: where does the ball *need* to end up?).

---

## End-of-Week Self-Assessment

- **Problems** (5 total, mixing conceptual and computational):
  1. A car moves 100 m east, then 50 m west, in a total of 20 s. Compute displacement, distance, average speed, and average velocity.
  2. A train accelerates uniformly from rest to 30 m/s in 90 s. How far does it travel?
  3. A ball is dropped from 80 m. When does it hit the ground, and at what speed?
  4. A position-time graph is a straight line with positive slope, then bends downward into a curve. Describe the motion in plain English and sketch the corresponding velocity-time graph.
  5. A ball is launched at 60° above horizontal at 20 m/s on level ground. Compute its maximum height and time of flight.
- **Synthesis prompt** (1–2 paragraphs): pick one moving system you can observe today (a car, a falling object, a rolling ball, a person walking). Describe it through the kinematics framework: which quantities are roughly constant vs. changing, which equations apply, and where the constant-acceleration model would break down for this particular system.

## Real-World Application

- **Vehicle stopping distance**: a car braking uniformly from 25 m/s to rest needs roughly 30 m on dry pavement (the exact number depends on tire–road friction). Highway brake-distance signage assumes specific deceleration values derived from kinematics — that's why posted figures change for wet vs. dry conditions.
- **Sports trajectories**: a basketball free throw is fundamentally a projectile-motion problem. The empirically near-optimal release angle (~50–55°) sits above the 45° theoretical maximum because the ball must clear the rim from above, not just maximize range.

## Going Deeper

- **Calculus connection**: velocity is the time derivative of position; acceleration is the time derivative of velocity. The kinematic equations are special cases that fall out when a is constant; the general case requires integration. Work through chapter 2 of *Resnick, Halliday & Krane* with this lens.
- **Air resistance**: at high speeds or low density, drag dominates and free-fall becomes terminal-velocity motion. Look up Stokes' law (linear-drag regime) and the quadratic-drag regime, and articulate when each applies.
- **Argue both sides**: is treating air resistance as negligible reasonable for a thrown baseball? For a thrown ping-pong ball? Reason through both cases — what determines the answer is mass-to-cross-sectional-area ratio, and the conclusion isn't symmetric.
