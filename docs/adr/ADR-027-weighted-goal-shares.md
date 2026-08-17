# ADR-027 — Durable Per-Goal Capacity Shares + Weighted Fair Scheduling

- **Status:** Approved & implemented (2026-08-16)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Problem

ADR-026 made the shared cross-process scheduler fair at the SCHEDULER
level: with `global_max_concurrency` configured, no single scheduler may
hold more than `ceil(cap / active_schedulers)` RUNNING rows. Fairness is
not goal-aware: a goal (tenant) hosted by one scheduler competes equally
with every other goal, so an operator cannot give a critical goal a larger
share of the shared capacity.

## Goals / non-goals

**Goals**

- Durable per-goal scheduling weights (goal_id → positive integer weight,
  enabled flag, bounded metadata, deterministic default weight = 1).
- Weighted fair admission inside the EXISTING atomic claim transaction
  (`BEGIN IMMEDIATE`): a goal with weight 2 gets ~2× the scheduling
  opportunity of a goal with weight 1 under sustained contention.
- Deterministic, reconstructable-from-durable-state fairness (no wall-clock
  authority, no in-memory counter required for correctness).
- Starvation guarantee: every contending enabled goal with queued work
  eventually makes progress; an idle goal never reserves capacity; a
  high-weight goal never monopolizes the entire global capacity.
- Cross-process: all processes sharing the registry observe the same
  durable policy; racing claims cannot bypass weighted admission or
  produce inconsistent capacity accounting.
- Dynamic policy: weight changes apply to future admission only; RUNNING
  work stays owned; no retroactive cancellation; no capacity duplication.
- Backward compatibility: no weights configured ⇒ behavior identical to
  the ADR-026 default scheduler (weights gate admission only when
  `global_max_concurrency` is configured).

**Non-goals**

- No new tenant abstraction (the repository has none; goal-level weights
  are the unit). No change to authorization, approvals, recovery,
  mutation locks, waiter fairness, ActionSpec fingerprints, leases,
  heartbeats, stale-owner rejection, or the scheduler-level fair share
  (ADR-019..026 immutable).
- No wall-clock-based fairness authority.
- Weights are SCHEDULER POLICY, never execution authority: planner/model/
  memory/guidance/task metadata can never establish or elevate a weight.

## Design

### Durable tables

```sql
CREATE TABLE IF NOT EXISTS scheduler_goal_weights (
    goal_id    TEXT PRIMARY KEY,
    weight     INTEGER NOT NULL,          -- positive, bounded
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS scheduler_goal_state (
    goal_id    TEXT PRIMARY KEY,
    deficit    INTEGER NOT NULL DEFAULT 0,  -- DWRR credit, bounded
    updated_at TEXT NOT NULL
);
```

Follows the existing convention (new tables in `SCHEMA`, `CREATE TABLE IF
NOT EXISTS`, no destructive migration). `scheduler_goal_weights` is
operator configuration; `scheduler_goal_state` is durable scheduler
coordination state (reconstructable after restart, never an authority).

### Weight registry (store protocol)

- `set_goal_weight(goal_id, weight, enabled=True, by="operator", now)`
  — upsert; rejects zero/negative/non-integer weights with the typed
  `SchedulerRegistryError` (fail closed); weight capped at a bounded max
  (e.g. 10_000) so metadata stays bounded.
- `get_goal_weight(goal_id) -> int` — configured weight or default 1.
- `get_goal_weight_config(goal_id) -> dict | None` — incl. enabled.
- `list_goal_weights() -> list[dict]` — bounded, ordered.
- `remove_goal_weight(goal_id) -> bool` — back to default behavior.
- `set_goal_weight_enabled(goal_id, enabled) -> dict | None`.
- A DISABLED goal is never admitted (fail closed) and is excluded from the
  contending set; its queued rows stay QUEUED (operator intent).

### Weighted admission algorithm (deterministic DWRR, inside the claim tx)

The existing claim transaction (`_sys_claim_in_tx`) gains one gate between
the scheduler fair-share check and the row UPDATE:

```
1. lazy stale-lease reclaim (unchanged)
2. global cap: running_total < global_max           (unchanged)
3. scheduler fair share: mine < ceil(cap/active)    (unchanged)
4. GOAL WEIGHT GATE (new):
   contending  = goals with rows in (QUEUED, RUNNING)
   weight(g)   = configured weight or 1; disabled goals excluded
   deficit(g)  = durable counter (lazy-init 0)
   if deficit(goal) < 1  AND  goal has queued work:
       refill EVERY contending goal: deficit += weight(g)
       (cap deficit at max(weight, 2 * global_max) — bounded)
   if deficit(goal) >= 1: deficit -= 1; GRANT
   else: DENY (row stays QUEUED)
5. row UPDATE QUEUED -> RUNNING (unchanged)
```

Properties (deterministic, no wall clock):

- **2:1 ratio:** per refill round a weight-2 goal receives 2 credit, a
  weight-1 goal 1; under sustained contention with both attempting,
  claims track weights exactly per cycle.
- **Starvation:** any contending enabled goal that attempts a claim with
  free capacity and deficit < 1 triggers a refill giving it ≥ 1 credit, so
  it is admitted on that attempt (modulo the scheduler fair share).
- **No monopolization:** each goal can spend at most its (bounded) credit;
  other contending goals refill on their own attempts, so a high-weight
  goal cannot exhaust capacity for peers forever.
- **Idle goals reserve nothing:** only goals with QUEUED/RUNNING rows are
  in the contending set and receive refills.
- **No capacity duplication:** refill only happens inside the claim
  transaction when the global cap has room; a full cap never refills, so
  counters cannot inflate capacity. Weight changes never refund spent
  deficit and never touch RUNNING rows.
- **Restart-safe:** the entire decision derives from durable rows
  (`scheduler_goal_weights`, `scheduler_goal_state`, `scheduler_work`);
  no in-memory counter is required for correctness.

### Claim paths

- `claim(work_id, ...)` — the row's goal_id (read inside the tx) drives
  the gate; engine `_admit_step` already passes scheduler_id.
- `claim_next(scheduler_id, ...)` — the candidate row's goal_id drives the
  gate; denied rows stay QUEUED (caller retries).
- `release_and_claim_next(...)` — same gate for the handed-off next row.
- No weights / no global cap configured ⇒ the gate is a no-op
  (ADR-026 behavior exactly).

### Configuration authority

Weights are stored ONLY via the store protocol (and the CLI, which uses
the store). The engine never reads weights from planner output, model
output, memory, guidance, task metadata, or worker input; a task carrying
a forged "weight=100" field is scheduled at its durable configured weight
(default 1). Forged deficit counters cannot bypass the global cap, the
scheduler fair share, the owner-checked terminal transitions, or the
authorization pipeline — scheduling policy can influence admission, never
execution authority.

### CLI

Extend `arion scheduler` with: `weights` (list), `weight set <goal_id>
<weight> [--disable] [--by ACTOR]`, `weight remove <goal_id>`,
`weight enable|disable <goal_id>`. Bounded, secret-free output;
validation failures exit non-zero.

## Acceptance criteria (tests-first)

- **A registry:** set/get/remove/list; default weight 1; positive bounded
  weights; invalid weights (0, negative, non-integer, oversized) typed-
  rejected; enable/disable; durable persistence across reopen; concurrent
  config access; goal isolation.
- **B admission:** equal weights; 2:1; 3:1; 5:1; three goals; idle goal
  reserves nothing; hot goal cannot monopolize; low-weight eventual
  progress; global cap never exceeded; no weights ⇒ ADR-026 behavior.
- **C cross-process:** two processes, two goals respect weights (durable
  observation invariants, generous deterministic bounds); racing claims
  cannot bypass weights; global cap exact; stale scheduler recovery does
  not distort capacity indefinitely.
- **D restart/dynamic:** weights survive restart; queued work keeps goal
  association; weight change while queued affects only future admission;
  disable/re-enable; new goal defaults to 1; persisted deficit survives
  restart; crash-while-running recovery unchanged with weights present.
- **E adversarial:** forged weights via plan/model/memory/guidance/task
  metadata cannot elevate admission; forged goal ids cannot create
  config; forged deficit cannot bypass cap/scheduler share/ownership;
  scheduling policy never establishes execution authority.
- **F CLI:** list/set/remove/enable/disable; validation; persistence;
  malformed input; multiple goals.
- **G demo:** `scripts/demo_adr027_weighted_scheduler.py`, 25-35
  deterministic checks, scenarios A-J, no wall-clock luck.
- **H docs:** ADR-027 + `architecture.md` + CLI help.

## Verification

Full suite green; ADR-024/025/026 demos green; new ADR-027 demo green;
cross-process + adversarial + CLI tests green; repeated stability runs of
concurrency-heavy tests; linear history; clean tree; commit with the
repository convention; push branch; update PR.

## Verification (full gauntlet)

- `tests/test_goal_weight_registry.py` (20): set/get/remove/list; default
  weight 1; positive bounded weights; invalid weights typed-rejected;
  enable/disable; durability across reopen; concurrent configuration;
  goal isolation.
- `tests/test_weighted_admission.py` (15): exact per-round ratios 1:1,
  2:1, 3:1, 5:1, 2:1:1 (gate-enforced with uniform attempts); idle goals
  reserve nothing; hot goal cannot monopolize; low-weight progress every
  round; global cap never exceeded; no global cap / no weights -> ADR-026
  behavior; disabled goals never admitted; dynamic weight change; new
  goals default to 1; persisted deficit across reopen.
- `tests/test_weighted_cross_process.py` (6): two/three processes respect
  weights (exact durable counts, subprocess-based); racing claims cannot
  bypass weights; rapid claimant cannot starve a low-weight goal; global
  cap exact under continuous observation; stale scheduler recovery does
  not distort capacity.
- `tests/test_weighted_restart_dynamic.py` (6): weights survive restart;
  weight change while queued -> future admission only, RUNNING work stays
  owned; disable/re-enable; new-goal default; deficit persists; crash-
  while-running recovery unchanged with weights.
- `tests/test_weighted_adversarial.py` (9): poisoned plan/task/model
  claims cannot set weights; fake goal ids create no config; forged
  deficits cannot exceed the global cap, bypass the scheduler fair share,
  or grant ownership (heartbeat/terminal/handoff remain owner-checked);
  disabled goals cannot be re-enabled via metadata; forged queue
  positions/completions still powerless; config bounded + audited.
- `tests/test_cli_scheduler_weights.py` (6): `arion scheduler weights`,
  `weight set|remove|enable|disable`; validation fails closed; durable
  persistence; bounded JSON output.
- `scripts/demo_adr027_weighted_scheduler.py` (31 deterministic checks,
  offline, scenarios A-J): default behavior, equal weights, 2:1, 2:1:1,
  low-weight progress, global cap, cross-process, dynamic change,
  restart, adversarial attempts.
- Full suite: 755 passed / 2 skipped (baseline 693); all demos
  ADR-016..027 pass; CLI smoke passes; security scans clean; linear
  history.

## Known limitations / future work

- Goal weights apply only when `global_max_concurrency` is configured
  (the durable cross-process policy scope).
- The scheduler-level fair share (`ceil(cap/active_schedulers)`) still
  bounds a single process's usage; goal weights refine admission within
  that envelope (documented composition).
- Deficit is bounded (max(weight, 2 × cap)) and clamped at spend time, so
  a forged/inflated deficit can delay peers by at most the attacker's own
  queued work.
- Per-tenant grouping / weighted capacity reservation per goal is future
  work.
