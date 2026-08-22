# ADR-043 — Atomic Recovery Acknowledgement

- **Status:** Approved and implemented (2026-08-22)
- **Scope:** Make REQUIRED → ACKNOWLEDGED a single-winner conditional transition

## Context

ADR-041 made mutation recovery creation and its failed-task mirror crash
consistent and added stale goal-blocker reconciliation. Recovery acknowledgement
still used a read-check-write sequence:

```text
get_recovery(id)
  -> require in-memory status REQUIRED
  -> set ACKNOWLEDGED + actor/time
  -> update_recovery(row)       # unconditional by id
  -> emit recovery.acknowledged
  -> clear recovery_required blocker
```

The default SQLite update had no expected-status predicate. Goals, tasks,
approvals, locks, and scheduler leases already used conditional transitions.

The audit baseline was **1,392 passed, 2 skipped** at local and remote commit
`c1973cc`.

## Reproductions

### Concurrent acknowledgement had two winners

Two engines/connections loaded the same REQUIRED row before either update. Both
acknowledgements returned success:

```text
successes: 2
errors: 0
durable actor: operator:a
recovery.acknowledged events: 2
event actors: operator:b, operator:a
```

The last writer silently replaced decision provenance. Both operators and both
audit records claimed to be the successful transition.

### Stale REQUIRED snapshot reversed ACKNOWLEDGED

A second connection retained a REQUIRED snapshot. The engine acknowledged the
recovery and transitioned the goal from BLOCKED to ACTIVE. The stale snapshot
was then written with `update_recovery()`.

Observed:

```text
acknowledge returned: acknowledged
goal after acknowledgement: active
durable recovery after stale write: required
durable acknowledged_by: null
```

The valid operator decision was lost. On the next goal run, existing recovery
gating safely reblocked the goal, so no mutation executed, but the operator had
to acknowledge the same recovery again.

## Invariants

1. REQUIRED → ACKNOWLEDGED has exactly one conditional winner.
2. Only the winner emits `recovery.acknowledged` and attempts blocker cleanup.
3. A stale REQUIRED object cannot move ACKNOWLEDGED back to REQUIRED.
4. A forged ACKNOWLEDGED object cannot bypass the explicit transition API.
5. Durable acknowledgement actor/time belong to the winning transition.
6. Sequential or concurrent retries receive a typed already-acknowledged or
   conflict error, preserving existing API semantics.
7. Same-status compatibility metadata refresh cannot change durable status.
8. SQLite failure rolls back status, actor, and time; no success event is
   emitted.
9. Recovery acknowledgement remains non-authoritative for capability execution;
   fresh work still passes live policy, approval, scheduler, and mutation locks.
10. ADR-041 recovery creation/task atomicity and stale-blocker reconciliation
    remain unchanged.

## Decision

### Conditional recovery transition

Add `RecoveryStore.transition_recovery(recovery, expected_status) -> bool`.

The default SQLite implementation supports the only legal transition:

```text
REQUIRED -> ACKNOWLEDGED
```

It runs under `BEGIN IMMEDIATE` and updates with:

```sql
WHERE recovery_id = ? AND status = 'required'
```

A row-count miss rolls back and returns false. Invalid source/target pairs raise
`RecoveryError` without touching state.

### Engine winner handling

`acknowledge_recovery()` requires conditional transition support and fails
closed when absent. After a false result it reloads the row:

- ACKNOWLEDGED → typed already-acknowledged error;
- missing/other state → typed conflict error.

Only a successful transition emits the acknowledgement event and clears the
goal blocker.

### Status-preserving compatibility update

`update_recovery()` remains available for existing metadata refresh callers but
its UPDATE now includes:

```sql
WHERE recovery_id = ? AND status = <object status>
```

It no longer writes status or acknowledgement actor/time. Unknown or
stale-status objects raise `RecoveryError`. Compatibility code may refresh only
the bounded diagnostic reason for the current durable state; it cannot create,
reverse, or rewrite decision authority/provenance.

## Compatibility

- No schema migration or record-format change is required.
- Existing recovery IDs, task links, reasons, actors, and timestamps remain
  readable.
- Existing sequential double acknowledgement still raises a typed error.
- Existing same-status `update_recovery()` callers continue to work.
- Approval, lock, waiter, scheduler, task, checkpoint, and goal schemas and
  transitions are unchanged.
- Alternate recovery stores without conditional transitions make
  acknowledgement fail closed rather than silently retaining the race.

## Explicit limitations

- Notification delivery and external operator identity authentication remain
  outside this transition primitive.
- General audit events remain a separate post-commit observational write; if
  event persistence fails, the durable acknowledgement still wins and retry
  reports already acknowledged.
- No voting, multi-party approval, event sourcing, distributed transaction, or
  workflow redesign is introduced.

## Test strategy

Focused tests prove:

1. concurrent acknowledgements produce one winner, one loser, and one event;
2. winning actor/time remain durable;
3. stale REQUIRED snapshots cannot reverse acknowledgement;
4. forged target status cannot bypass the transition API;
5. same-status metadata refresh remains compatible;
6. SQLite abort leaves REQUIRED state and no acknowledgement event;
7. ADR-041 blocker reconciliation and existing recovery flows remain green;
8. approval, lock, scheduler, task, checkpoint, goal, and full suites remain
   green.

## Verification

- Read-only baseline: **1,392 passed, 2 skipped**.
- ADR-043 conditional acknowledgement tests: **5 passed**.
- Focused recovery, crash consistency, goal, task, approval, lock, scheduler,
  write, and append regressions: **153 passed**.
- Complete suite after implementation: **1,397 passed, 2 skipped**.
