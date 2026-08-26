# ADR-056 — Atomic Terminal-Transition Lineage Fence

- **Status:** Approved and implemented (2026-08-26)
- **Scope:** Close the cross-connection timing gap in plan-lineage fencing at terminal goal transitions

## Context

ADR-054 introduced `expect_plan_version` on `GoalManager.transition()` so that
evaluation-driven terminal transitions (COMPLETED, FAILED) carry the plan version
they evaluated and refuse to commit if a newer plan has since become authoritative.
ADR-055 extended the same fence to the `max_replans_exceeded` failure path.

Both ADRs explicitly documented one residual timing gap (ADR-054 §"Concurrency
limitations"):

```text
Connection A (cognitive_store):

    latest_plan(goal_id)           ← fence READ (autocommit SELECT)
              │
              │  gap — newer plan can commit here on Connection B
              ▼
Connection B (storage):

    BEGIN IMMEDIATE
    UPDATE goals WHERE id=? AND version=?
    COMMIT
```

`SQLiteStorage` (goals, tasks) and `SQLiteCognitiveStore` (goal_plans,
strategy_outcomes) are two separate `sqlite3.connect()` calls to the **same
physical WAL database file**. The lineage read and the goal CAS run on
different connections, so no single transaction spans both.

ADR-049-era invariant tests (Probe H in the post-ADR-055 adversarial audit)
confirmed the gap is reproducible with a deterministic delay injection: if
`claim_goal_plan` commits v2 between the `latest_plan()` SELECT and the
`goals` UPDATE, the stale terminal transition commits, completing the goal
under v1 authority while v2 is already the authoritative plan. The
post-commit `latest_plan()` re-read then attributes `v2 → succeeded` to
strategy outcomes even though v2 was never evaluated or executed.

The feasibility audit (pre-ADR-056) established that the fix is bounded:
both tables (`goals` and `goal_plans`) reside in the same physical database
file, so `storage._conn` can read `goal_plans` inside a `BEGIN IMMEDIATE`
that also performs the `goals` UPDATE. SQLite's database-level write lock
(RESERVED/IMMEDIATE) prevents any concurrent `claim_goal_plan` write from
committing between those two statements on any connection.

The read-only baseline was **1,456 passed, 2 skipped** at local commit
`7f3fd7f` (ADR-053/054/055 docs).

## Problem

The cross-connection timing gap allows a stale terminal transition to commit
when a newer plan version is published between the ADR-054/055 lineage fence
read and the goal lifecycle CAS. This produces:

1. **Goal COMPLETED under stale plan authority.** The goal transitions to an
   irreversible terminal state while a newer plan — never evaluated, never
   executed — is already the authoritative latest plan.
2. **False strategy outcome.** The post-commit `latest_plan()` re-read sees
   the newer version and writes `v_new → succeeded` in `strategy_outcomes`,
   contaminating strategy selection (ADR-015) with provenance that was never
   earned.

The failure was probabilistic in production (sub-microsecond window between
two SQLite statements on different connections) but deterministic under
controlled injection. It is not self-healing: COMPLETED is irreversible, and
the false `strategy_outcomes` row cannot be overwritten (UNIQUE constraint,
idempotent by design).

## Invariant (strengthened)

A terminal goal transition authorized for plan version N commits successfully
if and only if N is the authoritative `MAX(plan_version)` for that goal at
the exact moment the `goals` UPDATE executes — with both the lineage read and
the lifecycle mutation inside the same `BEGIN IMMEDIATE` transaction on the
storage connection.

## Mechanism

### New storage method: `cas_goal_terminal_fenced`

`SQLiteStorage.cas_goal_terminal_fenced(goal_id, expected_goal_version,
expect_plan_version, fields)` runs inside one `@_threadsafe` / `BEGIN
IMMEDIATE` transaction:

1. `SELECT plan_version FROM goal_plans WHERE goal_id=? ORDER BY plan_version DESC LIMIT 1`
   — reads the authoritative latest plan **inside the transaction**.
2. If `latest_plan_version != expect_plan_version` → `ROLLBACK`; return
   `("lineage_mismatch", latest_plan_version)`.
3. `UPDATE goals SET ... WHERE id=? AND version=expected_goal_version`
4. If `rowcount != 1` → `ROLLBACK`; return `("cas_miss", None)`.
5. `COMMIT`; return `("ok", expect_plan_version)`.

Because `claim_goal_plan` (the only production plan-publication path) also
uses `BEGIN IMMEDIATE` on `cog._conn`, and because SQLite's write lock is
database-level (not connection-level), steps 1–5 are serialized with any
concurrent plan commit: either the plan commit runs first (step 1 sees the
newer version and the transition is fenced), or the transition runs first
(the plan commit waits until `COMMIT`). There is no ordering under which both
a newer plan commit and a stale terminal transition commit successfully.

The method reads `goal_plans` via `storage._conn`, crossing the existing
code-level store convention (cognitive vs. state domain). This is acceptable
because:

- Both connections already point at the same physical file; neither schema
  nor DDL changes.
- The read is a pure `SELECT MAX(plan_version)` — no write into cognitive
  tables, no knowledge of cognitive-domain logic.
- The Storage protocol is extended with an optional signature; backends that
  do not implement it retain the prior ADR-054/055 behaviour (fallback in
  `GoalManager.transition()`).

### GoalManager.transition() ADR-056 path

When `expect_plan_version is not None`:

- Detect `cas_goal_terminal_fenced` via `getattr(self.storage, ..., None)`.
- If present: build the fields payload and delegate to the atomic method.
  - `"lineage_mismatch"` → raise `GoalPlanLineageError` (same typed
    exception, same engine catch-sites, same audit events).
  - `"cas_miss"` → reload and retry (same retry loop semantics).
  - `"ok"` → committed. Use the returned `validated_plan_version` for
    `_record_strategy_outcome` instead of a post-commit `latest_plan()`
    re-read. This is authoritative: the version was confirmed at the exact
    moment of commit.
- If absent: fall back to the original two-step behaviour (ADR-054/055),
  preserving backward compatibility for test doubles and legacy stores.

### Fallback path (unchanged)

Storage backends that do not implement `cas_goal_terminal_fenced` continue to
use the two-step `latest_plan()` + `_commit_goal()` path from ADR-054/055.
The typed `GoalPlanLineageError` exception, the engine catch-sites, and all
audit events are identical. The fallback path retains the accepted residual
timing gap documented in ADR-054 §"Concurrency limitations".

## Interleaving proof

| Ordering | ADR-056 outcome |
|---|---|
| A validates, then B starts | A holds `BEGIN IMMEDIATE`; B's `claim_goal_plan` blocks until A commits; B publishes v2 after; ordering safe |
| B commits before A starts `BEGIN IMMEDIATE` | A enters `BEGIN IMMEDIATE`, reads `goal_plans`, sees v2, `"lineage_mismatch"` → fenced |
| B attempts `BEGIN IMMEDIATE` while A holds it | B blocks (database-level lock); after A commits, B publishes v2; ordering safe |
| A attempts `BEGIN IMMEDIATE` while B holds it | A waits; acquires after B commits; reads `goal_plans`, sees v2, `"lineage_mismatch"` → fenced |
| Crash/rollback between validation and commit | SQLite WAL rolls back automatically; goal and plans unchanged; next run retries with fresh state |

No ordering permits v2 becoming authoritative before A's terminal transition commits
while A still successfully commits under v1 authority.

## Strategy-outcome attribution fix

The post-commit `latest_plan()` re-read in the original `transition()` is
replaced with `validated_plan_version` (the version returned by the atomic
commit). This is the version that was authoritative at the exact moment the
`goals` UPDATE committed. The strategy outcome therefore correctly records the
plan that was evaluated and executed — not any plan committed by a concurrent
actor in the milliseconds between commit and the subsequent re-read.

### Informational residual: strategy name

The `plan_version` key in the `strategy_outcomes` row is fully authoritative:
it equals `validated_plan_version`, the version confirmed at the exact moment
the `goals` UPDATE committed. However, the strategy *name* written to that
row is still obtained from a subsequent `self.latest_plan()` call on
`cog._conn`, which is a separate cross-connection read made after the
terminal commit has released its write lock. For FAILED goals (where
`readopt_plan` is a legal operator action), a concurrent plan publication
landing in that narrow window can cause the strategy name to reflect the
newer plan rather than the plan that was actually evaluated. This does
**not** affect authoritative goal state, the `plan_version` attribution
invariant, or the correctness of the `strategy_outcomes` UNIQUE key.
`strategy_outcomes` remains informational and best-effort, and this residual
is pinned by the Category-B invariant tests (see §Verification above).

## Compatibility

- No schema change. No DDL. No migration.
- `GoalPlanLineageError` remains the typed exception at all boundaries.
- All engine catch-sites for `GoalPlanLineageError` are unchanged.
- `goal.completion.fenced` and `goal.failure.fenced` audit events are unchanged.
- `expect_plan_version` validation (positive integer, fail closed) is unchanged.
- All non-terminal and legacy transitions (pause, resume, cancel, blocked,
  operator-explicit fail/complete without a plan version) use the existing
  paths without modification.
- The goal-run lease (ADR-052), scheduler leases, mutation locks, approvals,
  recovery, task revision CAS, and all other authority mechanisms are unchanged.
- `claim_goal_plan` remains lease-free (ADR-053 §"Explicit limitations").

## Test strategy

The existing ADR-054 focused suite (`tests/test_plan_version_completion_fencing.py`,
5 tests) and ADR-055 suite (`tests/test_plan_version_failure_fencing.py`, 4 tests)
cover the fence invariant end-to-end through the real engine loops.
`test_transition_retry_rechecks_plan_lineage` was updated to patch
`cas_goal_terminal_fenced` instead of `cas_goal_fields` (the atomic method is
now the write boundary for plan-fenced transitions); the tested invariant is
identical: a forced CAS miss causes a retry that reads fresh plan lineage and
fences correctly on mismatch.

## Verification

- Read-only baseline at `7f3fd7f`: **1,456 passed, 2 skipped**.
- ADR-056 focused tests (ADR-054/055 suites): **11 passed** (1 test updated
  to patch the new atomic boundary; 2 subsequent Category-B invariant tests
  added to pin the `validated_plan_version` attribution guarantee for both
  the completion and failure paths — see
  `test_adr056_outcome_plan_version_attribution_survives_concurrent_readopt`
  in each fencing suite).
- Full regression: goal transitions/state machine, plan invariants/schema/
  hardening, goal manager, persistence/CAS/crash consistency, progress
  evaluator, replanning/readopt, goal lifecycle/approval/blocked, cross-goal
  bulk, ownership fencing, strategy outcomes ×5, strategy learning, ADR-048–055
  suites, recovery fencing, atomic recovery acknowledgement, audit, event
  contracts: **all green**.
- Complete suite (including Category-B additions): **1,458 passed, 2 skipped**.

## ADR-054 relationship

This ADR supersedes ADR-054 §"Concurrency limitations". The residual
cross-connection timing gap documented there is closed by the atomic storage
method. All other ADR-054 decisions (typed exception, retry loop, mismatch
behavior, strategy-outcome non-amplification, lease-free plan publication)
remain authoritative and unchanged. ADR-055 inherits this fix automatically
because it passes `expect_plan_version` through the same `transition()`
primitive.

## Explicit non-goals

- No schema/DDL change; no new tables.
- No change to `goal.version` semantics.
- No change to the plan-lineage funnel (`record_plan_version` / `readopt_plan`
  remain legitimate lease-free writers).
- No changes to non-terminal transitions, completion fencing, execution
  authority, publication ordering, canonical task identity, run ownership,
  coordination authority, scheduler leases, mutation locks, recovery
  architecture, approvals, or operator-explicit pause/cancel/resume semantics.
- The fallback path (for stores without `cas_goal_terminal_fenced`) retains the
  accepted residual gap from ADR-054; the fix is opt-in by storage backend.
