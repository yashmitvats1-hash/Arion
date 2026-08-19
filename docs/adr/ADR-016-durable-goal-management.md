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

---

## Phase-0 assessment + proposed increment: plan diff & re-adoption tooling

**Date:** 2026-08-17. **Phase 0 only** — state verification + inspection +
design; NO implementation changes in this phase.

### 1. State verification (exact)

- Local HEAD = remote `arena/01a00a1d-arion` = PR #1 head = **`769f6c0`**
  (ADR-015 completion); PR #1 **OPEN, MERGEABLE**, base
  `arena/019fe578-arion`, 11 strictly linear commits; working tree **clean**.
- CI: **none configured** (no `.github/workflows`, 0 Actions runs) —
  verification is the local gauntlet.
- **Baseline full suite: 1178 passed, 2 skipped** (exit 0); ADR-015 focused
  suite 118 passed.

### 2. Existing architecture (what ADR-016 already implements, complete)

The original ADR-016 ("Durable Goal Management and Long-Horizon Execution",
approved & implemented) provides, all tested and green:

1. **Authoritative goal state machine** — `GoalStatus`
   (active/paused/blocked/completed/failed/cancelled), validated
   `GOAL_TRANSITIONS`, `GoalManager.transition()` (version bump,
   `goal.state.changed` event, `GoalStateError` fail closed); terminal
   states have no outgoing transitions.
2. **`GoalManager`** — authoritative owner of goal state, version, strategy,
   immutable plan versions (`goal_plans`), task history
   (`tasks.plan_version`), blockers (structured, keyed, idempotent),
   progress metadata, timestamps; completion never inferred from a single
   successful task.
3. **`DeterministicProgressEvaluator`** — rule-ordered evaluation → 
   `ProgressResult` (progress, status, blockers, next_action, evidence with
   provenance); `GoalManager.evaluate()` persists `progress_metadata` +
   `last_evaluated_at` and emits `progress.evaluated` + `goal.evaluated`;
   material world-change relevance filter (only capabilities / plan-mentioned
   keys).
4. **Plan versioning + replanning** — monotonic immutable versions,
   replay-safe `record_plan_version` (identical latest + no task ⇒ return
   existing version), reason catalog (`initial_plan`, `replan_task_failed`,
   `replan_world_changed`, ...), `run_goal` per-call cycle semantics,
   `max_replans` bound → `failed`, `pending_task` resumes only the latest
   plan version.
5. **Strategy escalation** (extends ADR-015) — `previous_strategies` from
   plan history; direct → avoid_known_failures → defer_retry; informational.
6. **Restart safety** — all goal/plan/progress state in SQLite; replay
   guard + monotonic counters; checkpointed task resume (at-least-once);
   live re-authorization against current registry metadata.
7. **Audit + CLI + demos** — `goal.created|state.changed|evaluated|
   replanned`, `progress.evaluated`, `plan.versioned`; `arion goals
   list|show|progress|pause|resume|cancel [--json]` (+ ADR-017 approve/deny);
   `scripts/demo_goal_replan.py` (3-cycle DoD) and
   `scripts/demo_adr016_goal_replan.py` (41 checks).

**Later ADRs already consumed the rest of ADR-016's "Not built yet" list:**
concurrency/daemons → ADR-024/025/026; persistent goal scheduling + multi-goal
prioritization → ADR-025–031 (durable scheduler, weights, reservations,
capacity planning, ceilings); goal approval workflows → ADR-017/018/019.
Model-backed evaluation remains deliberately deferred (determinism ethos).

### 3. Explicit gaps (evidence)

- **G1 — Plan diff tooling does not exist.** No `diff` anywhere in
  `arion/`; `cognition goals <goal_id>` lists versions (strategy/reason/
  steps-count only) and `goals show` summarizes — neither compares two plan
  versions. ADR-016's own "Not built yet (by decision)" list names **"plan
  diff/rollback tooling"** — the ONLY item from that list not built by
  ADR-017–031.
- **G2 — Plan re-adoption (rollback) does not exist.** No GoalManager
  method; the only way a goal returns to an earlier approach is the planner
  independently producing similar content (no provenance, no operator
  control). `record_plan_version`'s replay guard only dedupes an identical
  (strategy, summary, reason) — it cannot express "re-adopt plan vN".
- **G3 — No deterministic stored-plan execution path.** `_plan_for_goal`
  always invokes the planner; `create_task(goal, plan_version)` exists but
  nothing reconstructs task steps from a stored `plan_summary`.
  `PlanStep.to_dict()/from_dict()` round-trip makes deterministic replay
  feasible (plan_summary stores the full step dicts).
- **G4 — `arion goals progress` mutates.** The read-only-looking command
  calls `gm.evaluate()`, which persists `progress_metadata`,
  `last_evaluated_at`, `updated_at` and emits `progress.evaluated` +
  `goal.evaluated`. A inspection command must not write (ADR-014/015
  observability convention; ADR-015 Phase E fixed the analogous CLI content
  leak).
- **G5 (minor) — no goal-scoped event timeline.** `audit_events` has no
  `goal_id` column/index; per-goal histories require O(n) detail-JSON
  scans. (Informational; not required for the increment below — noted.)

### 4. Architectural seam (smallest safe boundary)

**Plan re-adoption (rollback) + plan diff tooling**, built exclusively on
existing seams:

- **Write path:** a new `GoalManager.readopt_plan(goal_id, from_version,
  reason)` that creates a NEW immutable plan version whose
  (strategy, plan_summary) are copied from a stored historical version —
  going through the EXISTING `record_plan_version` funnel (replay guard,
  monotonic versioning, ADR-015 supersede-outcome coupling,
  `plan.versioned` event). History is never mutated; the new version's
  `reason` = `replan_rollback_v<from_version>` (bounded, provenance-carrying).
- **Execute path:** engine creates a task for the re-adopted (or any
  stored) latest plan version from its stored `plan_summary` instead of the
  planner — deterministic replay; every step still passes the FULL live
  pipeline (authorization → approval → mutation lock → FIFO → capability →
  verification) at execution; checkpointed/restart-safe.
- **Read path:** a deterministic, bounded plan diff
  (`goals diff <goal_id> <va> <vb> [--json]`) over immutable versions —
  strategy/reason change + step-level added/removed/kept (param KEY names
  only, never values/secrets).
- **CLI hardening:** `goals progress` becomes read-only via a non-mutating
  evaluation peek (the loop keeps the mutating `evaluate()`).

**Classification:** plan versions and their diffs are DERIVED state
(informational for execution — steps are re-authorized live); re-adoption is
a goal-management (authoritative-goals) operation that only appends
immutable history — it never touches the scheduler, ownership, leases,
weights, reservations, ceilings, approvals, or policy. Scheduler/claim-path/
DWRR state must remain byte-identical (tested).

### 5. Proposed data model

No new tables required:

- `goal_plans` — existing immutable versions; re-adopted versions are
  ordinary rows with `reason = replan_rollback_v<N>`.
- `strategy_outcomes` (ADR-015) — existing coupling: re-adoption supersedes
  the previous latest via `record_plan_version` (no schema change).
- Optional (deferred, not required): a `goal_id` column + index on
  `audit_events` for goal-scoped timelines (G5) — NOT part of this scope.

### 6. Lifecycle

- `readopt_plan(goal_id, from_version, reason)`:
  1. validate: goal exists; `from_version` exists in `plan_history`
     (fail closed if pruned/never existed); `from_version` is not the
     latest; goal is not terminal-completed/cancelled (FAILED→ACTIVE
     remains legal via `transition`);
  2. deep-copy the stored (strategy, plan_summary);
  3. `record_plan_version(goal_id, strategy, summary,
     reason="replan_rollback_v<from_version>")` — replay guard makes a
     repeated identical re-adoption idempotent (same version returned);
  4. the previous latest plan version's outcome becomes `superseded`
     (existing ADR-015 funnel; event emitted once per durable change).
- Diff (read-only): `diff_plans(goal_id, va, vb)` — both versions must
  exist; deterministic output; `va == vb` → empty diff (valid, rc 0).
- Engine execution: when `run_goal`/`run_goals` needs a task for a goal
  whose LATEST plan version has no implementing task and the version is
  re-adopted (reason prefix) — or more generally, has a stored summary —
  build the task steps from the stored summary (no planner invocation).

### 7. Planning/engine integration

- `_plan_for_goal` gains a stored-plan fast path (additive, default off):
  if the latest plan version exists, has no implementing task, and has a
  non-empty stored summary, create the task with
  `create_task(goal, plan_version=latest)` and set steps from the stored
  summary instead of `planner.plan(...)`. All subsequent execution is
  unchanged (live authorization, checkpoints, memory recording).
- Strategy selection, guidance, and the preference layer (ADR-015) are
  UNCHANGED — re-adoption is orthogonal: it re-uses a plan, not a strategy
  choice (the stored strategy travels with the plan and is recorded on the
  new version + goal row as today).

### 8. Restart/crash semantics

- Re-adoption is durable (goal_plans row + outcome row in the same DB);
  a crash between `record_goal_plan` and the outcome write is healed by the
  existing `repair_strategy_outcomes()` (ADR-015).
- Repeated re-adoption after restart returns the same version (replay
  guard) — idempotent, no duplicate versions/outcomes/events.
- Stored-plan execution is checkpointed like any task (at-least-once,
  resume-from-checkpoint, `task.resumed` mid_execution classification).

### 9. Pruning/archival interaction (ADR-014)

- `goals diff` and `readopt_plan` fail closed on versions that were pruned
  (`prune_goal_plans` deletes plan rows; a pruned version is
  indistinguishable from never-existing ⇒ deterministic "not found").
- `prune_goal_plans` already protects the latest version (keep_latest ≥ 1)
  and deletes coupled `strategy_outcomes` rows (ADR-015 Phase D) — no new
  coupling needed.
- Re-adoption of a surviving historical version is unaffected by pruning of
  other versions; after pruning, the diff/rollback CLI surfaces only
  surviving versions (bounded, deterministic).

### 10. Observability/CLI (proposed)

- `arion goals diff <goal_id> <version_a> <version_b> [--json]` — read-only,
  deterministic, bounded (param KEY names only; strategy/reason/step-count
  deltas); exit 0 incl. empty diff; exit 1 on unknown goal/version or
  invalid input (fail closed).
- `arion goals rollback <goal_id> <version> [--reason R]` — mutating via
  `readopt_plan`; prints the new plan version; exit 1 on invalid (unknown/
  pruned/latest version, terminal-completed/cancelled goal).
- `arion goals progress` — becomes READ-ONLY (non-mutating peek; no
  `progress.evaluated`/`goal.evaluated` emission, no persistence); the
  engine loop alone mutates via `evaluate()`.
- Events: no new kinds required — `plan.versioned` (reason carries
  `replan_rollback_v<N>`) and `strategy.outcome` supersede already fire
  through the existing funnels; document.

### 11. Security/authority boundary

- Re-adopted plans are INFORMATIONAL for execution: every step passes live
  authorization against current ActionSpec metadata (ADR-016 invariant,
  adversarial-tested). Rollback can never grant permissions, bypass
  approvals/recovery/locks, or touch scheduler admission/weights/
  reservations/ceilings/DWRR/ownership/leases (byte-identical assertions).
- `readopt_plan`/`diff_plans` validate: goal existence, version existence,
  strategy ∈ STRATEGY_NAMES, plan_summary structure (list of dicts,
  bounded size), reason bounds — fail closed on malformed/oversized/forged
  input. Raw-SQL-forged plan rows for never-existing versions are treated
  as absent (informational, never authoritative).
- Stored-plan execution does not bypass `_plan`'s status normalization
  (planner-status forge normalization applies to stored summaries the same
  way: statuses normalized to PENDING/SKIPPED before execution).

### 12. Adversarial cases (explicit analysis)

| Input | Planning influence | Strategy influence | Execution influence | Authority mutation | Disposition |
|---|---|---|---|---|---|
| forged episodes/reflections | yes (guidance, bounded) | yes (avoid/prefer via guidance) | no (live authz) | no | informational |
| forged consolidations | yes (ADR-014 Phase E lift) | no direct | no | no | informational |
| forged beliefs/preferences | yes (planning context) | yes (base rules) | no | no | informational |
| forged strategy outcomes | yes (preference layer, bounded 20, ≤300/200) | yes | no | no | informational + fail-closed validation |
| forged telemetry/events | no | no | no | no | informational; never read by repair |
| forged plan rows (versions/summaries) | no (planner ignores) | no | no | no | fail closed in diff/rollback (must exist + validate) |
| malformed/oversized records | bounded contexts | fail-closed validation | no | no | fail closed |
| stale cognitive state | yes (bounded, staleness-flagged) | yes | no | no | informational |
| cross-process races (re-adoption) | — | — | no | no | replay guard + UNIQUE + first-writer-wins (existing) |
| replay / crash windows | — | — | no | no | repair + idempotency (existing) |
| deletion/pruning | n/a | n/a | n/a | no | pruned versions fail closed in diff/rollback; never resurrected |

### 13. Acceptance criteria + proposed test phases

- **A — re-adoption seam:** `readopt_plan` creates a new immutable version
  (strategy/summary copied, reason `replan_rollback_v<N>`); previous latest
  superseded via the outcome funnel; replay-idempotent; fail closed on
  unknown/pruned/latest version, terminal-completed/cancelled goal,
  malformed summary, unknown strategy; authority tables byte-identical.
- **B — stored-plan execution:** engine builds the task from the stored
  summary (no planner invocation — deterministic); every step passes live
  authorization (tightened ActionSpec ⇒ DENY even for re-adopted plans);
  checkpoint/restart safe; `task.resumed` classification intact; memory
  recorded normally.
- **C — CLI + observability:** `goals diff` (deterministic, bounded,
  exit 1 fail-closed, `--json` without secrets/values); `goals rollback`
  (exit 0/1 semantics, output deterministic); `goals progress` read-only
  (byte-identical DB before/after; no events emitted); existing CLI output
  preserved.
- **D — adversarial/restart/pruning:** forged plan rows fail closed;
  pruned versions cannot be diffed/re-adopted; rollback after restart is
  idempotent; crash between version+outcome writes healed by existing
  repair; repeated rollback emits no duplicate events; scheduler/claim/
  DWRR/weights/reservations/ceilings byte-identical after rollback+execution.
- **E — demo + docs:** `scripts/demo_adr016_plan_management.py` (25–35
  deterministic checks, fixed timestamps) proving diff/rollback/stored-plan
  execution/restart/pruning/authority; this ADR + architecture.md updated.

### 14. Verification gauntlet (final)

Per-phase red→green→regression; final: full suite (baseline 1178/2),
ADR-014/015 focused suites, explicit ADR-029/030/031 re-runs (76/49/59),
all 20 demos + the new demo, ADR-016 focused suite + demo ×3 stable,
final diff audit (scheduler/authority untouched, no secrets/free-text
leakage), strict linear history, commit/push/PR update only after approval.

---

## Proposed scope (Phases A–E — IMPLEMENTED 2026-08-17)

The addendum completes ADR-016's last designed-but-deferred capability —
**plan diff & re-adoption (rollback) tooling** — as an informational,
immutable-history-only increment: a `GoalManager.readopt_plan` funnel, a
deterministic stored-plan execution path in the engine, a read-only
`goals diff` + mutating `goals rollback` CLI, a read-only `goals progress`
fix, adversarial/pruning/restart hardening, and a deterministic demo. No
new tables, no scheduler/policy/authorization changes, no planner
redesign; tests-first per phase, linear history preserved.

**Status: Phases A–E complete; every acceptance criterion in §13 above is
green (evidence below).** This closes the last item of the original
"Not built yet (by decision)" list (plan diff/rollback tooling).

---

## Phase A–E execution record (2026-08-17)

### A. Re-adoption seam — `GoalManager.readopt_plan` (IMPLEMENTED)

`readopt_plan(goal_id, from_version, reason=None)` (arion/cognition/goals.py):

- validates: goal exists; `from_version` exists in `plan_history` (fail
  closed if pruned/never existed); `from_version` is not the latest; goal
  not terminal-completed/cancelled (FAILED→ACTIVE remains legal via
  `transition`); strategy ∈ STRATEGY_NAMES; `plan_summary` is a bounded
  list of step dicts — malformed/oversized/cross-goal input raises
  `ValueError`;
- deep-copies the stored `(strategy, plan_summary)` and creates a NEW
  immutable version through the existing `record_plan_version` funnel with
  `reason = "replan_rollback_v<from_version>"` (bounded, provenance-carrying);
- the previously active version's strategy outcome becomes `superseded`
  (ADR-015 funnel) — exactly one outcome row and one `strategy.outcome`
  event per durable change;
- replay-guard idempotency: a repeated identical re-adoption returns the
  same version, emitting no duplicate outcome/event (cross-process-safe via
  INSERT OR IGNORE + replay guard);
- never touches scheduler authority tables (byte-identical, tested).

Tests: `tests/test_readopt_plan.py` (19).

### B. Stored-plan execution — planner-bypass fast path (IMPLEMENTED)

The engine's `_plan_for_goal` gains an additive fast path: when the goal's
LATEST plan version exists, has no implementing task, and has a non-empty
stored `plan_summary`, the task is created from the stored summary with
`create_task(goal, plan_version=latest)` — the planner is NOT invoked
(deterministic replay from durable `goal_plans`). Execution is otherwise
unchanged: every step passes the FULL live pipeline (authorization →
approval → mutation lock → FIFO → capability → verification) against
current ActionSpec metadata, and checkpoint/restart semantics are the
usual at-least-once task lifecycle. A successful stored-plan execution
creates no new plan version. Stored summaries pass the same status
normalization as planner output (statuses → PENDING/SKIPPED before
execution); planner-status forgery gets no new authority.

Tests: `tests/test_stored_plan_execution.py` (12).

### C. CLI + observability — diff / rollback / read-only progress (IMPLEMENTED)

- `GoalManager.diff_plans(goal_id, va, vb)` (arion/cognition/goals.py):
  deterministic, bounded, content-safe structural diff — strategy/reason
  deltas + step-level added/removed/kept by param KEY names only (never
  values/secrets); `va == vb` → explicit empty diff (valid, exit 0).
- `arion goals diff <goal_id> <va> <vb> [--json]` — read-only; exit 1
  fail-closed on unknown goal/version (incl. pruned versions) or invalid
  input; `--json` deterministic across repeated runs, secret/free-text-free.
- `arion goals rollback <goal_id> <version> [--reason R] [--json]` —
  mutating via `readopt_plan`; prints the new plan version; exit 1
  fail-closed (unknown/pruned/latest version, terminal-completed/cancelled
  goal, malformed input); `--json` stable schema, deterministic.
- `arion goals progress` — now READ-ONLY: the handler calls the public
  `GoalManager.peek_evaluate()` (below) instead of `evaluate()`; output
  schema unchanged and byte-identical across repeated calls.

Tests: `tests/test_plan_cli.py` (16).

### D. Hardening + the non-mutating evaluation seam — `peek_evaluate()` (IMPLEMENTED)

`GoalManager.peek_evaluate(goal_id) -> ProgressResult | None`
(arion/cognition/goals.py) is the **public non-mutating evaluation seam**:
it computes the same deterministic `ProgressResult` as `evaluate()`
(goal/task history/latest plan/`_relevant_world_changes` →
`progress_evaluator.evaluate`) WITHOUT persisting `progress_metadata` /
`last_evaluated_at` / `updated_at` and WITHOUT emitting `progress.evaluated`
/ `goal.evaluated`; returns `None` when the goal is missing. The
authoritative lifecycle (engine `run_goal`) keeps using the mutating
`evaluate()`; the read-only CLI surface uses `peek_evaluate()`. Progress,
diff, and rollback-preview remain purely observational — they can never
manufacture history.

Hardening tests (`tests/test_plan_hardening.py`, 14) prove: subprocess
rollback race (first-writer-wins + replay guard), crash repair idempotency
(close/reopen → wipe outcome+events → repair reconstructs from
authoritative `goal_state`+`goal_plans`, repeated repair emits nothing),
pruning (coupled outcome removal; diff/rollback fail closed; repair never
resurrects pruned history; surviving historical versions remain
repairable), forged plan/outcome telemetry rows (readable as
informational, powerless to manufacture history or block a real rollback;
scheduler/claim/DWRR/reservation/ceiling/approval/ownership/lease/recovery
tables byte-identical), restart execution of re-adopted plans, and
byte-boundary authority proofs (execution-created `scheduler_work` /
`checkpoints` rows excluded as legitimate execution artifacts, consistent
with the Phase C exclusions).

### E. Acceptance demo — `scripts/demo_adr016_plan_management.py` (IMPLEMENTED)

**35 deterministic checks in 8 sections** (fixed timeline
`T0 = 2026-01-01T00:00:00+00:00`; no wall clock in any assertion;
byte-identical output across 3 consecutive runs, exit 0):

1. **Plan history** — initial plan + replans create immutable versions
   v1–v3; historical rows byte-identical across further writes;
   deterministic structural diff; identical-version diff is an explicit
   empty diff; diff output bounded/content-safe.
2. **Plan re-adoption** — rollback creates exactly one new immutable
   version (v4) carrying the historical content; reason exactly
   `replan_rollback_v1`; ADR-015 supersede on the previously active
   version; exactly one outcome event; replay idempotent (no duplicate
   version/outcome/event).
3. **Stored-plan execution** — re-adopted plan executes deterministically
   from stored `plan_summary`; a planner spy proves the planner is never
   invoked; the task carries the re-adopted `plan_version`; success
   creates no new plan version; live capability/authz checks still apply
   (deny when `required_scope` is tightened).
4. **Restart/crash** — re-adopted plan survives close/reopen with reason
   intact; still executable after restart; missing strategy outcome
   repaired from authoritative plan history; repeated repair idempotent.
5. **Pruning** — historical plan + coupled outcome prunable; diff/rollback
   fail closed against pruned versions; repair never resurrects pruned
   history; surviving historical versions remain repairable.
6. **Read-only progress** — `peek_evaluate()` is a public non-mutating
   seam; `goals progress` byte-identical across repeated calls; no
   progress/audit/strategy-outcome mutation (only the one-time
   engine-startup capability-registration event, an observational
   exclusion consistent with earlier phases).
7. **Authority boundary** — forged `plan.versioned`/`strategy.outcome`
   telemetry rows cannot manufacture plan history or block a real
   rollback; scheduler config/weights/reservations/ceilings/ownership/
   leases/approvals/recoveries byte-identical after rollback+execution.
8. **CLI observability** — `goals diff --json`, `goals rollback --json`,
   `goals progress --json` deterministic across repeated runs with stable
   schemas, no free-text/secret leakage.

### Final verification (gauntlet, 2026-08-17)

- Full suite: **1239 passed, 2 skipped** (exit 0).
- ADR-016 addendum + goal-focused suites: green (×3 stable).
- ADR-014/015 focused suites: green; ADR-029 (76) / ADR-030 (49) /
  ADR-031 (59): green.
- All demos including the new plan-management demo: green; new demo ×3
  byte-identical.
- Diff audit: changes confined to `arion/cognition/goals.py`,
  `arion/interfaces/cli.py`, `arion/orchestration/engine.py`, the four new
  test files, this ADR, `docs/architecture.md` (2-line accuracy edit), and
  the demo — no scheduler/claim/DWRR/reservation/ceiling/approval/
  authority contamination, no secrets or free-text in JSON output.

### Remaining risks / design boundaries (unchanged authority model)

- `goal_state` + `goal_plans` remain the AUTHORITATIVE inputs to
  strategy-outcome repair; `strategy_outcomes`, episodes, reflections,
  consolidation, guidance, beliefs, planner output, and telemetry remain
  INFORMATIONAL. Rollback goes only through `readopt_plan`; diff/progress
  are purely observational. `peek_evaluate()` is the public non-mutating
  seam; the lifecycle engine keeps using mutating `evaluate()`.
- Cross-process rollback races are protected by INSERT OR IGNORE + replay
  guard (first-writer-wins); the probabilistic overlap window is pinned by
  the deterministic replay test.
- Raw-SQL-forged valid-shape plan rows remain readable in history and
  executable via the stored path THROUGH live authorization — same trust
  level as planner output (informational; every step re-authorized).
- `peek_evaluate()` internally still consults `_relevant_world_changes`,
  now encapsulated behind the public seam.
- No model-backed evaluation, no plan-approval workflow for rollbacks, no
  `audit_events.goal_id` index (G5, deferred by decision) — unchanged.
- All Phase 0/A–E changes remain uncommitted on `arena/01a00a1d-arion`
  (HEAD `769f6c0`, PR #1 open/mergeable); finalization = one commit of
  docs/tests/demo (or per-phase commits) + push + PR head update, subject
  to the next binding instruction.

---

## Addendum: durable goal-plan version allocation

**Status:** Approved & implemented (2026-08-18)

### Defect

Plan versioning was a best-effort read-decide-write sequence:

```
latest_plan() → compute latest.version + 1 → record_goal_plan()
```

and `record_goal_plan` used `INSERT OR REPLACE` keyed by
`(goal_id, plan_version)`. Two concurrent replanners could both read
version N, both allocate N+1, and the second writer would silently
replace the first. That corrupted the supposedly immutable goal-plan
lineage. The defect was reproduced with independent SQLite connections,
barrier-synchronized threads, and real subprocesses sharing the same
database.

### Invariant (now durable)

Goal plans are immutable and versioned. For each `goal_id`:

- allocation is append-only and monotonic at `MAX(plan_version) + 1`;
- an existing `(goal_id, plan_version)` row is never overwritten
  (plain `INSERT`; the primary key is the cross-process backstop);
- divergent concurrent replans all survive as distinct versions
  (`v1`, `v2`, `v3`, …);
- identical concurrent replans of an unimplemented latest plan
  converge: one caller creates, the other adopts the canonical row;
- an equivalent replan after a task already references/implements
  the latest plan creates a NEW version;
- only the creator of a new version emits `plan.versioned`;
- dense numbering is NOT required — pruning may leave gaps
  (before prune: 1, 2, 3; after prune: 2, 3; next claim: 4).
  Versions are never renumbered merely to make them dense.

### Design

`SQLiteCognitiveStore.claim_goal_plan(...) -> GoalPlanClaimResult`
is the ONE authoritative funnel. Inside one `BEGIN IMMEDIATE`
transaction it reads the current plan lineage, evaluates
replay/adoption semantics (identical latest + version not in the
caller-supplied `implemented_versions` snapshot), allocates
`MAX(plan_version) + 1`, and inserts with a plain `INSERT`.
`IntegrityError` is retried a bounded number of times so a
divergent loser takes the next free version and an identical
loser adopts the canonical latest plan.

`record_goal_plan` remains a low-level primitive for tests and
historical seeding. It now uses plain `INSERT` and refuses a
destructive overwrite (`IntegrityError` on a colliding version).
Production allocation routes through `claim_goal_plan`.

`GoalManager.record_plan_version` no longer owns non-atomic
version allocation. It supplies the implementing-task snapshot
and delegates to the store funnel. Informational follow-up
(predecessor `superseded` strategy outcome, `goal.strategy`
follow, `plan.versioned`) runs only when `created=True`. A crash
after the plan insert but before the outcome write does not
corrupt the plan lineage; the missing outcome is repairable
through the existing `repair_strategy_outcomes` mechanism.

`readopt_plan` continues to go through `record_plan_version`, so
re-adoption inherits the durable claim (replay of an identical
unimplemented rollback still adopts; a rollback after a task
implements the latest equivalent creates a new version).

---

## Addendum: durable goal-lifecycle compare-and-swap

**Status:** Approved & implemented (2026-08-19)

### Defect

Goal lifecycle writes were a best-effort read-mutate-write sequence:

```
load_goal() → validate / mutate → version += 1 → save_goal()
```

and `SQLiteStorage.save_goal` used `INSERT OR REPLACE` keyed by `id`.
Two independent processes or connections could both read version N,
mutate differently (pause vs set_blocked, two distinct blockers,
progress vs complete, strategy vs cancel), and the second writer
would silently replace the first. That is the same class of lost-update
already closed for beliefs (PR #5) and goal-plan versions (PR #6).

### Invariant (now durable)

The `goals` row is authoritative versioned state. For every goal:

- stale full-row writes never overwrite a newer committed lifecycle
  state;
- a successful lifecycle transition increments `goal.version` exactly
  once and emits `goal.state.changed` only after the CAS commits;
- a CAS miss reloads the canonical row and revalidates transition
  legality against the latest status (illegal after a race fails
  closed);
- concurrent additions of distinct blocker keys merge; the same key
  upserts in place and preserves the original `added_at`;
- a progress, strategy, or replan-reason patch reloads the latest
  row and applies only its informational fields, so it cannot
  resurrect a superseded lifecycle status;
- retries are bounded (`_GOAL_CAS_MAX_ATTEMPTS`) and fail closed
  under persistent contention.

A crash between a committed CAS and the subsequent event emission
remains an acknowledged limitation of the existing event architecture
(events are not in the same SQLite transaction as the goal row).

Plan-version allocation, belief supersession, and scheduler / lock /
approval authority are unchanged.

### Design

`SQLiteStorage.cas_goal(goal, expected_version) -> bool` is the
full-row primitive (`UPDATE … WHERE id=? AND version=?` inside
`BEGIN IMMEDIATE`; `goal.version` must be `expected_version + 1`).

Production writes go through `cas_goal_fields`, which updates only
the supplied columns under the same version predicate:

- lifecycle / blocker writes (`transition`, `set_blocked`,
  `clear_blocker` / `clear_blockers`, `recheck_blockers`) include
  `version = expected + 1` so a stale writer cannot replace a newer
  row;
- informational patches (`evaluate`, `strategy_for`,
  `set_replan_reason`, the strategy-follow in `record_plan_version`)
  omit `version`, so a progress or strategy update cannot bump the
  CAS token or overwrite status/blockers.

A miss returns False; the caller reloads and revalidates. `save_goal`
remains only for creation / explicit seeding. Engine lock-contention
upserts go through `set_blocked` (no raw `save_goal`).

`fail_goal` writes `last_replan_reason` in the same CAS as the FAILED
transition so the version increments once.

### Tests

`tests/test_goal_transition_invariant.py` covers independent-connection
pause vs blocked, distinct blocker merge, stale overwrite refusal,
CAS-miss reload/revalidate, illegal-after-race fail-closed, single
version increment (including `fail_goal`), progress/strategy patches
that cannot clobber lifecycle, exactly one event for one successful
competing transition, repeated contention, and a real-subprocess race.
