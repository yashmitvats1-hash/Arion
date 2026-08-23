# ADR-046 — Task Resume Goal Ownership

- **Status:** Approved and implemented (2026-08-23)
- **Scope:** Extend per-goal run leases to direct and bulk task resume entry points

## Context

ADR-045 prevents concurrent `run_goal`/`run_goals` callers from planning and
executing one durable goal twice. Public task execution remained outside that
boundary:

```text
arion resume <task_id> -> ArionEngine.run_task(task_id)
ArionEngine.run_tasks(task_ids)
```

Task revision and scheduler ownership are keyed by task ID. Distinct persisted
tasks belonging to one goal therefore did not contend on goal ownership.

The audit baseline was **1,408 passed, 2 skipped** at local and remote commit
`c8c3a30`.

## Reproduction

Two engines shared one database. Two distinct CREATED tasks belonged to one
ACTIVE goal and each carried the same non-retry-safe append step. Concurrent
`run_task()` calls produced:

```text
capability calls: 2
external effects: ["effect-1", "effect-2"]
task A: completed / succeeded
task B: completed / succeeded
goal: active
both run_task calls: completed
```

The mutation resource lock correctly serialized appends. Direct task execution
does not evaluate or complete the goal, so no terminal goal state became visible
between the effects.

## Root cause

- ADR-045 wrapped only long-horizon goal APIs.
- CLI task resume calls `run_task` directly.
- Distinct task IDs have independent task revisions and scheduler work leases.
- Resource locks serialize resources, not logical goal work.
- Public `run_tasks` could admit multiple requested task IDs for one goal in the
  same scheduler call.

## Invariants

1. Every public execution entry point that can run a task participates in
   per-goal run ownership.
2. A direct task contender returns current durable task state without planner,
   scheduler, capability, approval, or mutation activity.
3. Goal-owned internal loops do not reacquire and self-contend.
4. Public bulk task execution admits at most one requested task per goal per
   call.
5. Different goals retain shared-scheduler concurrency.
6. Same-task revision/scheduler fencing remains unchanged.
7. Goal leases remain coordination only and cannot grant authorization,
   approval, recovery, or mutation ownership.
8. Process death remains recoverable through the existing renewable lease.

## Decision

### Owned task internals

Split existing task implementations without changing their state machine:

```text
_run_task_owned(task_id)
_run_tasks_owned(task_ids)
```

These contain the previous task execution logic and assume the caller already
owns each task's goal lease.

### Public `run_task`

`run_task(task_id)` now:

1. loads the task;
2. acquires the existing ADR-045 lease for `task.goal_id`;
3. returns the loaded task unchanged on live contention;
4. invokes `_run_task_owned` only after ownership succeeds;
5. releases in `finally`.

`execute_goal` continues through public `run_task` and therefore receives the
same ownership. The already-owned `run_goal` loop calls `_run_task_owned`
directly to avoid reentrant lock acquisition.

### Public `run_tasks`

`run_tasks(task_ids)` groups requested tasks by goal, acquires one lease per
goal, selects the first requested non-terminal task for each owned goal, returns
current state for same-goal extras/contended goals, runs selected tasks through
`_run_tasks_owned`, and releases leases in reverse order.

The already-owned `run_goals` loop calls `_run_tasks_owned` directly. Existing
cross-goal fairness and capacity behavior is unchanged.

## Compatibility

- No schema, task status, checkpoint, scheduler, lock, approval, or recovery
  record changes are required.
- Public methods retain their signatures and return types.
- Terminal task resume remains idempotent.
- Alternate minimal stores without advisory leases retain historical
  single-process behavior.
- Bulk callers with distinct goals are unchanged; same-goal extras now cleanly
  stop instead of executing duplicate logical work in one call.

## Explicit limitations

- Explicit sequential resume of separately persisted legacy tasks remains an
  operator-directed action; this ADR prevents concurrent live execution and
  same-call bulk duplication.
- The goal lease does not merge/delete historical duplicate tasks.
- Hard process death after an external effect before any durable write remains
  governed by existing at-least-once/recovery semantics.
- No new task status, workflow engine, or persistent canonical-task table is
  introduced.

## Test strategy

Focused tests prove:

1. concurrent direct resume of distinct same-goal tasks produces one effect;
2. direct ownership renews beyond its initial lease;
3. the contender performs no mutation and its task remains unchanged;
4. bulk `run_tasks` admits one requested task per goal;
5. nested `run_goal` uses the owned task path without self-contention;
6. CLI resume, same-task fencing, different-goal concurrency, scheduler policy,
   locks, approvals, recovery, checkpoints, and full suites remain green.

## Verification

- Read-only baseline: **1,408 passed, 2 skipped**.
- ADR-046 direct/bulk task ownership tests: **3 passed**.
- Focused task state-machine, goal, bulk/cross-goal, scheduler, lock, approval,
  recovery, checkpoint, CLI, and event regressions: **256 passed**.
- Complete suite after implementation: **1,411 passed, 2 skipped**.
