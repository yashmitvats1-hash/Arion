# ADR-028 — Scheduler Observability / Telemetry

- **Status:** Approved & implemented (2026-08-16)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Problem

The ADR-025/026/027 scheduler is durable and cross-process, but its
behavior is hard to OBSERVE: there is no queryable record of claims,
denials, heartbeats, reclaims, handoffs, abandonment, capacity/fair-share/
DWRR decisions, or scheduler lifecycle. Operators and tests cannot answer
"who owns this work, why was a claim denied, did DWRR refill, is a
scheduler alive or stale?" without digging into raw registry rows.

## Goals / non-goals

**Goals**

- A durable, bounded, queryable telemetry layer over the EXISTING audit
  abstraction (`AuditEvent` + `audit_events` table) - no second competing
  event system.
- Instrument every scheduler state transition, with the event committed
  ATOMICALLY with the state transition wherever practical (a claim success
  event can never outlive a rolled-back claim).
- Typed event taxonomy, bounded metadata, schema version, reason codes for
  denials (global capacity / scheduler fair share / goal-weight DWRR /
  stale owner / terminal / unknown).
- Read-only query API (recent/by scheduler/goal/work/type/since, bounded
  limits, no unbounded SELECT *).
- `scheduler status` snapshot model computed from durable state (an
  observation, never a second authority).
- `scheduler watch` CLI (human + stable JSON, filters, `--follow` bounded
  polling with graceful shutdown, NO mutation/registration/heartbeat).
- Bounded retention: explicit `prune_scheduler_events(cutoff)` with batch
  deletion; pruning never touches authority tables and can never affect
  execution.
- **Critical authority rule:** telemetry is observational only. Forged,
  modified, or deleted events have ZERO effect on execution semantics -
  the registry rows + transactional claim path remain the only authority.

**Non-goals**

- No change to admission, ownership, leases, weights, capacity, approval,
  recovery, mutation locks, or dependencies.
- No storing secrets, prompts, planner output, credentials, task payloads,
  or memory contents.
- No new scheduling behavior.

## Design

### Event model (reuse `AuditEvent`)

Extend `EVENT_KINDS` with the scheduler taxonomy:

```
scheduler.registered        scheduler heartbeat (scheduler id, lease)
scheduler.heartbeat         scheduler.shutdown
scheduler.abandoned         scheduler.config_changed
work.queued                 work.claimed
work.claim_denied           work.heartbeat
work.reclaimed              work.handoff
work.completed              work.failed
capacity.denied             scheduler_share.denied
goal_weight.denied          goal_weight.refill
```

Each event carries bounded detail: scheduler_id, worker_id, goal_id,
task_id, work_id, step_index, lease_expires_at, reason, weight, credit
before/after, outcome. No secrets / payloads / prompts.

### Storage

- New table `scheduler_events` (id, ts, scheduler_id, worker_id, goal_id,
  task_id, work_id, step_index, event_type, reason, success, detail,
  schema_version) with bounded detail JSON. Follows the existing
  `audit_events` conventions.
- A `scheduler_events` insert happens INSIDE the same `BEGIN IMMEDIATE`
  transaction as the state transition (claim/heartbeat/reclaim/handoff/
  terminal/registration) - the event and the state commit atomically, so a
  rolled-back transition leaves no phantom success event.
- Scheduler-config/registration events are emitted through the same path.

### Instrumentation points (store = the authoritative transition path)

- `register_scheduler` -> `scheduler.registered`
- `heartbeat_scheduler` -> `scheduler.heartbeat`
- `unregister_scheduler` -> `scheduler.shutdown`
- construction-time `reclaim_stale`/`abandon_foreign_queued` ->
  `work.reclaimed` / `scheduler.abandoned`
- `claim`/`claim_next` -> `work.claimed` or `work.claim_denied` (with the
  precise reason: capacity / scheduler_share / goal_weight / stale /
  terminal) + `goal_weight.refill` when a DWRR refill round occurred
- `heartbeat` -> `work.heartbeat`
- `release_and_claim_next` -> `work.handoff`
- `mark_terminal` -> `work.completed` / `work.failed`
- `set_goal_weight`/`remove_goal_weight`/`set_goal_weight_enabled` ->
  `scheduler.config_changed` (goal weights)
- `set_scheduler_global_max` -> `scheduler.config_changed` (capacity)

### Query API (read-only, bounded)

`recent_scheduler_events(limit)`, `scheduler_events(scheduler_id=,
goal_id=, work_id=, event_type=, since=, limit=)` - all bounded
(default limit 100, max 1000), ordered by rowid.

### Status snapshot (`scheduler_status()`)

Computed from durable state: global_max, running_count, queued_count,
active_schedulers, stale_schedulers (expired registration leases), running
work by scheduler, queued/running work by goal, goal weights, current DWRR
credit (bounded), recent reclaim count, recent failure count. Read-only.

### `scheduler watch` CLI

```
arion scheduler watch [--json] [--goal G] [--scheduler S] [--work W]
                      [--type T] [--since TS] [--limit N] [--follow]
```

Human output answers: what's running / who owns it / lease age / which
scheduler consumes capacity / which goals get capacity / why claims denied
/ lease expiry / reclaim / DWRR refill / scheduler alive-or-stale. JSON is
stable and scripting-friendly. `--follow` = bounded polling (default 2s,
no unbounded memory growth: keeps a bounded window + prints deltas), no
mutation, no registration, no heartbeat, Ctrl-C clean. Events are read via
the store query API only.

## Acceptance criteria (tests-first)

- **A event model/storage:** taxonomy; bounded detail; durability; atomic
  commit with transitions; rollback leaves no phantom event; schema
  version; no secrets.
- **B instrumentation:** every transition emits; denial reason codes;
  DWRR refill exposes weight + credit before/after; events are never
  consulted by the admission path.
- **C query API:** filters + bounded limits; ordering; unknown filters
  fail closed.
- **D status snapshot:** all fields; computed from durable state; no
  caching authority.
- **E watch CLI:** human + JSON; filters; --follow bounded; no mutation;
  no registration/heartbeat side effects.
- **F retention:** prune older than cutoff; bounded batch; authority
  tables untouched; pruning cannot affect execution; recent events never
  silently deleted.
- **G adversarial:** forged/deleted/duplicated events cannot create
  ownership, extend leases, complete work, bypass capacity, alter weights,
  or resurrect stale work; oversized payloads rejected; bounded metadata.
- **H crash/restart:** subprocess crash after claim -> stale lease
  reclaimed WITH reclaim event; restart observes history; no duplicate
  mutation; committed events survive reopen; rollback no phantom;
  heartbeat-vs-stale distinguishable; abandoned != shutdown.
- **I demo:** `scripts/demo_adr028_scheduler_observability.py`, 25-35
  deterministic checks, scenarios A-P.
- **J docs:** ADR-028 + architecture.md + CLI docs.

## Verification

Full suite green; ADR-024..027 demos green; new ADR-028 demo green;
scheduler CLI smoke + JSON; adversarial + retention + crash/restart;
repeated stability runs of concurrency-heavy tests; linear history; clean
tree; commit with repository convention; push; update PR #1.

## Known limitations / future work

- Events are append-only with explicit pruning; no compaction.
- `--follow` is a bounded poller, not a push stream (documented).
- Retention is time-cutoff based; size-based retention is future work.
