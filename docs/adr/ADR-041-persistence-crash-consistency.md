# ADR-041 — Persistence Crash-Consistency Boundaries

- **Status:** Approved and implemented (2026-08-22)
- **Scope:** Converge recovery/task, recovery/blocker, scheduler/task, and legacy checkpoint split states

## Context

Arion's default state backend is one WAL-mode SQLite database, but not every
logical lifecycle boundary is one transaction. Phase 33 audited task revision
CAS, task rows/checkpoints, checkpoint pruning, approvals, mutation locks and
waiters, mutation recovery, scheduler ownership, and restart reconciliation.

The baseline was **1,374 passed, 2 skipped** at commit `4404b79`.

## Transaction ownership map

- Task snapshot: one revision-CAS transaction.
- Checkpoint: one insert transaction; pruning is a later best-effort
  `BEGIN IMMEDIATE` transaction; the checkpoint event is later and
  observational.
- Approval decision: request + task revision/status commit together.
- Approval creation: create-or-adopt request first; awaiting task/checkpoint and
  goal blocker later. Restart re-adopts the request.
- Mutation lock: acquire/reclaim/winning-waiter state is one transaction;
  release + next-waiter selection is one transaction.
- Waiter: create-or-adopt is one transaction; task wait metadata/checkpoint and
  goal blocker are later mirrors.
- Recovery: before this ADR, the REQUIRED record, failed task, and goal blocker
  were independent transactions.
- Scheduler work: claim and terminal work transitions are transactional with
  scheduler telemetry; before this ADR a worker marked work terminal before
  saving a read-only task result or approval pause.
- General audit events are observational and independently committed.

## Demonstrated behavior

### Safe existing windows

- Task CAS succeeded and checkpoint creation failed: restart used the task row,
  completed without replay, and capability calls remained one.
- Completion checkpoint succeeded and a later event failed: terminal task state
  remained authoritative.
- Pruning aborted inside SQLite: the delete rolled back entirely; all old rows
  and the newest checkpoint remained.
- Approval request committed before awaiting-task save: restart adopted the one
  canonical pending request, saved `AWAITING_APPROVAL`, and executed nothing.
- Approval decision task update aborted: request and task both rolled back to
  pending/awaiting.
- Waiter row before task metadata: create-or-adopt retained FIFO identity and
  deadline.
- REQUIRED recovery before failed-task save: Phase 32 fenced the pending task on
  restart and did not repeat the mutation.

These paths do not justify an atomic task/checkpoint bundle or storage redesign.

### Recovery persistence failure permitted mutation replay

A non-retry-safe mutation reported an uncertain failure. Recovery persistence
was forced to fail before the worker's task mirror became durable. The durable
combination was:

```text
task: created / step pending
recovery rows: none
scheduler work: failed
```

Restart executed the mutation again; measured calls increased from one to two.

### Acknowledgement before blocker cleanup permanently blocked progress

`REQUIRED -> ACKNOWLEDGED` committed, then goal blocker cleanup failed. Restart
found:

```text
recovery: acknowledged
goal: blocked
blocker: recovery_required
```

`GoalManager.recheck_blockers()` could reconcile approval and lock blockers but
not recovery blockers. Repeated `run_goal()` remained blocked, while a second
acknowledgement was correctly rejected.

### Revision-zero timestamp fallback executed stale parameters

A migrated legacy task row at revision zero contained current pending-step
parameters. A later-inserted revision-zero checkpoint contained older
parameters. Timestamp fallback selected the checkpoint, and worker preflight
could not distinguish them because both revisions and step statuses matched.
The older parameters executed and replaced the task row's current values.

### Scheduler terminal state preceded task progress

A read-only capability completed, scheduler work transitioned to `COMPLETED`,
then task result persistence failed. Restart saw a pending step and executed it
again. This did not grant authorization and mutation paths already persisted
under their lock first, but the durable work row falsely advertised completion
before task progress existed.

## Invariants

1. Recovery authority wins over its task mirror: a stale task CAS cannot remove
   or roll back a REQUIRED record.
2. On the default SQLite path, recovery create/adopt and the matching failed
   task revision are decided in one transaction.
3. If the combined task update cannot commit, the REQUIRED record is retained
   independently and Phase 32 fencing remains effective.
4. If the recovery table itself is temporarily unavailable but the task table
   works, a terminal repair marker prevents direct replay; a later goal run
   recreates the REQUIRED record before replanning.
5. An acknowledged/missing recovery record makes a `recovery_required` blocker
   stale only when no other REQUIRED row for the goal remains.
6. Task execution/approval-pause state is durable before scheduler work reports
   terminal completion.
7. Scheduler state remains coordination only and cannot substitute for task
   progress.
8. Default SQLite task rows never remain at revision zero after startup.
9. Checkpoint insertion/pruning remains separate because the revisioned task row
   already provides safe recovery precedence.
10. Approval decision atomicity, mutation lock ownership/renewal/FIFO, and task
    revision fencing are unchanged.

## Decision

### Atomic recovery requirement commit

Add optional `RecoveryStore.commit_recovery_requirement(recovery, task,
expected_task_revision)`.

The SQLite implementation uses `BEGIN IMMEDIATE` to:

1. create or adopt the canonical REQUIRED row for `(task_id, step_index)`;
2. conditionally transition the matching non-terminal task to FAILED at the
   expected revision;
3. commit both decisions together.

If the task is missing, terminal, or stale, the recovery row still commits and
the method reports `task_committed=False`. Recovery authority must never be
rolled back merely because its mirror lost a CAS race.

`create_recovery()` also becomes transactional create-or-adopt, eliminating the
check-then-insert duplicate window without a destructive schema migration.
Alternate stores retain create-then-save compatibility.

If the combined default-store operation fails, the engine attempts the
recovery-only write. If the recovery table itself is unavailable, it writes a
terminal task repair marker when possible and propagates the persistence error.
On a later `run_goal`, the engine recreates the REQUIRED row from the failed
mutation step before any replan.

### Recovery blocker reconciliation

`GoalManager` gains an optional `recovery_required_resolver`, parallel to the
existing lock-contention resolver. The engine wires it to the authoritative
recovery registry. `recheck_blockers()` drops a recovery blocker only when its
record is no longer REQUIRED and no other REQUIRED record exists for the goal.
This repairs acknowledgement-before-cleanup crashes without changing who may
acknowledge recovery.

### Legacy revision promotion

The additive task migration promotes every default-SQLite `revision=0` task row
to revision 1. The task-table column remains authoritative; JSON task snapshots
and historical checkpoints are not rewritten. Therefore revision-zero
checkpoint timestamp fallback cannot override executable state after reopening
the default store.

### Task state before scheduler terminal state

A worker now saves terminal step state or `AWAITING_APPROVAL` before marking its
scheduler work row terminal. If task persistence fails, the work row is marked
FAILED when possible and the failure propagates; it is never reported as
COMPLETED first. If task save succeeds but scheduler terminalization fails, the
task row remains authoritative and restart does not replay the step.

No task/checkpoint/scheduler mega-transaction is introduced.

## Compatibility

- One existing additive task column is reused; no new schema table or
  destructive migration is required.
- Legacy snapshot JSON remains readable.
- RecoveryStore implementations without the atomic method use the existing
  compatible fallback.
- Approval schemas and decision transactions are unchanged.
- Lock and waiter schemas/transactions are unchanged.
- Scheduler schema, capacity policy, weights, reservations, and ceilings are
  unchanged.
- Checkpoint representation and retention bound are unchanged.

## Explicit limitations

- Hard process death after an external side effect but before *any* SQLite write
  remains the documented at-least-once ambiguity.
- If all durable task and recovery writes are simultaneously unavailable, no
  in-process mechanism can manufacture durable recovery authority.
- General audit events remain observational and may be missing after a committed
  state transition.
- No event sourcing, distributed transaction, external coordinator, or storage
  redesign is introduced.

## Test strategy

Focused tests prove:

1. recovery and failed-task revision commit together;
2. task-update abort retains/recreates recovery authority and prevents replay;
3. recovery-table failure terminalizes the task marker and later repairs the
   REQUIRED record;
4. acknowledged recovery with failed blocker cleanup self-heals on restart;
5. revision-zero legacy rows are promoted and stale checkpoint parameters do
   not execute;
6. task-save failure cannot leave scheduler work `COMPLETED`;
7. task save followed by scheduler-terminal failure resumes without replay;
8. SQLite checkpoint-prune abort is all-or-nothing;
9. approval decision rollback remains atomic;
10. existing approval, lock, waiter, scheduler, checkpoint, task-CAS, and
    mutation-recovery suites remain green.

## Verification

- Read-only baseline: **1,374 passed, 2 skipped**.
- ADR-041 failure-injection tests: **9 passed**.
- Focused persistence, approval, lock, recovery, scheduler, checkpoint,
  concurrency, and restart regressions: **316 passed**.
- Complete suite after implementation: **1,383 passed, 2 skipped**.
