# ADR-054 — Plan-Version-Fenced Goal Completion

- **Status:** Approved and implemented (2026-08-23)
- **Scope:** Refuse terminal goal completion based on a superseded plan evaluation

## Context

ADR-048 scopes completion *evaluation* to the latest immutable plan.
ADR-049/050/051/052/053 scope execution, publication, task identity, run
ownership, and coordination to the latest plan. The terminal *transition*
itself, however, validated only goal-state legality and goal-row CAS:

- `GoalManager.evaluate()` preserved the evaluated immutable plan version in
  ``evidence["latest_plan_version"]``;
- the engine decided completion from that evidence;
- ``complete_goal(goal_id, reason)`` discarded the version;
- ``transition()`` CAS-compared only ``goal.version`` — and plan commits
  **intentionally never bump ``goal.version```` (informational patches
  explicitly "do NOT increment goal.version").

Goal-row concurrency and plan-lineage authority are therefore independent CAS
domains: a goal-row CAS miss cannot reveal that the authoritative plan
advanced.

The read-only audit baseline was **1,444 passed, 2 skipped** at local and
remote commit ``39f4c3c`` (ADR-053).

## Reproduction

A concurrent lineage writer (the documented ``record_plan_version`` /
``readopt_plan`` funnel — lease-free by design, legal for ACTIVE/BLOCKED
goals) commits immutable plan v2 inside the real ``evaluate() ->
complete_goal()`` window (the ADR-052 "goal completion" ownership boundary):

```text
evaluate()                      -> all_work_complete for v1
concurrent commit               -> plan v2 becomes latest (no task yet)
complete_goal()                 -> goal COMPLETED

observed durable state:
goal: COMPLETED (terminal; no outgoing transitions; readopt refused)
latest plan: v2, implementing tasks: 0
strategy outcome: (2, 'succeeded', 'all_work_complete')   <- never executed
```

Reproduced deterministically in the single-goal loop, deterministically in
the bulk/shared loop, and **naturally in an unsynchronized two-thread race**
(2 of 8 runs). The false strategy outcome is permanent: outcome rows are
UNIQUE per ``(goal_id, plan_version)``, never overwritten, and the
deterministic repair pass re-derives the same wrong row from the terminal
status. No duplicate external effects occur — the defect *suppresses*
authoritative work and stamps false provenance.

## Root cause

The completion decision's plan identity (already carried by the evaluation
evidence) was not part of the transition's authority check. ``goal.version``
CAS proves only row concurrency; plan lineage advancement is invisible to it.

## Invariants

1. A goal may transition to irreversible terminal completion based on an
   evaluation only if the immutable plan version that produced the completion
   decision is still the authoritative latest plan when the terminal
   transition commits.
2. The lineage comparison occurs inside the transition CAS/retry loop — every
   attempt re-reads the authoritative latest plan; a retry can never reuse
   stale plan authority from an earlier attempt.
3. On mismatch the transition fails closed (typed
   ``GoalPlanLineageError``, a ``GoalStateError``): no completion, no
   failure, no blocker, no task mutation, no strategy-outcome write, no
   ``goal.version`` change.
4. The engine treats the mismatch as "state changed — re-evaluate", not as an
   error: the owning loop continues against current durable state and the
   newer plan's work proceeds normally.
5. Legacy/direct ``complete_goal``/``transition`` calls without an expected
   version retain their existing behavior (raw GoalManager authority, ADR-049
   "explicit limitations" convention).
6. ``goal.version`` semantics, plan immutability, task canonicalization,
   goal-run ownership, scheduler leases, mutation locks, approvals, recovery,
   and ADR-049–053 guarantees are unchanged.

## Decision

### Completion request carries evaluated plan authority

``complete_goal(goal_id, reason, expect_plan_version=None)`` forwards the
evaluation's ``evidence["latest_plan_version"]``. Both production completion
sites (single-goal loop and bulk/shared loop) always pass it; they are the
only evaluation-driven completion callers.

### Transition-level fence (not check-then-act)

``transition(..., expect_plan_version=None)`` validates the parameter (positive
int, fail closed on malformed input) and, on EVERY CAS attempt after the
state-legality check and BEFORE any mutation or commit, re-reads
``latest_plan(goal_id)`` and raises ``GoalPlanLineageError`` on mismatch.
Because lineage only moves forward, a mismatch never retries: it fails closed
immediately. CAS-miss retries (row contention) re-run the fence against fresh
state.

### Mismatch behavior in the engine

Both loops catch ``GoalPlanLineageError`` at the completion boundary, emit one
bounded ``goal.completion.fenced`` event (new audit kind, ADR-052 precedent),
and continue the loop: the goal is re-evaluated against current durable
state, the newer plan's implementing task is published/executed through the
existing ADR-050/051/053 paths, and completion may later occur legitimately
against the new lineage. The mismatch is never routed through generic failure
handling.

## Concurrency limitations

The lineage read (cognitive-store connection) and the goal-row commit
(storage connection) are two SQLite connections to one database file. The
*commits* serialize at the file level, but a plan commit can still land in
the microsecond gap between a fence read and the goal UPDATE — the same class
of documented boundary as ADR-052 ("lease validation and external capability
invocation cannot be one SQLite transaction; the guard is renewed at the
immediate pre-invocation boundary"). The fence closes the entire
evaluate-to-transition window (milliseconds, including the ownership renew)
and re-validates at the authoritative commit/retry boundary; the residual
cross-connection gap is accepted and documented rather than introducing a
cross-store transaction architecture. No schema or DDL change is made.

**Update (ADR-056):** The cross-connection timing gap described above was
subsequently closed. ADR-056 introduced
`SQLiteStorage.cas_goal_terminal_fenced()`, which performs both the
plan-lineage read (`SELECT` from `goal_plans`) and the goal-row UPDATE
inside a single `BEGIN IMMEDIATE` transaction on `storage._conn`. Because
SQLite's write lock is database-level, any concurrent `claim_goal_plan`
commit (which also uses `BEGIN IMMEDIATE` on `cog._conn`) is serialized
with respect to this transaction, eliminating the residual gap. **This
section is superseded by ADR-056 §"Invariant (strengthened)"** for
production `SQLiteStorage` backends; the original two-step path is
retained only as a compatibility fallback for stores that do not implement
the atomic method.

## Why stale failure is not included

``max_replans_exceeded`` failure can stamp a newer plan ``failed`` through the
same stale-decision mechanism. It is intentionally NOT fenced here:
``FAILED -> ACTIVE`` is a legal transition (the state is recoverable by
resume/replan), while COMPLETED is the only reproduced *irreversible* terminal
corruption. The ``expect_plan_version`` primitive on ``transition()`` makes a
future failure fence a narrow follow-up should evidence demand it.

## Explicit non-goals

- No schema/DDL/index changes and no new tables.
- No change to ``goal.version`` semantics (plan commits still never bump it).
- No lease or ownership requirements added to the public plan-lineage funnel
  (``record_plan_version`` / ``readopt_plan`` remain legitimate lease-free
  writers).
- No fencing of failure, replan, blockers, cancellation, task transitions,
  scheduler, mutation locks, approvals, or recovery.
- No change to strategy-outcome storage/idempotence; the false-outcome
  amplifier is removed by refusing the transition that would create it.
- No broad refactoring; direct/legacy callers keep existing behavior.

## Test strategy

Focused tests (``tests/test_plan_version_completion_fencing.py``) prove:

1. a stale v1 completion decision cannot complete the goal after v2 commits
   (deterministic boundary injection at the real ownership boundary); v2 work
   proceeds normally and completion later succeeds against v2 with a TRUE
   outcome;
2. the bulk/shared completion path is fenced identically;
3. normal completion with the evaluated version still completes;
4. a forced CAS retry re-checks plan lineage (version bump + v2 commit during
   the first attempt) — the retry refuses instead of committing;
5. a mismatch fails closed: no terminal state, no failure, no blocker, no
   outcome row, no task mutation, ``goal.version`` untouched, and the caller
   re-evaluates from current durable state.

## Verification

- Read-only baseline at ``39f4c3c``: **1,444 passed, 2 skipped**.
- ADR-054 focused tests: **5 passed** (primary invariant demonstrated failing
  before the fix at ``goal completed on stale plan evidence``).
- Regression matrix (plans/schema/hardening/invariants, goal manager, goal
  transitions, state machine, persistence/CAS/crash consistency, progress
  evaluator, goal lifecycle/approval/blocked, cross-goal bulk, ownership
  fencing, replanning, readopt, latest-plan execution/completion/coordination,
  exact-plan claim, goal-run leases, task resume, recovery, atomic recovery
  acknowledgement, strategy outcomes ×5, strategy learning, audit, event
  contracts): **all green**.
- Complete suite after implementation: **1,449 passed, 2 skipped**.
