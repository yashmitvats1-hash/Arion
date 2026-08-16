# ADR-031: Per-Goal Reservation Ceilings / Maximum Concurrent Capacity

Status: Approved & implemented (tests-first, on top of ADR-030 `534f7d6`)

## Problem

ADR-029 guarantees a floor ("this goal may obtain at least R slots");
operators also need the complementary ceiling ("this goal may not own
more than C concurrent running items") so one goal cannot monopolize
capacity even within its weighted share. The ceiling is a third,
independent policy dimension, enforced transactionally inside the
existing authoritative claim path.

## Critical distinction

| Concept      | Meaning                                                         |
| ------------ | --------------------------------------------------------------- |
| weight       | relative scheduling opportunity among contending goals (DWRR)   |
| floor (R)    | minimum protected capacity while runnable (ADR-029)             |
| ceiling (C)  | maximum concurrent capacity (this ADR)                          |

Ceilings never create ownership, never reserve capacity, never bypass the
existing gates, and never establish execution authority.

## Data model (Phase A)

New table mirroring the reservation/weight registry conventions:

```sql
CREATE TABLE IF NOT EXISTS scheduler_goal_ceilings (
    goal_id     TEXT PRIMARY KEY,
    ceiling     INTEGER NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
```

- **Default: `ceiling = None` = unbounded by any goal-specific ceiling.**
  "Unbounded" is NEVER represented as an arbitrary huge integer in the
  public model; it is `None` at the API boundary and absent from the
  table.
- Validation: integer only; `1 <= ceiling <= _CEILING_MAX (10000)` when
  configured; `0` is rejected (use `remove`/`disable` to go unbounded);
  empty/fake goal ids fail closed (matching the reservation registry);
  typed `SchedulerRegistryError` for all failures.
- API: `set_goal_ceiling(goal_id, ceiling, *, enabled=True, by, now)`,
  `get_goal_ceiling(goal_id) -> int | None`,
  `get_goal_ceiling_config(goal_id) -> dict | None`,
  `list_goal_ceilings() -> list[dict]`,
  `remove_goal_ceiling(goal_id) -> bool`,
  `set_goal_ceiling_enabled(goal_id, enabled) -> dict | None`.

## Floor + ceiling compatibility (Phase C)

When both are configured (both enabled): **`0 <= R <= C`** is required.
Any configuration with `R > C` fails closed at write time, atomically
(no partial policy update):

- `set_goal_ceiling` rejects `C < R` (existing enabled floor);
- `set_goal_reservation` rejects `R > C` (existing enabled ceiling);
- `set_goal_reservation_enabled(True)` and `set_goal_ceiling_enabled(True)`
  re-validate the pair and fail closed;
- `remove`/`disable` of either side re-opens validity (the remaining side
  is unconstrained).

**Global feasibility:** ADR-029's reservation oversubscription rule is
preserved unchanged (`sum(enabled floors) <= cap`). `sum(ceilings)` does
NOT need to fit inside the global cap — ceilings are maximums, not
reservations (A ceiling 8 + B ceiling 8 with cap 8 is valid).

**Invariant (proven, not special-cased):** under valid configuration a
goal below its floor is never at its ceiling (`running < R <= C` ⇒
`running < C`). No runtime behavior is invented for that impossible
state; the config-time validation makes it unconstructible.

## Admission gate (Phase B)

Final order inside the existing `BEGIN IMMEDIATE` claim transaction
(documented; the reservation gate was already between fair share and
DWRR, so the ceiling slots in naturally):

1. stale reclaim (unchanged);
2. global capacity (unchanged) — `capacity.denied`;
3. scheduler fair share (unchanged) — `scheduler_share.denied`;
4. reservation floor/protection (unchanged) — `reservation.denied` /
   floor path;
5. **goal ceiling (NEW)** — `ceiling.denied`;
6. DWRR weighted admission (unchanged, with the at-ceiling skip below);
7. ownership establishment (unchanged).

Ceiling check: with an ENABLED ceiling `C` and `running_G >= C`, the
claim is denied (`ceiling.denied`, reason `goal_ceiling`, bounded detail:
goal, work, running, ceiling), the row stays QUEUED, and **no DWRR
credit is consumed and no refill is triggered** (the denial happens
before the DWRR gate). The check runs before the DWRR gate in BOTH the
specific-row and `claim_next` paths, so it also guards floor-path claims
(the floor can never bypass the ceiling — though valid configuration
makes a below-floor goal at its ceiling impossible).

**Core invariant:** `running_G <= C` at every committed state — never
bypassed by floor overrides, reservation floors, multiple processes,
races, stale reclaim (reclaim moves rows OUT of running), restart
(durable), or handoff (ownership moves, counts persist).

## DWRR interaction (Phase D) — no stranded credit

A ceiling-limited goal's credit must not permanently block refill rounds
for other goals. The DWRR gate's refill condition ("refill when the
attempting goal's deficit < 1 AND no other contending goal holds credit")
skips contending goals that are AT their enabled ceiling — they cannot
spend credit at that moment, so holding it must not stall peers. This
mirrors the existing skip for weight-disabled goals; credit is neither
created nor destroyed (it remains durable and spendable once the goal
drops below its ceiling). Exact durable credit assertions in tests.

## Cross-process (Phase E)

All gates read durable rows inside `BEGIN IMMEDIATE`, so two or more
processes cannot collectively exceed a goal's ceiling; a race for the
final ceiling slot yields exactly one owner (the other sees `running >= C`
and is denied); the global cap, floors, fair share, and DWRR remain
authoritative; stale reclaim frees a ceiling slot; restart preserves the
ceiling; disable/remove immediately permits future claims.

## Dynamic changes (Phase F)

- Increase `C 2 -> 5`: new claims may use the additional capacity;
  RUNNING work untouched.
- Decrease `C 5 -> 2` with running 5: nothing is cancelled; new claims
  denied until running falls below 2 (the ceiling binds future claims).
- Disable/remove: unbounded again for future claims.
- Restart: values persist.
- Floor/ceiling pair changes validate atomically (BEGIN IMMEDIATE +
  rollback on failure; a failed write leaves no event, no partial state).

## Telemetry (Phase G)

New kinds (ADR-028 taxonomy conventions): `goal_ceiling_changed`
(config set/remove/enable/disable) and `ceiling.denied` (goal, work,
running, ceiling, reason `goal_ceiling`). `_SECH_DETAIL_KEYS` gains
`ceiling` and `headroom`. `scheduler watch` renders both. Telemetry
remains observational: forged ceiling events have zero effect.

## Status & planning (Phases H/I)

`capacity_snapshot()` per-goal projection gains: `ceiling`
(`None` = unbounded), `ceiling_enabled`, and
`ceiling_headroom = max(C - running, 0)` for configured ceilings, else
`None` (never an invented integer). Aggregates:
`ceiling_limited_goal_count`, `goals_at_ceiling` (running >= C),
`recent_ceiling_denials` (from the last 200 telemetry events, like
recent reclaim/failure counts).

Explanation state gains `goal_ceiling_limited` — priority order:
`idle` → `weight_disabled` → below-floor states → capacity/share
exhaustion → **`goal_ceiling_limited`** (at/above floor, capacity and
share available, `running >= C`) → credit-based `eligible` /
`goal_weight_limited`. The explanation remains a snapshot projection
with the standing disclaimer ("admission is still authoritative at
claim time") and never runs the gates.

Planning:
- `reservation_feasibility()` also validates `floor <= ceiling` for
  goals with an existing enabled ceiling (proposed-mode only; the
  durable current config is valid by construction) — new reason
  `floor_exceeds_ceiling`; `sum(ceilings)` never participates in
  feasibility.
- `simulate_reservation_change()` validates the proposed floor against
  the goal's current ceiling.
- New `simulate_ceiling_change(goal_id, new_ceiling)` — dry-run:
  current/proposed ceiling, floor, floor/ceiling validity, headroom
  delta, feasibility note; never persists.
- New `simulate_goal_policy(goal_id, reservation=None, ceiling=None,
  weight=None)` — the general read-only policy simulator (current and
  proposed values for each supplied dimension, validity, feasibility,
  headroom, pressure impact).

## CLI (Phase J)

Mirror the reservation CLI:

```text
arion scheduler ceilings                      # list (human + --json)
arion scheduler ceiling set <goal> <n> [--disable] [--by WHO]
arion scheduler ceiling remove <goal>
arion scheduler ceiling enable <goal>
arion scheduler ceiling disable <goal>
arion scheduler ceiling plan <goal> <n> [--json]   # dry-run, never persists
```

Validation fails closed; `--by` audit metadata; persistence across
restart. `scheduler status`, `scheduler reservations --check`, and
`scheduler watch` expose ceiling information (per-goal rows, check
summary, event rendering).

## Security boundary (Phase K)

Forged ceiling config/changed/denied events, forged reservation/ceiling
metadata, planner/model output, fake goal ids, queue positions, running
counts, DWRR credit, heartbeat/reclaim events: zero effect — the ceiling
gate reads the durable `scheduler_goal_ceilings` table and
`scheduler_work` counts only. A ceiling cannot establish ownership;
another goal cannot consume or transfer a goal's ceiling; neither floor,
DWRR, nor fair share can bypass the ceiling; the global cap remains
authoritative. Policy influences admission; policy never establishes
execution authority.

## Demo (Phase L)

`scripts/demo_adr031_goal_ceilings.py` — deterministic, 25–35 checks,
scenarios A–S (unbounded default; set/get/remove; enforcement; exact
boundary; multiple goals; cross-process race; high-weight at ceiling
while low-weight continues; increase; decrease without cancellation;
disable/remove; restart; stale reclaim frees a slot; floor+ceiling
valid; floor > ceiling rejected; denial telemetry; status projection;
planning simulation; forged telemetry powerless).

## Acceptance criteria (tests-first)

- **A model:** default None; validation; bounded; remove/disable →
  unbounded; durability; typed errors.
- **B gate:** exact boundary; denial keeps QUEUED, no credit consumed;
  claim_next path; floor cannot bypass.
- **C composition:** R <= C enforced atomically in all four write
  directions; sum(ceilings) unconstrained; invariant unconstructible.
- **D DWRR:** ceiling-limited goal does not strand peer credit; exact
  durable credit assertions; mid-round ceiling; dynamic change; restart.
- **E cross-process:** 2+ processes; final-slot race exactly-one-owner;
  cap/floors/share/DWRR authoritative; stale reclaim frees; restart;
  disable immediate.
- **F dynamic:** increase/decrease/disable/restart; no cancellation.
- **G telemetry:** kinds emitted atomically; watch renders; forged
  powerless.
- **H status:** per-goal ceiling fields; headroom None vs int; aggregates;
  `goal_ceiling_limited` state with disclaimer; projection never mutates.
- **I planning:** feasibility floor<=ceiling; simulations; no mutation.
- **J CLI:** list/set/remove/enable/disable/plan; JSON; fail closed;
  plan provably mutation-free.
- **K adversarial:** as listed above.
- **L demo:** 25–35 checks.
- **M docs:** this ADR + architecture.md + CLI help + ADR-029/030 notes.

## Verification

Per-phase: tests red → implement → focused green → regression. Final:
full suite, ADR-016…031 demos + all repo demos, CLI smoke + JSON,
planning mutation checks, cross-process, restart/crash, adversarial,
repeated stability runs, and EXPLICIT re-runs of the ADR-029 and
ADR-030 focused suites (no global-cap / floor / DWRR / fair-share /
telemetry-authority regressions). Baseline at ADR-030: **915 passed,
2 skipped**. The claim path changes are additive (a new gate between
existing gates) and covered by the full regression suite.
