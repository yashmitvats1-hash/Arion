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

---

## Phase-0 assessment + proposed increment: long-horizon strategy learning

**Date:** 2026-08-17.

**Status:** Phases A–F IMPLEMENTED, tests-first (outcome recording →
selection influence → observability/CLI → restart/crash + adversarial →
pruning integration + lifecycle matrix → demo). Acceptance criterion F is
IMPLEMENTED by `scripts/demo_adr015_strategy_learning.py` — 33 deterministic
checks (fixed timestamps, no wall-clock assertions, byte-identical across
three runs), proving the five base rules + empty-history identity, outcome
recording, long-horizon learning (success preference, failure avoidance,
precedence, dissimilar isolation), provenance, bounded 20-row window +
prune exposure, restart/repair semantics, observability (bounded
`strategy.outcome` events, non-content CLI JSON), and the authority
boundary (forged memory/telemetry/outcome rows cannot manufacture
authoritative state; scheduler weights/reservations/ceilings/config/
ownership byte-identical). Phase E added two hardening details over the
original design: the selector fails closed on outcome rows whose
`goal_description` exceeds 300 chars or `reason` exceeds 200 chars
(raw-SQL-forged oversized rows cannot poison context matching), and the
`cognition strategies --json` CLI omits the free-text `goal_description`
(design §8: "no content, ids + counts only"). Final verification: full
suite **1178 passed, 2 skipped**; ADR-015 focused suite 118 ×3 stable;
ADR-014/013 suites green; ADR-029 (76) / ADR-030 (49) / ADR-031 (59)
green; all 20 demos green (incl. the new ADR-015 demo ×3).

### 0. State verification (exact)

- Local HEAD = remote `arena/01a00a1d-arion` = PR #1 head = **`eb2a235`**
  (ADR-014 addendum commit); working tree **clean**.
- PR #1: **OPEN**, base `arena/019fe578-arion`, **MERGEABLE**, 9 strictly
  linear commits (`06cda3a` ADR-025 … `0c3188e` ADR-013, `eb2a235`
  ADR-014 addendum). ADR-014 confirmed present remotely (ArchivalPolicy
  doc, `prune` implementation, 6 new test/demo files, PR body section).
- **CI: NONE.** No `.github/workflows`, no checks on PR #1, 0 Actions
  runs. Verification is local-only.
- **Baseline suite: 1075 passed, 2 skipped** (exit 0) — exact match with
  the ADR-014 final state.

### 1. Current behavior (verified against code + tests)

**Planning spine (ADR-015/016):** `World State → Beliefs → Goals →
Long-Horizon Planning → Strategy Selection → Authorization → Execution →
Observation → Verification → Learning`.

**Strategy selection (`arion/cognition/strategy.py`, `StrategySelector`):**
five deterministic, first-match rules over `(goal_description, beliefs,
environment, guidance, previous_strategies)`:

1. env lacks a capability the goal needs → `blocked_missing_capability`;
2. semantic/procedural belief says the goal is unachievable →
   `defer_retry`;
3. guidance contains avoids → `avoid_known_failures` — with the ONLY
   history-aware rule in the system: if `avoid_known_failures` is already
   in `previous_strategies`, escalate to `defer_retry` (ADR-016);
4. semantic belief says the approach is achievable → `capability_verified`;
5. otherwise → `direct`.

`previous_strategies` is a list of strategy **names** from the goal's
immutable plan history (`GoalManager.strategy_for` / engine
`_build_planning_context`). No outcome data reaches the selector.

**Strategy recording:** `goal_plans` (immutable rows: `goal_id,
plan_version, strategy, plan_summary, reason=initial_plan|replan_<evidence>,
created_at`); `goals.strategy` (current), `goals.status`, `goals.version`,
`goals.last_replan_reason`, `goals.progress_metadata`; events
`strategy.selected`, `goal.replanned`, `goal.state.changed`,
`progress.evaluated`.

**Outcome recording:** `Episode` (outcome `completed|failed|denied|
recovered`, goal text, `plan_summary` with param KEY names only, actions,
resources, verification, typed failures, authorization denials, recovery,
tags, importance, lifecycle) — **no strategy field, no plan_version
field**; `Task.plan_version` is the two-hop join key
(episode.task_id → task.plan_version → goal_plans.strategy). Terminal goal
transitions funnel through `GoalManager.transition` (`complete_goal`,
`fail_goal` — engine `run_goal`/`run_goals` lines 1188/1204/2219/2247).

**What is learned from an episode:** outcome + goal + plan skeleton +
failures (typed categories) + denials + recovery + tags + importance;
reflection (lesson/recommendation/confidence); derived guidance
(avoid/prefer/informational per capability/action/resource with strategy
hints like `defer`, `alternative_action`); derived beliefs (semantic
"not permitted"/"failed"/"achievable" + procedural lessons).

**What survives consolidation:** `ConsolidationRecord`
(`consolidation_id, source_episode_ids, category, merged_lesson, count,
importance, created_at`); episodes marked `consolidated` with
importance decay/boost; ADR-014 Phase E lifts merged lessons into
procedural beliefs with provenance. Sources are prunable; the record and
its provenance ids survive pruning.

**What influences future planning (`_build_planning_context`):**
retrieved episodes/reflections → guidance → non-mutating plan
transformation (resource/action substitution or drop, registry-aware);
beliefs (up to 100); environment facts (up to 20); plan history (last 5
versions: strategy/reason/steps-count only); ADR-020 recovery advisory;
the selected strategy. All informational.

### 2. Concrete gaps (evidence)

- **G1 — No strategy→outcome association exists.** `Episode` has no
  `strategy`/`plan_version` fields (`arion/memory/models.py`); `goal_plans`
  has no outcome column; no `strategy_outcome*` table anywhere (grep over
  `arion/ tests/ scripts/` is empty). A strategy's result is only
  inferable via a two-hop join (episode → task.plan_version → goal_plans)
  that nothing performs.
- **G2 — Selection is rule-only; historical OUTCOMES cannot influence
  it.** `StrategySelector.select` receives strategy names only; the sole
  history rule is the avoid→defer escalation. Nothing answers "did
  `direct` ever succeed for goals like this one?"
- **G3 — The file named `test_strategy_learning.py` does NOT test
  strategy-selection learning.** It tests guidance-driven *plan
  transformation* (avoid → alternative decomposition). Strategy-selection
  learning is untested and unimplemented.
- **G4 — No per-goal-context strategy affinity.** Beliefs are
  capability/resource-level ("X on R fails"), never strategy-level ("for
  goals shaped like G, `defer_retry` completes and `direct` does not").
- **G5 — Replan reasons are evidence labels, not outcome labels.**
  `reason=replan_failed_task` says *why* the goal replanned, not that the
  previous strategy (e.g. `direct` v1) ended in `failed`/`superseded`.
- **G6 — CLI cannot inspect strategy outcomes.** `goals show` shows the
  current strategy; `cognition goals` shows plan versions (strategy +
  reason + step count) but no outcome per version.

### 3. Proposed data model + lifecycle

**New durable table `strategy_outcomes`** (shared DB, cognition schema,
same `_threadsafe` store pattern as `goal_plans`):

```text
outcome_id      TEXT PRIMARY KEY      -- new_id("sout")
goal_id         TEXT NOT NULL
goal_description TEXT NOT NULL        -- bounded context (<=300 chars)
strategy        TEXT NOT NULL         -- direct|avoid_known_failures|...
plan_version    INTEGER NOT NULL
outcome         TEXT NOT NULL         -- succeeded | failed | superseded
reason          TEXT NOT NULL DEFAULT ''
episode_id      TEXT                  -- terminal episode when available
created_at      TEXT NOT NULL
UNIQUE(goal_id, plan_version)
```

**Lifecycle (write points = the two authoritative funnels):**

- `GoalManager.record_plan_version` — when a NEW plan version is created
  (reason `replan_*`), mark the PREVIOUS latest plan version's outcome
  `superseded` (reason = the replan evidence). Replay-safe: the existing
  replay-return path (same strategy/summary/reason, no task) creates no
  duplicate and writes no duplicate outcome.
- `GoalManager.transition` → `complete_goal` — mark the LATEST plan
  version `succeeded` (reason `all_work_complete`).
- `GoalManager.transition` → `fail_goal` — mark the LATEST plan version
  `failed` (reason `max_replans_exceeded` or explicit failure).
- A deterministic **catch-up pass** (like ADR-013
  `learn_from_terminal_tasks`) re-derives missing outcome rows from the
  AUTHORITATIVE goals/goal_plans/tasks state (never from episodes or
  telemetry).

Outcomes are **derived from the goal state machine**, which is durable and
authoritative — the same rule as ADR-013 catch-up: memory never creates
authority, and here authority (goal state) creates the informational
outcome record.

### 4. How learned preferences affect planning — without authority

- `StrategySelector.select` gains an OPTIONAL
  `outcome_history: list[dict] | None = None` parameter. Default
  `None`/empty ⇒ **byte-identical current behavior** (existing rules run
  first; existing tests must pass unmodified).
- New deterministic, explainable rules evaluated AFTER the existing five
  (as tie-breakers/preferences, not overrides), e.g.:
  - `direct` failed ≥2 times for a similar goal context → prefer
    `avoid_known_failures` (if avoids exist) or `defer_retry`;
  - a strategy succeeded before for a similar goal context → prefer it.
- "Similar goal context" = deterministic token-overlap signature of the
  goal description (same `_similar_goal` style as consolidation), bounded
  to the most recent N outcomes (e.g. 20 per strategy).
- The strategy still only shapes the **planner proposal**; the live
  authorization layer decides execution. Learned preferences NEVER touch:
  scheduler admission, weights, ceilings, reservations, DWRR credit,
  ownership/leases, approvals, recovery enforcement, or any policy
  config (weights and policies stay config-only — standing constraint).
- Provenance: selection adds `outcome_ids`/`strategy_ids` to the strategy
  provenance; `strategy.selected` event unchanged in shape.

### 5. Provenance and poisoning defenses

- Every outcome row carries `goal_id`, `plan_version`, `strategy`,
  `reason`, optional `episode_id`; selection provenance references the
  outcome ids it used.
- Deterministic-only derivation (no model, no embeddings).
- Outcome rows are written ONLY by the goal lifecycle funnels — never by
  memory/planner/model/telemetry content. Forged outcome rows cannot:
  alter tasks/goals authority, scheduler state, approvals, locks,
  recovery, or any policy decision (adversarial tests, mirroring
  ADR-013/014 boundaries).
- Bounded inputs: `goal_description` truncated; `reason` truncated;
  `strategy` must be one of the known names (fail closed otherwise);
  malformed/oversized ids fail closed.
- Memory deletion/pruning (ADR-014) cannot fabricate or alter outcomes
  (outcomes derive from goals, not episodes).

### 6. Idempotency / restart semantics

- `UNIQUE(goal_id, plan_version)` ⇒ repeated transitions are idempotent
  (INSERT OR IGNORE / OR REPLACE with same values).
- Crash between a goal transition and the outcome write ⇒ the catch-up
  pass repairs deterministically on next start (bounded, evented, never
  touches scheduler authority — same contract as ADR-013 catch-up).
- No in-memory state; all durable in the shared DB; fresh processes see
  the same outcomes.
- Completed mutations are never replayed (ADR-025 rule): outcome marking
  is a derived record, not a mutation replay.

### 7. Bounded growth / ADR-014 interaction

- `strategy_outcomes` is 1:1 with `goal_plans` rows, so it inherits
  `goal_plans` cardinality. **Extension of ADR-014 Phase B:**
  `prune_goal_plans` also deletes `strategy_outcomes` rows for removed
  `(goal_id, plan_version)` pairs (same bounded SELECT-then-DELETE
  batches, same fail-closed validation, same dry-run/deterministic
  semantics) — outcomes never outlive their plan version.
- Prune authority isolation extends to the new table (never touches
  scheduler/task/authority state); ADR-014 authority/restart/adversarial
  suites gain one table each.
- No new unbounded growth: per-goal outcome history is bounded by
  plan-version history.

### 8. CLI / observability (proposed)

- `arion goals show <id>` — add a bounded per-strategy outcome summary
  (attempts / succeeded / failed / superseded) to the existing output.
- `arion cognition strategies [--goal G] [--json]` — bounded, read-only
  outcome history listing (deterministic order; no content, ids + counts
  only). No new mutating CLI commands (outcomes are lifecycle-derived).
- Event `strategy.outcome` (bounded: `goal_id, strategy, plan_version,
  outcome, reason`; never content) emitted on each outcome write —
  observational only (ADR-028 rule).
- Exit 0 / 1 fail-closed conventions as in ADR-014.

### 9. Acceptance criteria + proposed test phases

- **A — outcome recording:** replan ⇒ previous version `superseded`;
  complete ⇒ `succeeded`; fail ⇒ `failed`; one row per plan version;
  replay-safe no-duplicate; catch-up repair after crash; authority tables
  untouched (tests-first).
- **B — selection influence:** default (no history) byte-identical;
  deterministic preference rules with provenance; preferences never
  change authorization/scheduler; poisoning-fail-closed validation.
- **C — observability/CLI:** `strategy.outcome` event bounded; `goals
  show` summary; `cognition strategies` command + `--json`; deterministic
  output; exit 1 on invalid input.
- **D — adversarial:** forged outcome rows cannot influence
  scheduler/tasks/approvals/policy; malformed/oversized inputs fail
  closed; delete-all memory leaves outcome derivation + scheduler
  byte-identical; outcomes can never authorize.
- **E — restart/archival:** outcomes survive restart; catch-up
  deterministic; `prune_goal_plans` prunes outcome rows with their plan
  versions (idempotent, dry-run byte-identical); scheduler state
  byte-identical after outcome pruning.
- **F — demo + docs:** IMPLEMENTED — `scripts/demo_adr015_strategy_learning.py`,
  **33 deterministic checks** (fixed timestamps, no wall clock), run ×3 with
  byte-identical output, covering the nine demo areas (initial selection,
  outcome recording, long-horizon learning, provenance, bounded learning +
  prune exposure, restart/repair, observability, authority boundary,
  determinism). Docs: this ADR addendum carries the implementation status
  and final verification numbers.

---

## Proposed scope (Phase 1 and beyond — NOT executed yet)

**Smallest safe seam (the answer to "where"):** `StrategySelector.select`
already accepts `previous_strategies` (names) but nothing outcome-bearing.
The increment adds: (1) a durable `strategy_outcomes` table written at the
two authoritative GoalManager funnels (`record_plan_version`,
`transition`); (2) an optional `outcome_history` input to `select()`
(default empty ⇒ current behavior byte-identical) feeding deterministic
preference rules; (3) catch-up repair; (4) `prune_goal_plans` outcome
coupling; (5) `strategy.outcome` event + read-only CLI; (6) adversarial +
restart suites; (7) demo + docs. No scheduler-admission, policy, or
authorization changes. Tests-first per phase, linear history preserved.
