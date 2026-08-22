# ADR-039 — Mutation Lock Lease Ownership and Waiter Adoption

- **Status:** Approved and implemented (2026-08-21)
- **Scope:** Keep active mutations leased and make pre-checkpoint waiters restart-safe

## Context

Arion's mutation lock store provides a durable unique row per
`(resource_kind, canonical_resource)`, explicit `lock_id`/`owner_id`, atomic
acquire/release/reclaim, leased crash recovery, and a durable FIFO wait queue.
Authorization and approval run before locking; recovery remains a separate
non-authoritative gate.

The Phase 31 baseline passed with **1,358 tests and 2 skips**.

## Verified ownership map

```text
live authorization + approval
  -> canonical execution resource
  -> scheduler work claim/lease
  -> mutation lock acquire (lock_id + owner_id + expires_at)
  -> optional FIFO waiter/task.lock_wait/checkpoint
  -> capability side effect + verification
  -> lock release
  -> task checkpoint / mutation recovery on failure
```

- SQLite's UNIQUE resource key and `BEGIN IMMEDIATE` acquisition are the lock
  authority.
- `lock_id` plus `owner_id` are the ownership token; stale owners cannot delete
  a different/new lock row.
- `task.lock_wait` and its checkpoint point to the durable waiter row and retain
  deadline/attempts/position across normal restart.
- Scheduler ownership is independent coordination; scheduler rows contain no
  mutation resource and cannot grant the mutation lock.
- Mutation recovery records are authoritative after a caught uncertain
  non-retry-safe mutation and remain independent of lock cleanup.

## Demonstrated behavior

Existing tests and direct audit confirmed:

- concurrent acquire on one live row has exactly one winner;
- non-owner release is rejected and repeated release is idempotent;
- expired lock rows can be reclaimed/reacquired and an old owner cannot release
  the replacement;
- persisted waiters preserve FIFO position across normal process restart;
- terminal task cleanup cancels queued waiters on normal engine paths;
- non-retry-safe caught failures retain recovery fencing after lock release.

Two gaps were reproduced through actual store/engine paths.

### Active mutation outlives lock lease

A capability was deliberately blocked longer than a 0.1-second mutation lease.
A second engine atomically deleted the expired row, acquired a new lock for the
same resource, and executed while the first capability was still active. Both
tasks completed and measured mutation concurrency reached two.

The first engine retained only an in-memory lock object and did not renew or
revalidate ownership during execution. A lease is safe for crashed owners only
if a live owner keeps it alive and confirms ownership before committing
success.

### Waiter row committed before task wait state

`enqueue_waiter()` committed a fresh row before `task.lock_wait` and its
checkpoint. A crash in that window loses the waiter ID from task state. On
restart, the same task/step enqueued a second row. The original row remained
FIFO head; the restarted row could not acquire its own resource and was blocked
until the old deadline.

The store accepted both rows because deduplication was delegated to the engine.

## Invariants

1. At most one unexpired durable lock row exists per canonical resource.
2. An active engine must renew that exact `lock_id`/`owner_id`; renewal cannot
   recreate, transfer, or resurrect expired ownership.
3. Successful mutation completion requires ownership still to be valid after
   capability execution and before success is accepted.
4. If ownership is lost after a non-retry-safe side effect may have happened,
   the task fails with durable mutation recovery required.
5. One QUEUED waiter is canonical per resource/task/step; restart adopts it and
   preserves its FIFO sequence/deadline.
6. Goal cancellation observed while waiting cancels the waiter and prevents
   mutation execution.
7. Scheduler leases, approval decisions, and recovery records cannot create or
   transfer mutation-lock ownership.

## Decision

### 1. Conditional lock renewal

Add `renew(lock_id, owner_id, lease_seconds, now)` to the mutation-lock store.
Inside `BEGIN IMMEDIATE` it:

- requires the exact row and owner;
- rejects an already-expired row (stale ownership cannot be resurrected);
- extends only that row's expiry;
- returns the renewed lock.

No schema migration is needed.

### 2. Engine heartbeat during capability execution

For a held mutation lock, the engine starts one bounded daemon heartbeat for the
execution window, renewing at a fraction of the configured lease. It stops and
joins the heartbeat before release.

The worker also performs a synchronous ownership renewal immediately after the
capability returns, before verification/success. If heartbeat or final renewal
shows ownership loss:

- success is not accepted;
- retry-safe behavior does not recreate ownership;
- a non-retry-safe mutation enters the existing durable recovery-required path.

Heartbeat is coordination only and never bypasses live authorization.

### 3. Atomic waiter create-or-adopt

`enqueue_waiter()` becomes a transactional create-or-adopt operation. Under
`BEGIN IMMEDIATE`, an existing QUEUED row for the same canonical
resource/task/step is returned unchanged; otherwise the next FIFO sequence is
allocated and inserted. A restart after the row-before-checkpoint crash window
therefore rediscovers the original waiter and position.

### 4. Cancellation while waiting

The bounded wait loop rechecks terminal goal state before each acquisition
attempt. A cancelled/terminal goal causes the queued waiter to transition to
CANCELLED and the task to fail without executing the capability. Holding an
already-running external side effect remains non-preemptive; it is released and
reported normally or fenced by ownership-loss recovery.

## Compatibility

- Existing lock/waiter rows and schemas remain readable.
- Existing exact canonical resource identity from ADR-021 and resource
  presentation separation from ADR-037 are unchanged.
- Existing release/reclaim APIs remain valid.
- A legacy queued waiter is adopted by matching resource/task/step.
- Approval atomicity from ADR-038 is untouched.
- Recovery records and acknowledgement semantics are unchanged.

## Test strategy

Tests prove:

1. renew requires exact owner and rejects missing/expired rows;
2. a long active mutation retains ownership and a competitor cannot overlap;
3. final ownership loss cannot be reported as successful mutation;
4. non-retry-safe ownership loss creates recovery required;
5. duplicate concurrent/restart waiter creation adopts one row and FIFO
   position;
6. cancellation while waiting cancels the waiter and performs no mutation;
7. stale owner release/new owner protection remains intact;
8. scheduler lease changes cannot transfer mutation ownership;
9. existing subprocess fairness, restart, recovery, approval, and full suites
   remain green.

## Explicit deferrals

- External/distributed lock services and consensus.
- Resource-level fencing tokens enforced by external systems.
- Preemptive cancellation of arbitrary blocking capability code.
- Cross-host process-liveness detection.
- Transactionally combining external side effects with SQLite lock/task writes.
- Changing at-least-once semantics for hard process death after an external
  side effect but before any durable state write.

## Verification

- Before implementation: **1,358 passed, 2 skipped**.
- ADR-039 lease/waiter tests: **5 passed**.
- Focused mutation lock, FIFO, subprocess restart, scheduler lease,
  non-retry-safe recovery, and approval regressions: **175 passed**.
- Complete suite after implementation: **1,363 passed, 2 skipped**.
- Reproduced 0.15-second lease with a capability blocked beyond the original
  expiry: the active owner renewed, the competitor performed zero capability
  calls, and measured concurrent mutation count remained one (before: both
  tasks completed with measured concurrency two).
