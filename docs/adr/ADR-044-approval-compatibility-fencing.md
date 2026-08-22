# ADR-044 — Approval Compatibility Write Fencing

- **Status:** Approved and implemented (2026-08-22)
- **Scope:** Prevent compatibility APIs from creating or reversing approval authority

## Context

ADR-038 made approval decisions atomic with task state. Current approve, deny,
and expire decisions use `commit_approval_decision()` to update the PENDING
request and AWAITING task revision in one SQLite transaction. Two compatibility
methods remained broader than that authority model:

```text
update_request(request)
  -> unconditional request update by approval_id

transition_request(request, expected_status)
  -> request-only status CAS accepting any target
```

Legacy reconciliation intentionally trusts an already-durable APPROVED row to
repair a historical APPROVED + AWAITING split.

The audit baseline was **1,397 passed, 2 skipped** at local and remote commit
`3e23716`.

## Reproductions

### Stale PENDING reversed atomic APPROVED

A second store loaded a PENDING request. The engine atomically committed:

```text
request: APPROVED
task: RUNNING
actor: operator:alice
```

The stale object then passed through `update_request()` and produced:

```text
request: PENDING
request actor: null
task: RUNNING
```

Live execution remained fail-closed: the durable request cross-check rejected
the approved task mirror and moved the task back to AWAITING_APPROVAL. No
capability ran. However the committed human decision/provenance was lost and the
operator had to approve again.

### Request-only CAS manufactured APPROVED

A PENDING request was changed to APPROVED with
`transition_request(request, PENDING)`, without the task transaction. The normal
same-outcome retry path treated it as a historical split and reconciled the
task:

```text
request-only commit: true
task before retry: awaiting_approval
task after retry: running
mirrored actor: forged:compatibility-writer
final task: completed
capability calls: 1
approval.granted events: 0
```

Reconciliation correctly supports real historical rows, but a current
request-only API could manufacture the authority it trusts.

## Invariants

1. APPROVED can be created only by the atomic request+task decision operation.
2. A stale request object cannot change durable status or decision provenance.
3. Compatibility metadata refresh is non-authoritative.
4. Request-only transitions are cleanup only: PENDING → DENIED or EXPIRED.
5. Legacy persisted APPROVED + AWAITING rows remain readable and reconcilable.
6. Durable request status remains authoritative over task approval mirrors.
7. Denied/expired rows cannot execute; terminal tasks/goals cannot be revived.
8. Live ActionSpec, policy, resource boundary, and fingerprint checks remain
   mandatory after approval.
9. SQLite failure rolls back compatibility metadata/cleanup writes.
10. Task revisions, scheduler ownership, mutation locks, checkpoints, and
    recovery fencing remain unchanged.

## Decision

### Status-preserving compatibility update

`ApprovalStore.update_request()` remains available for legacy metadata callers,
but the default SQLite implementation now updates only:

```text
summary
updated_at
```

Its UPDATE requires the object's status to equal the durable status. It never
writes status, decision actor/time, expiry time, fingerprints, or task state.
Unknown or stale-status objects raise `ApprovalError`.

### Cleanup-only request transition

`transition_request()` now accepts only:

```text
PENDING -> DENIED
PENDING -> EXPIRED
```

These transitions close missing-task, terminal-task, and elapsed-TTL orphan
requests. `PENDING -> APPROVED` raises `ApprovalError`; approved decisions must
use `commit_approval_decision()`.

The cleanup CAS runs under `BEGIN IMMEDIATE`, restores caller timestamps on a
miss/failure, and changes no task state.

### Historical reconciliation remains

`_reconcile_decided_request()` is unchanged. Persisted pre-ADR-038 APPROVED or
DENIED split rows remain compatible. Regression tests inject those historical
rows directly rather than using a current authority-changing compatibility API.

## Compatibility

- No schema or record-format change is required.
- Existing request IDs, fingerprints, summaries, decisions, actors, and times
  remain readable.
- Same-status summary refresh remains supported.
- Existing DENIED/EXPIRED orphan cleanup remains supported.
- Normal atomic approval decisions and idempotent retries are unchanged.
- Alternate stores retain the protocol methods but must enforce the narrowed
  semantics to remain safe.

## Explicit limitations

- Historical database repair/import tooling may still write raw rows outside
  runtime APIs; reconciliation validates task state and live authorization
  before execution.
- Notification delivery and human identity authentication remain outside the
  SQLite transition primitive.
- No voting, delegation workflow, event sourcing, or approval-system redesign
  is introduced.

## Test strategy

Focused tests prove:

1. stale PENDING cannot reverse APPROVED;
2. forged APPROVED cannot pass through `update_request()`;
3. request-only PENDING → APPROVED is rejected and executes nothing;
4. same-status summary refresh preserves status and decision provenance;
5. request-only DENIED/EXPIRED cleanup remains valid;
6. raw legacy APPROVED + AWAITING still reconciles and executes once;
7. SQLite abort preserves prior request status/summary;
8. approval, expiry, fingerprint, goal/task, scheduler, lock, recovery, and full
   suites remain green.

## Verification

- Read-only baseline: **1,397 passed, 2 skipped**.
- ADR-044 approval compatibility tests: **8 passed**.
- Focused approval, expiry, fingerprint, task/goal, scheduler, lock, recovery,
  and crash-consistency regressions: **153 passed**.
- Complete suite after implementation: **1,405 passed, 2 skipped**.
