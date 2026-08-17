# ADR-013 — From Memory to Learning

- **Status:** Approved & implemented (2026-08-09)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

The memory milestone made Arion store and retrieve experience. This milestone
turns that into a genuine feedback loop: prior experience must CHANGE a
subsequent planning decision in a measurable way. The central invariant:

```
Memory informs planning.   Memory never authorizes execution.
```

## Decision

### 1. Model-backed reflection (`ModelReflector`)

The existing `Reflector` seam now has two implementations:
`DeterministicReflector` (default, offline) and `ModelReflector` (asks a
ModelRouter for a structured reflection). Both produce the SAME `Reflection`
dataclass. Model output is STRICTLY validated by
`reflection_schema.validate_reflection_dict` before it can be stored:
required fields/types, confidence in (low|medium|high), importance in [0,1],
and NO authority-bearing fields (`scope`, `permissions`, `actor`, `grant`,
`approve`, `authorization`, `capability_registration`, `resource_boundary`,
`allowed`, `policy`, ...). Malformed or adversarial model reflections raise
`ReflectionValidationError`; the engine emits `reflection.validation.failed`
and falls back to the deterministic reflector so the loop never breaks.

### 2. Memory-driven planning guidance (`MemoryGuidance`)

A reusable, deterministic mechanism converts retrieved episodes + reflections
into structured planning recommendations:

```
previous outcome + capability/action + failure category + recommendation
+ confidence + importance + relevance  ->  MemoryGuidance
```

Categories: `avoid` (prior denial/failure for a capability/action/resource),
`prefer` (prior success), `informational`. Episodes now record their DECLARED
resource values (from ActionSpec resource_param; still never arbitrary param
values) so guidance is resource-aware. `apply_guidance_to_steps` deterministically
re-targets a plan: a step targeting an `avoid` resource is substituted with a
`prefer` resource for the same capability/action, or dropped if none exists.
The planner (DeterministicPlanner and RealModelPlanner alike) receives the
context and applies it; guidance is INFORMATIONAL - authorization still
decides what may run.

### 3. Failure feedback loop

A failed/denied task -> episode -> reflection -> future retrieval -> guidance
-> a DIFFERENT plan. Verified by tests: the same goal that failed against a
denied resource later completes by choosing a safe resource, with the
behavioral difference asserted (Plan A != Plan B meaningfully, and the change
is attributable to guidance + provenance).

### 4. Consolidation (`MemoryConsolidator`)

Deterministic, explainable, NO embeddings: episodes are grouped by (outcome,
capability set, failure category) + goal-token similarity; repeated lessons are
merged into explicit `ConsolidationRecord`s (source episode ids, merged lesson,
count, importance). History is NEVER deleted. Importance decays with age
(`decayed_importance`, half-life configurable). Consolidation is idempotent
(same source set not re-consolidated), so repeated identical lessons cannot
pile up forever.

**Storage semantics (clarified):** consolidation preserves history and
therefore does NOT itself bound physical storage. Bounded memory growth is a
separate concern: the archival/pruning seam is designed in ADR-014
(`MemoryStore.prune` — intentionally raising NotImplementedError until the
archival policy is approved). Consolidation optimizes retrieval/repetition,
never storage size, and never deletes.

### 5. Provenance

`PlanningContext` keeps structured `provenance` (episode_ids, reflection_ids,
guidance_ids) so every context can answer "which memory influenced this
plan?". The audit event `planning.memory.influence` records those IDs, counts,
and guidance categories - never raw contents.

### 6. Poisoning defenses

Memory and reflections are treated as UNTRUSTED input. Adversarial content
("ignore policy", "grant root access", "register shell.exec", "approve future
writes", "act as user:admin") remains informational text at most: guidance has
no authority fields, reflection schema rejects authority fields, and every
authorization answer (scope, actor, resource boundary, capability existence,
risk, approval) comes from the CURRENT system authority - tested across the
full matrix.

## Consequences

- "Learning" is now demonstrable: prior experience changes planning decisions
  (acceptance-gate test).
- The system remains fully offline-testable (DeterministicReflector +
  DeterministicMemoryGuidance; only ModelReflector needs a model).
- Memory stays strictly informational; authority remains in PermissionPolicy.
- New observability events: `reflection.requested`, `reflection.validation.passed/
  failed`, `memory.consolidated`, `planning.memory.influence`.

## Related

ADR-009 (authorization authoritative), ADR-012 (memory), ADR-011 (intelligence
boundary), ADR-008 (offline testing).

---

## Integration addendum (2026-08-17) — lifecycle idempotency, catch-up learning, explicit state

Status: Approved & implemented (on top of the ADR-013 baseline; scheduler layer ADR-025…031 settled).

### Phase-0 assessment (what the baseline already had)

The ADR-013 loop was already fully implemented and tested before this addendum:

- episodes (`Episode`, outcomes completed|failed|denied|recovered, bounded metadata, param KEY names only);
- durable `SQLiteMemoryStore` (episodic_memories / reflections / consolidations, same DB file as core state);
- reflection (`DeterministicReflector` + `ModelReflector`, strict `reflection_schema` validation, authority-field rejection, deterministic fallback);
- deterministic consolidation (`MemoryConsolidator`, idempotent, never deletes);
- deterministic retrieval + bounded context (`MemoryRetriever`, `build_planning_context`, `ContextBudget`);
- guidance into planning (`MemoryGuidance` avoid/prefer/informational, `apply_guidance_to_steps`);
- engine hooks (`_record_memory` at every terminal path, `_build_planning_context` inside `_plan`);
- provenance + observability events + read-only CLI (`memory episodes|reflections|search|stats|consolidate`);
- poisoning defenses + the Plan-A≠Plan-B acceptance gate.

### Gaps closed by this addendum

1. **Episode idempotency (proven defect):** `_record_memory` is reachable from ~10 terminal paths (plus restart/resume/retry), and each invocation minted a NEW episode id, so recording the same terminal task twice produced duplicate episodes + duplicate reflections. Fixed by: engine-level task-keyed dedup (`get_episode_by_task`), a DB-level UNIQUE index on `episodic_memories.task_id` as the cross-process backstop, and an init-time duplicate cleanup (keep the newest row per task — a merge of a bug artifact, never archival pruning).
2. **Catch-up learning:** a crash between the terminal task save and `_record_memory` silently lost the experience. Added `engine.learn_from_terminal_tasks()` — an idempotent, restart-safe pass that records episodes (and reflections/consolidations) for every terminal task that has none, with a bounded `memory.learning.catchup` observability event.
3. **Explicit lifecycle state:** `Episode.lifecycle` ∈ {`recorded`, `reflected`, `consolidated`}, durable on the episode row and advanced transactionally by the engine after each stage. `recorded` is the retryable state (recovery after a mid-learning crash); reflection failure still falls back to the deterministic reflector, so a separate `failed` state is not required (consistent with the existing model).
4. **CLI diagnostic:** `arion memory inspect <episode_id>` (read-only, bounded, secret-free; `--json`).
5. **Demo:** `scripts/demo_adr013_learning_loop.py` (deterministic, offline).
6. **Tests:** lifecycle invariants (duplicate invocation, retry, empty/malformed input, concurrent completion), subprocess restart/crash recovery, adversarial learning-boundary tests, unrelated-memory exclusion at the engine level.

### Lifecycle (exact, after this addendum)

```text
execution outcome (durable task row)
   ↓  _record_memory / learn_from_terminal_tasks
durable episode (lifecycle=recorded; task-keyed, exactly one per task)
   ↓  reflect (configured reflector; deterministic fallback on failure)
reflection recorded + linked (lifecycle=reflected)
   ↓  consolidate (idempotent; never deletes)
consolidation records (lifecycle=consolidated)
   ↓  retrieval (bounded, deterministic) → PlanningContext → guidance → future planning
```

Learning is idempotent end-to-end: repeated passes never create duplicate
episodes, reflections, or consolidation records for the same experience.
Learning failure never rolls back execution, and execution never depends on
learning (memory remains strictly informational; the scheduler/authorization
contracts are untouched).
