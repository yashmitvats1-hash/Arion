# ADR-030: Reservation-Aware Capacity Planning & Scheduler Status

Status: Approved & implemented (tests-first, on top of ADR-029 `504d252`)

## Problem

Operators need deterministic, read-only answers about scheduler capacity:
what is running, what is reserved, which goals are below their floors,
whether the reservation configuration is feasible, what a proposed
reservation change would do, and why capacity is unavailable. ADR-025…029
built the durable machinery; ADR-030 turns it into a **planning/status
projection** without adding any admission mechanism.

## Critical architectural rule

ADR-030 is a **read-only projection over durable scheduler state**. It
never becomes authority: it does not claim, heartbeat, register, modify
reservations/weights/capacity/DWRR credit, establish ownership, complete
work, reclaim leases, or bypass approvals. A forged planning result has
zero execution effect; if the planner says capacity is available but the
authoritative claim transaction says otherwise, the claim transaction
wins. The ADR-029 claim transaction is **unchanged**.

## Capacity arithmetic (exact definitions)

Let `cap = global_max_concurrency` (durable `scheduler_config`), and
`running = COUNT(scheduler_work WHERE status = 'running')`.

- `available_capacity = max(cap − running, 0)` when `cap` is set.
- **No global cap:** `cap = None` → `available_capacity = None` and
  `unreserved_capacity = None` (explicit unbounded sentinel — we never
  invent a finite capacity). `reserved_capacity` (a config view) is still
  computed. Admission gates are no-ops without a cap (ADR-026/029).
- `queued_count = COUNT(status = 'queued')`.

## Configured vs active reservations

Two distinct concepts, both exposed:

- **Configured reservation** (`reserved_capacity`): the sum of ENABLED
  reservations over ALL configured goals — a pure configuration view.
  An idle goal's reservation is *counted here* (it is configured) but
  consumes nothing.
- **Active reservation** (`active_reserved_capacity`): the sum of
  reservations of enabled reserved goals that HAVE queued (runnable)
  work — the floors currently in force. An idle goal contributes 0.
- **Reservation pressure** (`reservation_pressure`, ADR-029 semantics
  kept): Σ max(0, R_G − running_G) over enabled reserved goals with
  queued work — how many protected slots are still missing right now.
- `unreserved_capacity = max(cap − running − active_reserved_capacity, 0)`
  (None without a cap) — capacity neither running nor actively protected.

## Per-goal projection (Phase B)

For every goal that is configured (weight or reservation) OR has any
registry row, the snapshot exposes (all from durable state; no payloads,
prompts, memory, secrets, or arbitrary metadata):

- `goal_id`, `weight` (default 1), `weight_enabled`, `reservation`
  (default 0), `reservation_enabled`;
- `running`, `queued` (authority rows);
- `reservation_deficit = max(R − running, 0)`;
- `reservation_satisfied = (R == 0) or (running >= R)`;
- `reservation_pressure = max(R − running, 0) if queued > 0 else 0`;
- `dwr_credit` (durable deficit, clamped to the same bound the gate
  applies: min(deficit, max(weight, 2·cap)));
- `eligible` (bool) and `state` (bounded enum, see below).

## Admission explanation (Phase E) — a projection, not a gate

Per-goal `state` enum, computed read-only from the same durable tables
and constants the claim path uses (`_WEIGHT_MAX`, `ceil(cap/active)`
share, DWRR deficit, floor config). Priority order for a goal G:

1. no QUEUED rows → `idle`;
2. weight config disabled → `weight_disabled` (ADR-027 hard gate: never
   admitted);
3. below floor (enabled R ≥ 1, running < R):
   - `available_capacity > 0` AND some scheduler of its queued rows
     below its fair share → `reserved_floor` (the floor path would admit
     at claim time);
   - otherwise → `reservation_waiting` (floor unsatisfiable at this
     instant — capacity or share limited);
4. at/above floor:
   - `available_capacity == 0` → `global_capacity_exhausted`;
   - all schedulers of its queued rows at/above share →
     `scheduler_share_limited`;
   - durable credit ≥ 1 → `eligible` (weighted admission would grant);
   - durable credit < 1 but NO other contending enabled goal holds
     credit → `eligible` (a DWRR refill round would fire at claim time);
   - otherwise → `goal_weight_limited` (peers hold credit; the gate
     would deny until a round boundary).

Every explanation carries the disclaimer: *"Eligible based on current
snapshot; admission is still authoritative at claim time."* Because the
projection replicates the gate's decision logic (shared constants, same
SQL), drift risk is contained and explicitly documented; exact admission
is never promised — another process may change state between snapshot
and claim. The projection NEVER runs the gates (they mutate DWRR credit);
it only reads the same state.

## Feasibility (Phase C)

`reservation_feasibility(proposed=None)` — deterministic, read-only.
- `proposed=None`: evaluates the CURRENT enabled configuration.
- `proposed={goal_id: reservation}`: a FULL proposed configuration
  (values treated as enabled).
- Returns: `feasible`, `global_max`, `configured_total` (current enabled
  sum), `proposed_total` (Σ proposed values; = configured_total when
  `proposed is None`), `overflow = max(proposed_total − cap, 0)` (0 when
  no cap), `affected_goals` (sorted proposed goal ids), `reason` ∈
  {`ok`, `no_global_cap`, `oversubscribed`}.
- Rule: feasible iff `cap is None` OR `proposed_total ≤ cap`. With no
  cap the result is `feasible=True, reason="no_global_cap"` — any
  bounded config is satisfiable in unbounded capacity; the admission
  gates are no-ops. No mutation, no normalization.

## Proposed-policy simulation (Phase D)

`simulate_reservation_change(goal_id, new_reservation)` — read-only:

- validates `new_reservation` exactly like `set_goal_reservation`
  (integer in [0, 10000], fail closed otherwise);
- `current` = durable config (0/None if unconfigured);
- `current_total` = enabled total of ALL configured goals;
- `proposed_total` = current_total − (current enabled contribution) +
  new value (replacement semantics; 0 ⇒ removal);
- `capacity` (= cap or None), `remaining` = max(cap − proposed_total, 0)
  (None without cap);
- `feasible`, `overflow`;
- `pressure_delta` ∈ {`increase`, `decrease`, `unchanged`}: the ADR-029
  pressure formula evaluated on the current snapshot vs the same
  snapshot with the goal's reservation replaced by the proposed value;
- `affected_goals` = [goal_id] plus any goal whose below/at/above
  classification would change (always deterministic from the snapshot).

`simulate_reservation_config(proposed: dict)` — the full-config variant
(used by the feasibility checker's proposed mode). Neither API touches
reservations, weights, DWRR credit, events, or work rows.

## Scheduler status upgrade (Phase F)

`capacity_snapshot()` on the store returns the typed read-only snapshot:

- scalar block: `global_max_concurrency`, `running_count`, `queued_count`,
  `available_capacity`, `reserved_capacity`, `active_reserved_capacity`,
  `reservation_pressure`, `unreserved_capacity`, `active_scheduler_count`,
  `active_goal_count`, `reserved_goal_count`;
- classification lists: `goals_below_reservation`, `goals_at_reservation`,
  `goals_above_reservation` (enabled reserved goals, classified by
  running vs R — idle-but-below is still "below"; pressure fields show
  whether it matters);
- `goals` (per-goal projection array), `goal_weights`, `goal_reservations`
  (config views), `now`.

`scheduler_status()` keeps its ADR-028/029 fields (additive) and gains
the snapshot block. CLI `arion scheduler status` gets the planning
layout (human) and the full JSON schema (additive over the old flat
counts — existing keys unchanged).

## CLI (Phases F/G/H)

- `arion scheduler status [--json]` — upgraded human layout:

```
Global capacity:      8
Running:              5
Available:            3
Configured reserved:  5
Active reservation:   4
Unreserved capacity:  3

Goals:
  goal-a  weight=1 reservation=2 running=3 queued=4 satisfied=yes state=eligible
  ...
```

- `arion scheduler reservations --check [--json]` — read-only check of
  the CURRENT configuration: capacity, configured total, feasible/
  infeasible, overflow, active pressure, goals below floor, idle
  reserved goals, unreserved capacity. Exit: **0** feasible, **1**
  infeasible/config issue (no mutation in either case).
- `arion scheduler reservation plan <goal_id> <capacity> [--json]` —
  dry-run: current value, proposed value, current/proposed totals,
  global cap, remaining, feasible/infeasible, affected goals. NEVER
  persists. Running it repeatedly leaves reservations, weights, DWRR
  credit, scheduler events, and work ownership byte-identical.
  Invalid input → exit 1 with a deterministic error; valid input → exit 0.

## JSON schema (Phase I)

- Semantic field names only; no SQLite internals (no `rowid`, no table
  names, no column prefixes).
- Absent values: `global_max_concurrency`/`available_capacity`/
  `unreserved_capacity` = `null` when no cap (explicit unbounded);
  zero counts are `0`; empty lists are `[]`; booleans are `true/false`.
- Deterministic ordering: `goals` sorted by goal id; lists sorted.
- Bounded: per-goal entries carry ids/counts/enums only.

## Cross-process consistency (Phase J)

Planning reads are plain `SELECT`s over the shared registry; workers may
claim/heartbeat/complete while a snapshot is computed. The result may be
stale the instant it is returned — acceptable. The invariant proven by
tests: **observation cannot mutate authority** (snapshot/feasibility/
simulation calls leave reservations, weights, DWRR credit, events, and
ownership unchanged even while subprocess workers are active).

## Adversarial boundary (Phase K)

- Planning never trusts telemetry: forged reservation events, forged
  capacity/deficit/queue-position telemetry, fake goal ids, planner/
  model/task metadata cannot alter any planning input (planning reads
  authority tables only).
- Planning cannot create durable reservations, alter DWRR, establish
  ownership, or change capacity; an infeasible configuration stays
  infeasible no matter what events are forged; deleting telemetry does
  not change planning authority; stale telemetry cannot resurrect work.
- Malformed/oversized inputs (non-integers, negatives, > 10000,
  non-string goal ids) fail closed with typed errors.

## Demo (Phase L)

`scripts/demo_adr030_capacity_planning.py` — deterministic (fixed
timestamps), 25–35 checks, scenarios:

A empty scheduler; B global capacity snapshot; C running capacity;
D configured reservations; E active reservation pressure; F idle
reserved goal; G below-floor goal; H satisfied goal; I feasible
configuration; J infeasible configuration; K proposed reservation
increase; L proposed reservation decrease; M status JSON; N reservation
check JSON; O dry-run proves no mutation; P forged telemetry powerless;
Q cross-process observation; R restart persistence.

## Acceptance criteria (tests-first)

- **A capacity model:** arithmetic exact; no-cap sentinel; configured vs
  active vs pressure; classification lists.
- **B per-goal projection:** all fields; bounded/no payloads; default
  weights/reservations; DWRR credit clamped like the gate.
- **C feasibility:** current vs proposed; overflow; affected goals;
  reason enum; no-cap semantics; no mutation.
- **D simulation:** replacement semantics; totals; pressure delta;
  full-config variant; fail-closed validation; no mutation (events/
  credit/ownership byte-identical).
- **E explanations:** each state reachable and deterministic; disclaimer
  present; projection never mutates DWRR credit (credit before == after).
- **F status:** additive fields; human layout; JSON schema stable.
- **G --check:** exit 0/1 deterministic; JSON schema; no mutation.
- **H plan CLI:** dry-run; repeated runs leave state identical; errors
  fail closed.
- **J cross-process:** planning during active workers never mutates.
- **K adversarial:** forged telemetry powerless; oversized inputs fail
  closed; infeasible stays infeasible.
- **L demo:** 25–35 deterministic checks.
- **M docs:** this ADR + architecture.md + CLI help.

## Verification

Per-phase: tests red → implement → focused green → regression. Final:
full suite, ADR-016…030 demos + all repo demos, CLI smoke + JSON,
reservation check, dry-run planning, cross-process, adversarial,
restart, repeated stability runs. Baseline at ADR-029: **866 passed,
2 skipped**. Claim behavior unchanged from ADR-029 (no claim-path
edits; proven by the unchanged 866-test suite).
