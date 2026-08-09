# ADR-015 — World State, Strategy Selection, Long-Horizon Goals

- **Status:** Approved & implemented (2026-08-09)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

The cognitive architecture spine is:

```
World State -> Beliefs -> Goals -> Long-Horizon Planning -> Strategy
Selection -> Authorization -> Execution -> Observation -> Verification
-> Learning
```

Episodic memory records what happened; beliefs capture what Arion believes.
The next step: keep the CURRENT world state (with change detection so stale
facts cannot mislead planning), choose a STRATEGY for a goal from that state +
beliefs + memory, and manage long-horizon goals that span multiple planning
sessions via plan versions.

## Decision

### 1. World State / change detection (`WorldStateMonitor`)

Environment facts are versioned per key (`EnvironmentFact.version`,
`observed_at`). `WorldStateMonitor.observe(key, value)`:

- first observation records v1 (not a "change");
- a value change bumps the version and emits `world.state.changed` (key,
  old_value, new_value, version);
- unchanged re-observation refreshes `observed_at` without bumping the
  version;
- `stale_facts(max_age_days)` flags facts not observed recently, so planning
  can re-check before relying on them.

The planning context exposes the CURRENT environment facts (bounded, metadata
only) alongside retrieved memories.

### 2. Strategy selection (`StrategySelector`)

Deterministic, explainable strategy selection per goal:

- environment lacks a capability the goal needs → `blocked_missing_capability`;
- beliefs indicate the goal is blocked → `defer_retry`;
- memory guidance contains avoid entries → `avoid_known_failures` (with the
  avoid targets as constraints and guidance provenance);
- a semantic belief confirms the approach → `capability_verified`;
- otherwise → `direct`.

The strategy is INFORMATIONAL: it influences what the planner proposes and is
recorded (`strategy.selected`), but only the authorization layer decides what
may run. Provenance (belief/guidance/episode ids) is attached.

### 3. Long-horizon goals (`GoalManager`)

`goal_plans` table (goal_id, plan_version, strategy, plan_summary, created_at)
records every plan version a goal accumulates across sessions, so a goal's
history and strategy evolution are traceable. `GoalManager.progress(goal_id)`
reports per-goal task progress from the task store. The engine records a plan
version + strategy after each planning pass.

### 4. Cognitive hardening (from the deep review)

- **Validation:** `Belief` validates category/source/provenance structure
  (only episode/reflection/guidance id keys, lists of non-empty strings) and
  bounds confidence/importance/statement length; `Preference` validates
  key/value/user/source; `EnvironmentFact` validates key/source/version.
- **Versioning:** belief updates are APPEND-ONLY + VERSIONED - a higher-
  confidence revision is stored as a new row (version = max+1) and the prior
  row is marked `superseded_at` (history never rewritten; `list_beliefs`
  excludes superseded by default).
- **Authorization independence:** beliefs, preferences, environment facts,
  and world-state changes are informational. Adversarial tests prove stale/
  superseded beliefs, poisoned beliefs, model-sourced instructions,
  preference manipulation, and memory-derived strategy changes never change
  a policy decision, actor identity, resource boundary, risk/approval answer,
  or capability registration.

## Consequences

- Planning sees the CURRENT world (bounded) with staleness flags - stale
  facts cannot silently mislead.
- Goals become long-horizon objects with traceable plan/strategy history.
- The invariant holds: memory, cognition, reflections, beliefs, preferences,
  model output, world state, and strategy may influence PLANNING; only the
  live authorization layer authorizes execution.
- New events: `step.skipped`, `world.state.changed`, `strategy.selected`.

## Related

ADR-014 (cognitive state), ADR-013 (learning loop), ADR-009 (authorization
authoritative), ADR-008 (offline testing).
