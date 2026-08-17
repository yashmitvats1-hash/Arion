# ADR-022 — Bounded Lock-Contention Waiting/Backoff

- **Status:** Approved & implemented (2026-08-10)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

ADR-021 made mutation locking cross-process-safe, but a task that hit a
contention failed IMMEDIATELY (or, with the lease, only after an operator
released/reclaimed the lock). A short-lived owner (another process holding
the lock for a moment) caused spurious durable failures. This milestone adds
the missing piece: a task that encounters a contention should wait, in a
bounded, deterministic, restart-safe way, and then retry — while never
weakening any authorization/approval/recovery/lock invariant.

Explicit distinctions preserved throughout:

- **lock contention != mutation failure** (no recovery record, no
  `mutation.failed`, the capability never executes during the wait);
- **waiting != authorization** (authorization is re-checked, against the
  CURRENT ActionSpec/policy, before mutating after a wait);
- **recovery != lock release authority** (acknowledging recovery never
  touches locks);
- **approval != lock ownership** (an approved task still contends; an
  approval never reserves a lock);
- **memory/cognition/strategy != security authority** (none can modify the
  wait budget, lock owner, authorization result, or retry state).

## Decision

### Waiting state (durable, coordination-only)

- `Task.lock_wait` metadata: `{resource_kind, resource, deadline, attempts,
  next_retry}` — persisted on the task row AND mirrored on the goal's
  `lock_contention` blocker (which now carries deadline/attempts/next_retry
  and is UPSERTED on every retry so the surface always reflects the latest
  state).
- Entering the wait is NOT a mutation failure: the task stays `RUNNING`, no
  recovery record is created, no `mutation.attempted`/`mutation.failed` is
  emitted, and the capability never executes.
- The wait loop lives in the engine's lock-acquisition phase (ADR-021
  ordering preserved):

      plan -> authorize -> approval if required -> live re-authz
          -> lock contention -> bounded wait/backoff -> retry acquisition
          -> re-validate live authorization -> mutate -> verify -> release

- **Backoff** is deterministic exponential with caps:
  `delay = min(base * 2**(attempt-1), max)`. No jitter (deterministic under
  test). Injectable clock (`lock_clock`) and injectable sleeper
  (`lock_sleeper`) make timing fully deterministic in tests. `base`/`max`
  and the overall deadline (`lock_wait_max_seconds`) are engine config.
  **Never sleep inside a SQLite transaction**: the store commits/rolls back
  before the sleeper is called.
- **`lock_wait_max_seconds <= 0` disables waiting** and reproduces ADR-021's
  immediate, durable contention failure — so existing behavior remains
  available by configuration; the default enables bounded waiting.

### Restart safety

- Every retry persists `task.lock_wait` (task row + checkpoint) and the
  blocker. A restart resumes with the SAME deadline and attempt count — the
  retry budget is never reset. If the deadline has already passed on resume,
  the wait session times out IMMEDIATELY (no fresh window).
- While the resource is still locked, `run_goal` reports the goal as
  durably BLOCKED with next_action `await_lock` and returns without spinning
  or sleeping. When the lock frees, the blocker clears (live lock store is
  the only lock authority) and the waiting task resumes.
- A crashed waiter holds NO lock, so it can never leave an immortal waiter;
  its deadline still bounds any future resume.

### Evaluator distinction

`DeterministicProgressEvaluator` now distinguishes six states: runnable,
awaiting approval (`await_approval`), **waiting for mutation lock**
(`await_lock` + `waiting_for_lock` evidence with bounded metadata),
blocked on missing capability (`resolve_blocker`/replan), blocked on
recovery (recovery_required blocker), and terminal failure. No state permits
an infinite loop: waiting is bounded by the deadline (and backoff sleeps),
approval-pending never spins, and replans are bounded by `max_replans`.

### Authorization freshness after a wait

If the acquisition actually WAITED (contended at least once), the engine
re-builds the authorization request from the CURRENT registry ActionSpec and
policy immediately before mutating, and re-runs the approval seam (canonical
fingerprint comparison). If the approval/authorization went stale during the
wait, the normal fresh authorization/approval path is forced BEFORE any
mutation — and the just-acquired lock is released if the step pauses or is
denied. If nothing changed, the wait costs nothing extra (no duplicate
approval requests). Non-waited acquisitions skip re-validation entirely, so
the ADR-019/020/021 event counts are unchanged on the fast path.

### Events

`mutation.lock.waiting` (first contention of a session), `mutation.lock.retry`
(subsequent, with attempt/deadline/backoff/next_retry), `mutation.lock.timeout`
(deadline expiry; typed `MutationLockTimeoutError`, subclass of
`MutationLockError`). Metadata only — never file contents, prompts, secrets,
or arbitrary model text.

### CLI

`arion locks waiters` and `arion locks show <id>` (lock OR waiter) with
`--json`: task/goal/step, resource, owner/lock id, attempt count, deadline,
next retry time, status. Bounded and secret-free.

## Consequences

- A task that hits a contention waits (bounded) and proceeds when the lock
  frees, instead of failing spuriously; retries are coordination-only.
- The security invariants of ADR-019/020/021 are preserved: stale/expired/
  denied approvals never reach lock acquisition; recovery gates hold;
  contention never creates a recovery record; the lock store remains the only
  lock authority; no capability-specific `"path"` fallback; no shell/
  subprocess.
- Two real subprocesses demonstrate: contention → bounded wait → release →
  successful mutation (exactly once); timeout → durable typed failure with no
  mutation and no recovery; stale authorization mid-wait → fresh
  authorization/approval path before mutating.

## Deferred

- True parallel task execution (still explicitly deferred; waiting is
  coordination across processes, not concurrency within a process).
- Distributed locks / heartbeats (leases with the injectable clock remain the
  bounded staleness mechanism; no active-owner heartbeat yet).
- Bounded acquisition backoff is deterministic-only; jitter is deferred
  until a real distributed deployment needs it.
- Priority/fairness across waiters (first-acquire-wins semantics retained).
