# ADR-025 — Cross-Goal Durable Concurrency

- **Status:** Approved & implemented (2026-08-16)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Problem

ADR-024 bounded in-process concurrency to the steps of ONE task in ONE
engine instance. A goal's steps ran concurrently, but goals ran one at a
time: `run_goal`/`run_task` drove a single task through the shared
scheduler serially. Multiple goals could not safely share one scheduler,
and there was no durable record of what the scheduler had admitted, was
running, or had finished — a scheduler process that died left only the
task snapshots behind, with no way to observe or reclaim its in-flight
work (no immortal-RUNNING guarantee at the scheduler layer).

## Goals / non-goals

**Goals**

- Multiple goals/tasks share ONE bounded in-process scheduler with a global
  `max_concurrency`; independent tasks execute concurrently; total running
  workers never exceed the bound.
- A durable scheduler/work registry (SQLite) records every admission with
  bounded metadata: ids, task/goal/step references, scheduler + worker
  identity, timestamps, lease deadline, bounded error text.
- Restart/crash recovery for the scheduler layer: stale leases reclaimed,
  dead schedulers' queues abandoned, completed mutations never replayed.
- Fairness across goals: a goal with many runnable steps cannot starve a
  goal with few (round-robin admission, bounded window).
- Observability: `arion scheduler status|workers|queue|show|reclaim`.

**Non-goals**

- No distributed execution, no subprocess-based worker model, no unbounded
  threads, no busy-spin.
- No new capabilities, no GUI/browser/voice/wake-word/tool-catalog.
- No change to authorization, approvals, recovery, mutation locks, waiter
  fairness, or ActionSpec fingerprints (ADR-019..023 invariants immutable).
- The scheduler registry is NOT a task/checkpoint replacement: the durable
  per-step task state (ADR-004/016) remains the unit of restart for what
  actually executed; the registry is the unit of restart for what the
  scheduler admitted.

## The durable scheduler registry

`arion/state/scheduler_work.py` + a `scheduler_work` table in
`SQLiteStorage` (implementing the `SchedulerRegistry` protocol — the
engine/CLI never touch SQLite directly):

- **States (explicit, fail closed):** `QUEUED → RUNNING → COMPLETED | FAILED`,
  `QUEUED → CANCELLED | ABANDONED`, `RUNNING → ABANDONED` (stale lease).
  Terminal states are final. Every illegal transition raises the typed
  `SchedulerStateError` carrying the actual durable state; unknown ids fail
  closed; registry-level failures raise `SchedulerRegistryError`.
- **Bounded metadata only:** work/task/goal ids, step index, scheduler id,
  worker id, status, attempts, timestamps, lease expiry, error ≤ 500 chars.
  NEVER threads, callables, stack traces, capability outputs, model output,
  prompts, file contents, or secrets (regression-tested).
- **Lifecycle mirror:** the engine creates a QUEUED row at admission
  (`_enqueue_step`); the worker marks it RUNNING (with a lease) before the
  step's pipeline starts and terminal afterwards (`_run_step_worker`); a
  failed registry mirror FAILS THE STEP CLOSED (it never executes);
  `shutdown()` cancels this scheduler's QUEUED rows (cancelled rows can
  never run).
- **Restart reclamation:** engine construction reclaims expired RUNNING
  leases → ABANDONED (no immortal RUNNING worker) and abandons QUEUED rows
  owned by a different (presumed dead) scheduler. The CLI's `scheduler`
  command constructs its engine as a PASSIVE observer
  (`scheduler_reclaim_on_start=False`) so inspecting a live engine's queue
  never abandons it.

## Worker lifecycle

- One `StepScheduler` per engine, `max_concurrency` workers (bounded thread
  pool, no subprocess, no daemon surviving `shutdown()`), deterministic
  injectable clock/sleeper.
- The scheduler is the ONLY source of worker lifecycle state. Nothing in
  memory, beliefs, preferences, strategy, guidance, model output, approval
  metadata, recovery metadata, queue position, or worker identity can
  transition a registry row, claim a worker, cancel work, or mark a step
  complete (Phase H adversarial tests).
- A worker failure outside the step's own error handling is recorded and
  re-raised by `run_until_done` in the caller thread (ADR-024 crash
  semantics preserved).

## Cross-goal scheduling

`ArionEngine.run_tasks(task_ids)` drives multiple tasks through the one
scheduler; `run_goals(goal_ids)` adds the goal lifecycle (ADR-016/017:
evaluate → plan → execute → replan/complete) on top:

- **One round = one admission batch.** Each round, every active task
  computes its dispatchable steps (dependencies terminal-success/SKIPPED,
  capability present, approval gate satisfied, lock gate considered); the
  round admits at most `max_concurrency` steps total, then drains.
- **Fairness rule (documented, strictly scheduler coordination):** rounds
  rotate the task order round-robin; per task per round at most
  `ceil(max_concurrency / active_tasks)` steps are admitted. A task with
  one ready step gets a worker within the first round regardless of another
  goal's backlog (bounded window = one round; regression-tested).
  Fairness never enters authorization: `planning != authorization`,
  `scheduler != authorization`.
- **The cursor step is always admitted** unless parked (below) — this is
  how approval requests are raised and how the serial path behaves.
- **Blocked tasks consume no worker:** approval-pending, recovery-gated,
  and missing-capability goals are never admitted; their steps stay
  PENDING and the goal stays durably blocked. `run_goals` returns cleanly
  (no spin) and re-invokes when the blocker resolves.
- **Per-task dependency isolation:** `_step_deps_terminal` evaluates only
  the step's own task's steps; a step of goal A can never satisfy or bypass
  a dependency of goal B, even with identical step indices/capabilities/
  resources (Phase C tests).

## Safe parking (lock interaction)

A mutating step whose canonical resource is actively locked by ANOTHER task
is **parked** instead of dispatched: `_park_on_lock` registers a DURABLE
FIFO waiter row (existing ADR-023 queue, head-gated acquisition), persists
the bounded wait metadata (deadline/attempts preserved across restarts),
sets the goal's lock_contention blocker, and consumes NO worker and no
registry row. When the lock frees, a later round dispatches the step and
`_acquire_mutation_lock` picks up the persisted waiter row and acquires
head-gated. A parked step whose deadline elapsed fails durably (typed
timeout, no recovery record) — the DURABLE WAITER ROW is the deadline
authority (Phase H: a forged task-level deadline cannot extend a wait).

- The scheduler never acquires a mutation lock; it never bypasses the FIFO
  queue; worker availability is never a substitute for queue order.
- Mutation pipeline unchanged: plan → authorize → approval → live
  re-authorization → lock queue → acquire → execute → verify →
  release/handoff (ADR-021/022/023 exactly; `permission.checked` precedes
  `mutation.lock.acquired` per step, audited).

## Restart / reclamation

- Death while QUEUED: the dead scheduler's QUEUED rows are abandoned by the
  next engine construction; the tasks' steps are still PENDING and re-run
  the full fresh pipeline (new rows).
- Death while RUNNING: the expired lease is reclaimed → ABANDONED (no
  immortal RUNNING); the step re-runs with a fresh authorization/
  recovery path.
- Crash after mutation-lock acquisition, before mutation: the stale lock is
  reclaimed through the EXISTING lock store (`reclaim_stale_locks`/
  `reclaim_expired`) — never a second authority; the re-run acquires a
  fresh lock and mutates exactly once.
- Completed mutations are never replayed: a step durably SUCCEEDED is never
  re-dispatched (per-step task state is the authority; ADR-010
  at-least-once contract unchanged).
- Approval state, mutation-lock state, and FIFO waiter positions all
  survive a restart (durable rows), and abandoned work requires fresh live
  authorization (Phase E tests, including a real-subprocess crash test).

## Interaction with approval / recovery

- Approval: an approval-pending step is not admitted; the goal is durably
  BLOCKED (approval_pending) and consumes no worker; resolving the durable
  approval resumes the exact step. Stale/expired/denied approvals behave
  exactly as ADR-018/019 (fingerprint-matched; no reuse across goals).
- Recovery: an open recovery blocks its goal before planning/execution
  (`_block_on_open_recovery`); only `acknowledge_recovery` through the
  recovery registry clears it (a forged acknowledgement cannot; Phase H).

## Authorization boundary

Explicit and regression-tested:

> Scheduler coordinates execution. Durable lock store coordinates mutation
> ownership. Authorization is the only authority that permits execution.

**Scheduler state is never authorization state.** The registry's rows say
nothing about whether a step may touch the world; every dispatched step
runs the identical pipeline it ran under serial execution, with its OWN
live authorization check (never reused because another task authorized).
Poisoned memory/beliefs/preferences/strategy/guidance/model output and
forged worker ids, scheduler ids, completion states, queue positions,
leases, approvals, recovery acknowledgements, cross-goal task claims, and
stale checkpoints cannot raise concurrency, bypass a dependency, claim a
worker or a lock, bypass approval, clear recovery, reorder FIFO, or mark a
step complete (Phase H, 13 adversarial tests).

## CLI / observability

`arion scheduler status|workers|queue|show <work_id>|reclaim <work_id>`
(+ `--json`): bounded, metadata-only, secret-free, restart-safe output
(read from the durable registry, not live memory); unknown ids fail closed
(exit 1); `reclaim` moves only a STALE RUNNING row (expired lease) to
ABANDONED and never executes or authorizes anything.

## Known limitations

- Concurrency is per engine/process; no distributed execution.
- Parking covers cross-task lock contention; two steps of the SAME task
  contending on one resource still wait inline on their workers (ADR-024
  semantics, distinct durable waiter rows per step).
- `max_concurrency` is a per-engine construction constant (default 1);
  not dynamically adjustable per task/goal or from model output.
- With approval-required policies, effective concurrency is bounded by
  approval cadence (by design).
- A QUEUED row abandoned at construction can cause one redundant
  re-admission of not-yet-run work (safe: the step was never executed and
  the full pipeline re-runs).
- The registry is observability + reclamation; the durable per-step task
  state remains the authority on what executed.

## Future work

- Cross-process shared scheduler with a heartbeat-based lease owner model
  (the registry already carries scheduler ids + leases).
- Durable per-goal capacity shares (e.g. weighted fairness).
- `arion scheduler` daemon-style `watch` output.
