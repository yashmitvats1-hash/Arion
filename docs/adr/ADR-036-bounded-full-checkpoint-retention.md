# ADR-036 — Bounded Full-Checkpoint Retention

- **Status:** Approved and implemented (2026-08-21)
- **Scope:** Bound checkpoint history without changing snapshot/recovery representation

## Context

Arion uses two durable representations of a task:

- `tasks.snapshot`: the mutable current full `Task` row;
- `checkpoints.snapshot`: immutable full `Task` snapshots at durable execution
  boundaries.

A checkpoint contains lifecycle status, all steps and their params/status/
attempts/results/errors, dependency/guidance metadata, current step, approvals,
lock-wait state, plan version, and timestamps. Full snapshots make recovery
simple: the latest checkpoint can be parsed directly into a `Task` without
replaying events or deltas.

The Phase 28 audit found that recovery reads only `latest_checkpoint(task_id)`.
No production path consults older checkpoints. Older rows are historical; audit
events separately retain checkpoint IDs/reasons and lifecycle ordering.

The pre-change full suite passed with **1,335 tests and 2 skips**.

## Measured growth

Measurements used the actual SQLite store and engine checkpoint path:

| Scenario | Final task row | Checkpoints | Checkpoint bytes | Total snapshot factor | DB size |
| --- | ---: | ---: | ---: | ---: | ---: |
| normal 2-step task | 1,201 B | 4 | 4,327 B | 4.60x | 229,376 B |
| 3 steps x 1.5 MB results | 4,501,320 B | 5 | 13,506,280 B | 4.00x | 18,259,968 B |
| 100 steps x 4 KB results | 442,893 B | 102 | 24,315,453 B | 55.90x | 25,591,808 B |

For the 100-step task, checkpoint snapshots grew from about 34 KB after the
first result to about 443 KB near completion. Every later checkpoint recopied
all earlier results. Cumulative checkpoint volume is therefore the sum of
successive task sizes and approaches quadratic growth for long tasks.

Fresh-engine resume of the measured completed tasks invoked zero capabilities
and returned the exact terminal task, confirming that the newest full snapshot
is sufficient.

## Durable ownership and recovery invariants

1. **Current task row:** mutable current state and the immediate API/read model.
2. **Latest checkpoint:** independently restorable durable execution boundary.
3. **Older checkpoints:** historical snapshots; not consulted by runtime
   recovery, scheduling, authorization, or mutation fencing.
4. **Scheduler registry:** ownership/lease/admission metadata only; never task
   results and never a substitute for checkpoints.
5. **Mutation recovery registry:** authoritative gate for caught failures of
   non-retry-safe mutations. Checkpoint retention cannot clear or create it.
6. **Goal CAS/version state:** independent from task/checkpoint retention.

Recovery ordering is intentional:

- after a successful verified step, the task row is saved and then a full
  checkpoint is inserted;
- if a crash occurs between those writes, the newer task row wins;
- once checkpoint insertion completes, the newer/equal checkpoint wins;
- approval resolution writes a newer task row, so it wins over the pending
  checkpoint;
- lock-wait checkpoints preserve deadline, attempts, waiter/queue metadata,
  and task state;
- terminal checkpoints preserve the exact returned task and results;
- a crash inside an uncommitted step resumes from the previous boundary under
  the existing at-least-once semantics;
- completed mutating steps represented as `SUCCEEDED` in the newest retained
  snapshot are not replayed.

No retention policy can remove the ambiguity of a hard crash after an external
side effect but before any durable task write. This ADR does not claim to.

## Decision

### Keep full snapshots

Do not introduce deltas, event replay, blobs, compression, or a new checkpoint
format. Full snapshots retain their existing correctness and operational
simplicity.

### Retain the newest eight checkpoints per task

Add a bounded SQLite pruning primitive and call it after every successful full
checkpoint insertion:

- retain the newest **8** rows by SQLite insertion order (`rowid`);
- delete only older rows for the same task;
- never delete the newly inserted/latest checkpoint;
- validate the retention limit and perform deletion in one store operation;
- keep the current task row unchanged.

Eight preserves all checkpoints for the normal two-step flow (plan, two step
boundaries, terminal) while bounding long-task checkpoint bytes to a constant
multiple of the final task size instead of an unbounded cumulative sum.

### Pruning is best effort after durability

Checkpoint insertion commits first. Retention runs afterwards through an
optional storage capability. If pruning is unavailable or fails, execution and
recovery continue with extra historical rows. A failed storage optimization
must never invalidate a newly durable checkpoint or a completed side effect.

This ordering has a safe failure mode: too much history, never too little
recovery state.

## Compatibility and migration

- The checkpoint table and `Checkpoint` model are unchanged.
- Existing full checkpoints and task rows remain readable.
- Existing databases are not destructively migrated at startup.
- A task converges to the bound when it next creates a checkpoint; untouched
  terminal histories remain intact.
- Alternate `Storage` implementations without the pruning method retain the
  previous unbounded history behavior but remain functionally compatible.
- `list_checkpoints()` returns the retained recent history in original order.
- Audit events recording prior checkpoint persistence remain append-only even
  if the corresponding historical snapshot is later pruned.

## Test strategy

Tests must prove:

1. SQLite pruning keeps exactly the newest rows and never removes the latest;
2. legacy full checkpoints load before/after pruning;
3. a long task converges to eight checkpoints and measured retained bytes are
   bounded relative to the final task snapshot;
4. normal small-task checkpoint history remains unchanged;
5. fresh-engine terminal resume invokes no capabilities;
6. restart after a partial long mutating task restores the latest retained full
   state and does not replay completed non-retry-safe mutations;
7. approval, lock-wait, scheduler ownership, recovery registry, and existing
   persistence suites remain green;
8. pruning failure leaves a valid latest checkpoint and does not fail the task.

## Explicit deferrals

- Delta checkpoints and event sourcing.
- Content-addressed result blobs, external object storage, and compression.
- Snapshot deduplication across tasks or goals.
- Automatic pruning of untouched legacy terminal tasks.
- Fallback chains/upcasting for corrupt checkpoints.
- Distributed recovery and cross-database checkpoint transactions.
- Changing task-row/result retention or mutation execution semantics.

## Verification

- Before implementation: **1,335 passed, 2 skipped**.
- ADR-036 checkpoint-retention tests: **5 passed**.
- Focused checkpoint, restart, scheduler, approval, lock, concurrency, and
  mutation-recovery regressions: **130 passed**.
- Complete suite after implementation: **1,340 passed, 2 skipped**.
- Re-measured 100-step x 4 KB case after retention: 8 checkpoints,
  3,449,782 checkpoint bytes, 8.8x total snapshot factor, and 5,033,984-byte
  database (before: 102 checkpoints, 24,315,453 checkpoint bytes, 55.9x, and
  25,591,808-byte database).
