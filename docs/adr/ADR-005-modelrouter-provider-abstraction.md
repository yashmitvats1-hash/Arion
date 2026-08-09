# ADR-005 — ModelRouter / provider abstraction

- **Status:** Approved (2026-08-09)

## Context

Arion will use models for planning, reasoning and reflection — but no provider
should own the architecture. The agent loop must run (and be tested) without
any model at all.

## Decision

A minimal `ModelRouter` protocol exposes `generate(prompt)` and `planner()`.
Providers (OpenAI, Anthropic, local models) are implemented as router
adapters **only inside the intelligence layer**; nothing else may import a
provider SDK. The default `DeterministicRouter` uses a deterministic planner
and canned responses, so the full spine is functional offline.

The planner itself is behind the `Planner` protocol: the engine calls
`planner.plan(goal, task_id, registry)` and receives `PlanStep`s. A model-backed
planner can replace the deterministic one without touching orchestration.

## Consequences

- Zero LLM dependency for core tests (ADR-008).
- New providers = new router adapter; no changes outside `intelligence/`.
- Model routing decisions (which model for which task) stay centralized.

## Related

ADR-001 (intelligence layer), ADR-008 (LLM-independent testing).
