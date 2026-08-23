# ADR-045 — Per-Goal Run Lease

- **Status:** Approved and implemented (2026-08-22)
- **Scope:** Prevent concurrent engines from planning/executing the same durable goal

## Context

Arion already fences:

- task snapshots by revision;
- scheduler work by task/step lease;
- mutation resources by canonical resource lock;
- goal rows by version CAS;
- approval and recovery decisions by conditional transitions.

`run_goal()` replay safety still used a read followed later by task creation:

```text
pending_task(goal_id) -> None
  -> create_task(new task id)
  -> plan/version
  -> execute
```

The check and task creation were not one ownership decision. Scheduler and task
fences are keyed by task ID, so distinct tasks for one goal do not contend.

The audit baseline was **1,405 passed, 2 skipped** at local and remote commit
`aaa05de`.

## Reproduction

Two engines shared one SQLite database and one non-retry-safe append capability.
Both public `run_goal()` calls were synchronized to observe no pending task
before either created work. The first engine released the mutation resource lock
but delayed goal completion until the second capability call began.

Observed:

```text
capability calls: 2
external effects: ["1", "2"]
tasks for one goal: 2
plan versions: [1, 2]
goal: completed
both run_goal calls: completed
one task: completed / step succeeded
other task: failed / step succeeded
```

The mutation lock correctly serialized the two appends; it could not determine
that the second task represented duplicate logical goal work. Both
non-retry-safe effects occurred before terminal goal state became visible.

## Root cause

- `pending_task()` plus `_plan_for_goal()` is check-then-create.
- Plan version allocation is atomic, but a concurrent initial planner can be
  interpreted as a subsequent version once another task references version 1.
- Task revision and scheduler ownership fence one task ID only.
- Resource locks serialize effects but intentionally do not deduplicate goals.
- Goal CAS occurs after task side effects.

## Invariants

1. At most one live engine owns long-horizon execution for a goal ID.
2. Goal-run ownership is durable, leased, exact-owner checked, and renewable.
3. A contender performs no planning, task creation, scheduler claim, approval
   creation, capability call, or mutation; it returns current durable goal state.
4. Process death does not wedge a goal: expired ownership is atomically
   reclaimable.
5. Different goal IDs retain existing shared-scheduler concurrency.
6. Goal-run ownership is coordination only; it cannot grant authorization,
   approval, recovery acknowledgement, scheduler capacity, or mutation-resource
   ownership.
7. Task revisions, plan immutability, scheduler leases, mutation locks, approval,
   recovery, and checkpoint behavior remain unchanged.

## Decision

### Reserved internal advisory-lock namespace

Reuse the existing default SQLite advisory lease primitive with an internal
resource namespace:

```text
resource_kind = "arion:goal-run"
resource      = goal_id
capability    = "orchestration.goal"
action        = "run"
```

This avoids a second lease implementation or schema. The namespace is disjoint
from capability mutation resources and is never consulted by authorization.

### `run_goal`

Before evaluation/planning, `run_goal` atomically acquires the goal-run lease and
starts the existing owner heartbeat using the scheduler/process lease duration.
It holds ownership for the complete call and releases in `finally`.

- acquire succeeds → run the existing loop unchanged;
- live contention/store uncertainty → emit bounded contention metadata and
  return the current durable goal without creating work;
- expired row → existing acquire transaction reclaims and replaces it;
- body exception → `finally` still stops heartbeat and releases exact ownership.

### `run_goals`

Bulk execution deduplicates requested goal IDs, independently claims each goal,
runs only owned goals through the unchanged shared scheduler, returns current
state for contended goals, and releases owned leases in reverse order.

No lease is held across separate API calls. Approval/recovery/lock clean stops
release it, so a later invocation may continue normally.

### Compatibility

If an alternate minimal store has no advisory-lock capability, historical
single-process behavior remains available. The default SQLite composition is
the authoritative cross-process path.

## Security and durability notes

- The internal lease does not authorize any capability.
- External mutation locks are still acquired independently after live policy and
  approval.
- A goal-run lock row contains only goal/owner/timestamps—no prompts, params, or
  results.
- Internal goal-run rows are excluded from public mutation-lock list and bulk
  reclaim APIs; explicit internal-kind queries and lazy same-goal expiry reclaim
  remain available.
- Existing lease renewal rejects expired or foreign owners.
- Hard process death after an external effect but before any durable write
  remains the documented at-least-once ambiguity; the goal lease prevents a
  second live runner, not atomic external transactions.

## Test strategy

Focused tests prove:

1. concurrent `run_goal` calls create one task, one plan, and one non-retry-safe
   effect;
2. concurrent bulk `run_goals` calls have the same single-owner behavior;
3. active ownership renews beyond its original lease;
4. contended engines do not invoke planners or capabilities;
5. expired ownership is reclaimable and work resumes;
6. different-goal concurrency, approvals, recovery, scheduler policy, task CAS,
   mutation locks, checkpoint restart, and full suites remain green.

## Verification

- Read-only baseline: **1,405 passed, 2 skipped**.
- ADR-045 per-goal ownership tests: **3 passed**.
- Focused goal/planning, cross-goal, scheduler, task, lock, approval, recovery,
  checkpoint, subprocess restart/FIFO, and event regressions: **295 passed**.
- Complete suite after implementation: **1,408 passed, 2 skipped**.
