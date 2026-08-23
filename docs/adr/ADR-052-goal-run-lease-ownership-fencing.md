# ADR-052 — Goal-Run Lease Ownership Fencing

- **Status:** Approved and implemented (2026-08-23)
- **Scope:** Stop a resumed stale goal runner before it creates authority or starts new work

## Context

ADR-045/046 require every public goal/task execution entry point to hold one
renewable `arion:goal-run` lease. ADR-049 permits only latest-plan work to
execute, ADR-050 publishes immutable plan authority before its task, and
ADR-051 atomically creates or adopts one canonical task for each exact plan
version.

The goal-run heartbeat provided liveness but not an execution fence. Its error
was inspected only when the public call released the lease. Owned internals did
not carry the exact acquired identity and therefore could continue after a
suspended process lost its lease to another engine.

The read-only baseline was **1,432 passed, 2 skipped** at local and remote commit
`051b824`.

## Reproduction

Two engines shared one SQLite database and one non-retry-safe append
capability:

1. Engine A acquired the goal-run lease and blocked in its planner.
2. A's heartbeat stopped and its lease expired.
3. Engine B reclaimed the goal, published plan v1, and applied one external
   append while capability return was held.
4. A resumed its stale planner stack.
5. A observed B's implementing v1 task and committed equivalent plan v2.
6. B preserved its already-started result and correctly failed the now-
   superseded v1 task.
7. A lost task revision CAS, leaving committed v2 without a task.
8. Normal ADR-050/051 recovery reconstructed one canonical v2 task and repeated
   the append.

Observed before this decision:

```text
plans: [(1, initial_plan), (2, replan)]
task v1: failed / step succeeded
task v2 after recovery: completed / step succeeded
capability calls: 2
external effects: ["effect-1", "effect-2"]
```

## Root cause

- `_acquire_goal_run_lease()` returned a lock/heartbeat pair only to the public
  wrapper.
- `_run_goal_owned`, task/bulk owned paths, planning, scheduler admission, and
  workers did not receive or validate that exact lease.
- `_release_goal_run_lease()` was the first place heartbeat loss became visible.
- Task revision CAS correctly rejected A's stale task update, but plan v2 had
  already committed.
- ADR-051 deduplicates tasks within one exact version; it cannot deduplicate
  equivalent v1/v2 plans.
- ADR-050 then correctly reconstructed the invalidly stale-published v2.

## Invariants

1. A managed owned invocation carries the exact goal-run `lock_id`, `owner_id`,
   and goal identity it acquired.
2. A different live lease for the same goal never satisfies the invocation's
   ownership check.
3. Exact ownership is synchronously renewed after blocking planning and before
   immutable plan publication.
4. Stored-plan reconstruction and exact-plan task publication require the same
   live owner immediately before their task claim.
5. Scheduler admission and every new capability invocation require current
   exact goal ownership.
6. Every retry is a new capability invocation and receives a fresh ownership
   check.
7. Owned task and goal completion/failure transitions require current exact
   ownership.
8. Ownership loss is a clean stop: it creates no plan/task, starts no new
   capability, and does not fail or rewrite another owner's task.
9. A capability already invoked is not preempted. Its known result continues
   through existing task revision, scheduler, mutation-lock, verification, and
   recovery handling; no next invocation or normal task/goal completion starts
   under the lost goal lease.
10. Goal-run ownership remains orchestration coordination only. It grants no
    policy, approval, scheduler, mutation, or recovery authority.

## Decision

### Explicit owned-call guard

The engine carries an internal `_GoalRunLease` through single and bulk owned
paths. The guard contains the exact acquired lock and heartbeat plus bounded
loss/event state. Existing private tuple indexing remains compatible for
lease-race tests.

Default SQLite validation reuses `MutationLockStore.renew()` with:

```text
exact lock_id
exact owner_id
resource_kind = arion:goal-run
resource = goal_id
unexpired lease
```

The background heartbeat remains the liveness mechanism. Synchronous renewal
is the authority decision at sensitive boundaries. Alternate minimal stores
without a durable lock retain historical single-process compatibility.

### Fenced boundaries

The exact guard is checked:

- before and after goal evaluation, and immediately before owned goal terminal
  transitions;
- after planner return and immediately before `record_plan_version()`;
- before stored-plan reconstruction publication and `claim_task_for_plan()`;
- at task entry and every task/shared-task execution round;
- before task completion;
- before lock waiter publication/continued waiting;
- before scheduler admission and at worker start/preflight;
- after authorization and after mutation-lock acquisition/wait;
- immediately before each capability invocation, including retries.

Bulk calls carry a claim map keyed by goal. Losing one claim stops only that
goal; independently owned goals continue through the shared scheduler.

### Ownership-loss behavior

A failed exact renewal:

- marks only an in-process coordination guard;
- emits at most one bounded `goal.run.ownership_lost` event for the explicit
  guard;
- reloads/returns current durable task or goal state at the owned boundary;
- creates no task/plan and performs no task/goal failure transition;
- does not clear approval, blocker, waiter, or recovery state owned by another
  invocation.

A worker stopped before invocation reports its scheduler work as failed
coordination without changing task state. If capability code already started,
existing post-capability persistence remains authoritative and non-preemptible.

## Compatibility

- No schema, DDL, task, plan, approval, recovery, scheduler, lock, checkpoint,
  or event format changes are required.
- Public engine method signatures and return types are unchanged.
- Healthy long-running owners continue to renew beyond the initial lease.
- Expired/crashed owners remain reclaimable.
- Standalone/private paths without a goal-run claim retain existing behavior.
- ADR-049 latest-plan checks, ADR-050 ordered recovery, ADR-051 exact task
  claims, task revision CAS, scheduler leases, and mutation locks remain
  independent backstops.

## Explicit limitations

- Arbitrary capability code already running is not preempted.
- Hard process death after an external effect but before durable SQLite state
  remains governed by documented at-least-once semantics.
- Lease validation and external capability invocation cannot be one SQLite
  transaction; the guard is renewed at the immediate pre-invocation boundary,
  while scheduler and mutation ownership provide their existing independent
  fences.
- Coordination remains scoped to engines sharing the SQLite database. No
  external fencing-token service or distributed coordinator is introduced.
- Historical stale approval/progress/blocker scoping and plan-version-scoped
  completion races are separate decisions and are not changed here.

## Test strategy

Focused tests prove:

1. a stale planner cannot publish equivalent plan v2 after takeover, and the
   non-retry-safe effect occurs once;
2. ownership lost after one already-started step retains that result but stops
   before the next capability invocation;
3. a stale stored-plan reader cannot pass the exact task claim after takeover;
4. bulk ownership loss is isolated per goal;
5. a live lease for another goal cannot satisfy the guard or mutate task/
   blocker state;
6. stale ownership cannot deny a current approval, alter required recovery, or
   rewrite their task/blocker mirrors;
7. healthy heartbeat renewal and legitimate expiry reclaim remain unchanged;
8. latest-plan, publication, exact-task, task-CAS, scheduler, mutation-lock,
   approval, recovery, pause/cancel, bulk, and full regressions remain green.

## Verification

- Read-only baseline: **1,432 passed, 2 skipped**.
- ADR-052 ownership-fencing tests: **6 passed**.
- Goal/task lease, latest-plan, publication/reconstruction, task-CAS,
  scheduler/concurrency, mutation-lock/waiter, recovery, approval,
  pause/cancel, and bulk regressions: **402 passed**.
- Complete suite after implementation: **1,438 passed, 2 skipped**.
