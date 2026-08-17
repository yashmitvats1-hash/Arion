# ADR-007 — Auditability and observability

- **Status:** Approved (2026-08-09)

## Context

An autonomous agent that acts on its own must be auditable: every meaningful
transition, permission decision, capability call, verification and failure
must be recorded as structured data that can be replayed and reasoned about.

## Decision

- A canonical event vocabulary (`EVENT_KINDS`) covers the full lifecycle:
  goal.submitted, task.created, plan.produced, permission.checked/denied,
  capability.discovered/executed, observation.recorded,
  verification.passed/failed, step.retrying, checkpoint.persisted,
  task.completed/failed, error.
- Events are `AuditEvent`s (kind, task_id, step_id, actor, success, detail)
  emitted through an `EventLogger` fan-out: persisted to SQLite (via
  `Storage`) and mirrored to a JSONL file for log tooling.
- Every event is retrievable per task or globally, in order.

## Consequences

- Full replayability: "what did Arion do, in what order, and did it succeed?"
- Permission denials and verification failures are visible, enabling
  human oversight and later learning from failures.
- Observability is part of the spine, not an add-on.

## Related

ADR-001 (observability concerns), ADR-004 (transitions are the events).
