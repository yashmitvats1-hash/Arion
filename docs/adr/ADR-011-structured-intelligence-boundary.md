# ADR-011 — Structured intelligence boundary

- **Status:** Approved (2026-08-09)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

Arion needs model intelligence without becoming a chatbot. A free-text goal
fed to an LLM that returns "do anything" is unacceptable. The previous
milestones made the action substrate safe (authorization, fail-closed
resources, identity). This milestone establishes the intelligence-to-action
boundary:

```
Goal → ModelRouter → Structured Plan → Schema Validation
     → Capability/Authorization Validation → Orchestrator
```

**The model proposes. The system remains the authority.**

## Decision

### 1. Plan schema (versioned, strict, serializable)

`PlanSchema` (`arion/intelligence/plan_schema.py`) is the only shape a plan
may take across the boundary. It carries intent, ordered steps (capability,
action, params, verification requirements, optional `depends_on`), and the
schema version (`1.0`). Parsing is strict:

- unknown top-level/step fields are rejected;
- **authorization fields are forbidden in the schema**: `scope`,
  `resource_kind`, `resource_param`, `risk`, `side_effects`, `idempotent`,
  `retry_safe`, `permissions`, `actor`, `approve`, `grant`, ... - the model
  cannot set them, and reserved names cannot be smuggled inside `params`;
- verification specifications must use the known policies
  (`non_empty`, `schema_keys`) with valid args;
- `depends_on` may only reference earlier steps (valid ordering);
- the schema serializes to/from JSON (persistable).

### 2. PlanValidator

`PlanValidator` (`arion/intelligence/plan_validator.py`) sits between
intelligence and orchestration. Against the **live capability registry** it
checks: capability exists; action exists; parameters satisfy the action's
declared `param_schema` (required keys, types, no unknown/injected
arguments); resource-bearing actions carry their declared resource parameter
and the resource is present and well-formed. It resolves each step's scope
and resource metadata from the registry's `ActionSpec` - the plan cannot
redefine the resource kind. It NEVER grants permissions; boundary enforcement
belongs exclusively to `PermissionPolicy` during authorization.

### 3. ModelRouter provider abstraction

`ModelRouter` stays the stable, provider-neutral seam: `generate` (free-form)
and `plan_structured(goal, capabilities, context) -> PlanSchema`. The
OpenAI-compatible adapter (`arion/intelligence/providers/openai_compat.py`)
works against any `/chat/completions` endpoint (OpenAI, Azure, Ollama,
LiteLLM, vLLM, ...) using only the stdlib, requests JSON-structured output,
then **parses + strictly validates** the response into a `PlanSchema`,
rejecting anything invalid (the provider cannot silently degrade to prose).
Credentials come from the environment (`ARION_LLM_API_KEY`,
`ARION_LLM_BASE_URL`, `ARION_LLM_MODEL`) - never from the repo. The HTTP
transport is injectable so tests need no credentials or network.
`DeterministicRouter.plan_structured` implements the same structured path
offline, so the whole pipeline is testable with zero LLM access.

### 4. Capability discovery

The model never receives a hardcoded tool list. The planner builds the catalog
from `registry.capabilities_summary()`, which includes per action: name,
description, required scope, risk, side effects, reversibility, idempotency,
retry safety, resource kind/parameter, `param_schema` and default verification
expectations. This is the first real intelligence ↔ capability-substrate
connection - without letting intelligence bypass authorization.

### 5. Planners through one abstraction

`DeterministicPlanner`, `RealModelPlanner`, and future planners all implement
the same `Planner` protocol. `RealModelPlanner` runs
`Goal → ModelRouter.plan_structured → PlanSchema → PlanValidator → PlanSteps`.
The deterministic path remains fully functional.

### 6. Security invariants (non-negotiable)

- A model `scope` value never overrides the registry's `ActionSpec` scope.
- A model cannot change `resource_kind`, bypass a boundary, approve itself,
  change actor identity, grant permissions, or create an unregistered
  capability. Each is rejected at the schema, validator, or authorization
  gate, and audited.
- Model output never authorizes itself.

### 7. Observability

New event kinds: `planning.requested`, `model.response.received`,
`plan.validation.passed`, `plan.validation.failed`. Provider/model/latency/
token metadata is recorded where safely available; **raw prompts and raw
responses are never persisted** (they may contain private data).

## Consequences

- External model intelligence can be added without surrendering authority.
- Malformed/adversarial model plans fail the task gracefully before any
  capability executes, with a full audit trail.
- Adding a new provider = one adapter behind `ModelRouter`; no changes to
  orchestration, state, capabilities, authorization, or interfaces.

## Not built (by decision)

Voice, wake word, GUI, browser, shell, filesystem writes, RAG, vector DB,
multi-agent swarm, autonomous daemon, self-modifying code. This milestone
establishes the intelligence-to-action boundary only. "AI integration
complete" is not claimed.

## Related

ADR-001 (layers), ADR-004 (orchestration), ADR-005 (ModelRouter),
ADR-006/009 (capability + authorization), ADR-008 (LLM-independent tests),
ADR-010 (execution semantics).
