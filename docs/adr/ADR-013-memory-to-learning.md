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
