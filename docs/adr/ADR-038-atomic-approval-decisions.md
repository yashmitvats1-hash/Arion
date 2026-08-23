# ADR-038 — Atomic Durable Approval Decisions

- **Status:** Approved and implemented (2026-08-21)
- **Scope:** Approval/task consistency, conditional decisions, and crash reconciliation

## Context

Arion's durable approval queue and task snapshots represented one logical human
decision in two stores:

- `approval_requests`: pending/approved/denied/expired decision record;
- `tasks.snapshot`: awaiting/running/failed state plus a mirrored approval used
  by the execution path.

Before this ADR, request status was committed first, events were emitted, and
task state was saved last. Each operation used an independent SQLite commit.
Approval updates were unconditional, pending creation had no transactional
deduplication, and task execution trusted the mirrored approval rather than
rechecking the durable request row.

The Phase 30 baseline passed with **1,347 tests and 2 skips**.

## Demonstrated behavior

- Normal pending approval survives restart and remains non-executable.
- Changed resource/spec/policy invalidates an old fingerprint before execution.
- Sequential repeated decisions fail closed as "already resolved".
- In 30 real concurrent approve-vs-deny runs, 6 ended split-brain:
  approval `approved` with task `failed`, or approval `denied` with task
  `running` and an approved mirror. In the latter case `run_task()` executed
  the capability despite the durable approval row being denied.
- A simulated failure after approval-row commit but before task save left
  `approved + awaiting_approval`; restart remained stuck and retrying approval
  was rejected as already resolved.
- A failed `create_request()` was swallowed, leaving an awaiting task with no
  durable request and no resolvable operator action.
- The store accepted two pending rows for the same task/step/fingerprint.
- A configured TTL was not enforced by decision APIs; an elapsed request could
  be approved unless an explicit expiry sweep ran first.
- Cancelling a goal did not terminalize its awaiting task/request. Approving the
  old request and directly resuming the task executed the capability under a
  cancelled goal.
- A terminally failed task could leave a permanently pending orphan request.

These are correctness and authorization issues, not merely stale UI data.

## Ownership and invariants

- `ResourcePolicy` and live `ActionSpec` remain authoritative for current
  authorization metadata.
- `approval_requests` is authoritative for the human decision.
- `tasks.snapshot` is authoritative for executable task lifecycle.
- A transition that changes both must commit atomically in SQLite.
- The task mirror is a recovery/read model and may authorize only when its
  durable request is also approved (legacy immediate-handler records without a
  request ID remain compatible).
- One PENDING request is canonical per task/step/fingerprint.
- Only PENDING may transition to APPROVED, DENIED, or EXPIRED.
- Same-outcome retries are idempotent; opposite outcomes fail deterministically.
- Terminal/cancelled goal or task state can never be revived by approval.
- TTL is enforced at the decision boundary, not only by maintenance sweeps.

## Decision

### 1. Conditional store operations

Extend the approval persistence seam with SQLite-backed primitives:

- transactionally create-or-adopt the canonical pending request under
  `BEGIN IMMEDIATE`;
- conditionally transition one request only from PENDING;
- atomically transition a PENDING request and its AWAITING task snapshot in one
  transaction, guarded by task status and `updated_at` compare-and-swap;
- reconcile an already-decided legacy request with a still-awaiting task
  without changing the decision.

No schema migration is required. Existing rows remain readable.

### 2. Engine decision state machine

`resolve_approval_request` and expiry use the conditional operations:

```text
PENDING -> APPROVED  + task AWAITING -> RUNNING
PENDING -> DENIED    + task AWAITING -> FAILED
PENDING -> EXPIRED   + task AWAITING -> FAILED
```

The request and task commit together before audit events or goal-blocker
cleanup. A crash after commit leaves consistent durable authority; missing
observability/blocker cleanup is recoverable and cannot alter execution.

A concurrent loser reloads the committed decision. Same outcome is idempotent;
a conflicting outcome raises `ApprovalError` and cannot overwrite the winner.

### 3. Reconciliation and terminal guards

- Already-approved + awaiting legacy crash state reconciles task to RUNNING.
- Already-denied/expired + awaiting/running state reconciles task to FAILED.
- `_approved_record_for` verifies a durable request ID is still APPROVED before
  its mirror can authorize.
- Pending requests attached to terminal tasks/goals are conditionally denied;
  approval cannot revive them.
- Goal cancellation is checked by the engine decision/resume path.

### 4. Creation failure and deduplication

Pending creation returns the canonical row. Concurrent creators serialize and
adopt one existing matching PENDING row. If persistence fails, the step/task
fail closed rather than entering an unresolvable awaiting state.

### 5. Expiration at decision time

When TTL is configured, resolution checks age before approval. An elapsed
request atomically expires/fails the awaiting task and cannot be approved even
if no sweep ran. Sweeps use the same conditional transition, so expiry and a
human decision have one deterministic winner.

## Compatibility

- Existing pending/approved/denied/expired rows remain readable.
- Legacy exact-resource and new hash-based fingerprints remain accepted.
- Existing immediate `ApprovalHandler` records without durable request IDs keep
  their behavior.
- ADR-044 later restricted compatibility updates to same-status summary refresh
  and request-only transitions to DENIED/EXPIRED cleanup; historical raw
  APPROVED split rows remain reconcilable, but current compatibility APIs cannot
  manufacture them.
- ADR-049 rejects approval/reconciliation when the referenced task implements an
  explicitly superseded plan; a pending request is denied atomically with its
  task before any obsolete effect can execute.
- No destructive migration, workflow engine, voting model, or policy redesign.
- Alternate stores may implement the conditional protocol; the default SQLite
  path is authoritative. Missing atomic support fails closed for durable queue
  decisions rather than silently using a split transaction.

## Test strategy

Tests prove:

1. restart with pending approval remains stable;
2. concurrent approve/deny yields one winner and matching task state;
3. denied durable rows cannot execute through an approved mirror;
4. same-decision retry is idempotent and opposite retry fails;
5. simulated post-decision event failure leaves request/task consistent;
6. approval creation failure fails the task without an orphan wait;
7. concurrent duplicate creation adopts one canonical request;
8. elapsed TTL cannot be approved without a prior sweep;
9. cancelled/terminal goals/tasks cannot resume through old approval;
10. legacy split states and legacy fingerprints reconcile safely;
11. existing stale-resource/policy, scheduler, expiry, and restart suites remain
    green.

## Explicit deferrals

- Notifications, assignment, escalation, multi-party voting, and delegation
  workflows.
- Distributed consensus or approval decisions spanning multiple databases.
- Generic state-machine/workflow frameworks.
- Historical event backfill and automatic deletion of old approval records.
- Redesigning authorization policy or human identity semantics.

## Verification

- Before implementation: **1,347 passed, 2 skipped**.
- ADR-038 atomic-approval tests: **11 passed**.
- Focused approval, expiry, cancellation, scheduler, lock, CLI, stale
  fingerprint, and restart regressions: **165 passed**.
- Complete suite after implementation: **1,358 passed, 2 skipped**.
- Concurrent approve-vs-deny tests now produce one conditional winner and a
  matching task mirror; the loser receives `ApprovalError` and cannot overwrite
  or execute through the committed decision.
