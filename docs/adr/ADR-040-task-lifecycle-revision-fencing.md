# ADR-040 — Task Revision Fencing and Terminal Execution Guards

- **Status:** Approved and implemented (2026-08-22)
- **Scope:** Prevent stale task snapshots, checkpoints, or scheduler workers from regressing or replaying work

## Context

Arion already had conditional authority for goal lifecycle writes, approval
resolution, scheduler work rows, mutation locks, lock waiters, and recovery
records. The task row itself was the exception: `save_task()` used unconditional
`INSERT OR REPLACE`, task snapshots had no revision, and resume selected a
checkpoint by comparing wall-clock timestamps.

The Phase 32 baseline was **1,363 passed, 2 skipped**. A read-only audit traced:

```text
goal/task creation -> planning -> scheduler claim -> authorization/approval
  -> mutation lock/wait -> capability -> verification -> task snapshot
  -> checkpoint/event -> completion/recovery -> restart
```

## Demonstrated failures

All findings were reproduced against real SQLite and engine paths.

### Concurrent terminal writers

Two connections loaded the same task. One saved `COMPLETED`; the stale copy
then saved `FAILED`. Both writes succeeded and the later stale snapshot became
durable. The reverse ordering could replace a failure with completion.

### Stale checkpoint replay

A one-step task completed. A genuinely stale `PENDING` task snapshot was then
inserted as a later checkpoint. Timestamp precedence selected that checkpoint
and `run_task()` executed the completed step a second time.

### Same task, two live scheduler owners

Engine A held an unexpired `RUNNING` scheduler row and was inside capability
code. Engine B treated every foreign running row as stale, transitioned A's row
to `ABANDONED`, created another row, and executed the same task/step. Both
completed; measured concurrent executions reached two.

Scheduler work heartbeats were also only performed once before capability
execution. A legitimately long-running worker could therefore outlive its lease.

### Cancellation boundaries

A goal cancelled while its planner was blocked still saved the returned plan
and executed it. A goal cancelled while capability code was blocked ended with
the goal `CANCELLED` but the task `COMPLETED`. Arbitrary capability code remains
non-preemptible, but cancellation must prevent later dispatch and normal task
completion.

Approval cancellation and mutation-lock wait cancellation were already safe:
approval/task decisions are atomic, and lock waiters are cancelled before
mutation execution.

### Recovery row before task snapshot

A non-retry-safe mutation committed a durable `REQUIRED` recovery record, then
task snapshot persistence was interrupted. The task row remained `PENDING`.
Direct `run_task()` did not consult open recovery and executed the mutation
again even though recovery remained required.

### Ordered task-before-checkpoint crash

The normal per-step path that persisted `SUCCEEDED` task state and then crashed
before its checkpoint resumed without replay. The task row was newer. This path
was not itself defective; timestamp arbitration and stale writers made the
precedence unsafe.

## Invariants

1. Every committed task snapshot has a monotonic revision.
2. Existing-task writes require the exact expected revision.
3. `COMPLETED` and `FAILED` task rows cannot transition to another status.
4. A checkpoint cannot supersede a task row from a newer revision; a terminal
   task row always wins.
5. A terminal step cannot regress to `PENDING` or execute again.
6. One live scheduler owner is authoritative for a task/step. Only the existing
   lease-expiry reclaim transaction may abandon it.
7. A live scheduler owner renews its exact work row while capability code runs;
   expired owners cannot heartbeat or complete it.
8. Terminal goal state is rechecked after planning, before worker execution,
   and after non-preemptible capability return.
9. A durable `REQUIRED` recovery fences direct task resume as well as fresh goal
   planning.
10. A known terminal mutation-step snapshot is persisted while its mutation
    lock is still held, before a waiter can acquire the resource.
11. Approval, lock, waiter, scheduler, and recovery records retain their
    existing independent authority; task revision cannot grant authorization or
    mutation ownership.

## Decision

### Monotonic task revision and CAS

`Task` gains a backward-compatible integer `revision` (legacy snapshots default
to zero). SQLite `tasks` gains an additive `revision INTEGER NOT NULL DEFAULT 0`
column.

- New task insert: revision `0 -> 1`.
- Existing save: `UPDATE ... WHERE id=? AND revision=?`, committing
  `revision + 1`.
- A stale revision raises `TaskStateError` and changes nothing.
- Persisted transitions are explicit: execution states cannot regress to
  planning/created states; approval may move `AWAITING_APPROVAL -> RUNNING`;
  validation/execution may move non-terminal states to terminal outcomes.
- A current terminal row is immutable; any later full-snapshot save, including
  an attempted resurrection or terminal payload rewrite, raises
  `TaskStateError`.
- The task-table revision column is authoritative for legacy JSON snapshots.

Atomic approval commit/reconciliation increments the same task revision while
retaining the existing status and `updated_at` guards.

### Revision-aware checkpoint recovery

`run_task()` checks the durable task row for terminal state before consulting a
checkpoint. For revisioned state, the task row is authoritative: a checkpoint
cannot mint a higher revision and an equal-revision snapshot adds no uncommitted
state. Timestamp fallback is limited to revision-zero legacy rows/checkpoints,
after terminal-row protection.

### Scheduler ownership lifecycle

`_admit_step()` calls the existing expiry-checked `reclaim_stale()` operation.
A foreign row that remains `RUNNING` is live and cannot be abandoned or
reclaimed by the new engine. ADR-042 later applies the same atomic expiry
predicate to explicit single-row/CLI reclaim.

Workers synchronously validate their exact claim before capability execution
and run a bounded daemon heartbeat until capability return. Work and scheduler
registration heartbeat bounds are sliding per renewal: each extension is
capped, monotonic, exact-owner checked, and denied after expiry, while a live
worker can renew for the full duration of legitimate work.

Immediately before execution, the worker reloads the task and requires the
same task revision with the referenced step still `PENDING`.

### Terminal-goal and recovery fences

The engine rechecks terminal goal authority:

- after planner return and before saving/executing the plan;
- before scheduler worker execution;
- after capability return, because arbitrary capability code cannot be
  preempted.

Task cancellation continues to be represented by terminal `FAILED` task state;
`CANCELLED` remains a goal-level state. A capability that already started may
finish, and its known step result is retained, but the task cannot become
`COMPLETED` under a cancelled goal.

`run_task()` also checks durable `REQUIRED` recovery. A split crash leaving a
pending task is fenced to `FAILED` without capability execution.

### Mutation release ordering

When a mutation step has a known terminal outcome, the task snapshot is
revision-CAS persisted before releasing the mutation lock. A task CAS loss for
a non-retry-safe mutation creates/retains `REQUIRED` recovery before release.
The lock is still released on the handled conflict path.

## Compatibility

- Existing databases receive one additive task column; no rows or checkpoints
  are rewritten.
- Legacy task/checkpoint snapshots without `revision` remain readable. ADR-041
  later promotes default-SQLite revision-zero task rows to revision one at
  startup, without rewriting historical JSON.
- Existing `Task` constructors remain valid because revision defaults to zero.
- Approval request schemas and atomic decision semantics are unchanged.
- Mutation lock identity, renewal, FIFO waiter adoption, cancellation, and
  recovery acknowledgement semantics are unchanged.
- Scheduler work schemas, capacity, weights, reservations, and ceilings are
  unchanged.
- No generic workflow framework, external coordinator, or distributed
  consensus is introduced.

## Explicit limitations

- Hard process death after an external side effect but before any durable task
  or recovery write remains the documented at-least-once ambiguity. SQLite
  cannot atomically commit an external effect.
- Arbitrary blocking capability code is not preemptively cancellable. Terminal
  goal state is enforced before dispatch and after return.
- Scheduler and task coordination remain scoped to the shared SQLite database;
  no cross-host liveness service is introduced.
- Historical task/checkpoint rows are not rewritten.

## Test strategy

Focused tests cover:

1. concurrent `COMPLETED` versus `FAILED` task saves produce one CAS winner;
2. current terminal rows reject resurrection;
3. legacy task rows migrate and advance revision without snapshot rewrite;
4. a late stale checkpoint cannot replay a completed step;
5. crashes after a succeeded task snapshot but before its checkpoint, and after
   terminal task/checkpoint state but before its completion event, do not replay;
6. a live same-task scheduler owner renews beyond its original lease and is
   not preempted by another engine;
7. cancellation during planning performs no capability call;
8. cancellation during non-preemptible execution prevents task completion;
9. a recovery-row-before-task-snapshot crash is fenced on direct resume;
10. existing approval cancellation, lock-wait cancellation, mutation recovery,
    restart, scheduler ownership, checkpoint retention, and concurrency suites
    remain green.

## Verification

- Read-only baseline: **1,363 passed, 2 skipped**.
- ADR-040 focused lifecycle tests: **11 passed**.
- Focused lifecycle, approval, scheduler, lock, recovery, checkpoint, restart,
  and concurrency group: **322 passed**.
- Complete suite after implementation: **1,374 passed, 2 skipped**.
