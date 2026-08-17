# ADR-014 — Cognitive State / World Model v1

- **Status:** Approved & implemented (2026-08-09)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

Episodic memory answers "what happened". Arion now needs a distinct cognitive
state that answers "what Arion believes", "how to do things", "what the user
prefers", and "what the world/system looks like" — with every derived belief
traceable to its source. This is the transition from "agent with memory"
toward a cognitive architecture.

## Decision

### 1. Five-layer cognitive model

| layer        | question               | store                                      | v1 status |
|--------------|------------------------|--------------------------------------------|-----------|
| episodic     | what happened          | `arion/memory` (Episode/Reflection)        | existing  |
| semantic     | what Arion believes    | `arion/cognition` (Belief)                 | implemented |
| procedural   | how to accomplish      | reflections + guidance + procedural beliefs| implemented |
| preference   | user behavior/prefs    | `arion/cognition` (Preference)             | implemented |
| environment  | current world state    | `arion/cognition` (EnvironmentFact)        | implemented |

`SQLiteCognitiveStore` (beliefs, preferences, environment_facts tables, same
DB file) is behind the `CognitiveState` facade, which also aggregates episodic
memory.

### 2. Beliefs carry full provenance

Every derived belief carries:

- `category` (semantic | procedural | preference | environment);
- `statement` (bounded, curated);
- `confidence` (0..1, mapped from reflection confidence/importance);
- `importance` (0..1);
- `provenance` (source episode_ids / reflection_ids / guidance_ids);
- `source` ("deterministic" | "model");
- `created_at` / `updated_at`.

`DeterministicBeliefDeriver` is the reference path: denied episodes → semantic
belief "X on R is not permitted by current policy"; preferred successes →
"X on R is achievable"; reflection lessons → procedural beliefs. Deduplicated
by (category, statement) keeping the highest confidence; refresh is idempotent.

### 3. INFORMATIONAL ONLY (unchanged invariant)

Beliefs, preferences, and environment facts are advice to intelligence. No
code path reads them during authorization; `PermissionPolicy` remains the sole
authority. `Belief.__post_init__` enforces schema; adversarial beliefs (e.g.
"filesystem:write is allowed") cannot change any policy answer (tested).

### 4. Archival / pruning seam

Consolidation PRESERVES history and therefore does **not** bound physical
storage. Bounded memory growth needs an archival/pruning policy. The seam
was defined but intentionally NOT implemented in the original milestone:

- `MemoryStore.prune(older_than, max_episodes)` existed in the protocol;
- `SQLiteMemoryStore.prune` raised `NotImplementedError` — memory was never
  deleted in that milestone.

**Status: IMPLEMENTED by the ArchivalPolicy addendum below** (approved
design; age-based, count-capped, importance-weighted, dry-run, bounded
batches, delete-only — no archive-to-sidecar in v1).

### 5. Strategy-level learning

Memory guidance now drives MATERIAL plan changes, not just resource swaps:
`apply_guidance_to_steps` (non-mutating, registry-aware) can substitute an
avoided action with a different action of the same capability (adopting the
new action's verification from ActionSpec), producing a different
decomposition. The original plan, transformed plan, and every decision with
provenance are retained (`PlanTransformation`; audited via
`planning.memory.transformation`; each transformed step carries its guidance
provenance in `PlanStep.guidance`).

## Consequences

- Arion can remember, derive bounded-confidence beliefs, choose a better
  strategy later, and explain the provenance of that decision — with the
  authorization layer still deciding independently.
- Everything remains offline-testable (deterministic deriver/guidance).
- New observability: `belief.derived`, `planning.memory.transformation`.

## Related

ADR-012 (episodic memory), ADR-013 (learning loop), ADR-009 (authorization
authoritative), ADR-008 (offline testing).

---

## Addendum (2026-08-17) — Phase-0 assessment & ArchivalPolicy design

Status: design approved; implementation follows tests-first on top of the
ADR-013 lifecycle addendum (`0c3188e`; scheduler layer ADR-025…031 settled).

### Phase-0 state verification

- Local HEAD = remote `arena/01a00a1d-arion` = PR #1 head = `0c3188e`
  (ADR-013 addendum); PR #1 OPEN, base `arena/019fe578-arion`, MERGEABLE;
  working tree clean.
- CI: **none configured** on the repository (no `.github/workflows`, 0
  GitHub Actions runs, empty check rollup) — verification is the local
  gauntlet.
- Baseline suite re-run: **1003 passed, 2 skipped**.

### What ADR-014 already implements (complete, tested)

1. **Five-layer cognitive model:** episodic (`arion/memory`), semantic
   (`Belief`), procedural (reflections + guidance + procedural beliefs),
   preference (`Preference`), environment (`EnvironmentFact`).
2. **`SQLiteCognitiveStore`** (same DB file, restart-safe): beliefs
   (append-only + versioned, `superseded_at`, active-list excludes
   superseded), preferences (unique key+user), environment_facts
   (versioned per key), goal_plans (immutable plan versions).
3. **`CognitiveState` facade:** `derive_and_store` (dedupe by
   category+statement; higher-confidence revision supersedes),
   `refresh_from_memory`, bounded `snapshot`, deterministic `retrieve`.
4. **`DeterministicBeliefDeriver`:** full provenance, confidence mapping,
   dedupe; `_derive_beliefs` runs per episode in the engine and emits
   `belief.derived`.
5. **`WorldStateMonitor`:** observe/change detection/staleness (versioned
   facts, `world.state.changed`).
6. **`StrategySelector`:** 5 deterministic strategies
   (blocked_missing_capability, defer_retry, avoid_known_failures,
   capability_verified, direct) with provenance and escalation.
7. **`GoalManager` (ADR-016):** authoritative goal state machine, plan
   versioning (immutable history, replay-safe), progress evaluator,
   max_replans bound, blocker lifecycle; wired through
   `run_goal`/`run_goals` with strategy escalation across plan versions.
8. **Engine wiring:** bootstrap composes CognitiveState + WorldStateMonitor
   + StrategySelector + GoalManager; `_plan` injects strategy, environment,
   and plan_history into `PlanningContext`; catch-up learning
   (ADR-013 addendum) also re-derives beliefs for recovered episodes.
9. **CLI:** `cognition beliefs|preferences|environment|snapshot [--refresh]|
   world|goals <id>`; `memory episodes|reflections|search|stats|consolidate|
   inspect` (read-only diagnostics).
10. **Tests (~90 across cognition/memory/strategy/goals):**
    test_cognition (5), test_cognition_authority (11), test_strategy_learning
    (2), test_guidance_authority (5), test_goal_manager (6),
    test_replanning (9), test_goal_lifecycle (8), test_goal_blocked_capability
    (4), test_cli_durable_goals (4), memory suites; demo
    `demo_adr016_goal_replan.py` (40 checks). Informational-only invariant
    proven across the full poisoning matrix.

### What is incomplete / merely scaffolded

1. ~~**Archival/pruning policy — the ONLY explicitly designed-but-
   unimplemented seam**~~ — **RESOLVED by the ArchivalPolicy addendum
   below** (Phases A–G implemented, tests-first): `SQLiteMemoryStore.prune`
   (age/count/importance, reflection coupling, consolidation protection,
   bounded batches, dry-run, idempotent), `prune_superseded_beliefs`
   (active never pruned, keep_versions per lineage),
   `prune_goal_plans` (latest never pruned), bounded `memory.pruned`
   events, 3 CLI commands with fail-closed validation, consolidation-fed
   beliefs, restart/crash recovery, and adversarial authority isolation.
   The obsolete seam test now asserts the implemented semantics.
2. **Consolidation-fed beliefs:** the deriver consumes episodes +
   reflections + guidance but NOT consolidation records; merged lessons are
   not lifted into the belief layer (procedural knowledge stops at the
   consolidation table).
3. **No prune CLI / observability** for either memory or cognition.
4. Minor: `refresh_from_memory` (facade) and `_derive_beliefs` (engine) are
   two entry points with slightly different supersede semantics (facade
   supersedes; engine skips); both idempotent, documented, no fix needed
   unless consolidation-fed beliefs unify them.

### What is authoritative (never touched by cognition)

`scheduler_work`, `scheduler_config`, `scheduler_goal_reservations/
ceilings/weights`, `scheduler_events`, `audit_events`, `tasks`, `goals`.
Cognition (beliefs/preferences/environment/strategy/plans) is informational
only; the scheduler contract (ADR-025…031) is settled and regression-locked.

### Where learning/cognitive state persists / is invoked

- Persisted: `episodic_memories`, `reflections`, `consolidations` (memory
  store); `beliefs`, `preferences`, `environment_facts`, `goal_plans`
  (cognitive store) — all in the shared SQLite DB.
- Invoked: `_record_memory` → `_derive_beliefs` + `_consolidate` at every
  terminal task path; `_build_planning_context` in `_plan` (retrieval +
  strategy + environment + plan_history); `learn_from_terminal_tasks`
  (catch-up); CLI read-only diagnostics.

### Missing lifecycle transitions (gap list for this addendum)

- **Storage bound:** no memory/cognitive pruning → unbounded growth.
- **Consolidation → belief lift:** consolidation records never reach the
  belief layer.
- **Observability of pruning:** no `memory.pruned`-style events.

---

## ArchivalPolicy design (the ADR-014 addendum increment)

### 1. Principle

Pruning is **explicit, operator-invoked, deterministic, bounded-batched,
and never an authority**. It follows the ADR-028 scheduler-event retention
pattern (explicit cutoff, bounded SELECT-then-DELETE batches, authority
tables untouched). No silent deletion, no wall-clock races, no secrets in
any pruning decision. Consolidation records are the permanent merged
summary; episode-level pruning preserves them.

### 2. Memory-layer pruning (`SQLiteMemoryStore.prune` — implement the seam)

Signature (protocol-compatible, extended):

```text
prune(older_than: str | None = None,
      max_episodes: int | None = None,
      batch_size: int = 500,
      keep_importance: float = 0.0,
      dry_run: bool = False) -> int
```

- **Age-based:** episodes with `created_at < older_than` are removed with
  their reflections (strict `<` on an explicit ISO cutoff; `Z` suffix
  accepted).
- **Count-capped:** when `max_episodes` is set, keep the NEWEST
  `max_episodes` episodes (by `created_at`); older ones are removed with
  their reflections. `max_episodes` must be a positive integer (fail
  closed otherwise).
- **Importance floor:** `keep_importance > 0` protects episodes with
  `importance >= keep_importance` from age-pruning (salient failures stay
  longer). Default `0.0` = no floor (protects nothing).
- **Batches:** `batch_size ∈ [1, 5000]` (fail closed outside); bounded
  SELECT-then-DELETE chunks (ADR-028 pattern); total removed returned;
  idempotent (second run removes 0).
- **Consolidations:** NEVER pruned by this method, and their SOURCE
  EPISODES are NOT protected — the consolidation row (the permanent merged
  summary) is preserved with its `source_episode_ids` provenance intact,
  and episode-level pruning is free to remove the sources. Implemented
  decision (test-pinned): no `keep_consolidated` parameter exists; the
  consolidation is the archival form and its provenance stays resolvable
  as ids. A separate `prune_consolidations(older_than)` is NOT provided in
  v1 — consolidations are bounded by lesson-group dedupe; document.
- **Authority:** touches ONLY `episodic_memories` + `reflections`; never
  scheduler/audit/task/goal tables (adversarially tested).
- `dry_run=True` returns the would-be count without deleting (CLI preview).

**Interaction with catch-up learning (documented, deterministic):** a
pruned episode for a still-terminal task may be re-derived by an explicit
later `learn_from_terminal_tasks()` (the durable task row remains
authoritative). Pruning is storage hygiene; re-learning is a separate
explicit operator action. Tests pin this exact behavior.

### 3. Cognitive-layer pruning (`SQLiteCognitiveStore`)

- `prune_superseded_beliefs(older_than: str | None = None,
  keep_versions: int = 1, batch_size=500, dry_run=False) -> int` — deletes
  SUPERSEDED belief rows (history) older than the cutoff, always keeping
  the newest `keep_versions` per belief lineage; ACTIVE beliefs are never
  pruned (a belief is only removed by being superseded, never by pruning).
- `prune_goal_plans(goal_id: str | None = None, keep_latest: int = 10,
  batch_size=500, dry_run=False) -> int` — keeps the newest `keep_latest`
  immutable plan versions per goal (replan history bounded); the latest
  version is never pruned (replay safety needs it).
- Preferences (bounded by unique key+user) and environment_facts (one row
  per key) are already bounded per key — documented, no prune needed;
  stale facts are flagged by `WorldStateMonitor`, never deleted.

### 4. Observability

- New bounded audit kind: `memory.pruned` (counts: episodes/reflections/
  beliefs/goal_plans, cutoff/limit used, dry_run flag; NO content).
- `memory prune` and `cognition prune` CLI commands emit it; existing
  telemetry is unchanged; pruning events are observational (ADR-028 rule).

### 5. CLI (read-mostly; prune is the one explicit mutating diagnostic)

```text
arion memory prune [--older-than TS] [--max-episodes N]
                   [--keep-importance F] [--dry-run] [--json]
arion cognition prune-superseded [--older-than TS] [--keep-versions N]
                                 [--dry-run] [--json]
arion cognition prune-plans [--goal G] [--keep-latest N]
                            [--dry-run] [--json]
```

Exit 0 on success (including dry-run); 1 on invalid args (fail closed).
`--dry-run` never mutates.

### 6. Consolidation-fed beliefs (secondary increment)

**Implemented** as the second option: a new optional
`include_consolidations=True` path in `CognitiveState.refresh_from_memory`
(flag default False preserves pre-addendum behavior exactly). Consolidation
records lift their merged lessons into procedural beliefs (bounded to 500
chars, deterministic confidence `round(min(1.0, 0.5 + 0.5*importance), 3)`,
complete provenance = `episode_ids` (sources) + `consolidation_ids`).
Storage follows the SAME versioning rule as `derive_and_store`
(`_persist_belief`): higher-confidence revisions supersede, equal/lower are
skipped — idempotent. Consolidations without a lesson are skipped. Beliefs
stay informational. (`DeterministicBeliefDeriver.derive` itself is
unchanged.)

### 7. Security boundary

- Forged/oversized pruning inputs fail closed (non-ISO timestamps,
  negative limits, `batch_size` out of range, `keep_importance` outside
  [0,1]).
- Pruning can never: delete scheduler authority rows, alter
  reservations/ceilings/weights/capacity, complete/claim/heartbeat/
  reclaim work, bypass approvals, or change execution semantics.
- Deleting all memories/reflections/beliefs does not change scheduler
  behavior (tested).
- Pruning is never consulted by admission or planning (planning reads
  current tables; pruned rows are simply absent).

### 8. Acceptance criteria (tests-first, per phase)

- **A memory prune:** age/count/importance semantics; reflection coupling;
  consolidation protection; batch bounds; idempotency; dry-run; restart
  persistence; authority isolation.
- **B cognitive prune:** superseded-belief pruning (active never pruned,
  keep_versions), goal-plan history bounding (latest never pruned),
  preferences/facts documented-bounded.
- **C observability:** `memory.pruned` events bounded + emitted;
  CLI renders.
- **D CLI:** prune + dry-run + fail-closed validation; no-mutation proof
  for dry-run.
- **E adversarial:** forged inputs fail closed; pruning cannot touch
  scheduler authority; delete-all memory leaves scheduler behavior
  identical; catch-up-after-prune semantics pinned.
- **F consolidation-fed beliefs:** merged lessons become procedural
  beliefs with provenance; idempotent; informational only.
- **G restart/crash:** prune survives reopen; catch-up after prune
  deterministic; concurrent prune+learn does not corrupt.
- **H demo:** `scripts/demo_adr014_cognitive_archival.py` — 25–35
  deterministic checks (bounded growth proof, protection semantics,
  authority isolation, CLI, observability, consolidation lift).
- **I docs:** this ADR + architecture.md + CLI help.

### 9. Verification

Per-phase red→green→regression; final: full suite (baseline 1003/2),
ADR-013…031 demos + ADR-014 demo, explicit ADR-029/030/031 focused
re-runs, memory/cognition suites, cross-process + adversarial + restart,
repeated stability runs. Scheduler contract must remain byte-identical
(additive-only diff to authority code).
