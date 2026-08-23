# ADR-051 — Exact Plan Task Claim

- **Status:** Approved and implemented (2026-08-23)
- **Scope:** Atomically create or adopt one authoritative task per goal plan version

## Context

ADR-050 commits immutable plan authority before managed task publication. Stored
plan reconstruction still used:

```text
read: no task implements (goal_id, plan_version)
  -> create random task id
  -> save reconstructed steps
```

Per-goal leases normally serialize this path, but a suspended owner can lose its
lease after the read and later continue. Task revision CAS is per task ID, so two
random IDs do not conflict.

The audit baseline was **1,427 passed, 2 skipped** at local and remote commit
`addcc35`.

## Reproduction

1. Immutable plan version 1 committed with no task.
2. Engine A acquired the goal lease and observed no implementing task.
3. A's heartbeat stopped; its lease expired while it was suspended.
4. Engine B reclaimed ownership and reconstructed the task.
5. Engine A resumed from its stale observation and reconstructed again.

Durable state:

```text
exact-version tasks: 2
task A: planned, one step
task B: planned, one step
```

Both exact tasks passed ADR-049. Sequential direct resumes produced:

```text
task results: completed, completed
capability calls: 2
external non-retry-safe effects: ["effect-1", "effect-2"]
```

## Root cause

- Stored reconstruction check and task insertion were separate transactions.
- Goal lease ownership was not a database uniqueness boundary for tasks.
- Task revisions and scheduler leases key on task ID.
- ADR-049 treated every exact-version task as executable.
- `plan_version` lives in task snapshot JSON, with no relational uniqueness
  constraint.

## Invariants

1. For `(goal_id, plan_version)`, at most one deterministic canonical task is
   executable.
2. Normal publication and stored reconstruction use the same authoritative
   create/update-or-adopt transaction.
3. A stale owner adopts the winner even after its earlier “no task” observation.
4. Repeated reconstruction emits no duplicate task/plan publication events.
5. Reconstruction uses the immutable stored plan definition, not planner output.
6. Stored reconstruction refuses to adopt a divergent exact task definition.
7. Runtime status/attempt/result/error changes do not make a legitimate task
   diverge from its immutable execution definition; post-plan parameter edits
   remain governed by existing live authorization/fingerprint rules.
8. No schema migration is required; default SQLite serializes the snapshot scan
   and insert/update under `BEGIN IMMEDIATE`.
9. Goal leases, task CAS, scheduler leases, approval, recovery, mutation locks,
   checkpoints, pause, and latest-plan rules remain unchanged.

## Decision

### SQLite exact-plan task claim

Add `Storage.claim_task_for_plan(task) -> (canonical_task, published)`.

The default SQLite implementation requires a PLANNED task with a positive exact
plan version and, inside `BEGIN IMMEDIATE`:

1. loads all tasks for the goal;
2. parses their authoritative snapshots;
3. selects exact-version matches;
4. returns the deterministic canonical match ordered by `(created_at, task_id)`;
5. otherwise inserts a new candidate or revision-CAS updates its existing
   CREATED row to PLANNED + steps + exact version;
6. commits and returns the published task.

Because every default engine publication uses this method, no schema/index is
needed. Raw malformed snapshots fail closed while being parsed.

### Normal managed publication

ADR-050 `_plan()` calls the exact-plan claim after immutable plan commit. A stale
planner that loses publication adopts the existing canonical task and emits no
second `plan.produced` event. `_plan_for_goal` returns the canonical task to the
caller.

### Stored-plan reconstruction

The stored fast path:

- parses and normalizes steps only from immutable `plan_summary`;
- builds an in-memory PLANNED candidate (no preliminary task insert);
- calls the exact-plan claim;
- emits `task.created` and `plan.produced` only when publication wins;
- returns the existing canonical task on repeated/concurrent recovery.

A repeated `_plan_for_goal` with no explicit replan reason returns an existing
exact task instead of invoking the planner.

### Immutable execution-definition validation

Task/plan comparison includes:

```text
index, intent, capability, action, scope, params,
verification policy/args, dependencies, guidance,
skipped intent, skipped reason, max attempts
```

It ignores runtime status other than planned SKIPPED intent, attempts, result,
and error. Stored reconstruction and stale publication adoption validate the
claimed task. ADR-049 selects the deterministic canonical exact task (or
canonical unversioned legacy fallback); existing post-plan task parameter edits
continue through live policy and approval-fingerprint checks.

## Compatibility

- No schema, checkpoint, plan, task, approval, recovery, scheduler, or lock
  format changes are required.
- Existing exact task rows remain readable; the oldest deterministic match is
  authoritative if historical duplicates exist.
- Standalone unversioned tasks remain unchanged.
- Existing task-created/plan-produced event kinds remain; duplicate recovery
  stops emitting duplicates.
- Method signatures for public engine execution remain unchanged.

## Explicit limitations

- Historical duplicate rows are retained for audit; noncanonical rows are
  fenced rather than deleted.
- Snapshot scanning is per goal and optimized for correctness; a future schema
  migration may add a relational plan-version column only if scale requires it.
- A raw writer bypassing the authoritative store API can still create malformed
  rows; execution-definition checks fail them closed.
- No distributed database uniqueness or event sourcing is introduced.

## Test strategy

Focused tests prove:

1. stale-owner concurrent reconstruction returns one canonical exact task;
2. repeated reconstruction is idempotent and emits no duplicate events;
3. task commit followed by event failure adopts the existing task on retry;
4. non-retry-safe execution occurs once;
5. divergent exact task content is not adopted by reconstruction;
6. normal plan publication and ADR-050 restart recovery remain unchanged;
7. latest-plan authority, goal/task leases, task CAS, scheduler, locks,
   approvals, recovery, checkpoints, events, and full suites remain green.

## Verification

- Read-only baseline: **1,427 passed, 2 skipped**.
- ADR-051 exact-plan task claim tests: **5 passed**.
- Focused planning/replanning, stored-plan restart, latest authority, goal/task
  lease, task CAS, scheduler/cross-process, mutation lock/wait, approval,
  fingerprint, recovery, checkpoint, and event regressions: **488 passed**.
- Complete suite after implementation: **1,432 passed, 2 skipped**.
