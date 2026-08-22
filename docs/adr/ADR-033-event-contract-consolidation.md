# ADR-033 — Event Contract Consolidation

- **Status:** Approved and implemented (2026-08-19)
- **Scope:** One backward-compatible typed payload pattern and explicit sink failure policy

## Context

Arion already has a useful audit foundation: `AuditEvent` is a structured
envelope, `EVENT_KINDS` is a closed vocabulary, general audit history is
persisted in SQLite, scheduler telemetry is committed transactionally in a
versioned table, and an optional JSONL mirror is available. The full baseline
before this ADR was **1,304 passed, 2 skipped**.

The remaining weakness is not the absence of a distributed event bus. It is
that the envelope's `detail` boundary is an undocumented `dict[str, Any]` and
that sink failure behavior is implicit.

## Verified architecture map

### Producers

The major producer groups are:

| Producer | Event responsibility |
| --- | --- |
| `ArionEngine` | task, authorization, approval, execution, recovery, memory, and learning lifecycle |
| `GoalManager` | goal state, plan versions, progress, and strategy outcomes |
| `WorldStateMonitor` | environment change observations |
| model planner/provider adapters | planning validation and bounded provider metadata |
| model reflector | reflection request/validation lifecycle |
| memory/cognition CLI commands | explicit pruning audit records |
| `SQLiteStorage` scheduler transitions | transactional scheduler telemetry |

Static inspection found 117 literal emission sites across 73 event kinds, plus
dynamic helper sites. Producers consistently use string keys, but payload
shape is convention rather than contract. Some kinds intentionally have more
than one shape (`error`, `task.failed`, lock and approval events).

One shape is demonstrably stable: an authorization policy decision always
contains:

`outcome`, `reason`, `scope`, `resource`, `resource_kind`, `risk`, and
`side_effects`.

`permission.checked` adds bounded context such as actor identity, delegation
chain, declared scope, parameter names, and lock-wait revalidation. Existing
code currently emits the complete parameter mapping. That can persist file
contents or another sensitive value even though consumers use no parameter
values. New typed authorization details therefore retain parameter **names**
as `param_keys`; legacy events containing `params` remain readable.

Scheduler telemetry is also stable, but it already has a version-1 allowlist,
bounded sanitization, tests, and transactional persistence in ADR-028. This ADR
does not move or redesign that authority-sensitive path.

### Consumers

- Memory lifecycle classification reads authorization `scope`, `resource`, and
  `reason`; resume `mid_execution`; and error `category`.
- Scheduler status and watch commands consume the documented scheduler detail
  keys.
- The general events CLI renders the envelope and detail without imposing a
  schema.
- Other production consumers primarily inspect event kind and ordering.

No production consumer reads authorization parameter values.

### Persistence and compatibility

General events are stored in `audit_events.detail` as JSON and reconstructed by
`AuditEvent.from_row`. Scheduler events are stored in `scheduler_events` with
indexed columns, bounded detail JSON, and `schema_version = 1`, then adapted
back to `AuditEvent`. JSONL contains the complete `AuditEvent.to_dict()` object;
there is currently no repository JSONL reader.

The database representation remains a JSON object. Typed payloads are
normalized to that same dictionary before they reach any sink. No table or row
migration is required. Legacy dictionary payloads, including partial
pre-contract authorization details, remain accepted and readable. JSONL field
names and values remain compatible; a `from_dict` adapter formalizes reading
both current and older lines with optional fields absent.

### Fan-out failure mode

`EventLogger` synchronously invokes sinks in registration order and currently
propagates every sink exception. Bootstrap registers SQLite first and JSONL
second. Consequently, an optional JSONL filesystem failure can fail an engine
operation even after the durable SQLite audit row was committed. Other
producers locally swallow observability failures, so the effective policy is
inconsistent.

SQLite audit persistence is required and must continue to fail closed. JSONL is
a mirror and should be best effort. Required sinks retain fail-fast behavior;
best-effort failures are recorded as bounded diagnostic metadata and delivery
continues to later sinks.

## Decision

### 1. Generic compatible detail boundary

Add an `EventDetails` protocol with a unique `to_event_detail()` adapter and a
single `normalize_event_detail()` function. `AuditEvent` normalizes at
construction time:

- existing mappings continue to work and are copied;
- typed payload objects convert to ordinary dictionaries;
- keys must be strings;
- values must be JSON serializable under the same rules used by SQLite/JSONL;
- invalid producer payloads fail at the event boundary rather than at an
  arbitrary sink.

After construction, `AuditEvent.detail` remains `dict[str, Any]`. Persistence,
CLI consumers, and old dictionary producers therefore see the same API.

### 2. First typed payload: authorization decisions

Add immutable `AuthorizationEventDetails` with schema version 1, the seven
stable policy-decision fields, and the known optional permission-check context.
Migrate only the central `ArionEngine` decision emission sites:

- `permission.checked` (initial and post-lock-wait revalidation),
- `permission.denied`,
- `approval.requested`,
- immediate `approval.granted` / `approval.denied`.

Queue-resolution approval events have a different, durable-record shape and
remain dictionary payloads. This is intentional gradual migration, not a claim
that all approval events share one schema.

### 3. Explicit required versus best-effort sinks

Keep the existing `EventSink` protocol and synchronous transport. Extend
`EventLogger.add_sink` with a keyword-only `required` flag, defaulting to
`True` for compatibility. Required sink failures retain fail-fast propagation.
Best-effort failures are isolated, captured in a bounded `SinkFailure` snapshot,
and do not interrupt engine progress or prevent later delivery.

Bootstrap keeps SQLite required and registers JSONL as best effort.

## Compatibility and migration

- `AuditEvent(detail={...})`, positional `EventLogger(sinks=[...])`, and
  `add_sink(sink)` remain valid.
- Existing SQLite and scheduler rows are read without schema migration.
- Existing JSONL objects remain ordinary envelope dictionaries.
- New typed details are converted before serialization; no typed class name is
  persisted.
- Legacy authorization rows may contain `params`; new rows use `param_keys`.
  This is an intentional data-minimization change. Consumers never depended on
  parameter values.
- Required sink exceptions still propagate with their original exception type.

## Test strategy

Tests protect these invariants:

1. raw dictionaries remain accepted and are copied;
2. invalid mapping keys/non-JSON values fail at construction;
3. typed authorization details validate and normalize to the documented shape;
4. SQLite typed-payload round trips and manually inserted legacy rows load;
5. current and legacy JSONL dictionaries load through the compatibility adapter;
6. best-effort sink failure does not block later sinks or engine progress;
7. required sink failure retains fail-fast behavior;
8. core permission events contain stable fields and parameter names only;
9. existing audit, authorization, memory, scheduler, and full regression tests remain green.

## Explicit deferrals

- Typed models for all event kinds or a repository-wide producer migration.
- Changing the scheduler telemetry schema or transactional insertion path.
- Async dispatch, queues, brokers, Kafka, Redis Streams, NATS, or distributed
  delivery guarantees.
- Trace/span propagation and replay cursors.
- Event upcasting framework or destructive database migrations.
- Sink retries, circuit breakers, background workers, or dead-letter storage.
- Provider error-body sanitization (separate security review).

## Verification

- Before implementation: **1,304 passed, 2 skipped**.
- ADR-033 contract tests: **14 passed**.
- Focused audit, authorization, approval, memory, scheduler, mutation, JSONL,
  and sink-failure regressions: **196 passed**.
- Complete suite after implementation: **1,318 passed, 2 skipped**.
