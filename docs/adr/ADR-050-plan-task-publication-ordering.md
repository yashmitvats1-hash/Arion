# ADR-050 — Plan/Task Publication Ordering

- **Status:** Approved and implemented (2026-08-23)
- **Scope:** Prevent managed tasks from becoming executable before immutable plan authority exists

## Context

For GoalManager-managed work, Arion persists both:

- an immutable `goal_plans` version; and
- a mutable task snapshot carrying executable steps and `plan_version`.

Before this ADR, `_plan()` saved the task as PLANNED and emitted
`plan.produced` before attempting `record_plan_version()`. The plan-version block
caught and ignored every exception as informational.

The audit baseline was **1,423 passed, 2 skipped** at local and remote commit
`3435668`.

## Reproduction

`GoalManager.record_plan_version()` was made to fail once, then behave normally.
One public `run_goal()` produced:

```text
goal: completed
plan persistence attempts: 2
planner calls: 2
capability calls: 2
external effects: ["effect-1", "effect-2"]
durable plans: [(1, initial_plan)]
task 1: plan_version=None, completed/succeeded
task 2: plan_version=1, completed/succeeded
```

The first non-retry-safe effect ran from an unversioned task. Since no plan row
existed, the goal loop planned again and repeated the effect.

A hard process crash after the PLANNED task save but before plan claim leaves the
same durable shape.

## Root cause

- Executable task steps were published before their immutable plan version.
- Plan-version persistence errors were swallowed.
- ADR-049 intentionally permits unversioned legacy tasks when no latest plan
  exists, so the invalid publication looked compatible and executed.
- Checkpointing occurred after the swallowed failure, further preserving the
  unversioned executable snapshot.

## Invariants

1. A GoalManager-managed task cannot become durably PLANNED/executable until its
   immutable plan version is committed and attached.
2. Plan-version persistence failure fails the task closed and performs no
   capability call.
3. The durable task publication write contains normalized steps, PLANNED status,
   and exact `plan_version` together under task revision CAS.
4. `plan.produced` and plan checkpoint occur only after plan and task authorities
   both exist.
5. If plan commit succeeds but task publication fails, restart reconstructs from
   the stored plan through the existing fast path and executes once.
6. Standalone engines without GoalManager retain unversioned task behavior.
7. Goal leases, latest-plan completion/execution, task revisions, scheduler,
   approvals, recovery, locks, pause/cancel, and checkpoints remain unchanged.

## Decision

### Managed publication order

After planner validation/normalization, `_plan()` keeps steps in memory and, for
managed goals:

1. selects strategy/replan reason;
2. calls `record_plan_version()` to claim/adopt immutable plan authority;
3. sets `task.plan_version` from the canonical record;
4. saves the task once with steps + PLANNED + version;
5. emits `plan.produced` and replanning provenance;
6. checkpoints the published task.

No executable unversioned task row exists between steps 2–4. ADR-051 later
made step 4 an atomic exact-plan task create/update-or-adopt claim shared with
stored-plan reconstruction, fencing stale owners and repeated recovery.

### Plan persistence failure

Any strategy/plan-version persistence exception before task publication:

- changes the task to FAILED;
- stores bounded `planning persistence failed; execution denied` without raw
  provider/SQLite diagnostics;
- emits typed error and task failure events;
- records terminal memory through the existing idempotent lifecycle;
- returns without checkpoint or capability execution.

The next explicit goal cycle may plan again. The failed task has no executable
status and cannot authorize completion under ADR-048.

### Plan committed, task save failed

Task save failure after a plan commit propagates. The existing CREATED task row
has no steps/version. On restart:

- latest plan exists;
- no task implements it;
- stored-plan reconstruction creates one exact versioned task;
- ADR-049 rejects the old pre-plan unversioned row;
- execution occurs once.

### Replan events

`goal.replanned` is emitted only after the versioned task publication succeeds.
`plan.versioned` may precede task publication because the plan row is already a
valid recovery authority. Goal `last_replan_reason` remains a best-effort
read-model mirror.

## Compatibility

- No schema, plan, task, checkpoint, scheduler, approval, recovery, or lock
  format changes are required.
- Task/goal method signatures remain unchanged.
- Stored-plan recovery is reused unchanged.
- Standalone no-GoalManager execution remains unversioned and compatible.
- Existing plan summaries, versions, reasons, and event kinds remain readable.

## Explicit limitations

- Plan and task use separate SQLite connections/stores; this ADR provides safe
  ordering/reconciliation rather than a cross-connection transaction.
- Planner computation before plan commit remains in memory and may be repeated
  after process death.
- A plan row whose task publication repeatedly fails remains recoverable but may
  leave non-executable CREATED task history.
- No event sourcing, distributed transaction, or workflow redesign is added.

## Test strategy

Focused tests prove:

1. transient plan claim failure produces zero effects and a failed task;
2. the next explicit cycle publishes/executes once;
3. plan commit followed by task-save failure reconstructs once after restart;
4. normal managed publication carries exact version before execution;
5. standalone unversioned task compatibility remains;
6. replanning, stored plans, latest-plan fencing, goal/task leases, scheduler,
   locks, approvals, recovery, checkpoints, and full suites remain green.

## Verification

- Read-only baseline: **1,423 passed, 2 skipped**.
- ADR-050 publication-ordering tests: **4 passed**.
- Focused planning, replan, stored-plan, latest authority, lease, scheduler,
  lock, approval, recovery, checkpoint, memory, strategy, and event regressions:
  **307 passed**.
- Complete suite after implementation: **1,427 passed, 2 skipped**.
