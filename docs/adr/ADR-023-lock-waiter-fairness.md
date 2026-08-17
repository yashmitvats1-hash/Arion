# ADR-023 — Fair, Durable Mutation-Lock Wait Queues

- **Status:** Approved & implemented (2026-08-10)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

ADR-022 gave a task that hits a mutation-lock contention a bounded wait with
deterministic backoff, but the wait was first-come-first-served only by
timing luck: every waiter polled `acquire` and the fastest poller won. With
two or more processes waiting on the same canonical resource, a newer waiter
could repeatedly win the lock before an older waiter that had been waiting
longer — a livelock/unfairness hazard, and hard to reason about across
restarts.

This milestone introduces a durable, FIFO wait queue for mutation locks:
every waiter gets a persistent position, the oldest eligible waiter is the
only one allowed to acquire, positions survive process restarts, and timeout/
terminal waiters leave the queue cleanly without corrupting the positions of
the rest.

The architectural constraint is unchanged and explicit:

- **Fairness is a coordination concern, never an authorization concern.** The
  queue decides who gets the *opportunity* to acquire a lock. It is not part
  of the authorization fingerprint, the policy decision, the actor identity,
  the ActionSpec, the approval semantics, or the recovery semantics.
- The lock store remains the sole authority for lock ownership; the live
  authorization layer remains the sole authority for execution.

## Decision

### Durable FIFO wait queue (`mutation_lock_waiters`)

- One row per waiter: waiter_id, resource_kind, resource (CANONICAL),
  task_id, goal_id, step_index, seq (durable FIFO position, 1-based per
  resource), enqueued_at, deadline, attempts, next_retry, status
  (`queued | acquired | timed_out | cancelled`), created_at, updated_at.
  Bounded identifiers/timestamps only — never file contents, prompts,
  secrets, or model output.
- **Atomic enqueue:** `enqueue_waiter` computes `1 + MAX(seq)` for the
  resource inside `BEGIN IMMEDIATE`, so concurrent enqueues from different
  processes get distinct, commit-ordered positions.
- **Rows are append/audit-safe:** never deleted, only transitioned
  `queued -> acquired | timed_out | cancelled`. Eligibility for acquisition
  is exactly `status == queued` plus deadline/task checks.

### Head-gated acquisition (store authority)

- `acquire(..., waiter_id=...)` only succeeds when the caller is the HEAD:
  the oldest eligible waiter for the resource. The head check runs INSIDE the
  same `BEGIN IMMEDIATE` transaction as the lock insert (with the UNIQUE
  constraint still guarding against a held lock), so there is no
  peek-then-acquire race and a newer waiter can never overtake an older one.
- Eligibility: `status == queued AND deadline > now AND task not terminal`
  (JOIN against the tasks table). Expired waiters are marked `timed_out` in
  their OWN committed transaction BEFORE the head check (so the hygiene
  update survives a failed acquire), and terminal-task waiters are skipped by
  the JOIN — the head is always recomputed, so removing/expiring the head
  automatically promotes the next eligible waiter without rewriting any
  positions.
- **Acquire + dequeue is atomic:** the winning waiter transitions to
  `acquired` in the same transaction as the lock insert, so a concurrent
  observer can never see both a held lock AND a still-queued head.
- Omitting `waiter_id` preserves the ADR-021 immediate (non-queue) semantics.

### Release handoff

- `release_and_select_next` runs the ownership check, the lock deletion,
  expired-waiter cleanup, and the next-head selection in ONE transaction —
  release + next-waiter selection is atomic, with no check-then-act window.
- The engine uses it when releasing after a mutation; the selected next head
  is exactly what a subsequent `peek_waiter` returns.

### Engine integration

- With bounded waiting (`lock_wait_max_seconds > 0`), the engine enqueues a
  durable waiter on first contention and reuses it across restarts (same
  waiter_id/position; the deadline and attempt budget are preserved — never
  reset). `task.lock_wait` now carries `waiter_id` and `position`, mirrored
  on the goal's `lock_contention` blocker.
- The wait loop: check deadline (timeout → dequeue `timed_out`, durable typed
  `MutationLockTimeoutError`, no recovery record) → head-gated acquire →
  on refusal (not-head or contended) persist + backoff + retry (coordination
  only — never the mutation/plan/approval).
- **Fairness never grants authorization:** after a waited acquire the engine
  still re-validates the LIVE ActionSpec/policy (ADR-022 `_revalidate_
  before_mutation`); queue position is not in the fingerprint.
- A terminal task cancels its queued waiters (`cancel_waiter_for_task`,
  `cancelled`) so a finished task can never block the queue; the eligibility
  JOIN additionally guards against terminal-task waiters from crashed
  processes.

### Events

`mutation.lock.queued` (enqueue, bounded metadata: waiter_id, position,
resource, deadline) joins the existing `requested/acquired/contended/
waiting/retry/timeout/reclaimed/released` vocabulary — audit order now tells
the full story: requested → queued → waiting → retry → acquired → released.

### CLI

- `arion locks waiters` exposes queue position, waiter_id, attempts,
  deadline, next_retry, status (bounded, secret-free).
- `arion locks queue <resource> [--kind]` lists the durable queue for a
  resource (all statuses, oldest first); an empty queue is a valid safe
  result.
- `arion locks show <id>` resolves a lock id, a waiter id, or a task id.
- No CLI operation grants authorization, forces ownership, or mutates queue
  ownership; unknown ids fail closed (rc=1).

### DoD demo

`scripts/demo_adr023_lock_fairness.py` (real subprocesses, one shared DB):
FIFO handoff (A holds → B(1), C(2) queue → A releases → B acquires before C,
each mutates once); restart survival (B killed mid-wait keeps position 1 and
still wins over C after restart); timeout (typed durable failure, no
mutation, no recovery, queue left cleanly); live re-authorization after a
queue wait (ActionSpec tightens mid-wait → denied → fresh path); adversarial
(poisoned memory/model position/owner/priority claims are store-ignored).

## Consequences

- Multiple processes contending for one canonical resource are served in
  durable FIFO order; a newer waiter can never overtake an older one, even
  across restarts.
- Timeout/terminal waiters leave the queue cleanly; removal never corrupts
  remaining positions.
- Release + next-waiter selection is atomic at the SQLite layer.
- All ADR-019/020/021/022 invariants are preserved: stale/expired/denied
  approvals never reach lock acquisition; recovery gates hold; contention
  never creates a recovery record; the lock store is the only lock/queue
  authority; no capability-specific `"path"` fallback; no shell/subprocess;
  no raw contents/secrets in queue or audit records.

## Deferred

- True parallel in-process execution (still deferred; fairness is
  cross-process coordination).
- Distributed queues (Redis/etcd) — SQLite transactions suffice while the DB
  is the single coordination authority.
- Queue cleanup/pruning of terminal waiter rows (rows are retained for audit;
  a bounded retention policy is a future operational concern).
- Priority/fairness beyond FIFO (deliberately: FIFO is the fair default and
  keeps fairness out of any authorization-adjacent logic).
