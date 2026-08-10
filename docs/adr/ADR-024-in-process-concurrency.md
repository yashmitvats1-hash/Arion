# ADR-024 — Bounded In-Process Concurrency via the Durable Lock + FIFO Queue

- **Status:** Approved & implemented (2026-08-10)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

Up to ADR-023 the engine executed a task's steps strictly one at a time, in
plan order, on the calling thread. Steps that were logically independent
(same goal, no dependency edge, different resources, read-only work) still
ran serially. This milestone makes independent steps run **concurrently
inside the process** — bounded, deterministic, and without weakening any
invariant from ADR-019..023.

The fundamental constraint from ADR-019/021/022/023 is preserved and made
explicit for concurrency:

- **Authorization stays sole.** A step may execute only after its own live
  policy decision and its own approval check. No decision made for one step
  (or one task, or one process) is ever reused for another. Concurrency is
  an *execution* property; it is not and never becomes an *authorization*
  property.
- **The durable mutation lock stays sole ownership authority.** Two steps
  that mutate the same canonical resource serialize through the existing
  SQLite `BEGIN IMMEDIATE` lock store, exactly as two processes would. The
  in-process scheduler never re-implements locking, never bypasses the
  queue, and cannot transfer or forge ownership.
- **The durable FIFO queue stays sole waiter-order authority.** When two
  mutating steps contend on one resource, they join the same durable queue
  and acquire in position order — identical to cross-process waiters.

## Decision

### Scope: the unit of concurrency is the in-process step

- `ArionEngine(max_concurrency=N)` runs up to `N` *steps* of a task
  concurrently on scheduler worker threads. **Default `1` = the historical
  fully-sequential behavior**, bit for bit.
- Concurrency is bounded **per engine/process**. There is no distributed
  execution, no multi-goal execution, no new capabilities, no shell, no
  subprocess, no browser/GUI/voice.
- A capability may execute concurrently only when its security-relevant
  resources are protected by the existing durable mutation lock:
  independent **read-only** steps may overlap; **mutating** steps serialize
  per canonical resource through the lock store; mutating steps on
  *different* resources may overlap (each holding its own lock row).

### The scheduler (`arion/orchestration/scheduler.py`)

A small, explicit `StepScheduler` with a bounded worker pool:

- **Concepts:** `WorkItem` (label/task_id/step_index/fn) with a lifecycle —
  `runnable → running → completed | failed | cancelled`. The scheduler is
  the **only source of worker lifecycle state**; nothing in memory,
  cognition, strategy, model output, approval metadata, recovery metadata,
  queue position, or worker identity can manufacture concurrency, claim a
  worker, cancel work, or mark a step complete.
- **Deterministic hooks:** injectable `clock` and `sleeper`; tests and the
  demo use barriers and counters to *prove* overlap/serialization.
- **Lifecycle:** `enqueue`, `cancel` (queued-only, advisory), `cancel_all`,
  `run_until_done`, `shutdown(timeout)` — shutdown cancels queued work,
  joins bounded workers, rejects new work (fail closed), and leaves **no
  orphan worker** that could mutate after it returns.
- **Failure propagation:** a worker exception outside the step's own error
  handling (e.g. the injectable sleeper raising to simulate a crash) is
  recorded and **re-raised by `run_until_done` in the caller thread**,
  preserving the ADR-019..022 crash/restart contract: the durable task
  state is left exactly as the worker last persisted it.
- The scheduler holds **no SQLite transaction** while a work item runs; the
  lock store commits before any capability executes.

### Dependency-aware dispatch (`run_task`)

- The **cursor** (lowest-index pending step) is always dispatched — this is
  how approvals are requested and how the historical serial path behaves.
- Additional ready steps are dispatched concurrently only when:
  1. every `depends_on` prerequisite is terminal-success or explicitly
     `SKIPPED` (dependency constraints stay authoritative);
  2. with waiting disabled, the step would not collide with this round's
     own lock choice or an in-flight lock held by this engine;
  3. the live policy decision is not `REQUIRE_APPROVAL` without an existing
     approved record whose fingerprint still matches the live ActionSpec.
- The dispatch set is truncated to `max_concurrency`; each dispatched step
  runs the **full per-step pipeline** in its worker: live authorization →
  approval → pre-mutation revalidation → durable mutation lock → FIFO queue
  → capability → verification. **Every concurrent execution gets its own
  live authorization check; no authz decision is reused.**
- A blocked step (approval pending / lock contention / recovery-required)
  never stalls unrelated ready steps: the dispatch loop keeps the scheduler
  fed with ready work while a sibling waits, and the cursor's durable
  stop/re-check semantics (approval-pending stop, lock-wait checkpoint,
  recovery gate) are unchanged.

### Durable execution state (restart safety)

- **Per-step terminal status is persisted immediately from the worker**
  (`_run_step_worker`), so a crash mid-round never replays a completed
  mutation: the durable per-step state is the unit of restart, not the
  whole round. Only bounded metadata is written — task/step ids, statuses,
  timestamps; **never thread objects, stack traces, capability outputs, raw
  contents, or arbitrary model output** (the task snapshot is the existing
  durable record; nothing new is added to it).
- Restart semantics are the existing at-least-once/recovery contract: a
  step the engine had durably marked `SUCCEEDED` is never re-executed; a
  step interrupted mid-flight (still `PENDING` with bounded `lock_wait`
  metadata) resumes and runs again exactly once. Recovery fencing is
  authoritative and unchanged; lock ownership is recovered only through the
  lock store.

### Cancellation / shutdown

- **Advisory before execution:** a queued item can be cancelled by the
  scheduler and never runs. Once a mutation has acquired the lock and begun,
  cancellation cannot pretend it did not happen — the mutation's outcome is
  the capability's outcome.
- A mutation failure still creates durable `recovery-required` state per
  ADR-020/021/022; cancellation never clears recovery.
- Durable FIFO waiters can be cancelled (`queued → cancelled`); a cancelled
  waiter can never later acquire, and the engine enqueues a **fresh**
  waiter (new position) on resume instead of reusing the cancelled id.
- Waiter identity is **per step**: `task.lock_wait` is step-scoped
  (`step_index` in the durable metadata), so two steps of the same task
  waiting on the same resource get distinct durable waiter rows and can
  never reuse or clobber a sibling's FIFO position (regression-tested).
- `shutdown()` waits only for bounded active work, then joins; no orphan
  worker mutates after shutdown. The CLI calls `engine.shutdown()` before
  exiting.

### Thread safety of the store

- `SQLiteStorage` is now process-thread-safe: one connection guarded by a
  `threading.RLock` (`@_threadsafe` on every public method) with
  `check_same_thread=False`. **Cross-process authority is unchanged** —
  `BEGIN IMMEDIATE` + WAL are still the cross-process arbiters, so an
  in-process scheduler and other processes (e.g. the ADR-023 CLI) compete
  on exactly the same footing.

## Why concurrency doesn't grant authorization

Every step's capability execution is gated by the identical pipeline it ran
under serial execution: the registry's live ActionSpec → the policy's live
decision → the approval store's durable, fingerprint-matched approval → the
lock store's durable acquisition → the capability's own verification. The
scheduler only decides *when and where* a step's thread runs; it never
decides *whether* the step may touch the world. Poisoned memory, beliefs,
strategy, guidance, model output, approval metadata, recovery metadata,
queue positions, worker identities, and cancellation claims are all ignored
by that pipeline (regression-tested in `tests/test_concurrency_adversarial.py`).

## Why the durable SQLite lock stays authoritative

The lock store's `BEGIN IMMEDIATE` insert with a UNIQUE constraint is a
single atomic, crash-safe, cross-process decision point. In-process threads
gain nothing by "helping": the scheduler explicitly *does not* track
resource ownership, does not decide FIFO order, and never checks a lock
before authorizing. Two threads contending on one resource follow the exact
same durable queue + head-gated acquire as two processes, so the strongest
authority (SQLite transactions) remains the only authority.

## Verification

- `tests/test_concurrency_model.py` (16): concurrent reads overlap with
  independent authz; same-resource writes serialize (unique lock, exactly
  two mutations); different-resource writes overlap; read/write same
  resource; dependency ordering authoritative; blocked mutation does not
  stall ready reads; `max_concurrency=1` reproduces serial behavior;
  bounded shutdown; queued cancellation; restart (completed + crash
  mid-flight) never duplicates mutations; approval-pending / recovery-
  required steps consume no worker; FIFO preserved under concurrency;
  shutdown/cancel fail closed.
- `tests/test_concurrency_adversarial.py` (13): poisoned memory/beliefs/
  strategy/guidance/model output/approval/recovery/queue/worker/cancellation
  claims cannot raise concurrency, bypass dependencies, claim a worker or
  the lock, bypass approval, clear recovery, reorder FIFO, or mark a step
  complete; scheduler snapshot is bounded and secret-free; a cancelled
  waiter never later acquires.
- `scripts/demo_adr024_concurrency.py` (30 checks, deterministic, offline):
  A parallel reads; B same-resource FIFO serialization; C different-resource
  concurrency; D blocked mutation does not stall reads; E restart/cancel/
  shutdown with no orphan work.
- Full suite: 572 passed / 2 skipped; all ADR-016..023 demos still pass.

## Limitations

- Concurrency is per-engine/process only; no distributed execution is
  introduced, and the lock/queue design is deliberately the same as the
  cross-process one so the boundary never weakens.
- Reads of a resource being mutated are not mutually excluded by design:
  the mutation lock is a *mutation* lock. A read may observe pre- or
  post-state; correctness of the mutated resource is the lock's contract.
- With approval-required policies, steps pause at approval boundaries and
  resume one at a time, so effective concurrency is bounded by the approval
  cadence — by design, not a bug.
- `max_concurrency` is a per-engine construction constant (default 1); it is
  not dynamically adjustable per task, per goal, or from model output.
