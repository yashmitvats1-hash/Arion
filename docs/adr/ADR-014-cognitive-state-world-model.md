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
storage. Bounded memory growth needs a future archival/pruning policy. The
seam is defined but intentionally NOT implemented:

- `MemoryStore.prune(older_than, max_episodes)` exists in the protocol;
- `SQLiteMemoryStore.prune` raises `NotImplementedError` — memory is never
  deleted in this milestone.

A future `ArchivalPolicy` (age-based, count-capped, importance-weighted,
archive-to-sidecar vs delete) will be designed and approved before enabling.

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
