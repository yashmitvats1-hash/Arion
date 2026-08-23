# ADR-047 — Paused Goal Execution Boundary

- **Status:** Approved and implemented (2026-08-23)
- **Scope:** Prevent new capability execution after PAUSED becomes durable

## Context

Arion's deterministic goal evaluator already returns no action for a goal that
is PAUSED before `run_goal()` starts. ADR-040 rechecks terminal goal states at
planning and worker boundaries, and ADR-045/046 ensure every goal/task entry
owns one per-goal run lease. An operator may still pause a goal while that owner
is planning or between execution boundaries because goal lifecycle CAS is
independent from the run lease.

Task execution treated COMPLETED, FAILED, and CANCELLED as stop authority but
not PAUSED.

The audit baseline was **1,411 passed, 2 skipped** at local and remote commit
`7485d94`.

## Reproductions

### Paused goal executed through direct task resume

A pending non-retry-safe append task belonged to a goal that was durably PAUSED
before `run_task()`:

```text
goal: paused
task: completed
step: succeeded
capability calls: 1
external effects: ["effect-1"]
```

Goal-run ownership was correctly acquired; ownership did not validate pause
authority.

### Pause during planning still started the capability

`run_goal()` held its lease while the planner blocked. The operator committed
ACTIVE → PAUSED before any capability started, then the planner returned:

```text
goal: paused
run_goal result: paused
task: completed / succeeded
capability calls: 1
external effects: ["effect-1"]
```

The next evaluator pass observed PAUSED only after execution.

## Root cause

- PAUSED was enforced only by goal-loop evaluation.
- `run_task`/`run_tasks` did not check pause state.
- `_plan` checked terminal goal authority after planner return, not pause.
- Scheduler worker start and pre-capability authorization completion did not
  recheck pause.
- Post-lock-wait mutation revalidation checked terminal/recovery/task state but
  not pause.
- Task completion could race with a pause committed after capability return.

## Invariants

1. Once PAUSED is durable, no new capability invocation begins for that goal.
2. Direct task resume, bulk task execution, and long-horizon goal loops enforce
   the same pause boundary.
3. Planning already in flight may be discarded; it cannot create an effect
   after pause.
4. Arbitrary capability code already running is not preempted. Its known result
   is persisted honestly, but no next step or task completion begins while
   paused.
5. Resume continues from durable task state and does not re-execute succeeded
   steps.
6. A mutation lock acquired after waiting is released without mutation when a
   pause becomes visible; cleared task wait metadata is persisted.
7. PAUSED remains resumable and does not become task failure/cancellation.
8. Terminal goal, approval, recovery, scheduler, task revision, checkpoint, and
   per-goal lease invariants remain unchanged.

## Decision

### Current pause authority helper

The engine reads current `GoalManager` state through `_goal_is_paused(task)`.
When a configured goal authority cannot be read, task execution stops safely.
A transient in-process marker communicates pause observed by a worker back to
its execution round; it is not serialized into task snapshots.

### Task boundaries

Owned task execution checks PAUSED:

- immediately after loading the task;
- after planner return;
- at the top of every execution round, before handled-step completion;
- after worker drain in single and bulk task drivers;
- inside shared-task completion.

A paused task returns current durable state unchanged.

### Worker and capability boundaries

Workers check PAUSED before task-step freshness and again after capability
return. `_execute_step` checks after authorization/approval immediately before
execution. Thus a pause committed while planning, queuing, or authorizing wins
before capability start.

If pause occurs during capability code, terminal per-step persistence still
records the result. The task remains RUNNING rather than COMPLETED until resume.

### Mutation lock-wait boundary

Pre-mutation revalidation checks PAUSED before accepting either immediate or
waited execution. On a waited acquisition, the in-memory clearing of
`task.lock_wait` is persisted before returning, then the acquired mutation lock
is released by the existing caller. No mutation occurs and restart cannot remain
parked behind an already-acquired waiter.

## Compatibility

- No schema, task status, checkpoint, approval, recovery, scheduler, or lock
  format changes are required.
- PAUSED remains a goal-only state; tasks stay CREATED/PLANNED/RUNNING as
  appropriate.
- Existing PAUSED → ACTIVE resume semantics are unchanged.
- Goal/task method signatures and return types are unchanged.
- Alternate engines without GoalManager retain historical standalone task
  behavior.

## Explicit limitations

- Arbitrary in-flight capability code cannot be preempted.
- A pause does not roll back a side effect that already began.
- Planner work returned after pause is discarded and may be recomputed after
  resume.
- No process signalling, workflow engine, or task-level PAUSED state is added.

## Test strategy

Focused tests prove:

1. paused direct task resume invokes no capability and resumes once;
2. pause during planning prevents capability start and resume later succeeds;
3. pause during an in-flight first step persists it, stops the next step, and
   resume executes only remaining work;
4. pause after mutation lock wait releases without mutation and clears durable
   wait metadata;
5. cancellation, approval, recovery, goal-run ownership, scheduler, task CAS,
   checkpoint restart, lock fairness, and full suites remain green.

## Verification

- Read-only baseline: **1,411 passed, 2 skipped**.
- ADR-047 paused execution-boundary tests: **4 passed**.
- Focused goal/replanning, task entry, concurrency, scheduler, lock,
  approval, recovery, checkpoint, and event regressions: **247 passed**.
- Complete suite after implementation: **1,415 passed, 2 skipped**.
