# ADR-001 — Architectural boundaries

- **Status:** Approved (2026-08-09)
- **Deciders:** ChatGPT (architect/manager), Arena AI (engineering agent)

## Context

Arion must become an autonomous personal computing system, not a chatbot. The
critical risk is that every feature lands as "LLM prompt in -> text out", which
produces a demo, not a system. We need stable seams so the system can evolve
without rewrites.

## Decision

Arion is separated into five layers. Each layer owns a vocabulary and a public
interface; no layer reaches into another layer's internals:

1. **Intelligence** — reasoning, planning, reflection, decision-making,
   learning. Consumed via protocols (`Planner`, `ModelRouter`).
2. **Capabilities** — tools/APIs/shell/filesystem/GitHub/apps. Every
   capability implements `Capability` (name, actions, structured observations,
   declared permission scopes) and lives in a registry.
3. **State** — goals, tasks, plans, checkpoints, memories, preferences,
   environment. Persisted behind the `Storage` protocol.
4. **Orchestration** — task lifecycle, agent loop, permissions, retries,
   verification, recovery, completion. Owned by the engine; the LLM never owns
   the loop.
5. **Interfaces** — text, voice, vision, GUI, notifications, APIs. Adapters
   that translate external input into orchestration calls.

## Consequences

- Layers can evolve independently (new model, new tool, new interface).
- Every feature must be describable by which layer it belongs to; a feature
  that is "a conversation wrapper" is rejected.
- Cross-layer communication uses the protocols defined here, keeping the
  system testable and auditable.

## Related

ADR-004 (orchestration state machine), ADR-005 (ModelRouter),
ADR-006 (capability/permission model), ADR-007 (auditability).
