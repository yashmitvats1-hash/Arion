# ADR-026 — Cross-Process Shared Scheduler with Lease-Based Worker Ownership

- **Status:** Approved & implemented (2026-08-16)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Problem

ADR-025 made the scheduler/work registry durable, but ownership is weak:
`mark_running` writes a worker id + lease with no ownership enforcement, a
worker can complete a row it never legitimately owned, there is no
heartbeat, no atomic claim/handoff, no capacity bound shared across
processes, and engine construction abandons ALL foreign QUEUED rows —
correct for a single-process restart, wrong when two live processes share
one registry. Multiple Arion engine processes cannot yet safely share the
same scheduler/work registry database.

## Goals / non-goals

**Goals**

- Unique durable scheduler identity per engine process (already exists:
  `new_id("sched")` at construction) PLUS a durable **scheduler
  registration** with a lease, so a restarted engine can distinguish a dead
  scheduler's queue from a live one's.
- Explicit **worker ownership leases** on every dispatched work row with
  expiry; a worker that stops heartbeating becomes reclaimable; a stale
  owner can never complete or mutate work after its lease expired or was
  reassigned.
- Minimal **durable heartbeats** (work rows + scheduler registrations),
  bounded and monotonic; forged/future heartbeats and forged lease
  extensions cannot extend ownership.
- **Atomic claim / handoff**: `claim` (by id) and `claim_next` (oldest
  queued for this scheduler) inside `BEGIN IMMEDIATE`; a
  `release_and_claim_next` atomic handoff mirroring the mutation-lock
  store's `release_and_select_next`. Two racing processes → exactly one
  owner; never two live workers believing they own one row.
- **Cross-process capacity**: an optional durable `global_max_concurrency`
  enforced at claim time across ALL schedulers sharing the registry
  (lazy reclaim of expired RUNNING rows inside the claim transaction), so
  N engines cannot turn the ADR-025 per-engine limit into N × limit.
- Crash recovery: death while QUEUED (registration-lease-expired → queue
  abandoned), death while RUNNING (work lease expired → reclaimed), death
  after mutation-lock acquisition (existing lock recovery unchanged),
  completed mutations never replayed, approval/recovery gates survive.
- Adversarial coverage for forged ownership/lease/heartbeat/handoff state.
- ADR-024/025 behavior, demos, and mutation-lock FIFO semantics unchanged.

**Non-goals**

- No distributed execution beyond one shared SQLite registry; no
  subprocess worker model; no unbounded threads; no busy-spin.
- No new capabilities, no GUI/browser/voice.
- No change to authorization, approvals, recovery, mutation locks, waiter
  fairness, or ActionSpec fingerprints (ADR-019..023 immutable).
- No in-memory authority that defeats the durable registry: ownership and
  capacity decisions live in the store's transactions.

## Design

### New durable tables (`scheduler_config`, `scheduler_instances`)

```sql
CREATE TABLE IF NOT EXISTS scheduler_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduler_instances (
    scheduler_id     TEXT PRIMARY KEY,
    pid              INTEGER NOT NULL,
    registered_at    TEXT NOT NULL,
    heartbeat_at     TEXT,
    lease_expires_at TEXT NOT NULL
);
```

`scheduler_config('global_max_concurrency')` = optional cross-process
capacity. `scheduler_instances` = durable scheduler registration; liveness
is registration-lease-based, so `abandon_foreign_queued` only abandons
QUEUED rows whose scheduler has NO live registration (dead process), never
a live peer's queue.

### Registry protocol additions (`SchedulerRegistry`)

- `register_scheduler(scheduler_id, pid, lease_seconds, now)`,
  `heartbeat_scheduler(scheduler_id, lease_seconds, now, max_lease)`
  (bounded + monotonic; unknown id → False), `unregister_scheduler(id)`,
  `scheduler_registration_live(id, now)`.
- `claim(work_id, worker_id, lease_seconds, now, max_lease) -> SchedulerWork`
  — atomic: reclaim expired RUNNING rows, enforce global capacity (if
  configured), transition THIS row QUEUED→RUNNING with owner + lease.
- `claim_next(scheduler_id, worker_id, lease_seconds, now, max_lease)
  -> SchedulerWork | None` — same transaction, oldest QUEUED row for this
  scheduler; None when empty or capacity full.
- `heartbeat(work_id, worker_id, lease_seconds, now, max_lease)` —
  ownership-checked, monotonic (now ≥ started_at), bounded
  (expiry ≤ started_at + max_lease), dead-owner-rejected (expired rows
  cannot be resurrected).
- `release_and_claim_next(work_id, owner_worker_id, status, error,
  scheduler_id, worker_id, lease_seconds, now, max_lease) ->
  (SchedulerWork, SchedulerWork | None)` — one transaction: ownership-
  checked terminal transition + next claim (handoff).
- `mark_terminal(..., owner_worker_id=None)` — RUNNING→COMPLETED/FAILED now
  REQUIRES the current row owner (typed `SchedulerStateError` otherwise);
  QUEUED→CANCELLED/ABANDONED and RUNNING→ABANDONED (reclaim) need no owner.
- `mark_running(..., max_lease_seconds=None)` — lease capped when provided.
- `set_scheduler_global_max(n)` / `get_scheduler_global_max() -> int | None`.

### Engine integration (small, composable)

- `_enqueue_step` → `_admit_step(task, step) -> bool`: find-or-reuse the
  step's QUEUED row, `claim` it atomically (fresh worker id
  `worker:{pid}:{uuid}`), enqueue to the in-process scheduler only when
  claimed; a foreign/forged RUNNING row for the step is reclaimed
  (ABANDONED) so persisted state can never stall or fake execution;
  unclaimed rows stay QUEUED and are retried next round.
- `_run_step_worker(task, step, work_id, worker_id)`: no more
  `mark_running` (the claim already owns the row) — it heartbeats (a
  reclaimed row fails closed here), executes the full pipeline, then
  `mark_terminal(owner_worker_id=worker_id)`.
- Engine construction: register the scheduler; `run_tasks`/`run_task`
  lazily heartbeat the registration. `shutdown()` unregisters. New params:
  `scheduler_global_max_concurrency: int | None` (sets the durable config),
  `scheduler_max_lease_seconds: float | None` (default 10 × lease).
- Progress guards: a round that claims nothing returns cleanly (no spin
  when cross-process capacity is exhausted); `run_goals`/`run_goal` stop
  cleanly on no-progress cycles.

### Capacity semantics

`global_max_concurrency` is an OPT-IN durable config (unset = ADR-025
per-engine behavior preserved exactly). When set, every claim counts
live RUNNING rows across all schedulers inside the same transaction and
grants only below the cap; expired leases are reclaimed in-transaction so
a crashed process never permanently consumes capacity.

### Lease/heartbeat semantics

- Work lease: `started_at + lease_seconds` at claim; `heartbeat` extends to
  `min(now + lease, started_at + max_lease)` but never backwards; rejected
  when `now < started_at` (forged/past), when the owner differs (forged
  worker), or when the lease already expired (stale owner cannot
  resurrect).
- Registration lease: same rules on `scheduler_instances`.
- All clocks are the engine's injectable `_lock_now()` (deterministic
  tests) or wall clock; the STORE writes all timestamps from the `now` it
  is given — bounded by `max_lease`, so forged timestamps cannot extend
  ownership beyond the cap.

### Crash/recovery semantics

- QUEUED + dead registration → abandoned (construction + lazy in claims).
- RUNNING + expired lease → reclaimed to ABANDONED; the task's step is
  still PENDING (per-step task state is the execution authority) and
  re-enters the complete fresh authorization/approval/lock pipeline.
- Mutation-lock-after-crash → existing ADR-020/021 lock store recovery,
  unchanged; completed mutations never replay (durable per-step SUCCEEDED).
- Approval-pending / recovery-gated work survives restart and consumes no
  capacity (ADR-025 gates unchanged).

### Compatibility

- No global cap configured → single/multi engine claims always succeed
  below local admission limits: ADR-024/025 behavior identical.
- `mark_terminal` ownership is a strengthening; ADR-025 tests are updated
  to pass the owner they set (no invariant weakened).
- Mutation-lock FIFO untouched.

## Acceptance criteria (tests-first, per phase)

- **A registry primitives**: registration liveness; claim/claim_next
  atomicity (exactly one owner under race); heartbeat monotonic/bounded/
  ownership-checked; expiry + reclaim; release_and_claim_next handoff;
  illegal transitions typed; stale-owner rejection on terminal + heartbeat.
- **B multi-process**: two engines (in-process, one DB) + two real
  subprocesses racing one queued item → one owner; global capacity never
  exceeded across engines (shared active counter); fair admission; no
  duplicate execution; ownership transfer via handoff.
- **C crash**: subprocess dies QUEUED (dead registration → abandoned),
  dies RUNNING (expired lease → reclaimed), stale heartbeat, stale lease,
  crash after mutation-lock acquisition → no duplicate successful mutation.
- **D adversarial**: forged scheduler ids, worker ids, leases, lease
  deadlines, heartbeat timestamps, completion claims, handoff claims,
  queue positions, ownership transitions, stale checkpoints,
  approval/recovery acknowledgements — none can claim/complete/extend.
- **E integration**: full suite green; all ADR-016..025 demos green; new
  ADR-026 demo (~25-35 deterministic checks); CLI smoke; security scans;
  linear history; clean tree.

## Known limitations / future work

- Same-task execution by two live processes is unsupported (a foreign
  RUNNING row for a PENDING step is reclaimed; per-step SUCCEEDED state
  prevents replays).
- Global capacity is a single durable config; per-goal shares are future
  work (weighted fairness).
- Steps running longer than the lease can have their row reclaimed
  mid-flight; the durable per-step task state remains authoritative.
- Without an explicitly configured `global_max_concurrency`, cross-process
  capacity is bounded per engine only (ADR-025 semantics preserved); set
  the durable cap for multi-process deployments.

## Verification (full gauntlet)

- `tests/test_scheduler_leases.py` (24): registration/liveness/heartbeat;
  claim/claim_next atomicity (thread race -> one owner); heartbeat
  monotonic/bounded/ownership-checked/stale-rejected; stale-owner
  rejection on terminal + handoff; bounded `mark_running`; global capacity
  in-transaction; registration-liveness-keyed abandonment.
- `tests/test_multi_process_scheduler.py` (10): two engines share one
  registry; global cap across engines (max_active never 2+2); REAL
  subprocess race -> exactly one owner; no duplicate execution; atomic
  handoff; subprocess death while QUEUED (registration lapse) and while
  RUNNING (lease reclaim); stopped-heartbeat reclaim; death holding a
  mutation lock -> exactly one successful mutation; cross-process fair
  admission (bounded window).
- `tests/test_scheduler_lease_adversarial.py` (11): forged scheduler/
  worker ids, forged lease seconds (capped), forged heartbeat timestamps
  (past/future), stale-owner completion/re-claim/handoff, transitions out
  of terminal states, poisoned model output cannot skip claim/lease/live
  authz, forged registry rows never fake execution, stale checkpoints.
- `scripts/demo_adr026_cross_process_scheduler.py` (33 deterministic
  checks, offline; real SQLite transactions across store handles).
- Full suite: 691 passed / 2 skipped (baseline 646); all ADR-016..026
  demos pass; CLI smoke passes; security scans clean; linear history.
