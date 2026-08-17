# ADR-004 — Explicit orchestration state machine

- **Status:** Approved (2026-08-09)

## Context

A chatbot treats each message as an independent prompt. Arion must treat work
as a long-lived object with a lifecycle: create, plan, execute, observe,
verify, checkpoint, complete or recover. The loop must be explicit, resumable
and testable without a model.

## Decision

The orchestration layer owns an **explicit state machine** for tasks:

```
CREATED -> PLANNING -> PLANNED -> RUNNING -> COMPLETED | FAILED
```

Within RUNNING, each step follows:

```
start -> permission check -> capability discovery -> execute (with retries)
      -> observation recorded -> verification -> checkpoint -> next step
```

Task state is serialized (dataclass -> dict -> JSON) and every state change is
checkpointed to storage. Resuming a task restores the latest checkpoint and
continues from that step. **The LLM never drives transitions** — it is only
consulted through `ModelRouter` where intelligence is needed.

## Consequences

- Crash/restart semantics are inherent: work is never lost mid-task.
- Verification is a first-class step, not an afterthought.
- Audit events mark every transition, giving full replayability.
- A human can approve/deny at the permission gate without new machinery.

## Related

ADR-001 (orchestration layer), ADR-003 (checkpoints persisted),
ADR-007 (transitions are audited).
