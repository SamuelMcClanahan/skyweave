# Testing doctrine

**Status:** adopted 2026-08-24, approved by Samuel. Distilled from what
measurably worked in the D0-D8 suites, plus the two traps they fell
into. Applies to SkyWeave and to the drone stack (see
`writeups/DRONE_STACK_CARRYOVER.md`). The goal is that test sprawl
never tracks code sprawl, and the suite stays cheap enough to run on
every edit.

## Rules

1. **Tests map to contract lines, not modules.** Every test series (T,
   W, E; drone: G guidance contracts, S SITL scenarios, F link faults)
   exists because a contract clause exists, and each test cites the
   clause it enforces. If a behavior is not worth a contract line, it
   is not worth a test; if it is worth a contract line, it cannot ship
   untested. Contract-anchored tests survive refactors untouched,
   which is what keeps refactoring cheap.

2. **Fixtures over mocks.** Never mock what you own; replay recorded
   fixtures through the real code. Mock only the physical boundary
   (for the drone, SITL is the physics mock — one seeded scenario with
   recorded truth outranks fifty hand-written guidance-law tests).

3. **Assert against the system's own outputs, never a re-derivation.**
   An assertion's expected value comes from the system under test or a
   fixture, never from a reimplementation of the logic being tested.
   Lesson: the RAM-budget test that compared the harness's arithmetic
   to the same arithmetic passed while the daemon refused to run; the
   fix gated on the daemon's printed total.

4. **Load-bearing constants get mutation checks.** A test that pins a
   threshold must fail when the threshold moves; verify discrimination
   (the 49-fails / 51-passes / 50-untestable pattern from the
   saturation bound). Apply ONLY to load-bearing constants; applying
   it everywhere is fortification.

5. **Findings become tests.** Every bug-class finding gets one
   regression test named after it (the F-C1-5 pattern). This is the
   mechanism by which a logged-not-built-around edge case (the
   standing rule in /CLAUDE.md) is later promoted to machinery: it
   sits in the findings log until a boundary review promotes it.

6. **The suite has a speed budget, and the budget is a test.** Two
   tiers: a fast tier (contracts, pure logic, small fixtures) capped
   near 30 seconds, run on every edit; a slow tier (large fixtures,
   SITL scenarios, soak-class) marked and run at hand-back on the gate
   platform. A suite that silently crept from 30 s to 13 minutes stops
   being run per-edit, which is how debt actually accumulates.

7. **One truth per behavior.** If two tests assert the same fact, one
   is deleted at the next boundary review. Test count is not coverage;
   overlapping tests are debt because every refactor pays all of them.

8. **Determinism.** Every test is seeded; no wall-clock in assertions;
   a flaky test is a failing test.

9. **Tests are never deleted or weakened by feature work.** Removal
   happens only through rule 7's boundary review, recorded.

## The two traps (do not repeat)

- **Evidence machinery does not belong in unit tests.** Attestation,
  hash-binding, and staged-tree isolation are campaign-runner
  concerns. A unit suite that carries them stops being fast, and rule
  6 dies first.
- **No tests against unfrozen interfaces.** Contract first, then
  tests. Test-first against a moving interface produces churn that
  looks like rigor.
