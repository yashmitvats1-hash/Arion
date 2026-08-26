# ADR-055 — Plan-Version-Fenced Goal Failure

- **Status:** Approved and implemented (2026-08-24)
- **Scope:** Refuse evaluation-driven terminal failure based on a superseded plan evaluation

## Context

ADR-054 fenced terminal **completion** to the evaluated immutable plan version
by adding ``expect_plan_version`` to ``transition()`` with a lineage
re-read inside every CAS attempt. The two evaluation-driven **failure** sites
(``max_replans_exceeded`` in the single-goal and bulk/shared loops) still
discarded ``evidence["latest_plan_version"]`` — the exact pre-ADR-054
completion shape:

```text
evaluate v1 -> replan decision (replan_count >= max_replans)
concurrent lineage writer commits immutable plan v2 (public funnel,
lease-free by design) -> v2 latest, no implementing task
stale runner calls fail_goal() -> goal FAILED
                                      outcome: (2, 'failed')   <- never executed
```

Reproduced deterministically in both engine loops and in a genuine
unsynchronized two-thread race (3 of 8 runs pre-fix).

The read-only baseline was **1,449 passed, 2 skipped** at local and remote
commit ``3a1ee3f`` (ADR-054).

## Problem

A stale failure decision terminally fails a goal whose authoritative latest
plan was never evaluated and never executed, suppresses current work until an
operator intervenes, misleads the audit trail, and — while it persists —
feeds a false ``failed`` signal into ADR-015 strategy selection
(``strategy_for`` reads ``strategy_outcomes``).

## Why goal CAS is insufficient

``goal.version`` CAS protects **row concurrency**: a stale writer cannot
clobber a newer goal row. Plan commits intentionally never bump
``goal.version`` (informational patches "do NOT increment goal.version"), so
row CAS establishes nothing about **immutable plan-lineage authority**. A
transition can win its row CAS while the authoritative plan has advanced
underneath it.

## Invariant

A goal may transition to evaluation-driven terminal failure only if the
immutable plan version that produced the failure decision is still the
authoritative latest plan when the failure transition commits.

## Mechanism — reuse of the ADR-054 primitive

No second fencing mechanism exists or was added:

- ``fail_goal(goal_id, reason, expect_plan_version=None)`` forwards the
  expectation into ``transition()``, where the SINGLE ADR-054 lineage fence
  re-reads ``latest_plan()`` inside every CAS attempt and raises the same
  typed ``GoalPlanLineageError`` on mismatch.
- Both production failure sites pass
  ``result.evidence["latest_plan_version"]`` — they are the only
  evaluation-driven failure callers.
- Direct/legacy ``fail_goal`` calls without an expectation retain existing
  behavior (raw GoalManager authority convention).

## Mismatch behavior

Fail closed and re-evaluate. The engine catches ONLY the typed
``GoalPlanLineageError`` at the failure boundary, emits one bounded
``goal.failure.fenced`` audit event, and continues the loop against current
durable state: no failure, no completion, no blocker, no task mutation, no
replan-count mutation, no strategy-outcome write, no ``goal.version`` change.
The fresh ``replan_count`` is re-derived per cycle, so a genuinely exhausted
lineage still fails — never on stale authority.

## Why this matters despite recoverability

- ``FAILED -> ACTIVE`` is legal and the CLI exposes ``arion goals resume``,
  so the wrong state is operator-recoverable.
- The false ``(v2, 'failed')`` outcome row updates in place (to
  ``superseded`` on the next lineage advance, or ``succeeded`` if v2 later
  completes while latest).
- But transient false failure still **suppresses authoritative work** until
  an operator intervenes, and **contaminates strategy learning** and audit
  history while it persists. Transient incorrect authority and strategy
  input are not acceptable even when later correctable.

## Test strategy

Focused tests (``tests/test_plan_version_failure_fencing.py``) prove:

1. a stale v1 failure decision cannot fail the goal after v2 commits
   (deterministic boundary injection at the real ownership boundary);
   historical task state is untouched; current state is re-evaluated against
   v2; v2 proceeds normally and later completes with a TRUE outcome;
2. the bulk/shared failure path is fenced identically;
3. the legitimate ``max_replans_exceeded`` failure against a
   still-authoritative plan still fails (positive control, real path);
4. a mismatch fails closed with zero side effects (no terminal state, no
   outcome row, no blocker, no task mutation, ``goal.version`` untouched);
5. ADR-054 completion fencing remains intact (its focused suite passes
   unchanged).

Post-fix genuine two-thread verification: **0/8 stale failures and 0/8 false
outcomes** (pre-fix 3/8), with the legitimate same-lineage failure still
occurring when the operator thread commits nothing in time.

## Verification

- Read-only baseline at ``3a1ee3f``: **1,449 passed, 2 skipped**.
- ADR-055 focused tests: **4 passed** (primary invariant demonstrated failing
  before the fix at ``goal failed on stale plan evidence``); ADR-054 suite:
  **5 passed** unchanged.
- Regression matrix (goal transitions/state machine, plan
  invariants/schema/hardening, goal manager, persistence/CAS/crash
  consistency, progress evaluator, replanning, goal lifecycle/approval/
  blocked, cross-goal bulk, ownership fencing, strategy outcomes ×5,
  strategy learning, ADR-049/050/051/052/053/054 suites, recovery fencing,
  atomic recovery acknowledgement, audit, event contracts): **all green**.
- Complete suite after implementation: **1,453 passed, 2 skipped**.

## Explicit non-goals

- No schema/DDL change; no ``goal.version`` semantics change.
- No second lineage-fencing mechanism; the ADR-054 transition fence remains
  the single authority boundary.
- No lease requirements for the public plan-lineage funnel.
- No changes to completion fencing (ADR-054), execution authority (ADR-049),
  publication ordering (ADR-050), canonical task identity (ADR-051), run
  ownership (ADR-052), coordination authority (ADR-053), scheduler leases,
  mutation locks, recovery architecture, approvals, or operator-explicit
  pause/cancel semantics.
- ~~The residual cross-connection timing boundary documented in ADR-054 applies
  unchanged to the failure fence.~~ **Superseded by ADR-056:** the failure
  fence uses the same `GoalManager.transition()` path as the completion
  fence, so ADR-056's `cas_goal_terminal_fenced()` closes this gap for
  failure transitions as well. The original two-step path is retained only
  as a fallback for storage backends that do not implement the atomic method.
