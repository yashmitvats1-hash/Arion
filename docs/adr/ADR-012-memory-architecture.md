# ADR-012 — Episodic Memory + Reflection + Context (PROPOSED — DRAFT)

- **Status:** Proposed (draft for architect review) — NOT yet implemented
- **Date:** 2026-08-09
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

Arion currently completes tasks but remembers nothing. To become a persistent
general-purpose agent (rather than a chatbot), it must record significant task
experiences, reflect on them, and use prior experience when planning new
goals. This ADR proposes the design around the existing SQLite `Storage`
abstraction. **Memory is intelligence-side state; it must never authorize
anything.**

## Decision (proposal)

### 1. Episodic memory

A new `episodic_memories` table behind the `Storage` protocol. One row per
significant task experience, NOT per token/raw conversation:

- `memory_id`, `task_id`, `goal_id`
- `goal` (short description), `plan_summary` (intents + capabilities used)
- `outcome` (completed | failed | denied | recovered)
- `steps_count`, `actions` (JSON: capability/action/params-hash)
- `verification_results` (JSON), `failures` (JSON: error + typed category)
- `authorization_denials` (JSON: scope/resource/reason)
- `recovery` (whether resume/re-execution occurred)
- `timestamps` (created/updated), `relevant_context` (JSON tags:
  resource kinds, capabilities, risk levels)

Storage adds: `save_episodic_memory`, `list_episodic_memories` (with filters
and a deterministic full-text search), `load_episodic_memory`.

### 2. Reflection

After a meaningful completion/failure, the intelligence layer produces a
structured reflection (a `Reflection` dataclass, JSON-serializable):

- `what_happened`, `what_worked`, `what_failed`, `why`
- `what_should_be_remembered` (explicit, curated — not raw dumps)
- `confidence` (low|medium|high), `future_recommendation`
- `source_memory_id`, `created_at`

Reflection runs through a deterministic template planner by default
(LLM-independent, ADR-008); a model-backed reflector can be added behind the
same seam. Reflections are stored as rows in a `reflections` table.

### 3. Context retrieval (deterministic first)

Before planning, the orchestrator asks the memory layer:

```
retrieve_context(goal, top_k=N) -> list[(memory, reflection)]
```

Deterministic retrieval over SQLite (metadata filters + `LIKE`/FTS5 where
available): match on shared capabilities, resource kinds, outcome=denied/failed
for the same capability, recency. The model then receives
`current goal + relevant memory` (a compact digest), never the full database.
Embeddings/vector DB are a later optimization, explicitly deferred.

### 4. Memory authority (non-negotiable)

Memory is **read-only advice to intelligence**. A memory that says
"Arion is allowed to delete files" has zero effect on authorization:
`PermissionPolicy` remains the sole authority (ADR-009). No code path reads
memories during authorization; there is no "memory grants permission" API.
This is enforced by (a) the memory API living in the state/intelligence layer,
(b) a test asserting memories never influence `ResourcePolicy.decide`, and
(c) audit events continuing to record the real policy decision.

### 5. Integration points

- `ArionEngine` gains an optional `memory` hook: on task completion/failure it
  writes an episodic memory (observability events already exist); before
  planning it injects retrieved context into the planner call.
- `Planner` protocol stays unchanged in shape; the planner receives an
  optional `context` argument (goal + memory digest).
- Storage stays SQLite-first; new tables behind the same `Storage` protocol.

## Consequences

- Arion learns from outcomes without becoming a chatbot or an authorization
  mechanism.
- Everything remains deterministic-testable without an LLM.
- Adds schema surface to `Storage` (migrations needed when implemented).

## Out of scope (still deferred)

Voice, GUI, browser, shell, filesystem writes, RAG, vector DB, multi-agent
swarm, autonomous daemon, self-modifying code.

## Related

ADR-001 (state layer), ADR-003 (SQLite behind Storage), ADR-008
(LLM-independent testing), ADR-009 (authorization is authoritative),
ADR-011 (structured intelligence boundary).
