# ADR-032 — Runtime Lifecycle Ownership Consolidation

- **Status:** Approved and implemented (2026-08-19)
- **Scope:** Focused architectural consolidation after ADR-031

## Context and verified architecture map

The consolidation request that prompted this ADR described a different, much
larger AIOS checkout. This repository is Arion and the code is the source of
truth. The full baseline suite passed with 1,297 tests and 2 optional smoke
tests skipped before this change.

### Capabilities and integrations

Arion has no `ToolManager`, `IntegrationManager`, or separate integrations
layer. The actual ownership chain is:

| Concern | Owner | Concrete code |
| --- | --- | --- |
| Composition and built-in registration | composition root | `arion/bootstrap.py` |
| Capability registration and discovery | `CapabilityRegistry` | `arion/capabilities/registry.py` |
| Action metadata | capability-owned `ActionSpec` | `arion/capabilities/*.py` |
| Plan-time catalog and validation | intelligence layer | `planner.py`, `plan_validator.py` |
| Authorization and resource permissions | orchestration policy | `orchestration/authz.py` |
| Execution, retries, verification, recovery | `ArionEngine` | `orchestration/engine.py` |
| Capability-specific containment | each capability | filesystem path and HTTP origin checks |
| Worker lifecycle and capacity | scheduler plus durable registry | `scheduler.py`, `state/store.py` |

A capability is an executable, self-describing system operation. HTTP and Git
happen to reach external systems, but are capabilities under the same
registration, authorization, and execution path; they are not independently
managed "integrations." Introducing a second registry would create the overlap
that this consolidation is intended to prevent.

### Operational state, memory, and cognition

- `arion/state` owns authoritative operational records: goals, tasks, plans,
  checkpoints, approvals, recovery fences, locks, scheduler ownership, and
  audit persistence.
- `arion/memory` owns long-term episodic experiences, reflections,
  consolidations, deterministic retrieval, guidance, and bounded planning
  context. It is informational and cannot authorize execution.
- `arion/cognition` owns derived semantic/procedural beliefs, preferences,
  environment facts, strategies, and goal-management projections. Its
  provenance comes from operational state and memory, but it is also
  informational.
- Short-lived execution state is held by `Task`/`PlanStep` and bounded
  `PlanningContext`; Arion does not currently implement a separate working
  memory service.
- There is no knowledge package, vector store, or knowledge graph. Adding one
  is deferred until its authority, provenance, and retrieval contract can be
  distinguished from the existing memory and cognition stores.

### Event architecture

`AuditEvent` is a typed envelope with a closed `EVENT_KINDS` vocabulary.
`EventLogger` synchronously fans events out to typed `EventSink`s; SQLite and
JSONL provide durable audit output. Event `detail` remains
`dict[str, Any]`, and the logger is not a subscription bus. It has no delivery
isolation, trace/span context, replay cursor, broker transport, or distributed
execution semantics.

A repository-wide typed-payload migration is deliberately not part of this
ADR. The current typed envelope is already materially safer than a raw-dict
EventBus, while changing nearly one hundred payload shapes would be broad and
high-risk. A later ADR should add versioned payload contracts incrementally at
specific external boundaries.

### Security and execution boundaries

`ResourcePolicy` is the central decision point for identity, scope, risk, and
resource boundaries. Action security metadata comes from the live registry,
not a model-produced plan. Filesystem capabilities independently enforce
resolved-path containment; HTTP independently enforces scheme, credentials,
size, timeout, and redirect-origin constraints. The default composition denies
network and filesystem mutation. Shell and terminal execution are not built.
Provider credentials are supplied through configuration and excluded from
normal event metadata.

### Lifecycle problem

Construction is eager and opens multiple independently owned SQLite
connections (`SQLiteStorage`, `SQLiteMemoryStore`, and
`SQLiteCognitiveStore`). Existing termination APIs are inconsistent:
`ArionEngine.shutdown`, `StepScheduler.shutdown`, and store `close` methods.
Before this ADR, engine shutdown stopped worker ownership but did not close any
bootstrap-created stores. Callers manually closed whichever resources they
could reach; the cognitive store is nested behind `CognitiveState`. There was
no typed lifecycle state or health result, and partial bootstrap failures could
leak resources.

## Decision

Introduce a small, dependency-free runtime lifecycle contract in
`arion.runtime.lifecycle`:

- `Lifecycle` describes the stable runtime boundary: `shutdown()` and
  `health()`.
- `LifecycleState`, `HealthStatus`, `ComponentHealth`, and `HealthReport`
  provide structured lifecycle and health results.
- `ResourceLifecycle` owns only resources explicitly registered by the
  composition root, closes them exactly once in reverse construction order,
  continues cleanup after an individual close failure, and records failures.

`build_engine` registers the stores it creates as owned resources and transfers
that lifecycle to `ArionEngine`. Engine shutdown keeps its existing ordering:
first stop/drain the scheduler and unregister durable worker ownership, then
close bootstrap-owned cognition, memory, and state stores. Shutdown is
idempotent. `close()` and context-manager methods are compatibility/convenience
adapters over `shutdown()`.

Dependencies injected directly into `ArionEngine` are **borrowed by default**.
They are not closed unless their creator explicitly supplies a
`ResourceLifecycle`. This preserves existing tests and embedding use cases
where a caller manages a shared store.

Arion initializes eagerly today, so this ADR does not invent asynchronous
`initialize` or `start` phases. The typed state model leaves room for those
phases when a real long-running service requires them.

## Migration and compatibility

- `build_engine(...) -> ArionEngine` remains unchanged.
- Existing `engine.shutdown()` remains valid and now performs complete cleanup
  only for resources that `build_engine` owns.
- Existing manually assembled engines retain borrowed-resource behavior.
- Existing direct store `close()` calls remain valid.
- New code should prefer `with build_engine(...) as engine:` or a `try/finally`
  calling `engine.close()`.

The principal compatibility risk is code that calls `build_engine`, shuts the
engine down, and then expects its owned database connections to remain usable.
That behavior contradicted composition-root ownership and is intentionally
corrected. The database remains durable; a new engine/store may reopen it.

## Test strategy

Tests cover:

1. reverse-order, exactly-once resource cleanup;
2. cleanup continuation and typed unhealthy status after a close failure;
3. duplicate ownership rejection;
4. bootstrap-owned component reporting and complete engine shutdown;
5. context-manager and idempotent shutdown behavior;
6. preservation of borrowed dependency ownership for manually assembled
   engines;
7. the complete deterministic regression suite.

## Explicit deferrals

- A second tool/integration registry or manager.
- Knowledge graph, vector provider, and distributed retrieval architecture.
- Versioned typed payloads for every audit event.
- Asynchronous/distributed event delivery and tracing propagation.
- Async database drivers or moving SQLite work to an executor.
- Provider error-body credential sanitization hardening.
- Terminal, shell, browser, or host-isolation execution.
- Broad package renames or a rewrite of eager construction.

## Verification

- Before implementation: **1,297 passed, 2 skipped**.
- ADR-032 focused lifecycle tests: **7 passed**.
- Focused lifecycle, scheduler, memory, and CLI regressions: **182 passed**
  across the focused runs.
- Complete suite after implementation: **1,304 passed, 2 skipped**.
