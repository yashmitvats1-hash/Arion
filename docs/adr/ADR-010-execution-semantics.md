# ADR-010 — Execution semantics (at-least-once, retry safety)

- **Status:** Approved (2026-08-09)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

Tasks resume after interruption (restart, crash). What does that mean for the
steps that may have been running? And what may the engine automatically retry?
Without explicit semantics, side-effecting capabilities could double-apply
effects or be blindly re-run after a partial failure.

## Decision

1. **At-least-once execution.** A step that was interrupted (process crash,
   kill, power loss) is **re-executed** when the task resumes from its last
   checkpoint. The checkpoint records state *before* the step's effects, so
   the engine cannot know whether the interrupted execution completed. This is
   inherent and documented behavior: capability authors must make their
   operations idempotent (or tolerate re-execution) where it matters.

2. **Retry safety is declared, not assumed.** `ActionSpec` carries:
   - `idempotent` — re-running yields the same end state;
   - `retry_safe` — a failed attempt may be automatically retried;
   - `reversible` — a side effect can be undone (future rollback support);
   - `side_effects` — none | read_only | mutating | irreversible;
   - `risk` — none | low | medium | high (feeds the policy, ADR-009).

3. **Engine retry rule.** Within a step, the engine retries a failure (either
   a capability error or a verification failure) only while attempts remain
   **and** `retry_safe` is true. A non-retry-safe action fails the step
   immediately after the first failed attempt, so a partially applied side
   effect is never blindly re-applied. A verification failure on a retry-safe
   action is retried (reads are safe to re-verify).

4. **Crash recovery is out-of-band.** A hard crash (BaseException) propagates;
   the engine's checkpoint protects state. Recovery on resume re-executes the
   interrupted step under rule 1.

## Consequences

- Tests pin the semantics: retry-safe retries and succeeds; non-retry-safe
  fails after one attempt; verification failures follow the same rule; crash +
  resume re-executes exactly once (see `test_semantics.py`,
  `test_persistence.py`).
- Before any mutating capability ships, its metadata must be truthful; a
  `retry_safe=False, idempotent=False` action is effectively "attempt once,
  then require human/agent intervention".

## Related

ADR-004 (checkpointing), ADR-009 (authorization uses this metadata).
