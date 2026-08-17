# ADR-029: Per-Goal Weighted Capacity Reservation

Status: Approved & implemented (tests-first, on top of ADR-028 `8b87982`)

## Problem

ADR-027's DWRR weights give goals *relative* scheduling opportunity, but a
high-weight competitor can still consume all global capacity under sustained
contention, starving a low-weight goal indefinitely (DWRR admits a
weight-1 goal at most once per round, and a round can be arbitrarily long
when the weight-8 goal holds long-running leases). Operators need a
*floor*: "goal B must occupy at least R concurrent slots whenever it has
runnable work and capacity exists", regardless of weights.

## Goals

- Per-goal minimum concurrent capacity reservation (a floor, not a ceiling).
- Reservation coexists with global cap, scheduler fair share, DWRR weights,
  leases, cross-process ownership, approvals/recovery gates, mutation locks.
- Deterministic, durable, restart-safe, fail-closed, observable (ADR-028).
- Reservation is scheduler POLICY only — never execution authority.

## Non-goals

- Not a reinterpretation of ADR-027 weights (weight = relative opportunity;
  reservation = minimum guarantee). Weights stay exactly as they are.
- No reservation while the goal has no runnable work (idle goals reserve
  nothing).
- No eviction/cancellation of RUNNING work to satisfy a floor.
- No new execution/authorization path; no per-goal capacity ceilings.

## Critical distinction

| Concept      | Meaning                                                        |
| ------------ | -------------------------------------------------------------- |
| Weight       | relative scheduling opportunity among contending goals (DWRR)  |
| Reservation  | minimum concurrent RUNNING slots guaranteed while runnable     |

Unit: **reservation = minimum number of concurrent runnable execution
slots for that goal** (RUNNING rows of the goal in the durable registry).

## Data model

New table (mirrors `scheduler_goal_weights` conventions exactly):

```sql
CREATE TABLE IF NOT EXISTS scheduler_goal_reservations (
    goal_id     TEXT PRIMARY KEY,
    reservation INTEGER NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
```

- Unconfigured goal ⇒ reservation 0 (deterministic default).
- `reservation` is an integer in `[0, _RESERVATION_MAX]` (`_RESERVATION_MAX
  = 10000`, same bound style as `_WEIGHT_MAX`); bool/float/negative/
  non-numeric values fail closed (`SchedulerRegistryError`).
- `enabled` is an independent on/off switch (like goal weights).

Store API (all `@_threadsafe`, transactional, event-emitting):

- `set_goal_reservation(goal_id, reservation, *, enabled=True, by="operator", now=None)`
- `get_goal_reservation(goal_id) -> int` (default 0)
- `get_goal_reservation_config(goal_id) -> dict | None`
- `list_goal_reservations() -> list[dict]`
- `remove_goal_reservation(goal_id) -> bool`
- `set_goal_reservation_enabled(goal_id, enabled) -> dict | None`

## Oversubscription policy: REJECT at configuration time

- With a global cap configured: `set_goal_reservation` fails closed if the
  resulting **sum of enabled reservations** would exceed the global cap
  ("do not silently allow impossible guarantees"). Same for
  `set_goal_reservation_enabled(…, True)`.
- `set_scheduler_global_max(n)` fails closed if `n` is below the current
  sum of enabled reservations (a cap change that would make the existing
  policy impossible is rejected atomically).
- With **no global cap configured**: reservations are accepted and stored
  (any floor is trivially satisfiable when capacity is unbounded); the
  admission gate is a no-op, exactly like the DWRR gate when `global_max`
  is unset. `reserved_capacity` still reports the configured sum. Once a
  cap is set, the bound check applies.
- No silent normalization anywhere; configuration that cannot be honored
  is rejected with a typed error.

## Admission algorithm (inside the existing `BEGIN IMMEDIATE` claim transaction)

Order (deliberate; documented in architecture.md):

1. `_sys_reclaim_stale_in_tx(now)` — stale work first (unchanged).
2. Global capacity gate (unchanged) — `capacity.denied`.
3. Scheduler fair-share gate (unchanged) — `scheduler_share.denied`.
4. **Reservation gate (NEW)**:
   - `running_H` = RUNNING rows of the claiming goal H.
   - `cfg_H` = reservation config of H; H is *under floor* iff
     `cfg_H` enabled, `reservation_H >= 1`, `running_H < reservation_H`.
   - **Floor path** (H under floor): grant the claim — the floor is a
     guarantee, not an opportunity — but keep DWRR accounting honest:
     the claim flows through the DWRR gate normally (refill rounds fire,
     credit is debited when available), and the floor OVERRIDES only a
     credit denial (deficit < 1 while peers hold credit): the claim is
     admitted without debiting and without triggering a refill. This
     prevents a floor-only goal from stranding refill credit that would
     stall peer rounds. The claim still respects gates 1–3 (a scheduler
     at its fair share is denied even for an under-floor goal — the core
     invariant only guarantees the floor "when the scheduler/process is
     eligible under existing scheduler fair-share rules"). A
     weight-`disabled` goal is NEVER admitted by the floor (ADR-027 hard
     gate stays authoritative). Emit `reservation.satisfied` when
     `running_H` reaches exactly `reservation_H` after the claim.
   - **Protection path** (H not under floor — including H with no
     reservation and H at/above its own floor): compute
     `outstanding = Σ (reservation_G − running_G)` over every enabled
     reserved goal G ≠ H with QUEUED work and `running_G < reservation_G`
     (idle goals contribute 0 — they have no runnable work, so they
     reserve nothing). With `free = global_max − total_running` (≥ 1
     after gate 2), deny H's claim iff `free − 1 < outstanding`, i.e. the
     slot H wants is needed for an under-floor reserved goal to still
     reach its floor. Denial emits `reservation.denied` (reason
     `reservation`); the row stays QUEUED.
5. DWRR goal-weight gate (unchanged) — `goal_weight.denied` /
   `goal_weight.refill`.
6. Ownership establishment (unchanged) — `work.claimed`.

Why gate 4 sits between fair share and DWRR: capacity and scheduler-level
fairness are more fundamental than per-goal policy; the reservation floor
outranks *relative* weighted opportunity (an under-floor goal must be able
to reach its floor even when DWRR credit is exhausted), but never outranks
the global cap or the scheduler share. The floor path bypasses only DWRR,
and only for the goal that is itself under its floor.

### Guarantee (invariant, proven by tests with durable observations)

For every enabled reserved goal G with reservation R:

- if G has QUEUED (runnable) work,
- and global capacity ≥ R,
- and the claiming scheduler is eligible under fair share,

then no *other* goal can consume a free slot while G is below R and the
remaining free slots cannot cover G's deficit (`free − 1 < outstanding`
denies). Once G's RUNNING count reaches R, G competes for further slots
through the normal DWRR path. RUNNING work is never cancelled to satisfy a
floor; under sustained contention the floor is reached via the protection
path (competitors denied) and the floor path (G admitted past DWRR).

### Deterministic edge cases

- Multiple reserved goals simultaneously under floor with fewer free slots
  than their combined deficit: the slot goes to whichever goal claims
  first (existing transaction/race semantics); each claim admits its own
  under-floor goal. Documented, deterministic, no wall-clock fairness.
- `reservation == 0` or disabled config: no floor, no protection
  contribution (the goal's claims behave exactly like ADR-027).
- `reservation == global_max`: while the goal is runnable and below its
  floor, no other goal can claim at all; once the goal holds the full cap,
  its own further claims follow DWRR.

## Dynamic changes (Phase D)

- Config writes (`set`/`remove`/`enable`/`disable`, cap change) are
  transactional and emit `goal_reservation_changed` atomically with the
  durable change.
- Existing RUNNING work is never retroactively cancelled or re-owned; new
  claims use the current durable policy.
- A change that would violate the oversubscription policy fails closed
  (typed error, nothing written, no event).

## Cross-process correctness (Phase C)

All decisions (gates 2–5) read only durable rows inside `BEGIN IMMEDIATE`;
two processes racing for the last slot observe identical counts and exactly
one wins. Subprocess tests prove: shared registry, both processes see the
same reservations, a hot process cannot consume a reserved goal's floor,
reservations never exceed the cap, exactly-one-owner claims, stale
scheduler reclaim does not permanently consume reservation capacity
(reclaimed rows return to QUEUED under the same goal), and rapid repeated
claims cannot bypass the floor.

## Restart/crash (Phase E)

Reservations live in the durable table (survive reopen); DWRR deficit is
already durable; reclaim returns work to QUEUED under its original goal so
reclaimed work counts toward the correct reservation; a crashed scheduler
cannot permanently consume another goal's reserved capacity (its RUNNING
rows are reclaimed, then its goal's under-floor claims are admitted);
mutation-lock recovery is untouched (ADR-020/021); telemetry describes
transitions without becoming authority (ADR-028 rule unchanged).

## Observability (Phase F)

New event kinds (follow ADR-028 taxonomy conventions):

- `goal_reservation_changed` — config set/remove/enable/disable
  (detail: goal_id, config="goal_reservation", reason, outcome, by…).
- `reservation.denied` — a claim blocked by the protection path
  (reason="reservation"; bounded detail incl. pressure).
- `reservation.satisfied` — an under-floor goal's RUNNING count reaches
  its reservation (goal_id, work_id, reservation, running).

`_SECH_DETAIL_KEYS` gains: `reservation`, `reserved_capacity`, `pressure`,
`satisfied`, `running`, `deficit`, `reserved_goal_id`.

`scheduler_status()` gains:

- `goal_reservations` — list of configs (goal_id, reservation, enabled,
  updated_at, updated_by);
- `reserved_capacity` — sum of enabled reservations;
- `reservation_satisfied` — {goal_id: bool} for enabled reserved goals
  (running ≥ reservation);
- `reservation_pressure` — Σ max(0, reservation − running) over enabled
  reserved goals with QUEUED work (deterministic from durable rows).

`scheduler watch` human rows gain `reservation` extra lines for the new
kinds. Telemetry remains observational ONLY (never consulted by gates 2–6;
the gates read authority tables only — adversarial tests prove it).

## CLI (Phase G)

Mirror the existing `weights`/`weight` commands:

```text
arion scheduler reservations                          # list
arion scheduler reservation set <goal_id> <n> [--disable] [--by WHO]
arion scheduler reservation remove <goal_id>
arion scheduler reservation enable <goal_id>
arion scheduler reservation disable <goal_id>
```

Human + `--json` output, bounded validation (same errors as the store),
persistence across restart, `--by` audit metadata. Planner/model/task
metadata cannot modify reservations (no path exists; adversarial tests).

## Security boundary (Phase H)

Forged reservation events, fake goal/scheduler ids, forged capacity
counts / DWRR deficits / queue positions / stale ownership have zero
effect: the admission gates read only `scheduler_work` (authority),
`scheduler_goal_reservations` (authority config), and `scheduler_goal_state`
(authority, clamped at spend). Forged configuration through planner/model/
task metadata is impossible (config API is store/CLI-only). A goal cannot
use another goal's reservation (protection path excludes the claiming
goal's own deficit and blocks claims that would starve others). Disabling
a reservation cannot be forged by work metadata.

## Demo (Phase I)

`scripts/demo_adr029_reserved_capacity.py`, deterministic (fixed
timestamps, no wall-clock races), 25–35 checks, scenarios:

A default reservation = 0; B single reserved goal; C 2-goal reservation
protection; D high-weight goal cannot consume reserved capacity; E
multiple reservations; F reservation + DWRR interaction; G idle reserved
goal releases capacity; H goal becomes runnable again; I cross-process
reservation enforcement (real subprocess); J reservation change while
queued; K restart/reclaim; L reservation-denial telemetry; M forged
reservation attempt; N CLI/status/watch output.

## Acceptance criteria (tests-first)

- **A model:** default 0; validation (int, >= 0, bounded, <= cap); total
  <= cap rejected; cap-lowering rejected; no-cap behavior documented +
  tested; durability across reopen; enable/disable/remove; `--by`.
- **B admission:** floor reached under contention (durable RUNNING
  counts); protection denies competitors; floor bypasses DWRR; weight
  interaction (remaining capacity stays weighted); equal res + unequal
  weights; unequal res + equal weights; res 0; res == cap; multiple
  reserved goals; idle releases; runnable-again re-engages.
- **C cross-process:** real subprocesses; shared registry; same
  reservations; hot process cannot eat a floor; cap never exceeded;
  exactly-one-owner; reclaim frees reservation capacity; transactional
  enforcement; rapid claims cannot bypass.
- **D dynamic:** set/remove/increase/decrease/disable while queued; idle;
  cap change; restart persistence; fail-closed oversubscription; no
  retroactive cancellation.
- **E restart/crash:** reservation + DWRR state survive; stale reclaim
  counts toward the right goal; crashed scheduler cannot consume another
  goal's floor; mutation-lock exactly-once; telemetry never authority.
- **F telemetry/status:** three new kinds emitted atomically; status
  fields correct; watch shows them; observational-only proven.
- **G CLI:** list/set/remove/enable/disable; human + JSON; validation
  errors deterministic; persistence.
- **H adversarial:** forged events/config/counts/deficits powerless;
  no authority creation; cap respected; no cross-goal reservation theft;
  disable not forgeable.
- **I demo:** 25–35 deterministic checks.
- **J docs:** this ADR + architecture.md + scheduler CLI docs.

## Verification

Per-phase: tests red → implement → focused green → regression suite.
Final: full suite, ADR-024…029 demos + all repo demos, CLI smoke + JSON,
cross-process, adversarial, restart/crash, repeated stability runs.
Baseline at ADR-028: **790 passed, 2 skipped**.
