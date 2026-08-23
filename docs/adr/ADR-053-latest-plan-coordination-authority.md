# ADR-053 — Latest-Plan Coordination Authority

- **Status:** Approved and implemented (2026-08-23)
- **Scope:** Scope goal coordination/progress signals to authoritative latest-plan work

## Context

ADR-049 scopes **execution authority** to tasks implementing the latest immutable
plan; ADR-050 orders plan/task publication; ADR-051 gives each
`(goal_id, plan_version)` one deterministic canonical task; ADR-052 fences stale
goal-run owners. Coordination and progress decisions, however, still read the FULL
task history:

- `DeterministicProgressEvaluator` scanned ALL tasks for `AWAITING_APPROVAL`
  (Rule 3) and mutation-lock wait metadata (Rule 3b) BEFORE latest-plan filtering,
  and counted every exact-version task (not just the canonical one) as
  `latest_failed` (Rule 9);
- `GoalManager.recheck_blockers()` followed `approval_pending` blocker task
  references with no authority check;
- `GoalManager.pending_task()` returned ANY non-terminal exact-version task, not
  necessarily the canonical one;
- the ADR-049 supersession fence ran only at task/resume/approval-resolution
  boundaries — never when plan lineage advanced, so superseded coordination state
  (a queued approval, a parked lock waiter) stayed durable and current.

New immutable plan versions can legitimately be committed while work is parked —
`GoalManager.readopt_plan` (the documented `arion goal rollback` funnel, ADR-016)
requires no goal-run lease and is legal for ACTIVE/BLOCKED goals.

The read-only audit baseline was **1,438 passed, 2 skipped** at local and remote
commit `4e2ab87` (ADR-052).

## Reproduction

Three deterministic shapes, all reproduced against the ADR-052 tree through the
real `run_goal` path (second SQLite connection standing in for the operator
process):

**A — superseded approval blocks latest work**

```text
v1 task -> AWAITING_APPROVAL (pending request, goal BLOCKED approval_pending)
newer immutable plan v2 committed (public lineage funnel, no lease)
run_goal -> goal blocked, v2 tasks: 0, dead v1 approval still queued
```

**B — superseded mutation-lock waiter permanently stalls the goal**

```text
v1 task crashes mid-wait (durable lock_wait, goal BLOCKED lock_contention)
newer plan v2 committed; external lock released
run_goal xN -> goal active, blockers: [], v2 tasks: 0, capability calls: 0
```

The goal looked healthy but was inert forever; the only recovery was manually
resuming the OLD task to trigger the fence. No self-healing existed: approval
expiry is never swept inside the goal loop, and a superseded waiter is never
resumed (`pending_task` filters by version), so its wait deadline never fired.

**C — noncanonical exact-version duplicate influences progress**

```text
canonical v1 task COMPLETED (all steps handled, 1 capability call)
retained noncanonical v1 duplicate fenced FAILED by ADR-049
evaluate -> next_action=replan (reason=task_failed, latest_plan_failed=1)
run_goal -> NEW plan version manufactured; work re-executed
```

ADR-051 fences duplicate *execution* and retains duplicate rows for audit, but
the evaluator still let the fenced row veto completion.

## Root cause

- Coordination inputs (awaiting, waiters, failures) predated latest-plan scoping
  and were never filtered to authoritative work.
- `authoritative_tasks` included every exact-version row, unlike the canonical
  selection (`(created_at, task_id)`) used by ADR-049/051 execution authority —
  two different notions of "latest task".
- Supersession fencing existed but was resume-triggered; nothing converged
  durable coordination state when plan lineage advanced.

## Invariants

1. Historical, superseded, or noncanonical tasks retain their rows and remain
   observable, but hold no **coordination authority** over the current goal.
2. Awaiting-approval, lock-wait, resumable-selection, and failure/replan
   decisions for managed goal execution derive only from the canonical task
   implementing the latest immutable plan (plus the existing unversioned legacy
   fallback when no exact task exists).
3. A superseded task carrying coordination state (a queued approval or durable
   lock-wait metadata) is fenced by the live goal-run owner when an owned goal
   cycle observes it: pending approval denied with actor
   `system:superseded_plan`, task terminally FAILED, lock-wait metadata cleared,
   queued waiters cancelled — reusing the ADR-049 fence, never a second cleanup
   system.
4. No capability invocation and no historical-row deletion occurs during that
   reconciliation; it is idempotent and retried by the next owned cycle.
5. Coordination authority and execution authority use the SAME canonical-task
   definition (ADR-051 deterministic selection).
6. ADR-049 execution fences, ADR-050 publication ordering, ADR-051 exact-task
   claims, ADR-052 ownership fencing, task revision CAS, scheduler leases,
   mutation locks, approvals, recovery, pause, and checkpoints remain
   independent and unchanged.

## Decision

### Canonical coordination set in the evaluator

`DeterministicProgressEvaluator.evaluate()` computes its authoritative set
FIRST and scans ONLY that set for coordination state:

- exact-version tasks reduce to the single canonical task ordered by
  `(created_at, task_id)` — the same selection as ADR-049/051;
- the unversioned legacy fallback (only when no exact task exists) and the
  no-plan standalone case are unchanged;
- task-status totals (`completed`/`failed`/`pending`/`skipped`) remain
  observational over the full history.

Rules 3, 3b, 5, and 9 (awaiting approval, lock waiting, resumable work,
latest-plan failure) now act on authoritative work only.

### Canonical pending-task selection

`GoalManager.pending_task()` returns the canonical non-terminal exact-version
task (or the previous behavior when no plan lineage exists); noncanonical
duplicates are never selected as current work.

### Owned superseded-coordination reconciliation

`ArionEngine._reconcile_superseded_coordination(goal_id, claim)` runs inside the
owned goal loops (single and bulk) before evaluation. Under the exact goal-run
lease it fences the goal's non-terminal superseded/noncanonical tasks that carry
coordination state, using the existing `_fence_task_for_superseded_plan`
primitive (which itself re-decides authority, leaves terminal rows and the
canonical task untouched, denies the stale pending approval atomically with the
task transition, and clears the goal's `approval_pending` blocker through the
existing ADR-038 commit path). Paused and terminal goals are untouched;
tasks without coordination state keep relying on their existing
execution-boundary fences.

## Compatibility

- No schema, DDL, plan, task, approval, recovery, scheduler, lock, checkpoint,
  or event format changes.
- Historical rows are retained: fenced tasks stay FAILED with bounded
  provenance; denied approvals stay DENIED with `system:superseded_plan`.
- Current-plan behavior is unchanged: a canonical awaiting task still yields
  `await_approval` and is never fenced; a canonical waiter still yields
  `await_lock` with its deadline/retry budget preserved.
- `pending_task` returns the same task as before for every non-duplicate shape.
- Public method signatures and return types are unchanged.

## Explicit limitations

- A goal-level `lock_contention` blocker whose superseded carrier was fenced
  still clears through the existing recheck rules (resource free) — an
  externally held lock on a resource only superseded work wanted keeps the goal
  BLOCKED under existing ADR-021/022 semantics (operator release/expiry). No
  new blocker-authority matching is introduced.
- PLANNED/RUNNING superseded rows without coordination state are not
  proactively fenced; they are already non-executable (ADR-049) and ignored by
  evaluation.
- `GoalManager.record_plan_version`/`readopt_plan` remain lease-free public
  lineage writers by design; this ADR does not add ownership requirements to
  them.
- The stale plan-version goal-completion race (evaluate v1 → v2 committed →
  stale terminal transition) remains a separate, intentionally unaddressed
  decision; the terminal transition is still guarded by goal CAS and ADR-052
  ownership only.
- The evaluator remains informational: the reconciliation is an ENGINE action,
  never an evaluator write.

## Test strategy

Focused tests (`tests/test_latest_plan_coordination_authority.py`) prove:

1. a superseded pending approval is system-denied with its task and cannot
   block latest-plan publication (stored-plan reconstruction proceeds to its
   own fresh approval; only current work remains queued);
2. a superseded lock waiter is fenced (terminal, wait metadata cleared) and
   authoritative latest-plan work completes; the superseded target is never
   mutated;
3. a fenced noncanonical duplicate cannot flip a fully complete latest plan to
   `replan`, manufacture a new plan version, or re-execute work; an awaiting
   duplicate likewise coordinates nothing and is fenced;
4. positive controls: a current awaiting task still yields `await_approval`
   and is never fenced; a current waiter still yields `await_lock` with its
   deadline preserved;
5. historical rows remain retained after reconciliation.

## Verification

- Read-only baseline at `4e2ab87`: **1,438 passed, 2 skipped**.
- ADR-053 coordination-authority tests: **6 passed** (4 failing before the fix).
- Latest-plan execution/completion, exact-plan claim, goal-run lease/ownership,
  task lifecycle/resume, pause boundary, approval (all suites), fingerprint,
  goal manager/lifecycle/blocked, cognition, lock waiting/fairness/waiter
  queue/adversarial (incl. subprocess), append authorization/execution,
  recovery acknowledgement, progress evaluator, plan publication ordering,
  stored-plan, readopt, replanning, plan invariants, mutation-lock
  engine/store/renewal, scheduler leases/adversarial/reclaim/restart,
  concurrency model/adversarial, cross-goal, multi-process scheduler,
  reservations, ceilings, capacity cross-process regressions: **all green**.
- Complete suite after implementation: **1,444 passed, 2 skipped**.
