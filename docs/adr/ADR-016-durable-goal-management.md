# ADR-016 — Durable Goal Management and Long-Horizon Execution

- **Status:** Approved & implemented (2026-08-09)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

ADR-015 introduced world state, strategy selection, and long-horizon goals
with plan history. The next spine segment upgrades goals from a passive
bookkeeping record to the authoritative, restart-safe state machine that owns
the full loop:

```
Goal → Goal State → Strategy → Plan → Execute → Observe → Learn → Replan
```

Requirements driving this ADR:

1. **Explicit, auditable goal lifecycle** — ACTIVE, PAUSED, BLOCKED,
   COMPLETED, FAILED, CANCELLED; invalid transitions fail closed.
2. **GoalManager is authoritative** — it owns goal state, version, strategy,
   immutable plan versions, task history, progress metadata, blockers,
   timestamps; completion is NEVER inferred from a single successful task.
3. **Deterministic ProgressEvaluator seam** — model-independent evaluation of
   completed/failed/skipped work, blockers, outstanding plan steps, and
   world-state changes → structured progress/status/blockers/next_action/
   evidence-with-provenance.
4. **Replanning loop** — a terminal task triggers reevaluation; incomplete
   work produces a NEW immutable plan version (never mutating previous ones),
   consuming bounded memory/reflections/beliefs/strategy/world state/plan
   history.
5. **Strategy selection** extends ADR-015 (never replaced): explainable with
   provenance; escalation `direct → avoid_known_failures → defer_retry`;
   strategies can never modify authorization metadata.
6. **World-state changes** between plan versions trigger reevaluation →
   possible replan through a deterministic seam. No background daemons.
7. **Checkpoint/restart semantics** — a process may die at any boundary; a
   fresh process recovers without duplicating plan versions or corrupting
   goal state; tasks stay at-least-once.
8. **Authorization invariant extended** — goal state, strategy, progress
   evaluation, world state, memory, model output, and replanning can NEVER
   grant permissions; old successful decisions are never reused after
   resource/action metadata changes.
9. **Auditability** — structured events with IDs/provenance/bounded metadata;
   never raw prompts/responses/secrets.
10. **CLI** — `arion goals list|show|progress|pause|resume|cancel [--json]`.

## Decision

### 1. Goal lifecycle state machine (fail closed)

`GoalStatus` (active/paused/blocked/completed/failed/cancelled) with an
explicit transition table `GOAL_TRANSITIONS`; `GoalManager.transition()`
validates every transition and raises `GoalStateError` on invalid ones
(e.g. `paused → paused`, `cancelled → active`). Every successful transition:

- increments `goal.version` (revision counter, restart-safe);
- persists the row with a new `updated_at`;
- emits `goal.state.changed` with `from`, `to`, `reason`, `goal_version`,
  `actor`.

Terminal states (completed/failed/cancelled) have no outgoing transitions.

### 2. GoalManager — authoritative, persistent

`GoalManager` is the state machine; the engine calls it for all goal
operations. It persists, via the existing `SQLiteStorage` +
`SQLiteCognitiveStore` (same DB file):

- identity, description, source;
- current status + version; strategy (follows the latest plan version);
- blockers (structured, keyed, idempotent);
- progress metadata (last evaluation summary);
- `last_evaluated_at`, `last_replan_reason`, timestamps;
- plan versions (`goal_plans`: version, strategy, summary, reason, created_at);
- task history (`tasks.plan_version`).

Completion rule (never single-task): a goal is `complete` only when the
LATEST plan version's steps are all handled (succeeded or explicitly
skipped), there is no unresolved failure on that version, and there are no
blockers. A single successful task never completes a goal with outstanding
steps.

### 3. ProgressEvaluator (deterministic seam)

`DeterministicProgressEvaluator.evaluate(goal, tasks, latest_plan,
world_changes) → ProgressResult` (progress 0..1, status, blockers,
next_action, evidence). Rule order (first match wins):

1. terminal goal → `none`
2. paused → `paused`
3. blockers → `resolve_blocker` (status BLOCKED)
4. material world changes since last evaluation → `replan`
5. unresolved failure on the LATEST plan version → `replan`
   (failures on superseded plan versions are ignored once the newer plan is
   fully handled — `evidence["latest_plan_failed"]`)
6. no plan yet → `continue` (first plan)
7. all latest-plan steps handled + no latest-version failure → `complete`
8. otherwise → `continue`

`GoalManager.evaluate()` wraps it: computes relevant world changes, persists
`progress_metadata` + `last_evaluated_at`, emits `progress.evaluated` +
`goal.evaluated` (bounded detail).

Relevance filter (`_relevant_world_changes`): only `registered_capabilities`
or fact keys mentioned in the latest plan/goal description are material;
unrelated facts (e.g. `system_uptime`) never trigger a replan.

### 4. Plan versioning + replanning loop

- `record_plan_version` appends a NEW version (monotonic `plan_version`
  starting at 1); previous versions are immutable and never rewritten.
- **Replay-safe:** if the latest version matches (strategy, summary, reason)
  AND no task implements it yet, the existing version is returned instead of
  duplicating it. A failure against the latest version always yields a
  genuinely new version.
- Reasons are recorded: `initial_plan`, `replan_task_failed`,
  `replan_world_changed`, `replan_blocker_changed`, `replan_task_completed`,
  `replan_explicit_resume`, ... (bounded, audited in `plan.versioned` +
  `goal.replanned`).
- `engine.run_goal(goal_id, max_replans=5)` is the long-horizon loop with
  **per-call cycle semantics**: it evaluates, replans and executes, but
  returns as soon as a task FAILS (goal stays ACTIVE with the failure
  persisted) so the caller decides the next cycle; replanning is bounded
  across calls by `max_replans` (exceeded → goal FAILED with
  `last_replan_reason="max_replans_exceeded"`).
- Planning context (bounded) includes retrieved memory, guidance, beliefs,
  strategy, world state, and the goal's previous plan history
  (`PlanningContext.plan_history`).
- `pending_task` resumes only tasks implementing the LATEST plan version —
  stale pending tasks from superseded versions are never resumed (replay
  safety).

### 5. Strategy escalation (extends ADR-015)

`StrategySelector.select(..., previous_strategies=...)` — the engine passes
the goal's plan-history strategies so the selector escalates instead of
blindly repeating:

- `direct` (no signal) → after a failure with avoid guidance →
  `avoid_known_failures` → if still failing →
  `defer_retry` (don't repeat the same failing strategy).
- `blocked_missing_capability` when the goal names capabilities absent from
  the world state (with `missing_capabilities` constraints).
- Strategy is recorded per plan version AND on the goal row (restart-safe);
  it remains INFORMATIONAL — it can never authorize anything.

### 6. World-state → replan seam

`WorldStateMonitor.observe()` records versioned facts; `GoalManager` compares
changes since `last_evaluated_at`; material changes make `evaluate()` return
`replan` (`world_changed`). Everything is sequential and deterministic — no
daemons, no concurrency.

### 7. Restart safety

All goal/plan/progress state lives in the SQLite DB. A fresh process
(`build_engine(db_path, ...)` or `_build_engine` in the demo) reloads goals,
plan versions, tasks, and progress metadata. `run_goal` after restart:

- never duplicates plan versions (replay guard + monotonic counter);
- resumes the pending task for the latest plan version (at-least-once steps);
- re-evaluates from the CURRENT world state and CURRENT registry metadata.

### 8. Authorization invariant (tested)

Goal state, strategy, progress evaluation, world state, memory, model output,
and replanning are informational: only `PermissionPolicy.decide()` over live
`ActionSpec` metadata grants execution. In particular:

- replanning cannot bypass ActionSpec/resource boundaries (every step is
  re-authorized against the live registry);
- old successful decisions are never reused: changing `ActionSpec
  required_scope` (or the registry) makes previously-successful actions DENY
  on the next attempt (demonstrated in the DoD demo and adversarial tests).

### 9. Audit events

New kinds: `goal.created`, `goal.state.changed`, `goal.evaluated`,
`goal.replanned`, `progress.evaluated`, `plan.versioned` (plus existing
`strategy.selected`, `world.state.changed`). Events carry IDs, versions,
reasons, and bounded metadata only.

### 10. Memory classification fix (ADR-016 hardening)

A task resuming from a plan-only checkpoint (the normal start-of-run
boundary) is now classified `completed`, not `recovered` — `task.resumed`
carries `mid_execution` so only genuine mid-execution interruptions produce
`recovered` episodes. This makes successful outcomes yield `prefer` guidance
(learned successes), which the race-hardened transform (below) uses safely.

### 11. Race hardening in plan transformation

`apply_guidance_to_steps` will NOT resource- or action-substitute onto a
preferred resource that has SINCE become a known-failing (avoided) target;
it falls through to an explicit SKIPPED step with provenance. A `prefer`
from an old episode is never a license to re-target work onto a resource that
has failed since.

## Consequences

- Goals are durable, versioned, restart-safe, and auditable end-to-end.
- Replanning is bounded, deterministic, and every previous plan remains
  inspectable.
- Strategy evolution is explainable and informational.
- Authorization remains the single source of truth for permissions; the
  extended invariant is adversarial-tested.
- The DoD demo (`scripts/demo_goal_replan.py`) exercises the full 3-cycle
  flow including a mid-goal restart and live-metadata re-authorization.

## Definition-of-Done evidence

`scripts/demo_goal_replan.py` runs offline and passes all checks:

- Cycle 1: plan v1 `direct`, task fails on the binary README.md; failure
  persisted; goal stays ACTIVE (never completed from a failed task).
- Cycle 2: reevaluation retrieves memory/reflection/cognition evidence;
  strategy changes to `avoid_known_failures`; plan v2 re-targets the read via
  resource_substitution (provenance on the step); authorization independently
  approves each step; the task hits a RACE (docs/design.md turned binary) and
  fails — persisted.
- Restart: a fresh process on the same DB shows goal state, plan versions
  [1,2], task history, progress metadata, strategy + provenance identical;
  no duplicate plan version.
- Cycle 3: an irrelevant change (`system_uptime`) does NOT trigger a replan;
  a material change (`registered_capabilities`) DOES → plan v3
  (`replan_world_changed`, strategy escalates to `defer_retry`); the read step
  is explicitly SKIPPED with guidance provenance; the goal completes; all
  previous plans remain immutable; terminal goal creates no further versions.
- Authorization: tightening `filesystem.read` ActionSpec to
  `filesystem:write` makes a previously-successful read DENY under live
  metadata (`permission.denied`); restoring metadata lets the goal replan and
  complete (denial → learning, no stale retry).
- Strategy catalog: `direct`, `avoid_known_failures`, `defer_retry`,
  `blocked_missing_capability` all demonstrated with provenance.

## Not built yet (by decision)

Concurrency/daemons (world-change observation is a deterministic seam only),
persistent goal "scheduling", multi-goal prioritization, model-backed
evaluation, goal approval workflows, and plan diff/rollback tooling.
