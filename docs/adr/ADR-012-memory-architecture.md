# ADR-012 — Episodic Memory + Reflection + Context

- **Status:** Approved & implemented (2026-08-09); was initially drafted as
  PROPOSED, implemented per the memory milestone, now accepted.
- **Date:** 2026-08-09
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

Arion currently completes tasks but remembers nothing. To become a persistent
general-purpose agent (rather than a chatbot), it must record significant task
experiences, reflect on them, and use prior experience when planning new
goals. This ADR proposes the design around the existing SQLite `Storage`
abstraction. **Memory is intelligence-side state; it must never authorize
anything.**

## Decision (implemented)

### 0. What was built

A new `arion/memory/` package behind the existing SQLite abstraction:

- `models.py` — `Episode`, `Reflection`, `EpisodeFilter`, `ContextBudget`,
  `PlanningContext` (with a privacy-safe, bounded `digest()`).
- `interface.py` — the `MemoryStore` protocol (`record_episode`, `get_episode`,
  `search_episodes`, `list_recent`, `record_reflection`, `get_reflection`,
  `list_recent_reflections`, `link_reflection`).
- `store.py` — `SQLiteMemoryStore` (same DB file as core state; restart-safe).
- `retrieval.py` — `MemoryRetriever` (deterministic scoring + relevance gate)
  and `build_planning_context` (bounded context policy).
- `reflector.py` — `Reflector` protocol + `DeterministicReflector` (offline).
- `lifecycle.py` — `build_episode_from_task` (structured summaries only;
  param key names, never values).
- Engine integration: `ArionEngine(memory=..., reflector=...)` records episodes
  + reflections at terminal states and injects `PlanningContext` into the
  planner before planning.

### 1. Episodic memory

A new `episodic_memories` table behind the `MemoryStore` protocol. One row per
significant task experience, NOT per token/raw conversation:

- `episode_id`, `task_id`, `goal_id`
- `goal` (short description, bounded 500 chars), `plan_summary` (JSON: step
  intents, capabilities, actions, statuses, param KEY names only)
- `actions` (JSON), `outcome` (completed | failed | denied | recovered)
- `verification` (JSON: passed/failed step indices)
- `failures` (JSON: step, capability, action, error text bounded to 500 chars,
  typed category), `authorization` (JSON: denials scope/resource/reason,
  approvals_required)
- `recovery` (JSON: resumed), `tags` (JSON: capabilities, outcome, categories)
- `importance` (0..1), `reflection_id`, `created_at`, `updated_at`

Validation: `Episode.__post_init__` rejects unknown outcomes, empty ids, and
out-of-range importance (malformed records cannot be stored).

### 2. Reflection

After a meaningful completion/failure, the engine produces a structured
reflection (a `Reflection` dataclass, JSON-serializable):

- `reflection_id`, `episode_id`
- `what_happened`, `what_worked`, `what_failed`, `why`
- `lesson`, `recommendation` (curated, not raw dumps)
- `confidence` (low|medium|high), `importance`, `created_at`

`DeterministicReflector` (template-based, LLM-independent per ADR-008) handles
success/failure/denial/recovery variants; a `ModelReflector` can implement the
same `Reflector` protocol later. Reflections are stored in a `reflections`
table and linked to their episode.

### 3. Context retrieval (deterministic first)

Before planning, the orchestrator builds a bounded `PlanningContext`:

```
build_planning_context(retriever, goal, ContextBudget(max_episodes, max_reflections, max_chars))
```

`MemoryRetriever.retrieve` scores episodes deterministically: goal-token
overlap, shared capability tags, outcome salience (failed/denied/recovered),
importance, recency tie-break — with a relevance GATE (an episode must share a
goal token or capability to be retrieved at all; unrelated episodes are never
included just for being recent). The model then receives
`current goal + relevant memory` (a compact, character-bounded digest), never
the full database. Embeddings/vector DB are a later optimization, explicitly
deferred.

### 4. Memory authority (non-negotiable)

Memory is **read-only advice to intelligence**. A memory that says
"Arion is allowed to delete files" has zero effect on authorization:
`PermissionPolicy` remains the sole authority (ADR-009). No code path reads
memories during authorization; there is no "memory grants permission" API.
This is enforced by (a) the memory API living in the state/intelligence layer,
(b) a test asserting memories never influence `ResourcePolicy.decide`, and
(c) audit events continuing to record the real policy decision.

### 5. Integration points

- `ArionEngine` gains optional `memory` (MemoryStore) and `reflector`
  (Reflector) hooks. On terminal states (completed/failed/denied) it writes an
  episodic memory + reflection (best-effort; memory failure never changes task
  outcome). Before planning it builds a `PlanningContext` and passes it to
  `planner.plan(..., context=ctx)`; `RealModelPlanner` forwards
  `ctx.digest()` into the ModelRouter's context.
- New observability events: `memory.episode.recorded`, `memory.retrieval.completed`,
  `reflection.created`, `planning.context.created` — IDs/counts/tags only,
  never full memory contents.
- `bootstrap.build_engine(..., memory=True)` wires `SQLiteMemoryStore` +
  `DeterministicReflector` by default.

## Consequences

- Arion learns from outcomes without becoming a chatbot or an authorization
  mechanism.
- Everything remains deterministic-testable without an LLM.
- Memory is a separate bounded component: the `Storage` protocol is untouched;
  `MemoryStore` is its own protocol (Postgres/vector/encrypted later).
- Restart persistence is inherent: episodes/reflections live in the same DB
  file as core state.

## Out of scope (still deferred)

Voice, GUI, browser, shell, filesystem writes, RAG, vector DB, multi-agent
swarm, autonomous daemon, self-modifying code.

## Related

ADR-001 (state layer), ADR-003 (SQLite behind Storage), ADR-008
(LLM-independent testing), ADR-009 (authorization is authoritative),
ADR-011 (structured intelligence boundary).
