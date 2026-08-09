# Arion — Architecture (v0.1)

Arion is an autonomous personal computing system (JARVIS/FRIDAY-class). The
objective is **not** a chatbot with tools: it is an agentic system with
persistent state, memory, planning, perception, tool use, execution,
verification, learning, and long-running goal operation.

## Five layers (ADR-001)

```
┌─────────────────────────────────────────────────────────────┐
│  INTERFACES   cli (today) · voice · vision · gui · api      │
├─────────────────────────────────────────────────────────────┤
│  ORCHESTRATION  goal→plan→permission→execute→observe→verify │
│                 →checkpoint→complete/recover  (the loop)    │
├─────────────────────────────────────────────────────────────┤
│  INTELLIGENCE  planner · router · (future: reflection,      │
│                learning)   — never owns the loop            │
├─────────────────────────────────────────────────────────────┤
│  CAPABILITIES  filesystem.read (today) · registry · scopes  │
├─────────────────────────────────────────────────────────────┤
│  STATE         goals · tasks · checkpoints · audit events   │
│                (sqlite behind Storage)                      │
└─────────────────────────────────────────────────────────────┘
```

## The loop (ADR-004)

```
Goal → Task → Plan → Authorization → Capability → Observation
     → Verification → Checkpoint → Complete / Recover
```

- Tasks are persistent objects with status `created → planning → planned →
  running → awaiting_approval → completed | failed`.
- Every step: capability discovery (resolve ActionSpec metadata) →
  authorization (policy decision) → execute (retries per retry-safety) →
  observe → verify → checkpoint.
- Checkpoints are full task snapshots; resuming after a restart restores the
  latest checkpoint and continues where it left off.
- **The LLM never owns the loop** (ADR-005). Default planner/router are
  deterministic; the whole system runs and is tested with no model.

## Authorization model (ADR-009)

Every step is decided by a permission policy over
`Capability → Action → Resource → Parameters → Policy Decision`:

- The scope, risk and side effects come from the capability's declared
  `ActionSpec` — never from a scope the plan merely claims (spoofing cannot
  escalate).
- Outcomes: `ALLOW` / `DENY` / `REQUIRE_APPROVAL`.
- `REQUIRE_APPROVAL` routes through an `ApprovalHandler` seam; PENDING pauses
  the task (`awaiting_approval`) with a checkpoint, and a later `run_task`
  resumes the exact same step once approved. A future human approval interface
  implements this protocol.
- **Fail-closed resources:** resource-sensitive actions (ActionSpec declares
  `resource_kind`) are DENIED unless an explicit boundary is configured for
  that kind. Non-resource actions are unaffected. Boundaries are keyed by
  resource kind (extensible — future `url`, `queue:name`, ...) and enforced as
  pure string checks; the capability still enforces its own containment
  (sandbox root, symlinks). The two are independent layers.
- **Identity:** requests carry an `Actor` with a delegation chain
  (`user → agent → delegated agent`); policies can match the direct actor or
  any ancestor. Audit events record `actor` + `actor_chain`.
- Event kinds: `approval.requested`, `approval.granted`, `approval.denied`;
  `permission.checked` events include the decision (outcome, scope, resource,
  resource kind, risk, side effects) and the actor chain.

## Execution semantics (ADR-010)

- Steps are **at-least-once**: an interrupted step is re-executed on resume.
- Automatic retries are gated on `ActionSpec.retry_safe`; non-retry-safe
  actions fail immediately after one failed attempt.
- Action metadata: `required_scope`, `risk`, `side_effects`, `reversible`,
  `idempotent`, `retry_safe` — the substrate for safe side-effecting
  capabilities later.

## Vertical slice (implemented)

- `arion/state` — domain models + `SQLiteStorage` behind `Storage`.
- `arion/capabilities` — `CapabilityRegistry`, permission scopes,
  `filesystem.read` (read-only, sandboxed, symlink-safe, size-capped).
- `arion/intelligence` — `Planner` protocol + `DeterministicPlanner`,
  `ModelRouter` protocol + `DeterministicRouter`, `PlanSchema` (versioned,
  strict), `PlanValidator`, `RealModelPlanner`, `providers/` (OpenAI-compatible
  adapter behind ModelRouter).
- `arion/orchestration` — `authz.py` (authorization layer: requests, policy
  outcomes, `ResourcePolicy`, approval seam) + `ArionEngine` (the state
  machine: authorization gate, retries, verification policies, checkpointing,
  recovery).
- `arion/observability` — `AuditEvent` vocabulary, `EventLogger`, JSONL sink.
- `arion/interfaces` — CLI (`run`, `resume`, `status`, `tasks`, `events`,
  `capabilities`).
- `arion/memory` — `MemoryStore` protocol + `SQLiteMemoryStore` (episodic
  memories + reflections tables), `MemoryRetriever` (deterministic scoring +
  relevance gate), `DeterministicReflector`, `PlanningContext` (bounded
  digest), `build_episode_from_task` (structured summaries only).
- `arion/bootstrap.py` — composition root wiring all layers (memory on by
  default).
- `docs/adr/ADR-001..012` — approved architecture decisions.
- `tests/` — deterministic, LLM-independent tests.

## Persistent cognitive memory (ADR-012)

Memory is INFORMATIONAL — it never authorizes (`Memory ≠ Authority`):

- **Episodes:** one structured row per meaningful task experience (goal, plan
  summary with param KEY names only, actions, outcome, verification, failures
  with typed categories, authorization denials, recovery, tags, importance).
  No raw prompts/responses, no secrets, no transcript archives.
- **Reflections:** structured lessons (`what_happened/worked/failed/why`,
  `lesson`, `recommendation`, `confidence`, `importance`) from a
  `DeterministicReflector` (offline; `ModelReflector` later). A reflection can
  recommend but never executes anything.
- **Retrieval:** deterministic scoring (goal-token overlap, capability tags,
  outcome salience, importance, recency tie-break) with a relevance gate;
  bounded `PlanningContext` (max episodes/reflections/chars) handed to the
  planner — relevant memory, not the whole DB.
- **Lifecycle:** the engine records an episode + reflection at task
  completion/failure/denial and injects retrieved context before planning.
  Restart persistence is inherent (same SQLite file). Memory failure never
  breaks the task loop.
- **Events:** `memory.episode.recorded`, `memory.retrieval.completed`,
  `reflection.created`, `planning.context.created` (IDs/counts/tags only).

## Learning loop (ADR-013)

`Memory informs planning; memory never authorizes execution.`

- **ModelReflector** (behind the `Reflector` seam): produces the same
  structured `Reflection` as `DeterministicReflector`, strictly validated
  against a reflection schema that forbids authority-bearing fields
  (`scope`, `permissions`, `actor`, `grant`, `approve`, `authorization`,
  `capability_registration`, `resource_boundary`, ...). Malformed output is
  rejected; the engine falls back to the deterministic reflector.
- **MemoryGuidance**: deterministic conversion of retrieved episodes +
  reflections into structured recommendations (`avoid` / `prefer` /
  `informational`) with capability/action/resource scope. `apply_guidance_to_steps`
  re-targets plans away from known-failing resources. Episodes record their
  declared resource values (safe metadata, never arbitrary params).
- **Behavioral change is proven**: the same goal that was denied on a resource
  later completes by choosing a different resource, driven by retrieved
  experience (acceptance-gate test asserts Plan A != Plan B meaningfully).
- **Consolidation**: deterministic, explainable merging of repeated lessons
  into explicit records (never deletes history); importance decays with age;
  idempotent so lessons don't pile up.
- **Provenance**: `PlanningContext` exposes episode/reflection/guidance IDs;
  `planning.memory.influence` audits which memories influenced each plan.
- **Poisoning defenses**: adversarial memory/reflection content stays
  informational; all authorization answers come from the current policy.

## Cognitive State / World Model v1 (ADR-014)

Five cognitive layers, distinct from episodic memory:

- **episodic** (what happened) — `arion/memory` episodes/reflections
- **semantic** (what Arion believes) — `arion/cognition` Beliefs
- **procedural** (how to do things) — reflections + guidance + procedural beliefs
- **preference** (user-specific) — `arion/cognition` Preferences
- **environment** (current world state) — `arion/cognition` EnvironmentFacts

Every derived belief carries provenance (episode/reflection/guidance ids),
confidence, timestamps, and source (deterministic|model);
`DeterministicBeliefDeriver` is the reference path. `CognitiveState` facade +
`SQLiteCognitiveStore` (same DB file; restart-safe). **Informational only** —
beliefs can never authorize (tested).

- **Archival/pruning seam:** consolidation preserves history, so it does not
  bound storage; `MemoryStore.prune` is the designed (not-yet-implemented)
  seam for a future archival policy — memory is never deleted.
- **Strategy-level learning:** `apply_guidance_to_steps` is non-mutating,
  registry-aware (ActionSpec.resource_param — no hardcoded `path`), and can
  substitute actions (different decomposition) with verification adopted from
  the registry. `PlanTransformation` retains original + transformed plans with
  per-decision provenance; audited via `planning.memory.transformation`; each
  transformed step carries its provenance.
- CLI: `arion cognition beliefs|preferences|environment|snapshot|world|goals [--json]`.

## World State → Long-Horizon Goals (ADR-015)

The cognitive spine: `World State → Beliefs → Goals → Long-Horizon Planning →
Strategy Selection → Authorization → Execution → Observation → Verification →
Learning`.

- **World-state change detection:** `WorldStateMonitor` observes environment
  facts (versioned per key with `observed_at`); value changes bump the version
  and emit `world.state.changed`; `stale_facts()` flags facts that need
  re-checking so stale state cannot mislead planning.
- **Strategy selection:** `StrategySelector` (deterministic, explainable)
  maps goal + beliefs + environment + memory guidance to a `Strategy`
  (`blocked_missing_capability` / `defer_retry` / `avoid_known_failures` /
  `capability_verified` / `direct`) with provenance; emitted as
  `strategy.selected` and exposed in the planning context.
- **Long-horizon goals:** `GoalManager` records every plan version per goal
  (`goal_plans` table: version, strategy, summary) and reports per-goal task
  progress — goals span sessions with traceable history.
- **Cognitive hardening:** Belief/Preference/EnvironmentFact validation;
  belief updates are append-only + versioned (revisions supersede prior rows,
  history never rewritten); planning context carries `strategy` +
  `environment` (bounded) alongside memories.
- **Invariant (tested):** stale/poisoned beliefs, model instructions,
  preference manipulation, world-state facts, and memory-derived strategy
  changes never alter authorization - only `PermissionPolicy` decides.

## Structured intelligence boundary (ADR-011)

```
Goal → ModelRouter → Structured Plan → Schema Validation
     → Capability/Authorization Validation → Orchestrator
```

- **Plan schema (`v1.0`):** versioned, strict, serializable. Contains intent,
  ordered steps (capability, action, params, verification, `depends_on`).
  Authorization fields (`scope`, `resource_kind`, `resource_param`, `risk`,
  `permissions`, `actor`, `approve`, ...) are **forbidden in the schema** —
  the model cannot set them.
- **PlanValidator:** validates capability/action existence, `param_schema`
  conformance (required keys, types, no injected arguments), and resource
  parameters against the live registry. Never grants permissions.
- **ModelRouter:** provider-neutral (`generate`, `plan_structured`).
  OpenAI-compatible adapter (stdlib HTTP; OpenAI/Azure/Ollama/LiteLLM/vLLM)
  requests structured JSON, then parses + strictly validates into PlanSchema —
  invalid responses are rejected. Credentials via `ARION_LLM_*` env vars.
  `DeterministicRouter.plan_structured` runs the same structured path offline.
- **Capability discovery:** the model sees a catalog built live from
  `registry.capabilities_summary()` (actions, scopes, risk, side effects,
  resource kind/param, param_schema, verification expectations) — never a
  hardcoded tool list.
- **Planners:** `DeterministicPlanner`, `RealModelPlanner`, and future
  planners share one `Planner` protocol.
- **Invariant:** the model proposes; the system authorizes. Model `scope`
  values never override the registry; the model cannot change `resource_kind`,
  bypass a boundary, approve itself, change actor, grant permissions, or
  create capabilities.
- **Observability:** `planning.requested`, `model.response.received`,
  `plan.validation.passed/failed` — provider/model/latency/token metadata
  only; raw prompts/responses are never persisted.

## Security boundary (first slice)

No shell, no writes, no network. Filesystem access is read-only and confined
to the repository root; every action passes the permission gate (ADR-006).

## Not built yet (by decision)

wake-word, voice pipeline, GUI, vector DB, RAG, browser automation,
unrestricted shell, large tool catalogs, multi-agent infrastructure,
provider-specific code. The spine must be proven first.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/arion run "summarize this repository"
.venv/bin/arion tasks
.venv/bin/arion resume <task_id>     # survives process restarts
.venv/bin/arion events --task <task_id>
.venv/bin/python -m pytest
```

State lives in `arion_data/` (gitignored).
