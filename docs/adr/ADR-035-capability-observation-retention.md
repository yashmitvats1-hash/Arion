# ADR-035 — Capability Observation and Durable Retention Boundary

- **Status:** Approved and implemented (2026-08-21)
- **Scope:** Successful capability result normalization and HTTP response metadata minimization

## Context

Arion's capability protocol declares
`execute(action, params) -> dict[str, Any]`, but before this ADR the engine
accepted that dictionary by reference with no construction-time persistence
contract. Verification read it, `PlanStep.result` retained it, and task plus
checkpoint snapshots serialized it later.

The Phase 27 audit verified the complete flow:

```text
Capability.execute()
  -> raw observation dict (same object)
  -> PlanStep.result
  -> verification
  -> current full Task snapshot
  -> immutable full checkpoint after each step
  -> final full checkpoint
  -> restart/resume
```

Audit events persist observation key names only. Scheduler ownership/telemetry
persists no observation values. Episodic memory deliberately excludes successful
`PlanStep.result`; reflections, consolidation, guidance, planning context, and
cognition therefore receive summary metadata rather than raw content.

The pre-change full suite passed with **1,327 tests and 2 skips**.

## Concrete findings

### Built-in result shapes

- `filesystem.read/read`: metadata plus complete UTF-8 content, file input capped
  at 1 MB.
- `filesystem.read/list`: directory entries with no aggregate count/size bound.
- `http.get/get`: final URL, status, complete response headers, and body; body
  capped at 1 MB, headers unfiltered and aggregate-unbounded.
- `git.log/log`: at most 500 reflog entries and 200 characters per message.
- `git.log/branches`: branch/ref list without an aggregate observation bound.
- write/append: bounded mutation inputs and metadata-only results.
- injected/future capabilities: arbitrary dictionary values and sizes.

### Demonstrated persistence growth

An injected capability returning a 2,000,029-character body completed normally.
The current task snapshot was about 2.0 MB and result-bearing checkpoints copied
it twice; total durable task/checkpoint snapshots were about 6.0 MB for one
step. Multi-step full snapshots recopy earlier results into every later
checkpoint, so retained result volume can grow cumulatively toward quadratic
behavior.

An authorized HTTP result with a 600 KB body and a 100 KB header produced a
~701 KB task snapshot plus ~1.4 MB of result-bearing checkpoints. An
`Authorization` response header and `Set-Cookie` value were retained in the
task and checkpoints even though no execution, verification, audit, or memory
consumer needed them.

A query-token-bearing URL was also retained in task params/results,
authorization audit metadata, and memory resource metadata. Unlike response
headers, the original request URL is execution input needed for durable retry
and resume. Removing it requires a resource-secret reference design and is not
silently attempted here.

## Data classes and ownership

1. **Internal metadata:** status, IDs, counts, sizes, timing, and verification
   fields. Small and intentionally durable.
2. **Capability observation:** authorized filesystem/Git/HTTP content. Owned by
   task execution and durable recovery; not automatically knowledge.
3. **Transport metadata:** HTTP headers and complete URLs. Only a bounded safe
   diagnostic header subset belongs in durable result state.
4. **Durable knowledge:** explicit episode/reflection/cognition structures.
   Successful raw observations are not promoted automatically.

## Decision

### 1. Compatible observation normalization

Add one `normalize_observation()` boundary in the capability layer. It:

- continues accepting ordinary mapping results;
- requires a JSON object with string keys at every nested mapping level;
- converts through JSON to a detached, canonical snapshot rather than retaining
  capability-owned mutable objects;
- rejects non-serializable/cyclic values before assignment to `PlanStep.result`;
- enforces a fixed maximum durable encoded observation size;
- raises `ObservationContractError`, a `CapabilityError` subtype.

The engine normalizes immediately after `execute()` and before verification.
For a non-retry-safe mutation, a result-contract failure follows the existing
mutation-recovery path because the side effect may already have occurred.

The budget is measured using the same JSON representation used by current
SQLite task/checkpoint snapshots. It is intentionally high enough to preserve
the existing 1 MB filesystem/HTTP payload contracts, including JSON escaping,
while making arbitrary future results finite. Per-action budgets are deferred.

### 2. HTTP response header allowlist

`http.get` retains only bounded, canonical lower-case diagnostic headers:

- `cache-control`
- `content-length`
- `content-type`
- `etag`
- `expires`
- `last-modified`

Authorization, proxy authorization, cookies, authentication challenges, and
all unrecognized extension headers are omitted from the returned observation.
Header values are one-line and length-bounded. Body, status, and final URL
behavior remain unchanged.

### 3. Keep observation separate from knowledge

No memory/cognition promotion API is added. The existing boundary already
excludes successful raw results, so this ADR documents and tests it rather than
inventing a new subsystem.

## Compatibility

- Capability implementations may continue returning dictionaries.
- Built-in filesystem, Git, write, append, and HTTP body/status behavior remains
  available.
- Existing persisted tasks/checkpoints load unchanged; normalization applies
  only to newly executed observations.
- No database migration or new persistence table is introduced.
- Sensitive HTTP header omission is an intentional minimization of metadata
  that had no in-repository consumer.
- Oversized/invalid new results now fail as typed capability failures instead
  of crashing later during task persistence.

## Test strategy

Tests protect:

1. mapping compatibility, deep snapshotting, canonical JSON, and nested key
   validation;
2. explicit encoded-size rejection;
3. engine handling of oversized and non-serializable injected observations;
4. non-retry-safe mutation recovery after an invalid result;
5. HTTP body/status usability with sensitive/unrecognized headers removed and
   safe headers bounded;
6. task/checkpoint persistence contains no sensitive HTTP response headers;
7. raw successful content remains absent from memory/cognition summaries;
8. legacy task/checkpoint result dictionaries remain readable;
9. filesystem/Git/HTTP/resume and full regression suites remain green.

## Explicit deferrals

- Delta checkpoints, content-addressed blobs, result references, compression,
  distributed object storage, and encrypted result storage.
- Per-capability/action result budgets and streaming observations.
- URL query-secret handling and credential references for durable retry/resume.
- Sanitizing plan parameters and intentional authorization resource records.
- Full DLP/content classification, PII scanning, and automatic knowledge
  extraction/promotion.
- Changing authorized HTTP/file body retention semantics.

## Verification

- Before implementation: **1,327 passed, 2 skipped**.
- ADR-035 observation-retention tests: **8 passed**.
- Broad focused capability, HTTP, persistence, scheduler-resume, memory, event,
  and error-boundary regressions: **139 passed, 1 skipped**.
- Complete suite after implementation: **1,335 passed, 2 skipped**.
