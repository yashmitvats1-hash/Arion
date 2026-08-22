# ADR-042 — Atomic Scheduler Lease Transitions

- **Status:** Approved and implemented (2026-08-22)
- **Scope:** Enforce lease liveness inside reclaim, completion, failure, and handoff writes

## Context

ADR-026 introduced leased scheduler ownership, ADR-040 prevented engines from
preempting unexpired foreign work, and ADR-041 ordered task persistence before
scheduler terminal completion. The scheduler CLI still reclaimed one work row
through a check-then-act sequence:

```text
get_work(work_id)
  -> verify RUNNING and lease_expires_at <= now
  -> mark_terminal(work_id, ABANDONED)
```

The authoritative `mark_terminal(..., ABANDONED)` update accepted every RUNNING
row. It did not repeat the expiry predicate in SQLite. The same audit then found
that owner completion/failure and `release_and_claim_next` checked worker
identity but not `lease_expires_at > now`, allowing an expired owner to complete
work or complete-and-claim-next before any separate reclaim occurred.

The audit baseline was **1,383 passed, 2 skipped** at local commit `b30e8aa`.
The remote branch was one completed phase behind at `4404b79`.

## Reproduction

Two SQLite handles shared one RUNNING work row:

1. The reclaim caller retained a stale view expiring at `00:00:10`.
2. Before its transition, the exact owner heartbeated at `00:00:09` and extended
   the lease to `00:01:09`.
3. The reclaim caller acted with `now=00:00:11`.
4. The CLI called the unconditional store transition and changed the renewed row
   to ABANDONED.
5. A replacement row for the same task/step was immediately created and claimed
   RUNNING.

Observed:

```text
owner renewed to 2026-08-22T00:01:09+00:00
CLI reported reclaimed -> abandoned
original row: abandoned
replacement row: running
same task/step: task-shared / 0
```

A capability already running under the original worker cannot be preempted. The
invalid abandonment therefore permits another engine to dispatch the same
pending task/step. Mutation locks still serialize matching mutation resources,
but scheduler ownership and read-only work can overlap.

The interleaving is possible with process clock skew, injected clocks, clock
correction, or a direct caller of the public transition. More importantly, the
store authority did not enforce its documented invariant.

A second direct reproduction claimed a row through `00:00:10`, then called the
exact owner completion and atomic handoff paths at `00:00:11`. Both succeeded;
the handoff also claimed the next queued row. Lease expiry therefore had no
effect until another caller explicitly reclaimed it.

## Invariants

1. A RUNNING scheduler row can become ABANDONED only when its durable lease is
   expired in the same transaction that changes status.
2. A successful owner heartbeat before the reclaim transaction wins; stale
   observations cannot revoke it.
3. Unknown, queued, completed, failed, cancelled, and already-abandoned rows
   are not valid single-row reclaim targets.
4. Administrative QUEUED abandonment remains supported for dead scheduler
   queues and pre-execution cleanup.
5. Completion/failure and handoff require the exact owner and a durable live
   lease (`lease_expires_at > now`) in their authoritative UPDATE.
6. An expired owner cannot complete, fail, or claim the next work item.
7. Reclaim never executes work and never authorizes a task or mutation.
8. Reclaim emits `work.reclaimed` atomically with the state transition.
9. Existing task revision, task-before-work ordering, capacity policy,
   scheduler heartbeats, mutation locks, approvals, and recovery fencing remain
   unchanged.

## Decision

### Atomic single-row reclaim

Add `SchedulerRegistry.reclaim_work(work_id, now)`.

The SQLite implementation runs under `BEGIN IMMEDIATE` and:

- requires the row to exist;
- requires status RUNNING;
- requires a non-null `lease_expires_at <= now`;
- performs an UPDATE repeating status and expiry predicates;
- transitions to ABANDONED and clears the lease;
- inserts `work.reclaimed` scheduler telemetry in the same transaction;
- raises `SchedulerStateError` on a live, renewed, non-running, unknown, or raced
  row without changing authority.

### Generic abandonment guard

`mark_terminal(..., ABANDONED)` remains compatible for QUEUED rows. A RUNNING
observation delegates to `reclaim_work`, whose transaction repeats status and
expiry predicates and emits reclaim telemetry. A live RUNNING row is rejected
even when a caller uses the generic transition API.

### Live-lease owner completion and handoff

`mark_terminal(..., COMPLETED|FAILED, owner_worker_id=...)` now requires all of
these predicates in one UPDATE:

```text
status = RUNNING
worker_id = expected owner
lease_expires_at IS NOT NULL
lease_expires_at > now
```

`release_and_claim_next` applies the same predicates before it can terminalize
the current row or claim the next one. Wrong-owner and expired-owner failures
remain typed and roll back the whole handoff transaction.

### CLI

`scheduler reclaim` delegates directly to `reclaim_work`. The CLI no longer
uses a prior `get_work()` result as transition authority. Error text remains
bounded and preserves the existing unknown/non-running/live failure behavior.

## Compatibility

- No schema migration is required.
- Bulk `reclaim_stale()` remains unchanged and expiry-guarded.
- Claim, heartbeat, capacity, weights, reservations, and ceilings are unchanged.
- Live owner completion/failure and handoff retain their APIs; they now reject
  already-expired ownership instead of accepting it.
- QUEUED abandonment remains legal.
- Alternate registries without `reclaim_work` make the CLI fail closed instead
  of falling back to a split check.
- Scheduler state remains coordination only.

## Explicit limitations

- Leases still depend on supplied clocks; this decision guarantees that the
  durable row evaluated inside the transaction is authoritative.
- Hard process death while capability code is inside an external read or side
  effect remains governed by task and mutation recovery semantics.
- No distributed clock service, process kill mechanism, external coordinator,
  or consensus system is introduced.

## Test strategy

Focused tests prove:

1. generic abandonment rejects a live RUNNING row;
2. atomic single-row reclaim rejects live and accepts expired ownership;
3. a heartbeat between stale observation and reclaim wins;
4. the actual CLI uses the atomic decision and cannot abandon the renewed row;
5. expired owners cannot complete/fail work;
6. expired handoff rolls back and cannot claim next;
7. live owner completion/failure and handoff remain compatible;
8. QUEUED abandonment remains compatible;
9. scheduler ownership, CLI, restart, cross-process, task lifecycle, lock,
   approval, recovery, and full suites remain green.

## Verification

- Read-only baseline: **1,383 passed, 2 skipped**.
- ADR-042 atomic lease-transition tests: **9 passed**.
- Focused scheduler policy, capacity, fairness, reservation, ceiling,
  cross-process, task, lock, approval, and recovery regressions: **360 passed**.
- Complete suite after implementation: **1,392 passed, 2 skipped**.
